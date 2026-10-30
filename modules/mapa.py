"""
Habeas Corpus — modules/mapa.py
Mapa del expediente: una representación del caso que se recorre por tiempo, personas, documentos
y puntos por revisar, donde cada elemento lleva a su fuente. Por ahora se arma con lo que el equipo
registra (agenda, actividad, documentos, resoluciones, cobros y datos generales) y con reglas.
Además Dictum puede leer los documentos y sugerir hechos, plazos, personas y contradicciones. Cada
sugerencia trae la cita literal del documento; si la cita no aparece tal cual en el texto, se descarta.
Nada entra al mapa hasta que alguien del equipo la confirma.
"""

import hashlib
import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from flask import current_app, g, url_for

log = logging.getLogger(__name__)

TERMINOS = ("plazo", "vencimiento", "fecha_limite")
DEL_JUICIO_EVENTO = ("audiencia", "diligencia", *TERMINOS)
DEL_JUICIO_ACTIVIDAD = ("audiencia", "acuerdo", "diligencia", "vencimiento")
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
SIN_JUZGADO = ("notarial", "corporativo")
CLASES_ACTO = {
    "demanda": "Demanda", "contestacion": "Contestación", "promocion": "Promoción", "acuerdo": "Acuerdo",
    "auto": "Auto", "notificacion": "Notificación", "emplazamiento": "Emplazamiento", "audiencia": "Audiencia",
    "diligencia": "Diligencia", "prueba": "Prueba", "sentencia": "Sentencia", "recurso": "Recurso",
    "amparo": "Amparo", "contrato": "Contrato", "convenio": "Convenio", "pago": "Pago",
    "escritura": "Escritura", "requerimiento": "Requerimiento", "reunion": "Reunión", "otro": "Otro acto",
}
DE_GESTION = ("contrato", "convenio", "pago", "escritura", "reunion")


def carril_de(clase: str) -> str:
    return "gestion" if clase in DE_GESTION else "juicio"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS asunto_mapa_revisados (
    asunto_id  INTEGER NOT NULL,
    clave      TEXT    NOT NULL,
    user_id    INTEGER,
    fecha      DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (asunto_id, clave)
);
CREATE TABLE IF NOT EXISTS asunto_mapa_sugerencias (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id     INTEGER NOT NULL,
    clave         TEXT    NOT NULL,
    tipo          TEXT    NOT NULL,
    fecha         TEXT,
    datos_cifrados BLOB   NOT NULL,
    estado        TEXT    NOT NULL DEFAULT 'sugerido',
    revisado_por  INTEGER,
    revisado_el   DATETIME,
    creado_el     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (asunto_id, clave)
);
CREATE TABLE IF NOT EXISTS asunto_actos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id      INTEGER NOT NULL,
    fecha          TEXT    NOT NULL,
    clase          TEXT    NOT NULL DEFAULT 'otro',
    titulo         TEXT    NOT NULL,
    detalle        TEXT,
    documento_url  TEXT,
    documento      TEXT,
    pagina         INTEGER,
    origen         TEXT    NOT NULL DEFAULT 'equipo',
    sugerencia_id  INTEGER,
    creado_por     INTEGER,
    creado_el      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_asunto_actos ON asunto_actos(asunto_id, fecha);
