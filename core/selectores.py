"""Mapa de selectores de SIPP: fuera del código del flujo, con respaldos.

Cada entrada tiene un selector **primario** y una lista de **respaldos** que se
prueban en orden si el primero no aparece. Vivir aquí (y no incrustados en el
flujo) permite parchear un cambio de SIPP sin tocar la lógica, y `verificar()`
sirve para detectar esos cambios ANTES de lanzar un lote.

Los valores viven en `datos/sipp.json`, que NO se versiona (ver
`core/sipp_datos.py`). Antes de editarlos, lee ESPECIFICACION.md §8: hay tres
trampas que condicionan casi todo (selects ocultos tras el widget *chosen*,
eventos de Angular que hay que disparar a mano, e ids DUPLICADOS entre los
paneles de Proveedor / Deudor / Acreedor).

Parche en caliente: si existe `selectores.json` en `rutas.DATOS`, sus entradas
sustituyen a las de aquí. Así se puede corregir un selector en el equipo de un
usuario sin recompilar ni publicar una versión.
"""

from __future__ import annotations

import json
import os

from core import rutas, sipp_datos

RUTA_PARCHE = os.path.join(rutas.DATOS, "selectores.json")

# --------------------------------------------------------------------------- #
#  Mapa base
# --------------------------------------------------------------------------- #
# Los selectores NO viven aquí: están en `datos/sipp.json`, que no se versiona.
# El repositorio es público y el mapa describe la estructura interna del
# formulario de pagos del ERP. Ver `core/sipp_datos.py` para de dónde sale el
# archivo y qué hacer si falta.
def _base() -> dict[str, dict]:
    return sipp_datos.selectores()


# Pestañas del formulario. Se abren por el TEXTO de su enlace: los índices
# internos (`tab == 4`, `tab == 5`) no son visibles en el DOM.
#
# Son funciones y no una constante porque los textos salen de `datos/sipp.json`,
# que se lee del disco: una constante de módulo obligaría a leer el archivo al
# importar, y entonces un `import` fallaría en un equipo sin ese archivo en vez
# de fallar donde se puede explicar el problema.
def pestanas() -> dict[str, str]:
    """Todas las pestañas conocidas: `clave interna -> texto visible`."""
    return sipp_datos.pestanas()


def pestana(nombre: str) -> str:
    """Texto visible de una pestaña; el propio nombre si no está mapeada."""
    return pestanas().get(nombre, nombre)

# Paneles del beneficiario. El número es el id interno del tipo, y acotar por
# aquí es OBLIGATORIO: varios campos del beneficiario existen tres veces, uno
# por panel.
def panel_beneficiario(tipo_id: int) -> str:
    """Selector del panel de un tipo de beneficiario (1 Prov · 2 Deu · 3 Acr)."""
    return sipp_datos.plantilla_panel().format(tipo_id)


# --------------------------------------------------------------------------- #
#  Acceso
# --------------------------------------------------------------------------- #
_cache: dict[str, dict] | None = None


def _cargar() -> dict[str, dict]:
    """Mapa efectivo: el base, con lo que diga el parche local encima."""
    global _cache
    if _cache is not None:
        return _cache
    mapa = _base()
    try:
        if os.path.exists(RUTA_PARCHE):
            with open(RUTA_PARCHE, encoding="utf-8") as fh:
                parche = json.load(fh)
            for clave, valor in (parche or {}).items():
                if isinstance(valor, str):          # forma corta: solo primario
                    valor = {"primario": valor, "respaldos": []}
                if isinstance(valor, dict) and valor.get("primario"):
                    mapa[clave] = {"primario": valor["primario"],
                                   "respaldos": list(valor.get("respaldos", []))}
    except (OSError, ValueError):
        # Un parche corrupto NO debe impedir operar: se ignora y manda el base.
        pass
    _cache = mapa
    return mapa


def recargar() -> None:
    """Olvida la caché para releer el parche (tras editarlo en caliente)."""
    global _cache
    _cache = None


def css(clave: str) -> str:
    """Selector primario de `clave`. Lanza KeyError si no existe (es un error de
    programación, no del entorno: mejor que falle de inmediato)."""
    return _cargar()[clave]["primario"]


def todos(clave: str) -> list[str]:
    """Primario + respaldos, en el orden en que hay que probarlos."""
    entrada = _cargar()[clave]
    return [entrada["primario"], *entrada["respaldos"]]


def claves() -> list[str]:
    """Todas las claves del mapa (las recorre la verificación de conexión)."""
    return sorted(_cargar())
