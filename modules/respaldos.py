"""
Respaldos cifrados de las bases de datos y de la bóveda de documentos.

Un respaldo es un solo archivo, respaldo-AAAA-MM-DD_HHMMSS.tar.gz.enc, que contiene:
  manifiesto.json   lista de archivos con su huella SHA-256
  bases/            copia consistente de cada base SQLite (API de backup de sqlite)
  boveda/<slug>/    los archivos de la bóveda tal cual (ya van cifrados con la llave del despacho)

El paquete completo se cifra con AES-256-GCM en bloques, con una llave derivada de
BOVEDA_MASTER_KEY. Sin esa llave no se puede abrir el respaldo ni leer los documentos.

Destino (RESPALDO_DESTINO): una carpeta local o un bucket S3-compatible (s3://bucket/prefijo).
Retención: los 7 días más recientes con respaldo y, antes de esos, una copia por semana de 4 semanas.

Los scripts de uso están en scripts/respaldo.py y scripts/restaurar.py. Este módulo también
expone POST /interno/respaldo para que un cron de Railway pida el respaldo (ver docs/seguridad-y-respaldos.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import tarfile
import tempfile
import threading
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import Blueprint, abort, current_app, jsonify, request

from config import Config

MAGIA = b"HCRESP01"
TAM_BLOQUE = 4 * 1024 * 1024
DIARIOS, SEMANALES = 7, 4
NOMBRE = re.compile(r"^respaldo-(\d{4}-\d{2}-\d{2}_\d{6})\.tar\.gz\.enc$")
CORPUS = "corpus_legal.db"          # base legal pública: se puede reconstruir, no se respalda por defecto
APARTADO = "_antes_de_restaurar"    # adonde se mueve lo que había antes de restaurar en sitio


class FaltaConfiguracion(Exception):
    """Falta una variable o dependencia; el mensaje explica cuál y qué hacer."""


class RespaldoInvalido(Exception):
    """El archivo no es un respaldo, está dañado o la llave no corresponde."""


# ─────────────────────────────────────────────────────────────────────────────
#  Llave
# ─────────────────────────────────────────────────────────────────────────────

def llave_maestra(boveda_dir: str | None = None) -> tuple[bytes, str]:
    """Devuelve (llave, de dónde salió). En local usa la llave de desarrollo de la bóveda."""
    valor, origen = os.environ.get("BOVEDA_MASTER_KEY", "") or Config.BOVEDA_MASTER_KEY, "BOVEDA_MASTER_KEY"
    if not valor:
        ruta = os.path.join(boveda_dir or Config.BOVEDA_DIR, ".llave-desarrollo")
        if not os.path.exists(ruta):
            raise FaltaConfiguracion(
                "Falta BOVEDA_MASTER_KEY: sin la llave maestra no se puede cifrar ni abrir el respaldo.")
        with open(ruta) as fh:
            valor, origen = fh.read().strip(), "llave de desarrollo local"
    try:
        llave = base64.urlsafe_b64decode(valor)
    except ValueError:
        llave = b""
    if len(llave) != 32:
        raise FaltaConfiguracion("BOVEDA_MASTER_KEY debe ser de 32 bytes en base64 urlsafe.")
    return llave, origen


def _llave_paquete(maestra: bytes, sal: bytes) -> AESGCM:
    derivada = HKDF(algorithm=hashes.SHA256(), length=32, salt=sal,
                    info=b"habeas-corpus/respaldos/v1").derive(maestra)
    return AESGCM(derivada)


# ─────────────────────────────────────────────────────────────────────────────
#  Cifrado del paquete: MAGIA | sal(16) | bloques [largo(4) | nonce(12) | cifrado+etiqueta]
#  Cada bloque autentica su número y si es el último: no se pueden reordenar ni recortar.
# ─────────────────────────────────────────────────────────────────────────────

def cifrar_archivo(origen: str, destino: str, maestra: bytes) -> None:
    sal = os.urandom(16)
    aes = _llave_paquete(maestra, sal)
    with open(origen, "rb") as fi, open(destino, "wb") as fo:
        fo.write(MAGIA + sal)
        indice, bloque = 0, fi.read(TAM_BLOQUE)
        while True:
            siguiente = fi.read(TAM_BLOQUE)
            final = not siguiente
            nonce = os.urandom(12)
            cifrado = aes.encrypt(nonce, bloque, MAGIA + sal + struct.pack(">QB", indice, final))
            fo.write(struct.pack(">I", len(cifrado)) + nonce + cifrado)
            if final:
                return
            indice, bloque = indice + 1, siguiente


def descifrar_archivo(origen: str, destino: str, maestra: bytes) -> None:
    with open(origen, "rb") as fi, open(destino, "wb") as fo:
        cabecera = fi.read(len(MAGIA) + 16)
        if not cabecera.startswith(MAGIA) or len(cabecera) < len(MAGIA) + 16:
            raise RespaldoInvalido("El archivo no es un respaldo de Habeas Corpus.")
        sal = cabecera[len(MAGIA):]
        aes = _llave_paquete(maestra, sal)
        indice, largo = 0, fi.read(4)
        while True:
            if len(largo) != 4:
                raise RespaldoInvalido("El respaldo está incompleto (se cortó antes del final).")
            nonce = fi.read(12)
            cifrado = fi.read(struct.unpack(">I", largo)[0])
            largo = fi.read(4)
            final = not largo
            try:
                fo.write(aes.decrypt(nonce, cifrado, MAGIA + sal + struct.pack(">QB", indice, final)))
            except InvalidTag:
                raise RespaldoInvalido(
                    "No se pudo abrir el respaldo: la llave no corresponde o el archivo está dañado.") from None
            if final:
                return
            indice += 1


# ─────────────────────────────────────────────────────────────────────────────
#  Destinos
# ─────────────────────────────────────────────────────────────────────────────

class DestinoLocal:
    def __init__(self, carpeta: str):
        self.carpeta = os.path.abspath(os.path.expanduser(carpeta))
        self.descripcion = f"carpeta {self.carpeta}"

    def subir(self, ruta: str, nombre: str) -> None:
        os.makedirs(self.carpeta, exist_ok=True)
        temporal = os.path.join(self.carpeta, f".{nombre}.subiendo")
        shutil.copyfile(ruta, temporal)
        os.replace(temporal, os.path.join(self.carpeta, nombre))

    def listar(self) -> list[str]:
        if not os.path.isdir(self.carpeta):
            return []
        return sorted(n for n in os.listdir(self.carpeta) if NOMBRE.match(n))

    def bajar(self, nombre: str, ruta: str) -> None:
        shutil.copyfile(os.path.join(self.carpeta, nombre), ruta)

    def borrar(self, nombre: str) -> None:
        os.remove(os.path.join(self.carpeta, nombre))


class DestinoS3:
    """Cualquier servicio compatible con S3: AWS, Cloudflare R2, Backblaze B2, DigitalOcean Spaces…"""

    def __init__(self, url: str):
        try:
            import boto3
        except ImportError:
            raise FaltaConfiguracion("Para respaldar en S3 hace falta el paquete boto3 (pip install boto3).") from None
        self.bucket, _, prefijo = url[len("s3://"):].partition("/")
        if not self.bucket:
            raise FaltaConfiguracion("RESPALDO_DESTINO debe verse así: s3://nombre-del-bucket/carpeta")
        self.prefijo = prefijo.strip("/")
        self.descripcion = f"bucket s3://{self.bucket}/{self.prefijo}"
        self.cliente = boto3.client(
            "s3",
            endpoint_url=Config.RESPALDO_S3_ENDPOINT or None,
            region_name=Config.RESPALDO_S3_REGION or None,
            aws_access_key_id=os.environ.get("RESPALDO_S3_ACCESS_KEY") or None,
            aws_secret_access_key=os.environ.get("RESPALDO_S3_SECRET_KEY") or None,
        )

    def _llave(self, nombre: str) -> str:
        return f"{self.prefijo}/{nombre}" if self.prefijo else nombre

    def subir(self, ruta: str, nombre: str) -> None:
        self.cliente.upload_file(ruta, self.bucket, self._llave(nombre))

    def listar(self) -> list[str]:
        nombres, pag = [], self.cliente.get_paginator("list_objects_v2")
        for pagina in pag.paginate(Bucket=self.bucket, Prefix=self._llave("")):
            for obj in pagina.get("Contents", []):
                nombre = obj["Key"].rsplit("/", 1)[-1]
                if NOMBRE.match(nombre):
                    nombres.append(nombre)
        return sorted(nombres)

    def bajar(self, nombre: str, ruta: str) -> None:
        self.cliente.download_file(self.bucket, self._llave(nombre), ruta)

    def borrar(self, nombre: str) -> None:
        self.cliente.delete_object(Bucket=self.bucket, Key=self._llave(nombre))


def obtener_destino(valor: str | None = None):
    """Destino configurado, o None si RESPALDO_DESTINO no está puesto."""
    valor = (valor if valor is not None else os.environ.get("RESPALDO_DESTINO", Config.RESPALDO_DESTINO)).strip()
    if not valor:
        return None
    if valor.startswith("s3://"):
        return DestinoS3(valor)
    if valor.startswith("file://"):
        valor = valor[len("file://"):]
    return DestinoLocal(valor)


# ─────────────────────────────────────────────────────────────────────────────
#  Crear
# ─────────────────────────────────────────────────────────────────────────────

def _sha256(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for trozo in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


def _bases_a_respaldar(bases_dir: str) -> list[str]:
    incluir_corpus = os.environ.get("RESPALDO_INCLUIR_CORPUS", "").lower() in ("1", "true", "si", "sí")
    return sorted(n for n in os.listdir(bases_dir)
                  if n.endswith(".db") and (incluir_corpus or n != CORPUS)
                  and os.path.isfile(os.path.join(bases_dir, n)))


def _archivos_boveda(boveda_dir: str) -> list[str]:
    """Rutas relativas de los archivos de los despachos. Se omiten la llave de desarrollo,
    los modelos descargados (_modelos) y lo apartado por restauraciones (carpetas con _ o .)."""
    if not os.path.isdir(boveda_dir):
        return []
    rutas = []
    for carpeta in sorted(os.listdir(boveda_dir)):
        base = os.path.join(boveda_dir, carpeta)
        if carpeta.startswith(("_", ".")) or not os.path.isdir(base) or os.path.islink(base):
            continue
        for raiz, dirs, archivos in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for a in sorted(archivos):
                if not a.startswith("."):
                    rutas.append(os.path.relpath(os.path.join(raiz, a), boveda_dir))
    return rutas


def _copiar_base(origen: str, destino: str) -> None:
    """Copia consistente aunque la app esté escribiendo (API de backup de sqlite)."""
    fuente = sqlite3.connect(f"file:{origen}?mode=ro", uri=True, timeout=60)
    copia = sqlite3.connect(destino)
    try:
        fuente.backup(copia)
        estado = copia.execute("PRAGMA quick_check").fetchone()[0]
        if estado != "ok":
            raise RespaldoInvalido(f"La copia de {os.path.basename(origen)} no pasó la revisión: {estado}")
    finally:
        copia.close()
        fuente.close()


def _agregar_bytes(tar: tarfile.TarFile, nombre: str, datos: bytes) -> None:
    info = tarfile.TarInfo(nombre)
    info.size, info.mtime, info.mode = len(datos), int(datetime.now().timestamp()), 0o600
    tar.addfile(info, io.BytesIO(datos))


def crear_respaldo(destino, bases_dir: str | None = None, boveda_dir: str | None = None,
                   ahora: datetime | None = None, log=print) -> dict:
    """Arma, cifra y sube un respaldo; después aplica la retención. Devuelve un resumen."""
    bases_dir, boveda_dir = bases_dir or Config.DATABASE_DIR, boveda_dir or Config.BOVEDA_DIR
    maestra, origen_llave = llave_maestra(boveda_dir)
    if origen_llave != "BOVEDA_MASTER_KEY":
        log(f"Aviso: se usa la {origen_llave}; en producción debe estar BOVEDA_MASTER_KEY.")
    ahora = ahora or datetime.now(timezone.utc)
    nombre = f"respaldo-{ahora.strftime('%Y-%m-%d_%H%M%S')}.tar.gz.enc"
    manifiesto = {"formato": 1, "sistema": "Habeas Corpus", "creado": ahora.isoformat(timespec="seconds"),
                  "bases": {}, "boveda": {}}

    with tempfile.TemporaryDirectory(prefix="hc-respaldo-") as tmp:
        # 1. Bases primero: un documento subido durante el respaldo queda de más, nunca de menos.
        os.makedirs(os.path.join(tmp, "bases"))
        for n in _bases_a_respaldar(bases_dir):
            copia = os.path.join(tmp, "bases", n)
            _copiar_base(os.path.join(bases_dir, n), copia)
            manifiesto["bases"][n] = {"sha256": _sha256(copia), "bytes": os.path.getsize(copia)}

        # 2. Paquete tar.gz
        paquete = os.path.join(tmp, "paquete.tar.gz")
        with tarfile.open(paquete, "w:gz", compresslevel=6) as tar:
            for n in manifiesto["bases"]:
                tar.add(os.path.join(tmp, "bases", n), arcname=f"bases/{n}")
            for rel in _archivos_boveda(boveda_dir):
                ruta = os.path.join(boveda_dir, rel)
                try:
                    huella, tam = _sha256(ruta), os.path.getsize(ruta)
                    tar.add(ruta, arcname=f"boveda/{rel}")
                except FileNotFoundError:
                    continue  # se borró mientras respaldábamos
                manifiesto["boveda"][rel] = {"sha256": huella, "bytes": tam}
            _agregar_bytes(tar, "manifiesto.json", json.dumps(manifiesto, indent=1).encode())

        # 3. Cifrar y subir
        cifrado = os.path.join(tmp, nombre)
        cifrar_archivo(paquete, cifrado, maestra)
        tamanio = os.path.getsize(cifrado)
        destino.subir(cifrado, nombre)

    borrados = aplicar_retencion(destino)
    resumen = {"nombre": nombre, "bytes": tamanio, "bases": len(manifiesto["bases"]),
               "archivos": len(manifiesto["boveda"]), "destino": destino.descripcion, "borrados": borrados}
    log(f"Respaldo listo: {nombre} ({tamanio / 1024 / 1024:.1f} MB, {resumen['bases']} bases, "
        f"{resumen['archivos']} documentos) en {destino.descripcion}")
    if borrados:
        log(f"Retención: se borraron {len(borrados)} respaldos viejos: {', '.join(borrados)}")
    return resumen


def _fecha(nombre: str) -> datetime:
    return datetime.strptime(NOMBRE.match(nombre).group(1), "%Y-%m-%d_%H%M%S")


def a_conservar(nombres: list[str], diarios: int = DIARIOS, semanales: int = SEMANALES) -> set[str]:
    """El más reciente de cada uno de los últimos `diarios` días con respaldo y, de lo anterior
    a esos días, el más reciente de cada una de `semanales` semanas: 7 + 4 = 11 copias."""
    dias, semanas, conservar = set(), set(), set()
    for nombre in sorted((n for n in nombres if NOMBRE.match(n)), key=_fecha, reverse=True):
        f = _fecha(nombre)
        if len(dias) < diarios:
            if f.date() not in dias:
                dias.add(f.date())
                conservar.add(nombre)
        elif f.date() not in dias:
            semana = f.isocalendar()[:2]
            if semana not in semanas and len(semanas) < semanales:
                semanas.add(semana)
                conservar.add(nombre)
    return conservar


def aplicar_retencion(destino) -> list[str]:
    nombres = destino.listar()
    conservar = a_conservar(nombres)
    borrados = [n for n in nombres if n not in conservar]
    for n in borrados:
        destino.borrar(n)
    return borrados


# ─────────────────────────────────────────────────────────────────────────────
#  Restaurar
# ─────────────────────────────────────────────────────────────────────────────

def abrir_respaldo(archivo_cifrado: str, salida: str, boveda_dir: str | None = None,
                   despacho: str | None = None) -> dict:
    """Descifra y extrae el respaldo en `salida` (carpetas bases/ y boveda/), y comprueba
    la huella de cada archivo. Con `despacho` solo extrae la base y los documentos de ese despacho."""
    maestra, _ = llave_maestra(boveda_dir)
    os.makedirs(salida, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hc-restaurar-") as tmp:
        paquete = os.path.join(tmp, "paquete.tar.gz")
        descifrar_archivo(archivo_cifrado, paquete, maestra)
        with tarfile.open(paquete, "r:gz") as tar:
            try:
                manifiesto = json.load(tar.extractfile("manifiesto.json"))
            except KeyError:
                raise RespaldoInvalido("Al respaldo le falta su manifiesto.") from None

            def elegido(rel_base: str | None, rel_boveda: str | None) -> bool:
                if not despacho:
                    return True
                return rel_base == f"tenant_{despacho}.db" or (rel_boveda or "").startswith(f"{despacho}/")

            esperados = {f"bases/{n}": d for n, d in manifiesto["bases"].items() if elegido(n, None)}
            esperados.update({f"boveda/{r}": d for r, d in manifiesto["boveda"].items() if elegido(None, r)})
            if despacho and not esperados:
                raise RespaldoInvalido(f"El respaldo no tiene datos del despacho «{despacho}».")
            miembros = [m for m in tar.getmembers() if m.name in esperados and m.isfile()]
            tar.extractall(salida, members=miembros, filter="data")

    for rel, dato in esperados.items():
        ruta = os.path.join(salida, rel)
        if not os.path.exists(ruta) or _sha256(ruta) != dato["sha256"]:
            raise RespaldoInvalido(f"El archivo {rel} no coincide con el manifiesto: el respaldo está dañado.")
    return manifiesto


def colocar_en_sitio(extraido: str, bases_dir: str | None = None, boveda_dir: str | None = None,
                     log=print) -> dict:
    """Pone lo extraído en las carpetas reales. Nada se borra: lo que había se mueve a
    <carpeta>/_antes_de_restaurar/<fecha>/. Debe hacerse con la app detenida."""
    bases_dir, boveda_dir = bases_dir or Config.DATABASE_DIR, boveda_dir or Config.BOVEDA_DIR
    sello = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    resumen = {"bases": 0, "archivos": 0, "apartados": 0}

    origen_bases = os.path.join(extraido, "bases")
    if os.path.isdir(origen_bases):
        apartado = os.path.join(bases_dir, APARTADO, sello)
        os.makedirs(bases_dir, exist_ok=True)
        for n in sorted(os.listdir(origen_bases)):
            for sufijo in ("", "-wal", "-shm", "-journal"):
                actual = os.path.join(bases_dir, n + sufijo)
                if os.path.exists(actual):
                    os.makedirs(apartado, exist_ok=True)
                    shutil.move(actual, os.path.join(apartado, n + sufijo))
                    resumen["apartados"] += 1
            shutil.copyfile(os.path.join(origen_bases, n), os.path.join(bases_dir, n))
            resumen["bases"] += 1

    origen_boveda = os.path.join(extraido, "boveda")
    if os.path.isdir(origen_boveda):
        apartado = os.path.join(boveda_dir, APARTADO, sello)
        for raiz, _, archivos in os.walk(origen_boveda):
            for a in archivos:
                rel = os.path.relpath(os.path.join(raiz, a), origen_boveda)
                fuente, actual = os.path.join(origen_boveda, rel), os.path.join(boveda_dir, rel)
                if os.path.exists(actual):
                    if _sha256(actual) == _sha256(fuente):
                        continue
                    os.makedirs(os.path.dirname(os.path.join(apartado, rel)), exist_ok=True)
                    shutil.move(actual, os.path.join(apartado, rel))
                    resumen["apartados"] += 1
                os.makedirs(os.path.dirname(actual), exist_ok=True)
                shutil.copyfile(fuente, actual)
                resumen["archivos"] += 1

    log(f"Restaurado: {resumen['bases']} bases y {resumen['archivos']} documentos. "
        f"Lo que había antes ({resumen['apartados']} archivos) quedó en {APARTADO}/{sello}.")
    return resumen


# ─────────────────────────────────────────────────────────────────────────────
#  Disparador HTTP para el cron de Railway: POST /interno/respaldo
#  (el volumen solo está montado en el servicio web, así que el respaldo corre ahí)
# ─────────────────────────────────────────────────────────────────────────────

respaldos_bp = Blueprint("respaldos", __name__)
_en_curso = threading.Lock()


def _ruta_estado() -> str:
    return os.path.join(current_app.config["DATABASE_DIR"], "respaldo-estado.json")


def _escribir_estado(ruta: str, **datos) -> None:
    temporal = ruta + ".tmp"
    with open(temporal, "w") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=1)
    os.replace(temporal, ruta)


def _autorizado() -> None:
    token = current_app.config.get("RESPALDO_TOKEN", "")
    enviado = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(enviado.encode(), token.encode()):
        abort(404)


def _correr(ruta_estado: str, inicio: str) -> None:
    import fcntl
    candado = open(ruta_estado + ".lock", "w")
    try:
        fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)   # otro worker de gunicorn ya está respaldando
    except OSError:
        candado.close()
        return
    try:
        _escribir_estado(ruta_estado, estado="en_curso", inicio=inicio)
        destino = obtener_destino()
        if destino is None:
            raise FaltaConfiguracion("Falta RESPALDO_DESTINO: no se hizo el respaldo.")
        resumen = crear_respaldo(destino, log=lambda m: print(f"[respaldo] {m}", flush=True))
        _escribir_estado(ruta_estado, estado="ok", inicio=inicio,
                         fin=datetime.now(timezone.utc).isoformat(timespec="seconds"), **resumen)
    except Exception as exc:  # el estado debe reflejar cualquier falla
        print(f"[respaldo] Falló: {exc}", flush=True)
        _escribir_estado(ruta_estado, estado="error", inicio=inicio, mensaje=str(exc),
                         fin=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finally:
        fcntl.flock(candado, fcntl.LOCK_UN)
        candado.close()
        _en_curso.release()


@respaldos_bp.route("/interno/respaldo", methods=["POST"])
def pedir_respaldo():
    _autorizado()
    if not _en_curso.acquire(blocking=False):
        return jsonify(estado="en_curso"), 409
    inicio = datetime.now(timezone.utc).isoformat(timespec="seconds")
    threading.Thread(target=_correr, args=(_ruta_estado(), inicio), daemon=True).start()
    return jsonify(estado="iniciado", inicio=inicio), 202


@respaldos_bp.route("/interno/respaldo", methods=["GET"])
def estado_respaldo():
    _autorizado()
    try:
        with open(_ruta_estado()) as fh:
            return jsonify(json.load(fh))
    except FileNotFoundError:
        return jsonify(estado="sin_respaldos")
