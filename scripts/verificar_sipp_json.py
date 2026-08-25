"""Comprueba que `datos/sipp.json` traiga todo lo que el código le pide.

El mapa de selectores no se versiona —el repositorio es público— y viaja al
instalador por el secreto `SIPP_DATOS_JSON`. Eso abre un hueco silencioso: si el
código gana una clave y el secreto se queda como estaba, el build pasa sin
quejarse y el instalador sale roto. Roto de la peor manera, además, porque falla
en el equipo del usuario y a media captura: sin la consulta de la fecha, por
ejemplo, ninguna solicitud se guarda.

Las claves NO se enumeran aquí: se leen del propio código, para que esta
comprobación no se pueda quedar desincronizada de lo que el motor usa.

Uso:
    python scripts/verificar_sipp_json.py
"""

from __future__ import annotations

import json
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA_JSON = os.path.join(RAIZ, "datos", "sipp.json")

# Las formas en que el código pide algo del mapa. Solo se miran los literales:
# una clave construida en tiempo de ejecución no se puede comprobar así, y
# ninguna de las que hay hoy lo es.
PATRONES_SELECTOR = [
    r'\bloc\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bcss\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bexiste\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bvisible_\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bllenar\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bsubir_archivo\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bseleccionar_chosen\(\s*"([a-z_]+\.[a-z_]+)"',
    r'\bclic_con_reintento\(\s*"([a-z_]+\.[a-z_]+)"',
]
PATRONES_CONSULTA = [
    r'\bconsulta\(\s*"([a-z_]+)"',
    r'\bconsultar\(\s*"([a-z_]+)"',
]

CARPETAS = ("core", "ui")


def _fuentes() -> list[str]:
    rutas = []
    for carpeta in CARPETAS:
        base = os.path.join(RAIZ, carpeta)
        for dirpath, _dirs, ficheros in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            rutas.extend(os.path.join(dirpath, f) for f in ficheros
                         if f.endswith(".py"))
    return rutas


def claves_que_usa_el_codigo() -> tuple[set[str], set[str]]:
    """`(selectores, consultas)` que el código pide por su nombre literal."""
    selectores: set[str] = set()
    consultas: set[str] = set()
    for ruta in _fuentes():
        with open(ruta, encoding="utf-8") as fh:
            texto = fh.read()
        for patron in PATRONES_SELECTOR:
            selectores.update(re.findall(patron, texto))
        for patron in PATRONES_CONSULTA:
            consultas.update(re.findall(patron, texto))
    return selectores, consultas


def main() -> int:
    if not os.path.isfile(RUTA_JSON):
        print(f"No existe {RUTA_JSON}.")
        return 1
    with open(RUTA_JSON, encoding="utf-8") as fh:
        datos = json.load(fh)

    tiene_sel = set(datos.get("selectores", {}))
    tiene_con = set(datos.get("consultas", {}))
    usa_sel, usa_con = claves_que_usa_el_codigo()

    faltan_sel = sorted(usa_sel - tiene_sel)
    faltan_con = sorted(usa_con - tiene_con)
    if not faltan_sel and not faltan_con:
        print(f"datos/sipp.json completo: {len(usa_sel)} selector(es) y "
              f"{len(usa_con)} consulta(s) que el código usa están presentes.")
        return 0

    print("A datos/sipp.json le faltan claves que el código SÍ usa.")
    print("Si esto sale en el CI, el secreto SIPP_DATOS_JSON está desfasado: "
          "vuelve a pegarlo con el contenido actual del archivo.")
    for clave in faltan_sel:
        print(f"  selector ausente: {clave}")
    for clave in faltan_con:
        print(f"  consulta ausente: {clave}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
