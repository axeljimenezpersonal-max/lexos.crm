"""
Flujo de una pregunta al asistente:
  1. Modo privado o sin modelo → solo búsqueda local (nada sale del servidor).
  2. El modelo rápido decide si es legal y reformula la pregunta en términos jurídicos.
  3. Se busca en la base legal de México y en la memoria del despacho.
  4. El modelo principal responde citando únicamente esas fuentes con [n].
"""
from __future__ import annotations

import logging
from typing import Iterator

from modules.asistente import almacen
from modules.asistente import proveedor as proveedores
from modules.corpus import indice as corpus
from modules.insights import casos as precedentes
from modules.lector import indice as memoria

log = logging.getLogger(__name__)
MAX_TEXTO_FUENTE = 1800
HISTORIAL = 6

SISTEMA = """Eres {nombre}, el asistente jurídico de Habeas Corpus para abogados y despachos en México.

Solo ayudas con derecho mexicano y práctica jurídica: litigio, contratos, plazos y términos, trámites, estrategia y
los asuntos y documentos del despacho. Si te piden algo que no es legal, dilo con amabilidad en una frase y ofrece
ayudar con algo jurídico.

Cómo respondes:
- En español, claro y directo, como un colega abogado con experiencia. Ve al punto.
- Fundamenta con las FUENTES numeradas que se te entregan. Cada afirmación jurídica lleva su cita [n] al final de
  la oración. Nunca cites algo que no esté en las fuentes.
- No inventes artículos, tesis, jurisprudencia, criterios, plazos ni números de expediente. Si las fuentes no
  alcanzan para responder con seguridad, dilo y explica qué habría que revisar.
- Distingue lo que dice la ley de tu análisis o recomendación.
- Si una fuente indica fecha de última reforma, tómala en cuenta y menciónala cuando importe.
- Las fuentes marcadas como "Documento del despacho" son confidenciales: úsalas solo para responder esta consulta.
- Las fuentes "Precedente del despacho" son asuntos que el despacho ya concluyó, con su resultado y lo que
  funcionó. Úsalas para decir cómo le ha ido al despacho en casos parecidos ("en dos asuntos parecidos se ganó
  con…"). No son ley ni jurisprudencia: no las presentes como criterio obligatorio.
- Solo advierte que tu respuesta no sustituye el criterio del abogado cuando haya un riesgo real (plazos, penal,
  consecuencias graves), no en cada respuesta.

Formato: párrafos cortos; listas con guiones cuando ayuden; **negritas** solo para lo esencial."""


def _evento(tipo: str, **datos) -> dict:
    return {"tipo": tipo, **datos}


def _precedentes(app, tdb, consulta: str, asunto_id, para_modelo: bool) -> list[dict]:
    """Asuntos concluidos parecidos. Al modelo nunca van asuntos privados ni el título (puede traer nombres)."""
    try:
        if asunto_id:
            fichas = precedentes.similares(app, tdb, asunto_id, limite=3)["casos"]
            fichas = [f for f in fichas if not (para_modelo and f["privado"])]
        else:
            fichas = precedentes.por_texto(app, tdb, consulta, limite=2, incluir_privados=not para_modelo)
    except Exception as exc:
        log.warning("Asistente: no se pudieron buscar precedentes (%s)", type(exc).__name__)
        return []
    salida = []
    for f in fichas:
        etiqueta = f"Asunto {f['numero_expediente'] or '#' + str(f['id'])}"
        salida.append({
            "tipo": "caso", "titulo": etiqueta, "titulo_visible": f["titulo"],
            "referencia": f["resultado_info"]["label"] if f["resultado_info"] else "Concluido",
            "url": f"/asuntos/{f['id']}", "detalle": "Precedente del despacho",
            "texto": precedentes.texto_para_modelo(f),
        })
    return salida


