import os
import sqlite3
from flask import Flask, g, session, request, redirect, url_for, render_template
from flask_mail import Mail
from config import config, Config

# Mail instance — importado por módulos que necesiten enviar correo
mail = Mail()


def get_global_db():
    """Retorna conexión a la base de datos global."""
    if "global_db" not in g:
        os.makedirs(Config.DATABASE_DIR, exist_ok=True)
        g.global_db = sqlite3.connect(Config.GLOBAL_DB)
        g.global_db.row_factory = sqlite3.Row
        g.global_db.execute("PRAGMA foreign_keys = ON")
    return g.global_db


def get_tenant_db(slug: str):
    """Retorna conexión a la base de datos del tenant."""
    key = f"tenant_db_{slug}"
    if key not in g:
        db_path = Config.tenant_db(slug)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Auto-migrate: apply any missing columns idempotently
        from init_db import _MIGRATE_COLS
        for _tbl, _col, _coldef in _MIGRATE_COLS:
            try:
                conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_coldef}")
            except Exception:
                pass  # column already exists
        conn.commit()
        setattr(g, key, conn)
    return getattr(g, key)


def resolve_tenant(app):
    """Resuelve el tenant desde subdominio, header, query param o sesión."""
    slug = None

    # 1. Subdominio en producción
    host = request.host.split(":")[0]
    base_domain = app.config["BASE_DOMAIN"]
    if host.endswith(f".{base_domain}"):
        slug = host[: -(len(base_domain) + 1)]

    # 2. Header para desarrollo / API
    if not slug:
        slug = request.headers.get("X-Tenant-Slug")

    # 3. Query param para desarrollo
    if not slug:
        slug = request.args.get("tenant")

    # 4. Sesión activa
    if not slug:
        slug = session.get("tenant_slug")

    # 5. Default si está configurado (solo dev con DEFAULT_TENANT_SLUG)
    if not slug:
        slug = app.config.get("DEFAULT_TENANT_SLUG") or None

    return slug


