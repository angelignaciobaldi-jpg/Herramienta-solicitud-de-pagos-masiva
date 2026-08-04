"""Alta de solicitudes a partir de carátulas bancarias.

Una carátula = una solicitud. Es el flujo que corresponde a cómo llega el
trabajo: el área recibe una carpeta con un archivo por persona, y esa carpeta ya
**es** el listado de a quién hay que pagarle.

Tres pasos, con el Excel como paso opcional al final:

1. Elegir las carátulas (archivos sueltos o una carpeta completa).
2. Revisar los nombres que se extrajeron del nombre de cada archivo, y
   corregir los que hagan falta —son editables en la propia tabla.
3. Opcionalmente, cargar el Excel: sus filas se emparejan por nombre y
   completan importe, fecha, concepto, CLABE y demás.

Lo que este orden garantiza y el inverso no: **ninguna solicitud puede quedarse
sin carátula**, porque no existe si no hay archivo. Como SIPP la exige para dar
de alta la cuenta bancaria, eso elimina de raíz el caso que más trabajo manual
generaba.
"""

from __future__ import annotations

import asyncio
import os

import flet as ft

from core import catalogos, db, documentos
from core.adaptadores import caratulas
from core.adaptadores import excel as adaptador_excel
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE, fmt_importe
from ui.componentes import (Modal, boton_primario, boton_secundario,
                            campo_opciones, campo_tabla_texto, tarjeta_seccion)
from ui.tabla_responsiva import (DER, IZQ, ColumnaTabla, FilaDatos,
                                 TablaResponsiva)

_COLUMNAS = [
    ColumnaTabla("", 5),                             # semáforo
    ColumnaTabla("Archivo", 24, IZQ),
    ColumnaTabla("Beneficiario detectado", 25, IZQ),
    ColumnaTabla("Empresa", 14, IZQ),
    ColumnaTabla("Importe", 11, DER),
    ColumnaTabla("Detalle", 21, IZQ),
]


