"""
Habeas Corpus — modules/cobranzas.py
Cobranza del despacho: qué nos deben, quién y desde cuándo. Cobros con sus pagos
(abonos), recordatorios por correo o WhatsApp, horas por facturar, estado de cuenta
en PDF y exportación a Excel.
"""
from __future__ import annotations

import calendar
import html
import io
import json
import math
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import quote

from flask import (
    Blueprint, abort, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from modules import boveda
from modules.asuntos import _FILTRO_PROPIOS, _fecha, _hoy, _solo_lo_suyo, _usuarios, puede_ver
from modules.auth import login_required, role_required, tenant_required

cobranzas_bp = Blueprint("cobranzas", __name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

ESTADOS = {
    "pendiente": {"label": "Por cobrar",   "color": "#142E59", "fondo": "#F4EEDF"},
    "abono":     {"label": "Pago parcial", "color": "#3D5A80", "fondo": "#E0F2FE"},
    "vencido":   {"label": "Vencido",      "color": "#A23B33", "fondo": "#F8EDEA"},
    "pagado":    {"label": "Pagado",       "color": "#3D5A80", "fondo": "#EEF1F5"},
    "cancelado": {"label": "Cancelado",    "color": "#60718D", "fondo": "#F1EEE8"},
}
ABIERTOS = ("pendiente", "abono", "vencido")

GRUPOS = {
    "abiertos":   "Por cobrar",
    "vencidos":   "Vencidos",
    "pagados":    "Pagados",
    "cancelados": "Cancelados",
    "todos":      "Todos",
}

MONEDAS = {"MXN": "$", "USD": "US$"}
# Los totales del despacho van en pesos; lo que se cobra en otra moneda se muestra aparte, nunca sumado
EN_PESOS = "COALESCE(NULLIF(co.moneda,''),'MXN')='MXN'"
METODOS_PAGO = ["Transferencia", "Efectivo", "Tarjeta", "Cheque", "Otro"]
CONCEPTOS = ["Anticipo de honorarios", "Honorarios por etapa", "Iguala mensual",
             "Honorarios de éxito", "Gastos y derechos"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
ROLES_COBRANZA = ("super_admin", "admin", "abogado", "asistente")
# Retenciones que hace una persona moral al pagar honorarios a una persona física:
# dos terceras partes del IVA (10.6667 % de la base) y 10 % de ISR.
RET_IVA_PCT = Decimal("10.6667")
RET_ISR_PCT = Decimal("10")
TOPE_MONTO = Decimal("999999999")
PER_PAGE = 25

# Montos en SQL. Los cobros viejos guardaban solo `monto`; desde este módulo siempre hay `total`.
TOTAL = "COALESCE(NULLIF(co.total, 0), co.monto, 0)"
SALDO = (f"(CASE WHEN co.estado IN ('pagado','cancelado') THEN 0 "
         f"ELSE MAX({TOTAL} - COALESCE(co.monto_abonado, 0), 0) END)")
CLIENTE = "COALESCE(NULLIF(c.razon_social, ''), TRIM(c.nombre || ' ' || COALESCE(c.apellidos, '')))"
ABIERTO_SQL = "co.estado IN ('pendiente','abono','vencido')"

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _log(accion, entidad_id=None, detalles=None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, "cobro", entidad_id,
             json.dumps(detalles or {}, ensure_ascii=False), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "", "None") else default
    except (TypeError, ValueError):
        return default


def dinero(v, moneda="MXN", decimales=True) -> str:
    v = float(v or 0)
    simbolo = MONEDAS.get(moneda or "MXN", "$")
    signo = "-" if v < 0 else ""
    return f"{signo}{simbolo}{abs(v):,.2f}" if decimales else f"{signo}{simbolo}{abs(v):,.0f}"


cobranzas_bp.add_app_template_filter(dinero, "dinero")


def _fecha_txt(v, anio=True) -> str:
    f = _fecha(v)
    if not f:
        return "—"
    return f"{f.day} {MESES[f.month - 1]}" + (f" {f.year}" if anio else "")


def _auto_folio(tdb) -> str:
    anio = _hoy().year
    usados = {r[0] for r in tdb.execute(
        "SELECT numero_factura FROM cobros WHERE numero_factura LIKE ?", (f"C-{anio}-%",)).fetchall()}
    n = len(usados) + 1
    while f"C-{anio}-{n:04d}" in usados:
        n += 1
    return f"C-{anio}-{n:04d}"


CENTAVO = Decimal("0.01")


def _dec(v, default=None):
    """Texto o número a Decimal (acepta «$12,500.50»). Regresa `default` si no se puede leer."""
    if v in (None, "", "None"):
        return default
    try:
        d = Decimal(str(v).replace(",", "").replace("$", "").replace(" ", "").strip())
    except (InvalidOperation, ValueError):
        return default
    return d if d.is_finite() else default


def _centavos(d) -> Decimal:
    """Redondeo a centavos «de banco»: medio centavo hacia arriba."""
    return Decimal(str(d or 0)).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _calcular(monto, iva_pct, descuento, ret_iva_pct=0, ret_isr_pct=0) -> dict:
    """Honorarios − descuento = subtotal; + IVA; − retenciones = total que paga el cliente.
    Todo en Decimal y redondeado a centavos, igual que un CFDI."""
    base = max(_centavos(_centavos(monto) - _centavos(descuento)), Decimal("0"))
    iva = _centavos(base * Decimal(str(iva_pct or 0)) / 100)
    ret_iva = _centavos(base * Decimal(str(ret_iva_pct or 0)) / 100) if iva else Decimal("0")
    ret_isr = _centavos(base * Decimal(str(ret_isr_pct or 0)) / 100)
    total = base + iva - ret_iva - ret_isr
    return {"subtotal": float(base), "iva_monto": float(iva), "ret_iva_monto": float(ret_iva),
            "ret_isr_monto": float(ret_isr), "total": float(total)}


def _desglose(co) -> dict:
    """Desglose guardado de un cobro. `ajuste` son los descuentos aplicados después (notas de crédito)."""
    co = dict(co)
    hon = _centavos(co.get("monto"))
    desc = _centavos(co.get("descuento_valor"))
    if co.get("descuento_tipo") == "pct":
        desc = _centavos(hon * desc / 100)
    sub = hon - desc
    iva, riva, risr = (_centavos(co.get(k)) for k in ("iva_monto", "ret_iva_monto", "ret_isr_monto"))
    calculado = sub + iva - riva - risr
    total = _centavos(co.get("total")) or hon
    ajuste = min(total - calculado, Decimal("0"))
    if ajuste >= Decimal("-0.01"):        # diferencias de redondeo de cobros viejos, no un descuento
        ajuste = Decimal("0")
    return {"honorarios": float(hon), "descuento": float(desc), "subtotal": float(sub),
            "iva_pct": float(co.get("iva_pct") or 0), "iva": float(iva),
            "ret_iva_pct": float(co.get("ret_iva_pct") or 0), "ret_iva": float(riva),
            "ret_isr_pct": float(co.get("ret_isr_pct") or 0), "ret_isr": float(risr),
            "ajuste": float(ajuste), "total": float(total)}


def _sumar_mes(f: date, dia: int) -> date:
    """El mismo día del mes siguiente (o el último día, si ese mes es más corto)."""
    y, m = divmod(f.year * 12 + f.month, 12)
    return date(y, m + 1, min(dia, calendar.monthrange(y, m + 1)[1]))


_MES_EN_TEXTO = re.compile(r"\b(" + "|".join(MESES_LARGOS) + r")(\s+(?:de\s+|del\s+)?)(\d{4})\b", re.I)


def _concepto_del_mes(concepto: str, f: date) -> str:
    """«Iguala mensual — octubre 2026» → «Iguala mensual — noviembre 2026»."""
    def cambio(m):
        mes = MESES_LARGOS[f.month - 1]
        return (mes.capitalize() if m.group(1)[0].isupper() else mes) + m.group(2) + str(f.year)
    return _MES_EN_TEXTO.sub(cambio, concepto or "", count=1)


def _alcance() -> tuple[str, list]:
    """Los abogados solo ven los cobros de sus asuntos, los que llevan o los que crearon."""
    if not _solo_lo_suyo():
        return "1", []
    uid = session.get("user_id")
    return (f"(co.abogado_id=? OR co.creado_por=? OR co.asunto_id IN (SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}))",
            [uid, uid, uid, uid, uid])


def _puede_ver_cobro(tdb, cid: int) -> bool:
    alc, p = _alcance()
    return bool(tdb.execute(f"SELECT 1 FROM cobros co WHERE co.id=? AND {alc}", (cid, *p)).fetchone())


def _es_admin() -> bool:
    return session.get("role") in ("super_admin", "admin")


def al_dia(tdb) -> None:
    """Pone la cartera al día: montos de cobros viejos, pagos sin historial y vencimientos."""
    hoy = _hoy().isoformat()
    try:
        tdb.execute("UPDATE cobros SET total=monto, iva_monto=0, iva_pct=0 "
                    "WHERE COALESCE(total,0)=0 AND monto>0 AND COALESCE(iva_monto,0)=0")
        # Pagos registrados antes de que existiera el historial de pagos
        tdb.execute("""INSERT INTO cobro_pagos (cobro_id, monto, fecha, metodo, referencia, notas, creado_por)
                       SELECT id, CASE WHEN estado='pagado' THEN total ELSE monto_abonado END,
                              COALESCE(CASE WHEN estado='pagado' THEN fecha_pago ELSE fecha_abono END,
                                       fecha_emision, date('now')),
                              COALESCE(metodo_pago, 'Otro'), referencia, 'Registrado antes del historial de pagos', creado_por
                       FROM cobros co
                       WHERE NOT EXISTS (SELECT 1 FROM cobro_pagos p WHERE p.cobro_id=co.id)
                         AND ((estado='pagado' AND total>0) OR (estado IN ('pendiente','abono','vencido') AND monto_abonado>0))""")
        tdb.execute("""UPDATE cobros SET monto_abonado=COALESCE((SELECT SUM(monto) FROM cobro_pagos p WHERE p.cobro_id=cobros.id),0)
                       WHERE estado != 'cancelado'""")
        tdb.execute("UPDATE cobros SET estado='vencido' WHERE estado='pendiente' "
                    "AND fecha_vencimiento IS NOT NULL AND date(fecha_vencimiento) < ?", (hoy,))
        tdb.execute("UPDATE cobros SET estado='pendiente' WHERE estado='vencido' "
                    "AND (fecha_vencimiento IS NULL OR date(fecha_vencimiento) >= ?)", (hoy,))
        tdb.commit()
    except Exception:
        tdb.rollback()
    try:
        _repetir_igualas(tdb)
        tdb.commit()
    except Exception:
        tdb.rollback()


def _repetir_igualas(tdb) -> int:
    """Igualas que se repiten: cuando llega el día del mes siguiente se crea el cobro de ese mes,
    con los mismos honorarios, IVA, retenciones y plazo de pago. Solo repite el último de la cadena."""
    hoy, creados = _hoy(), 0
    for _ in range(24):                     # si nadie abrió la cobranza en meses, se ponen al día (con tope)
        nuevos = 0
        for co in tdb.execute("""SELECT * FROM cobros co WHERE co.repetir='mensual' AND co.estado != 'cancelado'
                                 AND NOT EXISTS (SELECT 1 FROM cobros h WHERE h.repetido_de=co.id)""").fetchall():
            emision = _fecha(co["fecha_emision"])
            if not emision:
                continue
            dia = co["repetir_dia"] or emision.day
            sig = _sumar_mes(emision, dia)
            if sig > hoy:
                continue
            vence = _fecha(co["fecha_vencimiento"])
            plazo = (vence - emision).days if vence else None
            d = _desglose(co)
            calc = _calcular(d["honorarios"], co["iva_pct"], d["descuento"], co["ret_iva_pct"], co["ret_isr_pct"])
            cur = tdb.execute(
                """INSERT INTO cobros (cliente_id, asunto_id, concepto, numero_factura, monto, moneda, iva_pct, iva_monto,
                                       ret_iva_pct, ret_iva_monto, ret_isr_pct, ret_isr_monto, total,
                                       descuento_tipo, descuento_valor, estado, monto_abonado, fecha_emision, fecha_vencimiento,
                                       abogado_id, repetir, repetir_dia, repetido_de, creado_por)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'fijo',?,'pendiente',0,?,?,?,'mensual',?,?,?)""",
                (co["cliente_id"], co["asunto_id"], _concepto_del_mes(co["concepto"], sig), _auto_folio(tdb),
                 d["honorarios"], co["moneda"] or "MXN", co["iva_pct"] or 0, calc["iva_monto"],
                 co["ret_iva_pct"] or 0, calc["ret_iva_monto"], co["ret_isr_pct"] or 0, calc["ret_isr_monto"], calc["total"],
                 d["descuento"], sig.isoformat(), (sig + timedelta(days=plazo)).isoformat() if plazo is not None else None,
                 co["abogado_id"], dia, co["id"], co["creado_por"]))
            _recalcular(tdb, cur.lastrowid)
            nuevos += 1
        creados += nuevos
        if not nuevos:
            break
    return creados


def _cadena(tdb, cid: int) -> list[int]:
    """Este cobro y los que se crearon después a partir de él (iguala que se repite)."""
    ids, actual = [cid], cid
    for _ in range(600):
        h = tdb.execute("SELECT id FROM cobros WHERE repetido_de=?", (actual,)).fetchone()
        if not h:
            break
        ids.append(h[0])
        actual = h[0]
    return ids


def _recalcular(tdb, cid: int) -> str | None:
    """Estado del cobro a partir de lo pagado y su vencimiento."""
    co = tdb.execute("SELECT * FROM cobros WHERE id=?", (cid,)).fetchone()
    if not co or co["estado"] == "cancelado":
        return co["estado"] if co else None
    suma, ultima = tdb.execute("SELECT COALESCE(SUM(monto),0), MAX(fecha) FROM cobro_pagos WHERE cobro_id=?", (cid,)).fetchone()
    suma = float(_centavos(suma))
    total = float(_centavos(co["total"] or co["monto"] or 0))
    vence = _fecha(co["fecha_vencimiento"])
    if total > 0 and suma >= total:
        estado = "pagado"
    elif suma > 0:
        estado = "abono"
    elif vence and vence < _hoy():
        estado = "vencido"
    else:
        estado = "pendiente"
    tdb.execute("UPDATE cobros SET estado=?, monto_abonado=?, fecha_pago=?, fecha_abono=? WHERE id=?",
                (estado, suma, ultima if estado == "pagado" else None, ultima if suma else None, cid))
    return estado


def cobrado_entre(tdb, desde: date, hasta: date, alcance: bool = True) -> float:
    """Pesos que entraron (pagos registrados) entre dos fechas, inclusive."""
    alc, p = _alcance() if alcance else ("1", [])
    return float(tdb.execute(
        f"""SELECT COALESCE(SUM(pg.monto),0) FROM cobro_pagos pg JOIN cobros co ON co.id=pg.cobro_id
            WHERE co.estado != 'cancelado' AND date(pg.fecha) BETWEEN ? AND ? AND {EN_PESOS} AND {alc}""",
        (desde.isoformat(), hasta.isoformat(), *p)).fetchone()[0])


def _vence_info(estado: str, fv, saldo: float) -> dict | None:
    if estado in ("pagado", "cancelado") or saldo <= 0:
        return None
    f = _fecha(fv)
    if not f:
        return {"texto": "Sin fecha de pago", "tono": "gris", "dias": None}
    dias = (f - _hoy()).days
    if dias < -1:
        return {"texto": f"Venció hace {-dias} días", "tono": "rojo", "dias": dias}
    if dias == -1:
        return {"texto": "Venció ayer", "tono": "rojo", "dias": dias}
    if dias == 0:
        return {"texto": "Vence hoy", "tono": "rojo", "dias": dias}
    if dias == 1:
        return {"texto": "Vence mañana", "tono": "ambar", "dias": dias}
    if dias <= 7:
        return {"texto": f"Vence en {dias} días", "tono": "ambar", "dias": dias}
    return {"texto": f"Vence el {_fecha_txt(f, anio=f.year != _hoy().year)}", "tono": "normal", "dias": dias}


def _decorar(r) -> dict:
    d = dict(r)
    d["total_v"] = float(d.get("total_v") or 0)
    d["saldo_v"] = float(d.get("saldo_v") or 0)
    d["pagado_v"] = max(d["total_v"] - d["saldo_v"], 0) if d["estado"] != "cancelado" else float(d.get("monto_abonado") or 0)
    d["pct"] = int(min(d["pagado_v"] / d["total_v"] * 100, 100)) if d["total_v"] else 0
    d["estado_info"] = ESTADOS.get(d["estado"], ESTADOS["pendiente"])
    d["vence"] = _vence_info(d["estado"], d.get("fecha_vencimiento"), d["saldo_v"])
    d["folio"] = d.get("numero_factura") or f"#{d['id']}"
    d["pagado_el"] = _fecha_txt(d.get("fecha_pago"))
    d["repite"] = d.get("repetir") == "mensual" and d["estado"] != "cancelado"
    d["retiene"] = bool((d.get("ret_iva_monto") or 0) or (d.get("ret_isr_monto") or 0))
    return d


def _clientes():
    return [dict(r) for r in g.tenant_db.execute(
        f"SELECT c.id, {CLIENTE} AS nombre, c.tipo, c.rfc FROM clientes c WHERE c.activo=1 "
        f"ORDER BY nombre COLLATE NOCASE LIMIT 5000"
    ).fetchall()]


def _despacho_persona_fisica() -> bool:
    """Un RFC de 13 caracteres es de persona física: sus clientes personas morales le retienen IVA e ISR."""
    try:
        rfc = (g.tenant["rfc"] or "").strip()
    except (KeyError, IndexError, TypeError):
        rfc = ""
    return len(rfc) == 13


def _abogados():
    try:
        return [dict(r) for r in g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users WHERE tenant_id=? "
            "AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],)).fetchall()]
    except Exception:
        return []


def _guardar_archivo(campo: str, sub: str, extensiones: set):
    f = request.files.get(campo)
    if not f or not f.filename:
        return None, None
    info = boveda.guardar(f, "cobranzas", sub, extensiones=extensiones)
    if not info:
        return None, "Ese tipo de archivo no se permite aquí"
    return info["url"], None


@cobranzas_bp.before_request
def _acceso_al_cobro():
    cid = (request.view_args or {}).get("id")
    tdb = getattr(g, "tenant_db", None)
    if cid and tdb is not None and session.get("user_id") and not _puede_ver_cobro(tdb, cid):
        abort(404)


# ─────────────────────────────────────────────────────────────────────────────
#  LISTA
# ─────────────────────────────────────────────────────────────────────────────


def _filtros():
    """Filtros de la lista (también los usa la exportación)."""
    a = request.args
    f = {
        "grupo": a.get("grupo") if a.get("grupo") in GRUPOS else ("todos" if a.get("q") else "abiertos"),
        "q": a.get("q", "").strip()[:100],
        "cliente": _int(a.get("cliente")),
        "asunto": _int(a.get("asunto")),
        "abogado": _int(a.get("abogado")),
        "semana": a.get("semana") == "1",
    }
    hoy = _hoy().isoformat()
    alc, p = _alcance()
    where, params = [alc], list(p)
    if f["grupo"] == "abiertos":
        where.append(ABIERTO_SQL)
    elif f["grupo"] == "vencidos":
        where.append(f"{ABIERTO_SQL} AND date(co.fecha_vencimiento) < ?")
        params.append(hoy)
    elif f["grupo"] == "pagados":
        where.append("co.estado='pagado'")
    elif f["grupo"] == "cancelados":
        where.append("co.estado='cancelado'")
    if f["semana"]:
        where.append(f"{ABIERTO_SQL} AND date(co.fecha_vencimiento) BETWEEN ? AND ?")
        params += [hoy, (_hoy() + timedelta(days=7)).isoformat()]
    f["chips"] = []
    if f["q"]:
        # Buscador inteligente: «vencidos de Omega», «honorarios de amparo», sin acentos y con errores de dedo
        from modules import buscador
        inter = buscador.interpretar(f["q"], _usuarios())
        ids = buscador.cobros(g.tenant_db, {**inter, "difuso": True}, limite=None)["ids"]
        f["chips"] = inter["chips"]
        where.append(f"co.id IN ({','.join('?' * len(ids)) or 'NULL'})")
        params += ids
    for campo, col in (("cliente", "co.cliente_id"), ("asunto", "co.asunto_id"), ("abogado", "co.abogado_id")):
        if f[campo]:
            where.append(f"{col}=?")
            params.append(f[campo])
    return f, " AND ".join(where), params


BASE_SQL = f"""
    SELECT co.*, {TOTAL} AS total_v, {SALDO} AS saldo_v, {CLIENTE} AS cliente,
           a.titulo AS asunto_titulo, a.numero_expediente
    FROM cobros co
    LEFT JOIN clientes c ON c.id = co.cliente_id
    LEFT JOIN asuntos a ON a.id = co.asunto_id
"""


@cobranzas_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    al_dia(tdb)
    hoy = _hoy()
    f, clause, params = _filtros()
    alc, ap = _alcance()

    orden = {
        "abiertos": "date(co.fecha_vencimiento) IS NULL, date(co.fecha_vencimiento), co.id DESC",
        "vencidos": "date(co.fecha_vencimiento), co.id DESC",
        "pagados":  "co.fecha_pago DESC, co.id DESC",
    }.get(f["grupo"], "date(co.fecha_emision) DESC, co.id DESC")

    total = tdb.execute(f"SELECT COUNT(*) FROM ({BASE_SQL} WHERE {clause})", params).fetchone()[0]
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, _int(request.args.get("page"), 1)), total_pages)
    cobros = [_decorar(r) for r in tdb.execute(
        f"{BASE_SQL} WHERE {clause} ORDER BY {orden} LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()]
    suma_filtro = [dict(x) for x in tdb.execute(
        f"SELECT COALESCE(NULLIF(moneda,''),'MXN') AS moneda, COALESCE(SUM(saldo_v),0) AS saldo, COALESCE(SUM(total_v),0) AS total "
        f"FROM ({BASE_SQL} WHERE {clause}) GROUP BY 1 ORDER BY moneda='MXN' DESC", params).fetchall()]

    # ── Resumen ──────────────────────────────────────────────
    r = tdb.execute(f"""
        SELECT COALESCE(SUM({SALDO}),0) AS por_cobrar,
               COUNT(*) AS abiertos,
               COUNT(DISTINCT co.cliente_id) AS clientes,
               COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) < :hoy THEN {SALDO} END),0) AS vencido,
               COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) < :hoy THEN 1 END),0) AS n_vencidos,
               MIN(CASE WHEN date(co.fecha_vencimiento) < :hoy THEN date(co.fecha_vencimiento) END) AS mas_viejo,
               COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) BETWEEN :hoy AND :sem THEN {SALDO} END),0) AS semana,
               COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) BETWEEN :hoy AND :sem THEN 1 END),0) AS n_semana
        FROM cobros co LEFT JOIN asuntos a ON a.id=co.asunto_id
        WHERE {ABIERTO_SQL} AND {EN_PESOS} AND {alc.replace('?', ':u')}
    """, {"hoy": hoy.isoformat(), "sem": (hoy + timedelta(days=7)).isoformat(), "u": session.get("user_id")}).fetchone()
    otras_monedas = [dict(x) for x in tdb.execute(f"""
        SELECT co.moneda AS moneda, COALESCE(SUM({SALDO}),0) AS por_cobrar, COUNT(*) AS abiertos,
               COALESCE(SUM(CASE WHEN date(co.fecha_vencimiento) < ? THEN {SALDO} END),0) AS vencido
        FROM cobros co LEFT JOIN asuntos a ON a.id=co.asunto_id
        WHERE {ABIERTO_SQL} AND NOT {EN_PESOS} AND {alc} GROUP BY co.moneda HAVING por_cobrar > 0""",
        (hoy.isoformat(), *ap)).fetchall()]
    inicio_mes = hoy.replace(day=1)
    fin_mes_pasado = inicio_mes - timedelta(days=1)
    viejo = _fecha(r["mas_viejo"])
    resumen = {
        **dict(r),
        "dias_viejo": (hoy - viejo).days if viejo else 0,
        "cobrado_mes": cobrado_entre(tdb, inicio_mes, hoy),
        "cobrado_mes_pasado": cobrado_entre(tdb, fin_mes_pasado.replace(day=1), fin_mes_pasado),
        "mes": MESES_LARGOS[hoy.month - 1],
        "mes_pasado": MESES_LARGOS[fin_mes_pasado.month - 1],
    }

    # ── Quién nos debe ───────────────────────────────────────
    deudores = []
    for d in tdb.execute(f"""
        SELECT co.cliente_id, {CLIENTE} AS cliente, SUM({SALDO}) AS debe, COUNT(*) AS n,
               COALESCE(NULLIF(co.moneda,''),'MXN') AS moneda,
               SUM(CASE WHEN date(co.fecha_vencimiento) < ? THEN {SALDO} ELSE 0 END) AS vencido,
               MIN(date(co.fecha_vencimiento)) AS vence
        FROM cobros co LEFT JOIN clientes c ON c.id=co.cliente_id LEFT JOIN asuntos a ON a.id=co.asunto_id
        WHERE {ABIERTO_SQL} AND {alc}
        GROUP BY co.cliente_id, COALESCE(NULLIF(co.moneda,''),'MXN') HAVING debe > 0
        ORDER BY moneda='MXN' DESC, vencido DESC, debe DESC LIMIT 6
    """, (hoy.isoformat(), *ap)).fetchall():
        d = dict(d)
        fv = _fecha(d["vence"])
        if d["vencido"] > 0 and fv:
            d["nota"], d["tono"] = f"{dinero(d['vencido'], d['moneda'], decimales=False)} vencido desde hace {(hoy - fv).days} días", "rojo"
        else:
            d["nota"], d["tono"] = f"{d['n']} cobro{'s' if d['n'] != 1 else ''} · {'vence el ' + _fecha_txt(fv, False) if fv else 'sin fecha de pago'}", ""
        deudores.append(d)

    # ── Antigüedad del saldo ─────────────────────────────────
    tramos = [("Al corriente", "#142E59"), ("1 a 30 días", "#B08A4A"), ("31 a 90 días", "#9A7536"), ("Más de 90 días", "#A23B33")]
    antig = [0.0, 0.0, 0.0, 0.0]
    for s in tdb.execute(f"""SELECT {SALDO} AS saldo, co.fecha_vencimiento FROM cobros co LEFT JOIN asuntos a ON a.id=co.asunto_id
                             WHERE {ABIERTO_SQL} AND {EN_PESOS} AND {alc}""", ap).fetchall():
        fv = _fecha(s["fecha_vencimiento"])
        dias = (hoy - fv).days if fv else 0
        antig[0 if dias <= 0 else 1 if dias <= 30 else 2 if dias <= 90 else 3] += float(s["saldo"] or 0)
    suma_ant = sum(antig)
    antiguedad = [{"label": l, "color": c, "monto": m, "pct": round(m / suma_ant * 100, 1) if suma_ant else 0}
                  for (l, c), m in zip(tramos, antig)]

    # ── Últimos 6 meses ──────────────────────────────────────
    meses = []
    for i in range(5, -1, -1):
        y, m = divmod(hoy.year * 12 + hoy.month - 1 - i, 12)
        ini = date(y, m + 1, 1)
        fin = (date(y + (m + 1) // 12, (m + 1) % 12 + 1, 1) - timedelta(days=1))
        facturado = tdb.execute(f"""SELECT COALESCE(SUM({TOTAL}),0) FROM cobros co LEFT JOIN asuntos a ON a.id=co.asunto_id
                                    WHERE co.estado != 'cancelado' AND date(co.fecha_emision) BETWEEN ? AND ? AND {EN_PESOS} AND {alc}""",
                                (ini.isoformat(), fin.isoformat(), *ap)).fetchone()[0]
        meses.append({"label": MESES[ini.month - 1], "facturado": float(facturado),
                      "cobrado": cobrado_entre(tdb, ini, min(fin, hoy)), "actual": i == 0})
    tope = max([max(x["facturado"], x["cobrado"]) for x in meses] + [1])
    for x in meses:
        x["hf"], x["hc"] = round(x["facturado"] / tope * 100), round(x["cobrado"] / tope * 100)

    # ── Conteos por grupo ────────────────────────────────────
    conteos = {k: 0 for k in GRUPOS}
    for e, n, vencidos in tdb.execute(
            f"""SELECT co.estado, COUNT(*), SUM(CASE WHEN date(co.fecha_vencimiento) < ? THEN 1 ELSE 0 END)
                FROM cobros co LEFT JOIN asuntos a ON a.id=co.asunto_id WHERE {alc} GROUP BY co.estado""",
            (hoy.isoformat(), *ap)).fetchall():
        conteos["todos"] += n
        if e in ABIERTOS:
            conteos["abiertos"] += n
            conteos["vencidos"] += vencidos or 0
        elif e == "pagado":
            conteos["pagados"] += n
        elif e == "cancelado":
            conteos["cancelados"] += n

    # Horas registradas que aún no se cobran
    alc_a = f"AND {_FILTRO_PROPIOS}" if _solo_lo_suyo() else ""
    uid = session.get("user_id")
    horas_pend = tdb.execute(
        f"""SELECT COALESCE(SUM(h.horas),0), COUNT(DISTINCT h.asunto_id) FROM asunto_horas h JOIN asuntos a ON a.id=h.asunto_id
            WHERE h.cobro_id IS NULL {alc_a}""", (uid, uid, uid) if alc_a else ()).fetchone()

    nombres = {}
    if f["cliente"]:
        row = tdb.execute(f"SELECT {CLIENTE} FROM clientes c WHERE c.id=?", (f["cliente"],)).fetchone()
        nombres["cliente"] = row[0] if row else "Cliente"
    if f["asunto"]:
        row = tdb.execute("SELECT titulo FROM asuntos WHERE id=?", (f["asunto"],)).fetchone()
        nombres["asunto"] = row[0] if row else "Asunto"

    base_args = {k: v for k, v in request.args.items() if k not in ("page", "tenant")}

    def liga(**cambios):
        args = {**base_args, **cambios}
        return url_for("cobranzas.index", **{k: v for k, v in args.items() if v not in (None, "", False)})

    usuarios = _usuarios()
    for c in cobros:
        c["abogado"] = usuarios.get(c.get("abogado_id"))

    return render_template(
        "cobranzas/index.html",
        cobros=cobros, total=total, page=page, total_pages=total_pages,
        suma_filtro=suma_filtro, otras_monedas=otras_monedas,
        f=f, nombres=nombres, liga=liga, grupos=GRUPOS, conteos=conteos,
        resumen=resumen, deudores=deudores, antiguedad=antiguedad, suma_ant=suma_ant, meses=meses,
        horas_pend={"horas": float(horas_pend[0]), "asuntos": horas_pend[1]},
        clientes=_clientes(), abogados=_abogados(), conceptos=CONCEPTOS, metodos=METODOS_PAGO,
        puede_crear=session.get("role") in ROLES_COBRANZA, es_admin=_es_admin(),
        hoy=hoy.isoformat(), folio_sugerido=_auto_folio(tdb), despacho_pf=_despacho_persona_fisica(),
        ret_iva_pct=float(RET_IVA_PCT), ret_isr_pct=float(RET_ISR_PCT),
    )


@cobranzas_bp.route("/nuevo")
@login_required
@tenant_required
def nuevo():
    return redirect(url_for("cobranzas.index") + "#nuevo")


# ─────────────────────────────────────────────────────────────────────────────
#  PANEL DE UN COBRO
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/<int:id>/panel")
@login_required
@tenant_required
def panel(id: int):
    tdb = g.tenant_db
    row = tdb.execute(f"{BASE_SQL} WHERE co.id=?", (id,)).fetchone()
    if not row:
        abort(404)
    co = _decorar(row)
    cli = tdb.execute("SELECT * FROM clientes WHERE id=?", (co["cliente_id"],)).fetchone() if co["cliente_id"] else None
    usuarios = _usuarios()
    pagos = []
    for p in tdb.execute("SELECT * FROM cobro_pagos WHERE cobro_id=? ORDER BY date(fecha) DESC, id DESC", (id,)).fetchall():
        p = dict(p)
        p["fecha_txt"] = _fecha_txt(p["fecha"])
        p["autor"] = (usuarios.get(p["creado_por"]) or {}).get("corto")
        pagos.append(p)

    desglose = _desglose(co)
    # Iguala que se repite: el cobro del mes siguiente, o cuándo se va a crear
    siguiente = tdb.execute("SELECT id, numero_factura, fecha_emision FROM cobros WHERE repetido_de=?", (id,)).fetchone()
    repeticion = None
    if co["repite"] or siguiente:
        em = _fecha(co.get("fecha_emision"))
        repeticion = {
            "activa": co["repite"] and not siguiente,
            "siguiente": dict(siguiente) if siguiente else None,
            "cuando": _fecha_txt(_sumar_mes(em, co.get("repetir_dia") or em.day)) if em else "—",
        }

    whatsapp = None
    tel = re.sub(r"\D", "", (cli["celular"] or cli["telefono"] or "") if cli else "")
    if len(tel) >= 10 and co["saldo_v"] > 0:
        tel = tel if len(tel) > 10 else "52" + tel
        nombre = (cli["nombre"] or "").split(" ")[0] if cli["tipo"] == "persona_fisica" else (cli["contacto_principal"] or "")
        vence = f", con vencimiento el {_fecha_txt(co['fecha_vencimiento'])}" if co.get("fecha_vencimiento") else ""
        texto = (f"Hola{(' ' + nombre) if nombre else ''}, le escribimos de {g.tenant['nombre_despacho']}. "
                 f"Le recordamos que tiene un saldo pendiente de {dinero(co['saldo_v'], co['moneda'])} "
                 f"por «{co['concepto']}» (folio {co['folio']}){vence}. "
                 f"Si ya realizó el pago, le agradeceremos enviarnos el comprobante. ¡Gracias!")
        whatsapp = f"https://wa.me/{tel}?text={quote(texto)}"

    actividad = []
    for a in tdb.execute("SELECT accion, detalles, user_id, fecha FROM actividad_log WHERE entidad='cobro' AND entidad_id=? "
                         "ORDER BY id DESC LIMIT 6", (id,)).fetchall():
        etiquetas = {"enviar_recordatorio": "Recordatorio enviado por correo", "nota_credito_parcial": "Descuento aplicado",
                     "editar_cobro": "Cobro editado", "crear_cobro": "Cobro registrado", "cancelar_cobro": "Cobro cancelado",
                     "borrar_pago": "Pago borrado", "dejar_de_repetir": "Dejó de repetirse cada mes",
                     "repetir_cada_mes": "Se repite cada mes"}
        if a["accion"] in etiquetas:
            actividad.append({"accion": etiquetas[a["accion"]],
                              "quien": (usuarios.get(a["user_id"]) or {}).get("corto"),
                              "fecha": _fecha_txt(a["fecha"])})

    return render_template(
        "cobranzas/_panel.html", co=co, cli=dict(cli) if cli else None, pagos=pagos, desglose=desglose, repeticion=repeticion,
        abogado=usuarios.get(co.get("abogado_id")), whatsapp=whatsapp, actividad=actividad,
        es_admin=_es_admin(), puede=session.get("role") in ROLES_COBRANZA,
        emision=_fecha_txt(co.get("fecha_emision")), vencimiento=_fecha_txt(co.get("fecha_vencimiento")),
    )


@cobranzas_bp.route("/<int:id>/datos")
@login_required
@tenant_required
def datos(id: int):
    """Campos del cobro para editarlo."""
    co = g.tenant_db.execute("SELECT * FROM cobros WHERE id=?", (id,)).fetchone()
    if not co:
        abort(404)
    d = {k: co[k] for k in ("id", "cliente_id", "asunto_id", "concepto", "numero_factura", "monto", "moneda", "iva_pct",
                             "fecha_emision", "fecha_vencimiento", "abogado_id", "notas", "estado")}
    des = _desglose(co)
    d["descuento"] = des["descuento"]
    d["ajuste"] = des["ajuste"]
    d["retenciones"] = bool(des["ret_iva"] or des["ret_isr"] or co["ret_isr_pct"])
    d["repetir"] = co["repetir"] == "mensual"
    d["pagado"] = float(co["monto_abonado"] or 0)
    d["archivo_factura"] = bool(co["archivo_factura_url"])
    d["archivo_xml"] = bool(co["archivo_xml_url"])
    nombre = g.tenant_db.execute(f"SELECT {CLIENTE} FROM clientes c WHERE c.id=?", (co["cliente_id"],)).fetchone()
    d["cliente_nombre"] = nombre[0] if nombre else ""
    return jsonify(d)


# ─────────────────────────────────────────────────────────────────────────────
#  ALTA Y EDICIÓN
# ─────────────────────────────────────────────────────────────────────────────


def _leer_cobro(tdb):
    """Valida el formulario de alta/edición. Regresa (valores, (campo, error))."""
    d = request.form
    concepto = " ".join((d.get("concepto") or "").split())[:300]
    cliente_id = _int(d.get("cliente_id"))
    asunto_id = _int(d.get("asunto_id"))
    monto = _dec(d.get("monto"))
    if not cliente_id or not tdb.execute("SELECT 1 FROM clientes WHERE id=?", (cliente_id,)).fetchone():
        return None, ("cliente_id", "Elige a qué cliente le vas a cobrar")
    if asunto_id:
        a = tdb.execute("SELECT cliente_id FROM asuntos WHERE id=?", (asunto_id,)).fetchone()
        if not a or not puede_ver(tdb, asunto_id):
            return None, ("asunto_id", "No encontramos ese asunto. Elige otro o déjalo sin asunto.")
        if a["cliente_id"] and a["cliente_id"] != cliente_id:
            return None, ("asunto_id", "Ese asunto es de otro cliente")
    if not concepto:
        return None, ("concepto", "Escribe qué le estás cobrando (por ejemplo, «Anticipo de honorarios»)")
    if monto is None and (d.get("monto") or "").strip():
        return None, ("monto", "Escribe los honorarios solo con números, por ejemplo 12500.50")
    if monto is None or monto <= 0:
        return None, ("monto", "Escribe cuánto son los honorarios")
    if monto > TOPE_MONTO:
        return None, ("monto", "Revisa los honorarios: la cantidad es demasiado grande")
    monto = _centavos(monto)
    descuento = _dec(d.get("descuento"), Decimal("0"))
    if descuento < 0:
        return None, ("descuento", "El descuento no puede ser negativo")
    descuento = _centavos(descuento)
    if descuento >= monto:
        return None, ("descuento", "El descuento tiene que ser menor que los honorarios")
    iva_pct = 16.0 if d.get("iva") != "0" else 0.0
    retiene = d.get("retenciones") == "1"
    ret_iva_pct = float(RET_IVA_PCT) if retiene and iva_pct else 0.0
    ret_isr_pct = float(RET_ISR_PCT) if retiene else 0.0
    emision = _fecha(d.get("fecha_emision")) or _hoy()
    if (d.get("fecha_vencimiento") or "").strip() and not _fecha(d.get("fecha_vencimiento")):
        return None, ("fecha_vencimiento", "Revisa la fecha límite de pago")
    vence = _fecha(d.get("fecha_vencimiento"))
    if vence and vence < emision:
        return None, ("fecha_vencimiento", "La fecha límite de pago no puede ser antes de la fecha del cobro")
    moneda = d.get("moneda") if d.get("moneda") in MONEDAS else "MXN"
    abogado_id = _int(d.get("abogado_id"))
    if abogado_id and abogado_id not in {a["id"] for a in _abogados()}:
        abogado_id = None
    repetir = d.get("repetir") == "1"
    return {
        "cliente_id": cliente_id, "asunto_id": asunto_id, "concepto": concepto,
        "numero_factura": (d.get("numero_factura") or "").strip()[:40] or None,
        "monto": float(monto), "moneda": moneda, "iva_pct": iva_pct, "descuento_tipo": "fijo", "descuento_valor": float(descuento),
        "ret_iva_pct": ret_iva_pct, "ret_isr_pct": ret_isr_pct,
        **{k: v for k, v in _calcular(monto, iva_pct, descuento, ret_iva_pct, ret_isr_pct).items() if k != "subtotal"},
        "fecha_emision": emision.isoformat(), "fecha_vencimiento": vence.isoformat() if vence else None,
        "abogado_id": abogado_id, "notas": (d.get("notas") or "").strip()[:3000] or None,
        "repetir": "mensual" if repetir else None, "repetir_dia": emision.day if repetir else None,
    }, None


def _error(err, status=400):
    campo, msg = err if isinstance(err, tuple) else (None, err)
    return jsonify(ok=False, error=msg, campo=campo), status


@cobranzas_bp.route("/crear", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def crear():
    tdb = g.tenant_db
    v, err = _leer_cobro(tdb)
    if err:
        return _error(err)
    if not v["abogado_id"]:
        resp = tdb.execute("SELECT abogado_responsable FROM asuntos WHERE id=?", (v["asunto_id"],)).fetchone() if v["asunto_id"] else None
        v["abogado_id"] = (resp["abogado_responsable"] if resp else None) or \
            (session.get("user_id") if session.get("role") != "asistente" else None)
    v["numero_factura"] = v["numero_factura"] or _auto_folio(tdb)
    pdf, err = _guardar_archivo("archivo_factura", "facturas", {"pdf", "png", "jpg", "jpeg"})
    xml, err2 = _guardar_archivo("archivo_xml", "xml", {"xml"})
    if err or err2:
        return _error(("archivo_factura" if err else "archivo_xml",
                       "La factura debe ser PDF o imagen, y el CFDI un archivo .xml"))

    cols = ["cliente_id", "asunto_id", "concepto", "numero_factura", "monto", "moneda", "iva_pct", "iva_monto",
            "ret_iva_pct", "ret_iva_monto", "ret_isr_pct", "ret_isr_monto", "total",
            "descuento_tipo", "descuento_valor", "fecha_emision", "fecha_vencimiento", "abogado_id", "notas",
            "repetir", "repetir_dia"]
    cur = tdb.execute(
        f"INSERT INTO cobros ({', '.join(cols)}, estado, monto_abonado, archivo_factura_url, archivo_xml_url, creado_por) "
        f"VALUES ({', '.join('?' * len(cols))}, 'pendiente', 0, ?, ?, ?)",
        (*[v[c] for c in cols], pdf, xml, session.get("user_id")))
    cid = cur.lastrowid

    if request.form.get("ya_pagado"):
        # Pagado con la fecha del cobro (o hoy, si el cobro tiene fecha futura)
        tdb.execute("INSERT INTO cobro_pagos (cobro_id, monto, fecha, metodo, notas, creado_por) VALUES (?,?,?,?,?,?)",
                    (cid, v["total"], min(v["fecha_emision"], _hoy().isoformat()),
                     request.form.get("metodo") if request.form.get("metodo") in METODOS_PAGO else "Transferencia",
                     "Pagado al registrar el cobro", session.get("user_id")))
    estado = _recalcular(tdb, cid)
    tdb.commit()
    _log("crear_cobro", cid, {"concepto": v["concepto"], "total": v["total"]})
    if v["repetir"]:
        al_dia(tdb)                                  # si la fecha del cobro ya pasó, se crean los meses que siguen
    return jsonify(ok=True, id=cid, folio=v["numero_factura"], estado=estado, total=v["total"])


@cobranzas_bp.route("/<int:id>/editar", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def editar(id: int):
    tdb = g.tenant_db
    co = tdb.execute("SELECT * FROM cobros WHERE id=?", (id,)).fetchone()
    if co["estado"] == "cancelado":
        return jsonify(ok=False, error="Este cobro está cancelado y ya no se puede editar"), 400
    v, err = _leer_cobro(tdb)
    if err:
        return _error(err)
    # Los descuentos aplicados después (notas de crédito) se conservan al editar
    ajuste = Decimal(str(_desglose(co)["ajuste"]))
    if ajuste:
        v["total"] = float(Decimal(str(v["total"])) + ajuste)
        if v["total"] < 0:
            return _error(("monto", f"Este cobro ya tiene descuentos por {dinero(-ajuste, co['moneda'])}; "
                                    "los honorarios no pueden quedar por debajo de eso"))
    pagado = _centavos(co["monto_abonado"])
    if _centavos(v["total"]) < pagado:
        return _error(("monto", f"Ya se pagaron {dinero(pagado, co['moneda'])}; el total no puede quedar por debajo de eso"))
    v["numero_factura"] = v["numero_factura"] or co["numero_factura"] or _auto_folio(tdb)
    pdf, err = _guardar_archivo("archivo_factura", "facturas", {"pdf", "png", "jpg", "jpeg"})
    xml, err2 = _guardar_archivo("archivo_xml", "xml", {"xml"})
    if err or err2:
        return _error(("archivo_factura" if err else "archivo_xml",
                       "La factura debe ser PDF o imagen, y el CFDI un archivo .xml"))
    v["archivo_factura_url"] = pdf or co["archivo_factura_url"]
    v["archivo_xml_url"] = xml or co["archivo_xml_url"]
    if not v["repetir"] and co["repetir"]:
        tdb.execute(f"UPDATE cobros SET repetir=NULL WHERE id IN ({','.join('?' * len(_cadena(tdb, id)))})", _cadena(tdb, id))
    elif v["repetir"] and co["repetir"] and co["repetir_dia"]:
        v["repetir_dia"] = co["repetir_dia"] if _fecha(co["fecha_emision"]) == _fecha(v["fecha_emision"]) else v["repetir_dia"]
    tdb.execute(f"UPDATE cobros SET {', '.join(k + '=?' for k in v)} WHERE id=?", (*v.values(), id))
    estado = _recalcular(tdb, id)
    tdb.commit()
    _log("editar_cobro", id, {"total": v["total"]})
    if v["repetir"] and not co["repetir"]:
        _log("repetir_cada_mes", id)
        al_dia(tdb)
    return jsonify(ok=True, estado=estado, total=v["total"])


@cobranzas_bp.route("/<int:id>/repetir", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def repetir(id: int):
    """Detiene (o reanuda) una iguala que se repite cada mes."""
    tdb = g.tenant_db
    activo = bool((request.get_json(silent=True) or {}).get("activo"))
    co = tdb.execute("SELECT * FROM cobros WHERE id=?", (id,)).fetchone()
    if activo:
        if co["estado"] == "cancelado":
            return jsonify(ok=False, error="Este cobro está cancelado"), 400
        em = _fecha(co["fecha_emision"]) or _hoy()
        tdb.execute("UPDATE cobros SET repetir='mensual', repetir_dia=COALESCE(repetir_dia, ?) WHERE id=?", (em.day, id))
        tdb.commit()
        _log("repetir_cada_mes", id)
        al_dia(tdb)
    else:
        ids = _cadena(tdb, id)
        tdb.execute(f"UPDATE cobros SET repetir=NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
        tdb.commit()
        _log("dejar_de_repetir", id)
    return jsonify(ok=True)


@cobranzas_bp.route("/asuntos-de/<int:cid>")
@login_required
@tenant_required
def asuntos_de(cid: int):
    """Asuntos de un cliente, con lo pactado y lo ya cobrado, para el alta de cobros."""
    tdb = g.tenant_db
    filtro, p = ("", [])
    if _solo_lo_suyo():
        uid = session.get("user_id")
        filtro, p = f"AND {_FILTRO_PROPIOS}", [uid, uid, uid]
    filas = tdb.execute(f"""
        SELECT a.id, a.titulo, a.numero_expediente, a.estado, a.honorarios_pactados, a.forma_cobro, a.abogado_responsable,
               (SELECT COALESCE(SUM({TOTAL}),0) FROM cobros co WHERE co.asunto_id=a.id AND co.estado!='cancelado') AS facturado,
               (SELECT COALESCE(SUM(co.monto - COALESCE(co.descuento_valor,0)),0) FROM cobros co
                 WHERE co.asunto_id=a.id AND co.estado!='cancelado') AS facturado_sub,
               (SELECT COUNT(*) FROM asunto_horas h WHERE h.asunto_id=a.id AND h.cobro_id IS NULL) AS horas_pendientes
        FROM asuntos a WHERE a.cliente_id=? AND a.estado != 'archivado' {filtro}
        ORDER BY a.estado IN ('cerrado','completado'), a.fecha_alta DESC""", (cid, *p)).fetchall()
    return jsonify([{**dict(r), "facturado_sub": round(r["facturado_sub"] or 0, 2)} for r in filas])


@cobranzas_bp.route("/cliente-de-asunto/<int:aid>")
@login_required
@tenant_required
def cliente_de_asunto(aid: int):
    tdb = g.tenant_db
    a = tdb.execute("SELECT cliente_id FROM asuntos WHERE id=?", (aid,)).fetchone()
    if not a or not puede_ver(tdb, aid):
        return jsonify(cliente_id=None), 404
    return jsonify(cliente_id=a["cliente_id"])


# ─────────────────────────────────────────────────────────────────────────────
#  PAGOS
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/<int:id>/pago", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def registrar_pago(id: int):
    tdb = g.tenant_db
    co = tdb.execute(f"SELECT co.*, {SALDO} AS saldo FROM cobros co WHERE co.id=?", (id,)).fetchone()
    if co["estado"] in ("cancelado", "pagado"):
        return jsonify(ok=False, error="Este cobro ya no tiene saldo"), 400
    monto = _dec(request.form.get("monto"))
    if monto is None or monto <= 0:
        return jsonify(ok=False, error="Escribe cuánto pagó, solo con números", campo="monto"), 400
    monto, saldo = _centavos(monto), _centavos(co["saldo"])
    if monto > saldo:
        return jsonify(ok=False, error=f"El pago es mayor a lo que debe ({dinero(saldo, co['moneda'])})", campo="monto"), 400
    monto = float(monto)
    fecha = _fecha(request.form.get("fecha")) or _hoy()
    if fecha > _hoy():
        return jsonify(ok=False, error="La fecha del pago no puede ser posterior a hoy", campo="fecha"), 400
    metodo = request.form.get("metodo") if request.form.get("metodo") in METODOS_PAGO else "Transferencia"
    comprobante, err = _guardar_archivo("comprobante", "comprobantes", {"pdf", "png", "jpg", "jpeg", "webp", "heic"})
    if err:
        return jsonify(ok=False, error="El comprobante debe ser PDF o imagen"), 400
    tdb.execute(
        "INSERT INTO cobro_pagos (cobro_id, monto, fecha, metodo, referencia, comprobante_url, notas, creado_por) VALUES (?,?,?,?,?,?,?,?)",
        (id, monto, fecha.isoformat(), metodo, (request.form.get("referencia") or "").strip()[:80] or None,
         comprobante, (request.form.get("notas") or "").strip()[:500] or None, session.get("user_id")))
    estado = _recalcular(tdb, id)
    tdb.commit()
    _log("registrar_pago", id, {"monto": monto, "estado": estado})
    return jsonify(ok=True, estado=estado, estado_label=ESTADOS[estado]["label"])


@cobranzas_bp.route("/<int:id>/pagos/<int:pid>/borrar", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def borrar_pago(id: int, pid: int):
    tdb = g.tenant_db
    p = tdb.execute("SELECT monto FROM cobro_pagos WHERE id=? AND cobro_id=?", (pid, id)).fetchone()
    if not p:
        return jsonify(ok=False, error="Pago no encontrado"), 404
    tdb.execute("DELETE FROM cobro_pagos WHERE id=?", (pid,))
    estado = _recalcular(tdb, id)
    tdb.commit()
    _log("borrar_pago", id, {"monto": p["monto"]})
    return jsonify(ok=True, estado=estado)


# ─────────────────────────────────────────────────────────────────────────────
#  DESCUENTOS, CANCELACIÓN Y NOTAS
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/<int:id>/nota-credito", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin")
def nota_credito(id: int):
    """tipo=total cancela el cobro; tipo=parcial descuenta un monto del total."""
    tdb = g.tenant_db
    data = request.get_json(silent=True) or {}
    motivo = (data.get("motivo") or "").strip()[:300]
    co = tdb.execute(f"SELECT co.*, {TOTAL} AS total_v, {SALDO} AS saldo FROM cobros co WHERE co.id=?", (id,)).fetchone()
    if co["estado"] == "cancelado":
        return jsonify(ok=False, error="El cobro ya está cancelado"), 400
    if data.get("tipo") == "total":
        if float(co["monto_abonado"] or 0) > 0:
            return jsonify(ok=False, error="Tiene pagos registrados. Bórralos antes de cancelarlo o aplica un descuento."), 400
        nota = f"Cancelado: {motivo}" if motivo else "Cancelado"
        tdb.execute("UPDATE cobros SET estado='cancelado', repetir=NULL, notas=TRIM(COALESCE(notas,'') || char(10) || ?) WHERE id=?",
                    (nota, id))
        tdb.commit()
        _log("cancelar_cobro", id, {"motivo": motivo})
        return jsonify(ok=True, estado="cancelado")
    monto = _centavos(_dec(data.get("monto"), Decimal("0")))
    if monto <= 0 or monto > _centavos(co["saldo"]):
        return jsonify(ok=False, error=f"El descuento debe ser mayor a cero y no pasar de lo que debe ({dinero(co['saldo'], co['moneda'])})"), 400
    nota = f"Descuento de {dinero(monto, co['moneda'])}" + (f": {motivo}" if motivo else "")
    tdb.execute("UPDATE cobros SET total=?, notas=TRIM(COALESCE(notas,'') || char(10) || ?) WHERE id=?",
                (float(_centavos(co["total_v"]) - monto), nota, id))
    estado = _recalcular(tdb, id)
    tdb.commit()
    _log("nota_credito_parcial", id, {"monto": monto, "motivo": motivo})
    return jsonify(ok=True, estado=estado)


@cobranzas_bp.route("/<int:id>/notas", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def actualizar_notas(id: int):
    notas = ((request.get_json(silent=True) or {}).get("notas") or "").strip()[:3000]
    g.tenant_db.execute("UPDATE cobros SET notas=? WHERE id=?", (notas or None, id))
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  RECORDATORIO POR CORREO
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/<int:id>/recordatorio", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def recordatorio(id: int):
    tdb = g.tenant_db
    co = tdb.execute(f"""SELECT co.*, {SALDO} AS saldo, c.email, c.nombre, c.tipo, c.contacto_principal, {CLIENTE} AS cliente
                         FROM cobros co LEFT JOIN clientes c ON c.id=co.cliente_id WHERE co.id=?""", (id,)).fetchone()
    if float(co["saldo"] or 0) <= 0:
        return jsonify(ok=False, error="Este cobro no tiene saldo pendiente"), 400
    if not co["email"]:
        return jsonify(ok=False, error="El cliente no tiene correo registrado"), 400

    despacho = g.tenant["nombre_despacho"]
    saludo = (co["nombre"] or "").split(" ")[0] if co["tipo"] == "persona_fisica" else (co["contacto_principal"] or "")
    filas = [("Concepto", co["concepto"]), ("Folio", co["numero_factura"] or f"#{id}"),
             ("Saldo pendiente", dinero(co["saldo"], co["moneda"]))]
    if co["fecha_vencimiento"]:
        filas.append(("Fecha de pago", _fecha_txt(co["fecha_vencimiento"])))
    tabla = "".join(
        f'<tr><td style="padding:10px 0;color:#60718D;font-size:13px">{html.escape(k)}</td>'
        f'<td style="padding:10px 0;text-align:right;font-weight:600;color:#142E59;font-size:14px">{html.escape(str(v))}</td></tr>'
        for k, v in filas)
    cuerpo = f"""
    <div style="font-family:Inter,'Segoe UI',Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;border:1px solid #E5E2DB;border-radius:14px;overflow:hidden">
      <div style="background:#142E59;padding:22px 26px"><div style="font-size:17px;font-weight:700;color:#fff">{html.escape(despacho)}</div></div>
      <div style="padding:26px">
        <p style="font-size:15px;color:#142E59;margin:0 0 12px">Hola{html.escape(' ' + saludo if saludo else '')},</p>
        <p style="font-size:14px;color:#3B4A61;line-height:1.6;margin:0 0 16px">Le compartimos un recordatorio amable del siguiente saldo pendiente:</p>
        <table style="width:100%;border-collapse:collapse;border-top:1px solid #E5E2DB;border-bottom:1px solid #E5E2DB">{tabla}</table>
        <p style="font-size:14px;color:#3B4A61;line-height:1.6;margin:16px 0 0">Si ya realizó el pago, le agradeceremos responder este correo con el comprobante. Quedamos atentos a cualquier duda.</p>
      </div>
      <div style="background:#F8F6F2;padding:14px 26px;font-size:11px;color:#60718D">{html.escape(despacho)} · Enviado con Habeas Corpus</div>
    </div>"""
    try:
        from flask_mail import Message
        from app import mail
        remitente = g.global_db.execute("SELECT email FROM tenant_users WHERE id=?", (session.get("user_id"),)).fetchone()
        mail.send(Message(subject=f"Recordatorio de pago — {co['concepto']} | {despacho}",
                          recipients=[co["email"]], html=cuerpo,
                          reply_to=(remitente["email"] if remitente else None) or None))
    except Exception:
        return jsonify(ok=False, error="No se pudo enviar el correo. Revisa la configuración de correo del despacho."), 500
    _log("enviar_recordatorio", id, {"email": co["email"]})
    return jsonify(ok=True, email=co["email"])


# ─────────────────────────────────────────────────────────────────────────────
#  HORAS POR FACTURAR
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/horas")
@login_required
@tenant_required
def horas_por_facturar():
    tdb = g.tenant_db
    filtro, p = ("", [])
    if _solo_lo_suyo():
        uid = session.get("user_id")
        filtro, p = f"AND {_FILTRO_PROPIOS}", [uid, uid, uid]
    filas = tdb.execute(f"""
        SELECT a.id, a.titulo, a.numero_expediente, a.cliente_id, {CLIENTE} AS cliente,
               SUM(h.horas) AS horas, COUNT(*) AS registros, MIN(h.fecha) AS desde, MAX(h.fecha) AS hasta
        FROM asunto_horas h JOIN asuntos a ON a.id=h.asunto_id LEFT JOIN clientes c ON c.id=a.cliente_id
        WHERE h.cobro_id IS NULL {filtro}
        GROUP BY a.id HAVING horas > 0 ORDER BY horas DESC""", p).fetchall()
    return jsonify([{**dict(r), "periodo": f"{_fecha_txt(r['desde'], False)} – {_fecha_txt(r['hasta'], False)}"} for r in filas])


@cobranzas_bp.route("/facturar-horas", methods=["POST"])
@login_required
@tenant_required
@role_required(*ROLES_COBRANZA)
def facturar_horas():
    tdb = g.tenant_db
    data = request.get_json(silent=True) or {}
    asunto_id = _int(data.get("asunto_id"))
    tarifa = round(_num(data.get("tarifa")), 2)
    if not asunto_id or tarifa <= 0:
        return jsonify(ok=False, error="Elige el asunto y escribe la tarifa por hora"), 400
    if not puede_ver(tdb, asunto_id):
        return jsonify(ok=False, error="Asunto no encontrado"), 404
    a = tdb.execute("SELECT * FROM asuntos WHERE id=?", (asunto_id,)).fetchone()
    if not a or not a["cliente_id"]:
        return jsonify(ok=False, error="El asunto necesita un cliente para poder cobrarle"), 400
    ids = [r["id"] for r in tdb.execute("SELECT id FROM asunto_horas WHERE asunto_id=? AND cobro_id IS NULL", (asunto_id,)).fetchall()]
    horas = float(tdb.execute(f"SELECT COALESCE(SUM(horas),0) FROM asunto_horas WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})",
                              ids).fetchone()[0])
    if horas <= 0:
        return jsonify(ok=False, error="Ese asunto no tiene horas pendientes de cobrar"), 400

    monto = float(_centavos(Decimal(str(horas)) * Decimal(str(tarifa))))
    iva_pct = 0.0 if str(data.get("iva")) == "0" else 16.0
    calc = _calcular(monto, iva_pct, 0)
    folio = _auto_folio(tdb)
    dias = _int(data.get("dias"), 15)
    cur = tdb.execute(
        """INSERT INTO cobros (cliente_id, asunto_id, concepto, numero_factura, monto, moneda, iva_pct, iva_monto, total,
                               estado, fecha_emision, fecha_vencimiento, abogado_id, notas, creado_por)
           VALUES (?,?,?,?,?,'MXN',?,?,?,'pendiente',?,?,?,?,?)""",
        (a["cliente_id"], asunto_id, f"Honorarios por hora — {horas:g} h", folio, monto, iva_pct, calc["iva_monto"], calc["total"],
         _hoy().isoformat(), (_hoy() + timedelta(days=max(dias, 0))).isoformat(), a["abogado_responsable"],
         f"{len(ids)} registros de horas a {dinero(tarifa)} por hora", session.get("user_id")))
    cid = cur.lastrowid
    tdb.execute(f"UPDATE asunto_horas SET cobro_id=? WHERE id IN ({','.join('?' * len(ids))})", (cid, *ids))
    tdb.commit()
    _log("facturar_horas", cid, {"asunto_id": asunto_id, "horas": horas, "tarifa": tarifa})
    return jsonify(ok=True, id=cid, folio=folio, total=calc["total"])


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORTAR A EXCEL
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/exportar")
@login_required
@tenant_required
def exportar_excel():
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    tdb = g.tenant_db
    al_dia(tdb)
    f, clause, params = _filtros()
    rows = [_decorar(r) for r in tdb.execute(f"{BASE_SQL} WHERE {clause} ORDER BY date(co.fecha_emision) DESC LIMIT 10000", params)]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cobranza"
    encabezados = ["Folio", "Cliente", "Asunto", "Expediente", "Concepto", "Emisión", "Fecha de pago", "Moneda",
                   "Honorarios", "Descuento", "Subtotal", "IVA", "Retención IVA", "Retención ISR", "Descuentos posteriores",
                   "Total", "Pagado", "Saldo", "Estado", "Se repite cada mes"]
    ws.append(encabezados)
    for c in ws[1]:
        c.fill = PatternFill("solid", fgColor="1D3FD6")
        c.font = Font(color="FFFFFF", bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        d = _desglose(r)
        ws.append([r["folio"], r["cliente"], r["asunto_titulo"], r["numero_expediente"], r["concepto"],
                   _fecha(r["fecha_emision"]), _fecha(r["fecha_vencimiento"]), r["moneda"] or "MXN",
                   d["honorarios"], d["descuento"], d["subtotal"], d["iva"], d["ret_iva"], d["ret_isr"], d["ajuste"],
                   r["total_v"], r["pagado_v"], r["saldo_v"], r["estado_info"]["label"], "Sí" if r["repite"] else ""])
    for fila in ws.iter_rows(min_row=2):
        for c in fila[5:7]:
            c.number_format = "DD/MM/YYYY"
        for c in fila[8:18]:
            c.number_format = '"$"#,##0.00'
    if rows:
        n = len(rows) + 1
        ws.append(["Total", None, None, None, None, None, None, None,
                   *[f"=SUM({get_column_letter(i)}2:{get_column_letter(i)}{n})" for i in range(9, 19)], None, None])
        for c in ws[n + 1]:
            c.font = Font(bold=True)
        for c in ws[n + 1][8:18]:
            c.number_format = '"$"#,##0.00'
    for i, ancho in enumerate([13, 30, 32, 14, 34, 12, 13, 8, 14, 12, 14, 12, 13, 13, 14, 14, 14, 14, 13, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = ancho
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(encabezados))}{len(rows) + 1}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"cobranza_{_hoy().isoformat()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ─────────────────────────────────────────────────────────────────────────────
#  ESTADO DE CUENTA (PDF por cliente)
# ─────────────────────────────────────────────────────────────────────────────


@cobranzas_bp.route("/estado-cuenta/<int:cliente_id>")
@login_required
@tenant_required
def estado_cuenta_pdf(cliente_id: int):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    tdb = g.tenant_db
    al_dia(tdb)
    cli = tdb.execute(f"SELECT c.*, {CLIENTE} AS nombre_v FROM clientes c WHERE c.id=?", (cliente_id,)).fetchone()
    if not cli:
        abort(404)
    alc, ap = _alcance()
    cobros = [_decorar(r) for r in tdb.execute(
        f"{BASE_SQL} WHERE co.cliente_id=? AND co.estado != 'cancelado' AND {alc} ORDER BY date(co.fecha_emision)",
        (cliente_id, *ap)).fetchall()]
    if not cobros and _solo_lo_suyo():
        abort(404)
    facturado = sum(c["total_v"] for c in cobros)
    pagado = sum(c["pagado_v"] for c in cobros)
    saldo = sum(c["saldo_v"] for c in cobros)
    vencido = sum(c["saldo_v"] for c in cobros if c["vence"] and (c["vence"]["dias"] or 0) < 0)

    AZUL, TINTA, GRIS, LINEA = (colors.HexColor(x) for x in ("#142E59", "#142E59", "#60718D", "#E5E2DB"))
    st = lambda n, **k: ParagraphStyle(n, fontName=k.pop("font", "Helvetica"), **k)  # noqa: E731
    s_titulo = st("t", font="Helvetica-Bold", fontSize=18, textColor=TINTA, leading=22)
    s_sub = st("s", fontSize=9, textColor=GRIS, leading=13)
    s_celda = st("c", fontSize=8.5, textColor=TINTA, leading=11)
    s_der = st("d", fontSize=8.5, textColor=TINTA, leading=11, alignment=TA_RIGHT)
    s_pie = st("p", fontSize=7.5, textColor=GRIS, leading=10)
    e = lambda x: html.escape(str(x or ""))  # noqa: E731

    hoy = _hoy()
    despacho = g.tenant["nombre_despacho"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm,
                            bottomMargin=1.6 * cm, title=f"Estado de cuenta — {cli['nombre_v']}")
    datos_cli = " · ".join(x for x in (cli["rfc"] and f"RFC {cli['rfc']}", cli["email"]) if x)
    story = [
        Paragraph(e(despacho), st("dsp", font="Helvetica-Bold", fontSize=10, textColor=AZUL)),
        Spacer(1, 6),
        Paragraph("Estado de cuenta", s_titulo),
        Paragraph(f"{e(cli['nombre_v'])}{' · ' + e(datos_cli) if datos_cli else ''}<br/>Al {hoy.day} de {MESES_LARGOS[hoy.month - 1]} de {hoy.year}", s_sub),
        Spacer(1, 14),
    ]
    resumen = Table([["Facturado", "Pagado", "Saldo pendiente", "Vencido"],
                     [dinero(facturado), dinero(pagado), dinero(saldo), dinero(vencido)]],
                    colWidths=[4.4 * cm] * 4)
    resumen.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica", 8), ("TEXTCOLOR", (0, 0), (-1, 0), GRIS),
        ("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 13), ("TEXTCOLOR", (0, 1), (-1, 1), TINTA),
        ("TEXTCOLOR", (2, 1), (2, 1), AZUL), ("TEXTCOLOR", (3, 1), (3, 1), colors.HexColor("#A23B33") if vencido else TINTA),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8F6F2")), ("BOX", (0, 0), (-1, -1), .5, LINEA),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [resumen, Spacer(1, 16)]

    filas = [["Folio", "Concepto", "Emisión", "Fecha de pago", "Total", "Pagado", "Saldo"]]
    for c in cobros:
        concepto = e(c["concepto"]) + (f"<br/><font color='#60718D' size='7'>{e(c['asunto_titulo'])}</font>" if c["asunto_titulo"] else "")
        filas.append([Paragraph(e(c["folio"]), s_celda), Paragraph(concepto, s_celda),
                      _fecha_txt(c["fecha_emision"]), _fecha_txt(c["fecha_vencimiento"]),
                      Paragraph(dinero(c["total_v"], c["moneda"]), s_der), Paragraph(dinero(c["pagado_v"], c["moneda"]), s_der),
                      Paragraph(f"<b>{dinero(c['saldo_v'], c['moneda'])}</b>", s_der)])
    if len(filas) == 1:
        filas.append(["", Paragraph("Sin cobros registrados.", s_celda), "", "", "", "", ""])
    tabla = Table(filas, colWidths=[2.2 * cm, 4.7 * cm, 2.1 * cm, 2.3 * cm, 2.2 * cm, 2.2 * cm, 2.2 * cm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), TINTA), ("ALIGN", (4, 0), (-1, 0), "RIGHT"),
        ("FONT", (2, 1), (3, -1), "Helvetica", 8), ("TEXTCOLOR", (2, 1), (3, -1), TINTA),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 1), (-1, -1), .5, LINEA),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [tabla, Spacer(1, 18)]

    pagos = tdb.execute(
        """SELECT p.fecha, p.monto, p.metodo, p.referencia, co.numero_factura, co.id AS cid, co.moneda
           FROM cobro_pagos p JOIN cobros co ON co.id=p.cobro_id
           WHERE co.cliente_id=? AND co.estado!='cancelado' AND p.cobro_id IN (%s) ORDER BY date(p.fecha)"""
        % (",".join(str(c["id"]) for c in cobros) or "NULL"), (cliente_id,)).fetchall()
    if pagos:
        story.append(Paragraph("Pagos recibidos", st("h", font="Helvetica-Bold", fontSize=10, textColor=TINTA, spaceAfter=6)))
        filas_p = [["Fecha", "Folio", "Forma de pago", "Referencia", "Monto"]]
        for p in pagos:
            filas_p.append([_fecha_txt(p["fecha"]), p["numero_factura"] or f"#{p['cid']}", p["metodo"] or "—",
                            Paragraph(e(p["referencia"] or "—"), s_celda), Paragraph(dinero(p["monto"], p["moneda"]), s_der)])
        tp = Table(filas_p, colWidths=[2.6 * cm, 2.8 * cm, 3.4 * cm, 6.1 * cm, 3 * cm], repeatRows=1)
        tp.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), GRIS),
            ("FONT", (0, 1), (2, -1), "Helvetica", 8), ("TEXTCOLOR", (0, 1), (-1, -1), TINTA),
            ("ALIGN", (4, 0), (4, 0), "RIGHT"), ("LINEBELOW", (0, 0), (-1, -1), .5, LINEA), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [tp, Spacer(1, 18)]
    story.append(Paragraph(f"{e(despacho)} · Documento informativo, no es un comprobante fiscal (CFDI). "
                           f"Generado el {_fecha_txt(hoy)} con Habeas Corpus.", s_pie))
    doc.build(story)
    buf.seek(0)
    seguro = re.sub(r"[^A-Za-z0-9]+", "_", cli["nombre_v"] or "cliente").strip("_")[:40]
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"estado_de_cuenta_{seguro}_{hoy.isoformat()}.pdf")
