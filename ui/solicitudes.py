"""Pantalla principal: el lote y su tabla maestro-detalle de solicitudes.

Un **lote** es el conjunto de solicitudes que se preparan y se ejecutan juntas.
Todo cuelga de él: el punto de parada por defecto, el ambiente contra el que se
capturó y el progreso. La tabla muestra una fila por solicitud y, al desplegarla,
su desglose (conceptos e insumos), que es exactamente lo que el robot capturará
en las pestañas correspondientes de SIPP.

La ejecución del lote llega con el motor RPA (fase 2). Hasta entonces el botón
existe pero avisa; la preparación y revisión del lote ya son plenamente usables.
"""

from __future__ import annotations

import asyncio
import os
import threading

import flet as ft

from core import asignacion, conceptos as cat_conceptos, db, documentos
from core.catalogos import (AMBIENTES, ETIQUETA_ESTADO, ETIQUETA_PARADA,
                            PARADA_DEFECTO, PARADAS, TIPOS_BENEFICIARIO)
from core.db import CONCEPTO, INSUMO, Lote, Partida, Solicitud
from ui.alta_caratulas import AltaDesdeCaratulas
from ui.asignacion_masiva import AsignacionMasiva
from ui.captura_solicitud import CapturaSolicitud
from ui.carga_masiva import CargaMasiva
from ui.comun import GRIS, NARANJA, ROJO, VERDE, color_estado, color_fila, fmt_importe
from ui.componentes import (boton_herramienta, boton_primario, boton_secundario,
                            buscador, campo_opciones, fila_resultado,
                            icono_accion, tarjeta_seccion)
from ui.configuracion import ambiente_actual, navegador_visible
from ui.tabla_responsiva import (DER, Cabecera, ColumnaTabla, FilaDatos,
                                 SegmentoCabecera, TablaResponsiva)

# Número de columnas de la tabla. Se necesita como constante para el colspan de
# la banda de detalle, que se dibuja sin tener las columnas a la mano.
_N_COLUMNAS = 11

# Filas por página. Cada fila son once controles y el detalle desplegado suma
# más: con un lote de doscientas, pintarlas todas manda al cliente miles de
# controles por cada clic —marcar una casilla, desplegar un detalle— y la
# navegación se siente trabada. Cincuenta llenan de sobra la pantalla.
_POR_PAGINA = 50


# Caracteres que caben en un renglón del detalle a size=11. Es una estimación:
# la banda tiene ALTO FIJO —lo pide la tabla— así que hay que saber de antemano
# cuánto va a ocupar el texto. Se queda corto a propósito: sobrar alto deja un
# hueco, quedarse corto encima el texto sobre el desglose.
_CHARS_POR_RENGLON = 140


