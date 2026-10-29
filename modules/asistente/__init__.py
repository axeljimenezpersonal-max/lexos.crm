"""Asistente jurídico del despacho (chat)."""
import json

from flask import (Blueprint, Response, abort, current_app, g, jsonify, render_template, request, session,
                   stream_with_context)

from modules.asistente import almacen, motor
from modules.auth import log_activity, login_required, role_required, tenant_required

asistente_bp = Blueprint("asistente", __name__)
MAX_PREGUNTA = 4000


def _uid() -> int:
    return session["user_id"]


def _asunto(asunto_id):
    if not asunto_id:
        return None
    return g.tenant_db.execute(
        "SELECT id, titulo, numero_expediente, tipo_asunto, COALESCE(privado, 0) AS privado FROM asuntos WHERE id=?",
        (asunto_id,),
    ).fetchone()


def _ajax():
    if request.headers.get("X-Requested-With") != "XMLHttpRequest" or not request.is_json:
        abort(400)
    return request.get_json(silent=True) or {}


@asistente_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    asuntos = tdb.execute(
        "SELECT id, titulo, numero_expediente, COALESCE(privado, 0) AS privado FROM asuntos "
        "WHERE estado NOT IN ('archivado') ORDER BY fecha_alta DESC LIMIT 300"
    ).fetchall()
    return render_template(
        "asistente/index.html",
        conversaciones=almacen.listar(tdb, g.tenant_slug, _uid()),
        asuntos=asuntos,
        nombre=current_app.config.get("ASISTENTE_NOMBRE", "Dictum"),
        conectado=bool(current_app.config.get("ANTHROPIC_API_KEY")),
        conversacion_id=request.args.get("c", type=int),
        asunto_inicial=request.args.get("asunto", type=int),
        pregunta_inicial=(request.args.get("q") or "")[:500],
    )


@asistente_bp.route("/conversaciones/<int:cid>")
@login_required
@tenant_required
def conversacion(cid):
    conv = almacen.obtener(g.tenant_db, cid, _uid())
    if not conv:
        abort(404)
    return jsonify(ok=True, id=cid, asunto_id=conv["asunto_id"], privado=bool(conv["privado"]),
                   mensajes=almacen.mensajes(g.tenant_db, g.tenant_slug, cid))


@asistente_bp.route("/conversaciones/<int:cid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def eliminar(cid):
    _ajax()
    if not almacen.eliminar(g.tenant_db, cid, _uid()):
        abort(404)
    return jsonify(ok=True)


@asistente_bp.route("/asuntos/<int:asunto_id>/privado", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def marcar_privado(asunto_id):
    privado = bool(_ajax().get("privado"))
    cur = g.tenant_db.execute("UPDATE asuntos SET privado=? WHERE id=?", (int(privado), asunto_id))
    g.tenant_db.commit()
    if not cur.rowcount:
        abort(404)
    log_activity("asunto_privado" if privado else "asunto_no_privado", "asunto", asunto_id)
    return jsonify(ok=True, privado=privado)


@asistente_bp.route("/preguntar", methods=["POST"])
@login_required
@tenant_required
def preguntar():
    datos = _ajax()
    pregunta = (datos.get("pregunta") or "").strip()[:MAX_PREGUNTA]
    if not pregunta:
        return jsonify(ok=False, error="Escribe tu pregunta"), 400
    tdb, slug, uid = g.tenant_db, g.tenant_slug, _uid()
    almacen.asegurar(tdb)

    cid = datos.get("conversacion_id")
    if cid:
        conv = almacen.obtener(tdb, int(cid), uid)
        if not conv:
            abort(404)
        asunto_id, privado = conv["asunto_id"], bool(conv["privado"])
    else:
        asunto_id = int(datos["asunto_id"]) if str(datos.get("asunto_id") or "").isdigit() else None
        privado = bool(datos.get("privado"))
    asunto = _asunto(asunto_id)
    if asunto_id and not asunto:
        abort(404)
    if asunto and asunto["privado"]:
        privado = True
    if not cid:
        cid = almacen.crear(tdb, slug, uid, pregunta, asunto_id, privado)
    historial = almacen.mensajes(tdb, slug, cid)
    almacen.agregar(tdb, slug, cid, "usuario", pregunta)
    log_activity("consulta_asistente", "asunto" if asunto_id else None, asunto_id,
                 {"conversacion": cid, "privado": privado})

    app = current_app._get_current_object()

    def sse(evento: dict) -> str:
        return f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"

    def generar():
        yield sse({"tipo": "conversacion", "id": cid, "privado": privado})
        partes, fuentes, modelo = [], [], None
        for ev in motor.responder(app, app.config, slug, tdb, pregunta, historial,
                                  dict(asunto) if asunto else None, privado):
            if ev["tipo"] == "texto":
                partes.append(ev["texto"])
            elif ev["tipo"] == "fuentes":
                fuentes = ev["fuentes"]
            elif ev["tipo"] == "fin":
                modelo = ev.get("modelo")
            yield sse(ev)
        almacen.agregar(tdb, slug, cid, "asistente", "".join(partes) or "(sin respuesta)", fuentes, modelo)

    return Response(stream_with_context(generar()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
