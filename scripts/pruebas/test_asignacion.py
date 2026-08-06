"""Asignación masiva: alcance, modo, reparto del importe, snapshot y deshacer.

Es la operación que toca el lote entero de una vez, así que lo que más se prueba
aquí es que **no haga nada hasta que se confirme** y que se pueda revertir.
"""

from __future__ import annotations

from core import asignacion, db
from core.db import CONCEPTO, INSUMO
from scripts.pruebas import comun


def _lote_con_cfdi():
    """Lote con la forma real del caso: insumos facturados, sin conceptos.

    Una solicitud importada de un CFDI llega con el importe en sus insumos y sin
    ningún concepto de pago —el CFDI no lo trae—, así que su `importe_total` es
    cero. Ese es justo el caso que la asignación masiva viene a resolver.
    """
    lote = comun.lote()
    ana = comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])
    bruno = comun.solicitud(lote.id, "BRUNO DIAZ", partidas=[
        comun.insumo("Facturado", 2000.0, origen="CFDI")])
    carla = comun.solicitud(lote.id, "CARLA RUIZ", partidas=[
        comun.concepto("VIEJO", 500.0, origen="CFDI")])
    prov = comun.solicitud(lote.id, "PROVEEDOR SA", tipo="Proveedor", partidas=[
        comun.insumo("Ya facturado", 3000.0, origen="CFDI")])
    capturada = comun.solicitud(lote.id, "YA CAPTURADA", estado="GUARDADA")
    return lote, ana, bruno, carla, prov, capturada


def probar_calcular_no_toca_la_base():
    """la vista previa se calcula sin escribir nada"""
    lote, ana, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA")
    assert plan.cambios
    assert len(db.listar_partidas(ana.id)) == 1, "seguía con solo su insumo"


def probar_no_toca_las_ya_capturadas():
    """una solicitud guardada en SIPP se omite y se dice por qué"""
    # Cambiarle el desglose aquí no cambiaría nada allá y dejaría la base
    # mintiendo sobre lo que se registró.
    lote, *_, capturada = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA")
    afectadas = {c.solicitud.id for c in plan.cambios}
    assert capturada.id not in afectadas
    assert any("YA CAPTURADA" in o for o in plan.omitidas)


def probar_la_clase_la_impone_el_tipo():
    """un Proveedor recibe insumo; un Acreedor, concepto"""
    lote, ana, _b, _c, prov, _ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA")
    por_id = {c.solicitud.id: c for c in plan.cambios}
    assert por_id[prov.id].partidas_despues[-1].clase == INSUMO
    assert por_id[ana.id].partidas_despues[-1].clase == CONCEPTO


def probar_tomar_el_total_entiende_el_importe_implicito():
    """«tomar el total» usa lo que el CFDI ya implicaba"""
    # `importe_total` se DERIVA de las partidas de la clase que toca, así que una
    # solicitud sin conceptos vale cero. Si «tomar el total» leyera solo ese
    # campo, no serviría para el caso principal.
    lote, ana, bruno, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA",
                               criterio_importe=asignacion.TOTAL_SOLICITUD)
    por_id = {c.solicitud.id: c for c in plan.cambios}
    assert por_id[ana.id].total_despues == 1000.0
    assert por_id[bruno.id].total_despues == 2000.0


def probar_avisa_de_las_partidas_de_cfdi():
    """se advierte antes de tocar lo que timbró el SAT"""
    lote, _a, _b, carla, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, alcance=asignacion.SELECCIONADAS,
                               seleccionadas={carla.id}, nombre="NUEVO",
                               modo=asignacion.REEMPLAZAR)
    cambio = plan.cambios[0]
    assert "CFDI" in cambio.aviso
    assert len(cambio.partidas_despues) == 1, "reemplaza, no agrega"