def _lineas_de(texto: str) -> int:
    """Cuántos renglones ocupará un mensaje dentro de la banda de detalle."""
    if not texto:
        return 0
    # Se respetan los saltos que ya trae (el validador junta varios motivos).
    return sum(max(1, -(-len(linea) // _CHARS_POR_RENGLON))
               for linea in texto.splitlines() or [texto])


def _columnas(chk_todos: ft.Control) -> list[ColumnaTabla]:
    """Las columnas de la tabla, con el check de «seleccionar todas» dentro.

    Se construyen por instancia y no como constante de módulo porque una de
    ellas lleva un CONTROL vivo: compartir la lista entre instancias haría que
    la segunda le robara el checkbox a la primera, y Flet no permite que un
    control tenga dos padres.

    Los porcentajes suman 100 (ver ui/tabla_responsiva.py). La columna de
    acciones lleva piso en píxeles porque su contenido son cuatro íconos de
    tamaño fijo: por debajo de ese ancho no se encogen, se recortan.
    """
    # Todo centrado salvo el importe: las cantidades van a la derecha para que
    # los pesos queden bajo los pesos y se puedan comparar de un vistazo, que
    # es justo lo que se pierde al centrarlas ($192,293.00 y $1,000,000.00
    # centradas no comparten ni el punto decimal ni el orden de magnitud).
    return [
        ColumnaTabla("", 4, encabezado_control=chk_todos),
        ColumnaTabla("", 4),                       # desplegar detalle
        ColumnaTabla("Estado", 9),
        ColumnaTabla("Beneficiario", 18),
        ColumnaTabla("Tipo", 8),
        ColumnaTabla("Empresa", 11),
        ColumnaTabla("Fecha", 7),
        ColumnaTabla("Importe", 10, DER),
        ColumnaTabla("Punto de parada", 10),
        ColumnaTabla("Folio SIPP", 7),
        ColumnaTabla("Acciones", 12, ancho_min_px=132),
    ]

# Las fuentes desde las que puede nacer una solicitud. Viven juntas en una lista
# porque el modal de «Nueva solicitud» es exactamente esta lista: agregar una
# fuente nueva (CFDI, cuando llegue) es agregar un renglón aquí.
_FUENTES = [
    ("manual", "Captura manual", "Una solicitud, llenando el formulario",
     ft.Icons.EDIT_NOTE),
    ("caratulas", "Alta desde carátulas",
     "Una carátula, una solicitud: lee la CLABE y el titular de cada archivo",
     ft.Icons.ACCOUNT_BALANCE),
    ("excel", "Carga masiva desde Excel",
     "Muchas solicitudes desde la plantilla, con vista previa fila por fila",
     ft.Icons.UPLOAD_FILE),
]


class SeccionSolicitudes:
    """Tabla maestro-detalle del lote activo, con su barra de acciones."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._lote: Lote | None = None
        self._solicitudes: list[Solicitud] = []
        self._partidas: dict[str, list[Partida]] = {}
        self._expandidos: set[str] = set()
        self._seleccionados: set[str] = set()
        self._detener = False
        # Despierta al hilo del motor si la app se cierra con el navegador
        # abierto; None cuando no hay lote corriendo. Ver `_correr_lote`.
        self._liberar_rpa = None
        self._busqueda = ""
        # Los tres tipos activos de entrada: el filtro sirve para acotar, no
        # para esconder. `selected` de SegmentedButton es LISTA, no set (ver
        # el aviso en ui/bitacora.py: un set revienta la serialización).
        self._tipos: list[str] = list(TIPOS_BENEFICIARIO)
        self._pagina = 0
        self.captura = CapturaSolicitud(app, self._recibir_solicitud)
        self.carga = CargaMasiva(app, self._tras_importar)
        self.alta_caratulas = AltaDesdeCaratulas(app, self._tras_importar)
        self.asignacion = AsignacionMasiva(app, self._tras_importar)
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        self.dd_lote = ft.Dropdown(
            label="Lote", options=[], on_select=self._cambiar_lote,
            width=280, expanded_insets=None)
        _, self.dd_parada = campo_opciones(
            None, [ETIQUETA_PARADA[p] for p in PARADAS],
            valor=ETIQUETA_PARADA[PARADA_DEFECTO], width=210,
            on_change=self._cambiar_parada_lote)

        self.txt_resumen = ft.Text("Sin lote activo.", color=GRIS)
        self.txt_ambiente = ft.Container(
            content=ft.Text("", size=11, weight=ft.FontWeight.BOLD),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=999)
        self._pintar_ambiente()

        # OJO con el patrón de esta barra: el espaciador `expand=True` que empuja
        # el grupo derecho NO puede convivir con `wrap=True` en el MISMO Row. En
        # Flutter eso es un `Expanded` dentro de un `Wrap`, que lanza un error de
        # ParentDataWidget y se dibuja como un rectángulo gris liso, tapando todo
        # lo que sigue. La solución es anidar: el Row exterior no envuelve (y ahí
        # sí vale `expand`), y el grupo izquierdo es un Row interior que envuelve
        # pero no lleva hijos expandibles.
        barra_lote = ft.Row(
            [ft.Row([self.dd_lote,
                     boton_secundario("Nuevo lote", ft.Icons.CREATE_NEW_FOLDER,
                                      on_click=self._nuevo_lote)],
                    spacing=12, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             ft.Text("Parada por defecto:", color=GRIS), self.dd_parada,
             self.txt_ambiente],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.btn_asignar = boton_herramienta(
            "Asignación masiva", ft.Icons.PLAYLIST_ADD_CHECK,
            on_click=self._asignacion_masiva)
        self.btn_eliminar_todas = boton_herramienta(
            "Eliminar todas", ft.Icons.DELETE_SWEEP, on_click=self._eliminar_todas,
            tooltip="Vaciar el lote por completo", destructivo=True)
        self.btn_ejecutar = boton_primario(
            "Ejecutar lote", ft.Icons.PLAY_ARROW, on_click=self._ejecutar)

        # --- Barra global: lo que aplica al lote entero, haya o no selección.
        # Mismo patrón anidado que `barra_lote` (ver el comentario de arriba).
        self.barra_global = ft.Row(
            [ft.Row([boton_secundario("Nueva solicitud", ft.Icons.ADD,
                                      on_click=self._nueva_solicitud,
                                      tooltip="Elegir de dónde sale la "
                                              "solicitud: captura, carátulas "
                                              "o Excel"),
                     boton_secundario("Adjuntar por carpeta",
                                      ft.Icons.DRIVE_FOLDER_UPLOAD,
                                      on_click=self._adjuntar_carpeta,
                                      tooltip="Asignar carátulas o Vo.Bo. a "
                                              "todo el lote, emparejando por "
                                              "el nombre del beneficiario"),
                     self.btn_asignar, self.btn_eliminar_todas],
                    spacing=8, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self.txt_resumen,
             self.btn_ejecutar],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # --- Barra contextual: aparece SOLO con filas seleccionadas.
        # Sustituye a la global en vez de sumarse a ella, y esa es la razón de
        # que exista: las acciones sobre la selección no tienen sentido sin
        # selección, y tenerlas ahí apagadas obliga a leer botones muertos cada
        # vez que se mira la barra.
        self.txt_seleccion = ft.Text("", weight=ft.FontWeight.BOLD)
        self.barra_seleccion = ft.Row(
            [ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, size=18,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     self.txt_seleccion,
                     boton_herramienta("Omitir", ft.Icons.BLOCK,
                                       on_click=self._omitir,
                                       tooltip="No capturar estas solicitudes "
                                               "al ejecutar el lote"),
                     boton_herramienta("Eliminar", ft.Icons.DELETE_OUTLINE,
                                       on_click=self._eliminar_seleccionadas,
                                       destructivo=True),
                     boton_herramienta("Asignación masiva",
                                       ft.Icons.PLAYLIST_ADD_CHECK,
                                       on_click=self._asignacion_masiva),
                     boton_herramienta("Limpiar selección", ft.Icons.CLOSE,
                                       on_click=self._limpiar_seleccion)],
                    spacing=8, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            visible=False)

        # --- Barra de filtros: buscador por beneficiario + tipos.
        self.tf_buscar = buscador("Buscar beneficiario…", width=320)
        self.tf_buscar.on_change = self._cambiar_busqueda
        self.btn_limpiar_busqueda = icono_accion(
            ft.Icons.CLOSE, "Limpiar búsqueda", self._limpiar_busqueda,
            color=GRIS)
        self.btn_limpiar_busqueda.visible = False
        self.tf_buscar.suffix = self.btn_limpiar_busqueda

        # `allow_empty_selection=False`: quedarse sin ningún tipo marcado
        # dejaría la tabla vacía sin que se vea por qué. Con los tres marcados
        # no filtra nada, que es el estado natural.
        self.filtro_tipos = ft.SegmentedButton(
            selected=list(self._tipos),
            allow_multiple_selection=True,
            allow_empty_selection=False,
            segments=[ft.Segment(t, label=ft.Text(t))
                      for t in TIPOS_BENEFICIARIO],
            on_change=self._cambiar_tipos)
        self.txt_filtrado = ft.Text("", color=GRIS, size=12)

        barra_filtros = ft.Row(
            [ft.Row([self.tf_buscar, self.filtro_tipos, self.txt_filtrado],
                    spacing=12, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # El check del encabezado se construye UNA vez y se muta: la tabla no
        # recrea su encabezado al repintar, justo para no re-parentear este
        # control (ver ui/tabla_responsiva.py).
        self.chk_todos = ft.Checkbox(
            value=False, scale=0.85, tooltip="Seleccionar todas",
            on_change=self._alternar_todos)

        # --- Paginación.
        self.btn_pag_anterior = icono_accion(
            ft.Icons.CHEVRON_LEFT, "Página anterior",
            lambda _e: self._mover_pagina(-1))
        self.btn_pag_siguiente = icono_accion(
            ft.Icons.CHEVRON_RIGHT, "Página siguiente",
            lambda _e: self._mover_pagina(1))
        self.txt_pagina = ft.Text("", color=GRIS, size=12)
        self.barra_paginacion = ft.Row(
            [ft.Container(expand=True), self.btn_pag_anterior,
             self.txt_pagina, self.btn_pag_siguiente],
            spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            visible=False)

        self.tabla = TablaResponsiva(self.page, _columnas(self.chk_todos))
        self.vacio = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.RECEIPT_LONG, size=40, color=GRIS),
                 ft.Text("Este lote todavía no tiene solicitudes.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 ft.Text("Usa «Nueva solicitud» y elige de dónde sale: captura "
                         "a mano, desde las carátulas o desde un Excel.",
                         color=GRIS, text_align=ft.TextAlign.CENTER)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        # Vacío por filtro ≠ lote vacío: con un solo mensaje, buscar a alguien
        # que no está invitaría a pensar que el lote se quedó sin solicitudes.
        self.txt_sin_resultados = ft.Text("", color=GRIS,
                                          text_align=ft.TextAlign.CENTER)
        self.sin_resultados = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.SEARCH_OFF, size=40, color=GRIS),
                 ft.Text("Ninguna solicitud coincide con el filtro.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 self.txt_sin_resultados,
                 boton_secundario("Quitar los filtros", ft.Icons.FILTER_ALT_OFF,
                                  on_click=self._quitar_filtros)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        self.contenido = ft.Column(
            [tarjeta_seccion(ft.Column([barra_lote, ft.Divider(),
                                        self.barra_global,
                                        self.barra_seleccion,
                                        ft.Divider(), barra_filtros],
                                       spacing=12, tight=True)),
             ft.Container(
                 content=ft.Column([self.tabla.control, self.vacio,
                                    self.sin_resultados],
                                   scroll=ft.ScrollMode.AUTO, expand=True,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True),
             self.barra_paginacion],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola con `on_size_change`; no hay nada que
        reacomodar aquí. Presente por el contrato de pantalla del shell."""

    def _marcar_detener(self) -> None:
        self._detener = True

    def cerrar_rpa(self) -> None:
        """Suelta el navegador si quedó abierto. Lo llama el shell al salir.

        Sin esto, cerrar la ventana con el navegador esperando dejaría un
        Chromium huérfano consumiendo memoria hasta reiniciar el equipo: el
        proceso no cuelga de la ventana de la app, sino del hilo del motor.
        """
        liberar = self._liberar_rpa
        if callable(liberar):
            try:
                liberar()
            except Exception:  # noqa: BLE001 — cerrando, nada debe estorbar
                pass

    # -------------------------------------------------------- ambiente
    def _pintar_ambiente(self) -> None:
        """Indicador SIEMPRE visible del ambiente activo: en producción cada
        solicitud guardada consume un folio real y eso no debe poder olvidarse."""
        amb = ambiente_actual()
        produccion = amb == "PRODUCCION"
        self.txt_ambiente.content.value = (
            "PRODUCCIÓN" if produccion else "PRUEBAS")
        self.txt_ambiente.content.color = (
            ft.Colors.ON_ERROR if produccion else ft.Colors.ON_SECONDARY_CONTAINER)
        self.txt_ambiente.bgcolor = (
            ft.Colors.ERROR if produccion else ft.Colors.SECONDARY_CONTAINER)

    def refrescar_ambiente(self) -> None:
        self._pintar_ambiente()
        try:
            self.txt_ambiente.update()
        except Exception:  # noqa: BLE001 — aún no montado
            pass

    # ------------------------------------------------------------ datos
    def cargar_desde_db(self) -> None:
        """Carga inicial: lotes existentes y el último usado (o uno nuevo)."""
        lotes = db.listar_lotes()
        if not lotes:
            self._lote = db.guardar_lote(Lote(nombre=self._nombre_sugerido(),
                                              ambiente=ambiente_actual()))
            lotes = [self._lote]
        else:
            self._lote = lotes[0]
        self._refrescar_lotes(lotes)
        self._recargar_solicitudes()

    def _nombre_sugerido(self) -> str:
        from datetime import datetime
        return f"Lote {datetime.now().strftime('%d/%m/%Y %H:%M')}"

    def _refrescar_lotes(self, lotes: list[Lote] | None = None) -> None:
        lotes = lotes if lotes is not None else db.listar_lotes()
        self.dd_lote.options = [
            ft.DropdownOption(key=l.id, text=f"{l.nombre}  ({l.ambiente})")
            for l in lotes]
        self.dd_lote.value = self._lote.id if self._lote else None
        if self._lote:
            self.dd_parada.value = ETIQUETA_PARADA.get(
                self._lote.parada_default, ETIQUETA_PARADA[PARADA_DEFECTO])

    def _recargar_solicitudes(self) -> None:
        if not self._lote:
            self._solicitudes = []
        else:
            self._solicitudes = db.listar_solicitudes(self._lote.id)
        self._partidas = {
            s.id: db.listar_partidas(s.id) for s in self._solicitudes}
        # Una selección que apunte a filas ya borradas confunde a las acciones.
        vivos = {s.id for s in self._solicitudes}
        self._seleccionados &= vivos
        self._expandidos &= vivos
        self._pintar()

    # ------------------------------------------------------------ filtros
    def _coincide(self, s: Solicitud) -> bool:
        """True si la solicitud pasa el buscador y el filtro de tipos.

        El nombre se compara con `conceptos.normalizar`, el mismo criterio del
        catálogo: buscar «jose muñoz» encuentra «JOSÉ MUÑOZ» sin depender de
        acentos. Las palabras van sueltas y en cualquier orden, porque nadie
        recuerda si el apellido iba antes o después.
        """
        if s.tipo_beneficiario not in self._tipos:
            return False
        palabras = cat_conceptos.normalizar(self._busqueda).split()
        if not palabras:
            return True
        nombre = cat_conceptos.normalizar(s.beneficiario_nombre or "")
        return all(p in nombre for p in palabras)

    def _visibles(self) -> list[Solicitud]:
        """Las solicitudes que pasan los filtros, en el orden del lote."""
        return [s for s in self._solicitudes if self._coincide(s)]

    def _filtrando(self) -> bool:
        return bool(cat_conceptos.normalizar(self._busqueda)) or \
            len(self._tipos) != len(TIPOS_BENEFICIARIO)

    def _cambiar_busqueda(self, _e=None) -> None:
        self._busqueda = self.tf_buscar.value or ""
        self._pagina = 0          # filtrar y quedarse en la página 7 no tiene
        self._pintar()            # sentido: el resultado suele caber en una

    def _limpiar_busqueda(self, _e=None) -> None:
        self.tf_buscar.value = ""
        self._busqueda = ""
        self._pagina = 0
        self._pintar()

    def _cambiar_tipos(self, e=None) -> None:
        seleccion = list(getattr(e.control, "selected", None) or []) if e else []
        self._tipos = seleccion or list(TIPOS_BENEFICIARIO)
        self._pagina = 0
        self._pintar()

    def _quitar_filtros(self, _e=None) -> None:
        self.tf_buscar.value = ""
        self._busqueda = ""
        self._tipos = list(TIPOS_BENEFICIARIO)
        self.filtro_tipos.selected = list(TIPOS_BENEFICIARIO)
        self._pagina = 0
        self._pintar()

    def _mover_pagina(self, delta: int) -> None:
        self._pagina += delta
        self._pintar()

    # ------------------------------------------------------------ pintado
    def _pintar(self) -> None:
        visibles = self._visibles()
        # La página se recorta aquí y no al cambiarla: borrar o filtrar puede
        # dejarla fuera de rango, y una tabla vacía con filas detrás es un
        # estado del que el usuario no sabe salir.
        paginas = max(1, -(-len(visibles) // _POR_PAGINA))
        self._pagina = max(0, min(self._pagina, paginas - 1))
        desde = self._pagina * _POR_PAGINA
        pagina = visibles[desde:desde + _POR_PAGINA]

        filas: list = []
        for s in pagina:
            filas.append(self._fila_solicitud(s))
            if s.id in self._expandidos:
                filas.append(self._fila_detalle(s))
        self.tabla.set_contenido(filas)

        hay_lote = bool(self._solicitudes)
        self.vacio.visible = not hay_lote
        self.sin_resultados.visible = hay_lote and not visibles
        self.tabla.control.visible = bool(pagina)
        self.txt_sin_resultados.value = (
            f"Ninguna de las {len(self._solicitudes)} solicitudes del lote "
            f"coincide con lo que buscas.")

        self.barra_paginacion.visible = paginas > 1
        self.txt_pagina.value = (
            f"{desde + 1}–{desde + len(pagina)} de {len(visibles)}  ·  "
            f"página {self._pagina + 1} de {paginas}")
        self.btn_pag_anterior.disabled = self._pagina == 0
        self.btn_pag_siguiente.disabled = self._pagina >= paginas - 1

        self.btn_limpiar_busqueda.visible = bool(self._busqueda.strip())
        self.txt_filtrado.value = (
            f"{len(visibles)} de {len(self._solicitudes)}"
            if self._filtrando() else "")

        self._actualizar_resumen()
        self._actualizar_acciones()
        self._refrescar()

    def _fila_solicitud(self, s: Solicitud) -> FilaDatos:
        chk = ft.Checkbox(
            value=s.id in self._seleccionados, scale=0.85,
            on_change=lambda e, sid=s.id: self._alternar_seleccion(sid, e))
        expandido = s.id in self._expandidos
        chevron = ft.IconButton(
            ft.Icons.EXPAND_MORE if not expandido else ft.Icons.EXPAND_LESS,
            icon_size=18, tooltip="Ver el desglose",
            on_click=lambda _e, sid=s.id: self._alternar_detalle(sid))
        estado = ft.Container(
            content=ft.Text(ETIQUETA_ESTADO.get(s.estado, s.estado), size=11,
                            weight=ft.FontWeight.BOLD, no_wrap=True,
                            color=color_estado(s.estado)),
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=4,
            bgcolor=ft.Colors.with_opacity(0.12, color_estado(s.estado)))
        parada = ft.Text(ETIQUETA_PARADA.get(s.parada, s.parada), size=12,
                         no_wrap=True)
        return FilaDatos(
            [chk, chevron, estado, s.beneficiario_nombre or "—",
             s.tipo_beneficiario, s.empresa or "—", s.fecha_pago or "—",
             fmt_importe(s.importe_total), parada, s.folio_sipp or "—",
             self._acciones_fila(s)],
            bgcolor=color_fila(s.estado))

    def _acciones_fila(self, s: Solicitud) -> ft.Control:
        """Los cuatro botones que actúan sobre ESTA solicitud.

        Van en la fila y no en la barra porque son acciones de una sola
        solicitud: obligar a seleccionarla primero añade un clic y, peor, deja
        una selección viva que después se arrastra a la siguiente acción masiva
        sin que nadie lo note.
        """
        puede_deshacer = self._lote is not None and db.solicitud_en_snapshot(
            self._lote.id, s.id)
        return ft.Row(
            [icono_accion(ft.Icons.EDIT, "Editar esta solicitud",
                          lambda _e, sid=s.id: self._editar(sid)),
             icono_accion(ft.Icons.CONTENT_COPY, "Duplicar esta solicitud",
                          lambda _e, sid=s.id: self._duplicar(sid)),
             icono_accion(
                 ft.Icons.UNDO,
                 ("Revertir esta solicitud a como estaba antes de la última "
                  "operación masiva" if puede_deshacer else
                  "Esta solicitud no cambió en la última operación masiva"),
                 (lambda _e, sid=s.id: self._deshacer_fila(sid))
                 if puede_deshacer else None,
                 color=None if puede_deshacer else ft.Colors.OUTLINE_VARIANT),
             icono_accion(ft.Icons.DELETE_OUTLINE, "Eliminar esta solicitud",
                          lambda _e, sid=s.id: self._eliminar(sid),
                          color=ft.Colors.ERROR)],
            spacing=4, tight=True,
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _fila_detalle(self, s: Solicitud) -> Cabecera:
        """Banda con el desglose de la solicitud, sangrada bajo su fila."""
        partidas = self._partidas.get(s.id, [])
        conceptos = [p for p in partidas if p.clase == CONCEPTO]
        insumos = [p for p in partidas if p.clase == INSUMO]

        def bloque(titulo: str, items: list[Partida], render) -> ft.Control:
            if not items:
                return ft.Text(f"{titulo}: sin renglones.", size=11, color=GRIS)
            return ft.Column(
                [ft.Text(titulo, size=11, weight=ft.FontWeight.BOLD,
                         color=ft.Colors.PRIMARY_CONTAINER),
                 *[ft.Text(render(p), size=11, color=GRIS) for p in items]],
                spacing=2, tight=True)

        detalle = ft.Row(
            [bloque("Conceptos de pago", conceptos,
                    lambda p: f"• {p.concepto_nombre or '—'}   "
                              f"{fmt_importe(p.importe)}"),
             bloque("Insumos y servicios", insumos,
                    lambda p: f"• {p.insumo_nombre or '—'}   "
                              f"{fmt_importe(p.importe)}   "
                              f"CC {p.centro_costos or '—'} · "
                              f"Cta {p.cuenta_contable or '—'}"),
             ft.Column(
                 [ft.Text("Cuenta destino", size=11, weight=ft.FontWeight.BOLD,
                          color=ft.Colors.PRIMARY_CONTAINER),
                  ft.Text(f"{s.cuenta_banco or '—'} · "
                          f"CLABE {s.cuenta_clabe or '—'}", size=11, color=GRIS),
                  ft.Text(f"Origen: {s.origen} · {s.forma_pago} · "
                          f"{s.tipo_gasto}", size=11, color=GRIS),
                  self._texto_documentos(s)],
                 spacing=2, tight=True)],
            spacing=32, vertical_alignment=ft.CrossAxisAlignment.START,
            expand=True)
        # El motivo de «Revisar a mano» va en su propia banda, con separador e
        # ícono: pegado al desglose se leía como un renglón más del pago, y es
        # lo contrario —la razón por la que ese pago NO se va a capturar—.
        lineas_error = 0
        if s.error_msg:
            lineas_error = _lineas_de(s.error_msg)
            detalle = ft.Column(
                [detalle,
                 ft.Divider(height=1),
                 ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, size=14, color=ROJO),
                         ft.Text(s.error_msg, size=11, color=ROJO, expand=True)],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.START)],
                spacing=6, tight=True)

        # Alto estimado por el bloque más largo, para que la banda no recorte.
        # El error suma sus propios renglones: sin contarlos, un mensaje largo
        # se dibujaba ENCIMA del desglose en vez de debajo.
        renglones = max(len(conceptos), len(insumos), 2) + 1 + lineas_error
        extra_error = 14 if s.error_msg else 0   # el divisor y su holgura
        return Cabecera(
            [SegmentoCabecera(2, ft.Container()),
             SegmentoCabecera(_N_COLUMNAS - 2, detalle,
                              padding=ft.Padding.symmetric(vertical=8))],
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            alto=max(64, 18 * renglones + 24 + extra_error))

    def _texto_documentos(self, s: Solicitud) -> ft.Control:
        """Los archivos de la solicitud, abribles con un clic.

        Se abren en el visor del sistema en vez de dentro de la herramienta:
        para comprobar que la CLABE leída es la de la carátula hace falta ver el
        documento a tamaño real y poder acercarse, y eso ya lo hace bien el
        visor de PDF o de imágenes que la persona usa todos los días.
        """
        archivos = documentos.de_solicitud(s.id)
        controles: list[ft.Control] = [
            ft.Text("Documentos:", size=11, color=GRIS)]

        def enlace(etiqueta: str, ruta: str, icono) -> ft.Control:
            return ft.Container(
                content=ft.Row(
                    [ft.Icon(icono, size=13, color=ft.Colors.PRIMARY),
                     ft.Text(etiqueta, size=11, color=ft.Colors.PRIMARY,
                             weight=ft.FontWeight.W_500)],
                    spacing=3, tight=True),
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4, ink=True,
                tooltip=f"Abrir {os.path.basename(ruta)}",
                on_click=lambda _e, r=ruta: self.app.abrir_en_sistema(r))

        if archivos.get("caratula"):
            controles.append(enlace("Carátula", archivos["caratula"],
                                    ft.Icons.ACCOUNT_BALANCE))
        elif documentos.falta_caratula(s):
            controles.append(
                ft.Text("FALTA LA CARÁTULA (obligatoria para dar de alta la "
                        "cuenta)", size=11, color=ROJO,
                        weight=ft.FontWeight.BOLD))
        if archivos.get("vobo"):
            controles.append(enlace("Vo.Bo. de compras", archivos["vobo"],
                                    ft.Icons.FACT_CHECK))
        if len(controles) == 1:
            controles.append(ft.Text("—", size=11, color=GRIS))
        return ft.Row(controles, spacing=4, tight=True, wrap=True,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _actualizar_resumen(self) -> None:
        if not self._lote:
            self.txt_resumen.value = "Sin lote activo."
            return
        total = sum(s.importe_total for s in self._solicitudes)
        n = len(self._solicitudes)
        listas = sum(1 for s in self._solicitudes if s.estado == "VALIDADA")
        self.txt_resumen.value = (
            f"{n} solicitud{'es' if n != 1 else ''} · "
            f"{listas} lista{'s' if listas != 1 else ''} · {fmt_importe(total)}")

    def _actualizar_acciones(self) -> None:
        """Estado de las dos barras según la selección.

        La global y la contextual se excluyen: con filas seleccionadas se ve la
        segunda y no la primera. Lo único que sobrevive a los dos modos es
        «Ejecutar lote», porque ejecutar no depende de lo que esté marcado.
        """
        n = len(self._seleccionados)
        self.barra_seleccion.visible = n > 0
        self.barra_global.visible = n == 0
        self.txt_seleccion.value = (
            f"{n} seleccionada{'s' if n != 1 else ''}")
        self.btn_asignar.disabled = not self._solicitudes
        self.btn_eliminar_todas.disabled = not self._solicitudes
        self.btn_ejecutar.disabled = not self._solicitudes
        # El check del encabezado refleja la selección en vez de mandarla: si
        # se marcan las filas una por una hasta completarlas todas, tiene que
        # aparecer marcado, no seguir vacío. Se mide contra lo VISIBLE, que es
        # sobre lo que actúa al pulsarlo.
        visibles = {s.id for s in self._visibles()}
        self.chk_todos.value = bool(visibles) and visibles <= self._seleccionados
        self.chk_todos.tooltip = ("Quitar la selección" if self.chk_todos.value
                                  else "Seleccionar todas")

    # ------------------------------------------------------------ eventos
    def _refrescar(self) -> None:
        """Repinta la pantalla si ya está montada.

        Todo lo que reacciona a un clic pasa por aquí: antes de que la pantalla
        esté en la página, `update()` lanza, y las acciones que no lo protegían
        reventaban al ejercitarlas fuera de la ventana.
        """
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montada en la página
            pass

    def _alternar_seleccion(self, sid: str, e) -> None:
        if e.control.value:
            self._seleccionados.add(sid)
        else:
            self._seleccionados.discard(sid)
        self._actualizar_acciones()
        self._refrescar()

    def _alternar_todos(self, e=None) -> None:
        """Check del encabezado: marca o desmarca lo que el filtro deja ver.

        Actúa sobre TODO lo filtrado y no solo sobre la página en pantalla: el
        caso de uso es «filtra los Proveedores y aplícales algo», y tener que
        marcar página por página lo volvería inútil. Lo que el filtro esconde no
        se toca, ni para marcar ni para desmarcar.
        """
        marcar = bool(e.control.value) if e is not None else True
        visibles = {s.id for s in self._visibles()}
        if marcar:
            self._seleccionados |= visibles
        else:
            self._seleccionados -= visibles
        # Repintado completo: hay que mover el check de CADA fila, y viven en
        # controles que la tabla recrea al pintar el cuerpo.
        self._pintar()

    def _limpiar_seleccion(self, _e=None) -> None:
        self._seleccionados.clear()
        self._pintar()

    def _alternar_detalle(self, sid: str) -> None:
        if sid in self._expandidos:
            self._expandidos.discard(sid)
        else:
            self._expandidos.add(sid)
        self._pintar()

    def _solicitud(self, sid: str) -> Solicitud | None:
        return next((s for s in self._solicitudes if s.id == sid), None)

    # ------------------------------------------------------------ lotes
    def _nuevo_lote(self, _e=None) -> None:
        self._lote = db.guardar_lote(Lote(nombre=self._nombre_sugerido(),
                                          ambiente=ambiente_actual()))
        self._seleccionados.clear()
        self._expandidos.clear()
        self._refrescar_lotes()
        self._recargar_solicitudes()
        self.app.avisar(f"Lote «{self._lote.nombre}» creado.", VERDE)

    def _cambiar_lote(self, _e=None) -> None:
        elegido = self.dd_lote.value
        if not elegido or (self._lote and elegido == self._lote.id):
            return
        self._lote = db.obtener_lote(elegido)
        self._seleccionados.clear()
        self._expandidos.clear()
        self._refrescar_lotes()
        self._recargar_solicitudes()

    def _cambiar_parada_lote(self, _e=None) -> None:
        """Cambia la parada por defecto del lote y la propaga a las filas que
        aún no se han procesado. Las ya ejecutadas se quedan como están: su
        parada es historia, no configuración."""
        if not self._lote:
            return
        etiqueta = self.dd_parada.value
        clave = next((k for k, v in ETIQUETA_PARADA.items() if v == etiqueta),
                     PARADA_DEFECTO)
        self._lote.parada_default = clave
        db.guardar_lote(self._lote)
        pendientes = {"PENDIENTE", "VALIDADA", "ERROR"}
        for s in self._solicitudes:
            if s.estado in pendientes:
                db.actualizar_parada(s.id, clave)
        self._recargar_solicitudes()

    # ------------------------------------------------------- solicitudes
    def _nueva_solicitud(self, _e=None) -> None:
        """Pregunta de dónde sale la solicitud antes de abrir nada.

        Las tres fuentes producen lo mismo —solicitudes en este lote— pero se
        usan en momentos distintos, y tenerlas como tres botones sueltos en la
        barra obligaba a saber de antemano cuál era cuál. Aquí cada una se
        explica en su renglón.
        """
        if not self._lote:
            self.app.avisar("Primero crea un lote.", NARANJA)
            return

        abrir = {"manual": lambda: self.captura.abrir(self._lote.id),
                 "caratulas": lambda: self.alta_caratulas.abrir(self._lote.id),
                 "excel": lambda: self.carga.abrir(self._lote.id)}

        def elegir(clave: str):
            def _accion(_e=None) -> None:
                self.page.pop_dialog()
                abrir[clave]()
            return _accion

        opciones = [
            ft.Container(
                ft.Row([ft.Icon(icono, color=ft.Colors.PRIMARY_CONTAINER),
                        ft.Column([ft.Text(titulo, size=13,
                                           weight=ft.FontWeight.BOLD),
                                   ft.Text(detalle, size=11, color=GRIS)],
                                  spacing=0, tight=True, expand=True)],
                       spacing=12,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                border_radius=8, ink=True, on_click=elegir(clave))
            for clave, titulo, detalle, icono in _FUENTES
        ]

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Nueva solicitud"),
            content=ft.Column(
                [ft.Text("¿De dónde sale esta solicitud?", color=GRIS),
                 *opciones],
                spacing=4, tight=True, width=460),
            actions=[ft.TextButton(
                "Cancelar", on_click=lambda _e: self.page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    def _editar(self, sid: str) -> None:
        s = self._solicitud(sid)
        if not s:
            return
        self.captura.abrir(self._lote.id, s, self._partidas.get(s.id, []))

    def _duplicar(self, sid: str) -> None:
        """Abre el editor con una copia de la solicitud.

        No la guarda de inmediato a propósito: una copia idéntica produce la
        MISMA clave de idempotencia y sería rechazada. Duplicar sirve para
        partir de una solicitud parecida y cambiarle lo que toque —el
        beneficiario, el importe—, y eso solo puede hacerlo el usuario.
        """
        s = self._solicitud(sid)
        if not s:
            return
        copia = Solicitud(**{**s.__dict__})
        copia.id = Solicitud().id             # id nuevo
        copia.folio_sipp = ""
        copia.estado = "PENDIENTE"
        copia.error_msg = ""
        copia.intentos = 0
        copia.clave_idempotencia = ""
        copia.orden = len(self._solicitudes)
        partidas = [Partida(**{**p.__dict__, "id": Partida().id,
                               "solicitud_id": copia.id})
                    for p in self._partidas.get(s.id, [])]
        self.captura.abrir(self._lote.id, copia, partidas)

    def _deshacer_fila(self, sid: str) -> None:
        """Revierte UNA solicitud a como estaba antes de la última masiva.

        No consume el snapshot (ver `db.restaurar_solicitud_snapshot`): lo
        normal es que una asignación masiva quede bien en casi todo el lote y
        mal en una o dos, y arreglar la primera no puede dejar a las otras sin
        vuelta atrás.
        """
        if not self._lote:
            return
        s = self._solicitud(sid)
        nombre = s.beneficiario_nombre if s else "la solicitud"

        def confirmar(_e=None) -> None:
            self.page.pop_dialog()
            if db.restaurar_solicitud_snapshot(self._lote.id, sid):
                self._recargar_solicitudes()
                self.app.avisar(
                    f"«{nombre}» volvió a como estaba antes de la última "
                    f"operación masiva.", VERDE)
            else:
                self.app.avisar("Esa solicitud no tiene un estado anterior al "
                                "que volver.", NARANJA)

        snap = db.hay_snapshot(self._lote.id)
        cuando = (snap["creado_en"][:16].replace("T", " ") if snap else "")
        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Deshacer en esta solicitud"),
            content=ft.Text(
                f"«{nombre}» volverá a como estaba antes de "
                f"«{snap['motivo'] if snap else 'la operación'}» ({cuando}).\n\n"
                "El resto del lote se queda como está, y se pierde lo que le "
                "hayas cambiado a mano después."),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Deshacer", icon=ft.Icons.UNDO,
                                on_click=confirmar),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def _omitir(self, _e=None) -> None:
        for sid in list(self._seleccionados):
            db.actualizar_estado(sid, "OMITIDA")
        cuantas = len(self._seleccionados)
        self._seleccionados.clear()
        self._recargar_solicitudes()
        self.app.avisar(f"{cuantas} solicitud(es) marcadas como omitidas.",
                        NARANJA)

    def _eliminar_seleccionadas(self, _e=None) -> None:
        """Manejador del botón de la barra: borra lo que esté seleccionado.

        Existe separado de `_eliminar` a propósito. Flet pasa el evento como
        PRIMER argumento posicional, así que enganchar `on_click=self._eliminar`
        metía el objeto del evento en `sid` y la pantalla intentaba borrar una
        solicitud con ese id: no encontraba ninguna y reventaba al confirmar.
        Con dos métodos, uno por cada punto de entrada, no hay forma de que el
        evento se confunda con un dato.
        """
        self._eliminar()

    def _eliminar(self, sid: str | None = None) -> None:
        """Elimina una solicitud (por su id) o todas las seleccionadas.

        Es la misma acción desde dos lados —el ícono de la fila y la barra de
        selección—, así que comparte confirmación: cambiar solo el texto evita
        dos diálogos que se pueden desincronizar.
        """
        objetivo = [sid] if sid else list(self._seleccionados)
        if not objetivo:
            return
        if sid:
            s = self._solicitud(sid)
            que = f"«{s.beneficiario_nombre or 'sin beneficiario'}»" if s \
                else "la solicitud"
        else:
            que = f"{len(objetivo)} solicitud(es)"

        def confirmar(_e=None) -> None:
            self.page.pop_dialog()
            for uno in objetivo:
                db.borrar_solicitud(uno)
                self._seleccionados.discard(uno)
            self._recargar_solicitudes()
            self.app.avisar(f"Se eliminó {que}.", VERDE)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Eliminar"),
            content=ft.Text(
                f"Se eliminará {que} con su desglose y su bitácora. Esto no se "
                "puede deshacer."),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Eliminar", icon=ft.Icons.DELETE_OUTLINE,
                                on_click=confirmar),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def _eliminar_todas(self, _e=None) -> None:
        """Vacía el lote entero.

        Se confirma nombrando cuántas y por cuánto: «eliminar todas» es la única
        acción de la barra que no se puede acotar con la selección, y el número
        es lo que hace notar si el lote activo no era el que se creía.
        """
        if not self._solicitudes:
            return
        cuantas = len(self._solicitudes)
        total = sum(s.importe_total for s in self._solicitudes)
        capturadas = sum(1 for s in self._solicitudes
                         if s.estado in ("GUARDADA", "ENVIADA_AUTORIZAR"))

        def confirmar(_e=None) -> None:
            self.page.pop_dialog()
            for s in list(self._solicitudes):
                db.borrar_solicitud(s.id)
            self._seleccionados.clear()
            self._expandidos.clear()
            self._recargar_solicitudes()
            self.app.avisar(f"El lote quedó vacío: {cuantas} solicitud(es) "
                            f"eliminadas.", VERDE)

        detalle = [
            ft.Text(f"Se eliminarán las {cuantas} solicitudes del lote "
                    f"«{self._lote.nombre}», por {fmt_importe(total)}, con su "
                    f"desglose y su bitácora."),
            ft.Text("Esto no se puede deshacer.", color=NARANJA),
        ]
        if capturadas:
            # Borrarlas de aquí no las cancela en SIPP: el folio ya se consumió
            # y quedaría vivo allá sin nada de este lado que lo explique.
            detalle.append(ft.Text(
                f"{capturadas} ya se capturaron en SIPP. Borrarlas aquí NO las "
                f"cancela allá: su folio seguirá vivo en el portal.",
                color=ROJO, weight=ft.FontWeight.BOLD))

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Eliminar todas las solicitudes"),
            content=ft.Column(detalle, spacing=8, tight=True),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Eliminar todas", icon=ft.Icons.DELETE_SWEEP,
                                on_click=confirmar),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def _recibir_solicitud(self, solicitud: Solicitud,
                           partidas: list[Partida]) -> None:
        """Callback del modal de captura: persiste y repinta."""
        if not solicitud.lote_id:
            solicitud.lote_id = self._lote.id if self._lote else ""
        if not solicitud.parada:
            solicitud.parada = (
                self._lote.parada_default if self._lote else PARADA_DEFECTO)
        if solicitud.orden == 0 and solicitud.id not in {
                s.id for s in self._solicitudes}:
            solicitud.orden = len(self._solicitudes)
        try:
            db.guardar_solicitud(solicitud, partidas)
        except db.ClaveDuplicada as exc:
            self.app.avisar(str(exc), ROJO)
            return
        self._recargar_solicitudes()
        self.app.avisar("Solicitud guardada.", VERDE)

    # ------------------------------------------------------- masivas
    def _asignacion_masiva(self, _e=None) -> None:
        if not self._lote or not self._solicitudes:
            self.app.avisar("Primero carga solicitudes en el lote.", NARANJA)
            return
        self.asignacion.abrir(self._lote.id, self._seleccionados)

    def _tras_importar(self, cuantas: int) -> None:
        """Callback de los modales que meten o cambian solicitudes."""
        self._recargar_solicitudes()

    # ------------------------------------------------------------ archivos
    def _adjuntar_carpeta(self, _e=None) -> None:
        """Pregunta qué tipo de documento y asigna una carpeta entera al lote."""
        if not self._lote or not self._solicitudes:
            self.app.avisar("Primero carga solicitudes en el lote.", NARANJA)
            return

        def elegir(tipo: str, etiqueta: str):
            def _accion(_e=None) -> None:
                self.page.pop_dialog()
                self.page.run_task(self._asignar_carpeta, tipo, etiqueta)
            return _accion

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Adjuntar documentos por carpeta"),
            content=ft.Column([
                ft.Text("Se recorrerá la carpeta y cada archivo se asignará a "
                        "la solicitud cuyo beneficiario coincida por nombre "
                        "(tolera acentos, Ñ y texto de más en el archivo)."),
                ft.Text("¿Qué tipo de documento vas a asignar?", color=GRIS),
            ], spacing=8, tight=True),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Vo.Bo. de Compras", icon=ft.Icons.FACT_CHECK,
                                on_click=elegir(documentos.TIPO_VOBO,
                                                "Vo.Bo.")),
                ft.FilledButton("Carátulas bancarias",
                                icon=ft.Icons.ACCOUNT_BALANCE,
                                on_click=elegir(documentos.TIPO_CARATULA,
                                                "carátulas")),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    async def _asignar_carpeta(self, tipo: str, etiqueta: str) -> None:
        carpeta = await self.app.picker.get_directory_path(
            dialog_title=f"Elige la carpeta con las {etiqueta}")
        if not carpeta:
            return
        resultado = await asyncio.to_thread(
            documentos.asignar_por_carpeta, self._lote.id, carpeta, tipo)
        self._recargar_solicitudes()
        faltantes = resultado["sin_archivo"]
        if not faltantes:
            self.app.avisar(
                f"{resultado['asignados']} {etiqueta} asignadas a todo el lote.",
                VERDE)
            return
        # Se nombra a quién le falta: es lo único accionable del resultado.
        muestra = ", ".join(faltantes[:5])
        if len(faltantes) > 5:
            muestra += f" y {len(faltantes) - 5} más"
        self.app.avisar(
            f"{resultado['asignados']} asignadas. Sin archivo: {muestra}.",
            NARANJA, duracion=10000)

    # ------------------------------------------------------------ ejecución
    def _ejecutar(self, _e=None) -> None:
        """Confirma y lanza el lote.

        Se confirma siempre, y en producción con más énfasis: a partir del punto
        de parada `GUARDADA` cada solicitud consume un folio real que después
        hay que cancelar a mano.
        """
        if not self._lote or not self._solicitudes:
            return
        usuario, contrasena = self.app.config.credenciales()
        if not (usuario and contrasena):
            self.app.avisar(
                "Captura las credenciales de SIPP en Configuración ⚙ antes de "
                "ejecutar.", NARANJA)
            return
        pendientes = [s for s in self._solicitudes
                      if s.estado not in ("GUARDADA", "ENVIADA_AUTORIZAR",
                                          "OMITIDA")]
        if not pendientes:
            self.app.avisar("No hay solicitudes pendientes en este lote.",
                            NARANJA)
            return
        sin_validar = [s for s in pendientes if s.estado == "PENDIENTE"]
        ambiente = ambiente_actual()
        paradas = {ETIQUETA_PARADA.get(s.parada, s.parada) for s in pendientes}

        def arrancar(_e=None) -> None:
            self.page.pop_dialog()
            self.page.run_task(self._correr_lote, usuario, contrasena,
                               pendientes)

        detalle = [
            ft.Text(f"Se procesarán {len(pendientes)} solicitud(es) por "
                    f"{fmt_importe(sum(s.importe_total for s in pendientes))}."),
            ft.Text(f"Punto de parada: {', '.join(sorted(paradas))}."),
        ]
        if sin_validar:
            detalle.append(ft.Text(
                f"{len(sin_validar)} no se han validado; si traen datos "
                f"incompletos, fallarán al capturarse.", color=NARANJA))
        # Sin carátula, el alta del beneficiario queda a medias en SIPP. Se
        # avisa ANTES de arrancar: descubrirlo a la mitad del lote obliga a
        # limpiar registros a mano.
        sin_caratula = [s for s in pendientes if documentos.falta_caratula(s)]
        if sin_caratula:
            nombres = ", ".join(s.beneficiario_nombre for s in sin_caratula[:3])
            if len(sin_caratula) > 3:
                nombres += f" y {len(sin_caratula) - 3} más"
            detalle.append(ft.Text(
                f"{len(sin_caratula)} beneficiario(s) nuevo(s) sin carátula "
                f"bancaria ({nombres}). Esas solicitudes se marcarán para "
                f"revisión en vez de capturarse.", color=ROJO))
        if ambiente == "PRODUCCION":
            detalle.append(ft.Text(
                "Estás en PRODUCCIÓN: cada solicitud guardada consume un folio "
                "real.", color=ROJO, weight=ft.FontWeight.BOLD))
        else:
            detalle.append(ft.Text(f"Ambiente: {ambiente} ({AMBIENTES[ambiente]})",
                                   color=GRIS, size=12))

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Ejecutar el lote"),
            content=ft.Column(detalle, spacing=8, tight=True),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Ejecutar", icon=ft.Icons.PLAY_ARROW,
                                on_click=arrancar),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    async def _correr_lote(self, usuario: str, contrasena: str,
                           pendientes: list[Solicitud]) -> None:
        """Corre el motor en un hilo y refleja su avance en un diálogo."""
        from core import rpa_sipp

        self._detener = False
        total = len(pendientes)
        texto = ft.Text(f"Preparando… (0 de {total})")
        barra = ft.ProgressBar(value=0, width=460)
        detalle = ft.Text("", size=12, color=GRIS)

        def cancelar(_e=None) -> None:
            # Detener corta la solicitud EN CURSO, no espera a que termine: se
            # abandona antes de guardar, así que en SIPP no queda nada de ella
            # y el último registro completo es el anterior.
            self._detener = True
            texto.value = "Deteniendo… la solicitud en curso no se capturará."
            self.fila_pausa.visible = False
            # Si estaba esperando la revisión, se le despierta para que el
            # motor lea la orden en vez de quedarse dormido.
            pausa.set()
            dialogo.update()

        # --- Sincronización con el hilo del motor.
        # El motor duerme en estos eventos mientras el usuario decide; nunca se
        # toca el navegador desde aquí (la API síncrona de Playwright solo
        # admite el hilo que lo creó, ver `rpa_sipp.procesar_lote`).
        pausa = threading.Event()
        cierre = threading.Event()
        decision: dict[str, str] = {}

        def decidir(que: str):
            def _accion(_e=None) -> None:
                decision["valor"] = que
                self.fila_pausa.visible = False
                texto.value = "Continuando…"
                try:
                    dialogo.update()
                except Exception:  # noqa: BLE001
                    pass
                pausa.set()
            return _accion

        self.fila_pausa = ft.Row(
            [boton_secundario("Aprobar y continuar", ft.Icons.PLAY_ARROW,
                              on_click=decidir(rpa_sipp.CONTINUAR),
                              tooltip="El robot la guarda, adjunta el Vo.Bo. "
                                      "como documento de respaldo, la manda a "
                                      "autorizar y sigue con la siguiente"),
             boton_secundario("Saltar esta", ft.Icons.SKIP_NEXT,
                              on_click=decidir(rpa_sipp.SALTAR),
                              tooltip="No se guarda ni se autoriza: queda "
                                      "omitida y no se reintenta en este lote"),
             boton_secundario("Aprobar el resto sin preguntar",
                              ft.Icons.FAST_FORWARD,
                              on_click=decidir(rpa_sipp.NO_PAUSAR),
                              tooltip="Termina el lote guardando y autorizando "
                                      "sin detenerse en cada formulario")],
            spacing=8, wrap=True, visible=False)

        self.btn_cerrar_navegador = boton_primario(
            "Cerrar el navegador", ft.Icons.CLOSE, lambda _e: cierre.set())
        self.btn_cerrar_navegador.visible = False

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Icon(ft.Icons.SMART_TOY, color=ft.Colors.PRIMARY),
                          ft.Text("Ejecutando el lote")], spacing=10),
            content=ft.Column([texto, barra, detalle, self.fila_pausa],
                              spacing=12, tight=True),
            actions=[ft.TextButton("Detener", icon=ft.Icons.STOP,
                                   on_click=cancelar),
                     self.btn_cerrar_navegador],
            actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dialogo)
        self.page.update()

        def en_pausa(datos: dict) -> str:
            """Bloquea el hilo del motor hasta que se decida en pantalla."""
            def mostrar() -> None:
                nombre = getattr(datos.get("solicitud"), "beneficiario_nombre",
                                 "")
                texto.value = (
                    f"Revisa el formulario de {nombre} en el navegador "
                    f"({datos.get('i', 0)} de {datos.get('total', 0)})")
                detalle.value = ("Quedó lleno y SIN guardar. Al aprobar, el "
                                 "robot lo guarda, adjunta el Vo.Bo. y lo "
                                 "manda a autorizar.")
                self.fila_pausa.visible = True
                try:
                    dialogo.update()
                except Exception:  # noqa: BLE001
                    pass
            decision.clear()
            pausa.clear()
            bucle.call_soon_threadsafe(mostrar)
            # Sin timeout: la revisión la marca la persona. La salida de
            # emergencia es «Detener», que el motor consulta al reanudar.
            pausa.wait()
            if self._detener:
                return rpa_sipp.DETENER
            return decision.get("valor", rpa_sipp.CONTINUAR)

        def esperar_cierre() -> None:
            """Deja el navegador abierto hasta que se pida cerrarlo."""
            def mostrar() -> None:
                texto.value = "Lote terminado. El navegador sigue abierto."
                detalle.value = ("Revisa lo capturado en SIPP; ciérralo desde "
                                 "aquí cuando termines.")
                barra.value = 1
                self.fila_pausa.visible = False
                self.btn_cerrar_navegador.visible = True
                try:
                    dialogo.update()
                except Exception:  # noqa: BLE001
                    pass
            cierre.clear()
            bucle.call_soon_threadsafe(mostrar)
            cierre.wait()

        # Si la app se cierra con el navegador abierto, se despiertan los dos
        # eventos: el hilo sale de su espera, cierra Playwright y termina. Sin
        # esto quedaría un Chromium huérfano comiendo memoria.
        self._liberar_rpa = lambda: (self._marcar_detener(), pausa.set(),
                                     cierre.set())

        # El motor corre en otro hilo; tocar controles desde ahí es una carrera.
        # Se agenda cada actualización en el bucle de la interfaz.
        bucle = asyncio.get_running_loop()

        def progreso(datos: dict) -> None:
            def aplicar() -> None:
                i = datos.get("i", 0)
                nombre = datos.get("nombre", "")
                estado = datos.get("estado", "")
                if estado == "procesando":
                    texto.value = f"Capturando {i} de {total}: {nombre}"
                    barra.value = (i - 1) / total if total else None
                else:
                    barra.value = i / total if total else None
                    detalle.value = f"{nombre}: {datos.get('mensaje', estado)}"
                try:
                    dialogo.update()
                except Exception:  # noqa: BLE001 — el diálogo ya se cerró
                    pass
            bucle.call_soon_threadsafe(aplicar)

        # La pausa solo se ofrece si alguna solicitud para en «Llenar y
        # esperar»: en un lote que se guarda entero no hay formulario que
        # revisar y el motor no debe detenerse a preguntar nada.
        hay_que_revisar = any(
            (s.parada or PARADA_DEFECTO) == "LLENADA" for s in pendientes)
        try:
            resumen = await asyncio.to_thread(
                rpa_sipp.procesar_lote, self._lote.id, usuario, contrasena,
                url_login=AMBIENTES[ambiente_actual()],
                visible=navegador_visible(),
                on_progreso=progreso, detener=lambda: self._detener,
                en_pausa=en_pausa if hay_que_revisar else None,
                esperar_cierre=esperar_cierre if navegador_visible() else None)
        except Exception as exc:  # noqa: BLE001 — se reporta, la app sigue viva
            self._liberar_rpa = None
            self.page.pop_dialog()
            self._recargar_solicitudes()
            self.app.avisar(f"El lote se interrumpió: {exc}", ROJO,
                            duracion=10000)
            return
        self._liberar_rpa = None

        self.page.pop_dialog()
        self._recargar_solicitudes()
        partes = [f"{resumen['ok']} capturada(s)"]
        if resumen["revisar"]:
            partes.append(f"{resumen['revisar']} para revisar")
        if resumen["error"]:
            partes.append(f"{resumen['error']} con error")
        if resumen["cancelado"]:
            partes.append("detenido por ti")
        color = (ROJO if resumen["error"] else
                 NARANJA if resumen["revisar"] or resumen["cancelado"] else VERDE)
        self.app.avisar("Lote terminado: " + " · ".join(partes), color,
                        accion="Ver bitácora",
                        on_accion=lambda _e: self.app.ir_a_bitacora(),
                        duracion=10000)
