"""Descarga de documentos enlazados desde una hoja de cálculo.

Existe porque parte de la operación no manda las carátulas como archivos: manda
un Excel donde cada fila trae un **hipervínculo** al documento. Este módulo baja
esos archivos a una carpeta temporal para que el resto del flujo —lectura de la
carátula, emparejamiento, alta— siga trabajando con rutas locales y no tenga que
enterarse de que vinieron de la red.

Se usa `urllib` de la biblioteca estándar en vez de un cliente HTTP externo: es
una descarga GET simple y así no se agrega una dependencia que además habría que
declararle a PyInstaller al empaquetar.

**Los límites de aquí no son decorativos.** Las URL salen de un archivo que la
herramienta no controla, así que se acota a qué se puede conectar (solo http/s),
cuánto puede tardar y cuánto puede pesar lo que baje. Sin eso, un enlace
equivocado deja la aplicación colgada o llena el disco.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# Parámetros con los que un enlace firmado declara hasta cuándo sirve. Los usan
# S3, CloudFront y la mayoría de los CDN.
_PARAMS_CADUCIDAD = ("expires", "Expires", "X-Amz-Expires", "se", "exp")

# Lo que tiene sentido que sea una carátula. Un enlace a otra cosa se rechaza
# aquí y no después, cuando el OCR ya no sabría qué hacer con el archivo.
EXTENSIONES = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")

TIMEOUT = 30                     # segundos por archivo
MAX_BYTES = 25 * 1024 * 1024     # 25 MB: una carátula escaneada no pasa de ahí
_TROZO = 64 * 1024

# Encabezado de navegador: algunos servidores devuelven 403 a un cliente que no
# se identifica, y el archivo está bien.
_AGENTE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class DescargaFallida(RuntimeError):
    """No se pudo bajar el documento. El mensaje es para mostrárselo al usuario."""


def _nombre_desde_cabecera(cabeceras) -> str:
    """Nombre propuesto por el servidor en Content-Disposition, si lo hay."""
    crudo = cabeceras.get("Content-Disposition", "") if cabeceras else ""
    if not crudo:
        return ""
    # filename*=UTF-8''algo.pdf  tiene prioridad sobre  filename="algo.pdf"
    extendido = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", crudo, re.I)
    if extendido:
        return urllib.parse.unquote(extendido.group(1).strip().strip('"'))
    simple = re.search(r'filename\s*=\s*"?([^";]+)"?', crudo, re.I)
    return simple.group(1).strip() if simple else ""


def _sanear(nombre: str) -> str:
    """Deja un nombre de archivo seguro: sin rutas ni caracteres prohibidos.

    El nombre lo propone un servidor remoto, así que se le quita cualquier cosa
    que lo saque de la carpeta de destino («../», rutas absolutas, separadores).
    """
    nombre = urllib.parse.unquote(nombre or "")
    nombre = nombre.replace("\\", "/").split("/")[-1]
    nombre = re.sub(r'[<>:"|?*\x00-\x1f]', "_", nombre).strip(" .")
    return nombre[:120]


def caducidad(url: str) -> "dt.datetime | None":
    """Hasta cuándo sirve un enlace firmado, o None si no lo declara.

    Los documentos no se sirven desde una dirección permanente: el sistema de
    origen genera un enlace **temporal** y lo mete en el Excel. Si la descarga
    se hace días después de exportarlo, ya no vale.
    """
    consulta = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    for clave in _PARAMS_CADUCIDAD:
        crudo = (consulta.get(clave) or [""])[0]
        if not crudo.isdigit():
            continue
        marca = int(crudo)
        # Algunos lo dan en milisegundos y otros en segundos desde 1970.
        if marca > 10 ** 12:
            marca //= 1000
        try:
            return dt.datetime.fromtimestamp(marca)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def nombre_de_url(url: str) -> str:
    """El nombre de archivo que sugiere la propia URL (sin parámetros)."""
    return _sanear(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])


def _destino_libre(carpeta: str, nombre: str) -> str:
    """Ruta que no pisa un archivo ya bajado (dos filas pueden traer el mismo
    nombre y son documentos distintos)."""
    base, ext = os.path.splitext(nombre)
    ruta = os.path.join(carpeta, nombre)
    contador = 2
    while os.path.exists(ruta):
        ruta = os.path.join(carpeta, f"{base} ({contador}){ext}")
        contador += 1
    return ruta


def descargar(url: str, carpeta: str, *, nombre_sugerido: str = "",
              timeout: int = TIMEOUT, max_bytes: int = MAX_BYTES) -> str:
    """Baja `url` a `carpeta` y devuelve la ruta local del archivo.

    `nombre_sugerido` se usa cuando ni la URL ni el servidor dan un nombre
    reconocible; sirve para que el archivo conserve algo identificable, porque
    de él sale el nombre de respaldo del beneficiario si el OCR no lee la
    carátula (ver `caratulas.nombre_desde_archivo`).

    Raises:
        DescargaFallida: URL no admitida, error de red, archivo demasiado
            grande o extensión que no corresponde a una carátula.
    """
    partes = urllib.parse.urlparse((url or "").strip())
    if partes.scheme.lower() not in ("http", "https"):
        raise DescargaFallida(
            f"El enlace no es una dirección web descargable: {url[:80]}")

    # Se comprueba ANTES de pedir nada: el servidor contestaría un 403 seco, y
    # «prohibido» manda a buscar un problema de permisos que no existe. Lo que
    # hay que hacer es volver a exportar el archivo, y eso es lo que se dice.
    vence = caducidad(url)
    if vence and vence < dt.datetime.now():
        raise DescargaFallida(
            f"El enlace caducó el {vence:%d/%m/%Y a las %H:%M}. Vuelve a "
            f"exportar el archivo desde el sistema de origen para obtener "
            f"enlaces vigentes.")

    os.makedirs(carpeta, exist_ok=True)
    peticion = urllib.request.Request(url, headers={"User-Agent": _AGENTE})
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            nombre = (_sanear(_nombre_desde_cabecera(respuesta.headers))
                      or nombre_de_url(url)
                      or _sanear(nombre_sugerido) or "caratula")
            if not nombre.lower().endswith(EXTENSIONES):
                # El servidor puede no dar extensión; se deduce del tipo MIME
                # antes de rendirse, que es el caso de muchos gestores docs.
                tipo = (respuesta.headers.get("Content-Type", "") or "").lower()
                if "pdf" in tipo:
                    nombre += ".pdf"
                elif "png" in tipo:
                    nombre += ".png"
                elif "jpeg" in tipo or "jpg" in tipo:
                    nombre += ".jpg"
                else:
                    raise DescargaFallida(
                        f"Lo que hay en el enlace no es un documento de "
                        f"carátula ({tipo or 'tipo desconocido'}).")

            declarado = respuesta.headers.get("Content-Length")
            if declarado and declarado.isdigit() and int(declarado) > max_bytes:
                raise DescargaFallida(
                    f"El archivo pesa más de {max_bytes // (1024 * 1024)} MB.")

            ruta = _destino_libre(carpeta, nombre)
            bajados = 0
            with open(ruta, "wb") as fh:
                while True:
                    trozo = respuesta.read(_TROZO)
                    if not trozo:
                        break
                    bajados += len(trozo)
                    # Se comprueba mientras se baja y no solo por Content-Length:
                    # ese encabezado puede faltar o mentir.
                    if bajados > max_bytes:
                        fh.close()
                        os.remove(ruta)
                        raise DescargaFallida(
                            f"El archivo pesa más de "
                            f"{max_bytes // (1024 * 1024)} MB.")
                    fh.write(trozo)
    except DescargaFallida:
        raise
    except urllib.error.HTTPError as exc:
        raise DescargaFallida(f"El servidor respondió {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise DescargaFallida(f"No se pudo conectar: {exc.reason}.") from exc
    except OSError as exc:
        raise DescargaFallida(f"No se pudo guardar el archivo: {exc}") from exc

    if not bajados:
        os.remove(ruta)
        raise DescargaFallida("El enlace devolvió un archivo vacío.")
    return ruta


def descargar_varios(enlaces: list[tuple[int, str]], carpeta: str, *,
                     nombres: "dict[int, str] | None" = None,
                     on_progreso=None, cancelado=None) -> tuple[list[str], list[str]]:
    """Baja una tanda de enlaces. Devuelve (rutas_bajadas, errores).

    Un enlace roto NO detiene la tanda: se acumula su error y se sigue con el
    resto, igual que una carátula ilegible no tumba la lectura de una carpeta.
    Cincuenta descargas no se pueden perder porque la fila 12 esté mal.

    `enlaces` son pares (fila_del_excel, url) para poder decir en qué renglón
    estaba el que falló.
    """
    nombres = nombres or {}
    rutas: list[str] = []
    errores: list[str] = []
    total = len(enlaces)
    for indice, (fila, url) in enumerate(enlaces, start=1):
        if cancelado is not None and cancelado():
            break
        try:
            rutas.append(descargar(url, carpeta,
                                   nombre_sugerido=nombres.get(fila, "")))
        except DescargaFallida as exc:
            errores.append(f"Fila {fila}: {exc}")
        if callable(on_progreso):
            on_progreso(indice, total, url)
    return rutas, errores
