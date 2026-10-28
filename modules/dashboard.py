from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, g, session, redirect, url_for, request, jsonify
from modules import cobranzas
from modules.auth import login_required, sesion_del_despacho, tenant_required

dashboard_bp = Blueprint("dashboard", __name__)

TERMINO_TIPOS = ("plazo", "vencimiento", "fecha_limite")
AUDIENCIA_TIPOS = ("audiencia", "diligencia")
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
DIAS_LARGOS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

SALDO_SQL = "(COALESCE(NULLIF(total, 0), monto) - COALESCE(monto_abonado, 0))"
NOMBRE_CLIENTE_SQL = "COALESCE(NULLIF(c.razon_social, ''), TRIM(c.nombre || ' ' || COALESCE(c.apellidos, '')))"


def _fecha(valor):
    try:
        return date.fromisoformat(str(valor)[:10])
    except (TypeError, ValueError):
        return None


def _cuando(dias):
    if dias < 0:
        return "Vencido" if dias < -1 else "Venció ayer"
    if dias == 0:
        return "Hoy"
    if dias == 1:
        return "Mañana"
    return f"En {dias} días"


def _tono(dias, es_termino):
    if dias < 0 or (es_termino and dias <= 1):
        return "red"
    if dias <= 5:
        return "amb"
    return "grn"


def _hora(evento):
    if evento["todo_el_dia"] or len(str(evento["fecha_inicio"] or "")) < 16:
        return "Todo el día"
    return str(evento["fecha_inicio"])[11:16]


def _clase(tipo):
    if tipo in TERMINO_TIPOS:
        return "plz"
    if tipo in AUDIENCIA_TIPOS:
        return "aud"
    return "reu"


