"""Alta de solicitudes a partir de carátulas bancarias.

Una carátula = una solicitud. Es el flujo que corresponde a cómo llega el
trabajo: el área recibe una carpeta con un archivo por persona, y esa carpeta ya
**es** el listado de a quién hay que pagarle.

Tres pasos, con el Excel como paso opcional al final:

1. Elegir las carátulas (archivos sueltos o una carpeta completa). De cada una
   se lee la **CLABE** y el **titular** (`adaptadores/ocr_caratula.py`).
2. Revisar lo que se leyó y corregir lo que haga falta —el beneficiario y la
   CLABE son editables en la propia tabla.
3. Opcionalmente, cargar el Excel: sus filas se emparejan **por CLABE** y
   completan importe, fecha, concepto, RFC, correo y demás.

Lo que este orden garantiza y el inverso no: **ninguna solicitud puede quedarse
sin carátula**, porque no existe si no hay archivo. Como SIPP la exige para dar
de alta la cuenta bancaria, eso elimina de raíz el caso que más trabajo manual
generaba.

Leer las carátulas cuesta segundos por archivo, así que el paso 1 corre en otro
hilo, informa su avance y se puede detener. Sin eso, elegir una carpeta de
cincuenta carátulas dejaría la ventana congelada un minuto sin explicar por qué.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import flet as ft

from core import catalogos, db, descargas, documentos, ocr
from core.adaptadores import caratulas
from core.adaptadores import excel as adaptador_excel
from core.adaptadores import ocr_caratula
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE, fmt_importe
from ui.componentes import (CampoFecha, Modal, boton_primario,
                            boton_secundario, campo_opciones,
                            campo_tabla_texto, tarjeta_seccion)
from ui.tabla_responsiva import (DER, IZQ, ColumnaTabla, FilaDatos,
                                 TablaResponsiva)

_COLUMNAS = [
    ColumnaTabla("", 4),                             # semáforo
    ColumnaTabla("Archivo", 17, IZQ),
    ColumnaTabla("Beneficiario", 22, IZQ),
    ColumnaTabla("CLABE", 17, IZQ),
    ColumnaTabla("Banco", 10, IZQ),
    ColumnaTabla("Importe", 10, DER),
    ColumnaTabla("Detalle", 20, IZQ),
]


class AltaDesdeCaratulas:
    """Modal de alta por carátulas. `al_importar(n)` recibe cuántas se crearon."""

    def __init__(self, app, al_importar) -> None:
        self.app = app
        self.page = app.page
        self._al_importar = al_importar
        self._borradores: list[caratulas.Borrador] = []
        self._lote_id = ""
        self._leyendo = False
        self._detener = False
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        # Paso 1 — carátulas.
        self.txt_archivos = ft.Text("Ninguna carátula elegida.", color=GRIS)
        self.barra_lectura = ft.ProgressBar(value=0, visible=False)
        self.btn_archivos = boton_secundario(
            "Elegir archivos…", ft.Icons.PICTURE_AS_PDF,
            on_click=self._elegir_archivos)
        self.btn_carpeta = boton_secundario(
            "Elegir carpeta…", ft.Icons.FOLDER_OPEN,
            on_click=self._elegir_carpeta)
        self.btn_enlaces = boton_secundario(
            "Desde archivo con enlaces…", ft.Icons.LINK,
            on_click=self._elegir_enlaces)
        self.btn_detener = boton_secundario(
            "Detener", ft.Icons.STOP, on_click=self._detener_lectura)
        self.btn_detener.visible = False
        paso1 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.ACCOUNT_BALANCE,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("1. Elige las carátulas",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Cada archivo se convierte en una solicitud, con su "
                    "carátula ya adjunta. Se lee la CLABE y el titular de cada "
                    "una: el titular es el beneficiario, y la CLABE es con lo "
                    "que después se empareja el Excel. Si tus carátulas están "
                    "en enlaces dentro de un Excel o CSV, se descargan solas.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            ft.Row([self.btn_archivos, self.btn_carpeta, self.btn_enlaces,
                    self.btn_detener, self.txt_archivos],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                   wrap=True),
            self.barra_lectura,
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
        # La carátula NO trae fecha de pago —no es un dato del banco, es una
        # decisión de quien paga—, así que sin Excel las 47 solicitudes de un
        # lote saldrían todas con «Falta la fecha de pago». Ponerla aquí una
        # vez evita corregirlas una por una.
        self.campo_fecha = CampoFecha(self.page, "Fecha de pago",
                                      on_change=self._aplicar_comunes)
        paso2 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.TUNE, color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("2. Datos comunes a todas",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Se aplican a las solicitudes que no traigan ese dato del "
                    "Excel.", theme_style=ft.TextThemeStyle.BODY_MEDIUM,
                    color=GRIS),
            ft.Row([bl_empresa, bl_sucursal, bl_tipo,
                    ft.Container(self.campo_fecha.control, width=200)],
                   spacing=16, wrap=True),
        ], spacing=10, tight=True))

        # Paso 3 — Excel o CSV.
        self.txt_excel = ft.Text("Sin archivo: tendrás que completar a mano lo "
                                 "que la carátula no trae.", color=GRIS)
        self.btn_excel = boton_secundario("Elegir Excel o CSV…",
                                          ft.Icons.UPLOAD_FILE,
                                          on_click=self._elegir_excel)
        paso3 = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.TABLE_VIEW,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("3. Completa con el Excel o CSV (opcional)",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Text("Sus filas se emparejan con las carátulas por CLABE, y por "
                    "nombre las que no tengan CLABE de los dos lados. Completa "
                    "empresa, sucursal, tipo de beneficiario, RFC, correo, "
                    "banco, forma de pago, tipo de gasto, fecha, moneda, "
                    "descripción e importe. Ni el beneficiario ni la cuenta se "
                    "sobrescriben: manda la carátula, que es la que acredita a "
                    "quién se le paga.",
                    theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
            ft.Row([self.btn_excel, self.txt_excel],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, tight=True))

        # Vista previa.
        self.txt_resumen = ft.Text("", theme_style=ft.TextThemeStyle.BODY_LARGE)
        self.tabla = TablaResponsiva(self.page, _COLUMNAS, ancho_inicial=1000,
                                     alto_cuerpo=250)
        self.previa = ft.Column(
            [ft.Row([ft.Icon(ft.Icons.FACT_CHECK,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Text("Revisa antes de dar de alta",
                             theme_style=ft.TextThemeStyle.LABEL_LARGE,
                             color=ft.Colors.PRIMARY_CONTAINER)],
                    spacing=8, tight=True),
             self.txt_resumen,
             self.tabla.control],
            spacing=10, tight=True, visible=False,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.btn_importar = boton_primario(
            "Dar de alta", ft.Icons.PLAYLIST_ADD, self._importar)
        self.btn_importar.disabled = True

        self.modal = Modal(
            self.page, "Alta desde carátulas",
            subtitulo="una carátula, una solicitud",
            ancho=1060, alto_cuerpo=520,
            acciones=[
                boton_secundario("Cerrar",
                                 on_click=lambda _e: self.modal.cerrar()),
                self.btn_importar,
            ])
        self.modal.cuerpo.controls = [paso1, paso2, paso3, self.previa]

    # -------------------------------------------------------- apertura
    def abrir(self, lote_id: str, parada: str = "LLENADA") -> None:
        self._lote_id = lote_id
        # Lo que se dé de alta hereda la parada del lote, no una fija: si el
        # lote está en «Guardar y autorizar», estas solicitudes también.
        self._parada = parada
        self._borradores = []
        self._detener = False
        self._modo_lectura(False)
        self.txt_archivos.value = "Ninguna carátula elegida."
        self.txt_archivos.color = GRIS
        self.txt_excel.value = ("Sin archivo: tendrás que completar a mano lo "
                                "que la carátula no trae.")
        self.txt_excel.color = GRIS
        self.campo_fecha.value = ""
        self.previa.visible = False
        self.btn_importar.disabled = True
        self.btn_importar.text = "Dar de alta"
        # Sin Tesseract se puede seguir trabajando —las carátulas en PDF traen
        # capa de texto—, pero las fotos no se van a poder leer y es mejor
        # decirlo antes de elegir la carpeta que después de esperar por ella.
        if not ocr.tesseract_disponible():
            self.txt_archivos.value = (
                "Sin Tesseract instalado: se leerán los PDF con texto, pero no "
                "las fotos ni los escaneos.")
            self.txt_archivos.color = NARANJA
        self.modal.abrir()

    # -------------------------------------------------------- carátulas
    async def _elegir_archivos(self, _e=None) -> None:
        if self._leyendo:
            return
        seleccion = await self.app.picker.pick_files(
            dialog_title="Elige las carátulas bancarias", allow_multiple=True,
            allowed_extensions=["pdf", "jpg", "jpeg", "png"])
        if not seleccion:
            return
        await self._cargar([a.path for a in seleccion])

    async def _elegir_carpeta(self, _e=None) -> None:
        if self._leyendo:
            return
        carpeta = await self.app.picker.get_directory_path(
            dialog_title="Elige la carpeta con las carátulas")
        if not carpeta:
            return
        await self._cargar([carpeta])

    async def _elegir_enlaces(self, _e=None) -> None:
        """Carátulas que llegan como enlaces dentro de una hoja de cálculo.

        Se bajan a una carpeta temporal y a partir de ahí el flujo es el mismo
        que si se hubieran elegido a mano: leer, revisar, dar de alta.
        """
        if self._leyendo:
            return
        seleccion = await self.app.picker.pick_files(
            dialog_title="Elige el archivo con los enlaces a las carátulas",
            allowed_extensions=["xlsx", "xlsm", "csv"], allow_multiple=False)
        if not seleccion:
            return
        ruta = seleccion[0].path

        self._detener = False
        self._modo_lectura(True)
        self.txt_archivos.value = "Buscando enlaces en el archivo…"
        self.txt_archivos.color = GRIS
        self.barra_lectura.value = None
        self.modal.refrescar()

        try:
            columnas = await asyncio.to_thread(
                adaptador_excel.columnas_con_enlaces, ruta)
            elegida = adaptador_excel.columna_de_caratulas(columnas)
            enlaces = await asyncio.to_thread(
                adaptador_excel.enlaces_por_fila, ruta, columna=elegida)
            nombres = await asyncio.to_thread(
                adaptador_excel.nombres_por_fila, ruta)
        except Exception as exc:  # noqa: BLE001 — se reporta, el modal sigue
            self._modo_lectura(False)
            self.txt_archivos.value = f"No se pudo leer el archivo: {exc}"
            self.txt_archivos.color = ROJO
            self.modal.refrescar()
            return

        if not enlaces:
            self._modo_lectura(False)
            self.txt_archivos.value = (
                "El archivo no trae enlaces a documentos. Deben ser "
                "direcciones http(s), como hipervínculo o escritas en la celda.")
            self.txt_archivos.color = NARANJA
            self.modal.refrescar()
            return

        # Un formulario puede pedir varios documentos por solicitud (boleta,
        # acta, carátula). Se dice de cuál columna se está bajando: si se eligió
        # la equivocada, el usuario tiene que poder notarlo ANTES de esperar
        # doscientas descargas y encontrarse con actas de nacimiento.
        if len(columnas) > 1:
            cual = next((c for c in columnas if c["indice"] == elegida), None)
            if cual:
                self.app.avisar(
                    f"El archivo trae {len(columnas)} columnas con documentos; "
                    f"se usará «{cual['encabezado'][:60]}…» (columna "
                    f"{cual['letra']}).", VERDE, duracion=6000)
            else:
                self.app.avisar(
                    f"El archivo trae {len(columnas)} columnas con documentos y "
                    f"ninguna dice ser la carátula: se tomará la primera de cada "
                    f"fila. Revisa los resultados.", NARANJA, duracion=8000)

        bucle = asyncio.get_running_loop()

        def progreso(hechos: int, total: int, _url: str) -> None:
            def aplicar() -> None:
                self.barra_lectura.value = hechos / total if total else None
                self.txt_archivos.value = f"Descargando {hechos} de {total}…"
                self.modal.refrescar()
            bucle.call_soon_threadsafe(aplicar)

        carpeta = tempfile.mkdtemp(prefix="caratulas_enlaces_")
        try:
            rutas, errores = await asyncio.to_thread(
                descargas.descargar_varios, list(enlaces.items()), carpeta,
                nombres=nombres, on_progreso=progreso,
                cancelado=lambda: self._detener)
        except Exception as exc:  # noqa: BLE001
            self._modo_lectura(False)
            self.txt_archivos.value = f"No se pudieron descargar: {exc}"
            self.txt_archivos.color = ROJO
            self.modal.refrescar()
            return
        finally:
            self._modo_lectura(False)

        self._errores_descarga = errores
        if not rutas:
            # Todos fallaron: el primer error explica por qué mejor que un
            # recuento, y el más común —enlaces caducados— se arregla volviendo
            # a exportar el archivo.
            detalle = errores[0].split(": ", 1)[-1] if errores else ""
            self.txt_archivos.value = (
                f"No se pudo descargar ninguno de los {len(enlaces)} enlaces. "
                f"{detalle}")
            self.txt_archivos.color = ROJO
            self.previa.visible = False
            self.btn_importar.disabled = True
            self.modal.refrescar()
            return

        await self._cargar([carpeta])
        if errores:
            self.app.avisar(
                f"{len(errores)} enlace(s) no se pudieron descargar; se "
                f"cargaron {len(rutas)}. Revisa el archivo de origen.", NARANJA)

    def _detener_lectura(self, _e=None) -> None:
        self._detener = True
        self.txt_archivos.value = "Deteniendo al terminar la carátula en curso…"
        self.modal.refrescar()

    def _modo_lectura(self, activo: bool) -> None:
        """Bloquea lo que no debe tocarse mientras se leen las carátulas."""
        self._leyendo = activo
        self.btn_archivos.disabled = activo
        self.btn_carpeta.disabled = activo
        self.btn_enlaces.disabled = activo
        self.btn_excel.disabled = activo
        self.btn_detener.visible = activo
        self.barra_lectura.visible = activo
        if activo:
            self.btn_importar.disabled = True

    async def _cargar(self, rutas: list[str]) -> None:
        """Lee las carátulas en otro hilo, informando el avance.

        El OCR de una carátula toma segundos, y una carpeta trae decenas. Correr
        esto en el hilo de la interfaz congelaría la ventana sin decir por qué,
        que es indistinguible de que la app se colgó.
        """
        self._detener = False
        self._modo_lectura(True)
        self.txt_archivos.value = "Leyendo las carátulas…"
        self.txt_archivos.color = GRIS
        self.barra_lectura.value = None      # indeterminada hasta saber cuántas
        self.modal.refrescar()

        bucle = asyncio.get_running_loop()

        def progreso(hechas: int, total: int, archivo: str) -> None:
            def aplicar() -> None:
                self.barra_lectura.value = hechas / total if total else None
                self.txt_archivos.value = (
                    f"Leyendo {hechas} de {total}: {archivo}")
                self.modal.refrescar()
            bucle.call_soon_threadsafe(aplicar)

        try:
            self._borradores = await asyncio.to_thread(
                caratulas.crear_borradores, rutas, self._lote_id,
                parada=getattr(self, "_parada", "LLENADA"),
                empresa=self.dd_empresa.value or "",
                sucursal=self.dd_sucursal.value or "",
                tipo_beneficiario=self.dd_tipo.value or "Acreedor",
                on_progreso=progreso, cancelado=lambda: self._detener)
        except Exception as exc:  # noqa: BLE001 — se reporta, el modal sigue vivo
            self._borradores = []
            self.txt_archivos.value = f"No se pudieron leer las carátulas: {exc}"
            self.txt_archivos.color = ROJO
        finally:
            self._modo_lectura(False)

        if not self._borradores:
            if self.txt_archivos.color is not ROJO:
                self.txt_archivos.value = ("No se encontraron archivos válidos "
                                           "(PDF, JPG o PNG).")
                self.txt_archivos.color = NARANJA
            self.previa.visible = False
            self.btn_importar.disabled = True
            self.modal.refrescar()
            return

        # La fecha común no la conoce `crear_borradores` (no es un dato de la
        # carátula), así que se vuelca aquí sobre lo recién leído. Si no, habría
        # que volver a tocar el campo para que surtiera efecto.
        if self.campo_fecha.value:
            self._aplicar_comunes()
        self.txt_archivos.value = self._resumen_lectura()
        self._pintar()
        self.modal.refrescar()

    def _resumen_lectura(self) -> str:
        """Qué se pudo leer de las carátulas, contado por lo accionable.

        Se separan «sin CLABE» y «CLABE dudosa» porque piden cosas distintas:
        la primera hay que teclearla, la segunda solo verificarla contra el
        documento —y a veces está bien, porque hay bancos que imprimen la CLABE
        con un dígito que el OCR confunde—.
        """
        total = len(self._borradores)
        sin_clabe = sum(1 for b in self._borradores
                        if not b.solicitud.cuenta_clabe)
        dudosas = sum(1 for b in self._borradores
                      if b.solicitud.cuenta_clabe and not b.clabe_confiable)
        sin_nombre = sum(1 for b in self._borradores if not b.nombre_detectado)

        partes = [f"{total} carátula(s)"]
        if sin_clabe:
            partes.append(f"{sin_clabe} sin CLABE: escríbela en la tabla")
        if dudosas:
            partes.append(f"{dudosas} con CLABE dudosa: verifícala")
        if sin_nombre:
            partes.append(f"{sin_nombre} sin beneficiario: escríbelo")
        self.txt_archivos.color = (
            GRIS if not (sin_clabe or dudosas or sin_nombre) else NARANJA)
        if self._detener:
            partes.append("lectura detenida por ti")
        return " · ".join(partes)

    def _aplicar_comunes(self, _e=None) -> None:
        """Vuelca los valores comunes sobre los borradores que no los tengan."""
        for b in self._borradores:
            if self.dd_empresa.value and not b.solicitud.empresa:
                b.solicitud.empresa = self.dd_empresa.value
            if self.dd_sucursal.value and not b.solicitud.sucursal:
                b.solicitud.sucursal = self.dd_sucursal.value
            if self.dd_tipo.value:
                b.solicitud.tipo_beneficiario = self.dd_tipo.value
            # Como empresa y sucursal: solo rellena lo vacío, para no pisar la
            # fecha que ya hubiera traído el Excel ni la corregida a mano.
            if self.campo_fecha.value and not b.solicitud.fecha_pago:
                b.solicitud.fecha_pago = self.campo_fecha.value
        if self._borradores:
            self._pintar()
        self.modal.refrescar()

    # ------------------------------------------------------------ Excel
    async def _elegir_excel(self, _e=None) -> None:
        if not self._borradores:
            self.app.avisar("Primero elige las carátulas.", NARANJA)
            return
        seleccion = await self.app.picker.pick_files(
            dialog_title="Elige el Excel o CSV con los datos",
            allowed_extensions=["xlsx", "xlsm", "csv"], allow_multiple=False)
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
        # Se dice CÓMO emparejó cada una: por CLABE no hay duda de que la fila
        # es de esa persona; por nombre sí puede haberla, y conviene que se note
        # la diferencia sin tener que abrir la tabla.
        if resumen["por_clabe"]:
            partes.append(f"{resumen['por_clabe']} por CLABE")
        if resumen["por_nombre"]:
            partes.append(f"{resumen['por_nombre']} por nombre")
        if resumen["discrepancias"]:
            partes.append(f"{resumen['discrepancias']} con la CLABE del Excel "
                          f"distinta: revísalas")
        if resumen["sin_excel"]:
            partes.append(f"{len(resumen['sin_excel'])} sin fila en el Excel")
        if resumen["sin_caratula"]:
            # Esto suele significar que falta una carátula, no que sobre una
            # fila: por eso se nombra a quién.
            muestra = ", ".join(resumen["sin_caratula"][:3])
            partes.append(f"{len(resumen['sin_caratula'])} fila(s) sin carátula "
                          f"({muestra})")
        self.txt_excel.value = " · ".join(partes)
        self.txt_excel.color = (
            ROJO if resumen["discrepancias"]
            else NARANJA if resumen["sin_excel"] else VERDE)
        self._pintar()
        self.modal.refrescar()

    # ---------------------------------------------------- vista previa
    def _semaforo(self, b: caratulas.Borrador) -> tuple:
        """Ícono, color y detalle de una carátula.

        El orden de los casos es el de la gravedad, y no es arbitrario: primero
        lo que impide dar de alta (sin beneficiario), luego lo que puede hacer
        que se pague a quien no es (CLABE dudosa o discrepante), y solo al final
        lo que nada más falta por llenar.
        """
        hallazgos = b.hallazgos
        errores = [h.mensaje for h in hallazgos if h.es_error]
        if not b.nombre_detectado:
            return (ft.Icons.HELP_OUTLINE, ROJO,
                    "No se reconoció el beneficiario: escríbelo aquí →")
        if b.avisos:
            return ft.Icons.WARNING_AMBER, NARANJA, b.avisos[0]
        if b.solicitud.cuenta_clabe and not b.clabe_confiable:
            return (ft.Icons.WARNING_AMBER, NARANJA,
                    "La CLABE no pasa su dígito verificador: verifícala.")
        if errores:
            return ft.Icons.ERROR, ROJO, f"{len(errores)}: {errores[0]}"
        if b.emparejado_por == caratulas.POR_CLABE:
            return (ft.Icons.CHECK_CIRCLE, VERDE,
                    "Completa con el Excel, emparejada por CLABE.")
        if b.emparejado_por == caratulas.POR_NOMBRE:
            return (ft.Icons.CHECK_CIRCLE, VERDE,
                    "Completa con el Excel, emparejada por nombre.")
        return ft.Icons.CHECK_CIRCLE, VERDE, "Lista."

    def _pintar(self) -> None:
        filas = []
        for b in self._borradores:
            icono, color, detalle = self._semaforo(b)
            tips = [h.mensaje for h in b.hallazgos] + b.avisos
            marca = ("leído de la carátula"
                     if b.origen_nombre == caratulas.NOMBRE_DE_OCR else
                     "escrito por ti"
                     if b.origen_nombre == caratulas.NOMBRE_A_MANO else
                     "deducido del nombre del archivo")
            filas.append(FilaDatos([
                ft.Icon(icono, size=18, color=color,
                        tooltip="\n".join(tips) or None),
                ft.Text(b.archivo, size=12, no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS, tooltip=b.ruta),
                # Editable: la lectura puede fallar y corregirla aquí es más
                # rápido que renombrar el archivo y volver a cargar.
                campo_tabla_texto(
                    valor=b.solicitud.beneficiario_nombre,
                    tooltip=f"Beneficiario {marca}",
                    on_blur=lambda e, br=b: self._renombrar(br, e.control.value)),
                # También editable, y por la misma razón de peso: es el dato del
                # que depende a quién se le paga, y si el OCR lo leyó mal hay que
                # poder arreglarlo sin salir de aquí.
                campo_tabla_texto(
                    valor=b.solicitud.cuenta_clabe,
                    tooltip=("CLABE de la carátula. Con ella se empareja el "
                             "Excel."),
                    on_blur=lambda e, br=b: self._recapturar_clabe(
                        br, e.control.value)),
                b.solicitud.cuenta_banco or "—",
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
        # sigue siendo útil —ya tiene beneficiario, cuenta y carátula— y se
        # termina de llenar en la tabla principal. Lo que no se puede es no
        # tener nombre.
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
        borrador.origen_nombre = caratulas.NOMBRE_A_MANO
        # El titular de la cuenta acompaña al beneficiario: son la misma persona
        # y en SIPP se capturan como tal. Se reescribe aunque ya tuviera valor,
        # porque el que había venía de la misma lectura que se acaba de corregir.
        borrador.solicitud.cuenta_titular = nombre
        self._pintar()
        self.modal.refrescar()

    def _recapturar_clabe(self, borrador: caratulas.Borrador,
                          clabe: str) -> None:
        """Corrige a mano la CLABE que leyó el OCR.

        Al cambiarla se borran los avisos de la lectura anterior: hablaban de
        una CLABE que ya no es la de esta solicitud, y dejarlos ahí haría dudar
        del dato que el usuario acaba de verificar contra el documento.
        """
        limpia = "".join(c for c in (clabe or "") if c.isdigit())
        if limpia == borrador.solicitud.cuenta_clabe:
            return
        borrador.solicitud.cuenta_clabe = limpia
        borrador.solicitud.cuenta_banco = (
            ocr_caratula.banco_de_clabe(limpia)
            or borrador.solicitud.cuenta_banco)
        borrador.avisos = []
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
