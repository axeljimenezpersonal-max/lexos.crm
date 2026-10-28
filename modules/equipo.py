"""
LexOS — modules/equipo.py
Módulo de Equipo: gestión de miembros del despacho.
Ruta base: /configuracion/equipo
"""
from __future__ import annotations

import json, os, hashlib
from datetime import datetime

from flask import (Blueprint, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from flask import current_app
from modules.auth import login_required, tenant_required

equipo_bp = Blueprint("equipo", __name__)

CARGOS = ["socio", "asociado", "pasante", "asistente", "admin"]
ESPECIALIDADES = [
    "civil", "penal", "mercantil", "laboral", "familiar",
    "fiscal", "amparo", "inmobiliario", "constitucional", "internacional",
    "administrativo", "otro",
]
CARGO_COLORS = {
    "socio": "#AD9060", "asociado": "#3D5A80", "pasante": "#4F6D8A",
    "asistente": "#60718D", "admin": "#B08A4A",
}
AVATAR_COLORS = [
    "#AD9060", "#8E6F4A", "#B08A4A", "#4F6D8A", "#3D5A80",
    "#B5483F", "#8C7A5B", "#4B6585", "#B08A4A", "#06B6D4",
]


def _user_color(user_id):
    return AVATAR_COLORS[user_id % len(AVATAR_COLORS)]


def _is_admin():
    return session.get("role") in ("super_admin", "admin")


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — grid de tarjetas
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/")
@login_required
@tenant_required
def index():
    gdb = g.global_db
    tdb = g.tenant_db
    tid = g.tenant["id"]

    members = [dict(r) for r in gdb.execute(
        "SELECT * FROM tenant_users WHERE tenant_id=? ORDER BY activo DESC, nombre",
        (tid,),
    ).fetchall()]

    # Enrich with stats from tenant DB
    for m in members:
        m["color"] = _user_color(m["id"])
        m["cargo"] = m.get("cargo") or "asociado"
        m["cargo_color"] = CARGO_COLORS.get(m["cargo"], "#60718D")
        try:
            m["especialidades_list"] = json.loads(m.get("especialidades") or "[]")
        except Exception:
            m["especialidades_list"] = []
        m["estatus"] = m.get("estatus") or ("activo" if m.get("activo") else "inactivo")

        # Stats from tenant DB
        try:
            m["asuntos_activos"] = tdb.execute(
                "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='activo'",
                (m["id"],),
            ).fetchone()[0]
        except Exception:
            m["asuntos_activos"] = 0
        try:
            m["asuntos_completados"] = tdb.execute(
                "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='completado'",
                (m["id"],),
            ).fetchone()[0]
        except Exception:
            m["asuntos_completados"] = 0

        # Carga
        a = m["asuntos_activos"]
        if a <= 3:
            m["carga_label"], m["carga_color"], m["carga_pct"] = "Bajo", "#4F6D8A", min(a * 15, 100)
        elif a <= 7:
            m["carga_label"], m["carga_color"], m["carga_pct"] = "Medio", "#B08A4A", min(a * 12, 100)
        else:
            m["carga_label"], m["carga_color"], m["carga_pct"] = "Alto", "#B5483F", min(a * 10, 100)

    return render_template(
        "configuracion/equipo.html",
        members=members,
        cargos=CARGOS,
        especialidades=ESPECIALIDADES,
        cargo_colors=CARGO_COLORS,
        is_admin=_is_admin(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DETALLE — datos para modal (AJAX)
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/detalle/<int:uid>")
@login_required
@tenant_required
def detalle(uid):
    gdb = g.global_db
    tdb = g.tenant_db
    tid = g.tenant["id"]

    u = gdb.execute(
        "SELECT * FROM tenant_users WHERE id=? AND tenant_id=?", (uid, tid)
    ).fetchone()
    if not u:
        return jsonify(ok=False, error="Miembro no encontrado"), 404

    m = dict(u)
    m["color"] = _user_color(m["id"])
    m["cargo"] = m.get("cargo") or "asociado"
    m["cargo_color"] = CARGO_COLORS.get(m["cargo"], "#60718D")
    try:
        m["especialidades_list"] = json.loads(m.get("especialidades") or "[]")
    except Exception:
        m["especialidades_list"] = []

    # Asuntos activos
    activos = [dict(r) for r in tdb.execute(
        "SELECT id, titulo, estado, numero_expediente, fecha_limite "
        "FROM asuntos WHERE abogado_responsable=? AND estado='activo' ORDER BY fecha_limite",
        (uid,),
    ).fetchall()]

    # Historial
    historial = [dict(r) for r in tdb.execute(
        "SELECT id, titulo, estado, numero_expediente, fecha_alta "
        "FROM asuntos WHERE abogado_responsable=? AND estado IN ('completado','archivado') "
        "ORDER BY fecha_alta DESC LIMIT 50",
        (uid,),
    ).fetchall()]

    return jsonify(ok=True, member=m, activos=activos, historial=historial)


# ─────────────────────────────────────────────────────────────────────────────
#  PERFIL — página completa de miembro
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/perfil/<int:uid>")
@login_required
@tenant_required
def perfil(uid):
    gdb = g.global_db
    tdb = g.tenant_db
    tid = g.tenant["id"]

    u = gdb.execute(
        "SELECT * FROM tenant_users WHERE id=? AND tenant_id=?", (uid, tid)
    ).fetchone()
    if not u:
        flash("Miembro no encontrado.", "warning")
        return redirect(url_for("equipo.index"))

    m = dict(u)
    m["color"] = _user_color(m["id"])
    m["cargo"] = m.get("cargo") or "asociado"
    m["cargo_color"] = CARGO_COLORS.get(m["cargo"], "#60718D")
    m["estatus"] = m.get("estatus") or ("activo" if m.get("activo") else "inactivo")
    try:
        m["especialidades_list"] = json.loads(m.get("especialidades") or "[]")
    except Exception:
        m["especialidades_list"] = []

    # ── Stats ──
    stats = {}
    stats["asuntos_activos"] = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='activo'", (uid,)
    ).fetchone()[0]
    stats["asuntos_completados"] = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='completado'", (uid,)
    ).fetchone()[0]
    stats["asuntos_total"] = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=?", (uid,)
    ).fetchone()[0]
    try:
        stats["eventos_proximos"] = tdb.execute(
            "SELECT COUNT(*) FROM eventos_calendario WHERE creado_por=? AND estado='pendiente' "
            "AND fecha_inicio >= datetime('now')", (uid,)
        ).fetchone()[0]
    except Exception:
        stats["eventos_proximos"] = 0
    try:
        stats["documentos"] = tdb.execute(
            "SELECT COUNT(*) FROM biblioteca_documentos WHERE creado_por=?", (uid,)
        ).fetchone()[0]
    except Exception:
        stats["documentos"] = 0
    try:
        stats["clientes_asignados"] = tdb.execute(
            "SELECT COUNT(DISTINCT cliente_id) FROM asuntos WHERE abogado_responsable=? AND cliente_id IS NOT NULL",
            (uid,),
        ).fetchone()[0]
    except Exception:
        stats["clientes_asignados"] = 0

    # ── Asuntos activos ──
    asuntos_activos = [dict(r) for r in tdb.execute(
        """SELECT a.id, a.titulo, a.estado, a.numero_expediente, a.tipo_asunto,
                  a.fecha_limite, a.prioridad,
                  c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
           WHERE a.abogado_responsable=? AND a.estado='activo'
           ORDER BY a.fecha_limite ASC NULLS LAST LIMIT 20""",
        (uid,),
    ).fetchall()]

    # ── Asuntos completados ──
    asuntos_historial = [dict(r) for r in tdb.execute(
        """SELECT a.id, a.titulo, a.estado, a.numero_expediente, a.tipo_asunto, a.fecha_alta,
                  c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
           WHERE a.abogado_responsable=? AND a.estado IN ('completado','archivado')
           ORDER BY a.fecha_alta DESC LIMIT 20""",
        (uid,),
    ).fetchall()]

    # ── Eventos próximos ──
    eventos = [dict(r) for r in tdb.execute(
        """SELECT id, titulo, tipo, fecha_inicio, fecha_fin, lugar, estado
           FROM eventos_calendario
           WHERE creado_por=? AND estado='pendiente' AND fecha_inicio >= datetime('now')
           ORDER BY fecha_inicio LIMIT 10""",
        (uid,),
    ).fetchall()]

    # ── Documentos recientes ──
    documentos = []
    try:
        documentos = [dict(r) for r in tdb.execute(
            """SELECT id, titulo, estado, fecha_alta
               FROM biblioteca_documentos
               WHERE creado_por=?
               ORDER BY fecha_alta DESC LIMIT 10""",
            (uid,),
        ).fetchall()]
    except Exception:
        pass

    # ── Actividad reciente ──
    actividad = []
    try:
        actividad = [dict(r) for r in tdb.execute(
            """SELECT accion, entidad, entidad_id, detalles, fecha, ip
               FROM actividad_log
               WHERE user_id=?
               ORDER BY fecha DESC LIMIT 30""",
            (uid,),
        ).fetchall()]
    except Exception:
        pass

    # Carga
    a = stats["asuntos_activos"]
    if a <= 3:
        carga = {"label": "Bajo", "color": "#4F6D8A", "pct": min(a * 15, 100)}
    elif a <= 7:
        carga = {"label": "Medio", "color": "#B08A4A", "pct": min(a * 12, 100)}
    else:
        carga = {"label": "Alto", "color": "#B5483F", "pct": min(a * 10, 100)}

    return render_template(
        "configuracion/perfil_miembro.html",
        member=m, stats=stats, carga=carga,
        asuntos_activos=asuntos_activos,
        asuntos_historial=asuntos_historial,
        eventos=eventos,
        documentos=documentos,
        actividad=actividad,
        cargos=CARGOS,
        especialidades=ESPECIALIDADES,
        cargo_colors=CARGO_COLORS,
        is_admin=_is_admin(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIAR CORREO A MIEMBRO (desde perfil)
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/perfil/<int:uid>/enviar-correo", methods=["POST"])
@login_required
@tenant_required
def enviar_correo(uid):
    """Envía un correo al miembro del equipo y lo registra en actividad_log."""
    gdb = g.global_db
    tdb = g.tenant_db
    tid = g.tenant["id"]

    dest = gdb.execute(
        "SELECT * FROM tenant_users WHERE id=? AND tenant_id=?", (uid, tid)
    ).fetchone()
    if not dest:
        return jsonify(ok=False, error="Miembro no encontrado"), 404

    asunto_correo = (request.form.get("asunto") or "").strip()
    cuerpo        = (request.form.get("cuerpo") or "").strip()
    cc_extra      = (request.form.get("cc") or "").strip()

    if not asunto_correo or not cuerpo:
        return jsonify(ok=False, error="Asunto y mensaje son obligatorios"), 400

    # Datos del remitente
    sender_id = session.get("user_id")
    sender = gdb.execute(
        "SELECT nombre, apellidos, email FROM tenant_users WHERE id=?", (sender_id,)
    ).fetchone()
    sender_name = f"{sender['nombre']} {sender['apellidos'] or ''}" if sender else "LexOS"
    sender_email = sender["email"] if sender else ""

    # Nombre del despacho
    tenant = gdb.execute("SELECT nombre FROM tenants WHERE id=?", (tid,)).fetchone()
    despacho_nombre = tenant["nombre"] if tenant else "LexOS"

    # ── Construir HTML bonito ──
    html_body = f"""
    <div style="font-family:'Segoe UI',Roboto,Arial,sans-serif;max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #E5E2DB;">
      <!-- Header -->
      <div style="background:linear-gradient(135deg,#1A2B44 0%,#334155 100%);padding:28px 32px;">
        <div style="font-size:20px;font-weight:800;color:#ffffff;letter-spacing:-.3px;">{despacho_nombre}</div>
        <div style="font-size:12px;color:#9AA4B5;margin-top:4px;">Mensaje interno del equipo</div>
      </div>

      <!-- Body -->
      <div style="padding:32px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#9AA4B5;margin-bottom:6px;">Asunto</div>
        <div style="font-size:17px;font-weight:700;color:#1A2B44;margin-bottom:24px;">{asunto_correo}</div>

        <div style="font-size:14px;color:#3B4A61;line-height:1.7;white-space:pre-wrap;">{cuerpo}</div>
      </div>

      <!-- Footer -->
      <div style="background:#FAF8F4;padding:20px 32px;border-top:1px solid #E5E2DB;">
        <div style="font-size:13px;color:#60718D;">
          Enviado por <strong>{sender_name}</strong>
          {f'&lt;{sender_email}&gt;' if sender_email else ''}
        </div>
        <div style="font-size:11px;color:#9AA4B5;margin-top:4px;">
          Este correo fue enviado desde la plataforma LexOS · {datetime.now().strftime('%d/%m/%Y %H:%M')}
        </div>
      </div>
    </div>
    """

    # ── Destinatarios ──
    recipients = [dest["email"]]
    cc_list = []
    if cc_extra:
        cc_list = [e.strip() for e in cc_extra.replace(";", ",").split(",") if "@" in e.strip()]

    # ── Enviar ──
    try:
        from app import mail
        from flask_mail import Message

        msg = Message(
            subject=f"{asunto_correo} — {despacho_nombre}",
            recipients=recipients,
            cc=cc_list if cc_list else None,
            html=html_body,
            reply_to=sender_email or None,
        )
        mail.send(msg)
        enviado = True
    except Exception as exc:
        current_app.logger.warning(f"[MAIL equipo] Error enviando a {dest['email']}: {exc}")
        enviado = False

    # ── Log en actividad ──
    try:
        tdb.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, fecha, ip) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), ?)",
            (
                sender_id,
                "enviar_correo",
                "equipo",
                uid,
                json.dumps({
                    "para": dest["email"],
                    "cc": cc_list,
                    "asunto": asunto_correo,
                    "enviado": enviado,
                }, ensure_ascii=False),
                request.remote_addr,
            ),
        )
        tdb.commit()
    except Exception:
        pass

    if enviado:
        return jsonify(ok=True, msg=f"Correo enviado a {dest['email']}")
    else:
        return jsonify(ok=False, error="No se pudo enviar el correo. Verifica la configuración SMTP en ajustes."), 500


