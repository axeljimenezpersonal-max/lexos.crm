"""
Índice de documentos por despacho.

Vive dentro de la base de datos de cada despacho. El texto de cada fragmento se guarda
cifrado con la llave del despacho; para la búsqueda por palabras se usa un índice FTS5
sin contenido (solo guarda términos, no el texto) y para la búsqueda por significado,
vectores calculados localmente.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from modules import boveda
from modules.lector import extraer, vectores

log = logging.getLogger(__name__)

ESTADOS = ("pendiente", "procesando", "listo", "sin_texto", "requiere_ocr", "no_soportado", "error")
TAM_FRAGMENTO = 800
TRASLAPE = 120
_ejecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lector")
_cache_vectores: dict[str, tuple[tuple, np.ndarray, np.ndarray]] = {}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS lector_documentos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    archivo_url   TEXT    NOT NULL UNIQUE,
    nombre        TEXT,
    origen        TEXT,
    origen_id     INTEGER,
    asunto_id     INTEGER,
    cliente_id    INTEGER,
    estado        TEXT    NOT NULL DEFAULT 'pendiente',
    paginas       INTEGER,
    caracteres    INTEGER,
    uso_ocr       INTEGER DEFAULT 0,
    error         TEXT,
    fecha_alta    DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_proceso DATETIME
);
CREATE TABLE IF NOT EXISTS lector_fragmentos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id  INTEGER NOT NULL REFERENCES lector_documentos(id) ON DELETE CASCADE,
    orden         INTEGER NOT NULL,
    pagina        INTEGER,
    texto_cifrado BLOB    NOT NULL,
    vector        BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lector_frag_doc ON lector_fragmentos(documento_id);
CREATE VIRTUAL TABLE IF NOT EXISTS lector_fts USING fts5(
    texto, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2'
);
"""

# Tablas de origen que el lector debe leer: (origen, tabla, columna de nombre, columna asunto, columna cliente)
ORIGENES = (
    ("documento",  "documentos",           "nombre",          "asunto_id", None),
    ("sentencia",  "asunto_sentencias",    "titulo",          "asunto_id", None),
    ("cliente",    "cliente_documentos",   "nombre",          None,        "cliente_id"),
    ("biblioteca", "biblioteca_archivos",  "nombre_original", "asunto_id", "cliente_id"),
    ("plantilla",  "biblioteca_plantillas", "nombre",         None,        None),
)


def asegurar_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript(ESQUEMA)


# ── Fragmentación ──────────────────────────────────────────────────────────────

@dataclass
class Fragmento:
    pagina: int
    texto: str


def _cola(texto: str) -> str:
    """Las últimas oraciones completas del fragmento anterior, para que el siguiente no empiece a media idea."""
    oraciones = [o for o in re.split(r"(?<=[.;:])\s+", texto) if o.strip()]
    cola = ""
    for o in reversed(oraciones[1:]):
        if len(cola) + len(o) > TRASLAPE * 2:
            break
        cola = f"{o} {cola}".strip()
    return cola


def fragmentar(paginas: list[tuple[int, str]]) -> list[Fragmento]:
    fragmentos: list[Fragmento] = []
    actual, pagina_actual = "", None
    for pagina, texto in paginas:
        for parrafo in re.split(r"\n\s*\n|\n", texto):
            parrafo = re.sub(r"\s+", " ", parrafo).strip()
            if not parrafo:
                continue
            if pagina_actual is None:
                pagina_actual = pagina
            while len(parrafo) > TAM_FRAGMENTO:
                corte = parrafo.rfind(" ", 0, TAM_FRAGMENTO)
                if corte <= TRASLAPE:
                    corte = TAM_FRAGMENTO
                if actual:
                    fragmentos.append(Fragmento(pagina_actual, actual))
                    actual = ""
                fragmentos.append(Fragmento(pagina, parrafo[:corte]))
                parrafo = parrafo[max(0, corte - TRASLAPE):]
                pagina_actual = pagina
            if len(actual) + len(parrafo) + 1 > TAM_FRAGMENTO and actual:
                fragmentos.append(Fragmento(pagina_actual, actual))
                actual, pagina_actual = _cola(actual), pagina
            actual = f"{actual} {parrafo}".strip()
    if actual.strip():
        fragmentos.append(Fragmento(pagina_actual or 1, actual))
    return fragmentos


