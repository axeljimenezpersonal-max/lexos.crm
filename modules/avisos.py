"""
Habeas Corpus — modules/avisos.py
Avisos de la campana: audiencias y citas según su recordatorio, lo que empieza en minutos,
términos vencidos y cobros vencidos. Cada persona marca los suyos como leídos.
"""

from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request, session, url_for

from modules.asuntos import _FILTRO_PROPIOS, _solo_lo_suyo
from modules.auth import login_required, tenant_required
from modules.dashboard import DIAS, ZONA

avisos_bp = Blueprint("avisos", __name__)

TERMINOS = ("plazo", "vencimiento", "fecha_limite")
AUDIENCIAS = ("audiencia", "diligencia")
AVISO_PREVIO = 1440          # minutos antes, cuando el evento no trae recordatorio
YA_MERO = 30                 # minutos antes en que «empieza pronto» se vuelve urgente

ESQUEMA = """
CREATE TABLE IF NOT EXISTS avisos_leidos (
    user_id   INTEGER NOT NULL,
    clave     TEXT    NOT NULL,
    leido_en  DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, clave)
);
"""


def asegurar(tdb) -> None:
    tdb.executescript(ESQUEMA)


def _fecha(valor: str) -> datetime:
    return datetime.fromisoformat(str(valor).replace("T", " ")[:19])


def _cuando(inicio: datetime, ahora: datetime, todo_el_dia: bool) -> str:
    dias = (inicio.date() - ahora.date()).days
    hora = "" if todo_el_dia else f" a las {inicio:%H:%M}"
    if dias == 0:
        return f"Hoy{hora}"
    if dias == 1:
        return f"Mañana{hora}"
    if dias < 7:
        return f"El {DIAS[inicio.weekday()].lower()}{hora}"
    return f"El {inicio:%d/%m}{hora}"


def avisos(tdb, uid: int, limite: int = 20) -> list[dict]:
    asegurar(tdb)
    ahora = datetime.now(ZONA).replace(tzinfo=None)
    hoy = ahora.date()
    alc, params = "1", []
    if _solo_lo_suyo():
        alc = f"(e.creado_por=? OR e.asunto_id IN (SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}))"
        params = [uid, uid, uid, uid]

    salida = []
    filas = tdb.execute(
        f"""SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.todo_el_dia, e.recordatorio, e.lugar, e.asunto_id,
                   a.numero_expediente, a.titulo AS asunto
            FROM eventos_calendario e LEFT JOIN asuntos a ON a.id = e.asunto_id
            WHERE e.estado = 'pendiente' AND date(e.fecha_inicio) BETWEEN ? AND ? AND {alc}
            ORDER BY e.fecha_inicio""",
        ((hoy - timedelta(days=14)).isoformat(), (hoy + timedelta(days=30)).isoformat(), *params)).fetchall()

    for e in filas:
        inicio = _fecha(e["fecha_inicio"])
        todo_el_dia = bool(e["todo_el_dia"])
        exp = e["numero_expediente"] or (f"#{e['asunto_id']}" if e["asunto_id"] else "")
        detalle = " · ".join(x for x in (exp, e["asunto"], e["lugar"]) if x)
        url = (url_for("asuntos.detalle", id=e["asunto_id"]) + "#actividad" if e["asunto_id"]
               else url_for("calendario.index"))
        tipo = "termino" if e["tipo"] in TERMINOS else ("audiencia" if e["tipo"] in AUDIENCIAS else "agenda")
        base = {"tipo": tipo, "titulo": e["titulo"], "detalle": detalle, "url": url, "fecha": inicio.isoformat()}

        if e["tipo"] in TERMINOS and inicio.date() < hoy:
            dias = (hoy - inicio.date()).days
            salida.append({**base, "clave": f"venc:{e['id']}:{inicio:%Y%m%d}", "urgente": True,
                           "titulo": f"Término vencido: {e['titulo']}",
                           "cuando": "Venció ayer" if dias == 1 else f"Venció hace {dias} días"})
            continue
        if not todo_el_dia and timedelta(0) <= inicio - ahora <= timedelta(minutes=YA_MERO):
            minutos = max(1, round((inicio - ahora).total_seconds() / 60))
            salida.append({**base, "clave": f"ya:{e['id']}:{inicio:%Y%m%d%H%M}", "tipo": "ahora", "urgente": True,
                           "cuando": f"Empieza en {minutos} min"})
            continue
        # El aviso normal sale cuando llega su recordatorio (un día antes si no tiene)
        referencia = inicio.replace(hour=8, minute=0) if todo_el_dia else inicio
        sale = referencia - timedelta(minutes=e["recordatorio"] or AVISO_PREVIO)
        sigue_vigente = inicio.date() >= hoy if todo_el_dia else inicio >= ahora
        if sale <= ahora and sigue_vigente:
            salida.append({**base, "clave": f"ev:{e['id']}:{inicio:%Y%m%d%H%M}",
                           "urgente": inicio.date() == hoy or tipo == "termino",
                           "cuando": _cuando(inicio, ahora, todo_el_dia)})

    # Cobros vencidos: un solo aviso al día con el total
    from modules import cobranzas
    alc_c, pc = cobranzas._alcance()
    n, monto = tdb.execute(
        f"""SELECT COUNT(*), COALESCE(SUM({cobranzas.SALDO}), 0) FROM cobros co
            WHERE {cobranzas.ABIERTO_SQL} AND date(co.fecha_vencimiento) < ? AND {alc_c}""",
        (hoy.isoformat(), *pc)).fetchone()
    if n:
        salida.append({"clave": f"cobros:{hoy:%Y%m%d}", "tipo": "cobro", "urgente": False,
                       "titulo": f"{n} cobro{'s' if n != 1 else ''} vencido{'s' if n != 1 else ''}",
                       "detalle": f"${monto:,.0f} pendientes de cobrar", "cuando": "Hoy",
                       "url": url_for("cobranzas.index", grupo="vencidos"), "fecha": hoy.isoformat()})

    leidos = {r[0] for r in tdb.execute("SELECT clave FROM avisos_leidos WHERE user_id=?", (uid,))}
    orden = {"ahora": 0, "termino": 1, "audiencia": 2, "agenda": 3, "cobro": 4}
    for a in salida:
        a["leido"] = a["clave"] in leidos
    salida.sort(key=lambda a: (a["leido"], not a["urgente"], orden[a["tipo"]], a["fecha"]))
    return salida[:limite]


@avisos_bp.route("/api")
@login_required
@tenant_required
def api():
    lista = avisos(g.tenant_db, session.get("user_id"))
    return jsonify(avisos=lista, sin_leer=sum(1 for a in lista if not a["leido"]))


@avisos_bp.route("/leer", methods=["POST"])
@login_required
@tenant_required
def leer():
    tdb, uid = g.tenant_db, session.get("user_id")
    d = request.get_json(silent=True) or {}
    claves = [a["clave"] for a in avisos(tdb, uid, limite=200)] if d.get("todas") else d.get("claves") or []
    asegurar(tdb)
    tdb.executemany("INSERT OR IGNORE INTO avisos_leidos (user_id, clave) VALUES (?, ?)",
                    [(uid, str(c)[:80]) for c in claves[:200]])
    tdb.execute("DELETE FROM avisos_leidos WHERE user_id=? AND leido_en < datetime('now', '-60 days')", (uid,))
    tdb.commit()
    return jsonify(ok=True)
