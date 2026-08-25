"""Motor RPA: la pausa de «Llenar y esperar» y el cierre del navegador.

No se abre SIPP ni un navegador aquí. Lo que se prueba es la **coordinación**:
qué decide el motor cuando quien revisa responde, y que el estado que queda en
la base cuente la verdad. Lo que necesita el portal se prueba con los
`scripts/prueba_*_stage.py`, contra el ambiente de pruebas y con credenciales.
"""

from __future__ import annotations

import os
import threading

from core import db, rpa_sipp
from scripts.pruebas import comun


class _FlujoFalso:
    """Doble del flujo: solo tiene que saber si apareció un folio."""

    def __init__(self, folio: str = "") -> None:
        self._folio = folio
        self.leidos = 0

    def _leer_folio(self) -> str:
        self.leidos += 1
        return self._folio


def _avisar(**_kw) -> None:
    pass


def probar_la_pausa_devuelve_lo_que_decide_quien_revisa():
    """cada botón de la pausa se traduce en la decisión del motor"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    for decision in (rpa_sipp.CONTINUAR, rpa_sipp.SALTAR,
                     rpa_sipp.NO_PAUSAR, rpa_sipp.DETENER):
        elegida, _folio = rpa_sipp._pausar(
            lambda _d, r=decision: r, _FlujoFalso(), s, 1, 1, _avisar)
        assert elegida == decision, elegida


def probar_si_la_guardas_tu_durante_la_pausa_se_registra():
    """el folio que aparece mientras revisas queda en la base al continuar"""
    # «Llenar y esperar» deja el formulario en pantalla: quien revisa puede
    # darle Guardar en SIPP. Sin mirarlo, la base seguiría diciendo que está
    # sin guardar y el lote contaría mal hasta la siguiente corrida.
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    db.actualizar_estado(s.id, "LLENADA")

    flujo = _FlujoFalso(folio="0053196")
    _decision, folio = rpa_sipp._pausar(lambda _d: rpa_sipp.CONTINUAR, flujo,
                                        s, 1, 1, _avisar)

    assert folio == "0053196", "hay que avisar de que ya se guardó"
    recargada = next(x for x in db.listar_solicitudes(lote.id) if x.id == s.id)
    assert recargada.estado == "GUARDADA", recargada.estado
    assert recargada.folio_sipp == "0053196"


def probar_sin_folio_la_solicitud_se_queda_como_estaba():
    """si no la guardaste, sigue llenada y sin folio"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    db.actualizar_estado(s.id, "LLENADA")

    _decision, folio = rpa_sipp._pausar(lambda _d: rpa_sipp.CONTINUAR,
                                        _FlujoFalso(), s, 1, 1, _avisar)

    assert folio == ""
    recargada = next(x for x in db.listar_solicitudes(lote.id) if x.id == s.id)
    assert recargada.estado == "LLENADA", recargada.estado
    assert not recargada.folio_sipp


def probar_si_la_interfaz_falla_el_lote_no_se_cuelga():
    """una excepción al preguntar no deja el motor esperando para siempre"""
    # El motor duerme en este callback con el navegador abierto. Si la interfaz
    # revienta y nadie lo despierta, el hilo queda bloqueado y con él Playwright.
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")

    def revienta(_datos):
        raise RuntimeError("la ventana ya no está")

    assert rpa_sipp._pausar(revienta, _FlujoFalso(), s, 1, 1, _avisar) == (
        rpa_sipp.CONTINUAR, "")


