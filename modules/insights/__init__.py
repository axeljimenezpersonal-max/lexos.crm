"""Precedentes del despacho (casos parecidos y cómo terminaron) y llenado de machotes."""
import io
import os

from flask import (Blueprint, abort, current_app, g, jsonify, render_template, request, send_file, session,
                   url_for)
from werkzeug.datastructures import FileStorage

from modules import boveda, lector
from modules.auth import log_activity, login_required, role_required, tenant_required
from modules.insights import casos, machotes

insights_bp = Blueprint("insights", __name__)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_VALOR = 2000


def _ajax():
    if request.headers.get("X-Requested-With") != "XMLHttpRequest" or not request.is_json:
        abort(400)
    return request.get_json(silent=True) or {}


def _machotes(tdb) -> list[dict]:
    """Plantillas Word de la biblioteca que se pueden llenar."""
    return [dict(p) for p in tdb.execute(
        "SELECT id, nombre, categoria, tipo_derecho, descripcion, archivo_url, archivo_nombre "
        "FROM biblioteca_plantillas WHERE archivo_url LIKE '/archivos/%.docx' ORDER BY categoria, nombre"
    ).fetchall()]


def _plantilla(pid: int):
    p = g.tenant_db.execute("SELECT * FROM biblioteca_plantillas WHERE id=?", (pid,)).fetchone()
    if not p or not boveda.es_de_boveda(p["archivo_url"]) or not p["archivo_url"].endswith(".docx"):
        abort(404)
    return p


def _asuntos_para_elegir(tdb):
    return tdb.execute(
        "SELECT a.id, a.titulo, a.numero_expediente, a.tipo_asunto, "
        "COALESCE(c.razon_social, c.nombre || ' ' || COALESCE(c.apellidos, '')) AS cliente "
        "FROM asuntos a LEFT JOIN clientes c ON c.id = a.cliente_id "
        "WHERE a.estado != 'archivado' ORDER BY a.fecha_alta DESC LIMIT 400"
    ).fetchall()


# ── Precedentes ───────────────────────────────────────────────────────────────

@insights_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    return render_template(
        "insights/index.html",
        stats=casos.estadisticas(tdb),
        resultados=casos.RESULTADOS,
        machotes=_machotes(tdb),
        campos_disponibles=machotes.CAMPOS,
        seccion=request.args.get("seccion", "precedentes"),
    )


@insights_bp.route("/asunto/<int:asunto_id>/parecidos")
@login_required
@tenant_required
def parecidos(asunto_id):
    if not g.tenant_db.execute("SELECT 1 FROM asuntos WHERE id=?", (asunto_id,)).fetchone():
        abort(404)
    from modules.asuntos import puede_ver
    datos = casos.similares(current_app._get_current_object(), g.tenant_db, asunto_id)
    for c in datos["casos"]:
        c["url"] = url_for("asuntos.detalle", id=c["id"]) if puede_ver(g.tenant_db, c["id"]) else None
    return jsonify(ok=True, **datos)


