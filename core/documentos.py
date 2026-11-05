"""Documentos adjuntos de una solicitud: carátula bancaria y Vo.Bo.

SIPP pide dos archivos que el robot no puede inventar:

- **Carátula bancaria** — al dar de alta una cuenta nueva, y además en el campo
  `PDF` de la solicitud. Sin ella, el alta del beneficiario queda incompleta y
  la cuenta no sirve para pagar.
- **Vo.Bo. de Compras** — documento de respaldo obligatorio en Pago
  Extraordinario. Solo se puede adjuntar DESPUÉS de guardar.

Se pueden asignar de dos maneras, y las dos acaban en la misma tabla
`documento`: una por una desde el formulario de captura, o **por carpeta**,
emparejando por el nombre de la persona. Lo segundo es lo que se usa en un lote
grande: el área recibe una carpeta con un PDF por colaborador y nadie va a
adjuntar noventa archivos a mano.

El emparejamiento por nombre tolera acentos, Ñ, dobles espacios y el orden de
los apellidos, porque así llegan los archivos en la práctica.
"""

from __future__ import annotations

import os
import re
import unicodedata

from core import db, rutas

TIPO_CARATULA = "CARATULA"
TIPO_VOBO = "VOBO"

# Lo que SIPP acepta. El PDF es lo normal; las imágenes aparecen cuando el
# Vo.Bo. es una captura de pantalla del correo de autorización.
EXTENSIONES = (".pdf", ".jpg", ".jpeg", ".png")


