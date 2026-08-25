"""Reporte final del lote: qué quedó registrado, qué falló y qué hay que revisar.

Al terminar un lote la herramienta decía «12 capturada(s) · 2 para revisar» en
un aviso que se desvanece solo. Con lotes de decenas de solicitudes eso no
alcanza: lo que hace falta saber es CUÁLES, y qué pasó con cada una, en el
momento en que el trabajo termina y todavía se puede hacer algo al respecto.

El desglose va arriba y las solicitudes en pestañas por resultado, porque son
tres públicos distintos: lo registrado se revisa por encima, lo que falló se
atiende ahora y lo que quedó para revisar se corrige antes del siguiente lote.
"""

from __future__ import annotations

import flet as ft

from core.catalogos import ETIQUETA_ESTADO
from ui.comun import GRIS, NARANJA, ROJO, VERDE, color_fila, fmt_importe
from ui.componentes import (GAP_MD, GAP_SM, Modal, Pestanas, boton_primario,
                            boton_secundario)
from ui.tabla_responsiva import (DER, IZQ, ColumnaTabla, FilaDatos,
                                 TablaResponsiva)

# Los tres resultados que le importan a quien lanzó el lote, más el cajón de lo
# que no se llegó a intentar. Ese cuarto grupo solo aparece cuando tiene algo:
# en un lote que terminó entero sobra, pero cuando se detuvo a la mitad,
# callarlo haría creer que todo se procesó.
_GRUPOS: list[tuple] = [
    ("registradas", "Registradas", ft.Icons.CHECK_CIRCLE_OUTLINE, VERDE,
     ("GUARDADA", "ENVIADA_AUTORIZAR")),
    ("error", "Con errores", ft.Icons.ERROR_OUTLINE, ROJO, ("ERROR",)),
    # LLENADA va aquí y no en «sin procesar»: el formulario SÍ se llenó, pero
    # quedó sin guardar esperando a que alguien lo revise. Es trabajo hecho a
    # medias que necesita una persona, igual que un REVISAR.
    ("revisar", "Para revisar", ft.Icons.FLAG_OUTLINED, NARANJA,
     ("REVISAR", "LLENADA")),
    ("sin_procesar", "Sin procesar", ft.Icons.SCHEDULE, GRIS,
     ("PENDIENTE", "VALIDADA", "EN_CAPTURA", "OMITIDA")),
]

# Las tres del resultado se muestran aunque estén vacías: un «Con errores (0)»
# es justo lo que se quiere ver de un vistazo.
_SIEMPRE = ("registradas", "error", "revisar")

# Un solo juego de columnas para las cuatro pestañas: cambiar de pestaña no
# debe cambiar la forma de la tabla, o cada salto obliga a releer los títulos.
_COLUMNAS = [
    ColumnaTabla("Beneficiario", 26, IZQ),
    ColumnaTabla("Descripción", 20, IZQ),
    ColumnaTabla("Importe", 12, DER),
    ColumnaTabla("Folio SIPP", 11),
    ColumnaTabla("Resultado", 31, IZQ),
]

_ANCHO = 1040
_ALTO_TABLA = 320

# Un grupo vacío no es un hueco: dice algo, y dice algo distinto en cada caso.
_VACIO = {
    "registradas": "Ninguna solicitud llegó a registrarse en SIPP.",
    "error": "Ninguna solicitud falló.",
    "revisar": "Ninguna solicitud quedó pendiente de revisión.",
    "sin_procesar": "Todas las solicitudes del lote se procesaron.",
}


def agrupar(solicitudes: list) -> dict[str, list]:
    """Reparte las solicitudes por resultado, en el orden de `_GRUPOS`.

    Un estado que no encaje en ningún grupo cae en «sin procesar» en vez de
    desaparecer: el desglose tiene que sumar el total del lote, siempre.
    """
    grupos: dict[str, list] = {clave: [] for clave, *_ in _GRUPOS}
    for s in solicitudes:
        destino = "sin_procesar"
        for clave, _texto, _icono, _color, estados in _GRUPOS:
            if s.estado in estados:
                destino = clave
                break
        grupos[destino].append(s)
    return grupos


def _resultado(solicitud) -> str:
    """Qué hay que leer de esta solicitud: el motivo si lo hay, o su estado."""
    motivo = " ".join((solicitud.error_msg or "").split())
    return motivo or ETIQUETA_ESTADO.get(solicitud.estado, solicitud.estado)


