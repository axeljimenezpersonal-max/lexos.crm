"""Conversaciones del asistente: viven en la base del despacho, con el contenido cifrado."""
from __future__ import annotations

import json

from modules import boveda

ESQUEMA = """
CREATE TABLE IF NOT EXISTS asistente_conversaciones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    asunto_id      INTEGER,
    titulo_cifrado BLOB,
    privado        INTEGER DEFAULT 0,
    fecha_alta     DATETIME DEFAULT CURRENT_TIMESTAMP,
    fecha_mod      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS asistente_mensajes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversacion_id   INTEGER NOT NULL REFERENCES asistente_conversaciones(id) ON DELETE CASCADE,
    rol               TEXT NOT NULL CHECK (rol IN ('usuario', 'asistente')),
    contenido_cifrado BLOB NOT NULL,
    fuentes           TEXT,
    modelo            TEXT,
    fecha             DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_asist_msg_conv ON asistente_mensajes(conversacion_id);
CREATE TABLE IF NOT EXISTS asunto_hallazgos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asunto_id     INTEGER NOT NULL,
    tipo          TEXT NOT NULL DEFAULT 'punto_clave' CHECK (tipo IN ('punto_clave', 'descubrimiento')),
    texto_cifrado BLOB NOT NULL,
    origen        TEXT NOT NULL DEFAULT 'manual' CHECK (origen IN ('manual', 'dictum')),
    autor_id      INTEGER,
    fecha         DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hallazgos_asunto ON asunto_hallazgos(asunto_id);
"""

# Columnas que llegaron después: la conversación compartida del expediente y quién hizo cada pregunta
_COLUMNAS = (("asistente_conversaciones", "compartida", "INTEGER DEFAULT 0"),
             ("asistente_mensajes", "user_id", "INTEGER"))


def asegurar(conn) -> None:
    conn.executescript(ESQUEMA)
    for tabla, col, tipo in _COLUMNAS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")


def crear(conn, slug: str, user_id: int, titulo: str, asunto_id=None, privado=False, compartida=False) -> int:
    asegurar(conn)
    cid = conn.execute(
        "INSERT INTO asistente_conversaciones (user_id, asunto_id, titulo_cifrado, privado, compartida) VALUES (?,?,?,?,?)",
        (user_id, asunto_id, boveda.cifrar_texto(slug, titulo[:120]), int(privado), int(bool(compartida and asunto_id))),
    ).lastrowid
    conn.commit()
    return cid


def obtener(conn, conversacion_id: int, user_id: int, puede_ver_asunto=None):
    """La conversación si es de la persona o, si es la compartida de un expediente, si puede ver ese expediente."""
    asegurar(conn)
    conv = conn.execute("SELECT * FROM asistente_conversaciones WHERE id=?", (conversacion_id,)).fetchone()
    if not conv:
        return None
    if conv["user_id"] == user_id:
        return conv
    if conv["compartida"] and conv["asunto_id"] and puede_ver_asunto and puede_ver_asunto(conv["asunto_id"]):
        return conv
    return None


def compartida_del_asunto(conn, asunto_id: int):
    """La conversación del equipo sobre el expediente: la más reciente."""
    asegurar(conn)
    fila = conn.execute("SELECT id FROM asistente_conversaciones WHERE asunto_id=? AND compartida=1 "
                        "ORDER BY fecha_mod DESC LIMIT 1", (asunto_id,)).fetchone()
    return fila["id"] if fila else None


def agregar(conn, slug: str, conversacion_id: int, rol: str, contenido: str, fuentes=None, modelo=None,
            user_id=None) -> int:
    mid = conn.execute(
        "INSERT INTO asistente_mensajes (conversacion_id, rol, contenido_cifrado, fuentes, modelo, user_id) VALUES (?,?,?,?,?,?)",
        (conversacion_id, rol, boveda.cifrar_texto(slug, contenido),
         boveda.cifrar_texto(slug, json.dumps(fuentes, ensure_ascii=False)).decode() if fuentes else None, modelo,
         user_id),
    ).lastrowid
    conn.execute("UPDATE asistente_conversaciones SET fecha_mod=CURRENT_TIMESTAMP WHERE id=?", (conversacion_id,))
    conn.commit()
    return mid


def mensajes(conn, slug: str, conversacion_id: int) -> list[dict]:
    return [
        {"rol": r["rol"], "texto": boveda.descifrar_texto(slug, r["contenido_cifrado"]),
         "fuentes": json.loads(boveda.descifrar_texto(slug, r["fuentes"].encode())) if r["fuentes"] else [],
         "fecha": r["fecha"], "user_id": r["user_id"]}
        for r in conn.execute(
            "SELECT * FROM asistente_mensajes WHERE conversacion_id=? ORDER BY id", (conversacion_id,))
    ]


def listar(conn, slug: str, user_id: int, limite: int = 40) -> list[dict]:
    asegurar(conn)
    return [
        {"id": r["id"], "titulo": boveda.descifrar_texto(slug, r["titulo_cifrado"]) if r["titulo_cifrado"] else "Conversación",
         "asunto_id": r["asunto_id"], "privado": bool(r["privado"]), "fecha_mod": r["fecha_mod"]}
        for r in conn.execute(
            "SELECT * FROM asistente_conversaciones WHERE user_id=? ORDER BY fecha_mod DESC LIMIT ?", (user_id, limite))
    ]


def eliminar(conn, conversacion_id: int, user_id: int) -> bool:
    asegurar(conn)
    cur = conn.execute("DELETE FROM asistente_conversaciones WHERE id=? AND user_id=?", (conversacion_id, user_id))
    conn.commit()
    return cur.rowcount > 0


# ── Memoria del expediente: puntos clave y descubrimientos que guarda el equipo ──

TIPOS_HALLAZGO = {"punto_clave": "Punto clave", "descubrimiento": "Descubrimiento"}


def hallazgos(conn, slug: str, asunto_id: int, limite: int = 60) -> list[dict]:
    asegurar(conn)
    return [
        {"id": r["id"], "tipo": r["tipo"], "texto": boveda.descifrar_texto(slug, r["texto_cifrado"]),
         "origen": r["origen"], "autor_id": r["autor_id"], "fecha": r["fecha"]}
        for r in conn.execute("SELECT * FROM asunto_hallazgos WHERE asunto_id=? ORDER BY id DESC LIMIT ?",
                              (asunto_id, limite))
    ]


def guardar_hallazgo(conn, slug: str, asunto_id: int, tipo: str, texto: str, autor_id: int, origen: str = "manual") -> int:
    asegurar(conn)
    hid = conn.execute(
        "INSERT INTO asunto_hallazgos (asunto_id, tipo, texto_cifrado, origen, autor_id) VALUES (?,?,?,?,?)",
        (asunto_id, tipo if tipo in TIPOS_HALLAZGO else "punto_clave", boveda.cifrar_texto(slug, texto[:2000]),
         "dictum" if origen == "dictum" else "manual", autor_id),
    ).lastrowid
    conn.commit()
    return hid


def borrar_hallazgo(conn, hallazgo_id: int):
    """Borra y devuelve la fila borrada (para saber de qué expediente era), o None."""
    asegurar(conn)
    fila = conn.execute("SELECT * FROM asunto_hallazgos WHERE id=?", (hallazgo_id,)).fetchone()
    if fila:
        conn.execute("DELETE FROM asunto_hallazgos WHERE id=?", (hallazgo_id,))
        conn.commit()
    return fila
