"""Datos de integración con SIPP, fuera del código versionado.

El repositorio de esta herramienta es **público**. El mapa de selectores y las
URLs del portal no son credenciales, pero juntos describen con detalle la
estructura interna de un sistema financiero: qué campos tiene el formulario de
pagos, cómo se llaman, qué valida y por dónde se entra. Eso no tiene por qué
estar a la vista de cualquiera.

Por eso viven en `datos/sipp.json`, que **no se versiona**. La herramienta lo
lee al arrancar y funciona igual; lo que cambia es que ese archivo llega por
otra vía:

- **En desarrollo**: está en tu copia de trabajo. Si lo pierdes, pídelo a quien
  mantenga el proyecto (o recupéralo de un equipo que ya lo tenga).
- **En el instalador**: el CI lo escribe desde el secreto `SIPP_DATOS_JSON` de
  GitHub Actions antes de compilar, así que el `.exe` lo trae dentro.
- **Parche en caliente**: si aparece un `sipp.json` en la carpeta de datos del
  usuario, ese gana. Sirve para corregir un selector en un equipo sin publicar
  una versión.

`datos/sipp.ejemplo.json` sí se versiona: documenta la estructura esperada con
valores de mentira, para que quien clone el repositorio sepa qué le falta.
"""

from __future__ import annotations

import json
import os

from core import rutas

NOMBRE = "sipp.json"

# Orden de búsqueda. El de DATOS va primero para permitir el parche en caliente:
# corregir un selector en el equipo de un usuario sin recompilar ni publicar.
_RUTAS = (
    os.path.join(rutas.DATOS, NOMBRE),
    os.path.join(rutas.DATOS, "datos", NOMBRE),
    os.path.join(rutas.BUNDLE, "datos", NOMBRE),
    os.path.join(rutas.INSTALL, "datos", NOMBRE),
)

MENSAJE_FALTANTE = (
    "Falta el archivo de integración con SIPP (datos/sipp.json). Sin él la "
    "herramienta no sabe cómo hablar con el portal: no se versiona porque el "
    "repositorio es público. Pídelo a quien mantenga el proyecto o cópialo de "
    "un equipo que ya lo tenga."
)


class FaltanDatosSipp(RuntimeError):
    """No se encontró `datos/sipp.json` en ninguna ubicación conocida."""

    def __init__(self) -> None:
        super().__init__(MENSAJE_FALTANTE)


_cache: dict | None = None


def ruta_encontrada() -> str:
    """Primera ruta existente, o cadena vacía. Útil para diagnosticar."""
    for ruta in _RUTAS:
        if os.path.isfile(ruta):
            return ruta
    return ""


def disponible() -> bool:
    return bool(ruta_encontrada())


def cargar(forzar: bool = False) -> dict:
    """Contenido del archivo. Lanza `FaltanDatosSipp` si no está.

    Se lanza en vez de devolver valores vacíos a propósito: un mapa de
    selectores vacío haría que el robot fallara con «no encontré el campo X»
    treinta veces seguidas, en lugar de decir de una vez cuál es el problema
    real.
    """
    global _cache
    if _cache is not None and not forzar:
        return _cache
    ruta = ruta_encontrada()
    if not ruta:
        raise FaltanDatosSipp()
    with open(ruta, encoding="utf-8") as fh:
        _cache = json.load(fh)
    return _cache


def recargar() -> None:
    global _cache
    _cache = None


# --------------------------------------------------------------------------- #
#  Accesos con nombre
# --------------------------------------------------------------------------- #
def ambientes() -> dict[str, str]:
    """`{'PRUEBAS': url, 'PRODUCCION': url}`."""
    return dict(cargar().get("ambientes", {}))


def selectores() -> dict[str, dict]:
    """Mapa de selectores: `clave -> {primario, respaldos}`."""
    return {k: dict(v) for k, v in cargar().get("selectores", {}).items()}


def pestanas() -> dict[str, str]:
    """`{'conceptos': 'Conceptos de Pago', ...}`."""
    return dict(cargar().get("pestanas", {}))


def plantilla_panel() -> str:
    """Plantilla del selector de panel por tipo de beneficiario."""
    return cargar().get("paneles_beneficiario", "")


def consulta(nombre: str) -> str:
    """Fragmento de JavaScript que interroga el modelo de AngularJS del portal.

    No son selectores, pero citan nombres internos del ERP, así que viajan por
    el mismo canal. Se usan para leer lo que SIPP no pinta en pantalla —el folio
    tras guardar, la cuenta que quedó registrada— y para forzar un recálculo
    cuando el portal no lo dispara solo.
    """
    valor = cargar().get("consultas", {}).get(nombre, "")
    if not valor:
        raise KeyError(
            f"Falta la consulta «{nombre}» en datos/sipp.json. "
            f"Compárala contra datos/sipp.ejemplo.json.")
    return valor
