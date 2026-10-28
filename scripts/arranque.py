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

    cuenta_compartida()

    if primera_vez:
        # Casos concluidos, sentencias y machotes de ejemplo (no detiene el arranque si falla)
        r = subprocess.run([sys.executable, os.path.join(RAIZ, "scripts", "demo_precedentes.py"),
                            "--tenant", "bufete-ramirez"], cwd=RAIZ)
        print(f"[arranque] Datos de precedentes: {'listos' if r.returncode == 0 else 'fallaron, se puede repetir a mano'}", flush=True)


def cuenta_compartida() -> None:
    """Crea o actualiza la cuenta de colaboradores (ACCESO_PRUEBA); sin la variable la desactiva."""
    usuario, clave = Config.ACCESO_PRUEBA_USUARIO.strip().lower(), Config.ACCESO_PRUEBA_CLAVE
    con = sqlite3.connect(Config.GLOBAL_DB)
    if not (usuario and clave):
        con.execute("UPDATE tenant_users SET activo=0 WHERE nombre='Equipo' AND apellidos='de prueba'")
        con.commit()
        con.close()
        return
    fila = con.execute("SELECT id FROM tenants WHERE slug=?", (Config.ACCESO_PRUEBA_DESPACHO,)).fetchone()
    if not fila:
        print("[arranque] No existe el despacho de demostración; no se creó la cuenta compartida", flush=True)
        con.close()
        return
    tenant_id, hash_ = fila[0], hashlib.sha256(clave.encode()).hexdigest()
    existe = con.execute("SELECT id FROM tenant_users WHERE tenant_id=? AND LOWER(email)=?", (tenant_id, usuario)).fetchone()
    if existe:
        user_id = existe[0]
        con.execute("UPDATE tenant_users SET password_hash=?, rol='super_admin', activo=1 WHERE id=?", (hash_, user_id))
    else:
        user_id = con.execute(
            "INSERT INTO tenant_users (tenant_id, nombre, apellidos, email, password_hash, rol) VALUES (?,?,?,?,?,?)",
            (tenant_id, "Equipo", "de prueba", usuario, hash_, "super_admin")).lastrowid
    con.commit()
    con.close()

    # Entra directo al sistema, sin el tour de configuración
    import init_db
    tdb = init_db.get_tenant_conn(Config.ACCESO_PRUEBA_DESPACHO)
    tdb.execute("INSERT OR IGNORE INTO onboarding_progress (user_id) VALUES (?)", (user_id,))
    tdb.execute("UPDATE onboarding_progress SET completado=1 WHERE user_id=?", (user_id,))
    tdb.commit()
    tdb.close()
    print(f"[arranque] Cuenta compartida «{usuario}» lista", flush=True)


if __name__ == "__main__":
    main()
