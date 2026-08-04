"""Documentos: ingesta de CFDI, carátulas y Vo.Bo.

El motor de esta pantalla —`core/adaptadores/cfdi.py`— ya está escrito y
probado: lee CFDI 3.3 y 4.0, empareja su PDF por UUID y convierte los conceptos
del comprobante en partidas. Lo que falta es la pantalla que lo opere.

La ingesta desde **Excel** sí está disponible, pero desde otro lado: es el botón
«Carga masiva» de la pantalla de Solicitudes, porque ahí es donde el usuario
está trabajando cuando la necesita.
"""

from __future__ import annotations

import flet as ft

from ui import comun


class SeccionDocumentos:
    """Carga y emparejamiento de los documentos que alimentan un lote."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self.contenido = comun.placeholder(
            "Documentos",
            "Para cargar solicitudes desde Excel usa «Carga masiva», en la "
            "pantalla de Solicitudes. Esta pantalla es para los documentos: "
            "CFDI, carátulas bancarias y Vo.Bo.",
            ft.Icons.FOLDER_OPEN,
            pendientes=[
                "Arrastrar archivos, elegirlos o recorrer una carpeta completa.",
                "Alta de solicitudes desde CFDI (el lector ya está listo: XML "
                "como fuente de verdad y PDF emparejado por UUID).",
                "Emparejamiento de carátulas bancarias y Vo.Bo. por nombre de "
                "la persona, tolerando acentos y espacios.",
                "Hash SHA-256 por archivo para no reprocesar el mismo documento.",
            ])

    def _on_resize(self, _e=None) -> None:
        """Sin nada que reacomodar todavía; presente por el contrato de
        pantalla que registra el shell."""
