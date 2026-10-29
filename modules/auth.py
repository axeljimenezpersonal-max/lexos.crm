"""
LexOS — modules/auth.py
Autenticación multi-tenant completa.

Rutas:
  GET/POST /auth/login             — Login 2-step con tenant discovery
  POST     /auth/discover          — AJAX: detecta tenant a partir del email
  GET      /auth/logout            — Cierra sesión
  GET/POST /auth/forgot            — Solicitar reset de contraseña
  GET/POST /auth/reset/<token>     — Restablecer contraseña con token
  GET/POST /auth/perfil            — Editar perfil del usuario
  GET/POST /auth/register-tenant   — Registro de nuevo despacho
"""

import os
import re
import json
import secrets
import hashlib
import unicodedata
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, g, current_app, jsonify
)

auth_bp = Blueprint("auth", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers internos
# ─────────────────────────────────────────────────────────────────────────────

def _hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def _verify(pwd: str, hashed: str) -> bool:
    return _hash(pwd) == hashed


def _slugify(text: str) -> str:
    """Convierte texto a slug URL-safe."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text[:50].strip("-")


def _gdb():
    from app import get_global_db
    return get_global_db()


def _tdb():
    return getattr(g, "tenant_db", None)


def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Envía email vía Flask-Mail. En dev sin config imprime en consola."""
    try:
        from app import mail
        from flask_mail import Message
        msg = Message(subject=subject, recipients=[to], html=html_body)
        mail.send(msg)
        return True
    except Exception as exc:
        # Fallback: log en consola (útil en desarrollo sin SMTP)
        current_app.logger.warning(
            f"[MAIL] No se pudo enviar a {to}: {exc}\n"
            f"[MAIL] Subject: {subject}\n"
            f"[MAIL] Body: {html_body[:300]}"
        )
        return False


def _log(action: str, entity: str = None, entity_id: int = None, details: dict = None):
    """Audit log en la DB del tenant."""
    try:
        tdb = _tdb()
        if tdb:
            tdb.execute(
                """INSERT INTO actividad_log
                   (user_id, accion, entidad, entidad_id, detalles, ip)
                   VALUES (?,?,?,?,?,?)""",
                (session.get("user_id"), action, entity, entity_id,
                 json.dumps(details or {}), request.remote_addr),
            )
            tdb.commit()
    except Exception:
        pass


def _reset_link(token: str) -> str:
    """Construye la URL de reset adaptada a dev/prod."""
    try:
        return url_for("auth.reset_password", token=token, _external=True)
    except Exception:
        dev_host = current_app.config.get("SERVER_NAME_DEV", "localhost:5000")
        return f"http://{dev_host}/auth/reset/{token}"


def _init_tenant_db(slug: str):
    """Crea el schema en la DB del nuevo tenant."""
    from init_db import TENANT_SCHEMA, get_tenant_conn
    conn = get_tenant_conn(slug)
    conn.executescript(TENANT_SCHEMA)
    conn.commit()
    conn.close()


def _ensure_onboarding(user_id: int):
    """Crea registro de onboarding para el usuario si no existe."""
    tdb = _tdb()
    if tdb:
        exists = tdb.execute(
            "SELECT id FROM onboarding_progress WHERE user_id=?", (user_id,)
        ).fetchone()
        if not exists:
            tdb.execute(
                "INSERT INTO onboarding_progress (user_id) VALUES (?)", (user_id,)
            )
            tdb.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  Decoradores públicos (importados por todos los módulos)
# ─────────────────────────────────────────────────────────────────────────────

def sesion_del_despacho():
    """La sesión solo vale si el usuario pertenece al despacho de esta petición."""
    if not session.get("user_id"):
        return False
    if getattr(g, "tenant", None) is not None and getattr(g, "user", None) is None:
        return False
    return True


def login_required(f):
    """Redirige a /auth/login si no hay sesión válida para este despacho."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not sesion_del_despacho():
            flash("Debes iniciar sesión para continuar.", "warning")
            next_url = request.path
            return redirect(url_for("auth.login", next=next_url))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Verifica que el rol del usuario esté entre los permitidos."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not sesion_del_despacho():
                return redirect(url_for("auth.login"))
            if session.get("role") not in roles:
                flash("No tienes permiso para acceder a esta sección.", "danger")
                return redirect(url_for("dashboard.index"))
            return f(*args, **kwargs)
        return decorated
    return decorator


def tenant_required(f):
    """Verifica que el tenant esté activo y el plan vigente."""
    @wraps(f)
    def decorated(*args, **kwargs):
        tenant = getattr(g, "tenant", None)
        if not tenant:
            flash("Despacho no encontrado o inactivo.", "danger")
            return redirect(url_for("auth.landing"))
        if not tenant["activo"]:
            return render_template("errors/tenant_inactive.html"), 403
        return f(*args, **kwargs)
    return decorated


# Alias conveniente para importar
log_activity = _log


# ─────────────────────────────────────────────────────────────────────────────
#  Landing y discovery de tenant
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/landing")
def landing():
    """Página pública de Habeas Corpus. Con sesión abierta ofrece volver al despacho."""
    return render_template("auth/landing.html",
                           en_sesion=bool(getattr(g, "user", None) and getattr(g, "tenant", None)))


_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@auth_bp.route("/contacto", methods=["POST"])
def contacto():
    """Solicitud de demostración desde la página pública: se guarda y nos avisa por correo."""
    d = request.get_json(silent=True) or request.form
    if (d.get("sitio") or "").strip():          # campo trampa para bots
        return jsonify(ok=True)
    datos = {k: (d.get(k) or "").strip()[:n] for k, n in
             (("nombre", 120), ("despacho", 160), ("email", 160), ("telefono", 30), ("tamano", 30), ("mensaje", 2000))}
    datos["email"] = datos["email"].lower()
    if not datos["nombre"] or not _RE_EMAIL.match(datos["email"]):
        return jsonify(ok=False, error="Escribe tu nombre y un correo válido."), 400

    gdb = _gdb()
    gdb.execute("""CREATE TABLE IF NOT EXISTS contactos_landing (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT, despacho TEXT, email TEXT, telefono TEXT,
        tamano TEXT, mensaje TEXT, ip TEXT, atendido INTEGER DEFAULT 0, fecha DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    recientes = gdb.execute("SELECT COUNT(*) FROM contactos_landing WHERE ip=? AND fecha >= datetime('now','-1 hour')",
                            (request.remote_addr,)).fetchone()[0]
    if recientes >= 5:
        return jsonify(ok=False, error="Ya recibimos tus datos. Te escribimos muy pronto."), 429
    gdb.execute("INSERT INTO contactos_landing (nombre, despacho, email, telefono, tamano, mensaje, ip) VALUES (?,?,?,?,?,?,?)",
                (*datos.values(), request.remote_addr))
    gdb.commit()

    destino = current_app.config.get("CONTACT_EMAIL")
    if destino:
        try:
            from flask_mail import Message
            from app import mail
            import html as _html
            filas = "".join(f"<tr><td style='padding:6px 12px 6px 0;color:#60718D'>{_html.escape(k.capitalize())}</td>"
                            f"<td style='padding:6px 0;color:#142E59'>{_html.escape(v) or '—'}</td></tr>" for k, v in datos.items())
            mail.send(Message(subject=f"Nueva solicitud de demostración — {datos['despacho'] or datos['nombre']}",
                              recipients=[destino], reply_to=datos["email"],
                              html=f"<div style='font-family:Inter,Arial,sans-serif'><h2 style='color:#142E59'>Solicitud de demostración</h2>"
                                   f"<table>{filas}</table></div>"))
        except Exception:
            current_app.logger.warning("No se pudo enviar el aviso de contacto; quedó guardado en contactos_landing")
    return jsonify(ok=True)


@auth_bp.route("/discover", methods=["POST"])
def discover():
    """
    AJAX + form POST: encuentra el(los) tenant(s) asociados a un email.
    Responde JSON si es XHR, redirect si es form normal.
    """
    email = (request.form.get("email") or request.json.get("email", "")).strip().lower()

    if not email:
        if request.is_json:
            return jsonify({"error": "Email requerido"}), 400
        flash("Ingresa tu correo electrónico.", "warning")
        return redirect(url_for("auth.landing"))

    gdb = _gdb()
    rows = gdb.execute(
        """SELECT tu.email, t.slug, t.nombre_despacho, t.activo
           FROM tenant_users tu
           JOIN tenants t ON tu.tenant_id = t.id
           WHERE LOWER(tu.email) = ? AND tu.activo = 1""",
        (email,),
    ).fetchall()

    base_domain = current_app.config["BASE_DOMAIN"]
    # Sin subdominios por despacho (local o un solo dominio, p. ej. Railway) el despacho
    # se elige con ?tenant= y queda en la sesión.
    req_host = request.host.split(":")[0]
    is_local = req_host in ("localhost", "127.0.0.1") or not req_host.endswith(f".{base_domain}")

    if not rows:
        if request.is_json:
            return jsonify({"found": False, "message": "No encontramos ninguna cuenta con ese correo."})
        flash("No encontramos ninguna cuenta con ese correo electrónico.", "danger")
        return redirect(url_for("auth.landing"))

    if len(rows) == 1:
        tenant = rows[0]
        if not tenant["activo"]:
            if request.is_json:
                return jsonify({"found": False, "message": "Este despacho está inactivo."})
            flash("Este despacho está inactivo.", "danger")
            return redirect(url_for("auth.landing"))

        slug = tenant["slug"]
        if is_local:
            redirect_url = url_for("auth.login", tenant=slug, prefill_email=email)
            session["tenant_slug"] = slug
        else:
            redirect_url = f"https://{slug}.{base_domain}/auth/login?email={email}"

        if request.is_json:
            return jsonify({
                "found": True,
                "count": 1,
                "redirect": redirect_url,
                "tenant_name": tenant["nombre_despacho"],
            })
        return redirect(redirect_url)

    # Múltiples tenants: mostrar selector
    tenants_list = [dict(r) for r in rows]
    if request.is_json:
        return jsonify({"found": True, "count": len(tenants_list), "tenants": tenants_list})

    return render_template("auth/select_tenant.html", tenants=tenants_list, email=email)


# ─────────────────────────────────────────────────────────────────────────────
#  Login
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if getattr(g, "user", None) and getattr(g, "tenant", None):
        return redirect(url_for("dashboard.index"))

    prefill_email = request.args.get("prefill_email", "")
    next_url = request.args.get("next", "")

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember_me"))
        next_url = request.form.get("next_url", "")

        tenant = getattr(g, "tenant", None)
        if not tenant:
            flash("No se detectó el despacho. Verifica la URL.", "danger")
            return render_template("auth/login.html", prefill_email=email, next_url=next_url)

        gdb = _gdb()
        user = gdb.execute(
            """SELECT * FROM tenant_users
               WHERE tenant_id=? AND LOWER(email)=? AND activo=1""",
            (tenant["id"], email),
        ).fetchone()

        if not user or not _verify(password, user["password_hash"]):
            flash("Correo o contraseña incorrectos.", "danger")
            _log("login_failed", details={"email": email})
            return render_template(
                "auth/login.html", prefill_email=email, next_url=next_url, error=True
            )

        # ── Sesión válida ─────────────────────────────────────────────────────
        session.clear()
        if remember:
            session.permanent = True
        else:
            session.permanent = False

        session["user_id"]    = user["id"]
        session["tenant_id"]  = tenant["id"]
        session["tenant_slug"] = tenant["slug"]
        session["tenant_name"] = tenant["nombre_despacho"]
        session["role"]       = user["rol"]
        session["user"] = {
            "id": user["id"],
            "nombre": user["nombre"],
            "apellidos": user["apellidos"],
            "email": user["email"],
            "rol": user["rol"],
            "avatar_url": user["avatar_url"],
        }

        gdb.execute(
            "UPDATE tenant_users SET ultimo_acceso=CURRENT_TIMESTAMP WHERE id=?",
            (user["id"],),
        )
        gdb.commit()

        _log("login")
        _ensure_onboarding(user["id"])

        # ── Verificar si el onboarding está completo ──────────────────────────
        from app import get_tenant_db
        tdb_conn = get_tenant_db(tenant["slug"])
        ob = tdb_conn.execute(
            "SELECT completado FROM onboarding_progress WHERE user_id=?",
            (user["id"],),
        ).fetchone()

        if next_url and next_url.startswith("/"):
            return redirect(next_url)

        if ob and not ob["completado"] and user["rol"] in ("super_admin", "admin"):
            return redirect(url_for("onboarding.tour"))

        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html", prefill_email=prefill_email, next_url=next_url)


# ─────────────────────────────────────────────────────────────────────────────
#  Logout
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/logout")
def logout():
    _log("logout")
    session.clear()
    flash("Sesión cerrada correctamente.", "success")
    return redirect(url_for("auth.landing"))


# ─────────────────────────────────────────────────────────────────────────────
#  Forgot password
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        tenant = getattr(g, "tenant", None)

        if not email:
            flash("Ingresa tu correo electrónico.", "warning")
            return render_template("auth/forgot_password.html")

        gdb = _gdb()

        if tenant:
            user = gdb.execute(
                """SELECT tu.*, t.slug, t.nombre_despacho
                   FROM tenant_users tu JOIN tenants t ON tu.tenant_id=t.id
                   WHERE tu.tenant_id=? AND LOWER(tu.email)=? AND tu.activo=1""",
                (tenant["id"], email),
            ).fetchone()
        else:
            user = gdb.execute(
                """SELECT tu.*, t.slug, t.nombre_despacho
                   FROM tenant_users tu JOIN tenants t ON tu.tenant_id=t.id
                   WHERE LOWER(tu.email)=? AND tu.activo=1 AND t.activo=1
                   LIMIT 1""",
                (email,),
            ).fetchone()

        # Siempre mostrar el mismo mensaje (anti-enumeración)
        flash(
            "Si ese correo está registrado, recibirás un enlace en minutos.",
            "success",
        )

        if user:
            token = secrets.token_urlsafe(48)
            ttl = current_app.config.get("RESET_TOKEN_TTL_HOURS", 24)
            expires = datetime.utcnow() + timedelta(hours=ttl)

            # Invalidar tokens previos del usuario
            gdb.execute(
                "UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
                (user["id"],),
            )
            gdb.execute(
                """INSERT INTO password_reset_tokens
                   (user_id, tenant_id, token, expires_at)
                   VALUES (?,?,?,?)""",
                (user["id"], user["tenant_id"], token, expires.isoformat()),
            )
            gdb.commit()

            reset_url = _reset_link(token)
            nombre_despacho = user["nombre_despacho"]
            nombre_usuario = f"{user['nombre']} {user['apellidos']}".strip()

            html = f"""
            <div style="font-family:sans-serif;max-width:520px;margin:0 auto;">
              <h2 style="color:#AD9060;">LexOS — Restablecer contraseña</h2>
              <p>Hola <strong>{nombre_usuario}</strong>,</p>
              <p>Recibimos una solicitud para restablecer tu contraseña en
                 <strong>{nombre_despacho}</strong>.</p>
              <p style="margin:24px 0;">
                <a href="{reset_url}"
                   style="background:#AD9060;color:#080C10;padding:12px 24px;
                          border-radius:6px;text-decoration:none;font-weight:600;">
                  Restablecer contraseña
                </a>
              </p>
              <p style="color:#60718D;font-size:13px;">
                Este enlace expira en {ttl} horas. Si no solicitaste esto, ignora este correo.
              </p>
              <p style="color:#60718D;font-size:12px;">
                O copia esta URL: {reset_url}
              </p>
            </div>
            """
            _send_email(email, "Restablecer contraseña — LexOS", html)

        return redirect(url_for("auth.forgot_password"))

    return render_template("auth/forgot_password.html")


# ─────────────────────────────────────────────────────────────────────────────
#  Reset password
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    gdb = _gdb()
    now = datetime.utcnow().isoformat()

    record = gdb.execute(
        """SELECT prt.*, tu.email, tu.nombre, tu.apellidos, t.slug
           FROM password_reset_tokens prt
           JOIN tenant_users tu ON prt.user_id = tu.id
           JOIN tenants t ON prt.tenant_id = t.id
           WHERE prt.token=? AND prt.used=0 AND prt.expires_at > ?""",
        (token, now),
    ).fetchone()

    if not record:
        flash("Este enlace es inválido o ha expirado. Solicita uno nuevo.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_pass = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if len(new_pass) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "danger")
            return render_template("auth/reset_password.html", token=token, record=record)
        if new_pass != confirm:
            flash("Las contraseñas no coinciden.", "danger")
            return render_template("auth/reset_password.html", token=token, record=record)

        gdb.execute(
            "UPDATE tenant_users SET password_hash=? WHERE id=?",
            (_hash(new_pass), record["user_id"]),
        )
        gdb.execute(
            "UPDATE password_reset_tokens SET used=1 WHERE token=?",
            (token,),
        )
        gdb.commit()
        flash("Contraseña restablecida. Ahora puedes iniciar sesión.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token, record=record)


# ─────────────────────────────────────────────────────────────────────────────
#  Registro de nuevo despacho
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/register-tenant", methods=["GET", "POST"])
def register_tenant():
    from config import Config

    if request.method == "POST":
        nombre   = request.form.get("nombre_despacho", "").strip()
        rfc      = request.form.get("rfc", "").strip().upper()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        plan     = request.form.get("plan", "starter")
        tipos    = request.form.getlist("tipos_despacho")

        errors = []
        if not nombre:
            errors.append("El nombre del despacho es obligatorio.")
        if not email or "@" not in email:
            errors.append("Correo electrónico inválido.")
        if len(password) < 8:
            errors.append("La contraseña debe tener al menos 8 caracteres.")
        if password != confirm:
            errors.append("Las contraseñas no coinciden.")
        if plan not in [p["id"] for p in Config.PLANES]:
            plan = "starter"
        if request.form.get("acepto_terminos") != "1":
            errors.append("Para crear tu despacho debes aceptar los términos y el aviso de privacidad.")

        gdb = _gdb()

        # Verificar email único globalmente
        existing = gdb.execute(
            "SELECT id FROM tenant_users WHERE LOWER(email)=?", (email,)
        ).fetchone()
        if existing:
            errors.append("Este correo ya tiene una cuenta. Inicia sesión.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "auth/register_tenant.html",
                form_data=request.form,
                planes=Config.PLANES,
                tipos_despacho=Config.TIPOS_DESPACHO,
            )

        # ── Generar slug único ────────────────────────────────────────────────
        base_slug = _slugify(nombre)
        slug = base_slug
        i = 2
        while gdb.execute("SELECT id FROM tenants WHERE slug=?", (slug,)).fetchone():
            slug = f"{base_slug}-{i}"
            i += 1

        # ── Crear tenant en DB global ─────────────────────────────────────────
        cur = gdb.execute(
            """INSERT INTO tenants
               (slug, nombre_despacho, plan, email_contacto, rfc, tipos_despacho)
               VALUES (?,?,?,?,?,?)""",
            (slug, nombre, plan, email, rfc, json.dumps(tipos)),
        )
        tenant_id = cur.lastrowid

        # ── Crear usuario admin ───────────────────────────────────────────────
        nombre_parts = email.split("@")[0].replace(".", " ").replace("_", " ").title().split()
        nombre_usr   = nombre_parts[0] if nombre_parts else "Admin"
        apellido_usr = " ".join(nombre_parts[1:]) if len(nombre_parts) > 1 else ""

        cur2 = gdb.execute(
            """INSERT INTO tenant_users
               (tenant_id, nombre, apellidos, email, password_hash, rol)
               VALUES (?,?,?,?,?,?)""",
            (tenant_id, nombre_usr, apellido_usr, email, _hash(password), "super_admin"),
        )
        user_id = cur2.lastrowid
        gdb.commit()

        # ── Inicializar DB del tenant ─────────────────────────────────────────
        os.makedirs(Config.DATABASE_DIR, exist_ok=True)
        _init_tenant_db(slug)

        # ── Onboarding inicial ────────────────────────────────────────────────
        from app import get_tenant_db
        tdb_conn = get_tenant_db(slug)
        tdb_conn.execute(
            "INSERT OR IGNORE INTO onboarding_progress (user_id) VALUES (?)", (user_id,)
        )
        tdb_conn.commit()

        # ── Autologin ─────────────────────────────────────────────────────────
        session.clear()
        session.permanent = True
        session["user_id"]    = user_id
        session["tenant_id"]  = tenant_id
        session["tenant_slug"] = slug
        session["tenant_name"] = nombre
        session["role"]       = "super_admin"
        session["user"] = {
            "id": user_id, "nombre": nombre_usr, "apellidos": apellido_usr,
            "email": email, "rol": "super_admin", "avatar_url": None,
        }
        from modules.legal import registrar_aceptacion
        registrar_aceptacion(user_id, tenant_id, email, "registro")

        # Email de bienvenida
        welcome_html = f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto;">
          <h2 style="color:#AD9060;">¡Bienvenido a LexOS!</h2>
          <p>Hola <strong>{nombre_usr}</strong>,</p>
          <p>Tu despacho <strong>{nombre}</strong> está listo.
             Tu subdominio de acceso es:</p>
          <p style="font-size:1.1em;font-weight:bold;color:#AD9060;">
            {slug}.lexos.mx
          </p>
          <p>Completa el tour de configuración para sacarle el máximo provecho a LexOS.</p>
        </div>
        """
        _send_email(email, f"¡Bienvenido a LexOS — {nombre}!", welcome_html)

        flash(f"¡Despacho creado! Completa la configuración inicial.", "success")
        return redirect(url_for("onboarding.tour"))

    return render_template(
        "auth/register_tenant.html",
        form_data={},
        planes=Config.PLANES,
        tipos_despacho=Config.TIPOS_DESPACHO,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Perfil
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    gdb = _gdb()
    user = gdb.execute(
        "SELECT * FROM tenant_users WHERE id=?", (session["user_id"],)
    ).fetchone()

    if request.method == "POST":
        nombre   = request.form.get("nombre", "").strip()
        apellidos = request.form.get("apellidos", "").strip()
        telefono = request.form.get("telefono", "").strip()
        new_pass = request.form.get("new_password", "")
        confirm  = request.form.get("confirm_password", "")

        if not nombre:
            flash("El nombre es obligatorio.", "danger")
            return render_template("auth/perfil.html", user=user)

        updates = {"nombre": nombre, "apellidos": apellidos, "telefono": telefono or None}

        if new_pass:
            if len(new_pass) < 8:
                flash("La contraseña debe tener al menos 8 caracteres.", "danger")
                return render_template("auth/perfil.html", user=user)
            if new_pass != confirm:
                flash("Las contraseñas no coinciden.", "danger")
                return render_template("auth/perfil.html", user=user)
            updates["password_hash"] = _hash(new_pass)

        set_clause = ", ".join(f"{k}=?" for k in updates)
        gdb.execute(
            f"UPDATE tenant_users SET {set_clause} WHERE id=?",
            (*updates.values(), session["user_id"]),
        )
        gdb.commit()

        session["user"]["nombre"] = nombre
        session["user"]["apellidos"] = apellidos
        flash("Perfil actualizado correctamente.", "success")
        return redirect(url_for("auth.perfil"))

    return render_template("auth/perfil.html", user=user)
