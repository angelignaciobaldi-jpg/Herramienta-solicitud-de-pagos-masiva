"""Catálogo local de insumos y servicios.

Es el equivalente de `core/conceptos.py` para el otro desglose de SIPP. La
distinción no es de nombre sino de **a quién se le paga**: el portal pide
Conceptos de Pago a un Deudor o un Acreedor, e Insumos & Servicios a un
Proveedor (ver `catalogos.clase_desglose`). Por eso son dos catálogos y no uno
con etiquetas: el mismo texto puede existir en los dos sin ser lo mismo, y quien
captura no elige por el nombre sino por el tipo de beneficiario de la solicitud.

De ahí sale también la regla de la plantilla de Excel: la columna del desglose
ofrece **insumos solo cuando el beneficiario es Proveedor**, y conceptos en los
demás casos. Ofrecer las dos listas juntas invita a elegir de la equivocada, y
un renglón de la clase que no toca no falla al capturarlo: falla a media corrida,
cuando el robot no lo encuentra en la pestaña que SIPP muestra.

El catálogo de SIPP es enorme y la mayoría no se solicita nunca desde aquí, así
que **no se importa del portal**: se carga desde un archivo ya depurado por el
área (`leer_archivo`), con los servicios que de verdad se piden.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from core import db
# La regla para decidir si dos nombres son EL MISMO escrito distinto —acentos,
# capitalización, puntuación— es la misma en los dos catálogos, y tiene que
# seguir siéndolo: se reutiliza en vez de copiarse.
from core.conceptos import comparable, normalizar

ORIGEN_ARCHIVO = "ARCHIVO"
ORIGEN_MANUAL = "MANUAL"

# Encabezados con los que suele venir la columna del nombre en el archivo que
# entrega el área. Se buscan sin acentos ni mayúsculas.
_ENCABEZADOS = ("insumo", "servicio", "insumo o servicio", "nombre",
                "descripcion", "concepto", "insumos y servicios",
                "nombre insumo", "nombre del insumo")

# Encabezados de la columna del folio. SIPP lo llama «Folio Insumo» en su modal
# de búsqueda; el archivo del área puede llamarlo de varias formas.
_ENCABEZADOS_CLAVE = ("folio", "folio insumo", "clave", "id", "codigo",
                      "cve", "no", "numero", "id insumo", "clave insumo")


@dataclass
class Insumo:
    """Un insumo o servicio del catálogo local.

    `empresa` NO forma parte de su identidad: es solo el rastro de dónde salió.
    El catálogo es una lista única para todo el grupo, igual que el de conceptos.
    """

    id: str = field(default_factory=lambda: db._nuevo_id())
    nombre: str = ""
    # Folio del insumo en SIPP, si el catálogo del área lo trae. El portal deja
    # buscar por folio o por nombre; por folio es exacto, y los nombres son
    # larguísimos y se parecen entre sí.
    clave: str = ""
    empresa: str = ""
    origen: str = ORIGEN_ARCHIVO
    nota: str = ""
    creado_en: str = field(
        default_factory=lambda: db._ahora())

    @property
    def verificado(self) -> bool:
        """True si salió del catálogo depurado, no de un alta a mano."""
        return self.origen == ORIGEN_ARCHIVO


# --------------------------------------------------------------------------- #
#  Consulta
# --------------------------------------------------------------------------- #
def listar() -> list[Insumo]:
    """Todo el catálogo, en orden alfabético."""
    with db.conectar() as con:
        filas = con.execute(
            "SELECT * FROM insumo_servicio ORDER BY nombre").fetchall()
    return [db._desde_fila(Insumo, f) for f in filas]


def nombres() -> list[str]:
    """Nombres para alimentar un desplegable."""
    return [i.nombre for i in listar() if i.nombre]


def existe(nombre: str) -> bool:
    objetivo = comparable(nombre)
    return bool(objetivo) and any(
        comparable(i.nombre) == objetivo for i in listar())


def canonico(nombre: str) -> str:
    """El insumo tal y como está escrito en el catálogo.

    Devuelve el original si no hay ninguno equivalente. Mismo criterio que en
    los conceptos: se ignora cómo está escrito, no lo que dice.
    """
    objetivo = comparable(nombre)
    if not objetivo:
        return (nombre or "").strip()
    for i in listar():
        if comparable(i.nombre) == objetivo:
            return i.nombre
    return (nombre or "").strip()


def clave_de(nombre: str) -> str:
    """Folio del insumo en SIPP, si el catálogo lo trae.

    Es lo que el robot usa para buscarlo en el portal: el modal de SIPP acepta
    folio o nombre, y el folio es exacto. Los nombres del catálogo del área son
    larguísimos y se parecen entre sí —«AJUSTE Y CALIBRACION DE MANGUERAS DE
    COMBUSTIBLE DE GASOLINA» y «… DIESEL»—, así que buscar por nombre puede
    devolver varios y elegir mal.
    """
    objetivo = comparable(nombre)
    if not objetivo:
        return ""
    for i in listar():
        if comparable(i.nombre) == objetivo:
            return i.clave
    return ""


def hay_catalogo() -> bool:
    with db.conectar() as con:
        fila = con.execute(
            "SELECT COUNT(*) AS n FROM insumo_servicio").fetchone()
    return bool(fila and fila["n"])


# --------------------------------------------------------------------------- #
#  Alta y baja
# --------------------------------------------------------------------------- #
def guardar(nombre: str, empresa: str = "", origen: str = ORIGEN_MANUAL,
            nota: str = "", clave: str = "") -> Insumo | None:
    """Da de alta un insumo. Devuelve None si ya estaba (no es un error)."""
    limpio = normalizar(nombre)
    if not limpio:
        return None
    insumo = Insumo(nombre=limpio, clave=(clave or "").strip(),
                    empresa=empresa, origen=origen, nota=nota)
    with db.conectar() as con:
        choque = con.execute(
            "SELECT id FROM insumo_servicio WHERE nombre = ?",
            (limpio,)).fetchone()
        if choque:
            return None
        db._guardar(con, "insumo_servicio", insumo)
    return insumo


def borrar(insumo_id: str) -> None:
    with db.conectar() as con:
        con.execute("DELETE FROM insumo_servicio WHERE id = ?", (insumo_id,))


def vaciar() -> int:
    """Borra el catálogo entero y devuelve cuántos había.

    Existe porque el catálogo se carga de un archivo depurado: cuando el área
    entrega una versión nueva, lo que quiere es sustituir la lista, no acumular
    los servicios que ya quitó.
    """
    with db.conectar() as con:
        fila = con.execute(
            "SELECT COUNT(*) AS n FROM insumo_servicio").fetchone()
        con.execute("DELETE FROM insumo_servicio")
    return int(fila["n"]) if fila else 0


def importar(lista: list[str], empresa: str = "",
             origen: str = ORIGEN_ARCHIVO, reemplazar: bool = False) -> dict:
    """Guarda de golpe una lista de insumos.

    Con `reemplazar`, sustituye el catálogo en vez de sumarse a él: es lo que se
    quiere cuando el área entrega una versión depurada nueva.

    Devuelve `{"nuevos": n, "ya_estaban": n, "retirados": n}`.
    """
    retirados = vaciar() if reemplazar else 0
    nuevos = ya_estaban = 0
    for entrada in lista:
        # Se aceptan las dos formas: solo el nombre, o (clave, nombre). El
        # archivo del área trae el folio cuando lo tiene, y un alta a mano no.
        if isinstance(entrada, (tuple, list)):
            clave, nombre = (list(entrada) + ["", ""])[:2]
        else:
            clave, nombre = "", entrada
        if guardar(nombre, empresa, origen, clave=clave) is not None:
            nuevos += 1
        else:
            ya_estaban += 1
    return {"nuevos": nuevos, "ya_estaban": ya_estaban, "retirados": retirados}


# --------------------------------------------------------------------------- #
#  Carga desde el archivo depurado
# --------------------------------------------------------------------------- #
def leer_archivo(ruta: str) -> list[tuple[str, str]]:
    """Los insumos que trae un Excel o CSV, como pares `(clave, nombre)`.

    El archivo lo depura el área a mano, así que llega como llega: con una
    columna o con dos, con encabezado o sin él. Se buscan las columnas por su
    encabezado —«Folio Insumo» y «Nombre Insumo» son los que usa SIPP— y, si no
    hay ninguno reconocible, se toma la primera columna con texto como nombre;
    es lo que hace una lista de una sola columna, el caso más común.

    La clave importa: es el folio con el que el robot busca el insumo en el
    portal, que es exacto. Sin ella hay que buscar por nombre, y los nombres del
    catálogo son larguísimos y se parecen entre sí.

    Devuelve los nombres normalizados y SIN repetir, conservando el orden del
    archivo para que se revise en el mismo orden en que se entregó.
    """
    if not ruta or not os.path.isfile(ruta):
        return []
    filas = _filas(ruta)
    if not filas:
        return []

    col_nombre, col_clave = _columnas(filas)
    vistos: list[tuple[str, str]] = []
    conocidos: set[str] = set()
    for i, fila in enumerate(filas):
        if i == 0 and _es_encabezado(fila):
            continue
        if col_nombre >= len(fila):
            continue
        limpio = normalizar(fila[col_nombre])
        etiqueta = comparable(limpio)
        if not etiqueta or etiqueta in conocidos:
            continue
        clave = ""
        if col_clave is not None and col_clave < len(fila):
            # El folio se guarda tal cual, sin normalizar: es un identificador,
            # no un nombre. Un «1024.0» que Excel guardó como número sí se
            # limpia, o SIPP no lo encontraría.
            clave = (fila[col_clave] or "").strip()
            if clave.endswith(".0") and clave[:-2].isdigit():
                clave = clave[:-2]
        conocidos.add(etiqueta)
        vistos.append((clave, limpio))
    return vistos


def _filas(ruta: str) -> list[list[str]]:
    """El archivo como una tabla de texto, venga en Excel o en CSV."""
    if ruta.lower().endswith(".csv"):
        from core.adaptadores.excel import _delimitador, _texto_csv
        import csv
        import io as _io

        texto = _texto_csv(ruta)
        lector = csv.reader(_io.StringIO(texto),
                            delimiter=_delimitador(texto))
        return [[(c or "").strip() for c in fila] for fila in lector]

    from openpyxl import load_workbook

    libro = load_workbook(ruta, data_only=True, read_only=True)
    try:
        hoja = libro[libro.sheetnames[0]]
        filas = []
        for fila in hoja.iter_rows(values_only=True):
            filas.append([("" if c is None else str(c)).strip() for c in fila])
        return filas
    finally:
        libro.close()


def _es_encabezado(fila: list[str]) -> bool:
    return any(_parece_encabezado(c) for c in fila)


def _parece_encabezado(texto: str) -> bool:
    limpio = comparable(texto)
    return bool(limpio) and any(
        comparable(e) == limpio for e in _ENCABEZADOS)


def _parece_clave(texto: str) -> bool:
    limpio = comparable(texto)
    return bool(limpio) and any(
        comparable(e) == limpio for e in _ENCABEZADOS_CLAVE)


def _columnas(filas: list[list[str]]) -> tuple[int, "int | None"]:
    """`(columna del nombre, columna del folio o None)`."""
    encabezado = filas[0] if filas else []
    col_nombre = col_clave = None
    for indice, celda in enumerate(encabezado):
        if col_nombre is None and _parece_encabezado(celda):
            col_nombre = indice
        elif col_clave is None and _parece_clave(celda):
            col_clave = indice
    if col_nombre is not None:
        return col_nombre, col_clave

    # Sin encabezado reconocible. Con una sola columna, es el nombre; con dos o
    # más, el archivo del área pone el folio delante, así que la primera es la
    # clave y la siguiente el nombre —pero solo si la primera PARECE un folio:
    # códigos cortos, sin espacios—.
    columnas = max((len(f) for f in filas[:20]), default=0)
    if columnas >= 2 and _columna_de_folios(filas, 0):
        return 1, 0
    return _columna_de_nombres(filas), None


def _columna_de_folios(filas: list[list[str]], indice: int) -> bool:
    """True si esa columna tiene pinta de folios y no de nombres."""
    valores = [f[indice].strip() for f in filas[:20]
               if len(f) > indice and f[indice].strip()]
    if not valores:
        return False
    return all(len(v) <= 12 and " " not in v for v in valores)


def _columna_de_nombres(filas: list[list[str]]) -> int:
    """Qué columna trae el nombre del insumo."""
    for celda, indice in ((c, i) for c, i in zip(filas[0],
                                                 range(len(filas[0])))):
        if _parece_encabezado(celda):
            return indice
    # Sin encabezado reconocible: la primera columna con texto en los primeros
    # renglones. Una lista de una sola columna cae aquí, que es el caso normal.
    for indice in range(max((len(f) for f in filas[:20]), default=0)):
        if any(len(f) > indice and f[indice].strip() for f in filas[:20]):
            return indice
    return 0
