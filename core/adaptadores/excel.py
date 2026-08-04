"""ExcelAdapter: convierte una hoja de cálculo en solicitudes y partidas.

El formato lo define `core/plantilla_excel.py`, pero el lector es **tolerante a
propósito**: reconoce los encabezados por sinónimos (las plantillas que ya
circulan en el área no usan los mismos nombres) y permite corregir a mano el
mapeo columna → campo cuando la detección automática no basta.

Nada de lo que sale de aquí se guarda sin pasar por `core.validador`: el objetivo
de la carga masiva es meter cien solicitudes de golpe, y cien errores de golpe
serían peor que capturarlas a mano.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from core import catalogos, validador
from core.db import CONCEPTO, INSUMO, Partida, Solicitud, total_desglose
from core.plantilla_excel import (CAMPOS, CAMPOS_PARTIDA, HOJA_PARTIDAS,
                                  HOJA_SOLICITUDES, _normalizar,
                                  indice_encabezados)

# Marca de la fila de ejemplo de la plantilla: se salta al importar.
_MARCA_EJEMPLO = "fila de ejemplo"

_CLAVES_REQUERIDAS = [c.clave for c in CAMPOS if c.requerido]


@dataclass
class Deteccion:
    """Resultado de mirar los encabezados: qué columna alimenta qué campo."""

    columnas: dict[str, int] = field(default_factory=dict)   # clave -> col (0-based)
    encabezados: list[str] = field(default_factory=list)
    faltantes: list[str] = field(default_factory=list)       # requeridos sin columna
    hoja: str = ""

    @property
    def completa(self) -> bool:
        return not self.faltantes


@dataclass
class FilaImportada:
    """Una fila del Excel ya convertida y validada."""

    numero: int                       # número de fila en Excel (para señalarla)
    solicitud: Solicitud
    partidas: list[Partida] = field(default_factory=list)
    hallazgos: list[validador.Hallazgo] = field(default_factory=list)

    @property
    def valida(self) -> bool:
        return not validador.hay_errores(self.hallazgos)

    @property
    def resumen_problemas(self) -> str:
        return validador.resumen(self.hallazgos)


@dataclass
class Importacion:
    """Lo que la interfaz necesita para mostrar la vista previa."""

    filas: list[FilaImportada] = field(default_factory=list)
    deteccion: Deteccion = field(default_factory=Deteccion)
    error: str = ""

    @property
    def validas(self) -> list[FilaImportada]:
        return [f for f in self.filas if f.valida]

    @property
    def invalidas(self) -> list[FilaImportada]:
        return [f for f in self.filas if not f.valida]


# --------------------------------------------------------------------------- #
#  Conversión de valores
# --------------------------------------------------------------------------- #
def _texto(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, float) and valor.is_integer():
        # Excel guarda todo como float: un folio 1234 llegaría como '1234.0'.
        return str(int(valor))
    return str(valor).strip()


def _numero(valor) -> float:
    """Importe tolerante: acepta '$1,234.56', '1 234,56' y números de Excel."""
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, (int, float)):
        return round(float(valor), 2)
    limpio = re.sub(r"[^\d,.\-]", "", str(valor))
    # Si hay coma Y punto, la coma es separador de miles. Si solo hay coma, es
    # el decimal (formato europeo, que aparece al pegar desde algunos sistemas).
    if "," in limpio and "." in limpio:
        limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    try:
        return round(float(limpio), 2)
    except ValueError:
        return 0.0


def _fecha(valor) -> str:
    """Devuelve 'DD/MM/AAAA'. Acepta fecha real de Excel o texto."""
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%d/%m/%Y")
    texto = _texto(valor)
    if not texto:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(texto, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return texto            # se devuelve tal cual; el validador lo rechazará


def _si(valor) -> int:
    """1 si el texto significa «sí». Acepta Sí/Si/S/X/1/True/Verdadero."""
    t = _normalizar(_texto(valor))
    return 1 if t in ("si", "s", "x", "1", "true", "verdadero", "sí") else 0


def _clabe(valor) -> str:
    """La CLABE es una cadena de 18 dígitos, aunque Excel la guarde como número.

    Si alguien dejó la celda en formato numérico, llega como 1.2345678901e+16 y
    perdió los ceros iniciales. Se recupera lo que se pueda y el validador se
    encarga de rechazar lo que no cuadre: es preferible a mandar una CLABE mocha
    a un pago real.
    """
    if isinstance(valor, float):
        return f"{int(valor):d}"
    return re.sub(r"\D", "", _texto(valor))


# --------------------------------------------------------------------------- #
#  Lectura
# --------------------------------------------------------------------------- #
def _abrir(ruta: str):
    from openpyxl import load_workbook

    # `data_only=True`: si la celda trae una fórmula, interesa su RESULTADO.
    return load_workbook(ruta, data_only=True, read_only=False)


def _hoja(libro, preferida: str):
    """La hoja preferida si existe; si no, la primera (plantillas ajenas)."""
    if preferida in libro.sheetnames:
        return libro[preferida]
    return libro[libro.sheetnames[0]]


def _fila_valores(hoja, numero: int) -> list:
    return [c.value for c in hoja[numero]]


def detectar(ruta: str, campos=None, hoja_nombre: str = HOJA_SOLICITUDES) -> Deteccion:
    """Empareja los encabezados de la primera fila con los campos del formato."""
    campos = campos or CAMPOS
    indice = indice_encabezados(campos)
    libro = _abrir(ruta)
    try:
        hoja = _hoja(libro, hoja_nombre)
        encabezados = [_texto(v) for v in _fila_valores(hoja, 1)]
        columnas: dict[str, int] = {}
        for i, encabezado in enumerate(encabezados):
            # El asterisco marca obligatorio en la plantilla; no es parte del
            # nombre y estorbaría al emparejar.
            clave = indice.get(_normalizar(encabezado.rstrip(" *")))
            if clave and clave not in columnas:
                columnas[clave] = i
        requeridos = [c.clave for c in campos if c.requerido]
        faltantes = [c for c in requeridos if c not in columnas]
        return Deteccion(columnas, encabezados, faltantes, hoja.title)
    finally:
        libro.close()


def _es_ejemplo(valores: list, ancho: int) -> bool:
    """La fila de ejemplo de la plantilla lleva una nota más allá de la última
    columna. Se reconoce por ahí y no por su formato, que el usuario puede
    haber cambiado sin querer."""
    for valor in valores[ancho:]:
        if _MARCA_EJEMPLO in _normalizar(_texto(valor)):
            return True
    return False


def _partidas_por_referencia(ruta: str) -> dict[str, list[Partida]]:
    """Lee la hoja opcional de desglose, agrupada por Referencia."""
    libro = _abrir(ruta)
    try:
        if HOJA_PARTIDAS not in libro.sheetnames:
            return {}
        hoja = libro[HOJA_PARTIDAS]
        indice = indice_encabezados(CAMPOS_PARTIDA)
        encabezados = [_texto(v) for v in _fila_valores(hoja, 1)]
        columnas: dict[str, int] = {}
        for i, encabezado in enumerate(encabezados):
            clave = indice.get(_normalizar(encabezado.rstrip(" *")))
            if clave and clave not in columnas:
                columnas[clave] = i
        if "referencia" not in columnas:
            return {}

        def dato(valores, clave):
            i = columnas.get(clave)
            return valores[i] if i is not None and i < len(valores) else None

        agrupadas: dict[str, list[Partida]] = {}
        for fila in hoja.iter_rows(min_row=2, values_only=True):
            valores = list(fila)
            if not any(v not in (None, "") for v in valores):
                continue
            referencia = _texto(dato(valores, "referencia"))
            if not referencia:
                continue
            clase_txt = _normalizar(_texto(dato(valores, "clase")))
            clase = INSUMO if clase_txt.startswith("insumo") else CONCEPTO
            nombre = _texto(dato(valores, "nombre"))
            partida = Partida(
                clase=clase,
                importe=_numero(dato(valores, "importe")),
                centro_costos=_texto(dato(valores, "centro_costos")),
                cuenta_contable=_texto(dato(valores, "cuenta_contable")),
                cantidad=_numero(dato(valores, "cantidad")) or 1.0,
                precio_unitario=_numero(dato(valores, "precio_unitario")),
                origen="EXCEL")
            if clase == CONCEPTO:
                partida.concepto_nombre = nombre
            else:
                partida.insumo_nombre = nombre
            agrupadas.setdefault(referencia, []).append(partida)
        return agrupadas
    finally:
        libro.close()


def leer(ruta: str, lote_id: str = "", *,
         columnas: dict[str, int] | None = None,
         parada: str = "LLENADA") -> Importacion:
    """Lee el archivo completo y devuelve las filas ya validadas.

    `columnas` permite forzar el mapeo cuando la detección automática no acertó
    (lo manda la interfaz tras corregirlo a mano).
    """
    try:
        deteccion = detectar(ruta)
    except Exception as exc:  # noqa: BLE001 — archivo corrupto o no es Excel
        return Importacion(error=f"No se pudo leer el archivo: {exc}")

    if columnas:
        deteccion.columnas = dict(columnas)
        deteccion.faltantes = [c for c in _CLAVES_REQUERIDAS
                               if c not in deteccion.columnas]
    if not deteccion.completa:
        etiquetas = {c.clave: c.etiqueta for c in CAMPOS}
        faltan = ", ".join(etiquetas.get(c, c) for c in deteccion.faltantes)
        return Importacion(
            deteccion=deteccion,
            error=f"Faltan columnas obligatorias: {faltan}. Descarga la "
                  f"plantilla o corrige el mapeo de columnas.")

    try:
        desglose = _partidas_por_referencia(ruta)
        libro = _abrir(ruta)
    except Exception as exc:  # noqa: BLE001
        return Importacion(deteccion=deteccion,
                           error=f"No se pudo leer el archivo: {exc}")

    filas: list[FilaImportada] = []
    try:
        hoja = _hoja(libro, deteccion.hoja)
        ancho = len(CAMPOS)

        def dato(valores, clave):
            i = deteccion.columnas.get(clave)
            return valores[i] if i is not None and i < len(valores) else None

        for numero, fila in enumerate(
                hoja.iter_rows(min_row=2, values_only=True), start=2):
            valores = list(fila)
            if not any(v not in (None, "") for v in valores):
                continue                       # fila vacía
            if _es_ejemplo(valores, ancho):
                continue                       # la fila de ejemplo no se importa

            solicitud = Solicitud(
                lote_id=lote_id,
                empresa=_texto(dato(valores, "empresa")),
                sucursal=_texto(dato(valores, "sucursal")),
                tipo_beneficiario=_texto(dato(valores, "tipo_beneficiario")),
                # `beneficiario_nuevo` y `beneficiario_folio` NO se leen del
                # archivo: los resuelve el robot consultando SIPP al capturar.
                beneficiario_nombre=_texto(dato(valores, "beneficiario_nombre")),
                beneficiario_rfc=_texto(dato(valores, "beneficiario_rfc")).upper(),
                beneficiario_correo=_texto(dato(valores, "beneficiario_correo")),
                forma_pago=_texto(dato(valores, "forma_pago")),
                tipo_gasto=_texto(dato(valores, "tipo_gasto")),
                cuenta_banco=_texto(dato(valores, "cuenta_banco")),
                cuenta_clabe=_clabe(dato(valores, "cuenta_clabe")),
                cuenta_titular=_texto(dato(valores, "cuenta_titular")),
                fecha_pago=_fecha(dato(valores, "fecha_pago")),
                moneda=_texto(dato(valores, "moneda")) or "Pesos (MXN)",
                descripcion=_texto(dato(valores, "descripcion")),
                origen="EXCEL",
                parada=parada,
                estado="PENDIENTE")

            referencia = _texto(dato(valores, "referencia"))
            if referencia and referencia in desglose:
                # La hoja de partidas MANDA sobre el concepto de la fila: si el
                # usuario se tomó el trabajo de desglosar, ese es el detalle real.
                partidas = [Partida(**{**p.__dict__, "id": Partida().id})
                            for p in desglose[referencia]]
            else:
                # La clase de la partida NO la elige el archivo: la impone el
                # tipo de beneficiario, porque es lo que decide qué pestaña le
                # muestra SIPP (Insumos a Proveedor, Conceptos a los otros dos).
                clase = catalogos.clase_desglose(solicitud.tipo_beneficiario)
                nombre = _texto(dato(valores, "concepto_nombre"))
                partida = Partida(clase=clase,
                                  importe=_numero(dato(valores, "importe")),
                                  origen="EXCEL")
                if clase == CONCEPTO:
                    partida.concepto_nombre = nombre
                else:
                    partida.insumo_nombre = nombre
                partidas = [partida]

            solicitud.importe_total = total_desglose(
                solicitud.tipo_beneficiario, partidas)
            hallazgos = validador.validar(solicitud, partidas)
            filas.append(FilaImportada(numero, solicitud, partidas, hallazgos))
    except Exception as exc:  # noqa: BLE001 — se reporta con lo leído hasta ahí
        libro.close()
        return Importacion(filas, deteccion,
                           error=f"Error leyendo la fila {len(filas) + 2}: {exc}")
    finally:
        try:
            libro.close()
        except Exception:  # noqa: BLE001
            pass

    if not filas:
        return Importacion(deteccion=deteccion,
                           error="El archivo no tiene filas con datos.")
    return Importacion(filas, deteccion)


def detectar_duplicados(filas: list[FilaImportada]) -> set[int]:
    """Números de fila que repiten una solicitud ya presente en el archivo.

    Se compara por la misma clave de idempotencia que usa el motor, así que dos
    filas «distintas» que producirían el mismo pago se marcan aquí y no hasta
    que la base las rechace una por una.
    """
    from core.db import calcular_clave

    vistas: dict[str, int] = {}
    repetidas: set[int] = set()
    for f in filas:
        clave = calcular_clave(f.solicitud, f.partidas)
        if clave in vistas:
            repetidas.add(f.numero)
        else:
            vistas[clave] = f.numero
    return repetidas
