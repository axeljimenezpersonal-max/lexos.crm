"""
Términos y condiciones, aviso de privacidad y su aceptación.

Rutas:
  GET      /legal/terminos     — Términos y condiciones (público)
  GET      /legal/privacidad   — Aviso de privacidad (público)
  GET/POST /legal/aceptar      — Pantalla "Acepto" para quien no ha aceptado la versión vigente

Cada aceptación queda en la base global (aceptaciones_legales): quién, qué versión, cuándo y
desde qué IP. Si un usuario con sesión no ha aceptado la versión vigente, cualquier página del
sistema lo manda primero a /legal/aceptar.
"""
import sqlite3

from flask import (
    Blueprint, current_app, g, jsonify, redirect, render_template, request, session, url_for,
)

legal_bp = Blueprint("legal", __name__)

# Mientras los textos tengan datos [POR DEFINIR], las páginas lo avisan arriba.
BORRADOR = True

# Páginas que se pueden ver sin haber aceptado (además de /legal/*).
_LIBRES = {"auth.logout", "auth.landing", "auth.login", "rastreo.registrar"}
_PREFIJOS_LIBRES = ("static", "legal.", "revision.", "portal.")


def _versiones() -> tuple[str, str]:
    return current_app.config["TERMINOS_VERSION"], current_app.config["PRIVACIDAD_VERSION"]


def _ip() -> str:
    """IP real del usuario: detrás del proxy de Railway llega en X-Forwarded-For."""
    reenviada = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return (reenviada or request.remote_addr or "")[:64]


def _gdb():
    from app import get_global_db
    return get_global_db()


@legal_bp.record_once
def _asegurar_tabla(state):
    """Al arrancar la app se crean las tablas globales que falten, esta incluida (idempotente)."""
    from init_db import GLOBAL_SCHEMA
    try:
        con = sqlite3.connect(state.app.config["GLOBAL_DB"])
        con.executescript(GLOBAL_SCHEMA)
        con.commit()
        con.close()
    except sqlite3.Error as exc:
        state.app.logger.warning("No se pudo asegurar aceptaciones_legales: %s", exc)


def ya_acepto(user_id: int) -> bool:
    terminos, privacidad = _versiones()
    return _gdb().execute(
        """SELECT 1 FROM aceptaciones_legales
           WHERE user_id=? AND terminos_version=? AND privacidad_version=? LIMIT 1""",
        (user_id, terminos, privacidad),
    ).fetchone() is not None


def registrar_aceptacion(user_id: int, tenant_id: int, email: str, origen: str) -> None:
    """Guarda la constancia y la recuerda en la sesión. No hace commit si ya existía."""
    terminos, privacidad = _versiones()
    gdb = _gdb()
    gdb.execute(
        """INSERT INTO aceptaciones_legales
           (user_id, tenant_id, email, terminos_version, privacidad_version, origen, ip, user_agent)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, tenant_id, email, terminos, privacidad, origen, _ip(),
         (request.headers.get("User-Agent") or "")[:300]),
    )
    gdb.commit()
    session["legal_ok"] = f"{terminos}/{privacidad}"


def _destino_seguro(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//") and not url.startswith("/legal/aceptar"):
        return url
    return url_for("dashboard.index")


@legal_bp.before_app_request
def exigir_aceptacion():
    """Antes de usar el sistema, el usuario debe aceptar la versión vigente (una sola vez)."""
    user = getattr(g, "user", None)
    endpoint = request.endpoint or ""
    if user is None or endpoint in _LIBRES or endpoint.startswith(_PREFIJOS_LIBRES):
        return None
    terminos, privacidad = _versiones()
    if session.get("legal_ok") == f"{terminos}/{privacidad}":
        return None
    if ya_acepto(user["id"]):
        session["legal_ok"] = f"{terminos}/{privacidad}"
        return None
    destino = url_for("legal.aceptar", next=request.full_path.rstrip("?"))
    if request.method != "GET" or request.headers.get("X-Requested-With") == "XMLHttpRequest" \
            or request.accept_mimetypes.best == "application/json":
        return jsonify(error="Antes de continuar acepta los términos y el aviso de privacidad.",
                       aceptar=destino), 403
    return redirect(destino)


# ─────────────────────────────────────────────────────────────────────────────
#  Páginas
# ─────────────────────────────────────────────────────────────────────────────

@legal_bp.route("/terminos")
def terminos():
    return render_template("legal/terminos.html", borrador=BORRADOR,
                           version=current_app.config["TERMINOS_VERSION"])


@legal_bp.route("/privacidad")
def privacidad():
    return render_template("legal/privacidad.html", borrador=BORRADOR,
                           version=current_app.config["PRIVACIDAD_VERSION"])


@legal_bp.route("/aceptar", methods=["GET", "POST"])
def aceptar():
    user = getattr(g, "user", None)
    if user is None:
        return redirect(url_for("auth.login"))
    siguiente = _destino_seguro(request.values.get("next"))
    if ya_acepto(user["id"]):
        terminos, privacidad = _versiones()
        session["legal_ok"] = f"{terminos}/{privacidad}"
        return redirect(siguiente)

    error = False
    if request.method == "POST":
        if request.form.get("acepto") == "1":
            registrar_aceptacion(user["id"], user["tenant_id"], user["email"], "login")
            return redirect(siguiente)
        error = True

    # Si ya había aceptado una versión anterior, se le dice que los textos cambiaron.
    anterior = _gdb().execute(
        "SELECT 1 FROM aceptaciones_legales WHERE user_id=? LIMIT 1", (user["id"],)
    ).fetchone() is not None
    return render_template("legal/aceptar.html", siguiente=siguiente, error=error,
                           actualizacion=anterior, nombre=user["nombre"])
