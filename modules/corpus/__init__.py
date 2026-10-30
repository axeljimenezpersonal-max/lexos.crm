"""Base legal de México: leyes federales y Diario Oficial, actualizados a diario."""
from flask import Blueprint, current_app, jsonify, render_template, request

from modules.auth import login_required, tenant_required
from modules.corpus import indice, programador

corpus_bp = Blueprint("corpus", __name__)


def _config():
    return current_app.config


@corpus_bp.route("/")
@login_required
@tenant_required
def index():
    return render_template("corpus/index.html", resumen=indice.resumen(_config()), avance=programador.estado(_config()))


@corpus_bp.route("/buscar")
@login_required
@tenant_required
def buscar():
    q = (request.args.get("q") or "").strip()[:300]
    fuente = request.args.get("fuente") if request.args.get("fuente") in ("ley_federal", "dof") else None
    resultados = indice.buscar(_config(), q, limite=10, fuente=fuente)
    return jsonify(ok=True, resultados=resultados)