# ── Cola ───────────────────────────────────────────────────────────────────────

def _carpeta_modelos(app) -> str:
    return os.path.join(app.config["BOVEDA_DIR"], "_modelos")


def _conexion(slug: str) -> sqlite3.Connection:
    from config import Config
    conn = sqlite3.connect(Config.tenant_db(slug), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def registrar(conn, url: str, nombre: str, origen: str, origen_id=None, asunto_id=None, cliente_id=None) -> int:
    asegurar_esquema(conn)
    conn.execute(
        """INSERT INTO lector_documentos (archivo_url, nombre, origen, origen_id, asunto_id, cliente_id, estado)
           VALUES (?,?,?,?,?,?,'pendiente')
           ON CONFLICT(archivo_url) DO UPDATE SET
             nombre=excluded.nombre, origen=excluded.origen, origen_id=excluded.origen_id,
             asunto_id=excluded.asunto_id, cliente_id=excluded.cliente_id""",
        (url, nombre, origen, origen_id, asunto_id, cliente_id),
    )
    conn.commit()
    return conn.execute("SELECT id FROM lector_documentos WHERE archivo_url=?", (url,)).fetchone()[0]


def encolar(app, slug: str, conn, url: str, nombre: str, origen: str,
            origen_id=None, asunto_id=None, cliente_id=None) -> None:
    """Registra el documento y lo manda a leer en segundo plano."""
    if not boveda.es_de_boveda(url):
        return
    doc_id = registrar(conn, url, nombre, origen, origen_id, asunto_id, cliente_id)
    _ejecutor.submit(procesar, app, slug, doc_id)


def encolar_pendientes(app, slug: str, conn, reintentar=("pendiente", "procesando", "requiere_ocr", "error")) -> int:
    """Registra archivos que aún no están en el índice y vuelve a mandar los que no terminaron."""
    asegurar_esquema(conn)
    for origen, tabla, col_nombre, col_asunto, col_cliente in ORIGENES:
        cols = ", ".join(c for c in ("id", "archivo_url", col_nombre, col_asunto, col_cliente) if c)
        try:
            filas = conn.execute(
                f"SELECT {cols} FROM {tabla} WHERE archivo_url LIKE '/archivos/%' "
                "AND archivo_url NOT IN (SELECT archivo_url FROM lector_documentos)"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for f in filas:
            registrar(conn, f["archivo_url"], f[col_nombre], origen, f["id"],
                      f[col_asunto] if col_asunto else None, f[col_cliente] if col_cliente else None)
    marcas = ",".join("?" * len(reintentar))
    ids = [r[0] for r in conn.execute(f"SELECT id FROM lector_documentos WHERE estado IN ({marcas})", reintentar)]
    for doc_id in ids:
        _ejecutor.submit(procesar, app, slug, doc_id)
    return len(ids)


def olvidar(conn, url: str | None) -> None:
    if not url:
        return
    try:
        doc = conn.execute("SELECT id FROM lector_documentos WHERE archivo_url=?", (url,)).fetchone()
    except sqlite3.OperationalError:
        return
    if not doc:
        return
    conn.execute("DELETE FROM lector_fts WHERE rowid IN (SELECT id FROM lector_fragmentos WHERE documento_id=?)", (doc[0],))
    conn.execute("DELETE FROM lector_fragmentos WHERE documento_id=?", (doc[0],))
    conn.execute("DELETE FROM lector_documentos WHERE id=?", (doc[0],))
    conn.commit()


# ── Procesamiento ──────────────────────────────────────────────────────────────

def _estado(conn, doc_id: int, estado: str, **campos) -> None:
    sets = ", ".join([f"{k}=?" for k in campos] + ["estado=?", "fecha_proceso=CURRENT_TIMESTAMP"])
    conn.execute(f"UPDATE lector_documentos SET {sets} WHERE id=?", (*campos.values(), estado, doc_id))
    conn.commit()


def procesar(app, slug: str, doc_id: int) -> None:
    with app.app_context():
        conn = _conexion(slug)
        try:
            asegurar_esquema(conn)
            doc = conn.execute("SELECT * FROM lector_documentos WHERE id=?", (doc_id,)).fetchone()
            if not doc:
                return
            _estado(conn, doc_id, "procesando", error=None)
            ext = doc["archivo_url"].rsplit(".", 1)[-1].lower()
            if ext not in extraer.SOPORTADOS:
                _estado(conn, doc_id, "no_soportado", error=f"Formato .{ext} aún no se puede leer")
                return
            try:
                resultado = extraer.extraer(boveda.leer_de(slug, doc["archivo_url"]), ext)
            except extraer.FaltaOCR:
                _estado(conn, doc_id, "requiere_ocr", error="Es un escaneo: falta instalar el lector OCR")
                return
            frags = fragmentar(resultado.paginas)
            conn.execute("DELETE FROM lector_fts WHERE rowid IN (SELECT id FROM lector_fragmentos WHERE documento_id=?)", (doc_id,))
            conn.execute("DELETE FROM lector_fragmentos WHERE documento_id=?", (doc_id,))
            if not frags:
                _estado(conn, doc_id, "sin_texto", paginas=len(resultado.paginas), caracteres=0,
                        uso_ocr=int(resultado.uso_ocr), error="No se encontró texto legible")
                return
            vecs = vectores.vectorizar([f.texto for f in frags], _carpeta_modelos(app))
            for orden, (frag, vec) in enumerate(zip(frags, vecs)):
                cur = conn.execute(
                    "INSERT INTO lector_fragmentos (documento_id, orden, pagina, texto_cifrado, vector) VALUES (?,?,?,?,?)",
                    (doc_id, orden, frag.pagina, boveda.cifrar_texto(slug, frag.texto), vec.astype(np.float32).tobytes()),
                )
                conn.execute("INSERT INTO lector_fts (rowid, texto) VALUES (?, ?)", (cur.lastrowid, frag.texto))
            _estado(conn, doc_id, "listo", paginas=len(resultado.paginas), caracteres=resultado.caracteres,
                    uso_ocr=int(resultado.uso_ocr), error=None)
            _cache_vectores.pop(slug, None)
        except Exception as exc:
            # Nunca se registra el contenido del documento, solo el tipo de error.
            log.warning("Lector: falló documento %s del despacho %s: %s", doc_id, slug, type(exc).__name__)
            try:
                _estado(conn, doc_id, "error", error=f"No se pudo leer ({type(exc).__name__})")
            except Exception:
                pass
        finally:
            conn.close()


# ── Búsqueda ───────────────────────────────────────────────────────────────────

def _matriz(conn, slug: str):
    firma = tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(f.id), 0) FROM lector_fragmentos f "
        "JOIN lector_documentos d ON d.id = f.documento_id WHERE d.estado='listo'"
    ).fetchone())
    guardado = _cache_vectores.get(slug)
    if guardado and guardado[0] == firma:
        return guardado[1], guardado[2]
    filas = conn.execute(
        "SELECT f.id, f.vector FROM lector_fragmentos f JOIN lector_documentos d ON d.id = f.documento_id "
        "WHERE d.estado='listo'"
    ).fetchall()
    ids = np.array([r[0] for r in filas], dtype=np.int64)
    matriz = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in filas]) if filas else np.zeros((0, 1), np.float32)
    _cache_vectores[slug] = (firma, ids, matriz)
    return ids, matriz


