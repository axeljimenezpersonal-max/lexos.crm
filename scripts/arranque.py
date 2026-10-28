"""
Habeas Corpus — scripts/arranque.py
Se ejecuta antes de levantar el servidor (Railway): crea las carpetas del volumen y,
la primera vez, las bases de datos con los despachos de demostración.

Variables:
  DEMO_PASSWORD  contraseña para todos los usuarios de demostración (@*.lexos.mx).
                 Se aplica en cada arranque, así que basta con cambiar la variable.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from config import Config  # noqa: E402


def main() -> None:
    os.makedirs(Config.DATABASE_DIR, exist_ok=True)
    os.makedirs(Config.BOVEDA_DIR, exist_ok=True)

    primera_vez = not os.path.exists(Config.GLOBAL_DB)
    if primera_vez:
        print("[arranque] Base de datos nueva: creando despachos de demostración…", flush=True)
        import init_db
        sys.argv = ["init_db.py"]
        init_db.main()

    clave = os.environ.get("DEMO_PASSWORD", "")
    if clave:
        con = sqlite3.connect(Config.GLOBAL_DB)
        n = con.execute("UPDATE tenant_users SET password_hash=? WHERE email LIKE '%.lexos.mx'",
                        (hashlib.sha256(clave.encode()).hexdigest(),)).rowcount
        con.commit()
        con.close()
        print(f"[arranque] Contraseña de demostración aplicada a {n} usuarios", flush=True)

    if primera_vez:
        # Casos concluidos, sentencias y machotes de ejemplo (no detiene el arranque si falla)
        r = subprocess.run([sys.executable, os.path.join(RAIZ, "scripts", "demo_precedentes.py"),
                            "--tenant", "bufete-ramirez"], cwd=RAIZ)
        print(f"[arranque] Datos de precedentes: {'listos' if r.returncode == 0 else 'fallaron, se puede repetir a mano'}", flush=True)


if __name__ == "__main__":
    main()
