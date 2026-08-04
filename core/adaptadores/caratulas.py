"""CaratulaAdapter: cada carátula bancaria es una solicitud.

Invierte el orden habitual de la carga masiva, y lo hace porque así llega el
trabajo: el área recibe una carpeta con una carátula por persona, y **esa
carpeta es el listado real de a quién hay que pagarle**. De cada archivo sale
un borrador de solicitud con el beneficiario ya identificado y su carátula ya
adjunta; el Excel llega después, solo a completar los datos que el nombre del
archivo no puede dar (empresa, importe, concepto, CLABE…).

La ventaja sobre el orden inverso es que **ninguna solicitud puede quedarse sin
carátula**: no existe si no hay archivo. Y como SIPP exige la carátula para dar
de alta la cuenta, eso elimina de raíz el caso que más trabajo manual genera.

El nombre del beneficiario se extrae del **nombre del archivo**, descartando el
ruido que suelen traer (`CARATULA`, el banco, meses, folios). No se lee el
contenido del PDF: los formatos de carátula varían por banco y el OCR sería
mucho menos confiable que el propio nombre del archivo, que es el que la
persona que armó la carpeta escribió a propósito.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

from core import catalogos, documentos, validador
from core.db import CONCEPTO, INSUMO, Partida, Solicitud

EXTENSIONES = documentos.EXTENSIONES

# Palabras que aparecen en el nombre de una carátula y NO son parte del nombre.
_RUIDO = {
    # Tipo de documento, incluidas las abreviaturas que se usan de verdad
    # («Edo Cta», «Bco»): sin ellas se colaban al nombre del beneficiario.
    "caratula", "caratulas", "estado", "estados", "cuenta", "cuentas", "edo",
    "edos", "cta", "ctas", "bco", "bcos", "suc", "sucursal",
    "vobo", "voboo", "bueno", "visto", "comprobante", "constancia", "copia",
    "scan", "escaneo", "img", "image", "imagen", "foto", "doc", "documento",
    "archivo", "final", "firmado", "nuevo", "nueva", "rev", "folio", "num",
    "ref", "referencia", "exp", "expediente",
    # Bancos
    "bbva", "bancomer", "banorte", "santander", "hsbc", "scotiabank",
    "scotia", "banamex", "citibanamex", "citi", "azteca", "inbursa", "bajio",
    "afirme", "banregio", "mifel", "multiva", "sabadell", "invex", "actinver",
    "bancoppel", "coppel", "compartamos", "banbajio", "spin", "oxxo", "nubank",
    "klar", "hey", "stp", "banca", "banco",
    # Meses y periodos
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "setiembre", "octubre", "noviembre", "diciembre",
    "ene", "feb", "mar", "abr", "jun", "jul", "ago", "sep", "sept", "oct",
    "nov", "dic", "periodo", "quincena",
    # Otros
    "clabe", "spei", "transferencia", "pago", "pagos", "deposito", "nomina",
    "finiquito", "ptu", "empleado", "colaborador", "excolaborador",
}


@dataclass
class Borrador:
    """Una carátula convertida en solicitud, antes de guardarse."""

    ruta: str
    nombre_detectado: str = ""
    solicitud: Solicitud = field(default_factory=Solicitud)
    partidas: list[Partida] = field(default_factory=list)
    # Fila del Excel con la que se emparejó (None si no hubo).
    fila_excel: int | None = None
    completado: bool = False

    @property
    def archivo(self) -> str:
        return os.path.basename(self.ruta)

    @property
    def hallazgos(self) -> list:
        return validador.validar(self.solicitud, self.partidas)

    @property
    def listo(self) -> bool:
        return not validador.hay_errores(self.hallazgos)


def _sin_acentos(texto: str) -> str:
    """Forma comparable de una palabra: sin acentos y en minúsculas."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def nombre_desde_archivo(ruta: str) -> str:
    """Nombre del beneficiario a partir del nombre del archivo.

    Descarta las palabras de `_RUIDO`, los números (folios, fechas) y las
    palabras de una o dos letras (iniciales y partículas). Lo que queda se
    devuelve en MAYÚSCULAS, que es como SIPP guarda los nombres.

    **Los acentos y la Ñ se conservan.** Solo se quitan para decidir si una
    palabra es ruido: el nombre que sale de aquí se registra tal cual en SIPP al
    dar de alta al beneficiario, y «JOSE NUNO» en vez de «JOSÉ ÑUÑO» deja mal
    escrito a alguien en el catálogo del ERP para siempre.

    Si no queda nada reconocible devuelve cadena vacía: es preferible a inventar
    un nombre, porque la interfaz lo marca y deja corregirlo a mano.
    """
    base = os.path.splitext(os.path.basename(ruta or ""))[0]
    # Se separa por lo que NO es letra ni dígito, conservando los caracteres
    # originales de cada palabra.
    crudas = re.split(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", base)
    palabras = []
    for palabra in crudas:
        comparable = _sin_acentos(palabra)
        if (not comparable or comparable in _RUIDO
                or comparable.isdigit() or len(comparable) <= 2):
            continue
        palabras.append(palabra.upper())
    return " ".join(palabras).strip()


def listar_archivos(rutas: list[str], recursivo: bool = True) -> list[str]:
    """Archivos válidos a partir de rutas sueltas y/o carpetas."""
    salida: list[str] = []
    for ruta in rutas or []:
        if os.path.isdir(ruta):
            for raiz, _dirs, archivos in os.walk(ruta):
                salida += [os.path.join(raiz, a) for a in archivos
                           if a.lower().endswith(EXTENSIONES)]
                if not recursivo:
                    break
        elif os.path.isfile(ruta) and ruta.lower().endswith(EXTENSIONES):
            salida.append(ruta)
    # Sin duplicados y en orden alfabético: el mismo archivo puede llegar suelto
    # y dentro de una carpeta elegida en la misma tanda.
    return sorted(set(salida), key=lambda r: os.path.basename(r).lower())


def crear_borradores(rutas: list[str], lote_id: str = "", *,
                     empresa: str = "", sucursal: str = "",
                     tipo_beneficiario: str = "Acreedor",
                     parada: str = "LLENADA") -> list[Borrador]:
    """Un borrador por archivo, con el nombre extraído y los valores por defecto.

    Los borradores nacen **incompletos a propósito**: les falta importe, fecha y
    concepto, que solo puede aportar el Excel o la captura manual. El validador
    los marca y la interfaz lo muestra; lo importante es que ya tienen
    beneficiario y carátula, que es lo que la carpeta sí sabe.
    """
    borradores = []
    for ruta in listar_archivos(rutas):
        nombre = nombre_desde_archivo(ruta)
        solicitud = Solicitud(
            lote_id=lote_id,
            empresa=empresa,
            sucursal=sucursal,
            tipo_beneficiario=tipo_beneficiario,
            # No se presupone si está dado de alta: lo resuelve el robot
            # consultando SIPP. Tener carátula sugiere que es nuevo, pero
            # sugerir no es saber, y el portal es quien sabe.
            beneficiario_nuevo=0,
            beneficiario_nombre=nombre,
            cuenta_titular=nombre,
            forma_pago=catalogos.FORMA_PAGO_CON_CUENTA,
            tipo_gasto="No Deducible",
            moneda=catalogos.MONEDA_DEFECTO,
            origen="CARATULA",
            parada=parada,
            estado="PENDIENTE")
        borradores.append(Borrador(ruta=ruta, nombre_detectado=nombre,
                                   solicitud=solicitud))
    return borradores


# --------------------------------------------------------------------------- #
#  Emparejamiento con el Excel
# --------------------------------------------------------------------------- #
# Campos que el Excel puede completar. El beneficiario NO está: ese lo manda la
# carátula, que es la que decide a quién se le paga.
_CAMPOS_DEL_EXCEL = (
    "empresa", "sucursal", "tipo_beneficiario", "beneficiario_folio",
    "beneficiario_rfc", "beneficiario_correo", "cuenta_banco", "cuenta_clabe",
    "cuenta_titular", "forma_pago", "tipo_gasto", "fecha_pago", "moneda",
    "descripcion",
)


def completar_con_excel(borradores: list[Borrador], filas_excel: list) -> dict:
    """Vuelca los datos del Excel sobre los borradores, emparejando por nombre.

    `filas_excel` son `FilaImportada` de `adaptadores/excel.py`. El
    emparejamiento es por nombre de beneficiario, tolerando orden y acentos
    (`documentos.nombres_coinciden`).

    Reglas deliberadas:

    - **La carátula manda sobre el beneficiario.** El nombre no se sobrescribe
      con el del Excel: si difieren, el que vale es el del archivo, porque es el
      que acredita la cuenta a la que se va a pagar.
    - Solo se copian los campos **vacíos** del borrador, para no pisar lo que el
      usuario ya haya corregido a mano.
    - Una fila del Excel se usa **una sola vez**: dos carátulas no pueden
      alimentarse del mismo renglón sin que alguien se entere.

    Devuelve un resumen con lo que quedó sin emparejar de los dos lados.
    """
    usadas: set[int] = set()
    emparejados = 0
    for borrador in borradores:
        nombre = borrador.solicitud.beneficiario_nombre
        if not nombre:
            continue
        fila = next(
            (f for f in filas_excel
             if f.numero not in usadas
             and documentos.nombres_coinciden(
                 nombre, f.solicitud.beneficiario_nombre)),
            None)
        if fila is None:
            continue
        usadas.add(fila.numero)
        borrador.fila_excel = fila.numero
        borrador.completado = True
        emparejados += 1

        destino, origen = borrador.solicitud, fila.solicitud
        for campo in _CAMPOS_DEL_EXCEL:
            valor = getattr(origen, campo, "")
            if valor and not getattr(destino, campo, ""):
                setattr(destino, campo, valor)
        # Las partidas se copian completas: el borrador no tenía ninguna.
        borrador.partidas = [Partida(**{**p.__dict__, "id": Partida().id,
                                        "solicitud_id": destino.id})
                             for p in fila.partidas]
        # El desglose tiene que corresponder al tipo de beneficiario final, que
        # pudo cambiar al copiar los datos del Excel.
        clase = catalogos.clase_desglose(destino.tipo_beneficiario)
        for p in borrador.partidas:
            if p.clase != clase:
                p.clase = clase
                if clase == CONCEPTO and not p.concepto_nombre:
                    p.concepto_nombre = p.insumo_nombre
                elif clase == INSUMO and not p.insumo_nombre:
                    p.insumo_nombre = p.concepto_nombre
        destino.importe_total = round(
            sum(p.importe for p in borrador.partidas if p.clase == clase), 2)
        if not destino.cuenta_titular:
            destino.cuenta_titular = nombre

    sin_excel = [b.nombre_detectado or b.archivo
                 for b in borradores if not b.completado]
    sin_caratula = [f.solicitud.beneficiario_nombre for f in filas_excel
                    if f.numero not in usadas]
    return {
        "emparejados": emparejados,
        "sin_excel": sin_excel,
        "sin_caratula": sin_caratula,
    }
