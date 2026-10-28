"""Memoria del despacho: lectura (OCR) e índice de búsqueda de los documentos de cada despacho."""
from flask import Blueprint, current_app, g, jsonify, render_template, request

from modules.auth import login_required, tenant_required
from modules.lector import extraer, indice

lector_bp = Blueprint("lector", __name__)


def encolar(url: str, nombre: str, origen: str, origen_id=None, asunto_id=None, cliente_id=None) -> None:
    """Para los módulos: manda a leer un archivo recién guardado en la bóveda."""
    indice.encolar(current_app._get_current_object(), g.tenant_slug, g.tenant_db,
                   url, nombre, origen, origen_id, asunto_id, cliente_id)


@lector_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    estado = indice.resumen_estado(tdb)
    documentos = tdb.execute(
        """SELECT d.*, a.titulo AS asunto_titulo, a.numero_expediente
           FROM lector_documentos d LEFT JOIN asuntos a ON a.id = d.asunto_id
           ORDER BY d.fecha_alta DESC LIMIT 200"""
    ).fetchall()
    return render_template("lector/index.html", estado=estado, documentos=documentos,
                           ocr=extraer.ocr_disponible())


@lector_bp.route("/buscar")
@login_required
@tenant_required
def buscar():
    q = (request.args.get("q") or "").strip()[:300]
    asunto = request.args.get("asunto", type=int)
    resultados = indice.buscar(current_app._get_current_object(), g.tenant_slug, g.tenant_db, q,
                               limite=min(request.args.get("limite", 8, type=int), 20), asunto_id=asunto)
    for r in resultados:
        r.pop("texto", None)
    return jsonify(ok=True, resultados=resultados)


@lector_bp.route("/estado")
@login_required
@tenant_required
def estado():
    return jsonify(ok=True, **indice.resumen_estado(g.tenant_db))


@lector_bp.route("/reintentar", methods=["POST"])
@login_required
@tenant_required
def reintentar():
    estados = ("pendiente", "procesando", "requiere_ocr", "error")
    if request.args.get("todo") == "1":
        estados += ("listo", "sin_texto")
    n = indice.encolar_pendientes(current_app._get_current_object(), g.tenant_slug, g.tenant_db, reintentar=estados)
    return jsonify(ok=True, encolados=n)
