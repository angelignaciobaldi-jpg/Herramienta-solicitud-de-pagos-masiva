"""Lógica de las pantallas y los modales, contra una página simulada.

Estas pruebas comprueban el COMPORTAMIENTO: que la vista previa no se pueda
saltar, que el editor cambie según el tipo de beneficiario, que un guardado
incompleto se bloquee. Que además se DIBUJEN es otra cosa, y la cubre
`scripts/smoke_render.py`, que abre la ventana de verdad.
"""

from __future__ import annotations

import asyncio
import os

from openpyxl import load_workbook

from core import asignacion, conceptos, db, documentos, plantilla_excel
from core.db import CONCEPTO, INSUMO
from scripts.pruebas import comun


def _correr(corrutina):
    """Ejecuta una corrutina en las pruebas que tocan métodos async."""
    return asyncio.run(corrutina)


# --------------------------------------------------------------------------- #
#  Pantallas
# --------------------------------------------------------------------------- #
def probar_las_pantallas_se_construyen():
    """cada pantalla se arma y expone su contenido sin abrir ventana"""
    from ui.bitacora import SeccionBitacora
    from ui.conceptos import SeccionConceptos
    from ui.documentos import SeccionDocumentos
    from ui.solicitudes import SeccionSolicitudes

    app = comun.AppFalsa()
    for clase in (SeccionDocumentos, SeccionSolicitudes, SeccionConceptos,
                  SeccionBitacora):
        pantalla = clase(app)
        assert pantalla.contenido is not None, clase.__name__


def probar_configuracion_avisa_cuando_es_produccion():
    """el modal enseña la URL del ambiente y advierte si es producción"""
    # Es lo único que separa un ensayo de una captura real en el ERP de verdad,
    # así que tiene que verse antes de aceptar, no después.
    from core.catalogos import AMBIENTES
    from ui.configuracion import SeccionConfiguracion

    app = comun.AppFalsa()
    modal = SeccionConfiguracion(app)
    assert modal.dialogo is not None

    modal.dd_ambiente.value = "PRUEBAS"
    modal._refrescar_ambiente()
    assert not modal.txt_aviso_prod.visible
    assert modal.txt_url.value == AMBIENTES["PRUEBAS"]

    modal.dd_ambiente.value = "PRODUCCION"
    modal._refrescar_ambiente()
    assert modal.txt_aviso_prod.visible
    assert modal.txt_url.value == AMBIENTES["PRODUCCION"]


def probar_solicitudes_crea_lote_y_da_de_alta():
    """la pantalla crea un lote si no hay y guarda lo que le pasa el modal"""
    from ui.solicitudes import SeccionSolicitudes

    app = comun.AppFalsa()
    pantalla = SeccionSolicitudes(app)
    pantalla.cargar_desde_db()
    assert pantalla._lote is not None
    assert "0 solicitudes" in pantalla.txt_resumen.value

    lote_id = pantalla._lote.id
    solicitud = db.Solicitud(
        lote_id=lote_id, empresa="Abastecedora", sucursal="Corporativo",
        tipo_beneficiario="Acreedor", beneficiario_nombre="ANA LOPEZ",
        beneficiario_rfc="XAXX010101000", cuenta_clabe="012345678901234567",
        cuenta_banco="BBVA", forma_pago="Transferencia",
        tipo_gasto="No Deducible", fecha_pago="20/09/2026")
    pantalla._recibir_solicitud(solicitud, [comun.concepto("VIGILANCIA", 1200.0)])
    assert len(pantalla._solicitudes) == 1
    assert "1 solicitud" in pantalla.txt_resumen.value


def probar_desplegar_el_detalle_de_una_fila():
    """la fila maestro-detalle se abre sin error"""
    from ui.solicitudes import SeccionSolicitudes

    app = comun.AppFalsa()
    pantalla = SeccionSolicitudes(app)
    pantalla.cargar_desde_db()
    s = comun.solicitud(pantalla._lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("VIGILANCIA", 100.0),
                                  comun.insumo("Servicio", 100.0)])
    pantalla._recargar_solicitudes()
    pantalla._alternar_detalle(s.id)
    assert s.id in pantalla._expandidos
    pantalla._alternar_detalle(s.id)
    assert s.id not in pantalla._expandidos


