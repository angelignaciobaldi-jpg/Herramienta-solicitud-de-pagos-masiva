"""Bitácora: historial de lo que hizo el robot, paso por paso.

Cada transición de estado y cada error quedan aquí, con su evidencia cuando la
hay (SIPP tarda y falla de formas poco obvias; sin captura de pantalla es
imposible saber si el problema fue del portal o del dato). Se alimenta de
`core.db.registrar`, que el motor RPA llama antes de ejecutar cada paso.
"""

from __future__ import annotations

import os

import flet as ft

from core import db
from ui.comun import GRIS, NARANJA, ROJO, VERDE
from ui.componentes import boton_secundario, tarjeta_seccion
from ui.tabla_responsiva import IZQ, ColumnaTabla, FilaDatos, TablaResponsiva

_COLUMNAS = [
    ColumnaTabla("Momento", 15, IZQ),
    ColumnaTabla("Nivel", 8),
    ColumnaTabla("Beneficiario", 20, IZQ),
    ColumnaTabla("Paso", 17, IZQ),
    ColumnaTabla("Mensaje", 33, IZQ),
    ColumnaTabla("Evidencia", 7),
]

_COLOR_NIVEL = {"INFO": GRIS, "WARN": NARANJA, "ERROR": ROJO}


class SeccionBitacora:
    """Historial de ejecución, filtrable por nivel."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._nivel = "TODOS"
        self._construir()

    def _construir(self) -> None:
        # `selected` es una LISTA, no un set: la documentación de Flet dice
        # "a set of Segment.values" pero el tipo declarado es `list[str]`, y un
        # set no se puede serializar a msgpack. Al enviarlo, la actualización de
        # la página entera revienta y la ventana se queda EN BLANCO, sin error
        # visible. No lo cambies a set.
        self.filtro = ft.SegmentedButton(
            selected=["TODOS"],
            segments=[ft.Segment("TODOS", label=ft.Text("Todo")),
                      ft.Segment("INFO", label=ft.Text("Info")),
                      ft.Segment("WARN", label=ft.Text("Avisos")),
                      ft.Segment("ERROR", label=ft.Text("Errores"))],
            on_change=self._cambiar_filtro)
        self.txt_resumen = ft.Text("", color=GRIS)

        # Sin `wrap=True`: un espaciador `expand=True` dentro de un Row que
        # envuelve es un `Expanded` dentro de un `Wrap` en Flutter, y eso se
        # dibuja como un rectángulo gris que tapa el resto de la pantalla.
        barra = ft.Row(
            [self.filtro, ft.Container(expand=True), self.txt_resumen,
             boton_secundario("Actualizar", ft.Icons.REFRESH,
                              on_click=lambda _e: self.cargar_desde_db())],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.tabla = TablaResponsiva(self.page, _COLUMNAS)
        self.vacio = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.HISTORY, size=40, color=GRIS),
                 ft.Text("Todavía no hay nada en la bitácora.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 ft.Text("Se irá llenando en cuanto ejecutes un lote.",
                         color=GRIS)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40)

        self.contenido = ft.Column(
            [tarjeta_seccion(barra),
             ft.Container(
                 content=ft.Column([self.tabla.control, self.vacio],
                                   scroll=ft.ScrollMode.AUTO, expand=True,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True)],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola; presente por el contrato de pantalla."""

    # ------------------------------------------------------------ datos
    def cargar_desde_db(self) -> None:
        entradas = db.listar_bitacora()
        if self._nivel != "TODOS":
            entradas = [e for e in entradas if e.nivel == self._nivel]
        # El nombre del beneficiario no vive en la bitácora (que guarda solo el
        # id): se resuelve aquí para que la tabla se lea sin abrir cada fila.
        nombres = {s.id: s.beneficiario_nombre
                   for s in db.listar_solicitudes()}
        filas = [
            FilaDatos([
                e.momento.replace("T", "  "),
                ft.Text(e.nivel, size=11, weight=ft.FontWeight.BOLD,
                        color=_COLOR_NIVEL.get(e.nivel, GRIS)),
                nombres.get(e.solicitud_id, "—") or "—",
                e.paso,
                e.mensaje or "—",
                self._celda_evidencia(e.captura_ruta),
            ])
            for e in entradas
        ]
        self.tabla.set_contenido(filas)
        hay = bool(filas)
        self.tabla.control.visible = hay
        self.vacio.visible = not hay
        self.txt_resumen.value = f"{len(filas)} registro(s)"
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montado en la página
            pass

    def _celda_evidencia(self, ruta: str):
        if not ruta or not os.path.exists(ruta):
            return "—"
        return ft.IconButton(
            ft.Icons.IMAGE_OUTLINED, icon_size=18,
            tooltip="Abrir la captura de pantalla",
            on_click=lambda _e, r=ruta: self.app.abrir_en_sistema(r))

    def _cambiar_filtro(self, e) -> None:
        seleccion = list(getattr(e.control, "selected", None) or ["TODOS"])
        self._nivel = seleccion[0]
        self.cargar_desde_db()
