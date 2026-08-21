"""Lectura de una carátula bancaria: CLABE, titular, banco y RFC.

Sobre el texto que devuelve `core/ocr.py` (capa de texto del PDF, o Tesseract si
está escaneado), este módulo interpreta lo que la carátula acredita. El dato que
importa es la **CLABE**: es la llave con la que la solicitud se empareja contra
el Excel, y la que decide a qué cuenta se va a pagar.

La lógica está portada del **Extractor Bancario** de Norte Operativo
(`OCR Bancario/extractor_bancario.py`), que ya se afinó contra carátulas reales
de los bancos con los que opera el grupo. Se conservan sus tres decisiones
importantes, que son las que evitan los errores caros:

1. **La CLABE se valida con su dígito verificador** (pesos 3·7·1). Un dígito mal
   leído por el OCR produce una CLABE con la forma correcta y el destinatario
   equivocado; el verificador atrapa el 90% de esos casos.
2. **El RFC del banco nunca se devuelve como el del titular.** Aparece en el
   encabezado de casi toda carátula y es el falso positivo más frecuente.
3. **Un nombre no se acepta solo por estar donde debería.** Pasa por un filtro
   que descarta domicilios, encabezados, avisos legales y nombres de banco,
   porque un titular equivocado da de alta mal al beneficiario en SIPP.

Lo que NO hace: decidir. Devuelve lo que leyó y qué tan confiable es; quién
manda entre la carátula y el Excel se resuelve en `adaptadores/caratulas.py`.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

from core import ocr
# El dígito verificador de la CLABE es una regla del dato, no de cómo se leyó,
# así que vive en el validador y aquí solo se reexporta: quien lee una carátula
# la usa con el mismo nombre, sin que existan dos implementaciones que se puedan
# separar con el tiempo.
from core.validador import clabe_valida

# --------------------------------------------------------------------------- #
#  Catálogos
# --------------------------------------------------------------------------- #
# Los tres primeros dígitos de la CLABE identifican al banco emisor. Es la vía
# más confiable para saber el banco: no depende de que el logo se lea bien.
BANCOS_POR_CLABE = {
    "002": "Citibanamex",
    "012": "BBVA",
    "014": "Santander",
    "021": "HSBC",
    "030": "Banco del Bajío",
    "036": "Inbursa",
    "042": "Mifel",
    "044": "Scotiabank",
    "058": "Banregio",
    "059": "Invex",
    "060": "Bansi",
    "062": "Afirme",
    "072": "Banorte",
    "127": "Banco Azteca",
    "137": "BanCoppel",
    "143": "CIBanco",
    "728": "Spin",   # NVIO Pagos México (Spin by OXXO y otras fintech)
}

# Cómo aparece cada banco escrito en el encabezado de sus propias carátulas.
ALIAS_BANCOS = [
    ("citibanamex", "Citibanamex"),
    ("banamex", "Citibanamex"),
    ("bbva", "BBVA"),
    ("bancomer", "BBVA"),
    ("santander", "Santander"),
    ("hsbc", "HSBC"),
    ("banco del bajio", "Banco del Bajío"),
    ("bajio", "Banco del Bajío"),
    ("scotiabank", "Scotiabank"),
    ("banregio", "Banregio"),
    ("invex", "Invex"),
    ("bansi", "Bansi"),
    ("banorte", "Banorte"),
    ("mercantil del norte", "Banorte"),
    ("banco azteca", "Banco Azteca"),
    ("azteca", "Banco Azteca"),
    ("cibanco", "CIBanco"),
    ("bancoppel", "BanCoppel"),
    ("coppel", "BanCoppel"),
    ("inbursa", "Inbursa"),
    ("afirme", "Afirme"),
    ("mifel", "Mifel"),
    ("spin", "Spin"),
    ("compropago", "Spin"),
]

# RFC de las propias instituciones financieras. Aparecen en el encabezado de
# casi cualquier carátula y NO deben confundirse con el RFC del titular: darlo
# de alta como suyo en SIPP factura el pago a nombre del banco.
RFC_BANCOS = {
    "BRM940216EQ6",   # Banco Regional (Banregio)
    "COM131212AI3",   # Compropago (Spin by OXXO)
    "BBA830831LJ2",   # BBVA México
    "BNM840515VB1",   # Banco Nacional de México (Citibanamex)
    "BMN930209927",   # Banco Mercantil del Norte (Banorte)
    "HMI950125KG8",   # HSBC México
    "BMS9007158J9",   # Banco Santander México
    "BSM970519DU8",   # Banco Santander México (variante)
    "SIN960904468",   # Scotiabank Inverlat
}

# Marcadores de que un RFC pertenece a la institución, no al titular.
_CONTEXTO_RFC_BANCO = (
    "institucion de banca", "banca multiple", "grupo financiero",
    "institucion de fondos", "banco regional",
)

# 18 dígitos, tolerando el espacio o guion cada pocos caracteres con que los
# bancos maquetan la CLABE.
_RE_CLABE = re.compile(r"(?<!\d)(?:\d[\s\-]?){17}\d(?!\d)")
_RE_CUENTA = re.compile(r"\b(?:\d[\d\s\-]{7,24}\d)\b")

# Etiquetas con las que una carátula anuncia al titular, de la más específica a
# la más genérica: el orden importa porque «titular» a secas también aparece en
# encabezados como «TARJETA (TITULAR)».
ETIQUETAS_TITULAR = [
    "nombre del beneficiario", "beneficiario", "nombre del titular",
    "titular de la cuenta", "titular de cuenta", "titular",
    "nombre del cuentahabiente", "cuentahabiente",
    "nombre del dueño de la cuenta", "dueño de la cuenta",
    "propietario de la cuenta", "a nombre de",
    "razon social", "nombre razon social", "nombre / razon social",
]

ETIQUETAS_RFC = [
    "rfc", "r f c", "r.f.c", "r.f.c.",
    "registro federal de contribuyentes", "registro federal",
    "clave del rfc", "datos fiscales", "datos de facturacion", "contribuyente",
]

ETIQUETAS_CUENTA = [
    "numero de cuenta", "numero cuenta", "nro de cuenta", "no. de cuenta",
    "no de cuenta", "cuenta bancaria", "cuenta", "cta",
]

# Partículas cortas válidas en nombres de personas y razones sociales: sin esta
# lista, «JOSE DE LA CRUZ» perdería el «DE» y el «LA» al limpiar el ruido.
_PARTICULAS = {
    "DE", "DEL", "LA", "LAS", "LOS", "Y", "E", "SAN", "SANTA", "DA", "DI", "DO",
    "MC", "MAC", "VON", "VAN", "SA", "CV", "RL", "SC", "SAS", "SAB", "SAPI",
    "SADECV",
}

# Palabras que descartan un renglón como nombre del titular: etiquetas del
# formato, líneas de domicilio, encabezados y avisos legales.
_NO_ES_NOMBRE = (
    "banco", "clabe", "rfc", "cuenta", "iban", "sucursal", "referencia",
    "datos", "fiscal", "domicilio", "direccion", "correo", "telefono", "firma",
    "estado", "fecha", "cliente", "producto", "contrato", "tarjeta", "nomina",
    "libreton", "toque", "posterior", "solicitante", "autorizador", "apellido",
    "nacimiento", "nacionalidad", "celular", "empleador", "autenticacion",
    "vendedor", "saldo", "disponible", "periodo", "pagina",
    "institucion", "fondos de pago", "banca multiple", "grupo financiero",
    # Nombres de banco que no son palabras comunes (se evitan «bajío»,
    # «azteca» y similares, que sí pueden ser apellidos).
    "santander", "banregio", "banorte", "bancomer", "scotiabank", "hsbc",
    "citibanamex", "bancoppel",
    # Textos legales.
    "incumplir", "obligaciones", "moratorios", "comision", "intereses",
    "generar",
)

_PALABRAS_DOMICILIO = {
    "av", "ave", "avenida", "calle", "col", "colonia", "blvd", "boulevard",
    "carretera", "priv", "privada", "fracc", "fraccionamiento", "manzana", "mz",
    "lote", "lt", "andador", "cp", "int", "ext", "esq", "circuito", "cda",
    "cerrada", "prolongacion", "retorno", "diagonal", "calzada", "depto",
    "edificio",
}

_MARCAS_RAZON_SOCIAL = {
    "sa", "s.a", "s.a.", "cv", "c.v", "c.v.", "de", "rl", "r.l", "r.l.",
    "decv", "sociedad", "anónima", "anonima", "empresa", "compania", "corp",
    "corporativo", "servicios", "industriales",
}

# RFC genéricos del SAT: no identifican a nadie, así que valen lo mismo que
# no tener RFC.
RFC_GENERICOS = {"XAXX010101000", "XEXX010101000"}


@dataclass
class DatosCaratula:
    """Lo que se pudo leer de una carátula. Sin decidir nada todavía."""

    ruta: str
    clabe: str = ""
    titular: str = ""
    banco: str = ""
    rfc: str = ""
    cuenta: str = ""
    uso_ocr: bool = False
    error: str = ""
    avisos: list[str] = field(default_factory=list)

    @property
    def archivo(self) -> str:
        return os.path.basename(self.ruta)

    @property
    def clabe_confiable(self) -> bool:
        """La CLABE tiene 18 dígitos y su verificador cuadra."""
        return clabe_valida(self.clabe)

    @property
    def vacio(self) -> bool:
        return not (self.clabe or self.titular or self.rfc)


# --------------------------------------------------------------------------- #
#  Normalización
# --------------------------------------------------------------------------- #
def _espacios(valor: str) -> str:
    return re.sub(r"\s+", " ", valor or "").strip(" \t\r\n:-|")


def _solo_digitos(valor: str) -> str:
    return re.sub(r"\D+", "", valor or "")


# Confusiones típicas del OCR letra→dígito, para campos que solo pueden ser
# números (la CLABE): el OCR lee '5' como 'S', '0' como 'O'…
_A_DIGITO = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1",
                           "S": "5", "B": "8", "G": "6", "Z": "2", "T": "7",
                           "A": "4"})
# La inversa, para las letras del RFC.
_A_LETRA = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "6": "G",
                          "2": "Z", "4": "A"})


def _digitos_corrigiendo_ocr(valor: str) -> str:
    return re.sub(r"\D+", "", (valor or "").upper().translate(_A_DIGITO))


def _comparable(valor: str) -> str:
    """Forma sin acentos, en minúsculas y sin puntuación, para comparar."""
    ascii_valor = unicodedata.normalize("NFKD", valor or "").encode(
        "ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_valor.lower()).strip()


def _rfc_comparable(valor: str) -> str:
    return _comparable(valor).replace(" ", "").upper()


# --------------------------------------------------------------------------- #
#  CLABE
# --------------------------------------------------------------------------- #
def _extraer_clabe(texto: str) -> str:
    candidatas: list[str] = []

    etiquetada = _valor_tras_etiqueta_simple(
        texto, ["clabe interbancaria", "clabe"])
    if etiquetada:
        digitos = _solo_digitos(etiquetada)
        if len(digitos) == 18:
            candidatas.append(digitos)
        else:
            # Reintento corrigiendo confusiones del OCR (el último '5' leído
            # como 'S' deja la CLABE en 17 dígitos y parecería no estar).
            corregida = _digitos_corrigiendo_ocr(etiquetada)
            if len(corregida) == 18:
                candidatas.append(corregida)

    for cruda in _RE_CLABE.findall(texto):
        digitos = _solo_digitos(cruda)
        if len(digitos) == 18:
            candidatas.append(digitos)

    # Ventanas de 18 dígitos dentro de cada renglón: en las tablas de algunos
    # formatos la CLABE va pegada a otros números. Solo se aceptan ventanas con
    # prefijo de banco conocido Y verificador válido, que juntos hacen
    # prácticamente imposible el falso positivo.
    for linea in texto.splitlines():
        digitos = _solo_digitos(linea)
        for i in range(0, max(0, len(digitos) - 17)):
            ventana = digitos[i:i + 18]
            if ventana[:3] in BANCOS_POR_CLABE and clabe_valida(ventana):
                candidatas.append(ventana)

    if not candidatas:
        return ""

    # Gana la que tiene prefijo de banco conocido y verificador válido.
    def puntaje(clabe: str) -> int:
        return ((2 if clabe[:3] in BANCOS_POR_CLABE else 0)
                + (1 if clabe_valida(clabe) else 0))

    candidatas.sort(key=puntaje, reverse=True)
    return candidatas[0]


# --------------------------------------------------------------------------- #
#  Lectura de valores etiquetados
# --------------------------------------------------------------------------- #
def _valor_tras_etiqueta_simple(texto: str, etiquetas: list[str]) -> str:
    """Lo que sigue a la etiqueta, buscándola como subcadena en minúsculas.

    Es la versión tolerante: no normaliza acentos, así que se usa para
    etiquetas que no los llevan (CLABE). Si la etiqueta cierra el renglón,
    devuelve el primer renglón no vacío que le siga.
    """
    lineas = [linea.strip() for linea in texto.splitlines()]
    for indice, linea in enumerate(lineas):
        compacta = _espacios(linea)
        minusculas = compacta.lower()
        for etiqueta in etiquetas:
            posicion = minusculas.find(etiqueta)
            if posicion == -1:
                continue
            candidato = compacta[posicion + len(etiqueta):].strip(" \t:-|")
            if candidato:
                return _espacios(candidato)
            for siguiente in lineas[indice + 1:]:
                siguiente = _espacios(siguiente)
                if siguiente:
                    return siguiente
    return ""


def _cola_tras_etiqueta(linea: str, etiqueta_normalizada: str) -> str:
    """Lo que sigue a la etiqueta DENTRO del mismo renglón.

    Trabaja sobre los tokens originales y su forma normalizada en paralelo,
    porque normalizar puede partir un token en varios ('21/04/2026' →
    '21 04 2026') y hay que devolver el texto original, no el normalizado.
    """
    tokens_etiqueta = etiqueta_normalizada.split()
    if not tokens_etiqueta:
        return ""
    originales = linea.split()
    plano: list[tuple[str, int]] = []
    for indice, token in enumerate(originales):
        for palabra in _comparable(token).split():
            plano.append((palabra, indice))
    palabras = [p for p, _ in plano]
    n = len(tokens_etiqueta)
    for i in range(len(palabras) - n + 1):
        if palabras[i:i + n] == tokens_etiqueta:
            desde = plano[i + n - 1][1] + 1
            cola = " ".join(originales[desde:]).strip(" \t:-|")
            if cola:
                return cola
    return ""


def _valor_tras_etiqueta(texto: str, etiquetas: list[str]) -> str:
    """Versión que compara sin acentos y respeta el orden de prioridad.

    Recorre las etiquetas de la más específica a la más genérica sobre TODO el
    texto, para que «no. de cuenta» gane a un «cuenta» suelto que aparecería en
    un encabezado como «Estado de Cuenta».
    """
    normalizadas = [_comparable(e) for e in etiquetas]
    lineas = [_espacios(l) for l in texto.splitlines() if _espacios(l)]
    comparables = [_comparable(l) for l in lineas]
    for etiqueta in normalizadas:
        for indice, linea in enumerate(lineas):
            if etiqueta not in comparables[indice]:
                continue
            misma = _cola_tras_etiqueta(linea, etiqueta)
            if misma:
                return _espacios(misma)
            partes = re.split(r"[:\-|]", linea, maxsplit=1)
            if len(partes) > 1:
                valor = _espacios(partes[1])
                if valor:
                    return valor
            for siguiente in lineas[indice + 1:]:
                if siguiente:
                    return siguiente
    return ""


# --------------------------------------------------------------------------- #
#  Titular
# --------------------------------------------------------------------------- #
def _parece_nombre(valor: str) -> bool:
    """Filtro que decide si un renglón puede ser el nombre del titular.

    Es deliberadamente estricto. Un titular equivocado no falla ruidosamente:
    da de alta mal al beneficiario en SIPP y queda así en el catálogo del ERP.
    """
    limpio = _espacios(valor)
    if not limpio or any(c.isdigit() for c in limpio):
        return False
    # Un nombre real trae al menos una mayúscula; descarta basura del OCR.
    if not any(c.isupper() for c in limpio):
        return False

    normalizado = _comparable(limpio)
    if any(marca in normalizado for marca in _NO_ES_NOMBRE):
        return False
    # Domicilios, comparando por palabra completa para no castigar nombres que
    # contengan esas letras («av» dentro de «GUSTAVO»).
    if any(p in _PALABRAS_DOMICILIO for p in normalizado.split()):
        return False

    palabras = limpio.split()
    if len(palabras) < 2 or len(palabras) > 10:
        return False
    if sum(c.isalpha() for c in limpio) < 6:
        return False

    # Descarta oraciones (avisos legales). Un nombre va en MAYÚSCULAS o en Tipo
    # Título; una oración trae varias palabras en minúscula que no son
    # partículas.
    particulas = {p.lower() for p in _PARTICULAS}
    minusculas_sueltas = sum(
        1 for p in palabras
        if p.isalpha() and p.islower() and p.lower() not in particulas)
    if minusculas_sueltas >= 2:
        return False

    sin_punto = [p.strip(".").lower() for p in palabras]
    if any(m in normalizado for m in ("sa de cv", "s de rl", "sociedad anonima",
                                      "razon social")):
        return True
    if len(sin_punto) >= 2 and all(
            p.isalpha() or p in _MARCAS_RAZON_SOCIAL for p in sin_punto):
        return True
    return len(palabras) >= 2


def _limpiar_nombre(valor: str) -> str:
    """Recorta la basura que el OCR pega al final de un nombre en MAYÚSCULAS.

    En las carátulas el titular va en mayúsculas dentro del bloque de dirección,
    y el OCR suele arrastrarle texto de la columna vecina. Si el nombre arranca
    en mayúsculas se conserva solo la racha inicial de palabras en mayúsculas y
    se corta en el primer token con minúsculas, dígitos o símbolos. Si no parece
    estar en mayúsculas se devuelve intacto, para no dañar nombres legítimos en
    minúsculas o mixtos.
    """
    tokens = valor.split()
    if not tokens:
        return valor

    def nucleo(token: str) -> str:
        return token.strip(".,:;|/\\()[]{}\"'?!¡¿-")

    cabeza = [nucleo(t) for t in tokens[:2]]
    if sum(1 for t in cabeza if t.isalpha() and t.isupper()) < 2:
        return valor

    conservados: list[str] = []
    for token in tokens:
        limpio = nucleo(token)
        if limpio.isalpha() and limpio.isupper():
            conservados.append(limpio)
        else:
            break

    # Quita ruido corto del final (una o dos letras sueltas) que no sea una
    # partícula válida ni un sufijo de razón social.
    while conservados and len(conservados[-1]) <= 2 \
            and conservados[-1] not in _PARTICULAS:
        conservados.pop()

    return " ".join(conservados) if len(conservados) >= 2 else ""


def _aceptar_nombre(crudo: str) -> str:
    """Acepta un candidato solo si el renglón ORIGINAL ya parecía un nombre.

    El orden importa: validar primero el original evita «rescatar» renglones de
    datos —«DEPOSITOS ANULADOS: 0.0 MXN»— a los que la limpieza les quitaría
    justo los dígitos que los delataban.
    """
    candidato = _espacios(crudo)
    if not candidato or not _parece_nombre(candidato):
        return ""
    limpio = _espacios(_limpiar_nombre(candidato))
    return limpio if limpio and _parece_nombre(limpio) else ""


def _primera_vocal_interna(palabra: str) -> str:
    """La primera vocal DESPUÉS de la inicial, que es la 2ª letra del RFC."""
    for letra in palabra[1:]:
        if letra in "AEIOU":
            return letra
    return ""


def _rfc_cuadra_con_nombre(nombre: str, rfc: str) -> bool:
    """True si las 4 letras de un RFC de persona física salen de este nombre.

    El RFC de persona física se arma con: inicial y primera vocal interna del
    apellido paterno, inicial del materno e inicial del nombre de pila. No se
    reconstruye el RFC completo —eso exigiría acertar dónde parte un nombre
    compuesto («MARIA LUZ» / «RIVERA» / «OCAMPO»)— sino que se comprueba que cada
    letra tenga de dónde salir, sin fijar el orden. Es tolerante a nombres y
    apellidos compuestos, que es donde una reconstrucción exacta falla.
    """
    candidato = _rfc_comparable(rfc)
    # Solo persona física: 4 letras + 6 dígitos + homoclave.
    if not re.fullmatch(r"[A-ZÑ&]{4}\d{6}[A-Z0-9]{3}", candidato):
        return False
    palabras = [p for p in _comparable(nombre).upper().split()
                if p.isalpha() and p not in _PARTICULAS and len(p) >= 2]
    if len(palabras) < 2:
        return False

    l1, l2, l3, l4 = candidato[0], candidato[1], candidato[2], candidato[3]
    # Se descarta POR POSICIÓN, no por valor: dos palabras iguales son el mismo
    # objeto en Python y excluir por identidad se llevaría las dos.
    # El apellido paterno aporta DOS letras, así que es la comprobación fuerte.
    paterno = next((i for i, p in enumerate(palabras)
                    if p[0] == l1 and _primera_vocal_interna(p) == l2), None)
    if paterno is None:
        return False
    # Materno e inicial del nombre: cada una necesita su propia palabra, y
    # ninguna puede ser la que ya se gastó como paterno.
    materno = next((i for i, p in enumerate(palabras)
                    if i != paterno and p[0] == l3), None)
    if materno is None:
        return False
    return any(p[0] == l4 for i, p in enumerate(palabras)
               if i not in (paterno, materno))


def _titular_por_rfc(lineas: list[str], rfc: str) -> str:
    """Busca el titular usando el RFC ya leído como prueba de identidad.

    Existe porque el camino normal descarta el renglón correcto en cuanto el
    OCR le pega texto de la columna vecina: «MARIA LUZ RIVERA OCAMPO Abe 10203040»
    trae dígitos y `_parece_nombre` lo rechaza antes de que `_limpiar_nombre`
    pueda recortarlo. Invertir ese orden en el camino normal es justo lo que su
    comentario advierte que no se haga (rescataría renglones de datos), así que
    aquí se recorta primero y se exige el RFC como evidencia a cambio.

    Al recorrer en orden de documento, gana el encabezado de la página 1 —donde
    el OCR sale más limpio— sobre sus repeticiones en las páginas siguientes.
    """
    if not rfc:
        return ""
    for linea in lineas:
        # `_limpiar_nombre` recorta por el final, pero el OCR también pega
        # basura por delante («3 JOSE EDUARDO…», el número de página de la
        # columna vecina). Se podan aquí los tokens iniciales sin letras.
        tokens = _espacios(linea).split()
        while tokens and not any(c.isalpha() for c in tokens[0]):
            tokens.pop(0)
        candidato = _espacios(_limpiar_nombre(" ".join(tokens)))
        if candidato and _rfc_cuadra_con_nombre(candidato, rfc):
            return candidato
    return ""


def _extraer_titular(texto: str, banco: str = "", rfc: str = "") -> str:
    lineas = [_espacios(l) for l in texto.splitlines() if _espacios(l)]

    # Cuando hay RFC de persona física, manda: es el único dato de la carátula
    # que confirma el nombre por sí solo, sin depender de dónde lo maquetó el
    # banco. Si no cuadra con nada, se sigue con la búsqueda por posición.
    por_rfc = _titular_por_rfc(lineas, rfc)
    if por_rfc:
        return por_rfc

    # BBVA maqueta al titular en el renglón siguiente al número de cliente.
    if banco == "BBVA":
        for indice, linea in enumerate(lineas):
            if "no de cliente" in _comparable(linea):
                for siguiente in lineas[indice + 1:]:
                    nombre = _aceptar_nombre(siguiente)
                    if nombre:
                        return nombre
                break

    valor = _valor_tras_etiqueta(texto, ETIQUETAS_TITULAR)
    if valor:
        nombre = _aceptar_nombre(valor)
        if nombre:
            return nombre

    # Formularios con encabezado de columna («NOMBRE(S), APELLIDO PATERNO…»):
    # el valor va en un renglón siguiente. Estos formatos traen varios nombres
    # —vendedor, autorizador, titular— y el del titular va bajo «SOLICITANTE»,
    # así que se arranca desde ahí cuando esa palabra existe.
    inicio = 0
    for indice, linea in enumerate(lineas):
        if "solicitante" in _comparable(linea):
            inicio = indice
            break
    if inicio:
        for indice in range(inicio, len(lineas)):
            normalizada = _comparable(lineas[indice])
            if "nombre" in normalizada and "apellido" in normalizada:
                for siguiente in lineas[indice + 1:]:
                    nombre = _aceptar_nombre(siguiente)
                    if nombre:
                        return nombre
                break

    for indice, linea in enumerate(lineas):
        normalizada = _comparable(linea)
        if not any(_comparable(e) in normalizada for e in ETIQUETAS_TITULAR):
            continue
        # Una etiqueta de campo real es corta: así se descartan las etiquetas
        # que aparecen dentro de un aviso legal («…a nombre del Titular de la
        # cuenta.»).
        if len(linea.split()) > 6:
            continue
        partes = re.split(r"[:\-|]", linea, maxsplit=1)
        if len(partes) > 1:
            nombre = _aceptar_nombre(partes[1])
            if nombre:
                return nombre
        # El valor va en la misma línea o en la inmediata siguiente; no se
        # recorre el documento entero, que traía basura lejana del OCR.
        for siguiente in lineas[indice + 1:indice + 3]:
            nombre = _aceptar_nombre(siguiente)
            if nombre:
                return nombre

    for linea in lineas:
        nombre = _aceptar_nombre(linea)
        if nombre:
            return nombre
    return ""


# --------------------------------------------------------------------------- #
#  RFC
# --------------------------------------------------------------------------- #
def _parece_rfc(valor: str) -> bool:
    candidato = _rfc_comparable(valor)
    m = re.fullmatch(r"[A-Z&]{3,4}(\d{2})(\d{2})(\d{2})[A-Z0-9]{3}", candidato)
    if not m:
        return False
    mes, dia = int(m.group(2)), int(m.group(3))
    return 1 <= mes <= 12 and 1 <= dia <= 31


def rfc_generico(valor: str) -> bool:
    """True si es uno de los RFC comodín del SAT (no identifican a nadie)."""
    return _rfc_comparable(valor) in RFC_GENERICOS


def _reparar_rfc(valor: str) -> str:
    """Recupera un RFC garabateado por el OCR corrigiendo por posición.

    Estructura del RFC: 3-4 letras + 6 dígitos (AAMMDD) + 3 alfanuméricos. Solo
    se usa cuando hay una etiqueta de RFC cerca, para no fabricar falsos
    positivos a partir de cualquier código largo.
    """
    # El OCR suele leer la 'S' inicial como '$'; se recupera antes de limpiar.
    valor = (valor or "").replace("$", "S")
    compacto = re.sub(r"[^A-Z0-9&]", "", _rfc_comparable(valor))
    if len(compacto) < 12:
        return ""
    for largo in (13, 12):
        if len(compacto) < largo:
            continue
        n_letras = 4 if largo == 13 else 3
        for inicio in range(0, len(compacto) - largo + 1):
            ventana = compacto[inicio:inicio + largo]
            fecha_cruda = ventana[n_letras:n_letras + 6]
            # La reparación corrige uno o dos errores de OCR, NO convierte una
            # palabra entera en dígitos: se exige que al menos 4 de los 6 ya lo
            # sean.
            if sum(c.isdigit() for c in fecha_cruda) < 4:
                continue
            candidato = (ventana[:n_letras].translate(_A_LETRA)
                         + fecha_cruda.translate(_A_DIGITO)
                         + ventana[n_letras + 6:])
            if _parece_rfc(candidato):
                return candidato
    return ""


def _extraer_rfc(texto: str) -> str:
    lineas = [_espacios(l) for l in texto.splitlines() if _espacios(l)]
    etiquetas = [_comparable(e).replace(" ", "") for e in ETIQUETAS_RFC]

    def buscar_en(valor: str) -> str:
        compacto = re.sub(r"[^A-Z0-9&]", "", _rfc_comparable(valor))
        if len(compacto) < 12:
            return ""
        for largo in (13, 12):
            if len(compacto) < largo:
                continue
            for inicio in range(0, len(compacto) - largo + 1):
                candidato = compacto[inicio:inicio + largo]
                if _parece_rfc(candidato):
                    return candidato
        return ""

    def tiene_etiqueta(valor: str) -> bool:
        compacto = _comparable(valor).replace(" ", "")
        return any(e in compacto for e in etiquetas)

    def es_del_banco(candidato: str, indice: int) -> bool:
        norm = _rfc_comparable(candidato)
        # Por subcadena: el OCR a veces pega una letra de la etiqueta «R.F.C.»
        # al RFC del banco ('CBSM970519DU8' por 'BSM970519DU8').
        if any(rfc in norm for rfc in RFC_BANCOS):
            return True
        contexto = _comparable(" ".join(lineas[indice:indice + 2]))
        return any(m in contexto for m in _CONTEXTO_RFC_BANCO)

    def considerar(candidato: str, indice: int) -> str:
        if not candidato or es_del_banco(candidato, indice):
            return ""
        return candidato

    for indice, linea in enumerate(lineas):
        if not tiene_etiqueta(linea):
            continue
        partes = re.split(r"[:\-|]", linea, maxsplit=1)
        candidato = ""
        if len(partes) > 1:
            candidato = _reparar_rfc(partes[1]) or buscar_en(partes[1])
        if not candidato:
            candidato = _reparar_rfc(linea) or buscar_en(linea)
        if not candidato:
            for siguiente in lineas[indice + 1:]:
                normalizada = _comparable(siguiente)
                if any(corte in normalizada for corte in
                       ("banco", "cuenta", "clabe", "nombre", "titular",
                        "beneficiario")):
                    break
                candidato = _reparar_rfc(siguiente) or buscar_en(siguiente)
                if candidato:
                    break
        elegido = considerar(candidato, indice)
        if elegido:
            return elegido

    for indice, linea in enumerate(lineas):
        candidato = buscar_en(linea)
        if not candidato:
            continue
        vecino_etiquetado = (indice + 1 < len(lineas)
                             and tiene_etiqueta(lineas[indice + 1]))
        if tiene_etiqueta(linea) or vecino_etiquetado:
            elegido = considerar(candidato, indice)
            if elegido:
                return elegido

    # Último recurso: un token aislado con forma de RFC aunque no haya etiqueta
    # cerca. Se acota el largo para no confundirlo con una referencia larga.
    for indice, linea in enumerate(lineas):
        for token in linea.split():
            compacto = re.sub(r"[^A-Z0-9&]", "", _rfc_comparable(token))
            if not (12 <= len(compacto) <= 16):
                continue
            elegido = considerar(buscar_en(token), indice)
            if elegido:
                return elegido

    # Nunca se devuelve el RFC del banco: preferimos dejarlo vacío y que se
    # capture a mano, a dar de alta al beneficiario con el RFC equivocado.
    return ""


# --------------------------------------------------------------------------- #
#  Banco y cuenta
# --------------------------------------------------------------------------- #
def banco_de_clabe(clabe: str) -> str:
    """Nombre del banco según los tres primeros dígitos de la CLABE."""
    return BANCOS_POR_CLABE.get((clabe or "")[:3], "")


def _extraer_banco(texto: str, clabe: str) -> str:
    # 1) El código de la CLABE identifica al banco EMISOR: lo más confiable,
    #    porque no depende de que el logo del encabezado se lea bien.
    banco = banco_de_clabe(clabe)
    if banco:
        return banco

    # 2) Alias, pero SOLO en el encabezado: así no se toma un banco mencionado
    #    en los movimientos («BANCO ORIGEN: BANORTE»).
    encabezado = _comparable(
        "\n".join([l for l in texto.splitlines() if l.strip()][:8]))
    for alias, nombre in ALIAS_BANCOS:
        if _comparable(alias) in encabezado:
            return nombre

    # 3) Etiqueta «banco» explícita, normalizando al alias conocido si aparece.
    etiquetado = _valor_tras_etiqueta(texto, ["banco"])
    if etiquetado:
        normalizado = _comparable(etiquetado)
        for alias, nombre in ALIAS_BANCOS:
            if _comparable(alias) in normalizado:
                return nombre
        limpio = _espacios(etiquetado)
        if limpio and len(limpio.split()) <= 4:
            return limpio

    # 4) CLABE con un código que no está en el catálogo: se dice cuál es, para
    #    que se pueda completar a mano sin volver a abrir el archivo.
    if len(clabe or "") >= 3:
        return f"Código CLABE {clabe[:3]}"
    return ""


def _parece_fecha(digitos: str) -> bool:
    """Detecta secuencias que en realidad son fechas concatenadas (DDMMAAAA).

    Evita que el periodo de la carátula («DEL 21/04/2026 AL 20/05/2026») se
    confunda con un número de cuenta.
    """
    def ddmmaaaa(valor: str) -> bool:
        if len(valor) != 8 or not valor.isdigit():
            return False
        dia, mes, anio = int(valor[:2]), int(valor[2:4]), int(valor[4:])
        return 1 <= dia <= 31 and 1 <= mes <= 12 and 1990 <= anio <= 2100

    if ddmmaaaa(digitos):
        return True
    return (len(digitos) == 16 and ddmmaaaa(digitos[:8])
            and ddmmaaaa(digitos[8:]))


def _extraer_cuenta(texto: str, clabe: str, rfc: str = "") -> str:
    digitos_rfc = _solo_digitos(rfc)

    def valida(digitos: str) -> bool:
        if not (8 <= len(digitos) <= 20):
            return False
        # La CLABE contiene al número de cuenta dentro de sí misma, así que solo
        # se descarta la CLABE completa, no que la cuenta sea subcadena suya.
        if digitos == clabe or len(digitos) == 18:
            return False
        return not (digitos_rfc and digitos == digitos_rfc)

    etiquetado = _valor_tras_etiqueta(texto, ETIQUETAS_CUENTA)
    if etiquetado:
        digitos = _solo_digitos(etiquetado)
        if valida(digitos):
            return digitos

    for cruda in _RE_CUENTA.findall(texto):
        digitos = _solo_digitos(cruda)
        if valida(digitos) and not _parece_fecha(digitos):
            return digitos
    return ""


# --------------------------------------------------------------------------- #
#  OCR reforzado para carátulas
# --------------------------------------------------------------------------- #
# Una carátula no es un documento de oficina: la mitad llegan como foto de
# celular o captura de pantalla de la app del banco, con sombras, iluminación
# despareja y el dato que importa en letra chica. El OCR de `core/ocr.py`, que
# está afinado para estados de cuenta en PDF, se salta filas enteras en esas
# condiciones —justo la de la CLABE—.
#
# Por eso aquí se rasca más: se prueban dos preprocesados por dos caminos
# distintos (enfocar vs. binarizar) contra tres modos de segmentación de página,
# y se conserva la pasada que devolvió MÁS texto. «Más texto» es un criterio
# tosco, pero mide bien lo único que importa: la pasada que no omitió filas.
#
# Cuesta seis llamadas a Tesseract por página, del orden de segundos. Por eso
# `leer` solo llega hasta aquí cuando la capa de texto del PDF no bastó.
_LADO_OBJETIVO = 2400          # px del lado mayor: por debajo, la letra chica se pierde
_PSM_CARATULA = (6, 4, 11)     # bloque uniforme · columnas · texto disperso
_DPI_PAGINA = 300

_EXTENSIONES_IMAGEN = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def _umbral_otsu(gris: "Image.Image") -> int:
    """Umbral óptimo de binarización (método de Otsu) desde el histograma.

    Se calcula por imagen en vez de usar un 128 fijo porque la foto de una
    carátula sobre un escritorio y una captura de pantalla del celular tienen
    fondos completamente distintos, y un umbral fijo borra el texto de una o
    deja el ruido de la otra.
    """
    histograma = gris.histogram()[:256]
    total = sum(histograma)
    if total == 0:
        return 128
    suma_total = sum(i * h for i, h in enumerate(histograma))
    suma_fondo = 0.0
    peso_fondo = 0
    mejor_varianza = -1.0
    umbral = 128
    for nivel in range(256):
        peso_fondo += histograma[nivel]
        if peso_fondo == 0:
            continue
        peso_frente = total - peso_fondo
        if peso_frente == 0:
            break
        suma_fondo += nivel * histograma[nivel]
        media_fondo = suma_fondo / peso_fondo
        media_frente = (suma_total - suma_fondo) / peso_frente
        varianza = peso_fondo * peso_frente * (media_fondo - media_frente) ** 2
        if varianza > mejor_varianza:
            mejor_varianza = varianza
            umbral = nivel
    return umbral


def _variantes(imagen: "Image.Image") -> list["Image.Image"]:
    """Las dos versiones de la imagen que se le dan a Tesseract.

    El preprocesado común corrige lo que arruina el OCR de una foto: la
    orientación EXIF (una carátula fotografiada de lado no se lee), el color
    (estorba), el contraste y el tamaño. Después se bifurca: **enfocar** define
    los bordes de la letra chica y funciona mejor en capturas de pantalla;
    **binarizar** elimina sombras e iluminación despareja y funciona mejor en
    fotos. Ninguna gana siempre, así que se prueban las dos.
    """
    from PIL import ImageFilter, ImageOps

    base = ImageOps.exif_transpose(imagen)
    base = ImageOps.grayscale(base)
    base = ImageOps.autocontrast(base)
    lado_mayor = max(base.size)
    if lado_mayor < _LADO_OBJETIVO:
        factor = min(4, max(2, round(_LADO_OBJETIVO / lado_mayor)))
        base = base.resize((base.width * factor, base.height * factor))

    variantes = [base.filter(
        ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))]
    try:
        umbral = _umbral_otsu(base)
        variantes.append(base.point(lambda px: 255 if px > umbral else 0))
    except Exception:  # noqa: BLE001 — sin binarizada se sigue con la enfocada
        pass
    return variantes


def _texto_de_imagen(imagen: "Image.Image", cancelado=None) -> str:
    """Barre variantes × modos de segmentación y devuelve la pasada más larga."""
    mejor = ""
    for variante in _variantes(imagen):
        for psm in _PSM_CARATULA:
            try:
                texto = ocr.ocr_imagen(variante, psm=psm, cancelado=cancelado)
            except ocr.OCRCancelado:
                raise
            except Exception:  # noqa: BLE001 — una pasada mala no anula el resto
                continue
            if len(texto) > len(mejor):
                mejor = texto
    return mejor


def _texto_reforzado(ruta: str, cancelado=None) -> str:
    """OCR reforzado de un archivo completo (imagen suelta o PDF rasterizado)."""
    from PIL import Image

    if os.path.splitext(ruta)[1].lower() != ".pdf":
        with Image.open(ruta) as imagen:
            return _texto_de_imagen(imagen, cancelado=cancelado)

    import fitz

    paginas: list[str] = []
    with fitz.open(ruta) as documento:
        for pagina in documento:
            if cancelado is not None and cancelado():
                raise ocr.OCRCancelado(ruta)
            pix = pagina.get_pixmap(dpi=_DPI_PAGINA, alpha=False)
            imagen = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            texto = _texto_de_imagen(imagen, cancelado=cancelado)
            if texto.strip():
                paginas.append(texto)
    return "\n".join(paginas)


# --------------------------------------------------------------------------- #
#  API pública
# --------------------------------------------------------------------------- #
def _recalcular_avisos(datos: DatosCaratula) -> None:
    """Deja `avisos` acorde a lo que hay ahora. Se recalcula en vez de acumular
    porque `leer` puede mejorar la lectura en un segundo intento, y el aviso de
    la primera pasada quedaría mintiendo."""
    avisos = []
    if not datos.clabe:
        avisos.append("No se encontró la CLABE en la carátula.")
    elif not clabe_valida(datos.clabe):
        avisos.append(
            "La CLABE leída no pasa su dígito verificador: revísala contra la "
            "carátula antes de dar de alta.")
    if not datos.titular:
        avisos.append("No se reconoció el titular de la cuenta.")
    datos.avisos = avisos


def interpretar(texto: str, ruta: str = "", uso_ocr: bool = False) -> DatosCaratula:
    """Extrae los datos de un texto ya leído. Separado de `leer` para poder
    probar la interpretación sin archivos ni Tesseract."""
    datos = DatosCaratula(ruta=ruta, uso_ocr=uso_ocr)
    datos.clabe = _extraer_clabe(texto)
    datos.banco = _extraer_banco(texto, datos.clabe)
    # El RFC va ANTES que el titular: cuando es de persona física sirve para
    # confirmar cuál renglón trae el nombre (ver `_titular_por_rfc`).
    datos.rfc = _extraer_rfc(texto)
    if rfc_generico(datos.rfc):
        # Un RFC comodín no identifica a nadie: vale lo mismo que no tenerlo, y
        # dejarlo pasar lo daría de alta como si fuera el del beneficiario.
        datos.rfc = ""
    datos.titular = _extraer_titular(texto, datos.banco, datos.rfc)
    datos.cuenta = _extraer_cuenta(texto, datos.clabe, datos.rfc)
    _recalcular_avisos(datos)
    return datos


def _completo(datos: DatosCaratula) -> bool:
    """True si ya no hay nada que ganar con otra pasada de OCR."""
    return bool(datos.clabe_confiable and datos.titular and datos.rfc)


def leer(ruta: str, cancelado=None) -> DatosCaratula:
    """Lee una carátula y devuelve lo que se pudo extraer.

    Escala en dos etapas, y esa es la decisión de diseño del módulo:

    1. **La capa de texto del PDF**, que es instantánea y exacta cuando existe.
       Las carátulas que el banco emite en PDF se resuelven aquí.
    2. **El OCR reforzado** (`_texto_de_imagen`), solo si a la primera etapa le
       faltó algo. Cuesta varios segundos por archivo, así que no se paga
       cuando no hace falta —pero cuando hace falta, hace toda la diferencia:
       la mitad de las carátulas llegan como foto de celular o captura de
       pantalla de la app del banco, y ahí la capa de texto no existe.

    El texto de ambas etapas se **concatena** antes de interpretar, en vez de
    quedarse con el de la segunda: cada una lee bien partes distintas de la
    hoja, y juntas encuentran campos que ninguna encuentra sola.

    Nunca lanza por un archivo ilegible: el error se devuelve en `.error`, para
    que una carpeta entera no se caiga por un PDF corrupto. Lo que sí propaga es
    la cancelación, que es una decisión del usuario.
    """
    ext = os.path.splitext(ruta)[1].lower()
    texto_capa = ""

    if ext == ".pdf":
        try:
            texto_capa, _ = ocr.extraer_texto(ruta, cancelado=cancelado)
        except ocr.OCRCancelado:
            raise
        except Exception as exc:  # noqa: BLE001 — un archivo malo no tumba el lote
            return DatosCaratula(ruta=ruta, error=f"No se pudo leer: {exc}")
        datos = interpretar(texto_capa, ruta=ruta)
        if _completo(datos):
            return datos
    elif ext not in _EXTENSIONES_IMAGEN:
        return DatosCaratula(
            ruta=ruta, error=f"Extensión no soportada: {ext or 'sin extensión'}")

    if not ocr.tesseract_disponible():
        datos = interpretar(texto_capa, ruta=ruta)
        if not texto_capa:
            datos.error = (
                "Se necesita OCR para leer esta carátula y no se encontró "
                "Tesseract. Instálalo para leer fotos y PDF escaneados.")
        return datos

    try:
        texto_ocr = _texto_reforzado(ruta, cancelado=cancelado)
    except ocr.OCRCancelado:
        raise
    except Exception as exc:  # noqa: BLE001
        datos = interpretar(texto_capa, ruta=ruta)
        if not texto_capa:
            datos.error = f"No se pudo leer: {exc}"
        return datos

    return interpretar("\n".join(p for p in (texto_capa, texto_ocr) if p),
                       ruta=ruta, uso_ocr=True)
