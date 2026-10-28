from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, g, session, redirect, url_for, request, jsonify
from modules.asuntos import ABIERTOS, ESTADOS, TIPOS_DERECHO, _FILTRO_PROPIOS, _solo_lo_suyo
from modules.auth import login_required, sesion_del_despacho, tenant_required

dashboard_bp = Blueprint("dashboard", __name__)

TERMINO_TIPOS = ("plazo", "vencimiento", "fecha_limite")
TIPO_EVENTO = {"plazo": "Término", "vencimiento": "Término", "fecha_limite": "Término",
               "audiencia": "Audiencia", "diligencia": "Diligencia", "reunion": "Reunión",
               "tarea": "Pendiente", "recordatorio": "Recordatorio"}
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

    # ── Expedientes recientes (primero los que siguen en curso) ──
    recientes = []
    for r in tdb.execute(
        f"""SELECT a.id, a.titulo, a.numero_expediente, a.tipo_asunto, a.sub_tipo, a.estado,
                   {NOMBRE_CLIENTE_SQL} AS cliente, {ULTIMA_SQL} AS ultima
            FROM asuntos a LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {alcance}
            ORDER BY a.estado IN ({abiertos}) DESC, ultima DESC LIMIT 6""",
        (*alcance_p, *ABIERTOS),
    ).fetchall():
        est = ESTADOS.get(r["estado"], ESTADOS["activo"])
        tipo = r["sub_tipo"] or TIPOS_DERECHO.get(r["tipo_asunto"], {}).get("label", "")
        tipo = tipo[:1].upper() + tipo[1:]
        recientes.append({
            "id": r["id"], "titulo": r["titulo"], "numero": r["numero_expediente"] or f"#{r['id']}",
            "detalle": " · ".join(x for x in (tipo, r["cliente"]) if x),
            "estado": "En trámite" if r["estado"] == "activo" else est["label"], "clave": r["estado"],
            "ultima": _corta(_fecha(r["ultima"])),
        })

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
        recientes=recientes,
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
