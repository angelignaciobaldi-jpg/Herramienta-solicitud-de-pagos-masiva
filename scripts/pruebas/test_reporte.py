"""Reporte final del lote: el reparto por resultado y lo que se lee de cada fila.

Que además se DIBUJE lo cubre `scripts/smoke_render.py`, que abre la ventana.
Aquí se comprueba lo que decide qué ve el usuario: en qué pestaña cae cada
solicitud y qué texto la explica.
"""

from __future__ import annotations

from core.db import Solicitud
from ui.reporte_lote import _GRUPOS, _resultado, agrupar


def _sol(estado: str, nombre: str = "ANA LOPEZ", **extra) -> Solicitud:
    return Solicitud(estado=estado, beneficiario_nombre=nombre, **extra)


def probar_cada_resultado_cae_en_su_pestana():
    """las tres pestañas del resultado son lo que el usuario viene a ver"""
    grupos = agrupar([
        _sol("GUARDADA"), _sol("ENVIADA_AUTORIZAR"),
        _sol("ERROR"),
        _sol("REVISAR"),
    ])
    assert len(grupos["registradas"]) == 2, "guardada y enviada son altas"
    assert len(grupos["error"]) == 1
    assert len(grupos["revisar"]) == 1
    assert not grupos["sin_procesar"]


def probar_lo_que_no_se_intento_no_se_esconde():
    """un lote detenido a la mitad se leería como completo"""
    grupos = agrupar([_sol("PENDIENTE"), _sol("OMITIDA"),
                      _sol("EN_CAPTURA"), _sol("VALIDADA")])
    assert len(grupos["sin_procesar"]) == 4


def probar_un_formulario_lleno_y_sin_guardar_espera_a_una_persona():
    """se llenó de verdad, pero nadie lo ha guardado: no es «sin procesar»"""
    # Es el resultado normal de «Llenar y esperar», y el motor la cuenta como
    # capturada: mandarla al cajón de lo no intentado contradecía al propio
    # lote que la acababa de llenar.
    grupos = agrupar([_sol("LLENADA")])
    assert len(grupos["revisar"]) == 1
    assert not grupos["sin_procesar"]


def probar_el_desglose_siempre_suma_el_total():
    """si una solicitud se cae del reparto, el reporte miente"""
    # Un estado que no exista en ningún grupo —porque el motor gane uno nuevo—
    # tiene que aparecer igual, no desaparecer del reporte.
    solicitudes = [_sol(e) for e in
                   ("GUARDADA", "ERROR", "REVISAR", "PENDIENTE", "INVENTADO")]
    grupos = agrupar(solicitudes)
    assert sum(len(v) for v in grupos.values()) == len(solicitudes)


def probar_todos_los_estados_conocidos_tienen_pestana():
    """un estado del motor sin grupo acabaría en el cajón equivocado"""
    from core.catalogos import ESTADOS

    con_grupo = {e for _c, _t, _i, _col, estados in _GRUPOS for e in estados}
    faltan = [e for e in ESTADOS if e not in con_grupo]
    assert not faltan, f"estados sin pestaña: {faltan}"


def probar_de_una_solicitud_fallida_se_lee_el_motivo():
    """el estado no explica nada; el motivo sí"""
    s = _sol("ERROR", error_msg="SIPP no aceptó la solicitud: falta la fecha")
    assert "falta la fecha" in _resultado(s)


def probar_el_motivo_se_lee_en_un_solo_renglon():
    """la celda de la tabla no crece: un mensaje de varias líneas la desborda"""
    s = _sol("REVISAR", error_msg="Falta el RFC.\nFalta la CLABE.")
    assert "\n" not in _resultado(s)
    assert "Falta el RFC. Falta la CLABE." == _resultado(s)


def probar_una_solicitud_registrada_muestra_su_estado():
    """sin motivo que contar, lo útil es en qué quedó"""
    assert _resultado(_sol("ENVIADA_AUTORIZAR")) == "Enviada a autorizar"
