"""Plantilla de Excel para la carga masiva: contrato de columnas y generador.

Este módulo es la **fuente única del formato de intercambio**. De aquí salen tanto
el archivo que el usuario descarga como las reglas con que `adaptadores/excel.py`
lo vuelve a leer, así que plantilla y lector no pueden desalinearse.

Decisiones del formato, y por qué:

- **Una fila = una solicitud.** El caso real es «un pago por persona con un
  concepto», y obligar a dos hojas para eso volvería tedioso lo común.
- **Hoja `Partidas` opcional**, ligada por `Referencia`, para las solicitudes que
  sí llevan varios conceptos o insumos con su centro de costos. Si no se usa, el
  concepto e importe de la hoja principal bastan.
- **Los catálogos van en su propia hoja**, y las columnas los validan con listas
  desplegables. Un valor mal escrito ahí es un error que solo aparecería a media
  captura en SIPP, cuando el desplegable no encuentre la opción.
- **Las fechas se piden como texto `DD/MM/AAAA`.** Excel reinterpreta las fechas
  según la configuración regional del equipo, y un `03/04` que en una máquina es
  3 de abril en otra es 4 de marzo. Como texto no hay ambigüedad, y el lector
  también acepta fechas reales de Excel por si alguien las teclea así.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from core import catalogos
from core.empresas import NOMBRES_EMPRESAS

NOMBRE_ARCHIVO = "Plantilla solicitudes de pago.xlsx"

HOJA_SOLICITUDES = "Solicitudes"
HOJA_PARTIDAS = "Partidas"
HOJA_CATALOGOS = "Catálogos"
HOJA_INSTRUCCIONES = "Instrucciones"


@dataclass
class Campo:
    """Una columna del formato.

    `clave` es el atributo de `core.db.Solicitud` que alimenta; `sinonimos` son
    los encabezados alternativos que el lector acepta, porque las plantillas que
    ya circulan en el área no usan exactamente estos nombres.
    """

    clave: str
    etiqueta: str
    requerido: bool = False
    ayuda: str = ""
    catalogo: str = ""          # nombre de la lista en la hoja de catálogos
    ancho: int = 22
    ejemplo: str = ""
    sinonimos: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Hoja principal
# --------------------------------------------------------------------------- #
CAMPOS: list[Campo] = [
    Campo("referencia", "Referencia", ancho=14,
          ayuda="Identificador libre de la fila. Solo hace falta si vas a "
                "desglosarla en la hoja Partidas.",
          ejemplo="SP-001", sinonimos=["ref", "id", "folio interno"]),
    Campo("empresa", "Empresa", requerido=True, catalogo="empresas", ancho=30,
          ayuda="Empresa del Grupo Petroil que paga.",
          ejemplo="Aske"),
    Campo("sucursal", "Sucursal", requerido=True, catalogo="sucursales",
          ayuda="Plaza a la que se carga el gasto.",
          ejemplo="Corporativo", sinonimos=["plaza"]),
    Campo("tipo_beneficiario", "Tipo de beneficiario", requerido=True,
          catalogo="beneficiarios",
          ayuda="Proveedor, Deudor o Acreedor, según cómo esté dado de alta.",
          ejemplo="Acreedor", sinonimos=["tipo de pago extraordinario"]),
    # No se pregunta si el beneficiario ya está dado de alta ni cuál es su
    # clave: **lo averigua el robot** consultando el catálogo de SIPP antes de
    # capturar (ESPECIFICACION.md §8). Nadie que llena un Excel puede saberlo,
    # y pedírselo solo servía para que se equivocara en los dos sentidos: dar de
    # alta un duplicado, o mandar a buscar a alguien que no existe.
    Campo("beneficiario_nombre", "Nombre del beneficiario", requerido=True,
          ancho=34,
          ayuda="Nombre o descripción exacta con la que quedará en SIPP.",
          ejemplo="JUAN PEREZ LOPEZ",
          sinonimos=["beneficiario", "nombre", "razon social", "razón social",
                     "descripcion del acreedor", "ex-colaborador",
                     "ex-colaborador (descripción)", "nombre de cuenta"]),
    Campo("beneficiario_rfc", "RFC", ancho=16,
          ayuda="Obligatorio si el beneficiario es nuevo.",
          ejemplo="PELJ800101AB1"),
    Campo("beneficiario_correo", "Correo electrónico", ancho=28,
          ayuda="Se registra al dar de alta la cuenta bancaria.",
          ejemplo="jperez@ejemplo.mx",
          sinonimos=["correo", "email", "correo electronico"]),
    Campo("forma_pago", "Forma de pago", requerido=True, catalogo="formas_pago",
          ayuda="Con Transferencia hay que llenar el bloque de la cuenta.",
          ejemplo="Transferencia", sinonimos=["metodo de pago", "método de pago"]),
    Campo("tipo_gasto", "Tipo de gasto", requerido=True, catalogo="tipos_gasto",
          ayuda="Deducible exige adjuntar el XML del CFDI en SIPP.",
          ejemplo="No Deducible"),
    Campo("cuenta_banco", "Banco", ancho=20,
          ayuda="Banco de la cuenta destino. Obligatorio si la cuenta es nueva.",
          ejemplo="BBVA", sinonimos=["bancos"]),
    Campo("cuenta_clabe", "CLABE", ancho=22,
          ayuda="18 dígitos. Obligatoria para Transferencia. Déjala como TEXTO: "
                "si Excel la vuelve número, pierde los ceros de la izquierda.",
          ejemplo="012345678901234568",
          sinonimos=["clabe interbancaria", "clave interbancaria", "cuenta"]),
    Campo("cuenta_titular", "Nombre de la cuenta", ancho=30,
          ayuda="A nombre de quién está la cuenta. Si se omite, se usa el "
                "nombre del beneficiario.",
          ejemplo="JUAN PEREZ LOPEZ", sinonimos=["titular"]),
    Campo("fecha_pago", "Fecha de pago", requerido=True, ancho=16,
          ayuda="Formato DD/MM/AAAA.",
          ejemplo="15/08/2026", sinonimos=["fecha"]),
    Campo("moneda", "Moneda", catalogo="monedas", ancho=16,
          ayuda="Si se omite, Pesos (MXN). La moneda real la impone la cuenta "
                "bancaria elegida.",
          ejemplo="Pesos (MXN)"),
    Campo("descripcion", "Descripción", ancho=40,
          ayuda="Texto que verá quien autorice la solicitud.",
          ejemplo="Pago de finiquito", sinonimos=["concepto solicitud", "detalle"]),
    Campo("concepto_nombre", "Concepto de pago o insumo", requerido=True,
          catalogo="conceptos", ancho=30,
          ayuda="Con Deudor o Acreedor: el concepto de pago, que debe existir "
                "en el catálogo de ESA empresa (el robot lo busca por palabras: "
                "«PAGO PTU» encuentra también «PAGO DE PTU»). Con Proveedor: el "
                "insumo o servicio, porque SIPP le muestra esa pestaña y no la "
                "de conceptos.",
          ejemplo="PAGO PTU", sinonimos=["concepto", "concepto de pago",
                                         "insumo", "insumo o servicio"]),
    Campo("importe", "Importe", requerido=True, ancho=16,
          ayuda="Importe del concepto. Es lo que SIPP tomará como total.",
          ejemplo="12500.00", sinonimos=["monto", "total"]),
]

# --------------------------------------------------------------------------- #
#  Hoja de partidas (desglose opcional)
# --------------------------------------------------------------------------- #
CAMPOS_PARTIDA: list[Campo] = [
    Campo("referencia", "Referencia", requerido=True, ancho=14,
          ayuda="Debe coincidir con la Referencia de la hoja Solicitudes.",
          ejemplo="SP-001"),
    Campo("clase", "Tipo de renglón", requerido=True, catalogo="clases",
          ancho=18,
          ayuda="Concepto = imputación del gasto (suma al total). "
                "Insumo = qué se compró (no suma).",
          ejemplo="Concepto"),
    Campo("nombre", "Concepto o insumo", requerido=True, ancho=32,
          ayuda="Nombre del concepto de pago o del insumo/servicio.",
          ejemplo="PAGO PTU"),
    Campo("centro_costos", "Centro de costos", ancho=20,
          ayuda="Solo para renglones de tipo Insumo.", ejemplo=""),
    Campo("cuenta_contable", "Cuenta contable", ancho=20,
          ayuda="Solo para renglones de tipo Insumo.", ejemplo=""),
    Campo("cantidad", "Cantidad", ancho=12,
          ayuda="Solo para Insumo. Si se omite, 1.", ejemplo=""),
    Campo("precio_unitario", "Precio unitario", ancho=16,
          ayuda="Solo para Insumo.", ejemplo=""),
    Campo("importe", "Importe", requerido=True, ancho=16,
          ayuda="Importe del renglón.", ejemplo="12500.00"),
]

def _listas() -> dict[str, list[str]]:
    """Listas que alimentan los desplegables de validación.

    Los conceptos de pago se leen del catálogo local (`core/conceptos.py`) en el
    momento de generar la plantilla, no de una constante: cambian por empresa y
    se importan del portal. Si el catálogo está vacío, la columna se queda sin
    desplegable en vez de ofrecer una lista inventada.
    """
    from core import conceptos

    listas = {
        "empresas": NOMBRES_EMPRESAS,
        "sucursales": catalogos.SUCURSALES,
        "beneficiarios": catalogos.TIPOS_BENEFICIARIO,
        "formas_pago": catalogos.FORMAS_PAGO,
        "tipos_gasto": catalogos.TIPOS_GASTO,
        "monedas": catalogos.MONEDAS,
        "si_no": ["Sí", "No"],
        "clases": ["Concepto", "Insumo"],
    }
    try:
        nombres = conceptos.nombres()
    except Exception:  # noqa: BLE001 — sin base todavía: plantilla sin lista
        nombres = []
    if nombres:
        listas["conceptos"] = nombres
    return listas


# Compatibilidad: las listas fijas, sin tocar la base. Para los conceptos usa
# `_listas()`, que sí consulta el catálogo.
LISTAS: dict[str, list[str]] = {
    "empresas": NOMBRES_EMPRESAS,
    "sucursales": catalogos.SUCURSALES,
    "beneficiarios": catalogos.TIPOS_BENEFICIARIO,
    "formas_pago": catalogos.FORMAS_PAGO,
    "tipos_gasto": catalogos.TIPOS_GASTO,
    "monedas": catalogos.MONEDAS,
    "si_no": ["Sí", "No"],
    "clases": ["Concepto", "Insumo"],
}


def _normalizar(texto: str) -> str:
    """Encabezado comparable: sin acentos, signos ni espacios de más."""
    import re
    import unicodedata

    t = unicodedata.normalize("NFKD", str(texto or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[¿?¡!:.]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def indice_encabezados(campos: list[Campo]) -> dict[str, str]:
    """Encabezado normalizado -> clave del campo, incluyendo los sinónimos.

    Existe para que el lector reconozca las plantillas que ya usa el área sin
    obligarlas a renombrar columnas.
    """
    indice: dict[str, str] = {}
    for campo in campos:
        for nombre in (campo.etiqueta, *campo.sinonimos):
            indice[_normalizar(nombre)] = campo.clave
    return indice


# --------------------------------------------------------------------------- #
#  Generación del archivo
# --------------------------------------------------------------------------- #
def generar(destino: str, *, filas_ejemplo: bool = True) -> str:
    """Escribe la plantilla en `destino` y devuelve la ruta.

    El ejemplo va en una fila marcada en gris y con una nota: sirve para ver el
    formato esperado, y se ignora al importar (el lector la salta por su marca).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    libro = Workbook()
    azul = "FF1A237E"          # primary-container del sistema de diseño
    gris = "FFF3F3F3"
    borde = Border(*[Side(style="thin", color="FFC6C5D4")] * 4)

    def encabezar(hoja, campos: list[Campo]) -> None:
        for col, campo in enumerate(campos, 1):
            celda = hoja.cell(row=1, column=col)
            celda.value = campo.etiqueta + (" *" if campo.requerido else "")
            celda.font = Font(bold=True, color="FFFFFFFF", size=11)
            celda.fill = PatternFill("solid", fgColor=azul)
            celda.alignment = Alignment(horizontal="center", vertical="center",
                                        wrap_text=True)
            celda.border = borde
            if campo.ayuda:
                celda.comment = _comentario(campo)
            hoja.column_dimensions[get_column_letter(col)].width = campo.ancho
        hoja.row_dimensions[1].height = 34
        hoja.freeze_panes = "A2"

    def _comentario(campo: Campo):
        from openpyxl.comments import Comment
        texto = campo.ayuda
        if campo.requerido:
            texto = "OBLIGATORIO. " + texto
        c = Comment(texto, "Quetzaltic")
        c.width, c.height = 320, 120
        return c

    def ejemplo(hoja, campos: list[Campo], fila: int) -> None:
        for col, campo in enumerate(campos, 1):
            celda = hoja.cell(row=fila, column=col)
            celda.value = campo.ejemplo
            celda.fill = PatternFill("solid", fgColor=gris)
            celda.font = Font(italic=True, color="FF767683")
            celda.border = borde
            # Los importes y la CLABE van como TEXTO para que Excel no los
            # reformatee: la CLABE perdería sus ceros iniciales.
            if campo.clave in ("cuenta_clabe", "fecha_pago"):
                celda.number_format = "@"

    # --- Hoja Solicitudes ---
    hoja = libro.active
    hoja.title = HOJA_SOLICITUDES
    encabezar(hoja, CAMPOS)
    if filas_ejemplo:
        ejemplo(hoja, CAMPOS, 2)
        nota = hoja.cell(row=2, column=len(CAMPOS) + 1)
        nota.value = "← FILA DE EJEMPLO: bórrala o déjala, al importar se ignora."
        nota.font = Font(italic=True, bold=True, color="FFBA1A1A")

    # --- Hoja Partidas ---
    partidas = libro.create_sheet(HOJA_PARTIDAS)
    encabezar(partidas, CAMPOS_PARTIDA)
    partidas.cell(row=2, column=len(CAMPOS_PARTIDA) + 1).value = (
        "Hoja OPCIONAL: úsala solo para las solicitudes con varios renglones.")
    partidas.cell(row=2, column=len(CAMPOS_PARTIDA) + 1).font = Font(
        italic=True, color="FF767683")

    # --- Hoja Catálogos ---
    cat = libro.create_sheet(HOJA_CATALOGOS)
    cat.cell(row=1, column=1).value = (
        "Valores válidos. No edites esta hoja: alimenta los desplegables.")
    cat.cell(row=1, column=1).font = Font(bold=True, color="FFBA1A1A")
    listas = _listas()
    columnas_lista: dict[str, str] = {}
    for i, (nombre, valores) in enumerate(listas.items(), 1):
        letra = get_column_letter(i)
        columnas_lista[nombre] = letra
        cat.cell(row=2, column=i).value = nombre
        cat.cell(row=2, column=i).font = Font(bold=True)
        cat.column_dimensions[letra].width = 34
        for j, valor in enumerate(valores, 3):
            cat.cell(row=j, column=i).value = valor

    def validar(hoja, campos: list[Campo]) -> None:
        for col, campo in enumerate(campos, 1):
            # Un catálogo puede no estar disponible —los conceptos vienen de la
            # base y puede estar vacía—: esa columna se queda sin desplegable,
            # que es mejor que ofrecer una lista falsa.
            if not campo.catalogo or campo.catalogo not in listas:
                continue
            letra = columnas_lista[campo.catalogo]
            n = len(listas[campo.catalogo]) + 2
            dv = DataValidation(
                type="list",
                formula1=f"='{HOJA_CATALOGOS}'!${letra}$3:${letra}${n}",
                allow_blank=True, showDropDown=False)
            # `showErrorMessage=False`: se avisa, pero no se bloquea. Un catálogo
            # puede quedarse corto frente a SIPP, y no queremos que la plantilla
            # impida capturar un valor legítimo que aquí no esté listado.
            dv.showErrorMessage = False
            hoja.add_data_validation(dv)
            columna = get_column_letter(col)
            dv.add(f"{columna}2:{columna}1000")

    validar(hoja, CAMPOS)
    validar(partidas, CAMPOS_PARTIDA)

    # --- Hoja Instrucciones ---
    ins = libro.create_sheet(HOJA_INSTRUCCIONES, 0)
    ins.column_dimensions["A"].width = 110
    lineas = [
        ("Carga masiva de solicitudes de pago", "titulo"),
        ("", ""),
        ("1. Llena una fila por solicitud en la hoja «Solicitudes».", "sub"),
        ("   Las columnas marcadas con * son obligatorias. Pasa el cursor sobre "
         "cada encabezado para ver su ayuda.", ""),
        ("", ""),
        ("2. Usa los desplegables.", "sub"),
        ("   Empresa, Sucursal, Tipo de beneficiario, Forma de pago, Tipo de "
         "gasto y Moneda tienen lista de valores válidos. Un valor escrito a "
         "mano que no exista en SIPP hará fallar esa solicitud al capturarla.", ""),
        ("", ""),
        ("3. La CLABE y la fecha van como TEXTO.", "sub"),
        ("   Si Excel convierte la CLABE en número, pierde los ceros de la "
         "izquierda y el pago se va a otra cuenta. La fecha va DD/MM/AAAA.", ""),
        ("", ""),
        ("4. Varios conceptos en una solicitud: usa la hoja «Partidas».", "sub"),
        ("   Ponle una Referencia a la fila en «Solicitudes» y repite esa misma "
         "Referencia en cada renglón de «Partidas». Si usas esa hoja, el "
         "concepto e importe de la hoja principal se ignoran para esa fila.", ""),
        ("", ""),
        ("5. Qué pasa al importar.", "sub"),
        ("   La herramienta valida todo antes de dar de alta nada y te muestra "
         "una vista previa con los errores fila por fila. Nada se guarda hasta "
         "que confirmas.", ""),
        ("", ""),
        ("Conceptos e insumos no son lo mismo.", "sub"),
        ("   El «Concepto de pago» es la imputación contable y es lo que SIPP "
         "suma para calcular el total de la solicitud. Los «Insumos» describen "
         "qué se compró y llevan centro de costos y cuenta contable, pero no "
         "suman al total.", ""),
    ]
    for i, (texto, estilo) in enumerate(lineas, 1):
        celda = ins.cell(row=i, column=1)
        celda.value = texto
        celda.alignment = Alignment(wrap_text=True, vertical="top")
        if estilo == "titulo":
            celda.font = Font(bold=True, size=16, color=azul)
        elif estilo == "sub":
            celda.font = Font(bold=True, size=11)
        if not texto.startswith("   "):
            ins.row_dimensions[i].height = 20
        else:
            ins.row_dimensions[i].height = 32

    os.makedirs(os.path.dirname(os.path.abspath(destino)) or ".", exist_ok=True)
    libro.save(destino)
    return destino
