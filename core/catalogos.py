"""Catálogos de la pantalla "Solicitud de Pago" del SIPP (fuente única).

Cada lista es **el texto exacto** que el SIPP muestra en su `<option>`. Importa
que sea idéntico: el RPA no selecciona por id sino escribiendo el texto en el
buscador del widget *chosen* (ver ESPECIFICACION.md §8), así que un acento o un
espacio de más hacen que no encuentre la opción. Por eso
`SERVICIOS  EDUCATIVOS IMAA` conserva su doble espacio: así viene del sistema.

Los ids numéricos de SIPP **no** se replican aquí (salvo donde el DOM los
revela), porque el `value` que aparece en el HTML es el índice interno de
AngularJS —`ng-options`—, no el id real. La única forma confiable de operar es
por texto visible.

Extraído del DOM real de `Paginas html/solicitud de pago agregar.html`.
El catálogo de empresas vive aparte, en `core/empresas.py`.
"""

from __future__ import annotations

# --- Sucursales / plazas -------------------------------------------------
# La lista de plazas se recarga al elegir empresa, así que no toda empresa
# ofrece todas: esto es el universo, no lo disponible en cada caso.
SUCURSALES: list[str] = [
    "Culiacan", "Durango", "Ensenada", "Guadalajara", "Guerrero Negro",
    "Hermosillo", "Los Mochis", "Mazatlan", "Mexicali", "Nogales", "Rosarito",
    "Tepic", "Tijuana", "Corporativo", "Salamanca", "Manzanillo", "Petroil II",
    "Petroil VIII", "HMexicali", "La Paz", "Monterrey", "Gomez Palacio",
    "Guaymas", "MYMexicali", "Ciudad de Mexico", "Empalme", "Silao",
    "Ciudad Obregon", "GPMEXICALI", "EQUIPO DE COMPUTO", "Villahermosa",
    "MERIDA",
]

# --- Tipo de pago --------------------------------------------------------
# La herramienta solo opera "Pago Extraordinario"; los demás se listan para
# poder mostrarlos deshabilitados y explicar por qué (ver ESPECIFICACION.md §2).
TIPOS_PAGO: list[str] = [
    "Anticipo", "Anticipo Deudores", "Pago Extraordinario",
    "Saldo de Viáticos y Gasolina",
]
TIPO_PAGO_SOPORTADO = "Pago Extraordinario"

# --- Tipo de beneficiario (solo si Tipo de pago = Pago Extraordinario) ----
# Aquí SÍ conocemos los ids internos, porque el propio formulario los cita al
# decidir qué panel muestra. Se necesitan para acotar los selectores de cada
# panel: varios campos del beneficiario están DUPLICADOS, uno por panel.
TIPOS_BENEFICIARIO: list[str] = ["Proveedor", "Deudor", "Acreedor"]
ID_PANEL_BENEFICIARIO: dict[str, int] = {
    "Proveedor": 1,
    "Deudor": 2,
    "Acreedor": 3,
}

# El tipo de beneficiario DECIDE por cuál de los dos mecanismos se desglosa la
# solicitud, y son excluyentes: verificado contra stage el 31/07/2026, las
# pestañas que SIPP muestra en cada caso son
#   Proveedor -> Documentos Respaldo · Autorización · Pagos · Insumos & Servicios
#   Deudor    -> Documentos Respaldo · Autorización · Pagos · Conceptos de Pago
#   Acreedor  -> Documentos Respaldo · Autorización · Pagos · Conceptos de Pago
# No es una preferencia del usuario: pedir conceptos para un Proveedor, o
# insumos para un Acreedor, es pedir una pestaña que no existe.
CLASE_DESGLOSE: dict[str, str] = {
    "Proveedor": "INSUMO",
    "Deudor": "CONCEPTO",
    "Acreedor": "CONCEPTO",
}


def clase_desglose(tipo_beneficiario: str) -> str:
    """Clase de partida que aplica a un tipo de beneficiario.

    Por defecto CONCEPTO: es el caso de dos de los tres tipos y el único con
    código probado en producción.
    """
    return CLASE_DESGLOSE.get(tipo_beneficiario, "CONCEPTO")

# --- Forma de pago -------------------------------------------------------
# Solo con "Transferencia" se habilita el combo de cuentas bancarias del
# beneficiario: el propio formulario lo deshabilita con cualquier otra.
FORMAS_PAGO: list[str] = [
    "Cheque", "Transferencia", "Efectivo", "Linea de Captura",
]
FORMA_PAGO_CON_CUENTA = "Transferencia"

