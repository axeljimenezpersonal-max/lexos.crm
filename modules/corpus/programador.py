"""
Actualización automática de la base legal dentro del servidor (Railway no tiene cron con acceso al volumen).

Cada proceso de gunicorn arranca un hilo, pero solo el que obtiene el candado del archivo trabaja:
DOF cada 6 horas (los últimos 15 días la primera vez) y leyes federales una vez al día.
Lo que ya se descargó no se repite: las leyes sin cambios se saltan con su etag.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from modules.corpus import indice

log = logging.getLogger(__name__)

ESPERA_INICIAL = 90          # segundos tras arrancar, para no competir con el healthcheck
REVISAR_CADA = 30 * 60
CADA_DOF = timedelta(hours=6)
CADA_LEYES = timedelta(hours=20)


def _activo() -> bool:
    valor = os.environ.get("BASE_LEGAL_AUTO")
    if valor is not None:
        return valor.strip().lower() in ("1", "true", "si", "sí")
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_NAME"))


def _ruta_estado(config) -> str:
    return os.path.join(config["DATABASE_DIR"], "base-legal-estado.json")


def estado(config) -> dict:
    """Avance de la descarga en curso (para mostrarlo en la página); {} si no hay ninguna."""
    try:
        with open(_ruta_estado(config)) as fh:
            datos = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    # Un estado viejo es de un proceso que ya no existe (reinicio o despliegue)
    try:
        if datetime.now(timezone.utc) - datetime.fromisoformat(datos.get("al", "")) > timedelta(minutes=20):
            return {}
    except ValueError:
        return {}
    return datos if datos.get("estado") == "descargando" else {}


def _escribir(config, **datos) -> None:
    ruta = _ruta_estado(config)
    datos["al"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ruta + ".tmp", "w") as fh:
        json.dump(datos, fh, ensure_ascii=False)
    os.replace(ruta + ".tmp", ruta)


def _ultima(config, fuente: str) -> datetime | None:
    conn = indice.conectar(config)
    try:
        fila = conn.execute("SELECT fin FROM corpus_sincronizaciones WHERE fuente=? AND fin IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (fuente,)).fetchone()
        hay = conn.execute("SELECT COUNT(*) FROM corpus_documentos WHERE fuente=?", (fuente,)).fetchone()[0]
    finally:
        conn.close()
    if not fila or not hay:
        return None
    return datetime.fromisoformat(fila["fin"]).replace(tzinfo=timezone.utc)


def _avisador(config, fuente: str):
    def avisar(mensaje: str) -> None:
        print(f"[base-legal] {mensaje}", flush=True)
        m = re.match(r"\[(\d+)/(\d+)\]", mensaje)
        if m:
            _escribir(config, estado="descargando", fuente=fuente, hecho=int(m.group(1)), total=int(m.group(2)))
        else:
            _escribir(config, estado="descargando", fuente=fuente)
    return avisar


def una_vuelta(config, ahora: datetime | None = None) -> list[str]:
    """Corre lo que toque según la última sincronización exitosa. Devuelve qué se corrió."""
    ahora = ahora or datetime.now(timezone.utc)
    hecho = []
    dof = _ultima(config, "dof")
    if dof is None or ahora - dof >= CADA_DOF:
        indice.sincronizar_dof(config, dias=15 if dof is None else 2, avisar=_avisador(config, "dof"))
        hecho.append("dof")
    leyes = _ultima(config, "ley_federal")
    if leyes is None or ahora - leyes >= CADA_LEYES:
        indice.sincronizar_leyes(config, avisar=_avisador(config, "ley_federal"))
        hecho.append("leyes")
    if hecho:
        _escribir(config, estado="listo")
    return hecho


def _bucle(config) -> None:
    import fcntl
    time.sleep(ESPERA_INICIAL)
    candado = open(os.path.join(config["DATABASE_DIR"], "base-legal.lock"), "w")
    while True:
        try:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)   # otro proceso ya la está actualizando
        except OSError:
            time.sleep(REVISAR_CADA)
            continue
        try:
            una_vuelta(config)
        except Exception as exc:   # una falla de red no debe tumbar el hilo; se reintenta en la siguiente vuelta
            print(f"[base-legal] Falló la actualización: {type(exc).__name__}: {exc}", flush=True)
            _escribir(config, estado="error")
        finally:
            fcntl.flock(candado, fcntl.LOCK_UN)
        time.sleep(REVISAR_CADA)


def iniciar(app) -> None:
    if app.testing or not _activo():
        return
    config = {k: app.config[k] for k in ("DATABASE_DIR", "BOVEDA_DIR")}
    threading.Thread(target=_bucle, args=(config,), name="base-legal", daemon=True).start()
