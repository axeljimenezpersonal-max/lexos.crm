"""
Bóveda de documentos.

Los archivos se guardan fuera de /static, en una carpeta por despacho, cifrados con una
llave propia de cada despacho (derivada de la llave maestra). Solo se entregan por
/archivos/... a usuarios del mismo despacho, o a un cliente del portal cuando el archivo
se le compartió o es una factura suya.
"""
from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import uuid

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import Blueprint, abort, current_app, g, request, send_file, session

from modules.auth import sesion_del_despacho

boveda_bp = Blueprint("boveda", __name__)

PREFIJO = "/archivos/"
EXTENSIONES = {
    "pdf", "doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "ods", "csv",
    "ppt", "pptx", "png", "jpg", "jpeg", "gif", "webp", "heic", "tif", "tiff", "xml", "zip",
}
# Se muestran en el navegador; todo lo demás se descarga. Nunca HTML ni SVG en línea.
EN_LINEA = {"pdf", "png", "jpg", "jpeg", "gif", "webp"}
_SEGMENTO = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9]+)?$")


# ── Llaves ─────────────────────────────────────────────────────────────────────

def _llave_maestra() -> bytes:
    valor = current_app.config.get("BOVEDA_MASTER_KEY", "")
    if not valor:
        if not current_app.debug:
            raise RuntimeError("Falta BOVEDA_MASTER_KEY: la bóveda no puede cifrar sin llave maestra.")
        valor = _llave_desarrollo()
    llave = base64.urlsafe_b64decode(valor)
    if len(llave) != 32:
        raise RuntimeError("BOVEDA_MASTER_KEY debe ser de 32 bytes en base64 urlsafe.")
    return llave


def _llave_desarrollo() -> str:
    """Solo en desarrollo: una llave local en la carpeta de la bóveda, con permisos 600."""
    base = current_app.config["BOVEDA_DIR"]
    os.makedirs(base, exist_ok=True)
    ruta = os.path.join(base, ".llave-desarrollo")
    if not os.path.exists(ruta):
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.urlsafe_b64encode(os.urandom(32)).decode())
    with open(ruta) as fh:
        return fh.read().strip()


def _cifrador(slug: str) -> Fernet:
    derivada = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=b"habeas-corpus/boveda/v1", info=slug.encode(),
    ).derive(_llave_maestra())
    return Fernet(base64.urlsafe_b64encode(derivada))


# ── Rutas en disco ─────────────────────────────────────────────────────────────

def _slug_actual() -> str:
    slug = getattr(g, "tenant_slug", "") or ""
    if not slug or not _SEGMENTO.match(slug):
        abort(404)
    return slug


def _ruta_disco(url: str, slug: str) -> str:
    if not url or not url.startswith(PREFIJO):
        raise ValueError("No es un archivo de la bóveda")
    partes = url[len(PREFIJO):].split("/")
    if not partes or any(not _SEGMENTO.match(p) for p in partes):
        raise ValueError("Ruta no válida")
    base = os.path.realpath(os.path.join(current_app.config["BOVEDA_DIR"], slug))
    destino = os.path.realpath(os.path.join(base, *partes)) + ".enc"
    if not destino.startswith(base + os.sep):
        raise ValueError("Ruta fuera de la bóveda")
    return destino


def es_de_boveda(url: str | None) -> bool:
    return bool(url) and url.startswith(PREFIJO)


# ── API para los módulos ───────────────────────────────────────────────────────

def guardar(archivo, area: str, *subcarpetas, extensiones=None) -> dict | None:
    """Cifra y guarda un archivo subido. Devuelve url, nombre, tamanio, mime y ext, o None si no es válido."""
    if not archivo or not archivo.filename:
        return None
    ext = os.path.splitext(archivo.filename)[1].lower().lstrip(".")
    if ext not in (extensiones or EXTENSIONES):
        return None
    datos = archivo.read()
    slug = _slug_actual()
    segmentos = [area, *(str(s) for s in subcarpetas if str(s)), f"{uuid.uuid4().hex}.{ext}"]
    url = PREFIJO + "/".join(segmentos)
    destino = _ruta_disco(url, slug)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as fh:
        fh.write(_cifrador(slug).encrypt(datos))
    return {
        "url": url,
        "nombre": archivo.filename,
        "tamanio": len(datos),
        "mime": archivo.mimetype or mimetypes.guess_type(archivo.filename)[0] or "application/octet-stream",
        "ext": ext,
    }


