"""
Base legal de México compartida por todos los despachos (información pública).

Leyes federales y sus reglamentos partidos por artículo, legislación de los 32 estados, tesis y jurisprudencia
del Semanario Judicial y notas del DOF, con búsqueda híbrida (palabras + significado) igual que la memoria del
despacho.

Fuentes (columna `fuente`): 'ley_federal', 'reglamento', 'ley_estatal' (con `entidad`), 'tesis' y 'dof'.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
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
    fuente              TEXT NOT NULL,           -- 'ley_federal' | 'reglamento' | 'ley_estatal' | 'tesis' | 'dof'
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
FUENTES = {
    "ley_federal": "Ley federal", "reglamento": "Reglamento federal", "ley_estatal": "Ley estatal",
    "tesis": "Jurisprudencia y tesis", "dof": "Diario Oficial",
}
# Columnas que se agregaron después (todas opcionales): se crean solas en bases que ya existían.
_COLUMNAS = {
    "corpus_documentos": {
        "entidad": "TEXT",          # estado, solo en 'ley_estatal'
        "tipo_tesis": "TEXT",       # 'jurisprudencia' | 'aislada'
        "epoca": "TEXT",
        "instancia": "TEXT",
        "clave_tesis": "TEXT",      # p. ej. "1a./J. 12/2023 (11a.)"
        "materias": "TEXT",
    },
    # Grupo de vectores: NULL = federal (leyes, reglamentos, DOF); 'tesis'; o el nombre del estado.
    # Así la búsqueda por significado solo carga en memoria lo que toca, no todo el país.
    "corpus_fragmentos": {"grupo": "TEXT"},
}
_INDICES = """
CREATE INDEX IF NOT EXISTS idx_corpus_doc_fuente ON corpus_documentos(fuente, entidad);
CREATE INDEX IF NOT EXISTS idx_corpus_frag_grupo ON corpus_fragmentos(grupo, id);
CREATE INDEX IF NOT EXISTS idx_corpus_frag_ref ON corpus_fragmentos(referencia);
CREATE TABLE IF NOT EXISTS corpus_avance (
    clave TEXT PRIMARY KEY,
    valor TEXT,
    al    DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
_cache: dict = {}
_migradas: set[str] = set()
MAX_GRUPOS_EN_MEMORIA = 4   # federal + tesis + dos estados


def ruta_db(config) -> str:
    return os.path.join(config["DATABASE_DIR"], "corpus_legal.db")


def _migrar(conn) -> None:
    for tabla, columnas in _COLUMNAS.items():
        existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}
        for col, tipo in columnas.items():
            if col not in existentes:
                conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
    conn.executescript(_INDICES)
    conn.commit()


