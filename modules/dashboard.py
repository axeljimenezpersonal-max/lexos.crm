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

    nombre = (session.get("user") or {}).get("nombre", "")
    return render_template(
        "dashboard/index.html",
        nombre=nombre,
        en_curso=en_curso,
        archivados=archivados,
        actividad=actividad,
        sugerencias=sugerencias,
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