def create_app(env="default"):
    app = Flask(__name__)
    cfg = config[env]
    app.config.from_object(cfg)

    os.makedirs(app.config["DATABASE_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # ─── Flask-Mail ───────────────────────────────────────────────────────────
    mail.init_app(app)

    # ─── Teardown de DBs ──────────────────────────────────────────────────────
    @app.teardown_appcontext
    def close_dbs(exc):
        db = g.pop("global_db", None)
        if db:
            db.close()
        for key in list(vars(g).keys()):
            if key.startswith("tenant_db_"):
                g.pop(key).close()

    # ─── Middleware multi-tenant + usuario ───────────────────────────────────
    @app.before_request
    def load_context():
        if request.endpoint and request.endpoint.startswith("static"):
            return

        # ── Resolver tenant ───────────────────────────────────────────────────
        slug = resolve_tenant(app)
        g.tenant = None
        g.tenant_slug = slug or ""
        g.user = None

        if slug:
            gdb = get_global_db()
            tenant = gdb.execute(
                "SELECT * FROM tenants WHERE slug=? AND activo=1", (slug,)
            ).fetchone()
            g.tenant = tenant

            if tenant:
                g.tenant_db = get_tenant_db(slug)
                session["tenant_slug"] = slug

                # ── Cargar usuario desde sesión ───────────────────────────────
                user_id = session.get("user_id")
                if user_id:
                    user = gdb.execute(
                        """SELECT * FROM tenant_users
                           WHERE id=? AND tenant_id=? AND activo=1""",
                        (user_id, tenant["id"]),
                    ).fetchone()
                    g.user = user

    # ─── Dev helper: inyectar ?tenant= en url_for cuando DEBUG ────────────────
    @app.url_defaults
    def inject_tenant_param(endpoint, values):
        """En modo DEBUG, propaga ?tenant=slug en todos los url_for."""
        if app.debug and endpoint and not endpoint.startswith("static"):
            slug = values.pop("tenant", None) or getattr(g, "tenant_slug", None) or session.get("tenant_slug")
            if slug:
                values["tenant"] = slug

    @app.url_value_preprocessor
    def pull_tenant_param(endpoint, values):
        """Extrae tenant de la URL antes de que llegue al view."""
        if values:
            values.pop("tenant", None)

    # ─── Context processor ───────────────────────────────────────────────────
    @app.context_processor
    def inject_globals():
        tenant = getattr(g, "tenant", None)
        user = getattr(g, "user", None) or session.get("user")
        role = session.get("role")

        # Estado de onboarding
        onboarding_done = True
        onboarding_pct  = 100
        if user and tenant and hasattr(g, "tenant_db"):
            try:
                ob = g.tenant_db.execute(
                    "SELECT completado, fase_actual FROM onboarding_progress WHERE user_id=?",
                    (session.get("user_id"),),
                ).fetchone()
                if ob:
                    onboarding_done = bool(ob["completado"])
                    onboarding_pct  = 100 if onboarding_done else (ob["fase_actual"] * 25)
            except Exception:
                pass

        # Avisos de la campana: lo que el usuario aún no ha leído
        notif_count = 0
        notif_items = []
        hoy_agenda = 0
        if user and tenant and hasattr(g, "tenant_db"):
            try:
                from modules.avisos import avisos as _avisos
                from datetime import datetime as _dtz
                from modules.dashboard import ZONA as _ZONA
                notif_items = _avisos(g.tenant_db, session.get("user_id"))
                notif_count = sum(1 for n in notif_items if not n["leido"])
                hoy_agenda = g.tenant_db.execute(
                    "SELECT COUNT(*) FROM eventos_calendario WHERE estado='pendiente' AND date(fecha_inicio)=?",
                    (_dtz.now(_ZONA).date().isoformat(),)).fetchone()[0]
            except Exception:
                app.logger.exception("No se pudieron calcular los avisos")

        from datetime import datetime as _dt
        return {
            "tenant": tenant,
            "current_user": user,
            "current_role": role,
            "onboarding_done": onboarding_done,
            "onboarding_pct": onboarding_pct,
            "notif_count": notif_count,
            "notif_items": notif_items,
            "hoy_agenda": hoy_agenda,
            "now": _dt.now,
            "today_str": _dt.now().strftime("%Y-%m-%d"),
        }

    # ─── Error handlers ──────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    # ─── Jinja2 filters ───────────────────────────────────────────────────────
    import json as _json
    @app.template_filter("from_json")
    def from_json_filter(s):
        try:
            return _json.loads(s) if s else []
        except Exception:
            return []

    # ─── Blueprints ───────────────────────────────────────────────────────────
    from modules.auth         import auth_bp
    from modules.dashboard    import dashboard_bp
    from modules.onboarding   import onboarding_bp
    from modules.asuntos      import asuntos_bp
    from modules.clientes     import clientes_bp
    from modules.cobranzas    import cobranzas_bp
    from modules.calendario   import calendario_bp
    from modules.investigacion import investigacion_bp
    from modules.biblioteca   import biblioteca_bp
    from modules.estadisticas  import estadisticas_bp
    from modules.reportes     import reportes_bp
    from modules.portal       import portal_bp
    from modules.equipo       import equipo_bp
    from modules.correo       import correo_bp
    from modules.contrapartes import contrapartes_bp
    from modules.boveda import boveda_bp
    from modules.lector import lector_bp
    from modules.corpus import corpus_bp
    from modules.asistente import asistente_bp
    from modules.insights import insights_bp
    from modules.revision import revision_bp
    from modules.buscador import buscador_bp
    from modules.avisos import avisos_bp

    app.register_blueprint(auth_bp,         url_prefix="/auth")
    app.register_blueprint(dashboard_bp,    url_prefix="/")
    app.register_blueprint(onboarding_bp,   url_prefix="/onboarding")
    app.register_blueprint(asuntos_bp,      url_prefix="/asuntos")
    app.register_blueprint(clientes_bp,     url_prefix="/clientes")
    app.register_blueprint(cobranzas_bp,    url_prefix="/cobranzas")
    app.register_blueprint(calendario_bp,   url_prefix="/calendario")
    app.register_blueprint(investigacion_bp, url_prefix="/investigacion")
    app.register_blueprint(biblioteca_bp,   url_prefix="/biblioteca")
    app.register_blueprint(estadisticas_bp, url_prefix="/estadisticas")
    app.register_blueprint(reportes_bp,     url_prefix="/reportes")
    app.register_blueprint(portal_bp,       url_prefix="/portal")
    app.register_blueprint(equipo_bp,       url_prefix="/configuracion/equipo")
    app.register_blueprint(correo_bp,       url_prefix="/correo")
    app.register_blueprint(contrapartes_bp, url_prefix="/contrapartes")
    app.register_blueprint(boveda_bp)
    app.register_blueprint(lector_bp, url_prefix="/lector")
    app.register_blueprint(corpus_bp, url_prefix="/base-legal")
    app.register_blueprint(asistente_bp, url_prefix="/asistente")
    app.register_blueprint(insights_bp, url_prefix="/precedentes")
    app.register_blueprint(revision_bp, url_prefix="/revision")
    app.register_blueprint(buscador_bp, url_prefix="/buscar")
    app.register_blueprint(avisos_bp, url_prefix="/avisos")

    # ─── Modo revisión: el sitio de pruebas no debe aparecer en buscadores ───
    @app.after_request
    def no_indexar(resp):
        if app.config.get("REVISION_ACTIVA"):
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    return app


# ─── Helpers de acceso a DB (para uso en módulos) ────────────────────────────
def tdb():
    return g.tenant_db


def gdb():
    return get_global_db()


if __name__ == "__main__":
    env = os.environ.get("FLASK_ENV", "development")
    application = create_app(env)
    port = int(os.environ.get("PORT", 5001))
    application.run(host="0.0.0.0", port=port, debug=application.config["DEBUG"])