def conectar(config) -> sqlite3.Connection:
    ruta = ruta_db(config)
    conn = sqlite3.connect(ruta, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if ruta not in _migradas:
        conn.executescript(ESQUEMA)
        _migrar(conn)
        _migradas.add(ruta)
    return conn


def avance(conn, clave: str, valor: str | None = None) -> str | None:
    """Lee (o guarda, si se da `valor`) el avance de una descarga por tandas."""
    if valor is None:
        fila = conn.execute("SELECT valor FROM corpus_avance WHERE clave=?", (clave,)).fetchone()
        return fila[0] if fila else None
    conn.execute("INSERT INTO corpus_avance (clave, valor, al) VALUES (?, ?, CURRENT_TIMESTAMP) "
                 "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, al=excluded.al", (clave, valor))
    conn.commit()
    return valor


def _carpeta_modelos(config) -> str:
    return os.path.join(config["BOVEDA_DIR"], "_modelos")


# ── Guardado ───────────────────────────────────────────────────────────────────

def _borrar_fragmentos(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM corpus_fts WHERE rowid IN (SELECT id FROM corpus_fragmentos WHERE documento_id=?)", (doc_id,))
    conn.execute("DELETE FROM corpus_fragmentos WHERE documento_id=?", (doc_id,))


def _guardar(conn, config, fuente: str, clave: str, titulo: str, secciones: list[tuple[str, str]], **campos) -> int:
    """secciones: (referencia, texto). Reemplaza los fragmentos del documento."""
    grupo = campos.get("entidad") if fuente == "ley_estatal" else ("tesis" if fuente == "tesis" else None)
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
        sin_notas = "\n".join(l for l in texto.split("\n") if not fuentes._NOTA_REFORMA.match(l))
        if fuente not in ("tesis", "dof") and len(sin_notas) < 300 and re.search(r"\(?\s*se deroga|\(?\s*derogad[oa]", sin_notas, re.I):
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
            "INSERT INTO corpus_fragmentos (documento_id, orden, referencia, texto, vector, grupo) VALUES (?,?,?,?,?,?)",
            (doc_id, orden, ref, txt, vec.astype(np.float32).tobytes(), grupo),
        ).lastrowid
        conn.execute("INSERT INTO corpus_fts (rowid, texto) VALUES (?, ?)", (fid, f"{titulo} {ref} {txt}"))
    conn.execute("UPDATE corpus_documentos SET fragmentos=? WHERE id=?", (len(trozos), doc_id))
    conn.commit()
    _cache.pop(grupo, None)
    return doc_id


# ── Sincronización ─────────────────────────────────────────────────────────────

def _bitacora(conn, fuente: str) -> int:
    return conn.execute("INSERT INTO corpus_sincronizaciones (fuente) VALUES (?)", (fuente,)).lastrowid


def _cerrar(conn, sid: int, **cuenta) -> None:
    sets = ", ".join(f"{k}=?" for k in cuenta)
    conn.execute(f"UPDATE corpus_sincronizaciones SET {sets}, fin=CURRENT_TIMESTAMP WHERE id=?", (*cuenta.values(), sid))
    conn.commit()


def _sincronizar_pdfs(config, fuente: str, lista, limite, forzar, avisar, hasta, nombre: str) -> dict:
    """Leyes y reglamentos de Diputados: PDF por documento, con etag para no bajar lo que no cambió."""
    conn = conectar(config)
    sid = _bitacora(conn, fuente)
    cuenta = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0}
    avisar(f"{nombre} en el índice de Diputados: {len(lista)}")
    total = len(lista[:limite] if limite else lista)
    for i, ley in enumerate(lista[:limite] if limite else lista, start=1):
        if hasta and time.monotonic() > hasta:
            avisar(f"{nombre}: se acabó el tiempo de esta vuelta; sigue en la próxima ({i - 1} de {total}).")
            break
        previa = conn.execute("SELECT etag FROM corpus_documentos WHERE fuente=? AND clave=?",
                              (fuente, ley.clave)).fetchone()
        try:
            pdf, etag, _ = fuentes.descargar_ley(ley, previa["etag"] if previa and not forzar else None)
            if pdf is None:
                # El PDF no cambió; el título o la fecha de reforma del índice sí pueden haberse corregido.
                conn.execute("UPDATE corpus_documentos SET titulo=?, ultima_reforma=? WHERE fuente=? AND clave=?",
                             (ley.titulo, ley.ultima_reforma, fuente, ley.clave))
                conn.commit()
                cuenta["sin_cambios"] += 1
                continue
            huella = hashlib.sha256(pdf).hexdigest()
            secciones = [(s.referencia, s.texto) for s in fuentes.articulos(fuentes.texto_ley(pdf))]
            _guardar(conn, config, fuente, ley.clave, ley.titulo, secciones,
                     detalle=f"Publicada DOF {ley.publicacion or 's/f'}", fecha_publicacion=ley.publicacion,
                     ultima_reforma=ley.ultima_reforma, url_oficial=ley.url_pdf, etag=etag, huella=huella)
            cuenta["actualizados" if previa else "nuevos"] += 1
            avisar(f"[{i}/{total}] {ley.titulo}: {len(secciones)} secciones")
        except Exception as exc:
            cuenta["errores"] += 1
            avisar(f"[{i}/{total}] ERROR {ley.clave}: {type(exc).__name__}: {exc}")
    _cerrar(conn, sid, **cuenta)
    conn.close()
    return cuenta


