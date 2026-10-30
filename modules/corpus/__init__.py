"""Base legal de México: leyes y reglamentos federales, leyes de los estados, tesis y Diario Oficial."""
from flask import Blueprint, current_app, jsonify, render_template, request

from modules.auth import login_required, tenant_required
from modules.corpus import indice, programador
from modules.corpus.estatal import ENTIDADES

corpus_bp = Blueprint("corpus", __name__)


def _config():
    return current_app.config


@corpus_bp.route("/")
@login_required
@tenant_required
def index():
    return render_template("corpus/index.html", resumen=indice.resumen(_config()), avance=programador.estado(_config()),
                           fuentes=indice.FUENTES)


@corpus_bp.route("/buscar")
@login_required
@tenant_required
def buscar():
    q = (request.args.get("q") or "").strip()[:300]
    fuente = request.args.get("fuente") if request.args.get("fuente") in indice.FUENTES else None
    entidad = request.args.get("entidad") if request.args.get("entidad") in ENTIDADES.values() else None
    if entidad and fuente is None:
        fuente = "ley_estatal"
    resultados = indice.buscar(_config(), q, limite=10, fuente=fuente, entidad=entidad)
    for r in resultados:
        r["cita"] = indice.cita(r)
    return jsonify(ok=True, resultados=resultados)
