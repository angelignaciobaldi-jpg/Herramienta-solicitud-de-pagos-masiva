"""Actualizador: qué release se considera «la nueva» y cuándo hay que avanzar.

El updater falló de la peor manera posible —callando—: con todas las releases
marcadas como pre-release, `releases/latest` respondía 404, el error se traducía
a «no hay novedad» y la app anunciaba que ya estaba al día mientras la versión
nueva llevaba días publicada. Estas pruebas fijan las dos reglas que lo evitan:
se miran TODAS las releases, y un fallo de consulta no se disfraza de «al día».
"""

from __future__ import annotations

import json

from core.auto_updater import AutoUpdater, ErrorActualizacion


def _updater(releases, version_actual="0.0.1"):
    """Updater con la respuesta de GitHub ya servida, sin tocar la red."""
    upd = AutoUpdater(token="x", version_actual=version_actual)
    upd._pedir = lambda _url, _accept: json.dumps(releases).encode("utf-8")
    return upd


def _rel(tag, *, draft=False, prerelease=True):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease,
            "assets": [{"name": "Instalador_SolicitudesPago.exe", "id": 1}]}


def probar_una_prerelease_tambien_es_una_version_nueva():
    """es el canal por el que se distribuye: ignorarla es no actualizar nunca"""
    upd = _updater([_rel("0.0.2"), _rel("0.0.1")])
    assert upd.obtener_release_mas_nueva()["tag_name"] == "0.0.2"


def probar_se_elige_la_version_mayor_y_no_la_mas_reciente():
    """publicar un parche de una versión vieja no debe hacer retroceder"""
    # GitHub devuelve la lista por fecha: si el 0.0.9 se publicó ayer y el 0.1.0
    # la semana pasada, quedarse con el primero degradaría a quien ya va delante.
    upd = _updater([_rel("0.0.9"), _rel("0.1.0"), _rel("0.0.1")])
    assert upd.obtener_release_mas_nueva()["tag_name"] == "0.1.0"


def probar_los_borradores_no_cuentan():
    """sus assets no se pueden descargar: elegirlos deja la descarga en nada"""
    upd = _updater([_rel("9.9.9", draft=True), _rel("0.0.2")])
    assert upd.obtener_release_mas_nueva()["tag_name"] == "0.0.2"


def probar_sin_releases_se_dice_el_motivo():
    """un repositorio sin releases no es «ya estás al día»"""
    try:
        _updater([]).obtener_release_mas_nueva()
        assert False, "debió fallar"
    except ErrorActualizacion as exc:
        assert "release" in str(exc).lower()


def probar_la_version_instalada_no_se_reinstala():
    """sin esto la app se actualizaría a sí misma en cada arranque"""
    upd = _updater([_rel("0.0.2")], version_actual="0.0.2")
    assert not upd.hay_version_mas_nueva("0.0.2")


def probar_las_versiones_se_comparan_por_numero_y_no_por_texto():
    """'0.0.10' es MAYOR que '0.0.9', aunque como texto sea menor"""
    upd = _updater([], version_actual="0.0.9")
    assert upd.hay_version_mas_nueva("0.0.10")
    assert not upd.hay_version_mas_nueva("0.0.8")


def probar_la_v_del_tag_no_estorba():
    """los tags se escriben 'v1.2.3' tan a menudo como '1.2.3'"""
    upd = _updater([], version_actual="1.2.3")
    assert upd.hay_version_mas_nueva("v1.2.4")
    assert not upd.hay_version_mas_nueva("v1.2.3")