def _entidad_del_asunto(tdb, asunto_id) -> str | None:
    """El estado del asunto, leído del juzgado ("Juzgado Tercero de lo Familiar de la CDMX" → Ciudad de México)."""
    if not asunto_id:
        return None
    try:
        from modules.corpus.estatal import entidad_en
        fila = tdb.execute("SELECT juzgado FROM asuntos WHERE id=?", (asunto_id,)).fetchone()
        return entidad_en(fila[0]) if fila else None
    except Exception:
        return None


def _fuente_legal(r: dict) -> dict:
    """Un resultado de la base legal como fuente citable."""
    tipo = {"ley_federal": "ley", "dof": "dof"}.get(r["fuente"], r["fuente"])
    if r["fuente"] == "tesis":
        detalle = " · ".join(p for p in (r.get("detalle"), f"registro digital {r['clave']}") if p)
        referencia = r.get("clave_tesis") or r["referencia"]
    elif r["fuente"] == "ley_estatal":
        detalle = (f"{r.get('entidad')} · última reforma {r['ultima_reforma'] or 'no indicada'}, "
                   f"según el Orden Jurídico Nacional")
        referencia = r["referencia"]
    elif r["fuente"] == "dof":
        detalle, referencia = f"DOF {r['fecha_publicacion']}", r["referencia"]
    else:
        detalle = f"Última reforma DOF {r['ultima_reforma'] or r['fecha_publicacion'] or 's/f'}"
        referencia = r["referencia"]
    return {"tipo": tipo, "titulo": r["titulo"], "referencia": referencia, "url": r["url_oficial"],
            "detalle": detalle, "texto": r["texto"][:MAX_TEXTO_FUENTE]}


def _buscar(app, config, slug, tdb, consulta: str, extra: str, asunto_id, para_modelo: bool = True) -> list[dict]:
    fuentes: list[dict] = []
    vistos = set()
    entidad = _entidad_del_asunto(tdb, asunto_id)
    for q in [c for c in (consulta, extra) if c]:
        for r in corpus.buscar(config, q, limite=5, preferir_entidad=entidad):
            clave = ("ley", r["id"])
            if clave not in vistos:
                vistos.add(clave)
                fuentes.append(_fuente_legal(r))
    # Solo documentos que esta persona puede ver (un abogado no ve los expedientes de otros equipos)
    from modules.lector import documentos_visibles
    visibles = set(documentos_visibles(tdb))
    for r in memoria.buscar(app, slug, tdb, consulta, limite=5, asunto_id=asunto_id, documentos=visibles):
        clave = ("doc", r["fragmento_id"])
        if clave not in vistos:
            vistos.add(clave)
            fuentes.append({
                "tipo": "despacho", "titulo": r["documento"], "referencia": f"página {r['pagina'] or 1}",
                "url": r["url"], "detalle": "Documento del despacho", "texto": r["texto"][:MAX_TEXTO_FUENTE],
            })
    return fuentes[:10] + _precedentes(app, tdb, consulta, asunto_id, para_modelo)


def _bloque_fuentes(fuentes: list[dict]) -> str:
    partes = []
    for i, f in enumerate(fuentes, start=1):
        etiqueta = {"despacho": "Documento del despacho", "dof": "Diario Oficial de la Federación",
                    "caso": "Precedente del despacho", "reglamento": "Reglamento federal",
                    "ley_estatal": "Ley estatal", "tesis": "Tesis del Semanario Judicial"}.get(f["tipo"], "Ley federal")
        partes.append(f"[{i}] {etiqueta} — {f['titulo']}, {f['referencia']} ({f['detalle']})\n{f['texto']}")
    return "\n\n".join(partes) if partes else "(No se encontraron fuentes relevantes.)"


def publicas(fuentes: list[dict]) -> list[dict]:
    """Lo que se manda al navegador y se guarda (cifrado): solo un extracto, no el texto completo."""
    return [{**{k: f[k] for k in ("tipo", "titulo", "referencia", "url", "detalle")},
             "titulo": f.get("titulo_visible") or f["titulo"],
             "extracto": " ".join(f["texto"].split())[:280]} for f in fuentes]


