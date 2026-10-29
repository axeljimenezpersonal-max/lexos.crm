"""
Habeas Corpus — scripts/simulacro_respaldo.py
Simulacro de desastre, de punta a punta, sin tocar los datos reales:

  1. Copia las bases y la bóveda actuales a una carpeta temporal («producción de prueba»).
  2. Hace un respaldo cifrado de esa copia a otra carpeta temporal.
  3. Borra la copia de prueba por completo (solo sobrevive la llave, como en la vida real).
  4. Restaura desde el respaldo y comprueba que todo quedó idéntico: cada base, cada documento,
     y que un documento de la bóveda se puede descifrar.
  5. Comprueba además la retención (7 diarios + 4 semanales anteriores), que una llave equivocada no abre
     el respaldo y que un respaldo alterado se detecta.

Uso:  python scripts/simulacro_respaldo.py     (sale con 0 si todo salió bien)
"""
import base64
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from config import Config  # noqa: E402
from modules import respaldos  # noqa: E402


def huella_base(ruta: str) -> str:
    con = sqlite3.connect(ruta)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", f"{ruta} está dañada"
        h = hashlib.sha256()
        for linea in con.iterdump():
            h.update(linea.encode())
        return h.hexdigest()
    finally:
        con.close()


def huellas(bases: str, boveda: str) -> dict:
    datos = {f"bases/{n}": huella_base(os.path.join(bases, n)) for n in respaldos._bases_a_respaldar(bases)}
    datos.update({f"boveda/{r}": respaldos._sha256(os.path.join(boveda, r)) for r in respaldos._archivos_boveda(boveda)})
    return datos


def paso(texto: str) -> None:
    print(f"  ✓ {texto}", flush=True)


def probar_documento(boveda: str, rel: str) -> None:
    """Descifra un documento restaurado con el mismo código que usa la app."""
    from flask import Flask
    from modules import boveda as mod_boveda
    slug, resto = rel.split("/", 1)
    app = Flask("simulacro")
    app.config.update(BOVEDA_DIR=boveda, BOVEDA_MASTER_KEY=os.environ["BOVEDA_MASTER_KEY"])
    with app.app_context():
        mod_boveda.leer_de(slug, mod_boveda.PREFIJO + resto.removesuffix(".enc"))


def probar_retencion(tmp: str) -> None:
    destino = respaldos.DestinoLocal(os.path.join(tmp, "retencion"))
    os.makedirs(destino.carpeta)
    hoy = datetime(2026, 10, 29, 3, 0, 0)
    for dias in range(60):                      # dos meses de respaldos diarios, dos por día
        for hora in (3, 15):
            f = hoy - timedelta(days=dias) + timedelta(hours=hora - 3)
            open(os.path.join(destino.carpeta, f"respaldo-{f:%Y-%m-%d_%H%M%S}.tar.gz.enc"), "w").close()
    open(os.path.join(destino.carpeta, "otro-archivo.txt"), "w").close()
    respaldos.aplicar_retencion(destino)
    quedan = destino.listar()
    fechas = sorted(respaldos._fecha(n) for n in quedan)
    assert fechas[-1] == hoy + timedelta(hours=12), "el respaldo más nuevo debe conservarse"
    diarios = [f for f in fechas if f >= hoy - timedelta(days=6)]
    semanales = [f for f in fechas if f < hoy - timedelta(days=6)]
    assert len({f.date() for f in diarios}) == len(diarios) == 7, f"deben quedar 7 diarios: {diarios}"
    assert len({f.isocalendar()[:2] for f in semanales}) == len(semanales) == 4, f"deben quedar 4 semanales: {semanales}"
    assert os.path.exists(os.path.join(destino.carpeta, "otro-archivo.txt")), "no debe borrar archivos ajenos"
    paso(f"Retención: de 120 respaldos quedaron {len(quedan)} (7 diarios + 4 semanales); lo ajeno no se toca")


