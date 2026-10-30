"""Memoria del despacho: lectura (OCR) e índice de búsqueda de los documentos de cada despacho.

La página propia («Memoria de documentos») ya no existe: la búsqueda dentro de los documentos y el
estado de lectura viven en la Bóveda (biblioteca.index). Aquí quedan la cola, la API y los permisos.
"""
import sqlite3

from flask import Blueprint, current_app, g, jsonify, redirect, request, session, url_for

from modules.auth import login_required, tenant_required
from modules.lector import extraer, indice

lector_bp = Blueprint("lector", __name__)


def encolar(url: str, nombre: str, origen: str, origen_id=None, asunto_id=None, cliente_id=None) -> None:
    """Para los módulos: manda a leer un archivo recién guardado en la bóveda."""
    indice.encolar(current_app._get_current_object(), g.tenant_slug, g.tenant_db,
                   url, nombre, origen, origen_id, asunto_id, cliente_id)


_NOMBRE_CLIENTE = "COALESCE(NULLIF(c.razon_social, ''), TRIM(COALESCE(c.nombre,'') || ' ' || COALESCE(c.apellidos, '')))"


def documentos_visibles(tdb) -> dict[int, dict]:
    """Documentos del índice que la persona puede ver, con de dónde vienen (id de lector_documentos → datos).

    Misma regla que los expedientes (modules.asuntos.puede_ver): los abogados solo ven lo de los
    expedientes donde son responsables, colaboradores o que crearon; el resto del equipo ve todo.
    Los documentos de personas (clientes activos), del estudio sin expediente y los formatos los ve
    todo el equipo, como en sus propias páginas. Lo que ya no existe en su tabla de origen no aparece.
    """
    from modules.asuntos import _FILTRO_PROPIOS, _solo_lo_suyo
    indice.asegurar_esquema(tdb)
    alc, p = "1", []
    if _solo_lo_suyo():
        uid = session.get("user_id")
        alc, p = _FILTRO_PROPIOS, [uid, uid, uid]
    base = ("l.id, l.estado, l.error, l.paginas, l.archivo_url, a.id AS asunto_id, a.titulo AS asunto, "
            f"a.numero_expediente, c.id AS cliente_id, {_NOMBRE_CLIENTE} AS cliente")
    consultas = (
        ("documento", f"""SELECT {base}, d.nombre, d.fecha_subida AS fecha
            FROM lector_documentos l JOIN documentos d ON d.archivo_url = l.archivo_url
            JOIN asuntos a ON a.id = d.asunto_id LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {alc}""", p),
        ("sentencia", f"""SELECT {base}, s.titulo AS nombre, COALESCE(s.fecha_emision, s.fecha_alta) AS fecha
            FROM lector_documentos l JOIN asunto_sentencias s ON s.archivo_url = l.archivo_url
            JOIN asuntos a ON a.id = s.asunto_id LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {alc}""", p),
        ("cliente", f"""SELECT {base}, cd.nombre, cd.fecha_subida AS fecha
            FROM lector_documentos l JOIN cliente_documentos cd ON cd.archivo_url = l.archivo_url
            JOIN clientes c ON c.id = cd.cliente_id AND c.activo = 1
            LEFT JOIN asuntos a ON 0""", []),
        ("biblioteca", f"""SELECT {base}, COALESCE(b.nombre_original, b.nombre) AS nombre, b.fecha_alta AS fecha
            FROM lector_documentos l JOIN biblioteca_archivos b ON b.archivo_url = l.archivo_url
            LEFT JOIN asuntos a ON a.id = b.asunto_id
            LEFT JOIN clientes c ON c.id = COALESCE(b.cliente_id, a.cliente_id)
            WHERE b.asunto_id IS NULL OR (a.id IS NOT NULL AND {alc})""", p),
        ("plantilla", f"""SELECT {base}, pl.nombre, pl.fecha_alta AS fecha
            FROM lector_documentos l JOIN biblioteca_plantillas pl ON pl.archivo_url = l.archivo_url
            LEFT JOIN asuntos a ON 0 LEFT JOIN clientes c ON 0""", []),
    )
    visibles: dict[int, dict] = {}
    for origen, sql, params in consultas:
        try:
            filas = tdb.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            continue   # despacho sin esa tabla todavía
        for f in filas:
            visibles.setdefault(f["id"], dict(f, origen=origen))
    return visibles


def conteo_lectura(visibles: dict[int, dict]) -> dict:
    """Cuántos de los documentos visibles ya se leyeron, cuántos van en fila y cuántos tuvieron problema."""
    cuenta = {e: 0 for e in indice.ESTADOS}
    for d in visibles.values():
        cuenta[d["estado"]] = cuenta.get(d["estado"], 0) + 1
    return {
        "leidos": cuenta["listo"],
        "en_fila": cuenta["pendiente"] + cuenta["procesando"],
        "ocr": cuenta["requiere_ocr"],
        "problema": cuenta["error"] + cuenta["sin_texto"] + cuenta["no_soportado"],
        "reintentables": cuenta["pendiente"] + cuenta["procesando"] + cuenta["requiere_ocr"] + cuenta["error"],
    }


def puede_reintentar() -> bool:
    return session.get("role") in ("super_admin", "admin")


@lector_bp.route("/")
@login_required
@tenant_required
def index():
    """La antigua «Memoria de documentos» ahora es parte de la Bóveda."""
    return redirect(url_for("biblioteca.index"))


@lector_bp.route("/buscar")
@login_required
@tenant_required
def buscar():
    q = (request.args.get("q") or "").strip()[:300]
    asunto = request.args.get("asunto", type=int)
    visibles = documentos_visibles(g.tenant_db)
    resultados = indice.buscar(current_app._get_current_object(), g.tenant_slug, g.tenant_db, q,
                               limite=min(request.args.get("limite", 8, type=int), 20), asunto_id=asunto,
                               documentos=set(visibles))
    for r in resultados:
        r.pop("texto", None)
    return jsonify(ok=True, resultados=resultados)


@lector_bp.route("/estado")
@login_required
@tenant_required
def estado():
    return jsonify(ok=True, **indice.resumen_estado(g.tenant_db), **conteo_lectura(documentos_visibles(g.tenant_db)))


@lector_bp.route("/reintentar", methods=["POST"])
@login_required
@tenant_required
def reintentar():
    if not puede_reintentar():
        return jsonify(ok=False, error="Solo un administrador puede volver a leer los documentos."), 403
    estados = ("pendiente", "procesando", "requiere_ocr", "error")
    if request.args.get("todo") == "1":
        estados += ("listo", "sin_texto")
    n = indice.encolar_pendientes(current_app._get_current_object(), g.tenant_slug, g.tenant_db, reintentar=estados)
    return jsonify(ok=True, encolados=n)
