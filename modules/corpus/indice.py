"""
Base legal de México compartida por todos los despachos (información pública).

Leyes federales partidas por artículo y notas del DOF, con búsqueda híbrida
(palabras + significado) igual que la memoria del despacho.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from datetime import date, timedelta

import numpy as np

from modules.corpus import fuentes
from modules.lector import vectores
from modules.lector.indice import fragmentar

log = logging.getLogger(__name__)
MAX_SECCION = 1600

ESQUEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS corpus_documentos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente              TEXT NOT NULL,           -- 'ley_federal' | 'dof'
    clave               TEXT NOT NULL,
    titulo              TEXT NOT NULL,
    detalle             TEXT,                    -- organismo (DOF) o fecha de publicación (ley)
    fecha_publicacion   TEXT,
    ultima_reforma      TEXT,
    url_oficial         TEXT,
    etag                TEXT,
    huella              TEXT,
    fragmentos          INTEGER DEFAULT 0,
    fecha_actualizacion DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (fuente, clave)
);
CREATE TABLE IF NOT EXISTS corpus_fragmentos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id  INTEGER NOT NULL REFERENCES corpus_documentos(id) ON DELETE CASCADE,
    orden         INTEGER NOT NULL,
    referencia    TEXT,
    texto         TEXT NOT NULL,
    vector        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corpus_frag_doc ON corpus_fragmentos(documento_id);
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
    texto, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS corpus_sincronizaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente      TEXT NOT NULL,
    inicio      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fin         DATETIME,
    nuevos      INTEGER DEFAULT 0,
    actualizados INTEGER DEFAULT 0,
    sin_cambios INTEGER DEFAULT 0,
    errores     INTEGER DEFAULT 0,
    nota        TEXT
);
"""
_cache: dict[str, tuple] = {}


def ruta_db(config) -> str:
    return os.path.join(config["DATABASE_DIR"], "corpus_legal.db")


def conectar(config) -> sqlite3.Connection:
    conn = sqlite3.connect(ruta_db(config), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ESQUEMA)
    return conn


def _carpeta_modelos(config) -> str:
    return os.path.join(config["BOVEDA_DIR"], "_modelos")


# ── Guardado ───────────────────────────────────────────────────────────────────

