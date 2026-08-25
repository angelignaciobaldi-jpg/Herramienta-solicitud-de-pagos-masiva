"""Ayudantes compartidos por las pruebas: base aislada y dobles de la interfaz."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, timedelta

from core import db
from core.db import CONCEPTO, INSUMO, Lote, Partida, Solicitud


def correr(corrutina):
    """Ejecuta hasta el final una corrutina de la interfaz.

    Varias acciones de pantalla son `async` porque hacen trabajo pesado en otro
    hilo (leer carátulas, recorrer una carpeta). Llamarlas sin esto devuelve una
    corrutina que nunca corre, y la prueba pasaría afirmando sobre el estado
    ANTERIOR a la acción —que es peor que fallar—.
    """
    return asyncio.run(corrutina)


def base_limpia() -> str:
    """Apunta la base a un archivo temporal nuevo y la inicializa.

    Cada prueba arranca con su propia base. Compartirla las volvería
    dependientes del orden, y una prueba que solo pasa cuando corre segunda no
    sirve para detectar nada.
    """
    ruta = os.path.join(tempfile.mkdtemp(), "prueba.db")
    db.RUTA_DB = ruta
    db.inicializar()
    return ruta


# --- Fechas de prueba ------------------------------------------------------
# El validador rechaza una fecha de pago ANTERIOR a hoy, asi que las pruebas no
# pueden llevar fechas escritas a mano: el dia que pasan, la suite entera
# empieza a fallar sin que nadie haya cambiado codigo. Se calculan al correr.
def fecha_futura(dias: int = 30) -> str:
    """Una fecha de pago valida, siempre por delante de hoy."""
    return (date.today() + timedelta(days=dias)).strftime("%d/%m/%Y")


def fecha_pasada(dias: int = 1) -> str:
    """Una fecha de pago vencida, para probar que se rechaza."""
    return (date.today() - timedelta(days=dias)).strftime("%d/%m/%Y")


def carpeta_temporal() -> str:
    return tempfile.mkdtemp()


def lote(nombre: str = "Lote de prueba") -> Lote:
    return db.guardar_lote(Lote(nombre=nombre))


def solicitud(lote_id: str, nombre: str, *, tipo: str = "Acreedor",
              partidas: list[Partida] | None = None,
              estado: str = "VALIDADA", **extra) -> Solicitud:
    """Solicitud válida con lo mínimo para pasar el validador.

    Los datos son ficticios a propósito: un RFC genérico y una CLABE de ejemplo.
    """
    datos = dict(
        lote_id=lote_id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario=tipo, beneficiario_nombre=nombre,
        beneficiario_rfc="XAXX010101000",
        beneficiario_correo="prueba@ejemplo.invalid",
        cuenta_clabe="012345678901234568", cuenta_banco="BBVA",
        cuenta_titular=nombre, forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago=fecha_futura(),
        descripcion=f"Pago a {nombre}", estado=estado)
    datos.update(extra)
    return db.guardar_solicitud(Solicitud(**datos), partidas or [])


def concepto(nombre: str, importe: float, **extra) -> Partida:
    return Partida(clase=CONCEPTO, concepto_nombre=nombre, importe=importe,
                   **extra)


def insumo(nombre: str, importe: float, **extra) -> Partida:
    return Partida(clase=INSUMO, insumo_nombre=nombre, importe=importe, **extra)


def evento_check(valor: bool):
    """Evento de Flet como lo recibe un manejador: `e.control.value`.

    Se pasa el evento y no el valor suelto porque así es como llegan de verdad,
    y esa diferencia ya causó un error: un manejador que esperaba un dato
    recibía el evento como primer argumento posicional y lo usaba como si fuera
    un id.
    """
    control = type("ControlFalso", (), {"value": valor})()
    return type("EventoFalso", (), {"control": control})()


def etiqueta_de(control) -> str:
    """Texto visible de un botón.

    Flet acepta la etiqueta como primer argumento posicional y la guarda en
    `content`, dejando `text` en None. Se miran los dos para no depender de por
    dónde se haya construido el botón.
    """
    for atributo in ("text", "content"):
        valor = getattr(control, atributo, None)
        if isinstance(valor, str) and valor:
            return valor
    return ""


def boton_por_texto(raiz, texto: str):
    """Busca un botón por su etiqueta recorriendo el árbol de controles.

    Sirve para pulsar el botón DE VERDAD en vez de llamar a su manejador. La
    diferencia importa: llamar al método comprueba la lógica, pero se salta el
    cableado —qué método quedó enganchado y con qué argumentos lo invoca Flet—,
    que es donde caben errores que la lógica sola no ve.
    """
    pendientes = [raiz]
    while pendientes:
        control = pendientes.pop(0)
        if getattr(control, "on_click", None) is not None \
                and etiqueta_de(control) == texto:
            return control
        for atributo in ("controls", "actions"):
            pendientes.extend(getattr(control, atributo, None) or [])
        hijo = getattr(control, "content", None)
        if hijo is not None and not isinstance(hijo, str):
            pendientes.append(hijo)
    raise AssertionError(f"No se encontró un botón «{texto}».")


def confirmar_dialogo(pagina, etiqueta: str | None = None) -> None:
    """Pulsa el botón de confirmación del último diálogo abierto.

    Las acciones destructivas (eliminar, deshacer) preguntan antes de escribir,
    así que una prueba que no confirme estaría comprobando que NO pasó nada.
    Se busca el `FilledButton` porque es el botón afirmativo del diálogo: el de
    cancelar siempre es un `TextButton`.
    """
    import flet as ft

    if not pagina.dialogos:
        raise AssertionError("No se abrió ningún diálogo de confirmación.")
    dialogo = pagina.dialogos[-1]
    for accion in dialogo.actions or []:
        if isinstance(accion, ft.FilledButton) and (
                etiqueta is None
                or etiqueta.lower() in etiqueta_de(accion).lower()):
            accion.on_click(None)
            return
    raise AssertionError(
        f"El diálogo no trae un botón de confirmar{f' «{etiqueta}»' if etiqueta else ''}.")


def elegir_del_dialogo(pagina, indice: int) -> None:
    """Pulsa una de las opciones del CUERPO del último diálogo.

    Para diálogos que ofrecen a elegir (las fuentes de «Nueva solicitud»), donde
    lo pulsable son los renglones del contenido y no los botones del pie.
    """
    if not pagina.dialogos:
        raise AssertionError("No se abrió ningún diálogo.")
    opciones = [c for c in (pagina.dialogos[-1].content.controls or [])
                if getattr(c, "on_click", None)]
    if indice >= len(opciones):
        raise AssertionError(
            f"El diálogo solo ofrece {len(opciones)} opción(es) pulsable(s).")
    opciones[indice].on_click(None)


def pdf_falso(carpeta: str, nombre: str) -> str:
    """Archivo con cabecera de PDF, suficiente para lo que se prueba aquí."""
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, nombre)
    with open(ruta, "wb") as fh:
        fh.write(b"%PDF-1.4\n")
    return ruta


def caratula_pdf(carpeta: str, nombre_archivo: str, *, titular: str,
                 clabe: str, banco: str = "BBVA MEXICO S.A.",
                 cuenta: str = "1234567890") -> str:
    """Carátula real, con capa de texto, para probar la lectura de verdad.

    Se genera con capa de texto y no como imagen a propósito: así la prueba
    corre en un segundo y **sin depender de que Tesseract esté instalado**, pero
    sigue ejercitando el camino completo —extraer texto, interpretarlo, decidir
    quién manda—, que es donde están las decisiones que importan. Que además se
    lea una foto es cosa del OCR, y eso se comprueba contra carátulas reales,
    no aquí.
    """
    import fitz

    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, nombre_archivo)
    documento = fitz.open()
    pagina = documento.new_page()
    renglones = [
        banco,
        "Estado de cuenta",
        f"Titular de la cuenta: {titular}",
        f"No. de cuenta: {cuenta}",
        f"CLABE interbancaria: {clabe}",
    ]
    for i, renglon in enumerate(renglones):
        pagina.insert_text((60, 90 + i * 26), renglon, fontsize=12)
    documento.save(ruta)
    documento.close()
    return ruta


# --------------------------------------------------------------------------- #
#  Dobles de la interfaz
# --------------------------------------------------------------------------- #
# Las pantallas solo necesitan de `page` cuatro cosas: guardar controles, decir
# su tamaño, abrir diálogos y refrescar. Con eso se pueden construir y operar
# sin abrir una ventana, que es lento y no se puede automatizar del todo.
#
# Ojo con el límite de esto: un doble comprueba la LÓGICA de la pantalla, no que
# se dibuje. Que un control se pueda construir no significa que el cliente lo
# pueda pintar —eso lo cubre `scripts/smoke_render.py`, que sí abre la ventana.
class PaginaFalsa:
    def __init__(self) -> None:
        import flet as ft

        self.services: list = []
        self.controls: list = []
        self.theme_mode = ft.ThemeMode.LIGHT
        self.height, self.width = 900, 1600
        self.title = "prueba"
        self.on_keyboard_event = None
        self.dialogos: list = []
        self.window = type("Ventana", (), {"always_on_top": False})()

    def update(self) -> None:
        pass

    def add(self, *controles) -> None:
        self.controls.extend(controles)

    def show_dialog(self, dialogo) -> None:
        self.dialogos.append(dialogo)

    def pop_dialog(self):
        return self.dialogos.pop() if self.dialogos else None

    def run_task(self, *_a, **_k) -> None:
        pass


class ConfiguracionFalsa:
    def credenciales(self) -> tuple[str, str]:
        return ("usuario", "contrasena")


class AppFalsa:
    """Sustituto del shell. Guarda los avisos para poder afirmar sobre ellos."""

    def __init__(self) -> None:
        self.page = PaginaFalsa()
        self.avisos: list[str] = []
        self.config = ConfiguracionFalsa()

    def avisar(self, mensaje: str, color=None, **_k) -> None:
        self.avisos.append(mensaje)

    def abrir_en_sistema(self, _ruta: str) -> None:
        pass

    def ir_a_bitacora(self) -> None:
        pass

    def refrescar_ambiente(self) -> None:
        pass

    @property
    def ultimo_aviso(self) -> str:
        return self.avisos[-1] if self.avisos else ""
