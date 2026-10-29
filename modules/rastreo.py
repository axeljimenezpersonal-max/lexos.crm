"""
Habeas Corpus — modules/rastreo.py
Medición propia del uso del sistema: qué secciones se abren, de dónde vienen, cuánto tiempo
se quedan y qué pestañas usan. Se guarda en la base global, sin terceros, y solo si la persona
aceptó las cookies de medición. No se guardan títulos de expedientes ni lo que se escribe.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from flask import (Blueprint, abort, current_app, render_template, request,
                   session)
from werkzeug.exceptions import HTTPException

from modules.auth import login_required

rastreo_bp = Blueprint("rastreo", __name__)

CONSENTIMIENTO = "hc_consent"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS analitica (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vid         TEXT,
    visitante   TEXT,
    sesion      TEXT,
    user_id     INTEGER,
    usuario     TEXT,
    despacho    TEXT,
    tipo        TEXT NOT NULL,
    endpoint    TEXT,
    seccion     TEXT,
    ruta        TEXT,
    desde       TEXT,
    detalle     TEXT,
    dispositivo TEXT,
    ancho       INTEGER,
    duracion    INTEGER,
    fecha       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_analitica_fecha ON analitica(fecha);
CREATE INDEX IF NOT EXISTS idx_analitica_vid ON analitica(vid);
"""

# Nombre que se muestra para cada pantalla
SECCIONES = {
    "auth.landing": "Página principal", "auth.login": "Iniciar sesión", "auth.register_tenant": "Registro de despacho",
    "legal.terminos": "Términos", "legal.privacidad": "Aviso de privacidad", "legal.aceptar": "Aceptar términos",
    "dashboard.index": "Inicio", "asuntos.index": "Expedientes", "asuntos.detalle": "Expediente",
    "asuntos.nuevo": "Nuevo expediente", "clientes.index": "Personas", "clientes.detalle": "Persona",
    "contrapartes.index": "Contrapartes", "calendario.index": "Calendario", "biblioteca.index": "Documentos",
    "cobranzas.index": "Cobranza", "asistente.index": "Dictum · Preguntar", "investigacion.index": "Dictum · Investigación",
    "corpus.index": "Dictum · Base legal", "insights.index": "Dictum · Precedentes", "buscador.resultados": "Búsqueda",
    "auth.perfil": "Mi perfil", "revision.index": "Comentarios de revisión", "rastreo.panel": "Uso de la plataforma",
    "lector.index": "Memoria de documentos", "estadisticas.index": "Estadísticas", "reportes.index": "Reportes",
}
SUBSECCION = {"biblioteca.index": ("seccion", {"expedientes": "De expedientes", "estudio": "Estudio",
                                               "formatos": "Formatos", "acuerdos": "Acuerdos y audiencias"})}
DISPOSITIVOS = ("telefono", "tablet", "computadora")


def _gdb():
    from app import get_global_db
    db = get_global_db()
    db.executescript(ESQUEMA)
    return db


def _seccion(url: str) -> tuple[str, str, str]:
    """(endpoint, nombre legible, ruta sin números) de una dirección de este mismo sitio."""
    partes = urlsplit(url or "")
    if partes.netloc and partes.netloc != request.host:
        return "", "Otro sitio", ""
    ruta = partes.path or "/"
    try:
        endpoint = current_app.url_map.bind(request.host).match(ruta, method="GET")[0]
    except HTTPException:
        endpoint = ""
    nombre = SECCIONES.get(endpoint, endpoint.replace(".index", "").replace("_", " ").capitalize() if endpoint else "Desconocida")
    if endpoint in SUBSECCION:
        param, nombres = SUBSECCION[endpoint]
        valor = (parse_qs(partes.query).get(param) or [""])[0]
        if valor in nombres:
            nombre = f"{nombre} · {nombres[valor]}"
    return endpoint, nombre, re.sub(r"/\d+", "/:id", ruta)[:120]


def _corto(valor, largo: int = 60) -> str:
    return str(valor or "")[:largo]


