"""
Habeas Corpus — modules/investigacion.py
La página «Investigación» ya no existe: la búsqueda vive en la Bóveda (biblioteca.index), dentro del
contenido de cada documento. Aquí quedan las rutas de colecciones y órganos jurisdiccionales.
"""
from __future__ import annotations

from flask import (
    Blueprint, g, jsonify, redirect, render_template, request, session, url_for,
)

from modules.auth import login_required, tenant_required

investigacion_bp = Blueprint("investigacion", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — main page
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/")
@login_required
@tenant_required
def index():
    """La investigación ahora vive en la Bóveda: se busca dentro de los documentos, uno por uno."""
    q = (request.args.get("q") or "").strip()[:300]
    return redirect(url_for("biblioteca.index", texto=q or None))


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — Colecciones
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/coleccion/crear", methods=["POST"])
@login_required
@tenant_required
def coleccion_crear():
    nombre = (request.form.get("nombre") or "").strip()
    asunto_id = _safe_int(request.form.get("asunto_id"))
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400
    g.tenant_db.execute(
        "INSERT INTO inv_colecciones (nombre, asunto_id, creado_por) VALUES (?,?,?)",
        (nombre, asunto_id, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/coleccion/<int:cid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def coleccion_eliminar(cid):
    g.tenant_db.execute("DELETE FROM inv_colecciones WHERE id=? AND creado_por=?",
                        (cid, session.get("user_id")))
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/coleccion/<int:cid>/agregar", methods=["POST"])
@login_required
@tenant_required
def coleccion_agregar_item(cid):
    data = request.get_json() or request.form
    tipo = data.get("tipo", "")
    titulo = (data.get("titulo") or "").strip()
    entidad_id = _safe_int(data.get("entidad_id"))
    url = data.get("url", "")
    notas = data.get("notas", "")

    if not titulo:
        return jsonify(ok=False, error="Título requerido"), 400

    g.tenant_db.execute(
        "INSERT INTO inv_coleccion_items (coleccion_id, tipo, entidad_id, titulo, url, notas) VALUES (?,?,?,?,?,?)",
        (cid, tipo, entidad_id, titulo, url, notas),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/coleccion/item/<int:item_id>/eliminar", methods=["POST"])
@login_required
@tenant_required
def coleccion_item_eliminar(item_id):
    g.tenant_db.execute("DELETE FROM inv_coleccion_items WHERE id=?", (item_id,))
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — Órganos
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/organo/crear", methods=["POST"])
@login_required
@tenant_required
def organo_crear():
    data = request.get_json() or request.form
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400
    g.tenant_db.execute(
        "INSERT INTO inv_organos (nombre, circuito, estado_mex, tipo_organo, materia, es_custom, creado_por) VALUES (?,?,?,?,?,1,?)",
        (nombre, data.get("circuito", ""), data.get("estado_mex", ""),
         data.get("tipo_organo", ""), data.get("materia", ""), session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/organo/<int:oid>/editar", methods=["POST"])
@login_required
@tenant_required
def organo_editar(oid):
    data = request.get_json() or request.form
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400
    g.tenant_db.execute(
        "UPDATE inv_organos SET nombre=?, circuito=?, estado_mex=?, tipo_organo=?, materia=? WHERE id=?",
        (nombre, data.get("circuito", ""), data.get("estado_mex", ""),
         data.get("tipo_organo", ""), data.get("materia", ""), oid),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/organo/<int:oid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def organo_eliminar(oid):
    g.tenant_db.execute("DELETE FROM inv_organos WHERE id=? AND es_custom=1", (oid,))
    g.tenant_db.commit()
    return jsonify(ok=True)


@investigacion_bp.route("/organo/<int:oid>/vincular", methods=["POST"])
@login_required
@tenant_required
def organo_vincular(oid):
    asunto_id = _safe_int(request.form.get("asunto_id"))
    if not asunto_id:
        return jsonify(ok=False, error="Asunto requerido"), 400
    organo = g.tenant_db.execute("SELECT nombre FROM inv_organos WHERE id=?", (oid,)).fetchone()
    if not organo:
        return jsonify(ok=False, error="Órgano no encontrado"), 404
    # Update asunto's juzgado field
    g.tenant_db.execute("UPDATE asuntos SET juzgado=? WHERE id=?", (organo["nombre"], asunto_id))
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — Guardar en colección desde resultado
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/guardar-resultado", methods=["POST"])
@login_required
@tenant_required
def guardar_resultado():
    data = request.get_json() or {}
    coleccion_id = _safe_int(data.get("coleccion_id"))
    if not coleccion_id:
        return jsonify(ok=False, error="Selecciona una colección"), 400

    g.tenant_db.execute(
        "INSERT INTO inv_coleccion_items (coleccion_id, tipo, entidad_id, titulo, url) VALUES (?,?,?,?,?)",
        (coleccion_id, data.get("tipo", ""), _safe_int(data.get("entidad_id")),
         data.get("titulo", ""), data.get("url", "")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Legacy routes (kept for backwards compat)
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/detalle/<int:id>")
@login_required
@tenant_required
def detalle(id):
    inv = g.tenant_db.execute(
        "SELECT i.*, a.titulo as asunto_titulo FROM investigaciones i "
        "LEFT JOIN asuntos a ON i.asunto_id=a.id WHERE i.id=?", (id,)
    ).fetchone()
    if not inv:
        return redirect(url_for("biblioteca.index"))
    return render_template("investigacion/detalle.html", investigacion=inv)