def probar_duplicar_precarga_el_editor():
    """duplicar no guarda: abre el editor con una copia"""
    # Una copia idéntica produce la MISMA clave de idempotencia y sería
    # rechazada; duplicar sirve para partir de una parecida y cambiarle algo.
    from ui.solicitudes import SeccionSolicitudes

    app = comun.AppFalsa()
    pantalla = SeccionSolicitudes(app)
    pantalla.cargar_desde_db()
    s = comun.solicitud(pantalla._lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("VIGILANCIA", 100.0)])
    pantalla._recargar_solicitudes()
    pantalla._seleccionados = {s.id}
    pantalla._duplicar()
    assert pantalla.captura.tf_nombre.value == "ANA LOPEZ"
    assert pantalla.captura._solicitud.id != s.id
    assert len(db.listar_solicitudes(pantalla._lote.id)) == 1, "no debe guardar"


def probar_deshacer_solo_cuando_hay_algo():
    """el botón de deshacer está apagado si no hay operación que revertir"""
    from ui.solicitudes import SeccionSolicitudes

    app = comun.AppFalsa()
    pantalla = SeccionSolicitudes(app)
    pantalla.cargar_desde_db()
    comun.solicitud(pantalla._lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])
    pantalla._recargar_solicitudes()
    assert pantalla.btn_deshacer.disabled

    plan = asignacion.calcular(pantalla._lote.id, nombre="VIGILANCIA")
    asignacion.aplicar(pantalla._lote.id, plan)
    pantalla._recargar_solicitudes()
    assert not pantalla.btn_deshacer.disabled
    assert "Revertir" in (pantalla.btn_deshacer.tooltip or "")


# --------------------------------------------------------------------------- #
#  Modal de captura
# --------------------------------------------------------------------------- #
def probar_captura_bloquea_lo_incompleto():
    """guardar con datos faltantes muestra los hallazgos y no cierra"""
    from ui.captura_solicitud import CapturaSolicitud

    app = comun.AppFalsa()
    guardadas = []
    captura = CapturaSolicitud(app, lambda s, p: guardadas.append(s))
    lote = comun.lote()
    captura.abrir(lote.id)
    captura._guardar()
    assert captura.txt_hallazgos.visible
    assert not guardadas


def probar_captura_calcula_el_total():
    """el total se recalcula al editar un renglón"""
    from ui.captura_solicitud import CapturaSolicitud

    app = comun.AppFalsa()
    captura = CapturaSolicitud(app, lambda *_a: None)
    captura.abrir(comun.lote().id)
    renglon = captura.ed_conceptos.renglones[0]
    renglon.tf_nombre.value = "VIGILANCIA"
    renglon.tf_importe.value = "2,500.75"
    captura.ed_conceptos.recalcular()
    assert captura.txt_total.value == "$2,500.75", captura.txt_total.value
    partida = renglon.recolectar()
    assert partida.concepto_nombre == "VIGILANCIA" and partida.importe == 2500.75


def probar_el_editor_cambia_con_el_tipo_de_beneficiario():
    """a un Proveedor se le muestran insumos; a un Acreedor, conceptos"""
    from ui.captura_solicitud import CapturaSolicitud

    app = comun.AppFalsa()
    captura = CapturaSolicitud(app, lambda *_a: None)
    captura.abrir(comun.lote().id)
    assert captura.ed_conceptos.control.visible
    assert not captura.ed_insumos.control.visible

    captura.dd_tipo_ben.value = "Proveedor"
    captura._cambio_tipo_beneficiario()
    assert captura.ed_insumos.control.visible
    assert not captura.ed_conceptos.control.visible
    # Y solo se recolecta el desglose que aplica.
    _s, partidas = captura._recolectar()
    assert all(p.clase == INSUMO for p in partidas)


def probar_captura_ofrece_el_catalogo_de_conceptos():
    """el renglón de concepto es un desplegable editable con el catálogo"""
    from ui.captura_solicitud import CapturaSolicitud

    conceptos.importar(["VIGILANCIA", "NO DEDUCIBLE"], "Abastecedora")
    app = comun.AppFalsa()
    captura = CapturaSolicitud(app, lambda *_a: None)
    captura.abrir(comun.lote().id)
    renglon = captura.ed_conceptos.renglones[0]
    opciones = [o.key for o in renglon.tf_nombre.options]
    assert "VIGILANCIA" in opciones
    assert renglon.tf_nombre.editable, "debe dejar escribir uno que no esté"


