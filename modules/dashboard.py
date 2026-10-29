from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, g, session, redirect, url_for, request, jsonify
from modules.asuntos import ABIERTOS, ESTADOS, TIPOS_DERECHO, _FILTRO_PROPIOS, _solo_lo_suyo, _usuarios
from modules.auth import login_required, sesion_del_despacho, tenant_required

dashboard_bp = Blueprint("dashboard", __name__)

TERMINO_TIPOS = ("plazo", "vencimiento", "fecha_limite")
TIPO_EVENTO = {"plazo": "Término", "vencimiento": "Término", "fecha_limite": "Término",
               "audiencia": "Audiencia", "diligencia": "Diligencia", "reunion": "Reunión",
               "tarea": "Pendiente", "recordatorio": "Recordatorio"}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                "octubre", "noviembre", "diciembre"]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

NOMBRE_CLIENTE_SQL = "COALESCE(NULLIF(c.razon_social, ''), TRIM(c.nombre || ' ' || COALESCE(c.apellidos, '')))"
# Última actividad de un expediente: alta, actividades, documentos o eventos, lo más reciente
ULTIMA_SQL = """MAX(COALESCE(a.fecha_alta, ''),
    COALESCE((SELECT MAX(x.fecha_alta) FROM asunto_actividades x WHERE x.asunto_id = a.id), ''),
    COALESCE((SELECT MAX(d.fecha_subida) FROM documentos d WHERE d.asunto_id = a.id), ''),
    COALESCE((SELECT MAX(e.fecha_alta) FROM eventos_calendario e WHERE e.asunto_id = a.id), ''))"""


def _fecha(valor):
    try:
        return date.fromisoformat(str(valor)[:10])
    except (TypeError, ValueError):
        return None


def _corta(f):
    return f"{f.day} {MESES_CORTOS[f.month - 1]} {f.year}" if f else ""


ZONA = ZoneInfo("America/Mexico_City")

# Actividad reciente: qué se movió en el despacho, de cualquier fuente.
# Cada fila: fecha (UTC), quién, qué hizo, sobre qué, en qué expediente y qué ícono lleva.
ACTIVIDAD_SQL = """
    SELECT a.fecha_alta AS fecha, a.creado_por AS quien, 'abrió el expediente' AS verbo, a.titulo AS que,
           a.id AS asunto_id, a.titulo AS asunto, 'expediente' AS tipo FROM asuntos a WHERE {alc}
    UNION ALL SELECT n.fecha, n.creado_por, 'agregó una nota', n.contenido, a.id, a.titulo, 'nota'
           FROM asunto_notas n JOIN asuntos a ON a.id = n.asunto_id WHERE {alc}
    UNION ALL SELECT n.fecha_alta, n.creado_por, 'agregó una nota', COALESCE(NULLIF(n.titulo, ''), n.contenido), a.id, a.titulo, 'nota'
           FROM notas n JOIN asuntos a ON a.id = n.asunto_id WHERE {alc}
    UNION ALL SELECT x.fecha_alta, x.creado_por, 'registró una actuación', x.titulo, a.id, a.titulo, 'actuacion'
           FROM asunto_actividades x JOIN asuntos a ON a.id = x.asunto_id WHERE {alc}
    UNION ALL SELECT d.fecha_subida, d.subido_por, 'subió', d.nombre, a.id, a.titulo, 'documento'
           FROM documentos d JOIN asuntos a ON a.id = d.asunto_id WHERE {alc}
    UNION ALL SELECT s.fecha_alta, s.creado_por, 'agregó la resolución', s.titulo, a.id, a.titulo, 'documento'
           FROM asunto_sentencias s JOIN asuntos a ON a.id = s.asunto_id WHERE {alc}
    UNION ALL SELECT e.fecha_alta, e.creado_por, 'agendó', e.titulo, a.id, a.titulo, 'evento'
           FROM eventos_calendario e JOIN asuntos a ON a.id = e.asunto_id WHERE {alc}
    UNION ALL SELECT p.fecha_alta, p.creado_por, 'registró un pago de', '$' || printf('%,d', CAST(p.monto AS INTEGER)) || ' · ' || co.concepto,
           a.id, a.titulo, 'pago'
           FROM cobro_pagos p JOIN cobros co ON co.id = p.cobro_id JOIN asuntos a ON a.id = co.asunto_id WHERE {alc}
"""


def _local(valor):
    try:
        dt = datetime.fromisoformat(str(valor)[:19])
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZONA).replace(tzinfo=None)


