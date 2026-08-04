"""Catálogo de conceptos de pago: consulta, importación y alta manual.

Existe para que nadie tenga que escribir un concepto de memoria. SIPP los asigna
por empresa y no hay forma de consultarlos sin entrar al formulario, así que se
importan una vez desde el portal y desde aquí alimentan los desplegables de la
plantilla de Excel y del formulario de captura.

El alta manual está permitida —a veces hace falta un concepto que todavía no se
ha importado— pero **avisa con claridad**: un concepto escrito a mano puede no
existir en SIPP, y si no existe, la solicitud que lo use no se podrá capturar.
Ese aviso es la parte importante de esta pantalla, no la tabla.
"""

from __future__ import annotations

import asyncio

import flet as ft

from core import conceptos
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE
from ui.componentes import (boton_herramienta, boton_primario, boton_secundario,
                            campo_opciones, campo_texto, tarjeta_seccion)
from ui.configuracion import ambiente_actual, url_login
from ui.tabla_responsiva import IZQ, ColumnaTabla, FilaDatos, TablaResponsiva

_COLUMNAS = [
    ColumnaTabla("Concepto de pago", 62, IZQ),
    ColumnaTabla("Origen", 28, IZQ),
    ColumnaTabla("", 10),
]


class SeccionConceptos:
    """Pantalla del catálogo de conceptos."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._conceptos: list[conceptos.Concepto] = []
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        # El selector de empresa es solo para DECIDIR DE DÓNDE LEER: el
        # catálogo resultante es una lista única, sin empresa.
        bl_empresa, self.dd_empresa = campo_opciones(
            None, NOMBRES_EMPRESAS, valor=conceptos.EMPRESA_BASE, width=260)
        self.btn_importar = boton_secundario(
            "Importar desde SIPP", ft.Icons.CLOUD_DOWNLOAD,
            on_click=self._importar)
        self.txt_estado = ft.Text("", color=GRIS)

        barra = ft.Row(
            [ft.Row([ft.Text("Leer de:", color=GRIS), bl_empresa,
                     self.btn_importar],
                    spacing=10, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self.txt_estado,
             boton_primario("Agregar concepto", ft.Icons.ADD,
                            self._abrir_alta)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.tabla = TablaResponsiva(self.page, _COLUMNAS)
        self.vacio = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.LIST_ALT, size=40, color=GRIS),
                 ft.Text("Todavía no hay conceptos en el catálogo.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 ft.Text(f"Impórtalos de SIPP —«{conceptos.EMPRESA_BASE}» es la "
                         f"empresa con el catálogo más completo— o agrégalos a "
                         f"mano.", color=GRIS, text_align=ft.TextAlign.CENTER)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        self.contenido = ft.Column(
            [tarjeta_seccion(ft.Column([
                ft.Text("Conceptos de pago",
                        theme_style=ft.TextThemeStyle.HEADLINE_SMALL),
                ft.Text("Es la lista que ofrecen la plantilla de Excel y el "
                        "formulario de captura, para elegir en vez de teclear. "
                        "Un concepto mal escrito no falla al capturarlo: falla "
                        "a media corrida, cuando el robot no lo encuentra.\n"
                        "Es una lista única para todo el grupo: se lee de una "
                        "empresa y sirve para todas.",
                        theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
                ft.Divider(), barra], spacing=12, tight=True)),
             ft.Container(
                 content=ft.Column([self.tabla.control, self.vacio],
                                   scroll=ft.ScrollMode.AUTO, expand=True,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True)],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self._construir_alta()

    def _construir_alta(self) -> None:
        """Diálogo de alta manual, con su advertencia."""
        bl_nombre, self.tf_nombre = campo_texto(
            "Concepto de pago", flotante=True,
            hint="Tal como aparece en SIPP")
        self.txt_error_alta = ft.Text(size=12, color=ROJO, visible=False)

        aviso = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER, color=NARANJA, size=22),
                ft.Column([
                    ft.Text("Este concepto podría no existir en SIPP",
                            weight=ft.FontWeight.BOLD, color=NARANJA),
                    ft.Text("Al agregarlo a mano solo lo estás guardando en "
                            "esta herramienta. Si SIPP no lo tiene asignado a "
                            "la empresa, las solicitudes que lo usen no se "
                            "podrán capturar y quedarán marcadas para revisión."
                            "\n\nSi es un concepto nuevo, solicítalo al equipo "
                            "de soporte antes de usarlo en un lote.",
                            theme_style=ft.TextThemeStyle.BODY_MEDIUM)],
                    spacing=4, tight=True, expand=True)],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
            bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.ORANGE),
            border=ft.Border.all(1, NARANJA), border_radius=8, padding=14)

        self.dialogo_alta = ft.AlertDialog(
            modal=True,
            title=ft.Text("Agregar concepto de pago"),
            content=ft.Container(
                ft.Column([bl_nombre, aviso, self.txt_error_alta],
                          spacing=16, tight=True, scroll=ft.ScrollMode.AUTO),
                width=520),
            actions=[
                ft.TextButton("Cancelar", on_click=self._cerrar_alta),
                ft.FilledButton("Agregar de todos modos", icon=ft.Icons.ADD,
                                on_click=self._guardar_alta),
            ],
            actions_alignment=ft.MainAxisAlignment.END)

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola; presente por el contrato de pantalla."""

    # ------------------------------------------------------------ datos
    def cargar_desde_db(self) -> None:
        self._conceptos = conceptos.listar()
        filas = []
        for c in self._conceptos:
            if c.verificado:
                # La empresa de la que se leyó va en el tooltip, no en una
                # columna: el catálogo es una lista única para todo el grupo y
                # ese dato solo sirve como rastro de dónde salió.
                origen = ft.Row([
                    ft.Icon(ft.Icons.VERIFIED, size=16, color=VERDE),
                    ft.Text("Importado de SIPP", size=12, color=VERDE)],
                    spacing=6, tight=True,
                    tooltip=(f"Leído del portal en «{c.empresa}»."
                             if c.empresa else "Leído del portal."))
            else:
                origen = ft.Row([
                    ft.Icon(ft.Icons.WARNING_AMBER, size=16, color=NARANJA),
                    ft.Text("Manual · sin verificar", size=12, color=NARANJA)],
                    spacing=6, tight=True,
                    tooltip="Podría no existir en SIPP. Si no existe, las "
                            "solicitudes que lo usen no se podrán capturar.")
            filas.append(FilaDatos([
                c.nombre,
                origen,
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18,
                              icon_color=ft.Colors.ERROR,
                              tooltip="Quitar del catálogo",
                              on_click=lambda _e, cid=c.id: self._borrar(cid)),
            ]))
        self.tabla.set_contenido(filas)
        hay = bool(filas)
        self.tabla.control.visible = hay
        self.vacio.visible = not hay
        sin_verificar = sum(1 for c in self._conceptos if not c.verificado)
        self.txt_estado.value = f"{len(filas)} concepto(s)"
        if sin_verificar:
            self.txt_estado.value += f" · {sin_verificar} sin verificar"
        self.txt_estado.color = NARANJA if sin_verificar else GRIS
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montado
            pass

    # -------------------------------------------------------- importar
    async def _importar(self, _e=None) -> None:
        usuario, contrasena = self.app.config.credenciales()
        if not (usuario and contrasena):
            self.app.avisar("Captura las credenciales de SIPP en "
                            "Configuración ⚙ antes de importar.", NARANJA)
            return
        empresa = self.dd_empresa.value or conceptos.EMPRESA_BASE
        self.btn_importar.disabled = True
        self.btn_importar.text = "Importando…"
        self.txt_estado.value = (f"Entrando a SIPP ({ambiente_actual()}) para "
                                 f"leer los conceptos de {empresa}…")
        self.page.update()
        try:
            resultado = await asyncio.to_thread(
                conceptos.leer_de_sipp, usuario, contrasena,
                url_login=url_login(), empresa=empresa)
        except Exception as exc:  # noqa: BLE001 — se reporta, la app sigue
            resultado = {"ok": False, "conceptos": [], "mensaje": str(exc)}
        finally:
            self.btn_importar.disabled = False
            self.btn_importar.text = "Importar desde SIPP"

        if not resultado["ok"]:
            self.txt_estado.value = resultado["mensaje"]
            self.txt_estado.color = ROJO
            self.page.update()
            self.app.avisar(resultado["mensaje"], ROJO, duracion=9000)
            return

        resumen = await asyncio.to_thread(
            conceptos.importar, resultado["conceptos"], empresa)
        self.cargar_desde_db()
        self.app.avisar(
            f"{resumen['nuevos']} concepto(s) nuevos de {empresa}"
            + (f", {resumen['ya_estaban']} ya estaban."
               if resumen["ya_estaban"] else "."),
            VERDE, duracion=8000)

    # ----------------------------------------------------- alta manual
    def _abrir_alta(self, _e=None) -> None:
        self.tf_nombre.value = ""
        self.txt_error_alta.visible = False
        self.page.show_dialog(self.dialogo_alta)

    def _cerrar_alta(self, _e=None) -> None:
        self.page.pop_dialog()

    def _guardar_alta(self, _e=None) -> None:
        nombre = (self.tf_nombre.value or "").strip()
        if not nombre:
            self.txt_error_alta.value = "Escribe el nombre del concepto."
            self.txt_error_alta.visible = True
            self.page.update()
            return
        creado = conceptos.guardar(nombre, origen=conceptos.ORIGEN_MANUAL)
        if creado is None:
            self.txt_error_alta.value = "Ese concepto ya está en el catálogo."
            self.txt_error_alta.visible = True
            self.page.update()
            return
        self._cerrar_alta()
        self.cargar_desde_db()
        self.app.avisar(
            f"«{creado.nombre}» agregado. Recuerda confirmar con soporte que "
            f"exista en SIPP antes de usarlo en un lote.", NARANJA,
            duracion=9000)

    def _borrar(self, concepto_id: str) -> None:
        conceptos.borrar(concepto_id)
        self.cargar_desde_db()