@insights_bp.route("/asunto/<int:asunto_id>/resultado", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def guardar_resultado(asunto_id):
    datos = _ajax()
    resultado = datos.get("resultado") or None
    if resultado and resultado not in casos.RESULTADOS:
        return jsonify(ok=False, error="Resultado no válido"), 400
    notas = (datos.get("notas") or "").strip()[:3000] or None
    tdb = g.tenant_db
    casos.asegurar(tdb)
    cur = tdb.execute("UPDATE asuntos SET resultado=?, resultado_notas=? WHERE id=?", (resultado, notas, asunto_id))
    if not cur.rowcount:
        abort(404)
    if resultado:
        tdb.execute("UPDATE asuntos SET fecha_cierre=COALESCE(fecha_cierre, date('now')) WHERE id=?", (asunto_id,))
    tdb.commit()
    log_activity("resultado_asunto", "asunto", asunto_id, {"resultado": resultado})
    return jsonify(ok=True, resultado=resultado, info=casos.RESULTADOS.get(resultado or ""))


# ── Machotes ──────────────────────────────────────────────────────────────────

@insights_bp.route("/machotes/<int:pid>")
@login_required
@tenant_required
def machote(pid):
    p = _plantilla(pid)
    try:
        lista = machotes.campos(boveda.leer(p["archivo_url"]))
    except Exception:
        lista = None
    return render_template(
        "insights/machote.html", plantilla=dict(p), campos=lista,
        asuntos=_asuntos_para_elegir(g.tenant_db), asunto_inicial=request.args.get("asunto", type=int),
    )


@insights_bp.route("/machotes/<int:pid>/valores")
@login_required
@tenant_required
def machote_valores(pid):
    _plantilla(pid)
    asunto_id = request.args.get("asunto", type=int)
    if asunto_id and not g.tenant_db.execute("SELECT 1 FROM asuntos WHERE id=?", (asunto_id,)).fetchone():
        abort(404)
    return jsonify(ok=True, valores=machotes.datos(g.tenant_db, g.tenant, g.user, asunto_id))


@insights_bp.route("/machotes/<int:pid>/generar", methods=["POST"])
@login_required
@tenant_required
def machote_generar(pid):
    p = _plantilla(pid)
    datos = _ajax()
    tdb = g.tenant_db
    asunto_id = datos.get("asunto_id")
    asunto_id = int(asunto_id) if str(asunto_id or "").isdigit() else None
    asunto = tdb.execute("SELECT id, titulo, numero_expediente FROM asuntos WHERE id=?",
                         (asunto_id,)).fetchone() if asunto_id else None
    if asunto_id and not asunto:
        abort(404)
    accion = datos.get("accion")
    if accion not in ("descargar", "expediente", "editor"):
        abort(400)
    if accion == "expediente" and not asunto:
        return jsonify(ok=False, error="Elige un asunto para guardarlo en su expediente."), 400

    # Lo que el abogado ajustó en pantalla manda sobre lo que hay en la base.
    valores = machotes.datos(tdb, g.tenant, g.user, asunto_id)
    for clave, valor in (datos.get("valores") or {}).items():
        if isinstance(clave, str) and isinstance(valor, str) and machotes.PATRON.fullmatch("{{" + clave + "}}"):
            valores[clave.lower()] = valor.strip()[:MAX_VALOR]

    original = boveda.leer(p["archivo_url"])
    base = os.path.splitext(p["archivo_nombre"] or p["nombre"])[0]
    sufijo = f" — {asunto['numero_expediente']}" if asunto and asunto["numero_expediente"] else ""
    nombre = f"{base}{sufijo}".replace("/", "-")[:120]
    log_activity("machote_generado", "asunto" if asunto else None, asunto_id, {"plantilla": pid, "accion": accion})

    if accion == "editor":
        contenido = machotes.resolver_html(machotes.a_html(original), valores)
        cur = tdb.execute(
            "INSERT INTO biblioteca_documentos (titulo, contenido_html, plantilla_id, asunto_id, creado_por) "
            "VALUES (?,?,?,?,?)", (nombre, contenido, pid, asunto_id, session.get("user_id")))
        tdb.execute("INSERT INTO biblioteca_doc_versiones (documento_id, contenido_html, version, creado_por) "
                    "VALUES (?,?,1,?)", (cur.lastrowid, contenido, session.get("user_id")))
        tdb.commit()
        return jsonify(ok=True, url=url_for("biblioteca.editor", doc_id=cur.lastrowid))

    lleno = machotes.llenar(original, valores)
    if accion == "descargar":
        resp = send_file(io.BytesIO(lleno), mimetype=DOCX_MIME, as_attachment=True, download_name=f"{nombre}.docx")
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    info = boveda.guardar(FileStorage(io.BytesIO(lleno), filename=f"{nombre}.docx", content_type=DOCX_MIME),
                          "asuntos", asunto_id)
    cur = tdb.execute(
        "INSERT INTO documentos (asunto_id, nombre, tipo_documento, archivo_url, archivo_nombre, tamanio, "
        "mime_type, subido_por) VALUES (?,?,?,?,?,?,?,?)",
        (asunto_id, nombre, "escrito", info["url"], info["nombre"], info["tamanio"], info["mime"],
         session.get("user_id")))
    tdb.commit()
    lector.encolar(info["url"], nombre, "documento", cur.lastrowid, asunto_id=asunto_id)
    return jsonify(ok=True, url=url_for("asuntos.detalle", id=asunto_id))
