"""RPA del SIPP: sesión base del portal y flujo de solicitudes de pago.

`SesionSipp` encapsula lo **común** a cualquier módulo que opere el SIPP: ciclo
de vida del navegador, login, configuración de sesión, los `select` "chosen" de
AngularJS, el cierre de avisos emergentes y la captura de diagnósticos cuando un
localizador falla. `FlujoSolicitudPago` agrega encima el flujo concreto de este
proyecto. Cualquier módulo nuevo debe **reutilizar** `SesionSipp` en vez de
duplicar la automatización (ARQUITECTURA.md §8).

Es un port del RPA anterior (`RPA Solicitud de pagos/sipp_rpa.py`), que ya corrió
contra producción. Los *workarounds* de aquí no son precauciones teóricas: cada
uno corresponde a algo que falló de verdad. Antes de "simplificar" alguno, lee su
comentario.

Todo el módulo es **síncrono** (API `sync_playwright`), igual que el original. La
interfaz lo llama desde `asyncio.to_thread(...)` para no congelarse.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from core import (catalogos, db, documentos, dpapi, rutas, selectores,
                  sipp_datos, validador)
from core.db import CONCEPTO, INSUMO, Partida, Solicitud

# Carpetas de trabajo, siempre dentro de DATOS (nunca junto al .exe).
CARPETA_NAVEGADOR = os.path.join(rutas.DATOS, "ms-playwright")
CARPETA_EVIDENCIAS = os.path.join(rutas.DATOS, "evidencias")
CARPETA_DIAGNOSTICO = os.path.join(rutas.DATOS, "_diagnostico_rpa")
RUTA_SESION = os.path.join(rutas.DATOS, "storage_state.json")

TIMEOUT_MS = 60_000        # 60s: tolera equipos lentos y catálogos grandes
MAX_SUBIDA_BYTES = int(9.6 * 1024 * 1024)   # SIPP corta en 10 MB; con margen


# --------------------------------------------------------------------------- #
#  Errores de dominio
# --------------------------------------------------------------------------- #
class ErrorRpa(Exception):
    """Falla del robot que amerita reportarse al usuario."""


class RequiereRevision(ErrorRpa):
    """No es un error: el caso necesita criterio humano y no debe automatizarse
    (beneficiario con varias coincidencias, empresa sin el concepto pedido)."""


class SesionCaida(ErrorRpa):
    """La sesión de SIPP expiró o pide cambio de contraseña: hay que PAUSAR el
    lote entero, no seguir reintentando fila por fila."""


class Cancelado(Exception):
    """El usuario detuvo el lote."""


# --------------------------------------------------------------------------- #
#  Utilidades de archivos (SIPP es quisquilloso con lo que se le sube)
# --------------------------------------------------------------------------- #
def _mismo_texto(a: str, b: str) -> str:
    """Compara dos textos de opción como los ve una persona.

    SIPP devuelve los rótulos con espacios de sobra y a veces con la caja
    cambiada, así que comparar en crudo daría por distinto lo que en pantalla
    es lo mismo, y se volvería a elegir un valor que ya estaba puesto.
    """
    return " ".join((a or "").split()).casefold() ==         " ".join((b or "").split()).casefold()


def _nombre_limpio(nombre_archivo: str) -> str:
    """Quita acentos, Ñ y caracteres especiales: SIPP no procesa bien esos
    nombres y la subida falla sin decir por qué."""
    base, ext = os.path.splitext(nombre_archivo)
    b = unicodedata.normalize("NFKD", base)
    b = "".join(c for c in b if not unicodedata.combining(c))
    b = re.sub(r"[^A-Za-z0-9 _.-]", "_", b)
    b = re.sub(r"\s+", " ", b).strip(" ._-") or "archivo"
    return b + ext.lower()


def _comprimir_pdf(origen: str, destino: str, max_bytes: int) -> bool:
    """Baja un PDF de `max_bytes`. Primero sin pérdida; si no basta, re-rasteriza
    bajando DPI y calidad por escalones. Devuelve True si lo consiguió."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return False
    try:
        doc = fitz.open(origen)
        doc.save(destino, garbage=4, deflate=True, clean=True)
        doc.close()
        if os.path.getsize(destino) <= max_bytes:
            return True
        for dpi, calidad in ((150, 70), (120, 65), (100, 60), (90, 55), (72, 50)):
            try:
                src, out = fitz.open(origen), fitz.open()
                for pagina in src:
                    img = pagina.get_pixmap(dpi=dpi).tobytes(
                        "jpeg", jpg_quality=calidad)
                    nueva = out.new_page(width=pagina.rect.width,
                                         height=pagina.rect.height)
                    nueva.insert_image(pagina.rect, stream=img)
                out.save(destino, garbage=4, deflate=True)
                out.close()
                src.close()
                if os.path.getsize(destino) <= max_bytes:
                    return True
            except Exception:  # noqa: BLE001 — se prueba el siguiente escalón
                continue
        return os.path.getsize(destino) <= max_bytes
    except Exception:  # noqa: BLE001 — sin compresión se intenta subir igual
        return False


def texto_sipp(texto: str) -> str:
    """Deja un texto tal como SIPP lo va a aceptar.

    Varios campos del formulario llevan la directiva
    `contenido_alfanumerico_con_espacios`, que **descarta al teclear** todo lo
    que no sea letra, número o espacio. Verificado en stage el 31/07/2026: la
    descripción «PRUEBA AUTOMATIZADA - NO AUTORIZAR» quedó guardada como
    «PRUEBA AUTOMATIZADA  NO AUTORIZAR», sin el guion.

    No es cosmético. Si se guarda de un lado el texto con signos y del otro sin
    ellos, la búsqueda de idempotencia no encuentra lo que ya se capturó y el
    pago se registra dos veces. Por eso se normaliza ANTES de teclear y también
    antes de buscar.
    """
    limpio = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]", " ", texto or "")
    return re.sub(r"\s+", " ", limpio).strip()