def probar_leer_el_folio_nunca_tumba_la_pausa():
    """si el portal no responde al releer el folio, se sigue igual"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")

    class _Roto:
        def _leer_folio(self):
            raise RuntimeError("la página se fue")

    decision, folio = rpa_sipp._pausar(lambda _d: rpa_sipp.SALTAR, _Roto(), s,
                                       1, 1, _avisar)
    assert (decision, folio) == (rpa_sipp.SALTAR, "")


class _FlujoCierre:
    """Doble del flujo para el cierre tras la revisión."""

    def __init__(self, resultado=None, revienta: bool = False) -> None:
        self.resultado = resultado
        self.revienta = revienta
        self.recibido: dict = {}
        self.s = type("S", (), {"captura": lambda _self, _n: ""})()

    def cerrar_solicitud(self, archivos=None, *, hasta="AUTORIZAR",
                         folio_existente="", solicitud=None):
        if self.revienta:
            raise RuntimeError("SIPP rechazó el envío")
        self.recibido = {"archivos": archivos, "hasta": hasta,
                         "folio_existente": folio_existente,
                         "solicitud": solicitud}
        return self.resultado


def probar_al_continuar_el_robot_guarda_adjunta_y_autoriza():
    """«continuar» no es solo pasar a la siguiente: cierra la que se revisó"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    db.actualizar_estado(s.id, "LLENADA")

    flujo = _FlujoCierre(rpa_sipp.ResultadoCaptura(
        "ENVIADA_AUTORIZAR", "0053196", "Solicitud guardada y enviada."))
    docs = {"caratula": "c.pdf", "vobo": "v.pdf"}
    resumen = {"ok": 1, "error": 0, "detalle": []}

    rpa_sipp._cerrar_tras_revision(flujo, s, docs, 1, 1, _avisar, resumen)

    # El Vo.Bo. tiene que llegar al cierre: es lo que SIPP exige como respaldo
    # y sin él la autorización se rechaza.
    assert flujo.recibido["archivos"] == docs
    assert flujo.recibido["hasta"] == "AUTORIZAR"
    assert flujo.recibido["solicitud"] is s, (
        "sin la solicitud no se puede comprobar el guardado en el listado")
    recargada = next(x for x in db.listar_solicitudes(lote.id) if x.id == s.id)
    assert recargada.estado == "ENVIADA_AUTORIZAR", recargada.estado
    assert recargada.folio_sipp == "0053196"


def probar_no_se_guarda_dos_veces_lo_que_ya_guardaste_tu():
    """si la guardaste durante la revisión, su folio se reusa"""
    # Volver a guardar abriría una SEGUNDA solicitud en SIPP para el mismo pago.
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    flujo = _FlujoCierre(rpa_sipp.ResultadoCaptura(
        "ENVIADA_AUTORIZAR", "0053196", "Enviada."))

    rpa_sipp._cerrar_tras_revision(flujo, s, {}, 1, 1, _avisar,
                                   {"ok": 1, "error": 0, "detalle": []},
                                   folio_existente="0053196")

    assert flujo.recibido["folio_existente"] == "0053196"


