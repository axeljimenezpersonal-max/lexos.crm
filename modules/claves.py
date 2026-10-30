"""
Contraseñas del equipo y de los clientes del portal.

Se guardan con scrypt y sal propia (werkzeug). Las cuentas viejas tienen un SHA-256 sin sal:
se siguen aceptando y se cambian al formato nuevo la primera vez que la persona entra.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from werkzeug.security import check_password_hash, generate_password_hash

_VIEJA = re.compile(r"[0-9a-f]{64}")


def cifrar(clave: str) -> str:
    return generate_password_hash(clave)


def es_vieja(guardada: str | None) -> bool:
    return bool(guardada) and bool(_VIEJA.fullmatch(guardada))


def coincide(clave: str, guardada: str | None) -> bool:
    if not guardada or not clave:
        return False
    if es_vieja(guardada):
        return hmac.compare_digest(hashlib.sha256(clave.encode()).hexdigest(), guardada)
    try:
        return check_password_hash(guardada, clave)
    except ValueError:   # formato desconocido
        return False
