"""Configuración: credenciales del SIPP, ambiente y modo del navegador.

Se abre como diálogo desde el botón de la barra superior. La contraseña se
guarda cifrada con DPAPI (core/credenciales.py); el ambiente y el modo del
navegador son preferencias por máquina.

El **ambiente** es lo más delicado de esta pantalla: decide si el RPA opera
contra pruebas o contra producción, donde cada solicitud consume un folio real.
Por eso se confirma al cambiarlo a producción y el shell lo muestra de forma
permanente en el encabezado.
"""

from __future__ import annotations

import asyncio

import flet as ft

from core import credenciales, preferencias
from core.catalogos import AMBIENTE_DEFECTO, AMBIENTES
from core.version import __version__
from ui.comun import GRIS, NARANJA, VERDE
from ui.componentes import (boton_primario, boton_secundario, campo_opciones,
                            campo_texto, tarjeta_seccion)

_ANCHO = 480

CLAVE_AMBIENTE = "ambiente"
CLAVE_VISIBLE = "navegador_visible"


def ambiente_actual() -> str:
    """Ambiente configurado en esta máquina ('PRUEBAS' | 'PRODUCCION')."""
    valor = preferencias.cargar_valor(CLAVE_AMBIENTE, AMBIENTE_DEFECTO)
    return valor if valor in AMBIENTES else AMBIENTE_DEFECTO


def url_login() -> str:
    """URL de login del SIPP para el ambiente configurado."""
    return AMBIENTES[ambiente_actual()]


def navegador_visible() -> bool:
    """True si el RPA debe abrir el navegador a la vista (valor por defecto).

    Se opera en visible a propósito: el operador necesita ver qué está pasando,
    y en headless los avisos emergentes de SIPP pasan desapercibidos.
    """
    return bool(preferencias.cargar_valor(CLAVE_VISIBLE, True))