CREATE TABLE IF NOT EXISTS asunto_mapa_lecturas (
    asunto_id   INTEGER PRIMARY KEY,
    estado      TEXT    NOT NULL,
    documentos  INTEGER DEFAULT 0,
    nuevas      INTEGER DEFAULT 0,
    descartadas INTEGER DEFAULT 0,
    recortado   INTEGER DEFAULT 0,
    mensaje     TEXT,
    modelo      TEXT,
    user_id     INTEGER,
    inicio      DATETIME,
    fin         DATETIME
);
"""


def asegurar(tdb) -> None:
    tdb.executescript(ESQUEMA)


def marcar(tdb, asunto_id: int, clave: str, user_id: int, revisado: bool) -> None:
    asegurar(tdb)
    if revisado:
        tdb.execute("INSERT OR REPLACE INTO asunto_mapa_revisados (asunto_id, clave, user_id) VALUES (?,?,?)",
                    (asunto_id, clave[:80], user_id))
    else:
        tdb.execute("DELETE FROM asunto_mapa_revisados WHERE asunto_id=? AND clave=?", (asunto_id, clave[:80]))
    tdb.commit()


def _dia(f: date) -> dict:
    return {"iso": f.isoformat(), "dia": f.day, "mes": MESES[f.month - 1], "anio": f.year}


def construir(tdb, asunto: dict, usuarios: dict, responsable, colaboradores: list, documentos: list,
              sentencias: list, ficha, sin_mov: bool, abierto: bool, vencido: float) -> dict:
    from modules.asuntos import ACTIVIDAD_LABEL, EVENTO_LABEL, SENTENCIA_LABEL, _fecha, _hoy, _local

    aid = asunto["id"]
    hoy = _hoy()
    agenda_url = url_for("calendario.index", asunto=aid)

    def doc_url(url: str | None) -> str | None:
        return url if url and url.startswith("/archivos/") else None

    # ── Cronología: lo del juicio y las gestiones, cada cosa con su fuente ──
    cronologia = []

    def evento(f, titulo, carril, tipo, fuente, url=None, ir=None, **extra):
        if not f:
            return
        cronologia.append({**_dia(f), "fecha": f, "titulo": titulo, "carril": carril, "tipo": tipo,
                           "fuente": fuente, "url": url, "ir": ir, "futuro": f > hoy, **extra})

    # Actos: los que registró el equipo y los que Dictum leyó en los documentos y alguien confirmó
    asegurar(tdb)
    slug = g.tenant_slug
    con_acto = set()
    for a in tdb.execute("SELECT * FROM asunto_actos WHERE asunto_id=? ORDER BY fecha, id", (aid,)):
        if a["documento_url"]:
            con_acto.add(a["documento_url"])
        fuente = (f"«{a['documento']}»" + (f" · p. {a['pagina']}" if a["pagina"] else "")) if a["documento"] else "Registrado por el equipo"
        evento(_fecha(a["fecha"]), a["titulo"], carril_de(a["clase"]), CLASES_ACTO.get(a["clase"], "Acto"), fuente,
               url=doc_url(a["documento_url"]), acto_id=a["id"], origen=a["origen"], detalle=a["detalle"])

    inicio = _fecha(asunto.get("fecha_inicio") or asunto.get("fecha_alta"))
    evento(inicio, "Se abre el expediente", "gestion", "Inicio", "Datos generales", ir="datos")

    agendados = set()
    for e in tdb.execute("SELECT id, titulo, tipo, fecha_inicio, estado FROM eventos_calendario "
                         "WHERE asunto_id=? AND estado != 'cancelado' ORDER BY fecha_inicio", (aid,)):
        f = _fecha(e["fecha_inicio"])
        agendados.add((e["titulo"], (e["fecha_inicio"] or "")[:10]))
        evento(f, e["titulo"], "juicio" if e["tipo"] in DEL_JUICIO_EVENTO else "gestion",
               EVENTO_LABEL.get(e["tipo"], "Evento"), "Agenda", url=agenda_url)
    for a in tdb.execute("SELECT titulo, tipo, fecha FROM asunto_actividades WHERE asunto_id=? ORDER BY fecha", (aid,)):
        if (a["titulo"], (a["fecha"] or "")[:10]) in agendados:
            continue
        evento(_fecha(a["fecha"]), a["titulo"], "juicio" if a["tipo"] in DEL_JUICIO_ACTIVIDAD else "gestion",
               ACTIVIDAD_LABEL.get(a["tipo"], "Actividad"), "Actividad", ir="actividad")
    for s in sentencias:
        evento(_fecha(s.get("fecha_emision") or s.get("fecha_alta")), s["titulo"], "juicio",
               SENTENCIA_LABEL.get(s["tipo"], "Resolución"), "Resolución",
               url=doc_url(s.get("archivo_url")), ir=None if doc_url(s.get("archivo_url")) else "documentos")
    for d in documentos:
        if d.get("archivo_url") in con_acto:
            continue   # el documento ya está en la cronología con la fecha real de su acto
        evento(_fecha(d.get("fecha_subida")), f"Se sube «{d['nombre']}» al sistema", "gestion", "Documento", "Documento",
               url=doc_url(d.get("archivo_url")), ir=None if doc_url(d.get("archivo_url")) else "documentos")
    for c in tdb.execute("SELECT concepto, total, monto, fecha_emision, estado FROM cobros "
                         "WHERE asunto_id=? AND estado != 'cancelado'", (aid,)):
        total = c["total"] or c["monto"] or 0
        evento(_fecha(c["fecha_emision"]), f"Cobro: {c['concepto'] or 'honorarios'} (${total:,.0f})", "gestion",
               "Cobro", "Cobranza", url=url_for("cobranzas.index", asunto=aid, grupo="todos"))
    if not abierto:
        evento(_fecha(asunto.get("fecha_cierre")), "Se cierra el expediente", "gestion", "Cierre", "Datos generales",
               ir="datos")
    cronologia.sort(key=lambda x: x["fecha"])

    # ── Personas y autoridades ──
    personas = []
    if asunto.get("cliente"):
        personas.append({"nombre": asunto["cliente"], "rol": "Cliente", "clase": "cliente",
                         "url": url_for("clientes.detalle", id=asunto["cliente_id"]) if asunto.get("cliente_id") else None})
    if asunto.get("contrario_nombre"):
        personas.append({"nombre": asunto["contrario_nombre"], "rol": "Contraparte", "clase": "contra",
                         "url": url_for("contrapartes.index", q=asunto["contrario_nombre"])})
    if asunto.get("contrario_abogado"):
        personas.append({"nombre": asunto["contrario_abogado"], "rol": "Abogado de la contraparte", "clase": "contra",
                         "detalle": asunto.get("contrario_despacho") or ""})
    if asunto.get("juzgado"):
        personas.append({"nombre": asunto["juzgado"], "rol": "Autoridad", "clase": "autoridad",
                         "detalle": f"Juicio {asunto['numero_juicio']}" if asunto.get("numero_juicio") else ""})
    if responsable:
        personas.append({"nombre": responsable["nombre"], "rol": "Responsable", "clase": "equipo"})
    for c in colaboradores:
        personas.append({"nombre": c["nombre"], "rol": "Colabora", "clase": "equipo"})
    todas = sugerencias(tdb, slug, aid)
    confirmadas = [x for x in todas if x["estado"] == "confirmado"]
    for x in confirmadas:
        if x["tipo"] == "persona":
            autoridad = re.search(r"juez|juzgad|tribunal|magistrad|secretari|notari|ministerio|autoridad|junta|sala",
                                  (x.get("rol") or "").lower())
            personas.append({"nombre": x["titulo"], "rol": x.get("rol") or "Interviene",
                             "clase": "autoridad" if autoridad else "dictum", "detalle": _cita_corta(x)})

    # ── Documentos ──
    docs = [{"nombre": d["nombre"], "tipo": (d.get("ext") or "doc").upper(), "dia": d.get("dia", ""),
             "url": doc_url(d.get("archivo_url"))} for d in documentos]
    docs += [{"nombre": s["titulo"], "tipo": "RES", "dia": s.get("dia", ""), "url": doc_url(s.get("archivo_url"))}
             for s in sentencias]

    # ── Puntos por revisar: reglas sobre lo registrado ──
    puntos = []

    def punto(clave, nivel, titulo, detalle, fuente, url=None, ir=None):
        puntos.append({"clave": clave, "nivel": nivel, "titulo": titulo, "detalle": detalle,
                       "fuente": fuente, "url": url, "ir": ir})

    proximos = []
    for e in tdb.execute("SELECT id, titulo, tipo, fecha_inicio FROM eventos_calendario "
                         "WHERE asunto_id=? AND estado='pendiente'", (aid,)):
        f = _fecha(e["fecha_inicio"])
        if not f:
            continue
        if f >= hoy:
            proximos.append(f)
        if e["tipo"] in TERMINOS and f < hoy:
            punto(f"venc:{e['id']}", "alto", f"Término vencido: {e['titulo']}",
                  f"Venció el {f.day} {MESES[f.month - 1]} y sigue pendiente en la agenda. Confirma si se cumplió.",
                  "Agenda", url=agenda_url)
        elif e["tipo"] in TERMINOS and f <= hoy + timedelta(days=3):
            dias = (f - hoy).days
            punto(f"pronto:{e['id']}:{f.isoformat()}", "alto", f"Vence {'hoy' if dias == 0 else 'mañana' if dias == 1 else f'en {dias} días'}: {e['titulo']}",
                  "Revisa que el escrito esté listo y presentado a tiempo.", "Agenda", url=agenda_url)
    if abierto and not proximos:
        punto("sin_agenda", "medio", "No hay nada agendado",
              "El expediente está abierto pero no tiene audiencias, términos ni citas por venir.", "Agenda", url=agenda_url)

    litigio = (asunto.get("tipo_asunto") or "") not in SIN_JUZGADO
    if litigio and not asunto.get("juzgado"):
        punto("falta:juzgado", "medio", "Falta el juzgado o la autoridad",
              "Sin autoridad no se puede saber dónde corre el juicio ni calcular bien los términos.", "Datos generales", ir="datos")
    if litigio and not asunto.get("numero_juicio"):
        punto("falta:numero_juicio", "bajo", "Falta el número de juicio",
              "Es el que se usa para buscar los acuerdos en el boletín.", "Datos generales", ir="datos")
    amparo = (asunto.get("tipo_asunto") or "") == "amparo"
    if litigio and not amparo and not asunto.get("contrario_nombre"):
        punto("falta:contraparte", "bajo", "Falta la contraparte", "Regístrala para tener su historial con el despacho.",
              "Datos generales", ir="datos")
    elif asunto.get("contrario_nombre") and not asunto.get("contrario_abogado"):
        punto("falta:abogado_contrario", "bajo", "Falta el abogado de la contraparte",
              "Ayuda a anticipar cómo litigan y a encontrar casos parecidos.", "Datos generales", ir="datos")
    if not responsable:
        punto("falta:responsable", "medio", "Nadie es responsable del expediente",
              "Asigna a alguien para que reciba los avisos de términos y audiencias.", "Partes y equipo", ir="resumen")
    if not documentos and not sentencias:
        punto("sin_documentos", "medio", "El expediente no tiene documentos",
              "Sube la demanda, los acuerdos y las pruebas para que el mapa y Dictum tengan de dónde partir.",
              "Documentos", ir="documentos")
    try:
        urls = [d["archivo_url"] for d in documentos if d.get("archivo_url")]
        if urls:
            marcas = ",".join("?" * len(urls))
            for r in tdb.execute(f"SELECT id, nombre, estado FROM lector_documentos WHERE archivo_url IN ({marcas}) "
                                 "AND estado IN ('requiere_ocr', 'sin_texto', 'error')", urls):
                punto(f"lectura:{r['id']}", "medio", f"No se pudo leer «{r['nombre']}»",
                      "Parece escaneado o sin texto: Dictum no puede buscar dentro de él hasta que se procese.",
                      "Documentos", ir="documentos")
    except Exception:
        pass   # el lector aún no ha visto documentos de este despacho
    if sin_mov:
        punto(f"sin_movimiento:{hoy:%Y-%m}", "medio", "Sin movimiento en más de 30 días",
              "Revisa el boletín por si hubo acuerdos que no se registraron.", "Actividad", ir="actividad")
    if vencido:
        punto(f"cobro_vencido:{int(vencido)}", "medio", f"${vencido:,.0f} de honorarios vencidos",
              "Hay cobros de este expediente que ya pasaron su fecha.", "Cobranza",
              url=url_for("cobranzas.index", asunto=aid, grupo="vencidos"))
    for x in confirmadas:
        if x["tipo"] in ("contradiccion", "pendiente") or (x["tipo"] == "plazo" and not x.get("fecha")):
            c = x["citas"][0]
            punto(f"ia:{x['id']}", "alto" if x["tipo"] == "contradiccion" else "medio",
                  f"{x['tipo_label']}: {x['titulo']}", x.get("detalle") or "",
                  f"«{c['documento']}» · p. {c['pagina']}", url=doc_url(c["url"]), ir=None if doc_url(c["url"]) else "documentos")

    asegurar(tdb)
    revisados = {r["clave"]: r for r in tdb.execute(
        "SELECT clave, user_id, fecha FROM asunto_mapa_revisados WHERE asunto_id=?", (aid,))}
    pendientes, hechos = [], []
    for p in puntos:
        r = revisados.get(p["clave"])
        if r:
            p["revisado_por"] = (usuarios.get(r["user_id"]) or {}).get("corto", "Alguien del equipo")
            f = _fecha(r["fecha"])
            p["revisado_el"] = f"{f.day} {MESES[f.month - 1]}" if f else ""
            hechos.append(p)
        else:
            pendientes.append(p)
    orden = {"alto": 0, "medio": 1, "bajo": 2}
    pendientes.sort(key=lambda p: orden[p["nivel"]])

    # Embudo de Dictum: documentos leídos → sugerencias con cita verificada → confirmadas por el equipo
    from modules.asistente import proveedor
    try:
        from modules.lector import indice
        indice.asegurar_esquema(tdb)
        estados = dict(tdb.execute("SELECT estado, COUNT(*) FROM lector_documentos WHERE asunto_id=? GROUP BY estado",
                                   (aid,)).fetchall())
    except Exception:
        estados = {}
    por_confirmar = [x for x in todas if x["estado"] == "sugerido"]
    for x in por_confirmar:
        for c in x["citas"]:
            c["url_doc"] = doc_url(c["url"])
        if x.get("fecha"):
            x["dia"] = _dia(date.fromisoformat(x["fecha"]))
    dictum = {
        "conectada": proveedor.obtener(current_app.config) is not None,
        "privado": bool(asunto.get("privado")),
        "leidos": estados.get("listo", 0),
        "por_leer": estados.get("pendiente", 0) + estados.get("procesando", 0),
        "lectura": lectura(tdb, aid),
        "por_confirmar": por_confirmar,
        "confirmadas": len(confirmadas),
        "descartadas": sum(1 for x in todas if x["estado"] == "descartado"),
    }
    fin = _local((dictum["lectura"] or {}).get("fin"))
    dictum["cuando"] = f"{fin.day} {MESES[fin.month - 1]}" if fin else ""

    return {"cronologia": cronologia, "personas": personas, "documentos": docs,
            "puntos": pendientes, "revisados": hechos,
            "juicio": sum(1 for c in cronologia if c["carril"] == "juicio"),
            "hoy": _dia(hoy), "dictum": dictum, "clases": CLASES_ACTO,
            "para_actos": [d for d in docs if d["url"]]}


def _cita_corta(x: dict) -> str:
    c = x["citas"][0]
    return f"Según «{c['documento']}», p. {c['pagina']}"


# ═════════════════════════════════════════════════════════════════════════════
#  Lectura de Dictum: hechos sugeridos con cita verificable
# ═════════════════════════════════════════════════════════════════════════════

TIPOS_SUGERENCIA = {"hecho": "Hecho", "plazo": "Plazo", "persona": "Persona",
                    "contradiccion": "Contradicción", "pendiente": "Por atender"}
LIMITE_TEXTO = 160_000        # caracteres de documentos por lectura (~45 mil tokens)
MAX_SUGERENCIAS = 30
LECTURA_VENCIDA = timedelta(minutes=10)
_ejecutor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mapa-dictum")

SISTEMA_LECTURA = """Eres {nombre}, analista jurídico de un despacho en México. Lees los documentos de un expediente
y propones al equipo hechos, plazos, personas, contradicciones y puntos por atender para su mapa del caso.

