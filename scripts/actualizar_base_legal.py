"""
Actualiza la base legal de México: leyes y reglamentos federales, leyes de los estados, tesis y Diario Oficial.

Uso:
    venv/bin/python scripts/actualizar_base_legal.py              # leyes y reglamentos con cambios + DOF de hoy y ayer
    venv/bin/python scripts/actualizar_base_legal.py --solo dof --dias 30
    venv/bin/python scripts/actualizar_base_legal.py --solo leyes --limite 5
    venv/bin/python scripts/actualizar_base_legal.py --solo reglamentos --limite 3
    venv/bin/python scripts/actualizar_base_legal.py --solo estados --entidad Jalisco --tipos codigo --limite 3
    venv/bin/python scripts/actualizar_base_legal.py --solo estados --pasada --minutos 20   # una tanda de la pasada
    venv/bin/python scripts/actualizar_base_legal.py --solo tesis --limite 200               # revisa 200 registros
    venv/bin/python scripts/actualizar_base_legal.py --solo tesis --archivo tesis.jsonl      # importa un archivo

Sin --solo no se tocan estados ni tesis (son descargas largas; el servidor las hace por tandas).

Cron sugerido (diario a las 6:30 y 20:30, para alcanzar ediciones vespertinas del DOF):
    30 6,20 * * *  cd /ruta/al/proyecto && venv/bin/python scripts/actualizar_base_legal.py >> logs/base_legal.log 2>&1
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from modules.corpus import indice  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--solo", choices=("leyes", "reglamentos", "estados", "tesis", "dof"),
                   help="actualizar solo una fuente")
    p.add_argument("--dias", type=int, default=2, help="días de DOF hacia atrás (por defecto 2)")
    p.add_argument("--limite", type=int,
                   help="máximo de documentos a revisar: leyes/reglamentos, por estado y tipo, o registros de tesis")
    p.add_argument("--forzar", action="store_true", help="volver a procesar aunque no haya cambiado")
    p.add_argument("--entidad", action="append", help="estados: solo esta entidad (se puede repetir)")
    p.add_argument("--tipos", help="estados: constitucion,codigo,ley (por defecto, los tres)")
    p.add_argument("--pasada", action="store_true", help="estados: seguir la pasada guardada (como el servidor)")
    p.add_argument("--minutos", type=float, help="tiempo máximo de la tanda")
    p.add_argument("--archivo", help="tesis: importar de un archivo JSON o JSONL en vez de descargar")
    p.add_argument("--desde", type=int, help="tesis: registro digital desde el que buscar lo nuevo")
    args = p.parse_args()

    config = {k: getattr(Config, k) for k in ("DATABASE_DIR", "BOVEDA_DIR")}
    avisar = lambda m: print(m, flush=True)  # noqa: E731
    hasta = time.monotonic() + args.minutos * 60 if args.minutos else None
    print(f"== Base legal · {datetime.now():%Y-%m-%d %H:%M} ==", flush=True)
    if args.solo in (None, "leyes"):
        print("Leyes federales:", indice.sincronizar_leyes(config, limite=args.limite, forzar=args.forzar,
                                                           avisar=avisar, hasta=hasta), flush=True)
    if args.solo in (None, "reglamentos"):
        print("Reglamentos federales:", indice.sincronizar_reglamentos(config, limite=args.limite, forzar=args.forzar,
                                                                       avisar=avisar, hasta=hasta), flush=True)
    if args.solo == "estados":
        from modules.corpus import estatal
        desconocidas = [e for e in (args.entidad or []) if e not in estatal.ENTIDADES.values()]
        if desconocidas:
            p.error(f"entidad desconocida: {', '.join(desconocidas)}. Usa: {', '.join(estatal.ENTIDADES.values())}")
        tipos = [t.strip() for t in args.tipos.split(",")] if args.tipos else None
        print("Leyes estatales:", estatal.sincronizar(config, entidades=args.entidad, tipos=tipos, limite=args.limite,
                                                      forzar=args.forzar, avisar=avisar, hasta=hasta,
                                                      pasada=args.pasada), flush=True)
    if args.solo == "tesis":
        from modules.corpus import tesis
        if args.archivo:
            print("Tesis importadas:", tesis.importar(config, args.archivo, limite=args.limite, avisar=avisar), flush=True)
        else:
            print("Tesis:", tesis.sincronizar(config, limite=args.limite, avisar=avisar, hasta=hasta,
                                              desde=args.desde), flush=True)
    if args.solo in (None, "dof"):
        print("Diario Oficial:", indice.sincronizar_dof(config, dias=args.dias, avisar=avisar), flush=True)


if __name__ == "__main__":
    main()
