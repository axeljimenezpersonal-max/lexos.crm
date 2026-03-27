"""
LexOS — modules/investigacion.py
Investigación Legal simplificada: búsqueda universal interna, recursos
legales externos, colecciones/favoritos, catálogo de órganos jurisdiccionales.
"""
from __future__ import annotations

import json
from datetime import datetime

from flask import (
    Blueprint, g, jsonify, render_template, request, session,
)

from modules.auth import login_required, tenant_required

investigacion_bp = Blueprint("investigacion", __name__)

RECURSOS_EXTERNOS = [
    {
        "nombre": "SCJN — Suprema Corte de Justicia",
        "url": "https://www.scjn.gob.mx",
        "search": "https://www.google.com/search?q={q}+site:scjn.gob.mx",
        "desc": "Jurisprudencia, tesis aisladas, sentencias relevantes, semanario judicial.",
        "icon": "scale",
    },
    {
        "nombre": "CJF — Consejo de la Judicatura Federal",
        "url": "https://www.cjf.gob.mx",
        "search": "https://www.google.com/search?q={q}+site:cjf.gob.mx",
        "desc": "Consulta de expedientes, acuerdos generales, órganos jurisdiccionales.",
        "icon": "building",
    },
    {
        "nombre": "DOF — Diario Oficial de la Federación",
        "url": "https://www.dof.gob.mx",
        "search": "https://www.google.com/search?q={q}+site:dof.gob.mx",
        "desc": "Leyes, reglamentos, decretos, acuerdos y avisos oficiales.",
        "icon": "book-open",
    },
    {
        "nombre": "SAT — Servicio de Administración Tributaria",
        "url": "https://www.sat.gob.mx",
        "search": "https://www.google.com/search?q={q}+site:sat.gob.mx",
        "desc": "Normativa fiscal, criterios, resoluciones, trámites tributarios.",
        "icon": "dollar-sign",
    },
    {
        "nombre": "IMSS — Instituto Mexicano del Seguro Social",
        "url": "https://www.imss.gob.mx",
        "search": "https://www.google.com/search?q={q}+site:imss.gob.mx",
        "desc": "Normativa de seguridad social, trámites, calculadoras.",
        "icon": "shield",
    },
    {
        "nombre": "Cámara de Diputados — Leyes Federales",
        "url": "https://www.diputados.gob.mx/LeyesBiblio/index.htm",
        "search": "https://www.google.com/search?q={q}+site:diputados.gob.mx",
        "desc": "Texto vigente de todas las leyes federales de México.",
        "icon": "file-text",
    },
]

MATERIAS_ORGANO = ["Civil", "Penal", "Administrativa", "Laboral", "Mercantil", "Familiar", "Mixta"]

