"""
LexOS — modules/portal.py
Portal de Clientes — acceso externo para clientes del despacho.
Login con email+contraseña o link mágico. Rate limiting. Sesión separada.
Secciones: Inicio, Mis Asuntos, Mis Documentos, Mis Facturas, Mensajes.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint, abort, g, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)

from modules.auth import login_required, role_required, tenant_required

portal_bp = Blueprint("portal", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _abogado_nombre(uid):
    try:
        u = g.global_db.execute(
            "SELECT nombre, apellidos FROM tenant_users WHERE id=?", (uid,)
        ).fetchone()
        return f"{u['nombre']} {u['apellidos']}" if u else "Abogado"
    except Exception:
        return "Abogado"


def portal_login_required(f):
    """Decorator: require portal client session."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("portal_cliente_id"):
            return redirect(url_for("portal.cliente_login"))
        # La sesión del portal solo vale en el despacho donde se inició
        if session.get("portal_tenant") != getattr(g, "tenant_slug", None):
            return redirect(url_for("portal.cliente_login"))
        # Verify client still active
        tdb = getattr(g, "tenant_db", None)
        if not tdb:
            return redirect(url_for("portal.cliente_login"))
        pc = tdb.execute(
            "SELECT id FROM portal_clientes WHERE id=? AND cliente_id=? AND activo=1",
            (session.get("portal_pc_id"), session["portal_cliente_id"])
        ).fetchone()
        if not pc:
            session.pop("portal_cliente_id", None)
            return redirect(url_for("portal.cliente_login"))
        return f(*args, **kwargs)
    return wrapped


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN VIEWS (internal — abogados managing portal)
# ─────────────────────────────────────────────────────────────────────────────

@portal_bp.route("/")
@login_required
@tenant_required
@role_required("super_admin", "admin")
def index():
    tdb = g.tenant_db
    mensajes_no_leidos = tdb.execute(
        """SELECT pm.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
                  a.titulo as asunto_titulo
           FROM portal_mensajes pm
           LEFT JOIN clientes c ON pm.cliente_id=c.id
           LEFT JOIN asuntos a ON pm.asunto_id=a.id
           WHERE pm.remitente='cliente' AND pm.leido=0
           ORDER BY pm.fecha DESC LIMIT 50"""
    ).fetchall()
    mensajes_recientes = tdb.execute(
        """SELECT pm.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM portal_mensajes pm
           LEFT JOIN clientes c ON pm.cliente_id=c.id
           ORDER BY pm.fecha DESC LIMIT 30"""
    ).fetchall()
    accesos = tdb.execute(
        """SELECT pc.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
                  c.email as cliente_email
           FROM portal_clientes pc
           JOIN clientes c ON pc.cliente_id=c.id
           ORDER BY pc.activo DESC, pc.fecha_alta DESC"""
    ).fetchall()
    # Clientes a los que todavía se les puede dar acceso (sin acceso o con acceso quitado)
    disponibles = tdb.execute(
        """SELECT c.id, c.nombre || ' ' || COALESCE(c.apellidos,'') as nombre,
                  COALESCE(c.email,'') as email
           FROM clientes c
           WHERE c.activo=1
             AND c.id NOT IN (SELECT cliente_id FROM portal_clientes WHERE activo=1)
           ORDER BY c.nombre, c.apellidos"""
    ).fetchall()
    return render_template("portal/admin.html",
                           mensajes_no_leidos=mensajes_no_leidos,
                           mensajes_recientes=mensajes_recientes,
                           accesos=accesos,
                           activos=sum(1 for a in accesos if a["activo"]),
                           han_entrado=sum(1 for a in accesos if a["activo"] and a["ultimo_login"]),
                           disponibles=disponibles,
                           enlace_portal=url_for("portal.cliente_login", tenant=g.tenant_slug, _external=True))