def _comparable(texto: str) -> str:
    """Texto sin acentos, en minúsculas y sin signos: para comparar renglones."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", t)


def _palabras_clave(nombre: str) -> set[str]:
    """Palabras significativas de un nombre, para reconocerlo en un renglón.

    Se descartan las de una o dos letras (iniciales y partículas): aparecen y
    desaparecen entre lo que se capturó y lo que el listado muestra.
    """
    return {p for p in _comparable(nombre).split() if len(p) > 2}


def preparar_archivo(ruta: str) -> str:
    """Deja un archivo LISTO para SIPP: nombre saneado y, si es un PDF pesado,
    comprimido. Devuelve la ruta original si no hacía falta tocar nada."""
    if not ruta or not os.path.isfile(ruta):
        return ruta
    nombre = os.path.basename(ruta)
    limpio = _nombre_limpio(nombre)
    necesita_nombre = limpio != nombre
    necesita_comprimir = (ruta.lower().endswith(".pdf")
                          and os.path.getsize(ruta) > MAX_SUBIDA_BYTES)
    if not necesita_nombre and not necesita_comprimir:
        return ruta
    carpeta = os.path.join(tempfile.gettempdir(), "rpa_sipp_subida")
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, limpio)
    if necesita_comprimir and _comprimir_pdf(ruta, destino, MAX_SUBIDA_BYTES):
        return destino
    shutil.copy2(ruta, destino)
    return destino


# --------------------------------------------------------------------------- #
#  Navegador
# --------------------------------------------------------------------------- #
def _hay_navegadores(carpeta: str) -> bool:
    """True si esa carpeta es un almacén de navegadores de Playwright."""
    try:
        return any(n.startswith("chromium") for n in os.listdir(carpeta))
    except OSError:                 # no existe, o no se puede leer
        return False


def _instalar_chromium() -> None:
    """Ejecuta `playwright install chromium` de la forma que funcione aquí.

    En la aplicación empaquetada `sys.executable` **es la propia herramienta**,
    no un intérprete: llamarla con `-m playwright` abriría otra ventana de la
    app en vez de instalar nada. Ahí hay que invocar directamente el driver
    —node y su cli— que sí viaja dentro del paquete.
    """
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    entorno = None
    if getattr(sys, "frozen", False):
        from playwright._impl._driver import (compute_driver_executable,
                                              get_driver_env)
        node, cli = compute_driver_executable()
        orden = [node, cli, "install", "chromium"]
        entorno = get_driver_env()      # hereda PLAYWRIGHT_BROWSERS_PATH
    else:
        orden = [sys.executable, "-m", "playwright", "install", "chromium"]
    subprocess.run(orden, check=False, creationflags=flags, env=entorno)


def asegurar_navegador(on_aviso=None) -> None:
    """Deja `PLAYWRIGHT_BROWSERS_PATH` apuntando a un Chromium utilizable.

    El navegador NO se empaqueta —son ~150 MB que harían enorme el instalador—;
    el driver de Playwright sí va incluido. Si ya hay navegadores en la máquina
    se reutilizan y no se descarga nada.

    **La variable se exporta SIEMPRE, incluso cuando no hay nada que
    descargar.** No es un detalle: al detectar que la app está empaquetada,
    Playwright se pone `PLAYWRIGHT_BROWSERS_PATH=0` a sí mismo, y ese `0` no
    significa «el valor por defecto» sino «busca los navegadores DENTRO del
    paquete», que es justo donde nunca los hay. Como lo hace con `setdefault`,
    basta con que la variable traiga ya una ruta para que respete la nuestra.
    Antes, encontrar los navegadores del sistema hacía volver de aquí sin
    definirla, así que el camino bueno —no hay nada que descargar— era
    precisamente el que dejaba la app sin navegador (visto en producción el
    26/08/2026 al importar el catálogo de conceptos).
    """
    # Una ruta puesta a mano se respeta; el «0» de Playwright NO es una ruta.
    elegida = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if elegida and elegida != "0":
        return
    del_sistema = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    for carpeta in (del_sistema, CARPETA_NAVEGADOR):
        if _hay_navegadores(carpeta):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = carpeta
            return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = CARPETA_NAVEGADOR
    if callable(on_aviso):
        on_aviso("Descargando el navegador por única vez (~150 MB)…")
    os.makedirs(CARPETA_NAVEGADOR, exist_ok=True)
    _instalar_chromium()


# --------------------------------------------------------------------------- #
#  Sesión base del SIPP  [R/D]
# --------------------------------------------------------------------------- #
class SesionSipp:
    """Ciclo de vida del navegador y operaciones comunes del portal.

    Uso:
        with SesionSipp(url, visible=True) as s:
            s.login(usuario, contrasena)
            s.configurar_sesion("Aske", "Corporativo")
            s.ir_a_solicitud_pago()
    """

    def __init__(self, url_login: str, *, visible: bool = True,
                 timeout_ms: int = TIMEOUT_MS, on_bitacora=None,
                 reusar_sesion: bool = True) -> None:
        self.url_login = url_login
        self.visible = visible
        self.timeout_ms = timeout_ms
        self.reusar_sesion = reusar_sesion
        # Callback (paso, mensaje, nivel, captura) para alimentar la bitácora.
        self._on_bitacora = on_bitacora
        # Lo pone `procesar_lote`. Sin esto, «Detener» solo se atendía entre
        # solicitudes: los bucles de reintento —hasta cuatro vueltas con
        # esperas de 60 s— seguían hasta agotarse aunque ya se hubiera pedido
        # parar, y la app parecía congelada.
        self.cancelado = None
        self._pw = None
        self.navegador = None
        self.contexto = None
        self.page = None

    # ------------------------------------------------------------ ciclo
    def __enter__(self) -> "SesionSipp":
        self.abrir()
        return self

    def __exit__(self, *_exc) -> None:
        self.cerrar()

    def abrir(self) -> None:
        from playwright.sync_api import sync_playwright

        asegurar_navegador(lambda m: self.anotar("navegador", m))
        self._pw = sync_playwright().start()
        self.navegador = self._pw.chromium.launch(headless=not self.visible)
        estado = self._cargar_sesion() if self.reusar_sesion else None
        self.contexto = self.navegador.new_context(storage_state=estado)
        self.contexto.set_default_timeout(self.timeout_ms)
        self.page = self.contexto.new_page()

    def cerrar(self) -> None:
        # Cierre tolerante: si algo ya murió, no se puede impedir que la app siga.
        if self.reusar_sesion:
            try:
                self._guardar_sesion()
            except Exception:  # noqa: BLE001
                pass
        for cerrar in (getattr(self.contexto, "close", None),
                       getattr(self.navegador, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if callable(cerrar):
                    cerrar()
            except Exception:  # noqa: BLE001
                pass
        self.page = self.contexto = self.navegador = self._pw = None

    # --------------------------------------------------- sesión persistida
    def _cargar_sesion(self):
        """Cookies de una sesión anterior, descifradas. None si no hay o no se
        pueden leer (entonces se entra con usuario y contraseña, sin drama)."""
        try:
            if not os.path.exists(RUTA_SESION):
                return None
            with open(RUTA_SESION, encoding="utf-8") as fh:
                import json
                return json.loads(dpapi.descifrar(fh.read()))
        except Exception:  # noqa: BLE001 — sesión ilegible: se rehace el login
            return None

    def _guardar_sesion(self) -> None:
        """Guarda las cookies CIFRADAS con DPAPI. Nunca en claro: dan acceso al
        ERP a quien lea el archivo."""
        import json

        estado = self.contexto.storage_state()
        with open(RUTA_SESION, "w", encoding="utf-8") as fh:
            fh.write(dpapi.cifrar(json.dumps(estado)))

    # ------------------------------------------------------------ bitácora
    def anotar(self, paso: str, mensaje: str = "", nivel: str = "INFO",
               captura: str = "") -> None:
        if callable(self._on_bitacora):
            try:
                self._on_bitacora(paso, mensaje, nivel, captura)
            except Exception:  # noqa: BLE001 — la bitácora nunca frena el robot
                pass

    # --------------------------------------------------------- localizadores
    def loc(self, clave: str, dentro=None):
        """Localizador de una clave del mapa, probando primario y respaldos.

        Devuelve el primero que exista en el DOM; si ninguno aparece, devuelve el
        del primario para que el error posterior mencione el selector esperado.
        `dentro` acota la búsqueda a un panel (imprescindible con los ids
        duplicados de los tres tipos de beneficiario).
        """
        raiz = dentro if dentro is not None else self.page
        candidatos = selectores.todos(clave)
        for css in candidatos:
            try:
                loc = raiz.locator(css)
                if loc.count() > 0:
                    return loc
            except Exception:  # noqa: BLE001 — selector inválido: sigue el otro
                continue
        return raiz.locator(candidatos[0])

    def existe(self, clave: str, dentro=None) -> bool:
        try:
            return self.loc(clave, dentro).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def visible_(self, clave: str, dentro=None) -> bool:
        try:
            loc = self.loc(clave, dentro)
            return loc.count() > 0 and loc.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ evidencias
    def captura(self, etiqueta: str, carpeta: str = CARPETA_EVIDENCIAS) -> str:
        """Captura de pantalla con la contraseña ENMASCARADA.

        El enmascarado no es cosmético: la bitácora se comparte para diagnosticar
        y una captura del login mostraría la contraseña en claro.
        """
        try:
            os.makedirs(carpeta, exist_ok=True)
            nombre = f"{etiqueta}_{datetime.now():%Y%m%d_%H%M%S}.png"
            ruta = os.path.join(carpeta, nombre)
            tapar = []
            try:
                campo = self.page.locator(selectores.css("login.contrasena"))
                if campo.count() > 0:
                    tapar.append(campo.first)
            except Exception:  # noqa: BLE001
                pass
            self.page.screenshot(path=ruta, mask=tapar or None)
            return ruta
        except Exception:  # noqa: BLE001 — sin evidencia, pero el lote sigue
            return ""

    def diagnostico(self, etiqueta: str) -> str:
        """Guarda captura + HTML de la página cuando un localizador falla, para
        poder ajustar el mapa de selectores sin repetir la corrida."""
        ruta = self.captura(etiqueta, CARPETA_DIAGNOSTICO)
        try:
            os.makedirs(CARPETA_DIAGNOSTICO, exist_ok=True)
            destino = os.path.join(
                CARPETA_DIAGNOSTICO,
                f"{etiqueta}_{datetime.now():%Y%m%d_%H%M%S}.html")
            with open(destino, "w", encoding="utf-8") as fh:
                fh.write(self.page.content())
        except Exception:  # noqa: BLE001
            pass
        return ruta

    # ------------------------------------------------------- helpers de UI
    def _valor_visible(self, select) -> str:
        """El texto de la opción elegida en un <select>, aunque esté oculto."""
        try:
            return select.evaluate(
                "el => el.selectedIndex >= 0"
                " ? (el.options[el.selectedIndex].text || '') : ''") or ""
        except Exception:  # noqa: BLE001 — no es un <select> o ya no está
            return ""

    def seleccionar_chosen(self, clave_o_loc, texto: str, descripcion: str,
                           intentos: int = 4, dentro=None) -> None:
        """Elige una opción en un `select` "chosen" de SIPP.

        El `<select>` nativo está OCULTO (`display:none`) tras el widget: no se
        puede usar `select_option`. Hay que abrir el widget, escribir en su
        buscador y clicar el resultado.

        Dos detalles que costaron caro:
          - Se **teclea** con `type()`, nunca `fill()`: chosen filtra en el
            evento `keyup`, y sin él las opciones más allá de las ~100 que
            renderiza de entrada nunca aparecen (el catálogo de beneficiarios es
            mucho mayor).
          - Se reintenta cerrando con Escape y reabriendo: las listas
            dependientes (Sucursal, Tipo de Pago Extraordinario, Cuentas) se
            llenan DESPUÉS de elegir su campo padre.
        """
        select = (self.loc(clave_o_loc, dentro)
                  if isinstance(clave_o_loc, str) else clave_o_loc)
        select.first.wait_for(state="attached", timeout=self.timeout_ms)

        # ¿Ya está elegido? Es lo más barato de comprobar y ocurre a menudo:
        # SIPP preselecciona valores y varias listas se vuelven a pedir al
        # recargar el formulario. Sin esto se abría el widget, se tecleaba y se
        # clicaba para dejar el campo exactamente como estaba.
        if _mismo_texto(self._valor_visible(select.first), texto):
            return

        # Las listas dependientes (Sucursal, Tipo de Pago, Cuentas) se llenan
        # DESPUÉS de elegir su campo padre. Se espera a que la opción exista de
        # verdad ANTES de abrir el widget: si se entra antes, el primer intento
        # falla seguro y se pagan cuatro ciclos de reintento —casi treinta
        # segundos— para acabar acertando por tiempo, no por lógica.
        self.esperar_a(
            lambda: select.first.locator("option", has_text=texto).count() > 0,
            tope_ms=12000, intervalo_ms=250)

        contenedor = select.first.locator(
            "xpath=following-sibling::div[contains(@class,'chosen-container')][1]")
        contenedor.wait_for(state="visible", timeout=self.timeout_ms)

        # El scroll va UNA vez, fuera del bucle. Dentro, cada reintento volvía a
        # llevar la página al campo y el resultado era un vaivén de arriba
        # abajo que parecía que el robot buscaba a ciegas. El campo no se mueve
        # entre intentos: basta con dejarlo a la vista al principio.
        try:
            contenedor.scroll_into_view_if_needed()
        except Exception:  # noqa: BLE001 — ya estaba a la vista
            pass

        ultimo = None
        for intento in range(1, intentos + 1):
            self.abortar_si_cancelan()
            try:
                contenedor.click()
                buscador = contenedor.locator("input.chosen-search-input")
                # Chosen se puebla cuando Angular termina de llenar el <select>
                # y dispara `chosen:updated`. Puede haber opciones en el select
                # y todavía ninguna en el widget: escribir en ese hueco filtra
                # sobre una lista vacía y el intento se pierde entero.
                self.esperar_a(
                    lambda: contenedor.locator("li.active-result").count() > 0,
                    tope_ms=4000, intervalo_ms=100)
                buscador.click()
                buscador.press("Control+a")
                buscador.press("Delete")
                buscador.type(texto, delay=20)
                opcion = contenedor.locator("li.active-result",
                                            has_text=texto).first
                # Chosen filtra en `keyup`: el resultado aparece en cuanto
                # termina de filtrar, normalmente en decenas de milisegundos.
                self.esperar_a(lambda: opcion.count() > 0, tope_ms=6000,
                               intervalo_ms=100)
                opcion.wait_for(state="visible", timeout=6000)
                opcion.click()
                return
            except Cancelado:
                raise                      # parar manda sobre reintentar
            except Exception as exc:  # noqa: BLE001 — se reintenta
                ultimo = exc
                self.anotar("seleccionar",
                            f"reintento {intento}/{intentos} en «{descripcion}»",
                            "WARN")
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
                self.page.wait_for_timeout(1200)
        self.diagnostico(f"chosen_{descripcion}")
        raise ErrorRpa(
            f"No se pudo elegir «{texto}» en «{descripcion}»: la lista no cargó "
            f"la opción a tiempo. ({ultimo})")

    def llenar(self, clave_o_loc, texto: str, descripcion: str,
               dentro=None, estricto: bool = False) -> None:
        """Escribe en un campo, dispara los eventos de Angular y RELEE.

        Sin `input`/`change` a mano, el modelo de Angular no se entera de nada y
        el valor se pierde al guardar, aunque en pantalla se vea escrito.

        La relectura no es paranoia: los campos de SIPP llevan directivas que
        **descartan caracteres al teclear** (`contenido_numerico`,
        `contenido_alfanumerico_con_espacios`) y respetan `maxlength`. Escribir
        y no mirar deja pasar un valor recortado sin que nadie se entere.

        `estricto=True` convierte la discrepancia en error, y es lo que hay que
        usar donde un valor a medias tiene consecuencias de dinero —la CLABE, el
        RFC—. En los demás basta con dejar constancia.
        """
        loc = (self.loc(clave_o_loc, dentro)
               if isinstance(clave_o_loc, str) else clave_o_loc)
        campo = loc.first
        campo.wait_for(state="visible", timeout=self.timeout_ms)
        campo.scroll_into_view_if_needed()
        esperado = str(texto).strip()

        # Primero de golpe con `fill`, que es UNA orden al navegador. Teclear
        # carácter por carácter cuesta un viaje por letra más su `delay`, y en
        # campos largos —o en la fecha, que dispara validación en cada tecla—
        # se nota. Si el valor no queda (los campos con máscara ignoran `fill`
        # o lo reformatean), se reintenta tecleando, que es como se hacía
        # siempre. Se conserva el camino lento como red, no como norma.
        def _quedo() -> "str | None":
            try:
                return (campo.input_value() or "").strip()
            except Exception:  # noqa: BLE001 — no es un <input>
                return None

        campo.fill("")
        try:
            campo.fill(str(texto))
        except Exception:  # noqa: BLE001 — no admite fill: se teclea
            pass
        if _quedo() not in (None, esperado):
            campo.fill("")
            campo.type(str(texto), delay=15)
        campo.dispatch_event("input")
        campo.dispatch_event("change")

        quedo = _quedo()
        if quedo is None:
            return
        if quedo == esperado:
            return
        detalle = (f"«{descripcion}»: se escribió «{esperado}» y quedó "
                   f"«{quedo}»")
        if estricto:
            self.diagnostico(f"campo_{descripcion}")
            raise ErrorRpa(
                f"{detalle}. SIPP no aceptó el valor completo; no se continúa "
                f"para no capturar un dato incorrecto.")
        self.anotar("campo", detalle + " (SIPP filtró parte del texto)", "WARN")

    def abortar_si_cancelan(self) -> None:
        """Corta lo que se esté haciendo si ya se pulsó «Detener»."""
        if callable(self.cancelado) and self.cancelado():
            raise Cancelado()

    def esperar_a(self, condicion, tope_ms: int, intervalo_ms: int = 150) -> bool:
        """Sondea `condicion()` hasta que se cumpla o se agote `tope_ms`.

        Sustituye a los `wait_for_timeout` fijos donde SÍ hay forma de saber que
        el paso terminó. El tope se conserva igual que la espera fija que
        reemplaza, así que en el peor caso se tarda lo mismo que antes; lo que
        cambia es que en el caso normal —que es el 99%— se sigue de largo en
        cuanto la página responde, en vez de esperar el máximo siempre.
        """
        vueltas = max(1, tope_ms // max(1, intervalo_ms))
        for _ in range(vueltas):
            self.abortar_si_cancelan()
            try:
                if condicion():
                    return True
            except Exception:  # noqa: BLE001 — la página puede estar navegando
                pass
            self.page.wait_for_timeout(intervalo_ms)
        try:
            return bool(condicion())
        except Exception:  # noqa: BLE001
            return False

    def esperar_quieto(self, tope_ms: int) -> None:
        """Espera a que no queden peticiones en vuelo, con tope.

        SIPP hace su trabajo por AJAX: subir un archivo o cambiar de pestaña
        dispara peticiones y la interfaz se actualiza al terminar. Esperar a que
        la red se calme mide lo que de verdad importa —que el servidor ya
        contestó— en vez de apostar un número de segundos.
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=tope_ms)
        except Exception:  # noqa: BLE001 — si nunca se calma, manda el tope
            pass

    def cerrar_alertas(self, vueltas: int = 6) -> None:
        """Cierra los avisos emergentes de SIPP.

        Clica específicamente **Aceptar**: varias alertas traen también «Ver
        detalle», y clicar el primer botón deja el aviso abierto en bucle.
        """
        try:
            for _ in range(vueltas):
                btn = self.loc("alerta.aceptar").first
                if btn.count() == 0:
                    return
                btn.click(timeout=5000)
                self.page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 — no había alerta o ya se cerró
            pass

    def texto_alertas(self) -> str:
        """Texto de las alertas visibles, en minúsculas (para reconocerlas)."""
        partes = []
        try:
            alertas = self.loc("alerta")
            for i in range(alertas.count()):
                el = alertas.nth(i)
                if el.is_visible():
                    partes.append((el.inner_text() or "").lower())
        except Exception:  # noqa: BLE001
            pass
        return " ".join(partes)

    def consultar(self, nombre: str, defecto=None):
        """Lee del modelo del portal lo que la pantalla no pinta.

        Devuelve `defecto` si la consulta no está en el mapa o si la página no
        la puede resolver: son lecturas de apoyo, y ninguna vale una excepción
        que tumbe la captura.
        """
        try:
            return self.page.evaluate(sipp_datos.consulta(nombre))
        except Exception:  # noqa: BLE001 — mapa desactualizado o página en vuelo
            return defecto

    def espiar_validaciones(self) -> None:
        """Empieza a registrar los avisos de validación del portal.

        SIPP rechaza un formulario de dos maneras y solo una se ve: además de
        las alertas emergentes, valida campo por campo con un mensaje pegado al
        campo y **corta ahí mismo**, sin alerta, sin diálogo y sin llamar al
        servidor. Un rechazo así es indistinguible de «el botón no hizo nada»:
        el robot se quedaba veinte segundos esperando un folio que ya nunca iba
        a llegar y reportaba que SIPP no había confirmado, sin decir por qué.

        Se envuelven las funciones que el portal usa para avisar, de modo que
        cada mensaje quede anotado aunque nadie lo vea y aunque `cerrar_alertas`
        lo tape un segundo después. Las originales se siguen llamando: esto
        observa, no cambia el comportamiento del portal.
        """
        try:
            self.page.evaluate(sipp_datos.consulta("espiar_validaciones"))
        except Exception:  # noqa: BLE001 — sin espía se sigue como antes
            self.anotar("rpa", "No se pudo observar las validaciones de SIPP; "
                        "si algo falla, el motivo puede quedar sin detalle",
                        "WARN")

    def validaciones(self) -> list[str]:
        """Avisos de validación registrados desde la última consulta.

        Vacía la lista al leerla: así cada paso pregunta por lo que SIPP dijo
        de ÉL y no arrastra el aviso de un paso anterior ya resuelto.
        """
        try:
            crudas = self.page.evaluate(
                sipp_datos.consulta("leer_validaciones")) or []
        except Exception:  # noqa: BLE001 — no se instaló o la página cambió
            return []
        mensajes = []
        for v in crudas:
            texto = " ".join(re.sub(r"<[^>]+>", " ",
                                    str(v.get("msg", ""))).split())
            if texto and texto not in mensajes:
                mensajes.append(texto)
        return mensajes

    def hay_error_sistema(self) -> bool:
        """Error del sistema visible, incluido el fallo intermitente de subida a
        Google Cloud Storage que SIPP arrastra."""
        t = self.texto_alertas()
        return any(x in t for x in (
            "error del sistema", "google rest", "making google", "cloudstorage",
            "403 forbidden"))

    def clic_con_reintento(self, clave_o_loc, descripcion: str,
                           intentos: int = 3) -> None:
        """Clic en una acción crítica, reintentando si la página no responde.
        Para Guardar y Solicitar Autorización en conexiones lentas."""
        loc = (self.loc(clave_o_loc) if isinstance(clave_o_loc, str)
               else clave_o_loc)
        ultimo = None
        for intento in range(1, intentos + 1):
            self.abortar_si_cancelan()
            try:
                loc.first.click(timeout=self.timeout_ms)
                return
            except Cancelado:
                raise
            except Exception as exc:  # noqa: BLE001
                ultimo = exc
                self.anotar("clic",
                            f"reintento {intento}/{intentos} en «{descripcion}»",
                            "WARN")
                self.cerrar_alertas()
                self.page.wait_for_timeout(2000)
        raise ErrorRpa(f"No se pudo pulsar «{descripcion}» tras {intentos} "
                       f"intentos: la página no respondió. ({ultimo})")

    def subir_archivo(self, clave_o_loc, ruta: str, etiqueta: str,
                      intentos: int = 3, dentro=None) -> None:
        """Sube un archivo reintentando ante el error de Google.

        Esa subida falla de forma intermitente por causas del propio SIPP; sin
        reintento, un lote largo se cae por algo que se resuelve solo.
        """
        ruta = preparar_archivo(ruta)
        entrada = (self.loc(clave_o_loc, dentro)
                   if isinstance(clave_o_loc, str) else clave_o_loc)
        for intento in range(1, intentos + 1):
            self.abortar_si_cancelan()
            entrada.first.set_input_files(ruta)
            # La subida es una petición AJAX: se espera a que la red se calme en
            # vez de contar cuatro segundos siempre. Una carátula de 200 KB
            # termina en menos de uno, y son dos documentos por solicitud.
            self.esperar_quieto(4000)
            if not self.hay_error_sistema():
                return
            self.anotar("subir", f"«{etiqueta}»: falló la subida "
                                 f"({intento}/{intentos}); reintentando", "WARN")
            self.cerrar_alertas()
            self.page.wait_for_timeout(1500)
        raise ErrorRpa(f"No se pudo subir «{etiqueta}» tras {intentos} intentos "
                       f"(error de almacenamiento del SIPP).")

    def abrir_pestana(self, nombre: str) -> None:
        """Activa una pestaña del formulario por su TEXTO visible.

        Se usa el texto porque los índices internos (`tab == 4`, `tab == 5`) no
        aparecen en el DOM y los `id` de las pestañas cambian según el tipo de
        pago.

        La búsqueda se ACOTA a la tira de pestañas: un `li a` suelto con el texto
        «Insumos» encuentra nueve elementos en la página, casi todos del menú
        lateral, y clicar uno de ellos sacaría al robot del formulario.
        """
        etiqueta = selectores.pestana(nombre)
        for contenedor in selectores.todos("tabs.tira"):
            loc = self.page.locator(contenedor, has_text=etiqueta)
            if loc.count() > 0:
                loc.first.click()
                # Cambiar de pestaña pide su contenido por AJAX; se espera a que
                # llegue, no a que pasen 800 ms.
                self.esperar_quieto(800)
                return
        self.diagnostico(f"pestana_{nombre}")
        raise ErrorRpa(f"No apareció la pestaña «{etiqueta}» en el formulario.")

    # ------------------------------------------------------------ navegación
    def login(self, usuario: str, contrasena: str) -> None:
        self.anotar("login", "Abriendo el portal")
        self.page.goto(self.url_login, timeout=self.timeout_ms)
        # Con sesión reutilizada, SIPP redirige solo y no hay formulario.
        if "login" not in (self.page.url or "").lower():
            self.anotar("login", "Sesión anterior reutilizada")
            return
        self.loc("login.usuario").first.fill(usuario)
        self.loc("login.contrasena").first.fill(contrasena)
        self.loc("login.entrar").first.click()
        try:
            self.page.wait_for_url("**/index.cfm*", timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # Antes de dar el login por fallido, revisa si SIPP está pidiendo
            # cambiar la contraseña predeterminada: es un modal, no un error.
            if self.visible_("login.modal_contrasena"):
                raise SesionCaida(
                    "SIPP pide actualizar la contraseña de este usuario. "
                    "Entra a mano al portal, cámbiala y vuelve a intentar."
                ) from exc
            ruta = self.diagnostico("login")
            raise ErrorRpa(
                "No se pudo iniciar sesión en SIPP. Revisa usuario y contraseña "
                "en Configuración."
            ) from exc
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 — la SPA sigue con peticiones abiertas
            pass
        self.anotar("login", "Sesión iniciada")

    def configurar_sesion(self, empresa_corta: str, sucursal: str) -> bool:
        """Llena la pantalla de Configuración de Sesión (Empresa + Plaza).

        Las empresas se muestran con el nombre largo y el corto entre
        paréntesis: `ADMINISTRACION DE SERVICIOS ASKE - (Aske )`. Se busca por el
        corto entre paréntesis, incluido el espacio, que así viene del sistema.
        Devuelve False si esa pantalla no apareció (sesión ya configurada).
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        select = self.loc("sesion.empresa")
        try:
            select.first.wait_for(state="attached", timeout=8000)
        except Exception:  # noqa: BLE001
            self.anotar("sesion", "No apareció la configuración de sesión")
            return False
        self.seleccionar_chosen(select, f"({empresa_corta} )", "Empresa de sesión")
        self.page.wait_for_timeout(1500)   # la lista de plazas se carga después
        self.seleccionar_chosen("sesion.sucursal", sucursal, "Plaza de sesión")
        self.loc("sesion.guardar").first.click()
        self.page.wait_for_timeout(1500)

        # Se comprueba que la pantalla se haya ido. Si sigue ahí, la sesión no
        # quedó configurada y todo el lote se capturaría en la empresa que
        # estuviera activa antes —el peor error posible, porque las solicitudes
        # sí se crean, solo que en la empresa equivocada.
        if self.visible_("sesion.empresa"):
            self.diagnostico("sesion_no_configurada")
            raise ErrorRpa(
                f"No se pudo fijar la sesión en {empresa_corta} / {sucursal}: "
                f"la pantalla de configuración sigue abierta.")
        self.anotar("sesion", f"Sesión en {empresa_corta} / {sucursal}")
        return True

    def ir_a_solicitud_pago(self) -> None:
        """Navega a Solicitud de Pago. Es una SPA: se cambia el hash, no la URL."""
        try:
            self.page.evaluate("window.location.hash = '#/SolicitudPago'")
        except Exception:  # noqa: BLE001 — respaldo: clic en el menú
            self.page.locator("a[href='#/SolicitudPago']").first.click()
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.loc("listado.crear").first.wait_for(
                state="visible", timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            self.diagnostico("navegacion")
            raise ErrorRpa(
                "No se llegó al listado de Solicitud de Pago. ¿El usuario tiene "
                "permiso sobre ese módulo?") from exc

    def verificar_selectores(self) -> dict[str, bool]:
        """Comprueba qué claves del mapa resuelven en la página actual.

        Es lo que usa «Verificar conexión con SIPP»: detecta un cambio del portal
        ANTES de lanzar un lote, en vez de a media captura. No llena ni guarda
        nada. Las claves de otras pantallas (login, modales cerrados) dan False
        de forma normal: lo que importa es el bloque de la pantalla en curso.
        """
        return {clave: self.existe(clave) for clave in selectores.claves()}


# --------------------------------------------------------------------------- #
#  Flujo de dominio: captura de una solicitud de pago
# --------------------------------------------------------------------------- #
@dataclass
class ResultadoCaptura:
    """Qué pasó con una solicitud."""

    estado: str                       # GUARDADA | LLENADA | ENVIADA_AUTORIZAR…
    folio_sipp: str = ""
    mensaje: str = ""
    capturas: list[str] = field(default_factory=list)


class FlujoSolicitudPago:
    """Captura solicitudes de tipo Pago Extraordinario sobre `SesionSipp`."""

    def __init__(self, sesion: SesionSipp, cancelado=None) -> None:
        self.s = sesion
        # Se guarda en la SESIÓN, no aquí: sus esperas y bucles de reintento
        # son los tramos largos y también tienen que poder cortarse. Así hay
        # un solo sitio que sabe si ya se pidió parar.
        if cancelado is not None:
            sesion.cancelado = cancelado

    def abortar_si_cancelan(self) -> None:
        """Corta la captura en curso si se pulsó «Detener».

        Se llama ENTRE pasos —no a media pulsación, que no se puede—, así que
        el corte tarda lo que tarde el paso que está corriendo, no la solicitud
        entera. Lo que se deja a medias en SIPP es un formulario sin guardar:
        no queda folio ni pago, y esa solicitud vuelve a su estado anterior
        para que el siguiente lote la tome desde cero.
        """
        self.s.abortar_si_cancelan()

    # ------------------------------------------------------------ helpers
    def _panel(self, tipo_beneficiario: str):
        """Panel del tipo de beneficiario activo.

        Acotar por aquí es OBLIGATORIO: varios campos del beneficiario existen
        TRES veces en el DOM, una por panel. Sin acotar,
        Playwright escribe en el primero que encuentra, que suele estar oculto.
        """
        tipo_id = catalogos.ID_PANEL_BENEFICIARIO.get(tipo_beneficiario, 3)
        return self.s.page.locator(selectores.panel_beneficiario(tipo_id))

    def asegurar_modo_agregar(self) -> None:
        try:
            crear = self.s.loc("listado.crear").first
            if crear.count() > 0 and crear.is_visible():
                crear.click()
        except Exception:  # noqa: BLE001 — quizá ya estamos en el formulario
            pass
        self.s.loc("sol.empresa").first.wait_for(
            state="attached", timeout=self.s.timeout_ms)

    def volver_al_listado(self) -> None:
        """Deja un formulario limpio para la siguiente solicitud.

        Si algo quedó atorado (un modal que no cierra tras el error de Google),
        recarga la página: es más barato que intentar desatorarlo paso a paso.
        """
        self.s.cerrar_alertas()
        try:
            btn = self.s.loc("listado.regresar").first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                self.s.page.wait_for_timeout(1200)
                self.s.cerrar_alertas()
                if self.s.visible_("listado.crear"):
                    return
        except Exception:  # noqa: BLE001 — se pasa a la recuperación dura
            pass
        self.s.anotar("recuperar", "Formulario atorado: recargando la página",
                      "WARN")
        try:
            self.s.page.reload(timeout=self.s.timeout_ms)
            self.s.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        self.s.cerrar_alertas()
        self.s.ir_a_solicitud_pago()

    # -------------------------------------------------------- idempotencia
    def dejar_listado_limpio(self) -> None:
        """Deja el portal en el listado general, SIN filtros, al terminar.

        El navegador se queda abierto para revisar lo capturado, y lo que se
        quiere ver entonces son TODAS las solicitudes del lote. Pero el listado
        conserva los filtros de la última búsqueda de duplicados —se filtra por
        descripción para no capturar dos veces el mismo pago—, así que sin
        limpiarlos se ve una sola fila, o ninguna, y parece que no se capturó
        nada.

        Es cortesía, no parte de la captura: cualquier fallo aquí se anota y se
        sigue. El lote ya terminó y su resultado no depende de esto.
        """
        s = self.s
        try:
            self.volver_al_listado()
            for clave in ("listado.filtro_beneficiario",
                          "listado.filtro_descripcion",
                          "listado.filtro_desde", "listado.filtro_hasta"):
                if s.existe(clave):
                    try:
                        s.loc(clave).first.fill("")
                    except Exception:  # noqa: BLE001 — ese filtro no aplica
                        pass
            if s.existe("listado.buscar"):
                s.loc("listado.buscar").first.click()
                s.esperar_quieto(8000)
            s.anotar("listado", "Listado general sin filtros, listo para revisar")
        except Exception as exc:  # noqa: BLE001 — el lote ya terminó
            s.anotar("listado",
                     f"No se pudo dejar el listado sin filtros: {exc}", "WARN")

    def buscar_existente(self, solicitud: Solicitud) -> str:
        """Busca en el listado si esta solicitud ya se capturó. Devuelve el folio
        si la encuentra, o cadena vacía.

        Es la salvaguarda contra el doble pago cuando un lote se reanuda: se
        consulta ANTES de capturar.

        Cómo se filtra, y por qué así (verificado en stage el 01/08/2026):

        - **NO por el campo de beneficiario.** `filtros.nb_Proveedor` es
          `readonly` y `ng-disabled="true"`: lo llena el buscador de proveedores,
          no se puede teclear. Escribir ahí hacía que la consulta muriera por
          timeout y, con el error tragado, la salvaguarda fallaba EN SILENCIO
          dando vía libre a capturar el pago dos veces.
        - **Por descripción**, que sí está habilitada, normalizada con
          `texto_sipp` porque SIPP descarta los signos al guardar y el texto
          original no encontraría nada.
        - Y luego se **confirma sobre los renglones**: el beneficiario y la
          descripción tienen que aparecer en la fila. El filtro acota; lo que
          decide es el contenido.

        **Limitación conocida.** El listado NO muestra el importe —sus columnas
        son empresa, sucursal, clave, tipo de pago, beneficiario, folio OC,
        descripción, solicitante, estatus y fecha de registro—, así que dos
        pagos al mismo beneficiario con la misma descripción son
        indistinguibles desde aquí y el segundo se daría por capturado. El
        equilibrio es deliberado: dejar de pagar algo se nota al conciliar y se
        corrige; pagar dos veces hay que perseguirlo con el banco. Para
        eliminarlo del todo habría que abrir cada renglón y leer su importe, o
        marcar la descripción con una referencia propia.

        Si algo falla, se avisa con el error concreto y se devuelve "" (no se
        encontró). Ojo: eso deja pasar la captura, así que un fallo aquí es un
        aviso serio, no ruido.
        """
        s = self.s
        try:
            descripcion = texto_sipp(solicitud.descripcion)
            # Se escribe SIEMPRE, aunque venga vacía: el filtro persiste entre
            # búsquedas, así que una solicitud sin descripción heredaría la de
            # la anterior y buscaría a la persona equivocada —devolviendo cero
            # renglones y dando por bueno capturar de nuevo.
            if s.existe("listado.filtro_descripcion"):
                s.llenar("listado.filtro_descripcion", descripcion,
                         "Filtro descripción")
            s.loc("listado.buscar").first.click()
            s.page.wait_for_timeout(2500)

            objetivo = _palabras_clave(solicitud.beneficiario_nombre)
            marca = _palabras_clave(descripcion)
            filas = s.loc("listado.filas")
            total = filas.count()
            for i in range(total):
                crudo = filas.nth(i).inner_text() or ""
                palabras = set(_comparable(crudo).split())
                if not objetivo or not objetivo.issubset(palabras):
                    continue
                if marca and not marca.issubset(palabras):
                    continue          # mismo beneficiario, otra solicitud
                # La columna «Clave» del listado es el folio de la solicitud.
                folio = re.search(r"\b(\d{3,})\b", crudo)
                return folio.group(1) if folio else "?"
            s.anotar("idempotencia",
                     f"Sin coincidencias en {total} renglón(es) del listado")
        except Exception as exc:  # noqa: BLE001 — se reporta CON el motivo
            s.anotar("idempotencia",
                     f"No se pudo consultar el listado previo: {exc}", "WARN")
        return ""

    # ------------------------------------------------------------ llenado
    def llenar(self, solicitud: Solicitud, partidas: list[Partida],
               archivos: dict[str, str] | None = None) -> None:
        """Llena el formulario completo SIN guardar."""
        archivos = archivos or {}
        s = self.s
        self.asegurar_modo_agregar()
        # Desde aquí queda constancia de todo lo que SIPP objete, incluidos los
        # avisos que el propio robot cierra al pasar al campo siguiente.
        s.espiar_validaciones()

        self.abortar_si_cancelan()
        # 1) Encabezado.
        s.seleccionar_chosen("sol.empresa", solicitud.empresa, "Empresa")
        s.seleccionar_chosen("sol.sucursal", solicitud.sucursal, "Sucursal")
        s.seleccionar_chosen("sol.tipo_pago", solicitud.tipo_pago, "Tipo de pago")
        s.seleccionar_chosen("sol.tipo_beneficiario", solicitud.tipo_beneficiario,
                             "Tipo de beneficiario")
        s.page.wait_for_timeout(1500)   # SIPP muestra sus avisos aquí

        # SIPP avisa cuando la empresa no tiene conceptos asignados para este
        # tipo de beneficiario. Sin conceptos la solicitud se guardaría en $0,
        # así que se cancela en vez de capturar basura.
        if "no tiene conceptos" in s.texto_alertas():
            s.cerrar_alertas()
            raise RequiereRevision(
                f"La empresa «{solicitud.empresa}» no tiene conceptos de gasto "
                f"asignados para {solicitud.tipo_beneficiario}.")
        s.cerrar_alertas()

        self.abortar_si_cancelan()
        # 2) Beneficiario.
        panel = self._panel(solicitud.tipo_beneficiario)
        self._resolver_beneficiario(solicitud, panel, archivos)

        # 3) Documento de respaldo del beneficiario (campo PDF).
        # OJO con el momento de la subida, que NO es el mismo en los dos campos
        # (verificado en stage el 31/07/2026 leyendo el modelo de Angular):
        #   - El campo PDF de la solicitud (este) solo **engancha** el archivo
        #     al modelo; SIPP lo sube al pulsar Guardar, y el error de
        #     almacenamiento, si aparece, lo detecta `guardar()`.
        #   - El del modal de cuenta bancaria sí sube al cerrar el modal, y
        #     ahí es donde el reintento de `subir_archivo` hace su trabajo.
        # No cambies uno por el otro pensando que son equivalentes.
        if archivos.get("caratula"):
            s.subir_archivo("sol.pdf", archivos["caratula"], "Carátula")

        self.abortar_si_cancelan()
        # 4) Moneda y descripción. La FECHA no va aquí: ver el paso 6.
        if solicitud.moneda and s.visible_("sol.moneda"):
            try:
                s.loc("sol.moneda").first.select_option(label=solicitud.moneda)
            except Exception:  # noqa: BLE001 — se deshabilita si ya hay cuenta
                pass
        if solicitud.descripcion:
            deseada = texto_sipp(solicitud.descripcion)
            if deseada != solicitud.descripcion:
                s.anotar("descripcion",
                         "SIPP no acepta signos en la descripción; se captura "
                         f"como «{deseada}»", "WARN")
            # `llenar` ya relee y avisa si el portal filtró algo más de lo
            # previsto, que aquí importa: la búsqueda de idempotencia usa esta
            # misma descripción para reconocer la solicitud después.
            s.llenar("sol.descripcion", deseada, "Descripción")

        self.abortar_si_cancelan()
        # 5) Desglose. Cuál de los dos grids existe lo decide el tipo de
        #    beneficiario, y son excluyentes (ver core/catalogos.py): a un
        #    Proveedor se le capturan Insumos & Servicios; a un Deudor o
        #    Acreedor, Conceptos de Pago. Intentar el otro abriría una pestaña
        #    que no está en pantalla.
        clase = catalogos.clase_desglose(solicitud.tipo_beneficiario)
        renglones = [p for p in partidas if p.clase == clase]
        if not renglones:
            raise ErrorRpa(
                f"La solicitud no trae renglones de "
                f"{'insumos y servicios' if clase == INSUMO else 'conceptos de pago'}, "
                f"que es el desglose que SIPP pide para "
                f"{solicitud.tipo_beneficiario}: se guardaría en $0.")
        descartados = [p for p in partidas if p.clase != clase]
        if descartados:
            s.anotar("desglose",
                     f"{len(descartados)} renglón(es) del otro tipo se omiten: "
                     f"con {solicitud.tipo_beneficiario} esa pestaña no existe",
                     "WARN")
        if clase == INSUMO:
            self._llenar_insumos(renglones)
        else:
            self._llenar_conceptos(renglones)

        # 6) La fecha de pago, AL FINAL y no con los demás campos de cabecera.
        #    Elegir un concepto recarga el grid y SIPP borra la fecha por el
        #    camino: puesta antes, se perdía y el Guardar no confirmaba nada
        #    —«no hay folio ni aparece Solicitar Autorización»— sin decir por
        #    qué. Es el último campo que se toca, así que ya nada la pisa.
        self._poner_fecha(solicitud)

    def _poner_fecha(self, solicitud: Solicitud) -> None:
        """Escribe la fecha de pago y comprueba que SIPP la haya tomado.

        El campo de fecha no es un campo: es un texto con máscara que SIPP
        convierte a fecha de verdad **al perder el foco**, y solo entonces la
        pasa al modelo que se guarda. Escribir y disparar los eventos de siempre
        deja el formulario con la fecha a la vista y el modelo vacío; el portal
        acepta seguir, y al guardar corta con «La información de la Fecha Pago
        es requerida» pegada al campo, sin alerta ni diálogo (verificado en
        stage el 24/08/2026 leyendo `directivas.js`). Por eso aquí se comprueba
        el MODELO y no lo que se ve en pantalla: mirar el recuadro es
        exactamente lo que hacía que esto pasara inadvertido.

        Se reintenta una vez porque el grid de conceptos se repinta de forma
        asíncrona y puede limpiar la fecha justo después de escribirla.
        """
        if not solicitud.fecha_pago:
            return
        s = self.s
        for intento in (1, 2):
            s.llenar("sol.fecha_pago", solicitud.fecha_pago, "Fecha de pago")
            # El foco sale del campo por la vía normal —así es como el portal
            # espera enterarse— y se comprueba qué quedó registrado.
            if s.esperar_a(lambda: bool(s.consultar("asentar_fecha_pago", "")),
                           tope_ms=4000, intervalo_ms=300):
                return
            s.anotar("fecha", f"SIPP no registró la fecha de pago tras "
                              f"escribirla (intento {intento}/2); se reintenta",
                     "WARN")
            s.page.wait_for_timeout(400)
        s.captura("fecha_no_registrada")
        raise ErrorRpa(
            "La fecha de pago no llega al formulario de SIPP: se escribe, se "
            "ve en pantalla y el portal la descarta. Sin ella rechaza el "
            "guardado sin decir por qué.")

    def _resolver_beneficiario(self, solicitud: Solicitud, panel,
                               archivos: dict) -> None:
        """Decide si el beneficiario ya existe en SIPP y actúa en consecuencia.

        **Se consulta el catálogo primero, siempre.** El campo
        `beneficiario_nuevo` de la solicitud es una expectativa de quien capturó,
        no un hecho: la persona que llena el Excel no tiene forma de saber si el
        beneficiario ya está dado de alta, y equivocarse sale caro en los dos
        sentidos —dar de alta un duplicado, o buscar a alguien que no existe—.
        Quien sabe la verdad es el propio SIPP, así que se le pregunta.

        Cuando la expectativa y la realidad no coinciden, se deja constancia y
        manda la realidad.
        """
        s = self.s
        nombre = solicitud.beneficiario_nombre
        estado = self._buscar_beneficiario(nombre)

        if estado == "ambiguo":
            raise RequiereRevision(
                f"«{nombre}» tiene varias coincidencias en el catálogo de SIPP; "
                f"elige a mano cuál corresponde.")

        if estado == "existente":
            if solicitud.beneficiario_nuevo:
                s.anotar("beneficiario",
                         "Venía marcado como nuevo, pero ya está en SIPP: se "
                         "usa el existente y no se duplica", "WARN")
            self._forma_y_gasto(solicitud)
            self._elegir_cuenta_existente(solicitud)
            return

        # No está en el catálogo: hay que darlo de alta.
        if not solicitud.beneficiario_nuevo:
            s.anotar("beneficiario",
                     "No está en el catálogo de SIPP: se dará de alta", "WARN")
        if not solicitud.beneficiario_rfc.strip():
            raise RequiereRevision(
                f"«{nombre}» no está en el catálogo de SIPP y hay que darlo de "
                f"alta, pero la solicitud no trae RFC. Captúralo y reintenta.")
        # Sin carátula no tiene caso seguir: SIPP no acepta el alta de la cuenta
        # bancaria, y el beneficiario quedaría a medio registrar. Se detiene
        # ANTES de tocar el formulario en vez de dejar basura.
        if (solicitud.forma_pago == catalogos.FORMA_PAGO_CON_CUENTA
                and not archivos.get("caratula")):
            raise RequiereRevision(
                f"«{nombre}» se va a dar de alta con cuenta bancaria, pero no "
                f"tiene carátula adjunta. SIPP la exige para registrar la "
                f"cuenta.")
        self._alta_beneficiario(solicitud, panel, archivos)

    def _elegir_cuenta_existente(self, solicitud: Solicitud) -> None:
        """Elige la cuenta bancaria de un beneficiario que ya está en SIPP."""
        s = self.s
        if solicitud.forma_pago != catalogos.FORMA_PAGO_CON_CUENTA:
            return                      # sin transferencia no hay cuenta
        if solicitud.cuenta_clabe:
            # Las cuentas se listan como 'CLABE - NOMBRE': se empareja por CLABE.
            s.seleccionar_chosen("ben.cuentas", solicitud.cuenta_clabe,
                                 "Cuenta bancaria")
            return

        # Sin CLABE en la solicitud, solo se puede elegir si no hay ambigüedad.
        try:
            opciones = [o.strip() for o in
                        s.loc("ben.cuentas").first.locator(
                            "option").all_inner_texts()
                        if o.strip() and o.strip() != "Seleccionar"]
        except Exception:  # noqa: BLE001
            opciones = []
        if len(opciones) == 1:
            s.seleccionar_chosen("ben.cuentas", opciones[0], "Cuenta bancaria")
            s.anotar("cuenta", f"Única cuenta del beneficiario: {opciones[0]}")
            return
        raise RequiereRevision(
            f"«{solicitud.beneficiario_nombre}» ya está en SIPP con "
            f"{len(opciones)} cuenta(s) bancaria(s) y la solicitud no trae "
            f"CLABE. Indica a cuál se le paga.")

    def _alta_beneficiario(self, solicitud: Solicitud, panel,
                           archivos: dict) -> None:
        """Registra un beneficiario nuevo (checkbox «No Registrado»)."""
        s = self.s
        s.anotar("beneficiario", f"Alta de «{solicitud.beneficiario_nombre}»")
        chk = s.loc("ben.no_registrado").first
        if not chk.is_checked():
            # El input está TAPADO por su <label> estilizado: clic en la etiqueta.
            s.loc("ben.no_registrado_label").first.click()
            s.page.wait_for_timeout(500)

        s.llenar("ben.razon_social", solicitud.beneficiario_nombre,
                 "Descripción del beneficiario", dentro=panel)
        if solicitud.beneficiario_rfc:
            # ESTRICTO: el RFC identifica fiscalmente al beneficiario y SIPP lo
            # corta a 13 caracteres. Uno incompleto da de alta a otra persona.
            s.llenar("ben.rfc", solicitud.beneficiario_rfc, "RFC", dentro=panel,
                     estricto=True)
            self._comprobar_rfc_libre(solicitud)
        # El correo NO existe en el panel de Proveedor (solo en Deudor y
        # Acreedor), así que se comprueba antes de escribir en vez de darlo por
        # hecho: sin esto, un alta de Proveedor moría aquí.
        if solicitud.beneficiario_correo and s.existe("ben.correo", panel):
            s.llenar("ben.correo", solicitud.beneficiario_correo, "Correo",
                     dentro=panel)
        elif solicitud.beneficiario_correo:
            s.anotar("beneficiario",
                     "El panel de este tipo de beneficiario no tiene campo de "
                     "correo; se omite", "WARN")

        self._forma_y_gasto(solicitud)
        if solicitud.forma_pago == catalogos.FORMA_PAGO_CON_CUENTA:
            self._alta_cuenta_bancaria(solicitud, panel, archivos)

    def _comprobar_rfc_libre(self, solicitud: Solicitud) -> None:
        """Aborta si el RFC que se va a dar de alta ya existe en SIPP.

        El catálogo se busca por NOMBRE, pero SIPP identifica al beneficiario
        por su RFC. Cuando el nombre del Excel no coincide letra por letra con
        el registrado —«S.A. DE C.V.» contra «SA DE CV», un acento, una
        abreviatura— la búsqueda no lo encuentra y el robot intenta darlo de
        alta otra vez. SIPP no lo impide mientras se llena: deja terminar el
        formulario entero y lo rechaza al guardar, con un mensaje pegado al
        campo del RFC y sin más explicación.

        Se pregunta aquí, en cuanto el RFC está escrito, para fallar con la
        causa a la mano en vez de al final y a ciegas.
        """
        s = self.s
        # El portal resuelve la comprobación contra su servidor: no está la
        # respuesta en el instante en que se termina de escribir.
        if not s.esperar_a(lambda: s.consultar("rfc_ya_registrado", False),
                           tope_ms=6000, intervalo_ms=300):
            return
        raise RequiereRevision(
            f"El RFC {solicitud.beneficiario_rfc} ya está registrado en SIPP, "
            f"pero «{solicitud.beneficiario_nombre}» no aparece en el catálogo "
            f"con ese nombre. Búscalo en SIPP para ver con qué nombre está dado "
            f"de alta y corrígelo en la solicitud; si se da de alta otra vez, "
            f"SIPP rechaza el guardado.")

    def _buscar_beneficiario(self, nombre: str) -> str:
        """'nuevo' | 'existente' (lo selecciona) | 'ambiguo'."""
        s = self.s
        s.cerrar_alertas()
        s.loc("ben.buscar").first.click()
        modal = s.loc("ben.modal_busqueda").first
        modal.wait_for(state="visible", timeout=s.timeout_ms)
        modal.locator(selectores.css("ben.modal_nombre")).fill(nombre)
        modal.locator(selectores.css("ben.modal_buscar")).click()
        s.page.wait_for_timeout(2500)
        filas = modal.locator(selectores.css("ben.modal_filas"))
        n = filas.count()
        s.anotar("beneficiario", f"{n} coincidencia(s) para «{nombre}»")
        if n != 1:
            try:
                modal.locator(selectores.css("ben.modal_cerrar")).click()
                s.page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
            return "nuevo" if n == 0 else "ambiguo"
        filas.first.dblclick()          # así se elige en ng-grid
        s.page.wait_for_timeout(2000)
        s.cerrar_alertas()

        # Se COMPRUEBA que quedó seleccionado. Un doble clic que no prende deja
        # el formulario sin beneficiario, y como los demás pasos sí funcionan,
        # la solicitud llegaría hasta Guardar apuntando a nadie —o peor, al
        # beneficiario que hubiera quedado de la solicitud anterior.
        elegido = self._beneficiario_en_pantalla()
        if not elegido:
            s.diagnostico("beneficiario_no_seleccionado")
            raise ErrorRpa(
                f"Se eligió «{nombre}» en el buscador pero el formulario no "
                f"quedó con ningún beneficiario.")
        if not _palabras_clave(nombre).issubset(_palabras_clave(elegido)):
            s.diagnostico("beneficiario_distinto")
            raise ErrorRpa(
                f"Se buscaba «{nombre}» y el formulario quedó con «{elegido}». "
                f"No se continúa para no capturar el pago a otra persona.")
        s.anotar("beneficiario", f"Seleccionado: «{elegido}»")
        return "existente"

    def _beneficiario_en_pantalla(self) -> str:
        """Nombre del beneficiario que el formulario tiene ahora mismo."""
        try:
            campo = self.s.loc("form.beneficiario_nombre")
            for i in range(campo.count()):
                valor = (campo.nth(i).input_value() or "").strip()
                if valor:
                    return valor
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _forma_y_gasto(self, solicitud: Solicitud) -> None:
        if solicitud.forma_pago:
            self.s.seleccionar_chosen("sol.forma_pago", solicitud.forma_pago,
                                      "Forma de pago")
        if solicitud.tipo_gasto:
            self.s.seleccionar_chosen("sol.tipo_gasto", solicitud.tipo_gasto,
                                      "Tipo de gasto")

    def _alta_cuenta_bancaria(self, solicitud: Solicitud, panel,
                              archivos: dict) -> None:
        """Captura una cuenta bancaria nueva en su modal."""
        s = self.s
        s.anotar("cuenta", "Capturando la cuenta bancaria")
        panel.locator(selectores.css("ben.agregar_cuenta")).first.click()
        modal = s.loc("cb.modal").first
        modal.wait_for(state="visible", timeout=s.timeout_ms)

        if solicitud.cuenta_banco:
            s.seleccionar_chosen("cb.banco", solicitud.cuenta_banco, "Banco")
        s.llenar("cb.nombre_cuenta",
                 solicitud.cuenta_titular or solicitud.beneficiario_nombre,
                 "Nombre de la cuenta")
        if solicitud.moneda:
            s.seleccionar_chosen("cb.moneda", solicitud.moneda, "Moneda")
        # ESTRICTO: una CLABE recortada manda el dinero a otra cuenta. Es el
        # único dato del formulario cuyo error no se puede corregir después.
        s.llenar("cb.clabe", solicitud.cuenta_clabe, "CLABE", estricto=True)
        s.seleccionar_chosen("cb.tipo_transf",
                             solicitud.cuenta_tipo_transf or "SPEI",
                             "Tipo de transferencia")
        correo = solicitud.beneficiario_correo
        if correo and s.visible_("cb.correo"):
            s.llenar("cb.correo", correo, "Correo de la cuenta")
        if archivos.get("caratula"):
            s.subir_archivo("cb.caratula", archivos["caratula"],
                            "Carátula (cuenta)")

        # Hay que CERRAR el modal siempre: su «Guardar» es un closeModal que solo
        # pasa los datos al formulario padre. Dejarlo abierto impide registrar la
        # moneda y los conceptos.
        s.loc("cb.guardar").first.click()
        cerrado = False
        for _ in range(60):                     # hasta ~30s (la subida tarda)
            if not modal.is_visible():
                cerrado = True
                break
            if s.hay_error_sistema():
                s.anotar("cuenta", "Error de almacenamiento al subir la carátula",
                         "WARN")
                s.cerrar_alertas()
                break
            s.page.wait_for_timeout(500)
        if not cerrado:
            s.cerrar_alertas()

        # Se COMPRUEBA que la cuenta quedó en el modelo. El «Guardar» del modal
        # es un closeModal: si una validación lo rechaza, el modal se cierra
        # igual y el formulario sigue sin cuenta. La solicitud se guardaría sin
        # forma de pagarla, y eso solo se descubre al querer dispersar.
        registrada = self._clabe_registrada()
        if not registrada:
            s.diagnostico("cuenta_no_registrada")
            raise ErrorRpa(
                "La cuenta bancaria no quedó registrada en el formulario. "
                "Revisa que el banco, la CLABE y la carátula sean válidos para "
                "SIPP.")
        esperada = re.sub(r"\D", "", solicitud.cuenta_clabe)
        if esperada and re.sub(r"\D", "", registrada) != esperada:
            s.diagnostico("clabe_distinta")
            raise ErrorRpa(
                f"La cuenta quedó registrada con la CLABE «{registrada}» y se "
                f"capturó «{solicitud.cuenta_clabe}».")
        s.anotar("cuenta", f"Cuenta registrada con CLABE {registrada}")

    def _clabe_registrada(self) -> str:
        """CLABE que el formulario tiene tras cerrar el modal de cuenta.

        Se lee del modelo de Angular: al dar de alta un beneficiario nuevo la
        cuenta vive solo en el modelo y no se pinta en ningún campo de la
        pantalla.
        """
        try:
            return self.s.page.evaluate(
                sipp_datos.consulta("clabe_registrada")) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _llenar_insumos(self, insumos: list[Partida]) -> None:
        """Captura el grid de Insumos y Servicios.

        Es el tramo que el RPA anterior nunca automatizó (su caso de uso no lo
        necesitaba), así que aquí hay más suposición que en el resto: cada
        renglón se agrega con `agregarInsumo()` y se completa por sus modales de
        ayuda. Confírmalo contra stage antes de darlo por bueno.
        """
        s = self.s
        s.abrir_pestana("insumos")
        primera = insumos[0]
        # `existe`, no `visible_`: «Tipo de compra» es un select "chosen", y el
        # <select> nativo SIEMPRE está oculto tras el widget. Preguntar por su
        # visibilidad da False incluso cuando el campo está ahí, y el tipo de
        # compra nunca se llegaba a poner.
        if primera.tipo_compra and s.existe("ins.tipo_compra"):
            s.seleccionar_chosen("ins.tipo_compra", primera.tipo_compra,
                                 "Tipo de compra")
        grid = s.page.locator(
            f"{selectores.css('ins.grid')} {selectores.css('grid.filas')}")
        for i, insumo in enumerate(insumos, 1):
            antes = grid.count()
            s.loc("ins.agregar").first.click()
            s.page.wait_for_timeout(1000)
            modal = s.loc("ins.modal").first
            if modal.count() > 0 and modal.is_visible():
                if insumo.insumo_id:
                    s.llenar("ins.modal_folio", insumo.insumo_id,
                             f"Folio insumo {i}")
                elif insumo.insumo_nombre:
                    s.llenar("ins.modal_nombre", insumo.insumo_nombre,
                             f"Insumo {i}")
                s.loc("ins.modal_buscar").first.click()
                s.page.wait_for_timeout(1500)
                filas = modal.locator(selectores.css("grid.filas"))
                if filas.count() == 0:
                    raise RequiereRevision(
                        f"El insumo «{insumo.insumo_nombre or insumo.insumo_id}» "
                        f"no existe en el catálogo de SIPP.")
                filas.first.dblclick()
                s.page.wait_for_timeout(1200)
            s.cerrar_alertas()
            # Se comprueba renglón por renglón: si el grid no creció, el insumo
            # no entró y seguir agregando solo acumularía silencio.
            if grid.count() <= antes:
                s.diagnostico(f"insumo_{i}_no_agregado")
                raise ErrorRpa(
                    f"El insumo «{insumo.insumo_nombre or insumo.insumo_id}» no "
                    f"se agregó al grid: quedó con {grid.count()} renglón(es).")
        s.anotar("insumos", f"{grid.count()} renglón(es) en el grid")
        # Aviso, no error: a diferencia de los conceptos, no está confirmado
        # cómo deriva SIPP el total desde este grid. Se deja constancia y que lo
        # juzgue quien revise; con la parada por defecto («Llenar y esperar»)
        # una persona ve el formulario antes de guardar.
        try:
            campo = s.loc("sol.cantidad_pagar").first
            actual = campo.input_value() if campo.count() > 0 else ""
            esperado = round(sum(p.importe for p in insumos), 2)
            s.anotar("insumos", f"Cantidad a pagar: {actual or '(vacío)'} "
                                f"(suma de insumos {esperado:,.2f})",
                     "WARN" if self._es_cero(actual) else "INFO")
        except Exception:  # noqa: BLE001 — informativo
            pass

    def _llenar_conceptos(self, conceptos: list[Partida]) -> None:
        """Captura los importes en el grid de Conceptos de Pago.

        Aquí está el detalle que decide si la solicitud se guarda con importe o
        en $0: además de teclear el importe hay que **seleccionar** el renglón
        con su celda de selección, porque SIPP suma solo los conceptos
        seleccionados.
        """
        s = self.s
        s.abrir_pestana("conceptos")
        grid = s.loc("con.filas")
        for _ in range(24):                     # hasta ~12s a que cargue
            if grid.count() > 0:
                break
            s.page.wait_for_timeout(500)
        if grid.count() == 0:
            raise RequiereRevision(
                "La empresa no tiene conceptos de pago asignados: el grid está "
                "vacío y la solicitud se guardaría en $0.")

        for concepto in conceptos:
            fila = self._fila_concepto(concepto.concepto_nombre)
            if fila is None:
                raise RequiereRevision(
                    f"La empresa no tiene un concepto que coincida con "
                    f"«{concepto.concepto_nombre}» ({grid.count()} disponibles).")
            fila.scroll_into_view_if_needed()
            campo = fila.locator(selectores.css("con.importe")).first
            # Se TECLEA: la directiva de moneda de SIPP necesita pulsaciones
            # reales; con fill() el modelo no confirma el importe.
            campo.click()
            try:
                campo.press("Control+a")
            except Exception:  # noqa: BLE001
                pass
            campo.type(f"{concepto.importe:.2f}", delay=30)
            campo.press("Tab")                  # el blur confirma el importe
            s.page.wait_for_timeout(400)

            # Seleccionar el renglón. OJO: hay que clicar la CELDA DE SELECCIÓN;
            # clicar el nombre o cualquier otra celda lo DESELECCIONA en ng-grid
            # y el total se va a cero.
            ya = False
            try:
                ya = bool(campo.evaluate(
                    sipp_datos.consulta("concepto_seleccionado")))
            except Exception:  # noqa: BLE001
                pass
            if not ya:
                fila.locator(selectores.css("con.seleccion")).first.click()
                s.page.wait_for_timeout(500)

        self._blindar_total(conceptos)

    def _fila_concepto(self, concepto: str):
        """Renglón cuyo texto contiene TODAS las palabras del concepto, sin
        distinguir acentos ni mayúsculas. Así «PAGO PTU» encuentra también
        «PAGO DE PTU», que es como algunas empresas lo tienen dado de alta."""
        def normalizar(t: str) -> str:
            t = unicodedata.normalize("NFKD", (t or "").upper())
            return "".join(c for c in t if not unicodedata.combining(c))

        palabras = [normalizar(w) for w in concepto.split() if w.strip()]
        if not palabras:
            return None
        grid = self.s.loc("con.filas")
        for i in range(grid.count()):
            fila = grid.nth(i)
            try:
                texto = normalizar(
                    fila.locator(selectores.css("con.nombre")).first.inner_text())
            except Exception:  # noqa: BLE001
                continue
            if all(w in texto for w in palabras):
                return fila
        return None

    def _blindar_total(self, conceptos: list[Partida]) -> None:
        """Verifica «Cantidad a Pagar» y, si quedó en cero, la fuerza.

        Existe porque pasó: por temporización, el recálculo de SIPP a veces no
        se dispara y la solicitud se guardaría en $0. Una solicitud en cero es
        peor que un error: se ve capturada y hay que cancelarla a mano.
        """
        s = self.s
        esperado = round(sum(c.importe for c in conceptos), 2)
        try:
            campo = s.loc("sol.cantidad_pagar").first
            actual = campo.input_value() if campo.count() > 0 else ""
            if self._es_cero(actual):
                s.anotar("conceptos", "El total quedó en cero: forzando el "
                                      "recálculo", "WARN")
                importe = s.loc("con.importe").first
                importe.evaluate(
                    sipp_datos.consulta("forzar_total_conceptos"))
                s.page.wait_for_timeout(300)
                actual = campo.input_value()
            s.anotar("conceptos", f"Cantidad a pagar: {actual or '(vacío)'} "
                                  f"(esperado {esperado:,.2f})")
            if self._es_cero(actual):
                raise ErrorRpa(
                    "«Cantidad a Pagar» quedó en cero pese al recálculo: no se "
                    "guarda la solicitud para no dejar un registro en $0.")
        except ErrorRpa:
            raise
        except Exception:  # noqa: BLE001 — la verificación no debe tumbar el paso
            pass

    @staticmethod
    def _es_cero(total: str | None) -> bool:
        limpio = (total or "0").replace("$", "").replace(",", "").strip()
        return limpio in ("", "0", "0.00", ".00")

    # ------------------------------------------------------------ guardado
    def guardar(self, solicitud: "Solicitud | None" = None) -> str:
        """Guarda la solicitud y devuelve su folio (vacío si no logró leerlo).

        `solicitud` se usa como ÚLTIMO recurso: si las dos señales de pantalla
        fallan, se va al listado a comprobar si de verdad se guardó. Un falso
        negativo aquí es caro —la solicitud queda en ERROR con su folio ya
        consumido en SIPP— y nadie la vuelve a intentar.
        """
        s = self.s
        s.anotar("guardar", "Guardando la solicitud")
        s.espiar_validaciones()
        s.validaciones()        # lo que SIPP objetó al llenar ya está resuelto
        s.clic_con_reintento("acc.guardar", "Guardar")

        # SIPP contesta de una de tres formas: pide confirmación, guarda de
        # golpe, o RECHAZA el formulario con un mensaje pegado a un campo y se
        # detiene ahí —sin alerta, sin diálogo y sin llamar a su servidor—. Las
        # tres se esperan a la vez porque son excluyentes: quedarse esperando el
        # diálogo cuando ya hubo rechazo era gastar quince segundos para acabar
        # diciendo «no confirmó», que es justo lo que no explica nada.
        objetadas: list[str] = []

        def _contesto() -> bool:
            for m in s.validaciones():
                if m not in objetadas:
                    objetadas.append(m)
            return bool(objetadas or s.visible_("acc.confirmar")
                        or self._leer_folio() or s.visible_("acc.autorizar"))

        s.esperar_a(_contesto, tope_ms=15000, intervalo_ms=300)
        if objetadas:
            # Es un dato que no cuadra, no una falla del robot: reintentarlo
            # daría exactamente el mismo rechazo.
            s.captura("guardar_rechazado")
            motivo = " ".join(objetadas)
            s.anotar("guardar", f"SIPP rechazó el formulario: {motivo}", "WARN")
            raise RequiereRevision(
                f"SIPP no aceptó la solicitud: {motivo}")

        # El diálogo de confirmación: si aparece y NO se acepta, SIPP no guarda
        # nada y después no hay forma de saber por qué. Se deja constancia de
        # cuál de los dos caminos se tomó. La espera es corta porque la de
        # arriba ya le dio a SIPP su tiempo de responder.
        confirmado = False
        try:
            aceptar = s.loc("acc.confirmar").first
            aceptar.wait_for(state="visible", timeout=3000)
            aceptar.click()
            confirmado = True
        except Cancelado:
            raise
        except Exception:  # noqa: BLE001 — no siempre pide confirmación
            pass
        s.anotar("guardar", "Confirmación aceptada" if confirmado
                 else "SIPP no pidió confirmación")

        # Lo que SIPP conteste se lee ANTES de cerrar nada: sus alertas llevan
        # el motivo del rechazo —un campo que falta, un importe que no cuadra—
        # y cerrarlas primero deja el fallo sin explicación. Es exactamente lo
        # que hacía que este error dijera solo «no confirmó».
        aviso = " ".join((s.texto_alertas() or "").split())
        if aviso:
            s.anotar("guardar", f"SIPP respondió: {aviso[:300]}", "WARN")
        # Los avisos que SIPP encadena tras confirmar tapan el formulario y
        # esconden tanto el folio como el botón de autorizar.
        s.cerrar_alertas()
        # El guardado es una petición al servidor: se espera a que la red se
        # calme en vez de contar cuatro segundos. Con el portal cargado tardaba
        # más que eso y la solicitud se daba por no guardada aunque SÍ lo
        # estuviera —quedaba en ERROR con su folio ya consumido en SIPP—.
        s.esperar_quieto(15000)
        if s.hay_error_sistema():
            s.captura("error_guardar")
            raise ErrorRpa("SIPP reportó un error del sistema al guardar.")

        # Se COMPRUEBA que el guardado ocurrió, no se da por hecho. Pulsar el
        # botón sin que SIPP acepte —por una validación del portal, por ejemplo—
        # dejaba la solicitud marcada como GUARDADA en la base sin existir en el
        # ERP: el peor de los desenlaces, porque nadie la vuelve a intentar.
        #
        # La evidencia es que aparezca el botón «Solicitar Autorización», que
        # SIPP solo muestra cuando la solicitud ya tiene folio. Se SONDEA hasta
        # 20 s en vez de mirar una sola vez: la pantalla se repinta después de
        # responder, y mirar demasiado pronto es no ver nada.
        s.esperar_a(lambda: bool(self._leer_folio())
                    or s.visible_("acc.autorizar"), tope_ms=20000,
                    intervalo_ms=400)
        folio = self._leer_folio()
        guardada = folio or s.visible_("acc.autorizar")
        if not guardada and solicitud is not None:
            # Las dos señales viven en esta pantalla y las dos pueden fallar:
            # el folio si el modelo de Angular cambió de forma, y el botón de
            # autorizar si SIPP lo condiciona a algo más. El listado es la
            # fuente de verdad, así que antes de dar el guardado por fallido se
            # comprueba ahí. Cuesta una navegación, pero solo se paga cuando ya
            # íbamos a fallar.
            s.anotar("guardar",
                     "No se vio folio en pantalla; se comprueba en el listado",
                     "WARN")
            try:
                folio = self.buscar_existente(solicitud)
            except Exception:  # noqa: BLE001 — el listado tampoco responde
                folio = ""
            if folio:
                s.anotar("guardar",
                         f"Estaba guardada: aparece en el listado con folio "
                         f"{folio}")
                return folio
        if not guardada:
            ruta = s.captura("guardar_sin_efecto")
            # El motivo real casi siempre está en la alerta que SIPP mostró o
            # en que el botón Guardar siga ahí (señal de que no llegó a
            # procesar). Sin esto, el mensaje obligaba a abrir la captura para
            # empezar a averiguar algo.
            pistas = []
            for m in s.validaciones():
                if m not in objetadas:
                    objetadas.append(m)
            if objetadas:
                pistas.append("SIPP objetó: «" + " ".join(objetadas)[:300] + "»")
            if aviso:
                pistas.append(f"SIPP respondió: «{aviso[:300]}»")
            if not confirmado:
                pistas.append("no apareció el diálogo de confirmación")
            if s.visible_("acc.guardar"):
                pistas.append("el botón Guardar sigue en pantalla, así que la "
                              "solicitud no llegó a procesarse")
            detalle = (" " + ". ".join(pistas) + ".") if pistas else ""
            raise ErrorRpa(
                "Se pulsó Guardar pero SIPP no confirmó la solicitud: no hay "
                f"folio ni aparece «Solicitar Autorización».{detalle} Revisa la "
                f"captura ({os.path.basename(ruta) or 'sin captura'}).")
        if not folio:
            s.anotar("guardar",
                     "Guardada, pero no se pudo leer el folio: la bitácora "
                     "queda sin ese rastro", "WARN")
        return folio

    def _leer_folio(self) -> str:
        """Lee el folio en cuanto aparece en pantalla.

        Se persiste de inmediato, no al final del flujo: si el proceso muere
        entre el guardado y la autorización, el folio es lo único que evita
        capturar el mismo pago otra vez.
        """
        # Se lee del MODELO de Angular, no del DOM: tras guardar, SIPP deja el
        # folio en `solicitudPago.ID_SOLICITUDPAGO` pero no lo pinta en ningún
        # campo de la pantalla, así que buscar un <input> no encontraba nada
        # (verificado en stage el 01/08/2026).
        try:
            folio = self.s.page.evaluate(
                sipp_datos.consulta("folio_solicitud"))
            if folio:
                return folio.strip()
        except Exception:  # noqa: BLE001 — se intenta el respaldo por texto
            pass
        try:
            texto = self.s.page.locator("body").inner_text() or ""
            m = re.search(r"Solicitud\s*(?:de\s*Pago)?\s*[#:]?\s*(\d{3,})",
                          texto, re.IGNORECASE)
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    def adjuntar_respaldo(self, ruta: str) -> None:
        """Adjunta un documento de respaldo (el Vo.Bo. de Compras) tras guardar.

        SIPP lo exige para Pago Extraordinario, y solo se puede después de
        guardar: antes, la pestaña no acepta archivos.
        """
        if not ruta or not os.path.isfile(ruta):
            return
        s = self.s
        s.cerrar_alertas()
        s.abrir_pestana("documentos")
        # Se cuenta ANTES: si el «+» no agrega renglón, `.last` apuntaría a un
        # campo de otra fila —o a ninguno— y el archivo se subiría al lugar
        # equivocado, o no se subiría, sin que nada lo delate.
        antes = s.page.locator(selectores.css("doc.archivo")).count()
        s.loc("doc.agregar").first.click()
        # El renglón aparece en cuanto el grid se redibuja: se espera A ESO y no
        # un segundo entero. Si no aparece, se agota el mismo tope de antes y el
        # error que sigue es idéntico.
        s.esperar_a(
            lambda: s.page.locator(selectores.css("doc.archivo")).count() > antes,
            tope_ms=1000)
        despues = s.page.locator(selectores.css("doc.archivo")).count()
        if despues <= antes:
            s.diagnostico("respaldo_sin_renglon")
            raise ErrorRpa(
                "No se pudo agregar el renglón de documento de respaldo: la "
                "pestaña no respondió al botón de agregar.")

        nombres = s.page.locator(selectores.css("doc.nombre"))

        def _registrados() -> list[str]:
            """Documentos que SIPP ya dio por subidos, por su nombre."""
            puestos = []
            try:
                for i in range(nombres.count()):
                    valor = (nombres.nth(i).input_value() or "").strip()
                    if valor:
                        puestos.append(valor)
            except Exception:  # noqa: BLE001 — el grid se está repintando
                pass
            return puestos

        ya_estaban = len(_registrados())
        entrada = s.page.locator(selectores.css("doc.archivo")).last
        s.subir_archivo(entrada, ruta, "Vo.Bo.")
        s.cerrar_alertas()

        # Elegir el archivo NO es haberlo subido. SIPP lo manda a su
        # almacenamiento y solo al terminar le pone nombre al renglón; ese
        # nombre es justo lo que mira para dejar enviar a autorizar. Antes se
        # comprobaba el selector de archivo —que se llena al instante, en el
        # navegador— y se seguía de largo con la subida a medias: la solicitud
        # quedaba guardada y SIPP contestaba que faltaba el documento de
        # respaldo, sin relación aparente con el adjunto que sí se había
        # elegido (verificado en stage el 24/08/2026).
        s.esperar_a(lambda: len(_registrados()) > ya_estaban,
                    tope_ms=60000, intervalo_ms=500)
        puestos = _registrados()
        if len(puestos) <= ya_estaban:
            s.diagnostico("respaldo_sin_archivo")
            raise ErrorRpa(
                "SIPP no terminó de registrar el Vo.Bo.: el renglón de "
                "documento de respaldo sigue sin nombre. Sin él no deja enviar "
                "la solicitud a autorizar.")
        s.anotar("respaldo", f"Vo.Bo. adjuntado: {puestos[-1]}")

    def solicitar_autorizacion(self) -> None:
        """Envía la solicitud a autorización y COMPRUEBA que se haya enviado.

        SIPP exige el Vo.Bo. de Compras como documento de respaldo en Pago
        Extraordinario. Sin él, pulsar el botón no hace nada: solo muestra un
        aviso. Antes se cerraban las alertas y se daba el paso por bueno, de
        modo que una solicitud se quedaba en BORRADOR mientras la herramienta
        la reportaba como enviada (verificado en stage el 01/08/2026).

        Por eso el texto del aviso se LEE antes de cerrarlo, y el éxito se
        confirma viendo que el botón desaparezca.
        """
        s = self.s
        s.cerrar_alertas()
        s.espiar_validaciones()
        s.validaciones()        # lo anterior ya se resolvió al guardar
        s.clic_con_reintento("acc.autorizar", "Solicitar autorización")
        s.page.wait_for_timeout(1500)
        try:
            aceptar = s.loc("acc.confirmar").first
            if aceptar.count() > 0 and aceptar.is_visible():
                aceptar.click()
        except Exception:  # noqa: BLE001
            pass
        s.page.wait_for_timeout(3000)

        # El motivo del rechazo va en la alerta: hay que leerla ANTES de
        # cerrarla, o se pierde justo la información que explica el fallo.
        aviso = " ".join((s.texto_alertas() or "").split())
        s.cerrar_alertas()
        s.page.wait_for_timeout(1000)

        if s.visible_("acc.autorizar"):
            ruta = s.captura("autorizar_sin_efecto")
            # SIPP también rechaza aquí en silencio, con el aviso pegado al
            # campo en vez de en una alerta.
            objetadas = s.validaciones()
            if objetadas and not aviso:
                aviso = " ".join(objetadas)
            detalle = f" SIPP dijo: «{aviso[:200]}»." if aviso else ""
            raise ErrorRpa(
                "Se pulsó «Solicitar Autorización» pero la solicitud sigue sin "
                f"enviarse.{detalle} La causa más común es que falte el Vo.Bo. "
                f"de Compras, que SIPP exige como documento de respaldo.")
        s.anotar("autorizar", "Autorización solicitada")

    # ------------------------------------------------------------ completo
    def capturar(self, solicitud: Solicitud, partidas: list[Partida],
                 archivos: dict[str, str] | None = None) -> ResultadoCaptura:
        """Captura una solicitud hasta su punto de parada.

        `solicitud.parada` se lee AQUÍ, al procesar la fila, no al iniciar el
        lote: así el usuario puede cambiar de opinión con el proceso corriendo.
        """
        parada = solicitud.parada or catalogos.PARADA_DEFECTO
        archivos = archivos or {}

        folio_previo = self.buscar_existente(solicitud)
        if folio_previo:
            return ResultadoCaptura(
                "GUARDADA", folio_previo,
                "Ya estaba capturada en SIPP; no se duplicó.")

        self.llenar(solicitud, partidas, archivos)
        # Último punto donde abandonar no deja rastro en SIPP: pasado el
        # Guardar ya hay folio, y cortar ahí dejaría una solicitud a medias.
        self.abortar_si_cancelan()
        if parada == "LLENADA":
            return ResultadoCaptura(
                "LLENADA", "",
                "Formulario lleno y sin guardar, listo para tu revisión.",
                [self.s.captura("llenada")])

        return self.cerrar_solicitud(archivos, hasta=parada,
                                     solicitud=solicitud)

    def cerrar_solicitud(self, archivos: dict[str, str] | None = None, *,
                         hasta: str = "AUTORIZAR",
                         folio_existente: str = "",
                         solicitud: "Solicitud | None" = None) -> ResultadoCaptura:
        """Guarda el formulario que ya está en pantalla y, si toca, lo autoriza.

        Va aparte de `capturar` porque se llega aquí por dos caminos: el normal
        —una parada que pide guardar— y el de «Llenar y esperar», donde el
        formulario se dejó lleno, alguien lo revisó y pidió continuar. En los
        dos casos falta exactamente lo mismo, y duplicarlo dejaría que uno de
        los dos se quedara sin el adjunto de respaldo o sin autorizar.
        """
        archivos = archivos or {}
        # `folio_existente` llega cuando quien revisó ya pulsó Guardar en SIPP:
        # volver a guardar abriría una segunda solicitud para el mismo pago.
        folio = folio_existente or self.guardar(solicitud)
        if hasta == "GUARDADA":
            return ResultadoCaptura("GUARDADA", folio, "Solicitud guardada.")

        # Sin Vo.Bo. no tiene caso intentar: SIPP lo exige como documento de
        # respaldo y rechaza el envío. Se detiene ANTES de pulsar el botón, con
        # la solicitud ya guardada, para que solo falte adjuntarlo.
        if not archivos.get("vobo"):
            return ResultadoCaptura(
                "GUARDADA", folio,
                "Guardada, pero SIN enviar a autorizar: falta el Vo.Bo. de "
                "Compras, que SIPP exige como documento de respaldo. "
                "Adjúntalo y vuelve a ejecutar el lote.")
        self.adjuntar_respaldo(archivos["vobo"])
        self.solicitar_autorizacion()
        return ResultadoCaptura("ENVIADA_AUTORIZAR", folio,
                                "Solicitud guardada y enviada a autorizar.")


# --------------------------------------------------------------------------- #
#  Verificación previa
# --------------------------------------------------------------------------- #
# Claves que DEBEN resolver en la pantalla de captura. Se listan aparte del mapa
# completo porque muchas otras (login, modales cerrados, grids sin renglones)
# solo aparecen tras una interacción y su ausencia no significa nada.
CLAVES_CRITICAS = [
    "listado.crear", "listado.regresar",
    "sol.empresa", "sol.sucursal", "sol.tipo_pago", "sol.tipo_beneficiario",
    "sol.tipo_gasto", "sol.forma_pago", "sol.fecha_pago", "sol.descripcion",
    "sol.cantidad_pagar", "sol.pdf",
    "ben.no_registrado", "ben.buscar", "ben.cuentas", "ben.razon_social",
    "ben.rfc", "ben.agregar_cuenta",
    "cb.modal", "cb.banco", "cb.clabe", "cb.tipo_transf", "cb.caratula",
    "con.grid", "con.total", "ins.grid", "ins.agregar",
    "acc.guardar", "doc.agregar",
]


def verificar_conexion(usuario: str, contrasena: str, *, url_login: str,
                       visible: bool = False, empresa_sesion: str = "Aske",
                       sucursal_sesion: str = "Corporativo") -> dict:
    """Entra a SIPP, llega a la pantalla de captura y comprueba los selectores.

    No llena ni guarda nada: sirve para detectar que el portal cambió ANTES de
    lanzar un lote, en vez de descubrirlo a media captura. Devuelve
    `{"ok": bool, "faltantes": [...], "revisadas": int, "mensaje": str}`.
    """
    faltantes: list[str] = []
    try:
        with SesionSipp(url_login, visible=visible) as sesion:
            sesion.login(usuario, contrasena)
            sesion.configurar_sesion(empresa_sesion, sucursal_sesion)
            sesion.ir_a_solicitud_pago()
            flujo = FlujoSolicitudPago(sesion)
            flujo.asegurar_modo_agregar()
            sesion.page.wait_for_timeout(1500)
            faltantes = [c for c in CLAVES_CRITICAS if not sesion.existe(c)]
            if faltantes:
                sesion.diagnostico("verificacion")
    except (ErrorRpa, SesionCaida) as exc:
        return {"ok": False, "faltantes": [], "revisadas": 0,
                "mensaje": str(exc)}
    except Exception as exc:  # noqa: BLE001 — se reporta tal cual al usuario
        return {"ok": False, "faltantes": [], "revisadas": 0,
                "mensaje": f"No se pudo completar la verificación: {exc}"}
    if faltantes:
        return {
            "ok": False, "faltantes": faltantes,
            "revisadas": len(CLAVES_CRITICAS),
            "mensaje": (f"{len(faltantes)} de {len(CLAVES_CRITICAS)} elementos "
                        f"ya no se encuentran: SIPP cambió. Se guardó un "
                        f"diagnóstico para ajustar el mapa de selectores."),
        }
    return {"ok": True, "faltantes": [], "revisadas": len(CLAVES_CRITICAS),
            "mensaje": (f"Conexión correcta: los {len(CLAVES_CRITICAS)} "
                        f"elementos del formulario responden.")}


# --------------------------------------------------------------------------- #
#  Motor del lote
# --------------------------------------------------------------------------- #
# Qué hacer tras revisar un formulario que quedó lleno y sin guardar.
CONTINUAR = "continuar"          # pasar a la siguiente solicitud
SALTAR = "saltar"                # marcarla OMITIDA y pasar a la siguiente
NO_PAUSAR = "no_pausar"          # seguir con el resto sin volver a preguntar
DETENER = "detener"              # terminar el lote aquí


def _cerrar_tras_revision(flujo, solicitud, docs, i, total, avisar,
                          resumen, folio_existente: str = "") -> "ResultadoCaptura":
    """Guarda, adjunta el Vo.Bo. y autoriza lo que acaba de revisarse.

    Se ejecuta con el formulario ya lleno en pantalla, así que NO se vuelve a
    llenar: solo falta cerrarlo. Los fallos se tratan como los del bucle —queda
    en ERROR con su motivo y el lote sigue—, porque a estas alturas el
    formulario existe en SIPP y abandonar en silencio dejaría a alguien creyendo
    que ya se autorizó.
    """
    nombre = solicitud.beneficiario_nombre
    try:
        res = flujo.cerrar_solicitud(docs, hasta="AUTORIZAR",
                                     folio_existente=folio_existente,
                                     solicitud=solicitud)
        db.actualizar_estado(solicitud.id, res.estado,
                             folio_sipp=res.folio_sipp, error_msg="")
        db.registrar(solicitud.id, "capturar", res.mensaje)
        avisar(i=i, total=total, nombre=nombre, estado="ok",
               mensaje=res.mensaje)
        return res
    except Exception as exc:  # noqa: BLE001 — se reporta y el lote continúa
        ruta = flujo.s.captura(f"cierre_{i}")
        db.actualizar_estado(solicitud.id, "ERROR", error_msg=str(exc))
        db.registrar(solicitud.id, "capturar", str(exc), "ERROR", ruta)
        resumen["error"] += 1
        resumen["ok"] = max(0, resumen["ok"] - 1)   # ya se contó como buena
        resumen["detalle"].append(f"{nombre}: {exc}")
        avisar(i=i, total=total, nombre=nombre, estado="error",
               mensaje=str(exc))
        return ResultadoCaptura("ERROR", "", str(exc))


def _pausar(en_pausa, flujo, solicitud, i, total, avisar) -> "tuple[str, str]":
    """Detiene el lote para que se revise el formulario.

    Devuelve `(decisión, folio)`. El folio viene lleno solo si quien revisaba le
    dio «Guardar» en SIPP por su cuenta: hay que saberlo para NO volver a
    guardar después —serían dos solicitudes para el mismo pago— y para que la
    base no siga diciendo que está sin guardar.
    """
    avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
           estado="pausado",
           mensaje="Formulario lleno, sin guardar. Revísalo en el navegador.")
    try:
        decision = en_pausa({"i": i, "total": total, "solicitud": solicitud})
    except Exception:  # noqa: BLE001 — si la interfaz falla, no se cuelga el lote
        return CONTINUAR, ""

    try:
        folio = flujo._leer_folio()
    except Exception:  # noqa: BLE001 — leerlo es cortesía, no puede tumbar nada
        folio = ""
    if folio:
        db.actualizar_estado(solicitud.id, "GUARDADA", folio_sipp=folio,
                             error_msg="")
        db.registrar(solicitud.id, "capturar",
                     f"La guardaste tú durante la revisión (folio {folio}).")
    return decision or CONTINUAR, folio


def procesar_lote(lote_id: str, usuario: str, contrasena: str, *,
                  url_login: str, visible: bool = True,
                  empresa_sesion: str = "Aske",
                  sucursal_sesion: str = "Corporativo",
                  documentos_por_solicitud=None,
                  on_progreso=None, detener=None,
                  en_pausa=None, esperar_cierre=None) -> dict:
    """Procesa todas las solicitudes pendientes de un lote.

    Una solicitud a la vez, un contexto de navegador, sin paralelismo: SIPP
    mantiene una sola sesión por usuario y dos capturas simultáneas se pisan.

    Antes de abrir el navegador se validan todas: las que traen datos
    incompletos quedan en REVISAR con el motivo y no se intentan. Es el único
    punto por el que se pasa a fuerza antes del portal, así que es aquí donde
    esa regla se hace cumplir, y no en cada pantalla que edita una solicitud.

    Cada transición se persiste ANTES de ejecutar el paso siguiente, para que un
    corte a media captura deje la base contando la verdad.

    Args:
        en_pausa: se llama tras dejar un formulario LLENO Y SIN GUARDAR, y
            **bloquea** hasta que quien revisa decida. Devuelve una de las
            constantes CONTINUAR / SALTAR / NO_PAUSAR / DETENER. Sin este
            callback el lote no se detiene, que es el comportamiento de siempre.
        esperar_cierre: se llama al terminar, con el navegador todavía abierto,
            y bloquea hasta que se pida cerrarlo.

    **Los dos callbacks bloquean a propósito, y tienen que hacerlo aquí.** La
    API síncrona de Playwright ata el navegador al hilo que lo creó: cerrarlo
    —o tocarlo— desde el hilo de la interfaz revienta. Manteniendo este hilo
    dormido en el callback, todo el trabajo sobre el navegador se queda donde
    debe estar, y la interfaz solo despierta al hilo cuando el usuario decide.
    """
    def avisar(**kw) -> None:
        if callable(on_progreso):
            try:
                on_progreso(kw)
            except Exception:  # noqa: BLE001
                pass

    def cancelado() -> bool:
        return bool(detener and detener())

    pendientes = [s for s in db.listar_solicitudes(lote_id)
                  if s.estado not in ("GUARDADA", "ENVIADA_AUTORIZAR", "OMITIDA")]
    total = len(pendientes)
    resumen = {"ok": 0, "revisar": 0, "error": 0, "cancelado": False,
               "detalle": []}
    if not total:
        return resumen

    def bitacora(sid):
        return lambda paso, msg, nivel, captura: db.registrar(
            sid, paso, msg, nivel, captura)

    # Última puerta antes de tocar SIPP. Una solicitud con datos incompletos no
    # se intenta siquiera: fallaría a media captura y puede dejar el
    # beneficiario a medio dar de alta en el ERP, que después hay que limpiar a
    # mano. Es también lo que permite que la asignación masiva se aplique sin
    # tener que estar completa: la regla se hace cumplir aquí, en el único punto
    # por el que se pasa obligatoriamente antes del portal.
    partidas_de: dict[str, list[Partida]] = {}
    for s in pendientes:
        partidas = db.listar_partidas(s.id)
        errores = [h for h in validador.validar(s, partidas) if h.es_error]
        if not errores:
            partidas_de[s.id] = partidas
            continue
        motivo = validador.resumen(errores)
        db.actualizar_estado(s.id, "REVISAR", error_msg=motivo)
        db.registrar(s.id, "validar",
                     f"No se capturó por datos incompletos:\n{motivo}", "WARN")
        resumen["revisar"] += 1
        resumen["detalle"].append(
            f"{s.beneficiario_nombre}: datos incompletos, no se intentó.")

    # Si no queda ninguna capturable, ni se abre el navegador.
    if not partidas_de:
        return resumen

    pausar = callable(en_pausa)
    ultimo_estado = ""
    with SesionSipp(url_login, visible=visible) as sesion:
        sesion.cancelado = cancelado
        sesion.login(usuario, contrasena)
        sesion.configurar_sesion(empresa_sesion, sucursal_sesion)
        sesion.ir_a_solicitud_pago()
        flujo = FlujoSolicitudPago(sesion, cancelado=cancelado)

        for i, solicitud in enumerate(pendientes, 1):
            if cancelado():
                resumen["cancelado"] = True
                break
            # Ya quedó en REVISAR arriba; se avisa igual para que la barra
            # avance y el usuario vea por qué se saltó.
            if solicitud.id not in partidas_de:
                avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                       estado="revisar",
                       mensaje="Datos incompletos; no se intentó capturar.")
                continue
            sesion._on_bitacora = bitacora(solicitud.id)
            avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                   estado="procesando")
            estado_previo = solicitud.estado
            db.actualizar_estado(solicitud.id, "EN_CAPTURA", sumar_intento=True)
            partidas = partidas_de[solicitud.id]
            # Por defecto, los archivos que el usuario ya asoció a la solicitud.
            # SIPP exige la carátula para dar de alta una cuenta bancaria, así
            # que sin ella el alta del beneficiario queda a medias.
            docs = (documentos_por_solicitud(solicitud)
                    if callable(documentos_por_solicitud)
                    else documentos.de_solicitud(solicitud.id))
            res = None
            try:
                res = flujo.capturar(solicitud, partidas, docs)
                db.actualizar_estado(solicitud.id, res.estado,
                                     folio_sipp=res.folio_sipp, error_msg="")
                db.registrar(solicitud.id, "capturar", res.mensaje)
                resumen["ok"] += 1
                avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                       estado="ok", mensaje=res.mensaje)
                # «Llenar y esperar» significa esperar: el formulario queda en
                # pantalla y la siguiente solicitud lo sobrescribiría, así que
                # la revisión solo puede ocurrir AHORA.
                if res.estado == "LLENADA" and pausar and callable(en_pausa):
                    decision, folio_manual = _pausar(
                        en_pausa, flujo, solicitud, i, total, avisar)
                    if decision == SALTAR:
                        db.actualizar_estado(solicitud.id, "OMITIDA")
                        db.registrar(solicitud.id, "revisar",
                                     "Saltada por quien revisó el formulario.",
                                     "WARN")
                    elif decision in (CONTINUAR, NO_PAUSAR):
                        # Revisado y aprobado: el robot termina el trabajo
                        # —guardar, adjuntar el Vo.Bo. y solicitar la
                        # autorización— en vez de dejar el formulario lleno
                        # para que alguien lo cierre a mano.
                        res = _cerrar_tras_revision(flujo, solicitud, docs, i,
                                                    total, avisar, resumen,
                                                    folio_manual)
                    if decision == DETENER:
                        # Detener NO aprueba: el formulario se queda lleno y
                        # sin guardar, así que esta solicitud no llegó a
                        # capturarse y deja de contar. El último registro
                        # completo es el anterior.
                        if not folio_manual:
                            resumen["ok"] = max(0, resumen["ok"] - 1)
                            db.registrar(
                                solicitud.id, "capturar",
                                "Detenido por ti tras revisar; quedó llena y "
                                "sin guardar.", "WARN")
                        resumen["cancelado"] = True
                        break
                    if decision == NO_PAUSAR:
                        pausar = False
            except Cancelado:
                # Se detuvo a media captura: el formulario queda sin guardar,
                # así que en SIPP no hay folio ni pago. La solicitud vuelve a
                # como estaba para que el próximo lote la tome desde cero, y NO
                # se cuenta: el último registro completo es el anterior a este.
                db.actualizar_estado(solicitud.id, estado_previo, error_msg="")
                db.registrar(solicitud.id, "capturar",
                             "Detenido por ti antes de guardar; no se capturó "
                             "nada en SIPP.", "WARN")
                resumen["cancelado"] = True
                avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                       estado="pausado",
                       mensaje="Detenido: esta solicitud no se capturó.")
                break
            except RequiereRevision as exc:
                db.actualizar_estado(solicitud.id, "REVISAR", error_msg=str(exc))
                db.registrar(solicitud.id, "revisar", str(exc), "WARN")
                resumen["revisar"] += 1
                avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                       estado="revisar", mensaje=str(exc))
            except SesionCaida as exc:
                # No tiene caso seguir: todas fallarían igual.
                db.actualizar_estado(solicitud.id, "ERROR", error_msg=str(exc))
                db.registrar(solicitud.id, "sesion", str(exc), "ERROR")
                resumen["error"] += 1
                resumen["detalle"].append(str(exc))
                avisar(estado="pausado", mensaje=str(exc))
                break
            except Exception as exc:  # noqa: BLE001 — se reporta y se sigue
                ruta = sesion.captura(f"error_{i}")
                db.actualizar_estado(solicitud.id, "ERROR", error_msg=str(exc))
                db.registrar(solicitud.id, "error", str(exc), "ERROR", ruta)
                resumen["error"] += 1
                resumen["detalle"].append(
                    f"{solicitud.beneficiario_nombre}: {exc}")
                avisar(i=i, total=total, nombre=solicitud.beneficiario_nombre,
                       estado="error", mensaje=str(exc))
            ultimo_estado = getattr(res, "estado", "") or ""
            if i < total:
                try:
                    flujo.volver_al_listado()
                except Exception:  # noqa: BLE001 — el siguiente lo reintenta
                    pass

        # El lote terminó pero el navegador sigue en pie: es donde quedaron los
        # formularios llenos y los registros recién capturados, y cerrarlo aquí
        # obligaría a volver a entrar a SIPP para comprobar cualquier cosa. Se
        # espera EN ESTE HILO porque es el único que puede tocar Playwright.
        # Se termina en el listado general y sin filtros, para que se vea el
        # trabajo completo. Salvo con «Llenar y esperar»: ahí el formulario
        # quedó lleno en pantalla a propósito y navegar lo perdería.
        if ultimo_estado != "LLENADA":
            flujo.dejar_listado_limpio()

        if callable(esperar_cierre):
            avisar(estado="navegador_abierto",
                   mensaje="El navegador quedó abierto para que revises lo "
                           "capturado. Ciérralo desde la herramienta cuando "
                           "termines.")
            try:
                esperar_cierre()
            except Exception:  # noqa: BLE001 — pase lo que pase, hay que cerrar
                pass
    return resumen
