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
    eventos_demo()

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


# Agenda de demostración: (expediente, título, tipo, días hábiles desde hoy, hora o None = todo el día, lugar)
EVENTOS_DEMO = (
    (3, "Reunión con cliente — revisión estrategia", "reunion", 1, "17:00", "Oficina"),
    (2, "Plazo para contestar demanda", "plazo", 2, None, None),
    (1, "Audiencia inicial — Divorcio Contencioso Fuentes-Gómez", "audiencia", 3, "10:30", "Juzgado Tercero de lo Familiar"),
    (3, "Vence término para rendir alegatos", "vencimiento", 5, None, None),
    (1, "Desahogar vista sobre el convenio de custodia", "fecha_limite", 7, None, None),
    (3, "Audiencia constitucional", "audiencia", 8, "12:00", "Juzgado Segundo de Distrito"),
    (2, "Diligencia de emplazamiento al tercero", "diligencia", 10, "09:00", None),
)


INHABILES = {(1, 1), (2, 5), (3, 21), (5, 1), (9, 16), (11, 2), (11, 20), (12, 25)}


def _dia_habil(desde, n: int):
    """Suma n días hábiles (lunes a viernes, sin los inhábiles fijos)."""
    from datetime import timedelta
    d = desde
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and (d.month, d.day) not in INHABILES:
            n -= 1
    return d


def eventos_demo() -> None:
    """Mantiene la agenda del despacho de demostración en las próximas dos semanas.
    Solo mueve los eventos de EVENTOS_DEMO que ya pasaron o faltan; no toca lo demás."""
    from datetime import date
    import init_db
    try:
        tdb = init_db.get_tenant_conn(Config.ACCESO_PRUEBA_DESPACHO)
    except Exception:
        return
    hoy, creados = date.today(), 0
    try:
        abiertos = {r[0] for r in tdb.execute("SELECT id FROM asuntos WHERE estado='activo'")}
        for asunto_id, titulo, tipo, dias, hora, lugar in EVENTOS_DEMO:
            if asunto_id not in abiertos:
                continue
            vigente = tdb.execute(
                """SELECT 1 FROM eventos_calendario WHERE asunto_id=? AND titulo=? AND estado='pendiente'
                   AND date(fecha_inicio) >= ?""", (asunto_id, titulo, hoy.isoformat())).fetchone()
            if vigente:
                continue
            viejos = "SELECT id FROM eventos_calendario WHERE asunto_id=? AND titulo=?"
            tdb.execute(f"DELETE FROM evento_participantes WHERE evento_id IN ({viejos})", (asunto_id, titulo))
            tdb.execute("DELETE FROM eventos_calendario WHERE asunto_id=? AND titulo=?", (asunto_id, titulo))
            dia = _dia_habil(hoy, dias).isoformat()
            ev = tdb.execute(
                """INSERT INTO eventos_calendario (titulo, tipo, asunto_id, fecha_inicio, todo_el_dia,
                                                  lugar, recordatorio, creado_por)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (titulo, tipo, asunto_id, f"{dia} {hora or '09:00'}:00", 0 if hora else 1, lugar, 1440, 3)).lastrowid
            tdb.execute("INSERT INTO evento_participantes (evento_id, user_id) VALUES (?, 3)", (ev,))
            creados += 1
        tdb.commit()
    finally:
        tdb.close()
    if creados:
        print(f"[arranque] Agenda de demostración: {creados} eventos puestos al día", flush=True)


if __name__ == "__main__":
    main()