# --- Tipo de gasto -------------------------------------------------------
# Con "Deducible" el formulario exige adjuntar el XML del CFDI.
TIPOS_GASTO: list[str] = [
    "Deducible", "No Deducible", "Deducible SF", "Contribución",
]
TIPO_GASTO_EXIGE_XML = "Deducible"

# --- Moneda --------------------------------------------------------------
# Se deshabilita en cuanto hay cuenta bancaria seleccionada: la
# moneda del pago la impone la cuenta. No existe campo de tipo de cambio.
MONEDAS: list[str] = ["Pesos (MXN)", "Dolar (USD)", "Euro"]
MONEDA_DEFECTO = "Pesos (MXN)"

# --- Tipo de proveedor (solo al registrar un beneficiario nuevo) ----------

TIPOS_PROVEEDOR: list[str] = [
    "Moneda Nacional", "Moneda Extranjera", "Partes relacionadas Nacionales",
]

# --- Tipo de compra (pestaña Insumos & Servicios) ------------------------
# En la captura del DOM solo venía "Servicios"; el catálogo
# real puede traer más según la empresa, así que NO se valida contra esta lista:
# se usa como sugerencia en la interfaz.
TIPOS_COMPRA: list[str] = ["Servicios"]

# --- Tipo de transferencia (modal de Cuenta Bancaria) --------------------
# Por indicación del área, todas las transferencias se
# capturan como SPEI (no se usa "Mismo Banco" ni el número de cuenta interno).
TIPOS_TRANSFERENCIA: list[str] = ["SPEI", "Mismo Banco"]
TIPO_TRANSFERENCIA_DEFECTO = "SPEI"

# --- Puntos de parada del RPA (concepto propio, no de SIPP) --------------
# Hasta dónde llega el robot con cada solicitud. Ver ESPECIFICACION.md §8.
PARADAS: list[str] = ["LLENADA", "GUARDADA", "AUTORIZAR"]
PARADA_DEFECTO = "LLENADA"
ETIQUETA_PARADA: dict[str, str] = {
    "LLENADA": "Llenar y esperar",
    "GUARDADA": "Guardar",
    "AUTORIZAR": "Guardar y autorizar",
}

# --- Estados de una solicitud (máquina de estados del motor) -------------
ESTADOS: list[str] = [
    "PENDIENTE", "VALIDADA", "EN_CAPTURA", "LLENADA", "GUARDADA",
    "ENVIADA_AUTORIZAR", "ERROR", "OMITIDA", "REVISAR",
]
ETIQUETA_ESTADO: dict[str, str] = {
    "PENDIENTE": "Pendiente",
    "VALIDADA": "Validada",
    "EN_CAPTURA": "En captura",
    "LLENADA": "Llenada (sin guardar)",
    "GUARDADA": "Guardada",
    "ENVIADA_AUTORIZAR": "Enviada a autorizar",
    "ERROR": "Error",
    "OMITIDA": "Omitida",
    "REVISAR": "Revisar a mano",
}

# --- Ambientes de SIPP ---------------------------------------------------
# Las URLs NO están aquí: salen de `datos/sipp.json`, que no se versiona porque
# el repositorio es público (ver core/sipp_datos.py). `AMBIENTES` se resuelve al
# primer uso y no al importar, para que la falta del archivo se explique donde
# se pueda hacer algo al respecto y no como un error de import.
# La herramienta opera contra PRODUCCIÓN y no ofrece cambiarlo: es la
# instalación con la que trabaja el área, y un selector de ambiente en la
# pantalla es sobre todo una forma de equivocarse —capturar un lote entero
# contra stage creyendo que quedó registrado, o al revés—. Los ensayos se hacen
# desde los scripts de `scripts/`, que sí eligen ambiente, y no desde la app
# instalada.
AMBIENTE_DEFECTO = "PRODUCCION"


def ambientes() -> dict[str, str]:
    """`{'PRUEBAS': url, 'PRODUCCION': url}` desde el archivo de integración."""
    from core import sipp_datos

    return sipp_datos.ambientes()


class _Ambientes(dict):
    """Compatibilidad con el uso previo `AMBIENTES[...]`, cargando al vuelo."""

    def _asegurar(self):
        if not dict.__len__(self):
            dict.update(self, ambientes())
        return self

    def __getitem__(self, clave):
        return dict.__getitem__(self._asegurar(), clave)

    def __contains__(self, clave):
        return dict.__contains__(self._asegurar(), clave)

    def __iter__(self):
        return dict.__iter__(self._asegurar())

    def __len__(self):
        return dict.__len__(self._asegurar())

    def get(self, clave, defecto=None):
        return dict.get(self._asegurar(), clave, defecto)

    def items(self):
        return dict.items(self._asegurar())

    def keys(self):
        return dict.keys(self._asegurar())


AMBIENTES = _Ambientes()
