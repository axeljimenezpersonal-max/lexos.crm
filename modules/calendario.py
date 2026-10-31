"""
Habeas Corpus — modules/calendario.py
Calendario del despacho: vistas de día, semana, mes y lista; crear, editar, mover y borrar
eventos; tipos con colores; repetición; aviso de empalmes por persona; cómputo de términos
en días hábiles; días inhábiles del despacho; exportar .ics y Google Calendar (opcional).

Fechas: todo se guarda y se muestra en hora de la Ciudad de México. Un evento con hora se
guarda como «AAAA-MM-DD HH:MM:SS» y uno de todo el día como «AAAA-MM-DD».

Quién ve qué: igual que en los expedientes. Un abogado ve los eventos de sus expedientes
(responsable, colaborador o los que abrió), los que creó y aquellos a los que lo asignaron.
El resto del equipo ve todo el calendario del despacho.
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint, current_app, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from modules import plazos
from modules.asuntos import _FILTRO_PROPIOS, _solo_lo_suyo, puede_ver
from modules.auth import login_required, tenant_required

calendario_bp = Blueprint("calendario", __name__)

ZONA = ZoneInfo("America/Mexico_City")

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

TIPOS_EVENTO = {
    "audiencia":    {"label": "Audiencia",            "color": "#142E59", "icon": "gavel"},
    "vencimiento":  {"label": "Término",              "color": "#A23B33", "icon": "clock"},
    "fecha_limite": {"label": "Fecha límite crítica", "color": "#7E2C26", "icon": "alert_triangle"},
    "diligencia":   {"label": "Diligencia",           "color": "#34496B", "icon": "flag"},
    "reunion":      {"label": "Cita o reunión",       "color": "#4B6585", "icon": "users"},
    "tarea":        {"label": "Tarea",                "color": "#3D5A80", "icon": "check_circle"},
    "recordatorio": {"label": "Recordatorio",         "color": "#60718D", "icon": "bell"},
    "otro":         {"label": "Otro",                 "color": "#60718D", "icon": "more_vertical"},
    # Tipo heredado de versiones anteriores: se muestra como término pero no se ofrece al crear.
    "plazo":        {"label": "Término",              "color": "#A23B33", "icon": "clock", "oculto": True},
}

TERMINO_TIPOS = ("vencimiento", "fecha_limite", "plazo")

RECURRENCIAS = ["none", "diario", "semanal", "mensual", "anual"]
# Cuántas repeticiones se crean por adelantado
REPETICIONES = {"diario": 90, "semanal": 52, "mensual": 12, "anual": 5}

RECORDATORIO_OPCIONES = [
    {"val": 15,    "label": "15 min antes"},
    {"val": 30,    "label": "30 min antes"},
    {"val": 60,    "label": "1 hora antes"},
    {"val": 1440,  "label": "1 día antes"},
    {"val": 4320,  "label": "3 días antes"},
    {"val": 10080, "label": "1 semana antes"},
]

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def hoy_mx() -> date:
    return datetime.now(ZONA).date()


def ahora_mx() -> datetime:
    return datetime.now(ZONA).replace(tzinfo=None)


def alcance_eventos(uid: int | None = None) -> tuple[str, list]:
    """Filtro SQL (alias «e») con los eventos que la persona puede ver.
    Un abogado: los de sus expedientes, los que creó y aquellos donde participa."""
    if not _solo_lo_suyo():
        return "1=1", []
    uid = uid if uid is not None else session.get("user_id")
    return (f"(e.creado_por=? OR e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?) "
            f"OR e.asunto_id IN (SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}))", [uid] * 5)


def contar_hoy(tdb, uid: int | None = None) -> int:
    """Eventos pendientes de hoy que la persona puede ver (globo del menú Calendario)."""
    alc, params = alcance_eventos(uid)
    return tdb.execute(
        f"SELECT COUNT(*) FROM eventos_calendario e WHERE e.estado='pendiente' "
        f"AND date(e.fecha_inicio) <= ? AND date(COALESCE(e.fecha_fin, e.fecha_inicio)) >= ? AND {alc}",
        (hoy_mx().isoformat(), hoy_mx().isoformat(), *params)).fetchone()[0]


def _puede(tdb, evento_id: int):
    """La fila del evento si la persona puede verlo y cambiarlo; None si no."""
    alc, params = alcance_eventos()
    return tdb.execute(
        f"SELECT e.* FROM eventos_calendario e WHERE e.id=? AND e.estado != 'cancelado' AND {alc}",
        (evento_id, *params)).fetchone()


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
            "WHERE tenant_id=? AND rol IN ('abogado','socio','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _nombres() -> dict[int, str]:
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users WHERE tenant_id=?", (g.tenant["id"],)).fetchall()
    except Exception:
        return {}
    return {r["id"]: f"{r['nombre']} {(r['apellidos'] or '').split(' ')[0]}".strip() for r in rows}


def _parse_fecha(valor) -> tuple[datetime | None, bool]:
    """Lee «2026-11-03», «2026-11-03T10:30» o «2026-11-03 10:30:00». Devuelve (fecha, trae_hora)."""
    s = str(valor or "").strip().replace("T", " ")
    if not s:
        return None, False
    try:
        if len(s) <= 10:
            return datetime.fromisoformat(s[:10]), False
        return datetime.fromisoformat(s[:19]), True
    except ValueError:
        return None, False


def _guardar_fecha(dt: datetime, todo_dia: bool) -> str:
    return dt.strftime("%Y-%m-%d") if todo_dia else dt.strftime("%Y-%m-%d %H:%M:%S")


def _salida_fecha(valor, todo_dia: bool) -> str | None:
    """Formato que entiende cualquier navegador (Safari incluido): «AAAA-MM-DD» o «AAAA-MM-DDTHH:MM»."""
    dt, _ = _parse_fecha(valor)
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d") if todo_dia else dt.strftime("%Y-%m-%dT%H:%M")


def _fecha_larga(d: date) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]} de {d.year}"


def _sumar_meses(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    anio, mes = dt.year + m // 12, m % 12 + 1
    ultimo = (date(anio + (mes == 12), mes % 12 + 1, 1) - timedelta(days=1)).day
    return dt.replace(year=anio, month=mes, day=min(dt.day, ultimo))


def _leer_evento(data: dict, actual=None) -> tuple[dict, dict]:
    """Valida lo que llega del formulario. Devuelve (campos, errores por campo).
    Con `actual` (fila existente) solo se validan los campos que vienen."""
    errores, campos = {}, {}
    parcial = actual is not None

    def viene(k):
        return (not parcial) or k in data

    if viene("titulo"):
        titulo = (data.get("titulo") or "").strip()
        if not titulo:
            errores["titulo"] = "Escribe qué es: por ejemplo, «Audiencia de pruebas»."
        elif len(titulo) > 200:
            errores["titulo"] = "El título es muy largo; déjalo en menos de 200 letras."
        campos["titulo"] = titulo

    if viene("tipo"):
        tipo = data.get("tipo") or "otro"
        if tipo not in TIPOS_EVENTO or (TIPOS_EVENTO[tipo].get("oculto") and not (actual and actual["tipo"] == tipo)):
            tipo = "otro"
        campos["tipo"] = tipo

    if "todo_dia" in data:
        todo_dia = bool(data.get("todo_dia"))
    elif "todo_el_dia" in data:
        todo_dia = bool(data.get("todo_el_dia"))
    else:
        todo_dia = bool(actual["todo_el_dia"]) if actual else False
    cambia_dia = "todo_dia" in data or "todo_el_dia" in data
    if not parcial or cambia_dia:
        campos["todo_el_dia"] = 1 if todo_dia else 0

    if not parcial or cambia_dia or "fecha_inicio" in data or "fecha_fin" in data:
        crudo_ini = data.get("fecha_inicio") if "fecha_inicio" in data else (actual["fecha_inicio"] if actual else "")
        ini, con_hora = _parse_fecha(crudo_ini)
        if "fecha_fin" in data:
            fin, _ = _parse_fecha(data.get("fecha_fin"))
            fin_ilegible = bool(data.get("fecha_fin")) and not fin
        else:
            fin, _ = _parse_fecha(actual["fecha_fin"] if actual else None)
            fin_ilegible = False
            # Mover: si solo cambia el inicio, el fin se recorre lo mismo
            viejo, _ = _parse_fecha(actual["fecha_inicio"] if actual else None)
            if fin and viejo and ini:
                fin = fin + (ini - viejo)
        if not ini:
            errores["fecha_inicio"] = ("Elige el día." if todo_dia else "Elige el día y la hora.")
        elif ini.year < 2000 or ini.year > 2100:
            errores["fecha_inicio"] = "Revisa el año de la fecha."
        else:
            if not todo_dia and not con_hora:
                errores["fecha_inicio"] = "Falta la hora. Si no tiene hora, marca «Todo el día»."
            campos["fecha_inicio"] = _guardar_fecha(ini, todo_dia)
            if fin_ilegible:
                errores["fecha_fin"] = "No entendimos la fecha de fin."
            elif fin and todo_dia:
                fin0, ini0 = fin.replace(hour=0, minute=0, second=0), ini.replace(hour=0, minute=0, second=0)
                if fin0 < ini0:
                    errores["fecha_fin"] = "El último día no puede ser antes del primero."
                campos["fecha_fin"] = None if fin0 == ini0 else _guardar_fecha(fin0, True)
            elif fin:
                if fin <= ini:
                    errores["fecha_fin"] = "La hora de fin debe ser después de la de inicio."
                campos["fecha_fin"] = _guardar_fecha(fin, False)
            else:
                campos["fecha_fin"] = None

    if viene("recurrencia"):
        rec = data.get("recurrencia") or "none"
        campos["recurrencia"] = rec if rec in RECURRENCIAS else "none"

    for k in ("descripcion", "lugar"):
        if viene(k):
            campos[k] = (data.get(k) or "").strip()[:2000] or None
    if viene("color_custom"):
        c = (data.get("color_custom") or "").strip()
        campos["color_custom"] = c if c.startswith("#") and len(c) in (4, 7) else None
    if viene("recordatorio"):
        campos["recordatorio"] = _safe_int(data.get("recordatorio"))

    tdb = g.tenant_db
    if viene("asunto_id"):
        aid = _safe_int(data.get("asunto_id"))
        if aid:
            fila = tdb.execute("SELECT id, cliente_id FROM asuntos WHERE id=?", (aid,)).fetchone()
            if not fila or not puede_ver(tdb, aid):
                errores["asunto_id"] = "Ese expediente no existe o no lo tienes asignado."
            elif not _safe_int(data.get("cliente_id")):
                campos["cliente_id"] = fila["cliente_id"]
        campos["asunto_id"] = aid
    if viene("cliente_id") and "cliente_id" not in campos:
        cid = _safe_int(data.get("cliente_id"))
        if cid and not tdb.execute("SELECT 1 FROM clientes WHERE id=?", (cid,)).fetchone():
            errores["cliente_id"] = "Ese cliente no existe."
        campos["cliente_id"] = cid
    return campos, errores


def _participantes_de(data, defecto: list[int]) -> list[int]:
    validos = {a["id"] for a in _get_abogados()}
    ids = []
    for u in data.get("participantes") or []:
        u = _safe_int(u)
        if u and u in validos and u not in ids:
            ids.append(u)
    return ids or defecto


def _check_conflictos(tdb, fecha_inicio: str, fecha_fin: str | None, user_ids: list[int],
                      exclude_ids: list[int] | None = None) -> list[dict]:
    """Eventos con hora que se empalman para cualquiera de esas personas. Cuenta a quien
    participa en el evento, o a quien lo creó si nadie más quedó asignado."""
    if not fecha_inicio or not user_ids:
        return []
    ini, con_hora = _parse_fecha(fecha_inicio)
    if not ini or not con_hora:
        return []
    fin, _ = _parse_fecha(fecha_fin)
    if not fin or fin <= ini:
        fin = ini + timedelta(hours=1)
    excluir = [i for i in (exclude_ids or []) if i]
    no_ids = f" AND e.id NOT IN ({','.join('?' * len(excluir))})" if excluir else ""
    nombres = _nombres()
    salida, vistos = [], set()
    for uid in user_ids:
        filas = tdb.execute(
            f"""SELECT e.id, e.titulo, e.fecha_inicio, e.fecha_fin, e.tipo
                FROM eventos_calendario e
                WHERE e.estado = 'pendiente' AND COALESCE(e.todo_el_dia, 0) = 0
                  AND datetime(e.fecha_inicio) < datetime(?)
                  AND datetime(COALESCE(e.fecha_fin, datetime(e.fecha_inicio, '+1 hour'))) > datetime(?)
                  AND (e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?)
                       OR (e.creado_por=? AND NOT EXISTS (SELECT 1 FROM evento_participantes p WHERE p.evento_id=e.id))){no_ids}
                ORDER BY e.fecha_inicio LIMIT 5""",
            (fin.strftime("%Y-%m-%d %H:%M:%S"), ini.strftime("%Y-%m-%d %H:%M:%S"), uid, uid, *excluir),
        ).fetchall()
        for r in filas:
            if (r["id"], uid) in vistos:
                continue
            vistos.add((r["id"], uid))
            salida.append({"id": r["id"], "titulo": r["titulo"], "tipo": r["tipo"],
                           "tipo_label": TIPOS_EVENTO.get(r["tipo"], TIPOS_EVENTO["otro"])["label"],
                           "hora": _salida_fecha(r["fecha_inicio"], False)[11:16],
                           "fecha_inicio": _salida_fecha(r["fecha_inicio"], False),
                           "persona": nombres.get(uid, "")})
    return salida


def _guardar_participantes(tdb, eid: int, ids: list[int]):
    tdb.execute("DELETE FROM evento_participantes WHERE evento_id=?", (eid,))
    tdb.executemany("INSERT INTO evento_participantes (evento_id, user_id) VALUES (?,?)", [(eid, u) for u in ids])


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — render page (events loaded via AJAX)
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/")
@login_required
@tenant_required
def index():
    abogados = _get_abogados()
    tdb      = g.tenant_db
    uid      = session.get("user_id")
    today    = hoy_mx()
    alc, alc_p = alcance_eventos()

    filtro_a, filtro_ap = "1=1", []
    if _solo_lo_suyo():
        filtro_a, filtro_ap = _FILTRO_PROPIOS, [uid, uid, uid]
    asuntos = [dict(r) for r in tdb.execute(
        f"""SELECT a.id, a.titulo, a.numero_expediente, a.cliente_id, a.abogado_responsable,
                   COALESCE(NULLIF(c.razon_social, ''), TRIM(COALESCE(c.nombre, '') || ' ' || COALESCE(c.apellidos, ''))) AS cliente
            FROM asuntos a LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE a.estado NOT IN ('archivado','cerrado') AND {filtro_a}
            ORDER BY a.titulo LIMIT 1000""", filtro_ap
    ).fetchall()]

    clientes = [dict(r) for r in tdb.execute(
        "SELECT id, tipo, nombre, apellidos, razon_social FROM clientes WHERE activo=1 ORDER BY nombre LIMIT 1000"
    ).fetchall()]

    # Mini-calendario: días con eventos del mes en curso
    ym = today.strftime("%Y-%m")
    busy_days = [r["d"] for r in tdb.execute(
        f"SELECT DISTINCT CAST(strftime('%d', e.fecha_inicio) AS INTEGER) AS d "
        f"FROM eventos_calendario e WHERE strftime('%Y-%m', e.fecha_inicio)=? AND e.estado!='cancelado' AND {alc}",
        (ym, *alc_p),
    ).fetchall()]

    # Google Calendar: solo si el servidor tiene credenciales
    gcal_disponible = bool(current_app.config.get("GOOGLE_CLIENT_ID"))
    gcal_active = False
    if gcal_disponible:
        try:
            tk = g.global_db.execute(
                "SELECT sync_activo FROM gcal_tokens WHERE user_id=? AND tenant_id=?",
                (uid, g.tenant["id"]),
            ).fetchone()
            gcal_active = bool(tk and tk["sync_activo"])
        except Exception:
            pass

    terminos = []
    marcas = ",".join("?" * len(TERMINO_TIPOS))
    for r in tdb.execute(
        f"""SELECT e.id, e.titulo, e.fecha_inicio, a.titulo AS asunto, a.numero_expediente
            FROM eventos_calendario e LEFT JOIN asuntos a ON a.id = e.asunto_id
            WHERE e.estado = 'pendiente' AND e.tipo IN ({marcas})
              AND date(e.fecha_inicio) <= ? AND {alc}
            ORDER BY date(e.fecha_inicio) LIMIT 8""",
        (*TERMINO_TIPOS, (today + timedelta(days=21)).isoformat(), *alc_p),
    ).fetchall():
        f, _ = _parse_fecha(r["fecha_inicio"])
        if not f:
            continue
        f = f.date()
        dias = (f - today).days
        if dias < 0:
            cuando, tono = ("Venció ayer" if dias == -1 else f"Venció hace {-dias} días"), "red"
        elif dias == 0:
            cuando, tono = "Hoy", "red"
        elif dias == 1:
            cuando, tono = "Mañana", "red"
        else:
            cuando, tono = f"En {dias} días", ("amb" if dias <= 5 else "grn")
        terminos.append({
            "id": r["id"], "titulo": r["titulo"], "fecha": f.isoformat(),
            "detalle": r["numero_expediente"] or r["asunto"] or "",
            "cuando": cuando, "tono": tono,
            "dia": f.day, "mes": ("ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC")[f.month - 1],
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
        gcal_disponible=gcal_disponible,
        today_str=today.isoformat(),
        yo=uid,
        solo_lo_suyo=_solo_lo_suyo(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — eventos para rango de fechas
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/eventos")
@login_required
@tenant_required
def ajax_eventos():
    """Eventos que tocan el rango [start, end): `end` no se incluye."""
    tdb   = g.tenant_db
    start = request.args.get("start", "")
    end   = request.args.get("end", "")
    tipo  = request.args.get("tipo", "")
    abog  = _safe_int(request.args.get("abogado"))
    asun  = _safe_int(request.args.get("asunto"))

    alc, alc_p = alcance_eventos()
    where, params = ["e.estado != 'cancelado'", alc], list(alc_p)
    if start:
        where.append("date(COALESCE(e.fecha_fin, e.fecha_inicio)) >= date(?)"); params.append(start)
    if end:
        where.append("date(e.fecha_inicio) < date(?)"); params.append(end)
    if tipo:
        tipos = TERMINO_TIPOS if tipo in TERMINO_TIPOS else (tipo,)
        where.append(f"e.tipo IN ({','.join('?' * len(tipos))})"); params += list(tipos)
    if abog:
        where.append("(e.creado_por=? OR e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?) "
                     "OR e.asunto_id IN (SELECT id FROM asuntos WHERE abogado_responsable=?))")
        params += [abog, abog, abog]
    if asun:
        where.append("e.asunto_id=?"); params.append(asun)

    rows = tdb.execute(
        f"""SELECT e.*, a.titulo as asunto_titulo, a.numero_expediente,
                   COALESCE(NULLIF(c.razon_social, ''), TRIM(COALESCE(c.nombre, '') || ' ' || COALESCE(c.apellidos, ''))) AS cliente_nombre
            FROM eventos_calendario e
            LEFT JOIN asuntos a  ON e.asunto_id=a.id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE {' AND '.join(where)}
            ORDER BY datetime(e.fecha_inicio) ASC LIMIT 800""",
        params,
    ).fetchall()

    ids = [r["id"] for r in rows]
    partes: dict[int, list[int]] = {}
    for i in range(0, len(ids), 500):
        lote = ids[i:i + 500]
        for p in tdb.execute(
            f"SELECT evento_id, user_id FROM evento_participantes WHERE evento_id IN ({','.join('?' * len(lote))})", lote
        ).fetchall():
            partes.setdefault(p["evento_id"], []).append(p["user_id"])
    nombres = _nombres()
    hoy = hoy_mx()

    result = []
    for r in rows:
        d  = dict(r)
        ti = TIPOS_EVENTO.get(d.get("tipo") or "otro", TIPOS_EVENTO["otro"])
        todo_dia = bool(d.get("todo_el_dia"))
        quienes = partes.get(d["id"]) or ([d["creado_por"]] if d.get("creado_por") else [])
        ini, _ = _parse_fecha(d["fecha_inicio"])
        result.append({
            "id":        d["id"],
            "titulo":    d["titulo"],
            "tipo":      d["tipo"],
            "color":     d.get("color_custom") or ti["color"],
            "icon":      ti["icon"],
            "inicio":    _salida_fecha(d["fecha_inicio"], todo_dia),
            "fin":       _salida_fecha(d["fecha_fin"], todo_dia) if d.get("fecha_fin") else None,
            "todo_dia":  todo_dia,
            "lugar":     d.get("lugar"),
            "respuesta_cliente": d.get("respuesta_cliente"),
            "descripcion": d.get("descripcion"),
            "asunto_id": d.get("asunto_id"),
            "asunto":    d.get("asunto_titulo"),
            "expediente": d.get("numero_expediente"),
            "cliente":   (d.get("cliente_nombre") or "").strip() or None,
            "responsables": [nombres[u] for u in quienes if u in nombres],
            "recurrencia": d.get("recurrencia") or "none",
            "serie":     bool((d.get("recurrencia") or "none") != "none" or d.get("recurrencia_parent_id")),
            "gcal":      bool(d.get("gcal_event_id")),
            "estado":    d.get("estado") or "pendiente",
            "vencido":   bool(d["tipo"] in TERMINO_TIPOS and (d.get("estado") or "pendiente") == "pendiente"
                              and ini and ini.date() < hoy),
        })
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — crear evento
# ─────────────────────────────────────────────────────────────────────────────

def _respuesta_errores(errores: dict):
    primero = next(iter(errores.values()))
    return jsonify(ok=False, error=primero, errores=errores), 400


@calendario_bp.route("/ajax/crear", methods=["POST"])
@login_required
@tenant_required
def ajax_crear():
    tdb  = g.tenant_db
    data = request.get_json(silent=True) or {}
    uid  = session.get("user_id")

    campos, errores = _leer_evento(data)
    if errores:
        return _respuesta_errores(errores)

    # Quién lo atiende: lo elegido; si nada, el responsable del expediente o quien lo crea
    defecto = [uid] if uid else []
    if campos.get("asunto_id") and not data.get("participantes"):
        resp = tdb.execute("SELECT abogado_responsable FROM asuntos WHERE id=?", (campos["asunto_id"],)).fetchone()
        if resp and resp["abogado_responsable"]:
            defecto = [resp["abogado_responsable"]]
    participantes = _participantes_de(data, defecto)

    conflictos = []
    if not campos["todo_el_dia"]:
        conflictos = _check_conflictos(tdb, campos["fecha_inicio"], campos.get("fecha_fin"), participantes)
    # El formulario pregunta antes de guardar si hay empalmes
    if conflictos and data.get("revisar_conflictos") and not data.get("confirmar_conflicto"):
        return jsonify(ok=False, necesita_confirmar=True, conflictos=conflictos)

    cur = tdb.execute(
        """INSERT INTO eventos_calendario
           (titulo, descripcion, tipo, color_custom, asunto_id, cliente_id,
            fecha_inicio, fecha_fin, todo_el_dia, recurrencia,
            lugar, recordatorio, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (campos["titulo"], campos.get("descripcion"), campos["tipo"], campos.get("color_custom"),
         campos.get("asunto_id"), campos.get("cliente_id"),
         campos["fecha_inicio"], campos.get("fecha_fin"), campos["todo_el_dia"], campos["recurrencia"],
         campos.get("lugar"), campos.get("recordatorio"), uid),
    )
    eid = cur.lastrowid
    _guardar_participantes(tdb, eid, participantes)
    tdb.commit()

    creados = 0
    if campos["recurrencia"] != "none":
        creados = _generar_recurrentes(tdb, eid, campos, participantes)

    _log("crear_evento", eid, {"titulo": campos["titulo"]})
    _gcal_push(eid)
    return jsonify(ok=True, id=eid, conflictos=conflictos, repeticiones=creados)