@dashboard_bp.route("/")
def index():
    if not getattr(g, "tenant", None):
        return render_template("auth/landing.html")
    if not sesion_del_despacho():
        return redirect(url_for("auth.login"))

    tdb = g.tenant_db
    uid = session.get("user_id")
    hoy = date.today()
    hoy_iso = hoy.isoformat()
    tipos_termino = ",".join("?" * len(TERMINO_TIPOS))

    # ── Términos ──────────────────────────────────────────────
    terminos_semana = tdb.execute(
        f"""SELECT COUNT(*) FROM eventos_calendario
            WHERE estado='pendiente' AND tipo IN ({tipos_termino})
              AND date(fecha_inicio) BETWEEN ? AND ?""",
        (*TERMINO_TIPOS, hoy_iso, (hoy + timedelta(days=6)).isoformat()),
    ).fetchone()[0]
    terminos_pronto = tdb.execute(
        f"""SELECT COUNT(*) FROM eventos_calendario
            WHERE estado='pendiente' AND tipo IN ({tipos_termino})
              AND date(fecha_inicio) BETWEEN ? AND ?""",
        (*TERMINO_TIPOS, hoy_iso, (hoy + timedelta(days=1)).isoformat()),
    ).fetchone()[0]
    terminos_vencidos = tdb.execute(
        f"""SELECT COUNT(*) FROM eventos_calendario
            WHERE estado='pendiente' AND tipo IN ({tipos_termino}) AND date(fecha_inicio) < ?""",
        (*TERMINO_TIPOS, hoy_iso),
    ).fetchone()[0]

    # ── Agenda de hoy (todo lo que no es término) ─────────────
    agenda_hoy = tdb.execute(
        f"""SELECT titulo, tipo, fecha_inicio, todo_el_dia FROM eventos_calendario
            WHERE estado='pendiente' AND tipo NOT IN ({tipos_termino}) AND date(fecha_inicio) = ?
            ORDER BY fecha_inicio""",
        (*TERMINO_TIPOS, hoy_iso),
    ).fetchall()

    # ── Asuntos ───────────────────────────────────────────────
    asuntos_activos = tdb.execute("SELECT COUNT(*) FROM asuntos WHERE estado='activo'").fetchone()[0]
    sin_movimiento = tdb.execute(
        """SELECT COUNT(*) FROM asuntos a
           WHERE a.estado='activo'
             AND date(COALESCE(a.fecha_alta, '1970-01-01')) < date('now', '-30 days')
             AND NOT EXISTS (SELECT 1 FROM asunto_actividades x
                             WHERE x.asunto_id = a.id AND date(x.fecha_alta) >= date('now', '-30 days'))
             AND NOT EXISTS (SELECT 1 FROM eventos_calendario e
                             WHERE e.asunto_id = a.id AND date(e.fecha_alta) >= date('now', '-30 days'))"""
    ).fetchone()[0]
    etapas_rows = tdb.execute("SELECT estado, COUNT(*) AS n FROM asuntos GROUP BY estado").fetchall()
    por_estado = {r["estado"]: r["n"] for r in etapas_rows}
    concluidos_anio = tdb.execute(
        """SELECT COUNT(*) FROM asuntos
           WHERE estado IN ('completado','cerrado','archivado')
             AND strftime('%Y', COALESCE(fecha_cierre, fecha_alta)) = strftime('%Y', 'now')"""
    ).fetchone()[0]
    etapas = [
        {"clave": "por_iniciar", "nombre": "Por iniciar", "n": por_estado.get("por_iniciar", 0), "color": "#B9C4E8"},
        {"clave": "activo", "nombre": "En trámite", "n": por_estado.get("activo", 0), "color": "#1D3FD6"},
        {"clave": "en_espera", "nombre": "En espera",
         "n": por_estado.get("en_espera", 0) + por_estado.get("suspendido", 0), "color": "#5BC0FF"},
        {"clave": "concluidos", "nombre": "Concluidos este año", "n": concluidos_anio, "color": "#A7DCC4"},
    ]

    # ── Cobranza ──────────────────────────────────────────────
    cobranzas.al_dia(tdb)
    por_cobrar = tdb.execute(
        f"SELECT COALESCE(SUM({SALDO_SQL}), 0) FROM cobros WHERE estado IN ('pendiente','abono','vencido')"
    ).fetchone()[0]
    vencido = tdb.execute(
        f"""SELECT COALESCE(SUM({SALDO_SQL}), 0) FROM cobros
            WHERE estado IN ('pendiente','abono','vencido')
              AND (estado='vencido' OR date(fecha_vencimiento) < ?)""",
        (hoy_iso,),
    ).fetchone()[0]
    cobrado_mes = cobranzas.cobrado_entre(tdb, hoy.replace(day=1), hoy, alcance=False)
    deudores = []
    for r in tdb.execute(
        f"""SELECT {NOMBRE_CLIENTE_SQL} AS cliente, x.cliente_id,
                   SUM(x.saldo) AS debe, MIN(x.fecha_vencimiento) AS vence
            FROM (SELECT cliente_id, {SALDO_SQL} AS saldo, fecha_vencimiento
                  FROM cobros WHERE estado IN ('pendiente','abono','vencido')) x
            LEFT JOIN clientes c ON c.id = x.cliente_id
            GROUP BY x.cliente_id
            HAVING debe > 0
            ORDER BY (MIN(x.fecha_vencimiento) < ?) DESC, debe DESC
            LIMIT 4""",
        (hoy_iso,),
    ).fetchall():
        f = _fecha(r["vence"])
        if f is None:
            nota, tarde = "Sin fecha de pago", False
        elif f < hoy:
            dias = (hoy - f).days
            nota, tarde = (f"Vencido hace {dias} día{'s' if dias != 1 else ''}", True)
        else:
            nota, tarde = (f"Vence el {f.day} de {MESES_CORTOS[f.month - 1]}", False)
        deudores.append({"cliente": r["cliente"] or "Sin cliente", "cliente_id": r["cliente_id"],
                         "debe": r["debe"], "nota": nota, "tarde": tarde})
    total_cartera = (por_cobrar or 0) + (cobrado_mes or 0)
    pct_cobrado = round(100 * (cobrado_mes or 0) / total_cartera) if total_cartera else 0
    pct_vencido = round(100 * (vencido or 0) / total_cartera) if total_cartera else 0

    # ── Lo urgente: vencidos + próximos 14 días ───────────────
    urgentes = []
    filas = tdb.execute(
        f"""SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.asunto_id,
                   a.titulo AS asunto, a.numero_expediente, {NOMBRE_CLIENTE_SQL} AS cliente
            FROM eventos_calendario e
            LEFT JOIN asuntos a ON a.id = e.asunto_id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE e.estado='pendiente'
              AND ((e.tipo IN ({tipos_termino}) AND date(e.fecha_inicio) < ?)
                   OR (e.tipo IN ({tipos_termino},?,?) AND date(e.fecha_inicio) BETWEEN ? AND ?))
            ORDER BY date(e.fecha_inicio), e.fecha_inicio
            LIMIT 6""",
        (*TERMINO_TIPOS, hoy_iso, *TERMINO_TIPOS, *AUDIENCIA_TIPOS,
         hoy_iso, (hoy + timedelta(days=14)).isoformat()),
    ).fetchall()
    for r in filas:
        f = _fecha(r["fecha_inicio"])
        if not f:
            continue
        dias = (f - hoy).days
        es_termino = r["tipo"] in TERMINO_TIPOS
        detalle = " · ".join(x for x in (r["asunto"], r["cliente"], r["numero_expediente"]) if x)
        urgentes.append({
            "titulo": r["titulo"], "detalle": detalle or "Sin asunto vinculado",
            "dia": f.day, "mes": MESES_CORTOS[f.month - 1],
            "cuando": _cuando(dias) if es_termino or dias <= 1 else "Audiencia",
            "tono": _tono(dias, es_termino),
            "url": url_for("asuntos.detalle", id=r["asunto_id"]) if r["asunto_id"] else url_for("calendario.index"),
        })

    # ── Tu semana (lunes a domingo) ───────────────────────────
    lunes = hoy - timedelta(days=hoy.weekday())
    domingo = lunes + timedelta(days=6)
    semana = [{"d": DIAS[i], "n": (lunes + timedelta(days=i)).day,
               "hoy": (lunes + timedelta(days=i)) == hoy, "ev": []} for i in range(7)]
    for e in tdb.execute(
        f"""SELECT e.titulo, e.tipo, e.fecha_inicio, e.todo_el_dia, e.lugar,
                   a.titulo AS asunto, {NOMBRE_CLIENTE_SQL} AS cliente
            FROM eventos_calendario e
            LEFT JOIN asuntos a ON a.id = e.asunto_id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE e.estado != 'cancelado' AND date(e.fecha_inicio) BETWEEN ? AND ?
            ORDER BY e.fecha_inicio""",
        (lunes.isoformat(), domingo.isoformat()),
    ).fetchall():
        f = _fecha(e["fecha_inicio"])
        if not f:
            continue
        clase = _clase(e["tipo"])
        semana[(f - lunes).days]["ev"].append({
            "t": _hora(e), "k": clase,
            "b": ("Vence: " if clase == "plz" else "") + e["titulo"],
            "s": " · ".join(x for x in (e["asunto"] or e["cliente"], e["lugar"]) if x),
            "chip": {"plz": "Término", "aud": "Audiencia"}.get(clase),
        })

    # ── Pendientes ────────────────────────────────────────────
    tareas = []
    for t in tdb.execute(
        """SELECT t.id, t.titulo, t.fecha_vencimiento, a.titulo AS asunto
           FROM tareas t LEFT JOIN asuntos a ON a.id = t.asunto_id
           WHERE t.estado IN ('pendiente','en_proceso') AND (t.asignado_a = ? OR t.asignado_a IS NULL)
           ORDER BY t.fecha_vencimiento IS NULL, t.fecha_vencimiento, t.id
           LIMIT 5""",
        (uid,),
    ).fetchall():
        f = _fecha(t["fecha_vencimiento"])
        if f is None:
            cuando = "Sin fecha"
        elif f < hoy:
            cuando = "Atrasada"
        elif f == hoy:
            cuando = "Hoy"
        elif f == hoy + timedelta(days=1):
            cuando = "Mañana"
        elif (f - hoy).days < 7:
            cuando = DIAS_LARGOS[f.weekday()]
        else:
            cuando = f"{f.day} de {MESES_CORTOS[f.month - 1]}"
        tareas.append({"id": t["id"], "titulo": t["titulo"], "cuando": cuando,
                       "atrasada": f is not None and f < hoy, "asunto": t["asunto"]})

    # ── Resumen en una frase ──────────────────────────────────
    partes = []
    if terminos_vencidos:
        partes.append(f"{terminos_vencidos} término{'s' if terminos_vencidos != 1 else ''} vencido{'s' if terminos_vencidos != 1 else ''}")
    if terminos_pronto:
        partes.append(f"{terminos_pronto} término{'s' if terminos_pronto != 1 else ''} que vence{'n' if terminos_pronto != 1 else ''} hoy o mañana")
    if agenda_hoy:
        partes.append(f"{len(agenda_hoy)} compromiso{'s' if len(agenda_hoy) != 1 else ''} hoy")

    agenda_nota = ""
    if agenda_hoy:
        primero = agenda_hoy[0]
        agenda_nota = ("Todo el día: " if _hora(primero) == "Todo el día" else f"{_hora(primero)} · ") + primero["titulo"]

    return render_template(
        "dashboard/index.html",
        fecha_larga=f"{DIAS_LARGOS[hoy.weekday()]} {hoy.day} de {MESES[hoy.month - 1]}",
        resumen=partes,
        mes_nombre=MESES[hoy.month - 1],
        terminos_semana=terminos_semana,
        terminos_pronto=terminos_pronto,
        terminos_vencidos=terminos_vencidos,
        agenda_hoy=len(agenda_hoy),
        agenda_nota=agenda_nota,
        asuntos_activos=asuntos_activos,
        sin_movimiento=sin_movimiento,
        por_cobrar=por_cobrar or 0,
        vencido=vencido or 0,
        cobrado_mes=cobrado_mes or 0,
        pct_cobrado=pct_cobrado,
        pct_vencido=min(pct_vencido, 100 - pct_cobrado),
        deudores=deudores,
        urgentes=urgentes,
        semana=semana,
        hoy_idx=hoy.weekday(),
        tareas=tareas,
        etapas=etapas,
    )


@dashboard_bp.route("/tareas/<int:tarea_id>/estado", methods=["POST"])
@login_required
@tenant_required
def tarea_estado(tarea_id):
    if request.headers.get("X-Requested-With") != "XMLHttpRequest" or not request.is_json:
        return jsonify(ok=False, error="Solicitud no válida"), 400
    hecha = bool((request.get_json(silent=True) or {}).get("hecha"))
    tdb = g.tenant_db
    cur = tdb.execute(
        "UPDATE tareas SET estado = ?, fecha_completada = ? WHERE id = ?",
        ("completada" if hecha else "pendiente",
         datetime.now().isoformat(timespec="seconds") if hecha else None, tarea_id),
    )
    tdb.commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, error="Tarea no encontrada"), 404
    return jsonify(ok=True)
