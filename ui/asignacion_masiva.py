"""Asignación masiva de concepto o insumo a muchas solicitudes.

El botón principal de este modal es **Vista previa**, no Aplicar. Es
deliberado: la operación toca el lote entero de una vez, y lo que rompe no se
nota hasta que el robot falla —o hasta que paga mal—. Ver el antes y el después
de cada fila antes de confirmar es lo que hace segura la función.

La clase del renglón no se elige aquí: la impone el tipo de beneficiario de cada
solicitud, como en el resto de la herramienta. Por eso el formulario habla de
«concepto o insumo» y muestra los campos de centro de costos solo cuando hay
proveedores en el alcance.

La vista previa distingue TRES estados, no dos. Que a una solicitud le falte la
CLABE no impide asignarle su concepto: son cosas distintas, y este modal no
permite corregir la CLABE, así que bloquear por ella solo dejaría al usuario sin
salida. Se aplica, se dice que sigue incompleta, y quien se niega a capturarla
es el motor.
"""

from __future__ import annotations

import flet as ft

from core import asignacion, catalogos, conceptos, db
from core.empresas import NOMBRES_EMPRESAS
from ui.comun import GRIS, NARANJA, ROJO, VERDE, fmt_importe
from ui.componentes import (CampoFecha, Modal, boton_primario, boton_secundario,
                            campo_opciones, campo_texto, tarjeta_seccion)
from ui.tabla_responsiva import (DER, IZQ, ColumnaTabla, FilaDatos,
                                 TablaResponsiva)

_COLUMNAS = [
    ColumnaTabla("", 5),
    ColumnaTabla("Beneficiario", 25, IZQ),
    ColumnaTabla("Renglones", 12),
    ColumnaTabla("Antes", 14, DER),
    ColumnaTabla("Después", 14, DER),
    ColumnaTabla("Detalle", 30, IZQ),
]

_MODOS = {
    "Agregar como renglón adicional": asignacion.AGREGAR,
    "Reemplazar los renglones existentes": asignacion.REEMPLAZAR,
}


def _clave(diccionario: dict, etiqueta: str, defecto: str) -> str:
    return diccionario.get(etiqueta, defecto)