def _borrar_fragmentos(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM corpus_fts WHERE rowid IN (SELECT id FROM corpus_fragmentos WHERE documento_id=?)", (doc_id,))
    conn.execute("DELETE FROM corpus_fragmentos WHERE documento_id=?", (doc_id,))


def _guardar(conn, config, fuente: str, clave: str, titulo: str, secciones: list[tuple[str, str]], **campos) -> int:
    """secciones: (referencia, texto). Reemplaza los fragmentos del documento."""
    fila = conn.execute("SELECT id FROM corpus_documentos WHERE fuente=? AND clave=?", (fuente, clave)).fetchone()
    if fila:
        doc_id = fila[0]
        sets = ", ".join(f"{k}=?" for k in campos)
        conn.execute(f"UPDATE corpus_documentos SET titulo=?, {sets}, fecha_actualizacion=CURRENT_TIMESTAMP WHERE id=?",
                     (titulo, *campos.values(), doc_id))
        _borrar_fragmentos(conn, doc_id)
    else:
        cols = ", ".join(["fuente", "clave", "titulo", *campos])
        doc_id = conn.execute(f"INSERT INTO corpus_documentos ({cols}) VALUES ({','.join('?' * (3 + len(campos)))})",
                              (fuente, clave, titulo, *campos.values())).lastrowid

    trozos: list[tuple[str, str]] = []
    for referencia, texto in secciones:
        if len(texto) < 300 and re.search(r"\(?\s*se deroga|\(?\s*derogad[oa]", texto, re.I):
            continue  # artículos derogados: no aportan y ensucian la búsqueda
        if len(texto) <= MAX_SECCION:
            trozos.append((referencia, texto))
            continue
        partes = fragmentar([(1, texto)])
        for i, p in enumerate(partes, start=1):
            trozos.append((f"{referencia} (parte {i} de {len(partes)})", p.texto))
    # Cada fragmento se vectoriza con su contexto (ley y artículo) para que se encuentre mejor.
    vecs = vectores.vectorizar([f"{titulo}. {ref}. {txt}" for ref, txt in trozos], _carpeta_modelos(config))
    for orden, ((ref, txt), vec) in enumerate(zip(trozos, vecs)):
        fid = conn.execute(
            "INSERT INTO corpus_fragmentos (documento_id, orden, referencia, texto, vector) VALUES (?,?,?,?,?)",
            (doc_id, orden, ref, txt, vec.astype(np.float32).tobytes()),
        ).lastrowid
        conn.execute("INSERT INTO corpus_fts (rowid, texto) VALUES (?, ?)", (fid, f"{titulo} {ref} {txt}"))
    conn.execute("UPDATE corpus_documentos SET fragmentos=? WHERE id=?", (len(trozos), doc_id))
    conn.commit()
    _cache.clear()
    return doc_id


# ── Sincronización ─────────────────────────────────────────────────────────────

def _bitacora(conn, fuente: str) -> int:
    return conn.execute("INSERT INTO corpus_sincronizaciones (fuente) VALUES (?)", (fuente,)).lastrowid


def _cerrar(conn, sid: int, **cuenta) -> None:
    sets = ", ".join(f"{k}=?" for k in cuenta)
    conn.execute(f"UPDATE corpus_sincronizaciones SET {sets}, fin=CURRENT_TIMESTAMP WHERE id=?", (*cuenta.values(), sid))
    conn.commit()


def sincronizar_leyes(config, limite: int | None = None, forzar: bool = False, avisar=print) -> dict:
    conn = conectar(config)
    sid = _bitacora(conn, "ley_federal")
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    leyes = fuentes.listar_leyes()
    avisar(f"Leyes federales en el índice de Diputados: {len(leyes)}")
    for i, ley in enumerate(leyes[:limite] if limite else leyes, start=1):
        previa = conn.execute("SELECT etag FROM corpus_documentos WHERE fuente='ley_federal' AND clave=?",
                              (ley.clave,)).fetchone()
        try:
            pdf, etag, _ = fuentes.descargar_ley(ley, previa["etag"] if previa and not forzar else None)
            if pdf is None:
                cuenta["sin_cambios"] += 1
                continue
            huella = hashlib.sha256(pdf).hexdigest()
            secciones = [(s.referencia, s.texto) for s in fuentes.articulos(fuentes.texto_ley(pdf))]
            _guardar(conn, config, "ley_federal", ley.clave, ley.titulo, secciones,
                     detalle=f"Publicada DOF {ley.publicacion or 's/f'}", fecha_publicacion=ley.publicacion,
                     ultima_reforma=ley.ultima_reforma, url_oficial=ley.url_pdf, etag=etag, huella=huella)
            cuenta["actualizados" if previa else "nuevos"] += 1
            avisar(f"[{i}/{len(leyes)}] {ley.titulo}: {len(secciones)} secciones")
        except Exception as exc:
            cuenta["errores"] += 1
            avisar(f"[{i}/{len(leyes)}] ERROR {ley.clave}: {type(exc).__name__}: {exc}")
    _cerrar(conn, sid, **cuenta)
    conn.close()
    return cuenta


def sincronizar_dof(config, dias: int = 2, avisar=print) -> dict:
    """Trae las notas de los últimos `dias` días (por defecto hoy y ayer, para no perder ediciones vespertinas)."""
    conn = conectar(config)
    sid = _bitacora(conn, "dof")
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    hoy = date.today()
    for d in range(dias):
        dia = hoy - timedelta(days=d)
        try:
            notas = fuentes.notas_del_dia(dia)
        except Exception as exc:
            cuenta["errores"] += 1
            avisar(f"DOF {dia}: ERROR al listar: {type(exc).__name__}")
            continue
        avisar(f"DOF {dia}: {len(notas)} notas")
        for nota in notas:
            if conn.execute("SELECT 1 FROM corpus_documentos WHERE fuente='dof' AND clave=?", (str(nota.codigo),)).fetchone():
                cuenta["sin_cambios"] += 1
                continue
            try:
                texto = fuentes.contenido_nota(nota)
                partes = fragmentar([(1, texto)]) if texto else []
                secciones = [(f"{nota.edicion}, parte {i}" if len(partes) > 1 else nota.edicion, p.texto)
                             for i, p in enumerate(partes, start=1)] or [(nota.edicion, nota.titulo)]
                _guardar(conn, config, "dof", str(nota.codigo), nota.titulo, secciones,
                         detalle=nota.organismo, fecha_publicacion=nota.fecha, url_oficial=nota.url)
                cuenta["nuevos"] += 1
            except Exception as exc:
                cuenta["errores"] += 1
                avisar(f"DOF nota {nota.codigo}: ERROR {type(exc).__name__}")
    _cerrar(conn, sid, **cuenta)
    conn.close()
    return cuenta


# ── Búsqueda ───────────────────────────────────────────────────────────────────

def _matriz(conn):
    firma = tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM corpus_fragmentos").fetchone())
    if _cache.get("firma") == firma:
        return _cache["ids"], _cache["matriz"]
    filas = conn.execute("SELECT id, vector FROM corpus_fragmentos").fetchall()
    ids = np.array([r[0] for r in filas], dtype=np.int64)
    matriz = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in filas]) if filas else np.zeros((0, 1), np.float32)
    _cache.update(firma=firma, ids=ids, matriz=matriz)
    return ids, matriz


