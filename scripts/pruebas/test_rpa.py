"""Motor RPA: la pausa de «Llenar y esperar» y el cierre del navegador.

No se abre SIPP ni un navegador aquí. Lo que se prueba es la **coordinación**:
qué decide el motor cuando quien revisa responde, y que el estado que queda en
la base cuente la verdad. Lo que necesita el portal se prueba con los
`scripts/prueba_*_stage.py`, contra el ambiente de pruebas y con credenciales.
"""

from __future__ import annotations

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
    """Doble de sesión que imita al portal borrando la fecha n veces."""

    def __init__(self, borrados=0):
        self.borrados = borrados
        self.escrituras = 0
        self.avisos = []
        self.page = type("P", (), {"wait_for_timeout": lambda _s, _ms: None})()

    def llenar(self, _clave, texto, _desc, **_kw):
        self.escrituras += 1
        self.valor = "" if self.escrituras <= self.borrados else texto

    def loc(self, _clave):
        valor = self.valor
        return type("L", (), {"first": type("F", (), {
            "input_value": lambda _s: valor})()})()

    def anotar(self, _paso, mensaje, _nivel=None):
        self.avisos.append(mensaje)


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
    assert any("se borró" in a for a in ses.avisos), "y dejar constancia"


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