def sincronizar_leyes(config, limite: int | None = None, forzar: bool = False, avisar=print,
                      hasta: float | None = None) -> dict:
    return _sincronizar_pdfs(config, "ley_federal", fuentes.listar_leyes(), limite, forzar, avisar, hasta,
                             "Leyes federales")


def sincronizar_reglamentos(config, limite: int | None = None, forzar: bool = False, avisar=print,
                            hasta: float | None = None) -> dict:
    """Reglamentos de leyes federales (Diputados, regla.htm). `hasta`: límite de tiempo (time.monotonic())."""
    return _sincronizar_pdfs(config, "reglamento", fuentes.listar_reglamentos(), limite, forzar, avisar, hasta,
                             "Reglamentos federales")


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

def _matriz(conn, grupo: str | None):
    """Vectores de un grupo (federal, tesis o un estado), en memoria mientras no cambien."""
    firma = tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM corpus_fragmentos WHERE grupo IS ?",
                               (grupo,)).fetchone())
    guardado = _cache.get(grupo)
    if guardado and guardado[0] == firma:
        _cache[grupo] = _cache.pop(grupo)            # recién usado: al final
        return guardado[1], guardado[2]
    filas = conn.execute("SELECT id, vector FROM corpus_fragmentos WHERE grupo IS ?", (grupo,)).fetchall()
    ids = np.array([r[0] for r in filas], dtype=np.int64)
    matriz = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in filas]) if filas else np.zeros((0, 1), np.float32)
    _cache.pop(grupo, None)
    _cache[grupo] = (firma, ids, matriz)
    while len(_cache) > MAX_GRUPOS_EN_MEMORIA:
        _cache.pop(next(iter(_cache)))               # el que lleva más tiempo sin usarse
    return ids, matriz


def expresion_fts(terminos: list[str]) -> str:
    """Busca por raíz en palabras largas (retroactividad → retroa*) para alcanzar sus variantes."""
    partes = [f'"{t[:6]}"*' if len(t) > 6 else f'"{t}"' for t in terminos]
    return " OR ".join(dict.fromkeys(partes))


def _filtro(fuente: str | None, entidad: str | None, preferir: str | None) -> tuple[str, list]:
    """Condición SQL sobre corpus_documentos (alias d) según los filtros."""
    partes, params = [], []
    if fuente:
        partes.append("d.fuente = ?")
        params.append(fuente)
    if entidad:
        partes.append("d.entidad = ?")
        params.append(entidad)
    elif fuente != "ley_estatal":
        # Sin estado elegido no se mezclan leyes de otros estados: solo las del estado del asunto, si lo hay.
        partes.append("(d.entidad IS NULL OR d.entidad = ?)" if preferir else "d.entidad IS NULL")
        params += [preferir] if preferir else []
    return " AND ".join(partes) or "1", params