@portal_bp.route("/invitar/<int:cliente_id>", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def invitar(cliente_id):
    """Create or reactivate portal access for a client."""
    tdb = g.tenant_db
    cliente = tdb.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()
    if not cliente:
        return jsonify(ok=False, error="Cliente no encontrado"), 404

    email = (request.form.get("email") or cliente["email"] or "").strip()
    password = request.form.get("password", "").strip()

    if not email or "@" not in email:
        return jsonify(ok=False, error="Escribe el correo del cliente"), 400

    # Un mismo correo no puede entrar por dos clientes distintos
    otro = tdb.execute(
        "SELECT id FROM portal_clientes WHERE LOWER(email)=LOWER(?) AND cliente_id<>? AND activo=1",
        (email, cliente_id),
    ).fetchone()
    if otro:
        return jsonify(ok=False, error="Ese correo ya lo usa otro cliente en el portal"), 400

    existing = tdb.execute("SELECT id FROM portal_clientes WHERE cliente_id=?", (cliente_id,)).fetchone()
    if existing:
        # Si no se escribe contraseña nueva, se conserva la que tenía
        tdb.execute(
            "UPDATE portal_clientes SET email=?, password_hash=COALESCE(?, password_hash), activo=1 WHERE cliente_id=?",
            (email, _hash(password) if password else None, cliente_id),
        )
    else:
        tdb.execute(
            "INSERT INTO portal_clientes (cliente_id, email, password_hash) VALUES (?,?,?)",
            (cliente_id, email, _hash(password) if password else None),
        )

    # Update invitar_portal flag
    tdb.execute("UPDATE clientes SET invitar_portal=1 WHERE id=?", (cliente_id,))
    tdb.commit()
    return jsonify(ok=True, message="Acceso al portal creado")


@portal_bp.route("/desactivar/<int:cliente_id>", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def desactivar(cliente_id):
    g.tenant_db.execute("UPDATE portal_clientes SET activo=0 WHERE cliente_id=?", (cliente_id,))
    g.tenant_db.execute("UPDATE clientes SET invitar_portal=0 WHERE id=?", (cliente_id,))
    g.tenant_db.commit()
    return jsonify(ok=True)


@portal_bp.route("/magic-link/<int:cliente_id>", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def generar_magic_link(cliente_id):
    """Generate a magic login link for a client."""
    tdb = g.tenant_db
    token = secrets.token_urlsafe(48)
    expires = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cur = tdb.execute(
        "UPDATE portal_clientes SET magic_token=?, magic_expires=? WHERE cliente_id=? AND activo=1",
        (token, expires, cliente_id),
    )
    if not cur.rowcount:
        return jsonify(ok=False, error="Este cliente no tiene acceso al portal"), 404
    tdb.commit()
    # El enlace lleva el despacho para que funcione en un navegador sin sesión
    link = url_for("portal.magic_login", token=token, tenant=g.tenant_slug, _external=True)
    return jsonify(ok=True, link=link, token=token)


@portal_bp.route("/responder/<int:mensaje_id>", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def responder(mensaje_id):
    tdb = g.tenant_db
    original = tdb.execute("SELECT * FROM portal_mensajes WHERE id=?", (mensaje_id,)).fetchone()
    if not original:
        return jsonify(ok=False, error="Mensaje no encontrado"), 404

    respuesta = (request.form.get("respuesta")
                 or (request.get_json(silent=True) or {}).get("respuesta") or "").strip()
    if not respuesta:
        return jsonify(ok=False, error="Respuesta vacía"), 400

    tdb.execute(
        "INSERT INTO portal_mensajes (cliente_id, asunto_id, remitente, mensaje) VALUES (?,?,?,?)",
        (original["cliente_id"], original["asunto_id"], "despacho", respuesta),
    )
    tdb.execute("UPDATE portal_mensajes SET leido=1 WHERE id=?", (mensaje_id,))
    tdb.commit()
    return jsonify(ok=True)


@portal_bp.route("/compartir-doc/<int:cliente_id>", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def compartir_doc(cliente_id):
    """Share a document with a portal client."""
    tdb = g.tenant_db
    titulo = (request.form.get("titulo") or "").strip()
    descripcion = request.form.get("descripcion", "")
    archivo_url = request.form.get("archivo_url", "")
    archivo_nombre = request.form.get("archivo_nombre", "")
    asunto_id = _safe_int(request.form.get("asunto_id"))

    if not titulo:
        return jsonify(ok=False, error="Título requerido"), 400

    tdb.execute(
        """INSERT INTO portal_docs_compartidos
           (cliente_id, titulo, descripcion, archivo_url, archivo_nombre, asunto_id, compartido_por)
           VALUES (?,?,?,?,?,?,?)""",
        (cliente_id, titulo, descripcion, archivo_url, archivo_nombre,
         asunto_id, session.get("user_id")),
    )
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  CLIENT VIEWS (external portal)
# ─────────────────────────────────────────────────────────────────────────────

@portal_bp.route("/cliente")
def cliente_login():
    if session.get("portal_cliente_id") and session.get("portal_tenant") == getattr(g, "tenant_slug", None):
        return redirect(url_for("portal.cliente_inicio"))
    return render_template("portal/login.html")


@portal_bp.route("/cliente/auth", methods=["POST"])
def cliente_auth():
    """Email + password login with rate limiting."""
    tdb = getattr(g, "tenant_db", None)
    if not tdb:
        return render_template("portal/login.html", error="Despacho no encontrado")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password", "").strip()

    if not email:
        return render_template("portal/login.html", error="Ingresa tu email")

    pc = tdb.execute(
        """SELECT pc.*, c.nombre, c.apellidos
           FROM portal_clientes pc JOIN clientes c ON pc.cliente_id=c.id
           WHERE LOWER(pc.email)=? AND pc.activo=1""",
        (email,),
    ).fetchone()

    if not pc:
        return render_template("portal/login.html", error="Email no registrado")

    # Rate limiting: 5 attempts, 15 min block
    if pc["bloqueado_hasta"]:
        block_until = datetime.strptime(pc["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < block_until:
            mins = int((block_until - datetime.now()).total_seconds() / 60) + 1
            return render_template("portal/login.html",
                                   error=f"Cuenta bloqueada. Intenta en {mins} minutos.")
        else:
            tdb.execute("UPDATE portal_clientes SET intentos_login=0, bloqueado_hasta=NULL WHERE id=?",
                        (pc["id"],))
            tdb.commit()

    # If no password set, only magic link works
    if not pc["password_hash"]:
        return render_template("portal/login.html",
                               error="Tu cuenta usa link mágico. Solicita uno a tu abogado.")

    if _hash(password) != pc["password_hash"]:
        intentos = (pc["intentos_login"] or 0) + 1
        if intentos >= 5:
            block = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            tdb.execute("UPDATE portal_clientes SET intentos_login=?, bloqueado_hasta=? WHERE id=?",
                        (intentos, block, pc["id"]))
        else:
            tdb.execute("UPDATE portal_clientes SET intentos_login=? WHERE id=?",
                        (intentos, pc["id"]))
        tdb.commit()
        remaining = 5 - intentos
        if remaining > 0:
            return render_template("portal/login.html",
                                   error=f"Contraseña incorrecta. {remaining} intentos restantes.")
        return render_template("portal/login.html",
                               error="Cuenta bloqueada por 15 minutos.")

    # Success
    _do_portal_login(tdb, pc)
    return redirect(url_for("portal.cliente_inicio"))


@portal_bp.route("/cliente/magic/<token>")
def magic_login(token):
    """Magic link login."""
    tdb = getattr(g, "tenant_db", None)
    if not tdb:
        return render_template("portal/login.html", error="Despacho no encontrado")

    pc = tdb.execute(
        """SELECT pc.*, c.nombre, c.apellidos
           FROM portal_clientes pc JOIN clientes c ON pc.cliente_id=c.id
           WHERE pc.magic_token=? AND pc.activo=1""",
        (token,),
    ).fetchone()

    if not pc:
        return render_template("portal/login.html", error="Link inválido")

    if pc["magic_expires"]:
        exp = datetime.strptime(pc["magic_expires"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp:
            return render_template("portal/login.html", error="Link expirado. Solicita uno nuevo.")

    # Invalidate token after use
    tdb.execute("UPDATE portal_clientes SET magic_token=NULL, magic_expires=NULL WHERE id=?",
                (pc["id"],))
    _do_portal_login(tdb, pc)
    return redirect(url_for("portal.cliente_inicio"))


def _do_portal_login(tdb, pc):
    session["portal_cliente_id"] = pc["cliente_id"]
    session["portal_cliente_nombre"] = f"{pc['nombre']} {pc['apellidos'] or ''}".strip()
    session["portal_pc_id"] = pc["id"]
    session["portal_tenant"] = g.tenant_slug
    tdb.execute(
        "UPDATE portal_clientes SET ultimo_login=CURRENT_TIMESTAMP, intentos_login=0, bloqueado_hasta=NULL WHERE id=?",
        (pc["id"],),
    )
    tdb.commit()


@portal_bp.route("/cliente/logout")
def cliente_logout():
    session.pop("portal_cliente_id", None)
    session.pop("portal_cliente_nombre", None)
    session.pop("portal_pc_id", None)
    session.pop("portal_tenant", None)
    return redirect(url_for("portal.cliente_login"))


# ── INICIO ──

@portal_bp.route("/cliente/inicio")
@portal_login_required
def cliente_inicio():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]

    asuntos = tdb.execute(
        "SELECT id, titulo, estado, tipo_asunto FROM asuntos WHERE cliente_id=? AND estado='activo' ORDER BY fecha_alta DESC LIMIT 5",
        (cid,),
    ).fetchall()

    prox_evento = tdb.execute(
        """SELECT titulo, fecha_inicio, tipo FROM eventos_calendario
           WHERE cliente_id=? AND fecha_inicio >= datetime('now')
           ORDER BY fecha_inicio ASC LIMIT 1""",
        (cid,),
    ).fetchone()

    ultima_factura = tdb.execute(
        """SELECT id, numero_factura AS folio, concepto, monto, total, estado, fecha_vencimiento
           FROM cobros WHERE cliente_id=? AND estado IN ('pendiente','vencido')
           ORDER BY fecha_emision DESC LIMIT 1""",
        (cid,),
    ).fetchone()

    msgs_no_leidos = tdb.execute(
        "SELECT COUNT(*) FROM portal_mensajes WHERE cliente_id=? AND remitente='despacho' AND leido=0",
        (cid,),
    ).fetchone()[0]

    return render_template("portal/inicio.html",
                           asuntos=asuntos, prox_evento=prox_evento,
                           ultima_factura=ultima_factura, msgs_no_leidos=msgs_no_leidos)


# ── MIS ASUNTOS ──

@portal_bp.route("/cliente/asuntos")
@portal_login_required
def cliente_asuntos():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    asuntos = [dict(r) for r in tdb.execute(
        "SELECT * FROM asuntos WHERE cliente_id=? ORDER BY fecha_alta DESC", (cid,)
    ).fetchall()]
    # Add abogado name
    for a in asuntos:
        a["abogado_nombre"] = _abogado_nombre(a.get("abogado_responsable"))
    return render_template("portal/asuntos.html", asuntos=asuntos)


@portal_bp.route("/cliente/asuntos/<int:asunto_id>")
@portal_login_required
def cliente_asunto_detalle(asunto_id):
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    asunto = tdb.execute(
        "SELECT * FROM asuntos WHERE id=? AND cliente_id=?", (asunto_id, cid)
    ).fetchone()
    if not asunto:
        abort(404)

    abogado_nombre = _abogado_nombre(asunto["abogado_responsable"])

    # Last 5 activities (filtered — no internal notes)
    actividades = tdb.execute(
        """SELECT titulo, tipo, fecha, realizado FROM asunto_actividades
           WHERE asunto_id=? ORDER BY fecha DESC LIMIT 5""",
        (asunto_id,),
    ).fetchall()

    # Client notes on this case
    notas_cliente = tdb.execute(
        """SELECT mensaje, fecha FROM portal_mensajes
           WHERE cliente_id=? AND asunto_id=? ORDER BY fecha DESC LIMIT 20""",
        (cid, asunto_id),
    ).fetchall()

    return render_template("portal/asunto_detalle.html",
                           asunto=asunto, abogado_nombre=abogado_nombre,
                           actividades=actividades, notas_cliente=notas_cliente)


@portal_bp.route("/cliente/asuntos/<int:asunto_id>/nota", methods=["POST"])
@portal_login_required
def cliente_asunto_nota(asunto_id):
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    # Verify ownership
    asunto = tdb.execute(
        "SELECT id FROM asuntos WHERE id=? AND cliente_id=?", (asunto_id, cid)
    ).fetchone()
    if not asunto:
        return jsonify(ok=False, error="Asunto no encontrado"), 404

    mensaje = (request.form.get("mensaje") or "").strip()
    if not mensaje:
        return jsonify(ok=False, error="Mensaje vacío"), 400

    tdb.execute(
        "INSERT INTO portal_mensajes (cliente_id, asunto_id, remitente, mensaje) VALUES (?,?,?,?)",
        (cid, asunto_id, "cliente", mensaje),
    )
    tdb.commit()
    return jsonify(ok=True)


# ── MIS DOCUMENTOS ──

@portal_bp.route("/cliente/documentos")
@portal_login_required
def cliente_documentos():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    docs = tdb.execute(
        """SELECT pd.*, a.titulo as asunto_titulo
           FROM portal_docs_compartidos pd
           LEFT JOIN asuntos a ON pd.asunto_id=a.id
           WHERE pd.cliente_id=?
           ORDER BY pd.fecha DESC""",
        (cid,),
    ).fetchall()
    return render_template("portal/documentos.html", docs=docs)


# ── MIS FACTURAS ──

@portal_bp.route("/cliente/facturas")
@portal_login_required
def cliente_facturas():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    cobros = tdb.execute(
        """SELECT co.*, a.titulo as asunto_titulo
           FROM cobros co
           LEFT JOIN asuntos a ON co.asunto_id=a.id
           WHERE co.cliente_id=?
           ORDER BY co.fecha_emision DESC""",
        (cid,),
    ).fetchall()
    return render_template("portal/facturas.html", cobros=cobros)


@portal_bp.route("/cliente/facturas/<int:cobro_id>/revision", methods=["POST"])
@portal_login_required
def cliente_factura_revision(cobro_id):
    """Client marks invoice as 'en revisión'."""
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    cobro = tdb.execute(
        "SELECT id FROM cobros WHERE id=? AND cliente_id=?", (cobro_id, cid)
    ).fetchone()
    if not cobro:
        return jsonify(ok=False, error="Factura no encontrada"), 404

    motivo = (request.form.get("motivo") or "").strip()
    # Add note via portal_mensajes
    tdb.execute(
        "INSERT INTO portal_mensajes (cliente_id, remitente, mensaje) VALUES (?,?,?)",
        (cid, "cliente", f"[Factura #{cobro_id} en revisión] {motivo}"),
    )
    tdb.commit()
    return jsonify(ok=True, message="Notificación enviada al despacho")


# ── MENSAJES (Chat) ──

@portal_bp.route("/cliente/mensajes")
@portal_login_required
def cliente_mensajes():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    mensajes = tdb.execute(
        "SELECT * FROM portal_mensajes WHERE cliente_id=? ORDER BY fecha ASC",
        (cid,),
    ).fetchall()
    # Mark despacho messages as read
    tdb.execute(
        "UPDATE portal_mensajes SET leido=1 WHERE cliente_id=? AND remitente='despacho' AND leido=0",
        (cid,),
    )
    tdb.commit()
    return render_template("portal/mensajes.html", mensajes=mensajes)


@portal_bp.route("/cliente/mensajes/enviar", methods=["POST"])
@portal_login_required
def cliente_mensaje_enviar():
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    mensaje = (request.form.get("mensaje") or "").strip()
    asunto_id = _safe_int(request.form.get("asunto_id"))

    if not mensaje:
        return jsonify(ok=False, error="Mensaje vacío"), 400

    tdb.execute(
        "INSERT INTO portal_mensajes (cliente_id, asunto_id, remitente, mensaje) VALUES (?,?,?,?)",
        (cid, asunto_id, "cliente", mensaje),
    )
    tdb.commit()
    return jsonify(ok=True)


@portal_bp.route("/cliente/mensajes/ajax")
@portal_login_required
def cliente_mensajes_ajax():
    """Return messages as JSON for live refresh."""
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    after = request.args.get("after", "2000-01-01")
    mensajes = [dict(r) for r in tdb.execute(
        "SELECT * FROM portal_mensajes WHERE cliente_id=? AND fecha > ? ORDER BY fecha ASC",
        (cid, after),
    ).fetchall()]
    # Mark as read
    tdb.execute(
        "UPDATE portal_mensajes SET leido=1 WHERE cliente_id=? AND remitente='despacho' AND leido=0",
        (cid,),
    )
    tdb.commit()
    return jsonify(ok=True, mensajes=mensajes)


# ── CONFIRMAR / RECHAZAR REUNIÓN ──

@portal_bp.route("/cliente/evento/<int:evento_id>/confirmar", methods=["POST"])
@portal_login_required
def cliente_evento_confirmar(evento_id):
    tdb = g.tenant_db
    cid = session["portal_cliente_id"]
    ev = tdb.execute(
        "SELECT id FROM eventos_calendario WHERE id=? AND cliente_id=?",
        (evento_id, cid),
    ).fetchone()
    if not ev:
        return jsonify(ok=False, error="Evento no encontrado"), 404

    accion = request.form.get("accion", "confirmar")
    if accion == "confirmar":
        tdb.execute("UPDATE eventos_calendario SET estado='confirmado' WHERE id=?", (evento_id,))
        msg = "Reunión confirmada"
    else:
        tdb.execute("UPDATE eventos_calendario SET estado='cancelado' WHERE id=?", (evento_id,))
        msg = "Reunión rechazada"
    tdb.execute(
        "INSERT INTO portal_mensajes (cliente_id, remitente, mensaje) VALUES (?,?,?)",
        (cid, "cliente", f"[Evento #{evento_id}] {msg}"),
    )
    tdb.commit()
    return jsonify(ok=True, message=msg)