def _generar_recurrentes(tdb, parent_id, campos, participantes) -> int:
    """Copias de un evento que se repite: el mismo día de cada mes o de cada año, no cada 30 o 365 días."""
    rec = campos["recurrencia"]
    todo_dia = bool(campos["todo_el_dia"])
    fi, _ = _parse_fecha(campos["fecha_inicio"])
    ff, _ = _parse_fecha(campos.get("fecha_fin"))
    if not fi:
        return 0
    dur = (ff - fi) if ff else None
    n = 0
    for i in range(1, REPETICIONES.get(rec, 0) + 1):
        if rec == "diario":
            ni = fi + timedelta(days=i)
        elif rec == "semanal":
            ni = fi + timedelta(weeks=i)
        elif rec == "mensual":
            ni = _sumar_meses(fi, i)
        else:
            ni = _sumar_meses(fi, 12 * i)
        nf = ni + dur if dur is not None else None
        cur = tdb.execute(
            """INSERT INTO eventos_calendario
               (titulo, descripcion, tipo, color_custom, asunto_id, cliente_id,
                fecha_inicio, fecha_fin, todo_el_dia, recurrencia, lugar,
                recordatorio, recurrencia_parent_id, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (campos["titulo"], campos.get("descripcion"), campos["tipo"], campos.get("color_custom"),
             campos.get("asunto_id"), campos.get("cliente_id"),
             _guardar_fecha(ni, todo_dia), _guardar_fecha(nf, todo_dia) if nf else None,
             1 if todo_dia else 0, rec, campos.get("lugar"), campos.get("recordatorio"),
             parent_id, session.get("user_id")),
        )
        tdb.executemany("INSERT INTO evento_participantes (evento_id, user_id) VALUES (?,?)",
                        [(cur.lastrowid, u) for u in participantes])
        n += 1
    tdb.commit()
    return n


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — editar o mover evento
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/editar", methods=["POST"])
@login_required
@tenant_required
def ajax_editar(id: int):
    tdb  = g.tenant_db
    data = request.get_json(silent=True) or {}
    row  = _puede(tdb, id)
    if not row:
        return jsonify(ok=False, error="Ese evento no existe o no lo tienes asignado."), 404

    campos, errores = _leer_evento(data, actual=row)
    if errores:
        return _respuesta_errores(errores)
    campos.pop("recurrencia", None)   # la repetición se define al crear

    if "participantes" in data:
        participantes = _participantes_de(data, [row["creado_por"]] if row["creado_por"] else [])
    else:
        participantes = [p["user_id"] for p in tdb.execute(
            "SELECT user_id FROM evento_participantes WHERE evento_id=?", (id,)).fetchall()] \
            or ([row["creado_por"]] if row["creado_por"] else [])

    ini = campos.get("fecha_inicio", row["fecha_inicio"])
    fin = campos["fecha_fin"] if "fecha_fin" in campos else row["fecha_fin"]
    todo = campos.get("todo_el_dia", row["todo_el_dia"])
    conflictos = [] if todo else _check_conflictos(tdb, ini, fin, participantes, [id])
    if conflictos and data.get("revisar_conflictos") and not data.get("confirmar_conflicto"):
        return jsonify(ok=False, necesita_confirmar=True, conflictos=conflictos)

    if campos:
        sets = ",".join(f"{k}=?" for k in campos)
        tdb.execute(f"UPDATE eventos_calendario SET {sets} WHERE id=?", (*campos.values(), id))
    if "participantes" in data:
        _guardar_participantes(tdb, id, participantes)
    tdb.commit()

    _log("editar_evento", id)
    _gcal_push(id)
    return jsonify(ok=True, conflictos=conflictos)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — obtener evento
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>")
@login_required
@tenant_required
def ajax_evento(id: int):
    tdb = g.tenant_db
    alc, alc_p = alcance_eventos()
    row = tdb.execute(
        f"""SELECT e.*, a.titulo as asunto_titulo, a.numero_expediente,
                   COALESCE(NULLIF(c.razon_social, ''), TRIM(COALESCE(c.nombre, '') || ' ' || COALESCE(c.apellidos, ''))) AS cliente_nombre
            FROM eventos_calendario e
            LEFT JOIN asuntos a ON e.asunto_id=a.id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE e.id=? AND e.estado != 'cancelado' AND {alc}""", (id, *alc_p)
    ).fetchone()
    if not row:
        return jsonify(ok=False, error="Ese evento no existe o no lo tienes asignado."), 404
    d = dict(row)
    todo_dia = bool(d.get("todo_el_dia"))
    tipo_info = TIPOS_EVENTO.get(d.get("tipo") or "otro", TIPOS_EVENTO["otro"])
    d["tipo_info"] = tipo_info
    d["color"] = d.get("color_custom") or tipo_info["color"]
    d["icon"] = tipo_info["icon"]
    d["fecha_inicio"] = _salida_fecha(d["fecha_inicio"], todo_dia)
    d["fecha_fin"] = _salida_fecha(d["fecha_fin"], todo_dia) if d.get("fecha_fin") else None
    d["participante_ids"] = [p["user_id"] for p in tdb.execute(
        "SELECT user_id FROM evento_participantes WHERE evento_id=?", (id,)).fetchall()]
    d["serie"] = bool((d.get("recurrencia") or "none") != "none" or d.get("recurrencia_parent_id"))
    return jsonify(ok=True, evento=d)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — eliminar
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
def ajax_eliminar(id: int):
    """Se marca como cancelado (no se borra de la base). Con «serie», también las
    repeticiones pendientes que siguen a esta."""
    tdb  = g.tenant_db
    data = request.get_json(silent=True) or {}
    ev = _puede(tdb, id)
    if not ev:
        return jsonify(ok=False, error="Ese evento no existe o no lo tienes asignado."), 404

    n = 1
    if data.get("serie") or data.get("delete_series"):
        raiz = ev["recurrencia_parent_id"] or id
        cur = tdb.execute(
            """UPDATE eventos_calendario SET estado='cancelado'
               WHERE id=? OR (recurrencia_parent_id=? AND estado='pendiente'
                              AND datetime(fecha_inicio) >= datetime(?))""",
            (id, raiz, ev["fecha_inicio"]))
        n = cur.rowcount
    else:
        tdb.execute("UPDATE eventos_calendario SET estado='cancelado' WHERE id=?", (id,))
    tdb.commit()
    _log("eliminar_evento", id, {"eventos": n})

    if ev["gcal_event_id"] and current_app.config.get("GOOGLE_CLIENT_ID"):
        try:
            tk = g.global_db.execute(
                "SELECT * FROM gcal_tokens WHERE user_id=? AND tenant_id=? AND sync_activo=1",
                (session.get("user_id"), g.tenant["id"]),
            ).fetchone()
            if tk:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build
                creds = Credentials(
                    token=tk["access_token"], refresh_token=tk["refresh_token"],
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=current_app.config["GOOGLE_CLIENT_ID"],
                    client_secret=current_app.config.get("GOOGLE_CLIENT_SECRET", ""),
                )
                service = build("calendar", "v3", credentials=creds)
                service.events().delete(calendarId="primary", eventId=ev["gcal_event_id"]).execute()
        except Exception:
            current_app.logger.exception("No se pudo borrar el evento en Google Calendar")

    return jsonify(ok=True, eliminados=n)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — marcar como hecho / volver a pendiente
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/<int:id>/completar", methods=["POST"])
@login_required
@tenant_required
def ajax_completar(id: int):
    tdb = g.tenant_db
    if not _puede(tdb, id):
        return jsonify(ok=False, error="Ese evento no existe o no lo tienes asignado."), 404
    hecho = (request.get_json(silent=True) or {}).get("hecho", True)
    estado = "completado" if hecho else "pendiente"
    tdb.execute("UPDATE eventos_calendario SET estado=? WHERE id=?", (estado, id))
    tdb.commit()
    _log("completar_evento" if hecho else "reabrir_evento", id)
    return jsonify(ok=True, estado=estado)


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
    alc, alc_p = alcance_eventos()
    rows = tdb.execute(
        f"""SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.fecha_fin,
                  e.todo_el_dia, e.creado_por, e.color_custom,
                  ep.user_id as participante
           FROM eventos_calendario e
           LEFT JOIN evento_participantes ep ON ep.evento_id=e.id
           WHERE e.estado != 'cancelado' AND {alc}
             AND date(e.fecha_inicio) >= date(?) AND date(e.fecha_inicio) < date(?)
           ORDER BY e.fecha_inicio""",
        (*alc_p, start, end),
    ).fetchall()
    by_user = {}
    for r in rows:
        for u in set(filter(None, [r["creado_por"], r["participante"]])):
            by_user.setdefault(u, []).append({
                "id":     r["id"],
                "titulo": r["titulo"],
                "inicio": _salida_fecha(r["fecha_inicio"], bool(r["todo_el_dia"])),
                "fin":    _salida_fecha(r["fecha_fin"], bool(r["todo_el_dia"])) if r["fecha_fin"] else None,
                "color":  r["color_custom"] or TIPOS_EVENTO.get(r["tipo"], {}).get("color", "#60718D"),
            })
    return jsonify(by_user)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — conflictos (antes de guardar)
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/conflictos")
@login_required
@tenant_required
def ajax_conflictos():
    fi  = request.args.get("inicio", "")
    ff  = request.args.get("fin", "")
    ids = [u for u in (_safe_int(x) for x in request.args.getlist("user_id")) if u] or [session.get("user_id")]
    eid = _safe_int(request.args.get("exclude_id"))
    if not fi:
        return jsonify([])
    return jsonify(_check_conflictos(g.tenant_db, fi, ff, ids, [eid] if eid else []))


# ─────────────────────────────────────────────────────────────────────────────
#  TÉRMINOS — cómputo en días hábiles y días inhábiles del despacho
# ─────────────────────────────────────────────────────────────────────────────

@calendario_bp.route("/ajax/plazo")
@login_required
@tenant_required
def ajax_plazo():
    """Fecha en que vence un término de N días hábiles contados desde el día siguiente
    a la fecha en que surtió efectos la notificación."""
    errores = {}
    try:
        desde = date.fromisoformat((request.args.get("desde") or "")[:10])
    except ValueError:
        desde = None
        errores["desde"] = "Elige la fecha en que surtió efectos la notificación."
    dias = _safe_int(request.args.get("dias"))
    if not dias or dias < 1:
        errores["dias"] = "Escribe cuántos días hábiles da la ley o el juzgado (por ejemplo, 9 o 15)."
    elif dias > plazos.MAX_DIAS:
        errores["dias"] = f"Para más de {plazos.MAX_DIAS} días, calcula a mano."
    if errores:
        return jsonify(ok=False, errores=errores, error=next(iter(errores.values()))), 400
    r = plazos.sumar_dias_habiles(desde, dias, g.tenant_db)
    vence = r["vence"]
    return jsonify(ok=True, vence=vence.isoformat(), vence_txt=_fecha_larga(vence),
                   saltados=[{**s, "txt": _fecha_larga(date.fromisoformat(s["fecha"]))} for s in r["saltados"]])


@calendario_bp.route("/ajax/inhabiles")
@login_required
@tenant_required
def ajax_inhabiles():
    """Días inhábiles en un rango: los oficiales y los que marcó el despacho.
    Sin rango devuelve la lista del despacho para administrarla."""
    tdb = g.tenant_db
    plazos.asegurar(tdb)
    start, end = request.args.get("start"), request.args.get("end")
    if start and end:
        try:
            a, b = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        except ValueError:
            return jsonify({})
        if (b - a).days > 400:
            b = a + timedelta(days=400)
        extra = plazos.inhabiles_despacho(tdb, a, b)
        salida, d = {}, a
        while d < b:
            m = plazos.descansos_oficiales(d.year).get(d) or extra.get(d)
            if m:
                salida[d.isoformat()] = m
            d += timedelta(days=1)
        return jsonify(salida)
    filas = tdb.execute(
        "SELECT id, desde, hasta, motivo FROM dias_inhabiles WHERE date(hasta) >= date(?, '-1 year') ORDER BY desde",
        (hoy_mx().isoformat(),)).fetchall()
    return jsonify([dict(r) for r in filas])


@calendario_bp.route("/ajax/inhabiles", methods=["POST"])
@login_required
@tenant_required
def ajax_inhabiles_agregar():
    tdb = g.tenant_db
    plazos.asegurar(tdb)
    data = request.get_json(silent=True) or {}
    errores = {}
    try:
        desde = date.fromisoformat((data.get("desde") or "")[:10])
    except ValueError:
        desde = None
        errores["desde"] = "Elige el primer día."
    try:
        hasta = date.fromisoformat((data.get("hasta") or "")[:10]) if data.get("hasta") else desde
    except ValueError:
        hasta = None
        errores["hasta"] = "No entendimos el último día."
    if desde and hasta and hasta < desde:
        errores["hasta"] = "El último día no puede ser antes del primero."
    elif desde and hasta and (hasta - desde).days > 120:
        errores["hasta"] = "Marca periodos de máximo 120 días."
    motivo = (data.get("motivo") or "").strip()[:120]
    if not motivo:
        errores["motivo"] = "Escribe el motivo, por ejemplo «Vacaciones del Poder Judicial»."
    if errores:
        return jsonify(ok=False, errores=errores, error=next(iter(errores.values()))), 400
    cur = tdb.execute("INSERT INTO dias_inhabiles (desde, hasta, motivo, creado_por) VALUES (?,?,?,?)",
                      (desde.isoformat(), hasta.isoformat(), motivo, session.get("user_id")))
    tdb.commit()
    _log("agregar_dia_inhabil", cur.lastrowid, {"desde": desde.isoformat(), "hasta": hasta.isoformat()})
    return jsonify(ok=True, id=cur.lastrowid)


@calendario_bp.route("/ajax/inhabiles/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
def ajax_inhabiles_eliminar(id: int):
    tdb = g.tenant_db
    plazos.asegurar(tdb)
    fila = tdb.execute("SELECT creado_por FROM dias_inhabiles WHERE id=?", (id,)).fetchone()
    if not fila:
        return jsonify(ok=False, error="Ese día ya no está marcado."), 404
    if _solo_lo_suyo() and fila["creado_por"] != session.get("user_id"):
        return jsonify(ok=False, error="Solo quien lo marcó o un administrador puede quitarlo."), 403
    tdb.execute("DELETE FROM dias_inhabiles WHERE id=?", (id,))
    tdb.commit()
    _log("quitar_dia_inhabil", id)
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR .ics (iCalendar)
# ─────────────────────────────────────────────────────────────────────────────

def _ics_texto(v) -> str:
    return (str(v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


@calendario_bp.route("/exportar-ics")
@login_required
@tenant_required
def exportar_ics():
    tdb    = g.tenant_db
    start  = request.args.get("start", "")
    end    = request.args.get("end", "")
    alc, alc_p = alcance_eventos()
    where, params = ["e.estado != 'cancelado'", alc], list(alc_p)
    if start:
        where.append("date(COALESCE(e.fecha_fin, e.fecha_inicio)) >= date(?)"); params.append(start)
    if end:
        where.append("date(e.fecha_inicio) < date(?)"); params.append(end)

    rows = tdb.execute(
        f"SELECT * FROM eventos_calendario e WHERE {' AND '.join(where)} ORDER BY datetime(e.fecha_inicio)",
        params,
    ).fetchall()

    tenant = g.tenant
    cal_name = _ics_texto(tenant["nombre_despacho"] if tenant else "Habeas Corpus")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Habeas Corpus//Calendario//ES",
        f"X-WR-CALNAME:{cal_name}",
        "X-WR-TIMEZONE:America/Mexico_City",
    ]
    sello = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for r in rows:
        ini, _ = _parse_fecha(r["fecha_inicio"])
        if not ini:
            continue
        fin, _ = _parse_fecha(r["fecha_fin"])
        lines += ["BEGIN:VEVENT", f"UID:{r['id']}-{g.tenant['id'] if tenant else 0}@habeascorpus", f"DTSTAMP:{sello}"]
        if r["todo_el_dia"]:
            ultimo = (fin or ini).date()
            lines += [f"DTSTART;VALUE=DATE:{ini:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{ultimo + timedelta(days=1):%Y%m%d}"]
        else:
            fin = fin if fin and fin > ini else ini + timedelta(hours=1)
            lines += [f"DTSTART;TZID=America/Mexico_City:{ini:%Y%m%dT%H%M%S}",
                      f"DTEND;TZID=America/Mexico_City:{fin:%Y%m%dT%H%M%S}"]
        lines += [f"SUMMARY:{_ics_texto(r['titulo'])}"]
        if r["descripcion"]:
            lines.append(f"DESCRIPTION:{_ics_texto(r['descripcion'])}")
        if r["lugar"]:
            lines.append(f"LOCATION:{_ics_texto(r['lugar'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines) + "\r\n"

    buf = io.BytesIO(content.encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/calendar", as_attachment=True,
                     download_name=f"calendario_{hoy_mx()}.ics")


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
            return

        # Normalize date-time to ISO 8601
        fi = _normalize_dt(ev["fecha_inicio"])
        ff = _normalize_dt(ev["fecha_fin"] or ev["fecha_inicio"])

        if ev["todo_el_dia"]:
            # En Google el último día de un evento de todo el día no se incluye
            fin_dia = (date.fromisoformat(ff[:10]) + timedelta(days=1)).isoformat()
            body = {
                "summary": ev["titulo"],
                "description": ev["descripcion"] or "",
                "location": ev["lugar"] or "",
                "start": {"date": fi[:10]},
                "end":   {"date": fin_dia},
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
    except Exception:
        current_app.logger.exception("No se pudo enviar el evento a Google Calendar")


@calendario_bp.route("/oauth/google")
@login_required
@tenant_required
def oauth_google():
    """Redirect user to Google OAuth2 consent screen."""
    if not current_app.config.get("GOOGLE_CLIENT_ID"):
        flash("La conexión con Google Calendar aún no está disponible en este despacho.", "warning")
        return redirect(url_for("calendario.index"))
    try:
        from config import Config
        from google_auth_oauthlib.flow import Flow

        # Build redirect_uri explicitly — must match Google Console exactly
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
    except Exception:
        current_app.logger.exception("No se pudo iniciar la conexión con Google Calendar")
        flash("No se pudo conectar con Google Calendar. Intenta más tarde.", "danger")
        return redirect(url_for("calendario.index"))


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