class AsignacionMasiva:
    """Modal de asignación masiva. `al_aplicar(n)` recibe cuántas cambiaron."""

    def __init__(self, app, al_aplicar) -> None:
        self.app = app
        self.page = app.page
        self._al_aplicar = al_aplicar
        self._lote_id = ""
        self._seleccionadas: set[str] = set()
        self._plan: asignacion.Plan | None = None
        self._hay_insumos = False
        self._construir()

    # ------------------------------------------------------------ UI
    def _construir(self) -> None:
        bl_modo, self.dd_modo = campo_opciones(
            "Qué hacer con lo que ya tienen", list(_MODOS),
            valor="Agregar como renglón adicional", on_change=self._invalidar)
        # A quién se aplica no se elige: lo dice la selección con la que se
        # abrió el modal (ver `abrir`). Un selector aparte permitía abrir con
        # filas marcadas y aplicar a todo el lote sin querer.
        self.txt_alcance = ft.Text("", size=12, color=GRIS)

        catalogo = conceptos.nombres()
        self.dd_nombre = ft.Dropdown(
            label="Concepto de pago o insumo",
            options=[ft.DropdownOption(key=n, text=n) for n in catalogo],
            editable=True, enable_filter=True, expand=True,
            hint_text=("Elige del catálogo o escribe uno" if catalogo
                       else "El catálogo está vacío: escríbelo"),
            on_select=self._invalidar)

        bl_cc, self.tf_centro = campo_texto(
            "Centro de costos", flotante=True,
            hint="Obligatorio para insumos")
        bl_cta, self.tf_cuenta = campo_texto(
            "Cuenta contable", flotante=True,
            hint="Obligatorio para insumos")
        # Centro de costos y cuenta contable son campos del renglón de INSUMO,
        # que solo le toca a un Proveedor. Con un alcance sin proveedores no
        # tienen dónde escribirse, así que se esconden en vez de quedarse ahí
        # pidiendo datos que nadie va a usar.
        self.bloque_insumo = ft.Row([bl_cc, bl_cta], spacing=16, visible=False)
        self.txt_insumo = ft.Text(
            "", size=12, color=NARANJA, visible=False)

        # --- Datos de la solicitud (cabecera), no del desglose.
        bl_empresa, self.dd_empresa = campo_opciones(
            "Empresa", NOMBRES_EMPRESAS, width=250, on_change=self._invalidar)
        bl_sucursal, self.dd_sucursal = campo_opciones(
            "Sucursal", catalogos.SUCURSALES, width=210,
            on_change=self._invalidar)
        self.campo_fecha = CampoFecha(self.page, "Fecha de pago",
                                      on_change=self._invalidar)

        # El importe NO se fija aquí: cada solicitud lleva el suyo y no hay un
        # criterio común que sirva para el lote. El renglón nace en cero y se
        # captura después; la solicitud queda marcada como incompleta hasta
        # entonces, y el motor no la captura así.
        self.txt_importe = ft.Text(
            "El importe de cada renglón se captura después, uno por uno: aquí "
            "solo se deja asignado el concepto.", size=12, color=GRIS)

        formulario = tarjeta_seccion(ft.Column([
            ft.Row([ft.Icon(ft.Icons.PLAYLIST_ADD_CHECK,
                            color=ft.Colors.PRIMARY_CONTAINER),
                    ft.Text("Qué asignar y a quiénes",
                            theme_style=ft.TextThemeStyle.LABEL_LARGE,
                            color=ft.Colors.PRIMARY_CONTAINER)],
                   spacing=8, tight=True),
            self.txt_alcance,
            ft.Row([bl_modo], spacing=16),
            self.dd_nombre,
            ft.Text("A un Proveedor le corresponde un insumo; a un Deudor o "
                    "Acreedor, un concepto de pago. La herramienta lo decide "
                    "por cada solicitud.", size=12, color=GRIS),
            self.txt_insumo,
            self.bloque_insumo,
            self.txt_importe,
            ft.Divider(),
            ft.Text("Datos de la solicitud (opcional)",
                    theme_style=ft.TextThemeStyle.LABEL_LARGE,
                    color=ft.Colors.PRIMARY_CONTAINER),
            ft.Text("Lo que llenes aquí SUSTITUYE lo que tengan las solicitudes "
                    "del alcance. Déjalo vacío para no tocarlo.",
                    size=12, color=GRIS),
            ft.Row([bl_empresa, bl_sucursal,
                    ft.Container(self.campo_fecha.control, width=200)],
                   spacing=16, wrap=True),
        ], spacing=14, tight=True))

        # Vista previa.
        self.txt_resumen = ft.Text("", theme_style=ft.TextThemeStyle.BODY_LARGE)
        self.txt_omitidas = ft.Text("", size=12, color=GRIS, visible=False)
        self.tabla = TablaResponsiva(self.page, _COLUMNAS, ancho_inicial=880)
        self.previa = ft.Column(
            [ft.Row([ft.Icon(ft.Icons.FACT_CHECK,
                             color=ft.Colors.PRIMARY_CONTAINER),
                     ft.Text("Antes y después",
                             theme_style=ft.TextThemeStyle.LABEL_LARGE,
                             color=ft.Colors.PRIMARY_CONTAINER)],
                    spacing=8, tight=True),
             self.txt_resumen, self.txt_omitidas,
             ft.Container(self.tabla.control, height=240)],
            spacing=10, tight=True, visible=False,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.btn_previa = boton_primario(
            "Vista previa", ft.Icons.VISIBILITY, self._calcular)
        self.btn_aplicar = boton_secundario(
            "Aplicar", ft.Icons.CHECK, on_click=self._aplicar)
        self.btn_aplicar.disabled = True

        self.modal = Modal(
            self.page, "Asignación masiva",
            subtitulo="concepto o insumo para todo el lote",
            ancho=940, alto_cuerpo=500,
            acciones=[
                boton_secundario("Cerrar",
                                 on_click=lambda _e: self.modal.cerrar()),
                self.btn_aplicar, self.btn_previa,
            ])
        self.modal.cuerpo.controls = [formulario, self.previa]

    # -------------------------------------------------------- apertura
    def abrir(self, lote_id: str, seleccionadas: set[str]) -> None:
        self._lote_id = lote_id
        self._seleccionadas = set(seleccionadas)
        self._plan = None
        self.previa.visible = False
        self.btn_aplicar.disabled = True
        self.btn_aplicar.text = "Aplicar"
        # Se dice a cuántas va a aplicar, en vez de ofrecer elegirlo: la
        # decisión ya se tomó al marcar (o no marcar) filas en la tabla.
        self.txt_alcance.value = (
            f"Se aplicará a las {len(seleccionadas)} solicitud(es) que "
            f"seleccionaste." if seleccionadas
            else "Se aplicará a TODAS las solicitudes del lote. Para acotarlo, "
                 "cierra y marca antes las filas que quieras.")
        # Los datos de cabecera arrancan vacíos SIEMPRE: son sobrescritura, y
        # heredar el valor de la vez pasada volvería a pisar el lote sin que
        # nadie lo haya pedido esta vez.
        self.dd_empresa.value = None
        self.dd_sucursal.value = None
        self.campo_fecha.value = ""
        self._refrescar_campos_insumo()
        self.modal.abrir()

    def _alcance(self) -> str:
        """A quién se aplica: lo que venía seleccionado o, si no había, el lote."""
        return (asignacion.SELECCIONADAS if self._seleccionadas
                else asignacion.TODAS)

    def _alcanzadas(self) -> list:
        """Las solicitudes que se tocarían, para saber qué campos pedir."""
        if not self._lote_id:
            return []
        todas = db.listar_solicitudes(self._lote_id)
        if self._alcance() == asignacion.SELECCIONADAS:
            return [s for s in todas if s.id in self._seleccionadas]
        return todas

    def _refrescar_campos_insumo(self) -> None:
        """Muestra centro de costos y cuenta contable solo si hay insumos.

        Un concepto de pago no los lleva, así que con un alcance de puros
        Acreedores o Deudores pedirlos sería pedir datos que no se escriben en
        ningún lado. Con proveedores dentro sí son obligatorios: el renglón de
        insumo va a SIPP con esos dos campos.
        """
        proveedores = [s for s in self._alcanzadas()
                       if catalogos.clase_desglose(s.tipo_beneficiario) == db.INSUMO]
        self._hay_insumos = bool(proveedores)
        self.bloque_insumo.visible = self._hay_insumos
        self.txt_insumo.visible = self._hay_insumos
        if self._hay_insumos:
            self.txt_insumo.value = (
                f"{len(proveedores)} de las solicitudes del alcance son de "
                f"Proveedor: su renglón es un insumo y necesita centro de "
                f"costos y cuenta contable.")

    def _invalidar(self, _e=None) -> None:
        """Cambiar los criterios invalida la vista previa anterior.

        Si no, se podría aplicar un plan calculado con otros parámetros: lo que
        se ve dejaría de ser lo que se hace, que es justo lo que este modal
        existe para evitar.
        """
        self._plan = None
        self.previa.visible = False
        self.btn_aplicar.disabled = True
        self._refrescar_campos_insumo()
        self.modal.refrescar()

    # ---------------------------------------------------- vista previa
    def _calcular(self, _e=None) -> None:
        nombre = (self.dd_nombre.value or "").strip()
        empresa = self.dd_empresa.value or ""
        sucursal = self.dd_sucursal.value or ""
        fecha = self.campo_fecha.value or ""
        if not nombre and not (empresa or sucursal or fecha):
            self.app.avisar(
                "Indica qué asignar: un concepto o insumo, o alguno de los "
                "datos de la solicitud.", NARANJA)
            return
        # Solo se exigen cuando de verdad se va a escribir un renglón de
        # insumo: con un alcance sin proveedores estos campos ni se muestran.
        if nombre and self._hay_insumos and not (
                (self.tf_centro.value or "").strip()
                and (self.tf_cuenta.value or "").strip()):
            self.app.avisar(
                "El alcance incluye Proveedores: su renglón es un insumo y "
                "necesita centro de costos y cuenta contable.", NARANJA)
            return
        plan = asignacion.calcular(
            self._lote_id,
            alcance=self._alcance(),
            seleccionadas=self._seleccionadas,
            modo=_clave(_MODOS, self.dd_modo.value, asignacion.AGREGAR),
            nombre=nombre,
            centro_costos=(self.tf_centro.value or "").strip(),
            cuenta_contable=(self.tf_cuenta.value or "").strip(),
            criterio_importe=asignacion.EN_BLANCO,
            empresa=empresa, sucursal=sucursal, fecha_pago=fecha)
        self._plan = plan

        if plan.error and not plan.cambios:
            self.previa.visible = False
            self.btn_aplicar.disabled = True
            self.app.avisar(plan.error, NARANJA)
            self.modal.refrescar()
            return

        filas = []
        for c in plan.cambios:
            # Tres estados, no dos: no se puede asignar · se asigna pero la
            # solicitud sigue incompleta · queda lista. Mezclar los dos últimos
            # haría creer que hay que arreglar algo aquí para poder aplicar.
            if not c.valido:
                icono, color = ft.Icons.ERROR, ROJO
                detalle = c.bloqueantes[0].mensaje
            elif c.pendientes:
                icono, color = ft.Icons.PENDING_ACTIONS, NARANJA
                detalle = f"Se asigna, pero falta: {c.pendientes[0].mensaje}"
            elif c.aviso:
                icono, color, detalle = ft.Icons.WARNING_AMBER, NARANJA, c.aviso
            else:
                icono, color, detalle = ft.Icons.CHECK_CIRCLE, VERDE, "Lista."
            filas.append(FilaDatos([
                ft.Icon(icono, size=18, color=color,
                        tooltip="\n".join(h.mensaje for h in c.hallazgos) or None),
                c.solicitud.beneficiario_nombre or "—",
                f"{len(c.partidas_antes)} → {len(c.partidas_despues)}",
                fmt_importe(c.total_antes),
                fmt_importe(c.total_despues),
                detalle,
            ]))
        self.tabla.set_contenido(filas)

        validos = plan.validos
        partes = [f"{len(plan.cambios)} solicitud(es) afectada(s)",
                  f"{len(plan.completos)} quedarían listas"]
        if plan.incompletos:
            partes.append(f"{len(plan.incompletos)} se asignan pero les falta "
                          f"algo fuera del desglose")
        bloqueadas = len(plan.cambios) - len(validos)
        if bloqueadas:
            partes.append(f"{bloqueadas} no se pueden asignar")
        self.txt_resumen.value = " · ".join(partes)
        self.txt_resumen.color = (
            ROJO if not validos else NARANJA if plan.incompletos else VERDE)
        if plan.omitidas:
            self.txt_omitidas.value = (
                "No se tocan: " + ", ".join(plan.omitidas[:4])
                + (f" y {len(plan.omitidas) - 4} más"
                   if len(plan.omitidas) > 4 else ""))
            self.txt_omitidas.visible = True
        else:
            self.txt_omitidas.visible = False
        self.previa.visible = True
        self.btn_aplicar.text = f"Aplicar a {len(validos)}"
        self.btn_aplicar.disabled = not validos
        self.modal.refrescar()

    # ------------------------------------------------------- aplicar
    def _aplicar(self, _e=None) -> None:
        if not self._plan:
            return
        incompletas = len(self._plan.incompletos)
        resultado = asignacion.aplicar(self._lote_id, self._plan)
        self.modal.cerrar()
        if callable(self._al_aplicar):
            self._al_aplicar(resultado["aplicadas"])
        mensaje = f"{resultado['aplicadas']} solicitud(es) actualizadas."
        # Se dice aquí porque el modal se cierra: si no, el usuario se queda
        # creyendo que el lote quedó listo para correr.
        if incompletas:
            mensaje += (f" {incompletas} siguen incompletas por otros campos y "
                        f"el robot no las capturará hasta corregirlas.")
        # Este aviso es el ÚNICO lugar desde donde se revierte la operación
        # completa: en la tabla, deshacer actúa sobre una solicitud a la vez.
        # Por eso dura más de lo normal en pantalla y lo dice explícitamente.
        mensaje += (" Deshazlo aquí para revertir todo, o desde el ícono ↺ de "
                    "cada fila para revertir solo esa.")
        self.app.avisar(
            mensaje, NARANJA if incompletas else VERDE,
            accion="Deshacer", on_accion=self._deshacer_desde_aviso,
            duracion=12000)

    def _deshacer_desde_aviso(self, _e=None) -> None:
        if asignacion.deshacer(self._lote_id):
            if callable(self._al_aplicar):
                self._al_aplicar(0)
            self.app.avisar("Asignación deshecha.", VERDE)
