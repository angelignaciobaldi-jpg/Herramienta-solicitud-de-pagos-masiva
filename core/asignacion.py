"""Asignación masiva de concepto o insumo a muchas solicitudes a la vez.

Es la operación que ahorra más trabajo del proyecto —cien solicitudes que
comparten el mismo concepto de pago— y también la más peligrosa: toca todo el
lote de golpe y lo que rompe no se nota hasta que el robot falla, o peor, hasta
que paga mal.

Por eso está partida en dos pasos que no se pueden saltar:

1. `calcular()` produce el **antes y después** de cada solicitud afectada, sin
   tocar la base. Es lo que la interfaz muestra como vista previa.
2. `aplicar()` lo escribe, guardando antes un snapshot para poder deshacer.

Y por eso todo lo que sale de aquí pasa por `core.validador`: el objetivo de la
asignación masiva es ahorrar capturas, no saltarse las reglas.

Ahora bien, «pasa por el validador» no es lo mismo que «se niega a aplicar». Lo
único que esta operación escribe es el desglose, así que solo la bloquean los
errores del desglose. Lo que ya venía mal por otro lado —una CLABE faltante, un
RFC mal escrito— se informa pero no impide asignar: negarse dejaría al usuario
sin salida, porque este modal tampoco permite corregir esos campos. La red que
impide que una solicitud incompleta llegue a SIPP está donde corresponde, en
`rpa_sipp.procesar_lote`, que la valida antes de capturarla.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core import catalogos, db, validador
from core.db import CONCEPTO, INSUMO, Partida, Solicitud

# --- Alcance: a qué solicitudes se les aplica ---
TODAS = "TODAS"
SELECCIONADAS = "SELECCIONADAS"
SIN_PARTIDAS = "SIN_PARTIDAS"

# --- Modo: qué hacer con lo que ya tienen ---
AGREGAR = "AGREGAR"
REEMPLAZAR = "REEMPLAZAR"

# --- Importe: de dónde sale el del renglón nuevo ---
TOTAL_SOLICITUD = "TOTAL"        # el importe actual de la solicitud
DISTRIBUIR = "DISTRIBUIR"        # el total repartido entre los renglones
EN_BLANCO = "BLANCO"             # se deja en cero y se llena a mano
IMPORTE_FIJO = "FIJO"            # el mismo importe para todas


# El único campo que esta operación escribe es el desglose. El validador marca
# sus hallazgos con `campo`, así que ese es el corte entre lo que la asignación
# provoca y lo que ya venía mal de antes.
_CAMPO_DESGLOSE = "partidas"


@dataclass
class Cambio:
    """Lo que le pasaría a UNA solicitud. Nada de esto se ha guardado aún."""

    solicitud: Solicitud
    partidas_antes: list[Partida] = field(default_factory=list)
    partidas_despues: list[Partida] = field(default_factory=list)
    hallazgos: list = field(default_factory=list)
    aviso: str = ""

    @property
    def total_antes(self) -> float:
        return db.total_desglose(self.solicitud.tipo_beneficiario,
                                 self.partidas_antes)

    @property
    def total_despues(self) -> float:
        return db.total_desglose(self.solicitud.tipo_beneficiario,
                                 self.partidas_despues)

    @property
    def bloqueantes(self) -> list:
        """Errores del desglose: los que esta asignación dejaría escritos."""
        return [h for h in self.hallazgos
                if h.es_error and h.campo == _CAMPO_DESGLOSE]

    @property
    def pendientes(self) -> list:
        """Errores ajenos al desglose, que ya traía la solicitud.

        La CLABE, el RFC o la fecha no se tocan aquí, así que negarse a asignar
        el concepto por culpa de ellos no arregla nada: deja al usuario sin
        salida, porque este modal tampoco permite corregirlos. Se informan y la
        solicitud se aplica; sigue marcada como incompleta en la tabla del lote
        y **el motor se niega a capturarla** hasta que se corrija.
        """
        return [h for h in self.hallazgos
                if h.es_error and h.campo != _CAMPO_DESGLOSE]

    @property
    def valido(self) -> bool:
        """True si se puede aplicar; no si la solicitud queda lista.

        Son cosas distintas: una solicitud puede recibir bien su concepto y
        seguir sin CLABE. Para «lista para capturar» está `completo`.
        """
        return not self.bloqueantes

    @property
    def completo(self) -> bool:
        """True si tras aplicar no le quedaría ningún error."""
        return not validador.hay_errores(self.hallazgos)


@dataclass
class Plan:
    """El resultado de `calcular()`: qué cambiaría y qué hay que advertir."""

    cambios: list[Cambio] = field(default_factory=list)
    omitidas: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def validos(self) -> list[Cambio]:
        """Los que se pueden aplicar (el desglose queda bien)."""
        return [c for c in self.cambios if c.valido]

    @property
    def completos(self) -> list[Cambio]:
        """Los que además quedarían listos para capturar."""
        return [c for c in self.cambios if c.completo]

    @property
    def incompletos(self) -> list[Cambio]:
        """Se aplican, pero siguen con algo que corregir fuera del desglose."""
        return [c for c in self.cambios if c.valido and not c.completo]

    @property
    def con_aviso(self) -> list[Cambio]:
        return [c for c in self.cambios if c.aviso]


def _elegibles(lote_id: str, alcance: str, seleccionadas: set[str],
               clase: str) -> tuple[list[Solicitud], list[str]]:
    """Solicitudes a las que aplica, y los nombres de las que se omiten."""
    todas = db.listar_solicitudes(lote_id)
    # Las ya capturadas no se tocan: cambiarles el desglose aquí no cambiaría
    # nada en SIPP y dejaría la base mintiendo sobre lo que se registró.
    intocables = {"GUARDADA", "ENVIADA_AUTORIZAR"}
    elegibles, omitidas = [], []
    for s in todas:
        if s.estado in intocables:
            omitidas.append(f"{s.beneficiario_nombre} (ya capturada)")
            continue
        if alcance == SELECCIONADAS and s.id not in seleccionadas:
            continue
        if alcance == SIN_PARTIDAS and db.listar_partidas(s.id, clase):
            continue
        elegibles.append(s)
    return elegibles, omitidas


def calcular(lote_id: str, *, alcance: str = TODAS,
             seleccionadas: set[str] | None = None,
             modo: str = AGREGAR,
             nombre: str = "", centro_costos: str = "",
             cuenta_contable: str = "", tipo_compra: str = "",
             criterio_importe: str = TOTAL_SOLICITUD,
             importe_fijo: float = 0.0) -> Plan:
    """Calcula el antes/después sin escribir nada.

    La **clase del renglón no se elige**: la impone el tipo de beneficiario de
    cada solicitud, igual que en el resto de la herramienta (a un Proveedor le
    corresponden insumos; a un Deudor o Acreedor, conceptos). Por eso una misma
    asignación puede producir renglones de clases distintas en el mismo lote, y
    está bien.
    """
    seleccionadas = seleccionadas or set()
    if not nombre.strip():
        return Plan(error="Falta el concepto o insumo que se va a asignar.")

    # Para el alcance «sin partidas» hace falta saber de qué clase hablamos, y
    # eso depende de cada solicitud; se resuelve por solicitud más abajo.
    elegibles, omitidas = _elegibles(lote_id, alcance, seleccionadas, CONCEPTO)
    if alcance == SIN_PARTIDAS:
        elegibles = [
            s for s in db.listar_solicitudes(lote_id)
            if s.estado not in ("GUARDADA", "ENVIADA_AUTORIZAR")
            and not db.listar_partidas(
                s.id, catalogos.clase_desglose(s.tipo_beneficiario))]

    if not elegibles:
        return Plan(omitidas=omitidas,
                    error="Ninguna solicitud cumple el alcance elegido.")

    cambios = []
    for s in elegibles:
        clase = catalogos.clase_desglose(s.tipo_beneficiario)
        antes = db.listar_partidas(s.id)
        conservadas = [] if modo == REEMPLAZAR else [
            Partida(**{**p.__dict__}) for p in antes]

        nueva = Partida(clase=clase, solicitud_id=s.id, origen="MASIVA")
        if clase == CONCEPTO:
            nueva.concepto_nombre = nombre.strip()
        else:
            nueva.insumo_nombre = nombre.strip()
            nueva.centro_costos = centro_costos.strip()
            nueva.cuenta_contable = cuenta_contable.strip()
            nueva.tipo_compra = tipo_compra.strip()

        despues = conservadas + [nueva]
        _repartir_importe(s, despues, nueva, criterio_importe, importe_fijo,
                          clase, antes)

        cambio = Cambio(solicitud=s, partidas_antes=antes,
                        partidas_despues=despues)
        # Advertencia explícita cuando se pisa lo que trajo un CFDI: eso lo
        # timbró el SAT y sustituirlo a ciegas es perder el respaldo del gasto.
        if modo == REEMPLAZAR and any(p.origen == "CFDI" for p in antes):
            cambio.aviso = ("Se reemplazarán partidas que venían de un CFDI.")
        elif any(p.origen == "CFDI" for p in antes):
            cambio.aviso = "Ya tiene partidas de un CFDI."

        # Se valida con el importe recalculado, igual que si se hubiera
        # capturado a mano: la asignación masiva no es un atajo a las reglas.
        copia = Solicitud(**{**s.__dict__})
        copia.importe_total = db.total_desglose(s.tipo_beneficiario, despues)
        cambio.hallazgos = validador.validar(copia, despues)
        cambios.append(cambio)

    return Plan(cambios=cambios, omitidas=omitidas)


def _total_implicito(solicitud: Solicitud, partidas: list[Partida]) -> float:
    """Cuánto vale ya esta solicitud, aunque le falte el desglose que toca.

    No basta con `importe_total`: ese se **deriva** de las partidas de la clase
    que corresponde al tipo de beneficiario, así que una solicitud sin conceptos
    vale cero por definición —y ese es justamente el caso que la asignación
    masiva viene a resolver—.

    El caso real es el CFDI: llega con partidas de INSUMO por sus conceptos
    facturados y sin ningún concepto de pago, porque el CFDI no lo trae. «Tomar
    el total» tiene que entender ese importe, no el cero.

    Orden: el total derivado si ya existe; si no, la suma de lo que haya.
    """
    if solicitud.importe_total:
        return solicitud.importe_total
    derivado = db.total_desglose(solicitud.tipo_beneficiario, partidas)
    if derivado:
        return derivado
    return round(sum(p.importe for p in partidas), 2)


def _repartir_importe(solicitud: Solicitud, despues: list[Partida],
                      nueva: Partida, criterio: str, fijo: float,
                      clase: str, antes: list[Partida]) -> None:
    """Fija el importe del renglón nuevo según el criterio elegido."""
    if criterio == EN_BLANCO:
        nueva.importe = 0.0
        return
    if criterio == IMPORTE_FIJO:
        nueva.importe = round(fijo, 2)
        return

    total = _total_implicito(solicitud, antes)
    if criterio == TOTAL_SOLICITUD:
        nueva.importe = round(total, 2)
        return

    # DISTRIBUIR: el total de la solicitud repartido entre los renglones de su
    # clase. El sobrante de redondeo va al último, para que la suma cuadre
    # exactamente con el total en vez de quedar a unos centavos.
    de_la_clase = [p for p in despues if p.clase == clase]
    if not de_la_clase:
        nueva.importe = round(total, 2)
        return
    cuota = round(total / len(de_la_clase), 2)
    for p in de_la_clase:
        p.importe = cuota
    de_la_clase[-1].importe = round(
        total - cuota * (len(de_la_clase) - 1), 2)


def aplicar(lote_id: str, plan: Plan, *, solo_validas: bool = True) -> dict:
    """Escribe el plan, guardando antes un snapshot para deshacer.

    Devuelve `{"aplicadas": n, "omitidas": n, "snapshot": id}`.
    """
    cambios = plan.validos if solo_validas else plan.cambios
    if not cambios:
        return {"aplicadas": 0, "omitidas": len(plan.cambios), "snapshot": ""}

    snapshot = db.guardar_snapshot(lote_id, "Asignación masiva")
    aplicadas = 0
    for cambio in cambios:
        s = cambio.solicitud
        try:
            db.guardar_solicitud(s, cambio.partidas_despues)
            aplicadas += 1
        except db.ClaveDuplicada:
            # Dos solicitudes que acaban idénticas tras la asignación: se deja
            # la primera y se avisa, en vez de tumbar toda la operación.
            db.registrar(s.id, "asignacion",
                         "Quedó idéntica a otra del lote y no se aplicó",
                         "WARN")
        except Exception as exc:  # noqa: BLE001
            db.registrar(s.id, "asignacion", str(exc), "ERROR")
    return {"aplicadas": aplicadas,
            "omitidas": len(plan.cambios) - aplicadas,
            "snapshot": snapshot}


def deshacer(lote_id: str) -> bool:
    """Devuelve el lote al estado previo a la última asignación masiva."""
    return db.restaurar_snapshot(lote_id)
