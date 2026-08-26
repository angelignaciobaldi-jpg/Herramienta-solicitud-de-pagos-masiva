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


def asegurar_pdf(ruta: str) -> str:
    """Devuelve la carátula en PDF, convirtiéndola si llegó como imagen.

    La carátula tiene que ser un PDF: es lo que el área entrega al banco y lo
    que SIPP guarda como respaldo de la cuenta. Pero llega a menudo como foto o
    captura de pantalla, y hasta ahora se subía tal cual.

    Si algo falla al convertir se devuelve el original: quedarse sin carátula
    es peor que subir un JPG, porque sin ella SIPP no deja dar de alta la cuenta
    y la solicitud entera se detiene.
    """
    if not ruta or not os.path.isfile(ruta):
        return ruta
    if not ruta.lower().endswith(_IMAGENES):
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
        with pymupdf.open(ruta) as imagen:
            pdf = pymupdf.open("pdf", imagen.convert_to_pdf())
            try:
                pdf.save(destino)
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

    Una carátula que llegue como imagen se convierte a PDF aquí, que es el único
    sitio por el que pasan todas: el formulario de captura, el alta desde
    carátulas y el emparejamiento por carpeta.
    """
    if not ruta or not os.path.isfile(ruta):
        return None
    if tipo == TIPO_CARATULA:
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