def expresion_fts(terminos: list[str]) -> str:
    """Busca por raíz en palabras largas (retroactividad → retroa*) para alcanzar sus variantes."""
    partes = [f'"{t[:6]}"*' if len(t) > 6 else f'"{t}"' for t in terminos]
    return " OR ".join(dict.fromkeys(partes))


def buscar(config, consulta: str, limite: int = 8, fuente: str | None = None) -> list[dict]:
    consulta = (consulta or "").strip()
    if not consulta:
        return []
    conn = conectar(config)
    try:
        terminos = [t for t in re.findall(r"\w+", consulta.lower()) if len(t) > 1]
        rangos: dict[int, float] = {}
        if terminos:
            filas = conn.execute(
                "SELECT rowid FROM corpus_fts WHERE corpus_fts MATCH ? ORDER BY bm25(corpus_fts) LIMIT 60",
                (expresion_fts(terminos),),
            ).fetchall()
            for pos, (fid,) in enumerate(filas):
                rangos[fid] = rangos.get(fid, 0) + 1 / (60 + pos)
        ids, matriz = _matriz(conn)
        if len(ids):
            qv = vectores.vectorizar([consulta], _carpeta_modelos(config))[0]
            sim = matriz @ qv
            for pos, i in enumerate(np.argsort(-sim)[:60]):
                if sim[i] < 0.2:
                    break
                rangos[int(ids[i])] = rangos.get(int(ids[i]), 0) + 1 / (60 + pos)
        bajo = consulta.lower()
        # "artículo 14", "artículos 6 y 7", "arts. 303 y 304"
        numeros = []
        for grupo in re.findall(r"art[íi]?c?u?l?o?s?\.?\s+((?:\d+\s*[oº°]?(?:\s*(?:,|y|e)\s*)?)+)", bajo):
            numeros += re.findall(r"\d+", grupo)
        numeros = list(dict.fromkeys(numeros))[:4]
        for n in numeros:
            # Búsqueda directa del artículo nombrado, en todas las leyes; el refuerzo por ley decide cuál.
            for (fid,) in conn.execute(
                "SELECT id FROM corpus_fragmentos WHERE referencia = ? OR referencia LIKE ? OR referencia LIKE ? "
                "OR referencia LIKE ?",
                (f"Artículo {n}", f"Artículo {n} (%", f"Artículo {n}o%", f"Artículo {n} %"),
            ):
                rangos[fid] = rangos.get(fid, 0) + 1 / 60
        if rangos:
            pide_constitucion = bool(re.search(r"constituci[oó]n|constitucional", bajo))
            raices_q = {t[:6] for t in terminos if len(t) > 3}
            marcas = ",".join("?" * len(rangos))
            for fid, ref, titulo in conn.execute(
                f"SELECT f.id, f.referencia, d.titulo FROM corpus_fragmentos f JOIN corpus_documentos d "
                f"ON d.id = f.documento_id WHERE f.id IN ({marcas})", list(rangos)
            ):
                ref, tit = ref or "", (titulo or "").lower()
                # Los transitorios son enormes y casi nunca son la respuesta, salvo que se pregunte por ellos.
                if ref.startswith("Transitorios") and "transitori" not in bajo:
                    rangos[fid] *= 0.25
                # Si la pregunta nombra la ley, sus artículos pesan más.
                coincide = len(raices_q & {w[:6] for w in re.findall(r"\w+", tit) if len(w) > 3})
                if pide_constitucion and tit.startswith("constituci"):
                    coincide += 2
                rangos[fid] *= 1 + 0.6 * coincide
                # Si nombra el artículo, ese artículo exacto va primero.
                if any(re.match(rf"Artículo {n}(\D|$)", ref) for n in numeros):
                    rangos[fid] *= 4
        resultados = []
        for fid in sorted(rangos, key=rangos.get, reverse=True):
            f = conn.execute(
                """SELECT f.id, f.referencia, f.texto, d.fuente, d.titulo, d.detalle, d.fecha_publicacion,
                          d.ultima_reforma, d.url_oficial
                   FROM corpus_fragmentos f JOIN corpus_documentos d ON d.id = f.documento_id WHERE f.id=?""", (fid,)
            ).fetchone()
            if not f or (fuente and f["fuente"] != fuente):
                continue
            resultados.append({**dict(f), "puntaje": round(rangos[fid], 4)})
            if len(resultados) >= limite:
                break
        return resultados
    finally:
        conn.close()


