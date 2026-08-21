"""ExcelAdapter: convierte una hoja de cálculo en solicitudes y partidas.

Acepta .xlsx/.xlsm y también .csv. El CSV se envuelve en un libro de openpyxl
(ver `_libro_desde_csv`) para que los dos formatos pasen por exactamente las
mismas reglas de detección y validación, en vez de tener un camino paralelo
con menos comprobaciones.

El formato lo define `core/plantilla_excel.py`, pero el lector es **tolerante a
propósito**: reconoce los encabezados por sinónimos (las plantillas que ya
circulan en el área no usan los mismos nombres) y permite corregir a mano el
mapeo columna → campo cuando la detección automática no basta.

Nada de lo que sale de aquí se guarda sin pasar por `core.validador`: el objetivo
de la carga masiva es meter cien solicitudes de golpe, y cien errores de golpe
serían peor que capturarlas a mano.
"""

from __future__ import annotations

import csv
import io
import os
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
def _texto_csv(ruta: str) -> str:
    """Lee el CSV probando codificaciones, de la más estricta a la más laxa.

    Los CSV que exporta Excel en español suelen venir en la codificación de
    Windows (cp1252), no en UTF-8, y ahí un nombre con acento se leería roto.
    `latin-1` cierra la lista porque acepta cualquier byte: es preferible un
    acento raro a no poder abrir el archivo.
    """
    for codec in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(ruta, encoding=codec, newline="") as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(ruta, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _delimitador(texto: str) -> str:
    """El separador del CSV, deducido del renglón de encabezados.

    Excel guarda con ';' cuando el Windows está en español (la coma es el
    separador decimal), y con ',' cuando está en inglés. Se cuenta sobre los
    encabezados y no sobre una fila de datos porque esa puede traer comas
    dentro de un importe entrecomillado —« $7,966.13 »— y desempatar mal.
    """
    cabecera = next((l for l in texto.splitlines() if l.strip()), "")
    conteos = {d: cabecera.count(d) for d in (",", ";", "\t", "|")}
    mejor = max(conteos, key=lambda d: conteos[d])
    return mejor if conteos[mejor] else ","


def _libro_desde_csv(ruta: str):
    """Envuelve un CSV en un libro de openpyxl para no duplicar el lector.

    Todo lo que sigue (detección de encabezados, mapeo manual de columnas,
    validación) trabaja sobre la interfaz de openpyxl. Convertir aquí, en vez
    de escribir un camino paralelo para CSV, hace que un CSV pase exactamente
    por las mismas reglas que un .xlsx y no haya un formato con menos
    validaciones que el otro.

    Todas las celdas quedan como TEXTO, que es lo que un CSV es. No estorba:
    `_numero`, `_fecha` y `_clabe` ya interpretan texto porque las plantillas
    que circulan traen los importes como '$1,234.56'. De hecho ayuda con la
    CLABE, que en .xlsx se corrompe cuando la celda quedó en formato numérico.
    """
    from openpyxl import Workbook

    texto = _texto_csv(ruta)
    libro = Workbook()
    hoja = libro.active
    hoja.title = HOJA_SOLICITUDES
    lector = csv.reader(io.StringIO(texto), delimiter=_delimitador(texto))
    for indice, fila in enumerate(lector, start=1):
        for columna, valor in enumerate(fila, start=1):
            # Celda por celda y no `append`: este último interpreta como
            # fórmula cualquier valor que empiece con '=', y una descripción
            # bien puede hacerlo.
            hoja.cell(row=indice, column=columna).value = valor
    return libro


def _letra_columna(indice: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(indice)


def _abrir(ruta: str):
    if os.path.splitext(ruta)[1].lower() == ".csv":
        return _libro_desde_csv(ruta)

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


# --------------------------------------------------------------------------- #
#  Documentos enlazados
# --------------------------------------------------------------------------- #
# Una URL escrita como texto en la celda. Se acepta además del hipervínculo real
# porque al pegar un listado en la hoja el enlace suele llegar como texto plano.
_RE_URL = re.compile(r"https?://\S+", re.I)


# Palabras del encabezado que delatan la columna de la carátula bancaria. Un
# formulario puede pedir varios documentos por solicitud —boleta, acta, censo,
# carátula—, y bajarlos todos sería traer tres archivos inútiles por persona.
_PISTAS_CARATULA = ("caratula", "estado de cuenta", "cuenta bancaria",
                    "datos bancarios", "clabe")
# Hasta qué fila se busca el renglón de encabezados. No siempre es la primera:
# los export de formularios suelen traer dos o tres filas de títulos agrupados.
_FILAS_ENCABEZADO = 12


def _url_de_celda(celda) -> str:
    """La URL de una celda, sea hipervínculo real o texto pegado.

    Se miran las dos formas porque llegan las dos: «Insertar > Vínculo» produce
    un hipervínculo, y pegar un listado deja la dirección como texto. En un
    archivo real de 212 filas, una era hipervínculo y el resto texto.
    """
    destino = getattr(getattr(celda, "hyperlink", None), "target", None)
    if not destino:
        hallazgo = _RE_URL.search(_texto(celda.value))
        destino = hallazgo.group(0) if hallazgo else None
    destino = (destino or "").strip()
    return destino if destino.lower().startswith(("http://", "https://")) else ""


def columnas_con_enlaces(ruta: str,
                         hoja_nombre: str = HOJA_SOLICITUDES) -> list[dict]:
    """Columnas que contienen documentos enlazados, con su encabezado y cuántos.

    Devuelve una lista de dicts `{indice, letra, encabezado, cantidad}`, para
    que la interfaz pueda decir de cuál columna va a bajar y dejar cambiarla.
    """
    libro = _abrir(ruta)
    try:
        hoja = _hoja(libro, hoja_nombre)
        conteo: dict[int, int] = {}
        for fila in hoja.iter_rows():
            for celda in fila:
                if _url_de_celda(celda):
                    conteo[celda.column] = conteo.get(celda.column, 0) + 1

        salida = []
        for columna, cantidad in sorted(conteo.items()):
            # El encabezado es el último texto que NO es un enlace por encima de
            # los datos: así se acierta aunque el renglón de títulos no sea el
            # primero, que es lo normal en un export de formulario.
            encabezado = ""
            for numero in range(1, _FILAS_ENCABEZADO + 1):
                celda = hoja.cell(row=numero, column=columna)
                texto = _texto(celda.value)
                if texto and not _url_de_celda(celda):
                    encabezado = texto
            salida.append({
                "indice": columna,
                # Con `get_column_letter` y no con `celda.column_letter`: la
                # fila de títulos suele traer celdas COMBINADAS, y una
                # `MergedCell` no expone esa propiedad.
                "letra": _letra_columna(columna),
                "encabezado": encabezado,
                "cantidad": cantidad,
            })
        return salida
    finally:
        try:
            libro.close()
        except Exception:  # noqa: BLE001
            pass


def columna_de_caratulas(columnas: list[dict]) -> "int | None":
    """De las columnas con enlaces, la que el encabezado señala como carátula.

    Devuelve None si ninguna lo dice: ahí no se adivina, porque bajar la columna
    equivocada trae el documento de otra cosa y el OCR no encuentra CLABE en un
    acta de nacimiento.
    """
    for columna in columnas:
        normalizado = _normalizar(columna.get("encabezado", ""))
        if any(pista in normalizado for pista in _PISTAS_CARATULA):
            return columna["indice"]
    return None


def enlaces_por_fila(ruta: str, hoja_nombre: str = HOJA_SOLICITUDES, *,
                     columna: "int | None" = None) -> dict[int, str]:
    """Documentos enlazados en la hoja: {número de fila -> URL}.

    Con `columna` se toma el enlace de esa columna y solo de esa. Sin ella se
    intenta reconocer la de la carátula por su encabezado y, si no se reconoce,
    se cae al primer enlace de cada fila —que es lo correcto cuando la hoja trae
    un único documento por renglón—.

    Un CSV nunca trae hipervínculos (el formato no los soporta), así que ahí
    solo se encuentran las URL escritas como texto.
    """
    if columna is None:
        columna = columna_de_caratulas(columnas_con_enlaces(ruta, hoja_nombre))

    libro = _abrir(ruta)
    try:
        hoja = _hoja(libro, hoja_nombre)
        encontrados: dict[int, str] = {}
        for fila in hoja.iter_rows():
            for celda in fila:
                if columna is not None and celda.column != columna:
                    continue
                if celda.row in encontrados:
                    break
                url = _url_de_celda(celda)
                if url:
                    encontrados[celda.row] = url
                    break
        return encontrados
    finally:
        try:
            libro.close()
        except Exception:  # noqa: BLE001
            pass


def nombres_por_fila(ruta: str,
                     hoja_nombre: str = HOJA_SOLICITUDES) -> dict[int, str]:
    """{fila -> nombre del beneficiario}, para nombrar lo que se descargue.

    Del nombre del archivo sale el beneficiario de respaldo cuando el OCR no
    puede leer la carátula (`caratulas.nombre_desde_archivo`). Si lo bajado se
    llamara «documento.pdf», ese respaldo se perdería.
    """
    try:
        deteccion = detectar(ruta, hoja_nombre=hoja_nombre)
    except Exception:  # noqa: BLE001 — sin nombres se sigue igual
        return {}
    columna = deteccion.columnas.get("beneficiario_nombre")
    if columna is None:
        return {}
    libro = _abrir(ruta)
    try:
        hoja = _hoja(libro, deteccion.hoja)
        nombres = {}
        for fila in hoja.iter_rows(min_row=2, values_only=False):
            if columna < len(fila):
                valor = _texto(fila[columna].value)
                if valor:
                    nombres[fila[columna].row] = valor
        return nombres
    finally:
        try:
            libro.close()
        except Exception:  # noqa: BLE001
            pass


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