def _resumen(texto: str, terminos: list[str], qv=None, carpeta: str | None = None, largo: int = 340) -> str:
    """La oración más cercana a la consulta (por significado y por palabras), con algo de contexto."""
    oraciones = [o.strip() for o in re.split(r"(?<=[.;:])\s+", texto) if o.strip()]
    if not oraciones:
        return texto[:largo]
    raices = {t[:5] for t in terminos if len(t) > 3}
    coincidencias = [len(raices & {w[:5] for w in re.findall(r"\w+", o.lower())}) for o in oraciones]
    if qv is not None and carpeta and len(oraciones) > 1:
        semantica = vectores.vectorizar(oraciones, carpeta) @ qv
        puntajes = [float(semantica[i]) + 0.05 * coincidencias[i] for i in range(len(oraciones))]
    else:
        puntajes = coincidencias
    mejor = max(range(len(oraciones)), key=lambda i: puntajes[i])
    trozo = oraciones[mejor]
    if len(trozo) < largo and mejor + 1 < len(oraciones):
        trozo = f"{trozo} {oraciones[mejor + 1]}"
    trozo = trozo[:largo].rstrip()
    return ("…" if mejor else "") + trozo + ("…" if len(trozo) >= largo or mejor + 2 < len(oraciones) else "")


