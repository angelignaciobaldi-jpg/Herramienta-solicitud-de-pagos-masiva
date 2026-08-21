"""Carga masiva de solicitudes desde un archivo de Excel o CSV.

El flujo de la pantalla es deliberadamente en tres pasos —descargar la plantilla,
elegir el archivo, revisar la vista previa— y **nada se guarda hasta el último**.
Importar cien solicitudes de golpe solo sirve si antes se ve, de golpe, qué está
mal en cuáles: corregir cien errores de uno en uno sería más lento que capturar a
mano.

Las filas con errores no se importan, pero tampoco bloquean a las demás: se
importa lo bueno y el archivo se corrige para las que faltan.
"""

from __future__ import annotations

import asyncio
import os

import flet as ft

from core import db, plantilla_excel
from core.adaptadores import excel as adaptador
from core.plantilla_excel import CAMPOS
from ui.comun import GRIS, NARANJA, ROJO, VERDE, fmt_importe
from ui.componentes import (Modal, boton_primario, boton_secundario,
                            campo_opciones, tarjeta_seccion)
from ui.tabla_responsiva import (DER, IZQ, ColumnaTabla, FilaDatos,
                                 TablaResponsiva)

_COLUMNAS = [
    ColumnaTabla("Fila", 6),
    ColumnaTabla("", 6),                       # semáforo
    ColumnaTabla("Beneficiario", 24, IZQ),
    ColumnaTabla("Empresa", 16, IZQ),
    ColumnaTabla("Fecha", 10),
    ColumnaTabla("Importe", 12, DER),
    ColumnaTabla("Detalle", 26, IZQ),
]

_ETIQUETAS = {c.clave: c.etiqueta for c in CAMPOS}