@rastreo_bp.route("/r", methods=["POST"])
def registrar():
    """Recibe los eventos del navegador (navigator.sendBeacon). Siempre responde 204."""
    if request.cookies.get(CONSENTIMIENTO) != "si" or (request.content_length or 0) > 4000:
        return "", 204
    d = request.get_json(force=True, silent=True) or {}
    tipo = d.get("tipo")
    if tipo not in ("vista", "tiempo", "pestana", "busqueda", "clic"):
        return "", 204
    db = _gdb()
    if tipo == "tiempo":
        duracion = max(0, min(int(d.get("ms") or 0), 4 * 3600 * 1000))
        db.execute("UPDATE analitica SET duracion=? WHERE vid=? AND tipo='vista'", (duracion, _corto(d.get("vid"), 40)))
        db.commit()
        return "", 204

    endpoint, seccion, ruta = _seccion(d.get("url"))
    desde = _seccion(d.get("desde"))[1] if d.get("desde") else "Entrada directa"
    ancho = int(d.get("ancho") or 0)
    dispositivo = "telefono" if ancho and ancho < 600 else ("tablet" if ancho and ancho < 1100 else "computadora")
    u = session.get("user") or {}
    db.execute(
        """INSERT INTO analitica (vid, visitante, sesion, user_id, usuario, despacho, tipo, endpoint, seccion, ruta,
                                  desde, detalle, dispositivo, ancho)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_corto(d.get("vid"), 40), _corto(d.get("visitante"), 40), _corto(d.get("sesion"), 40),
         session.get("user_id"), _corto(f"{u.get('nombre', '')} {u.get('apellidos') or ''}".strip(), 80) or None,
         session.get("tenant_slug"), tipo, endpoint, seccion, ruta, desde if tipo == "vista" else None,
         _corto(d.get("detalle"), 40) or None, dispositivo, ancho or None))
    db.commit()
    return "", 204


def puede_ver_panel() -> bool:
    """El panel es para los dueños de la plataforma (PLATAFORMA_ADMINS) y, en el sitio de revisión, sus administradores."""
    u = session.get("user") or {}
    admins = {e.strip().lower() for e in (current_app.config.get("PLATAFORMA_ADMINS") or "").split(",") if e.strip()}
    if (u.get("email") or "").lower() in admins:
        return True
    return bool(current_app.config.get("REVISION_ACTIVA")) and session.get("role") in ("super_admin", "admin")


@rastreo_bp.route("/uso/")
@login_required
def panel():
    if not puede_ver_panel():
        abort(404)
    db = _gdb()
    dias = request.args.get("dias", 30, type=int)
    dias = dias if dias in (7, 30, 90) else 30
    despacho = (request.args.get("despacho") or "").strip()
    desde = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    w, p = "fecha >= ?", [desde]
    if despacho:
        w += " AND despacho = ?"
        p.append(despacho)
    vistas = f"{w} AND tipo='vista'"

    def uno(sql, extra=()):
        return db.execute(sql, (*p, *extra)).fetchone()

    def varios(sql, extra=()):
        return [dict(r) for r in db.execute(sql, (*p, *extra)).fetchall()]

    tot = uno(f"""SELECT COUNT(*) AS vistas, COUNT(DISTINCT visitante) AS visitantes, COUNT(DISTINCT sesion) AS sesiones,
                         COUNT(DISTINCT user_id) AS usuarios, AVG(NULLIF(duracion, 0)) AS ms
                  FROM analitica WHERE {vistas}""")
    por_sesion = uno(f"""SELECT AVG(n) AS paginas, AVG(ms) AS ms FROM (
                           SELECT COUNT(*) AS n, SUM(COALESCE(duracion, 0)) AS ms FROM analitica WHERE {vistas} GROUP BY sesion)""")
    secciones = varios(f"""SELECT seccion, COUNT(*) AS vistas, COUNT(DISTINCT visitante) AS personas,
                                  AVG(NULLIF(duracion, 0)) AS ms
                           FROM analitica WHERE {vistas} GROUP BY seccion ORDER BY vistas DESC LIMIT 15""")
    flujos = varios(f"""SELECT desde, seccion, COUNT(*) AS n FROM analitica
                        WHERE {vistas} AND desde IS NOT NULL AND desde != seccion
                        GROUP BY desde, seccion ORDER BY n DESC LIMIT 12""")
    entradas = varios(f"""SELECT seccion, COUNT(*) AS n FROM analitica a
                          WHERE {vistas} AND id = (SELECT MIN(id) FROM analitica b WHERE b.sesion = a.sesion AND b.tipo='vista')
                          GROUP BY seccion ORDER BY n DESC LIMIT 6""")
    salidas = varios(f"""SELECT seccion, COUNT(*) AS n FROM analitica a
                         WHERE {vistas} AND id = (SELECT MAX(id) FROM analitica b WHERE b.sesion = a.sesion AND b.tipo='vista')
                         GROUP BY seccion ORDER BY n DESC LIMIT 6""")
    acciones = varios(f"""SELECT tipo, seccion, detalle, COUNT(*) AS n FROM analitica
                          WHERE {w} AND tipo IN ('pestana', 'busqueda', 'clic')
                          GROUP BY tipo, seccion, detalle ORDER BY n DESC LIMIT 15""")
    dispositivos = {r["dispositivo"]: r["n"] for r in varios(
        f"SELECT dispositivo, COUNT(DISTINCT sesion) AS n FROM analitica WHERE {vistas} GROUP BY dispositivo")}
    por_despacho = varios(f"""SELECT COALESCE(despacho, 'Sin sesión (páginas públicas)') AS despacho, COUNT(*) AS vistas,
                                     COUNT(DISTINCT user_id) AS usuarios, MAX(fecha) AS ultima
                              FROM analitica WHERE {vistas} GROUP BY despacho ORDER BY vistas DESC LIMIT 12""")
    personas = varios(f"""SELECT usuario, despacho, COUNT(*) AS vistas, COUNT(DISTINCT sesion) AS sesiones, MAX(fecha) AS ultima
                          FROM analitica WHERE {vistas} AND usuario IS NOT NULL
                          GROUP BY user_id, despacho ORDER BY vistas DESC LIMIT 12""")
    por_dia = {r["dia"]: r for r in varios(
        f"""SELECT date(fecha, '-6 hours') AS dia, COUNT(*) AS vistas, COUNT(DISTINCT visitante) AS visitantes
            FROM analitica WHERE {vistas} GROUP BY dia""")}
    hoy = (datetime.utcnow() - timedelta(hours=6)).date()
    serie = []
    for i in range(dias - 1, -1, -1):
        dia = (hoy - timedelta(days=i)).isoformat()
        r = por_dia.get(dia) or {"vistas": 0, "visitantes": 0}
        serie.append({"dia": dia, "vistas": r["vistas"], "visitantes": r["visitantes"]})
    publicas = {r["seccion"]: r["n"] for r in varios(
        f"""SELECT seccion, COUNT(DISTINCT visitante) AS n FROM analitica WHERE {vistas}
            AND seccion IN ('Página principal', 'Iniciar sesión', 'Registro de despacho', 'Inicio') GROUP BY seccion""")}
    nombres = {r[0]: r[1] for r in db.execute("SELECT slug, nombre_despacho FROM tenants").fetchall()}
    lista_despachos = [r[0] for r in db.execute(
        "SELECT DISTINCT despacho FROM analitica WHERE despacho IS NOT NULL ORDER BY despacho").fetchall()]

    return render_template(
        "rastreo/panel.html", dias=dias, despacho=despacho, despachos=lista_despachos, tot=dict(tot),
        por_sesion=dict(por_sesion), secciones=secciones, flujos=flujos, entradas=entradas, salidas=salidas,
        acciones=acciones, dispositivos=dispositivos, por_despacho=por_despacho, personas=personas, serie=serie,
        publicas=publicas, nombres=nombres, max_dia=max([s["vistas"] for s in serie] + [1]),
        max_seccion=max([s["vistas"] for s in secciones] + [1]),
    )
