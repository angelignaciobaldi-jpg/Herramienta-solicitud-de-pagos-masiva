"""Captura manual de una solicitud de pago (alta y edición).

Es el `ManualAdapter` de ESPECIFICACION.md §6 con cara de formulario: produce el
mismo par (`Solicitud`, `[Partida]`) que producirán los adaptadores de Excel y de
CFDI, y pasa por el mismo validador. Por eso el modal no guarda nada por su
cuenta: devuelve el resultado a la pantalla que lo abrió.

El orden de los campos sigue el del formulario real de SIPP (encabezado →
beneficiario → pago → desglose), para que quien capture aquí reconozca lo que
verá en el portal.
"""

from __future__ import annotations

import os

import flet as ft

from core import conceptos, db, documentos, validador
from core.catalogos import (FORMA_PAGO_CON_CUENTA, FORMAS_PAGO, MONEDAS,
                            SUCURSALES, TIPOS_BENEFICIARIO, TIPOS_GASTO,
                            TIPO_PAGO_SOPORTADO, clase_desglose)
from core.db import CONCEPTO, INSUMO, Partida, Solicitud
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE, fmt_importe, parse_importe
from ui.componentes import (CampoFecha, Modal, boton_herramienta,
                            boton_primario, boton_secundario, campo_opciones,
                            campo_texto, icono_accion, seccion_formulario)


class _RenglonPartida:
    """Un renglón editable del desglose. Los campos visibles dependen de la
    clase: un concepto solo lleva nombre e importe; un insumo lleva además
    centro de costos y cuenta contable, que es lo que pide su grid en SIPP."""

    def __init__(self, clase: str, partida: Partida | None, al_cambiar,
                 al_quitar) -> None:
        self.clase = clase
        self.partida = partida or Partida(clase=clase)
        self._al_cambiar = al_cambiar

        if clase == CONCEPTO:
            # Desplegable EDITABLE con el catálogo local: elegir en vez de
            # teclear evita el error que más caro sale —un concepto mal escrito
            # no falla al capturarlo, falla a media corrida—, pero sigue
            # permitiendo escribir uno que aún no se ha importado.
            catalogo = conceptos.nombres()
            self.tf_nombre = ft.Dropdown(
                options=[ft.DropdownOption(key=n, text=n) for n in catalogo],
                editable=True, enable_filter=True, expand=True,
                hint_text=("Concepto de pago (elige o escribe)" if catalogo
                           else "Concepto de pago — el catálogo está vacío"),
                value=self.partida.concepto_nombre or None)
            extras: list[ft.Control] = []
        else:
            _, self.tf_nombre = campo_texto(hint="Insumo o servicio", expand=True)
            self.tf_nombre.value = self.partida.insumo_nombre
            _, self.tf_centro = campo_texto(hint="Centro de costos", width=150)
            self.tf_centro.value = self.partida.centro_costos
            _, self.tf_cuenta = campo_texto(hint="Cuenta contable", width=150)
            self.tf_cuenta.value = self.partida.cuenta_contable
            extras = [self.tf_centro, self.tf_cuenta]

        _, self.tf_importe = campo_texto(hint="0.00", width=130,
                                         on_blur=self._importe_editado)
        self.tf_importe.value = (
            f"{self.partida.importe:,.2f}" if self.partida.importe else "")
        self.tf_importe.text_align = ft.TextAlign.RIGHT

        self.control = ft.Row(
            [self.tf_nombre, *extras, self.tf_importe,
             ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18,
                           tooltip="Quitar este renglón",
                           icon_color=ft.Colors.ERROR,
                           on_click=lambda _e: al_quitar(self))],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _importe_editado(self, _e=None) -> None:
        # Se normaliza al salir del campo para que el total de abajo y lo que se
        # ve en el renglón no puedan discrepar.
        valor = parse_importe(self.tf_importe.value)
        self.tf_importe.value = f"{valor:,.2f}" if valor else ""
        self._al_cambiar()

    def recolectar(self) -> Partida:
        p = self.partida
        p.clase = self.clase
        p.importe = parse_importe(self.tf_importe.value)
        if self.clase == CONCEPTO:
            p.concepto_nombre = (self.tf_nombre.value or "").strip()
        else:
            p.insumo_nombre = (self.tf_nombre.value or "").strip()
            p.centro_costos = (self.tf_centro.value or "").strip()
            p.cuenta_contable = (self.tf_cuenta.value or "").strip()
        return p


