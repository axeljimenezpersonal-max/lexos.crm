"""
Habeas Corpus — modules/mapa.py
Mapa del expediente: una representación del caso que se recorre por tiempo, personas, documentos
y puntos por revisar, donde cada elemento lleva a su fuente. Por ahora se arma con lo que el equipo
registra (agenda, actividad, documentos, resoluciones, cobros y datos generales) y con reglas;
cuando se conecte la IA, Dictum sumará hechos sugeridos con su cita.
"""

from datetime import date, timedelta

from flask import url_for

TERMINOS = ("plazo", "vencimiento", "fecha_limite")
DEL_JUICIO_EVENTO = ("audiencia", "diligencia", *TERMINOS)
DEL_JUICIO_ACTIVIDAD = ("audiencia", "acuerdo", "diligencia", "vencimiento")
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
SIN_JUZGADO = ("notarial", "corporativo")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS asunto_mapa_revisados (
    asunto_id  INTEGER NOT NULL,
    clave      TEXT    NOT NULL,
    user_id    INTEGER,
    fecha      DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (asunto_id, clave)
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
    from modules.asuntos import ACTIVIDAD_LABEL, EVENTO_LABEL, SENTENCIA_LABEL, _fecha, _hoy

    aid = asunto["id"]
    hoy = _hoy()
    agenda_url = url_for("calendario.index", asunto=aid)

    def doc_url(url: str | None) -> str | None:
        return url if url and url.startswith("/archivos/") else None

    # ── Cronología: lo del juicio y las gestiones, cada cosa con su fuente ──
    cronologia = []

    def evento(f, titulo, carril, tipo, fuente, url=None, ir=None):
        if not f:
            return
        cronologia.append({**_dia(f), "fecha": f, "titulo": titulo, "carril": carril, "tipo": tipo,
                           "fuente": fuente, "url": url, "ir": ir, "futuro": f > hoy})

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
        evento(_fecha(d.get("fecha_subida")), f"Se integra «{d['nombre']}»", "gestion", "Documento", "Documento",
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
    if abierto and not (ficha and (ficha.get("objetivo_cliente") or "").strip()):
        punto("estrategia_vacia", "bajo", "Falta definir la estrategia",
              "Escribe qué quiere lograr el cliente; así todo el equipo y Dictum van hacia lo mismo.", "Estrategia",
              ir="estrategia")

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

    return {"cronologia": cronologia, "personas": personas, "documentos": docs,
            "puntos": pendientes, "revisados": hechos,
            "juicio": sum(1 for c in cronologia if c["carril"] == "juicio"),
            "hoy": _dia(hoy)}
