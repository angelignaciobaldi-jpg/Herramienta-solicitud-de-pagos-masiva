"""Corre todas las pruebas de la herramienta.

    python scripts/probar.py                 # todas
    python scripts/probar.py datos           # solo los módulos que digan «datos»
    python scripts/probar.py -v              # con el detalle de cada prueba

Descubre los módulos `scripts/pruebas/test_*.py` y ejecuta sus funciones
`probar_*`. Cada una recibe la base recién creada y aislada, así que el orden no
importa y una prueba no puede contaminar a la siguiente.

No cubre lo que necesita SIPP ni la ventana real: para eso están
`scripts/smoke_render.py` (abre la app y pinta cada pantalla) y los
`scripts/prueba_*_stage.py` (contra el portal, con credenciales).
"""

from __future__ import annotations

import importlib
import io
import os
import pkgutil
import sys
import traceback

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

# Las pruebas imprimen «•» y «◦» (errores y avisos del validador), que la consola
# de Windows en cp1252 no sabe codificar. Sin esto, una prueba correcta falla con
# UnicodeEncodeError y el mensaje no dice nada del problema real.
for flujo in ("stdout", "stderr"):
    corriente = getattr(sys, flujo)
    if isinstance(corriente, io.TextIOWrapper):
        corriente.reconfigure(encoding="utf-8", errors="replace")


def _modulos(filtro: str = "") -> list:
    ruta = os.path.join(_RAIZ, "scripts", "pruebas")
    nombres = [m.name for m in pkgutil.iter_modules([ruta])
               if m.name.startswith("test_")]
    if filtro:
        nombres = [n for n in nombres if filtro.lower() in n.lower()]
    return [importlib.import_module(f"scripts.pruebas.{n}")
            for n in sorted(nombres)]


def main(argv: list[str]) -> int:
    detallado = "-v" in argv or "--verbose" in argv
    filtro = next((a for a in argv if not a.startswith("-")), "")

    from scripts.pruebas import comun

    modulos = _modulos(filtro)
    if not modulos:
        print(f"No hay módulos de prueba que coincidan con «{filtro}».")
        return 1

    total = fallidas = 0
    fallos: list[tuple[str, str]] = []

    for modulo in modulos:
        corto = modulo.__name__.rsplit(".", 1)[-1].removeprefix("test_")
        funciones = [(n, f) for n, f in vars(modulo).items()
                     if n.startswith("probar_") and callable(f)]
        # En orden de declaración: las pruebas se leen como una narración y
        # alfabetizarlas rompería ese hilo.
        funciones.sort(key=lambda par: par[1].__code__.co_firstlineno)
        print(f"\n\033[1m{corto}\033[0m  ({len(funciones)} prueba(s))")

        for nombre, funcion in funciones:
            total += 1
            etiqueta = (funcion.__doc__ or nombre).strip().split("\n")[0]
            comun.base_limpia()          # aislamiento entre pruebas
            try:
                funcion()
                print(f"  OK  {etiqueta}")
            except Exception as exc:  # noqa: BLE001 — se reporta y se sigue
                fallidas += 1
                detalle = f"{type(exc).__name__}: {exc}"
                fallos.append((f"{corto}.{nombre}", detalle))
                print(f"  XX  {etiqueta}")
                print(f"      {detalle}")
                if detallado:
                    for linea in traceback.format_exc().splitlines()[-6:]:
                        print(f"      {linea}")

    print()
    if fallos:
        print(f"{fallidas} de {total} prueba(s) fallaron:")
        for nombre, detalle in fallos:
            print(f"  - {nombre}: {detalle}")
        return 1
    print(f"Las {total} pruebas pasaron.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
