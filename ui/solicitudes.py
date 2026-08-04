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

import flet as ft

from core import asignacion, db, documentos
from core.catalogos import (AMBIENTES, ETIQUETA_ESTADO, ETIQUETA_PARADA,
                            PARADA_DEFECTO, PARADAS)
from core.db import CONCEPTO, INSUMO, Lote, Partida, Solicitud
from ui.alta_caratulas import AltaDesdeCaratulas
from ui.asignacion_masiva import AsignacionMasiva
from ui.captura_solicitud import CapturaSolicitud
from ui.carga_masiva import CargaMasiva
from ui.comun import GRIS, NARANJA, ROJO, VERDE, color_estado, color_fila, fmt_importe
from ui.componentes import (boton_herramienta, boton_primario, boton_secundario,
                            campo_opciones, tarjeta_seccion)
from ui.configuracion import ambiente_actual, navegador_visible
from ui.tabla_responsiva import (DER, IZQ, Cabecera, ColumnaTabla, FilaDatos,
                                 SegmentoCabecera, TablaResponsiva)

# Porcentajes de ancho por columna; suman 100 (ver ui/tabla_responsiva.py).
_COLUMNAS = [
    ColumnaTabla("", 4),                       # selección
    ColumnaTabla("", 4),                       # desplegar detalle
    ColumnaTabla("Estado", 10, IZQ),
    ColumnaTabla("Beneficiario", 20, IZQ),
    ColumnaTabla("Tipo", 9, IZQ),
    ColumnaTabla("Empresa", 13, IZQ),
    ColumnaTabla("Fecha", 8),
    ColumnaTabla("Importe", 11, DER),
    ColumnaTabla("Punto de parada", 12, IZQ),
    ColumnaTabla("Folio SIPP", 9),
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
        self.btn_deshacer = boton_herramienta(
            "Deshacer", ft.Icons.UNDO, on_click=self._deshacer)
        self.btn_editar = boton_herramienta(
            "Editar", ft.Icons.EDIT, on_click=self._editar)
        self.btn_duplicar = boton_herramienta(
            "Duplicar", ft.Icons.CONTENT_COPY, on_click=self._duplicar)
        self.btn_omitir = boton_herramienta(
            "Omitir", ft.Icons.BLOCK, on_click=self._omitir)
        self.btn_eliminar = boton_herramienta(
            "Eliminar", ft.Icons.DELETE_OUTLINE, on_click=self._eliminar,
            destructivo=True)
        self.btn_ejecutar = boton_primario(
            "Ejecutar lote", ft.Icons.PLAY_ARROW, on_click=self._ejecutar)

        # Mismo patrón anidado que `barra_lote` (ver el comentario de arriba).
        barra_acciones = ft.Row(
            [ft.Row([boton_secundario("Nueva solicitud", ft.Icons.ADD,
                                      on_click=self._nueva_solicitud),
                     boton_secundario("Alta desde carátulas",
                                      ft.Icons.ACCOUNT_BALANCE,
                                      on_click=self._alta_caratulas,
                                      tooltip="Una carátula, una solicitud: "
                                              "toma el beneficiario del nombre "
                                              "del archivo y completa con Excel"),
                     boton_secundario("Carga masiva", ft.Icons.UPLOAD_FILE,
                                      on_click=self._carga_masiva,
                                      tooltip="Dar de alta muchas solicitudes "
                                              "desde un Excel"),
                     boton_secundario("Adjuntar por carpeta",
                                      ft.Icons.DRIVE_FOLDER_UPLOAD,
                                      on_click=self._adjuntar_carpeta,
                                      tooltip="Asignar carátulas o Vo.Bo. a "
                                              "todo el lote, emparejando por "
                                              "el nombre del beneficiario"),
                     self.btn_asignar, self.btn_deshacer,
                     self.btn_editar, self.btn_duplicar, self.btn_omitir,
                     self.btn_eliminar],
                    spacing=8, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self.txt_resumen,
             self.btn_ejecutar],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.tabla = TablaResponsiva(self.page, _COLUMNAS)
        self.vacio = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.RECEIPT_LONG, size=40, color=GRIS),
                 ft.Text("Este lote todavía no tiene solicitudes.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 ft.Text("Captúralas a mano con «Nueva solicitud», o cárgalas "
                         "desde Documentos cuando esté disponible la ingesta.",
                         color=GRIS, text_align=ft.TextAlign.CENTER)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        self.contenido = ft.Column(
            [tarjeta_seccion(ft.Column([barra_lote, ft.Divider(),
                                        barra_acciones], spacing=12,
                                       tight=True)),
             ft.Container(
                 content=ft.Column([self.tabla.control, self.vacio],
                                   scroll=ft.ScrollMode.AUTO, expand=True,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True)],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola con `on_size_change`; no hay nada que
        reacomodar aquí. Presente por el contrato de pantalla del shell."""

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

    # ------------------------------------------------------------ pintado
    def _pintar(self) -> None:
        filas: list = []
        for s in self._solicitudes:
            filas.append(self._fila_solicitud(s))
            if s.id in self._expandidos:
                filas.append(self._fila_detalle(s))
        self.tabla.set_contenido(filas)
        hay = bool(self._solicitudes)
        self.vacio.visible = not hay
        self.tabla.control.visible = hay
        self._actualizar_resumen()
        self._actualizar_acciones()
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montado en la página
            pass

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
             fmt_importe(s.importe_total), parada, s.folio_sipp or "—"],
            bgcolor=color_fila(s.estado))

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
        if s.error_msg:
            detalle = ft.Column(
                [detalle,
                 ft.Text(s.error_msg, size=11, color=ROJO)],
                spacing=6, tight=True)

        # Alto estimado por el bloque más largo, para que la banda no recorte.
        renglones = max(len(conceptos), len(insumos), 2) + 1
        return Cabecera(
            [SegmentoCabecera(2, ft.Container()),
             SegmentoCabecera(len(_COLUMNAS) - 2, detalle,
                              padding=ft.Padding.symmetric(vertical=8))],
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            alto=max(64, 18 * renglones + 24))

    def _texto_documentos(self, s: Solicitud) -> ft.Control:
        """Qué archivos tiene la solicitud, y si le falta alguno obligatorio."""
        archivos = documentos.de_solicitud(s.id)
        partes = []
        if archivos.get("caratula"):
            partes.append("carátula ✓")
        elif documentos.falta_caratula(s):
            return ft.Text("Documentos: FALTA LA CARÁTULA (obligatoria para "
                           "dar de alta la cuenta)", size=11, color=ROJO,
                           weight=ft.FontWeight.BOLD)
        if archivos.get("vobo"):
            partes.append("Vo.Bo. ✓")
        return ft.Text(f"Documentos: {' · '.join(partes) if partes else '—'}",
                       size=11, color=GRIS)

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
        uno = len(self._seleccionados) == 1
        alguno = bool(self._seleccionados)
        self.btn_editar.disabled = not uno
        self.btn_duplicar.disabled = not uno
        self.btn_omitir.disabled = not alguno
        self.btn_eliminar.disabled = not alguno
        self.btn_asignar.disabled = not self._solicitudes
        self.btn_ejecutar.disabled = not self._solicitudes
        # «Deshacer» solo tiene sentido si hay algo que deshacer, y decirlo en
        # el tooltip evita que alguien lo pulse esperando otra cosa.
        snap = db.hay_snapshot(self._lote.id) if self._lote else None
        self.btn_deshacer.disabled = snap is None
        self.btn_deshacer.tooltip = (
            f"Revertir: {snap['motivo']} ({snap['creado_en'][:16].replace('T', ' ')})"
            if snap else "No hay ninguna operación masiva que deshacer")

    # ------------------------------------------------------------ eventos
    def _alternar_seleccion(self, sid: str, e) -> None:
        if e.control.value:
            self._seleccionados.add(sid)
        else:
            self._seleccionados.discard(sid)
        self._actualizar_acciones()
        self.contenido.update()

    def _alternar_detalle(self, sid: str) -> None:
        if sid in self._expandidos:
            self._expandidos.discard(sid)
        else:
            self._expandidos.add(sid)
        self._pintar()

    def _seleccionada(self) -> Solicitud | None:
        if len(self._seleccionados) != 1:
            return None
        sid = next(iter(self._seleccionados))
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
        if not self._lote:
            self.app.avisar("Primero crea un lote.", NARANJA)
            return
        self.captura.abrir(self._lote.id)

    def _editar(self, _e=None) -> None:
        s = self._seleccionada()
        if not s:
            return
        self.captura.abrir(self._lote.id, s, self._partidas.get(s.id, []))

    def _duplicar(self, _e=None) -> None:
        """Abre el editor con una copia de la solicitud seleccionada.

        No la guarda de inmediato a propósito: una copia idéntica produce la
        MISMA clave de idempotencia y sería rechazada. Duplicar sirve para
        partir de una solicitud parecida y cambiarle lo que toque —el
        beneficiario, el importe—, y eso solo puede hacerlo el usuario.
        """
        s = self._seleccionada()
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

    def _omitir(self, _e=None) -> None:
        for sid in list(self._seleccionados):
            db.actualizar_estado(sid, "OMITIDA")
        self._recargar_solicitudes()
        self.app.avisar("Solicitudes marcadas como omitidas.", NARANJA)

    def _eliminar(self, _e=None) -> None:
        cuantas = len(self._seleccionados)

        def confirmar(_e=None) -> None:
            self.page.pop_dialog()
            for sid in list(self._seleccionados):
                db.borrar_solicitud(sid)
            self._seleccionados.clear()
            self._recargar_solicitudes()
            self.app.avisar(f"{cuantas} solicitud(es) eliminadas.", VERDE)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Eliminar solicitudes"),
            content=ft.Text(
                f"Se eliminarán {cuantas} solicitud(es) con su desglose y su "
                "bitácora. Esto no se puede deshacer."),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Eliminar", icon=ft.Icons.DELETE_OUTLINE,
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

    # ------------------------------------------------------------ ejecución
    def _carga_masiva(self, _e=None) -> None:
        if not self._lote:
            self.app.avisar("Primero crea un lote.", NARANJA)
            return
        self.carga.abrir(self._lote.id)

    def _alta_caratulas(self, _e=None) -> None:
        if not self._lote:
            self.app.avisar("Primero crea un lote.", NARANJA)
            return
        self.alta_caratulas.abrir(self._lote.id)

    # ------------------------------------------------------- masivas
    def _asignacion_masiva(self, _e=None) -> None:
        if not self._lote or not self._solicitudes:
            self.app.avisar("Primero carga solicitudes en el lote.", NARANJA)
            return
        self.asignacion.abrir(self._lote.id, self._seleccionados)

    def _deshacer(self, _e=None) -> None:
        """Revierte la última operación masiva del lote."""
        if not self._lote:
            return
        snap = db.hay_snapshot(self._lote.id)
        if not snap:
            self.app.avisar("No hay ninguna operación masiva que deshacer.",
                            NARANJA)
            return

        def confirmar(_e=None) -> None:
            self.page.pop_dialog()
            if asignacion.deshacer(self._lote.id):
                self._recargar_solicitudes()
                self.app.avisar("Lote restaurado al estado anterior.", VERDE)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Deshacer la última operación"),
            content=ft.Text(
                f"Se revertirá «{snap['motivo']}» y el lote volverá a como "
                f"estaba el {snap['creado_en'][:16].replace('T', ' ')}.\n\n"
                "Se pierde lo que hayas cambiado a mano después."),
            actions=[
                ft.TextButton("Cancelar",
                              on_click=lambda _e: self.page.pop_dialog()),
                ft.FilledButton("Deshacer", icon=ft.Icons.UNDO,
                                on_click=confirmar),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def _tras_importar(self, cuantas: int) -> None:
        """Callback del modal de carga masiva: refresca la tabla."""
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
            self._detener = True
            texto.value = "Deteniendo al terminar la solicitud en curso…"
            dialogo.update()

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Icon(ft.Icons.SMART_TOY, color=ft.Colors.PRIMARY),
                          ft.Text("Ejecutando el lote")], spacing=10),
            content=ft.Column([texto, barra, detalle], spacing=12, tight=True),
            actions=[ft.TextButton("Detener", icon=ft.Icons.STOP,
                                   on_click=cancelar)],
            actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dialogo)
        self.page.update()

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

        try:
            resumen = await asyncio.to_thread(
                rpa_sipp.procesar_lote, self._lote.id, usuario, contrasena,
                url_login=AMBIENTES[ambiente_actual()],
                visible=navegador_visible(),
                on_progreso=progreso, detener=lambda: self._detener)
        except Exception as exc:  # noqa: BLE001 — se reporta, la app sigue viva
            self.page.pop_dialog()
            self._recargar_solicitudes()
            self.app.avisar(f"El lote se interrumpió: {exc}", ROJO,
                            duracion=10000)
            return

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
