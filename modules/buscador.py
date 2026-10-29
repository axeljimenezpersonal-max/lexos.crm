"""
Habeas Corpus — modules/buscador.py
Buscador inteligente: una sola forma de buscar en todo el despacho.

Entiende frases en español además de palabras sueltas:
  "amparos de Luis que vencen esta semana", "documentos de Fuentes",
  "audiencias de mañana", "expedientes sin movimiento", "cobros vencidos".
De la frase saca filtros (materia, estado, responsable, fechas, qué buscar) y el
resto lo busca como texto, sin importar acentos y tolerando errores de dedo.
Respeta el alcance: un abogado solo encuentra lo de sus expedientes.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date, timedelta

from flask import Blueprint, g, jsonify, render_template, request, session, url_for

from modules import cobranzas
from modules.asuntos import (ABIERTOS, CONCLUIDOS, ESTADOS, EVENTO_LABEL, TIPOS_DERECHO, _FILTRO_PROPIOS,
                             _hoy, _proximos, _solo_lo_suyo, _ultimos_movimientos, _usuarios)
from modules.auth import login_required, tenant_required

buscador_bp = Blueprint("buscador", __name__)

TERMINOS = ("plazo", "vencimiento", "fecha_limite")
NOMBRE_CLIENTE = "COALESCE(NULLIF(c.razon_social, ''), TRIM(COALESCE(c.nombre,'') || ' ' || COALESCE(c.apellidos, '')))"


# ─────────────────────────────────────────────────────────────────────────────
#  Texto
# ─────────────────────────────────────────────────────────────────────────────

def _letra(ch: str) -> str:
    base = unicodedata.normalize("NFKD", ch)[:1]
    return base if base.isascii() else ch


def normalizar(texto) -> str:
    """Minúsculas y sin acentos, conservando la longitud (para poder recortar la frase original)."""
    return "".join(_letra(ch) for ch in str(texto or "").lower())


VACIAS = set("""de del la las el los lo que con en para por y a al un una unos unas sobre todo todos todas ver
buscar busca dame muestrame mostrar quiero tengo hay cuales cuantos cual esta este estos estas vence vencen venza
venzan tiene tienen se me mi sus su o e les ya donde como""".split())

# Frases que se vuelven filtros. Se revisan de la más larga a la más corta.
_FRASES: list[tuple[str, str, str]] = []


def _frase(clave: str, valor: str, *textos: str) -> None:
    for t in textos:
        _FRASES.append((t, clave, valor))


for _k, _info in TIPOS_DERECHO.items():
    if _k == "otro":
        continue
    _n = normalizar(_info["label"])
    _frase("materia", _k, _n, _n + "s", _n + "es")
_frase("materia", "notarial", "notaria", "notarias", "escrituras", "escritura")
_frase("materia", "amparo", "amparos")

_frase("estado", "abiertos", "en tramite", "tramite", "en curso", "abiertos", "abiertas", "activos", "activas", "vigentes")
_frase("estado", "concluidos", "concluidos", "concluidas", "concluido", "cerrados", "cerradas", "terminados", "terminadas")
_frase("estado", "archivados", "archivados", "archivadas", "archivado", "archivo muerto")
_frase("estado", "en_espera", "en espera", "pausados", "suspendidos", "suspendidas")
_frase("estado", "por_iniciar", "por iniciar", "nuevos sin iniciar")

_frase("periodo", "hoy", "hoy", "para hoy", "de hoy")
_frase("periodo", "manana", "manana", "para manana", "de manana")
_frase("periodo", "semana", "esta semana", "en la semana", "estos dias", "proximos dias")
_frase("periodo", "prox_semana", "proxima semana", "siguiente semana", "semana que entra")
_frase("periodo", "mes", "este mes", "en el mes")
_frase("periodo", "prox_mes", "proximo mes", "siguiente mes", "mes que entra")
_frase("periodo", "vencido", "vencidos", "vencidas", "vencido", "vencida", "atrasados", "atrasadas", "pasados de fecha")

_frase("atencion", "sin_mov", "sin movimiento", "sin moverse", "olvidados", "olvidadas", "parados", "abandonados")
_frase("atencion", "termino", "por vencer", "con termino", "con terminos", "urgentes", "urgente")

_frase("que", "expedientes", "expedientes", "expediente", "asuntos", "asunto", "casos", "caso", "carpetas", "juicios")
_frase("que", "personas", "personas", "clientes", "cliente", "contactos", "contrapartes")
_frase("que", "documentos", "documentos", "documento", "archivos", "archivo", "pdf", "pdfs", "escritos", "sentencias",
       "resoluciones", "acuerdos")
_frase("que", "audiencias", "audiencias", "audiencia")
_frase("que", "terminos", "terminos", "termino", "plazos", "plazo", "vencimientos")
_frase("que", "citas", "citas", "cita", "reuniones", "reunion", "juntas")
_frase("que", "diligencias", "diligencias", "diligencia", "emplazamientos", "emplazamiento")
_frase("que", "eventos", "agenda", "calendario", "eventos", "pendientes")
_frase("que", "cobros", "cobros", "cobro", "facturas", "factura", "pagos", "adeudos", "deudas", "cobranza", "honorarios")

_frase("responsable", "yo", "mis", "mios", "mias", "a mi cargo", "conmigo")
_FRASES.sort(key=lambda f: -len(f[0]))

ETIQUETAS = {
    ("estado", "abiertos"): "En trámite", ("estado", "concluidos"): "Concluidos", ("estado", "archivados"): "Archivados",
    ("estado", "en_espera"): "En espera", ("estado", "por_iniciar"): "Por iniciar",
    ("periodo", "hoy"): "Hoy", ("periodo", "manana"): "Mañana", ("periodo", "semana"): "Esta semana",
    ("periodo", "prox_semana"): "Próxima semana", ("periodo", "mes"): "Este mes", ("periodo", "prox_mes"): "Próximo mes",
    ("periodo", "vencido"): "Vencidos", ("atencion", "sin_mov"): "Sin movimiento", ("atencion", "termino"): "Por vencer",
    ("que", "expedientes"): "Expedientes", ("que", "personas"): "Personas", ("que", "documentos"): "Documentos",
    ("que", "audiencias"): "Audiencias", ("que", "terminos"): "Términos", ("que", "citas"): "Citas",
    ("que", "diligencias"): "Diligencias", ("que", "eventos"): "Agenda", ("que", "cobros"): "Cobros",
    ("responsable", "yo"): "Míos",
}
GRUPO_DE = {"expedientes": "expedientes", "personas": "personas", "documentos": "documentos", "audiencias": "agenda",
            "terminos": "agenda", "citas": "agenda", "diligencias": "agenda", "eventos": "agenda", "cobros": "cobros"}
TIPOS_EVENTO = {"audiencias": ("audiencia",), "terminos": TERMINOS, "citas": ("reunion",), "diligencias": ("diligencia",)}


def _rango(periodo: str, hoy: date) -> tuple[date, date] | None:
    lunes = hoy - timedelta(days=hoy.weekday())
    inicio_mes = hoy.replace(day=1)
    prox_mes = (inicio_mes + timedelta(days=32)).replace(day=1)
    return {
        "hoy": (hoy, hoy),
        "manana": (hoy + timedelta(days=1), hoy + timedelta(days=1)),
        "semana": (hoy, lunes + timedelta(days=6)),
        "prox_semana": (lunes + timedelta(days=7), lunes + timedelta(days=13)),
        "mes": (hoy, prox_mes - timedelta(days=1)),
        "prox_mes": (prox_mes, (prox_mes + timedelta(days=32)).replace(day=1) - timedelta(days=1)),
    }.get(periodo)


def interpretar(q: str, usuarios: dict | None = None) -> dict:
    """Separa la frase en filtros y texto libre.
    Devuelve {"filtros": {clave: valor}, "chips": [...], "tokens": [...], "q": q}."""
    q = (q or "").strip()[:200]
    norm = normalizar(q)
    usado = [False] * len(norm)
    filtros: dict[str, str] = {}
    chips = []

    def tomar(inicio: int, fin: int, clave: str, valor: str, etiqueta: str) -> None:
        for i in range(inicio, fin):
            usado[i] = True
        if clave in filtros:
            return
        filtros[clave] = valor
        # La frase sin este filtro, para quitarlo desde el chip
        resto = re.sub(r"\s{2,}", " ", (q[:inicio] + " " + q[fin:])).strip()
        resto = re.sub(r"(^|\s)(de|del|que|con|en)\s*$", "", resto).strip()
        chips.append({"clave": clave, "valor": valor, "etiqueta": etiqueta, "sin": resto})

    # Responsable por nombre: "de Luis", "de Mariana Ríos"
    for uid, u in (usuarios or {}).items():
        nombre = normalizar(u.get("nombre") or "").split(" ")[0]
        if len(nombre) < 3:
            continue
        for m in re.finditer(rf"\b(?:de|del abogado|de la abogada|abogado|abogada|responsable)\s+({re.escape(nombre)})\b", norm):
            if not any(usado[m.start():m.end()]):
                tomar(m.start(), m.end(), "responsable", str(uid), "De " + (u.get("corto") or u.get("nombre") or ""))

    for texto, clave, valor in _FRASES:
        for m in re.finditer(rf"(?<![\w/]){re.escape(texto)}(?![\w/])", norm):
            if any(usado[m.start():m.end()]):
                continue
            etiqueta = ETIQUETAS.get((clave, valor)) or TIPOS_DERECHO.get(valor, {}).get("label", valor)
            tomar(m.start(), m.end(), clave, valor, etiqueta)

    libre = "".join(ch if not usado[i] else " " for i, ch in enumerate(norm))
    tokens = [t for t in re.findall(r"[\w/.\-]+", libre) if t not in VACIAS and len(t) > 1 or re.fullmatch(r"\d+", t)]
    tokens = [t.strip(".-") for t in tokens if t.strip(".-")]
    return {"q": q, "filtros": filtros, "chips": chips, "tokens": tokens}


# ─────────────────────────────────────────────────────────────────────────────
#  Coincidencias y orden
# ─────────────────────────────────────────────────────────────────────────────

def _patron(t: str):
    """La palabra debe empezar ahí: «pension» encuentra «pensión», no «suspensión»."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(t))


