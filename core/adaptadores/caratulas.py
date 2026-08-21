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

De cada carátula se **lee la CLABE** (`adaptadores/ocr_caratula.py`), y esa CLABE
es la llave con la que la solicitud se empareja contra el Excel. Es un cambio de
fondo respecto de emparejar por nombre: el nombre lo teclea una persona en dos
lugares distintos —el archivo y la hoja— y basta un acento, un apellido de más o
un «MA.» por «MARÍA» para que no case; la CLABE es el mismo número en los dos
lados, y además es el dato del que depende a quién se le paga.

El **titular de la cuenta es el beneficiario**: es lo que la carátula acredita,
y por eso manda sobre el nombre del archivo, que queda de respaldo para cuando
el OCR no lo alcanza a leer.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

from core import catalogos, documentos, validador
from core.adaptadores import ocr_caratula
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


# De dónde salió el nombre del beneficiario. Se conserva porque cambia lo que la
# interfaz debe mostrar y lo que se puede dar por bueno sin revisar.
NOMBRE_DE_OCR = "OCR"          # el titular que acredita la carátula
NOMBRE_DE_ARCHIVO = "ARCHIVO"  # respaldo: lo que se dedujo del nombre del archivo
NOMBRE_A_MANO = "MANUAL"       # lo corrigió el usuario en la tabla

# Cómo se emparejó con el Excel.
POR_CLABE = "CLABE"
POR_NOMBRE = "NOMBRE"


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
    # Lectura de la carátula, tal cual salió del OCR (None si no se leyó).
    ocr: ocr_caratula.DatosCaratula | None = None
    origen_nombre: str = NOMBRE_DE_ARCHIVO
    emparejado_por: str = ""
    # Discrepancias entre lo que dice la carátula y lo que dice el Excel. No son
    # errores del validador: la solicitud se puede dar de alta igual, pero
    # alguien tiene que verlas.
    avisos: list[str] = field(default_factory=list)

    @property
    def archivo(self) -> str:
        return os.path.basename(self.ruta)

    @property
    def hallazgos(self) -> list:
        return validador.validar(self.solicitud, self.partidas)

    @property
    def listo(self) -> bool:
        return not validador.hay_errores(self.hallazgos)

    @property
    def clabe_confiable(self) -> bool:
        """La CLABE pasa su dígito verificador.

        Se consulta contra la CLABE ACTUAL de la solicitud y no contra la que
        leyó el OCR, porque el usuario pudo corregirla a mano en la tabla —que
        es justo lo que se espera que haga cuando el verificador no cuadra—.
        """
        return ocr_caratula.clabe_valida(self.solicitud.cuenta_clabe)


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
                     parada: str = "LLENADA",
                     leer_caratula: bool = True,
                     on_progreso=None, cancelado=None) -> list[Borrador]:
    """Un borrador por archivo, leyendo cada carátula para sacar sus datos.

    Los borradores nacen **incompletos a propósito**: les falta importe, fecha y
    concepto, que solo puede aportar el Excel o la captura manual. El validador
    los marca y la interfaz lo muestra; lo importante es que ya tienen
    beneficiario, CLABE y carátula, que es lo que la carpeta sí sabe.

    Args:
        leer_caratula: si es False no se abre ningún archivo y el nombre sale
            del nombre del archivo, como antes de que hubiera OCR.
        on_progreso: `(procesados, total, archivo)` tras cada carátula. Leer una
            carpeta entera toma segundos por archivo y sin esto la interfaz se
            queda muda.
        cancelado: callable consultado entre archivos; al devolver True se
            devuelve lo que se lleve leído en vez de seguir.
    """
    archivos = listar_archivos(rutas)
    borradores = []
    for indice, ruta in enumerate(archivos, start=1):
        if cancelado is not None and cancelado():
            break

        del_archivo = nombre_desde_archivo(ruta)
        datos = ocr_caratula.leer(ruta, cancelado=cancelado) if leer_caratula \
            else None

        # El titular que acredita la carátula manda sobre el nombre del archivo:
        # es el que va a quedar registrado como beneficiario en SIPP y el que
        # tiene que corresponder con la cuenta a la que se paga. El nombre del
        # archivo lo escribió alguien de memoria al armar la carpeta.
        if datos and datos.titular:
            nombre, origen = datos.titular.upper(), NOMBRE_DE_OCR
        else:
            nombre, origen = del_archivo, NOMBRE_DE_ARCHIVO

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
            # El nombre de la cuenta ES el del beneficiario: la carátula acredita
            # que esa cuenta es de esa persona, y en SIPP el titular se captura
            # al dar de alta la cuenta bancaria.
            cuenta_titular=nombre,
            beneficiario_rfc=(datos.rfc if datos else ""),
            cuenta_clabe=(datos.clabe if datos else ""),
            cuenta_banco=(datos.banco if datos else ""),
            forma_pago=catalogos.FORMA_PAGO_CON_CUENTA,
            tipo_gasto="No Deducible",
            moneda=catalogos.MONEDA_DEFECTO,
            origen="CARATULA",
            parada=parada,
            estado="PENDIENTE")

        borrador = Borrador(ruta=ruta, nombre_detectado=nombre,
                            solicitud=solicitud, ocr=datos,
                            origen_nombre=origen)
        if datos:
            borrador.avisos = list(datos.avisos)
            if datos.error:
                borrador.avisos.append(datos.error)
        borradores.append(borrador)

        if callable(on_progreso):
            on_progreso(indice, len(archivos), os.path.basename(ruta))
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


