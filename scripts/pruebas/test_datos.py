"""Base de datos, validador y las reglas que deciden el importe.

Aquí viven las pruebas de las reglas que, si se rompen, hacen que se pague mal:
de dónde sale el importe de una solicitud, cuándo dos solicitudes son la misma,
y qué desglose le corresponde a cada tipo de beneficiario.
"""

from __future__ import annotations

import sqlite3

from core import catalogos, db, validador
from core.db import CONCEPTO, INSUMO, Partida, Solicitud
from scripts.pruebas import comun


def probar_inicializar_es_idempotente():
    """inicializar() se puede correr dos veces (la app arranca muchas)"""
    db.inicializar()
    db.inicializar()
    assert db.listar_lotes() == []


def probar_lote_ida_y_vuelta():
    """un lote se guarda y se relee con sus datos"""
    lote = db.guardar_lote(db.Lote(nombre="Agosto", ambiente="PRUEBAS"))
    leido = db.obtener_lote(lote.id)
    assert leido.nombre == "Agosto"
    assert leido.ambiente == "PRUEBAS"


def probar_importe_sale_solo_de_los_conceptos():
    """el importe de un Acreedor ignora los insumos"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.concepto("PAGO PTU", 1500.50),
        comun.concepto("FINIQUITO", 2000.00),
        comun.insumo("Servicio", 9999.00),
    ])
    # SIPP calcula el total sumando los conceptos SELECCIONADOS; los insumos
    # describen qué se compró. Sumar ambos daría el doble.
    assert s.importe_total == 3500.50, s.importe_total


def probar_importe_de_un_proveedor_sale_de_los_insumos():
    """el importe de un Proveedor sale de sus insumos"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "PROVEEDOR SA", tipo="Proveedor", partidas=[
        comun.insumo("A", 300.0), comun.insumo("B", 200.0),
        comun.concepto("Sobrante", 9999.0),
    ])
    assert s.importe_total == 500.0, s.importe_total


def probar_total_desglose_respeta_el_tipo():
    """total_desglose usa la clase que le toca al beneficiario"""
    partidas = [Partida(clase=CONCEPTO, importe=400.0),
                Partida(clase=INSUMO, importe=999.0)]
    assert db.total_desglose("Acreedor", partidas) == 400.0
    assert db.total_desglose("Proveedor", partidas) == 999.0


def probar_clase_de_desglose_por_tipo():
    """cada tipo de beneficiario tiene UN desglose, verificado en stage"""
    assert catalogos.clase_desglose("Proveedor") == INSUMO
    assert catalogos.clase_desglose("Deudor") == CONCEPTO
    assert catalogos.clase_desglose("Acreedor") == CONCEPTO


def probar_clave_cambia_con_las_partidas():
    """cambiar el desglose hace que sea otro pago"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("PAGO PTU", 1000.0)])
    primera = s.clave_idempotencia
    assert len(primera) == 64

    otra = Solicitud(**{**s.__dict__, "id": Solicitud().id})
    otra = db.guardar_solicitud(
        otra, [comun.concepto("PAGO PTU", 1600.0)])
    assert otra.clave_idempotencia != primera
    assert otra.importe_total == 1600.0


def probar_no_se_duplica_dentro_del_lote():
    """dos solicitudes idénticas en el mismo lote se rechazan"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("PAGO PTU", 1000.0)])
    gemela = Solicitud(**{**s.__dict__, "id": Solicitud().id})
    try:
        db.guardar_solicitud(gemela, [comun.concepto("PAGO PTU", 1000.0)])
    except db.ClaveDuplicada as exc:
        assert "este lote" in str(exc)
        return
    raise AssertionError("debió rechazarse por clave duplicada")


def probar_el_mismo_pago_en_otro_lote_si_se_permite():
    """rehacer un lote desde cero es legítimo"""
    # El candado es POR LOTE: la protección real contra pagar dos veces es la
    # consulta a SIPP antes de capturar, no este índice.
    a, b = comun.lote("A"), comun.lote("B")
    partidas = [comun.concepto("PAGO PTU", 1000.0)]
    comun.solicitud(a.id, "ANA LOPEZ", partidas=partidas)
    comun.solicitud(b.id, "ANA LOPEZ",
                    partidas=[comun.concepto("PAGO PTU", 1000.0)])
    assert len(db.listar_solicitudes(a.id)) == 1
    assert len(db.listar_solicitudes(b.id)) == 1