def buscar(config, consulta: str, limite: int = 8, fuente: str | None = None, entidad: str | None = None,
           preferir_entidad: str | None = None) -> list[dict]:
    """
    Búsqueda híbrida en la base legal.
      fuente: solo esa fuente ('ley_federal', 'reglamento', 'ley_estatal', 'tesis', 'dof').
      entidad: solo leyes de ese estado.
      preferir_entidad: el estado del asunto; sus leyes entran y pesan un poco más, sin dejar fuera lo federal.
    Si la pregunta nombra un estado ("código civil de Jalisco"), se toma como estado preferido.
    """
    from modules.corpus.estatal import ENTIDADES, entidad_en
    consulta = (consulta or "").strip()
    if not consulta:
        return []
    fuente = fuente if fuente in FUENTES else None
    entidad = entidad if entidad in ENTIDADES.values() else None
    nombrada = entidad_en(consulta)
    preferir = entidad or nombrada or (preferir_entidad if preferir_entidad in ENTIDADES.values() else None)
    donde, params = _filtro(fuente, entidad, preferir)
    conn = conectar(config)
    try:
        terminos = [t for t in re.findall(r"\w+", consulta.lower()) if len(t) > 1]
        rangos: dict[int, float] = {}
        if terminos:
            filas = conn.execute(
                "SELECT corpus_fts.rowid FROM corpus_fts JOIN corpus_fragmentos f ON f.id = corpus_fts.rowid "
                f"JOIN corpus_documentos d ON d.id = f.documento_id WHERE corpus_fts MATCH ? AND {donde} "
                "ORDER BY bm25(corpus_fts) LIMIT 60",
                (expresion_fts(terminos), *params),
            ).fetchall()
            for pos, (fid,) in enumerate(filas):
                rangos[fid] = rangos.get(fid, 0) + 1 / (60 + pos)
        # Por significado: solo los grupos de vectores que pueden responder
        grupos: list[str | None] = []
        if fuente in (None, "ley_federal", "reglamento", "dof") and not entidad:
            grupos.append(None)
        if fuente in (None, "tesis") and not entidad:
            grupos.append("tesis")
        if fuente in (None, "ley_estatal") and preferir:
            grupos.append(preferir)
        if grupos:
            qv = vectores.vectorizar([consulta], _carpeta_modelos(config))[0]
            for grupo in grupos:
                ids, matriz = _matriz(conn, grupo)
                if not len(ids):
                    continue
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
            # "Artículo 14", "Artículo 14 Bis", "Artículo 14 (parte 1 de 2)", "Artículo 1o", "Artículo 1º"
            for prefijo in (f"Artículo {n} ", f"Artículo {n}o", f"Artículo {n}º", f"Artículo {n}°", None):
                cond, valores = (("f.referencia >= ? AND f.referencia < ?", (prefijo, prefijo + "\uffff"))
                                 if prefijo else ("f.referencia = ?", (f"Artículo {n}",)))
                for (fid,) in conn.execute(
                    "SELECT f.id FROM corpus_fragmentos f JOIN corpus_documentos d ON d.id = f.documento_id "
                    f"WHERE {cond} AND {donde}", (*valores, *params),
                ):
                    rangos[fid] = rangos.get(fid, 0) + 1 / 60
        if rangos:
            pide_constitucion = bool(re.search(r"constituci[oó]n|constitucional", bajo))
            raices_q = {t[:6] for t in terminos if len(t) > 3}
            marcas = ",".join("?" * len(rangos))
            for fid, ref, titulo, ent in conn.execute(
                f"SELECT f.id, f.referencia, d.titulo, d.entidad FROM corpus_fragmentos f JOIN corpus_documentos d "
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
                # "artículo 14 constitucional" es el de la Constitución, no el artículo 14 de una ley
                # reglamentaria "del artículo 5o. constitucional".
                if pide_constitucion and tit.startswith("constituci") and numeros:
                    rangos[fid] *= 2 if not ent or ent == nombrada else 1
                # La ley del estado del asunto pesa un poco más que la federal equivalente.
                # (Salvo "constitucional" a secas, que en México es la Constitución federal.)
                if ent and ent == preferir and not pide_constitucion:
                    rangos[fid] *= 1.3
                # Si nombra el artículo, ese artículo exacto va primero.
                if any(re.match(rf"Artículo {n}(\D|$)", ref) for n in numeros):
                    rangos[fid] *= 4
        resultados = []
        for fid in sorted(rangos, key=rangos.get, reverse=True):
            f = conn.execute(
                f"""SELECT f.id, f.referencia, f.texto, d.fuente, d.titulo, d.detalle, d.fecha_publicacion,
                          d.ultima_reforma, d.url_oficial, d.entidad, d.tipo_tesis, d.epoca, d.instancia,
                          d.clave_tesis, d.clave
                   FROM corpus_fragmentos f JOIN corpus_documentos d ON d.id = f.documento_id
                   WHERE f.id=? AND {donde}""", (fid, *params)
            ).fetchone()
            if not f:
                continue
            resultados.append({**dict(f), "fuente_nombre": FUENTES.get(f["fuente"], f["fuente"]),
                               "puntaje": round(rangos[fid], 4)})
            if len(resultados) >= limite:
                break
        return resultados
    finally:
        conn.close()


def cita(r: dict) -> str:
    """Cómo se cita un resultado: «Código Civil del Estado de Jalisco, art. 1234» o la tesis con su registro."""
    if r.get("fuente") == "tesis":
        partes = [f"Tesis {r['clave_tesis']}" if r.get("clave_tesis") else "Tesis",
                  f"registro digital {r.get('clave')}" if r.get("clave") else ""]
        return ", ".join(p for p in partes if p)
    ref = r.get("referencia") or ""
    ref = re.sub(r"^Artículo ", "art. ", ref)
    return f"{r.get('titulo')}, {ref}" if ref else (r.get("titulo") or "")


def resumen(config) -> dict:
    from modules.corpus.estatal import ENTIDADES
    conn = conectar(config)
    try:
        por_fuente = {r["fuente"]: dict(r) for r in conn.execute(
            "SELECT fuente, COUNT(*) AS documentos, COALESCE(SUM(fragmentos), 0) AS fragmentos, "
            "COUNT(DISTINCT entidad) AS entidades, MAX(fecha_actualizacion) AS ultima "
            "FROM corpus_documentos GROUP BY fuente")}
        por_entidad = {r[0]: r[1] for r in conn.execute(
            "SELECT entidad, COUNT(*) FROM corpus_documentos WHERE fuente='ley_estatal' GROUP BY entidad")}
        jurisprudencias = conn.execute(
            "SELECT COUNT(*) FROM corpus_documentos WHERE fuente='tesis' AND tipo_tesis='jurisprudencia'").fetchone()[0]
        ultimas = [dict(r) for r in conn.execute(
            "SELECT id, fuente, nuevos, actualizados, sin_cambios, errores, nota, "
            "datetime(inicio, 'localtime') AS inicio, datetime(fin, 'localtime') AS fin "
            "FROM corpus_sincronizaciones ORDER BY id DESC LIMIT 6")]
        recientes_dof = [dict(r) for r in conn.execute(
            "SELECT titulo, detalle, fecha_publicacion, url_oficial FROM corpus_documentos "
            "WHERE fuente='dof' AND titulo NOT LIKE '%REF:%' AND COALESCE(detalle, '') NOT LIKE 'AVISOS JUDICIALES%' "
            "AND COALESCE(detalle, '') NOT LIKE 'CONVOCATORIAS%' "
            "ORDER BY fecha_publicacion DESC, id DESC LIMIT 8")]
        reformas = [dict(r) for r in conn.execute(
            "SELECT titulo, ultima_reforma, url_oficial, fuente, entidad FROM corpus_documentos "
            "WHERE fuente IN ('ley_federal', 'reglamento') AND ultima_reforma IS NOT NULL "
            "ORDER BY ultima_reforma DESC LIMIT 8")]
        tesis_bloqueada = avance(conn, "tesis.bloqueado")
        return {"leyes": por_fuente.get("ley_federal", {}), "dof": por_fuente.get("dof", {}),
                "reglamentos": por_fuente.get("reglamento", {}), "estatales": por_fuente.get("ley_estatal", {}),
                "tesis": {**por_fuente.get("tesis", {}), "jurisprudencias": jurisprudencias,
                          "bloqueada": tesis_bloqueada},
                "entidades": [{"nombre": n, "documentos": por_entidad.get(n, 0)} for n in ENTIDADES.values()],
                "sincronizaciones": ultimas, "recientes_dof": recientes_dof, "reformas": reformas}
    finally:
        conn.close()
