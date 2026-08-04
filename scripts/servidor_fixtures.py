"""Sirve las páginas guardadas del SIPP en un servidor local.

Playwright no puede operar un `file://` con la misma fidelidad que una página
servida (rutas relativas, política de mismo origen), así que las copias de
`Paginas html/` se publican en `http://127.0.0.1:<puerto>/`.

Las páginas son **datos reales del portal** y no se versionan (ver .gitignore).
Si no están en tu copia de trabajo, pídelas a quien mantenga el proyecto.

Uso directo (para inspeccionar a mano):
    python scripts/servidor_fixtures.py
Uso desde una prueba:
    with servidor_fixtures.servir() as base:
        page.goto(f"{base}/{servidor_fixtures.AGREGAR}")
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import threading
import urllib.parse

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA = os.path.join(_RAIZ, "Paginas html")

# Nombres tal como están guardados (con espacios: hay que codificarlos en la URL).
LOGIN = "Ingreso a sipp.html"
EMPRESA = "seleccion empresa.html"
AGREGAR = "solicitud de pago agregar.html"
LISTADO = "Solicitud de pago listado.html"


class _Silencioso(http.server.SimpleHTTPRequestHandler):
    """Sin log por petición: las páginas piden decenas de assets que no existen
    y el ruido tapa la salida de la prueba."""

    def log_message(self, *_args) -> None:
        pass


def url(base: str, pagina: str) -> str:
    """URL completa de una página, con los espacios codificados."""
    return f"{base}/{urllib.parse.quote(pagina)}"


def hay_fixtures() -> bool:
    return os.path.isdir(CARPETA) and any(
        f.lower().endswith(".html") for f in os.listdir(CARPETA))


@contextlib.contextmanager
def servir(puerto: int = 0):
    """Levanta el servidor en un hilo y devuelve su URL base.

    `puerto=0` deja que el sistema elija uno libre: así dos corridas simultáneas
    no chocan.
    """
    if not hay_fixtures():
        raise FileNotFoundError(
            f"No hay páginas de referencia en «{CARPETA}». Son datos reales del "
            f"portal y no se versionan; consíguelas antes de correr la prueba.")
    manejador = functools.partial(_Silencioso, directory=CARPETA)
    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", puerto), manejador)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    try:
        yield f"http://127.0.0.1:{servidor.server_address[1]}"
    finally:
        servidor.shutdown()
        servidor.server_close()


if __name__ == "__main__":
    with servir(8765) as base:
        print(f"Sirviendo «{CARPETA}» en {base}")
        for pagina in (LOGIN, EMPRESA, AGREGAR, LISTADO):
            print(f"  {url(base, pagina)}")
        print("\nCtrl+C para detener.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nDetenido.")