def probar_alcance_sin_desglose():
    """«sin desglose» se mide contra la clase que le toca a cada una"""
    lote, ana, bruno, carla, prov, _ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, alcance=asignacion.SIN_PARTIDAS,
                               nombre="VIGILANCIA")
    ids = {c.solicitud.id for c in plan.cambios}
    assert ids == {ana.id, bruno.id}, (
        "Carla ya tiene concepto y el Proveedor ya tiene insumo")


def probar_alcance_seleccionadas():
    """solo se toca lo que el usuario marcó"""
    lote, _a, bruno, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, alcance=asignacion.SELECCIONADAS,
                               seleccionadas={bruno.id}, nombre="VIGILANCIA")
    assert [c.solicitud.id for c in plan.cambios] == [bruno.id]


def probar_importe_fijo_y_en_blanco():
    """un importe fijo se aplica igual; en blanco no pasa la validación"""
    lote, ana, *_ = _lote_con_cfdi()
    fijo = asignacion.calcular(
        lote.id, alcance=asignacion.SELECCIONADAS, seleccionadas={ana.id},
        nombre="VIGILANCIA", criterio_importe=asignacion.IMPORTE_FIJO,
        importe_fijo=777.77)
    assert fijo.cambios[0].partidas_despues[-1].importe == 777.77

    blanco = asignacion.calcular(
        lote.id, alcance=asignacion.SELECCIONADAS, seleccionadas={ana.id},
        nombre="VIGILANCIA", criterio_importe=asignacion.EN_BLANCO)
    cambio = blanco.cambios[0]
    assert cambio.partidas_despues[-1].importe == 0.0
    assert not cambio.valido, "un renglón en cero no debe pasar"


