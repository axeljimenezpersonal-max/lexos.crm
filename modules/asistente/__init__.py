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


def _puede_ver(asunto_id) -> bool:
    from modules.asuntos import puede_ver
    return puede_ver(g.tenant_db, asunto_id)


def _autores(mensajes: list[dict]) -> dict:
    from modules.asuntos import _usuarios
    usuarios = _usuarios()
    return {str(uid): usuarios.get(uid, {}).get("nombre", "Alguien del equipo")
            for uid in {m.get("user_id") for m in mensajes} if uid}


def _asunto(asunto_id):
    if not asunto_id or not _puede_ver(asunto_id):
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
    conv = almacen.obtener(g.tenant_db, cid, _uid(), _puede_ver)
    if not conv:
        abort(404)
    mensajes = almacen.mensajes(g.tenant_db, g.tenant_slug, cid)
    return jsonify(ok=True, id=cid, asunto_id=conv["asunto_id"], privado=bool(conv["privado"]),
                   compartida=bool(conv["compartida"]), mensajes=mensajes, autores=_autores(mensajes), yo=_uid())


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
        conv = almacen.obtener(tdb, int(cid), uid, _puede_ver)
        if not conv:
            abort(404)
        asunto_id, privado = conv["asunto_id"], bool(conv["privado"])
        compartida = bool(conv["compartida"])
    else:
        asunto_id = int(datos["asunto_id"]) if str(datos.get("asunto_id") or "").isdigit() else None
        privado = bool(datos.get("privado"))
        compartida = bool(datos.get("compartida"))
    asunto = _asunto(asunto_id)
    if asunto_id and not asunto:
        abort(404)
    if asunto and asunto["privado"]:
        privado = True
    if not cid:
        cid = almacen.crear(tdb, slug, uid, pregunta, asunto_id, privado, compartida)
    historial = almacen.mensajes(tdb, slug, cid)
    almacen.agregar(tdb, slug, cid, "usuario", pregunta, user_id=uid)
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
        mid = almacen.agregar(tdb, slug, cid, "asistente", "".join(partes) or "(sin respuesta)", fuentes, modelo)
        yield sse({"tipo": "guardado", "mensaje_id": mid})

    return Response(stream_with_context(generar()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Memoria del expediente ───────────────────────────────────────────────────

def _hallazgo_json(h: dict, usuarios: dict) -> dict:
    return {**h, "tipo_label": almacen.TIPOS_HALLAZGO.get(h["tipo"], "Punto clave"),
            "autor": usuarios.get(h["autor_id"], {}).get("nombre", "Alguien del equipo"),
            "puede_borrar": h["autor_id"] == _uid() or session.get("role") in ("super_admin", "admin")}


@asistente_bp.route("/asuntos/<int:asunto_id>/hallazgos", methods=["GET", "POST"])
@login_required
@tenant_required
def hallazgos(asunto_id):
    from modules.asuntos import _usuarios
    if not _asunto(asunto_id):
        abort(404)
    tdb, slug = g.tenant_db, g.tenant_slug
    if request.method == "POST":
        datos = _ajax()
        texto = (datos.get("texto") or "").strip()
        if not texto:
            return jsonify(ok=False, error="Escribe el punto clave o el descubrimiento"), 400
        hid = almacen.guardar_hallazgo(tdb, slug, asunto_id, datos.get("tipo") or "punto_clave", texto, _uid(),
                                       datos.get("origen") or "manual")
        log_activity("hallazgo_guardado", "asunto", asunto_id, {"hallazgo": hid})
    usuarios = _usuarios()
    return jsonify(ok=True, hallazgos=[_hallazgo_json(h, usuarios) for h in almacen.hallazgos(tdb, slug, asunto_id)])


@asistente_bp.route("/hallazgos/<int:hid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def eliminar_hallazgo(hid):
    _ajax()
    tdb = g.tenant_db
    almacen.asegurar(tdb)
    fila = tdb.execute("SELECT asunto_id, autor_id FROM asunto_hallazgos WHERE id=?", (hid,)).fetchone()
    if not fila or not _asunto(fila["asunto_id"]):
        abort(404)
    if fila["autor_id"] != _uid() and session.get("role") not in ("super_admin", "admin"):
        abort(403)
    almacen.borrar_hallazgo(tdb, hid)
    log_activity("hallazgo_borrado", "asunto", fila["asunto_id"], {"hallazgo": hid})
    return jsonify(ok=True)
