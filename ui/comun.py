"""Constantes y utilidades compartidas por las pantallas de la interfaz.

Aquí viven los colores de ESTADO, los catálogos re-exportados y las fechas. El
estilo de botones, campos, tarjetas y modales NO está aquí: es de
ui/componentes.py.
"""

from __future__ import annotations

from datetime import datetime

import flet as ft

# Catálogos: una sola puerta de entrada para las pantallas, aunque por dentro
# vivan en dos módulos (empresas es reutilizable entre herramientas; el resto es
# específico de la pantalla de Solicitud de Pago del SIPP).
from core.catalogos import (  # noqa: F401
    AMBIENTE_DEFECTO, AMBIENTES, ESTADOS, ETIQUETA_ESTADO, ETIQUETA_PARADA,
    FORMAS_PAGO, MONEDA_DEFECTO, MONEDAS, PARADA_DEFECTO, PARADAS, SUCURSALES,
    TIPOS_BENEFICIARIO, TIPOS_GASTO, TIPOS_PAGO, TIPO_PAGO_SOPORTADO,
)
from core.empresas import EMPRESAS, ID_POR_EMPRESA, NOMBRES_EMPRESAS  # noqa: F401

# --- Colores -------------------------------------------------------------
# Colores semánticos de ESTADO (éxito / error / aviso). El resto del estilo
# —botones, campos, tarjetas— vive en ui/componentes.py y se pide por rol.
VERDE = ft.Colors.GREEN_700
ROJO = ft.Colors.RED_700
NARANJA = ft.Colors.ORANGE_800
GRIS = ft.Colors.ON_SURFACE_VARIANT

CENTRO = ft.Alignment(0, 0)

# Color de fondo de la fila de la tabla según el estado de la solicitud. Se
# devuelve como color CON OPACIDAD sobre un rol del tema, para que funcione
# igual en claro y en oscuro (un hex fijo se vería mal en uno de los dos).
_COLOR_ESTADO: dict[str, str] = {
    "PENDIENTE": "",
    "VALIDADA": ft.Colors.SECONDARY,
    "EN_CAPTURA": ft.Colors.SECONDARY,
    "LLENADA": ft.Colors.TERTIARY,
    "GUARDADA": ft.Colors.GREEN,
    "ENVIADA_AUTORIZAR": ft.Colors.GREEN,
    "ERROR": ft.Colors.ERROR,
    "OMITIDA": ft.Colors.ON_SURFACE_VARIANT,
    "REVISAR": ft.Colors.ORANGE,
}


def color_fila(estado: str) -> str | None:
    """Fondo tenue para una fila según su estado (None si no lleva color)."""
    base = _COLOR_ESTADO.get(estado, "")
    return ft.Colors.with_opacity(0.10, base) if base else None


def color_estado(estado: str) -> str:
    """Color pleno para el texto/insignia del estado."""
    return _COLOR_ESTADO.get(estado) or ft.Colors.ON_SURFACE_VARIANT


# --- Fechas --------------------------------------------------------------
# Formato ÚNICO de fecha en toda la app (México). Las fechas NUNCA se teclean:
# se usa componentes.CampoFecha (solo lectura + DatePicker).
FORMATO_FECHA = "%d/%m/%Y"


def parse_fecha(texto: str | None) -> "datetime | None":
    """Convierte 'DD/MM/AAAA' a datetime (None si vacío o inválido)."""
    texto = (texto or "").strip()
    if not texto:
        return None
    for fmt in (FORMATO_FECHA, "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    return None


def fmt_fecha(fecha: "datetime | None") -> str:
    return fecha.strftime(FORMATO_FECHA) if fecha else ""


# --- Importes ------------------------------------------------------------
def fmt_importe(valor: float | None) -> str:
    """Formato de dinero para la tabla y los totales: '$1,234.56'."""
    return f"${(valor or 0):,.2f}"


def parse_importe(texto: str | None) -> float:
    """Lee un importe tecleado tolerando '$', comas y espacios. 0.0 si no es
    un número (la validación de "está vacío" la hace el validador, no esto)."""
    limpio = (texto or "").replace("$", "").replace(",", "").strip()
    try:
        return round(float(limpio), 2)
    except ValueError:
        return 0.0


# --- Andamio de pantallas pendientes -------------------------------------
def placeholder(titulo: str, descripcion: str, icono=ft.Icons.CONSTRUCTION,
                pendientes: list[str] | None = None) -> ft.Control:
    """Pantalla en construcción, con lo que SÍ va a hacer cuando esté lista.

    Sirve para registrar la pantalla en el shell desde el primer día (y que la
    navegación quede completa) sin fingir que ya funciona.
    """
    hijos: list[ft.Control] = [
        ft.Icon(icono, size=44, color=ft.Colors.ON_SURFACE_VARIANT),
        ft.Text(titulo, theme_style=ft.TextThemeStyle.HEADLINE_SMALL,
                text_align=ft.TextAlign.CENTER),
        ft.Text(descripcion, theme_style=ft.TextThemeStyle.BODY_MEDIUM,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.CENTER),
    ]
    if pendientes:
        hijos.append(ft.Container(
            content=ft.Column(
                [ft.Row([ft.Icon(ft.Icons.CIRCLE, size=6,
                                 color=ft.Colors.ON_SURFACE_VARIANT),
                         ft.Text(p, theme_style=ft.TextThemeStyle.BODY_MEDIUM)],
                        spacing=10, tight=True)
                 for p in pendientes],
                spacing=8, tight=True),
            padding=16))
    return ft.Container(
        content=ft.Column(hijos, spacing=14, tight=True,
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                          alignment=ft.MainAxisAlignment.CENTER),
        alignment=CENTRO, expand=True)
