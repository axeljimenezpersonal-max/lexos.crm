"""
Habeas Corpus — scripts/restaurar.py
Recupera un respaldo hecho con scripts/respaldo.py.

Por defecto NO toca nada del sistema: descifra el respaldo en una carpeta aparte para revisarlo.
Con --en-sitio lo pone en las carpetas reales (DATABASE_DIR y BOVEDA_DIR); lo que había se mueve
a _antes_de_restaurar/<fecha>/, nada se borra. Hazlo con la app detenida.

Uso:
    python scripts/restaurar.py --listar
    python scripts/restaurar.py --ultimo                              # extrae a ./restauracion-<fecha>/
    python scripts/restaurar.py --archivo respaldo-2026-10-29_030000.tar.gz.enc --salida /tmp/revisar
    python scripts/restaurar.py --archivo /ruta/al/respaldo.tar.gz.enc   # un archivo ya descargado
    python scripts/restaurar.py --ultimo --despacho bufete-ramirez --en-sitio --confirmar

Necesita la misma BOVEDA_MASTER_KEY con la que se hizo el respaldo.
"""
import argparse
import os
import shutil
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from modules import respaldos  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Restaurar un respaldo de Habeas Corpus")
    p.add_argument("--destino", help="dónde están los respaldos (por defecto RESPALDO_DESTINO)")
    p.add_argument("--listar", action="store_true", help="listar los respaldos disponibles")
    p.add_argument("--ultimo", action="store_true", help="usar el respaldo más reciente")
    p.add_argument("--archivo", help="nombre del respaldo en el destino, o ruta a un archivo local")
    p.add_argument("--salida", help="carpeta donde extraer (por defecto ./restauracion-<fecha>)")
    p.add_argument("--despacho", help="restaurar solo la base y los documentos de este despacho (slug)")
    p.add_argument("--en-sitio", action="store_true", help="poner lo restaurado en las carpetas reales")
    p.add_argument("--confirmar", action="store_true", help="obligatorio junto con --en-sitio")
    args = p.parse_args()

    try:
        local = args.archivo and os.path.isfile(args.archivo)
        destino = None if local else respaldos.obtener_destino(args.destino)
        if not local and destino is None:
            print("AVISO: no hay destino configurado (RESPALDO_DESTINO o --destino) ni un archivo local.")
            return 1
        if args.listar:
            for n in destino.listar():
                print(n)
            return 0
        if args.en_sitio and not args.confirmar:
            print("Restaurar en sitio reemplaza las bases actuales. Detén la app y repite con --confirmar.")
            return 1

        with tempfile.TemporaryDirectory(prefix="hc-descarga-") as tmp:
            if local:
                archivo, nombre = args.archivo, os.path.basename(args.archivo)
            else:
                disponibles = destino.listar()
                if not disponibles:
                    print(f"No hay respaldos en {destino.descripcion}.")
                    return 1
                nombre = disponibles[-1] if args.ultimo or not args.archivo else args.archivo
                if nombre not in disponibles:
                    print(f"No existe {nombre} en {destino.descripcion}. Usa --listar.")
                    return 1
                archivo = os.path.join(tmp, nombre)
                print(f"Descargando {nombre} de {destino.descripcion}…")
                destino.bajar(nombre, archivo)

            fecha = nombre.replace("respaldo-", "").split(".")[0]
            salida = args.salida or (os.path.join(tmp, "extraido") if args.en_sitio
                                     else os.path.join(os.getcwd(), f"restauracion-{fecha}"))
            manifiesto = respaldos.abrir_respaldo(archivo, salida, despacho=args.despacho)
            print(f"Respaldo del {manifiesto['creado']} descifrado y verificado "
                  f"({len(manifiesto['bases'])} bases y {len(manifiesto['boveda'])} documentos en total).")
            if args.en_sitio:
                respaldos.colocar_en_sitio(salida)
                if args.salida is None:
                    shutil.rmtree(salida, ignore_errors=True)
            else:
                print(f"Quedó en {salida} (carpetas bases/ y boveda/). No se tocó el sistema.")
        return 0
    except (respaldos.FaltaConfiguracion, respaldos.RespaldoInvalido) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