ORGANOS_SEED = [
    ("Juzgado Primero de Distrito en Materia Civil", "1", "Ciudad de México", "Juzgado de Distrito", "Civil"),
    ("Juzgado Segundo de Distrito en Materia Penal", "1", "Ciudad de México", "Juzgado de Distrito", "Penal"),
    ("Tribunal Colegiado en Materia Administrativa", "1", "Ciudad de México", "Tribunal Colegiado", "Administrativa"),
    ("Juzgado de Distrito en Materia Laboral", "1", "Ciudad de México", "Juzgado de Distrito", "Laboral"),
    ("Tribunal Unitario del Primer Circuito", "1", "Ciudad de México", "Tribunal Unitario", "Civil"),
    ("Juzgado Primero de Distrito en el Estado de Jalisco", "3", "Jalisco", "Juzgado de Distrito", "Mixta"),
    ("Tribunal Colegiado del Tercer Circuito", "3", "Jalisco", "Tribunal Colegiado", "Civil"),
    ("Juzgado de Distrito en Materia Administrativa en Nuevo León", "4", "Nuevo León", "Juzgado de Distrito", "Administrativa"),
    ("Tribunal Colegiado en Materia de Trabajo del Cuarto Circuito", "4", "Nuevo León", "Tribunal Colegiado", "Laboral"),
    ("Juzgado de Distrito en el Estado de Puebla", "6", "Puebla", "Juzgado de Distrito", "Mixta"),
]

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _get_abogados():
    try:
        return [dict(r) for r in g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users "
            "WHERE tenant_id=? AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()]
    except Exception:
        return []


def _seed_organos(tdb):
    """Seed default órganos if table is empty."""
    cnt = tdb.execute("SELECT COUNT(*) FROM inv_organos").fetchone()[0]
    if cnt == 0:
        for nombre, circuito, estado, tipo, materia in ORGANOS_SEED:
            tdb.execute(
                "INSERT INTO inv_organos (nombre, circuito, estado_mex, tipo_organo, materia, es_custom) "
                "VALUES (?,?,?,?,?,0)",
                (nombre, circuito, estado, tipo, materia),
            )
        tdb.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — main page
# ─────────────────────────────────────────────────────────────────────────────

@investigacion_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    uid = session.get("user_id")

    # Seed órganos on first load
    try:
        _seed_organos(tdb)
    except Exception:
        pass

    # Search query
    q = request.args.get("q", "").strip()
    tipo_filtro = request.args.get("tipo", "")
    abogado_f = _safe_int(request.args.get("abogado"))
    tipo_derecho_f = request.args.get("tipo_derecho", "")
    desde_f = request.args.get("desde", "")
    hasta_f = request.args.get("hasta", "")
    tab = request.args.get("tab", "busqueda")

    results = []
    if q:
        like = f"%{q}%"
        # Save search history
        try:
            tdb.execute(
                "INSERT INTO inv_busqueda_historial (user_id, termino) VALUES (?,?)",
                (uid, q),
            )
            tdb.commit()
        except Exception:
            pass

        # Search asuntos
        if not tipo_filtro or tipo_filtro == "asuntos":
            aq = "SELECT id, titulo, numero_expediente, tipo_asunto, estado, fecha_alta, 'asunto' as _tipo FROM asuntos WHERE (titulo LIKE ? OR numero_expediente LIKE ? OR descripcion LIKE ?)"
            ap = [like, like, like]
            if abogado_f:
                aq += " AND abogado_responsable=?"; ap.append(abogado_f)
            if tipo_derecho_f:
                aq += " AND tipo_asunto=?"; ap.append(tipo_derecho_f)
            if desde_f:
                aq += " AND fecha_alta >= ?"; ap.append(desde_f)
            if hasta_f:
                aq += " AND fecha_alta <= ? || ' 23:59:59'"; ap.append(hasta_f)
            aq += " ORDER BY fecha_alta DESC LIMIT 30"
            for r in tdb.execute(aq, ap).fetchall():
                results.append({
                    "tipo": "asunto", "id": r["id"], "titulo": r["titulo"],
                    "subtitulo": f"Exp: {r['numero_expediente'] or '—'} · {r['tipo_asunto'] or ''} · {r['estado']}",
                    "fecha": r["fecha_alta"], "url": f"/asuntos/{r['id']}",
                })

        # Search documentos (biblioteca)
        if not tipo_filtro or tipo_filtro == "documentos":
            dq = "SELECT id, titulo, estado, fecha_alta, 'documento' as _tipo FROM biblioteca_documentos WHERE (titulo LIKE ? OR contenido_html LIKE ?)"
            dp = [like, like]
            if desde_f:
                dq += " AND fecha_alta >= ?"; dp.append(desde_f)
            if hasta_f:
                dq += " AND fecha_alta <= ? || ' 23:59:59'"; dp.append(hasta_f)
            dq += " ORDER BY fecha_alta DESC LIMIT 20"
            for r in tdb.execute(dq, dp).fetchall():
                results.append({
                    "tipo": "documento", "id": r["id"], "titulo": r["titulo"],
                    "subtitulo": f"Documento · {r['estado'] or 'borrador'}",
                    "fecha": r["fecha_alta"], "url": f"/biblioteca/editor?doc_id={r['id']}",
                })

        # Search archivos (biblioteca estudio)
        if not tipo_filtro or tipo_filtro == "documentos":
            fq = "SELECT id, nombre, extension, fecha_alta FROM biblioteca_archivos WHERE (nombre LIKE ?)"
            fp = [like]
            if desde_f:
                fq += " AND fecha_alta >= ?"; fp.append(desde_f)
            if hasta_f:
                fq += " AND fecha_alta <= ? || ' 23:59:59'"; fp.append(hasta_f)
            fq += " ORDER BY fecha_alta DESC LIMIT 20"
            for r in tdb.execute(fq, fp).fetchall():
                results.append({
                    "tipo": "archivo", "id": r["id"], "titulo": r["nombre"],
                    "subtitulo": f"Archivo · {r['extension'] or ''}",
                    "fecha": r["fecha_alta"], "url": f"/biblioteca",
                })

        # Search formatos (plantillas)
        if not tipo_filtro or tipo_filtro == "formatos":
            pq = "SELECT id, nombre, categoria, tipo_derecho, fecha_alta FROM biblioteca_plantillas WHERE (nombre LIKE ? OR descripcion LIKE ?)"
            pp = [like, like]
            if tipo_derecho_f:
                pq += " AND tipo_derecho=?"; pp.append(tipo_derecho_f)
            pq += " ORDER BY fecha_alta DESC LIMIT 20"
            for r in tdb.execute(pq, pp).fetchall():
                results.append({
                    "tipo": "formato", "id": r["id"], "titulo": r["nombre"],
                    "subtitulo": f"Formato · {r['categoria'] or ''} · {r['tipo_derecho'] or ''}",
                    "fecha": r["fecha_alta"], "url": f"/biblioteca#formatos",
                })

        # Search notas de clientes
        if not tipo_filtro or tipo_filtro == "notas":
            nq = """SELECT cn.id, cn.contenido, cn.fecha, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre, cn.cliente_id
                    FROM cliente_notas cn JOIN clientes c ON cn.cliente_id=c.id
                    WHERE cn.contenido LIKE ?"""
            np_ = [like]
            if desde_f:
                nq += " AND cn.fecha >= ?"; np_.append(desde_f)
            if hasta_f:
                nq += " AND cn.fecha <= ? || ' 23:59:59'"; np_.append(hasta_f)
            nq += " ORDER BY cn.fecha DESC LIMIT 20"
            for r in tdb.execute(nq, np_).fetchall():
                results.append({
                    "tipo": "nota", "id": r["id"],
                    "titulo": r["contenido"][:80] + ("..." if len(r["contenido"]) > 80 else ""),
                    "subtitulo": f"Nota de {r['cliente_nombre']}",
                    "fecha": r["fecha"], "url": f"/clientes/{r['cliente_id']}#notas",
                })

        # Search investigaciones (legacy)
        if not tipo_filtro or tipo_filtro == "investigaciones":
            iq = "SELECT id, titulo, fuente, fecha_alta FROM investigaciones WHERE (titulo LIKE ? OR contenido LIKE ?)"
            ip = [like, like]
            iq += " ORDER BY fecha_alta DESC LIMIT 20"
            for r in tdb.execute(iq, ip).fetchall():
                results.append({
                    "tipo": "investigacion", "id": r["id"], "titulo": r["titulo"],
                    "subtitulo": f"Investigación · {r['fuente'] or ''}",
                    "fecha": r["fecha_alta"], "url": f"/investigacion/detalle/{r['id']}",
                })

        # Update result count in history
        try:
            tdb.execute(
                "UPDATE inv_busqueda_historial SET resultados=? WHERE user_id=? AND termino=? ORDER BY fecha DESC LIMIT 1",
                (len(results), uid, q),
            )
            tdb.commit()
        except Exception:
            pass

    # Colecciones
    colecciones = [dict(r) for r in tdb.execute(
        "SELECT c.*, a.titulo as asunto_titulo FROM inv_colecciones c "
        "LEFT JOIN asuntos a ON c.asunto_id=a.id "
        "WHERE c.creado_por=? ORDER BY c.fecha_alta DESC", (uid,)
    ).fetchall()]

    # Favoritos (colección especial: items sin colección formalmente, marcados)
    # For simplicity, use coleccion_id based items
    coleccion_items = {}
    for col in colecciones:
        items = [dict(r) for r in tdb.execute(
            "SELECT * FROM inv_coleccion_items WHERE coleccion_id=? ORDER BY fecha_alta DESC",
            (col["id"],),
        ).fetchall()]
        coleccion_items[col["id"]] = items

    # Historial de búsquedas
    historial = [dict(r) for r in tdb.execute(
        "SELECT * FROM inv_busqueda_historial WHERE user_id=? ORDER BY fecha DESC LIMIT 20",
        (uid,),
    ).fetchall()]

    # Órganos jurisdiccionales
    organos_materia = request.args.get("organo_materia", "")
    organos_q = "SELECT * FROM inv_organos WHERE 1=1"
    organos_p = []
    if organos_materia:
        organos_q += " AND materia=?"; organos_p.append(organos_materia)
    organos_q += " ORDER BY nombre"
    organos = [dict(r) for r in tdb.execute(organos_q, organos_p).fetchall()]

    abogados = _get_abogados()
    asuntos_list = [dict(r) for r in tdb.execute(
        "SELECT id, titulo FROM asuntos WHERE estado='activo' ORDER BY titulo"
    ).fetchall()]

    return render_template(
        "investigacion/index.html",
        q=q, tipo_filtro=tipo_filtro, abogado_f=abogado_f,
        tipo_derecho_f=tipo_derecho_f, desde_f=desde_f, hasta_f=hasta_f,
        tab=tab, results=results, total_results=len(results),
        recursos=RECURSOS_EXTERNOS,
        colecciones=colecciones, coleccion_items=coleccion_items,
        historial=historial,
        organos=organos, materias_organo=MATERIAS_ORGANO,
        organos_materia=organos_materia,
        abogados=abogados, asuntos_list=asuntos_list,
    )


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
        return redirect("/investigacion")
    return render_template("investigacion/detalle.html", investigacion=inv)