class AltaDesdeCaratulas:
    """Modal de alta por carátulas. `al_importar(n)` recibe cuántas se crearon."""

    def __init__(self, app, al_importar) -> None:
        self.app = app
        self.page = app.page
        self._al_importar = al_importar
        self._borradores: list[caratulas.Borrador] = []
        self._lote_id = ""
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        # Paso 1 — carátulas.
        self.txt_archivos = ft.Text("Ninguna carátula elegida.", color=GRIS)
        paso1 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.ACCOUNT_BALANCE,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("1. Elige las carátulas",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Cada archivo se convierte en una solicitud, con su "
                    "carátula ya adjunta. El nombre del beneficiario se toma "
                    "del nombre del archivo.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            ft.Row([boton_secundario("Elegir archivos…", ft.Icons.PICTURE_AS_PDF,
                                     on_click=self._elegir_archivos),
                    boton_secundario("Elegir carpeta…", ft.Icons.FOLDER_OPEN,
                                     on_click=self._elegir_carpeta),
                    self.txt_archivos],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                   wrap=True),
        ], spacing=10, tight=True))

        # Paso 2 — valores comunes.
        bl_empresa, self.dd_empresa = campo_opciones(
            "Empresa", NOMBRES_EMPRESAS, width=280,
            on_change=self._aplicar_comunes)
        bl_sucursal, self.dd_sucursal = campo_opciones(
            "Sucursal", catalogos.SUCURSALES, width=220,
            on_change=self._aplicar_comunes)
        bl_tipo, self.dd_tipo = campo_opciones(
            "Tipo de beneficiario", catalogos.TIPOS_BENEFICIARIO, width=220,
            valor="Acreedor", on_change=self._aplicar_comunes)
        paso2 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.TUNE, color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("2. Datos comunes a todas",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Se aplican a las solicitudes que no traigan ese dato del "
                    "Excel.", theme_style=ft.TextThemeStyle.BODY_MEDIUM,
                    color=GRIS),
            ft.Row([bl_empresa, bl_sucursal, bl_tipo], spacing=16, wrap=True),
        ], spacing=10, tight=True))

        # Paso 3 — Excel.
        self.txt_excel = ft.Text("Sin Excel: tendrás que completar importe, "
                                 "fecha y concepto a mano.", color=GRIS)
        paso3 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.TABLE_VIEW,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("3. Completa con el Excel (opcional)",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Sus filas se emparejan por nombre con las carátulas y "
                    "rellenan lo que falte. El nombre del beneficiario NO se "
                    "sobrescribe: manda el de la carátula, que es la que "
                    "acredita la cuenta.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            ft.Row([boton_secundario("Elegir Excel…", ft.Icons.UPLOAD_FILE,
                                     on_click=self._elegir_excel),
                    self.txt_excel],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, tight=True))

        # Vista previa.
        self.txt_resumen = ft.Text("", theme_style=ft.TextThemeStyle.BODY_LARGE)
        self.tabla = TablaResponsiva(self.page, _COLUMNAS, ancho_inicial=900)
        self.previa = ft.Column(
            [ft.Row([ft.Icon(ft.Icons.FACT_CHECK,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Text("Revisa antes de dar de alta",
                             theme_style=ft.TextThemeStyle.LABEL_LARGE,
                             color=ft.Colors.PRIMARY_CONTAINER)],
                    spacing=8, tight=True),
             self.txt_resumen,
             ft.Container(self.tabla.control, height=250)],
            spacing=10, tight=True, visible=False,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.btn_importar = boton_primario(
            "Dar de alta", ft.Icons.PLAYLIST_ADD, self._importar)
        self.btn_importar.disabled = True

        self.modal = Modal(
            self.page, "Alta desde carátulas", subtitulo="una carátula, una solicitud",
            ancho=960, alto_cuerpo=520,
            acciones=[
                boton_secundario("Cerrar",
                                 on_click=lambda _e: self.modal.cerrar()),
                self.btn_importar,
            ])
        self.modal.cuerpo.controls = [paso1, paso2, paso3, self.previa]

    # -------------------------------------------------------- apertura
    def abrir(self, lote_id: str) -> None:
        self._lote_id = lote_id
        self._borradores = []
        self.txt_archivos.value = "Ninguna carátula elegida."
        self.txt_archivos.color = GRIS
        self.txt_excel.value = ("Sin Excel: tendrás que completar importe, "
                                "fecha y concepto a mano.")
        self.txt_excel.color = GRIS
        self.previa.visible = False
        self.btn_importar.disabled = True
        self.btn_importar.text = "Dar de alta"
        self.modal.abrir()

    # -------------------------------------------------------- carátulas
    async def _elegir_archivos(self, _e=None) -> None:
        seleccion = await self.app.picker.pick_files(
            dialog_title="Elige las carátulas bancarias", allow_multiple=True,
            allowed_extensions=["pdf", "jpg", "jpeg", "png"])
        if not seleccion:
            return
        self._cargar([a.path for a in seleccion])

    async def _elegir_carpeta(self, _e=None) -> None:
        carpeta = await self.app.picker.get_directory_path(
            dialog_title="Elige la carpeta con las carátulas")
        if not carpeta:
            return
        self._cargar([carpeta])

    def _cargar(self, rutas: list[str]) -> None:
        self._borradores = caratulas.crear_borradores(
            rutas, self._lote_id,
            empresa=self.dd_empresa.value or "",
            sucursal=self.dd_sucursal.value or "",
            tipo_beneficiario=self.dd_tipo.value or "Acreedor")
        if not self._borradores:
            self.txt_archivos.value = ("No se encontraron archivos válidos "
                                       "(PDF, JPG o PNG).")
            self.txt_archivos.color = NARANJA
            self.previa.visible = False
            self.btn_importar.disabled = True
            self.modal.refrescar()
            return
        sin_nombre = sum(1 for b in self._borradores if not b.nombre_detectado)
        self.txt_archivos.value = f"{len(self._borradores)} carátula(s)"
        self.txt_archivos.color = GRIS
        if sin_nombre:
            self.txt_archivos.value += (
                f" · {sin_nombre} sin nombre reconocible: escríbelo en la tabla")
            self.txt_archivos.color = NARANJA
        self._pintar()
        self.modal.refrescar()

    def _aplicar_comunes(self, _e=None) -> None:
        """Vuelca los valores comunes sobre los borradores que no los tengan."""
        for b in self._borradores:
            if self.dd_empresa.value and not b.solicitud.empresa:
                b.solicitud.empresa = self.dd_empresa.value
            if self.dd_sucursal.value and not b.solicitud.sucursal:
                b.solicitud.sucursal = self.dd_sucursal.value
            if self.dd_tipo.value:
                b.solicitud.tipo_beneficiario = self.dd_tipo.value
        if self._borradores:
            self._pintar()
        self.modal.refrescar()

    # ------------------------------------------------------------ Excel
    async def _elegir_excel(self, _e=None) -> None:
        if not self._borradores:
            self.app.avisar("Primero elige las carátulas.", NARANJA)
            return
        seleccion = await self.app.picker.pick_files(
            dialog_title="Elige el Excel con los datos",
            allowed_extensions=["xlsx", "xlsm"], allow_multiple=False)
        if not seleccion:
            return
        ruta = seleccion[0].path
        self.txt_excel.value = f"Leyendo «{os.path.basename(ruta)}»…"
        self.modal.refrescar()

        importacion = await asyncio.to_thread(
            adaptador_excel.leer, ruta, self._lote_id)
        if importacion.error and not importacion.filas:
            self.txt_excel.value = importacion.error
            self.txt_excel.color = ROJO
            self.modal.refrescar()
            return

        resumen = caratulas.completar_con_excel(self._borradores,
                                                importacion.filas)
        partes = [f"{resumen['emparejados']} de {len(self._borradores)} "
                  f"carátulas completadas"]
        if resumen["sin_excel"]:
            partes.append(f"{len(resumen['sin_excel'])} sin fila en el Excel")
        if resumen["sin_caratula"]:
            # Esto suele significar que falta una carátula, no que sobre una
            # fila: por eso se nombra a quién.
            muestra = ", ".join(resumen["sin_caratula"][:3])
            partes.append(f"{len(resumen['sin_caratula'])} fila(s) sin carátula "
                          f"({muestra})")
        self.txt_excel.value = " · ".join(partes)
        self.txt_excel.color = VERDE if not resumen["sin_excel"] else NARANJA
        self._pintar()
        self.modal.refrescar()

    # ---------------------------------------------------- vista previa
    def _pintar(self) -> None:
        filas = []
        for b in self._borradores:
            hallazgos = b.hallazgos
            errores = [h.mensaje for h in hallazgos if h.es_error]
            if not b.nombre_detectado:
                icono, color = ft.Icons.HELP_OUTLINE, ROJO
                detalle = "No se reconoció el nombre: escríbelo aquí →"
            elif errores:
                icono, color = ft.Icons.ERROR, ROJO
                detalle = f"{len(errores)}: {errores[0]}"
            elif b.completado:
                icono, color = ft.Icons.CHECK_CIRCLE, VERDE
                detalle = "Completa con datos del Excel."
            else:
                icono, color = ft.Icons.CHECK_CIRCLE, VERDE
                detalle = "Lista."
            filas.append(FilaDatos([
                ft.Icon(icono, size=18, color=color,
                        tooltip="\n".join(h.mensaje for h in hallazgos) or None),
                ft.Text(b.archivo, size=12, no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS, tooltip=b.ruta),
                # Editable: la extracción del nombre puede fallar y corregirla
                # aquí es más rápido que renombrar el archivo y volver a cargar.
                campo_tabla_texto(
                    valor=b.solicitud.beneficiario_nombre,
                    on_blur=lambda e, br=b: self._renombrar(br, e.control.value)),
                b.solicitud.empresa or "—",
                fmt_importe(b.solicitud.importe_total),
                detalle,
            ]))
        self.tabla.set_contenido(filas)

        listos = [b for b in self._borradores if b.listo]
        self.txt_resumen.value = (
            f"{len(self._borradores)} carátula(s) · {len(listos)} lista(s) "
            f"para dar de alta · "
            f"{len(self._borradores) - len(listos)} incompleta(s)")
        self.txt_resumen.color = VERDE if listos else NARANJA
        # Se pueden dar de alta TODAS, completas o no: el borrador incompleto
        # sigue siendo útil —ya tiene beneficiario y carátula— y se termina de
        # llenar en la tabla principal. Lo que no se puede es no tener nombre.
        con_nombre = [b for b in self._borradores if b.nombre_detectado]
        self.btn_importar.text = f"Dar de alta {len(con_nombre)}"
        self.btn_importar.disabled = not con_nombre
        self.previa.visible = True

    def _renombrar(self, borrador: caratulas.Borrador, nombre: str) -> None:
        nombre = (nombre or "").strip().upper()
        if nombre == borrador.solicitud.beneficiario_nombre:
            return
        borrador.nombre_detectado = nombre
        borrador.solicitud.beneficiario_nombre = nombre
        if not borrador.solicitud.cuenta_titular:
            borrador.solicitud.cuenta_titular = nombre
        self._pintar()
        self.modal.refrescar()

    # ------------------------------------------------------- importar
    def _importar(self, _e=None) -> None:
        guardadas, repetidas, sin_nombre = 0, [], 0
        for b in self._borradores:
            if not b.solicitud.beneficiario_nombre:
                sin_nombre += 1
                continue
            b.solicitud.lote_id = self._lote_id
            try:
                db.guardar_solicitud(b.solicitud, b.partidas)
            except db.ClaveDuplicada:
                repetidas.append(b.archivo)
                continue
            except Exception as exc:  # noqa: BLE001
                db.registrar("", "caratulas", f"{b.archivo}: {exc}", "ERROR")
                continue
            # La carátula se registra DESPUÉS: necesita que la solicitud exista.
            documentos.registrar(b.solicitud.id, b.ruta,
                                 documentos.TIPO_CARATULA, self._lote_id)
            guardadas += 1

        self.modal.cerrar()
        if callable(self._al_importar):
            self._al_importar(guardadas)
        avisos = [f"{guardadas} solicitud(es) dadas de alta con su carátula"]
        if repetidas:
            avisos.append(f"{len(repetidas)} ya existían")
        if sin_nombre:
            avisos.append(f"{sin_nombre} sin nombre, omitidas")
        self.app.avisar(" · ".join(avisos) + ".",
                        VERDE if not (repetidas or sin_nombre) else NARANJA,
                        duracion=9000)