def probar_captura_muestra_los_adjuntos_al_editar():
    """al reabrir una solicitud se ven sus archivos"""
    # Esta es la rama donde vivía un NameError: solo se ejecuta cuando la
    # solicitud YA trae archivos, y abrir el formulario en alta nunca la tocaba.
    from ui.captura_solicitud import CapturaSolicitud

    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("VIGILANCIA", 100.0)])
    carpeta = comun.carpeta_temporal()
    documentos.registrar(s.id, comun.pdf_falso(carpeta, "caratula.pdf"),
                         documentos.TIPO_CARATULA, lote.id)
    documentos.registrar(s.id, comun.pdf_falso(carpeta, "vobo.pdf"),
                         documentos.TIPO_VOBO, lote.id)

    app = comun.AppFalsa()
    captura = CapturaSolicitud(app, lambda *_a: None)
    captura.abrir(lote.id, s, db.listar_partidas(s.id))
    assert "caratula.pdf" in (captura.txt_caratula.value or "")
    assert "vobo.pdf" in (captura.txt_vobo.value or "")


# --------------------------------------------------------------------------- #
#  Modal de carga masiva
# --------------------------------------------------------------------------- #
def _excel_de_prueba(carpeta: str) -> str:
    campos = [c.clave for c in plantilla_excel.CAMPOS]

    def fila(**valores):
        return [valores.get(c, "") for c in campos]

    ruta = os.path.join(carpeta, plantilla_excel.NOMBRE_ARCHIVO)
    plantilla_excel.generar(ruta)
    libro = load_workbook(ruta)
    hoja = libro[plantilla_excel.HOJA_SOLICITUDES]
    filas = [
        fila(empresa="Abastecedora", sucursal="Corporativo",
             tipo_beneficiario="Acreedor", beneficiario_nombre="ANA LOPEZ RUIZ",
             beneficiario_rfc="XAXX010101000", forma_pago="Transferencia",
             tipo_gasto="No Deducible", cuenta_banco="BBVA",
             cuenta_clabe="012345678901234567", fecha_pago="15/09/2026",
             concepto_nombre="VIGILANCIA", importe="9500"),
        fila(empresa="Abastecedora", sucursal="Corporativo",
             tipo_beneficiario="Acreedor", beneficiario_nombre="LUIS DIAZ MORA",
             beneficiario_rfc="XAXX010101000", forma_pago="Transferencia",
             tipo_gasto="No Deducible", cuenta_banco="BBVA",
             cuenta_clabe="012345678901234568", fecha_pago="15/09/2026",
             concepto_nombre="VIGILANCIA", importe="7200"),
        # Sin empresa y con CLABE corta: debe salir marcada.
        fila(sucursal="Corporativo", tipo_beneficiario="Acreedor",
             beneficiario_nombre="SIN EMPRESA",
             beneficiario_rfc="XAXX010101000", forma_pago="Transferencia",
             tipo_gasto="No Deducible", cuenta_clabe="12",
             concepto_nombre="VIGILANCIA", importe="100"),
    ]
    for i, f in enumerate(filas, 3):
        for j, v in enumerate(f, 1):
            hoja.cell(row=i, column=j).value = v
    lleno = os.path.join(carpeta, "lleno.xlsx")
    libro.save(lleno)
    return lleno


def probar_carga_masiva_exige_revisar_antes():
    """la previa está oculta y el botón bloqueado hasta analizar el archivo"""
    from ui.carga_masiva import CargaMasiva

    app = comun.AppFalsa()
    carga = CargaMasiva(app, lambda *_a: None)
    carga.abrir(comun.lote().id)
    assert not carga.previa.visible
    assert carga.btn_importar.disabled


def probar_carga_masiva_importa_lo_bueno():
    """se importan las filas correctas y se marcan las malas"""
    from ui.carga_masiva import CargaMasiva

    app = comun.AppFalsa()
    importadas = []
    carga = CargaMasiva(app, lambda n: importadas.append(n))
    lote = comun.lote()
    carga.abrir(lote.id)
    carga._ruta = _excel_de_prueba(comun.carpeta_temporal())
    _correr(carga._analizar())

    assert carga.previa.visible
    assert carga.btn_importar.text == "Importar 2", carga.btn_importar.text
    carga._importar()
    assert importadas == [2]
    assert len(db.listar_solicitudes(lote.id)) == 2
    assert all(s.origen == "EXCEL" for s in db.listar_solicitudes(lote.id))


def probar_reimportar_no_duplica():
    """el mismo archivo dos veces no crea solicitudes repetidas"""
    from ui.carga_masiva import CargaMasiva

    app = comun.AppFalsa()
    carga = CargaMasiva(app, lambda *_a: None)
    lote = comun.lote()
    ruta = _excel_de_prueba(comun.carpeta_temporal())
    for _ in range(2):
        carga.abrir(lote.id)
        carga._ruta = ruta
        _correr(carga._analizar())
        carga._importar()
    assert len(db.listar_solicitudes(lote.id)) == 2
    assert "ya existían" in app.ultimo_aviso


