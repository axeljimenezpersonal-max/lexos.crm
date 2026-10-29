"""
Habeas Corpus — scripts/respaldo.py
Hace un respaldo cifrado de todas las bases y de la bóveda de documentos, lo sube al destino
configurado y borra los viejos (se conservan 7 diarios y, antes de esos, 4 semanales).

Uso:
    python scripts/respaldo.py                       # al destino de RESPALDO_DESTINO
    python scripts/respaldo.py --destino /ruta       # a otra carpeta (o s3://bucket/prefijo)
    python scripts/respaldo.py --listar              # ver los respaldos que hay en el destino

Variables:
    RESPALDO_DESTINO          carpeta local o s3://bucket/prefijo  (sin ella: avisa y no hace nada)
    BOVEDA_MASTER_KEY         llave con la que se cifra el respaldo (la misma de la bóveda)
    RESPALDO_S3_ENDPOINT      solo si no es AWS (R2, B2, Spaces…): URL del servicio
    RESPALDO_S3_REGION        región del bucket (por defecto "auto")
    RESPALDO_S3_ACCESS_KEY / RESPALDO_S3_SECRET_KEY   credenciales del bucket
    RESPALDO_INCLUIR_CORPUS   1 para incluir la base legal pública (pesa cientos de MB)

Códigos de salida: 0 listo (o sin destino configurado), 1 falló.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import respaldos  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Respaldo cifrado de Habeas Corpus")
    parser.add_argument("--destino", help="carpeta o s3://bucket/prefijo (por defecto RESPALDO_DESTINO)")
    parser.add_argument("--listar", action="store_true", help="solo listar los respaldos del destino")
    args = parser.parse_args()

    try:
        destino = respaldos.obtener_destino(args.destino)
        if destino is None:
            print("[respaldo] AVISO: no hay destino configurado (RESPALDO_DESTINO). No se hizo ningún respaldo.")
            return 0
        if args.listar:
            nombres = destino.listar()
            print(f"{len(nombres)} respaldos en {destino.descripcion}:")
            for n in nombres:
                print(f"  {n}")
            return 0
        respaldos.crear_respaldo(destino, log=lambda m: print(f"[respaldo] {m}", flush=True))
        return 0
    except respaldos.FaltaConfiguracion as exc:
        print(f"[respaldo] AVISO: {exc}")
        return 1
    except Exception as exc:
        print(f"[respaldo] ERROR: el respaldo falló: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
