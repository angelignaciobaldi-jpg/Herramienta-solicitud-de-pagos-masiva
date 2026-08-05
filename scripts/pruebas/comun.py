"""Ayudantes compartidos por las pruebas: base aislada y dobles de la interfaz."""

from __future__ import annotations

import os
import tempfile

from core import db
from core.db import CONCEPTO, INSUMO, Lote, Partida, Solicitud


def base_limpia() -> str:
    """Apunta la base a un archivo temporal nuevo y la inicializa.

    Cada prueba arranca con su propia base. Compartirla las volvería
    dependientes del orden, y una prueba que solo pasa cuando corre segunda no
    sirve para detectar nada.
    """
    ruta = os.path.join(tempfile.mkdtemp(), "prueba.db")
    db.RUTA_DB = ruta
    db.inicializar()
    return ruta


def carpeta_temporal() -> str:
    return tempfile.mkdtemp()


def lote(nombre: str = "Lote de prueba") -> Lote:
    return db.guardar_lote(Lote(nombre=nombre))


def solicitud(lote_id: str, nombre: str, *, tipo: str = "Acreedor",
              partidas: list[Partida] | None = None,
              estado: str = "VALIDADA", **extra) -> Solicitud:
    """Solicitud válida con lo mínimo para pasar el validador.

    Los datos son ficticios a propósito: un RFC genérico y una CLABE de ejemplo.
    """
    datos = dict(
        lote_id=lote_id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario=tipo, beneficiario_nombre=nombre,
        beneficiario_rfc="XAXX010101000",
        beneficiario_correo="prueba@ejemplo.invalid",
        cuenta_clabe="012345678901234567", cuenta_banco="BBVA",
        cuenta_titular=nombre, forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago="20/09/2026",
        descripcion=f"Pago a {nombre}", estado=estado)
    datos.update(extra)
    return db.guardar_solicitud(Solicitud(**datos), partidas or [])


def concepto(nombre: str, importe: float, **extra) -> Partida:
    return Partida(clase=CONCEPTO, concepto_nombre=nombre, importe=importe,
                   **extra)


def insumo(nombre: str, importe: float, **extra) -> Partida:
    return Partida(clase=INSUMO, insumo_nombre=nombre, importe=importe, **extra)


def pdf_falso(carpeta: str, nombre: str) -> str:
    """Archivo con cabecera de PDF, suficiente para lo que se prueba aquí."""
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, nombre)
    with open(ruta, "wb") as fh:
        fh.write(b"%PDF-1.4\n")
    return ruta


# --------------------------------------------------------------------------- #
#  Dobles de la interfaz
# --------------------------------------------------------------------------- #
# Las pantallas solo necesitan de `page` cuatro cosas: guardar controles, decir
# su tamaño, abrir diálogos y refrescar. Con eso se pueden construir y operar
# sin abrir una ventana, que es lento y no se puede automatizar del todo.
#
# Ojo con el límite de esto: un doble comprueba la LÓGICA de la pantalla, no que
# se dibuje. Que un control se pueda construir no significa que el cliente lo
# pueda pintar —eso lo cubre `scripts/smoke_render.py`, que sí abre la ventana.
class PaginaFalsa:
    def __init__(self) -> None:
        import flet as ft

        self.services: list = []
        self.controls: list = []
        self.theme_mode = ft.ThemeMode.LIGHT
        self.height, self.width = 900, 1600
        self.title = "prueba"
        self.on_keyboard_event = None
        self.dialogos: list = []
        self.window = type("Ventana", (), {"always_on_top": False})()

    def update(self) -> None:
        pass

    def add(self, *controles) -> None:
        self.controls.extend(controles)

    def show_dialog(self, dialogo) -> None:
        self.dialogos.append(dialogo)

    def pop_dialog(self):
        return self.dialogos.pop() if self.dialogos else None

    def run_task(self, *_a, **_k) -> None:
        pass


class ConfiguracionFalsa:
    def credenciales(self) -> tuple[str, str]:
        return ("usuario", "contrasena")


class AppFalsa:
    """Sustituto del shell. Guarda los avisos para poder afirmar sobre ellos."""

    def __init__(self) -> None:
        self.page = PaginaFalsa()
        self.avisos: list[str] = []
        self.config = ConfiguracionFalsa()

    def avisar(self, mensaje: str, color=None, **_k) -> None:
        self.avisos.append(mensaje)

    def abrir_en_sistema(self, _ruta: str) -> None:
        pass

    def ir_a_bitacora(self) -> None:
        pass

    def refrescar_ambiente(self) -> None:
        pass

    @property
    def ultimo_aviso(self) -> str:
        return self.avisos[-1] if self.avisos else ""