def responder(app, config, slug: str, tdb, pregunta: str, historial: list[dict],
              asunto: dict | None, privado: bool) -> Iterator[dict]:
    asunto_id = asunto["id"] if asunto else None
    proveedor = None if privado else proveedores.obtener(config)

    if proveedor is None:
        fuentes = _buscar(app, config, slug, tdb, pregunta, "", asunto_id, para_modelo=False)
        yield _evento("fuentes", fuentes=publicas(fuentes))
        if privado:
            texto = ("**Modo privado:** esta consulta no salió del servidor y no se usó ningún modelo externo. "
                     "Estos son los pasajes más relevantes que encontré en la ley y en los documentos del despacho:")
        else:
            texto = ("El asistente todavía no tiene conectado el modelo de lenguaje, así que no puedo redactar una "
                     "respuesta. Estos son los pasajes más relevantes que encontré:")
        if not fuentes:
            texto = texto.rsplit(" Estos son", 1)[0] + " No encontré pasajes relevantes."
        yield _evento("texto", texto=texto)
        yield _evento("fin", modelo=None)
        return

    contexto = "\n".join(f"{m['rol']}: {m['texto'][:400]}" for m in historial[-4:])
    try:
        plan = proveedor.reformular(pregunta, contexto)
    except Exception as exc:
        log.warning("Asistente: falló la reformulación (%s)", type(exc).__name__)
        plan = {"es_legal": True, "consulta": pregunta, "terminos": [], "materia": ""}

    if not plan["es_legal"]:
        yield _evento("fuentes", fuentes=[])
        yield _evento("texto", texto="Solo puedo ayudarte con temas legales y con los asuntos del despacho. "
                                     "¿Hay algo jurídico en lo que te pueda apoyar?")
        yield _evento("fin", modelo=proveedor.modelo_rapido)
        return

    fuentes = _buscar(app, config, slug, tdb, plan["consulta"], " ".join(plan["terminos"]) or pregunta, asunto_id)
    yield _evento("fuentes", fuentes=publicas(fuentes))

    sistema = SISTEMA.format(nombre=config.get("ASISTENTE_NOMBRE", "Dictum"))
    mensajes: list[dict] = []
    for m in historial[-HISTORIAL:]:
        rol = "user" if m["rol"] == "usuario" else "assistant"
        if not mensajes and rol == "assistant":
            continue
        if mensajes and mensajes[-1]["role"] == rol:
            mensajes[-1]["content"] += "\n\n" + m["texto"]
        else:
            mensajes.append({"role": rol, "content": m["texto"]})
    if mensajes and mensajes[-1]["role"] == "user":
        mensajes.pop()
    encabezado = ""
    if asunto:
        encabezado = (f"Contexto: la consulta es sobre el asunto «{asunto['titulo']}»"
                      f"{' (expediente ' + asunto['numero_expediente'] + ')' if asunto.get('numero_expediente') else ''}"
                      f", materia {asunto.get('tipo_asunto') or 'no indicada'}.\n\n")
        recuerdos = almacen.hallazgos(tdb, slug, asunto_id, limite=20)
        if recuerdos:
            encabezado += ("MEMORIA DEL CASO (puntos clave y descubrimientos que el equipo guardó; tómalos en cuenta, "
                           "pero no los cites como fuente legal):\n"
                           + "\n".join(f"- [{almacen.TIPOS_HALLAZGO.get(h['tipo'], 'Punto clave')}] {h['texto'][:500]}"
                                        for h in reversed(recuerdos)) + "\n\n")
    mensajes.append({"role": "user", "content": f"{encabezado}FUENTES:\n{_bloque_fuentes(fuentes)}\n\nPREGUNTA: {pregunta}"})
    try:
        for trozo in proveedor.responder(sistema, mensajes):
            yield _evento("texto", texto=trozo)
    except Exception as exc:
        log.warning("Asistente: falló la respuesta (%s)", type(exc).__name__)
        yield _evento("error", mensaje="No pude completar la respuesta. Intenta de nuevo en un momento.")
    yield _evento("fin", modelo=proveedor.modelo)
