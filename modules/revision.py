"""
Habeas Corpus — modules/revision.py
Modo revisión: botón "Comentar" en todas las pantallas (se activa con REVISION_ACTIVA=1)
para que socios y probadores marquen lo que está mal. Los comentarios se guardan en la
base global con la página, el punto señalado y quién lo escribió.
"""
from __future__ import annotations

import hmac

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, session

from modules.auth import login_required

revision_bp = Blueprint("revision", __name__)

TIPOS = {
    "error":  {"label": "Error",  "color": "#DC2626", "fondo": "#FEF2F2"},
    "diseno": {"label": "Diseño", "color": "#6D28D9", "fondo": "#F5F3FF"},
    "texto":  {"label": "Texto",  "color": "#B45309", "fondo": "#FFFBEB"},
    "idea":   {"label": "Idea",   "color": "#1D3FD6", "fondo": "#EEF2FF"},
}


def _gdb():
    from app import get_global_db
    db = get_global_db()
    db.execute("""CREATE TABLE IF NOT EXISTS revision_comentarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT, texto TEXT NOT NULL, url TEXT, pagina TEXT, ancho INTEGER,
        senal TEXT, autor TEXT, despacho TEXT, user_id INTEGER, ip TEXT,
        estado TEXT DEFAULT 'pendiente', fecha DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    return db


def activa() -> bool:
    return bool(current_app.config.get("REVISION_ACTIVA"))


def _autor() -> str:
    u = session.get("user") or {}
    nombre = f"{u.get('nombre', '')} {u.get('apellidos', '') or ''}".strip()
    return nombre or ""


@revision_bp.route("/comentario", methods=["POST"])
def comentar():
    if not activa():
        abort(404)
    d = request.get_json(silent=True) or {}
    texto = (d.get("texto") or "").strip()[:3000]
    if not texto:
        return jsonify(ok=False, error="Escribe tu comentario"), 400
    autor = _autor() or (d.get("nombre") or "").strip()[:80]
    if not autor:
        return jsonify(ok=False, error="Escribe tu nombre para saber quién comenta"), 400
    db = _gdb()
    if db.execute("SELECT COUNT(*) FROM revision_comentarios WHERE ip=? AND fecha >= datetime('now','-1 hour')",
                  (request.remote_addr,)).fetchone()[0] >= 60:
        return jsonify(ok=False, error="Demasiados comentarios seguidos. Intenta en un rato."), 429
    tenant = getattr(g, "tenant", None)
    db.execute(
        "INSERT INTO revision_comentarios (tipo, texto, url, pagina, ancho, senal, autor, despacho, user_id, ip) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (d.get("tipo") if d.get("tipo") in TIPOS else "idea", texto, (d.get("url") or "")[:500],
         (d.get("pagina") or "")[:200], int(d.get("ancho") or 0) or None, (d.get("senal") or "")[:300] or None,
         autor, tenant["nombre_despacho"] if tenant else None, session.get("user_id"), request.remote_addr))
    db.commit()
    return jsonify(ok=True)


def _lista():
    filas = _gdb().execute("SELECT * FROM revision_comentarios ORDER BY estado='resuelto', id DESC LIMIT 1000").fetchall()
    return [dict(r) for r in filas]


@revision_bp.route("/")
@login_required
def index():
    if not activa() or session.get("role") not in ("super_admin", "admin"):
        abort(404)
    comentarios = _lista()
    return render_template("revision/index.html", comentarios=comentarios, tipos=TIPOS,
                           pendientes=sum(1 for c in comentarios if c["estado"] != "resuelto"))


@revision_bp.route("/<int:cid>/estado", methods=["POST"])
@login_required
def cambiar_estado(cid: int):
    if not activa() or session.get("role") not in ("super_admin", "admin"):
        abort(404)
    estado = (request.get_json(silent=True) or {}).get("estado")
    if estado not in ("pendiente", "resuelto"):
        return jsonify(ok=False), 400
    db = _gdb()
    db.execute("UPDATE revision_comentarios SET estado=? WHERE id=?", (estado, cid))
    db.commit()
    return jsonify(ok=True)


@revision_bp.route("/exportar.json")
def exportar():
    """Para leer los comentarios desde fuera (header X-Revision-Clave)."""
    clave = current_app.config.get("REVISION_CLAVE") or ""
    dada = request.headers.get("X-Revision-Clave", "")
    if not activa() or not clave or not hmac.compare_digest(clave, dada):
        abort(404)
    return jsonify([{k: v for k, v in c.items() if k != "ip"} for c in _lista()])