class SeccionConfiguracion:
    """Diálogo de configuración (credenciales SIPP + ambiente + navegador)."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._construir()
        self._cargar_credenciales()

    # ------------------------------------------------------------ UI
    @staticmethod
    def _apartado(titulo: str, ayuda: str | None, *controles) -> ft.Control:
        encabezado = [ft.Text(titulo, size=14, weight=ft.FontWeight.BOLD)]
        if ayuda:
            encabezado.append(ft.Icon(
                ft.Icons.HELP_OUTLINE, size=18, color=GRIS,
                tooltip=ft.Tooltip(
                    message=ayuda, wait_duration=ft.Duration(milliseconds=0))))
        return ft.Column(
            [ft.Row(encabezado, spacing=6, tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             *controles],
            spacing=8, tight=True)

    def _construir(self) -> None:
        bl_usuario, self.tf_usuario = campo_texto("Usuario", expand=True)
        bl_contrasena, self.tf_contrasena = campo_texto(
            "Contraseña", password=True, expand=True)

        bl_ambiente, self.dd_ambiente = campo_opciones(
            "Ambiente de SIPP", list(AMBIENTES), valor=ambiente_actual(),
            on_change=self._cambio_ambiente)
        self.txt_url = ft.Text(size=12, color=GRIS)
        self.txt_aviso_prod = ft.Text(
            "En producción cada solicitud guardada consume un folio real.",
            size=12, color=NARANJA, weight=ft.FontWeight.BOLD)
        self._refrescar_ambiente()

        self.chk_visible = ft.Checkbox(
            label="Mostrar el navegador mientras trabaja el robot",
            value=navegador_visible())

        self.btn_verificar = boton_secundario(
            "Verificar conexión con SIPP", ft.Icons.LAN,
            on_click=self._verificar)

        cred = self._apartado(
            "Credenciales SIPP",
            "Usuario y contraseña del portal SIPP que usa el robot. La contraseña "
            "se guarda cifrada en este equipo (DPAPI); nunca en claro ni en el "
            "repositorio.",
            bl_usuario, bl_contrasena)
        ambiente = self._apartado(
            "Ambiente",
            "Contra qué instalación de SIPP trabaja el robot. Pruebas (stage) "
            "para desarrollar y ensayar; producción solo para la corrida real.",
            bl_ambiente, self.txt_url, self.txt_aviso_prod)
        navegador = self._apartado(
            "Navegador",
            "En modo visible puedes seguir el trabajo del robot y detectar avisos "
            "de SIPP que en modo oculto pasarían desapercibidos.",
            self.chk_visible, self.btn_verificar)

        grupo = tarjeta_seccion(ft.Column(
            [ft.Text("Sistema", size=15, weight=ft.FontWeight.BOLD),
             cred, ft.Divider(), ambiente, ft.Divider(), navegador,
             ft.Divider(),
             ft.Text(f"Versión {__version__}", size=12, color=GRIS)],
            spacing=14, tight=True))

        contenido = ft.Column(
            [ft.Container(grupo, width=_ANCHO)],
            scroll=ft.ScrollMode.AUTO, tight=True)

        self.dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [ft.Text("Configuración", size=22, weight=ft.FontWeight.BOLD),
                 ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Cerrar",
                               on_click=self._cerrar)],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER, width=_ANCHO),
            content=ft.Container(contenido, width=_ANCHO),
            actions=[boton_primario("Aceptar", ft.Icons.CHECK, self._guardar)],
            actions_alignment=ft.MainAxisAlignment.END,
        )

    # -------------------------------------------------------- acciones
    def abrir(self, _e=None) -> None:
        self.page.show_dialog(self.dialogo)

    def _cerrar(self, _e=None) -> None:
        self.page.pop_dialog()

    def _on_resize(self, _e=None) -> None:
        """El modal es de ancho fijo; no requiere reacomodo. Presente por
        consistencia con el registro de listeners del shell."""

    def _refrescar_ambiente(self) -> None:
        elegido = self.dd_ambiente.value or AMBIENTE_DEFECTO
        self.txt_url.value = AMBIENTES.get(elegido, "")
        self.txt_aviso_prod.visible = elegido == "PRODUCCION"

    def _cambio_ambiente(self, _e=None) -> None:
        self._refrescar_ambiente()
        self.page.update()

    async def _verificar(self, _e=None) -> None:
        """Entra a SIPP y comprueba que el formulario responda, sin capturar nada.

        Corre en un hilo aparte: abre un navegador y navega, que tarda decenas de
        segundos, y la interfaz no puede quedarse congelada mientras tanto.
        """
        usuario, contrasena = self.credenciales()
        if not (usuario and contrasena):
            self.app.avisar("Captura usuario y contraseña antes de verificar.",
                            NARANJA)
            return
        self.btn_verificar.disabled = True
        self.btn_verificar.text = "Verificando…"
        self.page.update()
        try:
            from core import rpa_sipp

            resultado = await asyncio.to_thread(
                rpa_sipp.verificar_conexion, usuario, contrasena,
                url_login=AMBIENTES[self.dd_ambiente.value or AMBIENTE_DEFECTO],
                visible=bool(self.chk_visible.value))
        except Exception as exc:  # noqa: BLE001 — se reporta, la app sigue viva
            resultado = {"ok": False, "mensaje": f"No se pudo verificar: {exc}"}
        finally:
            self.btn_verificar.disabled = False
            self.btn_verificar.text = "Verificar conexión con SIPP"
            self.page.update()
        self.app.avisar(resultado["mensaje"],
                        VERDE if resultado.get("ok") else NARANJA,
                        duracion=8000)

    def _guardar(self, _e=None) -> None:
        usuario, contrasena = self.credenciales()
        credenciales.guardar(usuario, contrasena)
        nuevo_ambiente = self.dd_ambiente.value or AMBIENTE_DEFECTO
        preferencias.guardar_valor(CLAVE_AMBIENTE, nuevo_ambiente)
        preferencias.guardar_valor(CLAVE_VISIBLE, bool(self.chk_visible.value))
        self._cerrar()
        # El shell repinta su indicador: el ambiente tiene que verse siempre,
        # no solo mientras el diálogo está abierto.
        avisar_cambio = getattr(self.app, "refrescar_ambiente", None)
        if callable(avisar_cambio):
            avisar_cambio()
        self.app.avisar("Configuración guardada.", VERDE)

    # --------------------------------------------------- credenciales
    def _cargar_credenciales(self) -> None:
        datos = credenciales.cargar()
        if datos is None:
            return
        usuario, contrasena = datos
        self.tf_usuario.value = usuario
        self.tf_contrasena.value = contrasena

    def credenciales(self) -> tuple[str, str]:
        """Devuelve (usuario, contraseña) tal como están capturados ahora."""
        return (self.tf_usuario.value or "").strip(), self.tf_contrasena.value or ""

    def hay_credenciales(self) -> bool:
        usuario, contrasena = self.credenciales()
        return bool(usuario and contrasena)