# --------------------------------------------------------------------------- #
#  Modal de alta desde carátulas
# --------------------------------------------------------------------------- #
def probar_alta_desde_caratulas():
    """cada carátula se vuelve una solicitud, y el nombre se puede corregir"""
    from ui.alta_caratulas import AltaDesdeCaratulas

    carpeta = comun.carpeta_temporal()
    for nombre in ("CARATULA JUAN PEREZ LOPEZ.pdf",
                   "CARATULA MARIA RUIZ SOTO.pdf",
                   "12345678.pdf"):
        comun.pdf_falso(carpeta, nombre)

    app = comun.AppFalsa()
    creadas = []
    modal = AltaDesdeCaratulas(app, lambda n: creadas.append(n))
    lote = comun.lote()
    modal.abrir(lote.id)
    modal.dd_empresa.value = "Abastecedora"
    modal.dd_sucursal.value = "Corporativo"
    modal._cargar([carpeta])

    assert modal.btn_importar.text == "Dar de alta 2", modal.btn_importar.text
    # El que no se reconoció se corrige en la tabla.
    sin_nombre = [b for b in modal._borradores if not b.nombre_detectado][0]
    modal._renombrar(sin_nombre, "pedro gomez soto")
    assert sin_nombre.solicitud.beneficiario_nombre == "PEDRO GOMEZ SOTO"
    assert modal.btn_importar.text == "Dar de alta 3"

    modal._importar()
    assert creadas == [3]
    con_caratula = sum(1 for s in db.listar_solicitudes(lote.id)
                       if "caratula" in documentos.de_solicitud(s.id))
    assert con_caratula == 3, "ninguna solicitud debe quedarse sin carátula"


# --------------------------------------------------------------------------- #
#  Modal de asignación masiva
# --------------------------------------------------------------------------- #
def probar_asignacion_invalida_la_previa_al_cambiar_criterios():
    """cambiar un criterio obliga a recalcular"""
    # Si no, se aplicaría un plan distinto del que se vio, que es exactamente lo
    # que este modal existe para evitar.
    from ui.asignacion_masiva import AsignacionMasiva

    conceptos.importar(["VIGILANCIA"], "Abastecedora")
    app = comun.AppFalsa()
    modal = AsignacionMasiva(app, lambda *_a: None)
    lote = comun.lote()
    comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])

    modal.abrir(lote.id, set())
    modal.dd_nombre.value = "VIGILANCIA"
    modal._calcular()
    assert modal.previa.visible and not modal.btn_aplicar.disabled

    modal.dd_modo.value = "Reemplazar los renglones existentes"
    modal._invalidar()
    assert not modal.previa.visible and modal.btn_aplicar.disabled


def probar_asignacion_respeta_la_seleccion_previa():
    """abrir con filas marcadas propone el alcance «seleccionadas»"""
    from ui.asignacion_masiva import AsignacionMasiva

    app = comun.AppFalsa()
    modal = AsignacionMasiva(app, lambda *_a: None)
    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])
    comun.solicitud(lote.id, "OTRO", partidas=[
        comun.insumo("Facturado", 500.0, origen="CFDI")])

    modal.abrir(lote.id, {s.id})
    assert modal.dd_alcance.value == "Solo las seleccionadas"
    modal.dd_nombre.value = "VIGILANCIA"
    modal._calcular()
    assert [c.solicitud.id for c in modal._plan.cambios] == [s.id]


def probar_asignacion_aplica_y_deshace_desde_el_aviso():
    """se aplica y se puede revertir desde el propio aviso de éxito"""
    from ui.asignacion_masiva import AsignacionMasiva

    app = comun.AppFalsa()
    modal = AsignacionMasiva(app, lambda *_a: None)
    lote = comun.lote()
    ana = comun.solicitud(lote.id, "ANA LOPEZ", partidas=[
        comun.insumo("Facturado", 1000.0, origen="CFDI")])

    modal.abrir(lote.id, set())
    modal.dd_nombre.value = "VIGILANCIA"
    modal._calcular()
    modal._aplicar()
    assert len(db.listar_partidas(ana.id)) == 2

    modal._deshacer_desde_aviso()
    assert len(db.listar_partidas(ana.id)) == 1


