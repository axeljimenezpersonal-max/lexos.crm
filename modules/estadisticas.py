"""
LexOS — modules/estadisticas.py
Dashboard de estadísticas con KPIs, gráficas Chart.js, filtros globales,
heatmap de actividad, comparativa entre períodos.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from flask import Blueprint, g, jsonify, render_template, request, session

from modules import cobranzas
from modules.asuntos import TIPOS_DERECHO, _FILTRO_PROPIOS, _solo_lo_suyo
from modules.auth import login_required, tenant_required

estadisticas_bp = Blueprint("estadisticas", __name__)
# Las cifras de dinero son en pesos: un cobro en dólares no se suma con los de pesos
PESOS = "COALESCE(NULLIF(moneda,''),'MXN')='MXN'"

MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
            "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

# Las mismas materias que se usan al crear un expediente
TIPOS_ASUNTO = list(TIPOS_DERECHO)

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


def _alcances():
    """(asuntos, cobros, clientes) que esta persona puede ver: un abogado solo cuenta lo suyo."""
    if not _solo_lo_suyo():
        return "1", "1", "1"
    uid = str(int(session["user_id"]))
    asuntos = _FILTRO_PROPIOS.replace("?", uid)
    cobros = cobranzas._alcance()[0].replace("?", uid)
    clientes = (f"(c.id IN (SELECT a.cliente_id FROM asuntos a WHERE {asuntos}) "
                f"OR c.id IN (SELECT co.cliente_id FROM cobros co WHERE {cobros}))")
    return asuntos, cobros, clientes


def _period_range(periodo, desde_c=None, hasta_c=None):
    """Return (start_date, end_date) ISO strings for the selected period."""
    today = date.today()
    if periodo == "semana":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif periodo == "mes":
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        end = nxt - timedelta(days=1)
    elif periodo == "trimestre":
        q = (today.month - 1) // 3
        start = today.replace(month=q * 3 + 1, day=1)
        nxt = (start + timedelta(days=95)).replace(day=1)
        end = nxt - timedelta(days=1)
    elif periodo == "anio":
        start = today.replace(month=1, day=1)
        end = today.replace(month=12, day=31)
    elif periodo == "custom" and desde_c and hasta_c:
        start = date.fromisoformat(desde_c)
        end = date.fromisoformat(hasta_c)
    else:
        # default: YTD
        start = today.replace(month=1, day=1)
        end = today
    return start.isoformat(), end.isoformat()


def _prev_period(start_s, end_s):
    """Return equivalent previous period for comparisons."""
    s = date.fromisoformat(start_s)
    e = date.fromisoformat(end_s)
    delta = (e - s).days + 1
    prev_e = s - timedelta(days=1)
    prev_s = prev_e - timedelta(days=delta - 1)
    return prev_s.isoformat(), prev_e.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX
# ─────────────────────────────────────────────────────────────────────────────

@estadisticas_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db

    # ── Filtros ──
    periodo   = request.args.get("periodo", "anio")
    desde_c   = request.args.get("desde", "")
    hasta_c   = request.args.get("hasta", "")
    abogado_f = _safe_int(request.args.get("abogado"))
    tipo_f    = request.args.get("tipo_derecho", "")
    # Los filtros van dentro del SQL: solo se aceptan valores conocidos
    if tipo_f not in TIPOS_ASUNTO:
        tipo_f = ""

    try:
        start, end = _period_range(periodo, desde_c, hasta_c)
    except ValueError:
        periodo, desde_c, hasta_c = "anio", "", ""
        start, end = _period_range(periodo)
    prev_start, prev_end = _prev_period(start, end)

    # Un abogado solo ve sus números: sin filtro por colega ni carga del equipo
    solo_lo_suyo = _solo_lo_suyo()
    alc_a, alc_c, alc_cli = _alcances()
    alc_h = f"asunto_id IN (SELECT a.id FROM asuntos a WHERE {alc_a})" if solo_lo_suyo else "1"
    if solo_lo_suyo:
        abogado_f = None
    abogados = [] if solo_lo_suyo else _get_abogados()

    # ── Clause builders ──
    def aw(extra="", prefix="a"):
        """Asuntos WHERE clause."""
        p = f"{prefix}." if prefix else ""
        w = [f"{p}fecha_alta >= '{start}'", f"{p}fecha_alta <= '{end} 23:59:59'", alc_a.replace("a.", p)]
        if abogado_f:
            w.append(f"{p}abogado_responsable={abogado_f}")
        if tipo_f:
            w.append(f"{p}tipo_asunto='{tipo_f}'")
        if extra:
            w.append(extra)
        return " AND ".join(w)

    def cw(extra="", desde=None, hasta=None):
        """Cobros WHERE clause. La consulta debe llamar «co» a la tabla de cobros."""
        w = [f"co.fecha_emision >= '{desde or start}'", f"co.fecha_emision <= '{hasta or end}'", alc_c]
        if abogado_f:
            w.append(f"co.abogado_id={abogado_f}")
        if extra:
            w.append(extra)
        return " AND ".join(w)

    # ══════════════════════════════════════════════════════════════════════════
    #  KPI CARDS
    # ══════════════════════════════════════════════════════════════════════════

    # Asuntos
    kpi_asuntos_activos = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos WHERE estado='activo' AND {aw(prefix='')}"
    ).fetchone()[0]
    kpi_asuntos_completados = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos WHERE estado='completado' AND {aw(prefix='')}"
    ).fetchone()[0]
    kpi_asuntos_archivados = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos WHERE estado='archivado' AND {aw(prefix='')}"
    ).fetchone()[0]
    kpi_asuntos_total = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos WHERE {aw(prefix='')}"
    ).fetchone()[0]

    # Clientes
    kpi_clientes_activos = tdb.execute(
        f"SELECT COUNT(*) FROM clientes c WHERE c.activo=1 AND c.estatus='activo' AND {alc_cli}"
    ).fetchone()[0]
    kpi_clientes_prospectos = tdb.execute(
        f"SELECT COUNT(*) FROM clientes c WHERE c.estatus='prospecto' AND {alc_cli}"
    ).fetchone()[0]

    # Facturación
    r_fac = tdb.execute(f"""
        SELECT COALESCE(SUM(COALESCE(total,monto)),0) as facturado,
               COALESCE(SUM(CASE WHEN estado='pagado' THEN COALESCE(total,monto) ELSE 0 END),0) as cobrado,
               COALESCE(SUM(CASE WHEN estado IN ('pendiente','abono','vencido') THEN COALESCE(total,monto) ELSE 0 END),0) as pendiente
        FROM cobros co WHERE {cw()} AND estado != 'cancelado' AND {PESOS}
    """).fetchone()
    kpi_facturado = float(r_fac["facturado"])
    kpi_cobrado   = float(r_fac["cobrado"])
    kpi_pendiente = float(r_fac["pendiente"])
    kpi_cobrado_pct = round(kpi_cobrado / kpi_facturado * 100, 1) if kpi_facturado > 0 else 0

    # Previous period for comparison
    prev_fac = tdb.execute(f"""
        SELECT COALESCE(SUM(COALESCE(total,monto)),0) FROM cobros co
        WHERE {cw(desde=prev_start, hasta=prev_end)}
          AND estado != 'cancelado' AND {PESOS}
    """).fetchone()[0]
    kpi_fac_delta = round(((kpi_facturado - float(prev_fac)) / float(prev_fac) * 100), 1) if prev_fac else 0

    # Horas
    kpi_horas = tdb.execute(f"""
        SELECT COALESCE(SUM(horas),0) FROM asunto_horas
        WHERE fecha >= '{start}' AND fecha <= '{end}' AND {alc_h}
        {"AND user_id=" + str(abogado_f) if abogado_f else ""}
    """).fetchone()[0]

    # Vencidos
    kpi_vencidos = tdb.execute(
        f"SELECT COUNT(*) FROM asuntos WHERE estado='activo' AND fecha_limite IS NOT NULL AND {alc_a.replace('a.', '')} "
        f"AND fecha_limite < date('now') {'AND abogado_responsable=' + str(abogado_f) if abogado_f else ''}"
    ).fetchone()[0]

    # ══════════════════════════════════════════════════════════════════════════
    #  CHART DATA
    # ══════════════════════════════════════════════════════════════════════════

    # 1. Asuntos por estatus (dona)
    ch_asuntos_estado = [dict(r) for r in tdb.execute(
        f"SELECT estado, COUNT(*) as total FROM asuntos WHERE {aw(prefix='')} GROUP BY estado"
    ).fetchall()]

    # 2. Asuntos por tipo de derecho (barras horizontales)
    ch_asuntos_tipo = [dict(r) for r in tdb.execute(
        f"SELECT tipo_asunto, COUNT(*) as total FROM asuntos WHERE {aw(prefix='')} GROUP BY tipo_asunto ORDER BY total DESC"
    ).fetchall()]

    # 3. Facturación mensual 12 meses (línea + área)
    ch_facturacion = []
    today = date.today()
    for i in range(11, -1, -1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year - ((today.month - i - 1) // 12)
        ym = f"{y:04d}-{m:02d}"
        label = f"{MESES_ES[m-1]} {str(y)[2:]}"
        row = tdb.execute(f"""
            SELECT COALESCE(SUM(COALESCE(total,monto)),0) as facturado,
                   COALESCE(SUM(CASE WHEN estado='pagado' THEN COALESCE(total,monto) ELSE 0 END),0) as cobrado
            FROM cobros co WHERE strftime('%Y-%m', fecha_emision) = ?
              AND estado != 'cancelado' AND {PESOS} AND {alc_c}
              {"AND abogado_id=" + str(abogado_f) if abogado_f else ""}
        """, (ym,)).fetchone()
        ch_facturacion.append({
            "label": label, "facturado": float(row["facturado"]), "cobrado": float(row["cobrado"]),
        })

    # 4. Clientes nuevos por mes (barras)
    ch_clientes_mes = []
    for i in range(11, -1, -1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year - ((today.month - i - 1) // 12)
        ym = f"{y:04d}-{m:02d}"
        cnt = tdb.execute(
            f"SELECT COUNT(*) FROM clientes c WHERE strftime('%Y-%m', c.fecha_alta)=? AND {alc_cli}", (ym,)
        ).fetchone()[0]
        ch_clientes_mes.append({"label": f"{MESES_ES[m-1]} {str(y)[2:]}", "total": cnt})

    # 5. Carga por abogado (barras agrupadas)
    ch_carga_abogado = []
    for ab in abogados:
        activos = tdb.execute(
            "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='activo'",
            (ab["id"],),
        ).fetchone()[0]
        completados = tdb.execute(
            "SELECT COUNT(*) FROM asuntos WHERE abogado_responsable=? AND estado='completado'",
            (ab["id"],),
        ).fetchone()[0]
        ch_carga_abogado.append({
            "nombre": f"{ab['nombre']} {ab['apellidos'][:1]}.",
            "activos": activos, "completados": completados,
        })

    # 6. Heatmap actividad día×hora
    ch_heatmap = []
    hm_query = """
        SELECT CAST(strftime('%w', fecha) AS INTEGER) as dow,
               CAST(strftime('%H', fecha) AS INTEGER) as hour,
               COUNT(*) as cnt
        FROM actividad_log
        WHERE fecha >= date('now', '-30 days')
    """
    hm_user = int(session["user_id"]) if solo_lo_suyo else abogado_f
    if hm_user:
        hm_query += f" AND user_id={hm_user}"
    hm_query += " GROUP BY dow, hour"
    for r in tdb.execute(hm_query).fetchall():
        ch_heatmap.append({"day": r["dow"], "hour": r["hour"], "count": r["cnt"]})

    # 7. Top 5 clientes por facturación
    ch_top_clientes = [dict(r) for r in tdb.execute(f"""
        SELECT c.nombre || ' ' || COALESCE(c.apellidos,'') as nombre,
               COALESCE(SUM(COALESCE(co.total,co.monto)),0) as total
        FROM cobros co
        JOIN clientes c ON co.cliente_id=c.id
        WHERE {cw()} AND co.estado != 'cancelado' AND {PESOS}
        GROUP BY co.cliente_id ORDER BY total DESC LIMIT 5
    """).fetchall()]

    # ══════════════════════════════════════════════════════════════════════════
    #  TABLE DATA (tabs)
    # ══════════════════════════════════════════════════════════════════════════
    tab = request.args.get("tab", "asuntos")

    tab_asuntos = [dict(r) for r in tdb.execute(f"""
        SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
        FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id
        WHERE {aw()} ORDER BY a.fecha_alta DESC LIMIT 100
    """).fetchall()]

    tab_desglose = [dict(r) for r in tdb.execute(f"""
        SELECT tipo_asunto,
               COUNT(*) as total,
               SUM(CASE WHEN estado='activo' THEN 1 ELSE 0 END) as activos,
               SUM(CASE WHEN estado='completado' THEN 1 ELSE 0 END) as completados,
               COALESCE(SUM(honorarios_pactados),0) as honorarios
        FROM asuntos WHERE {aw(prefix='')} GROUP BY tipo_asunto ORDER BY total DESC
    """).fetchall()]

    tab_clientes = [dict(r) for r in tdb.execute(f"""
        SELECT c.*,
               (SELECT COUNT(*) FROM asuntos a WHERE a.cliente_id=c.id AND a.estado='activo' AND {alc_a}) as asuntos_activos,
               (SELECT COALESCE(SUM(COALESCE(co.total,co.monto)),0) FROM cobros co WHERE co.cliente_id=c.id AND co.estado='pagado' AND {PESOS} AND {alc_c}) as total_cobrado
        FROM clientes c WHERE c.activo=1 AND {alc_cli} ORDER BY total_cobrado DESC LIMIT 100
    """).fetchall()]

    tab_facturacion = [dict(r) for r in tdb.execute(f"""
        SELECT co.*, c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre,
               a.titulo as asunto_titulo
        FROM cobros co
        LEFT JOIN clientes c ON co.cliente_id=c.id
        LEFT JOIN asuntos a ON co.asunto_id=a.id
        WHERE {cw()} ORDER BY co.fecha_emision DESC LIMIT 100
    """).fetchall()]

    # ── Alerta: asuntos sin actividad ──
    dias_inactivos = _safe_int(request.args.get("dias_inactivos"), 30)
    alerta_inactivos = [dict(r) for r in tdb.execute(f"""
        SELECT a.id, a.titulo, a.numero_expediente, a.estado,
               MAX(al.fecha) as ultima_actividad,
               julianday('now') - julianday(MAX(al.fecha)) as dias_sin
        FROM asuntos a
        LEFT JOIN actividad_log al ON al.entidad='asunto' AND al.entidad_id=a.id
        WHERE a.estado='activo' AND {alc_a}
        {"AND a.abogado_responsable=" + str(abogado_f) if abogado_f else ""}
        GROUP BY a.id
        HAVING dias_sin > {dias_inactivos} OR ultima_actividad IS NULL
        ORDER BY dias_sin DESC LIMIT 20
    """).fetchall()]

    return render_template(
        "estadisticas/index.html",
        # Filtros
        periodo=periodo, desde_c=desde_c, hasta_c=hasta_c,
        abogado_f=abogado_f, tipo_f=tipo_f, solo_lo_suyo=solo_lo_suyo,
        start=start, end=end,
        abogados=abogados, tipos_asunto=TIPOS_ASUNTO,
        # KPIs
        kpi=dict(
            asuntos_total=kpi_asuntos_total,
            asuntos_activos=kpi_asuntos_activos,
            asuntos_completados=kpi_asuntos_completados,
            asuntos_archivados=kpi_asuntos_archivados,
            clientes_activos=kpi_clientes_activos,
            clientes_prospectos=kpi_clientes_prospectos,
            facturado=kpi_facturado,
            cobrado=kpi_cobrado,
            pendiente=kpi_pendiente,
            cobrado_pct=kpi_cobrado_pct,
            fac_delta=kpi_fac_delta,
            horas=float(kpi_horas),
            vencidos=kpi_vencidos,
        ),
        # Charts
        ch_asuntos_estado=ch_asuntos_estado,
        ch_asuntos_tipo=ch_asuntos_tipo,
        ch_facturacion=ch_facturacion,
        ch_clientes_mes=ch_clientes_mes,
        ch_carga_abogado=ch_carga_abogado,
        ch_heatmap=ch_heatmap,
        ch_top_clientes=ch_top_clientes,
        # Tables
        tab=tab,
        tab_asuntos=tab_asuntos,
        tab_desglose=tab_desglose,
        tab_clientes=tab_clientes,
        tab_facturacion=tab_facturacion,
        # Alerts
        alerta_inactivos=alerta_inactivos,
        dias_inactivos=dias_inactivos,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — datos para export
# ─────────────────────────────────────────────────────────────────────────────

@estadisticas_bp.route("/ajax/datos")
@login_required
@tenant_required
def ajax_datos():
    """Returns raw chart data as JSON for client-side use."""
    # Reuse the same logic but return JSON
    # The frontend already has the data injected via Jinja,
    # so this is mainly for refreshing without page reload
    return jsonify(ok=True)