def probar_estado_folio_e_intentos():
    """actualizar_estado persiste el avance del motor"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("PAGO PTU", 1000.0)])
    db.actualizar_estado(s.id, "GUARDADA", folio_sipp="SP-99887",
                         sumar_intento=True)
    tras = db.obtener_solicitud(s.id)
    assert (tras.estado, tras.folio_sipp, tras.intentos) == (
        "GUARDADA", "SP-99887", 1)


def probar_borrado_en_cascada():
    """borrar una solicitud se lleva sus partidas y su bitácora"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("PAGO PTU", 1000.0)])
    db.registrar(s.id, "prueba", "algo")
    db.borrar_solicitud(s.id)
    assert db.obtener_solicitud(s.id) is None
    assert db.listar_partidas(s.id) == []
    assert db.listar_bitacora(s.id) == []


def probar_bitacora_y_resumen():
    """la bitácora guarda niveles y el resumen cuenta por estado"""
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("PAGO PTU", 1000.0)])
    db.registrar(s.id, "login", "Sesión iniciada")
    db.registrar(s.id, "guardar", "Timeout", nivel="ERROR")
    entradas = db.listar_bitacora(s.id)
    assert len(entradas) == 2 and entradas[0].nivel == "ERROR"

    resumen = db.resumen_lote(lote.id)
    assert resumen["total"] == 1 and resumen["importe"] == 1000.0


# --------------------------------------------------------------------------- #
#  Validador
# --------------------------------------------------------------------------- #
def probar_validador_acepta_una_solicitud_completa():
    """una solicitud bien llena no tiene errores"""
    lote = comun.lote()
    s = Solicitud(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA LOPEZ",
        beneficiario_rfc="XAXX010101000",
        beneficiario_correo="a@ejemplo.invalid",
        cuenta_clabe="012345678901234568", cuenta_banco="BBVA",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="20/09/2026", descripcion="Pago")
    hallazgos = validador.validar(s, [comun.concepto("PAGO PTU", 1000.0)])
    assert not validador.hay_errores(hallazgos), validador.resumen(hallazgos)


def probar_validador_señala_lo_que_falta():
    """se reportan TODOS los errores de una vez, no el primero"""
    s = Solicitud(tipo_beneficiario="Acreedor", forma_pago="Transferencia",
                  tipo_gasto="No Deducible", cuenta_clabe="123")
    hallazgos = validador.validar(s, [])
    mensajes = " ".join(h.mensaje for h in hallazgos if h.es_error)
    for esperado in ("empresa", "sucursal", "nombre", "CLABE", "fecha",
                     "conceptos de pago"):
        assert esperado.lower() in mensajes.lower(), (esperado, mensajes)


def probar_clabe_de_18_digitos():
    """una CLABE corta es un error; una de 18 bien formada pasa"""
    lote = comun.lote()
    base = dict(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA",
        beneficiario_rfc="XAXX010101000", forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago="20/09/2026")
    partidas = [comun.concepto("PAGO PTU", 100.0)]

    corta = validador.validar(Solicitud(**base, cuenta_clabe="123"), partidas)
    assert any("18 dígitos" in h.mensaje for h in corta if h.es_error)

    buena = validador.validar(
        Solicitud(**base, cuenta_clabe="012345678901234568"), partidas)
    assert not validador.hay_errores(buena)


def probar_la_clabe_no_pasa_solo_por_medir_18():
    """una CLABE de 18 dígitos con uno cambiado se rechaza, no se encola"""
    # Es el error que ninguna otra regla puede ver: tiene el largo correcto, son
    # todos dígitos y el prefijo es de un banco real. Si pasa de aquí, el
    # siguiente en enterarse es quien no recibió su pago.
    lote = comun.lote()
    base = dict(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA",
        beneficiario_rfc="XAXX010101000", forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago="20/09/2026")
    partidas = [comun.concepto("PAGO PTU", 100.0)]

    # La misma CLABE buena de la prueba anterior con el último dígito cambiado.
    hallazgos = validador.validar(
        Solicitud(**base, cuenta_clabe="012345678901234567"), partidas)
    errores = [h for h in hallazgos if h.es_error]
    assert any(h.campo == "cuenta_clabe" and "verificador" in h.mensaje
               for h in errores), validador.resumen(hallazgos)

    # Y no se acumula con el de longitud: una CLABE mal da UN error, no dos que
    # digan cosas distintas sobre el mismo campo.
    assert sum(1 for h in errores if h.campo == "cuenta_clabe") == 1


def probar_rfc_faltante_es_aviso_no_error():
    """sin RFC solo se avisa: quién ya está en SIPP lo decide el portal"""
    lote = comun.lote()
    s = Solicitud(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA LOPEZ",
        beneficiario_correo="a@ejemplo.invalid",
        cuenta_clabe="012345678901234568", cuenta_banco="BBVA",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="20/09/2026")
    hallazgos = validador.validar(s, [comun.concepto("PAGO PTU", 100.0)])
    assert not validador.hay_errores(hallazgos)
    assert any("RFC" in h.mensaje and not h.es_error for h in hallazgos)