# --------------------------------------------------------------------------- #
#  Bitácora y conceptos
# --------------------------------------------------------------------------- #
def probar_bitacora_filtra_por_nivel():
    """el filtro de la bitácora reduce lo que se muestra"""
    from ui.bitacora import SeccionBitacora

    lote = comun.lote()
    s = comun.solicitud(lote.id, "ANA LOPEZ",
                        partidas=[comun.concepto("VIGILANCIA", 100.0)])
    db.registrar(s.id, "login", "Sesión iniciada")
    db.registrar(s.id, "guardar", "Falló", nivel="ERROR")

    app = comun.AppFalsa()
    pantalla = SeccionBitacora(app)
    pantalla.cargar_desde_db()
    assert "2 registro" in pantalla.txt_resumen.value
    pantalla._nivel = "ERROR"
    pantalla.cargar_desde_db()
    assert "1 registro" in pantalla.txt_resumen.value


def probar_buscar_en_el_catalogo_de_conceptos():
    """el buscador filtra sin acentos, por palabras sueltas y en cualquier orden"""
    from ui.conceptos import SeccionConceptos

    conceptos.importar(["ISR RETENCIONES POR SALARIOS", "PREVISION OBRERA",
                        "PREVISION PATRONAL", "VIGILANCIA"], "Abastecedora")
    app = comun.AppFalsa()
    pantalla = SeccionConceptos(app)
    pantalla.cargar_desde_db()
    assert "4 concepto(s)" in pantalla.txt_estado.value
    assert not pantalla.btn_limpiar.visible

    def buscar(texto):
        pantalla.tf_buscar.value = texto
        pantalla._cambiar_filtro()
        return [c.nombre for c in pantalla._conceptos
                if pantalla._coincide(c)]

    assert buscar("prevision") == ["PREVISION OBRERA", "PREVISION PATRONAL"]
    assert buscar("previsión") == ["PREVISION OBRERA", "PREVISION PATRONAL"], (
        "el acento no debe cambiar el resultado")
    assert buscar("salarios isr") == ["ISR RETENCIONES POR SALARIOS"], (
        "las palabras van sueltas y en cualquier orden")

    buscar("prevision")
    assert "2 de 4 concepto(s)" in pantalla.txt_estado.value, (
        "el total tiene que seguir a la vista al filtrar")
    assert pantalla.btn_limpiar.visible


def probar_buscar_sin_resultados_no_invita_a_importar():
    """el vacío por filtro se distingue del catálogo vacío"""
    # Con un solo mensaje, no encontrar «vigilancia» invitaría a importar de
    # SIPP un catálogo que ya está completo, o a darla de alta a mano duplicada.
    from ui.conceptos import SeccionConceptos

    app = comun.AppFalsa()
    pantalla = SeccionConceptos(app)
    pantalla.cargar_desde_db()
    assert pantalla.vacio.visible and not pantalla.sin_resultados.visible

    conceptos.importar(["VIGILANCIA"], "Abastecedora")
    pantalla.tf_buscar.value = "no existe"
    pantalla._cambiar_filtro()
    assert pantalla.sin_resultados.visible
    assert not pantalla.vacio.visible
    assert not pantalla.tabla.control.visible

    pantalla._limpiar_filtro()
    assert pantalla.tabla.control.visible
    assert not pantalla.sin_resultados.visible
    assert not pantalla.btn_limpiar.visible


def probar_el_alta_deja_ver_el_concepto_nuevo():
    """agregar con una búsqueda puesta no esconde lo recién agregado"""
    from ui.conceptos import SeccionConceptos

    conceptos.importar(["VIGILANCIA"], "Abastecedora")
    app = comun.AppFalsa()
    pantalla = SeccionConceptos(app)
    pantalla.cargar_desde_db()
    pantalla.tf_buscar.value = "vigilancia"
    pantalla._cambiar_filtro()

    pantalla._abrir_alta()
    pantalla.tf_nombre.value = "FLETES FORANEOS"
    pantalla._guardar_alta()
    assert not pantalla.tf_buscar.value, "la búsqueda debe quedar limpia"
    assert "2 concepto(s)" in pantalla.txt_estado.value


def probar_alta_manual_de_concepto_avisa_de_soporte():
    """al crear un concepto a mano se advierte que puede no existir en SIPP"""
    from ui.conceptos import SeccionConceptos

    app = comun.AppFalsa()
    pantalla = SeccionConceptos(app)
    pantalla.cargar_desde_db()
    pantalla._abrir_alta()
    pantalla.tf_nombre.value = "CONCEPTO NUEVO"
    pantalla._guardar_alta()
    assert "soporte" in app.ultimo_aviso.lower()
    assert conceptos.existe("CONCEPTO NUEVO")

    # Y el duplicado se rechaza dentro del diálogo, sin cerrarlo.
    pantalla._abrir_alta()
    pantalla.tf_nombre.value = "concepto nuevo"
    pantalla._guardar_alta()
    assert pantalla.txt_error_alta.visible