def _normalizar(texto: str) -> str:
    """Nombre comparable: sin acentos, sin signos, en minúsculas."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _palabras(texto: str) -> set[str]:
    """Palabras significativas de un nombre.

    Se descartan las de una o dos letras (iniciales y partículas como «de» o
    «la»), que aparecen y desaparecen entre el archivo y el sistema y solo
    generan coincidencias falsas.
    """
    return {p for p in _normalizar(texto).split() if len(p) > 2}


def coincide(nombre_archivo: str, nombre_persona: str) -> bool:
    """True si el archivo parece ser de esa persona.

    Se pide que **todas** las palabras significativas del nombre estén en el
    archivo, no al revés: el archivo suele traer texto de más («CARATULA JUAN
    PEREZ BBVA.pdf»), pero si le falta un apellido ya no es la misma persona.
    """
    objetivo = _palabras(nombre_persona)
    if not objetivo:
        return False
    base = _palabras(os.path.splitext(os.path.basename(nombre_archivo))[0])
    return objetivo.issubset(base)


def nombres_coinciden(uno: str, otro: str) -> bool:
    """True si dos nombres se refieren a la misma persona.

    A diferencia de `coincide`, aquí ninguno de los dos es «el archivo»: se
    comparan dos nombres de persona —el leído de una carátula y el de una fila
    de Excel—, que pueden venir en distinto orden o con un apellido de más.
    Basta con que uno sea subconjunto del otro.

    Se exigen al menos **dos** palabras significativas. Con una sola, «GARCIA»
    coincidiría con cualquier García del lote, y emparejar mal una carátula
    manda el dinero a la cuenta equivocada.
    """
    a, b = _palabras(uno), _palabras(otro)
    if len(a) < 2 or len(b) < 2:
        return False
    return a.issubset(b) or b.issubset(a)


def buscar_en_carpeta(carpeta: str, nombre_persona: str,
                      recursivo: bool = True) -> str:
    """Ruta del archivo de esa persona dentro de la carpeta ('' si no hay).

    Si hay varios candidatos devuelve el de nombre más corto, que es el que
    menos texto ajeno trae y casi siempre el correcto.
    """
    if not carpeta or not os.path.isdir(carpeta):
        return ""
    candidatos: list[str] = []
    for raiz, _dirs, archivos in os.walk(carpeta):
        for archivo in archivos:
            if not archivo.lower().endswith(EXTENSIONES):
                continue
            if coincide(archivo, nombre_persona):
                candidatos.append(os.path.join(raiz, archivo))
        if not recursivo:
            break
    if not candidatos:
        return ""
    return min(candidatos, key=lambda r: len(os.path.basename(r)))


# Carátulas convertidas desde una imagen. Van a la carpeta de datos y no junto
# al original: el usuario suele adjuntarlas desde una carpeta compartida o desde
# Descargas, donde no se debe escribir y de donde el archivo puede desaparecer.
CARPETA_CONVERTIDAS = os.path.join(rutas.DATOS, "caratulas")

_IMAGENES = (".jpg", ".jpeg", ".png")

# Lado mayor, en píxeles, con el que se guarda una imagen convertida. Una foto
# de celular trae 4000 px o más y pesa varios MB; a 2200 px una carátula o la
# captura de un correo se siguen leyendo sin forzar la vista, y el PDF queda en
# unos cientos de KB en vez de rozar el tope de 10 MB de SIPP.
_LADO_MAXIMO = 2200
_CALIDAD_JPEG = 82


def es_imagen(ruta: str) -> bool:
    return bool(ruta) and ruta.lower().endswith(_IMAGENES)


def asegurar_pdf(ruta: str) -> str:
    """Devuelve el documento en PDF, convirtiéndolo si llegó como imagen.

    Aplica a la carátula y al Vo.Bo. La carátula tiene que ser un PDF: es lo
    que el área entrega al banco y lo que SIPP guarda como respaldo de la
    cuenta. El Vo.Bo. SIPP lo aceptaría como imagen, pero suele ser una foto de
    celular de varios MB, y en PDF queda uniforme con lo demás y se puede
    reducir.

    Las imágenes grandes se **reducen** al convertir (ver `_LADO_MAXIMO`): una
    foto de 12 MB subida tal cual la rechaza SIPP, y el robot solo sabía
    comprimir PDFs.

    Si algo falla al convertir se devuelve el original: quedarse sin carátula
    es peor que subir un JPG, porque sin ella SIPP no deja dar de alta la cuenta
    y la solicitud entera se detiene.
    """
    if not ruta or not os.path.isfile(ruta):
        return ruta
    if not es_imagen(ruta):
        return ruta
    try:
        import pymupdf

        os.makedirs(CARPETA_CONVERTIDAS, exist_ok=True)
        base = os.path.splitext(os.path.basename(ruta))[0]
        # El hash va en el nombre para que dos imágenes distintas con el mismo
        # nombre —«caratula.jpg» de dos personas— no se pisen la una a la otra.
        destino = os.path.join(
            CARPETA_CONVERTIDAS, f"{base}_{db.hash_archivo(ruta)[:8]}.pdf")
        if os.path.isfile(destino):
            return destino
        pix = pymupdf.Pixmap(ruta)
        if pix.alpha or pix.n > 3:      # PNG con transparencia, o CMYK
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix, 0)
        grande = max(pix.width, pix.height) > _LADO_MAXIMO
        if grande:
            escala = _LADO_MAXIMO / max(pix.width, pix.height)
            pix = pymupdf.Pixmap(pix, int(pix.width * escala),
                                 int(pix.height * escala), None)
        # Las pequeñas conservan su formato (un PNG de captura de pantalla se
        # lee mejor sin JPEG); las grandes se recomprimen, que es lo que pesa.
        datos = (pix.tobytes("jpeg", jpg_quality=_CALIDAD_JPEG) if grande
                 else pix.tobytes("png"))
        # La página mide lo que la imagen a 150 ppp: un tamaño de hoja
        # razonable al abrirla o imprimirla.
        ancho, alto = pix.width * 72 / 150, pix.height * 72 / 150
        pdf = pymupdf.open()
        try:
            pagina = pdf.new_page(width=ancho, height=alto)
            pagina.insert_image(pagina.rect, stream=datos)
            pdf.save(destino, garbage=4, deflate=True)
        finally:
            pdf.close()
        return destino
    except Exception:  # noqa: BLE001 — ver el docstring: mejor el original
        return ruta


def registrar(solicitud_id: str, ruta: str, tipo: str,
              lote_id: str = "") -> db.Documento | None:
    """Asocia un archivo a una solicitud, reemplazando el anterior de ese tipo.

    Se reemplaza en vez de acumular porque una solicitud tiene UNA carátula y UN
    Vo.Bo.; guardar los dos intentos dejaría al motor eligiendo a ciegas.

    Un documento que llegue como imagen se convierte a PDF aquí, que es el único
    sitio por el que pasan todos: el formulario de captura, el alta desde
    carátulas y el emparejamiento por carpeta.
    """
    if not ruta or not os.path.isfile(ruta):
        return None
    ruta = asegurar_pdf(ruta)
    for previo in db.listar_documentos(solicitud_id=solicitud_id):
        if previo.tipo == tipo:
            db.borrar_documento(previo.id)
    doc = db.Documento(solicitud_id=solicitud_id, lote_id=lote_id, tipo=tipo,
                       ruta=ruta, hash_sha256=db.hash_archivo(ruta))
    return db.guardar_documento(doc)


def de_solicitud(solicitud_id: str) -> dict[str, str]:
    """Los archivos de una solicitud en la forma que espera el motor RPA:
    `{"caratula": ruta, "vobo": ruta}`, omitiendo los que ya no existen."""
    salida: dict[str, str] = {}
    for doc in db.listar_documentos(solicitud_id=solicitud_id):
        if not os.path.isfile(doc.ruta):
            continue          # el archivo se movió o se borró del disco
        if doc.tipo == TIPO_CARATULA:
            salida["caratula"] = doc.ruta
        elif doc.tipo == TIPO_VOBO:
            salida["vobo"] = doc.ruta
    return salida


def asignar_por_carpeta(lote_id: str, carpeta: str, tipo: str) -> dict:
    """Recorre la carpeta y asigna sus archivos a las solicitudes del lote.

    Devuelve `{"asignados": n, "sin_archivo": [nombres]}` para poder decirle al
    usuario exactamente a quién le falta, que es lo único accionable.
    """
    asignados, sin_archivo = 0, []
    for solicitud in db.listar_solicitudes(lote_id):
        ruta = buscar_en_carpeta(carpeta, solicitud.beneficiario_nombre)
        if ruta:
            registrar(solicitud.id, ruta, tipo, lote_id)
            asignados += 1
        else:
            sin_archivo.append(solicitud.beneficiario_nombre)
    return {"asignados": asignados, "sin_archivo": sin_archivo}


def falta_caratula(solicitud) -> bool:
    """True si esta solicitud **podría** necesitar carátula y no la tiene.

    La carátula solo se usa al dar de alta una cuenta bancaria: si el
    beneficiario ya existe en SIPP, la suya ya está registrada. Pero **quién ya
    existe lo decide SIPP al capturar**, no la solicitud, así que aquí no se
    puede saber con certeza: se avisa siempre que haya transferencia y no haya
    archivo, y quien decide es la persona que revisa el lote.

    Es un aviso, no un impedimento. El motor sí la exige cuando comprueba que el
    beneficiario no está dado de alta, y ahí sí detiene esa solicitud.
    """
    from core import catalogos

    if solicitud.forma_pago != catalogos.FORMA_PAGO_CON_CUENTA:
        return False
    return "caratula" not in de_solicitud(solicitud.id)