class _EditorPartidas:
    """Lista de renglones de una clase, con su botón de agregar y su total."""

    def __init__(self, clase: str, titulo: str, ayuda: str, al_cambiar) -> None:
        self.clase = clase
        self._al_cambiar = al_cambiar
        self.renglones: list[_RenglonPartida] = []
        self._lista = ft.Column(spacing=8, tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.txt_total = ft.Text("$0.00", weight=ft.FontWeight.BOLD)
        self.control = ft.Column(
            [ft.Row([ft.Text(titulo, theme_style=ft.TextThemeStyle.LABEL_LARGE,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Icon(ft.Icons.HELP_OUTLINE, size=16, color=GRIS,
                             tooltip=ft.Tooltip(
                                 message=ayuda,
                                 wait_duration=ft.Duration(milliseconds=0)))],
                    spacing=6, tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER),
             self._lista,
             ft.Row([boton_herramienta("Agregar renglón", ft.Icons.ADD,
                                       on_click=lambda _e: self.agregar()),
                     ft.Container(expand=True),
                     ft.Text("Total:", color=GRIS), self.txt_total],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            spacing=10, tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def cargar(self, partidas: list[Partida]) -> None:
        self.renglones = []
        self._lista.controls = []
        for p in partidas:
            self.agregar(p, refrescar=False)
        self.recalcular()

    def agregar(self, partida: Partida | None = None,
                refrescar: bool = True) -> None:
        r = _RenglonPartida(self.clase, partida, self.recalcular, self.quitar)
        self.renglones.append(r)
        self._lista.controls.append(r.control)
        if refrescar:
            self.recalcular()
            self._refrescar()

    def quitar(self, renglon: _RenglonPartida) -> None:
        if renglon in self.renglones:
            self.renglones.remove(renglon)
            self._lista.controls.remove(renglon.control)
        self.recalcular()
        self._refrescar()

    def recolectar(self) -> list[Partida]:
        return [r.recolectar() for r in self.renglones]

    def total(self) -> float:
        return round(sum(parse_importe(r.tf_importe.value)
                         for r in self.renglones), 2)

    def recalcular(self) -> None:
        self.txt_total.value = fmt_importe(self.total())
        self._al_cambiar()

    def _refrescar(self) -> None:
        try:
            self.control.update()
        except Exception:  # noqa: BLE001 — aún no montado en la página
            pass


class CapturaSolicitud:
    """Modal de alta/edición.

    `al_guardar(solicitud, partidas, archivos)` recibe el resultado; guardar en
    la base es responsabilidad de quien lo abre. Los archivos van en la MISMA
    llamada y no aparte: cuelgan de la solicitud, así que solo se pueden
    registrar cuando su fila existe, y quien repinta tiene que hacerlo después
    de eso o la tabla mostrará documentos que ya se adjuntaron como faltantes.
    """

    def __init__(self, app, al_guardar) -> None:
        self.app = app
        self.page = app.page
        self._al_guardar = al_guardar
        self._solicitud = Solicitud()
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        p = self.page
        bl_empresa, self.dd_empresa = campo_opciones(
            "Empresa", NOMBRES_EMPRESAS, flotante=True)
        bl_sucursal, self.dd_sucursal = campo_opciones(
            "Sucursal", SUCURSALES, flotante=True)
        bl_tipo_ben, self.dd_tipo_ben = campo_opciones(
            "Tipo de beneficiario", TIPOS_BENEFICIARIO, flotante=True,
            valor="Acreedor", on_change=self._cambio_tipo_beneficiario)

        # No se pregunta si está dado de alta ni su clave: el robot lo consulta
        # en SIPP al capturar. Pedirlo aquí solo daba oportunidad de
        # equivocarse, y el dato se ignoraba de todos modos.
        bl_nombre, self.tf_nombre = campo_texto(
            "Nombre / descripción del beneficiario", flotante=True)
        bl_rfc, self.tf_rfc = campo_texto("RFC", flotante=True)
        bl_correo, self.tf_correo = campo_texto(
            "Correo electrónico", flotante=True)

        bl_forma, self.dd_forma = campo_opciones(
            "Forma de pago", FORMAS_PAGO, flotante=True,
            valor=FORMA_PAGO_CON_CUENTA, on_change=self._cambio_forma)
        bl_gasto, self.dd_gasto = campo_opciones(
            "Tipo de gasto", TIPOS_GASTO, flotante=True, valor="No Deducible")
        bl_moneda, self.dd_moneda = campo_opciones(
            "Moneda", MONEDAS, flotante=True, valor="Pesos (MXN)")
        self.campo_fecha = CampoFecha(p, "Fecha de pago", flotante=True)

        bl_clabe, self.tf_clabe = campo_texto(
            "CLABE interbancaria", flotante=True, hint="18 dígitos")
        bl_banco, self.tf_banco = campo_texto("Banco", flotante=True)
        bl_titular, self.tf_titular = campo_texto(
            "Nombre de la cuenta", flotante=True)

        bl_desc, self.tf_desc = campo_texto("Descripción", flotante=True)
        self.tf_desc.multiline = True
        self.tf_desc.min_lines = 2
        self.tf_desc.max_lines = 4

        # --- Archivos que SIPP exige y el robot no puede inventar ---
        self._archivos: dict[str, str] = {}
        self.txt_caratula = ft.Text("Sin archivo.", size=12, color=GRIS)
        self.txt_vobo = ft.Text("Sin archivo.", size=12, color=GRIS)
        # Ver el archivo adjunto, no solo su nombre: es como se comprueba que la
        # CLABE capturada sea la que dice la carátula, que es el error que más
        # caro sale de esta pantalla.
        self.btn_ver_caratula = icono_accion(
            ft.Icons.VISIBILITY, "Ver la carátula adjunta",
            lambda _e: self._ver(documentos.TIPO_CARATULA))
        self.btn_ver_caratula.visible = False
        self.btn_ver_vobo = icono_accion(
            ft.Icons.VISIBILITY, "Ver el Vo.Bo. adjunto",
            lambda _e: self._ver(documentos.TIPO_VOBO))
        self.btn_ver_vobo.visible = False
        self.bloque_archivos = ft.Column([
            ft.Row([ft.Icon(ft.Icons.ATTACH_FILE, size=20,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("Documentos", theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            ft.Row([boton_secundario("Carátula bancaria…", ft.Icons.UPLOAD_FILE,
                                     on_click=self._elegir_caratula),
                    self.btn_ver_caratula, self.txt_caratula],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([boton_secundario("Vo.Bo. de Compras…", ft.Icons.UPLOAD_FILE,
                                     on_click=self._elegir_vobo),
                    self.btn_ver_vobo, self.txt_vobo],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text("La carátula es obligatoria al dar de alta un beneficiario "
                    "nuevo con transferencia: SIPP no registra la cuenta sin "
                    "ella. El Vo.Bo. se adjunta después de guardar.",
                    size=12, color=GRIS),
        ], spacing=10, tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        # Los dos editores existen siempre, pero solo se muestra el que aplica:
        # SIPP enseña la pestaña de Conceptos de Pago a Deudores y Acreedores, y
        # la de Insumos & Servicios a Proveedores. Nunca las dos.
        self.ed_conceptos = _EditorPartidas(
            CONCEPTO, "Conceptos de pago",
            "El importe de la solicitud lo calcula SIPP sumando los conceptos "
            "seleccionados: 'Cantidad a Pagar' está deshabilitado en Pago "
            "Extraordinario. Sin conceptos, la solicitud se guardaría en $0. "
            "La lista sale del catálogo (pestaña Conceptos); también puedes "
            "escribir uno que no esté.",
            self._recalcular_total)
        self.ed_insumos = _EditorPartidas(
            INSUMO, "Insumos y servicios",
            "Desglose de lo que se compró, con su centro de costos y cuenta "
            "contable. Es el mecanismo que SIPP usa con los Proveedores, en "
            "lugar de los conceptos de pago.",
            self._recalcular_total)

        self.txt_total = ft.Text("$0.00", size=20, weight=ft.FontWeight.BOLD)
        self.txt_hallazgos = ft.Text(size=12, color=ROJO, visible=False)

        self._bloques_cuenta = [bl_clabe, bl_banco, bl_titular]

        cuerpo = [
            seccion_formulario("Encabezado", ft.Icons.BUSINESS,
                               [bl_empresa, bl_sucursal, bl_tipo_ben,
                                ft.Container(
                                    ft.Text(f"Tipo de pago: "
                                            f"{TIPO_PAGO_SOPORTADO}",
                                            color=GRIS),
                                    padding=ft.Padding.only(top=18))]),
            ft.Divider(),
            seccion_formulario("Beneficiario", ft.Icons.PERSON,
                               [bl_nombre, bl_rfc, bl_correo]),
            ft.Divider(),
            seccion_formulario("Pago", ft.Icons.PAYMENTS,
                               [bl_forma, bl_gasto, self.campo_fecha.control,
                                bl_moneda]),
            seccion_formulario("Cuenta destino", ft.Icons.ACCOUNT_BALANCE,
                               [bl_clabe, bl_banco, bl_titular]),
            ft.Divider(),
            seccion_formulario("Detalle", ft.Icons.NOTES, [bl_desc], columnas=1),
            ft.Divider(),
            self.bloque_archivos,
            ft.Divider(),
            self.ed_conceptos.control,
            ft.Divider(),
            self.ed_insumos.control,
            ft.Divider(),
            ft.Row([ft.Text("Importe de la solicitud:",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE),
                    ft.Container(expand=True), self.txt_total],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.txt_hallazgos,
        ]

        self.modal = Modal(
            p, "Solicitud de pago", ancho=900, alto_cuerpo=560,
            acciones=[
                boton_secundario("Cancelar", on_click=lambda _e: self.modal.cerrar()),
                boton_primario("Guardar", ft.Icons.CHECK, self._guardar),
            ])
        self.modal.cuerpo.controls = cuerpo

    # -------------------------------------------------------- apertura
    def abrir(self, lote_id: str, solicitud: Solicitud | None = None,
              partidas: list[Partida] | None = None) -> None:
        """Abre en alta (sin `solicitud`) o en edición."""
        self._solicitud = solicitud or Solicitud(lote_id=lote_id)
        self._solicitud.lote_id = lote_id
        s = self._solicitud

        self.dd_empresa.value = s.empresa or None
        self.dd_sucursal.value = s.sucursal or None
        self.dd_tipo_ben.value = s.tipo_beneficiario or "Acreedor"
        self.tf_nombre.value = s.beneficiario_nombre
        self.tf_rfc.value = s.beneficiario_rfc
        self.tf_correo.value = s.beneficiario_correo
        self.dd_forma.value = s.forma_pago or FORMA_PAGO_CON_CUENTA
        self.dd_gasto.value = s.tipo_gasto or "No Deducible"
        self.dd_moneda.value = s.moneda or "Pesos (MXN)"
        self.campo_fecha.value = s.fecha_pago
        self.tf_clabe.value = s.cuenta_clabe
        self.tf_banco.value = s.cuenta_banco
        self.tf_titular.value = s.cuenta_titular
        self.tf_desc.value = s.descripcion

        # Los archivos ya asociados a esta solicitud (si se está editando).
        self._archivos = {}
        if solicitud:
            for doc in db.listar_documentos(solicitud_id=solicitud.id):
                if doc.tipo in (documentos.TIPO_CARATULA, documentos.TIPO_VOBO):
                    self._archivos[doc.tipo] = doc.ruta

        todas = list(partidas or [])
        self.ed_conceptos.cargar([p for p in todas if p.clase == CONCEPTO])
        self.ed_insumos.cargar([p for p in todas if p.clase == INSUMO])
        activo = self._editor_activo()
        if not activo.renglones:
            activo.agregar(refrescar=False)

        self.modal.subtitulo = (
            s.beneficiario_nombre if solicitud else "Nueva solicitud")
        self.txt_hallazgos.visible = False
        self._aplicar_visibilidad()
        self._recalcular_total()
        self.modal.abrir()

    # --------------------------------------------------------- archivos
    async def _elegir_archivo(self, tipo: str, titulo: str) -> None:
        seleccion = await self.app.picker.pick_files(
            dialog_title=titulo, allow_multiple=False,
            allowed_extensions=["pdf", "jpg", "jpeg", "png"])
        if not seleccion:
            return
        self._archivos[tipo] = seleccion[0].path
        self._pintar_archivos()
        self.modal.refrescar()

    async def _elegir_caratula(self, _e=None) -> None:
        await self._elegir_archivo(documentos.TIPO_CARATULA,
                                   "Elige la carátula bancaria")

    async def _elegir_vobo(self, _e=None) -> None:
        await self._elegir_archivo(documentos.TIPO_VOBO,
                                   "Elige el Vo.Bo. de Compras")

    def _ver(self, tipo: str) -> None:
        """Abre el documento adjunto en el visor del sistema."""
        ruta = self._archivos.get(tipo, "")
        if not ruta:
            return
        if not os.path.exists(ruta):
            # El archivo se guarda por RUTA: si alguien movió la carpeta de
            # origen, el registro queda apuntando a la nada. Es mejor decirlo
            # que abrir un visor con un error del sistema.
            self.app.avisar(
                f"El archivo ya no está en {ruta}. Vuelve a adjuntarlo.", ROJO)
            return
        self.app.abrir_en_sistema(ruta)

    def _pintar_archivos(self) -> None:
        for tipo, etiqueta, boton in (
                (documentos.TIPO_CARATULA, self.txt_caratula,
                 self.btn_ver_caratula),
                (documentos.TIPO_VOBO, self.txt_vobo, self.btn_ver_vobo)):
            ruta = self._archivos.get(tipo, "")
            etiqueta.value = os.path.basename(ruta) if ruta else "Sin archivo."
            etiqueta.color = VERDE if ruta else GRIS
            boton.visible = bool(ruta)
        # La carátula solo se usa al dar de alta la cuenta de un beneficiario
        # que no exista en SIPP, y eso se sabe al capturar, no aquí. Se avisa
        # sin afirmar que sea obligatoria.
        if (self.dd_forma.value == FORMA_PAGO_CON_CUENTA
                and not self._archivos.get(documentos.TIPO_CARATULA)):
            self.txt_caratula.value = (
                "Sin archivo — hará falta si el beneficiario no está en SIPP.")
            self.txt_caratula.color = NARANJA

    # -------------------------------------------------------- reactividad
    def _cambio_forma(self, _e=None) -> None:
        self._aplicar_visibilidad()
        self.page.update()

    def _cambio_tipo_beneficiario(self, _e=None) -> None:
        self._aplicar_visibilidad()
        self._recalcular_total()
        self.page.update()

    def _clase_activa(self) -> str:
        return clase_desglose(self.dd_tipo_ben.value or "Acreedor")

    def _editor_activo(self) -> "_EditorPartidas":
        return (self.ed_insumos if self._clase_activa() == INSUMO
                else self.ed_conceptos)

    def _aplicar_visibilidad(self) -> None:
        """RFC y correo solo aplican al dar de alta al beneficiario; la cuenta
        solo si se paga por transferencia (SIPP deshabilita el combo de cuentas
        con cualquier otra forma de pago)."""
        transferencia = self.dd_forma.value == FORMA_PAGO_CON_CUENTA
        for bloque in self._bloques_cuenta:
            bloque.visible = transferencia
        # Solo el desglose que SIPP mostrará para este tipo de beneficiario.
        insumos = self._clase_activa() == INSUMO
        self.ed_insumos.control.visible = insumos
        self.ed_conceptos.control.visible = not insumos
        self._pintar_archivos()

    def _recalcular_total(self) -> None:
        self.txt_total.value = fmt_importe(self._editor_activo().total())
        try:
            self.txt_total.update()
        except Exception:  # noqa: BLE001 — aún no montado
            pass

    # -------------------------------------------------------- guardado
    def _recolectar(self) -> tuple[Solicitud, list[Partida]]:
        s = self._solicitud
        s.empresa = self.dd_empresa.value or ""
        s.sucursal = self.dd_sucursal.value or ""
        s.tipo_pago = TIPO_PAGO_SOPORTADO
        s.tipo_beneficiario = self.dd_tipo_ben.value or ""
        s.beneficiario_nombre = (self.tf_nombre.value or "").strip()
        s.beneficiario_rfc = (self.tf_rfc.value or "").strip().upper()
        s.beneficiario_correo = (self.tf_correo.value or "").strip()
        s.forma_pago = self.dd_forma.value or ""
        s.tipo_gasto = self.dd_gasto.value or ""
        s.moneda = self.dd_moneda.value or ""
        s.fecha_pago = self.campo_fecha.value
        s.cuenta_clabe = (self.tf_clabe.value or "").strip()
        s.cuenta_banco = (self.tf_banco.value or "").strip()
        s.cuenta_titular = (self.tf_titular.value or "").strip()
        s.descripcion = (self.tf_desc.value or "").strip()
        s.origen = s.origen or "MANUAL"
        # Solo el desglose que aplica: recolectar los dos dejaría renglones
        # huérfanos del editor oculto, que el validador marcaría como sobrantes.
        partidas = self._editor_activo().recolectar()
        return s, partidas

    def _guardar(self, _e=None) -> None:
        solicitud, partidas = self._recolectar()
        hallazgos = validador.validar(solicitud, partidas)
        # La carátula no es un campo del formulario, así que su falta se agrega
        # aquí. Es AVISO y no error: solo hace falta si el beneficiario resulta
        # no estar dado de alta, y eso lo decide SIPP al capturar.
        if (solicitud.forma_pago == FORMA_PAGO_CON_CUENTA
                and not self._archivos.get(documentos.TIPO_CARATULA)):
            hallazgos.append(validador.Hallazgo(
                "caratula",
                "Sin carátula bancaria: si el beneficiario no está dado de alta "
                "en SIPP, no se podrá registrar su cuenta.",
                validador.AVISO))
        if validador.hay_errores(hallazgos):
            self.txt_hallazgos.value = validador.resumen(hallazgos)
            self.txt_hallazgos.color = ROJO
            self.txt_hallazgos.visible = True
            self.modal.refrescar()
            # El texto vive dentro del cuerpo del modal, que tiene scroll: si
            # el formulario está desplazado, el motivo queda fuera de vista y
            # «Guardar» parece no hacer nada. El aviso flotante sí se ve
            # siempre, y es la diferencia entre «no guardó y sé por qué» y
            # «no guardó y no sé qué pasó».
            self.app.avisar(
                f"No se guardó: {validador.resumen(hallazgos)}", ROJO,
                duracion=9000)
            return
        if hallazgos:  # solo avisos: se guarda, pero el usuario se entera
            self.app.avisar(validador.resumen(hallazgos), NARANJA)
        # Sin errores: la solicitud queda lista para encolarse.
        solicitud.estado = "VALIDADA"
        self.modal.cerrar()
        # Los archivos van JUNTO con la solicitud, no después: quien recibe esto
        # los registra una vez que la fila existe —cuelgan de ella— y repinta
        # ya con todo puesto. Registrarlos aquí, tras `_al_guardar`, dejaba la
        # tabla dibujada con los documentos todavía sin guardar: seguía diciendo
        # «FALTA LA CARÁTULA» hasta que alguien plegaba y desplegaba la fila.
        self._al_guardar(solicitud, partidas, dict(self._archivos))