def resumen(config) -> dict:
    conn = conectar(config)
    try:
        por_fuente = {r["fuente"]: dict(r) for r in conn.execute(
            "SELECT fuente, COUNT(*) AS documentos, COALESCE(SUM(fragmentos), 0) AS fragmentos, "
            "MAX(fecha_actualizacion) AS ultima FROM corpus_documentos GROUP BY fuente")}
        ultimas = [dict(r) for r in conn.execute(
            "SELECT id, fuente, nuevos, actualizados, sin_cambios, errores, "
            "datetime(inicio, 'localtime') AS inicio, datetime(fin, 'localtime') AS fin "
            "FROM corpus_sincronizaciones ORDER BY id DESC LIMIT 6")]
        recientes_dof = [dict(r) for r in conn.execute(
            "SELECT titulo, detalle, fecha_publicacion, url_oficial FROM corpus_documentos "
            "WHERE fuente='dof' AND titulo NOT LIKE '%REF:%' AND COALESCE(detalle, '') NOT LIKE 'AVISOS JUDICIALES%' "
            "AND COALESCE(detalle, '') NOT LIKE 'CONVOCATORIAS%' "
            "ORDER BY fecha_publicacion DESC, id DESC LIMIT 8")]
        reformas = [dict(r) for r in conn.execute(
            "SELECT titulo, ultima_reforma, url_oficial FROM corpus_documentos "
            "WHERE fuente='ley_federal' AND ultima_reforma IS NOT NULL ORDER BY ultima_reforma DESC LIMIT 8")]
        return {"leyes": por_fuente.get("ley_federal", {}), "dof": por_fuente.get("dof", {}),
                "sincronizaciones": ultimas, "recientes_dof": recientes_dof, "reformas": reformas}
    finally:
        conn.close()
