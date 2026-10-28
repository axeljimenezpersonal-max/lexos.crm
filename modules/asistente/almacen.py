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
"""


def asegurar(conn) -> None:
    conn.executescript(ESQUEMA)


def crear(conn, slug: str, user_id: int, titulo: str, asunto_id=None, privado=False) -> int:
    asegurar(conn)
    cid = conn.execute(
        "INSERT INTO asistente_conversaciones (user_id, asunto_id, titulo_cifrado, privado) VALUES (?,?,?,?)",
        (user_id, asunto_id, boveda.cifrar_texto(slug, titulo[:120]), int(privado)),
    ).lastrowid
    conn.commit()
    return cid


def obtener(conn, conversacion_id: int, user_id: int):
    asegurar(conn)
    return conn.execute(
        "SELECT * FROM asistente_conversaciones WHERE id=? AND user_id=?", (conversacion_id, user_id)
    ).fetchone()


def agregar(conn, slug: str, conversacion_id: int, rol: str, contenido: str, fuentes=None, modelo=None) -> None:
    conn.execute(
        "INSERT INTO asistente_mensajes (conversacion_id, rol, contenido_cifrado, fuentes, modelo) VALUES (?,?,?,?,?)",
        (conversacion_id, rol, boveda.cifrar_texto(slug, contenido),
         boveda.cifrar_texto(slug, json.dumps(fuentes, ensure_ascii=False)).decode() if fuentes else None, modelo),
    )
    conn.execute("UPDATE asistente_conversaciones SET fecha_mod=CURRENT_TIMESTAMP WHERE id=?", (conversacion_id,))
    conn.commit()


def mensajes(conn, slug: str, conversacion_id: int) -> list[dict]:
    return [
        {"rol": r["rol"], "texto": boveda.descifrar_texto(slug, r["contenido_cifrado"]),
         "fuentes": json.loads(boveda.descifrar_texto(slug, r["fuentes"].encode())) if r["fuentes"] else [],
         "fecha": r["fecha"]}
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
