"""Bitácora: historial de lo que hizo el robot, paso por paso.

Cada transición de estado y cada error quedan aquí, con su evidencia cuando la
hay (SIPP tarda y falla de formas poco obvias; sin captura de pantalla es
imposible saber si el problema fue del portal o del dato). Se alimenta de
`core.db.registrar`, que el motor RPA llama antes de ejecutar cada paso.

Lo que se ve **agrupado por solicitud** y no como una lista corrida: el historial
de un lote de doscientas son miles de líneas, y lo que se busca en él casi nunca
es «qué pasó a las 15:04» sino «qué pasó con esta persona». Cada grupo lleva su
propia cuenta de errores, para localizar de un vistazo dónde mirar.
"""

from __future__ import annotations

import os

import flet as ft

from core import db
from ui.comun import GRIS, NARANJA, ROJO, VERDE, parse_fecha
from ui.componentes import CampoFecha, boton_secundario, tarjeta_seccion
from ui.tabla_responsiva import (IZQ, Cabecera, ColumnaTabla, FilaDatos,
                                 SegmentoCabecera, TablaResponsiva)

_COLUMNAS = [
    ColumnaTabla("Momento", 20, IZQ),
    ColumnaTabla("Nivel", 9),
    ColumnaTabla("Paso", 18, IZQ),
    ColumnaTabla("Mensaje", 49, IZQ),
    ColumnaTabla("Evidencia", 8),
]
_N_COLUMNAS = len(_COLUMNAS)

_COLOR_NIVEL = {"INFO": GRIS, "WARN": NARANJA, "ERROR": ROJO}

_TODOS = "TODOS"
_SIN_LOTE = "«sin lote»"