def _clabe_comparable(valor: str) -> str:
    """Solo los dígitos: el Excel guarda la CLABE con espacios o como número."""
    return re.sub(r"\D+", "", valor or "")


def completar_con_excel(borradores: list[Borrador], filas_excel: list) -> dict:
    """Vuelca los datos del Excel sobre los borradores, emparejando por CLABE.

    `filas_excel` son `FilaImportada` de `adaptadores/excel.py`.

    El emparejamiento va en **dos pasadas, y el orden es la decisión de fondo**:

    1. **Por CLABE.** Es el mismo número en los dos lados, así que no hay forma
       de que «no case por poco». Además es el dato del que depende a quién se
       le paga: si la CLABE de la carátula y la del Excel son la misma, no hay
       duda de que ese renglón es de esa persona.
    2. **Por nombre**, solo para lo que quedó suelto: cuando el OCR no pudo leer
       la CLABE, o cuando el Excel no la trae. Recupera los casos que la primera
       pasada no puede resolver, en vez de mandarlos a captura manual.

    Las dos pasadas van COMPLETAS, una después de la otra. Resolviendo borrador
    por borrador, un emparejamiento por nombre podría consumir la fila que a
    otra carátula le correspondía por CLABE, que es la llave fuerte.

    Reglas que se conservan:

    - **La carátula manda sobre el beneficiario y su cuenta.** Ni el nombre ni
      la CLABE se sobrescriben con los del Excel: la carátula es el documento
      que acredita que esa cuenta es de esa persona. Si difieren, se empareja
      igual y se deja constancia en `borrador.avisos`, porque una discrepancia
      ahí casi siempre significa que el Excel trae la fila de otra persona.
    - Solo se copian los campos **vacíos** del borrador, para no pisar lo que el
      usuario ya haya corregido a mano.
    - Una fila del Excel se usa **una sola vez**: dos carátulas no pueden
      alimentarse del mismo renglón sin que alguien se entere.

    Devuelve un resumen con lo que quedó sin emparejar de los dos lados.
    """
    usadas: set[int] = set()
    emparejados = 0
    discrepancias = 0

    def fila_por_clabe(clabe: str):
        if not clabe:
            return None
        return next((f for f in filas_excel
                     if f.numero not in usadas
                     and _clabe_comparable(f.solicitud.cuenta_clabe) == clabe),
                    None)

    def fila_por_nombre(nombre: str):
        if not nombre:
            return None
        return next((f for f in filas_excel
                     if f.numero not in usadas
                     and documentos.nombres_coinciden(
                         nombre, f.solicitud.beneficiario_nombre)),
                    None)

    pendientes = []
    for borrador in borradores:
        clabe = _clabe_comparable(borrador.solicitud.cuenta_clabe)
        fila = fila_por_clabe(clabe)
        if fila is None:
            pendientes.append(borrador)
            continue
        usadas.add(fila.numero)
        borrador.emparejado_por = POR_CLABE
        _volcar(borrador, fila)
        emparejados += 1

    for borrador in pendientes:
        fila = fila_por_nombre(borrador.solicitud.beneficiario_nombre)
        if fila is None:
            continue
        usadas.add(fila.numero)
        borrador.emparejado_por = POR_NOMBRE
        # Emparejó por nombre teniendo las dos CLABE: son distintas, y eso es
        # justo lo que hay que mirar antes de pagar.
        propia = _clabe_comparable(borrador.solicitud.cuenta_clabe)
        del_excel = _clabe_comparable(fila.solicitud.cuenta_clabe)
        if propia and del_excel and propia != del_excel:
            discrepancias += 1
            borrador.avisos.append(
                f"La CLABE del Excel ({del_excel}) no es la de la carátula "
                f"({propia}). Se conserva la de la carátula, que es la que "
                f"acredita la cuenta: confirma que el renglón sea de esta "
                f"persona.")
        _volcar(borrador, fila)
        emparejados += 1

    sin_excel = [b.nombre_detectado or b.archivo
                 for b in borradores if not b.completado]
    sin_caratula = [f.solicitud.beneficiario_nombre for f in filas_excel
                    if f.numero not in usadas]
    return {
        "emparejados": emparejados,
        "por_clabe": sum(1 for b in borradores if b.emparejado_por == POR_CLABE),
        "por_nombre": sum(1 for b in borradores
                          if b.emparejado_por == POR_NOMBRE),
        "discrepancias": discrepancias,
        "sin_excel": sin_excel,
        "sin_caratula": sin_caratula,
    }


def _volcar(borrador: Borrador, fila) -> None:
    """Copia a un borrador los datos de su fila del Excel."""
    borrador.fila_excel = fila.numero
    borrador.completado = True

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
        destino.cuenta_titular = destino.beneficiario_nombre
