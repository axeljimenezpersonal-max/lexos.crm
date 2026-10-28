"""
Precedentes del despacho: asuntos concluidos que se parecen al que se está trabajando.

Cada asunto se resume en una "huella" (materia, título, hechos, juzgado, sentencias) que se
convierte en vector con el mismo modelo local del lector. Solo se guarda el vector y un hash
del texto, nunca el texto. Nada de esto sale del servidor.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date

import numpy as np

from modules.lector import vectores

RESULTADOS = {
    "favorable":     {"label": "Ganado",        "color": "#3D5A80", "fondo": "#EEF1F5"},
    "parcial":       {"label": "Parcial",       "color": "#8A6A2F", "fondo": "#F7F0E1"},
    "convenio":      {"label": "Convenio",      "color": "#4B6585", "fondo": "#F0F9FF"},
    "desfavorable":  {"label": "Perdido",       "color": "#A23B33", "fondo": "#F8EDEA"},
    "desistimiento": {"label": "Desistimiento", "color": "#60718D", "fondo": "#F1EEE8"},
}
SENTIDOS = {"favorable": "Favorable", "parcial": "Parcial", "desfavorable": "Desfavorable"}

UMBRAL = 0.42          # puntaje mínimo para considerar un asunto "parecido"
UMBRAL_TEXTO = 0.50    # similitud mínima pregunta → asunto (Tribuno)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS insights_huellas (
    asunto_id  INTEGER PRIMARY KEY REFERENCES asuntos(id) ON DELETE CASCADE,
    huella     TEXT    NOT NULL,
    vector     BLOB    NOT NULL,
    fecha      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
_COLUMNAS = (
    ("asuntos", "resultado", "TEXT"),
    ("asuntos", "resultado_notas", "TEXT"),
    ("asunto_sentencias", "sentido", "TEXT"),
)


def asegurar(tdb) -> None:
    for tabla, col, tipo in _COLUMNAS:
        try:
            tdb.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        except sqlite3.OperationalError:
            pass
    tdb.executescript(ESQUEMA)


def _carpeta_modelos(app) -> str:
    return os.path.join(app.config["BOVEDA_DIR"], "_modelos")


# ── Huellas ───────────────────────────────────────────────────────────────────

def _sentencias(tdb, asunto_id: int) -> list[dict]:
    return [dict(s) for s in tdb.execute(
        "SELECT id, titulo, tipo, sentido, fecha_emision, contenido FROM asunto_sentencias "
        "WHERE asunto_id=? ORDER BY COALESCE(fecha_emision, fecha_alta)", (asunto_id,)
    ).fetchall()]


def _texto_huella(a: dict, sentencias: list[dict]) -> str:
    """Describe los hechos del asunto, no su resultado: lo que se compara es el caso, no cómo terminó."""
    partes = [
        f"Materia {a.get('tipo_asunto') or ''} {a.get('sub_tipo') or ''}".strip(),
        a.get("titulo") or "",
        (a.get("descripcion") or "")[:900],
    ]
    if a.get("juzgado"):
        partes.append(f"Juzgado: {a['juzgado']}")
    for s in sentencias[:4]:
        partes.append(f"{s['titulo']}. {(s.get('contenido') or '')[:400]}")
    return "\n".join(p for p in partes if p)


def _vectores(app, tdb, asuntos: list[dict]) -> dict[int, np.ndarray]:
    """Vector de cada asunto; recalcula solo los que cambiaron desde la última vez."""
    guardados = {r["asunto_id"]: (r["huella"], r["vector"])
                 for r in tdb.execute("SELECT asunto_id, huella, vector FROM insights_huellas").fetchall()}
    resultado: dict[int, np.ndarray] = {}
    faltan: list[tuple[int, str, str]] = []
    for a in asuntos:
        texto = _texto_huella(a, a["_sentencias"])
        huella = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        previo = guardados.get(a["id"])
        if previo and previo[0] == huella:
            resultado[a["id"]] = np.frombuffer(previo[1], dtype=np.float32)
        else:
            faltan.append((a["id"], huella, texto))
    if faltan:
        vecs = vectores.vectorizar([t for _, _, t in faltan], _carpeta_modelos(app))
        for (aid, huella, _), v in zip(faltan, vecs):
            resultado[aid] = v
            tdb.execute(
                "INSERT INTO insights_huellas (asunto_id, huella, vector) VALUES (?,?,?) "
                "ON CONFLICT(asunto_id) DO UPDATE SET huella=excluded.huella, vector=excluded.vector, "
                "fecha=CURRENT_TIMESTAMP",
                (aid, huella, v.astype(np.float32).tobytes()),
            )
        tdb.commit()
    return resultado


def _asuntos(tdb) -> list[dict]:
    filas = [dict(r) for r in tdb.execute(
        "SELECT id, titulo, descripcion, numero_expediente, tipo_asunto, sub_tipo, estado, juzgado, "
        "contrario_nombre, contrario_despacho, fecha_inicio, fecha_cierre, resultado, resultado_notas, "
        "COALESCE(privado, 0) AS privado FROM asuntos WHERE estado != 'archivado' OR resultado IS NOT NULL"
    ).fetchall()]
    for a in filas:
        a["_sentencias"] = _sentencias(tdb, a["id"])
    return filas


# ── Presentación ──────────────────────────────────────────────────────────────

def _fecha(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def duracion_meses(a: dict) -> float | None:
    ini, fin = _fecha(a.get("fecha_inicio")), _fecha(a.get("fecha_cierre"))
    if not ini or not fin or fin < ini:
        return None
    return round((fin - ini).days / 30.44, 1)


def _mismo(x, y) -> bool:
    return bool(x) and bool(y) and str(x).strip().lower() == str(y).strip().lower()


def _ficha(a: dict, puntaje: float, base: dict | None = None) -> dict:
    razones = []
    if base:
        if _mismo(a["tipo_asunto"], base["tipo_asunto"]):
            razones.append("Misma materia")
        if _mismo(a["juzgado"], base["juzgado"]):
            razones.append("Mismo juzgado")
        if _mismo(a["contrario_despacho"], base["contrario_despacho"]):
            razones.append("Misma contraparte")
    return {
        "id": a["id"], "titulo": a["titulo"], "numero_expediente": a["numero_expediente"],
        "tipo_asunto": a["tipo_asunto"], "sub_tipo": a["sub_tipo"], "juzgado": a["juzgado"],
        "contrario_despacho": a["contrario_despacho"], "privado": bool(a["privado"]),
        "resultado": a["resultado"], "resultado_info": RESULTADOS.get(a["resultado"] or ""),
        "resultado_notas": a["resultado_notas"] or "", "duracion_meses": duracion_meses(a),
        "similitud": int(round(min(puntaje, 1.0) * 100)), "razones": razones,
        "sentencias": [{"titulo": s["titulo"], "sentido": SENTIDOS.get(s["sentido"] or ""),
                        "fecha": (s["fecha_emision"] or "")[:10]} for s in a["_sentencias"]],
    }


def resumen(fichas: list[dict]) -> dict:
    conteo = {k: 0 for k in RESULTADOS}
    for f in fichas:
        if f["resultado"] in conteo:
            conteo[f["resultado"]] += 1
    duraciones = [f["duracion_meses"] for f in fichas if f["duracion_meses"]]
    return {
        "total": len(fichas), "conteo": conteo,
        "a_favor": conteo["favorable"] + conteo["convenio"] + conteo["parcial"],
        "duracion_media": round(sum(duraciones) / len(duraciones), 1) if duraciones else None,
    }


# ── Consultas ─────────────────────────────────────────────────────────────────

def similares(app, tdb, asunto_id: int, limite: int = 5) -> dict:
    """Asuntos concluidos (con resultado registrado) más parecidos a este."""
    asegurar(tdb)
    todos = _asuntos(tdb)
    base = next((a for a in todos if a["id"] == asunto_id), None)
    if base is None:
        return {"casos": [], "resumen": resumen([]), "concluidos": 0}
    candidatos = [a for a in todos if a["id"] != asunto_id and a["resultado"]]
    if not candidatos:
        return {"casos": [], "resumen": resumen([]), "concluidos": 0}
    vecs = _vectores(app, tdb, [base, *candidatos])
    qv = vecs[base["id"]]
    puntuados = []
    for a in candidatos:
        puntaje = 0.72 * float(vecs[a["id"]] @ qv)
        puntaje += 0.16 if _mismo(a["tipo_asunto"], base["tipo_asunto"]) else 0
        puntaje += 0.04 if _mismo(a["sub_tipo"], base["sub_tipo"]) else 0
        puntaje += 0.05 if _mismo(a["juzgado"], base["juzgado"]) else 0
        puntaje += 0.05 if _mismo(a["contrario_despacho"], base["contrario_despacho"]) else 0
        if puntaje >= UMBRAL:
            puntuados.append((puntaje, a))
    puntuados.sort(key=lambda x: x[0], reverse=True)
    fichas = [_ficha(a, p, base) for p, a in puntuados[:limite]]
    return {"casos": fichas, "resumen": resumen(fichas), "concluidos": len(candidatos)}


def por_texto(app, tdb, consulta: str, limite: int = 3, incluir_privados: bool = True) -> list[dict]:
    """Precedentes del despacho que tienen que ver con una pregunta escrita (para Tribuno)."""
    asegurar(tdb)
    candidatos = [a for a in _asuntos(tdb) if a["resultado"] and (incluir_privados or not a["privado"])]
    if not candidatos or not consulta.strip():
        return []
    vecs = _vectores(app, tdb, candidatos)
    qv = vectores.vectorizar([consulta], _carpeta_modelos(app))[0]
    puntuados = sorted(((float(vecs[a["id"]] @ qv), a) for a in candidatos), key=lambda x: x[0], reverse=True)
    return [_ficha(a, p) for p, a in puntuados[:limite] if p >= UMBRAL_TEXTO]


def texto_para_modelo(f: dict) -> str:
    """Lo que Tribuno lee de un precedente: datos del caso y cómo terminó, sin datos del cliente."""
    lineas = [
        f"Materia: {f['tipo_asunto'] or 'no indicada'}{' / ' + f['sub_tipo'] if f['sub_tipo'] else ''}",
        f"Resultado para el despacho: {f['resultado_info']['label'] if f['resultado_info'] else 'sin registrar'}",
    ]
    if f["juzgado"]:
        lineas.append(f"Juzgado: {f['juzgado']}")
    if f["duracion_meses"]:
        lineas.append(f"Duración: {f['duracion_meses']} meses")
    if f["resultado_notas"]:
        lineas.append(f"Qué funcionó / lecciones: {f['resultado_notas'][:900]}")
    for s in f["sentencias"][:4]:
        lineas.append(f"Resolución: {s['titulo']}{' (' + s['sentido'] + ')' if s['sentido'] else ''}")
    return "\n".join(lineas)


def estadisticas(tdb) -> dict:
    """Cómo le ha ido al despacho, por materia, juzgado y contraparte."""
    asegurar(tdb)
    concluidos = [a for a in _asuntos(tdb) if a["resultado"]]
    fichas = [_ficha(a, 1.0) for a in concluidos]

    def agrupar(campo: str, minimo: int = 1) -> list[dict]:
        grupos: dict[str, list[dict]] = {}
        for f in fichas:
            clave = (f[campo] or "").strip()
            if clave:
                grupos.setdefault(clave, []).append(f)
        filas = [{"nombre": k, **resumen(v)} for k, v in grupos.items() if len(v) >= minimo]
        return sorted(filas, key=lambda r: r["total"], reverse=True)

    lecciones = sorted(
        (f for f in fichas if f["resultado_notas"] and f["resultado"] in ("favorable", "parcial", "convenio")),
        key=lambda f: f["id"], reverse=True,
    )
    return {
        "general": resumen(fichas),
        "por_materia": agrupar("tipo_asunto"),
        "por_juzgado": agrupar("juzgado")[:8],
        "por_contraparte": agrupar("contrario_despacho")[:8],
        "lecciones": lecciones[:8],
        "recientes": sorted(fichas, key=lambda f: f["id"], reverse=True)[:12],
    }