def probar_rfc_mal_formado_si_es_error():
    """un RFC presente pero inválido sí se rechaza"""
    lote = comun.lote()
    s = Solicitud(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA",
        beneficiario_rfc="NO-ES-UN-RFC", cuenta_clabe="012345678901234568",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="20/09/2026")
    hallazgos = validador.validar(s, [comun.concepto("PAGO PTU", 100.0)])
    assert any("RFC" in h.mensaje for h in hallazgos if h.es_error)


def probar_proveedor_necesita_insumos_no_conceptos():
    """a un Proveedor se le piden insumos; los conceptos sobran"""
    lote = comun.lote()
    base = dict(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Proveedor", beneficiario_nombre="PROVEEDOR SA",
        beneficiario_rfc="XAXX010101000", cuenta_clabe="012345678901234568",
        cuenta_banco="BBVA", forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago="20/09/2026")

    con_insumo = validador.validar(Solicitud(**base),
                                   [comun.insumo("Mantenimiento", 2500.0)])
    assert not validador.hay_errores(con_insumo), validador.resumen(con_insumo)

    con_concepto = validador.validar(Solicitud(**base),
                                     [comun.concepto("PAGO PTU", 2500.0)])
    assert validador.hay_errores(con_concepto)


def probar_renglon_de_la_otra_clase_es_solo_aviso():
    """un renglón que SIPP no mostrará se avisa, no se rechaza"""
    lote = comun.lote()
    s = Solicitud(
        lote_id=lote.id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA",
        beneficiario_rfc="XAXX010101000", beneficiario_correo="a@b.invalid",
        cuenta_clabe="012345678901234568", cuenta_banco="BBVA",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="20/09/2026")
    hallazgos = validador.validar(s, [comun.concepto("PAGO PTU", 400.0),
                                      comun.insumo("Sobrante", 999.0)])
    assert not validador.hay_errores(hallazgos)
    assert any("no muestra esa pestaña" in h.mensaje for h in hallazgos)


# --------------------------------------------------------------------------- #
#  Migración del catálogo de conceptos
# --------------------------------------------------------------------------- #
def probar_migracion_del_catalogo_de_conceptos():
    """el catálogo pasa de único por (empresa, nombre) a único por nombre"""
    from core import conceptos

    ruta = db.RUTA_DB
    con = sqlite3.connect(ruta)
    con.execute("DROP TABLE IF EXISTS concepto_pago")
    con.execute("""CREATE TABLE concepto_pago (
        id TEXT PRIMARY KEY, nombre TEXT NOT NULL DEFAULT '',
        empresa TEXT NOT NULL DEFAULT '',
        origen TEXT NOT NULL DEFAULT 'MANUAL',
        nota TEXT NOT NULL DEFAULT '', creado_en TEXT NOT NULL DEFAULT '')""")
    con.execute("CREATE UNIQUE INDEX idx_concepto "
                "ON concepto_pago(empresa, nombre)")
    filas = [
        # El mismo concepto en tres empresas: debe quedar UNO.
        ("1", "VIGILANCIA", "Abastecedora", "SIPP"),
        ("2", "VIGILANCIA", "Aske", "SIPP"),
        ("3", "VIGILANCIA", "Petroplazas", "MANUAL"),
        # El MANUAL se creó primero, pero debe ganar el importado de SIPP.
        ("4", "NO DEDUCIBLE", "Aske", "MANUAL"),
        ("5", "NO DEDUCIBLE", "Abastecedora", "SIPP"),
        ("6", "SOLO MANUAL", "", "MANUAL"),
    ]
    for i, n, e, o in filas:
        con.execute("INSERT INTO concepto_pago "
                    "(id, nombre, empresa, origen, creado_en) "
                    "VALUES (?, ?, ?, ?, '2026-08-01')", (i, n, e, o))
    con.commit()
    con.close()

    db.inicializar()          # aquí ocurre la migración

    lista = conceptos.listar()
    por_nombre = {c.nombre: c for c in lista}
    assert len(lista) == 3, [c.nombre for c in lista]
    assert por_nombre["VIGILANCIA"].origen == "SIPP"
    assert por_nombre["NO DEDUCIBLE"].origen == "SIPP", (
        "debe conservarse el verificado, no el manual más antiguo")

    con = sqlite3.connect(ruta)
    indices = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='concepto_pago'")]
    con.close()
    assert "idx_concepto_nombre" in indices
    assert "idx_concepto" not in indices

    db.inicializar()          # y es idempotente
    assert len(conceptos.listar()) == 3
