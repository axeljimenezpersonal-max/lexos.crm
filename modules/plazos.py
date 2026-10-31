"""
Habeas Corpus — modules/plazos.py
Cómputo de términos en días hábiles: salta sábados, domingos, los días de descanso
obligatorio de la Ley Federal del Trabajo (art. 74) y los días inhábiles que el despacho
haya marcado (vacaciones del juzgado, suspensiones de labores). Cada juzgado publica sus
propios días inhábiles: el abogado siempre confirma la fecha con el calendario del tribunal.
"""
from __future__ import annotations

from datetime import date, timedelta

ESQUEMA = """
CREATE TABLE IF NOT EXISTS dias_inhabiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    desde       DATE    NOT NULL,
    hasta       DATE    NOT NULL,
    motivo      TEXT    NOT NULL,
    creado_por  INTEGER,
    fecha_alta  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

MAX_DIAS = 365


def _lunes(anio: int, mes: int, n: int) -> date:
    """El n-ésimo lunes del mes."""
    d = date(anio, mes, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def descansos_oficiales(anio: int) -> dict[date, str]:
    """Días de descanso obligatorio (LFT art. 74) de un año."""
    dias = {
        date(anio, 1, 1): "Año Nuevo",
        _lunes(anio, 2, 1): "Día de la Constitución",
        _lunes(anio, 3, 3): "Natalicio de Benito Juárez",
        date(anio, 5, 1): "Día del Trabajo",
        date(anio, 9, 16): "Día de la Independencia",
        _lunes(anio, 11, 3): "Revolución Mexicana",
        date(anio, 12, 25): "Navidad",
    }
    # Transmisión del Poder Ejecutivo Federal: 1 de octubre cada seis años (2024, 2030…)
    if anio >= 2024 and (anio - 2024) % 6 == 0:
        dias[date(anio, 10, 1)] = "Transmisión del Poder Ejecutivo Federal"
    return dias


def asegurar(tdb) -> None:
    tdb.executescript(ESQUEMA)


def inhabiles_despacho(tdb, desde: date, hasta: date) -> dict[date, str]:
    """Días que el despacho marcó como inhábiles dentro del rango."""
    if tdb is None:
        return {}
    asegurar(tdb)
    salida: dict[date, str] = {}
    for r in tdb.execute(
        "SELECT desde, hasta, motivo FROM dias_inhabiles WHERE date(hasta) >= ? AND date(desde) <= ?",
        (desde.isoformat(), hasta.isoformat()),
    ).fetchall():
        try:
            a, b = date.fromisoformat(str(r["desde"])[:10]), date.fromisoformat(str(r["hasta"])[:10])
        except ValueError:
            continue
        d = max(a, desde)
        while d <= min(b, hasta):
            salida.setdefault(d, r["motivo"])
            d += timedelta(days=1)
    return salida


def motivo_inhabil(d: date, extra: dict[date, str]) -> str | None:
    """Por qué un día no cuenta, o None si es hábil."""
    if d.weekday() == 5:
        return "Sábado"
    if d.weekday() == 6:
        return "Domingo"
    oficial = descansos_oficiales(d.year).get(d)
    if oficial:
        return oficial
    return extra.get(d)


def sumar_dias_habiles(desde: date, dias: int, tdb=None) -> dict:
    """
    Cuenta `dias` hábiles a partir del día siguiente a `desde` (la fecha en que surtió
    efectos la notificación) y devuelve la fecha de vencimiento y los días que se saltaron.
    """
    dias = max(1, min(int(dias), MAX_DIAS))
    extra = inhabiles_despacho(tdb, desde, desde + timedelta(days=dias * 3 + 60))
    saltados, d, contados = [], desde, 0
    while contados < dias:
        d += timedelta(days=1)
        motivo = motivo_inhabil(d, extra)
        if motivo:
            if d.weekday() < 5:
                saltados.append({"fecha": d.isoformat(), "motivo": motivo})
            continue
        contados += 1
    return {"vence": d, "saltados": saltados}
