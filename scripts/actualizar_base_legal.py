"""
Actualiza la base legal de México (leyes federales y Diario Oficial).

Uso:
    venv/bin/python scripts/actualizar_base_legal.py              # todo: leyes con cambios + DOF de hoy y ayer
    venv/bin/python scripts/actualizar_base_legal.py --solo dof --dias 30
    venv/bin/python scripts/actualizar_base_legal.py --solo leyes --limite 5

Cron sugerido (diario a las 6:30 y 20:30, para alcanzar ediciones vespertinas del DOF):
    30 6,20 * * *  cd /ruta/al/proyecto && venv/bin/python scripts/actualizar_base_legal.py >> logs/base_legal.log 2>&1
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from modules.corpus import indice  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--solo", choices=("leyes", "dof"), help="actualizar solo una fuente")
    p.add_argument("--dias", type=int, default=2, help="días de DOF hacia atrás (por defecto 2)")
    p.add_argument("--limite", type=int, help="máximo de leyes a revisar (para pruebas)")
    p.add_argument("--forzar", action="store_true", help="volver a procesar leyes aunque no hayan cambiado")
    args = p.parse_args()

    config = {k: getattr(Config, k) for k in ("DATABASE_DIR", "BOVEDA_DIR")}
    print(f"== Base legal · {datetime.now():%Y-%m-%d %H:%M} ==", flush=True)
    if args.solo in (None, "leyes"):
        print("Leyes federales:", indice.sincronizar_leyes(config, limite=args.limite, forzar=args.forzar,
                                                           avisar=lambda m: print(m, flush=True)), flush=True)
    if args.solo in (None, "dof"):
        print("Diario Oficial:", indice.sincronizar_dof(config, dias=args.dias,
                                                        avisar=lambda m: print(m, flush=True)), flush=True)


if __name__ == "__main__":
    main()
