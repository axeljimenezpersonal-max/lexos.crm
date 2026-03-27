"""
LexOS — modules/clientes.py
CRM de clientes: listado dual, creación modal, importación masiva,
ficha detallada con 6 pestañas, exportación PDF/XLSX, salud del cliente.
"""

from __future__ import annotations

import io
import json
import math
import os
import uuid
from datetime import date, datetime

from flask import (
    Blueprint,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from modules.auth import login_required, tenant_required

clientes_bp = Blueprint("clientes", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

ESTADOS_MX = [
    "Aguascalientes", "Baja California", "Baja California Sur", "Campeche",
    "Chiapas", "Chihuahua", "Ciudad de México", "Coahuila de Zaragoza",
    "Colima", "Durango", "Estado de México", "Guanajuato", "Guerrero",
    "Hidalgo", "Jalisco", "Michoacán de Ocampo", "Morelos", "Nayarit",
    "Nuevo León", "Oaxaca", "Puebla", "Querétaro", "Quintana Roo",
    "San Luis Potosí", "Sinaloa", "Sonora", "Tabasco", "Tamaulipas",
    "Tlaxcala", "Veracruz de Ignacio de la Llave", "Yucatán", "Zacatecas",
]

ORIGENES = [
    "Referido por cliente", "Referido por colega", "Búsqueda web",
    "Redes sociales", "Publicidad digital", "Directorio legal",
    "Evento / conferencia", "Llamada en frío", "Otro",
]

TIPOS_COMUNICACION = ["llamada", "correo", "reunion", "whatsapp", "otro"]
TIPOS_NOTA = ["nota", "llamada", "reunion", "correo", "whatsapp", "otro"]

PER_PAGE = 20

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(accion: str, entidad: str = "cliente", entidad_id: int | None = None, detalles: dict | None = None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, entidad, entidad_id,
             json.dumps(detalles or {}), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _calcular_salud(cliente_id: int) -> dict:
    tdb = g.tenant_db
    activos = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE cliente_id=? AND estado='activo'", (cliente_id,)
    ).fetchone()[0]
    vencidos = tdb.execute(
        "SELECT COUNT(*) FROM cobros WHERE cliente_id=? AND estado='pendiente' AND fecha_vencimiento < date('now')",
        (cliente_id,),
    ).fetchone()[0]
    total_cobros = tdb.execute(
        "SELECT COUNT(*) FROM cobros WHERE cliente_id=?", (cliente_id,)
    ).fetchone()[0]

    if activos == 0 and total_cobros == 0:
        return {"score": 0, "label": "Sin actividad", "class": "health-none"}

    score = min(100, activos * 30 + (20 if total_cobros > 0 else 0))
    score -= vencidos * 25
    score = max(0, score)

    if score >= 70:
        label, cls = "Bueno", "health-good"
    elif score >= 40:
        label, cls = "Regular", "health-med"
    elif score > 0:
        label, cls = "Crítico", "health-bad"
    else:
        label, cls = "Inactivo", "health-none"

    return {"score": score, "label": label, "class": cls}


def _get_abogados():
    try:
        tenant = g.tenant
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users WHERE tenant_id=? AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — listado dual (tabla + tarjetas)
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    q       = request.args.get("q", "").strip()
    estatus = request.args.get("estatus", "")
    tipo    = request.args.get("tipo", "")
    abogado = request.args.get("abogado", "")
    desde   = request.args.get("desde", "")
    hasta   = request.args.get("hasta", "")
    page    = _safe_int(request.args.get("page", 1), 1)
    if page < 1:
        page = 1

    where, params = ["activo=1"], []

    if q:
        where.append("(nombre LIKE ? OR apellidos LIKE ? OR razon_social LIKE ? OR rfc LIKE ? OR email LIKE ? OR celular LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like, like, like]
    if estatus:
        where.append("estatus=?")
        params.append(estatus)
    if tipo:
        where.append("tipo=?")
        params.append(tipo)
    if abogado:
        where.append("abogado_id=?")
        params.append(abogado)
    if desde:
        where.append("date(fecha_alta) >= ?")
        params.append(desde)
    if hasta:
        where.append("date(fecha_alta) <= ?")
        params.append(hasta)

    clause = " AND ".join(where) if where else "1"
    total = tdb.execute(f"SELECT COUNT(*) FROM clientes WHERE {clause}", params).fetchone()[0]
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, total_pages)
    offset = (page - 1) * PER_PAGE

    rows = tdb.execute(
        f"SELECT * FROM clientes WHERE {clause} ORDER BY fecha_alta DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()

    clientes = []
    for r in rows:
        c = dict(r)
        c["salud"] = _calcular_salud(c["id"])
        c["asuntos_count"] = tdb.execute(
            "SELECT COUNT(*) FROM asuntos WHERE cliente_id=?", (c["id"],)
        ).fetchone()[0]
        try:
            c["etiquetas"] = json.loads(c.get("etiquetas") or "[]")
        except Exception:
            c["etiquetas"] = []
        clientes.append(c)

    abogados = _get_abogados()

    return render_template(
        "clientes/index.html",
        clientes=clientes,
        total=total,
        page=page,
        total_pages=total_pages,
        q=q,
        estatus=estatus,
        tipo=tipo,
        abogado=abogado,
        desde=desde,
        hasta=hasta,
        abogados=abogados,
        estados_mx=ESTADOS_MX,
        origenes=ORIGENES,
        per_page=PER_PAGE,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — crear cliente (modal)
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/ajax/crear", methods=["POST"])
@login_required
@tenant_required
def ajax_crear():
    tdb = g.tenant_db
    uid = session.get("user_id")
    d   = request.form

    nombre = (d.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="El nombre es requerido"), 400

    tipo = d.get("tipo", "persona_fisica")
    if tipo not in ("persona_fisica", "persona_moral"):
        tipo = "persona_fisica"

    etiquetas_raw = d.get("etiquetas", "[]")
    try:
        etiquetas = json.dumps(json.loads(etiquetas_raw))
    except Exception:
        etiquetas = "[]"

    cur = tdb.execute(
        """INSERT INTO clientes
           (tipo, nombre, apellidos, razon_social, rfc, curp, email,
            telefono, celular, direccion, num_ext, num_int, colonia, municipio,
            ciudad, estado, cp, fecha_nacimiento, giro_comercial,
            representante_legal, contacto_principal, origen_cliente,
            potencial_facturacion, abogado_id, etiquetas, estatus,
            invitar_portal, notas, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tipo,
            nombre,
            (d.get("apellidos") or "").strip() or None,
            (d.get("razon_social") or "").strip() or None,
            (d.get("rfc") or "").upper().strip() or None,
            (d.get("curp") or "").upper().strip() or None,
            (d.get("email") or "").strip() or None,
            (d.get("telefono") or "").strip() or None,
            (d.get("celular") or "").strip() or None,
            (d.get("direccion") or "").strip() or None,
            (d.get("num_ext") or "").strip() or None,
            (d.get("num_int") or "").strip() or None,
            (d.get("colonia") or "").strip() or None,
            (d.get("municipio") or "").strip() or None,
            (d.get("ciudad") or "").strip() or None,
            (d.get("estado") or "").strip() or None,
            (d.get("cp") or "").strip() or None,
            d.get("fecha_nacimiento") or None,
            (d.get("giro_comercial") or "").strip() or None,
            (d.get("representante_legal") or "").strip() or None,
            (d.get("contacto_principal") or "").strip() or None,
            (d.get("origen_cliente") or "").strip() or None,
            _safe_float(d.get("potencial_facturacion"), 0.0),
            _safe_int(d.get("abogado_id")) or None,
            etiquetas,
            d.get("estatus", "activo"),
            1 if d.get("invitar_portal") else 0,
            (d.get("notas") or "").strip() or None,
            uid,
        ),
    )
    tdb.commit()
    new_id = cur.lastrowid
    _log("crear_cliente", "cliente", new_id, {"nombre": nombre})

    return jsonify(
        ok=True,
        id=new_id,
        redirect=url_for("clientes.detalle", id=new_id),
        message=f"Cliente «{nombre}» creado exitosamente.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — abogados del tenant
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/ajax/abogados")
@login_required
@tenant_required
def ajax_abogados():
    return jsonify(_get_abogados())


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — búsqueda rápida (autocomplete para otros módulos)
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/ajax/buscar")
@login_required
@tenant_required
def ajax_buscar():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = g.tenant_db.execute(
        """SELECT id,
                  CASE tipo
                    WHEN 'persona_moral' THEN COALESCE(razon_social, nombre)
                    ELSE nombre || ' ' || COALESCE(apellidos,'')
                  END as label,
                  tipo, email, celular
           FROM clientes
           WHERE activo=1
             AND (nombre LIKE ? OR apellidos LIKE ? OR razon_social LIKE ? OR rfc LIKE ? OR email LIKE ?)
           ORDER BY nombre LIMIT 10""",
        (like, like, like, like, like),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
#  DETALLE
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>")
@login_required
@tenant_required
def detalle(id: int):
    tdb = g.tenant_db
    row = tdb.execute("SELECT * FROM clientes WHERE id=? AND activo=1", (id,)).fetchone()
    if not row:
        return redirect(url_for("clientes.index"))

    cliente = dict(row)
    try:
        cliente["etiquetas"] = json.loads(cliente.get("etiquetas") or "[]")
    except Exception:
        cliente["etiquetas"] = []

    salud = _calcular_salud(id)

    asuntos = tdb.execute(
        "SELECT * FROM asuntos WHERE cliente_id=? ORDER BY fecha_alta DESC", (id,)
    ).fetchall()

    cobros = tdb.execute(
        "SELECT * FROM cobros WHERE cliente_id=? ORDER BY fecha_emision DESC", (id,)
    ).fetchall()

    cobros_resumen = {
        "total":     sum(c["monto"] for c in cobros),
        "pagado":    sum(c["monto"] for c in cobros if c["estado"] == "pagado"),
        "pendiente": sum(c["monto"] for c in cobros if c["estado"] == "pendiente"),
        "vencido":   sum(c["monto"] for c in cobros if c["estado"] == "vencido"),
    }

    documentos = tdb.execute(
        "SELECT * FROM cliente_documentos WHERE cliente_id=? ORDER BY fecha_subida DESC", (id,)
    ).fetchall()

    comunicaciones = tdb.execute(
        "SELECT * FROM cliente_comunicaciones WHERE cliente_id=? ORDER BY fecha DESC", (id,)
    ).fetchall()

    notas = tdb.execute(
        "SELECT * FROM cliente_notas WHERE cliente_id=? ORDER BY fecha DESC", (id,)
    ).fetchall()

    portal_msgs = tdb.execute(
        "SELECT * FROM portal_mensajes WHERE cliente_id=? ORDER BY fecha DESC LIMIT 20", (id,)
    ).fetchall()

    abogados = _get_abogados()
    abogado_nombre = None
    if cliente.get("abogado_id"):
        for a in abogados:
            if a["id"] == cliente["abogado_id"]:
                abogado_nombre = f"{a['nombre']} {a['apellidos']}"
                break

    return render_template(
        "clientes/detalle.html",
        cliente=cliente,
        salud=salud,
        asuntos=asuntos,
        cobros=cobros,
        cobros_resumen=cobros_resumen,
        documentos=documentos,
        comunicaciones=comunicaciones,
        notas=notas,
        portal_msgs=portal_msgs,
        abogados=abogados,
        abogado_nombre=abogado_nombre,
        estados_mx=ESTADOS_MX,
        origenes=ORIGENES,
        tipos_comunicacion=TIPOS_COMUNICACION,
        tipos_nota=TIPOS_NOTA,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  EDICIÓN INLINE (campo a campo via AJAX)
# ─────────────────────────────────────────────────────────────────────────────

_EDITABLE_FIELDS = {
    "nombre", "apellidos", "razon_social", "rfc", "curp", "email",
    "telefono", "celular", "direccion", "num_ext", "num_int", "colonia",
    "municipio", "ciudad", "estado", "cp", "fecha_nacimiento",
    "giro_comercial", "representante_legal", "contacto_principal",
    "origen_cliente", "potencial_facturacion", "abogado_id",
    "etiquetas", "estatus", "notas", "invitar_portal",
}


@clientes_bp.route("/<int:id>/editar-campo", methods=["POST"])
@login_required
@tenant_required
def editar_campo(id: int):
    tdb = g.tenant_db
    row = tdb.execute("SELECT id FROM clientes WHERE id=? AND activo=1", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="Cliente no encontrado"), 404

    payload = request.json or {}
    campo = payload.get("campo", "")
    valor = payload.get("valor")

    if campo not in _EDITABLE_FIELDS:
        return jsonify(ok=False, error="Campo no permitido"), 400

    if campo == "etiquetas":
        if isinstance(valor, list):
            valor = json.dumps(valor)
        else:
            try:
                json.loads(valor)
            except Exception:
                valor = "[]"
    elif campo == "potencial_facturacion":
        valor = _safe_float(valor, 0.0)
    elif campo == "abogado_id":
        valor = _safe_int(valor) or None
    elif campo == "invitar_portal":
        valor = 1 if valor else 0

    tdb.execute(f"UPDATE clientes SET {campo}=? WHERE id=?", (valor, id))
    tdb.commit()
    _log("actualizar_cliente", "cliente", id, {"campo": campo})
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ELIMINAR (soft delete)
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/crear-rapido", methods=["POST"])
@login_required
@tenant_required
def crear_rapido():
    data = request.get_json()
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400

    cur = g.tenant_db.execute(
        """INSERT INTO clientes
           (nombre, tipo, telefono, email, rfc, activo)
           VALUES (?,?,?,?,?,1)""",
        (
            nombre,
            data.get("tipo", "persona_fisica"),
            (data.get("telefono") or "").strip() or None,
            (data.get("email") or "").strip() or None,
            (data.get("rfc") or "").upper().strip() or None,
        ),
    )
    g.tenant_db.commit()
    _log("crear_cliente_rapido", "cliente", cur.lastrowid, {"nombre": nombre})
    return jsonify(ok=True, id=cur.lastrowid, nombre=nombre)


@clientes_bp.route("/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
def eliminar(id: int):
    tdb = g.tenant_db
    row = tdb.execute("SELECT nombre FROM clientes WHERE id=? AND activo=1", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404
    tdb.execute("UPDATE clientes SET activo=0 WHERE id=?", (id,))
    tdb.commit()
    _log("eliminar_cliente", "cliente", id, {"nombre": row["nombre"]})
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    return redirect(url_for("clientes.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  NOTAS
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>/notas", methods=["POST"])
@login_required
@tenant_required
def agregar_nota(id: int):
    tdb = g.tenant_db
    if request.is_json:
        contenido = (request.json.get("contenido") or "").strip()
        tipo      = request.json.get("tipo", "nota")
    else:
        contenido = (request.form.get("contenido") or "").strip()
        tipo      = request.form.get("tipo", "nota")

    if not contenido:
        return jsonify(ok=False, error="Contenido requerido"), 400
    if tipo not in TIPOS_NOTA:
        tipo = "nota"

    cur = tdb.execute(
        "INSERT INTO cliente_notas (cliente_id, contenido, tipo, creado_por) VALUES (?,?,?,?)",
        (id, contenido, tipo, session.get("user_id")),
    )
    tdb.commit()
    return jsonify(ok=True, id=cur.lastrowid, fecha=datetime.now().strftime("%Y-%m-%d %H:%M"))


# ─────────────────────────────────────────────────────────────────────────────
#  COMUNICACIONES
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>/comunicaciones", methods=["POST"])
@login_required
@tenant_required
def agregar_comunicacion(id: int):
    data    = request.json or {}
    resumen = (data.get("resumen") or "").strip()
    tipo    = data.get("tipo", "llamada")

    if not resumen:
        return jsonify(ok=False, error="Resumen requerido"), 400
    if tipo not in TIPOS_COMUNICACION:
        tipo = "llamada"

    cur = g.tenant_db.execute(
        "INSERT INTO cliente_comunicaciones (cliente_id, tipo, resumen, creado_por) VALUES (?,?,?,?)",
        (id, tipo, resumen, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True, id=cur.lastrowid, fecha=datetime.now().strftime("%Y-%m-%d %H:%M"))


# ─────────────────────────────────────────────────────────────────────────────
#  DOCUMENTOS
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>/documentos", methods=["POST"])
@login_required
@tenant_required
def subir_documento(id: int):
    tdb = g.tenant_db
    nombre      = (request.form.get("nombre") or "").strip()
    descripcion = (request.form.get("descripcion") or "").strip() or None
    archivo_url = archivo_nombre = None
    tamanio = mime_type = None

    f = request.files.get("archivo")
    if f and f.filename:
        from config import Config
        upload_dir = os.path.join(Config.UPLOAD_FOLDER, "clientes", str(id))
        os.makedirs(upload_dir, exist_ok=True)
        ext = os.path.splitext(f.filename)[1].lower()
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(upload_dir, safe_name)
        f.save(dest)
        archivo_nombre = f.filename
        archivo_url    = f"/uploads/clientes/{id}/{safe_name}"
        mime_type      = f.mimetype
        tamanio        = os.path.getsize(dest)
        if not nombre:
            nombre = f.filename

    if not nombre:
        return jsonify(ok=False, error="Nombre del documento requerido"), 400

    cur = tdb.execute(
        """INSERT INTO cliente_documentos
           (cliente_id, nombre, descripcion, archivo_url, archivo_nombre, tamanio, mime_type, subido_por)
           VALUES (?,?,?,?,?,?,?,?)""",
        (id, nombre, descripcion, archivo_url, archivo_nombre, tamanio, mime_type, session.get("user_id")),
    )
    tdb.commit()
    return jsonify(ok=True, id=cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
#  FOTO
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>/foto", methods=["POST"])
@login_required
@tenant_required
def subir_foto(id: int):
    f = request.files.get("foto")
    if not f or not f.filename:
        return jsonify(ok=False, error="Archivo requerido"), 400

    from config import Config
    upload_dir = os.path.join(Config.UPLOAD_FOLDER, "clientes", "fotos")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return jsonify(ok=False, error="Formato no soportado"), 400
    safe_name = f"cli_{id}_{uuid.uuid4().hex}{ext}"
    dest = os.path.join(upload_dir, safe_name)
    f.save(dest)
    url = f"/uploads/clientes/fotos/{safe_name}"
    g.tenant_db.execute("UPDATE clientes SET foto_url=? WHERE id=?", (url, id))
    g.tenant_db.commit()
    return jsonify(ok=True, url=url)


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR XLSX
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/exportar")
@login_required
@tenant_required
def exportar():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return "openpyxl no instalado", 500

    tdb = g.tenant_db
    rows = tdb.execute("SELECT * FROM clientes WHERE activo=1 ORDER BY nombre").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Clientes"

    headers = [
        "ID", "Tipo", "Nombre", "Apellidos", "Razón Social", "RFC", "CURP",
        "Email", "Teléfono", "Celular", "Dirección", "Colonia", "Municipio",
        "Ciudad", "Estado", "CP", "Estatus", "Origen", "Potencial Facturación",
        "Etiquetas", "Fecha Alta",
    ]
    header_fill = PatternFill("solid", fgColor="6366F1")
    header_font = Font(bold=True, color="FFFFFF")

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for r in rows:
        try:
            etiq = ", ".join(json.loads(r["etiquetas"] or "[]"))
        except Exception:
            etiq = ""
        ws.append([
            r["id"], r["tipo"], r["nombre"], r["apellidos"] or "",
            r["razon_social"] or "", r["rfc"] or "", r["curp"] or "",
            r["email"] or "", r["telefono"] or "", r["celular"] or "",
            r["direccion"] or "", r["colonia"] or "", r["municipio"] or "",
            r["ciudad"] or "", r["estado"] or "", r["cp"] or "",
            r["estatus"] or "activo", r["origen_cliente"] or "",
            r["potencial_facturacion"] or 0, etiq,
            (r["fecha_alta"] or "")[:10],
        ])

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    tenant_slug = g.tenant["slug"] if g.tenant else "clientes"
    filename = f"clientes_{tenant_slug}_{date.today().isoformat()}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  PLANTILLA DE IMPORTACIÓN
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/plantilla-importar")
@login_required
@tenant_required
def plantilla_importar():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        return "openpyxl no instalado", 500

    wb = Workbook()
    ws = wb.active
    ws.title = "Importar Clientes"

    headers = [
        "tipo*", "nombre*", "apellidos", "razon_social", "rfc", "curp",
        "email", "telefono", "celular", "direccion", "colonia", "municipio",
        "ciudad", "estado", "cp", "origen_cliente", "estatus",
        "etiquetas (separadas por coma)",
    ]
    notes = [
        "persona_fisica o persona_moral", "Nombre", "Solo persona física",
        "Solo persona moral", "Ej: ABCD891231AB1", "Ej: ABCD891231HDFABC01",
        "correo@ejemplo.com", "10 dígitos", "10 dígitos", "Calle y número",
        "Colonia", "Municipio/delegación", "Ciudad", "Estado",
        "5 dígitos", "Referido, web, etc.", "activo | inactivo | prospecto",
        "tag1, tag2",
    ]

    header_fill = PatternFill("solid", fgColor="6366F1")
    note_fill   = PatternFill("solid", fgColor="374151")

    for i, (h, n) in enumerate(zip(headers, notes), 1):
        c1 = ws.cell(row=1, column=i, value=h)
        c1.fill = header_fill
        c1.font = Font(bold=True, color="FFFFFF")
        c1.alignment = Alignment(horizontal="center")
        c2 = ws.cell(row=2, column=i, value=n)
        c2.fill = note_fill
        c2.font = Font(italic=True, color="9CA3AF")

    ws.append(["persona_fisica", "Juan", "Pérez García", "", "PEGA900101AB1", "",
               "juan@mail.com", "5512345678", "5512345678", "Insurgentes 100",
               "Del Valle", "Benito Juárez", "CDMX", "Ciudad de México", "03100",
               "Referido por cliente", "activo", "corporativo, vip"])

    dv = DataValidation(type="list", formula1='"persona_fisica,persona_moral"', allow_blank=False)
    ws.add_data_validation(dv)
    dv.add("A3:A10000")

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="plantilla_importar_clientes.xlsx",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  IMPORTACIÓN MASIVA
# ─────────────────────────────────────────────────────────────────────────────

def _parse_import_xlsx(file_obj) -> tuple[list[dict], list[dict]]:
    from openpyxl import load_workbook
    wb = load_workbook(file_obj, data_only=True)
    ws = wb.active

    rows_ok, rows_err = [], []
    for row_idx, row in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        if not any(row):
            continue

        tipo   = str(row[0] or "").strip() or "persona_fisica"
        nombre = str(row[1] or "").strip()
        errors = []

        if not nombre:
            errors.append("nombre requerido")
        if tipo not in ("persona_fisica", "persona_moral"):
            tipo = "persona_fisica"

        etiq_raw  = str(row[17] or "").strip()
        etiquetas = json.dumps([t.strip() for t in etiq_raw.split(",") if t.strip()])

        record = {
            "fila":          row_idx,
            "tipo":          tipo,
            "nombre":        nombre,
            "apellidos":     str(row[2] or "").strip() or None,
            "razon_social":  str(row[3] or "").strip() or None,
            "rfc":           str(row[4] or "").upper().strip() or None,
            "curp":          str(row[5] or "").upper().strip() or None,
            "email":         str(row[6] or "").strip() or None,
            "telefono":      str(row[7] or "").strip() or None,
            "celular":       str(row[8] or "").strip() or None,
            "direccion":     str(row[9] or "").strip() or None,
            "colonia":       str(row[10] or "").strip() or None,
            "municipio":     str(row[11] or "").strip() or None,
            "ciudad":        str(row[12] or "").strip() or None,
            "estado":        str(row[13] or "").strip() or None,
            "cp":            str(row[14] or "").strip() or None,
            "origen_cliente": str(row[15] or "").strip() or None,
            "estatus":       str(row[16] or "activo").strip() or "activo",
            "etiquetas":     etiquetas,
        }

        if errors:
            record["errores"] = errors
            rows_err.append(record)
        else:
            rows_ok.append(record)

    return rows_ok, rows_err


@clientes_bp.route("/importar/preview", methods=["POST"])
@login_required
@tenant_required
def importar_preview():
    f = request.files.get("archivo")
    if not f or not f.filename:
        return jsonify(ok=False, error="Archivo requerido"), 400
    try:
        rows_ok, rows_err = _parse_import_xlsx(f)
    except Exception as e:
        return jsonify(ok=False, error=f"Error al leer el archivo: {e}"), 400

    return jsonify(ok=True, rows_ok=rows_ok, rows_err=rows_err,
                   total_ok=len(rows_ok), total_err=len(rows_err))


@clientes_bp.route("/importar/confirmar", methods=["POST"])
@login_required
@tenant_required
def importar_confirmar():
    data = request.json or {}
    rows = data.get("rows", [])
    if not rows:
        return jsonify(ok=False, error="Sin filas para importar"), 400

    tdb = g.tenant_db
    uid = session.get("user_id")
    inserted = 0

    for r in rows:
        try:
            tdb.execute(
                """INSERT INTO clientes
                   (tipo, nombre, apellidos, razon_social, rfc, curp, email,
                    telefono, celular, direccion, colonia, municipio, ciudad,
                    estado, cp, origen_cliente, estatus, etiquetas, creado_por)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get("tipo", "persona_fisica"), r["nombre"],
                    r.get("apellidos"), r.get("razon_social"), r.get("rfc"),
                    r.get("curp"), r.get("email"), r.get("telefono"), r.get("celular"),
                    r.get("direccion"), r.get("colonia"), r.get("municipio"),
                    r.get("ciudad"), r.get("estado"), r.get("cp"),
                    r.get("origen_cliente"), r.get("estatus", "activo"),
                    r.get("etiquetas", "[]"), uid,
                ),
            )
            inserted += 1
        except Exception:
            pass

    tdb.commit()
    _log("importar_clientes", "cliente", detalles={"insertados": inserted})
    return jsonify(ok=True, insertados=inserted)


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR PDF (ficha de cliente)
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:id>/pdf")
@login_required
@tenant_required
def exportar_pdf(id: int):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        return "reportlab no instalado", 500

    tdb = g.tenant_db
    row = tdb.execute("SELECT * FROM clientes WHERE id=? AND activo=1", (id,)).fetchone()
    if not row:
        return redirect(url_for("clientes.index"))

    c      = dict(row)
    tenant = g.tenant

    asuntos = tdb.execute(
        "SELECT titulo, estado, tipo_asunto, fecha_inicio FROM asuntos WHERE cliente_id=? ORDER BY fecha_alta DESC LIMIT 10",
        (id,),
    ).fetchall()
    cobros = tdb.execute(
        "SELECT concepto, monto, estado, fecha_vencimiento FROM cobros WHERE cliente_id=? ORDER BY fecha_emision DESC LIMIT 10",
        (id,),
    ).fetchall()

    salud = _calcular_salud(id)

    try:
        etiquetas = ", ".join(json.loads(c.get("etiquetas") or "[]"))
    except Exception:
        etiquetas = ""

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    ACCENT = colors.HexColor("#00DFA2")
    MUTED  = colors.HexColor("#9CA3AF")

    title_s   = ParagraphStyle("t", parent=styles["Heading1"],  fontSize=20, textColor=ACCENT, spaceAfter=4)
    sub_s     = ParagraphStyle("s", parent=styles["Normal"],    fontSize=10, textColor=MUTED, spaceAfter=12)
    section_s = ParagraphStyle("sec", parent=styles["Heading2"], fontSize=11, textColor=ACCENT, spaceBefore=12, spaceAfter=4)
    footer_s  = ParagraphStyle("f", parent=styles["Normal"],    fontSize=7, textColor=MUTED, alignment=TA_CENTER)

    nombre_display = (
        c["razon_social"] or c["nombre"]
        if c["tipo"] == "persona_moral"
        else f"{c['nombre']} {c.get('apellidos') or ''}"
    ).strip()

    story = [
        Paragraph(nombre_display, title_s),
        Paragraph(f"Ficha de cliente · {tenant['nombre_despacho'] if tenant else 'LexOS'}", sub_s),
        HRFlowable(width="100%", color=ACCENT, thickness=1),
        Spacer(1, 0.3*cm),
    ]

    def add_section(title):
        story.append(Paragraph(title, section_s))

    def kv_table(data_rows):
        t = Table(data_rows, colWidths=[5*cm, 12*cm])
        t.setStyle(TableStyle([
            ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE",      (0, 0), (-1, -1), 9),
            ("TEXTCOLOR",     (0, 0), (0, -1), MUTED),
            ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(t)

    add_section("Información general")
    info = [
        ["Tipo",    c["tipo"].replace("_", " ").title()],
        ["RFC",     c.get("rfc") or "—"],
        ["Email",   c.get("email") or "—"],
        ["Celular", c.get("celular") or "—"],
        ["Teléfono", c.get("telefono") or "—"],
        ["Estado",  (c.get("estatus") or "activo").title()],
        ["Salud",   f"{salud['label']} ({salud['score']}%)"],
        ["Origen",  c.get("origen_cliente") or "—"],
        ["Potencial facturación", f"${(c.get('potencial_facturacion') or 0):,.0f} MXN"],
    ]
    if etiquetas:
        info.append(["Etiquetas", etiquetas])
    kv_table(info)

    if c.get("direccion"):
        add_section("Dirección")
        dir_parts = [c.get("direccion", ""), c.get("colonia", ""), c.get("municipio", ""),
                     c.get("ciudad", ""), c.get("estado", ""), c.get("cp", "")]
        kv_table([["Dirección", ", ".join(p for p in dir_parts if p)]])

    if asuntos:
        add_section("Asuntos")
        ah = [["Título", "Tipo", "Estado", "Inicio"]]
        for a in asuntos:
            ah.append([a["titulo"][:50], a["tipo_asunto"] or "", a["estado"] or "", (a["fecha_inicio"] or "")[:10]])
        t = Table(ah, colWidths=[9*cm, 3*cm, 2.5*cm, 2.5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)

    if cobros:
        add_section("Cobros")
        ch = [["Concepto", "Monto", "Estado", "Vencimiento"]]
        for co in cobros:
            ch.append([co["concepto"][:40], f"${co['monto']:,.0f}", co["estado"] or "", (co["fecha_vencimiento"] or "")[:10]])
        t = Table(ch, colWidths=[8*cm, 3*cm, 2.5*cm, 3.5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)

    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"Generado el {date.today().strftime('%d/%m/%Y')} · LexOS", footer_s))

    doc.build(story)
    buf.seek(0)
    safe = nombre_display.replace(" ", "_")[:30]
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"cliente_{safe}.pdf")


# ─────────────────────────────────────────────────────────────────────────────
#  Redirect legacy
# ─────────────────────────────────────────────────────────────────────────────

@clientes_bp.route("/nuevo")
@login_required
@tenant_required
def nuevo():
    return redirect(url_for("clientes.index", _anchor="nuevo"))
