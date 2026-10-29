"""
Habeas Corpus — modules/asuntos.py
Asuntos (expedientes): lista y tablero, alta, ficha del asunto con su actividad,
documentos, horas, estrategia, equipo y precedentes.
"""

from __future__ import annotations

import html
import io
import json
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from modules import boveda
from modules import lector
from modules.insights import casos as precedentes
from modules.auth import login_required, role_required, tenant_required

asuntos_bp = Blueprint("asuntos", __name__)
ZONA = ZoneInfo("America/Mexico_City")

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────────────────────

TIPOS_DERECHO = {
    "civil":          {"label": "Civil",          "color": "#2F4A6E",
                       "subtypes": ["Arrendamiento", "Sucesión", "Responsabilidad civil", "Obligaciones", "Contratos", "Otro"]},
    "mercantil":      {"label": "Mercantil",      "color": "#4B6585",
                       "subtypes": ["Cobro", "Contratos mercantiles", "Sociedades", "Concurso mercantil", "Títulos de crédito", "Otro"]},
    "laboral":        {"label": "Laboral",        "color": "#9A7536",
                       "subtypes": ["Despido injustificado", "Reinstalación", "Prestaciones", "Seguridad social", "Otro"]},
    "penal":          {"label": "Penal",          "color": "#A23B33",
                       "subtypes": ["Defensa", "Querella", "Delitos patrimoniales", "Delitos contra la persona", "Otro"]},
    "familiar":       {"label": "Familiar",       "color": "#8E6F4A",
                       "subtypes": ["Divorcio", "Custodia", "Pensión alimenticia", "Adopción", "Otro"]},
    "administrativo": {"label": "Administrativo", "color": "#7A6440",
                       "subtypes": ["IMSS", "ISSSTE", "SAT", "Permisos y licencias", "Otro"]},
    "fiscal":         {"label": "Fiscal",         "color": "#3D5A80",
                       "subtypes": ["Controversia fiscal", "Devoluciones", "Resoluciones SAT", "Otro"]},
    "amparo":         {"label": "Amparo",         "color": "#9A7536",
                       "subtypes": ["Amparo indirecto", "Amparo directo", "Amparo en revisión", "Otro"]},
    "inmobiliario":   {"label": "Inmobiliario",   "color": "#4B6585",
                       "subtypes": ["Compraventa", "Arrendamiento", "Usucapión", "Fraccionamiento", "Otro"]},
    "constitucional": {"label": "Constitucional", "color": "#6E5A3A",
                       "subtypes": ["Acción de inconstitucionalidad", "Controversia constitucional", "Otro"]},
    "internacional":  {"label": "Internacional",  "color": "#60718D",
                       "subtypes": ["Arbitraje", "Contratos internacionales", "Otro"]},
    "notarial":       {"label": "Notarial",       "color": "#AD9060",
                       "subtypes": ["Compraventa", "Poder notarial", "Constitución de sociedad", "Testamento", "Sucesión notarial",
                                    "Cancelación de hipoteca", "Ratificación de firmas", "Otro"]},
    "otro":           {"label": "Otro",           "color": "#60718D", "subtypes": []},
}
# Materias sin juicio: no se piden juzgado ni contraparte
SIN_JUICIO = ("notarial",)

ESTADOS = {
    "por_iniciar": {"label": "Por iniciar", "color": "#60718D", "fondo": "#F1EEE8"},
    "activo":      {"label": "En trámite",      "color": "#142E59", "fondo": "#F4EEDF"},
    "en_espera":   {"label": "En espera",   "color": "#8A6A2F", "fondo": "#F7F0E1"},
    "suspendido":  {"label": "Suspendido",  "color": "#6E5A3A", "fondo": "#F6F1E6"},
    "completado":  {"label": "Concluido",   "color": "#3D5A80", "fondo": "#EEF1F5"},
    "cerrado":     {"label": "Concluido",   "color": "#3D5A80", "fondo": "#EEF1F5"},
    "archivado":   {"label": "Archivado",   "color": "#60718D", "fondo": "#F1EEE8"},
}
ABIERTOS = ("por_iniciar", "activo", "en_espera", "suspendido")
CONCLUIDOS = ("completado", "cerrado")
# Estados que se ofrecen al usuario (completado queda como sinónimo heredado de cerrado)
ESTADOS_MENU = ("por_iniciar", "activo", "en_espera", "suspendido", "cerrado", "archivado")
GRUPOS = {
    "abiertos":   ("En curso", ABIERTOS),
    "concluidos": ("Concluidos", CONCLUIDOS),
    "archivados": ("Archivados", ("archivado",)),
    "todos":      ("Todos", tuple(ESTADOS)),
}
TABLERO = [
    {"key": "por_iniciar", "label": "Por iniciar", "estados": ("por_iniciar",)},
    {"key": "activo",      "label": "En trámite",      "estados": ("activo",)},
    {"key": "en_espera",   "label": "En espera",   "estados": ("en_espera", "suspendido")},
    {"key": "cerrado",     "label": "Concluido",   "estados": CONCLUIDOS},
]

TIPOS_ACTIVIDAD = ["audiencia", "acuerdo", "diligencia", "vencimiento", "reunion", "nota", "otro"]
ACTIVIDAD_LABEL = {"audiencia": "Audiencia", "acuerdo": "Acuerdo", "diligencia": "Diligencia",
                   "vencimiento": "Término", "reunion": "Reunión", "nota": "Pendiente", "otro": "Otro"}
TIPOS_SENTENCIA = ["sentencia", "resolucion", "auto", "acuerdo", "otro"]
SENTENCIA_LABEL = {"sentencia": "Sentencia", "resolucion": "Resolución", "auto": "Auto",
                   "acuerdo": "Acuerdo", "otro": "Otro"}
EVENTO_LABEL = {"audiencia": "Audiencia", "vencimiento": "Término", "plazo": "Término",
                "fecha_limite": "Fecha límite", "diligencia": "Diligencia", "reunion": "Reunión",
                "tarea": "Tarea", "recordatorio": "Recordatorio", "otro": "Evento"}

# Texto sugerido al elegir la materia en el alta
PLANTILLAS = {
    "civil":     {"titulo_ejemplo": "Juicio ordinario civil — [Actor] vs [Demandado]",
                  "descripcion": "Hechos principales:\n\n"},
    "mercantil": {"titulo_ejemplo": "Juicio mercantil — [Empresa] vs [Empresa]",
                  "descripcion": "Reclamación por incumplimiento de contrato. Hechos:\n\n"},
    "laboral":   {"titulo_ejemplo": "Demanda laboral — [Trabajador] vs [Empresa]",
                  "descripcion": "Juicio laboral por despido injustificado. Hechos:\n\n"},
    "penal":     {"titulo_ejemplo": "Carpeta de investigación — [Nombre]",
                  "descripcion": "Delito que se imputa y hechos:\n\n"},
    "amparo":    {"titulo_ejemplo": "Amparo — [Quejoso] vs [Autoridad]",
                  "descripcion": "Acto reclamado:\n\n"},
    "familiar":  {"titulo_ejemplo": "Divorcio / custodia — [Apellidos]",
                  "descripcion": "Hechos y pretensiones del cliente:\n\n"},
    "notarial":  {"titulo_ejemplo": "Compraventa de inmueble — [Calle y número]",
                  "descripcion": "Partes, inmueble y condiciones de la operación:\n\n"},
}

PER_PAGE = 25

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(accion: str, entidad_id: int | None = None, detalles: dict | None = None):
    try:
        g.tenant_db.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) VALUES (?,?,?,?,?,?)",
            (session.get("user_id"), accion, "asunto", entidad_id,
             json.dumps(detalles or {}), request.remote_addr),
        )
        g.tenant_db.commit()
    except Exception:
        pass


def _auto_numero(tdb) -> str:
    year = date.today().year
    count = tdb.execute(
        "SELECT COUNT(*) FROM asuntos WHERE strftime('%Y', fecha_alta) = ?", (str(year),)
    ).fetchone()[0] + 1
    return f"EXP-{year}-{count:03d}"