def main() -> int:
    print("Simulacro de respaldo y restauración (no toca los datos reales)\n")
    with tempfile.TemporaryDirectory(prefix="hc-simulacro-") as tmp:
        bases, boveda = os.path.join(tmp, "data"), os.path.join(tmp, "storage")
        os.makedirs(bases)
        os.makedirs(boveda)

        # La llave vive fuera del sistema (variable de entorno), igual que en producción.
        if not os.environ.get("BOVEDA_MASTER_KEY") and not Config.BOVEDA_MASTER_KEY:
            llave, origen = respaldos.llave_maestra(Config.BOVEDA_DIR)
            os.environ["BOVEDA_MASTER_KEY"] = base64.urlsafe_b64encode(llave).decode()
            print(f"  (se usa la {origen})")
        else:
            os.environ.setdefault("BOVEDA_MASTER_KEY", Config.BOVEDA_MASTER_KEY)

        # 1. Producción de prueba
        for n in respaldos._bases_a_respaldar(Config.DATABASE_DIR):
            respaldos._copiar_base(os.path.join(Config.DATABASE_DIR, n), os.path.join(bases, n))
        for rel in respaldos._archivos_boveda(Config.BOVEDA_DIR):
            os.makedirs(os.path.dirname(os.path.join(boveda, rel)), exist_ok=True)
            shutil.copy2(os.path.join(Config.BOVEDA_DIR, rel), os.path.join(boveda, rel))
        antes = huellas(bases, boveda)
        documentos = [r for r in antes if r.startswith("boveda/")]
        paso(f"Copia de prueba: {len(antes) - len(documentos)} bases y {len(documentos)} documentos")

        # 2. Respaldo
        destino = respaldos.DestinoLocal(os.path.join(tmp, "respaldos"))
        resumen = respaldos.crear_respaldo(destino, bases, boveda, log=lambda m: None)
        paso(f"Respaldo cifrado: {resumen['nombre']} ({resumen['bytes'] / 1024:.0f} KB)")

        # 3. Desastre: se pierde todo menos la llave
        shutil.rmtree(bases)
        shutil.rmtree(boveda)
        paso("Copia de prueba borrada por completo")

        # 4. Restaurar desde el destino y comparar
        nombre = destino.listar()[-1]
        descarga = os.path.join(tmp, nombre)
        destino.bajar(nombre, descarga)
        extraido = os.path.join(tmp, "extraido")
        respaldos.abrir_respaldo(descarga, extraido)
        respaldos.colocar_en_sitio(extraido, bases, boveda, log=lambda m: None)
        despues = huellas(bases, boveda)
        faltan = [k for k in antes if despues.get(k) != antes[k]]
        assert not faltan, f"No coinciden después de restaurar: {faltan[:5]}"
        assert set(despues) == set(antes), "Hay archivos de más después de restaurar"
        paso(f"Restauración idéntica: {len(antes)} archivos comparados, todas las bases pasan integrity_check")
        if documentos:
            probar_documento(boveda, documentos[0][len("boveda/"):])
            paso("Un documento restaurado se descifró con la llave de la bóveda")

        # 5. Llave equivocada y archivo alterado
        llave_buena = os.environ["BOVEDA_MASTER_KEY"]
        os.environ["BOVEDA_MASTER_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
        try:
            respaldos.abrir_respaldo(descarga, os.path.join(tmp, "x"))
            raise AssertionError("Una llave equivocada abrió el respaldo")
        except respaldos.RespaldoInvalido:
            paso("Con otra llave el respaldo no se abre")
        finally:
            os.environ["BOVEDA_MASTER_KEY"] = llave_buena
        with open(descarga, "r+b") as fh:
            fh.seek(os.path.getsize(descarga) // 2)
            byte = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([byte[0] ^ 1]))
        try:
            respaldos.abrir_respaldo(descarga, os.path.join(tmp, "y"))
            raise AssertionError("Un respaldo alterado se abrió sin error")
        except respaldos.RespaldoInvalido:
            paso("Un respaldo alterado se detecta y se rechaza")

        probar_retencion(tmp)
    print("\nSIMULACRO EXITOSO: el respaldo se puede restaurar completo.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nSIMULACRO FALLIDO: {exc}")
        sys.exit(1)
