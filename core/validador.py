"""Validaciones previas a encolar una solicitud (ESPECIFICACION.md §7).

Se ejecutan al capturar, al importar y al editar en la tabla. Regla de oro: el
modal de asignación masiva usa ESTE mismo validador. No debe existir un camino
corto que lo evite, porque un lote se lanza sin supervisión y un dato malo se
convierte en una solicitud mal capturada en el ERP.

Aquí solo viven las reglas que se pueden comprobar **sin abrir SIPP**. Las que
requieren consultar catálogos en vivo —que el beneficiario exista, que el
concepto esté asignado a la empresa, que el centro de costos sea compatible— las
hace el motor RPA al procesar la fila, y su resultado llega como estado REVISAR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core import catalogos
from core.db import CONCEPTO, INSUMO, Partida, Solicitud, total_desglose

# RFC de persona moral (12) o física (13). SIPP lo revalida en línea con
# `validarRFCRegistrado()`, así que esto solo atrapa lo evidente antes de gastar
# un viaje al portal.
_RFC = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z\d]{3}$", re.IGNORECASE)
_FECHA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_CLABE = re.compile(r"^\d{18}$")

# Severidades. Un ERROR impide encolar; un AVISO deja pasar pero se marca en la
# tabla, porque hay casos legítimos (p. ej. un beneficiario sin correo).
ERROR = "ERROR"
AVISO = "AVISO"


@dataclass
class Hallazgo:
    """Un problema encontrado. `campo` permite resaltar el control en la UI."""

    campo: str
    mensaje: str
    severidad: str = ERROR

    @property
    def es_error(self) -> bool:
        return self.severidad == ERROR


def validar(solicitud: Solicitud, partidas: list[Partida]) -> list[Hallazgo]:
    """Devuelve todos los hallazgos de una solicitud (lista vacía = está lista).

    Se devuelven TODOS de una vez, no el primero: corregir de uno en uno,
    reabriendo el formulario cada vez, es la forma más lenta de capturar.
    """
    h: list[Hallazgo] = []
    conceptos = [p for p in partidas if p.clase == CONCEPTO]
    insumos = [p for p in partidas if p.clase == INSUMO]

    # --- Encabezado ---
    if not solicitud.empresa:
        h.append(Hallazgo("empresa", "Falta la empresa."))
    if not solicitud.sucursal:
        h.append(Hallazgo("sucursal", "Falta la sucursal."))
    if solicitud.tipo_pago != catalogos.TIPO_PAGO_SOPORTADO:
        h.append(Hallazgo(
            "tipo_pago",
            f"La herramienta solo captura '{catalogos.TIPO_PAGO_SOPORTADO}'."))
    if solicitud.tipo_beneficiario not in catalogos.TIPOS_BENEFICIARIO:
        h.append(Hallazgo("tipo_beneficiario",
                          "Falta el tipo de beneficiario."))

    # --- Beneficiario ---
    if not solicitud.beneficiario_nombre.strip():
        h.append(Hallazgo("beneficiario_nombre",
                          "Falta el nombre o descripción del beneficiario."))

    # Si el beneficiario ya está en SIPP no hace falta ni RFC ni correo, y si no
    # está hacen falta los dos. **Quién sabe eso es SIPP, no esta validación**:
    # el robot lo consulta al capturar (ver `_resolver_beneficiario`). Por eso
    # aquí son AVISOS condicionales y no errores: bloquear una solicitud por
    # falta de RFC cuando el beneficiario lleva años dado de alta sería
    # inventarse un requisito. Si resulta que no existe, el motor la marca para
    # revisión con el motivo exacto.
    rfc = solicitud.beneficiario_rfc.strip()
    if not rfc:
        h.append(Hallazgo(
            "beneficiario_rfc",
            "Sin RFC: si el beneficiario no está dado de alta en SIPP, no se "
            "podrá registrar y la solicitud quedará para revisión.", AVISO))
    elif not _RFC.match(rfc):
        # Un RFC presente pero mal formado sí es un error: alguien lo escribió.
        h.append(Hallazgo("beneficiario_rfc",
                          "El RFC no tiene un formato válido."))
    if not solicitud.beneficiario_correo.strip():
        h.append(Hallazgo(
            "beneficiario_correo",
            "Sin correo, SIPP suele rechazar el alta de la cuenta bancaria de "
            "un beneficiario nuevo.", AVISO))

    # --- Forma de pago y cuenta ---
    if solicitud.forma_pago not in catalogos.FORMAS_PAGO:
        h.append(Hallazgo("forma_pago", "Falta la forma de pago."))
    if solicitud.forma_pago == catalogos.FORMA_PAGO_CON_CUENTA:
        clabe = solicitud.cuenta_clabe.strip()
        if not clabe:
            h.append(Hallazgo(
                "cuenta_clabe",
                "Una transferencia necesita la CLABE de la cuenta destino."))
        elif not _CLABE.match(clabe):
            h.append(Hallazgo("cuenta_clabe",
                              "La CLABE debe tener exactamente 18 dígitos."))
        if solicitud.beneficiario_nuevo and not solicitud.cuenta_banco.strip():
            h.append(Hallazgo(
                "cuenta_banco",
                "Al registrar una cuenta nueva hay que indicar el banco."))

    # --- Tipo de gasto ---
    if solicitud.tipo_gasto not in catalogos.TIPOS_GASTO:
        h.append(Hallazgo("tipo_gasto", "Falta el tipo de gasto."))
    elif solicitud.tipo_gasto == catalogos.TIPO_GASTO_EXIGE_XML:
        h.append(Hallazgo(
            "tipo_gasto",
            "Un gasto deducible exige adjuntar el XML del CFDI en SIPP.",
            AVISO))

    # --- Fecha y moneda ---
    if not solicitud.fecha_pago:
        h.append(Hallazgo("fecha_pago", "Falta la fecha de pago."))
    elif not _FECHA.match(solicitud.fecha_pago):
        h.append(Hallazgo("fecha_pago", "La fecha debe ir como DD/MM/AAAA."))
    if solicitud.moneda not in catalogos.MONEDAS:
        h.append(Hallazgo("moneda", "Falta la moneda."))
    elif (solicitud.moneda != catalogos.MONEDA_DEFECTO
          and not solicitud.cuenta_clabe.strip()):
        # La moneda del pago la impone la cuenta bancaria: SIPP deshabilita el
        # combo en cuanto hay cuenta. Sin cuenta en esa moneda, no hay pago.
        h.append(Hallazgo(
            "moneda",
            "Un pago en moneda extranjera exige una cuenta bancaria en esa "
            "misma moneda.", AVISO))

    # --- Desglose ---
    # Cuál de los dos mecanismos aplica lo decide el tipo de beneficiario, y son
    # excluyentes: SIPP muestra la pestaña de Insumos & Servicios a los
    # Proveedores y la de Conceptos de Pago a Deudores y Acreedores. Pedir la
    # que no toca es pedir una pestaña que no existe en pantalla.
    clase = catalogos.clase_desglose(solicitud.tipo_beneficiario)
    esperadas = insumos if clase == INSUMO else conceptos
    sobrantes = conceptos if clase == INSUMO else insumos
    nombre_clase = ("insumos y servicios" if clase == INSUMO
                    else "conceptos de pago")
    nombre_sobrante = ("conceptos de pago" if clase == INSUMO
                       else "insumos y servicios")

    if not esperadas:
        h.append(Hallazgo(
            "partidas",
            f"Un beneficiario de tipo {solicitud.tipo_beneficiario} se desglosa "
            f"en {nombre_clase}, y no hay ningún renglón. El importe de la "
            f"solicitud lo calcula SIPP sumando ese desglose."))
    if sobrantes:
        h.append(Hallazgo(
            "partidas",
            f"Hay {len(sobrantes)} renglón(es) de {nombre_sobrante}, pero con "
            f"{solicitud.tipo_beneficiario} SIPP no muestra esa pestaña: se "
            f"ignorarán al capturar.", AVISO))

    for i, p in enumerate(conceptos, 1):
        if not p.concepto_nombre.strip():
            h.append(Hallazgo("partidas", f"Concepto {i}: falta el nombre."))
        if p.importe <= 0:
            h.append(Hallazgo(
                "partidas", f"Concepto {i}: el importe debe ser mayor que cero."))
    for i, p in enumerate(insumos, 1):
        if not (p.insumo_nombre.strip() or p.insumo_id.strip()):
            h.append(Hallazgo("partidas", f"Insumo {i}: falta el insumo."))
        if p.importe <= 0:
            h.append(Hallazgo(
                "partidas", f"Insumo {i}: el importe debe ser mayor que cero."))

    total = total_desglose(solicitud.tipo_beneficiario, partidas)
    if esperadas and total <= 0:
        h.append(Hallazgo("partidas", f"El total de {nombre_clase} quedó en cero."))

    return h


def hay_errores(hallazgos: list[Hallazgo]) -> bool:
    return any(x.es_error for x in hallazgos)


def resumen(hallazgos: list[Hallazgo]) -> str:
    """Texto de una línea por hallazgo, para el aviso o el tooltip de la fila."""
    return "\n".join(
        f"{'•' if x.es_error else '◦'} {x.mensaje}" for x in hallazgos)