def _get_abogados() -> list[dict]:
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos, cargo FROM tenant_users WHERE tenant_id=? "
            "AND rol IN ('abogado','admin','super_admin') AND activo=1 ORDER BY nombre",
            (g.tenant["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _usuarios() -> dict[int, dict]:
    """Todos los usuarios del despacho por id, con nombre e iniciales."""
    try:
        rows = g.global_db.execute(
            "SELECT id, nombre, apellidos FROM tenant_users WHERE tenant_id=?", (g.tenant["id"],)
        ).fetchall()
    except Exception:
        return {}
    return {r["id"]: _persona(r) for r in rows}


# Avatares en la familia marino, gris azulado y dorado
_COLORES_PERSONA = ("#142E59", "#AD9060", "#34496B", "#60718D", "#8A6A2F", "#3D5A80", "#6E5A3A", "#4A5A73")


def _persona(r) -> dict:
    nombre = f"{r['nombre']} {r['apellidos'] or ''}".strip()
    partes = nombre.split()
    iniciales = (partes[0][0] + partes[-1][0]) if len(partes) > 1 else nombre[:2]
    return {"id": r["id"], "nombre": nombre, "corto": f"{r['nombre']} {(r['apellidos'] or '').split(' ')[0]}".strip(),
            "iniciales": iniciales.upper(), "color": _COLORES_PERSONA[r["id"] % len(_COLORES_PERSONA)]}


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


def _fecha(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _local(v) -> datetime | None:
    """Fechas guardadas por SQLite (CURRENT_TIMESTAMP, UTC) a hora de México."""
    try:
        dt = datetime.fromisoformat(str(v)[:19])
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZONA).replace(tzinfo=None)


def _hoy() -> date:
    return datetime.now(ZONA).date()


def _cliente_nombre(row) -> str:
    return (row["cliente_razon"] or row["cliente_nombre"] or "").strip()


def _solo_lo_suyo() -> bool:
    """Los abogados solo ven los asuntos donde son responsables, colaboradores o que crearon."""
    return session.get("role") == "abogado"


_FILTRO_PROPIOS = ("(a.abogado_responsable=? OR a.creado_por=? OR "
                   "a.id IN (SELECT asunto_id FROM asunto_colaboradores WHERE user_id=?))")


def puede_ver(tdb, asunto_id: int) -> bool:
    if not _solo_lo_suyo():
        return True
    uid = session.get("user_id")
    return bool(tdb.execute(f"SELECT 1 FROM asuntos a WHERE a.id=? AND {_FILTRO_PROPIOS}",
                            (asunto_id, uid, uid, uid)).fetchone())


@asuntos_bp.before_request
def _acceso_al_asunto():
    aid = (request.view_args or {}).get("id")
    tdb = getattr(g, "tenant_db", None)
    if aid and tdb is not None and session.get("user_id") and not puede_ver(tdb, aid):
        abort(404)


def _semaforo(dias: int | None) -> str:
    if dias is None:
        return "gris"
    if dias < 0 or dias <= 3:
        return "rojo"
    if dias <= 10:
        return "ambar"
    return "normal"


def _cuando(dias: int) -> str:
    if dias < -1:
        return f"Venció hace {abs(dias)} días"
    if dias == -1:
        return "Venció ayer"
    if dias == 0:
        return "Hoy"
    if dias == 1:
        return "Mañana"
    return f"En {dias} días"


def _proximos(tdb, ids: list[int], limites: dict[int, str | None]) -> dict[int, dict]:
    """Lo siguiente que vence en cada asunto: evento de calendario, actividad pendiente o fecha límite."""
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    candidatos: dict[int, list[tuple]] = {i: [] for i in ids}
    for r in tdb.execute(
        f"SELECT asunto_id, titulo, tipo, fecha_inicio FROM eventos_calendario WHERE estado='pendiente' "
        f"AND asunto_id IN ({marcas}) AND date(fecha_inicio) >= date('now','-1 day') ORDER BY fecha_inicio", ids
    ).fetchall():
        f = _fecha(r["fecha_inicio"])
        if f:
            candidatos[r["asunto_id"]].append((f, EVENTO_LABEL.get(r["tipo"], "Evento"), r["titulo"]))
    for r in tdb.execute(
        f"SELECT asunto_id, titulo, tipo, fecha FROM asunto_actividades WHERE realizado=0 "
        f"AND asunto_id IN ({marcas}) ORDER BY fecha", ids
    ).fetchall():
        f = _fecha(r["fecha"])
        if f:
            candidatos[r["asunto_id"]].append((f, ACTIVIDAD_LABEL.get(r["tipo"], "Pendiente"), r["titulo"]))
    for aid, limite in limites.items():
        f = _fecha(limite)
        if f:
            candidatos[aid].append((f, "Fecha límite", "Fecha límite del asunto"))
    hoy, salida = _hoy(), {}
    for aid, lista in candidatos.items():
        if lista:
            f, tipo, titulo = min(lista, key=lambda c: c[0])
            dias = (f - hoy).days
            salida[aid] = {"fecha": f.isoformat(), "tipo": tipo, "titulo": titulo, "dias": dias,
                           "cuando": _cuando(dias), "semaforo": _semaforo(dias),
                           "corta": f"{f.day} {_MESES[f.month - 1][:3]}"}
    return salida


def _ultimos_movimientos(tdb, ids: list[int]) -> dict[int, date]:
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    filas = tdb.execute(
        f"""SELECT asunto_id, MAX(f) AS ultima FROM (
              SELECT id AS asunto_id, fecha_alta AS f FROM asuntos WHERE id IN ({marcas})
              UNION ALL SELECT asunto_id, fecha FROM asunto_notas WHERE asunto_id IN ({marcas})
              UNION ALL SELECT asunto_id, fecha_alta FROM asunto_actividades WHERE asunto_id IN ({marcas})
              UNION ALL SELECT asunto_id, fecha_subida FROM documentos WHERE asunto_id IN ({marcas})
              UNION ALL SELECT asunto_id, fecha_alta FROM asunto_sentencias WHERE asunto_id IN ({marcas})
              UNION ALL SELECT entidad_id, fecha FROM actividad_log WHERE entidad='asunto' AND entidad_id IN ({marcas})
              UNION ALL SELECT asunto_id, fecha_alta FROM eventos_calendario WHERE asunto_id IN ({marcas})
            ) GROUP BY asunto_id""",
        ids * 7,
    ).fetchall()
    return {r["asunto_id"]: (_local(r["ultima"]) or datetime.now()).date() for r in filas}


_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


def _fecha_corta(v) -> str:
    """'2026-10-29T10:00' → '29 oct 2026 · 10:00'."""
    f = _fecha(v)
    if not f:
        return ""
    hora = str(v)[11:16]
    return f"{f.day} {_MESES[f.month - 1][:3]} {f.year}" + (f" · {hora}" if hora and hora != "00:00" else "")


def _hace(d: date | None) -> str:
    if not d:
        return "—"
    dias = (_hoy() - d).days
    if dias <= 0:
        return "Hoy"
    if dias == 1:
        return "Ayer"
    if dias < 30:
        return f"Hace {dias} días"
    if dias < 365:
        meses = dias // 30
        return f"Hace {meses} mes{'es' if meses > 1 else ''}"
    return f"{d.day} {_MESES[d.month - 1][:3]} {d.year}"


def _decorar(rows, tdb, usuarios) -> list[dict]:
    """Datos que necesitan la lista y el tablero."""
    asuntos = [dict(r) for r in rows]
    abiertos = [a for a in asuntos if a["estado"] in ABIERTOS]
    proximos = _proximos(tdb, [a["id"] for a in abiertos], {a["id"]: a["fecha_limite"] for a in abiertos})
    movimientos = _ultimos_movimientos(tdb, [a["id"] for a in asuntos])
    for a in asuntos:
        a["cliente"] = _cliente_nombre(a)
        a["tipo_info"] = TIPOS_DERECHO.get(a.get("tipo_asunto") or "otro", TIPOS_DERECHO["otro"])
        a["estado_info"] = ESTADOS.get(a["estado"], ESTADOS["activo"])
        a["responsable"] = usuarios.get(a.get("abogado_responsable"))
        a["proximo"] = proximos.get(a["id"])
        a["movimiento"] = _hace(movimientos.get(a["id"]))
        a["resultado_info"] = precedentes.RESULTADOS.get(a.get("resultado") or "")
    return asuntos


# ─────────────────────────────────────────────────────────────────────────────
#  LISTA Y TABLERO
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/")
@login_required
@tenant_required
def index():
    tdb = g.tenant_db
    uid = session.get("user_id")
    precedentes.asegurar(tdb)

    q        = request.args.get("q", "").strip()
    grupo    = request.args.get("grupo", "abiertos")
    tipo     = request.args.get("tipo", "")
    abogado  = _safe_int(request.args.get("abogado"))
    atencion = request.args.get("atencion", "")
    vista    = request.args.get("vista") if request.args.get("vista") in ("tablero", "lista") else "carpetas"
    page     = max(1, _safe_int(request.args.get("page"), 1))
    if grupo not in GRUPOS:
        grupo = "abiertos"

    # Buscador inteligente: entiende la frase (materia, responsable, fechas, estado) y ordena por parecido
    from modules import buscador
    chips, orden_busqueda, sugerencia = [], None, None
    if q:
        inter = buscador.interpretar(q, _usuarios())
        hallados = buscador.expedientes(tdb, inter, limite=None)
        chips, orden_busqueda = inter["chips"], hallados["ids"]
        sugerencia = " ".join(hallados["corregido"]) if hallados.get("corregido") else None
        # Si la frase no dice el estado, se busca en todos
        if "grupo" not in request.args or inter["filtros"].get("estado"):
            grupo = "todos"

    # Alcance: lo que esta persona puede ver
    alcance, alcance_p = ["1"], []
    if _solo_lo_suyo():
        alcance.append(_FILTRO_PROPIOS)
        alcance_p += [uid, uid, uid]
    base = ("FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id WHERE " + " AND ".join(alcance))
    columnas = ("SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') AS cliente_nombre, "
                "c.razon_social AS cliente_razon ")

    # Resumen (siempre sobre los asuntos en curso del alcance)
    abiertos = tdb.execute(
        f"SELECT a.id, a.fecha_limite {base} AND a.estado IN ({','.join('?' * len(ABIERTOS))})",
        alcance_p + list(ABIERTOS),
    ).fetchall()
    ids_abiertos = [r["id"] for r in abiertos]
    proximos = _proximos(tdb, ids_abiertos, {r["id"]: r["fecha_limite"] for r in abiertos})
    movimientos = _ultimos_movimientos(tdb, ids_abiertos)
    con_termino = [i for i, p in proximos.items() if p["dias"] <= 7]
    sin_mov = [i for i in ids_abiertos if (_hoy() - movimientos.get(i, _hoy())).days > 30]
    concluidos_anio = tdb.execute(
        f"SELECT COUNT(*) {base} AND a.estado IN ('completado','cerrado') "
        "AND strftime('%Y', COALESCE(a.fecha_cierre, a.fecha_alta)) = strftime('%Y','now')",
        alcance_p,
    ).fetchone()[0]
    resumen = {"abiertos": len(ids_abiertos), "termino": len(con_termino), "sin_mov": len(sin_mov),
               "concluidos": concluidos_anio}

    # Filtros
    where, params = [], []
    if orden_busqueda is not None:
        where.append(f"a.id IN ({','.join('?' * len(orden_busqueda)) or 'NULL'})")
        params += orden_busqueda
    if tipo:
        where.append("a.tipo_asunto=?")
        params.append(tipo)
    if abogado:
        where.append("a.abogado_responsable=?")
        params.append(abogado)
    if atencion in ("termino", "sin_mov"):
        ids = con_termino if atencion == "termino" else sin_mov
        where.append(f"a.id IN ({','.join('?' * len(ids)) or 'NULL'})")
        params += ids
    filtro = (" AND " + " AND ".join(where)) if where else ""

    usuarios = _usuarios()
    asuntos, columnas_tablero, total, total_pages = [], [], 0, 1
    if vista == "tablero":
        for col in TABLERO:
            marcas = ",".join("?" * len(col["estados"]))
            limite = 30 if col["key"] == "cerrado" else 80
            filas = tdb.execute(
                f"{columnas}{base}{filtro} AND a.estado IN ({marcas}) "
                f"ORDER BY COALESCE(a.fecha_cierre, a.fecha_alta) DESC LIMIT {limite}",
                alcance_p + params + list(col["estados"]),
            ).fetchall()
            columnas_tablero.append({**col, "asuntos": _decorar(filas, tdb, usuarios)})
    else:
        estados = GRUPOS[grupo][1]
        cond = f" AND a.estado IN ({','.join('?' * len(estados))})"
        total = tdb.execute(f"SELECT COUNT(*) {base}{filtro}{cond}", alcance_p + params + list(estados)).fetchone()[0]
        total_pages = max(1, math.ceil(total / PER_PAGE))
        page = min(page, total_pages)
        orden = ("COALESCE(a.fecha_cierre, a.fecha_alta) DESC" if grupo != "abiertos" else
                 "CASE a.prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, a.fecha_alta DESC")
        if orden_busqueda:
            # Los ids vienen del buscador (enteros), ya ordenados por parecido
            orden = "CASE a.id " + " ".join(f"WHEN {int(i)} THEN {n}" for n, i in enumerate(orden_busqueda)) + " END"
        filas = tdb.execute(
            f"{columnas}{base}{filtro}{cond} ORDER BY {orden} LIMIT ? OFFSET ?",
            alcance_p + params + list(estados) + [PER_PAGE, (page - 1) * PER_PAGE],
        ).fetchall()
        asuntos = _decorar(filas, tdb, usuarios)
        if orden_busqueda:
            # Lo que más se parece a la búsqueda, arriba
            pos = {i: n for n, i in enumerate(orden_busqueda)}
            asuntos.sort(key=lambda a: pos.get(a["id"], 10**6))
        elif grupo == "abiertos":
            # Lo que vence primero, arriba
            asuntos.sort(key=lambda a: (a["proximo"]["dias"] if a["proximo"] else 10**6))
        if asuntos:
            marcas = ",".join("?" * len(asuntos))
            ids = [a["id"] for a in asuntos]
            docs = dict(tdb.execute(
                f"""SELECT asunto_id, COUNT(*) FROM (SELECT asunto_id FROM documentos WHERE asunto_id IN ({marcas})
                    UNION ALL SELECT asunto_id FROM asunto_sentencias WHERE asunto_id IN ({marcas})) GROUP BY asunto_id""",
                ids * 2).fetchall())
            for a in asuntos:
                a["documentos"] = docs.get(a["id"], 0)

    # Los conteos de las pestañas respetan la búsqueda y los filtros
    conteos = {k: tdb.execute(
        f"SELECT COUNT(*) {base}{filtro} AND a.estado IN ({','.join('?' * len(v[1]))})", alcance_p + params + list(v[1])
    ).fetchone()[0] for k, v in GRUPOS.items()}

    actuales = {"q": q, "grupo": "" if grupo == "abiertos" else grupo, "tipo": tipo, "abogado": abogado,
                "atencion": atencion, "vista": "" if vista == "carpetas" else vista}

    def liga(**cambios):
        datos = {**actuales, **cambios}
        return url_for("asuntos.index", **{k: v for k, v in datos.items() if v not in (None, "", 0)})

    return render_template(
        "asuntos/index.html", liga=liga,
        asuntos=asuntos, tablero=columnas_tablero, resumen=resumen, conteos=conteos,
        grupos=GRUPOS, grupo=grupo, vista=vista, q=q, tipo=tipo, abogado=abogado, atencion=atencion,
        total=total, page=page, total_pages=total_pages,
        tipos_derecho=TIPOS_DERECHO, abogados=_get_abogados() if not _solo_lo_suyo() else [],
        resultados=precedentes.RESULTADOS, estados=ESTADOS,
        chips=chips, sugerencia=sugerencia,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  NUEVO
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/nuevo", methods=["GET", "POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def nuevo():
    tdb      = g.tenant_db
    abogados = _get_abogados()
    clientes = [dict(r) for r in tdb.execute(
        """SELECT id, tipo, nombre, apellidos, razon_social
           FROM clientes WHERE activo=1 ORDER BY COALESCE(razon_social, nombre) LIMIT 500"""
    ).fetchall()]

    def pantalla(err=None, form=None):
        return render_template(
            "asuntos/nuevo.html", err=err, form=form or {},
            tipos_derecho=TIPOS_DERECHO, abogados=abogados, clientes=clientes, plantillas=PLANTILLAS,
            auto_num=_auto_numero(tdb), today_str=_hoy().isoformat(), sin_juicio=SIN_JUICIO,
            extensiones=sorted(boveda.EXTENSIONES),
            cliente_inicial=_safe_int(request.args.get("cliente_id") or request.args.get("cliente")),
        )

    if request.method == "GET":
        return pantalla()

    f = request.form
    titulo = (f.get("titulo") or "").strip()
    if not titulo:
        return pantalla("Escribe un título para el asunto.", f)
    tipo = f.get("tipo_asunto") if f.get("tipo_asunto") in TIPOS_DERECHO else "otro"
    estado = f.get("estado") if f.get("estado") in ("por_iniciar", "activo") else "activo"
    prioridad = f.get("prioridad") if f.get("prioridad") in ("alta", "media", "baja") else "media"
    forma = f.get("forma_cobro") if f.get("forma_cobro") in ("fijo", "por_hora", "por_evento") else "fijo"
    texto = lambda k: (f.get(k) or "").strip() or None  # noqa: E731

    cur = tdb.execute(
        """INSERT INTO asuntos
           (numero_expediente, titulo, descripcion, tipo_asunto, sub_tipo,
            estado, prioridad, cliente_id, abogado_responsable,
            juzgado, numero_juicio, fecha_inicio, fecha_limite,
            monto_estimado, honorarios_pactados, forma_cobro,
            contrario_nombre, contrario_abogado, contrario_despacho, privado, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            texto("numero_expediente") or _auto_numero(tdb), titulo, texto("descripcion"), tipo,
            texto("sub_tipo"), estado, prioridad,
            _safe_int(f.get("cliente_id")), _safe_int(f.get("abogado_responsable")) or session.get("user_id"),
            texto("juzgado"), texto("numero_juicio"), texto("fecha_inicio") or _hoy().isoformat(),
            texto("fecha_limite"), _safe_float(f.get("monto_estimado")), _safe_float(f.get("honorarios_pactados")),
            forma, texto("contrario_nombre"), texto("contrario_abogado"), texto("contrario_despacho"),
            1 if f.get("privado") else 0, session.get("user_id"),
        ),
    )
    new_id = cur.lastrowid
    validos = {a["id"] for a in abogados}
    for cid in {_safe_int(x) for x in f.getlist("colaboradores")}:
        if cid in validos:
            tdb.execute("INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)", (new_id, cid))
    tdb.commit()
    _log("crear_asunto", new_id, {"titulo": titulo})

    # Documentos que vinieron en el paso 2
    rechazados = []
    for archivo in request.files.getlist("archivos"):
        if archivo and archivo.filename and not _guardar_documento(tdb, new_id, archivo,
                                                                   archivo.filename.rsplit(".", 1)[0]):
            rechazados.append(archivo.filename)
    if rechazados:
        flash(f"No se guardaron {len(rechazados)} archivo(s) con formato no permitido: {', '.join(rechazados[:3])}", "warning")
    return redirect(url_for("asuntos.detalle", id=new_id, nuevo=1))


@asuntos_bp.route("/ajax/plantilla/<tipo>")
@login_required
@tenant_required
def ajax_plantilla(tipo: str):
    return jsonify({"subtypes": TIPOS_DERECHO.get(tipo, {}).get("subtypes", []),
                    "plantilla": PLANTILLAS.get(tipo, {})})


@asuntos_bp.route("/ajax/auto-num")
@login_required
@tenant_required
def ajax_auto_num():
    return jsonify({"numero": _auto_numero(g.tenant_db)})


# ─────────────────────────────────────────────────────────────────────────────
#  FICHA DEL ASUNTO
# ─────────────────────────────────────────────────────────────────────────────

def _bitacora(tdb, id: int, asunto: dict, usuarios: dict) -> list[dict]:
    """Todo lo que ha pasado en el asunto, agrupado por día (hora de México)."""
    items = []

    def autor(uid):
        return usuarios.get(uid) or {"nombre": "Sistema", "iniciales": "HC", "color": "#60718D"}

    for a in tdb.execute("SELECT * FROM asunto_actividades WHERE asunto_id=?", (id,)).fetchall():
        items.append({"id": a["id"], "tipo": "actividad", "etiqueta": ACTIVIDAD_LABEL.get(a["tipo"], "Actividad"),
                      "titulo": a["titulo"], "texto": a["descripcion"] or "", "autor": autor(a["creado_por"]),
                      "momento": _local(a["fecha_alta"]), "fecha_evento": _fecha_corta(a["fecha"]),
                      "realizado": bool(a["realizado"])})
    for n in tdb.execute("SELECT * FROM asunto_notas WHERE asunto_id=?", (id,)).fetchall():
        items.append({"id": n["id"], "tipo": "nota", "etiqueta": "Nota", "titulo": "", "texto": n["contenido"],
                      "autor": autor(n["creado_por"]), "momento": _local(n["fecha"])})
    for d in tdb.execute("SELECT * FROM documentos WHERE asunto_id=?", (id,)).fetchall():
        items.append({"id": d["id"], "tipo": "documento", "etiqueta": "Documento",
                      "titulo": d["nombre"] or d["archivo_nombre"] or "Documento", "texto": "",
                      "autor": autor(d["subido_por"]), "momento": _local(d["fecha_subida"]),
                      "url": d["archivo_url"] if boveda.es_de_boveda(d["archivo_url"]) else None})
    for s in tdb.execute("SELECT * FROM asunto_sentencias WHERE asunto_id=?", (id,)).fetchall():
        items.append({"id": s["id"], "tipo": "sentencia", "etiqueta": SENTENCIA_LABEL.get(s["tipo"], "Resolución"),
                      "titulo": s["titulo"], "texto": s["contenido"] or "", "autor": autor(s["creado_por"]),
                      "momento": _local(s["fecha_alta"]), "sentido": precedentes.SENTIDOS.get(s["sentido"] or ""),
                      "url": s["archivo_url"] if boveda.es_de_boveda(s["archivo_url"]) else None})
    for h in tdb.execute(
        "SELECT * FROM actividad_log WHERE entidad='asunto' AND entidad_id=? AND accion IN "
        "('cambiar_estado_asunto','crear_asunto','archivar_asunto','resultado_asunto') ORDER BY fecha DESC LIMIT 40",
        (id,),
    ).fetchall():
        try:
            det = json.loads(h["detalles"] or "{}")
        except Exception:
            det = {}
        if h["accion"] == "crear_asunto":
            titulo = "Abrió el asunto"
        elif h["accion"] == "archivar_asunto":
            titulo = "Archivó el asunto"
        elif h["accion"] == "resultado_asunto":
            info = precedentes.RESULTADOS.get(det.get("resultado") or "")
            titulo = f"Registró el resultado: {info['label']}" if info else "Quitó el resultado"
        else:
            de = ESTADOS.get(det.get("de"), {}).get("label", det.get("de", "?"))
            a_ = ESTADOS.get(det.get("a") or det.get("estado"), {}).get("label", "?")
            titulo = f"Cambió el estado de {de} a {a_}"
        items.append({"id": h["id"], "tipo": "sistema", "etiqueta": "", "titulo": titulo, "texto": "",
                      "autor": autor(h["user_id"]), "momento": _local(h["fecha"])})

    items.sort(key=lambda x: x["momento"] or datetime.min, reverse=True)
    hoy, grupos, indice = _hoy(), [], {}
    for it in items:
        dia = it["momento"].date() if it["momento"] else hoy
        it["hora"] = it["momento"].strftime("%H:%M") if it["momento"] else ""
        if dia not in indice:
            diff = (hoy - dia).days
            nombre = f"{dia.day} de {_MESES[dia.month - 1]} de {dia.year}"
            etiqueta = "Hoy" if diff == 0 else "Ayer" if diff == 1 else nombre
            indice[dia] = {"etiqueta": etiqueta, "items": []}
            grupos.append(indice[dia])
        indice[dia]["items"].append(it)
    return grupos


@asuntos_bp.route("/<int:id>")
@login_required
@tenant_required
def detalle(id: int):
    tdb = g.tenant_db
    precedentes.asegurar(tdb)
    row = tdb.execute(
        """SELECT a.*, c.nombre || ' ' || COALESCE(c.apellidos,'') AS cliente_nombre,
                  c.razon_social AS cliente_razon, c.email AS cliente_email,
                  COALESCE(c.celular, c.telefono) AS cliente_tel, c.tipo AS cliente_tipo
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id WHERE a.id=?""",
        (id,),
    ).fetchone()
    if not row:
        abort(404)
    asunto = dict(row)
    asunto["cliente"] = _cliente_nombre(row)
    asunto["tipo_info"] = TIPOS_DERECHO.get(asunto.get("tipo_asunto") or "otro", TIPOS_DERECHO["otro"])
    asunto["estado_info"] = ESTADOS.get(asunto["estado"], ESTADOS["activo"])

    uid, rol = session.get("user_id"), session.get("role")
    usuarios = _usuarios()
    abogados = _get_abogados()
    responsable = usuarios.get(asunto.get("abogado_responsable"))
    col_ids = [r["user_id"] for r in tdb.execute(
        "SELECT user_id FROM asunto_colaboradores WHERE asunto_id=?", (id,)).fetchall()]
    colaboradores = [usuarios[c] for c in col_ids if c in usuarios and c != asunto.get("abogado_responsable")]

    abierto = asunto["estado"] in ABIERTOS
    proximo = _proximos(tdb, [id], {id: asunto["fecha_limite"]}).get(id) if abierto else None
    eventos = [dict(e) for e in tdb.execute(
        "SELECT id, titulo, tipo, fecha_inicio, lugar FROM eventos_calendario WHERE asunto_id=? "
        "AND estado='pendiente' AND date(fecha_inicio) >= date('now','-1 day') ORDER BY fecha_inicio LIMIT 5", (id,)
    ).fetchall()]
    for e in eventos:
        f = _fecha(e["fecha_inicio"])
        e["etiqueta"] = EVENTO_LABEL.get(e["tipo"], "Evento")
        e["dia"] = f"{f.day} {_MESES[f.month - 1][:3]}" if f else ""
        e["hora"] = (e["fecha_inicio"] or "")[11:16]
    pendientes = [dict(t) for t in tdb.execute(
        "SELECT id, titulo, tipo, fecha FROM asunto_actividades WHERE asunto_id=? AND realizado=0 "
        "ORDER BY fecha LIMIT 8", (id,)).fetchall()]
    # Una actividad que también se agendó en el calendario se muestra una sola vez.
    agendados = {(e["titulo"], (e["fecha_inicio"] or "")[:10]) for e in eventos}
    pendientes = [t for t in pendientes if (t["titulo"], (t["fecha"] or "")[:10]) not in agendados]
    for t in pendientes:
        f = _fecha(t["fecha"])
        t["etiqueta"] = ACTIVIDAD_LABEL.get(t["tipo"], "Pendiente")
        t["dias"] = (f - _hoy()).days if f else None
        t["cuando"] = _cuando(t["dias"]) if f else ""
        t["semaforo"] = _semaforo(t["dias"])

    documentos = [dict(d) for d in tdb.execute(
        "SELECT id, nombre, archivo_url, archivo_nombre, mime_type, tamanio, fecha_subida, subido_por "
        "FROM documentos WHERE asunto_id=? ORDER BY fecha_subida DESC", (id,)).fetchall()]
    sentencias = [dict(s) for s in tdb.execute(
        "SELECT * FROM asunto_sentencias WHERE asunto_id=? ORDER BY COALESCE(fecha_emision, fecha_alta) DESC",
        (id,)).fetchall()]
    for d in documentos:
        d["ext"] = (d["archivo_nombre"] or d["archivo_url"] or "").rsplit(".", 1)[-1].lower()[:4]
        d["dia"] = (_local(d["fecha_subida"]) or datetime.now()).strftime("%d/%m/%Y")
        d["autor"] = (usuarios.get(d["subido_por"]) or {}).get("corto", "")
    for s in sentencias:
        s["etiqueta"] = SENTENCIA_LABEL.get(s["tipo"], "Resolución")
        s["sentido_label"] = precedentes.SENTIDOS.get(s["sentido"] or "")

    horas = [dict(h) for h in tdb.execute(
        "SELECT * FROM asunto_horas WHERE asunto_id=? ORDER BY fecha DESC, id DESC", (id,)).fetchall()]
    for h in horas:
        h["autor"] = (usuarios.get(h["user_id"]) or {}).get("corto", "")
    horas_total = sum(_safe_float(h["horas"]) for h in horas)

    cobros = [dict(c) for c in tdb.execute(
        "SELECT * FROM cobros WHERE asunto_id=? AND estado != 'cancelado' ORDER BY fecha_emision DESC", (id,)
    ).fetchall()]
    facturado = sum(_safe_float(c.get("total")) or _safe_float(c.get("monto")) for c in cobros)
    cobrado = sum((_safe_float(c.get("total")) or _safe_float(c.get("monto"))) if c["estado"] == "pagado"
                  else _safe_float(c.get("monto_abonado")) for c in cobros)
    pactados = _safe_float(asunto.get("honorarios_pactados"))
    base_cobro = pactados or facturado
    dinero = {"pactados": pactados, "facturado": facturado, "cobrado": cobrado,
              "pendiente": max(base_cobro - cobrado, 0),
              "pct": int(min(cobrado / base_cobro * 100, 100)) if base_cobro else 0}

    etiquetas, todas_etiquetas = [], []
    try:
        etiquetas = [dict(r) for r in tdb.execute(
            "SELECT e.* FROM etiquetas e JOIN asunto_etiquetas ae ON ae.etiqueta_id=e.id "
            "WHERE ae.asunto_id=? ORDER BY e.nombre", (id,)).fetchall()]
        todas_etiquetas = [dict(r) for r in tdb.execute("SELECT * FROM etiquetas ORDER BY nombre").fetchall()]
    except Exception:
        pass

    ficha = None
    try:
        fila = tdb.execute("SELECT * FROM ficha_tecnica WHERE asunto_id=?", (id,)).fetchone()
        if fila:
            ficha = dict(fila)
            for k in ("argumentos_clave", "riesgos", "insights"):
                try:
                    ficha[k] = json.loads(ficha.get(k) or "[]")
                except Exception:
                    ficha[k] = []
    except Exception:
        pass

    inicio = _fecha(asunto.get("fecha_inicio"))
    fin = _fecha(asunto.get("fecha_cierre")) if not abierto else None
    dias_llevamos = ((fin or _hoy()) - inicio).days if inicio else None

    clientes = [dict(r) for r in tdb.execute(
        "SELECT id, COALESCE(razon_social, nombre || ' ' || COALESCE(apellidos,'')) AS nombre FROM clientes "
        "WHERE activo=1 OR id=? ORDER BY 2 LIMIT 500", (asunto.get("cliente_id") or 0,)).fetchall()]

    bitacora = _bitacora(tdb, id, asunto, usuarios)
    ultimo = _ultimos_movimientos(tdb, [id]).get(id)

    es_admin = rol in ("super_admin", "admin")
    return render_template(
        "asuntos/detalle.html",
        apertura=_fecha_corta(asunto.get("fecha_inicio") or asunto.get("fecha_alta")).split(" · ")[0],
        ultima_actividad=_fecha_corta(ultimo.isoformat()) if ultimo else "",
        asunto=asunto, responsable=responsable, colaboradores=colaboradores, abogados=abogados,
        bitacora=bitacora, proximo=proximo, eventos=eventos, pendientes=pendientes,
        documentos=documentos, sentencias=sentencias, horas=horas, horas_total=horas_total,
        cobros=cobros[:4], dinero=dinero, etiquetas=etiquetas, todas_etiquetas=todas_etiquetas, ficha=ficha,
        dias_llevamos=dias_llevamos, abierto=abierto, clientes=clientes,
        estados=ESTADOS, estados_menu=ESTADOS_MENU, tipos_derecho=TIPOS_DERECHO,
        tipos_actividad=TIPOS_ACTIVIDAD, actividad_label=ACTIVIDAD_LABEL,
        tipos_sentencia=TIPOS_SENTENCIA, sentencia_label=SENTENCIA_LABEL,
        resultados=precedentes.RESULTADOS, sentidos=precedentes.SENTIDOS,
        es_admin=es_admin,
        puede_editar=es_admin or uid == asunto.get("abogado_responsable") or uid in col_ids
                     or uid == asunto.get("creado_por"),
        recien_creado=request.args.get("nuevo") == "1",
        today_str=_hoy().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FICHA TÉCNICA (Nota 9)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/ficha", methods=["POST", "PUT"])
@login_required
@tenant_required
def guardar_ficha(id):
    """Create or update the strategic brief for a case."""
    tdb = g.tenant_db
    data = request.get_json(force=True)
    import json as _json

    existing = tdb.execute("SELECT id FROM ficha_tecnica WHERE asunto_id=?", (id,)).fetchone()
    if existing:
        tdb.execute(
            """UPDATE ficha_tecnica SET
                objetivo_cliente=?, via_procesal=?, justificacion_via=?,
                argumentos_clave=?, riesgos=?, probabilidad_exito=?,
                insights=?, updated_at=CURRENT_TIMESTAMP
               WHERE asunto_id=?""",
            (
                data.get("objetivo_cliente", ""),
                data.get("via_procesal", ""),
                data.get("justificacion_via", ""),
                _json.dumps(data.get("argumentos_clave", []), ensure_ascii=False),
                _json.dumps(data.get("riesgos", []), ensure_ascii=False),
                data.get("probabilidad_exito", "media"),
                _json.dumps(data.get("insights", []), ensure_ascii=False),
                id,
            ),
        )
    else:
        tdb.execute(
            """INSERT INTO ficha_tecnica
               (asunto_id, objetivo_cliente, via_procesal, justificacion_via,
                argumentos_clave, riesgos, probabilidad_exito, insights)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                id,
                data.get("objetivo_cliente", ""),
                data.get("via_procesal", ""),
                data.get("justificacion_via", ""),
                _json.dumps(data.get("argumentos_clave", []), ensure_ascii=False),
                _json.dumps(data.get("riesgos", []), ensure_ascii=False),
                data.get("probabilidad_exito", "media"),
                _json.dumps(data.get("insights", []), ensure_ascii=False),
            ),
        )
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ETIQUETAS (Nota 4)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/etiquetas", methods=["POST"])
@login_required
@tenant_required
def agregar_etiqueta(id):
    """Add a tag to a case. Creates the tag if it doesn't exist."""
    tdb = g.tenant_db
    data = request.get_json(force=True)
    nombre = (data.get("nombre") or "").strip()
    color = data.get("color") or "#142E59"
    if not (isinstance(color, str) and len(color) == 7 and color.startswith("#")):
        color = "#142E59"
    etiqueta_id = data.get("etiqueta_id")

    if not etiqueta_id and not nombre:
        return jsonify(ok=False, error="Nombre requerido"), 400

    if not etiqueta_id:
        # Check if tag with same name exists
        existing = tdb.execute("SELECT id FROM etiquetas WHERE nombre=?", (nombre,)).fetchone()
        if existing:
            etiqueta_id = existing["id"]
        else:
            cur = tdb.execute(
                "INSERT INTO etiquetas (nombre, color) VALUES (?,?)", (nombre, color)
            )
            etiqueta_id = cur.lastrowid

    # Link to asunto (ignore if already linked)
    try:
        tdb.execute(
            "INSERT OR IGNORE INTO asunto_etiquetas (asunto_id, etiqueta_id) VALUES (?,?)",
            (id, etiqueta_id),
        )
        tdb.commit()
    except Exception:
        pass

    tag = tdb.execute("SELECT * FROM etiquetas WHERE id=?", (etiqueta_id,)).fetchone()
    return jsonify(ok=True, etiqueta=dict(tag) if tag else {})


@asuntos_bp.route("/<int:id>/etiquetas/<int:eid>", methods=["DELETE"])
@login_required
@tenant_required
def quitar_etiqueta(id, eid):
    """Remove a tag from a case."""
    tdb = g.tenant_db
    tdb.execute(
        "DELETE FROM asunto_etiquetas WHERE asunto_id=? AND etiqueta_id=?", (id, eid)
    )
    tdb.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  EDICIÓN INLINE
# ─────────────────────────────────────────────────────────────────────────────

_EDITABLE = {
    "titulo", "descripcion", "tipo_asunto", "sub_tipo", "estado", "prioridad",
    "numero_expediente", "juzgado", "numero_juicio", "fecha_inicio", "fecha_limite",
    "fecha_cierre", "monto_estimado", "honorarios_pactados", "forma_cobro",
    "contrario_nombre", "contrario_abogado", "contrario_despacho",
    "cliente_id", "abogado_responsable", "notas",
}


@asuntos_bp.route("/<int:id>/editar-campo", methods=["POST"])
@login_required
@tenant_required
def editar_campo(id: int):
    tdb  = g.tenant_db
    row  = tdb.execute("SELECT id FROM asuntos WHERE id=?", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404

    payload = request.json or {}
    campo   = payload.get("campo", "")
    valor   = payload.get("valor")

    # Special case: multi-field update (edit modal)
    if campo == "multi":
        updates = []
        for k, v in (valor or {}).items():
            if k in _EDITABLE:
                if k in ("monto_estimado", "honorarios_pactados"):
                    v = _safe_float(v)
                elif k in ("cliente_id", "abogado_responsable"):
                    v = _safe_int(v)
                updates.append((k, v))
        for k, v in updates:
            tdb.execute(f"UPDATE asuntos SET {k}=? WHERE id=?", (v, id))
        if updates:
            tdb.commit()
            _log("actualizar_asunto", id, {"campos": [k for k, _ in updates]})
        return jsonify(ok=True)

    # Special case: add collaborator
    if campo == "colaborador_add":
        uid = _safe_int(valor)
        if uid:
            tdb.execute(
                "INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)",
                (id, uid),
            )
            tdb.commit()
            _log("agregar_colaborador", id, {"user_id": uid})
        return jsonify(ok=True)

    if campo not in _EDITABLE:
        return jsonify(ok=False, error="Campo no permitido"), 400

    if campo in ("monto_estimado", "honorarios_pactados"):
        valor = _safe_float(valor)
    elif campo in ("cliente_id", "abogado_responsable"):
        valor = _safe_int(valor)

    old_val = tdb.execute(f"SELECT {campo} FROM asuntos WHERE id=?", (id,)).fetchone()
    tdb.execute(f"UPDATE asuntos SET {campo}=? WHERE id=?", (valor, id))
    tdb.commit()
    _log("actualizar_asunto", id, {"campo": campo, "valor": valor})
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  CAMBIAR ESTADO (Kanban drag & drop)
# ─────────────────────────────────────────────────────────────────────────────

VALID_STATES = set(ESTADOS_MENU) | {"completado"}


@asuntos_bp.route("/<int:id>/estado", methods=["POST", "PATCH"])
@login_required
@tenant_required
def cambiar_estado(id: int):
    nuevo_estado = (request.json or {}).get("estado", "")
    if nuevo_estado not in VALID_STATES:
        return jsonify(ok=False, error="Estado inválido"), 400
    old = g.tenant_db.execute("SELECT estado FROM asuntos WHERE id=?", (id,)).fetchone()
    if not old:
        return jsonify(ok=False, error="No encontrado"), 404
    old_estado = old["estado"]
    if nuevo_estado == old_estado:
        return jsonify(ok=True, estado=nuevo_estado, info=ESTADOS[nuevo_estado])
    g.tenant_db.execute("UPDATE asuntos SET estado=? WHERE id=?", (nuevo_estado, id))
    if nuevo_estado in CONCLUIDOS:
        g.tenant_db.execute("UPDATE asuntos SET fecha_cierre=COALESCE(fecha_cierre, ?) WHERE id=?",
                            (_hoy().isoformat(), id))
    elif nuevo_estado in ABIERTOS:
        g.tenant_db.execute("UPDATE asuntos SET fecha_cierre=NULL WHERE id=?", (id,))
    g.tenant_db.commit()
    _log("cambiar_estado_asunto", id, {
        "de": old_estado,
        "a": nuevo_estado,
        "estado": nuevo_estado,
    })
    return jsonify(ok=True, estado=nuevo_estado, info=ESTADOS[nuevo_estado])


# ─────────────────────────────────────────────────────────────────────────────
#  ELIMINAR (archiva)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/eliminar", methods=["POST"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "abogado")
def eliminar(id: int):
    tdb = g.tenant_db
    row = tdb.execute("SELECT titulo FROM asuntos WHERE id=?", (id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="No encontrado"), 404
    tdb.execute("UPDATE asuntos SET estado='archivado' WHERE id=?", (id,))
    tdb.commit()
    _log("archivar_asunto", id, {"titulo": row["titulo"]})
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True)
    return redirect(url_for("asuntos.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  GESTIÓN DE EQUIPO (punto 6)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/equipo/<int:user_id>", methods=["PATCH"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "socio")
def editar_equipo(id: int, user_id: int):
    """Editar rol de un miembro o cambiar responsable."""
    tdb = g.tenant_db
    data = request.json or {}
    accion = data.get("accion", "")

    if accion == "cambiar_responsable":
        # El actual responsable baja a colaborador
        asunto = tdb.execute("SELECT abogado_responsable FROM asuntos WHERE id=?", (id,)).fetchone()
        if asunto:
            old_resp = asunto["abogado_responsable"]
            if old_resp and old_resp != user_id:
                tdb.execute(
                    "INSERT OR IGNORE INTO asunto_colaboradores (asunto_id, user_id) VALUES (?,?)",
                    (id, old_resp),
                )
        tdb.execute("UPDATE asuntos SET abogado_responsable=? WHERE id=?", (user_id, id))
        tdb.execute("DELETE FROM asunto_colaboradores WHERE asunto_id=? AND user_id=?", (id, user_id))
        tdb.commit()
        _log("cambiar_responsable", id, {"nuevo_responsable": user_id})
        return jsonify(ok=True)

    return jsonify(ok=False, error="Acción no válida"), 400


@asuntos_bp.route("/<int:id>/equipo/<int:user_id>", methods=["DELETE"])
@login_required
@tenant_required
@role_required("super_admin", "admin", "socio")
def quitar_equipo(id: int, user_id: int):
    """Quitar colaborador del asunto."""
    tdb = g.tenant_db
    tdb.execute(
        "DELETE FROM asunto_colaboradores WHERE asunto_id=? AND user_id=?",
        (id, user_id),
    )
    tdb.commit()
    _log("quitar_colaborador", id, {"user_id": user_id})
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  ACTIVIDADES
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/actividades", methods=["POST"])
@login_required
@tenant_required
def agregar_actividad(id: int):
    data   = request.json or request.form
    titulo = (data.get("titulo") or "").strip()
    fecha  = (data.get("fecha") or "").strip()
    if not titulo or not fecha:
        return jsonify(ok=False, error="Título y fecha son requeridos"), 400

    tipo = data.get("tipo", "nota")
    if tipo not in TIPOS_ACTIVIDAD:
        tipo = "nota"

    cur = g.tenant_db.execute(
        """INSERT INTO asunto_actividades
           (asunto_id, tipo, titulo, descripcion, fecha, recordatorio_dias, creado_por)
           VALUES (?,?,?,?,?,?,?)""",
        (
            id, tipo, titulo,
            (data.get("descripcion") or "").strip() or None,
            fecha,
            _safe_int(data.get("recordatorio_dias")),
            session.get("user_id"),
        ),
    )
    g.tenant_db.commit()

    # Opcionalmente crear evento en calendario
    if data.get("crear_evento"):
        try:
            g.tenant_db.execute(
                """INSERT INTO eventos_calendario
                   (titulo, tipo, asunto_id, fecha_inicio, descripcion, creado_por)
                   VALUES (?,?,?,?,?,?)""",
                (titulo, tipo if tipo in ("audiencia","reunion","plazo","tarea") else "otro",
                 id, fecha, data.get("descripcion") or None, session.get("user_id")),
            )
            g.tenant_db.commit()
        except Exception:
            pass

    _log("agregar_actividad", id, {"titulo": titulo})
    return jsonify(
        ok=True, id=cur.lastrowid,
        fecha=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


@asuntos_bp.route("/<int:id>/actividades/<int:act_id>/realizado", methods=["POST"])
@login_required
@tenant_required
def marcar_realizado(id: int, act_id: int):
    realizado = 1 if (request.json or {}).get("realizado") else 0
    g.tenant_db.execute(
        "UPDATE asunto_actividades SET realizado=? WHERE id=? AND asunto_id=?",
        (realizado, act_id, id),
    )
    g.tenant_db.commit()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  SENTENCIAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/sentencias", methods=["POST"])
@login_required
@tenant_required
def agregar_sentencia(id: int):
    titulo = (request.form.get("titulo") or "").strip()
    if not titulo:
        return jsonify(ok=False, error="Título requerido"), 400
    tipo = request.form.get("tipo", "sentencia")
    if tipo not in TIPOS_SENTENCIA:
        tipo = "sentencia"
    sentido = request.form.get("sentido") if request.form.get("sentido") in precedentes.SENTIDOS else None

    archivo_url = archivo_nombre = None
    f = request.files.get("archivo")
    if f and f.filename:
        info = boveda.guardar(f, "asuntos", id, "sentencias")
        if not info:
            return jsonify(ok=False, error="Tipo de archivo no permitido"), 400
        archivo_nombre = info["nombre"]
        archivo_url    = info["url"]

    cur = g.tenant_db.execute(
        """INSERT INTO asunto_sentencias
           (asunto_id, titulo, tipo, sentido, contenido, fecha_emision, archivo_url, archivo_nombre, creado_por)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (id, titulo, tipo, sentido,
         (request.form.get("contenido") or "").strip() or None,
         request.form.get("fecha_emision") or None,
         archivo_url, archivo_nombre, session.get("user_id")),
    )
    g.tenant_db.commit()
    if archivo_url:
        lector.encolar(archivo_url, titulo, "sentencia", cur.lastrowid, asunto_id=id)
    return jsonify(ok=True, id=cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
#  HORAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/horas", methods=["POST"])
@login_required
@tenant_required
def registrar_horas(id: int):
    data  = request.json or {}
    horas = _safe_float(data.get("horas"))
    if not horas or horas <= 0:
        return jsonify(ok=False, error="Horas inválidas"), 400

    cur = g.tenant_db.execute(
        "INSERT INTO asunto_horas (asunto_id, user_id, horas, descripcion, fecha) VALUES (?,?,?,?,?)",
        (id, session.get("user_id"), horas,
         (data.get("descripcion") or "").strip() or None,
         data.get("fecha") or date.today().isoformat()),
    )
    g.tenant_db.commit()
    total = g.tenant_db.execute(
        "SELECT COALESCE(SUM(horas),0) FROM asunto_horas WHERE asunto_id=?", (id,)
    ).fetchone()[0]
    return jsonify(ok=True, id=cur.lastrowid, total_horas=total)


# ─────────────────────────────────────────────────────────────────────────────
#  NOTAS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/notas", methods=["POST"])
@login_required
@tenant_required
def agregar_nota(id: int):
    data      = request.json or {}
    contenido = (data.get("contenido") or "").strip()
    if not contenido:
        return jsonify(ok=False, error="Contenido requerido"), 400

    menciones = json.dumps(data.get("menciones", []))
    cur = g.tenant_db.execute(
        "INSERT INTO asunto_notas (asunto_id, contenido, menciones, creado_por) VALUES (?,?,?,?)",
        (id, contenido, menciones, session.get("user_id")),
    )
    g.tenant_db.commit()
    return jsonify(ok=True, id=cur.lastrowid, fecha=datetime.now().strftime("%Y-%m-%d %H:%M"))


# ─────────────────────────────────────────────────────────────────────────────
#  DOCUMENTOS
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/documentos", methods=["POST"])
@login_required
@tenant_required
def subir_documento(id: int):
    tdb  = g.tenant_db
    nombre = (request.form.get("nombre") or "").strip()
    f = request.files.get("archivo")
    if not f or not f.filename:
        return jsonify(ok=False, error="Archivo requerido"), 400

    doc_id = _guardar_documento(tdb, id, f, nombre)
    if not doc_id:
        return jsonify(ok=False, error="Tipo de archivo no permitido"), 400
    return jsonify(ok=True, id=doc_id)


def _guardar_documento(tdb, id: int, f, nombre: str = "") -> int | None:
    """Cifra el archivo en la bóveda, lo registra en el expediente y lo manda a leer (OCR e índice)."""
    info = boveda.guardar(f, "asuntos", id)
    if not info:
        return None
    cur = tdb.execute(
        """INSERT INTO documentos
           (asunto_id, nombre, archivo_url, archivo_nombre, tamanio, mime_type, subido_por)
           VALUES (?,?,?,?,?,?,?)""",
        (id, nombre or info["nombre"], info["url"], info["nombre"],
         info["tamanio"], info["mime"], session.get("user_id")),
    )
    tdb.commit()
    lector.encolar(info["url"], nombre or info["nombre"], "documento", cur.lastrowid, asunto_id=id)
    _log("subir_documento", id, {"nombre": nombre or f.filename})
    return cur.lastrowid


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIAR CORREO (desde asunto)
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/enviar-correo", methods=["POST"])
@login_required
@tenant_required
def enviar_correo_asunto(id: int):
    """Envía correo vinculado al asunto y lo registra."""
    para = (request.form.get("para") or "").strip()
    asunto_correo = (request.form.get("asunto_correo") or "").strip()
    cuerpo = (request.form.get("cuerpo") or "").strip()
    cc = (request.form.get("cc") or "").strip()

    if not para or not asunto_correo or not cuerpo:
        return jsonify(ok=False, error="Faltan campos"), 400

    # Sender info
    gdb = g.global_db
    tid = g.tenant["id"]
    sender = gdb.execute(
        "SELECT nombre, apellidos, email FROM tenant_users WHERE id=?",
        (session.get("user_id"),),
    ).fetchone()
    sender_name = f"{sender['nombre']} {sender['apellidos'] or ''}".strip() if sender else ""
    sender_email = sender["email"] if sender else ""
    despacho = g.tenant["nombre_despacho"]

    asunto_row = g.tenant_db.execute("SELECT titulo FROM asuntos WHERE id=?", (id,)).fetchone()
    asunto_titulo = asunto_row["titulo"] if asunto_row else ""

    despacho_h, titulo_h, asunto_h = html.escape(despacho), html.escape(asunto_titulo), html.escape(asunto_correo)
    cuerpo_h, sender_h = html.escape(cuerpo), html.escape(sender_name)
    html_body = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E5E2DB">
      <div style="background:linear-gradient(135deg,#11284F,#334155);padding:24px 28px">
        <div style="font-size:18px;font-weight:800;color:#fff">{despacho_h}</div>
        <div style="font-size:11px;color:#9AA4B5;margin-top:3px">Ref: {titulo_h}</div>
      </div>
      <div style="padding:28px">
        <div style="font-size:15px;font-weight:700;color:#11284F;margin-bottom:16px">{asunto_h}</div>
        <div style="font-size:14px;color:#3B4A61;line-height:1.7;white-space:pre-wrap">{cuerpo_h}</div>
      </div>
      <div style="background:#FAF8F4;padding:16px 28px;border-top:1px solid #E5E2DB">
        <div style="font-size:12px;color:#60718D">Enviado por <strong>{sender_h}</strong></div>
        <div style="font-size:10px;color:#9AA4B5;margin-top:2px">Habeas Corpus · {date.today().strftime('%d/%m/%Y')}</div>
      </div>
    </div>"""

    try:
        from app import mail
        from flask_mail import Message
        recipients = [e.strip() for e in para.replace(";", ",").split(",") if "@" in e.strip()]
        cc_list = [e.strip() for e in cc.replace(";", ",").split(",") if "@" in e.strip()] if cc else []
        msg = Message(
            subject=f"{asunto_correo} — {despacho}",
            recipients=recipients,
            cc=cc_list or None,
            html=html_body,
            reply_to=sender_email or None,
        )
        mail.send(msg)
        return jsonify(ok=True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify(ok=False, error="No se pudo enviar el correo. Revisa la configuración de correo."), 500


# ─────────────────────────────────────────────────────────────────────────────
#  EXPORT PDF
# ─────────────────────────────────────────────────────────────────────────────

@asuntos_bp.route("/<int:id>/pdf")
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
    row = tdb.execute(
        """SELECT a.*, c.nombre||' '||COALESCE(c.apellidos,'') as cliente_nombre,
                  c.razon_social
           FROM asuntos a LEFT JOIN clientes c ON a.cliente_id=c.id WHERE a.id=?""",
        (id,),
    ).fetchone()
    if not row:
        return redirect(url_for("asuntos.index"))

    a      = dict(row)
    tenant = g.tenant
    abogados = _get_abogados()
    abogado_nombre = next(
        (f"{ab['nombre']} {ab['apellidos']}" for ab in abogados if ab["id"] == a.get("abogado_responsable")),
        "—"
    )
    tipo_info = TIPOS_DERECHO.get(a.get("tipo_asunto") or "otro", TIPOS_DERECHO["otro"])
    actividades = tdb.execute(
        "SELECT * FROM asunto_actividades WHERE asunto_id=? ORDER BY fecha ASC LIMIT 20", (id,)
    ).fetchall()

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    ACCENT = colors.HexColor(tipo_info["color"])
    MUTED  = colors.HexColor("#8E99AB")

    title_s   = ParagraphStyle("t", parent=styles["Heading1"],  fontSize=18, textColor=ACCENT, spaceAfter=4)
    sub_s     = ParagraphStyle("s", parent=styles["Normal"],    fontSize=9, textColor=MUTED, spaceAfter=10)
    section_s = ParagraphStyle("sec", parent=styles["Heading2"], fontSize=10, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
    footer_s  = ParagraphStyle("f", parent=styles["Normal"],    fontSize=7, textColor=MUTED, alignment=TA_CENTER)

    story = [
        Paragraph(a["titulo"], title_s),
        Paragraph(f"{tipo_info['label']} · {a.get('numero_expediente','—')} · {tenant['nombre_despacho'] if tenant else 'Habeas Corpus'}", sub_s),
        HRFlowable(width="100%", color=ACCENT, thickness=1),
        Spacer(1, 0.3*cm),
    ]

    def kv(rows_data):
        t = Table(rows_data, colWidths=[5*cm, 12*cm])
        t.setStyle(TableStyle([
            ("FONTSIZE", (0,0),(-1,-1), 8),
            ("TEXTCOLOR",(0,0),(0,-1), MUTED),
            ("FONTNAME", (0,0),(0,-1),"Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white, colors.HexColor("#FAF8F4")]),
            ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E2DB")),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(t)

    story.append(Paragraph("Información general", section_s))
    kv([
        ["Expediente",  a.get("numero_expediente") or "—"],
        ["Tipo",        tipo_info["label"]],
        ["Estado",      ESTADOS.get(a.get("estado"), {}).get("label", "—")],
        ["Prioridad",   a.get("prioridad","—").title()],
        ["Cliente",     (a.get("razon_social") or a.get("cliente_nombre") or "—").strip()],
        ["Abogado",     abogado_nombre],
        ["Juzgado",     a.get("juzgado") or "—"],
        ["No. juicio",  a.get("numero_juicio") or "—"],
        ["Inicio",      (a.get("fecha_inicio") or "—")[:10]],
        ["Fecha límite", (a.get("fecha_limite") or "—")[:10]],
        ["Honorarios",  f"${(a.get('honorarios_pactados') or 0):,.0f} MXN"],
        ["Valor litigio", f"${(a.get('monto_estimado') or 0):,.0f} MXN"],
    ])

    if a.get("contrario_nombre"):
        story.append(Paragraph("Contraparte", section_s))
        kv([
            ["Nombre", a.get("contrario_nombre","—")],
            ["Abogado", a.get("contrario_abogado","—")],
            ["Despacho", a.get("contrario_despacho","—")],
        ])

    if a.get("descripcion"):
        story.append(Paragraph("Descripción / Hechos", section_s))
        story.append(Paragraph(a["descripcion"][:1000], ParagraphStyle("d", parent=styles["Normal"], fontSize=8, leading=12)))

    if actividades:
        story.append(Paragraph("Actividades / Timeline", section_s))
        ah = [["Fecha","Tipo","Actividad","Estado"]]
        for act in actividades:
            ah.append([
                str(act["fecha"])[:10], act["tipo"].title(), act["titulo"][:50],
                "✓ Realizado" if act["realizado"] else "Pendiente"
            ])
        t = Table(ah, colWidths=[2.5*cm, 2.5*cm, 10*cm, 2*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),ACCENT),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#E5E2DB")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#FAF8F4")]),
        ]))
        story.append(t)

    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"Generado el {date.today().strftime('%d/%m/%Y')} · Habeas Corpus", footer_s))
    doc.build(story)
    buf.seek(0)
    safe = a["titulo"].replace(" ","_")[:30]
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"asunto_{safe}.pdf")
