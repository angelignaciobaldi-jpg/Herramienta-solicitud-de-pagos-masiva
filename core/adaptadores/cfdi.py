"""CfdiAdapter: convierte un CFDI en una solicitud con su desglose.

**El XML es la fuente de verdad, no el PDF.** El PDF es una representación
impresa que cada emisor arma como quiere; el XML es lo que timbró el SAT. De ahí
se saca todo, y el PDF se conserva únicamente como evidencia adjunta.

Soporta CFDI 3.3 y 4.0: cambia el espacio de nombres y algún atributo, pero la
estructura que interesa —emisor, receptor, conceptos, total y timbre— es la
misma, así que se buscan los dos.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from xml.etree import ElementTree

from core.db import INSUMO, Partida, Solicitud

# Espacios de nombres de las dos versiones vigentes, más el del timbre.
_NS = {
    "cfdi33": "http://www.sat.gob.mx/cfd/3",
    "cfdi40": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}

# Catálogo del SAT, solo lo que la herramienta necesita mostrar.
FORMAS_PAGO_SAT = {
    "01": "Efectivo", "02": "Cheque nominativo", "03": "Transferencia",
    "04": "Tarjeta de crédito", "28": "Tarjeta de débito",
    "99": "Por definir",
}
MONEDAS_SAT = {"MXN": "Pesos (MXN)", "USD": "Dolar (USD)", "EUR": "Euro"}


@dataclass
class Comprobante:
    """Los datos del CFDI que la herramienta usa."""

    uuid: str = ""
    ruta_xml: str = ""
    ruta_pdf: str = ""
    version: str = ""
    fecha: str = ""                 # 'DD/MM/AAAA'
    serie_folio: str = ""
    rfc_emisor: str = ""
    nombre_emisor: str = ""
    rfc_receptor: str = ""
    nombre_receptor: str = ""
    subtotal: float = 0.0
    descuento: float = 0.0
    impuestos: float = 0.0
    total: float = 0.0
    moneda: str = "Pesos (MXN)"
    tipo_cambio: float = 1.0
    forma_pago: str = ""
    metodo_pago: str = ""
    conceptos: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def valido(self) -> bool:
        return not self.error and bool(self.uuid)


def _num(valor, defecto: float = 0.0) -> float:
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return defecto


def _sin_ns(etiqueta: str) -> str:
    """Nombre local de una etiqueta, sin su espacio de nombres."""
    return etiqueta.rsplit("}", 1)[-1]


def _buscar(raiz, nombre: str):
    """Primer descendiente cuyo nombre local coincida, sea cual sea la versión."""
    for nodo in raiz.iter():
        if _sin_ns(nodo.tag) == nombre:
            return nodo
    return None


def _todos(raiz, nombre: str) -> list:
    return [n for n in raiz.iter() if _sin_ns(n.tag) == nombre]


def leer_xml(ruta: str) -> Comprobante:
    """Lee un CFDI y devuelve sus datos. Nunca lanza: el error va en `.error`,
    porque una carpeta con cincuenta XML no debe caerse por uno malo."""
    comp = Comprobante(ruta_xml=ruta)
    try:
        arbol = ElementTree.parse(ruta)
    except (ElementTree.ParseError, OSError) as exc:
        comp.error = f"No es un XML legible: {exc}"
        return comp

    raiz = arbol.getroot()
    if _sin_ns(raiz.tag) != "Comprobante":
        comp.error = "El XML no es un CFDI (no tiene nodo Comprobante)."
        return comp

    a = raiz.attrib
    comp.version = a.get("Version") or a.get("version") or ""
    comp.subtotal = _num(a.get("SubTotal"))
    comp.descuento = _num(a.get("Descuento"))
    comp.total = _num(a.get("Total"))
    comp.tipo_cambio = _num(a.get("TipoCambio"), 1.0) or 1.0
    codigo_moneda = (a.get("Moneda") or "MXN").upper()
    comp.moneda = MONEDAS_SAT.get(codigo_moneda, codigo_moneda)
    comp.forma_pago = FORMAS_PAGO_SAT.get(a.get("FormaPago", ""),
                                          a.get("FormaPago", ""))
    comp.metodo_pago = a.get("MetodoPago", "")
    serie, folio = a.get("Serie", ""), a.get("Folio", "")
    comp.serie_folio = "-".join(x for x in (serie, folio) if x)

    fecha = a.get("Fecha") or a.get("fecha") or ""
    try:
        comp.fecha = datetime.fromisoformat(fecha[:19]).strftime("%d/%m/%Y")
    except ValueError:
        comp.fecha = ""

    emisor = _buscar(raiz, "Emisor")
    if emisor is not None:
        comp.rfc_emisor = (emisor.get("Rfc") or emisor.get("rfc") or "").upper()
        comp.nombre_emisor = emisor.get("Nombre") or emisor.get("nombre") or ""
    receptor = _buscar(raiz, "Receptor")
    if receptor is not None:
        comp.rfc_receptor = (receptor.get("Rfc") or receptor.get("rfc") or "").upper()
        comp.nombre_receptor = receptor.get("Nombre") or receptor.get("nombre") or ""

    # El UUID vive en el complemento del timbre, no en el comprobante.
    timbre = _buscar(raiz, "TimbreFiscalDigital")
    if timbre is not None:
        comp.uuid = (timbre.get("UUID") or "").upper()
    if not comp.uuid:
        comp.error = ("El CFDI no está timbrado (no trae UUID): no sirve como "
                      "respaldo de un pago.")

    # Impuestos trasladados del total (los del nodo raíz, no los de concepto).
    for nodo in _todos(raiz, "Impuestos"):
        if nodo.get("TotalImpuestosTrasladados"):
            comp.impuestos = _num(nodo.get("TotalImpuestosTrasladados"))
            break

    for nodo in _todos(raiz, "Concepto"):
        c = nodo.attrib
        comp.conceptos.append({
            "clave": c.get("ClaveProdServ", ""),
            "descripcion": c.get("Descripcion", ""),
            "cantidad": _num(c.get("Cantidad"), 1.0) or 1.0,
            "unidad": c.get("Unidad") or c.get("ClaveUnidad", ""),
            "precio_unitario": _num(c.get("ValorUnitario")),
            "importe": _num(c.get("Importe")),
        })
    return comp


# --------------------------------------------------------------------------- #
#  Emparejamiento con el PDF
# --------------------------------------------------------------------------- #
def _normalizar_nombre(texto: str) -> str:
    t = unicodedata.normalize("NFKD", os.path.splitext(texto)[0].lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", t)


_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def emparejar_pdf(comp: Comprobante, pdfs: list[str]) -> str:
    """Busca el PDF que acompaña al CFDI. Devuelve su ruta o cadena vacía.

    Se intenta por UUID (en el nombre del archivo o dentro del texto del PDF) y,
    si no, por nombre de archivo idéntico al del XML. El UUID va primero porque
    es el único criterio que no se equivoca: dos facturas del mismo proveedor
    suelen tener nombres de archivo parecidísimos.
    """
    if not comp.uuid:
        return ""
    uuid_norm = comp.uuid.lower()
    for pdf in pdfs:
        if uuid_norm in os.path.basename(pdf).lower():
            return pdf
    objetivo = _normalizar_nombre(os.path.basename(comp.ruta_xml))
    for pdf in pdfs:
        if _normalizar_nombre(os.path.basename(pdf)) == objetivo:
            return pdf
    # Último recurso: leer el texto del PDF y buscar el UUID dentro.
    try:
        import fitz  # PyMuPDF

        for pdf in pdfs:
            try:
                with fitz.open(pdf) as doc:
                    texto = "".join(p.get_text() for p in doc)
                if uuid_norm in texto.lower():
                    return pdf
            except Exception:  # noqa: BLE001 — PDF ilegible: se prueba el otro
                continue
    except ImportError:
        pass
    return ""


# --------------------------------------------------------------------------- #
#  Conversión al modelo de la herramienta
# --------------------------------------------------------------------------- #
def a_solicitud(comp: Comprobante, *, lote_id: str = "", empresa: str = "",
                sucursal: str = "", tipo_beneficiario: str = "Proveedor",
                parada: str = "LLENADA") -> tuple[Solicitud, list[Partida]]:
    """Arma la solicitud y sus partidas a partir del comprobante.

    Los conceptos del CFDI se mapean a partidas de clase **INSUMO**: describen
    qué se compró. El concepto de pago —la imputación contable que SIPP suma
    para calcular el total— no está en el CFDI y lo tiene que poner el usuario,
    a mano o con la asignación masiva. Por eso una solicitud recién importada de
    un CFDI queda incompleta a propósito, y el validador lo dice.
    """
    solicitud = Solicitud(
        lote_id=lote_id,
        empresa=empresa,
        sucursal=sucursal,
        tipo_beneficiario=tipo_beneficiario,
        beneficiario_nombre=comp.nombre_emisor,
        beneficiario_rfc=comp.rfc_emisor,
        forma_pago="Transferencia",
        # Un CFDI es el respaldo natural de un gasto deducible.
        tipo_gasto="Deducible",
        fecha_pago=comp.fecha,
        moneda=comp.moneda,
        descripcion=(f"CFDI {comp.serie_folio} — {comp.nombre_emisor}".strip()
                     if comp.serie_folio else comp.nombre_emisor),
        origen="CFDI",
        parada=parada,
        estado="PENDIENTE")

    partidas = [
        Partida(clase=INSUMO,
                insumo_nombre=c["descripcion"][:250],
                descripcion=c["clave"],
                cantidad=c["cantidad"],
                precio_unitario=c["precio_unitario"],
                importe=c["importe"],
                origen="CFDI")
        for c in comp.conceptos
    ]
    # `importe_total` se queda en 0 a propósito: lo derivan los conceptos de
    # pago, que aquí todavía no existen. El total del CFDI se compara aparte.
    return solicitud, partidas


def discrepancia_total(comp: Comprobante, partidas: list[Partida]) -> str:
    """Compara la suma de partidas contra el total del CFDI.

    No se sobrescribe nada: se marca la diferencia y decide el usuario. Un CFDI
    con descuentos o impuestos locales puede no cuadrar por razones legítimas, y
    ajustar el importe en silencio sería peor que avisar.
    """
    suma = round(sum(p.importe for p in partidas), 2)
    if abs(suma - comp.subtotal) <= 0.01 or abs(suma - comp.total) <= 0.01:
        return ""
    return (f"La suma de los conceptos ({suma:,.2f}) no coincide con el "
            f"subtotal ({comp.subtotal:,.2f}) ni con el total "
            f"({comp.total:,.2f}) del CFDI.")


def leer_carpeta(carpeta: str, recursivo: bool = True) -> list[Comprobante]:
    """Lee todos los CFDI de una carpeta y les empareja su PDF.

    Recorre recursivamente porque así es como llegan del contador: una carpeta
    por proveedor o por mes.
    """
    xmls: list[str] = []
    pdfs: list[str] = []
    for raiz, _dirs, archivos in os.walk(carpeta):
        for nombre in archivos:
            ruta = os.path.join(raiz, nombre)
            if nombre.lower().endswith(".xml"):
                xmls.append(ruta)
            elif nombre.lower().endswith(".pdf"):
                pdfs.append(ruta)
        if not recursivo:
            break
    comprobantes = []
    for xml in sorted(xmls):
        comp = leer_xml(xml)
        if comp.valido:
            comp.ruta_pdf = emparejar_pdf(comp, pdfs)
        comprobantes.append(comp)
    return comprobantes
