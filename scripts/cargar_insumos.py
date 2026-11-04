"""Carga el catálogo depurado de insumos y servicios desde un archivo.

El catálogo de SIPP es enorme y casi nada de él se solicita desde la
herramienta, así que no se importa del portal: el área entrega una lista ya
depurada y es esa la que alimenta el desplegable de la plantilla de Excel.

Uso:
    python scripts/cargar_insumos.py <archivo.xlsx|csv> [--agregar]

Por defecto REEMPLAZA el catálogo, que es lo que se quiere cuando llega una
versión nueva: lo que el área quitó tiene que dejar de ofrecerse. Con
`--agregar` se suma a lo que ya hay.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, insumos  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    ruta = argv[0]
    reemplazar = "--agregar" not in argv
    if not os.path.isfile(ruta):
        print(f"No existe el archivo: {ruta}")
        return 1

    db.inicializar()
    leidos = insumos.leer_archivo(ruta)
    if not leidos:
        print("No se encontró ninguna columna con nombres de insumo en el "
              "archivo. Revisa que la lista esté en la primera hoja.")
        return 1

    con_folio = sum(1 for clave, _n in leidos if clave)
    print(f"Leídos {len(leidos)} insumo(s) del archivo; {con_folio} con folio.")
    if not con_folio:
        print("   AVISO: ninguno trae folio. El robot tendrá que buscarlos por "
              "nombre en SIPP, que es menos exacto.")
    print("Ejemplos:")
    for clave, nombre in leidos[:5]:
        print(f"   · {clave or '(sin folio)':<10} {nombre}")

    resumen = insumos.importar(leidos, reemplazar=reemplazar)
    if resumen["retirados"]:
        print(f"\nSe retiraron {resumen['retirados']} del catálogo anterior.")
    print(f"Nuevos: {resumen['nuevos']} · ya estaban: {resumen['ya_estaban']}")
    print(f"El catálogo queda con {len(insumos.nombres())} insumo(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