class ReporteLote:
    """Modal con el resultado del lote, en pestañas por resultado."""

    def __init__(self, page, solicitudes: list, *, nombre_lote: str = "",
                 cancelado: bool = False, al_ver_bitacora=None) -> None:
        self.page = page
        self._grupos = agrupar(solicitudes)
        self._total = len(solicitudes)
        self._al_ver_bitacora = al_ver_bitacora

        visibles = [g for g in _GRUPOS
                    if g[0] in _SIEMPRE or self._grupos[g[0]]]
        self.pestanas = Pestanas(
            [(clave, texto, icono)
             for clave, texto, icono, _color, _estados in visibles],
            al_cambiar=self._cambiar_pestana)
        for clave, *_ in visibles:
            self.pestanas.set_conteo(clave, len(self._grupos[clave]))

        self.tabla = TablaResponsiva(self.page, _COLUMNAS,
                                     ancho_inicial=_ANCHO - 80)
        self.txt_vacio = ft.Text("", color=GRIS, visible=False,
                                 text_align=ft.TextAlign.CENTER)

        acciones = [boton_primario("Cerrar", on_click=lambda _e: self.cerrar())]
        if callable(al_ver_bitacora):
            acciones.insert(0, boton_secundario(
                "Ver bitácora", ft.Icons.HISTORY,
                on_click=lambda _e: self._ir_a_bitacora()))

        self.modal = Modal(
            self.page, "Resultado del lote", subtitulo=nombre_lote,
            ancho=_ANCHO, alto_cuerpo=520, acciones=acciones)
        self.modal.cuerpo.controls = [
            self._encabezado(cancelado, visibles),
            self.pestanas.control,
            ft.Container(ft.Column([self.tabla.control, self.txt_vacio],
                                   spacing=GAP_MD, tight=True),
                         height=_ALTO_TABLA),
        ]
        self._pintar(self.pestanas.activa)

    # ------------------------------------------------------------ encabezado
    def _encabezado(self, cancelado: bool, visibles: list) -> ft.Control:
        """El total a la izquierda y el desglose por resultado a su derecha."""
        total = ft.Column(
            [ft.Text(str(self._total), size=34, weight=ft.FontWeight.BOLD,
                     color=ft.Colors.ON_SURFACE),
             ft.Text("solicitudes en el lote",
                     theme_style=ft.TextThemeStyle.LABEL_LARGE, color=GRIS)],
            spacing=0, tight=True)

        desglose = ft.Row(
            [self._chip(texto, icono, color, len(self._grupos[clave]))
             for clave, texto, icono, color, _estados in visibles],
            spacing=GAP_MD, wrap=True, expand=True,
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.CENTER)

        tarjeta = ft.Container(
            ft.Row([total, desglose], spacing=GAP_MD,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=GAP_MD, border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT))

        if not cancelado:
            return tarjeta
        # Sin esto, un lote detenido a la mitad se lee igual que uno completo.
        aviso = ft.Container(
            ft.Row([ft.Icon(ft.Icons.PAUSE_CIRCLE_OUTLINE, color=NARANJA,
                            size=18),
                    ft.Text("El lote se detuvo antes de terminar: lo que "
                            "aparece como «sin procesar» no se intentó.",
                            color=NARANJA, size=12, expand=True)],
                   spacing=GAP_SM),
            padding=ft.Padding.symmetric(horizontal=GAP_MD, vertical=GAP_SM),
            border_radius=8, bgcolor=ft.Colors.with_opacity(0.10, NARANJA))
        return ft.Column([tarjeta, aviso], spacing=GAP_SM, tight=True,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    @staticmethod
    def _chip(texto: str, icono: str, color: str, n: int) -> ft.Control:
        return ft.Container(
            ft.Row([ft.Icon(icono, color=color, size=18),
                    ft.Text(str(n), size=20, weight=ft.FontWeight.BOLD,
                            color=color),
                    ft.Text(texto, size=12, color=GRIS)],
                   spacing=GAP_SM, tight=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=GAP_MD, vertical=GAP_SM),
            border_radius=8, bgcolor=ft.Colors.with_opacity(0.08, color))

    # ---------------------------------------------------------------- cuerpo
    def _cambiar_pestana(self, clave: str) -> None:
        self._pintar(clave)
        try:
            self.modal.refrescar()
        except Exception:  # noqa: BLE001 — el modal ya se cerró
            pass

    def _pintar(self, clave: str) -> None:
        solicitudes = self._grupos.get(clave, [])
        self.tabla.set_contenido([
            FilaDatos(celdas=[s.beneficiario_nombre or "(sin beneficiario)",
                              s.descripcion or "",
                              fmt_importe(s.importe_total),
                              s.folio_sipp or "—",
                              _resultado(s)],
                      bgcolor=color_fila(s.estado))
            for s in solicitudes])
        self.tabla.control.visible = bool(solicitudes)
        self.txt_vacio.visible = not solicitudes
        self.txt_vacio.value = _VACIO.get(clave, "No hay solicitudes aquí.")

    # -------------------------------------------------------------- acciones
    def _ir_a_bitacora(self) -> None:
        self.cerrar()
        if callable(self._al_ver_bitacora):
            self._al_ver_bitacora()

    def abrir(self) -> None:
        self.modal.abrir()

    def cerrar(self) -> None:
        self.modal.cerrar()
