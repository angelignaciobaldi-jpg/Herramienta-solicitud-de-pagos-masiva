"""Catálogo local de conceptos de pago.

SIPP asigna los conceptos **por empresa** y no ofrece forma de consultarlos sin
entrar al formulario de captura. Eso obliga a que quien llena el Excel escriba
el concepto de memoria, y un concepto mal escrito no falla al capturar: falla a
media corrida, cuando el robot no lo encuentra en el grid y marca la solicitud
para revisión.

Este módulo importa esa lista una vez desde el portal y la guarda, para que
tanto la plantilla de Excel como el formulario de captura la ofrezcan como
desplegable. El objetivo es que el colaborador **elija en vez de teclear**.

Es una **lista única para todo el grupo**, no una por empresa: el criterio del
área es que todas manejen los mismos conceptos, y el de Abastecedora sirve de
base. De qué empresa se leyó cada uno se conserva solo como rastro informativo.

Dos orígenes, y la diferencia importa:

- `SIPP` — leído del portal. Existe con certeza.
- `MANUAL` — lo capturó una persona porque le hacía falta. **Puede no existir
  en SIPP**, y si no existe, la solicitud que lo use fallará. Por eso se marca
  distinto y la interfaz avisa al crearlo: hay que pedirlo a soporte.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from core import db

ORIGEN_SIPP = "SIPP"
ORIGEN_MANUAL = "MANUAL"

# Empresa desde la que se sembró el catálogo por primera vez. Sus conceptos son
# los más completos del grupo y sirven de base para las demás.
EMPRESA_BASE = "Abastecedora"


@dataclass
class Concepto:
    """Un concepto de pago del catálogo local.

    `empresa` NO forma parte de su identidad: es solo el rastro de dónde se
    importó. El catálogo es una lista única para todo el grupo.
    """

    id: str = field(default_factory=lambda: db._nuevo_id())
    nombre: str = ""
    empresa: str = ""
    origen: str = ORIGEN_MANUAL
    nota: str = ""
    creado_en: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def verificado(self) -> bool:
        """True si se leyó del portal, o sea que existe con certeza."""
        return self.origen == ORIGEN_SIPP


def normalizar(nombre: str) -> str:
    """Forma canónica de un concepto: mayúsculas, sin acentos ni dobles espacios.

    SIPP los guarda en mayúsculas y el robot los busca por palabras, así que
    conservar variantes de capitalización solo generaría duplicados aparentes.
    """
    t = unicodedata.normalize("NFKD", (nombre or "").strip().upper())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t)


# --------------------------------------------------------------------------- #
#  Consulta
# --------------------------------------------------------------------------- #
def listar() -> list[Concepto]:
    """Todo el catálogo, en orden alfabético."""
    with db.conectar() as con:
        filas = con.execute(
            "SELECT * FROM concepto_pago ORDER BY nombre").fetchall()
    return [db._desde_fila(Concepto, f) for f in filas]


def nombres() -> list[str]:
    """Nombres para alimentar un desplegable.

    Sin filtro por empresa a propósito: el catálogo es uno solo para el grupo.
    Filtrar dejaría al usuario sin opciones en cuanto capturara para una empresa
    cuyo catálogo todavía no se ha importado.
    """
    return [c.nombre for c in listar() if c.nombre]


def existe(nombre: str) -> bool:
    objetivo = normalizar(nombre)
    return any(normalizar(c.nombre) == objetivo for c in listar())


def hay_catalogo() -> bool:
    with db.conectar() as con:
        fila = con.execute("SELECT COUNT(*) AS n FROM concepto_pago").fetchone()
    return bool(fila and fila["n"])


# --------------------------------------------------------------------------- #
#  Alta y baja
# --------------------------------------------------------------------------- #
def guardar(nombre: str, empresa: str = "", origen: str = ORIGEN_MANUAL,
            nota: str = "") -> Concepto | None:
    """Da de alta un concepto. Devuelve None si ya estaba (no es un error).

    El nombre se normaliza antes de guardar para que «Pago PTU» y «PAGO PTU» no
    acaben siendo dos entradas del mismo concepto. `empresa` solo deja rastro de
    dónde salió: no participa en la identidad.
    """
    limpio = normalizar(nombre)
    if not limpio:
        return None
    concepto = Concepto(nombre=limpio, empresa=empresa, origen=origen, nota=nota)
    with db.conectar() as con:
        choque = con.execute(
            "SELECT id FROM concepto_pago WHERE nombre = ?",
            (limpio,)).fetchone()
        if choque:
            return None
        db._guardar(con, "concepto_pago", concepto)
    return concepto


def borrar(concepto_id: str) -> None:
    with db.conectar() as con:
        con.execute("DELETE FROM concepto_pago WHERE id = ?", (concepto_id,))


def importar(nombres_sipp: list[str], empresa: str) -> dict:
    """Guarda de golpe los conceptos leídos del portal.

    Devuelve `{"nuevos": n, "ya_estaban": n}`. Volver a importar es seguro: no
    duplica, y sirve para detectar conceptos que SIPP agregó después.
    """
    nuevos = ya_estaban = 0
    for nombre in nombres_sipp:
        if guardar(nombre, empresa, ORIGEN_SIPP) is not None:
            nuevos += 1
        else:
            ya_estaban += 1
    return {"nuevos": nuevos, "ya_estaban": ya_estaban}


# --------------------------------------------------------------------------- #
#  Lectura desde SIPP
# --------------------------------------------------------------------------- #
def _leer_grid_con_scroll(s, grid, selectores) -> list[str]:
    """Todos los conceptos del grid, no solo los que caben en pantalla.

    El grid del portal es un **ng-grid**, que virtualiza: mantiene en el DOM
    únicamente las filas visibles y las recicla al desplazarse. Leerlo de una
    pasada devolvía siempre las primeras trece y los conceptos dados de alta
    después —que quedan más abajo en la lista— no se importaban nunca, sin que
    nada lo delatara: el resumen decía «0 nuevos» y parecía que ya estaban.

    Se baja de pantalla en pantalla acumulando lo leído. Se para cuando el
    viewport llega al fondo, o cuando varias vueltas seguidas no aportan nada
    nuevo (por si el scroll no avanza).
    """
    css_nombre = selectores.css("con.nombre")
    vistos: list[str] = []
    conocidos: set[str] = set()

    def cosechar() -> int:
        antes = len(vistos)
        for i in range(grid.count()):
            try:
                texto = grid.nth(i).locator(css_nombre).first.inner_text()
            except Exception:  # noqa: BLE001 — renglón reciclado a media lectura
                continue
            limpio = normalizar(texto)
            if limpio and limpio not in conocidos:
                conocidos.add(limpio)
                vistos.append(limpio)
        return len(vistos) - antes

    # El contenedor con scroll de ng-grid cuelga del propio grid.
    viewport = s.page.locator(f"{selectores.css('con.grid')} .ngViewport").first
    cosechar()
    if viewport.count() == 0:
        return vistos                      # sin viewport no hay nada que bajar

    secos = 0
    for _ in range(200):                   # tope duro: nunca un bucle infinito
        try:
            al_fondo = viewport.evaluate(
                "el => { const y = el.scrollTop;"
                " el.scrollTop = y + Math.max(40, el.clientHeight * 0.8);"
                " return el.scrollTop <= y + 1"
                " || el.scrollTop >= el.scrollHeight - el.clientHeight - 2; }")
        except Exception:  # noqa: BLE001 — el grid desapareció
            break
        s.page.wait_for_timeout(180)       # deja que ng-grid repinte las filas
        secos = 0 if cosechar() else secos + 1
        if al_fondo or secos >= 3:
            break
    cosechar()                             # última pantalla
    return vistos


def leer_de_sipp(usuario: str, contrasena: str, *, url_login: str,
                 empresa: str = EMPRESA_BASE, sucursal: str = "Corporativo",
                 tipo_beneficiario: "str | None" = None,
                 visible: bool = False) -> dict:
    """Entra a SIPP, abre el formulario y lee el grid de Conceptos de Pago.

    No captura ni guarda nada en el portal: solo llega a la pantalla, elige la
    empresa y lee la lista. Devuelve
    `{"ok": bool, "conceptos": [...], "mensaje": str}`.
    """
    from core.db import CONCEPTO
    from core.rpa_sipp import (ErrorRpa, FlujoSolicitudPago, SesionSipp,
                               catalogos, selectores)

    try:
        with SesionSipp(url_login, visible=visible) as s:
            s.login(usuario, contrasena)
            s.configurar_sesion(empresa, sucursal)
            s.ir_a_solicitud_pago()
            flujo = FlujoSolicitudPago(s)
            flujo.asegurar_modo_agregar()

            s.seleccionar_chosen("sol.empresa", empresa, "Empresa")
            s.seleccionar_chosen("sol.sucursal", sucursal, "Sucursal")
            s.seleccionar_chosen("sol.tipo_pago",
                                 catalogos.TIPO_PAGO_SOPORTADO, "Tipo de pago")

            # SIPP asigna los conceptos por empresa Y POR TIPO DE
            # BENEFICIARIO: el grid de un Acreedor no es el de un Deudor. Se
            # recorren los dos —Proveedor no cuenta, su desglose son insumos—
            # y se unen, porque el catálogo local es una lista única. Leer solo
            # uno dejaba fuera, en silencio, los del otro.
            tipos = ([tipo_beneficiario] if tipo_beneficiario else
                     [t for t in catalogos.TIPOS_BENEFICIARIO
                      if catalogos.clase_desglose(t) == CONCEPTO])
            encontrados: list[str] = []
            for tipo in tipos:
                # Cambiar de tipo puede hacer que SIPP vuelva a pedir las
                # listas dependientes y pierda el tipo de pago. Reafirmarlo es
                # barato: si ya está puesto, `seleccionar_chosen` no toca nada.
                s.seleccionar_chosen("sol.tipo_pago",
                                     catalogos.TIPO_PAGO_SOPORTADO,
                                     "Tipo de pago")
                s.seleccionar_chosen("sol.tipo_beneficiario", tipo,
                                     "Tipo de beneficiario")
                s.page.wait_for_timeout(1500)
                s.cerrar_alertas()
                s.abrir_pestana("conceptos")
                grid = s.loc("con.filas")
                for _ in range(24):      # hasta ~12s a que cargue
                    if grid.count() > 0:
                        break
                    s.page.wait_for_timeout(500)
                for nombre in _leer_grid_con_scroll(s, grid, selectores):
                    if nombre not in encontrados:
                        encontrados.append(nombre)
    except ErrorRpa as exc:
        return {"ok": False, "conceptos": [], "mensaje": str(exc)}
    except Exception as exc:  # noqa: BLE001 — se reporta tal cual
        return {"ok": False, "conceptos": [],
                "mensaje": f"No se pudo leer el catálogo: {exc}"}

    if not encontrados:
        return {
            "ok": False, "conceptos": [],
            "mensaje": (f"«{empresa}» no tiene conceptos asignados para "
                        f"{' ni '.join(tipos)}. Prueba con otra empresa."),
        }
    return {"ok": True, "conceptos": encontrados,
            "mensaje": f"{len(encontrados)} concepto(s) leídos de {empresa}."}