def _coincide(tokens: list[str], pajar: str) -> bool:
    return all(_patron(t).search(pajar) for t in tokens)


def _puntaje(tokens: list[str], titulo: str, numero: str, pajar: str) -> int:
    titulo, numero = normalizar(titulo), normalizar(numero)
    p = 0
    for t in tokens:
        if numero and t == numero:
            p += 60
        elif numero and t in numero:
            p += 14
        if re.search(rf"\b{re.escape(t)}", titulo):
            p += 8
        elif t in titulo:
            p += 4
        elif re.search(rf"\b{re.escape(t)}", pajar):
            p += 3
        else:
            p += 1
    return p


def _corregir(tokens: list[str], pajares: list[str]) -> list[str] | None:
    """Si alguna palabra no aparece en nada, prueba con la más parecida (errores de dedo)."""
    vocab = set()
    for pj in pajares:
        vocab.update(w for w in re.findall(r"[a-z0-9ñ]{3,}", pj))
    cambio, salida = False, []
    for t in tokens:
        if any(_patron(t).search(pj) for pj in pajares) or len(t) < 4 or any(ch.isdigit() for ch in t):
            salida.append(t)
            continue
        cerca = difflib.get_close_matches(t, vocab, n=1, cutoff=0.74)
        if cerca:
            salida.append(cerca[0])
            cambio = True
        else:
            salida.append(t)
    return salida if cambio else None


