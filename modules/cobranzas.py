"""
LexOS — modules/cobranzas.py
Módulo de cobranzas/facturación: listado con tabs, dashboard, modal de creación,
registro de pagos parciales, estado de cuenta PDF, exportación Excel.
"""
from __future__ import annotations

import io
import json
import math
import os
import uuid
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from modules.auth import login_required, role_required, tenant_required

cobranzas_bp = Blueprint("cobranzas", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

MONEDAS = {
    "MXN": {"simbolo": "$",    "nombre": "Peso mexicano"},
    "USD": {"simbolo": "USD ", "nombre": "Dólar americano"},
    "EUR": {"simbolo": "€",    "nombre": "Euro"},
}

METODOS_PAGO = ["Efectivo", "Transferencia", "Tarjeta", "Cheque", "SPEI", "Otro"]

ESTADOS_COLOR = {
    "pendiente": ("cbr-amarillo", "Pendiente"),
    "abono":     ("cbr-azul",     "Abono"),
    "pagado":    ("cbr-verde",    "Pagado"),
    "vencido":   ("cbr-rojo",     "Vencido"),
    "cancelado": ("cbr-gris",     "Cancelado"),
}

MESES_ES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]

PER_PAGE = 20

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(accion, entidad_id=None, detalles=None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, "cobro", entidad_id,
             json.dumps(detalles or {}), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _auto_folio(tdb) -> str:
    year  = date.today().year
    count = tdb.execute(
        "SELECT COUNT(*) FROM cobros WHERE strftime('%Y', fecha_alta)=?", (str(year),)
    ).fetchone()[0] + 1
    return f"FAC-{year}-{count:04d}"


def _get_abogados():
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users "
            "WHERE tenant_id=? AND rol IN ('abogado','admin','super_admin') AND activo=1 "
            "ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _calcular_total(monto: float, iva_pct: float, descuento_tipo: str, descuento_valor: float) -> dict:
    iva_monto   = round(monto * iva_pct / 100, 2)
    subtotal    = monto + iva_monto
    if descuento_tipo == "pct":
        descuento_monto = round(subtotal * descuento_valor / 100, 2)
    else:
        descuento_monto = descuento_valor
    total = round(subtotal - descuento_monto, 2)
    return {"iva_monto": iva_monto, "descuento_monto": descuento_monto, "total": total}


def _check_vencidos(tdb):
    """Marca cobros pendientes con fecha vencida como 'vencido'."""
    try:
        tdb.execute(
            "UPDATE cobros SET estado='vencido' "
            "WHERE estado='pendiente' AND fecha_vencimiento IS NOT NULL "
            "AND fecha_vencimiento < date('now')"
        )
        tdb.commit()
    except Exception:
        pass


def _enrich(row: dict) -> dict:
    row["estado_info"] = ESTADOS_COLOR.get(row.get("estado", "pendiente"), ("cbr-gris", row.get("estado","")))
    return row


def _dashboard_stats(tdb) -> dict:
    r = tdb.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN estado IN ('pendiente','abono') THEN COALESCE(total,monto) ELSE 0 END),0) as por_cobrar,
          COALESCE(SUM(CASE WHEN estado='pagado'    THEN COALESCE(total,monto) ELSE 0 END),0) as cobrado,
          COALESCE(SUM(CASE WHEN estado='vencido'   THEN COALESCE(total,monto) ELSE 0 END),0) as vencido,
          COALESCE(SUM(COALESCE(total,monto)),0) as total_facturado
        FROM cobros WHERE estado != 'cancelado'
    """).fetchone()
    stats = dict(r)

    # Por vencer esta semana
    today = date.today().isoformat()
    fin_semana = (date.today() + timedelta(days=7)).isoformat()
    pv = tdb.execute("""
        SELECT COUNT(*) as cnt,
               COALESCE(SUM(COALESCE(total,monto)),0) as monto
        FROM cobros
        WHERE estado IN ('pendiente','abono')
          AND fecha_vencimiento IS NOT NULL
          AND fecha_vencimiento >= ? AND fecha_vencimiento <= ?
    """, (today, fin_semana)).fetchone()
    stats["por_vencer_semana_cnt"] = pv["cnt"]
    stats["por_vencer_semana_monto"] = float(pv["monto"])

    # Proyección 30 días
    fin_30 = (date.today() + timedelta(days=30)).isoformat()
    proy = tdb.execute("""
        SELECT COALESCE(SUM(COALESCE(total,monto)),0) as monto
        FROM cobros
        WHERE estado IN ('pendiente','abono','vencido')
          AND (fecha_vencimiento IS NULL OR fecha_vencimiento <= ?)
    """, (fin_30,)).fetchone()
    stats["proyeccion_30d"] = float(proy["monto"])

    # Tiempo promedio de pago (días entre emisión y pago, solo pagadas)
    avg_row = tdb.execute("""
        SELECT AVG(CAST(julianday(fecha_pago) - julianday(fecha_emision) AS REAL)) as avg_days
        FROM cobros
        WHERE estado='pagado' AND fecha_pago IS NOT NULL AND fecha_emision IS NOT NULL
    """).fetchone()
    stats["avg_dias_pago"] = round(float(avg_row["avg_days"] or 0))

    # Cliente que más debe
    top_cliente = tdb.execute("""
        SELECT c.id, c.nombre, c.apellidos, c.razon_social, c.tipo,
               COALESCE(SUM(COALESCE(co.total,co.monto) - COALESCE(co.monto_abonado,0)),0) as deuda
        FROM cobros co
        JOIN clientes c ON co.cliente_id=c.id
        WHERE co.estado IN ('pendiente','abono','vencido')
        GROUP BY co.cliente_id
        ORDER BY deuda DESC LIMIT 1
    """).fetchone()
    if top_cliente and float(top_cliente["deuda"]) > 0:
        nombre = top_cliente["razon_social"] if top_cliente["tipo"] == "moral" else \
                 f"{top_cliente['nombre'] or ''} {top_cliente['apellidos'] or ''}".strip()
        stats["top_deudor_nombre"] = nombre
        stats["top_deudor_monto"] = float(top_cliente["deuda"])
        stats["top_deudor_id"] = top_cliente["id"]
    else:
        stats["top_deudor_nombre"] = None
        stats["top_deudor_monto"] = 0
        stats["top_deudor_id"] = None

    # Próximo vencimiento
    prox = tdb.execute("""
        SELECT id, numero_factura, fecha_vencimiento,
               CAST(julianday(fecha_vencimiento) - julianday('now') AS INTEGER) as dias
        FROM cobros
        WHERE estado IN ('pendiente','abono')
          AND fecha_vencimiento IS NOT NULL AND fecha_vencimiento >= ?
        ORDER BY fecha_vencimiento ASC LIMIT 1
    """, (today,)).fetchone()
    if prox:
        stats["prox_venc_folio"] = prox["numero_factura"] or f"#{prox['id']}"
        stats["prox_venc_dias"] = prox["dias"]
        stats["prox_venc_id"] = prox["id"]
    else:
        stats["prox_venc_folio"] = None
        stats["prox_venc_dias"] = None
        stats["prox_venc_id"] = None

    return stats


def _monthly_chart(tdb) -> list[dict]:
    """Returns last 6 months of billed vs collected."""
    today = date.today()
    result = []
    for i in range(5, -1, -1):
        d  = today.replace(day=1) - timedelta(days=1)
        # go back i months from current month start
        m  = (today.month - i - 1) % 12 + 1
        y  = today.year - ((today.month - i - 1) // 12)
        label = f"{MESES_ES[m-1]} {str(y)[2:]}"
        ym = f"{y:04d}-{m:02d}"
        row = tdb.execute("""
            SELECT
              COALESCE(SUM(COALESCE(total,monto)),0) as facturado,
              COALESCE(SUM(CASE WHEN estado='pagado' THEN COALESCE(total,monto) ELSE 0 END),0) as cobrado
            FROM cobros
            WHERE strftime('%Y-%m', fecha_emision) = ?
              AND estado != 'cancelado'
        """, (ym,)).fetchone()
        result.append({
            "label":      label,
            "facturado":  float(row["facturado"]),
            "cobrado":    float(row["cobrado"]),
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    _check_vencidos(tdb)

    # Moneda global
    if "moneda" in request.args:
        session["moneda_global"] = request.args["moneda"]
    moneda_global = session.get("moneda_global", "MXN")

    # Filtros
    tab        = request.args.get("tab", "pendientes")  # pendientes|pagadas|todas
    q          = request.args.get("q", "").strip()
    cliente_f  = request.args.get("cliente", "")
    asunto_f   = request.args.get("asunto", "")
    desde      = request.args.get("desde", "")
    hasta      = request.args.get("hasta", "")
    monto_min  = request.args.get("monto_min", "")
    monto_max  = request.args.get("monto_max", "")
    estado_f   = request.args.get("estado", "")
    moneda_f   = request.args.get("moneda_f", "")
    abogado_f  = request.args.get("abogado", "")
    page       = max(1, _safe_int(request.args.get("page", 1), 1))

    where, params = ["1"], []

    # Tab filters
    if tab == "pendientes":
        where.append("co.estado IN ('pendiente','abono','vencido')")
    elif tab == "pagadas":
        where.append("co.estado = 'pagado'")
    elif tab == "vencidas":
        where.append("co.estado = 'vencido'")
    # 'todas' → no extra filter

    if q:
        where.append("(c.nombre LIKE ? OR c.razon_social LIKE ? OR co.concepto LIKE ? OR co.numero_factura LIKE ?)")
        lk = f"%{q}%"
        params += [lk, lk, lk, lk]
    if cliente_f:
        where.append("co.cliente_id=?")
        params.append(cliente_f)
    if asunto_f:
        where.append("co.asunto_id=?")
        params.append(asunto_f)
    if desde:
        where.append("date(co.fecha_emision) >= ?")
        params.append(desde)
    if hasta:
        where.append("date(co.fecha_emision) <= ?")
        params.append(hasta)
    if monto_min:
        where.append("COALESCE(co.total, co.monto) >= ?")
        params.append(_safe_float(monto_min))
    if monto_max:
        where.append("COALESCE(co.total, co.monto) <= ?")
        params.append(_safe_float(monto_max))
    if estado_f:
        where.append("co.estado=?")
        params.append(estado_f)
    if moneda_f:
        where.append("co.moneda=?")
        params.append(moneda_f)
    if abogado_f:
        where.append("co.abogado_id=?")
        params.append(abogado_f)

    clause = " AND ".join(where)
    base_q = f"""
        SELECT co.*,
               c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
               c.razon_social as cliente_razon, c.email as cliente_email,
               a.titulo as asunto_titulo, a.numero_expediente
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        LEFT JOIN asuntos a  ON co.asunto_id=a.id
        WHERE {clause}
    """
    total_count = tdb.execute(f"SELECT COUNT(*) FROM ({base_q})", params).fetchone()[0]
    total_pages = max(1, math.ceil(total_count / PER_PAGE))
    page = min(page, total_pages)
    offset = (page - 1) * PER_PAGE

    rows = tdb.execute(
        f"{base_q} ORDER BY co.fecha_alta DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()
    cobros = [_enrich(dict(r)) for r in rows]

    # Tab counts
    tab_counts = {}
    for t_key, t_clause in [("pendientes","co.estado IN ('pendiente','abono','vencido')"),
                              ("pagadas","co.estado='pagado'"),
                              ("vencidas","co.estado='vencido'"),
                              ("todas","1")]:
        tab_counts[t_key] = tdb.execute(
            f"SELECT COUNT(*) FROM cobros co LEFT JOIN clientes c ON co.cliente_id=c.id WHERE {t_clause}"
        ).fetchone()[0]

    stats    = _dashboard_stats(tdb)
    monthly  = _monthly_chart(tdb)
    abogados = _get_abogados()

    clientes = [dict(r) for r in tdb.execute(
        "SELECT id, tipo, nombre, apellidos, razon_social FROM clientes WHERE activo=1 ORDER BY nombre LIMIT 300"
    ).fetchall()]

    asuntos_list = [dict(r) for r in tdb.execute(
        "SELECT id, titulo, numero_expediente FROM asuntos WHERE estado NOT IN ('archivado','cerrado') ORDER BY titulo LIMIT 300"
    ).fetchall()]

    return render_template(
        "cobranzas/index.html",
        cobros=cobros,
        tab=tab, tab_counts=tab_counts,
        total_count=total_count, page=page, total_pages=total_pages, per_page=PER_PAGE,
        q=q, cliente_f=cliente_f, asunto_f=asunto_f,
        desde=desde, hasta=hasta, monto_min=monto_min, monto_max=monto_max,
        estado_f=estado_f, moneda_f=moneda_f, abogado_f=abogado_f,
        stats=stats, monthly=monthly,
        moneda_global=moneda_global, monedas=MONEDAS,
        metodos_pago=METODOS_PAGO, estados_color=ESTADOS_COLOR,
        abogados=abogados, clientes=clientes,
        asuntos_list=asuntos_list,
        today_str=date.today().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — crear cobro (modal)
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/ajax/crear", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def ajax_crear():
    tdb     = g.tenant_db
    data    = request.form

    concepto = (data.get("concepto") or "").strip()
    if not concepto:
        return jsonify(ok=False, error="La descripción es requerida"), 400

    cliente_id = _safe_int(data.get("cliente_id"))
    if not cliente_id:
        return jsonify(ok=False, error="El cliente es requerido"), 400

    monto          = _safe_float(data.get("monto"))
    iva_pct        = _safe_float(data.get("iva_pct"), 16.0)
    descuento_tipo = data.get("descuento_tipo", "fijo")
    descuento_val  = _safe_float(data.get("descuento_valor"))
    calc           = _calcular_total(monto, iva_pct, descuento_tipo, descuento_val)

    numero_factura = (data.get("numero_factura") or "").strip() or _auto_folio(tdb)
    estado         = data.get("estado", "pendiente")
    if estado not in ESTADOS_COLOR:
        estado = "pendiente"

    monto_abonado = _safe_float(data.get("monto_abonado")) if estado == "abono" else 0.0
    fecha_abono   = data.get("fecha_abono") or None if estado == "abono" else None

    # File uploads
    archivo_factura_url = archivo_xml_url = None
    for field, subdir in [("archivo_factura", "facturas"), ("archivo_xml", "xml")]:
        f = request.files.get(field)
        if f and f.filename:
            try:
                from config import Config
                udir = os.path.join(Config.UPLOAD_FOLDER, "cobranzas", subdir)
                os.makedirs(udir, exist_ok=True)
                ext  = os.path.splitext(f.filename)[1].lower()
                safe = f"{uuid.uuid4().hex}{ext}"
                f.save(os.path.join(udir, safe))
                url  = f"/uploads/cobranzas/{subdir}/{safe}"
                if field == "archivo_factura":
                    archivo_factura_url = url
                else:
                    archivo_xml_url = url
            except Exception:
                pass

    cur = tdb.execute(
        """INSERT INTO cobros
           (cliente_id, asunto_id, concepto, numero_factura,
            monto, moneda, iva_pct, iva_monto, total,
            descuento_tipo, descuento_valor,
            estado, fecha_emision, fecha_vencimiento, fecha_pago,
            monto_abonado, fecha_abono,
            abogado_id, archivo_factura_url, archivo_xml_url,
            enviar_correo, notas, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cliente_id,
            _safe_int(data.get("asunto_id")),
            concepto, numero_factura,
            monto,
            data.get("moneda", "MXN"),
            iva_pct, calc["iva_monto"], calc["total"],
            descuento_tipo, descuento_val,
            estado,
            data.get("fecha_emision") or date.today().isoformat(),
            data.get("fecha_vencimiento") or None,
            data.get("fecha_pago") or None,
            monto_abonado, fecha_abono,
            _safe_int(data.get("abogado_id")),
            archivo_factura_url, archivo_xml_url,
            1 if data.get("enviar_correo") else 0,
            (data.get("notas") or "").strip() or None,
            session.get("user_id"),
        ),
    )
    tdb.commit()
    new_id = cur.lastrowid
    _log("crear_cobro", new_id, {"concepto": concepto, "monto": monto})
    return jsonify(ok=True, id=new_id, folio=numero_factura)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — asuntos de un cliente
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/ajax/asuntos-cliente/<int:cid>")
@login_required
@tenant_required
def ajax_asuntos_cliente(cid: int):
    rows = g.tenant_db.execute(
        "SELECT id, titulo, numero_expediente FROM asuntos WHERE cliente_id=? AND estado NOT IN ('archivado') ORDER BY fecha_alta DESC",
        (cid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — auto folio
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/ajax/auto-folio")
@login_required
@tenant_required
def ajax_auto_folio():
    return jsonify({"folio": _auto_folio(g.tenant_db)})


# ─────────────────────────────────────────────────────────────────────────────
#  REGISTRO DE PAGO
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/<int:id>/pago", methods=["POST"])
@login_required
@tenant_required
def registrar_pago(id: int):
    tdb  = g.tenant_db
    cobro = tdb.execute("SELECT * FROM cobros WHERE id=?", (id,)).fetchone()
    if not cobro:
        return jsonify(ok=False, error="No encontrado"), 404

    data     = request.form
    monto_p  = _safe_float(data.get("monto_pagado"))
    if not monto_p or monto_p <= 0:
        return jsonify(ok=False, error="Monto inválido"), 400

    fecha  = data.get("fecha_pago") or date.today().isoformat()
    metodo = data.get("metodo", "Transferencia")
    ref    = (data.get("referencia") or "").strip() or None
    notas  = (data.get("notas") or "").strip() or None

    # Comprobante upload
    comprobante_url = None
    f = request.files.get("comprobante")
    if f and f.filename:
        try:
            from config import Config
            udir = os.path.join(Config.UPLOAD_FOLDER, "cobranzas", "comprobantes")
            os.makedirs(udir, exist_ok=True)
            ext  = os.path.splitext(f.filename)[1].lower()
            safe = f"{uuid.uuid4().hex}{ext}"
            f.save(os.path.join(udir, safe))
            comprobante_url = f"/uploads/cobranzas/comprobantes/{safe}"
        except Exception:
            pass

    # Insertar en historial de pagos
    tdb.execute(
        """INSERT INTO cobro_pagos
           (cobro_id, monto, fecha, metodo, referencia, comprobante_url, notas, creado_por)
           VALUES (?,?,?,?,?,?,?,?)""",
        (id, monto_p, fecha, metodo, ref, comprobante_url, notas, session.get("user_id")),
    )

    # Calcular total abonado
    total_abonado = tdb.execute(
        "SELECT COALESCE(SUM(monto),0) FROM cobro_pagos WHERE cobro_id=?", (id,)
    ).fetchone()[0]
    total_cobro = float(cobro["total"] or cobro["monto"] or 0)

    # Determinar nuevo estado
    if total_abonado >= total_cobro - 0.01:
        nuevo_estado   = "pagado"
        fecha_pago_col = fecha
    elif total_abonado > 0:
        nuevo_estado   = "abono"
        fecha_pago_col = None
    else:
        nuevo_estado   = cobro["estado"]
        fecha_pago_col = None

    tdb.execute(
        "UPDATE cobros SET estado=?, monto_abonado=?, fecha_pago=? WHERE id=?",
        (nuevo_estado, total_abonado, fecha_pago_col, id),
    )
    tdb.commit()
    _log("registrar_pago", id, {"monto": monto_p, "estado": nuevo_estado})
    return jsonify(ok=True, nuevo_estado=nuevo_estado, total_abonado=total_abonado)


# ─────────────────────────────────────────────────────────────────────────────
#  NOTA DE CRÉDITO (cancelar/parcial)
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/<int:id>/nota-credito", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def nota_credito(id: int):
    tdb  = g.tenant_db
    data = request.json or {}
    tipo = data.get("tipo", "total")  # total | parcial
    monto_nc = _safe_float(data.get("monto"))
    motivo   = (data.get("motivo") or "Nota de crédito").strip()

    cobro = tdb.execute("SELECT * FROM cobros WHERE id=?", (id,)).fetchone()
    if not cobro:
        return jsonify(ok=False, error="No encontrado"), 404

    if tipo == "total":
        tdb.execute("UPDATE cobros SET estado='cancelado' WHERE id=?", (id,))
        tdb.commit()
        _log("nota_credito_total", id, {"motivo": motivo})
        return jsonify(ok=True, nuevo_estado="cancelado")
    else:
        if not monto_nc or monto_nc <= 0:
            return jsonify(ok=False, error="Monto inválido"), 400
        total_actual = float(cobro["total"] or cobro["monto"] or 0)
        nuevo_total  = round(total_actual - monto_nc, 2)
        tdb.execute(
            "UPDATE cobros SET total=?, notas=COALESCE(notas,'') || '\nNC: ' || ? WHERE id=?",
            (nuevo_total, f"{motivo} -${monto_nc:,.2f}", id),
        )
        tdb.commit()
        _log("nota_credito_parcial", id, {"monto": monto_nc, "motivo": motivo})
        return jsonify(ok=True, nuevo_total=nuevo_total)


# ─────────────────────────────────────────────────────────────────────────────
#  ACTUALIZAR NOTAS
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/<int:id>/notas", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def actualizar_notas(id: int):
    tdb  = g.tenant_db
    data = request.json or {}
    notas = (data.get("notas") or "").strip()
    cobro = tdb.execute("SELECT id FROM cobros WHERE id=?", (id,)).fetchone()
    if not cobro:
        return jsonify(ok=False, error="No encontrado"), 404
    tdb.execute("UPDATE cobros SET notas=? WHERE id=?", (notas or None, id))
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  CANCELAR COBRO
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/<int:id>/cancelar", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def cancelar(id: int):
    g.tenant_db.execute("UPDATE cobros SET estado='cancelado' WHERE id=?", (id,))
    g.tenant_db.commit()
    _log("cancelar_cobro", id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    return redirect(url_for("cobranzas.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  HISTORIAL DE PAGOS (AJAX)
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/<int:id>/pagos")
@login_required
@tenant_required
def historial_pagos(id: int):
    rows = g.tenant_db.execute(
        "SELECT * FROM cobro_pagos WHERE cobro_id=? ORDER BY fecha DESC", (id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR EXCEL
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/exportar-excel")
@login_required
@tenant_required
def exportar_excel():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return "openpyxl no instalado", 500

    tdb = g.tenant_db
    rows = tdb.execute("""
        SELECT co.numero_factura, c.nombre||' '||COALESCE(c.apellidos,'') as cliente,
               c.razon_social, a.titulo as asunto,
               co.concepto, co.fecha_emision, co.fecha_vencimiento,
               co.moneda, co.monto, co.iva_pct, co.iva_monto, co.total,
               co.monto_abonado, co.estado, co.metodo_pago
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        LEFT JOIN asuntos  a ON co.asunto_id=a.id
        ORDER BY co.fecha_alta DESC LIMIT 5000
    """).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cobranzas"

    HEADERS = ["Folio","Cliente","Razón Social","Asunto","Concepto",
                "Fecha emisión","Fecha vencimiento","Moneda","Monto base",
                "IVA %","IVA","Total","Abonado","Estado","Método pago"]
    header_fill = PatternFill("solid", fgColor="6366F1")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    for col, h in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill   = header_fill
        cell.font   = header_font
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = max(12, len(h) + 2)

    for r, row in enumerate(rows, 2):
        ws.append(list(row))

    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"cobranzas_{date.today()}.xlsx")


# ─────────────────────────────────────────────────────────────────────────────
#  ESTADO DE CUENTA PDF (por cliente)
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/estado-cuenta/<int:cliente_id>")
@login_required
@tenant_required
def estado_cuenta_pdf(cliente_id: int):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    except ImportError:
        return "reportlab no instalado", 500

    tdb    = g.tenant_db
    cliente = tdb.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()
    if not cliente:
        return redirect(url_for("cobranzas.index"))

    cobros = tdb.execute("""
        SELECT co.*, a.titulo as asunto_titulo, a.numero_expediente
        FROM cobros co LEFT JOIN asuntos a ON co.asunto_id=a.id
        WHERE co.cliente_id=? ORDER BY co.fecha_emision DESC
    """, (cliente_id,)).fetchall()

    total_facturado = sum(float(c["total"] or c["monto"] or 0) for c in cobros if c["estado"] != "cancelado")
    total_cobrado   = sum(float(c["monto_abonado"] or 0) for c in cobros)
    saldo_pendiente = total_facturado - total_cobrado

    cliente_d = dict(cliente)
    nombre = (cliente_d.get("razon_social") or
              f"{cliente_d.get('nombre','')} {cliente_d.get('apellidos','')}").strip()
    tenant = g.tenant

    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                              topMargin=2*cm, bottomMargin=2*cm)
    st   = getSampleStyleSheet()
    ACC  = colors.HexColor("#00DFA2")
    MUTED= colors.HexColor("#9CA3AF")

    title_s   = ParagraphStyle("t",  parent=st["Heading1"], fontSize=16, textColor=ACC, spaceAfter=4)
    sub_s     = ParagraphStyle("s",  parent=st["Normal"],   fontSize=9,  textColor=MUTED, spaceAfter=8)
    sec_s     = ParagraphStyle("sc", parent=st["Heading2"], fontSize=10, textColor=ACC, spaceBefore=12, spaceAfter=4)
    body_s    = ParagraphStyle("b",  parent=st["Normal"],   fontSize=8,  leading=12)
    footer_s  = ParagraphStyle("f",  parent=st["Normal"],   fontSize=7,  textColor=MUTED, alignment=TA_CENTER)
    total_s   = ParagraphStyle("tt", parent=st["Normal"],   fontSize=10, textColor=ACC, alignment=TA_RIGHT, spaceAfter=4)

    story = [
        Paragraph(f"Estado de Cuenta", title_s),
        Paragraph(f"{nombre} · {date.today().strftime('%d/%m/%Y')} · {tenant['nombre_despacho'] if tenant else 'LexOS'}", sub_s),
        HRFlowable(width="100%", color=ACC, thickness=1),
        Spacer(1, 0.3*cm),
    ]

    # Summary
    summary_data = [
        ["Total facturado", f"${total_facturado:,.2f}"],
        ["Total cobrado",   f"${total_cobrado:,.2f}"],
        ["Saldo pendiente", f"${saldo_pendiente:,.2f}"],
    ]
    st_tbl = Table(summary_data, colWidths=[5*cm, 5*cm])
    st_tbl.setStyle(TableStyle([
        ("FONTSIZE",(0,0),(-1,-1),9),
        ("TEXTCOLOR",(0,0),(0,-1),MUTED),
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("TEXTCOLOR",(1,-1),(1,-1),ACC),
        ("FONTNAME",(1,-1),(1,-1),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),.25,colors.HexColor("#E5E7EB")),
        ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white, colors.HexColor("#F9FAFB")]),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
    ]))
    story.append(st_tbl)
    story.append(Spacer(1, 0.4*cm))

    # Cobros table
    story.append(Paragraph("Detalle de facturas", sec_s))
    rows_data = [["Folio","Concepto","Fecha","Vencimiento","Total","Abonado","Estado"]]
    for c in cobros:
        rows_data.append([
            c["numero_factura"] or "—",
            str(c["concepto"])[:35],
            (c["fecha_emision"] or "—")[:10],
            (c["fecha_vencimiento"] or "—")[:10],
            f"${float(c['total'] or c['monto'] or 0):,.2f}",
            f"${float(c['monto_abonado'] or 0):,.2f}",
            (c["estado"] or "—").title(),
        ])
    tbl = Table(rows_data, colWidths=[2.5*cm, 5*cm, 2*cm, 2.5*cm, 2.5*cm, 2.5*cm, 2*cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),ACC),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTSIZE",(0,0),(-1,-1),7),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),.25,colors.HexColor("#E5E7EB")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F9FAFB")]),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(tbl)
    story.append(Spacer(1, .5*cm))
    story.append(Paragraph(f"Generado el {date.today().strftime('%d/%m/%Y')} · LexOS", footer_s))
    doc.build(story)
    buf.seek(0)
    safe = nombre.replace(" ","_")[:25]
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"estado_cuenta_{safe}.pdf")


# ─────────────────────────────────────────────────────────────────────────────
#  DASHBOARD DATA (JSON para gráfica)
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/ajax/dashboard")
@login_required
@tenant_required
def ajax_dashboard():
    return jsonify({
        "stats":   _dashboard_stats(g.tenant_db),
        "monthly": _monthly_chart(g.tenant_db),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  HONORARIOS POR HORA — generar factura desde horas registradas en asunto
# ─────────────────────────────────────────────────────────────────────────────

@cobranzas_bp.route("/ajax/facturar-horas", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def facturar_horas():
    """Genera una factura a partir de las horas registradas en un asunto."""
    tdb  = g.tenant_db
    data = request.json or {}

    asunto_id  = _safe_int(data.get("asunto_id"))
    tarifa     = _safe_float(data.get("tarifa_hora"), 0)
    if not asunto_id or tarifa <= 0:
        return jsonify(ok=False, error="Asunto y tarifa/hora son requeridos"), 400

    asunto = tdb.execute("SELECT * FROM asuntos WHERE id=?", (asunto_id,)).fetchone()
    if not asunto:
        return jsonify(ok=False, error="Asunto no encontrado"), 404

    # Sumar horas no facturadas (horas sin cobro asociado en el periodo)
    horas_rows = tdb.execute(
        "SELECT SUM(horas) as total_horas, COUNT(*) as registros "
        "FROM asunto_horas WHERE asunto_id=?",
        (asunto_id,),
    ).fetchone()
    total_horas = float(horas_rows["total_horas"] or 0)
    if total_horas <= 0:
        return jsonify(ok=False, error="No hay horas registradas en este asunto"), 400

    monto    = round(total_horas * tarifa, 2)
    iva_pct  = _safe_float(data.get("iva_pct"), 16.0)
    calc     = _calcular_total(monto, iva_pct, "fijo", 0)
    folio    = _auto_folio(tdb)

    cur = tdb.execute(
        """INSERT INTO cobros
           (cliente_id, asunto_id, concepto, numero_factura,
            monto, moneda, iva_pct, iva_monto, total,
            estado, fecha_emision, abogado_id, notas, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            asunto["cliente_id"], asunto_id,
            f"Honorarios por hora — {total_horas:.1f}h × ${tarifa:,.2f}/h — {asunto['titulo'][:40]}",
            folio, monto,
            data.get("moneda", "MXN"),
            iva_pct, calc["iva_monto"], calc["total"],
            "pendiente", date.today().isoformat(),
            _safe_int(data.get("abogado_id")) or asunto["abogado_responsable"],
            f"Auto-generado desde {int(horas_rows['registros'])} registros de horas ({total_horas:.1f}h @ ${tarifa:,.2f}/h)",
            session.get("user_id"),
        ),
    )
    tdb.commit()
    new_id = cur.lastrowid
    _log("facturar_horas", new_id, {"asunto_id": asunto_id, "horas": total_horas, "tarifa": tarifa})
    return jsonify(ok=True, id=new_id, folio=folio, horas=total_horas, monto=monto, total=calc["total"])


# ─────────────────────────────────────────────────────────────────────────────
#  RECORDATORIOS AUTOMÁTICOS DE VENCIMIENTO
# ─────────────────────────────────────────────────────────────────────────────

def check_recordatorios(tdb, global_db=None, tenant=None):
    """
    Verifica cobros con vencimiento próximo y genera alertas.
    Reglas:
      - 7 días antes → aviso preventivo
      - Día de vencimiento → aviso urgente
      - 3 días después → cobro vencido, aviso de seguimiento
    Retorna lista de alertas para mostrar en UI o enviar por correo.
    """
    hoy = date.today().isoformat()
    d7  = (date.today() + timedelta(days=7)).isoformat()
    d3  = (date.today() - timedelta(days=3)).isoformat()

    alertas = []

    # 7 días antes de vencimiento
    proximos = tdb.execute("""
        SELECT co.id, co.concepto, co.numero_factura, co.fecha_vencimiento,
               co.total, co.monto, co.moneda,
               c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
               c.email as cliente_email
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        WHERE co.estado IN ('pendiente','abono')
          AND co.fecha_vencimiento IS NOT NULL
          AND co.fecha_vencimiento = ?
    """, (d7,)).fetchall()
    for r in proximos:
        alertas.append({
            "cobro_id":   r["id"],
            "tipo":       "preventivo",
            "dias":       7,
            "mensaje":    f"La factura {r['numero_factura'] or r['concepto'][:30]} de {r['cliente_nombre']} vence en 7 días ({r['fecha_vencimiento']})",
            "email":      r["cliente_email"],
            "monto":      float(r["total"] or r["monto"] or 0),
            "moneda":     r["moneda"],
        })

    # Día de vencimiento
    hoy_vence = tdb.execute("""
        SELECT co.id, co.concepto, co.numero_factura, co.fecha_vencimiento,
               co.total, co.monto, co.moneda,
               c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
               c.email as cliente_email
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        WHERE co.estado IN ('pendiente','abono')
          AND co.fecha_vencimiento = ?
    """, (hoy,)).fetchall()
    for r in hoy_vence:
        alertas.append({
            "cobro_id":   r["id"],
            "tipo":       "urgente",
            "dias":       0,
            "mensaje":    f"La factura {r['numero_factura'] or r['concepto'][:30]} de {r['cliente_nombre']} VENCE HOY",
            "email":      r["cliente_email"],
            "monto":      float(r["total"] or r["monto"] or 0),
            "moneda":     r["moneda"],
        })

    # 3 días después de vencimiento (ya vencidos)
    vencidos_3d = tdb.execute("""
        SELECT co.id, co.concepto, co.numero_factura, co.fecha_vencimiento,
               co.total, co.monto, co.moneda,
               c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
               c.email as cliente_email
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        WHERE co.estado IN ('pendiente','abono','vencido')
          AND co.fecha_vencimiento = ?
    """, (d3,)).fetchall()
    for r in vencidos_3d:
        alertas.append({
            "cobro_id":   r["id"],
            "tipo":       "seguimiento",
            "dias":       -3,
            "mensaje":    f"La factura {r['numero_factura'] or r['concepto'][:30]} de {r['cliente_nombre']} está vencida desde hace 3 días",
            "email":      r["cliente_email"],
            "monto":      float(r["total"] or r["monto"] or 0),
            "moneda":     r["moneda"],
        })

    return alertas


@cobranzas_bp.route("/ajax/recordatorios")
@login_required
@tenant_required
def ajax_recordatorios():
    """Retorna las alertas de vencimiento para mostrar en la UI."""
    alertas = check_recordatorios(g.tenant_db)
    return jsonify(alertas=alertas, total=len(alertas))


@cobranzas_bp.route("/ajax/enviar-recordatorio", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def enviar_recordatorio():
    """Envía recordatorio por correo al cliente de un cobro específico."""
    data     = request.json or {}
    cobro_id = _safe_int(data.get("cobro_id"))
    if not cobro_id:
        return jsonify(ok=False, error="cobro_id requerido"), 400

    tdb   = g.tenant_db
    cobro = tdb.execute("""
        SELECT co.*, c.email as cliente_email,
               c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        WHERE co.id=?
    """, (cobro_id,)).fetchone()
    if not cobro:
        return jsonify(ok=False, error="Cobro no encontrado"), 404
    if not cobro["cliente_email"]:
        return jsonify(ok=False, error="El cliente no tiene email registrado"), 400

    # Send email via Flask-Mail or SMTP
    try:
        from flask import current_app
        from flask_mail import Message, Mail
        mail = Mail(current_app)
        tenant = g.tenant
        despacho = tenant["nombre_despacho"] if tenant else "LexOS"
        total   = float(cobro["total"] or cobro["monto"] or 0)
        moneda  = cobro["moneda"] or "MXN"
        folio   = cobro["numero_factura"] or "S/N"
        vence   = cobro["fecha_vencimiento"] or "—"

        msg = Message(
            subject=f"Recordatorio de pago — {folio} | {despacho}",
            sender=current_app.config.get("MAIL_DEFAULT_SENDER", f"noreply@{despacho.lower().replace(' ','-')}.mx"),
            recipients=[cobro["cliente_email"]],
        )
        msg.html = f"""
        <div style="font-family:system-ui;max-width:600px;margin:0 auto;padding:24px">
          <h2 style="color:#00DFA2">{despacho}</h2>
          <p>Estimado/a <strong>{cobro['cliente_nombre']}</strong>,</p>
          <p>Le recordamos que tiene una factura pendiente de pago:</p>
          <table style="width:100%;border-collapse:collapse;margin:16px 0">
            <tr><td style="padding:8px;border:1px solid #E5E7EB;font-weight:600">Folio</td>
                <td style="padding:8px;border:1px solid #E5E7EB">{folio}</td></tr>
            <tr><td style="padding:8px;border:1px solid #E5E7EB;font-weight:600">Concepto</td>
                <td style="padding:8px;border:1px solid #E5E7EB">{cobro['concepto']}</td></tr>
            <tr><td style="padding:8px;border:1px solid #E5E7EB;font-weight:600">Total</td>
                <td style="padding:8px;border:1px solid #E5E7EB;font-weight:700;color:#00DFA2">{moneda} ${total:,.2f}</td></tr>
            <tr><td style="padding:8px;border:1px solid #E5E7EB;font-weight:600">Vencimiento</td>
                <td style="padding:8px;border:1px solid #E5E7EB">{vence}</td></tr>
          </table>
          <p>Agradecemos su pronta atención.</p>
          <p style="color:#9CA3AF;font-size:12px;margin-top:24px">
            Enviado desde {despacho} · LexOS
          </p>
        </div>"""
        mail.send(msg)
        _log("enviar_recordatorio", cobro_id, {"email": cobro["cliente_email"]})
        return jsonify(ok=True, email=cobro["cliente_email"])
    except ImportError:
        return jsonify(ok=False, error="Flask-Mail no está configurado. Configure MAIL_SERVER en config.py"), 500
    except Exception as e:
        return jsonify(ok=False, error=f"Error al enviar correo: {str(e)}"), 500
