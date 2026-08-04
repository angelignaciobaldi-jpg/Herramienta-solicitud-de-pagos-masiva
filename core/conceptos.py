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
def leer_de_sipp(usuario: str, contrasena: str, *, url_login: str,
                 empresa: str = EMPRESA_BASE, sucursal: str = "Corporativo",
                 tipo_beneficiario: str = "Acreedor",
                 visible: bool = False) -> dict:
    """Entra a SIPP, abre el formulario y lee el grid de Conceptos de Pago.

    No captura ni guarda nada en el portal: solo llega a la pantalla, elige la
    empresa y lee la lista. Devuelve
    `{"ok": bool, "conceptos": [...], "mensaje": str}`.
    """
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
            s.seleccionar_chosen("sol.tipo_beneficiario", tipo_beneficiario,
                                 "Tipo de beneficiario")
            s.page.wait_for_timeout(1500)
            s.cerrar_alertas()

            s.abrir_pestana("conceptos")
            grid = s.loc("con.filas")
            for _ in range(24):          # hasta ~12s a que cargue
                if grid.count() > 0:
                    break
                s.page.wait_for_timeout(500)

            encontrados = []
            for i in range(grid.count()):
                try:
                    texto = grid.nth(i).locator(
                        selectores.css("con.nombre")).first.inner_text()
                except Exception:  # noqa: BLE001 — renglón ilegible
                    continue
                limpio = normalizar(texto)
                if limpio:
                    encontrados.append(limpio)
    except ErrorRpa as exc:
        return {"ok": False, "conceptos": [], "mensaje": str(exc)}
    except Exception as exc:  # noqa: BLE001 — se reporta tal cual
        return {"ok": False, "conceptos": [],
                "mensaje": f"No se pudo leer el catálogo: {exc}"}

    if not encontrados:
        return {
            "ok": False, "conceptos": [],
            "mensaje": (f"«{empresa}» no tiene conceptos asignados para "
                        f"{tipo_beneficiario}. Prueba con otra empresa o con "
                        f"otro tipo de beneficiario."),
        }
    return {"ok": True, "conceptos": encontrados,
            "mensaje": f"{len(encontrados)} concepto(s) leídos de {empresa}."}