def probar_distribuir_no_pierde_centavos():
    """el reparto cuadra al centavo: 100 entre 3 da 33.33 · 33.33 · 33.34"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 100.0, origen="CFDI"),
        comun.concepto("A", 0.0),
        comun.concepto("B", 0.0)])
    plan = asignacion.calcular(
        lote.id, alcance=asignacion.SELECCIONADAS, seleccionadas={s.id},
        nombre="C", criterio_importe=asignacion.DISTRIBUIR)
    importes = [p.importe for p in plan.cambios[0].partidas_despues
                if p.clase == CONCEPTO]
    assert importes == [33.33, 33.33, 33.34], importes
    assert round(sum(importes), 2) == 100.0


def probar_aplicar_y_deshacer():
    """se aplica, se guarda un snapshot y se puede volver atrás"""
    lote, ana, _b, carla, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA")
    resultado = asignacion.aplicar(lote.id, plan)
    assert resultado["aplicadas"] == len(plan.validos)
    assert len(db.listar_partidas(ana.id)) == 2
    assert db.obtener_solicitud(ana.id).importe_total == 1000.0

    snapshot = db.hay_snapshot(lote.id)
    assert snapshot and snapshot["motivo"]

    assert asignacion.deshacer(lote.id)
    assert len(db.listar_partidas(ana.id)) == 1, "vuelve solo el insumo"
    assert db.obtener_solicitud(ana.id).importe_total == 0.0
    assert len(db.listar_partidas(carla.id)) == 1, "su partida de CFDI regresa"
    # El snapshot se consume: deshacer dos veces seguidas no tiene sentido.
    assert not db.hay_snapshot(lote.id)
    assert not asignacion.deshacer(lote.id)


def probar_un_campo_ajeno_no_impide_asignar():
    """faltar la CLABE no bloquea el concepto, pero se informa"""
    # El modal no permite corregir la CLABE, así que negarse a asignar por ella
    # dejaría al usuario sin salida sin evitar ningún riesgo: quien impide que
    # llegue a SIPP es el motor.
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANDREA BARROS GARCIA", cuenta_clabe="",
                        partidas=[comun.insumo("Facturado", 1500.0,
                                               origen="CFDI")])
    plan = asignacion.calcular(lote.id, nombre="PAGO TARJETA CREDITO",
                               criterio_importe=asignacion.TOTAL_SOLICITUD)
    cambio = plan.cambios[0]
    assert cambio.valido, "el desglose queda bien; debe poder aplicarse"
    assert not cambio.completo, "pero la solicitud no queda lista"
    assert any("CLABE" in h.mensaje for h in cambio.pendientes)
    assert not cambio.bloqueantes
    assert plan.incompletos == [cambio] and not plan.completos

    asignacion.aplicar(lote.id, plan)
    partidas = db.listar_partidas(s.id)
    assert [p.concepto_nombre for p in partidas if p.clase == CONCEPTO] == [
        "PAGO TARJETA CREDITO"]
    assert db.obtener_solicitud(s.id).importe_total == 1500.0


def probar_un_desglose_malo_si_bloquea():
    """lo que esta operación sí escribe la sigue bloqueando"""
    lote = comun.lote()
    comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])
    plan = asignacion.calcular(lote.id, nombre="VIGILANCIA",
                               criterio_importe=asignacion.EN_BLANCO)
    cambio = plan.cambios[0]
    assert not cambio.valido
    assert cambio.bloqueantes and all(h.campo == "partidas"
                                      for h in cambio.bloqueantes)
    assert asignacion.aplicar(lote.id, plan)["aplicadas"] == 0


def probar_sin_nombre_no_hay_plan():
    """sin concepto que asignar no se calcula nada"""
    lote, *_ = _lote_con_cfdi()
    plan = asignacion.calcular(lote.id, nombre="   ")
    assert plan.error and not plan.cambios


# --------------------------------------------------------------------------- #
#  La puerta del motor
# --------------------------------------------------------------------------- #
# La contraparte de lo anterior: si la asignación masiva deja pasar una solicitud
# incompleta, alguien tiene que negarse a capturarla. Ese alguien es el motor, y
# es el único punto por el que se pasa a fuerza antes de tocar SIPP. Estas dos
# pruebas van aquí, junto a las que relajan el bloqueo, para que nadie quite una
# sin ver la otra.
def probar_el_motor_no_intenta_lo_incompleto():
    """una solicitud sin CLABE queda en REVISAR y no se abre el navegador"""
    # Si `procesar_lote` llegara a abrir Playwright, esta prueba fallaría por
    # falta de navegador o se colgaría: que devuelva sin tocarlo es la prueba.
    from core import rpa_sipp

    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANDREA BARROS GARCIA", cuenta_clabe="",
                        partidas=[comun.concepto("VIGILANCIA", 1500.0)])
    resumen = rpa_sipp.procesar_lote(
        lote.id, "usuario", "clave", url_login="http://no.se.usa.invalid")

    assert resumen == {"ok": 0, "revisar": 1, "error": 0, "cancelado": False,
                       "detalle": ["ANDREA BARROS GARCIA: datos incompletos, "
                                   "no se intentó."]}
    guardada = db.obtener_solicitud(s.id)
    assert guardada.estado == "REVISAR"
    assert "CLABE" in guardada.error_msg
    assert guardada.intentos == 0, "no se intentó, no se cuenta un intento"
    # El motivo queda en la bitácora, no solo en la pantalla.
    assert any("CLABE" in b.mensaje for b in db.listar_bitacora(s.id))


def probar_el_motor_no_cuenta_como_lista_la_que_salto():
    """lo incompleto no se confunde con lo capturado"""
    lote = comun.lote()
    comun.solicitud(lote.id, "SIN CLABE", cuenta_clabe="",
                    partidas=[comun.concepto("VIGILANCIA", 100.0)])
    comun.solicitud(lote.id, "SIN DESGLOSE", partidas=[])
    comun.solicitud(lote.id, "YA CAPTURADA", estado="GUARDADA",
                    partidas=[comun.concepto("VIGILANCIA", 100.0)])

    from core import rpa_sipp

    resumen = rpa_sipp.procesar_lote(
        lote.id, "usuario", "clave", url_login="http://no.se.usa.invalid")
    assert resumen["revisar"] == 2 and resumen["ok"] == 0
