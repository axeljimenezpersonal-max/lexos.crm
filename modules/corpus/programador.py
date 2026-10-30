"""
Actualización automática de la base legal dentro del servidor (Railway no tiene cron con acceso al volumen).

Cada proceso de gunicorn arranca un hilo, pero solo el que obtiene el candado del archivo trabaja:
  - DOF cada 6 horas (los últimos 15 días la primera vez);
  - leyes federales cada 20 horas y reglamentos federales cada día (se saltan con su etag si no cambiaron);
  - leyes de los estados: una pasada completa por semana, por tandas de tiempo limitado (el avance se guarda
    y la siguiente vuelta sigue donde se quedó);
  - tesis del Semanario Judicial: lo nuevo cada día y el histórico por tandas.
Ninguna tanda pasa de TANDA, para no tener el candado (y el servidor) ocupado horas seguidas.
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
CADA_REGLAMENTOS = timedelta(hours=24)
CADA_ESTADOS = timedelta(days=7)
CADA_TESIS = timedelta(hours=24)
TANDA = 20 * 60              # segundos máximos por fuente en cada vuelta


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
        # "[3/40] Jalisco · Código Civil…" → hecho 3 de 40, detalle "Jalisco"
        m = re.match(r"\[(\d+)/(\d+)\]\s*(?:([^·:]{2,40}) · )?", mensaje)
        if m:
            _escribir(config, estado="descargando", fuente=fuente, hecho=int(m.group(1)), total=int(m.group(2)),
                      detalle=(m.group(3) or "").strip() or None)
        else:
            _escribir(config, estado="descargando", fuente=fuente)
    return avisar


def _fecha_avance(config, clave: str) -> datetime | None:
    conn = indice.conectar(config)
    try:
        valor = indice.avance(conn, clave)
    finally:
        conn.close()
    try:
        return datetime.fromisoformat(valor) if valor else None
    except ValueError:
        return None


def _toca_estados(config, ahora: datetime) -> bool:
    """Toca si hay una pasada a medias o si la última completa tiene más de una semana."""
    conn = indice.conectar(config)
    try:
        cursor = int(indice.avance(conn, "estados.cursor") or 0)
    finally:
        conn.close()
    ultima = _fecha_avance(config, "estados.ultima_pasada")
    return cursor > 0 or ultima is None or ahora - ultima >= CADA_ESTADOS


def _toca_tesis(config, ahora: datetime) -> bool:
    bloqueo = _fecha_avance(config, "tesis.bloqueado")
    if bloqueo and ahora - bloqueo < CADA_TESIS:
        return False                       # el SJF nos bloqueó hace poco: no insistir
    conn = indice.conectar(config)
    try:
        atras = int(indice.avance(conn, "tesis.cursor_atras") or 0)
    finally:
        conn.close()
    from modules.corpus.tesis import PRIMER_REGISTRO
    if atras == 0 or atras >= PRIMER_REGISTRO:
        return True                        # falta histórico: una tanda en cada vuelta
    ultima = _ultima(config, "tesis")
    return ultima is None or ahora - ultima >= CADA_TESIS


def una_vuelta(config, ahora: datetime | None = None, fuentes_activas: set[str] | None = None) -> list[str]:
    """Corre lo que toque según la última sincronización exitosa. Devuelve qué se corrió."""
    ahora = ahora or datetime.now(timezone.utc)
    activas = fuentes_activas or _fuentes_activas()
    hecho = []
    dof = _ultima(config, "dof")
    if "dof" in activas and (dof is None or ahora - dof >= CADA_DOF):
        indice.sincronizar_dof(config, dias=15 if dof is None else 2, avisar=_avisador(config, "dof"))
        hecho.append("dof")
    leyes = _ultima(config, "ley_federal")
    if "leyes" in activas and (leyes is None or ahora - leyes >= CADA_LEYES):
        indice.sincronizar_leyes(config, avisar=_avisador(config, "ley_federal"))
        hecho.append("leyes")
    reglamentos = _ultima(config, "reglamento")
    if "reglamentos" in activas and (reglamentos is None or ahora - reglamentos >= CADA_REGLAMENTOS):
        indice.sincronizar_reglamentos(config, avisar=_avisador(config, "reglamento"),
                                       hasta=time.monotonic() + TANDA)
        hecho.append("reglamentos")
    if "estados" in activas and _toca_estados(config, ahora):
        from modules.corpus import estatal
        estatal.sincronizar(config, tipos=_tipos_estatales(config), avisar=_avisador(config, "ley_estatal"),
                            hasta=time.monotonic() + TANDA, pasada=True)
        hecho.append("estados")
    if "tesis" in activas and _toca_tesis(config, ahora):
        from modules.corpus import tesis
        tesis.sincronizar(config, avisar=_avisador(config, "tesis"), hasta=time.monotonic() + TANDA)
        hecho.append("tesis")
    if hecho:
        _escribir(config, estado="listo")
    return hecho


def _fuentes_activas() -> set[str]:
    """BASE_LEGAL_FUENTES=dof,leyes,reglamentos,estados,tesis (por defecto, todas)."""
    valor = os.environ.get("BASE_LEGAL_FUENTES", "").strip()
    todas = {"dof", "leyes", "reglamentos", "estados", "tesis"}
    return {f.strip() for f in valor.split(",") if f.strip() in todas} if valor else todas


def _tipos_estatales(config) -> list[str] | None:
    """BASE_LEGAL_ESTADOS_TIPOS=constitucion,codigo,ley elige a mano. Sin la variable: primero constituciones y
    códigos de todos los estados; las ~3,000 leyes estatales entran cuando esa primera pasada terminó."""
    valor = os.environ.get("BASE_LEGAL_ESTADOS_TIPOS", "").strip()
    tipos = [t.strip() for t in valor.split(",") if t.strip() in ("constitucion", "codigo", "ley")]
    if tipos:
        return tipos
    return None if _fecha_avance(config, "estados.ultima_pasada") else ["constitucion", "codigo"]


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