class CargaMasiva:
    """Modal de importación. `al_importar(filas)` recibe las solicitudes buenas."""

    def __init__(self, app, al_importar) -> None:
        self.app = app
        self.page = app.page
        self._al_importar = al_importar
        self._ruta = ""
        self._importacion: adaptador.Importacion | None = None
        self._mapeo: dict[str, ft.Dropdown] = {}
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        # Paso 1 — plantilla.
        paso1 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.DOWNLOAD, color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("1. Descarga la plantilla",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Trae una hoja por llenar, los catálogos de SIPP como "
                    "listas desplegables y las instrucciones. Si ya tienes un "
                    "archivo propio, también sirve —Excel o CSV—: se reconocen "
                    "los encabezados más comunes.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            ft.Row([boton_secundario("Descargar plantilla de Excel",
                                     ft.Icons.TABLE_VIEW,
                                     on_click=self._descargar_plantilla)]),
        ], spacing=10, tight=True))

        # Paso 2 — archivo.
        self.txt_archivo = ft.Text("Ningún archivo elegido.", color=GRIS)
        paso2 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.UPLOAD_FILE,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("2. Elige el archivo lleno",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Row([boton_secundario("Elegir archivo…", ft.Icons.FOLDER_OPEN,
                                     on_click=self._elegir_archivo),
                    self.txt_archivo],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, tight=True))

        # Mapeo manual: solo aparece si la detección automática no bastó.
        self.col_mapeo = ft.Column(spacing=10, tight=True, visible=False,
                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        # Paso 3 — vista previa.
        self.txt_resumen = ft.Text("", theme_style=ft.TextThemeStyle.BODY_LARGE)
        self.tabla = TablaResponsiva(self.page, _COLUMNAS, ancho_inicial=880,
                                     alto_cuerpo=280)
        self.chk_solo_malas = ft.Checkbox(
            label="Ver solo las filas con problemas", value=False,
            on_change=lambda _e: self._pintar_previa())
        self.previa = ft.Column(
            [ft.Row([ft.Icon(ft.Icons.FACT_CHECK,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Text("3. Revisa antes de importar",
                             theme_style=ft.TextThemeStyle.LABEL_LARGE,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Container(expand=True),
                     self.chk_solo_malas],
                    spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self.txt_resumen,
             self.tabla.control],
            spacing=10, tight=True, visible=False,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.btn_importar = boton_primario(
            "Importar", ft.Icons.PLAYLIST_ADD, self._importar)
        self.btn_importar.disabled = True

        self.modal = Modal(
            self.page, "Carga masiva de solicitudes",
            subtitulo="desde Excel", ancho=940, alto_cuerpo=520,
            acciones=[
                boton_secundario("Cerrar",
                                 on_click=lambda _e: self.modal.cerrar()),
                self.btn_importar,
            ])
        self.modal.cuerpo.controls = [paso1, paso2, self.col_mapeo, self.previa]

    # -------------------------------------------------------- apertura
    def abrir(self, lote_id: str) -> None:
        self._lote_id = lote_id
        self._ruta = ""
        self._importacion = None
        self.txt_archivo.value = "Ningún archivo elegido."
        self.txt_archivo.color = GRIS
        self.col_mapeo.visible = False
        self.previa.visible = False
        self.btn_importar.disabled = True
        self.btn_importar.text = "Importar"
        self.modal.abrir()

    # ------------------------------------------------------- plantilla
    async def _descargar_plantilla(self, _e=None) -> None:
        destino = await self.app.picker.save_file(
            dialog_title="Guardar la plantilla",
            file_name=plantilla_excel.NOMBRE_ARCHIVO,
            allowed_extensions=["xlsx"])
        if not destino:
            return
        ruta = destino if destino.lower().endswith(".xlsx") else destino + ".xlsx"
        try:
            await asyncio.to_thread(plantilla_excel.generar, ruta)
        except Exception as exc:  # noqa: BLE001 — se reporta y la app sigue
            self.app.avisar(f"No se pudo generar la plantilla: {exc}", ROJO)
            return
        self.app.avisar("Plantilla guardada.", VERDE, accion="Abrir",
                        on_accion=lambda _e: self.app.abrir_en_sistema(ruta))

    # --------------------------------------------------------- archivo
    async def _elegir_archivo(self, _e=None) -> None:
        archivos = await self.app.picker.pick_files(
            dialog_title="Elige el archivo con las solicitudes",
            allowed_extensions=["xlsx", "xlsm", "csv"], allow_multiple=False)
        if not archivos:
            return
        self._ruta = archivos[0].path
        self.txt_archivo.value = f"Analizando «{archivos[0].name}»…"
        self.txt_archivo.color = GRIS
        self.modal.refrescar()
        await self._analizar()

    async def _analizar(self, columnas: dict[str, int] | None = None) -> None:
        """Lee y valida el archivo fuera del hilo de la interfaz."""
        try:
            importacion = await asyncio.to_thread(
                adaptador.leer, self._ruta, self._lote_id, columnas=columnas)
        except Exception as exc:  # noqa: BLE001
            importacion = adaptador.Importacion(error=str(exc))
        self._importacion = importacion
        nombre = os.path.basename(self._ruta)

        if importacion.error and not importacion.filas:
            self.txt_archivo.value = f"«{nombre}» — {importacion.error}"
            self.txt_archivo.color = ROJO
            self.previa.visible = False
            self.btn_importar.disabled = True
            # Si el problema es que faltan columnas, se ofrece mapearlas a mano.
            self._construir_mapeo(importacion.deteccion)
            self.modal.refrescar()
            return

        self.txt_archivo.value = f"«{nombre}»"
        self.txt_archivo.color = GRIS
        self.col_mapeo.visible = False
        self._pintar_previa()
        self.modal.refrescar()

    def _construir_mapeo(self, deteccion: adaptador.Deteccion) -> None:
        """Desplegables para asignar a mano las columnas que no se reconocieron.

        La detección automática cubre la plantilla y los encabezados que ya usa
        el área, pero no puede adivinar un archivo cualquiera. En vez de exigir
        que lo renombren, se deja corregir aquí.
        """
        self._mapeo = {}
        if not deteccion.faltantes or not deteccion.encabezados:
            self.col_mapeo.visible = False
            return
        opciones = [f"{i + 1}. {h}" for i, h in enumerate(deteccion.encabezados)
                    if str(h).strip()]
        filas = []
        for clave in deteccion.faltantes:
            bloque, dd = campo_opciones(
                _ETIQUETAS.get(clave, clave), opciones, width=380)
            self._mapeo[clave] = dd
            filas.append(bloque)
        self.col_mapeo.controls = [tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.RULE, color=ft.Colors.ERROR),
                    ft.Text("No reconocí algunas columnas",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.ERROR)], spacing=8, tight=True),
            ft.Text("Indica qué columna del archivo corresponde a cada campo.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            *filas,
            ft.Row([boton_secundario("Aplicar el mapeo", ft.Icons.CHECK,
                                     on_click=self._aplicar_mapeo)]),
        ], spacing=10, tight=True))]
        self.col_mapeo.visible = True

    async def _aplicar_mapeo(self, _e=None) -> None:
        base = dict(self._importacion.deteccion.columnas) if self._importacion else {}
        for clave, dd in self._mapeo.items():
            if dd.value:
                # El desplegable muestra «3. Nombre»: el número es la columna.
                base[clave] = int(str(dd.value).split(".", 1)[0]) - 1
        await self._analizar(columnas=base)

    # ---------------------------------------------------- vista previa
    def _pintar_previa(self) -> None:
        imp = self._importacion
        if not imp or not imp.filas:
            self.previa.visible = False
            return
        duplicadas = adaptador.detectar_duplicados(imp.filas)
        mostrar = imp.filas
        if self.chk_solo_malas.value:
            mostrar = [f for f in imp.filas
                       if not f.valida or f.numero in duplicadas]

        filas_tabla = []
        for f in mostrar:
            duplicada = f.numero in duplicadas
            if duplicada:
                icono, color, detalle = (
                    ft.Icons.CONTENT_COPY, NARANJA,
                    "Repetida: ya hay otra fila idéntica en el archivo.")
            elif f.valida:
                avisos = [h.mensaje for h in f.hallazgos]
                icono, color = ft.Icons.CHECK_CIRCLE, VERDE
                detalle = avisos[0] if avisos else "Lista para importar."
            else:
                icono, color = ft.Icons.ERROR, ROJO
                errores = [h.mensaje for h in f.hallazgos if h.es_error]
                detalle = (f"{len(errores)} problema(s): {errores[0]}"
                           if errores else "Con problemas.")
            filas_tabla.append(FilaDatos([
                str(f.numero),
                ft.Icon(icono, size=18, color=color,
                        tooltip=f.resumen_problemas or None),
                f.solicitud.beneficiario_nombre or "—",
                f.solicitud.empresa or "—",
                f.solicitud.fecha_pago or "—",
                fmt_importe(f.solicitud.importe_total),
                detalle,
            ]))
        self.tabla.set_contenido(filas_tabla)

        buenas = [f for f in imp.validas if f.numero not in duplicadas]
        malas = len(imp.invalidas)
        self.txt_resumen.value = (
            f"{len(imp.filas)} fila(s) leídas · {len(buenas)} lista(s) para "
            f"importar · {malas} con errores · {len(duplicadas)} repetida(s)")
        self.txt_resumen.color = VERDE if buenas else ROJO
        self.btn_importar.text = (
            f"Importar {len(buenas)}" if buenas else "Importar")
        self.btn_importar.disabled = not buenas
        self._buenas = buenas
        self.previa.visible = True

    # ------------------------------------------------------- importar
    def _importar(self, _e=None) -> None:
        buenas = getattr(self, "_buenas", [])
        if not buenas:
            return
        guardadas, rechazadas = 0, []
        for f in buenas:
            f.solicitud.lote_id = self._lote_id
            try:
                db.guardar_solicitud(f.solicitud, f.partidas)
                guardadas += 1
            except db.ClaveDuplicada:
                # Ya existía en la base (de otra importación o de captura
                # manual). No es un error del archivo: se informa y sigue.
                rechazadas.append(f.numero)
            except Exception as exc:  # noqa: BLE001
                rechazadas.append(f.numero)
                db.registrar("", "importar", f"Fila {f.numero}: {exc}", "ERROR")
        self.modal.cerrar()
        if callable(self._al_importar):
            self._al_importar(guardadas)
        if rechazadas:
            self.app.avisar(
                f"{guardadas} solicitud(es) importadas. {len(rechazadas)} ya "
                f"existían y se omitieron (filas {', '.join(map(str, rechazadas))}).",
                NARANJA, duracion=8000)
        else:
            self.app.avisar(f"{guardadas} solicitud(es) importadas.", VERDE)
