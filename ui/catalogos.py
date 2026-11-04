"""Catálogos del desglose: conceptos de pago e insumos y servicios.

Los dos alimentan lo mismo —los desplegables de la plantilla de Excel y del
formulario de captura, para que nadie escriba de memoria— y se administran igual,
así que viven en una sola pantalla con una pestaña cada uno. Lo que cambia entre
ellos es **a quién se le ofrece** y **de dónde sale la lista**:

| | A quién | De dónde |
|---|---|---|
| Conceptos de pago | Deudor y Acreedor | Se importan del portal |
| Insumos y servicios | Proveedor | El área entrega una lista depurada |

El alta manual está permitida en los dos —a veces hace falta algo que todavía no
se ha cargado— pero **avisa con claridad**: lo escrito a mano puede no existir en
SIPP, y si no existe, la solicitud que lo use no se podrá capturar. Ese aviso es
la parte importante de esta pantalla, no la tabla.

Las dos listas se **paginan**: el catálogo de insumos del área pasa de dos mil
entradas, y pintarlas todas manda al cliente miles de controles por cada tecla
del buscador.
"""

from __future__ import annotations

import asyncio

import flet as ft

from core import conceptos, insumos
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE
from ui.componentes import (Pestanas, boton_primario, boton_secundario,
                            buscador, campo_opciones, campo_texto, icono_accion,
                            tarjeta_seccion)
from ui.configuracion import ambiente_actual, url_login
from ui.tabla_responsiva import IZQ, ColumnaTabla, FilaDatos, TablaResponsiva

# Cuántos registros por página puede elegir el usuario. Mil es mucho para la
# vista, pero es lo que hace falta para revisar un catálogo entero de un tirón
# —o para buscar a ojo algo cuyo nombre no se recuerda—, así que se ofrece.
TAMANOS_PAGINA = [10, 50, 100, 1000]
TAMANO_DEFECTO = 50