def leer(url: str) -> bytes:
    return leer_de(_slug_actual(), url)


def leer_de(slug: str, url: str) -> bytes:
    """Lee un archivo de un despacho explícito (procesos en segundo plano, con app_context)."""
    with open(_ruta_disco(url, slug), "rb") as fh:
        return _cifrador(slug).decrypt(fh.read())


def cifrar_texto(slug: str, texto: str) -> bytes:
    return _cifrador(slug).encrypt(texto.encode("utf-8"))


def descifrar_texto(slug: str, dato: bytes) -> str:
    return _cifrador(slug).decrypt(dato).decode("utf-8")


def eliminar(url: str | None) -> None:
    if not es_de_boveda(url):
        return
    slug = _slug_actual()
    try:
        os.remove(_ruta_disco(url, slug))
    except (FileNotFoundError, ValueError):
        pass
    from modules.lector import indice
    indice.olvidar(g.tenant_db, url)


def respuesta(url: str, nombre: str | None = None, descargar: bool = False):
    """Respuesta HTTP con el archivo descifrado."""
    try:
        datos = leer(url)
    except (FileNotFoundError, ValueError, InvalidToken):
        abort(404)
    ext = url.rsplit(".", 1)[-1].lower()
    nombre = nombre or url.rsplit("/", 1)[-1]
    resp = send_file(
        io.BytesIO(datos),
        mimetype=mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream",
        as_attachment=descargar or ext not in EN_LINEA,
        download_name=nombre,
        max_age=0,
    )
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ── Control de acceso ──────────────────────────────────────────────────────────

def _cliente_portal_puede(url: str) -> bool:
    if session.get("portal_tenant") != getattr(g, "tenant_slug", None):
        return False
    cid = session.get("portal_cliente_id")
    tdb = getattr(g, "tenant_db", None)
    if not cid or tdb is None:
        return False
    compartido = tdb.execute(
        "SELECT 1 FROM portal_docs_compartidos WHERE cliente_id=? AND archivo_url=? LIMIT 1", (cid, url)
    ).fetchone()
    factura = tdb.execute(
        "SELECT 1 FROM cobros WHERE cliente_id=? AND (archivo_factura_url=? OR archivo_xml_url=?) LIMIT 1",
        (cid, url, url),
    ).fetchone()
    return bool(compartido or factura)


def _nombre_original(url: str) -> str | None:
    tdb = getattr(g, "tenant_db", None)
    if tdb is None:
        return None
    for tabla, col_nombre in (
        ("documentos", "archivo_nombre"), ("cliente_documentos", "archivo_nombre"),
        ("asunto_sentencias", "archivo_nombre"), ("biblioteca_archivos", "nombre_original"),
        ("biblioteca_plantillas", "archivo_nombre"), ("portal_docs_compartidos", "archivo_nombre"),
    ):
        try:
            fila = tdb.execute(f"SELECT {col_nombre} FROM {tabla} WHERE archivo_url=? LIMIT 1", (url,)).fetchone()
        except Exception:
            continue
        if fila and fila[0]:
            return fila[0]
    return None


@boveda_bp.route("/archivos/<path:rel>")
def servir(rel: str):
    if getattr(g, "tenant", None) is None:
        abort(404)
    url = PREFIJO + rel
    if not (sesion_del_despacho() or _cliente_portal_puede(url)):
        abort(404)
    return respuesta(url, _nombre_original(url), descargar=request.args.get("descargar") == "1")