def _hace(dt) -> str:
    if not dt:
        return ""
    ahora = datetime.now(ZONA).replace(tzinfo=None)
    seg = (ahora - dt).total_seconds()
    if seg < 90:
        return "Hace un momento"
    if seg < 3600:
        return f"Hace {int(seg // 60)} min"
    if dt.date() == ahora.date():
        return f"Hoy · {dt:%H:%M}"
    if dt.date() == ahora.date() - timedelta(days=1):
        return f"Ayer · {dt:%H:%M}"
    return f"{dt.day} {MESES_CORTOS[dt.month - 1]}" + ("" if dt.year == ahora.year else f" {dt.year}")


def actividad_reciente(tdb, alcance: str, alcance_p: list, limite: int = 8) -> list[dict]:
    partes = ACTIVIDAD_SQL.count("{alc}")
    filas = tdb.execute(
        f"SELECT * FROM ({ACTIVIDAD_SQL.format(alc=alcance)}) WHERE fecha IS NOT NULL ORDER BY fecha DESC LIMIT ?",
        alcance_p * partes + [limite * 6],
    ).fetchall()
    usuarios = _usuarios()
    salida = []
    for r in filas:
        u = usuarios.get(r["quien"]) or {}
        que = " ".join(str(r["que"] or "").split())
        dt = _local(r["fecha"])
        previo = salida[-1] if salida else None
        # Varias cosas iguales seguidas de la misma persona se cuentan en una sola línea
        if (previo and previo["_quien"] == r["quien"] and previo["_verbo"] == r["verbo"] and dt and previo["_dt"]
                and abs((previo["_dt"] - dt).total_seconds()) < 1800 and r["tipo"] in ("evento", "documento", "nota")):
            previo["_n"] += 1
            nombres = {"evento": ("evento", "eventos"), "documento": ("documento", "documentos"), "nota": ("nota", "notas")}[r["tipo"]]
            previo["verbo"] = {"evento": "agendó", "documento": "subió", "nota": "agregó"}[r["tipo"]]
            previo["que"] = f"{previo['_n']} {nombres[1]}"
            previo["asunto"] = previo["_primero"] + f" y {previo['_n'] - 1} más"
            continue
        salida.append({
            "quien": u.get("corto") or "Alguien del despacho", "iniciales": u.get("iniciales", "·"),
            "color": u.get("color", "#60718D"), "verbo": r["verbo"],
            "que": que[:140] + ("…" if len(que) > 140 else ""), "tipo": r["tipo"],
            "asunto": r["asunto"] if r["tipo"] != "expediente" else "",
            "url": url_for("asuntos.detalle", id=r["asunto_id"]),
            "cuando": _hace(dt),
            "_quien": r["quien"], "_verbo": r["verbo"], "_dt": dt, "_n": 1, "_primero": que[:60],
        })
        if len(salida) > limite:
            break
    return salida[:limite]


def _hora(evento):
    if evento["todo_el_dia"] or len(str(evento["fecha_inicio"] or "")) < 16:
        return "Todo el día"
    return str(evento["fecha_inicio"])[11:16]