# ─────────────────────────────────────────────────────────────────────────────
#  INVITAR MIEMBRO
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/invitar", methods=["POST"])
@login_required
@tenant_required
def invitar():
    if not _is_admin():
        return jsonify(ok=False, error="Sin permisos"), 403

    gdb = g.global_db
    tid = g.tenant["id"]

    email = (request.form.get("email") or "").strip().lower()
    nombre = (request.form.get("nombre") or "").strip()
    apellidos = (request.form.get("apellidos") or "").strip()
    cargo = request.form.get("cargo", "asociado")
    especialidades = request.form.getlist("especialidades")

    if not email or not nombre:
        flash("Correo y nombre son obligatorios.", "danger")
        return redirect(url_for("equipo.index"))

    # Check duplicate
    exists = gdb.execute(
        "SELECT id FROM tenant_users WHERE tenant_id=? AND email=?", (tid, email)
    ).fetchone()
    if exists:
        flash("Ya existe un miembro con ese correo.", "warning")
        return redirect(url_for("equipo.index"))

    # Create with temp password (pending invitation)
    import secrets
    temp_pass = secrets.token_urlsafe(16)
    gdb.execute(
        """INSERT INTO tenant_users
           (tenant_id, nombre, apellidos, email, password_hash, rol, cargo,
            especialidades, estatus, activo)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (tid, nombre, apellidos, email, generate_password_hash(temp_pass),
         "abogado", cargo, json.dumps(especialidades), "pendiente", 1),
    )
    gdb.commit()

    flash(f"Invitación enviada a {email}.", "success")
    return redirect(url_for("equipo.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  EDITAR MIEMBRO
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/editar/<int:uid>", methods=["POST"])
@login_required
@tenant_required
def editar(uid):
    gdb = g.global_db
    tid = g.tenant["id"]
    is_self = (uid == session.get("user_id"))

    # Non-admin can only edit own phone
    if not _is_admin() and not is_self:
        return jsonify(ok=False, error="Sin permisos"), 403

    u = gdb.execute(
        "SELECT * FROM tenant_users WHERE id=? AND tenant_id=?", (uid, tid)
    ).fetchone()
    if not u:
        return jsonify(ok=False, error="No encontrado"), 404

    telefono = request.form.get("telefono", "").strip()

    if _is_admin():
        cargo = request.form.get("cargo", u["cargo"] or "asociado")
        especialidades = request.form.getlist("especialidades")
        fecha_ingreso = request.form.get("fecha_ingreso", "").strip() or None

        gdb.execute(
            """UPDATE tenant_users
               SET cargo=?, especialidades=?, telefono=?, fecha_ingreso=?
               WHERE id=? AND tenant_id=?""",
            (cargo, json.dumps(especialidades), telefono, fecha_ingreso, uid, tid),
        )
    else:
        gdb.execute(
            "UPDATE tenant_users SET telefono=? WHERE id=? AND tenant_id=?",
            (telefono, uid, tid),
        )
    gdb.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    flash("Miembro actualizado.", "success")
    return redirect(url_for("equipo.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  CAMBIAR ESTATUS (suspender / desactivar / reactivar)
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/estatus/<int:uid>", methods=["POST"])
@login_required
@tenant_required
def cambiar_estatus(uid):
    if not _is_admin():
        return jsonify(ok=False, error="Sin permisos"), 403

    gdb = g.global_db
    tid = g.tenant["id"]
    accion = request.form.get("accion", "")

    if accion == "suspender":
        gdb.execute(
            "UPDATE tenant_users SET estatus='suspendido', activo=0 WHERE id=? AND tenant_id=?",
            (uid, tid),
        )
    elif accion == "desactivar":
        gdb.execute(
            "UPDATE tenant_users SET estatus='inactivo', activo=0 WHERE id=? AND tenant_id=?",
            (uid, tid),
        )
    elif accion == "reactivar":
        gdb.execute(
            "UPDATE tenant_users SET estatus='activo', activo=1 WHERE id=? AND tenant_id=?",
            (uid, tid),
        )
    gdb.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    flash("Estatus actualizado.", "success")
    return redirect(url_for("equipo.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  UPLOAD AVATAR
# ─────────────────────────────────────────────────────────────────────────────

@equipo_bp.route("/avatar/<int:uid>", methods=["POST"])
@login_required
@tenant_required
def upload_avatar(uid):
    is_self = (uid == session.get("user_id"))
    if not _is_admin() and not is_self:
        return jsonify(ok=False, error="Sin permisos"), 403

    gdb = g.global_db
    tid = g.tenant["id"]

    f = request.files.get("avatar")
    if not f or not f.filename:
        return jsonify(ok=False, error="No se subió archivo"), 400

    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        return jsonify(ok=False, error="Formato no soportado"), 400

    upload_dir = os.path.join("static", "uploads", "avatars")
    os.makedirs(upload_dir, exist_ok=True)
    fname = f"{tid}_{uid}_{int(datetime.now().timestamp())}.{ext}"
    fpath = os.path.join(upload_dir, fname)
    f.save(fpath)

    avatar_url = f"/static/uploads/avatars/{fname}"
    gdb.execute(
        "UPDATE tenant_users SET avatar_url=? WHERE id=? AND tenant_id=?",
        (avatar_url, uid, tid),
    )
    gdb.commit()

    return jsonify(ok=True, avatar_url=avatar_url)
