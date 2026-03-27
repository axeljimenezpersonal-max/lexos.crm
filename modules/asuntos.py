"""
LexOS — modules/asuntos.py
Módulo de asuntos/expedientes: listado 3 vistas (cards/tabla/kanban),
creación con plantillas, ficha detallada 7 tabs, Gantt, semáforo, horas.
"""

from __future__ import annotations

import io
import json
import math
import os
import uuid
from datetime import date, datetime

from flask import (
    Blueprint,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from modules.auth import login_required, role_required, tenant_required

asuntos_bp = Blueprint("asuntos", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

TIPOS_DERECHO = {
    "civil":          {"label": "Civil",           "color": "#00DFA2", "icon": "scale",
                       "subtypes": ["Divorcio","Sucesión","Responsabilidad civil","Obligaciones","Contratos","Otro"]},
    "mercantil":      {"label": "Mercantil",       "color": "#0EA5E9", "icon": "building",
                       "subtypes": ["Contratos mercantiles","Sociedades","Concurso mercantil","Títulos de crédito","Otro"]},
    "laboral":        {"label": "Laboral",         "color": "#F59E0B", "icon": "hard_hat",
                       "subtypes": ["Despido injustificado","Reinstalación","Prestaciones","Seguridad social","Otro"]},
    "penal":          {"label": "Penal",           "color": "#EF4444", "icon": "lock",
                       "subtypes": ["Defensa","Querella","Delitos patrimoniales","Delitos contra la persona","Otro"]},
    "familiar":       {"label": "Familiar",        "color": "#EC4899", "icon": "heart",
                       "subtypes": ["Divorcio","Custodia","Pensión alimenticia","Adopción","Otro"]},
    "administrativo": {"label": "Administrativo",  "color": "#8B5CF6", "icon": "landmark",
                       "subtypes": ["IMSS","ISSSTE","SAT","Permisos y licencias","Otro"]},
    "fiscal":         {"label": "Fiscal",          "color": "#10B981", "icon": "chart",
                       "subtypes": ["Controversia fiscal","Devoluciones","Resoluciones SAT","Otro"]},
    "amparo":         {"label": "Amparo",          "color": "#F97316", "icon": "shield",
                       "subtypes": ["Amparo directo","Amparo indirecto","Amparo en revisión","Otro"]},
    "inmobiliario":   {"label": "Inmobiliario",    "color": "#14B8A6", "icon": "home",
                       "subtypes": ["Compraventa","Arrendamiento","Usucapión","Fraccionamiento","Otro"]},
    "constitucional": {"label": "Constitucional",  "color": "#A78BFA", "icon": "scroll",
                       "subtypes": ["Acción de inconstitucionalidad","Controversia constitucional","Otro"]},
    "internacional":  {"label": "Internacional",   "color": "#06B6D4", "icon": "globe",
                       "subtypes": ["Arbitraje","Contratos internacionales","Otro"]},
    "otro":           {"label": "Otro",            "color": "#6B7280", "icon": "folder", "subtypes": []},
}

KANBAN_COLS = [
    {"key": "por_iniciar", "label": "Por Iniciar",  "color": "#6B7280"},
    {"key": "activo",      "label": "Activo",       "color": "#00DFA2"},
    {"key": "en_espera",   "label": "En Espera",    "color": "#F59E0B"},
    {"key": "completado",  "label": "Completado",   "color": "#10B981"},
    {"key": "archivado",   "label": "Archivado",    "color": "#374151"},
]

TIPOS_ACTIVIDAD = ["audiencia", "acuerdo", "diligencia", "vencimiento", "reunion", "nota", "otro"]
TIPOS_SENTENCIA = ["sentencia", "resolucion", "auto", "acuerdo", "otro"]

# Plantillas predefinidas por tipo
PLANTILLAS = {
    "civil": {
        "titulo_ejemplo": "Juicio de divorcio — [Apellidos]",
        "prioridad": "media",
        "estado": "activo",
        "descripcion": "Juicio de divorcio contencioso. Hechos:\n\n",
    },
    "mercantil": {
        "titulo_ejemplo": "Demanda mercantil — [Empresa vs Empresa]",
        "prioridad": "alta",
        "estado": "activo",
        "descripcion": "Reclamación por incumplimiento de contrato. Hechos:\n\n",
    },
    "laboral": {
        "titulo_ejemplo": "Demanda laboral — [Trabajador vs Empresa]",
        "prioridad": "alta",
        "estado": "activo",
        "descripcion": "Juicio laboral por despido injustificado. Hechos:\n\n",
    },
    "penal": {
        "titulo_ejemplo": "Causa penal — [Nombre]",
        "prioridad": "alta",
        "estado": "activo",
        "descripcion": "Proceso penal. Delito imputado:\n\n",
    },
    "amparo": {
        "titulo_ejemplo": "Amparo — [Nombre] vs [Autoridad]",
        "prioridad": "alta",
        "estado": "activo",
        "descripcion": "Juicio de amparo contra acto de autoridad. Acto reclamado:\n\n",
    },
    "familiar": {
        "titulo_ejemplo": "Divorcio / Custodia — [Apellidos]",
        "prioridad": "media",
        "estado": "activo",
        "descripcion": "Asunto de derecho familiar. Hechos:\n\n",
    },
}

PER_PAGE = 18

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(accion: str, entidad_id: int | None = None, detalles: dict | None = None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, "asunto", entidad_id,
             json.dumps(detalles or {}), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _semaforo(fecha_limite_str: str | None) -> dict:
    if not fecha_limite_str:
        return {"clase": "sem-none", "label": "Sin fecha límite", "dias": None}
    try:
        fl   = date.fromisoformat(fecha_limite_str[:10])
        dias = (fl - date.today()).days
        if dias < 0:
            return {"clase": "sem-rojo", "label": f"Vencido hace {abs(dias)}d", "dias": dias}
        elif dias <= 7:
            return {"clase": "sem-rojo", "label": f"Vence en {dias}d", "dias": dias}
        elif dias <= 30:
            return {"clase": "sem-amarillo", "label": f"Vence en {dias}d", "dias": dias}
        else:
            return {"clase": "sem-verde", "label": f"Vence en {dias}d", "dias": dias}
    except Exception:
        return {"clase": "sem-none", "label": "—", "dias": None}


def _auto_numero(tdb) -> str:
    year = date.today().year
    count = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE strftime('%Y', fecha_alta) = ?", (str(year),)
    ).fetchone()[0] + 1
    return f"EXP-{year}-{count:03d}"


def _get_abogados() -> list[dict]:
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos, cargo FROM tenant_users WHERE tenant_id=? AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _user_name(gdb, tid, uid) -> str:
    """Get user display name from tenant_users."""
    if not uid:
        return "Sistema"
    try:
        u = gdb.execute(
            "SELECT nombre, apellidos FROM tenant_users WHERE id=? AND tenant_id=?",
            (uid, tid),
        ).fetchone()
        if u:
            return f"{u['nombre']} {u['apellidos'] or ''}".strip()
    except Exception:
        pass
    return f"Usuario #{uid}"


def _enrich(row: dict, tdb) -> dict:
    """Add semáforo, tipo_info, horas_total to an asunto dict."""
    row["semaforo"] = _semaforo(row.get("fecha_limite"))
    row["tipo_info"] = TIPOS_DERECHO.get(row.get("tipo_asunto", "otro"), TIPOS_DERECHO["otro"])
    row["horas_total"] = tdb.execute(
        "SELECT COALESCE(SUM(horas),0) FROM asunto_horas WHERE asunto_id=?", (row["id"],)
    ).fetchone()[0]
    return row


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — 3 vistas
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/")
@login_required
@tenant_required
def index():
    tdb  = g.tenant_db
    role = session.get("role")
    uid  = session.get("user_id")

    q         = request.args.get("q", "").strip()
    estados   = request.args.getlist("estado")
    tipos     = request.args.getlist("tipo")
    abogado   = request.args.get("abogado", "")
    cliente_q = request.args.get("cliente", "")
    desde     = request.args.get("desde", "")
    hasta     = request.args.get("hasta", "")
    vencidos  = request.args.get("vencidos", "")
    page      = max(1, _safe_int(request.args.get("page", 1), 1))
    vista     = request.args.get("vista", "")   # cards|tabla|kanban

    where, params = ["1"], []

    if q:
        where.append("(a.titulo LIKE ? OR a.numero_expediente LIKE ? OR c.nombre LIKE ? OR c.razon_social LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if estados:
        placeholders = ",".join("?" * len(estados))
        where.append(f"a.estado IN ({placeholders})")
        params += estados
    if tipos:
        placeholders = ",".join("?" * len(tipos))
        where.append(f"a.tipo_asunto IN ({placeholders})")
        params += tipos
    if abogado:
        where.append("a.abogado_responsable=?")
        params.append(abogado)
    if cliente_q:
        where.append("(c.nombre LIKE ? OR c.razon_social LIKE ?)")
        like = f"%{cliente_q}%"
        params += [like, like]
    if desde:
        where.append("date(a.fecha_alta) >= ?")
        params.append(desde)
    if hasta:
        where.append("date(a.fecha_alta) <= ?")
        params.append(hasta)
    if vencidos == "1":
        where.append("a.fecha_limite IS NOT NULL AND a.fecha_limite < date('now')")
    if vencidos == "0":
        where.append("(a.fecha_limite IS NULL OR a.fecha_limite >= date('now'))")

    # Abogados solo ven los suyos
    if role == "abogado":
        where.append("(a.abogado_responsable=? OR a.id IN (SELECT asunto_id FROM asunto_colaboradores WHERE user_id=?))")
        params += [uid, uid]

    clause = " AND ".join(where)
    base_q = f"""SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
                        c.razon_social as cliente_razon, c.id as cid
                 FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
                 WHERE {clause}"""

    total = tdb.execute(f"SELECT COUNT(*) FROM ({base_q})", params).fetchone()[0]
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, total_pages)
    offset = (page - 1) * PER_PAGE

    rows = tdb.execute(
        f"{base_q} ORDER BY a.fecha_alta DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()

    asuntos = [_enrich(dict(r), tdb) for r in rows]

    # Kanban — load all (not paginated) for drag-drop
    kanban_data = {}
    if not vista or vista == "kanban":
        for col in KANBAN_COLS:
            krows = tdb.execute(
                f"""SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
                           c.razon_social as cliente_razon
                    FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
                    WHERE a.estado=? ORDER BY a.fecha_alta DESC LIMIT 50""",
                (col["key"],),
            ).fetchall()
            kanban_data[col["key"]] = [_enrich(dict(r), tdb) for r in krows]

    abogados = _get_abogados()

    return render_template(
        "asuntos/index.html",
        asuntos=asuntos,
        kanban_data=kanban_data,
        kanban_cols=KANBAN_COLS,
        tipos_derecho=TIPOS_DERECHO,
        total=total, page=page, total_pages=total_pages, per_page=PER_PAGE,
        q=q, estados=estados, tipos=tipos,
        abogado=abogado, cliente_q=cliente_q,
        desde=desde, hasta=hasta, vencidos=vencidos,
        abogados=abogados,
        vista=vista,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  NUEVO — form con selector de plantilla
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/nuevo", methods=["GET", "POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def nuevo():
    tdb      = g.tenant_db
    abogados = _get_abogados()

    clientes = [dict(r) for r in tdb.execute(
        """SELECT id, tipo, nombre, apellidos, razon_social
           FROM clientes WHERE activo=1 ORDER BY nombre LIMIT 200"""
    ).fetchall()]

    if request.method == "POST":
        titulo = (request.form.get("titulo") or "").strip()
        if not titulo:
            return render_template("asuntos/nuevo.html",
                err="El título es requerido.",
                tipos_derecho=TIPOS_DERECHO, abogados=abogados,
                clientes=clientes, plantillas=PLANTILLAS,
                form=request.form, auto_num=_auto_numero(tdb),
                today_str=date.today().isoformat())

        num_exp = (request.form.get("numero_expediente") or "").strip() or _auto_numero(tdb)

        cur = tdb.execute(
            """INSERT INTO asuntos
               (numero_expediente, titulo, descripcion, tipo_asunto, sub_tipo,
                estado, prioridad, cliente_id, abogado_responsable,
                juzgado, numero_juicio, fecha_inicio, fecha_limite, fecha_cierre,
                monto_estimado, honorarios_pactados, forma_cobro,
                contrario_nombre, contrario_abogado, contrario_despacho,
                notas, creado_por)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                num_exp, titulo,
                (request.form.get("descripcion") or "").strip() or None,
                request.form.get("tipo_asunto", "civil"),
                (request.form.get("sub_tipo") or "").strip() or None,
                request.form.get("estado", "activo"),
                request.form.get("prioridad", "media"),
                _safe_int(request.form.get("cliente_id")),
                _safe_int(request.form.get("abogado_responsable")),
                (request.form.get("juzgado") or "").strip() or None,
                (request.form.get("numero_juicio") or "").strip() or None,
                request.form.get("fecha_inicio") or None,
                request.form.get("fecha_limite") or None,
                request.form.get("fecha_cierre") or None,
                _safe_float(request.form.get("monto_estimado")),
                _safe_float(request.form.get("honorarios_pactados")),
                request.form.get("forma_cobro", "fijo"),
                (request.form.get("contrario_nombre") or "").strip() or None,
                (request.form.get("contrario_abogado") or "").strip() or None,
                (request.form.get("contrario_despacho") or "").strip() or None,
                (request.form.get("notas") or "").strip() or None,
                session.get("user_id"),
            ),
        )
        tdb.commit()
        new_id = cur.lastrowid

        # Colaboradores adicionales
        cols_raw = request.form.get("colaboradores", "")
        for cid_str in cols_raw.split(","):
            cid = _safe_int(cid_str.strip())
            if cid:
                tdb.execute(
                    "INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)",
                    (new_id, cid),
                )
        tdb.commit()
        _log("crear_asunto", new_id, {"titulo": titulo})
        return redirect(url_for("asuntos.detalle", id=new_id))

    return render_template(
        "asuntos/nuevo.html",
        tipos_derecho=TIPOS_DERECHO, abogados=abogados,
        clientes=clientes, plantillas=PLANTILLAS,
        form={}, auto_num=_auto_numero(tdb), err=None,
        today_str=date.today().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — datos de plantilla por tipo
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/ajax/plantilla/<tipo>")
@login_required
@tenant_required
def ajax_plantilla(tipo: str):
    p = PLANTILLAS.get(tipo, {})
    t = TIPOS_DERECHO.get(tipo, {})
    return jsonify({
        "subtypes": t.get("subtypes", []),
        "plantilla": p,
    })


@asuntos_bp.route("/ajax/auto-num")
@login_required
@tenant_required
def ajax_auto_num():
    return jsonify({"numero": _auto_numero(g.tenant_db)})


# ─────────────────────────────────────────────────────────────────────────────
#  DETALLE
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>")
@login_required
@tenant_required
def detalle(id: int):
    tdb = g.tenant_db
    gdb = g.global_db
    tid = g.tenant["id"]

    row = tdb.execute(
        """SELECT a.*,
                  c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
                  c.razon_social as cliente_razon, c.id as cid,
                  c.email as cliente_email, c.celular as cliente_celular,
                  c.rfc as cliente_rfc, c.tipo as cliente_tipo,
                  c.telefono as cliente_telefono
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
           WHERE a.id=?""",
        (id,),
    ).fetchone()
    if not row:
        return redirect(url_for("asuntos.index"))

    asunto = _enrich(dict(row), tdb)

    # Abogado responsable
    abogados = _get_abogados()
    abogado_resp = None
    for a in abogados:
        if a["id"] == asunto.get("abogado_responsable"):
            abogado_resp = dict(a)
            abogado_resp["color"] = f"hsl({a['id'] * 47 % 360}, 50%, 45%)"
            break
    abogado_nombre = f"{abogado_resp['nombre']} {abogado_resp['apellidos']}" if abogado_resp else None

    # Colaboradores (exclude responsable to avoid duplicates — Fix #8)
    resp_id = asunto.get("abogado_responsable")
    cols = tdb.execute(
        "SELECT user_id FROM asunto_colaboradores WHERE asunto_id=?", (id,)
    ).fetchall()
    col_ids = [c["user_id"] for c in cols if c["user_id"] != resp_id]
    colaboradores = []
    for a in abogados:
        if a["id"] in col_ids:
            d = dict(a)
            d["color"] = f"hsl({a['id'] * 47 % 360}, 50%, 45%)"
            colaboradores.append(d)

    # ── Unified timeline ──
    # Gather ALL activity types and merge into a single feed
    timeline = []

    # 1. Actividades (audiencia, diligencia, etc.)
    for act in tdb.execute(
        "SELECT * FROM asunto_actividades WHERE asunto_id=? ORDER BY fecha DESC", (id,)
    ).fetchall():
        a = dict(act)
        uname = _user_name(gdb, tid, a.get("creado_por"))
        timeline.append({
            "id": a["id"], "table": "asunto_actividades",
            "tipo": "actividad",
            "subtipo": a["tipo"],
            "titulo": a["titulo"],
            "contenido": a.get("descripcion") or "",
            "fecha": a.get("fecha") or a.get("fecha_alta") or "",
            "autor_id": a.get("creado_por"),
            "autor_nombre": uname,
            "realizado": a.get("realizado", 0),
            "metadata": {},
        })

    # 2. Notas internas
    for n in tdb.execute(
        "SELECT * FROM asunto_notas WHERE asunto_id=? ORDER BY fecha DESC", (id,)
    ).fetchall():
        n = dict(n)
        uname = _user_name(gdb, tid, n.get("creado_por"))
        menciones = []
        try:
            menciones = json.loads(n.get("menciones") or "[]")
        except Exception:
            pass
        timeline.append({
            "id": n["id"], "table": "asunto_notas",
            "tipo": "nota",
            "subtipo": "nota",
            "titulo": "Nota interna",
            "contenido": n["contenido"],
            "fecha": n.get("fecha") or "",
            "autor_id": n.get("creado_por"),
            "autor_nombre": uname,
            "realizado": 0,
            "metadata": {"menciones": menciones},
        })

    # 3. Documentos
    for d in tdb.execute(
        "SELECT * FROM documentos WHERE asunto_id=? ORDER BY fecha_subida DESC", (id,)
    ).fetchall():
        d = dict(d)
        uname = _user_name(gdb, tid, d.get("subido_por"))
        timeline.append({
            "id": d["id"], "table": "documentos",
            "tipo": "documento",
            "subtipo": (d.get("mime_type") or "").split("/")[-1][:10],
            "titulo": d.get("nombre") or d.get("archivo_nombre") or "Documento",
            "contenido": "",
            "fecha": d.get("fecha_subida") or "",
            "autor_id": d.get("subido_por"),
            "autor_nombre": uname,
            "realizado": 0,
            "metadata": {
                "url": d.get("archivo_url"),
                "archivo_nombre": d.get("archivo_nombre"),
                "mime_type": d.get("mime_type") or "",
                "tamanio": d.get("tamanio") or 0,
            },
        })

    # 4. Sentencias
    for s in tdb.execute(
        "SELECT * FROM asunto_sentencias WHERE asunto_id=? ORDER BY fecha_alta DESC", (id,)
    ).fetchall():
        s = dict(s)
        uname = _user_name(gdb, tid, s.get("creado_por"))
        timeline.append({
            "id": s["id"], "table": "asunto_sentencias",
            "tipo": "sentencia",
            "subtipo": s.get("tipo") or "sentencia",
            "titulo": s["titulo"],
            "contenido": s.get("contenido") or "",
            "fecha": s.get("fecha_emision") or s.get("fecha_alta") or "",
            "autor_id": s.get("creado_por"),
            "autor_nombre": uname,
            "realizado": 0,
            "metadata": {
                "archivo_url": s.get("archivo_url"),
                "archivo_nombre": s.get("archivo_nombre"),
            },
        })

    # 5. Status changes from actividad_log
    for h in tdb.execute(
        "SELECT * FROM actividad_log WHERE entidad='asunto' AND entidad_id=? "
        "AND accion IN ('cambiar_estado_asunto','crear_asunto','archivar_asunto') "
        "ORDER BY fecha DESC LIMIT 20",
        (id,),
    ).fetchall():
        h = dict(h)
        uname = _user_name(gdb, tid, h.get("user_id"))
        detalles = {}
        try:
            detalles = json.loads(h.get("detalles") or "{}")
        except Exception:
            pass
        # Build descriptive title (point 4)
        accion = h.get("accion", "")
        if accion == "crear_asunto":
            titulo_tl = f"Creó el asunto {asunto.get('numero_expediente', '')}"
        elif accion == "cambiar_estado_asunto":
            de = detalles.get("de", "?").replace("_", " ").upper()
            a_est = detalles.get("a", detalles.get("estado", "?")).replace("_", " ").upper()
            titulo_tl = f"Cambió el estatus de {de} → {a_est}"
        elif accion == "archivar_asunto":
            titulo_tl = "Archivó el asunto"
        else:
            titulo_tl = accion.replace("_", " ").title()
        timeline.append({
            "id": h["id"], "table": "actividad_log",
            "tipo": "cambio_estatus",
            "subtipo": accion,
            "titulo": titulo_tl,
            "contenido": "",
            "fecha": h.get("fecha") or "",
            "autor_id": h.get("user_id"),
            "autor_nombre": uname,
            "realizado": 0,
            "metadata": detalles,
        })

    # Sort timeline by date descending
    def _sort_key(item):
        f = item.get("fecha") or ""
        return f[:19] if f else "0"
    timeline.sort(key=_sort_key, reverse=True)

    # ── Sidebar data ──
    # Cobros
    cobros = [dict(c) for c in tdb.execute(
        "SELECT * FROM cobros WHERE asunto_id=? ORDER BY fecha_emision DESC", (id,)
    ).fetchall()]
    cobros_resumen = {
        "total":     sum(_safe_float(c.get("monto")) for c in cobros),
        "pagado":    sum(_safe_float(c.get("monto")) for c in cobros if c.get("estado") == "pagado"),
        "pendiente": sum(_safe_float(c.get("monto")) for c in cobros if c.get("estado") == "pendiente"),
    }

    # Eventos próximos
    eventos_prox = [dict(e) for e in tdb.execute(
        """SELECT id, titulo, tipo, fecha_inicio, fecha_fin, lugar, estado
           FROM eventos_calendario
           WHERE asunto_id=? AND estado='pendiente' AND fecha_inicio >= datetime('now')
           ORDER BY fecha_inicio ASC LIMIT 5""",
        (id,),
    ).fetchall()]

    # Documentos recientes (for sidebar)
    docs_recientes = [dict(d) for d in tdb.execute(
        "SELECT id, nombre, archivo_url, archivo_nombre, mime_type, tamanio, fecha_subida "
        "FROM documentos WHERE asunto_id=? ORDER BY fecha_subida DESC LIMIT 4",
        (id,),
    ).fetchall()]

    # Horas
    horas_rows = [dict(h) for h in tdb.execute(
        "SELECT * FROM asunto_horas WHERE asunto_id=? ORDER BY fecha DESC", (id,)
    ).fetchall()]
    horas_total = sum(_safe_float(h.get("horas")) for h in horas_rows)

    # Tareas/actividades pendientes (for sidebar)
    tareas_pend = [dict(t) for t in tdb.execute(
        "SELECT id, titulo, tipo, fecha FROM asunto_actividades "
        "WHERE asunto_id=? AND realizado=0 ORDER BY fecha ASC LIMIT 6",
        (id,),
    ).fetchall()]

    # Timeline counters (point 3)
    tl_counts = {"all": len(timeline)}
    for key in ("nota", "actividad", "documento", "sentencia", "llamada", "cambio_estatus"):
        tl_counts[key] = sum(1 for t in timeline if t["tipo"] == key)

    # User role for admin-only UI
    user_role = session.get("role", "abogado")

    # ── Column A computed variables ──────────────────────────────────────
    from datetime import datetime as _dt

    # dias_activo — days since fecha_inicio
    _fi = asunto.get("fecha_inicio") or ""
    try:
        dias_activo = (date.today() - date.fromisoformat(_fi[:10])).days if _fi else None
    except Exception:
        dias_activo = None

    # ultima_actividad — most recent timeline entry date
    ultima_actividad = timeline[0]["fecha"][:10] if timeline else None

    # proxima_audiencia — next future actividad of type audiencia/diligencia
    _hoy = date.today().isoformat()
    proxima_audiencia = None
    for act in tdb.execute(
        "SELECT titulo, fecha FROM asunto_actividades "
        "WHERE asunto_id=? AND realizado=0 AND fecha>=? "
        "ORDER BY fecha ASC LIMIT 1",
        (id, _hoy),
    ).fetchall():
        proxima_audiencia = {"titulo": act["titulo"], "fecha": act["fecha"][:10] if act["fecha"] else ""}

    # audiencias_realizadas — count of completed activities
    audiencias_realizadas = tdb.execute(
        "SELECT COUNT(*) as c FROM asunto_actividades WHERE asunto_id=? AND realizado=1", (id,)
    ).fetchone()["c"]

    # documentos_count
    documentos_count = tdb.execute(
        "SELECT COUNT(*) as c FROM documentos WHERE asunto_id=?", (id,)
    ).fetchone()["c"]

    # notas_count
    notas_count = tdb.execute(
        "SELECT COUNT(*) as c FROM asunto_notas WHERE asunto_id=?", (id,)
    ).fetchone()["c"]

    # facturas_count (cobros)
    facturas_count = len(cobros)

    # dias_pago_promedio — avg days between fecha_emision and fecha_pago for paid cobros
    dias_pago_promedio = None
    try:
        _paid = [dict(c) for c in tdb.execute(
            "SELECT fecha_emision, fecha_pago FROM cobros "
            "WHERE asunto_id=? AND estado='pagado' AND fecha_emision IS NOT NULL AND fecha_pago IS NOT NULL",
            (id,),
        ).fetchall()]
        if _paid:
            _days = []
            for p in _paid:
                try:
                    d1 = date.fromisoformat(p["fecha_emision"][:10])
                    d2 = date.fromisoformat(p["fecha_pago"][:10])
                    _days.append((d2 - d1).days)
                except Exception:
                    pass
            if _days:
                dias_pago_promedio = round(sum(_days) / len(_days))
    except Exception:
        pass

    # gastos_total for this asunto (table may not exist yet)
    gastos_total = 0
    try:
        _gt = tdb.execute(
            "SELECT COALESCE(SUM(monto),0) as t FROM gastos_asunto WHERE asunto_id=?", (id,)
        ).fetchone()
        if _gt:
            gastos_total = _gt["t"]
    except Exception:
        gastos_total = 0

    col_a_stats = {
        "dias_activo": dias_activo,
        "ultima_actividad": ultima_actividad,
        "proxima_audiencia": proxima_audiencia,
        "audiencias_realizadas": audiencias_realizadas,
        "documentos_count": documentos_count,
        "notas_count": notas_count,
        "facturas_count": facturas_count,
        "dias_pago_promedio": dias_pago_promedio,
        "gastos_total": gastos_total,
    }

    # ── Ficha Técnica (Nota 9) ───────────────────────────────────────────
    ficha_row = None
    try:
        ficha_row = tdb.execute(
            "SELECT * FROM ficha_tecnica WHERE asunto_id=?", (id,)
        ).fetchone()
    except Exception:
        pass
    ficha = dict(ficha_row) if ficha_row else None
    if ficha:
        import json as _json
        for k in ("argumentos_clave", "riesgos", "insights"):
            try:
                ficha[k] = _json.loads(ficha.get(k) or "[]")
            except Exception:
                ficha[k] = []

    # ── Etiquetas del asunto (Nota 4) ────────────────────────────────────
    etiquetas = []
    try:
        etiquetas = [dict(r) for r in tdb.execute(
            """SELECT e.* FROM etiquetas e
               JOIN asunto_etiquetas ae ON ae.etiqueta_id = e.id
               WHERE ae.asunto_id = ?
               ORDER BY e.nombre""",
            (id,),
        ).fetchall()]
    except Exception:
        pass

    # All available etiquetas for the tag picker
    all_etiquetas = []
    try:
        all_etiquetas = [dict(r) for r in tdb.execute(
            "SELECT * FROM etiquetas ORDER BY nombre"
        ).fetchall()]
    except Exception:
        pass

    # ── Timeline enrichment & date grouping ──────────────────────────────
    _MESES_ES = ['enero','febrero','marzo','abril','mayo','junio',
                 'julio','agosto','septiembre','octubre','noviembre','diciembre']

    # Enrich each timeline item with display metadata
    for _it in timeline:
        # Initials
        _nombre = _it.get('autor_nombre') or ''
        _partes = _nombre.strip().split()
        if not _partes:
            _it['iniciales'] = 'SIS'
        elif len(_partes) >= 2:
            _it['iniciales'] = (_partes[0][0] + _partes[-1][0]).upper()
        else:
            _it['iniciales'] = _partes[0][:2].upper()

        # Color / badge based on type
        _tipo    = _it.get('tipo', 'sistema')
        _subtipo = _it.get('subtipo', '')
        _meta    = _it.get('metadata')
        _a_est   = (_meta.get('a', '') if isinstance(_meta, dict) else '') if _meta else ''

        if _tipo == 'nota':
            _it['tl_cls']      = 'nota'
            _it['tl_color']    = '#00DFA2'
            _it['tl_av_bg']    = 'rgba(0,223,162,0.12)'
            _it['tl_badge_bg'] = 'rgba(0,223,162,0.1)'
            _it['tl_badge']    = 'Nota'
        elif _tipo in ('documento', 'sentencia'):
            _it['tl_cls']      = 'doc'
            _it['tl_color']    = '#3B8BD4'
            _it['tl_av_bg']    = 'rgba(59,139,212,0.1)'
            _it['tl_badge_bg'] = 'rgba(59,139,212,0.1)'
            _it['tl_badge']    = 'Documento' if _tipo == 'documento' else 'Sentencia'
        elif _tipo == 'actividad':
            _it['tl_cls']      = 'actividad'
            _it['tl_color']    = '#F97316'
            _it['tl_av_bg']    = 'rgba(249,115,22,0.1)'
            _it['tl_badge_bg'] = 'rgba(249,115,22,0.1)'
            _it['tl_badge']    = 'Actividad'
        elif _tipo in ('llamada', 'correo'):
            _it['tl_cls']      = 'llamada'
            _it['tl_color']    = '#8B5CF6'
            _it['tl_av_bg']    = 'rgba(139,92,246,0.1)'
            _it['tl_badge_bg'] = 'rgba(139,92,246,0.1)'
            _it['tl_badge']    = 'Llamada' if _tipo == 'llamada' else 'Correo'
        elif _tipo == 'cambio_estatus':
            if _subtipo == 'archivar_asunto' or _a_est == 'archivado':
                _it['tl_cls']      = 'estado-arch'
                _it['tl_color']    = '#EF4444'
                _it['tl_av_bg']    = 'rgba(239,68,68,0.1)'
                _it['tl_badge_bg'] = 'rgba(239,68,68,0.1)'
                _it['tl_badge']    = '→ Archivado'
            elif _a_est == 'activo':
                _it['tl_cls']      = 'estado-activo'
                _it['tl_color']    = '#00DFA2'
                _it['tl_av_bg']    = 'rgba(0,223,162,0.1)'
                _it['tl_badge_bg'] = 'rgba(0,223,162,0.1)'
                _it['tl_badge']    = '→ Activo'
            elif _subtipo == 'crear_asunto':
                _it['tl_cls']      = 'sistema'
                _it['tl_color']    = '#8A9BA8'
                _it['tl_av_bg']    = 'rgba(138,155,168,0.08)'
                _it['tl_badge_bg'] = 'rgba(138,155,168,0.08)'
                _it['tl_badge']    = 'Sistema'
            else:
                _lbl = ('→ ' + _a_est.replace('_', ' ').title()) if _a_est else 'Cambio'
                _it['tl_cls']      = 'estado-cambio'
                _it['tl_color']    = '#B8922A'
                _it['tl_av_bg']    = 'rgba(184,146,42,0.1)'
                _it['tl_badge_bg'] = 'rgba(184,146,42,0.1)'
                _it['tl_badge']    = _lbl
        else:
            _it['tl_cls']      = 'sistema'
            _it['tl_color']    = '#8A9BA8'
            _it['tl_av_bg']    = 'rgba(138,155,168,0.08)'
            _it['tl_badge_bg'] = 'rgba(138,155,168,0.08)'
            _it['tl_badge']    = 'Sistema'

        # ── Timezone: UTC → México (UTC-6) ──
        from datetime import datetime as _datetime_cls, timedelta as _td
        _MX = _td(hours=-6)
        _fecha_str = _it.get('fecha') or ''
        try:
            _dt_mx = _datetime_cls.fromisoformat(_fecha_str[:19]) + _MX
            _it['tl_hora']    = _dt_mx.strftime('%H:%M')
            _it['_fecha_mx']  = _dt_mx.date()
        except Exception:
            _it['tl_hora']   = _fecha_str[11:16] if len(_fecha_str) > 10 else _fecha_str
            _it['_fecha_mx'] = None

    # Hoy en México (UTC-6)
    from datetime import datetime as _datetime_cls, timedelta as _td, timezone as _tz
    _hoy_mx = (_datetime_cls.now(_tz.utc) + _td(hours=-6)).date()

    # Group by Mexico date (timeline already sorted descending)
    grupos = []
    _seen_dates = []
    _date_map = {}
    for _it in timeline:
        _mx_date = _it.get('_fecha_mx')
        if _mx_date is None:
            try:
                _mx_date = date.fromisoformat((_it.get('fecha') or '')[:10])
            except Exception:
                _mx_date = _hoy_mx
        _fkey = str(_mx_date)
        if _fkey not in _date_map:
            _diff = (_hoy_mx - _mx_date).days
            _mes  = _MESES_ES[_mx_date.month - 1]
            _dl   = f"{_mx_date.day} de {_mes} {_mx_date.year}"
            if _diff == 0:
                _label = 'Hoy · ' + _dl
            elif _diff == 1:
                _label = 'Ayer · ' + _dl
            else:
                _label = _dl
            _date_map[_fkey] = {'label': _label, 'registros': []}
            _seen_dates.append(_fkey)
        _date_map[_fkey]['registros'].append(_it)
    grupos = [_date_map[d] for d in _seen_dates]

    return render_template(
        "asuntos/detalle.html",
        asunto=asunto,
        abogado_resp=abogado_resp,
        abogado_nombre=abogado_nombre,
        colaboradores=colaboradores,
        timeline=timeline,
        grupos=grupos,
        tl_counts=tl_counts,
        cobros=cobros,
        cobros_resumen=cobros_resumen,
        eventos_prox=eventos_prox,
        docs_recientes=docs_recientes,
        horas_rows=horas_rows,
        horas_total=horas_total,
        tareas_pend=tareas_pend,
        abogados=abogados,
        tipos_derecho=TIPOS_DERECHO,
        tipos_actividad=TIPOS_ACTIVIDAD,
        tipos_sentencia=TIPOS_SENTENCIA,
        kanban_cols=KANBAN_COLS,
        today_str=date.today().isoformat(),
        user_role=user_role,
        col_a_stats=col_a_stats,
        ficha=ficha,
        etiquetas=etiquetas,
        all_etiquetas=all_etiquetas,
        es_responsable=(asunto.get("abogado_responsable") == session.get("user_id")),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FICHA TÉCNICA (Nota 9)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/ficha", methods=["POST", "PUT"])
@login_required
@tenant_required
def guardar_ficha(id):
    """Create or update the strategic brief for a case."""
    tdb = g.tenant_db
    data = request.get_json(force=True)
    import json as _json

    existing = tdb.execute("SELECT id FROM ficha_tecnica WHERE asunto_id=?", (id,)).fetchone()
    if existing:
        tdb.execute(
            """UPDATE ficha_tecnica SET
                objetivo_cliente=?, via_procesal=?, justificacion_via=?,
                argumentos_clave=?, riesgos=?, probabilidad_exito=?,
                insights=?, updated_at=CURRENT_TIMESTAMP
               WHERE asunto_id=?""",
            (
                data.get("objetivo_cliente", ""),
                data.get("via_procesal", ""),
                data.get("justificacion_via", ""),
                _json.dumps(data.get("argumentos_clave", []), ensure_ascii=False),
                _json.dumps(data.get("riesgos", []), ensure_ascii=False),
                data.get("probabilidad_exito", "media"),
                _json.dumps(data.get("insights", []), ensure_ascii=False),
                id,
            ),
        )
    else:
        tdb.execute(
            """INSERT INTO ficha_tecnica
               (asunto_id, objetivo_cliente, via_procesal, justificacion_via,
                argumentos_clave, riesgos, probabilidad_exito, insights)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                id,
                data.get("objetivo_cliente", ""),
                data.get("via_procesal", ""),
                data.get("justificacion_via", ""),
                _json.dumps(data.get("argumentos_clave", []), ensure_ascii=False),
                _json.dumps(data.get("riesgos", []), ensure_ascii=False),
                data.get("probabilidad_exito", "media"),
                _json.dumps(data.get("insights", []), ensure_ascii=False),
            ),
        )
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ETIQUETAS (Nota 4)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/etiquetas", methods=["POST"])
@login_required
@tenant_required
def agregar_etiqueta(id):
    """Add a tag to a case. Creates the tag if it doesn't exist."""
    tdb = g.tenant_db
    data = request.get_json(force=True)
    nombre = (data.get("nombre") or "").strip()
    color = data.get("color", "#6C5CE7")
    etiqueta_id = data.get("etiqueta_id")

    if not etiqueta_id and not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400

    if not etiqueta_id:
        # Check if tag with same name exists
        existing = tdb.execute("SELECT id FROM etiquetas WHERE nombre=?", (nombre,)).fetchone()
        if existing:
            etiqueta_id = existing["id"]
        else:
            cur = tdb.execute(
                "INSERT INTO etiquetas (nombre, color) VALUES (?,?)", (nombre, color)
            )
            etiqueta_id = cur.lastrowid

    # Link to asunto (ignore if already linked)
    try:
        tdb.execute(
            "INSERT OR IGNORE INTO asunto_etiquetas (asunto_id, etiqueta_id) VALUES (?,?)",
            (id, etiqueta_id),
        )
        tdb.commit()
    except Exception:
        pass

    tag = tdb.execute("SELECT * FROM etiquetas WHERE id=?", (etiqueta_id,)).fetchone()
    return jsonify(ok=True, etiqueta=dict(tag) if tag else {})


@asuntos_bp.route("/<int:id>/etiquetas/<int:eid>", methods=["DELETE"])
@login_required
@tenant_required
def quitar_etiqueta(id, eid):
    """Remove a tag from a case."""
    tdb = g.tenant_db
    tdb.execute(
        "DELETE FROM asunto_etiquetas WHERE asunto_id=? AND etiqueta_id=?", (id, eid)
    )
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  EDICIÓN INLINE
# ─────────────────────────────────────────────────────────────────────────────

_EDITABLE = {
    "titulo", "descripcion", "tipo_asunto", "sub_tipo", "estado", "prioridad",
    "numero_expediente", "juzgado", "numero_juicio", "fecha_inicio", "fecha_limite",
    "fecha_cierre", "monto_estimado", "honorarios_pactados", "forma_cobro",
    "contrario_nombre", "contrario_abogado", "contrario_despacho",
    "cliente_id", "abogado_responsable", "notas",
}


@asuntos_bp.route("/<int:id>/editar-campo", methods=["POST"])
@login_required
@tenant_required
def editar_campo(id: int):
    tdb  = g.tenant_db
    row  = tdb.execute("SELECT id FROM asuntos WHERE id=?", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404

    payload = request.json or {}
    campo   = payload.get("campo", "")
    valor   = payload.get("valor")

    # Special case: multi-field update (edit modal)
    if campo == "multi":
        updates = []
        for k, v in (valor or {}).items():
            if k in _EDITABLE:
                if k in ("monto_estimado", "honorarios_pactados"):
                    v = _safe_float(v)
                elif k in ("cliente_id", "abogado_responsable"):
                    v = _safe_int(v)
                updates.append((k, v))
        for k, v in updates:
            tdb.execute(f"UPDATE asuntos SET {k}=? WHERE id=?", (v, id))
        if updates:
            tdb.commit()
            _log("actualizar_asunto", id, {"campos": [k for k, _ in updates]})
        return jsonify(ok=True)

    # Special case: add collaborator
    if campo == "colaborador_add":
        uid = _safe_int(valor)
        if uid:
            tdb.execute(
                "INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)",
                (id, uid),
            )
            tdb.commit()
            _log("agregar_colaborador", id, {"user_id": uid})
        return jsonify(ok=True)

    if campo not in _EDITABLE:
        return jsonify(ok=False, error="Campo no permitido"), 400

    if campo in ("monto_estimado", "honorarios_pactados"):
        valor = _safe_float(valor)
    elif campo in ("cliente_id", "abogado_responsable"):
        valor = _safe_int(valor)

    old_val = tdb.execute(f"SELECT {campo} FROM asuntos WHERE id=?", (id,)).fetchone()
    tdb.execute(f"UPDATE asuntos SET {campo}=? WHERE id=?", (valor, id))
    tdb.commit()
    _log("actualizar_asunto", id, {"campo": campo, "valor": valor})
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  CAMBIAR ESTADO (Kanban drag & drop)
# ─────────────────────────────────────────────────────────────────────────────

VALID_STATES = {c["key"] for c in KANBAN_COLS}


@asuntos_bp.route("/<int:id>/estado", methods=["POST"])
@login_required
@tenant_required
def cambiar_estado(id: int):
    nuevo_estado = (request.json or {}).get("estado", "")
    if nuevo_estado not in VALID_STATES:
        return jsonify(ok=False, error="Estado inválido"), 400
    old = g.tenant_db.execute("SELECT estado FROM asuntos WHERE id=?", (id,)).fetchone()
    old_estado = old["estado"] if old else "?"
    g.tenant_db.execute("UPDATE asuntos SET estado=? WHERE id=?", (nuevo_estado, id))
    g.tenant_db.commit()
    _log("cambiar_estado_asunto", id, {
        "de": old_estado,
        "a": nuevo_estado,
        "estado": nuevo_estado,
    })
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ELIMINAR (archiva)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def eliminar(id: int):
    tdb = g.tenant_db
    row = tdb.execute("SELECT titulo FROM asuntos WHERE id=?", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404
    tdb.execute("UPDATE asuntos SET estado='archivado' WHERE id=?", (id,))
    tdb.commit()
    _log("archivar_asunto", id, {"titulo": row["titulo"]})
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    return redirect(url_for("asuntos.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  GESTIÓN DE EQUIPO (punto 6)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/equipo/<int:user_id>", methods=["PATCH"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "socio")
def editar_equipo(id: int, user_id: int):
    """Editar rol de un miembro o cambiar responsable."""
    tdb = g.tenant_db
    data = request.json or {}
    accion = data.get("accion", "")

    if accion == "cambiar_responsable":
        # El actual responsable baja a colaborador
        asunto = tdb.execute("SELECT abogado_responsable FROM asuntos WHERE id=?", (id,)).fetchone()
        if asunto:
            old_resp = asunto["abogado_responsable"]
            if old_resp and old_resp != user_id:
                tdb.execute(
                    "INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)",
                    (id, old_resp),
                )
        tdb.execute("UPDATE asuntos SET abogado_responsable=? WHERE id=?", (user_id, id))
        tdb.execute("DELETE FROM asunto_colaboradores WHERE asunto_id=? AND user_id=?", (id, user_id))
        tdb.commit()
        _log("cambiar_responsable", id, {"nuevo_responsable": user_id})
        return jsonify(ok=True)

    return jsonify(ok=False, error="Acción no válida"), 400


@asuntos_bp.route("/<int:id>/equipo/<int:user_id>", methods=["DELETE"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "socio")
def quitar_equipo(id: int, user_id: int):
    """Quitar colaborador del asunto."""
    tdb = g.tenant_db
    tdb.execute(
        "DELETE FROM asunto_colaboradores WHERE asunto_id=? AND user_id=?",
        (id, user_id),
    )
    tdb.commit()
    _log("quitar_colaborador", id, {"user_id": user_id})
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ACTIVIDADES
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/actividades", methods=["POST"])
@login_required
@tenant_required
def agregar_actividad(id: int):
    data   = request.json or request.form
    titulo = (data.get("titulo") or "").strip()
    fecha  = (data.get("fecha") or "").strip()
    if not titulo or not fecha:
        return jsonify(ok=False, error="Título y fecha son requeridos"), 400

    tipo = data.get("tipo", "nota")
    if tipo not in TIPOS_ACTIVIDAD:
        tipo = "nota"

    cur = g.tenant_db.execute(
        """INSERT INTO asunto_actividades
           (asunto_id, tipo, titulo, descripcion, fecha, recordatorio_dias, creado_por)
           VALUES (?,?,?,?,?,?,?)""",
        (
            id, tipo, titulo,
            (data.get("descripcion") or "").strip() or None,
            fecha,
            _safe_int(data.get("recordatorio_dias")),
            session.get("user_id"),
        ),
    )
    g.tenant_db.commit()

    # Opcionalmente crear evento en calendario
    if data.get("crear_evento"):
        try:
            g.tenant_db.execute(
                """INSERT INTO eventos_calendario
                   (titulo, tipo, asunto_id, fecha_inicio, descripcion, creado_por)
                   VALUES (?,?,?,?,?,?)""",
                (titulo, tipo if tipo in ("audiencia","reunion","plazo","tarea") else "otro",
                 id, fecha, data.get("descripcion") or None, session.get("user_id")),
            )
            g.tenant_db.commit()
        except Exception:
            pass

    _log("agregar_actividad", id, {"titulo": titulo})
    return jsonify(
        ok=True, id=cur.lastrowid,
        fecha=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


@asuntos_bp.route("/<int:id>/actividades/<int:act_id>/realizado", methods=["POST"])
@login_required
@tenant_required
def marcar_realizado(id: int, act_id: int):
    realizado = 1 if (request.json or {}).get("realizado") else 0
    g.tenant_db.execute(
        "UPDATE asunto_actividades SET realizado=? WHERE id=? AND asunto_id=?",
        (realizado, act_id, id),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  SENTENCIAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/sentencias", methods=["POST"])
@login_required
@tenant_required
def agregar_sentencia(id: int):
    titulo = (request.form.get("titulo") or "").strip()
    if not titulo:
        return jsonify(ok=False, error="Título requerido"), 400
    tipo = request.form.get("tipo", "sentencia")
    if tipo not in TIPOS_SENTENCIA:
        tipo = "sentencia"

    archivo_url = archivo_nombre = None
    f = request.files.get("archivo")
    if f and f.filename:
        from config import Config
        upload_dir = os.path.join(Config.UPLOAD_FOLDER, "asuntos", str(id), "sentencias")
        os.makedirs(upload_dir, exist_ok=True)
        ext = os.path.splitext(f.filename)[1].lower()
        safe_name = f"{uuid.uuid4().hex}{ext}"
        f.save(os.path.join(upload_dir, safe_name))
        archivo_nombre = f.filename
        archivo_url    = f"/uploads/asuntos/{id}/sentencias/{safe_name}"

    cur = g.tenant_db.execute(
        """INSERT INTO asunto_sentencias
           (asunto_id, titulo, tipo, contenido, fecha_emision, archivo_url, archivo_nombre, creado_por)
           VALUES (?,?,?,?,?,?,?,?)""",
        (id, titulo, tipo,
         (request.form.get("contenido") or "").strip() or None,
         request.form.get("fecha_emision") or None,
         archivo_url, archivo_nombre, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True, id=cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
#  HORAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/horas", methods=["POST"])
@login_required
@tenant_required
def registrar_horas(id: int):
    data  = request.json or {}
    horas = _safe_float(data.get("horas"))
    if not horas or horas <= 0:
        return jsonify(ok=False, error="Horas inválidas"), 400

    cur = g.tenant_db.execute(
        "INSERT INTO asunto_horas (asunto_id, user_id, horas, descripcion, fecha) VALUES (?,?,?,?,?)",
        (id, session.get("user_id"), horas,
         (data.get("descripcion") or "").strip() or None,
         data.get("fecha") or date.today().isoformat()),
    )
    g.tenant_db.commit()
    total = g.tenant_db.execute(
        "SELECT COALESCE(SUM(horas),0) FROM asunto_horas WHERE asunto_id=?", (id,)
    ).fetchone()[0]
    return jsonify(ok=True, id=cur.lastrowid, total_horas=total)


# ─────────────────────────────────────────────────────────────────────────────
#  NOTAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/notas", methods=["POST"])
@login_required
@tenant_required
def agregar_nota(id: int):
    data      = request.json or {}
    contenido = (data.get("contenido") or "").strip()
    if not contenido:
        return jsonify(ok=False, error="Contenido requerido"), 400

    menciones = json.dumps(data.get("menciones", []))
    cur = g.tenant_db.execute(
        "INSERT INTO asunto_notas (asunto_id, contenido, menciones, creado_por) VALUES (?,?,?,?)",
        (id, contenido, menciones, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True, id=cur.lastrowid, fecha=datetime.now().strftime("%Y-%m-%d %H:%M"))


# ─────────────────────────────────────────────────────────────────────────────
#  DOCUMENTOS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/documentos", methods=["POST"])
@login_required
@tenant_required
def subir_documento(id: int):
    tdb  = g.tenant_db
    nombre = (request.form.get("nombre") or "").strip()
    f = request.files.get("archivo")
    if not f or not f.filename:
        return jsonify(ok=False, error="Archivo requerido"), 400

    from config import Config
    upload_dir = os.path.join(Config.UPLOAD_FOLDER, "asuntos", str(id))
    os.makedirs(upload_dir, exist_ok=True)
    ext       = os.path.splitext(f.filename)[1].lower()
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest      = os.path.join(upload_dir, safe_name)
    f.save(dest)

    cur = tdb.execute(
        """INSERT INTO documentos
           (asunto_id, nombre, archivo_url, archivo_nombre, tamanio, mime_type, subido_por)
           VALUES (?,?,?,?,?,?,?)""",
        (id, nombre or f.filename,
         f"/uploads/asuntos/{id}/{safe_name}", f.filename,
         os.path.getsize(dest), f.mimetype, session.get("user_id")),
    )
    tdb.commit()
    _log("subir_documento", id, {"nombre": nombre or f.filename})
    return jsonify(ok=True, id=cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIAR CORREO (desde asunto)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/enviar-correo", methods=["POST"])
@login_required
@tenant_required
def enviar_correo_asunto(id: int):
    """Envía correo vinculado al asunto y lo registra."""
    para = (request.form.get("para") or "").strip()
    asunto_correo = (request.form.get("asunto_correo") or "").strip()
    cuerpo = (request.form.get("cuerpo") or "").strip()
    cc = (request.form.get("cc") or "").strip()

    if not para or not asunto_correo or not cuerpo:
        return jsonify(ok=False, error="Faltan campos"), 400

    # Sender info
    gdb = g.global_db
    tid = g.tenant["id"]
    sender = gdb.execute(
        "SELECT nombre, apellidos, email FROM tenant_users WHERE id=?",
        (session.get("user_id"),),
    ).fetchone()
    sender_name = f"{sender['nombre']} {sender['apellidos'] or ''}" if sender else "LexOS"
    sender_email = sender["email"] if sender else ""

    tenant = gdb.execute("SELECT nombre FROM tenants WHERE id=?", (tid,)).fetchone()
    despacho = tenant["nombre"] if tenant else "LexOS"

    asunto_row = g.tenant_db.execute("SELECT titulo FROM asuntos WHERE id=?", (id,)).fetchone()
    asunto_titulo = asunto_row["titulo"] if asunto_row else ""

    html_body = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">
      <div style="background:linear-gradient(135deg,#1e293b,#334155);padding:24px 28px">
        <div style="font-size:18px;font-weight:800;color:#fff">{despacho}</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:3px">Ref: {asunto_titulo}</div>
      </div>
      <div style="padding:28px">
        <div style="font-size:15px;font-weight:700;color:#1e293b;margin-bottom:16px">{asunto_correo}</div>
        <div style="font-size:14px;color:#374151;line-height:1.7;white-space:pre-wrap">{cuerpo}</div>
      </div>
      <div style="background:#f8fafc;padding:16px 28px;border-top:1px solid #e5e7eb">
        <div style="font-size:12px;color:#64748b">Enviado por <strong>{sender_name}</strong></div>
        <div style="font-size:10px;color:#94a3b8;margin-top:2px">LexOS · {date.today().strftime('%d/%m/%Y')}</div>
      </div>
    </div>"""

    try:
        from app import mail
        from flask_mail import Message
        recipients = [e.strip() for e in para.replace(";", ",").split(",") if "@" in e.strip()]
        cc_list = [e.strip() for e in cc.replace(";", ",").split(",") if "@" in e.strip()] if cc else []
        msg = Message(
            subject=f"{asunto_correo} — {despacho}",
            recipients=recipients,
            cc=cc_list or None,
            html=html_body,
            reply_to=sender_email or None,
        )
        mail.send(msg)
        return jsonify(ok=True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify(ok=False, error=str(exc)), 500


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORT PDF
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/pdf")
@login_required
@tenant_required
def exportar_pdf(id: int):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        return "reportlab no instalado", 500

    tdb = g.tenant_db
    row = tdb.execute(
        """SELECT a.*, c.nombre||' '||COALESCE(c.apellidos,'') as cliente_nombre,
                  c.razon_social
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id WHERE a.id=?""",
        (id,),
    ).fetchone()
    if not row:
        return redirect(url_for("asuntos.index"))

    a      = dict(row)
    tenant = g.tenant
    abogados = _get_abogados()
    abogado_nombre = next(
        (f"{ab['nombre']} {ab['apellidos']}" for ab in abogados if ab["id"] == a.get("abogado_responsable")),
        "—"
    )
    tipo_info = TIPOS_DERECHO.get(a.get("tipo_asunto", "otro"), TIPOS_DERECHO["otro"])
    semaforo  = _semaforo(a.get("fecha_limite"))
    actividades = tdb.execute(
        "SELECT * FROM asunto_actividades WHERE asunto_id=? ORDER BY fecha ASC LIMIT 20", (id,)
    ).fetchall()

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    ACCENT = colors.HexColor(tipo_info["color"])
    MUTED  = colors.HexColor("#9CA3AF")

    title_s   = ParagraphStyle("t", parent=styles["Heading1"],  fontSize=18, textColor=ACCENT, spaceAfter=4)
    sub_s     = ParagraphStyle("s", parent=styles["Normal"],    fontSize=9, textColor=MUTED, spaceAfter=10)
    section_s = ParagraphStyle("sec", parent=styles["Heading2"], fontSize=10, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
    footer_s  = ParagraphStyle("f", parent=styles["Normal"],    fontSize=7, textColor=MUTED, alignment=TA_CENTER)

    story = [
        Paragraph(a["titulo"], title_s),
        Paragraph(f"{tipo_info['label']} · {a.get('numero_expediente','—')} · {tenant['nombre_despacho'] if tenant else 'LexOS'}", sub_s),
        HRFlowable(width="100%", color=ACCENT, thickness=1),
        Spacer(1, 0.3*cm),
    ]

    def kv(rows_data):
        t = Table(rows_data, colWidths=[5*cm, 12*cm])
        t.setStyle(TableStyle([
            ("FONTSIZE", (0,0),(-1,-1), 8),
            ("TEXTCOLOR",(0,0),(0,-1), MUTED),
            ("FONTNAME", (0,0),(0,-1),"Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white, colors.HexColor("#F9FAFB")]),
            ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(t)

    story.append(Paragraph("Información general", section_s))
    kv([
        ["Expediente",  a.get("numero_expediente") or "—"],
        ["Tipo",        tipo_info["label"]],
        ["Estado",      a.get("estado","—").replace("_"," ").title()],
        ["Prioridad",   a.get("prioridad","—").title()],
        ["Cliente",     (a.get("razon_social") or a.get("cliente_nombre") or "—").strip()],
        ["Abogado",     abogado_nombre],
        ["Juzgado",     a.get("juzgado") or "—"],
        ["No. juicio",  a.get("numero_juicio") or "—"],
        ["Inicio",      (a.get("fecha_inicio") or "—")[:10]],
        ["Fecha límite", f"{(a.get('fecha_limite') or '—')[:10]}  ({semaforo['label']})"],
        ["Honorarios",  f"${(a.get('honorarios_pactados') or 0):,.0f} MXN"],
        ["Valor litigio", f"${(a.get('monto_estimado') or 0):,.0f} MXN"],
    ])

    if a.get("contrario_nombre"):
        story.append(Paragraph("Contraparte", section_s))
        kv([
            ["Nombre", a.get("contrario_nombre","—")],
            ["Abogado", a.get("contrario_abogado","—")],
            ["Despacho", a.get("contrario_despacho","—")],
        ])

    if a.get("descripcion"):
        story.append(Paragraph("Descripción / Hechos", section_s))
        story.append(Paragraph(a["descripcion"][:1000], ParagraphStyle("d", parent=styles["Normal"], fontSize=8, leading=12)))

    if actividades:
        story.append(Paragraph("Actividades / Timeline", section_s))
        ah = [["Fecha","Tipo","Actividad","Estado"]]
        for act in actividades:
            ah.append([
                str(act["fecha"])[:10], act["tipo"].title(), act["titulo"][:50],
                "✓ Realizado" if act["realizado"] else "Pendiente"
            ])
        t = Table(ah, colWidths=[2.5*cm, 2.5*cm, 10*cm, 2*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),ACCENT),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F9FAFB")]),
        ]))
        story.append(t)

    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"Generado el {date.today().strftime('%d/%m/%Y')} · LexOS", footer_s))
    doc.build(story)
    buf.seek(0)
    safe = a["titulo"].replace(" ","_")[:30]
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"asunto_{safe}.pdf")
