from flask import Blueprint, render_template, g, session, redirect, url_for, request
from modules.auth import login_required, tenant_required
import json

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    # Sin tenant resuelto → landing de discovery
    if not getattr(g, "tenant", None):
        return render_template("auth/landing.html")
    # Sin sesión → login
    if not session.get("user_id"):
        return redirect(url_for("auth.login"))

    tdb = g.tenant_db
    gdb = g.global_db
    uid = session.get("user_id")
    role = session.get("role")

    # ── KPIs básicos ──────────────────────────────────────────────
    total_asuntos = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE estado='activo'"
    ).fetchone()[0]

    total_clientes = tdb.execute(
        "SELECT COUNT(*) FROM clientes WHERE activo=1"
    ).fetchone()[0]

    cobros_pendientes = tdb.execute(
        "SELECT COALESCE(SUM(monto),0) FROM cobros WHERE estado='pendiente'"
    ).fetchone()[0]

    cobros_mes = tdb.execute(
        """SELECT COALESCE(SUM(monto),0) FROM cobros
           WHERE estado='pagado'
           AND strftime('%Y-%m', fecha_pago) = strftime('%Y-%m', 'now')"""
    ).fetchone()[0]

    # Facturación total = pagado + pendiente del mes en curso
    facturacion_total = tdb.execute(
        """SELECT COALESCE(SUM(monto),0) FROM cobros
           WHERE strftime('%Y-%m', COALESCE(fecha_vencimiento, fecha_pago)) = strftime('%Y-%m','now')"""
    ).fetchone()[0]

    # Horas del mes (tabla registros_tiempo si existe, else 0)
    try:
        total_horas_mes = tdb.execute(
            """SELECT COALESCE(SUM(horas), 0) FROM registros_tiempo
               WHERE strftime('%Y-%m', fecha) = strftime('%Y-%m','now')"""
        ).fetchone()[0]
    except Exception:
        total_horas_mes = 0

    # ── Asuntos prioritarios (top 5 activos por prioridad) ────────
    try:
        asuntos_prioritarios = tdb.execute(
            """SELECT a.id, a.titulo, a.numero_expediente,
                      COALESCE(a.prioridad,'media') as prioridad,
                      COALESCE(a.materia,'Sin materia') as materia,
                      COALESCE(a.monto_honorarios, 0) as monto_honorarios,
                      c.nombre || ' ' || COALESCE(c.apellidos,'') as cliente_nombre
               FROM asuntos a
               LEFT JOIN clientes c ON a.cliente_id = c.id
               WHERE a.estado = 'activo'
               ORDER BY CASE COALESCE(a.prioridad,'baja')
                         WHEN 'alta'  THEN 1
                         WHEN 'media' THEN 2
                         ELSE 3 END ASC,
                        a.fecha_alta DESC
               LIMIT 5"""
        ).fetchall()
    except Exception:
        asuntos_prioritarios = []

    # ── Próximos eventos (30 días, top 8) ─────────────────────────
    proximos_eventos = tdb.execute(
        """SELECT e.*, a.titulo as asunto_titulo
           FROM eventos_calendario e
           LEFT JOIN asuntos a ON e.asunto_id = a.id
           WHERE e.fecha_inicio >= datetime('now')
             AND e.fecha_inicio <= datetime('now', '+30 days')
             AND e.estado = 'pendiente'
           ORDER BY e.fecha_inicio
           LIMIT 8"""
    ).fetchall()

    # ── Distribución de asuntos por materia (donut) ───────────────
    try:
        distribucion_materias = tdb.execute(
            """SELECT COALESCE(materia,'Sin materia') as materia,
                      COUNT(*) as total
               FROM asuntos
               WHERE estado = 'activo'
               GROUP BY materia
               ORDER BY total DESC
               LIMIT 7"""
        ).fetchall()
    except Exception:
        distribucion_materias = []

    # ── Datos para gráfica de líneas: últimos 6 meses ─────────────
    from datetime import date

    _MESES = ["Ene","Feb","Mar","Abr","May","Jun",
              "Jul","Ago","Sep","Oct","Nov","Dic"]

    chart_labels   = []
    chart_ingresos = []
    chart_cobros   = []

    hoy = date.today()
    for i in range(5, -1, -1):
        m = hoy.month - i
        y = hoy.year
        while m <= 0:
            m += 12
            y -= 1
        ym = f"{y:04d}-{m:02d}"
        chart_labels.append(_MESES[m - 1])

        total_creado = tdb.execute(
            """SELECT COALESCE(SUM(monto),0) FROM cobros
               WHERE strftime('%Y-%m', COALESCE(fecha_vencimiento,'1970-01')) = ?""",
            (ym,)
        ).fetchone()[0]
        chart_ingresos.append(float(total_creado))

        total_pagado = tdb.execute(
            """SELECT COALESCE(SUM(monto),0) FROM cobros
               WHERE estado='pagado'
               AND strftime('%Y-%m', fecha_pago) = ?""",
            (ym,)
        ).fetchone()[0]
        chart_cobros.append(float(total_pagado))

    # ── Actividad reciente ─────────────────────────────────────────
    # NOTE: usuarios lives in global_db (tenant_users), not tenant db
    _act_rows = tdb.execute(
        "SELECT * FROM actividad_log ORDER BY fecha DESC LIMIT 4"
    ).fetchall()
    # Build user name map from global DB
    _uid_set = {r["user_id"] for r in _act_rows if r["user_id"]}
    _user_map = {}
    if _uid_set:
        _placeholders = ",".join("?" * len(_uid_set))
        for u in gdb.execute(
            f"SELECT id, nombre, apellidos FROM tenant_users WHERE id IN ({_placeholders})",
            list(_uid_set)
        ).fetchall():
            _user_map[u["id"]] = f"{u['nombre']} {u['apellidos'] or ''}".strip()
    actividad = []
    for r in _act_rows:
        row = dict(r)
        row["user_nombre"] = _user_map.get(r["user_id"], "Sistema")
        # Build display_title from detalles JSON or entity lookup
        display_title = None
        try:
            if r["detalles"]:
                det = json.loads(r["detalles"])
                for key in ("titulo", "nombre", "asunto_titulo", "cliente_nombre", "folio"):
                    if det.get(key):
                        raw = str(det[key])
                        display_title = raw[:36] + "…" if len(raw) > 36 else raw
                        break
        except Exception:
            pass
        if not display_title and r["entidad"] and r["entidad_id"]:
            try:
                if r["entidad"] == "asunto":
                    a = tdb.execute("SELECT titulo FROM asuntos WHERE id=?", (r["entidad_id"],)).fetchone()
                    if a: display_title = (a["titulo"][:36] + "…") if len(a["titulo"]) > 36 else a["titulo"]
                elif r["entidad"] == "cliente":
                    c = tdb.execute("SELECT nombre, apellidos, razon_social FROM clientes WHERE id=?", (r["entidad_id"],)).fetchone()
                    if c: display_title = c["razon_social"] or f"{c['nombre']} {c['apellidos'] or ''}".strip()
                elif r["entidad"] == "cobro":
                    cb = tdb.execute("SELECT folio FROM cobros WHERE id=?", (r["entidad_id"],)).fetchone()
                    if cb and cb["folio"]: display_title = cb["folio"]
                elif r["entidad"] in ("evento", "evento_calendario"):
                    ev = tdb.execute("SELECT titulo FROM eventos_calendario WHERE id=?", (r["entidad_id"],)).fetchone()
                    if ev: display_title = (ev["titulo"][:36] + "…") if len(ev["titulo"]) > 36 else ev["titulo"]
            except Exception:
                pass
        row["display_title"] = display_title
        actividad.append(row)

    # ── ZONA ROJA: plazos críticos ─────────────────────────────────
    _hoy_iso = hoy.isoformat()
    from datetime import timedelta
    _7d = (hoy + timedelta(days=7)).isoformat()

    plazos_criticos_raw = tdb.execute(
        """SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.asunto_id,
                  a.titulo as asunto_titulo, a.numero_expediente
           FROM eventos_calendario e
           LEFT JOIN asuntos a ON e.asunto_id = a.id
           WHERE e.estado = 'pendiente'
             AND e.tipo IN ('fecha_limite','vencimiento','audiencia','diligencia','plazo','tarea')
             AND date(e.fecha_inicio) >= ?
             AND date(e.fecha_inicio) <= ?
           ORDER BY e.fecha_inicio ASC
           LIMIT 8""",
        (_hoy_iso, _7d),
    ).fetchall()

    plazos_criticos = []
    for p in plazos_criticos_raw:
        d = dict(p)
        try:
            fi = date.fromisoformat(d["fecha_inicio"][:10])
            dias = (fi - hoy).days
            d["dias_restantes"] = dias
            d["urgencia"] = "critica" if dias <= 1 else ("alta" if dias <= 3 else "media")
        except Exception:
            d["dias_restantes"] = None
            d["urgencia"] = "media"
        plazos_criticos.append(d)

    vencidos = tdb.execute(
        """SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.asunto_id,
                  a.titulo as asunto_titulo, a.numero_expediente
           FROM eventos_calendario e
           LEFT JOIN asuntos a ON e.asunto_id = a.id
           WHERE e.estado = 'pendiente'
             AND e.tipo IN ('fecha_limite','vencimiento','plazo')
             AND date(e.fecha_inicio) < ?
           ORDER BY e.fecha_inicio DESC
           LIMIT 5""",
        (_hoy_iso,),
    ).fetchall()
    for v in vencidos:
        d = dict(v)
        try:
            fi = date.fromisoformat(d["fecha_inicio"][:10])
            d["dias_restantes"] = (fi - hoy).days
        except Exception:
            d["dias_restantes"] = -1
        d["urgencia"] = "vencido"
        plazos_criticos.insert(0, d)

    # ── Serializar datos de gráficas para Jinja → JS ──────────────
    chart_labels_json   = json.dumps(chart_labels)
    chart_ingresos_json = json.dumps(chart_ingresos)
    chart_cobros_json   = json.dumps(chart_cobros)

    materias_nombres = [row["materia"] for row in distribucion_materias]
    materias_totales = [row["total"] for row in distribucion_materias]
    materias_nombres_json = json.dumps(materias_nombres)
    materias_totales_json = json.dumps(materias_totales)

    return render_template(
        "dashboard/index.html",
        # KPIs
        total_asuntos=total_asuntos,
        total_clientes=total_clientes,
        cobros_pendientes=cobros_pendientes,
        cobros_mes=cobros_mes,
        facturacion_total=facturacion_total,
        total_horas_mes=total_horas_mes,
        # Contenido
        asuntos_prioritarios=asuntos_prioritarios,
        proximos_eventos=proximos_eventos,
        distribucion_materias=distribucion_materias,
        actividad=actividad,
        plazos_criticos=plazos_criticos,
        # Chart data (JSON strings)
        chart_labels_json=chart_labels_json,
        chart_ingresos_json=chart_ingresos_json,
        chart_cobros_json=chart_cobros_json,
        materias_nombres_json=materias_nombres_json,
        materias_totales_json=materias_totales_json,
        # Legacy (kept for backward compat)
        asuntos_recientes=[],
        cobros_proximos=[],
        tareas_pendientes=[],
    )