Reglas estrictas:
- Usa SOLO lo que dicen los documentos. No supongas, no completes con conocimiento general, no inventes.
- Cada elemento lleva al menos una cita: copia el texto EXACTO del documento (entre 30 y 200 caracteres), sin
  cambiar palabras, acentos ni puntuación, e indica el número del documento. El sistema verifica cada cita contra el
  documento y descarta las que no coinciden.
- Primero caracteriza cada documento: registra como "hecho" el acto que el documento contiene (la demanda y su
  fecha de presentación, el acuerdo y su fecha, la sentencia y su fecha, el contrato y su fecha de firma…), con su
  "clase" y una cita donde aparezca esa fecha. Así se arma la cronología real del caso.
- "hecho": además del acto de cada documento, lo que el documento narra que ocurrió en una fecha concreta
  (emplazamiento, audiencia, pago, despido…). La fecha va en formato AAAA-MM-DD y solo si el documento la da
  completa. Sin fecha completa no es un hecho: omítelo o, si importa, regístralo como "pendiente".
- "plazo": un término o plazo que el documento concede o menciona. No calcules fechas de vencimiento: si el
  documento no da la fecha exacta, deja "fecha" vacía y explica el plazo en "detalle" (p. ej. "9 días hábiles para
  contestar a partir del emplazamiento").
- "persona": partes, jueces, secretarios, peritos, testigos, notarios, autoridades o empresas que intervienen, con
  su papel en "rol". No repitas a quien ya está registrado en el expediente.
- "contradiccion": dos documentos (o dos partes de uno) que dicen cosas incompatibles sobre fechas, montos,
  nombres o hechos. Lleva dos citas, una de cada lado.
- "pendiente": algo que el equipo debería revisar o atender (un requerimiento, una prueba que falta, un riesgo).
- "carril": "juicio" si es del procedimiento (actuaciones, audiencias, términos, resoluciones); "gestion" si es
  del asunto fuera del juzgado (contratos, pagos, reuniones, trámites).
- "titulo": una línea clara de máximo 90 caracteres. "detalle": máximo 2 frases.
- No repitas lo que ya está registrado en el sistema (te lo indico abajo).
- Prefiere pocos elementos útiles a muchos obvios. Máximo {maximo}.
- Escribe en español de México, en tono profesional."""

HERRAMIENTA = {
    "name": "registrar_hallazgos",
    "description": "Registra los hechos, plazos, personas, contradicciones y pendientes encontrados en los documentos.",
    "input_schema": {
        "type": "object",
        "properties": {"hallazgos": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": list(TIPOS_SUGERENCIA)},
                "titulo": {"type": "string"},
                "detalle": {"type": "string"},
                "fecha": {"type": ["string", "null"], "description": "AAAA-MM-DD o null"},
                "carril": {"type": "string", "enum": ["juicio", "gestion"]},
                "clase": {"type": "string", "enum": list(CLASES_ACTO), "description": "Para hechos y plazos"},
                "rol": {"type": ["string", "null"], "description": "Solo para personas"},
                "citas": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                    "type": "object",
                    "properties": {"documento": {"type": "integer"}, "texto": {"type": "string"}},
                    "required": ["documento", "texto"]}},
            },
            "required": ["tipo", "titulo", "carril", "citas"]}}},
        "required": ["hallazgos"],
    },
}


def _plano(texto: str) -> str:
    """Texto comparable: sin diferencias de mayúsculas, espacios, comillas ni guiones."""
    t = unicodedata.normalize("NFKC", texto or "").lower()
    t = t.translate(str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'",
                                   "–": "-", "—": "-", "­": ""}))
    return re.sub(r"\s+", " ", t).strip()


def _unir(a: str, b: str) -> str:
    """Pega dos fragmentos consecutivos quitando el traslape que deja el lector."""
    for k in range(min(len(a), len(b), 240), 15, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return a + " " + b


def _documentos(slug: str, conn, asunto_id: int) -> tuple[list[dict], int]:
    """Texto de los documentos ya leídos del expediente, por página. Devuelve (documentos, pendientes de leer)."""
    from modules import boveda
    from modules.lector import indice
    indice.asegurar_esquema(conn)
    filas = conn.execute("SELECT id, nombre, archivo_url, estado FROM lector_documentos WHERE asunto_id=? "
                         "ORDER BY fecha_alta, id", (asunto_id,)).fetchall()
    pendientes = sum(1 for d in filas if d["estado"] in ("pendiente", "procesando"))
    docs = []
    for d in filas:
        if d["estado"] != "listo":
            continue
        paginas: dict[int, str] = {}
        for f in conn.execute("SELECT pagina, texto_cifrado FROM lector_fragmentos WHERE documento_id=? ORDER BY orden",
                              (d["id"],)):
            p = f["pagina"] or 1
            texto = boveda.descifrar_texto(slug, f["texto_cifrado"])
            paginas[p] = _unir(paginas[p], texto) if p in paginas else texto
        if paginas:
            docs.append({"id": d["id"], "nombre": d["nombre"] or "Documento", "url": d["archivo_url"],
                         "paginas": sorted(paginas.items())})
    return docs, pendientes


def _verificar(cita: str, doc: dict) -> int | None:
    """Página donde aparece la cita tal cual, o None si no aparece."""
    buscada = _plano(cita).strip(' ."…')
    if len(buscada) < 12:
        return None
    for p, texto in doc["paginas"]:
        if buscada in _plano(texto):
            return p
    completo = _plano(" ".join(t for _, t in doc["paginas"]))
    if buscada in completo:
        return doc["paginas"][0][0]
    return None


def _fecha_valida(valor) -> str | None:
    try:
        f = date.fromisoformat(str(valor or "")[:10])
        return f.isoformat() if 1950 <= f.year <= date.today().year + 5 else None
    except ValueError:
        return None


def _clave(h: dict) -> str:
    base = f"{h['tipo']}|{h.get('fecha') or ''}|{_plano(h['titulo'])[:80]}"
    return hashlib.sha1(base.encode()).hexdigest()[:20]


def _registrado(conn, asunto: dict) -> str:
    """Lo que ya está en el sistema, para que Dictum no lo repita."""
    lineas = [f"Expediente: {asunto.get('titulo') or ''} · materia {asunto.get('tipo_asunto') or 'no indicada'}"]
    for campo, nombre in (("cliente", "Cliente"), ("contrario_nombre", "Contraparte"),
                          ("contrario_abogado", "Abogado contrario"), ("juzgado", "Autoridad"),
                          ("numero_juicio", "Número de juicio")):
        if asunto.get(campo):
            lineas.append(f"{nombre}: {asunto[campo]}")
    for e in conn.execute("SELECT titulo, fecha_inicio FROM eventos_calendario WHERE asunto_id=? "
                          "AND estado != 'cancelado' ORDER BY fecha_inicio LIMIT 60", (asunto["id"],)):
        lineas.append(f"Agenda: {(e['fecha_inicio'] or '')[:10]} {e['titulo']}")
    return "\n".join(lineas)


def _contenido(docs: list[dict], registrado: str) -> tuple[str, bool]:
    """Arma el mensaje con los documentos numerados, repartiendo el límite de texto entre todos."""
    total = sum(len(t) for d in docs for _, t in d["paginas"])
    recortado = total > LIMITE_TEXTO
    cuota = LIMITE_TEXTO // max(len(docs), 1)
    partes = []
    for d in docs:
        resto, bloques = (cuota if recortado else total), []
        for p, t in d["paginas"]:
            if resto <= 0:
                break
            bloques.append(f"[p. {p}] {t[:resto]}")
            resto -= len(t)
        partes.append(f"<documento numero=\"{d['id']}\" nombre=\"{d['nombre']}\">\n" + "\n".join(bloques) + "\n</documento>")
    hoy = date.today().isoformat()
    return (f"Hoy es {hoy}.\n\nYA REGISTRADO EN EL SISTEMA:\n{registrado}\n\nDOCUMENTOS DEL EXPEDIENTE:\n\n"
            + "\n\n".join(partes)), recortado


def lectura(tdb, asunto_id: int) -> dict | None:
    asegurar(tdb)
    r = tdb.execute("SELECT * FROM asunto_mapa_lecturas WHERE asunto_id=?", (asunto_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    if r["estado"] == "leyendo":
        inicio = datetime.fromisoformat(r["inicio"]) if r["inicio"] else None
        if not inicio or datetime.utcnow() - inicio > LECTURA_VENCIDA:
            r.update(estado="error", mensaje="La lectura anterior no terminó. Vuelve a intentarlo.")
    return r


def iniciar(app, slug: str, tdb, asunto: dict, uid) -> tuple[bool, str]:
    """Pone a Dictum a leer el expediente en segundo plano."""
    from modules.asistente import proveedor
    if asunto.get("privado"):
        return False, "Este expediente es privado: sus documentos no salen del servidor."
    if proveedor.obtener(app.config) is None:
        return False, "Falta conectar la IA."
    previa = lectura(tdb, asunto["id"])
    if previa and previa["estado"] == "leyendo":
        return False, "Dictum ya está leyendo este expediente."
    docs, _ = _documentos(slug, tdb, asunto["id"])
    if not docs:
        return False, "Todavía no hay documentos leídos en este expediente."
    tdb.execute("INSERT OR REPLACE INTO asunto_mapa_lecturas (asunto_id, estado, documentos, user_id, inicio) "
                "VALUES (?, 'leyendo', ?, ?, ?)", (asunto["id"], len(docs), uid, datetime.utcnow().isoformat(" ", "seconds")))
    tdb.commit()
    _ejecutor.submit(_leer, app, slug, asunto["id"])
    return True, f"Dictum está leyendo {len(docs)} documento{'s' if len(docs) != 1 else ''}."


def _leer(app, slug: str, asunto_id: int) -> None:
    from modules import boveda
    from modules.asistente import proveedor
    from modules.lector.indice import _conexion
    with app.app_context():
        conn = _conexion(slug)
        try:
            asunto = dict(conn.execute("SELECT * FROM asuntos WHERE id=?", (asunto_id,)).fetchone())
            docs, _ = _documentos(slug, conn, asunto_id)
            modelo = proveedor.obtener(app.config)
            contenido, recortado = _contenido(docs, _registrado(conn, asunto))
            sistema = SISTEMA_LECTURA.format(nombre=app.config.get("ASISTENTE_NOMBRE", "Dictum"), maximo=MAX_SUGERENCIAS)
            datos = modelo.extraer(sistema, contenido, HERRAMIENTA)

            por_id = {d["id"]: d for d in docs}
            nuevas = descartadas = 0
            for h in (datos.get("hallazgos") or [])[:MAX_SUGERENCIAS * 2]:
                tipo = h.get("tipo")
                titulo = " ".join(str(h.get("titulo") or "").split())[:140]
                if tipo not in TIPOS_SUGERENCIA or not titulo:
                    descartadas += 1
                    continue
                citas = []
                for c in (h.get("citas") or [])[:2]:
                    doc = por_id.get(c.get("documento"))
                    pagina = _verificar(str(c.get("texto") or ""), doc) if doc else None
                    if pagina is not None:
                        citas.append({"doc_id": doc["id"], "documento": doc["nombre"], "url": doc["url"],
                                      "pagina": pagina, "texto": " ".join(str(c["texto"]).split())[:300]})
                fecha = _fecha_valida(h.get("fecha"))
                if not citas or (tipo == "contradiccion" and len(citas) < 2) or (tipo == "hecho" and not fecha):
                    descartadas += 1
                    continue
                s = {"tipo": tipo, "fecha": fecha, "titulo": titulo,
                     "detalle": " ".join(str(h.get("detalle") or "").split())[:400],
                     "carril": "juicio" if h.get("carril") == "juicio" else "gestion",
                     "clase": h.get("clase") if h.get("clase") in CLASES_ACTO else "otro",
                     "rol": " ".join(str(h.get("rol") or "").split())[:80], "citas": citas}
                cur = conn.execute(
                    "INSERT OR IGNORE INTO asunto_mapa_sugerencias (asunto_id, clave, tipo, fecha, datos_cifrados) "
                    "VALUES (?,?,?,?,?)",
                    (asunto_id, _clave(s), tipo, fecha, boveda.cifrar_texto(slug, json.dumps(s, ensure_ascii=False))))
                nuevas += cur.rowcount
            conn.execute("UPDATE asunto_mapa_lecturas SET estado='listo', nuevas=?, descartadas=?, recortado=?, "
                         "modelo=?, mensaje=NULL, fin=? WHERE asunto_id=?",
                         (nuevas, descartadas, int(recortado), modelo.modelo, datetime.utcnow().isoformat(" ", "seconds"), asunto_id))
            conn.commit()
        except Exception as exc:
            # Nunca se registra el contenido de los documentos, solo el tipo de error.
            log.warning("Mapa: falló la lectura de Dictum del asunto %s (%s): %s", asunto_id, slug, type(exc).__name__)
            try:
                conn.execute("UPDATE asunto_mapa_lecturas SET estado='error', mensaje=?, fin=? WHERE asunto_id=?",
                             ("No se pudo completar la lectura. Intenta de nuevo en un momento.",
                              datetime.utcnow().isoformat(" ", "seconds"), asunto_id))
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()


def resolver(tdb, slug: str, asunto_id: int, sid: int, accion: str, uid) -> bool:
    """Confirmar, descartar o volver a abrir una sugerencia. Un hecho o plazo con fecha confirmado
    se vuelve un acto de la cronología, con su documento y página como fuente."""
    estado = {"confirmar": "confirmado", "descartar": "descartado", "reabrir": "sugerido"}.get(accion)
    if not estado:
        return False
    s = next((x for x in sugerencias(tdb, slug, asunto_id) if x["id"] == sid), None)
    if not s:
        return False
    tdb.execute("UPDATE asunto_mapa_sugerencias SET estado=?, revisado_por=?, revisado_el=CURRENT_TIMESTAMP "
                "WHERE id=? AND asunto_id=?", (estado, None if estado == "sugerido" else uid, sid, asunto_id))
    tdb.execute("DELETE FROM asunto_actos WHERE asunto_id=? AND sugerencia_id=?", (asunto_id, sid))
    if estado == "confirmado" and s["tipo"] in ("hecho", "plazo") and s.get("fecha"):
        c = s["citas"][0]
        tdb.execute("INSERT INTO asunto_actos (asunto_id, fecha, clase, titulo, detalle, documento_url, documento, "
                    "pagina, origen, sugerencia_id, creado_por) VALUES (?,?,?,?,?,?,?,?, 'dictum', ?, ?)",
                    (asunto_id, s["fecha"], s.get("clase") or ("otro" if s["tipo"] == "hecho" else "requerimiento"),
                     s["titulo"], s.get("detalle"), c["url"], c["documento"], c["pagina"], sid, uid))
    tdb.commit()
    return True


def agregar_acto(tdb, asunto_id: int, datos: dict, uid, documentos: list[dict]) -> str | None:
    """Acto que registra el equipo a mano. Devuelve un mensaje de error o None."""
    asegurar(tdb)
    fecha = _fecha_valida(datos.get("fecha"))
    titulo = " ".join(str(datos.get("titulo") or "").split())[:160]
    clase = datos.get("clase") if datos.get("clase") in CLASES_ACTO else "otro"
    if not fecha:
        return "Indica la fecha del acto."
    if not titulo:
        return "Describe el acto en una línea."
    doc = next((d for d in documentos if d["url"] and d["url"] == datos.get("documento")), None)
    tdb.execute("INSERT INTO asunto_actos (asunto_id, fecha, clase, titulo, detalle, documento_url, documento, "
                "origen, creado_por) VALUES (?,?,?,?,?,?,?, 'equipo', ?)",
                (asunto_id, fecha, clase, titulo, " ".join(str(datos.get("detalle") or "").split())[:400] or None,
                 doc["url"] if doc else None, doc["nombre"] if doc else None, uid))
    tdb.commit()
    return None


def quitar_acto(tdb, asunto_id: int, acto_id: int) -> bool:
    """Quita un acto. Si venía de Dictum, la sugerencia vuelve a quedar por confirmar."""
    asegurar(tdb)
    r = tdb.execute("SELECT sugerencia_id FROM asunto_actos WHERE id=? AND asunto_id=?", (acto_id, asunto_id)).fetchone()
    if not r:
        return False
    tdb.execute("DELETE FROM asunto_actos WHERE id=?", (acto_id,))
    if r["sugerencia_id"]:
        tdb.execute("UPDATE asunto_mapa_sugerencias SET estado='sugerido', revisado_por=NULL, revisado_el=NULL "
                    "WHERE id=? AND asunto_id=?", (r["sugerencia_id"], asunto_id))
    tdb.commit()
    return True


def sugerencias(tdb, slug: str, asunto_id: int) -> list[dict]:
    from modules import boveda
    asegurar(tdb)
    salida = []
    for r in tdb.execute("SELECT * FROM asunto_mapa_sugerencias WHERE asunto_id=? ORDER BY fecha IS NULL, fecha, id",
                         (asunto_id,)):
        try:
            datos = json.loads(boveda.descifrar_texto(slug, r["datos_cifrados"]))
        except Exception:
            continue
        salida.append({**datos, "id": r["id"], "estado": r["estado"], "revisado_por": r["revisado_por"],
                       "revisado_el": r["revisado_el"], "tipo_label": TIPOS_SUGERENCIA[r["tipo"]]})
    return salida
