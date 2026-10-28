"""
LexOS — modules/reportes.py
Reportes guardados con carpetas, 6 tipos de reporte, exportación Excel/PDF,
filtros guardados y programación de reportes.
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime

from flask import (
    Blueprint, g, jsonify, make_response, render_template, request, session,
)

from modules.auth import login_required, role_required, tenant_required

reportes_bp = Blueprint("reportes", __name__)

TIPOS_REPORTE = [
    {"id": "asuntos",    "label": "Asuntos",              "icon": "file-text"},
    {"id": "clientes",   "label": "Clientes",             "icon": "users"},
    {"id": "estado_cuenta", "label": "Estado de Cuenta",   "icon": "credit-card"},
    {"id": "facturacion","label": "Facturación",           "icon": "dollar-sign"},
    {"id": "actividad",  "label": "Actividad del Equipo",  "icon": "activity"},
    {"id": "vencimientos","label": "Vencimientos",          "icon": "alert-triangle"},
]

TIPOS_ASUNTO = [
    "civil", "penal", "mercantil", "laboral", "familiar",
    "administrativo", "fiscal", "amparo", "constitucional", "otro",
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


def _user_name(uid):
    try:
        u = g.global_db.execute(
            "SELECT nombre, apellidos FROM tenant_users WHERE id=?", (uid,)
        ).fetchone()
        return f"{u['nombre']} {u['apellidos']}" if u else "—"
    except Exception:
        return "—"


# ─────────────────────────────────────────────────────────────────────────────
#  Report data generators
# ─────────────────────────────────────────────────────────────────────────────

def _gen_asuntos(tdb, filtros):
    q = """SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id WHERE 1=1"""
    p = []
    if filtros.get("estado"):
        q += " AND a.estado=?"; p.append(filtros["estado"])
    if filtros.get("tipo"):
        q += " AND a.tipo_asunto=?"; p.append(filtros["tipo"])
    if filtros.get("abogado"):
        q += " AND a.abogado_responsable=?"; p.append(int(filtros["abogado"]))
    if filtros.get("desde"):
        q += " AND a.fecha_alta >= ?"; p.append(filtros["desde"])
    if filtros.get("hasta"):
        q += " AND a.fecha_alta <= ? || ' 23:59:59'"; p.append(filtros["hasta"])
    q += " ORDER BY a.fecha_alta DESC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    columns = ["id", "numero_expediente", "titulo", "cliente_nombre", "tipo_asunto",
               "estado", "fecha_alta", "fecha_limite"]
    headers = ["ID", "Expediente", "Título", "Cliente", "Tipo", "Estado", "Fecha Alta", "Fecha Límite"]
    return rows, columns, headers


def _gen_clientes(tdb, filtros):
    q = """SELECT c.*,
           (SELECT COUNT(*) FROM asuntos WHERE cliente_id=c.id) as total_asuntos,
           (SELECT COALESCE(SUM(COALESCE(total,monto)),0) FROM cobros WHERE cliente_id=c.id AND estado='pagado') as total_cobrado
           FROM clientes c WHERE 1=1"""
    p = []
    if filtros.get("tipo"):
        q += " AND c.tipo=?"; p.append(filtros["tipo"])
    if filtros.get("estatus"):
        q += " AND c.estatus=?"; p.append(filtros["estatus"])
    if filtros.get("desde"):
        q += " AND c.fecha_alta >= ?"; p.append(filtros["desde"])
    if filtros.get("hasta"):
        q += " AND c.fecha_alta <= ? || ' 23:59:59'"; p.append(filtros["hasta"])
    q += " ORDER BY c.fecha_alta DESC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    columns = ["id", "nombre", "apellidos", "tipo", "estatus", "email", "total_asuntos", "total_cobrado"]
    headers = ["ID", "Nombre", "Apellidos", "Tipo", "Estatus", "Email", "Asuntos", "Total Cobrado"]
    return rows, columns, headers


def _gen_estado_cuenta(tdb, filtros):
    q = """SELECT co.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
           a.titulo as asunto_titulo
           FROM cobros co
           LEFT JOIN clientes c ON co.cliente_id=c.id
           LEFT JOIN asuntos a ON co.asunto_id=a.id WHERE 1=1"""
    p = []
    if filtros.get("cliente_id"):
        q += " AND co.cliente_id=?"; p.append(int(filtros["cliente_id"]))
    if filtros.get("estado"):
        q += " AND co.estado=?"; p.append(filtros["estado"])
    if filtros.get("desde"):
        q += " AND co.fecha_emision >= ?"; p.append(filtros["desde"])
    if filtros.get("hasta"):
        q += " AND co.fecha_emision <= ?"; p.append(filtros["hasta"])
    q += " ORDER BY co.fecha_emision DESC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    columns = ["id", "folio", "cliente_nombre", "asunto_titulo", "concepto", "monto", "total",
               "estado", "fecha_emision", "fecha_vencimiento"]
    headers = ["ID", "Folio", "Cliente", "Asunto", "Concepto", "Monto", "Total",
               "Estado", "Emisión", "Vencimiento"]
    return rows, columns, headers


def _gen_facturacion(tdb, filtros):
    q = """SELECT co.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM cobros co
           LEFT JOIN clientes c ON co.cliente_id=c.id
           WHERE co.estado != 'cancelado'"""
    p = []
    if filtros.get("desde"):
        q += " AND co.fecha_emision >= ?"; p.append(filtros["desde"])
    if filtros.get("hasta"):
        q += " AND co.fecha_emision <= ?"; p.append(filtros["hasta"])
    if filtros.get("moneda"):
        q += " AND co.moneda=?"; p.append(filtros["moneda"])
    q += " ORDER BY co.fecha_emision DESC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    # Add summary
    total_fac = sum(float(r.get("total") or r.get("monto") or 0) for r in rows)
    total_cob = sum(float(r.get("total") or r.get("monto") or 0) for r in rows if r.get("estado") == "pagado")
    columns = ["id", "folio", "cliente_nombre", "concepto", "monto", "total", "estado",
               "fecha_emision"]
    headers = ["ID", "Folio", "Cliente", "Concepto", "Monto", "Total", "Estado", "Emisión"]
    return rows, columns, headers


def _gen_actividad(tdb, filtros):
    q = """SELECT al.*, a.titulo as asunto_titulo
           FROM actividad_log al
           LEFT JOIN asuntos a ON al.entidad='asunto' AND al.entidad_id=a.id
           WHERE 1=1"""
    p = []
    if filtros.get("abogado"):
        q += " AND al.user_id=?"; p.append(int(filtros["abogado"]))
    if filtros.get("desde"):
        q += " AND al.fecha >= ?"; p.append(filtros["desde"])
    if filtros.get("hasta"):
        q += " AND al.fecha <= ? || ' 23:59:59'"; p.append(filtros["hasta"])
    if filtros.get("accion"):
        q += " AND al.accion=?"; p.append(filtros["accion"])
    q += " ORDER BY al.fecha DESC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    columns = ["id", "user_id", "accion", "entidad", "entidad_id", "asunto_titulo",
               "descripcion", "fecha"]
    headers = ["ID", "Usuario", "Acción", "Entidad", "Entidad ID", "Asunto",
               "Descripción", "Fecha"]
    return rows, columns, headers


def _gen_vencimientos(tdb, filtros):
    q = """SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
           WHERE a.estado='activo' AND a.fecha_limite IS NOT NULL"""
    p = []
    rango = filtros.get("rango", "30")
    if rango == "vencidos":
        q += " AND a.fecha_limite < date('now')"
    elif rango == "7":
        q += " AND a.fecha_limite BETWEEN date('now') AND date('now', '+7 days')"
    elif rango == "30":
        q += " AND a.fecha_limite BETWEEN date('now') AND date('now', '+30 days')"
    elif rango == "90":
        q += " AND a.fecha_limite BETWEEN date('now') AND date('now', '+90 days')"
    if filtros.get("abogado"):
        q += " AND a.abogado_responsable=?"; p.append(int(filtros["abogado"]))
    q += " ORDER BY a.fecha_limite ASC LIMIT 500"
    rows = [dict(r) for r in tdb.execute(q, p).fetchall()]
    columns = ["id", "numero_expediente", "titulo", "cliente_nombre", "tipo_asunto",
               "fecha_limite", "abogado_responsable"]
    headers = ["ID", "Expediente", "Título", "Cliente", "Tipo", "Fecha Límite", "Abogado"]
    return rows, columns, headers


GENERATORS = {
    "asuntos": _gen_asuntos,
    "clientes": _gen_clientes,
    "estado_cuenta": _gen_estado_cuenta,
    "facturacion": _gen_facturacion,
    "actividad": _gen_actividad,
    "vencimientos": _gen_vencimientos,
}


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@reportes_bp.route("/")
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def index():
    tdb = g.tenant_db
    uid = session.get("user_id")

    # Folders
    carpetas = [dict(r) for r in tdb.execute(
        "SELECT * FROM reportes_carpetas ORDER BY nombre"
    ).fetchall()]

    # Saved reports
    guardados = [dict(r) for r in tdb.execute(
        "SELECT * FROM reportes_guardados ORDER BY fecha_mod DESC"
    ).fetchall()]

    abogados = _get_abogados()

    return render_template(
        "reportes/index.html",
        tipos_reporte=TIPOS_REPORTE,
        tipos_asunto=TIPOS_ASUNTO,
        carpetas=carpetas,
        guardados=guardados,
        abogados=abogados,
    )


# ── Carpetas CRUD ──

@reportes_bp.route("/carpeta/crear", methods=["POST"])
@login_required
@tenant_required
def carpeta_crear():
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400
    g.tenant_db.execute(
        "INSERT INTO reportes_carpetas (nombre, creado_por) VALUES (?,?)",
        (nombre, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


@reportes_bp.route("/carpeta/<int:cid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def carpeta_eliminar(cid):
    g.tenant_db.execute("UPDATE reportes_guardados SET carpeta_id=NULL WHERE carpeta_id=?", (cid,))
    g.tenant_db.execute("DELETE FROM reportes_carpetas WHERE id=?", (cid,))
    g.tenant_db.commit()
    return jsonify(ok=True)


# ── Guardar reporte ──

@reportes_bp.route("/guardar", methods=["POST"])
@login_required
@tenant_required
def guardar():
    data = request.get_json() or request.form
    nombre = (data.get("nombre") or "").strip()
    tipo = data.get("tipo", "")
    filtros_json = json.dumps(data.get("filtros", {}))
    carpeta_id = _safe_int(data.get("carpeta_id"))
    rid = _safe_int(data.get("id"))

    if not nombre or tipo not in GENERATORS:
        return jsonify(ok=False, error="Datos inválidos"), 400

    uid = session.get("user_id")
    tdb = g.tenant_db

    if rid:
        tdb.execute(
            "UPDATE reportes_guardados SET nombre=?, tipo=?, filtros_json=?, carpeta_id=?, fecha_mod=CURRENT_TIMESTAMP WHERE id=?",
            (nombre, tipo, filtros_json, carpeta_id, rid),
        )
    else:
        tdb.execute(
            "INSERT INTO reportes_guardados (nombre, tipo, filtros_json, carpeta_id, creado_por) VALUES (?,?,?,?,?)",
            (nombre, tipo, filtros_json, carpeta_id, uid),
        )
        rid = tdb.execute("SELECT last_insert_rowid()").fetchone()[0]
    tdb.commit()
    return jsonify(ok=True, id=rid)


@reportes_bp.route("/guardado/<int:rid>/eliminar", methods=["POST"])
@login_required
@tenant_required
def guardado_eliminar(rid):
    g.tenant_db.execute("DELETE FROM reportes_guardados WHERE id=?", (rid,))
    g.tenant_db.commit()
    return jsonify(ok=True)


# ── Generar reporte ──

@reportes_bp.route("/generar", methods=["POST"])
@login_required
@tenant_required
def generar():
    data = request.get_json() or {}
    tipo = data.get("tipo", "")
    filtros = data.get("filtros", {})

    gen = GENERATORS.get(tipo)
    if not gen:
        return jsonify(ok=False, error="Tipo inválido"), 400

    rows, columns, headers = gen(g.tenant_db, filtros)

    # Resolve user names for actividad
    if tipo == "actividad":
        for r in rows:
            r["user_id"] = _user_name(r.get("user_id"))
    if tipo == "vencimientos":
        for r in rows:
            r["abogado_responsable"] = _user_name(r.get("abogado_responsable"))

    return jsonify(ok=True, rows=rows, columns=columns, headers=headers, total=len(rows))


# ── Exportar Excel ──

@reportes_bp.route("/exportar/excel", methods=["POST"])
@login_required
@tenant_required
def exportar_excel():
    data = request.get_json() or {}
    tipo = data.get("tipo", "")
    filtros = data.get("filtros", {})
    nombre = data.get("nombre", "reporte")

    gen = GENERATORS.get(tipo)
    if not gen:
        return jsonify(ok=False, error="Tipo inválido"), 400

    rows, columns, headers = gen(g.tenant_db, filtros)

    if tipo == "actividad":
        for r in rows:
            r["user_id"] = _user_name(r.get("user_id"))
    if tipo == "vencimientos":
        for r in rows:
            r["abogado_responsable"] = _user_name(r.get("abogado_responsable"))

    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = nombre[:31]

        # Headers
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=ci, value=h)
            cell.font = openpyxl.styles.Font(bold=True)

        # Data
        for ri, row in enumerate(rows, 2):
            for ci, col in enumerate(columns, 1):
                ws.cell(row=ri, column=ci, value=row.get(col, ""))

        # Auto-width
        for col_cells in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 50)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        resp = make_response(buf.read())
        resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        resp.headers["Content-Disposition"] = f'attachment; filename="{nombre}.xlsx"'
        return resp

    except ImportError:
        # Fallback: CSV
        import csv
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{nombre}.csv"'
        return resp


# ── Exportar PDF ──

@reportes_bp.route("/exportar/pdf", methods=["POST"])
@login_required
@tenant_required
def exportar_pdf():
    data = request.get_json() or {}
    tipo = data.get("tipo", "")
    filtros = data.get("filtros", {})
    nombre = data.get("nombre", "reporte")

    gen = GENERATORS.get(tipo)
    if not gen:
        return jsonify(ok=False, error="Tipo inválido"), 400

    rows, columns, headers = gen(g.tenant_db, filtros)

    if tipo == "actividad":
        for r in rows:
            r["user_id"] = _user_name(r.get("user_id"))
    if tipo == "vencimientos":
        for r in rows:
            r["abogado_responsable"] = _user_name(r.get("abogado_responsable"))

    # Build HTML table
    despacho = g.tenant.get("nombre_despacho", "LexOS")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>{nombre}</title>
    <style>
      body {{ font-family: system-ui, sans-serif; font-size: 11px; padding: 20px; }}
      h1 {{ font-size: 16px; margin-bottom: 4px; }}
      .meta {{ color: #666; font-size: 10px; margin-bottom: 16px; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th {{ background: #AD9060; color: #080C10; text-align: left; padding: 6px 8px; font-size: 10px; font-weight: 600; }}
      td {{ padding: 5px 8px; border-bottom: 1px solid #E5E2DB; }}
      tr:nth-child(even) {{ background: #FAF8F4; }}
    </style></head><body>
    <h1>{nombre}</h1>
    <div class="meta">{despacho} · Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
    <table><thead><tr>{"".join(f'<th>{h}</th>' for h in headers)}</tr></thead><tbody>"""

    for row in rows[:200]:
        html += "<tr>" + "".join(f"<td>{row.get(c, '') or ''}</td>" for c in columns) + "</tr>"
    html += "</tbody></table></body></html>"

    # Try WeasyPrint → pdfkit → fallback HTML
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html).write_pdf()
    except ImportError:
        try:
            import pdfkit
            pdf_bytes = pdfkit.from_string(html, False)
        except (ImportError, OSError):
            resp = make_response(html)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            resp.headers["Content-Disposition"] = f'attachment; filename="{nombre}.html"'
            return resp

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="{nombre}.pdf"'
    return resp