class SeccionBitacora:
    """Historial de ejecución, filtrable por nivel, lote y fechas."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._nivel = _TODOS
        self._lote_id = ""
        # Qué bloques están abiertos, y cuáles ya se mostraron alguna vez. Lo
        # segundo evita que un grupo con error que el usuario cerró a mano se
        # vuelva a abrir solo en cada refresco.
        self._expandidos: set[str] = set()
        self._conocidos: set[str] = set()
        self._construir()

    def _construir(self) -> None:
        # `selected` es una LISTA, no un set: la documentación de Flet dice
        # "a set of Segment.values" pero el tipo declarado es `list[str]`, y un
        # set no se puede serializar a msgpack. Al enviarlo, la actualización de
        # la página entera revienta y la ventana se queda EN BLANCO, sin error
        # visible. No lo cambies a set.
        self.filtro = ft.SegmentedButton(
            selected=[_TODOS],
            segments=[ft.Segment(_TODOS, label=ft.Text("Todo")),
                      ft.Segment("INFO", label=ft.Text("Info")),
                      ft.Segment("WARN", label=ft.Text("Avisos")),
                      ft.Segment("ERROR", label=ft.Text("Errores"))],
            on_change=self._cambiar_nivel)

        self.dd_lote = ft.Dropdown(
            label="Lote", options=[], width=300,
            on_select=self._cambiar_lote, expanded_insets=None)
        # `flotante=True`: la etiqueta va DENTRO del campo, como la del
        # desplegable de lote. Con la etiqueta encima, esos dos bloques son más
        # altos que el resto y la fila se ve escalonada aunque el Row centre.
        self.campo_desde = CampoFecha(
            self.page, "Desde", flotante=True,
            on_change=lambda _e: self.cargar_desde_db())
        self.campo_hasta = CampoFecha(
            self.page, "Hasta", flotante=True,
            on_change=lambda _e: self.cargar_desde_db())
        self.btn_limpiar = boton_secundario(
            "Quitar filtros", ft.Icons.FILTER_ALT_OFF,
            on_click=self._quitar_filtros)
        self.btn_expandir = boton_secundario(
            "Expandir todo", ft.Icons.UNFOLD_MORE,
            on_click=lambda _e: self._todos_los_bloques(True))
        self.btn_colapsar = boton_secundario(
            "Colapsar todo", ft.Icons.UNFOLD_LESS,
            on_click=lambda _e: self._todos_los_bloques(False))

        self.txt_resumen = ft.Text("", color=GRIS)

        # Sin `wrap=True` en el Row exterior: un espaciador `expand=True` dentro
        # de un Row que envuelve es un `Expanded` dentro de un `Wrap` en
        # Flutter, y eso se dibuja como un rectángulo gris que tapa el resto.
        barra = ft.Column([
            ft.Row([self.filtro, ft.Container(expand=True), self.txt_resumen,
                    boton_secundario("Actualizar", ft.Icons.REFRESH,
                                     on_click=lambda _e: self.cargar_desde_db())],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.dd_lote,
                    ft.Container(self.campo_desde.control, width=170),
                    ft.Container(self.campo_hasta.control, width=170),
                    self.btn_limpiar, self.btn_expandir, self.btn_colapsar],
                   spacing=12, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=12, tight=True)

        self.tabla = TablaResponsiva(self.page, _COLUMNAS, alto_cuerpo=430)
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
        # Vacío por filtro ≠ bitácora vacía: buscar un día sin actividad no
        # significa que el robot no haya corrido nunca.
        self.sin_resultados = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.SEARCH_OFF, size=40, color=GRIS),
                 ft.Text("Ningún registro coincide con el filtro.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 boton_secundario("Quitar filtros", ft.Icons.FILTER_ALT_OFF,
                                  on_click=self._quitar_filtros)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        self.contenido = ft.Column(
            [tarjeta_seccion(barra),
             ft.Container(
                 content=ft.Column([self.tabla.control, self.vacio,
                                    self.sin_resultados],
                                   scroll=ft.ScrollMode.AUTO, expand=True,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True)],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola; presente por el contrato de pantalla."""

    # ------------------------------------------------------------ filtros
    def _iso(self, campo: CampoFecha) -> str:
        """La fecha del campo como 'AAAA-MM-DD', que es como compara la base."""
        fecha = parse_fecha(campo.value)
        return fecha.strftime("%Y-%m-%d") if fecha else ""

    def _hay_filtros(self) -> bool:
        return bool(self._lote_id or self.campo_desde.value
                    or self.campo_hasta.value or self._nivel != _TODOS)

    def _cambiar_nivel(self, e) -> None:
        seleccion = list(getattr(e.control, "selected", None) or [_TODOS])
        self._nivel = seleccion[0]
        self.cargar_desde_db()

    def _cambiar_lote(self, _e=None) -> None:
        self._lote_id = self.dd_lote.value or ""
        self.cargar_desde_db()

    def _alternar_bloque(self, solicitud_id: str) -> None:
        if solicitud_id in self._expandidos:
            self._expandidos.discard(solicitud_id)
        else:
            self._expandidos.add(solicitud_id)
        self.cargar_desde_db()

    def _todos_los_bloques(self, abrir: bool) -> None:
        if abrir:
            self._expandidos = set(self._conocidos)
        else:
            self._expandidos.clear()
        self.cargar_desde_db()

    def _quitar_filtros(self, _e=None) -> None:
        self._nivel = _TODOS
        self.filtro.selected = [_TODOS]
        self._lote_id = ""
        self.dd_lote.value = ""
        self.campo_desde.value = ""
        self.campo_hasta.value = ""
        self.cargar_desde_db()

    def _refrescar_lotes(self) -> None:
        lotes = db.listar_lotes()
        self.dd_lote.options = [
            ft.DropdownOption(key="", text="Todos los lotes"),
            *[ft.DropdownOption(key=l.id, text=f"{l.nombre}  ({l.ambiente})")
              for l in lotes],
        ]
        # Un lote borrado deja el filtro apuntando a nada y la tabla vacía sin
        # explicación: se vuelve a «todos».
        if self._lote_id and self._lote_id not in {l.id for l in lotes}:
            self._lote_id = ""
        self.dd_lote.value = self._lote_id

    # ------------------------------------------------------------ datos
    def cargar_desde_db(self) -> None:
        self._refrescar_lotes()
        entradas = db.listar_bitacora(
            lote_id=self._lote_id or None,
            desde=self._iso(self.campo_desde) or None,
            hasta=self._iso(self.campo_hasta) or None)
        if self._nivel != _TODOS:
            entradas = [e for e in entradas if e.nivel == self._nivel]

        solicitudes = {s.id: s for s in db.listar_solicitudes()}
        lotes = {l.id: l.nombre for l in db.listar_lotes()}

        # Agrupado por solicitud, conservando el orden en que llegaron (más
        # reciente primero): así el grupo de arriba es el del último trabajo.
        grupos: dict[str, list] = {}
        for e in entradas:
            grupos.setdefault(e.solicitud_id, []).append(e)

        # Un bloque nuevo se abre solo si trae errores: es lo que se viene a
        # mirar. Los ya conocidos conservan como los dejó el usuario, para que
        # cerrar uno no se deshaga en el refresco siguiente.
        for solicitud_id, lineas in grupos.items():
            if solicitud_id in self._conocidos:
                continue
            self._conocidos.add(solicitud_id)
            if any(e.nivel == "ERROR" for e in lineas):
                self._expandidos.add(solicitud_id)

        filas: list = []
        for solicitud_id, lineas in grupos.items():
            filas.append(self._cabecera_grupo(solicitud_id, lineas,
                                              solicitudes, lotes))
            if solicitud_id in self._expandidos:
                filas.extend(self._fila(e) for e in lineas)

        self.tabla.set_contenido(filas)
        hay = bool(entradas)
        filtrando = self._hay_filtros()
        self.tabla.control.visible = hay
        self.vacio.visible = not hay and not filtrando
        self.sin_resultados.visible = not hay and filtrando

        errores = sum(1 for e in entradas if e.nivel == "ERROR")
        partes = [f"{len(entradas)} registro(s)", f"{len(grupos)} solicitud(es)"]
        if errores:
            partes.append(f"{errores} error(es)")
        self.txt_resumen.value = " · ".join(partes)
        self.txt_resumen.color = ROJO if errores else GRIS
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montado en la página
            pass

    def _cabecera_grupo(self, solicitud_id: str, lineas: list,
                        solicitudes: dict, lotes: dict) -> Cabecera:
        """Banda que abre el bloque de una solicitud, con su lote y su saldo."""
        s = solicitudes.get(solicitud_id)
        nombre = (s.beneficiario_nombre if s else "") or "—"
        lote = lotes.get(s.lote_id, "") if s else ""
        errores = sum(1 for e in lineas if e.nivel == "ERROR")
        avisos = sum(1 for e in lineas if e.nivel == "WARN")

        etiquetas = [f"{len(lineas)} paso(s)"]
        if errores:
            etiquetas.append(f"{errores} error(es)")
        if avisos:
            etiquetas.append(f"{avisos} aviso(s)")
        if s and s.folio_sipp:
            etiquetas.append(f"folio {s.folio_sipp}")

        color = ROJO if errores else NARANJA if avisos else VERDE
        abierto = solicitud_id in self._expandidos
        # Toda la banda es el área de clic, no solo el chevron: es una barra
        # ancha y obligar a apuntar a un ícono de 16 px es peor de usar.
        titulo = ft.Container(
            content=ft.Row(
                [ft.Icon(ft.Icons.EXPAND_LESS if abierto
                         else ft.Icons.EXPAND_MORE, size=16, color=GRIS),
                 ft.Icon(ft.Icons.RECEIPT_LONG, size=15, color=color),
                 ft.Text(nombre, size=12, weight=ft.FontWeight.BOLD,
                         no_wrap=True),
                 ft.Text(f"·  {lote or _SIN_LOTE}", size=11, color=GRIS,
                         no_wrap=True),
                 ft.Container(expand=True),
                 ft.Text("  ·  ".join(etiquetas), size=11, color=color)],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            on_click=lambda _e, sid=solicitud_id: self._alternar_bloque(sid),
            ink=True, padding=ft.Padding.symmetric(horizontal=10),
            tooltip="Ocultar los pasos" if abierto else "Ver los pasos")
        return Cabecera(
            [SegmentoCabecera(_N_COLUMNAS, titulo)],
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, alto=34)

    def _fila(self, e) -> FilaDatos:
        return FilaDatos([
            e.momento.replace("T", "  "),
            ft.Text(e.nivel, size=11, weight=ft.FontWeight.BOLD,
                    color=_COLOR_NIVEL.get(e.nivel, GRIS)),
            e.paso,
            e.mensaje or "—",
            self._celda_evidencia(e.captura_ruta),
        ])

    def _celda_evidencia(self, ruta: str):
        if not ruta or not os.path.exists(ruta):
            return "—"
        return ft.IconButton(
            ft.Icons.IMAGE_OUTLINED, icon_size=18,
            tooltip="Abrir la captura de pantalla",
            on_click=lambda _e, r=ruta: self.app.abrir_en_sistema(r))