class _VistaCatalogo:
    """Media pantalla: la tabla de un catálogo, con búsqueda y paginación.

    Lo común a los dos catálogos vive aquí; lo que los distingue —la barra de
    acciones y cómo se explica el origen de cada renglón— lo ponen las clases de
    abajo. Se hizo así al unirlos en una sola pantalla: eran dos módulos casi
    idénticos, y mantener dos copias de la búsqueda y la paginación garantizaba
    que se separaran a la primera corrección.
    """

    # --- lo que cada catálogo define ---
    modulo = conceptos          # core.conceptos | core.insumos
    titulo = ""
    descripcion = ""
    columna = ""
    singular = ""
    plural = ""
    hint_buscar = ""
    icono_vacio = ft.Icons.LIST_ALT
    texto_vacio = ""
    detalle_vacio = ""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._items: list = []
        self._filtro = ""
        self._pagina = 0
        self._por_pagina = TAMANO_DEFECTO
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        self.txt_estado = ft.Text("", color=GRIS)
        barra = ft.Row(
            [ft.Row(self.acciones(), spacing=10, wrap=True, expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             boton_primario(f"Agregar {self.singular}", ft.Icons.ADD,
                            self._abrir_alta)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        # Filtra mientras se escribe: son listas largas de nombres parecidos y
        # dar Enter para ver si algo existe es peor que recorrerlas a ojo.
        self.tf_buscar = buscador(self.hint_buscar, width=340)
        self.tf_buscar.on_change = self._cambiar_filtro
        self.btn_limpiar = icono_accion(
            ft.Icons.CLOSE, "Limpiar búsqueda", self._limpiar_filtro,
            color=GRIS)
        self.btn_limpiar.visible = False
        self.tf_buscar.suffix = self.btn_limpiar

        # --- Paginación.
        _, self.dd_tamano = campo_opciones(
            None, [str(n) for n in TAMANOS_PAGINA],
            valor=str(TAMANO_DEFECTO), width=110,
            on_change=self._cambiar_tamano)
        self.btn_pag_anterior = icono_accion(
            ft.Icons.CHEVRON_LEFT, "Página anterior",
            lambda _e: self._mover_pagina(-1))
        self.btn_pag_siguiente = icono_accion(
            ft.Icons.CHEVRON_RIGHT, "Página siguiente",
            lambda _e: self._mover_pagina(1))
        self.txt_pagina = ft.Text("", color=GRIS, size=12)
        self.barra_paginacion = ft.Row(
            [ft.Text("Mostrar:", color=GRIS, size=12), self.dd_tamano,
             ft.Container(expand=True), self.btn_pag_anterior,
             self.txt_pagina, self.btn_pag_siguiente],
            spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        fila_buscar = ft.Row(
            [self.tf_buscar, ft.Container(expand=True), self.txt_estado],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self.tabla = TablaResponsiva(self.page, self.columnas())
        self.vacio = ft.Container(
            content=ft.Column(
                [ft.Icon(self.icono_vacio, size=40, color=GRIS),
                 ft.Text(self.texto_vacio,
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 ft.Text(self.detalle_vacio, color=GRIS,
                         text_align=ft.TextAlign.CENTER)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        # Vacío por la búsqueda ≠ vacío de verdad. Con un solo mensaje, filtrar
        # algo y no encontrarlo invitaría a recargar un catálogo ya completo, o
        # a dar de alta a mano lo que ya existe escrito de otra forma.
        self.txt_sin_resultados = ft.Text("", color=GRIS,
                                          text_align=ft.TextAlign.CENTER)
        self.sin_resultados = ft.Container(
            content=ft.Column(
                [ft.Icon(ft.Icons.SEARCH_OFF, size=40, color=GRIS),
                 ft.Text(f"Ningún {self.singular} coincide con la búsqueda.",
                         theme_style=ft.TextThemeStyle.BODY_LARGE),
                 self.txt_sin_resultados,
                 ft.TextButton("Quitar la búsqueda", icon=ft.Icons.CLOSE,
                               on_click=self._limpiar_filtro)],
                spacing=8, tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0), padding=40, visible=False)

        self.contenido = ft.Column(
            [tarjeta_seccion(ft.Column([
                ft.Text(self.titulo,
                        theme_style=ft.TextThemeStyle.HEADLINE_SMALL),
                ft.Text(self.descripcion,
                        theme_style=ft.TextThemeStyle.BODY_MEDIUM, color=GRIS),
                ft.Divider(), barra, fila_buscar], spacing=12, tight=True)),
             ft.Container(
                 content=ft.Column(
                     [self.tabla.control, self.vacio, self.sin_resultados],
                     scroll=ft.ScrollMode.AUTO, expand=True,
                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                 expand=True),
             self.barra_paginacion],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self._construir_alta()

    # --- lo que cada catálogo pone de su parte ---
    def columnas(self) -> list:
        return [ColumnaTabla(self.columna, 62, IZQ),
                ColumnaTabla("Origen", 28, IZQ),
                ColumnaTabla("", 10)]

    def celdas(self, item) -> list:
        return [item.nombre, self.insignia(item)]

    def acciones(self) -> list:
        """Botones propios de este catálogo (importar, cargar…)."""
        return []

    def insignia(self, item) -> ft.Control:
        """Cómo se explica de dónde salió este renglón."""
        raise NotImplementedError

    def _construir_alta(self) -> None:
        raise NotImplementedError

    # ---------------------------------------------------------- búsqueda
    def _cambiar_filtro(self, _e=None) -> None:
        self._filtro = self.tf_buscar.value or ""
        # Filtrar y quedarse en la página 7 no tiene sentido: lo que se busca
        # casi siempre cabe en la primera.
        self._pagina = 0
        self.cargar_desde_db()

    def _limpiar_filtro(self, _e=None) -> None:
        self.tf_buscar.value = ""
        self._filtro = ""
        self._pagina = 0
        self.cargar_desde_db()

    def _coincide(self, item) -> bool:
        """True si el renglón casa con lo escrito en el buscador.

        Se compara con `normalizar`, el mismo criterio con el que el catálogo
        decide si dos nombres son el mismo: así buscar «prevision» encuentra
        «PREVISIÓN». Las palabras se buscan sueltas y en cualquier orden, porque
        los nombres de SIPP son largos y encadenados y nadie recuerda el orden
        exacto: «salarios isr» tiene que encontrar «ISR RETENCIONES POR
        SALARIOS».
        """
        palabras = self.modulo.normalizar(self._filtro).split()
        if not palabras:
            return True
        nombre = self.modulo.normalizar(item.nombre)
        return all(p in nombre for p in palabras)

    # -------------------------------------------------------- paginación
    def _cambiar_tamano(self, _e=None) -> None:
        try:
            self._por_pagina = int(self.dd_tamano.value or TAMANO_DEFECTO)
        except ValueError:
            self._por_pagina = TAMANO_DEFECTO
        self._pagina = 0
        self.cargar_desde_db()

    def _mover_pagina(self, delta: int) -> None:
        self._pagina += delta
        self.cargar_desde_db()

    # ------------------------------------------------------------ datos
    def cargar_desde_db(self) -> None:
        self._items = self.modulo.listar()
        visibles = [i for i in self._items if self._coincide(i)]

        paginas = max(1, -(-len(visibles) // self._por_pagina))
        self._pagina = max(0, min(self._pagina, paginas - 1))
        desde = self._pagina * self._por_pagina
        pagina = visibles[desde:desde + self._por_pagina]

        filas = []
        for item in pagina:
            filas.append(FilaDatos(self.celdas(item) + [
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18,
                              icon_color=ft.Colors.ERROR,
                              tooltip="Quitar del catálogo",
                              on_click=lambda _e, i=item.id: self._borrar(i)),
            ]))
        self.tabla.set_contenido(filas)

        total = len(self._items)
        buscando = bool(self.modulo.normalizar(self._filtro))
        self.tabla.control.visible = bool(filas)
        # El catálogo vacío y la búsqueda sin resultados son estados distintos y
        # nunca coinciden: si no hay nada que buscar, no puede sobrar el filtro.
        self.vacio.visible = not filas and not buscando
        self.sin_resultados.visible = not filas and buscando
        self.txt_sin_resultados.value = (
            f"Ninguno de los {total} {self.plural} del catálogo contiene "
            f"«{self._filtro.strip()}».")
        self.btn_limpiar.visible = buscando

        # El contador dice lo que se está VIENDO y, al lado, el total: si no,
        # filtrar parecería haber perdido registros.
        if buscando:
            self.txt_estado.value = f"{len(visibles)} de {total} {self.plural}"
        else:
            self.txt_estado.value = f"{total} {self.plural}"
        self.txt_estado.color = GRIS
        self._pintar_paginacion(len(visibles), desde, len(pagina), paginas)
        self._refrescar()

    def _pintar_paginacion(self, visibles: int, desde: int, en_pagina: int,
                           paginas: int) -> None:
        # El selector de tamaño se queda SIEMPRE: es el control con el que se
        # decide cuánto ver, y esconderlo cuando todo cabe en una página lo haría
        # aparecer y desaparecer según el filtro.
        hay_varias = paginas > 1
        self.btn_pag_anterior.visible = hay_varias
        self.btn_pag_siguiente.visible = hay_varias
        self.btn_pag_anterior.disabled = self._pagina == 0
        self.btn_pag_siguiente.disabled = self._pagina >= paginas - 1
        if hay_varias:
            self.txt_pagina.value = (
                f"{desde + 1}–{desde + en_pagina} de {visibles}   "
                f"(página {self._pagina + 1} de {paginas})")
        else:
            self.txt_pagina.value = ""

    def _refrescar(self) -> None:
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montada
            pass

    def _on_resize(self, _e=None) -> None:
        """La tabla se remide sola; presente por el contrato de pantalla."""

    # ----------------------------------------------------- alta y baja
    def _abrir_alta(self, _e=None) -> None:
        self.tf_nombre.value = ""
        self.txt_error_alta.visible = False
        self.page.show_dialog(self.dialogo_alta)

    def _cerrar_alta(self, _e=None) -> None:
        self.page.pop_dialog()

    def _guardar_alta(self, _e=None) -> None:
        nombre = (self.tf_nombre.value or "").strip()
        if not nombre:
            self.txt_error_alta.value = f"Escribe el nombre del {self.singular}."
            self.txt_error_alta.visible = True
            self.page.update()
            return
        creado = self.modulo.guardar(nombre, origen=self.modulo.ORIGEN_MANUAL)
        if creado is None:
            self.txt_error_alta.value = (
                f"«{self.modulo.canonico(nombre)}» ya está en el catálogo.")
            self.txt_error_alta.visible = True
            self.page.update()
            return
        self._cerrar_alta()
        # Igual que al importar: lo recién dado de alta tiene que quedar a la
        # vista, no escondido detrás del filtro anterior.
        self._limpiar_filtro()
        self.app.avisar(
            f"«{creado.nombre}» agregado. Recuerda confirmar con soporte que "
            f"exista en SIPP antes de usarlo en un lote.", NARANJA,
            duracion=9000)

    def _borrar(self, item_id: str) -> None:
        self.modulo.borrar(item_id)
        self.cargar_desde_db()


class VistaConceptos(_VistaCatalogo):
    """Conceptos de pago: lo que SIPP pide a un Deudor o a un Acreedor."""

    modulo = conceptos
    titulo = "Conceptos de pago"
    descripcion = (
        "Es lo que SIPP pide cuando el beneficiario es un DEUDOR o un ACREEDOR. "
        "La plantilla de Excel y el formulario de captura ofrecen esta lista "
        "para elegir en vez de teclear: un concepto mal escrito no falla al "
        "capturarlo, falla a media corrida, cuando el robot no lo encuentra.\n"
        "Es una lista única para todo el grupo: se lee de una empresa y sirve "
        "para todas.")
    columna = "Concepto de pago"
    singular = "concepto"
    plural = "concepto(s)"
    hint_buscar = "Buscar concepto…"
    icono_vacio = ft.Icons.LIST_ALT
    texto_vacio = "Todavía no hay conceptos en el catálogo."
    detalle_vacio = (f"Impórtalos de SIPP —«{conceptos.EMPRESA_BASE}» es la "
                     f"empresa con el catálogo más completo— o agrégalos a "
                     f"mano.")

    def acciones(self) -> list:
        # El selector de empresa es solo para DECIDIR DE DÓNDE LEER: el catálogo
        # resultante es una lista única, sin empresa.
        bl_empresa, self.dd_empresa = campo_opciones(
            None, NOMBRES_EMPRESAS, valor=conceptos.EMPRESA_BASE, width=260)
        self.btn_importar = boton_secundario(
            "Importar desde SIPP", ft.Icons.CLOUD_DOWNLOAD,
            on_click=self._importar)
        return [ft.Text("Leer de:", color=GRIS), bl_empresa, self.btn_importar]

    def insignia(self, item) -> ft.Control:
        if item.verificado:
            # La empresa de la que se leyó va en el tooltip, no en una columna:
            # el catálogo es una lista única y ese dato solo es rastro.
            return ft.Row([
                ft.Icon(ft.Icons.VERIFIED, size=16, color=VERDE),
                ft.Text("Importado de SIPP", size=12, color=VERDE)],
                spacing=6, tight=True,
                tooltip=(f"Leído del portal en «{item.empresa}»."
                         if item.empresa else "Leído del portal."))
        return ft.Row([
            ft.Icon(ft.Icons.WARNING_AMBER, size=16, color=NARANJA),
            ft.Text("Manual · sin verificar", size=12, color=NARANJA)],
            spacing=6, tight=True,
            tooltip="Podría no existir en SIPP. Si no existe, las solicitudes "
                    "que lo usen no se podrán capturar.")

    def cargar_desde_db(self) -> None:
        super().cargar_desde_db()
        # El aviso cuenta lo que se tiene ENFRENTE, no el catálogo entero.
        visibles = [c for c in self._items if self._coincide(c)]
        sin_verificar = sum(1 for c in visibles if not c.verificado)
        if sin_verificar:
            self.txt_estado.value += f" · {sin_verificar} sin verificar"
            self.txt_estado.color = NARANJA
        self._refrescar()

    def _construir_alta(self) -> None:
        """Diálogo de alta manual, con su advertencia."""
        bl_nombre, self.tf_nombre = campo_texto(
            "Concepto de pago", flotante=True,
            hint="Tal como aparece en SIPP")
        self.txt_error_alta = ft.Text(size=12, color=ROJO, visible=False)
        self.dialogo_alta = _dialogo_alta(
            "Agregar concepto de pago", bl_nombre, self.txt_error_alta,
            "Este concepto podría no existir en SIPP",
            "Al agregarlo a mano solo lo estás guardando en esta herramienta. "
            "Si SIPP no lo tiene asignado a la empresa, las solicitudes que lo "
            "usen no se podrán capturar y quedarán marcadas para revisión."
            "\n\nSi es un concepto nuevo, solicítalo al equipo de soporte antes "
            "de usarlo en un lote.",
            self._cerrar_alta, self._guardar_alta)

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
        # Sin esto, importar con una búsqueda puesta parecería no haber traído
        # nada: lo nuevo entraría al catálogo pero fuera del filtro.
        self._limpiar_filtro()
        self.app.avisar(
            f"{resumen['nuevos']} concepto(s) nuevos de {empresa}"
            + (f", {resumen['ya_estaban']} ya estaban."
               if resumen["ya_estaban"] else "."),
            VERDE, duracion=8000)


class VistaInsumos(_VistaCatalogo):
    """Insumos y servicios: lo que SIPP pide a un Proveedor."""

    modulo = insumos
    titulo = "Insumos y servicios"
    descripcion = (
        "Es lo que SIPP pide cuando el beneficiario es un PROVEEDOR, en lugar "
        "de los conceptos de pago. La plantilla de Excel ofrece esta lista solo "
        "en las filas de Proveedor.\n"
        "El catálogo de SIPP es enorme y casi nada se solicita desde aquí: se "
        "carga la lista depurada que entrega el área.")
    columna = "Insumo o servicio"
    singular = "insumo"
    plural = "insumo(s)"
    hint_buscar = "Buscar insumo o servicio…"
    icono_vacio = ft.Icons.HANDYMAN
    texto_vacio = "Todavía no hay insumos ni servicios en el catálogo."
    detalle_vacio = ("Carga el archivo que entrega el área con los servicios "
                     "que se pueden solicitar, o agrégalos a mano. Mientras "
                     "esté vacío, la plantilla de Excel seguirá ofreciendo solo "
                     "conceptos de pago.")

    def columnas(self) -> list:
        # El folio es lo que el robot usa para buscar el insumo en SIPP, así que
        # tiene que poder verse y contrastarse contra el portal.
        return [ColumnaTabla("Folio", 12, IZQ),
                ColumnaTabla(self.columna, 52, IZQ),
                ColumnaTabla("Origen", 26, IZQ),
                ColumnaTabla("", 10)]

    def celdas(self, item) -> list:
        return [item.clave or "—", item.nombre, self.insignia(item)]

    def _coincide(self, item) -> bool:
        """Además del nombre, se busca por FOLIO.

        Es como viene identificado el insumo en el Excel del área y en SIPP, y
        teclearlo es mucho más rápido que un nombre de ochenta caracteres.
        """
        filtro = (self._filtro or "").strip()
        if filtro and item.clave and filtro.lower() == item.clave.lower():
            return True
        return super()._coincide(item)

    def acciones(self) -> list:
        self.btn_cargar = boton_secundario(
            "Cargar catálogo", ft.Icons.UPLOAD_FILE, on_click=self._elegir)
        return [self.btn_cargar]

    def insignia(self, item) -> ft.Control:
        if item.verificado:
            return ft.Row([
                ft.Icon(ft.Icons.VERIFIED, size=16, color=VERDE),
                ft.Text("Del catálogo del área", size=12, color=VERDE)],
                spacing=6, tight=True,
                tooltip="Cargado del archivo depurado que entrega el área.")
        return ft.Row([
            ft.Icon(ft.Icons.WARNING_AMBER, size=16, color=NARANJA),
            ft.Text("Manual · sin verificar", size=12, color=NARANJA)],
            spacing=6, tight=True,
            tooltip="Podría no existir en SIPP. Si no existe, las solicitudes "
                    "que lo usen no se podrán capturar.")

    def _construir_alta(self) -> None:
        bl_nombre, self.tf_nombre = campo_texto(
            "Insumo o servicio", flotante=True,
            hint="Tal como aparece en SIPP")
        self.txt_error_alta = ft.Text(size=12, color=ROJO, visible=False)
        self.dialogo_alta = _dialogo_alta(
            "Agregar insumo o servicio", bl_nombre, self.txt_error_alta,
            "Este insumo podría no existir en SIPP",
            "Al agregarlo a mano solo lo estás guardando en esta herramienta. "
            "Si SIPP no lo tiene, las solicitudes que lo usen no se podrán "
            "capturar: el robot no lo encontrará en la pestaña de Insumos & "
            "Servicios.\n\nSi es un servicio nuevo, pídelo al área antes de "
            "usarlo en un lote.",
            self._cerrar_alta, self._guardar_alta)

    # ------------------------------------------------------------ carga
    async def _elegir(self, _e=None) -> None:
        archivos = await self.app.picker.pick_files(
            dialog_title="Elige el catálogo depurado de insumos y servicios",
            allowed_extensions=["xlsx", "xlsm", "csv"], allow_multiple=False)
        if not archivos:
            return
        self.btn_cargar.disabled = True
        self.btn_cargar.text = "Leyendo…"
        self.page.update()
        try:
            leidos = await asyncio.to_thread(insumos.leer_archivo,
                                             archivos[0].path)
        except Exception as exc:  # noqa: BLE001 — se reporta, la app sigue viva
            leidos = []
            self.app.avisar(f"No se pudo leer el archivo: {exc}", ROJO,
                            duracion=9000)
        finally:
            self.btn_cargar.disabled = False
            self.btn_cargar.text = "Cargar catálogo"
            self.page.update()
        if not leidos:
            self.app.avisar(
                "No se encontró ninguna columna con nombres de insumo. Revisa "
                "que la lista esté en la primera hoja del archivo.", NARANJA,
                duracion=9000)
            return
        self._confirmar_carga(archivos[0].name, leidos)

    def _confirmar_carga(self, nombre_archivo: str, leidos: list[str]) -> None:
        """Pregunta si sustituir el catálogo o sumarse a él.

        Se pregunta en vez de decidir porque las dos cosas son legítimas y se
        parecen: cuando el área entrega una versión nueva lo que quiere es
        SUSTITUIR —lo que quitó tiene que dejar de ofrecerse—, pero a veces solo
        manda los servicios que se agregaron.
        """
        actuales = len(self._items)

        def aplicar(reemplazar: bool):
            def _accion(_e=None) -> None:
                self.page.pop_dialog()
                resumen = insumos.importar(leidos, reemplazar=reemplazar)
                self._limpiar_filtro()
                partes = [f"{resumen['nuevos']} nuevo(s)"]
                if resumen["ya_estaban"]:
                    partes.append(f"{resumen['ya_estaban']} ya estaban")
                if resumen["retirados"]:
                    partes.append(f"{resumen['retirados']} retirado(s)")
                self.app.avisar("Catálogo actualizado: " + " · ".join(partes),
                                VERDE, duracion=8000)
            return _accion

        acciones = [ft.TextButton("Cancelar",
                                  on_click=lambda _e: self.page.pop_dialog())]
        if actuales:
            acciones.append(ft.TextButton("Agregar a lo que hay",
                                          on_click=aplicar(False)))
        acciones.append(ft.FilledButton(
            "Sustituir el catálogo" if actuales else "Cargar",
            icon=ft.Icons.CHECK, on_click=aplicar(True)))

        detalle = f"El archivo «{nombre_archivo}» trae {len(leidos)} insumo(s)."
        if actuales:
            detalle += (f"\n\nEl catálogo tiene ahora {actuales}. Si sustituyes, "
                        f"los que no vengan en el archivo dejarán de ofrecerse "
                        f"en la plantilla.")
        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Icon(ft.Icons.UPLOAD_FILE, color=ft.Colors.PRIMARY),
                          ft.Text("Cargar catálogo de insumos")], spacing=10),
            content=ft.Column(
                [ft.Text(detalle),
                 ft.Text("Ejemplos de lo que se leyó:", color=GRIS, size=12),
                 ft.Text("\n".join(f"· {n}" for n in leidos[:5]), size=12)],
                spacing=8, tight=True),
            actions=acciones,
            actions_alignment=ft.MainAxisAlignment.END))


def _dialogo_alta(titulo, bloque_nombre, txt_error, titulo_aviso, cuerpo_aviso,
                  al_cancelar, al_guardar) -> ft.AlertDialog:
    """El diálogo de alta manual, que en los dos catálogos es el mismo salvo
    el texto: un campo y una advertencia grande, que es lo que de verdad
    importa aquí."""
    aviso = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.WARNING_AMBER, color=NARANJA, size=22),
            ft.Column([
                ft.Text(titulo_aviso, weight=ft.FontWeight.BOLD, color=NARANJA),
                ft.Text(cuerpo_aviso,
                        theme_style=ft.TextThemeStyle.BODY_MEDIUM)],
                spacing=4, tight=True, expand=True)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
        bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.ORANGE),
        border=ft.Border.all(1, NARANJA), border_radius=8, padding=14)
    return ft.AlertDialog(
        modal=True,
        title=ft.Text(titulo),
        content=ft.Container(
            ft.Column([bloque_nombre, aviso, txt_error],
                      spacing=16, tight=True, scroll=ft.ScrollMode.AUTO),
            width=520),
        actions=[
            ft.TextButton("Cancelar", on_click=al_cancelar),
            ft.FilledButton("Agregar de todos modos", icon=ft.Icons.ADD,
                            on_click=al_guardar),
        ],
        actions_alignment=ft.MainAxisAlignment.END)


class SeccionCatalogos:
    """Pantalla de los dos catálogos, uno por pestaña."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self.conceptos = VistaConceptos(app)
        self.insumos = VistaInsumos(app)
        self._vistas = {"conceptos": self.conceptos, "insumos": self.insumos}

        self.pestanas = Pestanas(
            [("conceptos", "Conceptos de pago", ft.Icons.LIST_ALT),
             ("insumos", "Insumos y servicios", ft.Icons.HANDYMAN)],
            al_cambiar=self._cambiar)
        self.insumos.contenido.visible = False

        self.contenido = ft.Column(
            [ft.Row([self.pestanas.control]),
             self.conceptos.contenido, self.insumos.contenido],
            spacing=16, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _cambiar(self, clave: str) -> None:
        for nombre, vista in self._vistas.items():
            vista.contenido.visible = nombre == clave
        try:
            self.contenido.update()
        except Exception:  # noqa: BLE001 — aún no montada
            pass

    def cargar_desde_db(self) -> None:
        for vista in self._vistas.values():
            vista.cargar_desde_db()
        # El conteo va en la pestaña: es lo que dice de un vistazo si un
        # catálogo está cargado, sin tener que entrar a mirarlo.
        self.pestanas.set_conteo("conceptos", len(self.conceptos._items))
        self.pestanas.set_conteo("insumos", len(self.insumos._items))

    def _on_resize(self, _e=None) -> None:
        for vista in self._vistas.values():
            vista._on_resize()