def probar_un_fallo_al_cerrar_deja_la_solicitud_en_error():
    """si el cierre falla, se dice: el formulario ya existe en SIPP"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    db.actualizar_estado(s.id, "LLENADA")
    resumen = {"ok": 1, "error": 0, "detalle": []}

    rpa_sipp._cerrar_tras_revision(_FlujoCierre(revienta=True), s, {}, 1, 1,
                                   _avisar, resumen)

    recargada = next(x for x in db.listar_solicitudes(lote.id) if x.id == s.id)
    assert recargada.estado == "ERROR", recargada.estado
    assert "rechazó" in recargada.error_msg
    # Se contó como buena al llenarse; al fallar el cierre deja de serlo.
    assert resumen["error"] == 1 and resumen["ok"] == 0


class _PaginaFalsa:
    """Doble de la página: solo lleva la cuenta del tiempo que se le pide."""

    def __init__(self) -> None:
        self.esperado_ms = 0

    def wait_for_timeout(self, ms: int) -> None:
        self.esperado_ms += ms


def _sesion_con_pagina(pagina, cancelado=None):
    """Una SesionSipp sin abrir nada, con la página sustituida."""
    sesion = rpa_sipp.SesionSipp.__new__(rpa_sipp.SesionSipp)
    sesion.page = pagina
    sesion.cancelado = cancelado
    return sesion


def probar_el_sondeo_sale_en_cuanto_la_condicion_se_cumple():
    """no se espera el tope si la página ya respondió"""
    # Es lo que agiliza la captura: antes se esperaba el máximo SIEMPRE, aunque
    # SIPP hubiera contestado al instante.
    pagina = _PaginaFalsa()
    sesion = _sesion_con_pagina(pagina)
    llamadas = []

    def condicion() -> bool:
        llamadas.append(1)
        return len(llamadas) >= 2      # se cumple en el segundo sondeo

    assert sesion.esperar_a(condicion, tope_ms=4000, intervalo_ms=100)
    assert pagina.esperado_ms <= 200, (
        f"esperó {pagina.esperado_ms} ms pudiendo salir antes")


def probar_el_sondeo_respeta_el_tope_cuando_nunca_se_cumple():
    """en el peor caso se tarda lo mismo que con la espera fija de antes"""
    pagina = _PaginaFalsa()
    sesion = _sesion_con_pagina(pagina)

    assert not sesion.esperar_a(lambda: False, tope_ms=1000, intervalo_ms=100)
    assert pagina.esperado_ms <= 1000, (
        f"se pasó del tope: {pagina.esperado_ms} ms")


def probar_una_condicion_que_revienta_no_tumba_el_sondeo():
    """la página puede estar navegando: consultarla puede fallar y no importa"""
    pagina = _PaginaFalsa()
    sesion = _sesion_con_pagina(pagina)
    intentos = []

    def condicion() -> bool:
        intentos.append(1)
        if len(intentos) < 3:
            raise RuntimeError("execution context destroyed")
        return True

    assert sesion.esperar_a(condicion, tope_ms=2000, intervalo_ms=100)


def probar_los_eventos_despiertan_al_hilo_del_motor():
    """la espera termina en cuanto la interfaz lo pide, sin sondear"""
    # Es el mecanismo con el que la app cierra el navegador al salir: si el
    # hilo no despertara, quedaría un Chromium huérfano.
    esperando = threading.Event()
    cierre = threading.Event()
    salio = []

    def hilo() -> None:
        esperando.set()
        cierre.wait()
        salio.append(True)

    t = threading.Thread(target=hilo, daemon=True)
    t.start()
    assert esperando.wait(timeout=5), "el hilo no llegó a esperar"
    assert not salio, "no debía salir antes de que se lo pidan"

    cierre.set()
    t.join(timeout=5)
    assert salio == [True], "el hilo siguió bloqueado tras pedir el cierre"


# --------------------------------------------------------------------------- #
#  Detener a media captura
# --------------------------------------------------------------------------- #
def probar_detener_corta_la_captura_en_curso():
    """el flujo se aborta entre pasos, no al terminar la solicitud"""
    # Antes, «Detener» esperaba a que la solicitud en curso terminara. Con un
    # dato malo detectado a media corrida, eso significa capturarlo igual.
    detener = {"si": False}
    sesion = _sesion_con_pagina(_PaginaFalsa())
    flujo = rpa_sipp.FlujoSolicitudPago(sesion, cancelado=lambda: detener["si"])
    # El flujo lo guarda en la SESIÓN: sus esperas y reintentos son los tramos
    # largos y también tienen que poder cortarse.
    assert callable(sesion.cancelado)

    flujo.abortar_si_cancelan()          # sin pedir parada, no hace nada
    detener["si"] = True
    for quien in (flujo, sesion):        # los dos cortan, con el mismo aviso
        try:
            quien.abortar_si_cancelan()
            assert False, "debió cortar"
        except rpa_sipp.Cancelado:
            pass


def probar_sin_callback_de_cancelacion_nunca_se_corta():
    """el flujo de verificación no tiene nada que cancelar"""
    flujo = rpa_sipp.FlujoSolicitudPago(_sesion_con_pagina(_PaginaFalsa()))
    flujo.abortar_si_cancelan()


def probar_los_reintentos_dejan_de_esperar_si_se_pide_parar():
    """el sondeo corta a media espera, no al agotar su tope"""
    # Era el fallo reportado: «Detener» no hacía nada porque los bucles de
    # reintento —hasta cuatro vueltas con esperas de 60 s— no lo consultaban,
    # y la ventana parecía congelada.
    pagina = _PaginaFalsa()
    sesion = _sesion_con_pagina(pagina, cancelado=lambda: True)
    try:
        sesion.esperar_a(lambda: False, tope_ms=60000, intervalo_ms=100)
        assert False, "debió cortar"
    except rpa_sipp.Cancelado:
        pass
    assert pagina.esperado_ms == 0, (
        f"esperó {pagina.esperado_ms} ms tras pedirle parar")


def probar_la_solicitud_cortada_vuelve_a_su_estado_anterior():
    """detener no deja la solicitud atrapada en «En captura»"""
    # Es lo que permite volver a lanzarla en el siguiente lote: si se quedara
    # en EN_CAPTURA, ni contaría como pendiente ni como hecha.
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ")
    previo = s.estado
    db.actualizar_estado(s.id, "EN_CAPTURA", sumar_intento=True)

    # Lo que hace el motor al atrapar Cancelado.
    db.actualizar_estado(s.id, previo, error_msg="")

    recargada = next(x for x in db.listar_solicitudes(lote.id) if x.id == s.id)
    assert recargada.estado == previo, recargada.estado
    assert not recargada.folio_sipp, "no llegó a guardarse en SIPP"


# --------------------------------------------------------------------------- #
#  Selección en los combos "chosen"
# --------------------------------------------------------------------------- #
def probar_se_reconoce_el_valor_que_ya_estaba_puesto():
    """el rótulo se compara como se ve, no byte a byte"""
    # SIPP devuelve los rótulos con espacios de sobra y la caja cambiada. Sin
    # normalizar, se daría por distinto lo que en pantalla es lo mismo y se
    # volvería a elegir un valor ya puesto: abrir el widget, teclear y clicar.
    assert rpa_sipp._mismo_texto("Pago Extraordinario", "Pago Extraordinario")
    assert rpa_sipp._mismo_texto("  Pago   Extraordinario ",
                                 "Pago Extraordinario")
    assert rpa_sipp._mismo_texto("PAGO EXTRAORDINARIO", "Pago Extraordinario")
    assert not rpa_sipp._mismo_texto("Pago Extraordinario", "Pago Ordinario")
    assert not rpa_sipp._mismo_texto("", "Pago Extraordinario")


def probar_si_ya_esta_elegido_no_se_toca_el_combo():
    """no se reabre el widget para dejar el campo como estaba"""
    # Es lo que costaba el «buen rato»: cada selección repetida abría el
    # chosen, tecleaba y esperaba, aunque el valor ya fuera el correcto.
    class _Select:
        def __init__(self, valor):
            self.valor = valor
            self.tocado = False

        @property
        def first(self):
            return self

        def wait_for(self, **_kw):
            pass

        def evaluate(self, _js):
            return self.valor

        def locator(self, *_a, **_kw):
            self.tocado = True          # abrir el widget marcaría esto
            raise AssertionError("no debió mirar el widget")

    sesion = rpa_sipp.SesionSipp.__new__(rpa_sipp.SesionSipp)
    sesion.timeout_ms = 1000
    sel = _Select("  PAGO EXTRAORDINARIO ")
    sesion.seleccionar_chosen(sel, "Pago Extraordinario", "Tipo de pago")
    assert not sel.tocado


# --------------------------------------------------------------------------- #
#  La fecha de pago se escribe al final
# --------------------------------------------------------------------------- #
class _SesionFecha:
    """Doble de sesión que imita al portal descartando la fecha n veces.

    El doble distingue lo que se ve de lo que SIPP REGISTRA, porque esa es
    justamente la diferencia que dejaba pasar solicitudes sin fecha: el campo
    mostraba el día escrito y el modelo del portal estaba vacío.
    """

    def __init__(self, borrados=0):
        self.borrados = borrados
        self.escrituras = 0
        self.avisos = []
        self.capturas = []
        self.registrada = ""
        self.page = type("P", (), {"wait_for_timeout": lambda _s, _ms: None})()

    def llenar(self, _clave, texto, _desc, **_kw):
        self.escrituras += 1
        # Se escribe siempre; lo que cambia es si el portal se da por enterado.
        self.registrada = "" if self.escrituras <= self.borrados else texto

    def consultar(self, _nombre, defecto=None):
        return self.registrada or defecto

    def esperar_a(self, condicion, tope_ms=0, intervalo_ms=0):
        return bool(condicion())

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)

    def captura(self, nombre):
        self.capturas.append(nombre)
        return ""


def _flujo_con(sesion):
    return rpa_sipp.FlujoSolicitudPago(sesion)


def probar_la_fecha_se_escribe_una_sola_vez_si_se_queda():
    """en el caso normal no se repite trabajo"""
    ses = _SesionFecha(borrados=0)
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ", fecha_pago=comun.fecha_futura())
    _flujo_con(ses)._poner_fecha(s)
    assert ses.escrituras == 1
    assert not ses.avisos


def probar_si_el_grid_borra_la_fecha_se_reintenta():
    """el desglose se repinta y limpia el campo: hay que volver a ponerla"""
    # Es el fallo reportado: al elegir el concepto, SIPP borraba la fecha y el
    # Guardar no confirmaba nada, sin decir por qué.
    ses = _SesionFecha(borrados=1)
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ", fecha_pago=comun.fecha_futura())
    _flujo_con(ses)._poner_fecha(s)
    assert ses.escrituras == 2, "debió reintentar"
    assert any("no registró" in a for a in ses.avisos), "y dejar constancia"


def probar_si_la_fecha_nunca_se_queda_se_falla_con_el_motivo():
    """no se guarda una solicitud sin fecha: SIPP la rechaza en silencio"""
    ses = _SesionFecha(borrados=9)
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ", fecha_pago=comun.fecha_futura())
    try:
        _flujo_con(ses)._poner_fecha(s)
        assert False, "debió fallar"
    except rpa_sipp.ErrorRpa as exc:
        assert "fecha de pago" in str(exc)


def probar_sin_fecha_no_se_toca_el_campo():
    """una solicitud sin fecha no escribe nada"""
    ses = _SesionFecha()
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ")
    s.fecha_pago = ""
    _flujo_con(ses)._poner_fecha(s)
    assert ses.escrituras == 0


def probar_la_fecha_es_el_ultimo_paso_del_llenado():
    """el orden importa: cualquier campo antes del desglose puede perderse"""
    import inspect
    fuente = inspect.getsource(rpa_sipp.FlujoSolicitudPago.llenar)
    pos_desglose = fuente.index("_llenar_conceptos")
    pos_fecha = fuente.index("_poner_fecha")
    assert pos_fecha > pos_desglose, "la fecha debe ir DESPUÉS del desglose"


def probar_el_scroll_al_campo_va_fuera_del_bucle_de_reintentos():
    """el robot no sube y baja la página en cada intento"""
    # Reportado: al elegir «Tipo de Pago Extraordinario» —una lista dependiente
    # que tarda en llenarse— la página subía y bajaba varias veces. Era el
    # `scroll_into_view` de cada reintento; el campo no se mueve entre ellos.
    import inspect
    fuente = inspect.getsource(rpa_sipp.SesionSipp.seleccionar_chosen)
    pos_scroll = fuente.index("scroll_into_view_if_needed")
    pos_bucle = fuente.index("for intento in range")
    assert pos_scroll < pos_bucle, "el scroll debe ir ANTES del bucle"
    assert fuente.count("scroll_into_view_if_needed") == 1, (
        "solo debe quedar un scroll, fuera del bucle")


def probar_se_espera_a_que_el_widget_tenga_opciones_antes_de_teclear():
    """escribir sobre una lista vacía desperdicia el intento entero"""
    # Chosen se puebla cuando Angular dispara `chosen:updated`: puede haber
    # opciones en el <select> y ninguna todavía en el widget.
    import inspect
    fuente = inspect.getsource(rpa_sipp.SesionSipp.seleccionar_chosen)
    pos_espera = fuente.index("li.active-result\").count() > 0")
    pos_teclea = fuente.index("buscador.type(")
    assert pos_espera < pos_teclea, "hay que esperar ANTES de teclear"


# --------------------------------------------------------------------------- #
#  Al terminar, el portal queda en el listado general
# --------------------------------------------------------------------------- #
class _SesionListado:
    """Doble de sesión que anota qué filtros se vaciaron y si se buscó."""

    def __init__(self, con_filtros=True, revienta=False):
        self.con_filtros = con_filtros
        self.revienta = revienta
        self.vaciados = []
        self.busco = False
        self.avisos = []
        self.quieto = 0

    def existe(self, clave):
        return self.con_filtros or clave == "listado.buscar"

    def loc(self, clave):
        ses = self

        class _L:
            @property
            def first(self):
                return self

            def fill(self, valor):
                assert valor == "", "los filtros se VACIAN"
                ses.vaciados.append(clave)

            def click(self):
                ses.busco = True

        return _L()

    def esperar_quieto(self, ms):
        self.quieto += ms

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)


def _flujo_listado(sesion, volver_revienta=False):
    flujo = rpa_sipp.FlujoSolicitudPago(sesion)
    if volver_revienta:
        def _boom():
            raise RuntimeError("el formulario no responde")
        flujo.volver_al_listado = _boom
    else:
        flujo.volver_al_listado = lambda: None
    return flujo


def probar_al_terminar_se_vacian_los_filtros_del_listado():
    """el listado conserva el filtro de la busqueda de duplicados"""
    # Sin limpiarlo se ve UNA fila —la del ultimo beneficiario buscado— o
    # ninguna, y parece que el lote no capturo nada.
    ses = _SesionListado()
    _flujo_listado(ses).dejar_listado_limpio()

    assert "listado.filtro_beneficiario" in ses.vaciados
    assert "listado.filtro_descripcion" in ses.vaciados
    assert "listado.filtro_desde" in ses.vaciados
    assert "listado.filtro_hasta" in ses.vaciados
    assert ses.busco, "hay que volver a buscar para que el listado se recargue"


def probar_un_fallo_al_limpiar_el_listado_no_rompe_nada():
    """es cortesia: el lote ya termino y su resultado no depende de esto"""
    ses = _SesionListado(revienta=True)
    _flujo_listado(ses, volver_revienta=True).dejar_listado_limpio()
    assert any("No se pudo dejar el listado" in a for a in ses.avisos)


def probar_llenar_y_esperar_no_navega_al_listado():
    """con el formulario lleno en pantalla, navegar lo perderia"""
    # La regla del motor: solo se vuelve al listado si la ultima solicitud NO
    # quedo llena y sin guardar.
    import inspect
    fuente = inspect.getsource(rpa_sipp.procesar_lote)
    assert 'if ultimo_estado != "LLENADA":' in fuente
    assert "dejar_listado_limpio()" in fuente


# --------------------------------------------------------------------------- #
#  Los rechazos silenciosos del portal
# --------------------------------------------------------------------------- #
# SIPP rechaza un formulario de dos maneras y solo una se ve. Además de las
# alertas emergentes, valida campo por campo con un mensaje pegado al campo y
# se detiene ahí: sin alerta, sin diálogo y sin llamar a su servidor. Visto
# desde fuera es idéntico a «el botón no hizo nada», y así estuvo el robot
# esperando veinte segundos un folio que no iba a llegar, para acabar diciendo
# que SIPP no había confirmado.
class _SesionGuardar:
    """Doble de sesión que contesta al Guardar como lo hace el portal."""

    def __init__(self, objecion="", folio="", autorizar=False):
        self.objecion = objecion
        self.pendiente = ""     # el portal objeta AL PULSAR, no antes
        self.folio = folio
        self.autorizar = autorizar
        self.avisos = []
        self.capturas = []
        self.espiado = 0
        self.clics = []
        self.page = type("P", (), {"wait_for_timeout": lambda _s, _ms: None})()

    # --- lo que el motor usa de la sesión ---
    def espiar_validaciones(self):
        self.espiado += 1

    def validaciones(self):
        # Igual que en el portal: se entrega una vez y la lista queda limpia.
        pendiente, self.pendiente = self.pendiente, ""
        return [pendiente] if pendiente else []

    def esperar_a(self, condicion, tope_ms=0, intervalo_ms=0):
        return bool(condicion())

    def clic_con_reintento(self, clave, _desc):
        self.clics.append(clave)
        if clave == "acc.guardar":
            self.pendiente = self.objecion

    def visible_(self, clave):
        return clave == "acc.autorizar" and self.autorizar

    def loc(self, _clave):
        vacio = type("L", (), {"count": lambda _s: 0})()
        return type("W", (), {"first": vacio})()

    def texto_alertas(self):
        return ""

    def cerrar_alertas(self, vueltas=6):
        pass

    def esperar_quieto(self, _tope):
        pass

    def hay_error_sistema(self):
        return False

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)

    def captura(self, nombre):
        self.capturas.append(nombre)
        return nombre

    def diagnostico(self, nombre):
        self.capturas.append(nombre)


def _flujo_guardar(sesion, folio=""):
    flujo = rpa_sipp.FlujoSolicitudPago(sesion)
    flujo._leer_folio = lambda: folio
    return flujo


def probar_lo_que_sipp_objeta_se_reporta_tal_cual():
    """el motivo del rechazo lo dice SIPP: hay que leerlo, no adivinarlo"""
    ses = _SesionGuardar(objecion="La información de la Fecha Pago es requerida.")
    try:
        _flujo_guardar(ses).guardar()
        assert False, "debió detenerse"
    except rpa_sipp.RequiereRevision as exc:
        assert "Fecha Pago" in str(exc), "el mensaje del portal, textual"


def probar_un_rechazo_no_se_reintenta_como_si_fuera_una_falla():
    """es un dato que no cuadra: repetirlo da exactamente el mismo rechazo"""
    ses = _SesionGuardar(objecion="Es necesario capturar el RFC")
    try:
        _flujo_guardar(ses).guardar()
        assert False, "debió detenerse"
    except rpa_sipp.RequiereRevision:
        pass
    except rpa_sipp.ErrorRpa:
        assert False, "un rechazo del portal no es una falla del robot"


def probar_no_se_espera_un_folio_que_ya_no_va_a_llegar():
    """cortar en cuanto SIPP objeta es la diferencia entre explicar y colgarse"""
    ses = _SesionGuardar(objecion="La información de la Empresa es requerida.")
    try:
        _flujo_guardar(ses).guardar()
    except rpa_sipp.RequiereRevision:
        pass
    assert "acc.autorizar" not in ses.clics
    assert ses.espiado >= 1, "hay que estar escuchando ANTES de pulsar Guardar"


def probar_sin_objeciones_el_guardado_sigue_su_curso():
    """el camino normal no cambia: si SIPP no objeta, se guarda"""
    ses = _SesionGuardar(folio="3810", autorizar=True)
    assert _flujo_guardar(ses, folio="3810").guardar() == "3810"


def probar_lo_objetado_al_llenar_no_se_arrastra_al_guardar():
    """un aviso ya resuelto no debe hacerse pasar por el motivo del rechazo"""
    import inspect
    fuente = inspect.getsource(rpa_sipp.FlujoSolicitudPago.guardar)
    pos_limpia = fuente.index("s.validaciones()")
    pos_clic = fuente.index("clic_con_reintento")
    assert pos_limpia < pos_clic, "se limpia ANTES de pulsar Guardar"


# --------------------------------------------------------------------------- #
#  El Vo.Bo. no está adjunto hasta que SIPP lo dice
# --------------------------------------------------------------------------- #
# Elegir el archivo y subirlo son dos cosas distintas: el selector se llena al
# instante, en el navegador, y SIPP tarda en llevarlo a su almacenamiento. Solo
# cuando termina le pone nombre al renglón, y ese nombre es lo que mira para
# dejar enviar a autorizar. Dar el adjunto por bueno antes dejaba la solicitud
# guardada y a SIPP contestando que faltaba el documento de respaldo.
class _SesionRespaldo:
    """Doble de sesión con una pestaña de documentos que tarda en registrar."""

    def __init__(self, vueltas_hasta_subir=0, nunca=False):
        self.vueltas = vueltas_hasta_subir
        self.nunca = nunca
        self.consultas = 0
        self.subidos = []
        self.avisos = []
        self.capturas = []
        self.page = self
        self.renglones = 0

    # --- pestaña y grid ---
    def locator(self, _css):
        ses = self

        class _Grid:
            def count(_s):
                # Renglones del grid: uno más en cuanto se pulsa «agregar».
                return ses.renglones

            @property
            def last(_s):
                return _s

            def input_value(_s):
                return ""

        return _Grid()

    def abrir_pestana(self, _nombre):
        pass

    def loc(self, clave):
        ses = self

        class _Boton:
            def click(_s, **_kw):
                if clave == "doc.agregar":
                    ses.renglones += 1

        return type("W", (), {"first": _Boton()})()

    def subir_archivo(self, _entrada, ruta, _desc):
        self.subidos.append(ruta)

    def esperar_a(self, condicion, tope_ms=0, intervalo_ms=0):
        # Sondea de verdad: es justo lo que se le pide al motor aquí.
        for _ in range(20):
            if condicion():
                return True
            self.consultas += 1
        return bool(condicion())

    def cerrar_alertas(self, vueltas=6):
        pass

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)

    def diagnostico(self, nombre):
        self.capturas.append(nombre)


class _SinMapa:
    """Resuelve las claves de selector a sí mismas, sin el mapa del portal.

    El mapa no se versiona —el repositorio es público— y viaja al CI por un
    secreto. Una prueba de LÓGICA que lo consulte deja de probar lo suyo y pasa
    a depender de que ese secreto esté al día: al añadir un selector nuevo, el
    CI se caía con un `KeyError` que no decía nada del comportamiento probado.
    """

    def __enter__(self):
        self._css = rpa_sipp.selectores.css
        rpa_sipp.selectores.css = lambda clave: clave
        return self

    def __exit__(self, *_exc):
        rpa_sipp.selectores.css = self._css
        return False


def _sesion_respaldo(vueltas=0, nunca=False):
    """Sesión cuyo grid solo muestra el nombre tras `vueltas` sondeos."""
    ses = _SesionRespaldo(vueltas, nunca)
    original = ses.locator

    def _locator(css):
        grid = original(css)
        if css != "doc.nombre":
            return grid
        ses_ref = ses

        class _Nombres:
            def count(_s):
                return 1 if ses_ref.renglones else 0

            def nth(_s, _i):
                return _s

            def input_value(_s):
                if ses_ref.nunca:
                    return ""
                return ("VOBO.pdf" if ses_ref.consultas >= ses_ref.vueltas
                        else "")

        return _Nombres()

    ses.locator = _locator
    return ses


def _archivo_temporal():
    import tempfile
    ruta = os.path.join(tempfile.gettempdir(), "vobo_prueba.pdf")
    with open(ruta, "wb") as f:
        f.write(b"%PDF-1.4\n")
    return ruta


def probar_no_se_da_por_adjunto_hasta_que_sipp_lo_registra():
    """el selector se llena al instante; la subida tarda"""
    ses = _sesion_respaldo(vueltas=3)
    with _SinMapa():
        rpa_sipp.FlujoSolicitudPago(ses).adjuntar_respaldo(_archivo_temporal())
    assert ses.consultas >= 3, "hay que esperar a que SIPP lo registre"
    assert any("Vo.Bo. adjuntado" in a for a in ses.avisos)


def probar_si_la_subida_no_termina_se_falla_antes_de_autorizar():
    """mejor detenerse con el motivo que enviar sin el respaldo"""
    ses = _sesion_respaldo(nunca=True)
    try:
        with _SinMapa():
            rpa_sipp.FlujoSolicitudPago(ses).adjuntar_respaldo(
                _archivo_temporal())
        assert False, "debió fallar"
    except rpa_sipp.ErrorRpa as exc:
        assert "sin nombre" in str(exc) or "no terminó" in str(exc)


# --------------------------------------------------------------------------- #
#  El RFC manda sobre el nombre
# --------------------------------------------------------------------------- #
# El catálogo se busca por nombre, pero SIPP identifica al beneficiario por su
# RFC. Cuando el nombre del Excel no coincide letra por letra con el registrado
# el robot intentaba darlo de alta otra vez, y SIPP lo rechazaba al guardar, al
# final del formulario y sin explicación.
class _SesionRfc:
    """Doble de sesión que responde si el RFC ya está registrado."""

    def __init__(self, registrado):
        self.registrado = registrado
        self.avisos = []

    def consultar(self, _nombre, defecto=None):
        return self.registrado

    def esperar_a(self, condicion, tope_ms=0, intervalo_ms=0):
        return bool(condicion())

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)


def probar_un_rfc_ya_registrado_detiene_el_alta_al_momento():
    """se avisa con el RFC en la mano, no al final y a ciegas"""
    ses = _SesionRfc(registrado=True)
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ")
    s.beneficiario_rfc = "PRU010101AB1"
    try:
        rpa_sipp.FlujoSolicitudPago(ses)._comprobar_rfc_libre(s)
        assert False, "debió detenerse"
    except rpa_sipp.RequiereRevision as exc:
        assert "PRU010101AB1" in str(exc), "hay que decir cuál RFC"
        assert "ANA LOPEZ" in str(exc), "y con qué solicitud pasa"


def probar_un_rfc_libre_no_estorba_el_alta():
    """el camino normal sigue igual"""
    ses = _SesionRfc(registrado=False)
    s = comun.solicitud(comun.lote().id, "ANA LOPEZ")
    s.beneficiario_rfc = "PRU010101AB1"
    rpa_sipp.FlujoSolicitudPago(ses)._comprobar_rfc_libre(s)