def _filtrar(filas: list[dict], tokens: list[str], difuso: bool = True) -> tuple[list[dict], list[str] | None]:
    """Filas que tienen todas las palabras; si no hay ninguna, intenta corrigiendo errores de dedo."""
    if not tokens:
        return filas, None
    hallados = [f for f in filas if _coincide(tokens, f["_pajar"])]
    if hallados or not difuso:
        return hallados, None
    corregidos = _corregir(tokens, [f["_pajar"] for f in filas])
    if corregidos:
        hallados = [f for f in filas if _coincide(corregidos, f["_pajar"])]
        if hallados:
            return hallados, corregidos
    return [], None


def _alcance_asuntos(alias: str = "a") -> tuple[str, list]:
    if not _solo_lo_suyo():
        return "1", []
    uid = session.get("user_id")
    return _FILTRO_PROPIOS.replace("a.", f"{alias}."), [uid, uid, uid]


def _uid_responsable(filtros: dict) -> int | None:
    r = filtros.get("responsable")
    if r == "yo":
        return session.get("user_id")
    try:
        return int(r) if r else None
    except ValueError:
        return None


def _fecha(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _corta(f: date | None) -> str:
    return f"{f.day} {_MESES[f.month - 1]}" + ("" if f.year == _hoy().year else f" {f.year}") if f else ""


# ─────────────────────────────────────────────────────────────────────────────
#  Búsquedas por grupo
# ─────────────────────────────────────────────────────────────────────────────

def expedientes(tdb, inter: dict, limite: int | None = 6) -> dict:
    f = inter["filtros"]
    alc, params = _alcance_asuntos()
    where = [alc]
    if f.get("materia"):
        where.append("a.tipo_asunto=?")
        params.append(f["materia"])
    estado = f.get("estado")
    if estado == "abiertos" or f.get("atencion") or (f.get("periodo") and f["periodo"] != "vencido" and not estado):
        where.append(f"a.estado IN ({','.join('?' * len(ABIERTOS))})")
        params += list(ABIERTOS)
    elif estado == "concluidos":
        where.append(f"a.estado IN ({','.join('?' * len(CONCLUIDOS))})")
        params += list(CONCLUIDOS)
    elif estado == "archivados":
        where.append("a.estado='archivado'")
    elif estado in ("en_espera", "por_iniciar"):
        where.append("a.estado IN (?,?)" if estado == "en_espera" else "a.estado=?")
        params += ["en_espera", "suspendido"] if estado == "en_espera" else ["por_iniciar"]
    uid = _uid_responsable(f)
    if uid:
        where.append("(a.abogado_responsable=? OR a.id IN (SELECT asunto_id FROM asunto_colaboradores WHERE user_id=?))")
        params += [uid, uid]
    periodo, hoy = f.get("periodo"), _hoy()
    tipos_ev = TIPOS_EVENTO.get(f.get("que", ""), ())
    if periodo:
        cond_tipo = f" AND tipo IN ({','.join('?' * len(tipos_ev))})" if tipos_ev else ""
        if periodo == "vencido":
            where.append(f"a.id IN (SELECT asunto_id FROM eventos_calendario WHERE estado='pendiente' "
                         f"AND tipo IN ({','.join('?' * len(TERMINOS))}) AND date(fecha_inicio) < ?)")
            params += list(TERMINOS) + [hoy.isoformat()]
        elif _rango(periodo, hoy):
            ini, fin = _rango(periodo, hoy)
            where.append(f"a.id IN (SELECT asunto_id FROM eventos_calendario WHERE estado='pendiente' "
                         f"AND date(fecha_inicio) BETWEEN ? AND ?{cond_tipo})")
            params += [ini.isoformat(), fin.isoformat(), *tipos_ev]
    filas = [dict(r) for r in tdb.execute(
        f"""SELECT a.id, a.titulo, a.numero_expediente, a.numero_juicio, a.tipo_asunto, a.sub_tipo, a.estado,
                   a.juzgado, a.contrario_nombre, a.descripcion, a.abogado_responsable, a.fecha_limite, a.fecha_alta,
                   {NOMBRE_CLIENTE} AS cliente
            FROM asuntos a LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {' AND '.join(where)}""", params).fetchall()]

    abiertos = [x for x in filas if x["estado"] in ABIERTOS]
    if f.get("atencion") == "termino" or f.get("que") == "terminos" and not periodo:
        prox = _proximos(tdb, [x["id"] for x in abiertos], {x["id"]: x["fecha_limite"] for x in abiertos})
        filas = [x for x in filas if x["id"] in prox and prox[x["id"]]["dias"] <= 7]
    elif f.get("atencion") == "sin_mov":
        mov = _ultimos_movimientos(tdb, [x["id"] for x in abiertos])
        filas = [x for x in filas if x["estado"] in ABIERTOS and (hoy - mov.get(x["id"], hoy)).days > 30]

    usuarios = _usuarios()
    for x in filas:
        materia = TIPOS_DERECHO.get(x["tipo_asunto"] or "otro", TIPOS_DERECHO["otro"])["label"]
        resp = usuarios.get(x["abogado_responsable"]) or {}
        x["_pajar"] = normalizar(" ".join(str(v or "") for v in (
            x["titulo"], x["numero_expediente"], x["numero_juicio"], x["cliente"], x["contrario_nombre"],
            x["juzgado"], x["sub_tipo"], materia, resp.get("nombre"), x["descripcion"])))
    filas, corregido = _filtrar(filas, inter["tokens"], inter.get("difuso", True))
    tokens = corregido or inter["tokens"]
    for x in filas:
        x["_p"] = _puntaje(tokens, x["titulo"], x["numero_expediente"] or "", x["_pajar"]) + (3 if x["estado"] in ABIERTOS else 0)
    # Lo más parecido primero; a igual parecido, lo que sigue en curso y lo más nuevo
    filas.sort(key=lambda x: str(x["fecha_alta"] or ""), reverse=True)
    filas.sort(key=lambda x: (-x["_p"], x["estado"] not in ABIERTOS))

    total = len(filas)
    visibles = filas if limite is None else filas[:limite]
    prox = _proximos(tdb, [x["id"] for x in visibles if x["estado"] in ABIERTOS],
                     {x["id"]: x["fecha_limite"] for x in visibles if x["estado"] in ABIERTOS})
    items = []
    for x in visibles:
        est = ESTADOS.get(x["estado"], ESTADOS["activo"])
        materia = TIPOS_DERECHO.get(x["tipo_asunto"] or "otro", TIPOS_DERECHO["otro"])["label"]
        p = prox.get(x["id"])
        items.append({
            "id": x["id"], "tipo": "expediente", "titulo": x["titulo"],
            "numero": x["numero_expediente"] or f"#{x['id']}",
            "sub": " · ".join(v for v in (materia, x["cliente"]) if v),
            "chip": est["label"], "chip_clave": x["estado"],
            "extra": f"{p['tipo']}: {p['cuando'].lower()}" if p else "",
            "url": url_for("asuntos.detalle", id=x["id"]),
        })
    return {"items": items, "total": total, "ids": [x["id"] for x in filas], "corregido": corregido}


def personas(tdb, inter: dict, limite: int | None = 6) -> dict:
    f = inter["filtros"]
    where, params = ["c.activo=1"], []
    uid = _uid_responsable(f)
    if uid:
        where.append("c.abogado_id=?")
        params.append(uid)
    if f.get("materia"):
        alc, pa = _alcance_asuntos("x")
        where.append(f"c.id IN (SELECT x.cliente_id FROM asuntos x WHERE x.tipo_asunto=? AND {alc})")
        params += [f["materia"], *pa]
    filas = [dict(r) for r in tdb.execute(
        f"""SELECT c.id, c.tipo, {NOMBRE_CLIENTE} AS nombre, c.rfc, c.email, c.telefono, c.celular, c.ciudad,
                   c.contacto_principal, c.estatus
            FROM clientes c WHERE {' AND '.join(where)}""", params).fetchall()]
    for x in filas:
        x["_pajar"] = normalizar(" ".join(str(v or "") for v in (
            x["nombre"], x["rfc"], x["email"], x["ciudad"], x["contacto_principal"],
            re.sub(r"\D", "", x["telefono"] or ""), re.sub(r"\D", "", x["celular"] or ""))))
    filas, corregido = _filtrar(filas, inter["tokens"], inter.get("difuso", True))
    tokens = corregido or inter["tokens"]
    for x in filas:
        x["_p"] = _puntaje(tokens, x["nombre"], x["rfc"] or "", x["_pajar"])
    filas.sort(key=lambda x: (-x["_p"], normalizar(x["nombre"])))
    visibles = filas if limite is None else filas[:limite]
    items = [{
        "id": x["id"], "tipo": "persona", "titulo": x["nombre"] or "Sin nombre",
        "sub": " · ".join(v for v in ("Empresa" if x["tipo"] == "persona_moral" else "Persona física",
                                       x["rfc"], x["celular"] or x["telefono"]) if v),
        "chip": "Prospecto" if x["estatus"] == "prospecto" else "",
        "url": url_for("clientes.detalle", id=x["id"]),
    } for x in visibles]
    return {"items": items, "total": len(filas), "ids": [x["id"] for x in filas], "corregido": corregido}


def documentos(tdb, inter: dict, limite: int | None = 6) -> dict:
    f = inter["filtros"]
    alc, params = _alcance_asuntos()
    where = [alc]
    if f.get("materia"):
        where.append("a.tipo_asunto=?")
        params.append(f["materia"])
    uid = _uid_responsable(f)
    if uid:
        where.append("a.abogado_responsable=?")
        params.append(uid)
    w = " AND ".join(where)
    filas = [dict(r, origen="documento") for r in tdb.execute(
        f"""SELECT d.id, d.nombre AS titulo, d.archivo_nombre, d.archivo_url, d.tipo_documento AS clase,
                   d.fecha_subida AS fecha, d.descripcion, a.id AS asunto_id, a.titulo AS asunto, a.numero_expediente,
                   {NOMBRE_CLIENTE} AS cliente
            FROM documentos d JOIN asuntos a ON a.id = d.asunto_id LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {w}""", params).fetchall()]
    filas += [dict(r, origen="resolucion", archivo_nombre=None) for r in tdb.execute(
        f"""SELECT s.id, s.titulo, s.archivo_url, s.tipo AS clase, COALESCE(s.fecha_emision, s.fecha_alta) AS fecha,
                   s.contenido AS descripcion, a.id AS asunto_id, a.titulo AS asunto, a.numero_expediente,
                   {NOMBRE_CLIENTE} AS cliente
            FROM asunto_sentencias s JOIN asuntos a ON a.id = s.asunto_id LEFT JOIN clientes c ON c.id = a.cliente_id
            WHERE {w}""", params).fetchall()]
    for x in filas:
        x["_pajar"] = normalizar(" ".join(str(v or "") for v in (
            x["titulo"], x["archivo_nombre"], x["clase"], x["asunto"], x["numero_expediente"], x["cliente"],
            (x["descripcion"] or "")[:400])))
    filas, corregido = _filtrar(filas, inter["tokens"], inter.get("difuso", True))
    tokens = corregido or inter["tokens"]
    for x in filas:
        x["_p"] = _puntaje(tokens, x["titulo"], "", x["_pajar"])
    filas.sort(key=lambda x: str(x["fecha"] or ""), reverse=True)
    filas.sort(key=lambda x: -x["_p"])
    visibles = filas if limite is None else filas[:limite]
    items = []
    for x in visibles:
        ext = (x["archivo_nombre"] or x["archivo_url"] or "").rsplit(".", 1)[-1].lower()[:4] if (x["archivo_nombre"] or x["archivo_url"]) else ""
        items.append({
            "id": x["id"], "tipo": "documento", "titulo": x["titulo"] or x["archivo_nombre"] or "Documento",
            "sub": " · ".join(v for v in (x["numero_expediente"], x["asunto"]) if v),
            "chip": ("Resolución" if x["origen"] == "resolucion" else (ext.upper() if ext else "")),
            "extra": _corta(_fecha(x["fecha"])),
            "url": url_for("asuntos.detalle", id=x["asunto_id"]) + "#documentos",
        })
    return {"items": items, "total": len(filas), "corregido": corregido}


def agenda(tdb, inter: dict, limite: int | None = 6) -> dict:
    f = inter["filtros"]
    hoy = _hoy()
    where, params = ["e.estado != 'cancelado'"], []
    if _solo_lo_suyo():
        uid = session.get("user_id")
        where.append(f"(e.creado_por=? OR e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?) "
                     f"OR e.asunto_id IN (SELECT a.id FROM asuntos a WHERE {_FILTRO_PROPIOS}))")
        params += [uid, uid, uid, uid, uid]
    tipos = TIPOS_EVENTO.get(f.get("que", ""))
    if tipos:
        where.append(f"e.tipo IN ({','.join('?' * len(tipos))})")
        params += list(tipos)
    if f.get("materia"):
        where.append("a.tipo_asunto=?")
        params.append(f["materia"])
    uid = _uid_responsable(f)
    if uid:
        where.append("(e.creado_por=? OR a.abogado_responsable=? OR e.id IN (SELECT evento_id FROM evento_participantes WHERE user_id=?))")
        params += [uid, uid, uid]
    periodo = f.get("periodo")
    if periodo == "vencido":
        where.append(f"e.estado='pendiente' AND date(e.fecha_inicio) < ?")
        params.append(hoy.isoformat())
    elif _rango(periodo or "", hoy):
        ini, fin = _rango(periodo, hoy)
        where.append("date(e.fecha_inicio) BETWEEN ? AND ?")
        params += [ini.isoformat(), fin.isoformat()]
    filas = [dict(r) for r in tdb.execute(
        f"""SELECT e.id, e.titulo, e.tipo, e.fecha_inicio, e.todo_el_dia, e.lugar, e.estado, e.asunto_id,
                   a.titulo AS asunto, a.numero_expediente, {NOMBRE_CLIENTE} AS cliente
            FROM eventos_calendario e LEFT JOIN asuntos a ON a.id = e.asunto_id
            LEFT JOIN clientes c ON c.id = COALESCE(e.cliente_id, a.cliente_id)
            WHERE {' AND '.join(where)}""", params).fetchall()]
    for x in filas:
        x["_pajar"] = normalizar(" ".join(str(v or "") for v in (
            x["titulo"], EVENTO_LABEL.get(x["tipo"], ""), x["lugar"], x["asunto"], x["numero_expediente"], x["cliente"])))
    filas, corregido = _filtrar(filas, inter["tokens"], inter.get("difuso", True))
    # Primero lo que viene (de hoy en adelante), luego lo pasado del más reciente al más viejo
    hoy_iso = hoy.isoformat()
    futuros = sorted([x for x in filas if str(x["fecha_inicio"])[:10] >= hoy_iso], key=lambda x: str(x["fecha_inicio"]))
    pasados = sorted([x for x in filas if str(x["fecha_inicio"])[:10] < hoy_iso], key=lambda x: str(x["fecha_inicio"]), reverse=True)
    if periodo == "vencido":
        futuros, pasados = pasados, []
    filas = futuros + pasados
    visibles = filas if limite is None else filas[:limite]
    items = []
    for x in visibles:
        d = _fecha(x["fecha_inicio"])
        hora = "" if x["todo_el_dia"] or len(str(x["fecha_inicio"])) < 16 else str(x["fecha_inicio"])[11:16]
        vencido = d and d < hoy and x["estado"] == "pendiente" and x["tipo"] in TERMINOS
        items.append({
            "id": x["id"], "tipo": "evento", "titulo": x["titulo"],
            "sub": " · ".join(v for v in (EVENTO_LABEL.get(x["tipo"], "Evento"), x["asunto"] or x["cliente"]) if v),
            "chip": "Vencido" if vencido else ("Hoy" if d == hoy else ("Mañana" if d == hoy + timedelta(days=1) else "")),
            "chip_clave": "vencido" if vencido else "",
            "dia": d.day if d else "", "mes": _MESES[d.month - 1].upper() if d else "",
            "extra": hora or "Todo el día",
            "url": url_for("asuntos.detalle", id=x["asunto_id"]) if x["asunto_id"] else url_for("calendario.index"),
        })
    return {"items": items, "total": len(filas), "corregido": corregido}


def cobros(tdb, inter: dict, limite: int | None = 6) -> dict:
    f = inter["filtros"]
    alc, params = cobranzas._alcance()
    where = [alc]
    if f.get("materia"):
        where.append("a.tipo_asunto=?")
        params.append(f["materia"])
    uid = _uid_responsable(f)
    if uid:
        where.append("(co.abogado_id=? OR a.abogado_responsable=?)")
        params += [uid, uid]
    hoy = _hoy()
    periodo = f.get("periodo")
    if periodo == "vencido":
        where.append(f"{cobranzas.ABIERTO_SQL} AND date(co.fecha_vencimiento) < ?")
        params.append(hoy.isoformat())
    elif _rango(periodo or "", hoy):
        ini, fin = _rango(periodo, hoy)
        where.append(f"{cobranzas.ABIERTO_SQL} AND date(co.fecha_vencimiento) BETWEEN ? AND ?")
        params += [ini.isoformat(), fin.isoformat()]
    filas = [dict(r) for r in tdb.execute(
        f"""SELECT co.id, co.concepto, co.numero_factura, co.estado, co.fecha_vencimiento, {cobranzas.SALDO} AS saldo,
                   co.total, a.titulo AS asunto, a.numero_expediente, {NOMBRE_CLIENTE} AS cliente
            FROM cobros co LEFT JOIN asuntos a ON a.id = co.asunto_id LEFT JOIN clientes c ON c.id = co.cliente_id
            WHERE {' AND '.join(where)}""", params).fetchall()]
    for x in filas:
        x["_pajar"] = normalizar(" ".join(str(v or "") for v in (
            x["concepto"], x["numero_factura"], x["cliente"], x["asunto"], x["numero_expediente"],
            cobranzas.ESTADOS.get(x["estado"], {}).get("label", ""))))
    filas, corregido = _filtrar(filas, inter["tokens"], inter.get("difuso", True))
    filas.sort(key=lambda x: (x["estado"] not in cobranzas.ABIERTOS, str(x["fecha_vencimiento"] or "")))
    visibles = filas if limite is None else filas[:limite]
    items = [{
        "id": x["id"], "tipo": "cobro", "titulo": f"{x['concepto']} · {x['cliente'] or 'Sin cliente'}",
        "sub": " · ".join(v for v in (x["numero_expediente"], x["asunto"]) if v),
        "chip": cobranzas.ESTADOS.get(x["estado"], {}).get("label", ""), "chip_clave": x["estado"],
        "extra": f"${x['saldo']:,.0f} por cobrar" if x["saldo"] else f"${(x['total'] or 0):,.0f}",
        "url": url_for("cobranzas.index", q=x["concepto"]),
    } for x in visibles]
    return {"items": items, "total": len(filas), "ids": [x["id"] for x in filas], "corregido": corregido}


GRUPOS = {
    "expedientes": ("Expedientes", expedientes, "asuntos.index"),
    "personas":    ("Personas", personas, "clientes.index"),
    "documentos":  ("Documentos", documentos, "biblioteca.index"),
    "agenda":      ("Agenda", agenda, "calendario.index"),
    "cobros":      ("Cobranza", cobros, "cobranzas.index"),
}


def buscar(tdb, q: str, ambito: str = "todo", limite: int | None = 5) -> dict:
    usuarios = _usuarios()
    inter = interpretar(q, usuarios)
    que = GRUPO_DE.get(inter["filtros"].get("que", ""))
    if ambito in GRUPOS:
        claves = [ambito]
    elif que:
        claves = [que]
    else:
        claves = list(GRUPOS)
        # Si la frase solo habla de fechas o términos, lo natural es la agenda y los expedientes
        if inter["filtros"].get("periodo") and not inter["tokens"]:
            claves = ["agenda", "expedientes", "cobros"]
    vacia = not inter["tokens"] and not inter["filtros"]
    # Primero tal cual; solo si nada coincide se corrigen errores de dedo
    for difuso in (False, True):
        inter["difuso"] = difuso
        grupos, corregido = [], None
        for clave in claves:
            if vacia and ambito not in GRUPOS:
                break
            titulo, fn, destino = GRUPOS[clave]
            r = fn(tdb, inter, limite)
            corregido = corregido or r.get("corregido")
            if r["items"]:
                grupos.append({"clave": clave, "titulo": titulo, "items": r["items"], "total": r["total"],
                               "ver_todo": url_for("buscador.resultados", q=q, en=clave)})
        if grupos or not inter["tokens"]:
            break
    sugerencia = None
    if corregido:
        sugerencia = " ".join(corregido)
    return {"q": q, "chips": inter["chips"], "tokens": inter["tokens"], "grupos": grupos,
            "sugerencia": sugerencia, "total": sum(gr["total"] for gr in grupos)}


# ─────────────────────────────────────────────────────────────────────────────
#  Rutas
# ─────────────────────────────────────────────────────────────────────────────

@buscador_bp.route("/api")
@login_required
@tenant_required
def api():
    q = request.args.get("q", "")
    ambito = request.args.get("en", "todo")
    return jsonify(buscar(g.tenant_db, q, ambito, limite=4 if ambito == "todo" else 6))


@buscador_bp.route("/")
@login_required
@tenant_required
def resultados():
    q = request.args.get("q", "").strip()
    ambito = request.args.get("en", "todo")
    datos = buscar(g.tenant_db, q, ambito, limite=None if ambito in GRUPOS else 8) if q else None
    return render_template("buscador/resultados.html", datos=datos, q=q, ambito=ambito,
                           ambitos=[(k, v[0]) for k, v in GRUPOS.items()])