def buscar(app, slug: str, conn, consulta: str, limite: int = 8, asunto_id: int | None = None) -> list[dict]:
    asegurar_esquema(conn)
    consulta = (consulta or "").strip()
    if not consulta:
        return []
    terminos = [t for t in re.findall(r"\w+", consulta.lower()) if len(t) > 1]
    rangos: dict[int, float] = {}

    if terminos:
        from modules.corpus.indice import expresion_fts
        expresion = expresion_fts(terminos)
        filas = conn.execute(
            "SELECT rowid FROM lector_fts WHERE lector_fts MATCH ? ORDER BY bm25(lector_fts) LIMIT 50", (expresion,)
        ).fetchall()
        for pos, (fid,) in enumerate(filas):
            rangos[fid] = rangos.get(fid, 0) + 1 / (60 + pos)

    ids, matriz = _matriz(conn, slug)
    qv = None
    if len(ids):
        qv = vectores.vectorizar([consulta], _carpeta_modelos(app))[0]
        similitud = matriz @ qv
        for pos, i in enumerate(np.argsort(-similitud)[:50]):
            if similitud[i] < 0.2:
                break
            fid = int(ids[i])
            rangos[fid] = rangos.get(fid, 0) + 1 / (60 + pos)

    if not rangos:
        return []
    mejores = sorted(rangos, key=rangos.get, reverse=True)
    resultados, vistos = [], set()
    for fid in mejores:
        f = conn.execute(
            """SELECT f.id, f.pagina, f.texto_cifrado, d.id AS doc_id, d.nombre, d.archivo_url, d.origen,
                      d.asunto_id, d.cliente_id
               FROM lector_fragmentos f JOIN lector_documentos d ON d.id = f.documento_id
               WHERE f.id=? AND d.estado='listo'""", (fid,)
        ).fetchone()
        if not f or (asunto_id and f["asunto_id"] != asunto_id):
            continue
        texto = boveda.descifrar_texto(slug, f["texto_cifrado"])
        resumen = _resumen(texto, terminos, qv, _carpeta_modelos(app))
        clave = (f["doc_id"], resumen.strip("…")[:120])
        if clave in vistos:
            continue
        vistos.add(clave)
        resultados.append({
            "fragmento_id": f["id"], "documento": f["nombre"], "url": f["archivo_url"], "origen": f["origen"],
            "pagina": f["pagina"], "asunto_id": f["asunto_id"], "cliente_id": f["cliente_id"],
            "texto": texto, "resumen": resumen, "puntaje": round(rangos[fid], 4),
        })
        if len(resultados) >= limite:
            break
    return resultados


def resumen_estado(conn) -> dict:
    asegurar_esquema(conn)
    cuenta = {e: 0 for e in ESTADOS}
    for estado, n in conn.execute("SELECT estado, COUNT(*) FROM lector_documentos GROUP BY estado"):
        cuenta[estado] = n
    cuenta["fragmentos"] = conn.execute("SELECT COUNT(*) FROM lector_fragmentos").fetchone()[0]
    return cuenta
