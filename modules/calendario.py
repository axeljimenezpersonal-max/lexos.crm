"""
LexOS — modules/calendario.py
Calendario del despacho: 4 vistas (semana/mes/día/agenda), crear/editar eventos,
tipos con colores, recurrencia, detección de conflictos, Google Calendar sync,
exportar .ics, disponibilidad del equipo.
"""
from __future__ import annotations

import io
import json
import math
import os
import uuid
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from modules.auth import login_required, role_required, tenant_required

calendario_bp = Blueprint("calendario", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

TIPOS_EVENTO = {
    "audiencia":    {"label": "Audiencia",            "color": "#132238", "icon": "gavel"},
    "vencimiento":  {"label": "Término",              "color": "#A23B33", "icon": "clock"},
    "fecha_limite": {"label": "Fecha límite crítica", "color": "#7E2C26", "icon": "alert_triangle"},
    "diligencia":   {"label": "Diligencia",           "color": "#34496B", "icon": "flag"},
    "reunion":      {"label": "Reunión",              "color": "#4B6585", "icon": "users"},
    "tarea":        {"label": "Tarea",                "color": "#3D5A80", "icon": "check_circle"},
    "recordatorio": {"label": "Recordatorio",         "color": "#60718D", "icon": "bell"},
    "otro":         {"label": "Otro",                 "color": "#60718D", "icon": "more_vertical"},
    # Tipo heredado de versiones anteriores: se muestra como término pero no se ofrece al crear.
    "plazo":        {"label": "Término",              "color": "#A23B33", "icon": "clock", "oculto": True},
}

TERMINO_TIPOS = ("vencimiento", "fecha_limite", "plazo")

RECURRENCIAS = ["none", "diario", "semanal", "mensual", "anual"]

RECORDATORIO_OPCIONES = [
    {"val": 15,    "label": "15 min antes"},
    {"val": 30,    "label": "30 min antes"},
    {"val": 60,    "label": "1 hora antes"},
    {"val": 1440,  "label": "1 día antes"},
    {"val": 4320,  "label": "3 días antes"},
    {"val": 10080, "label": "1 semana antes"},
]

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(accion, entidad_id=None, detalles=None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, "evento", entidad_id,
             json.dumps(detalles or {}), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _get_abogados():
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users "
            "WHERE tenant_id=? AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _event_to_dict(row, tdb=None) -> dict:
    """Convert a DB row to a JSON-safe event dict."""
    d = dict(row)
    tipo_info = TIPOS_EVENTO.get(d.get("tipo", "otro"), TIPOS_EVENTO["otro"])
    d["tipo_info"]  = tipo_info
    d["color"]      = d.get("color_custom") or tipo_info["color"]
    d["icon"]       = tipo_info["icon"]
    # Participantes
    if tdb:
        parts = tdb.execute(
            "SELECT user_id FROM evento_participantes WHERE evento_id=?", (d["id"],)
        ).fetchall()
        d["participante_ids"] = [p["user_id"] for p in parts]
    return d


def _check_conflictos(tdb, fecha_inicio: str, fecha_fin: str | None,
                       user_id: int, exclude_id: int | None = None) -> list[dict]:
    """Return overlapping events for the same user."""
    if not fecha_fin:
        fecha_fin = fecha_inicio
    where = ["e.estado != 'cancelado'", "e.todo_el_dia = 0"]
    params = []
    # Overlap: starts before our end AND ends after our start
    where.append("e.fecha_inicio < ?")
    params.append(fecha_fin)
    where.append("COALESCE(e.fecha_fin, e.fecha_inicio) > ?")
    params.append(fecha_inicio)
    if exclude_id:
        where.append("e.id != ?")
        params.append(exclude_id)
    # Same user (creator or participant)
    where.append("""(e.creado_por = ? OR e.id IN
        (SELECT evento_id FROM evento_participantes WHERE user_id=?))""")
    params += [user_id, user_id]

    rows = tdb.execute(
        f"SELECT e.id, e.titulo, e.fecha_inicio, e.fecha_fin, e.tipo "
        f"FROM eventos_calendario e WHERE {' AND '.join(where)} LIMIT 5",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — render page (events loaded via AJAX)
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/")
@login_required
@tenant_required
def index():
    abogados = _get_abogados()
    tdb      = g.tenant_db

    asuntos = [dict(r) for r in tdb.execute(
        "SELECT id, titulo, numero_expediente, cliente_id FROM asuntos "
        "WHERE estado NOT IN ('archivado','cerrado') ORDER BY titulo LIMIT 300"
    ).fetchall()]

    clientes = [dict(r) for r in tdb.execute(
        "SELECT id, tipo, nombre, apellidos, razon_social FROM clientes WHERE activo=1 ORDER BY nombre LIMIT 300"
    ).fetchall()]

    # Mini-calendar: days with events this month
    today = date.today()
    ym    = today.strftime("%Y-%m")
    days_with = tdb.execute(
        "SELECT DISTINCT CAST(strftime('%d', fecha_inicio) AS INTEGER) as d "
        "FROM eventos_calendario WHERE strftime('%Y-%m', fecha_inicio)=? AND estado!='cancelado'",
        (ym,),
    ).fetchall()
    busy_days = [r["d"] for r in days_with]

    # GCal sync status
    gcal_active = False
    try:
        tk = g.global_db.execute(
            "SELECT sync_activo FROM gcal_tokens WHERE user_id=? AND tenant_id=?",
            (session.get("user_id"), g.tenant["id"]),
        ).fetchone()
        if tk:
            gcal_active = bool(tk["sync_activo"])
    except Exception:
        pass

    terminos = []
    marcas = ",".join("?" * len(TERMINO_TIPOS))
    for r in tdb.execute(
        f"""SELECT e.id, e.titulo, e.fecha_inicio, a.titulo AS asunto, a.numero_expediente
            FROM eventos_calendario e LEFT JOIN asuntos a ON a.id = e.asunto_id
            WHERE e.estado = 'pendiente' AND e.tipo IN ({marcas})
              AND date(e.fecha_inicio) <= date('now', '+21 days')
            ORDER BY date(e.fecha_inicio) LIMIT 8""",
        TERMINO_TIPOS,
    ).fetchall():
        try:
            dias = (date.fromisoformat(str(r["fecha_inicio"])[:10]) - today).days
        except ValueError:
            continue
        if dias < 0:
            cuando, tono = ("Vencido" if dias < -1 else "Venció ayer"), "red"
        elif dias == 0:
            cuando, tono = "Hoy", "red"
        elif dias == 1:
            cuando, tono = "Mañana", "red"
        else:
            cuando, tono = f"En {dias} días", ("amb" if dias <= 5 else "grn")
        terminos.append({
            "id": r["id"], "titulo": r["titulo"], "fecha": str(r["fecha_inicio"])[:10],
            "detalle": r["numero_expediente"] or r["asunto"] or "",
            "cuando": cuando, "tono": tono,
        })

    return render_template(
        "calendario/index.html",
        terminos=terminos,
        tipos_evento=TIPOS_EVENTO,
        recurrencias=RECURRENCIAS,
        recordatorio_opciones=RECORDATORIO_OPCIONES,
        abogados=abogados,
        asuntos=asuntos,
        clientes=clientes,
        busy_days=busy_days,
        gcal_active=gcal_active,
        today_str=today.isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — eventos para rango de fechas
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/eventos")
@login_required
@tenant_required
def ajax_eventos():
    tdb   = g.tenant_db
    start = request.args.get("start", "")
    end   = request.args.get("end", "")
    tipo  = request.args.get("tipo", "")
    abog  = request.args.get("abogado", "")
    asun  = request.args.get("asunto", "")

    where, params = ["e.estado != 'cancelado'"], []
    if start:
        where.append("e.fecha_inicio >= ?"); params.append(start)
    if end:
        where.append("e.fecha_inicio <= ?"); params.append(end)
    if tipo:
        where.append("e.tipo = ?"); params.append(tipo)
    if abog:
        where.append("(e.creado_por=? OR e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?))")
        params += [abog, abog]
    if asun:
        where.append("e.asunto_id=?"); params.append(asun)

    rows = tdb.execute(
        f"""SELECT e.*, a.titulo as asunto_titulo, a.numero_expediente,
                   c.nombre||' '||COALESCE(c.apellidos,'') as cliente_nombre
            FROM eventos_calendario e
            LEFT JOIN asuntos a  ON e.asunto_id=a.id
            LEFT JOIN clientes c ON e.cliente_id=c.id
            WHERE {' AND '.join(where)}
            ORDER BY e.fecha_inicio ASC LIMIT 500""",
        params,
    ).fetchall()

    result = []
    for r in rows:
        d  = dict(r)
        ti = TIPOS_EVENTO.get(d.get("tipo", "otro"), TIPOS_EVENTO["otro"])
        result.append({
            "id":        d["id"],
            "titulo":    d["titulo"],
            "tipo":      d["tipo"],
            "color":     d.get("color_custom") or ti["color"],
            "icon":      ti["icon"],
            "inicio":    d["fecha_inicio"],
            "fin":       d["fecha_fin"],
            "todo_dia":  bool(d.get("todo_el_dia")),
            "lugar":     d.get("lugar"),
            "descripcion": d.get("descripcion"),
            "asunto":    d.get("asunto_titulo"),
            "expediente": d.get("numero_expediente"),
            "cliente":   d.get("cliente_nombre"),
            "recurrencia": d.get("recurrencia", "none"),
            "gcal":      bool(d.get("gcal_event_id")),
            "estado":    d.get("estado", "pendiente"),
        })
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — crear evento
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/crear", methods=["POST"])
@login_required
@tenant_required
def ajax_crear():
    tdb  = g.tenant_db
    data = request.json or {}

    titulo = (data.get("titulo") or "").strip()
    if not titulo:
        return jsonify(ok=False, error="El título es requerido"), 400
    fecha_inicio = (data.get("fecha_inicio") or "").strip()
    if not fecha_inicio:
        return jsonify(ok=False, error="La fecha de inicio es requerida"), 400

    tipo         = data.get("tipo", "otro")
    if tipo not in TIPOS_EVENTO or TIPOS_EVENTO[tipo].get("oculto"):
        tipo = "otro"
    color_custom = data.get("color_custom") or None
    recurrencia  = data.get("recurrencia", "none")
    if recurrencia not in RECURRENCIAS:
        recurrencia = "none"

    cur = tdb.execute(
        """INSERT INTO eventos_calendario
           (titulo, descripcion, tipo, color_custom, asunto_id, cliente_id,
            fecha_inicio, fecha_fin, todo_el_dia, recurrencia,
            lugar, recordatorio, gcal_event_id, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            titulo,
            (data.get("descripcion") or "").strip() or None,
            tipo, color_custom,
            _safe_int(data.get("asunto_id")),
            _safe_int(data.get("cliente_id")),
            fecha_inicio,
            data.get("fecha_fin") or None,
            1 if data.get("todo_dia") else 0,
            recurrencia,
            (data.get("lugar") or "").strip() or None,
            _safe_int(data.get("recordatorio")),
            None,  # gcal_event_id — set later by sync
            session.get("user_id"),
        ),
    )
    tdb.commit()
    eid = cur.lastrowid

    # Participantes
    for uid in (data.get("participantes") or []):
        uid = _safe_int(uid)
        if uid:
            tdb.execute(
                "INSERT OR IGNORE INTO evento_participantes (evento_id, user_id) VALUES (?,?)",
                (eid, uid),
            )
    tdb.commit()

    # Generar recurrentes (si aplica) — hasta 52 semanas / 12 meses
    if recurrencia != "none":
        _generar_recurrentes(tdb, eid, data, recurrencia)

    _log("crear_evento", eid, {"titulo": titulo})

    # Google Calendar sync
    _gcal_push(eid)

    # Conflictos
    uid = session.get("user_id")
    conflictos = _check_conflictos(tdb, fecha_inicio, data.get("fecha_fin"), uid, eid) if uid else []

    return jsonify(ok=True, id=eid, conflictos=conflictos)


def _generar_recurrentes(tdb, parent_id, data, recurrencia):
    """Create recurring copies. Max 52 for weekly, 12 for monthly/yearly."""
    try:
        fi = datetime.fromisoformat(data["fecha_inicio"])
        ff = datetime.fromisoformat(data["fecha_fin"]) if data.get("fecha_fin") else None
        dur = (ff - fi) if ff else timedelta(hours=1)
    except Exception:
        return

    deltas = {"diario": (timedelta(days=1), 90),
              "semanal": (timedelta(weeks=1), 52),
              "mensual": (timedelta(days=30), 12),
              "anual": (timedelta(days=365), 5)}
    delta, count = deltas.get(recurrencia, (None, 0))
    if not delta:
        return

    for i in range(1, count + 1):
        ni = fi + delta * i
        nf = ni + dur if ff else None
        tdb.execute(
            """INSERT INTO eventos_calendario
               (titulo, descripcion, tipo, color_custom, asunto_id, cliente_id,
                fecha_inicio, fecha_fin, todo_el_dia, recurrencia, lugar,
                recordatorio, recurrencia_parent_id, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("titulo"), data.get("descripcion"),
                data.get("tipo", "otro"), data.get("color_custom"),
                _safe_int(data.get("asunto_id")), _safe_int(data.get("cliente_id")),
                ni.isoformat(), nf.isoformat() if nf else None,
                1 if data.get("todo_dia") else 0, recurrencia,
                data.get("lugar"), _safe_int(data.get("recordatorio")),
                parent_id, session.get("user_id"),
            ),
        )
    tdb.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — editar evento
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/editar", methods=["POST"])
@login_required
@tenant_required
def ajax_editar(id: int):
    tdb  = g.tenant_db
    data = request.json or {}
    row  = tdb.execute("SELECT id FROM eventos_calendario WHERE id=?", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404

    campos_permitidos = {
        "titulo", "descripcion", "tipo", "color_custom", "asunto_id", "cliente_id",
        "fecha_inicio", "fecha_fin", "todo_el_dia", "recurrencia", "lugar", "recordatorio",
    }
    if "todo_dia" in data:
        data["todo_el_dia"] = 1 if data.pop("todo_dia") else 0
    if "tipo" in data and data["tipo"] not in TIPOS_EVENTO:
        data["tipo"] = "otro"
    sets, vals = [], []
    for k, v in data.items():
        if k in campos_permitidos:
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(id)
        tdb.execute(f"UPDATE eventos_calendario SET {','.join(sets)} WHERE id=?", vals)
        tdb.commit()

    # Actualizar participantes
    if "participantes" in data:
        tdb.execute("DELETE FROM evento_participantes WHERE evento_id=?", (id,))
        for uid in (data["participantes"] or []):
            uid = _safe_int(uid)
            if uid:
                tdb.execute(
                    "INSERT OR IGNORE INTO evento_participantes (evento_id, user_id) VALUES (?,?)",
                    (id, uid),
                )
        tdb.commit()

    _log("editar_evento", id)
    _gcal_push(id)

    fi = data.get("fecha_inicio")
    ff = data.get("fecha_fin")
    uid = session.get("user_id")
    conflictos = _check_conflictos(tdb, fi, ff, uid, id) if fi and uid else []
    return jsonify(ok=True, conflictos=conflictos)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — obtener evento
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>")
@login_required
@tenant_required
def ajax_evento(id: int):
    tdb = g.tenant_db
    row = tdb.execute(
        """SELECT e.*, a.titulo as asunto_titulo, c.nombre||' '||COALESCE(c.apellidos,'') as cliente_nombre
           FROM eventos_calendario e
           LEFT JOIN asuntos a ON e.asunto_id=a.id
           LEFT JOIN clientes c ON e.cliente_id=c.id
           WHERE e.id=?""", (id,)
    ).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404
    d = _event_to_dict(row, tdb)
    return jsonify(ok=True, evento=d)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — eliminar
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
def ajax_eliminar(id: int):
    tdb  = g.tenant_db
    data = request.json or {}
    delete_series = data.get("delete_series", False)

    # Get gcal_event_id before deleting
    ev = tdb.execute("SELECT gcal_event_id FROM eventos_calendario WHERE id=?", (id,)).fetchone()

    if delete_series:
        tdb.execute(
            "UPDATE eventos_calendario SET estado='cancelado' WHERE recurrencia_parent_id=? OR id=?",
            (id, id),
        )
    else:
        tdb.execute("UPDATE eventos_calendario SET estado='cancelado' WHERE id=?", (id,))
    tdb.commit()
    _log("eliminar_evento", id)

    # Delete from Google Calendar too
    if ev and ev["gcal_event_id"]:
        try:
            from flask import current_app
            cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
            csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
            tk = g.global_db.execute(
                "SELECT * FROM gcal_tokens WHERE user_id=? AND tenant_id=? AND sync_activo=1",
                (session.get("user_id"), g.tenant["id"]),
            ).fetchone()
            if tk and cid:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build
                creds = Credentials(
                    token=tk["access_token"], refresh_token=tk["refresh_token"],
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=cid, client_secret=csecret,
                )
                service = build("calendar", "v3", credentials=creds)
                service.events().delete(calendarId="primary", eventId=ev["gcal_event_id"]).execute()
        except Exception:
            pass

    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — completar
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/completar", methods=["POST"])
@login_required
@tenant_required
def ajax_completar(id: int):
    g.tenant_db.execute(
        "UPDATE eventos_calendario SET estado='completado' WHERE id=?", (id,)
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — disponibilidad del equipo
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/disponibilidad")
@login_required
@tenant_required
def ajax_disponibilidad():
    tdb   = g.tenant_db
    start = request.args.get("start", "")
    end   = request.args.get("end", "")
    if not start or not end:
        return jsonify([])

    rows = tdb.execute(
        """SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.fecha_fin,
                  e.todo_el_dia, e.creado_por, e.color_custom,
                  ep.user_id as participante
           FROM eventos_calendario e
           LEFT JOIN evento_participantes ep ON ep.evento_id=e.id
           WHERE e.estado != 'cancelado'
             AND e.fecha_inicio >= ? AND e.fecha_inicio <= ?
           ORDER BY e.fecha_inicio""",
        (start, end),
    ).fetchall()

    # Group by user
    by_user = {}
    for r in rows:
        for uid in set(filter(None, [r["creado_por"], r["participante"]])):
            by_user.setdefault(uid, []).append({
                "id":     r["id"],
                "titulo": r["titulo"],
                "inicio": r["fecha_inicio"],
                "fin":    r["fecha_fin"],
                "color":  r["color_custom"] or TIPOS_EVENTO.get(r["tipo"], {}).get("color", "#60718D"),
            })
    return jsonify(by_user)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — conflictos
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/conflictos")
@login_required
@tenant_required
def ajax_conflictos():
    fi  = request.args.get("inicio", "")
    ff  = request.args.get("fin", "")
    uid = _safe_int(request.args.get("user_id")) or session.get("user_id")
    eid = _safe_int(request.args.get("exclude_id"))
    if not fi or not uid:
        return jsonify([])
    return jsonify(_check_conflictos(g.tenant_db, fi, ff, uid, eid))


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR .ics (iCalendar)
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/exportar-ics")
@login_required
@tenant_required
def exportar_ics():
    tdb    = g.tenant_db
    start  = request.args.get("start", "")
    end    = request.args.get("end", "")
    where, params = ["e.estado != 'cancelado'"], []
    if start:
        where.append("e.fecha_inicio >= ?"); params.append(start)
    if end:
        where.append("e.fecha_inicio <= ?"); params.append(end)

    rows = tdb.execute(
        f"SELECT * FROM eventos_calendario e WHERE {' AND '.join(where)} ORDER BY fecha_inicio",
        params,
    ).fetchall()

    tenant = g.tenant
    cal_name = tenant["nombre_despacho"] if tenant else "LexOS"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//LexOS//{cal_name}//ES",
        f"X-WR-CALNAME:{cal_name}",
    ]
    for r in rows:
        uid_ics = f"{r['id']}@lexos.app"
        dtstart = (r["fecha_inicio"] or "").replace("-", "").replace(":", "").replace(" ", "T")[:15]
        dtend   = (r["fecha_fin"] or r["fecha_inicio"] or "").replace("-", "").replace(":", "").replace(" ", "T")[:15]
        summary = (r["titulo"] or "").replace(",", "\\,")
        desc    = (r["descripcion"] or "").replace("\n", "\\n").replace(",", "\\,")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_ics}",
            f"DTSTART:{dtstart}" if not r["todo_el_dia"] else f"DTSTART;VALUE=DATE:{dtstart[:8]}",
            f"DTEND:{dtend}" if not r["todo_el_dia"] else f"DTEND;VALUE=DATE:{dtend[:8]}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            f"LOCATION:{(r['lugar'] or '').replace(',','\\,')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines)

    buf = io.BytesIO(content.encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/calendar", as_attachment=True,
                     download_name=f"calendario_{date.today()}.ics")


# ─────────────────────────────────────────────────────────────────────────────
#  GOOGLE CALENDAR — OAuth2 + sync
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_dt(raw: str) -> str:
    """Normalize a date-time string to ISO 8601 for Google API.
    Handles: '2026-03-20 13:00', '2026-03-20T13:00', '2026-03-20 13:00:00', etc."""
    if not raw:
        return ""
    s = raw.strip().replace(" ", "T")
    # If only HH:MM, add :00
    parts = s.split("T")
    if len(parts) == 2:
        time_part = parts[1]
        if len(time_part) == 5:  # HH:MM
            s = parts[0] + "T" + time_part + ":00"
    return s


def _gcal_push(event_id: int):
    """Push a single event to Google Calendar (if user has sync active). Non-blocking."""
    try:
        from flask import current_app
        cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
        csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
        if not cid:
            return

        uid = session.get("user_id")
        tid = g.tenant["id"]

        tk = g.global_db.execute(
            "SELECT * FROM gcal_tokens WHERE user_id=? AND tenant_id=? AND sync_activo=1",
            (uid, tid),
        ).fetchone()
        if not tk:
            return

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=tk["access_token"],
            refresh_token=tk["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid,
            client_secret=csecret,
        )
        service = build("calendar", "v3", credentials=creds)

        ev = g.tenant_db.execute("SELECT * FROM eventos_calendario WHERE id=?", (event_id,)).fetchone()
        if not ev:
            print(f"[GCal Push] Event {event_id} not found in DB")
            return

        # Normalize date-time to ISO 8601
        fi = _normalize_dt(ev["fecha_inicio"])
        ff = _normalize_dt(ev["fecha_fin"] or ev["fecha_inicio"])
        print(f"[GCal Push] fi={fi}, ff={ff}, todo_dia={ev['todo_el_dia']}")

        if ev["todo_el_dia"]:
            body = {
                "summary": ev["titulo"],
                "description": ev["descripcion"] or "",
                "location": ev["lugar"] or "",
                "start": {"date": fi[:10]},
                "end":   {"date": ff[:10]},
            }
        else:
            body = {
                "summary": ev["titulo"],
                "description": ev["descripcion"] or "",
                "location": ev["lugar"] or "",
                "start": {"dateTime": fi, "timeZone": "America/Mexico_City"},
                "end":   {"dateTime": ff, "timeZone": "America/Mexico_City"},
            }

        gcal_id = ev.get("gcal_event_id") if hasattr(ev, 'get') else ev["gcal_event_id"]
        if gcal_id:
            service.events().update(calendarId="primary", eventId=gcal_id, body=body).execute()
        else:
            result = service.events().insert(calendarId="primary", body=body).execute()
            g.tenant_db.execute(
                "UPDATE eventos_calendario SET gcal_event_id=? WHERE id=?",
                (result["id"], event_id),
            )
            g.tenant_db.commit()
    except Exception as e:
        import traceback
        print(f"[GCal Push Error] {e}")
        traceback.print_exc()


@calendario_bp.route("/oauth/google")
@login_required
@tenant_required
def oauth_google():
    """Redirect user to Google OAuth2 consent screen."""
    try:
        from config import Config
        from google_auth_oauthlib.flow import Flow

        # Build redirect_uri explicitly — must match Google Console exactly
        from flask import current_app
        is_dev = current_app.config.get("DEBUG", False)
        base = f"http://{current_app.config.get('SERVER_NAME_DEV', 'localhost:5001')}" if is_dev else request.host_url.rstrip("/")
        callback_uri = base + "/calendario/oauth/google/callback"
        flow = Flow.from_client_config(
            {"web": {
                "client_id":     Config.GOOGLE_CLIENT_ID,
                "client_secret": Config.GOOGLE_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }},
            scopes=["https://www.googleapis.com/auth/calendar.events"],
            redirect_uri=callback_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline", prompt="consent",
            code_verifier=None,  # Disable PKCE
        )
        session["gcal_state"] = state
        # Save code_verifier for callback (if PKCE is used)
        session["gcal_code_verifier"] = flow.code_verifier
        return redirect(auth_url)
    except Exception as e:
        return jsonify(ok=False, error=f"Google Calendar no configurado: {e}"), 500


@calendario_bp.route("/oauth/google/callback")
def oauth_callback():
    """Handle Google OAuth2 callback, store tokens."""
    # Session must survive the Google redirect — validate manually
    uid = session.get("user_id")
    tid = session.get("tenant_id")
    slug = session.get("tenant_slug")
    if not uid or not tid:
        flash("Sesión expirada. Inicia sesión y vuelve a conectar Google Calendar.", "warning")
        return redirect(url_for("auth.landing"))
    try:
        from config import Config
        from google_auth_oauthlib.flow import Flow

        from flask import current_app
        is_dev = current_app.config.get("DEBUG", False)
        base = f"http://{current_app.config.get('SERVER_NAME_DEV', 'localhost:5001')}" if is_dev else request.host_url.rstrip("/")
        callback_uri = base + "/calendario/oauth/google/callback"
        flow = Flow.from_client_config(
            {"web": {
                "client_id":     Config.GOOGLE_CLIENT_ID,
                "client_secret": Config.GOOGLE_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }},
            scopes=["https://www.googleapis.com/auth/calendar.events"],
            redirect_uri=callback_uri,
        )
        # Restore code_verifier from session
        flow.code_verifier = session.pop("gcal_code_verifier", None)
        # Rebuild the callback URL with the exact redirect_uri + Google's params
        auth_response = callback_uri + "?" + request.query_string.decode()
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials

        # Upsert token — use global DB directly
        from init_db import get_global_conn
        gdb = get_global_conn()
        gdb.execute(
            """INSERT INTO gcal_tokens (user_id, tenant_id, access_token, refresh_token, token_expiry, sync_activo)
               VALUES (?,?,?,?,?,1)
               ON CONFLICT(user_id, tenant_id) DO UPDATE SET
                 access_token=excluded.access_token,
                 refresh_token=COALESCE(excluded.refresh_token, gcal_tokens.refresh_token),
                 token_expiry=excluded.token_expiry,
                 sync_activo=1""",
            (uid, tid,
             creds.token, creds.refresh_token,
             creds.expiry.isoformat() if creds.expiry else None),
        )
        gdb.commit()
        gdb.close()
        flash("Google Calendar conectado exitosamente.", "success")
        return redirect(url_for("calendario.index"))
    except Exception as e:
        flash(f"Error al conectar Google Calendar: {e}", "danger")
        return redirect(url_for("calendario.index"))


@calendario_bp.route("/ajax/sincronizar", methods=["POST"])
@login_required
@tenant_required
def ajax_sincronizar():
    """Manual Google Calendar sync trigger."""
    try:
        from flask import current_app
        cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
        csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
        if not cid:
            return jsonify(ok=False, error="Google Calendar no configurado"), 400

        tk = g.global_db.execute(
            "SELECT * FROM gcal_tokens WHERE user_id=? AND tenant_id=? AND sync_activo=1",
            (session.get("user_id"), g.tenant["id"]),
        ).fetchone()
        if not tk:
            return jsonify(ok=False, error="Sincronización no activa"), 400

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=tk["access_token"],
            refresh_token=tk["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid,
            client_secret=csecret,
        )
        service = build("calendar", "v3", credentials=creds)

        # Pull events from Google Calendar (next 30 days)
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
        gcal_events = service.events().list(
            calendarId="primary", timeMin=now, timeMax=end,
            maxResults=100, singleEvents=True, orderBy="startTime",
        ).execute().get("items", [])

        tdb = g.tenant_db
        imported = 0
        for ge in gcal_events:
            gcal_id = ge["id"]
            exists  = tdb.execute(
                "SELECT id FROM eventos_calendario WHERE gcal_event_id=?", (gcal_id,)
            ).fetchone()
            if exists:
                continue  # Skip existing
            start = ge.get("start", {})
            end_g = ge.get("end", {})
            tdb.execute(
                """INSERT INTO eventos_calendario
                   (titulo, descripcion, tipo, fecha_inicio, fecha_fin,
                    todo_el_dia, lugar, gcal_event_id, creado_por)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    ge.get("summary", "Evento Google"),
                    ge.get("description"),
                    "otro",
                    start.get("dateTime", start.get("date", "")),
                    end_g.get("dateTime", end_g.get("date")),
                    1 if "date" in start else 0,
                    ge.get("location"),
                    gcal_id,
                    session.get("user_id"),
                ),
            )
            imported += 1
        tdb.commit()

        # Update last_sync
        g.global_db.execute(
            "UPDATE gcal_tokens SET last_sync=CURRENT_TIMESTAMP WHERE user_id=? AND tenant_id=?",
            (session.get("user_id"), g.tenant["id"]),
        )
        g.global_db.commit()
        return jsonify(ok=True, importados=imported)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@calendario_bp.route("/ajax/toggle-gcal", methods=["POST"])
@login_required
@tenant_required
def ajax_toggle_gcal():
    """Toggle Google Calendar sync on/off."""
    data   = request.json or {}
    activo = 1 if data.get("activo") else 0
    try:
        g.global_db.execute(
            "UPDATE gcal_tokens SET sync_activo=? WHERE user_id=? AND tenant_id=?",
            (activo, session.get("user_id"), g.tenant["id"]),
        )
        g.global_db.commit()
        return jsonify(ok=True)
    except Exception:
        return jsonify(ok=False, error="No hay cuenta vinculada"), 400