@dashboard_bp.route("/")
def index():
    if not getattr(g, "tenant", None):
        return render_template("auth/landing.html")
    if not sesion_del_despacho():
        return redirect(url_for("auth.login"))

    tdb = g.tenant_db
    uid = session.get("user_id")
    hoy = date.today()
    hoy_iso = hoy.isoformat()

    # Un abogado solo ve sus expedientes; el resto del equipo ve todos
    alcance, alcance_p = ("1=1", [])
    if _solo_lo_suyo():
        alcance, alcance_p = (_FILTRO_PROPIOS, [uid, uid, uid])

    abiertos = ",".join("?" * len(ABIERTOS))
    en_curso = tdb.execute(f"SELECT COUNT(*) FROM asuntos a WHERE {alcance} AND a.estado IN ({abiertos})",
                           (*alcance_p, *ABIERTOS)).fetchone()[0]
    archivados = tdb.execute(f"SELECT COUNT(*) FROM asuntos a WHERE {alcance} AND a.estado = 'archivado'",
                             alcance_p).fetchone()[0]

    # ── Actividad reciente en los expedientes que esta persona puede ver ──
    actividad = actividad_reciente(tdb, alcance, alcance_p)

    # Filtros rápidos del buscador: lo más útil del día y las materias que lleva el despacho
    materias = [r[0] for r in tdb.execute(
        f"""SELECT a.tipo_asunto FROM asuntos a WHERE {alcance} AND a.estado IN ({abiertos}) AND a.tipo_asunto IS NOT NULL
            GROUP BY a.tipo_asunto ORDER BY COUNT(*) DESC LIMIT 3""", (*alcance_p, *ABIERTOS)).fetchall()]
    sugerencias = [("esta semana", "Esta semana"), ("términos por vencer", "Términos por vencer"),
                   ("audiencias de mañana", "Audiencias de mañana"), ("sin movimiento", "Sin movimiento")]
    if _solo_lo_suyo():
        sugerencias.insert(0, ("mis expedientes", "Mis expedientes"))
    sugerencias += [(TIPOS_DERECHO[m]["label"].lower(), TIPOS_DERECHO[m]["label"]) for m in materias if m in TIPOS_DERECHO]
    sugerencias.append(("cobros vencidos", "Cobros vencidos"))

    # ── Próximos vencimientos: términos vencidos + lo de los próximos 30 días ──
    terminos = ",".join("?" * len(TERMINO_TIPOS))
    filtro_ev, filtro_p = "", []
    if _solo_lo_suyo():
        filtro_ev = f" AND (e.asunto_id IS NULL OR e.asunto_id IN (SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}))"
        filtro_p = [uid, uid, uid]
    vencimientos = []
    for e in tdb.execute(
        f"""SELECT e.titulo, e.tipo, e.fecha_inicio, e.todo_el_dia, e.asunto_id,
                   a.titulo AS asunto, {NOMBRE_CLIENTE_SQL} AS cliente
            FROM eventos_calendario e
            LEFT JOIN asuntos a ON a.id = e.asunto_id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE e.estado = 'pendiente'
              AND ((e.tipo IN ({terminos}) AND date(e.fecha_inicio) < ?)
                   OR date(e.fecha_inicio) BETWEEN ? AND ?){filtro_ev}
            ORDER BY date(e.fecha_inicio), e.fecha_inicio
            LIMIT 6""",
        (*TERMINO_TIPOS, hoy_iso, hoy_iso, (hoy + timedelta(days=30)).isoformat(), *filtro_p),
    ).fetchall():
        f = _fecha(e["fecha_inicio"])
        if not f:
            continue
        vencido = f < hoy
        vencimientos.append({
            "dia": f.day, "mes": MESES_CORTOS[f.month - 1].upper(),
            "titulo": e["titulo"],
            "detalle": " · ".join(x for x in (TIPO_EVENTO.get(e["tipo"], ""), e["asunto"] or e["cliente"]) if x),
            "cuando": "Vencido" if vencido else ("Hoy" if f == hoy else _hora(e)),
            "vencido": vencido,
            "url": url_for("asuntos.detalle", id=e["asunto_id"]) if e["asunto_id"] else url_for("calendario.index"),
        })

    # ── Avisos en una línea ───────────────────────────────────
    terminos_pronto = tdb.execute(
        f"""SELECT COUNT(*) FROM eventos_calendario e WHERE e.estado = 'pendiente' AND e.tipo IN ({terminos})
              AND date(e.fecha_inicio) <= ?{filtro_ev}""",
        (*TERMINO_TIPOS, (hoy + timedelta(days=1)).isoformat(), *filtro_p),
    ).fetchone()[0]
    sin_movimiento = tdb.execute(
        f"""SELECT COUNT(*) FROM asuntos a
            WHERE {alcance} AND a.estado = 'activo' AND date({ULTIMA_SQL}) < date('now', '-30 days')""",
        alcance_p,
    ).fetchone()[0]

    # ── Insights: lo que hoy importa, en números con una lectura ──
    semana = (hoy + timedelta(days=7)).isoformat()
    def eventos_rango(tipos):
        marcas = ",".join("?" * len(tipos))
        return tdb.execute(
            f"""SELECT e.titulo, e.fecha_inicio FROM eventos_calendario e
                WHERE e.estado = 'pendiente' AND e.tipo IN ({marcas}) AND date(e.fecha_inicio) BETWEEN ? AND ?{filtro_ev}
                ORDER BY e.fecha_inicio""", (*tipos, hoy_iso, semana, *filtro_p)).fetchall()
    def cuando(f):
        d = (f - hoy).days
        return "hoy" if d == 0 else ("mañana" if d == 1 else f"el {DIAS[f.weekday()]} {f.day}")
    terminos_sem = eventos_rango(TERMINO_TIPOS)
    audiencias_sem = eventos_rango(("audiencia", "diligencia"))
    vencidos_n = tdb.execute(
        f"""SELECT COUNT(*) FROM eventos_calendario e WHERE e.estado='pendiente' AND e.tipo IN ({terminos})
            AND date(e.fecha_inicio) < ?{filtro_ev}""", (*TERMINO_TIPOS, hoy_iso, *filtro_p)).fetchone()[0]
    from modules import cobranzas
    alc_c, pc = cobranzas._alcance()
    cartera = tdb.execute(
        f"""SELECT COALESCE(SUM({cobranzas.SALDO}), 0),
                   COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) < ? THEN {cobranzas.SALDO} ELSE 0 END), 0),
                   COUNT(DISTINCT CASE WHEN date(co.fecha_vencimiento) < ? THEN co.cliente_id END)
            FROM cobros co WHERE {cobranzas.ABIERTO_SQL} AND {alc_c}""", (hoy_iso, hoy_iso, *pc)).fetchone()
    usuarios = _usuarios()
    carga = []
    if not _solo_lo_suyo():
        for r in tdb.execute(
            f"""SELECT a.abogado_responsable AS uid, COUNT(*) AS n FROM asuntos a
                WHERE a.estado IN ({abiertos}) GROUP BY a.abogado_responsable ORDER BY n DESC LIMIT 4""",
            ABIERTOS).fetchall():
            u = usuarios.get(r["uid"]) or {}
            carga.append({"nombre": u.get("corto") or "Sin responsable", "iniciales": u.get("iniciales", "?"),
                          "color": u.get("color", "#9AA3B2"), "n": r["n"]})
        tope = max((c["n"] for c in carga), default=1)
        for c in carga:
            c["pct"] = round(c["n"] / tope * 100)
    nuevos_mes = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos a WHERE {alcance} AND date(COALESCE(a.fecha_inicio, a.fecha_alta)) >= ?",
        (*alcance_p, hoy.replace(day=1).isoformat())).fetchone()[0]

    def primero(filas):
        if not filas:
            return ""
        f = _fecha(filas[0]["fecha_inicio"])
        return f"El primero: {filas[0]['titulo']}, {cuando(f)}." if f else ""

    insights = [
        {"clave": "terminos", "n": len(terminos_sem), "titulo": "Términos en 7 días",
         "lectura": primero(terminos_sem) or "Ningún término vence esta semana.",
         "extra": f"{vencidos_n} ya vencido{'s' if vencidos_n != 1 else ''}" if vencidos_n else "",
         "tono": "rojo" if vencidos_n or any((_fecha(t["fecha_inicio"]) - hoy).days <= 2 for t in terminos_sem) else "",
         "url": url_for("buscador.resultados", q="términos próximos días")},
        {"clave": "audiencias", "n": len(audiencias_sem), "titulo": "Audiencias y diligencias",
         "lectura": primero(audiencias_sem).replace("El primero", "La primera") or "No hay audiencias en los próximos 7 días.",
         "extra": "", "tono": "", "url": url_for("buscador.resultados", q="audiencias próximos días")},
        {"clave": "cobro", "n": f"${cartera[0]:,.0f}", "titulo": "Por cobrar",
         "lectura": ((f"Todo ya está vencido; son {cartera[2]} cliente{'s' if cartera[2] != 1 else ''}." if cartera[1] >= cartera[0]
                      else f"${cartera[1]:,.0f} ya vencieron, de {cartera[2]} cliente{'s' if cartera[2] != 1 else ''}.") if cartera[1]
                     else ("Todo va al corriente." if cartera[0] else "No hay cobros pendientes.")),
         "extra": "", "tono": "rojo" if cartera[1] else "", "url": url_for("cobranzas.index", grupo="vencidos" if cartera[1] else None)},
        {"clave": "parados", "n": sin_movimiento, "titulo": "Sin movimiento",
         "lectura": ("Llevan más de 30 días sin notas, documentos ni audiencias. Conviene revisarlos." if sin_movimiento
                     else "Todos los expedientes en curso se han movido en el último mes."),
         "extra": f"{nuevos_mes} nuevo{'s' if nuevos_mes != 1 else ''} este mes" if nuevos_mes else "",
         "tono": "ambar" if sin_movimiento else "", "url": url_for("asuntos.index", atencion="sin_mov")},
    ]

    nombre = (session.get("user") or {}).get("nombre", "")
    return render_template(
        "dashboard/index.html",
        nombre=nombre,
        en_curso=en_curso,
        archivados=archivados,
        actividad=actividad,
        sugerencias=sugerencias,
        insights=insights, carga=carga,
        hoy_txt=f"{DIAS[hoy.weekday()]} {hoy.day} de {MESES_LARGOS[hoy.month - 1]}",
        vencimientos=vencimientos,
        terminos_pronto=terminos_pronto,
        sin_movimiento=sin_movimiento,
    )


@dashboard_bp.route("/tareas/<int:tarea_id>/estado", methods=["POST"])
@login_required
@tenant_required
def tarea_estado(tarea_id):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest" or not request.is_json:
        return jsonify(ok=False, error="Solicitud no válida"), 400
    hecha = bool((request.get_json(silent=True) or {}).get("hecha"))
    tdb = g.tenant_db
    cur = tdb.execute(
        "UPDATE tareas SET estado = ?, fecha_completada = ? WHERE id = ?",
        ("completada" if hecha else "pendiente",
         datetime.now().isoformat(timespec="seconds") if hecha else None, tarea_id),
    )
    tdb.commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, error="Tarea no encontrada"), 404
    return jsonify(ok=True)
