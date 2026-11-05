"""Catálogo de conceptos de pago y documentos adjuntos.

Dos cosas que SIPP impone y la herramienta tiene que reflejar sin inventar: qué
conceptos existen de verdad, y que una cuenta bancaria nueva no se registra sin
su carátula.
"""

from __future__ import annotations

import os

from openpyxl import load_workbook

from core import conceptos, db, documentos, plantilla_excel
from scripts.pruebas import comun


# --------------------------------------------------------------------------- #
#  Conceptos
# --------------------------------------------------------------------------- #
def probar_catalogo_arranca_vacio():
    """sin importar nada, no hay conceptos"""
    assert not conceptos.hay_catalogo()
    assert conceptos.nombres() == []


def probar_importar_no_duplica():
    """volver a importar detecta lo nuevo sin repetir lo que ya estaba"""
    lista = ["VIGILANCIA", "NO DEDUCIBLE", "PAGO TARJETA CREDITO"]
    primera = conceptos.importar(lista, "Abastecedora")
    assert primera == {"nuevos": 3, "ya_estaban": 0}
    segunda = conceptos.importar(lista, "Abastecedora")
    assert segunda == {"nuevos": 0, "ya_estaban": 3}
    assert len(conceptos.listar()) == 3


def probar_es_una_lista_unica_para_el_grupo():
    """el mismo concepto en otra empresa no crea una entrada nueva"""
    conceptos.importar(["VIGILANCIA"], "Abastecedora")
    conceptos.importar(["VIGILANCIA", "OTRO"], "Aske")
    assert len(conceptos.listar()) == 2


def probar_normaliza_para_no_duplicar():
    """«vigilancia» y «VIGILANCIA » son el mismo concepto"""
    conceptos.guardar("VIGILANCIA", "Abastecedora", conceptos.ORIGEN_SIPP)
    assert conceptos.guardar("vigilancia") is None
    assert conceptos.guardar("Vigilancia ") is None
    assert len(conceptos.listar()) == 1


def probar_el_manual_queda_sin_verificar():
    """un concepto capturado a mano se distingue del importado"""
    # La diferencia importa: uno existe con certeza en SIPP y el otro puede no
    # existir, y si no existe, la solicitud que lo use no se podrá capturar.
    conceptos.importar(["VIGILANCIA"], "Abastecedora")
    manual = conceptos.guardar("CONCEPTO INVENTADO")
    importado = [c for c in conceptos.listar() if c.nombre == "VIGILANCIA"][0]
    assert manual is not None and not manual.verificado
    assert importado.verificado


def probar_la_plantilla_ofrece_el_catalogo():
    """los conceptos del catálogo llegan a la plantilla como desplegable"""
    carpeta = comun.carpeta_temporal()
    sin = os.path.join(carpeta, "sin.xlsx")
    plantilla_excel.generar(sin)
    listas_sin = [c.value for c in
                  load_workbook(sin)[plantilla_excel.HOJA_CATALOGOS][2]]
    assert "conceptos" not in listas_sin, (
        "sin catálogo no debe inventarse una lista")

    conceptos.importar(["VIGILANCIA", "NO DEDUCIBLE"], "Abastecedora")
    con = os.path.join(carpeta, "con.xlsx")
    plantilla_excel.generar(con)
    libro = load_workbook(con)
    hoja_cat = libro[plantilla_excel.HOJA_CATALOGOS]
    listas_con = [c.value for c in hoja_cat[2]]
    assert "conceptos" in listas_con
    columna = listas_con.index("conceptos") + 1
    valores = [hoja_cat.cell(row=r, column=columna).value for r in (3, 4)]
    assert sorted(v for v in valores if v) == ["NO DEDUCIBLE", "VIGILANCIA"]


# --------------------------------------------------------------------------- #
#  Documentos
# --------------------------------------------------------------------------- #
def probar_emparejar_caratula_por_nombre():
    """el archivo se reconoce aunque traiga texto de más"""
    carpeta = comun.carpeta_temporal()
    for nombre in ("CARATULA JUAN PEREZ LOPEZ BBVA.pdf",
                   "María Ñuño Gómez.pdf",
                   "estado de cuenta - LUIS ALBERTO DIAZ MORA - agosto.pdf",
                   "ruido que no corresponde a nadie.pdf"):
        comun.pdf_falso(carpeta, nombre)

    casos = [("Juan Pérez López", True), ("MARIA NUNO GOMEZ", True),
             ("Luis Alberto Díaz Mora", True), ("Juan Pérez", True),
             # Un apellido distinto NO es la misma persona.
             ("Juan Pérez Ramírez", False), ("PEDRO SIN ARCHIVO", False)]
    for nombre, esperado in casos:
        hallado = bool(documentos.buscar_en_carpeta(carpeta, nombre))
        assert hallado == esperado, nombre


def probar_asignar_una_carpeta_al_lote():
    """se asigna por nombre y se dice a quién le falta"""
    carpeta = comun.carpeta_temporal()
    comun.pdf_falso(carpeta, "CARATULA JUAN PEREZ LOPEZ.pdf")
    comun.pdf_falso(carpeta, "CARATULA MARIA RUIZ SOTO.pdf")

    lote = comun.lote()
    partidas = [comun.concepto("VIGILANCIA", 1000.0)]
    juan = comun.solicitud(lote.id, "JUAN PEREZ LOPEZ", partidas=partidas)
    comun.solicitud(lote.id, "MARIA RUIZ SOTO",
                    partidas=[comun.concepto("VIGILANCIA", 2000.0)])
    pedro = comun.solicitud(lote.id, "PEDRO SIN ARCHIVO",
                            partidas=[comun.concepto("VIGILANCIA", 500.0)])

    resultado = documentos.asignar_por_carpeta(
        lote.id, carpeta, documentos.TIPO_CARATULA)
    assert resultado["asignados"] == 2
    assert "PEDRO SIN ARCHIVO" in resultado["sin_archivo"]
    assert "caratula" in documentos.de_solicitud(juan.id)
    assert "caratula" not in documentos.de_solicitud(pedro.id)


def probar_reasignar_no_duplica():
    """una solicitud tiene UNA carátula, no un historial"""
    carpeta = comun.carpeta_temporal()
    comun.pdf_falso(carpeta, "CARATULA ANA LOPEZ.pdf")
    lote = comun.lote()
    ana = comun.solicitud(lote.id, "ANA LOPEZ",
                          partidas=[comun.concepto("VIGILANCIA", 100.0)])
    for _ in range(2):
        documentos.asignar_por_carpeta(lote.id, carpeta,
                                       documentos.TIPO_CARATULA)
    assert len(db.listar_documentos(solicitud_id=ana.id)) == 1


def probar_caratula_y_vobo_conviven():
    """los dos tipos de documento se guardan a la vez"""
    lote = comun.lote()
    ana = comun.solicitud(lote.id, "ANA LOPEZ",
                          partidas=[comun.concepto("VIGILANCIA", 100.0)])
    carpeta = comun.carpeta_temporal()
    documentos.registrar(ana.id, comun.pdf_falso(carpeta, "c.pdf"),
                         documentos.TIPO_CARATULA, lote.id)
    documentos.registrar(ana.id, comun.pdf_falso(carpeta, "v.pdf"),
                         documentos.TIPO_VOBO, lote.id)
    assert set(documentos.de_solicitud(ana.id)) == {"caratula", "vobo"}


def probar_archivo_borrado_del_disco_no_se_ofrece():
    """si el archivo desapareció, el motor no lo recibe"""
    lote = comun.lote()
    ana = comun.solicitud(lote.id, "ANA LOPEZ",
                          partidas=[comun.concepto("VIGILANCIA", 100.0)])
    carpeta = comun.carpeta_temporal()
    ruta = comun.pdf_falso(carpeta, "c.pdf")
    documentos.registrar(ana.id, ruta, documentos.TIPO_CARATULA, lote.id)
    os.remove(ruta)
    assert "caratula" not in documentos.de_solicitud(ana.id)


def probar_aviso_de_caratula_no_depende_del_flag():
    """se avisa por transferencia sin archivo, no por un dato declarado"""
    # Quién ya está dado de alta lo decide SIPP al capturar, así que aquí no se
    # puede afirmar que la carátula sea obligatoria: solo que podría hacer falta.
    lote = comun.lote()
    partidas = [comun.concepto("VIGILANCIA", 100.0)]
    transferencia = comun.solicitud(lote.id, "POR TRANSFERENCIA",
                                    partidas=partidas)
    efectivo = comun.solicitud(lote.id, "EN EFECTIVO",
                               partidas=[comun.concepto("VIGILANCIA", 200.0)],
                               forma_pago="Efectivo")
    assert documentos.falta_caratula(transferencia)
    assert not documentos.falta_caratula(efectivo), (
        "sin transferencia no hay cuenta que dar de alta")

    carpeta = comun.carpeta_temporal()
    documentos.registrar(transferencia.id,
                         comun.pdf_falso(carpeta, "c.pdf"),
                         documentos.TIPO_CARATULA, lote.id)
    assert not documentos.falta_caratula(transferencia)


# --------------------------------------------------------------------------- #
#  Lectura del grid de SIPP (ng-grid virtualizado)
# --------------------------------------------------------------------------- #
class _GridVirtual:
    """Imita un ng-grid: solo expone las filas de la ventana visible.

    Es el comportamiento que hacía que la importación trajera siempre los
    mismos trece conceptos: las filas de abajo NO están en el DOM hasta que se
    baja hasta ellas.
    """

    VENTANA = 10

    def __init__(self, nombres, alto_fila=20):
        self.nombres = nombres
        self.alto_fila = alto_fila
        self.scroll_top = 0
        self.client_height = self.VENTANA * alto_fila
        self.scroll_height = len(nombres) * alto_fila

    # --- lo que ve el lector como "grid"
    def _ventana(self):
        desde = self.scroll_top // self.alto_fila
        return self.nombres[desde:desde + self.VENTANA]

    def count(self):
        return len(self._ventana())

    def nth(self, i):
        texto = self._ventana()[i]
        return type("Fila", (), {
            "locator": lambda _s, _css: type("L", (), {
                "first": type("F", (), {"inner_text": lambda _x: texto})()})()
        })()

    # --- lo que ve como "viewport"
    @property
    def count_viewport(self):
        return 1

    def evaluate(self, _js):
        antes = self.scroll_top
        self.scroll_top = min(self.scroll_top + int(self.client_height * 0.8),
                              max(0, self.scroll_height - self.client_height))
        return (self.scroll_top <= antes
                or self.scroll_top >= self.scroll_height - self.client_height - 2)


def _sesion_falsa(grid):
    viewport = type("V", (), {"count": lambda _s: 1,
                              "evaluate": lambda _s, js: grid.evaluate(js)})()
    page = type("P", (), {
        "wait_for_timeout": lambda _s, _ms: None,
        "locator": lambda _s, _css: type("L", (), {"first": viewport})(),
    })()
    return type("S", (), {"page": page})()


class _SelectoresFalsos:
    @staticmethod
    def css(clave):
        return {"con.nombre": ".ngCellText", "con.grid": "#grid"}[clave]


def probar_se_leen_todos_los_conceptos_aunque_el_grid_use_scroll():
    """el grid virtualizado se recorre entero, no solo la primera pantalla"""
    # El caso reportado: SIPP tenía conceptos nuevos más abajo y la importación
    # decía «0 nuevos» porque solo leía las filas que cabían en pantalla.
    nombres = [f"CONCEPTO {i:02}" for i in range(1, 31)]
    grid = _GridVirtual(nombres)

    leidos = conceptos._leer_grid_con_scroll(_sesion_falsa(grid), grid,
                                             _SelectoresFalsos)

    assert len(leidos) == 30, f"solo leyó {len(leidos)} de 30"
    assert leidos == nombres, "y en el orden del grid"


def probar_un_grid_que_cabe_entero_se_lee_de_una():
    """sin scroll que hacer, no se inventan vueltas"""
    nombres = ["UNO", "DOS", "TRES"]
    grid = _GridVirtual(nombres)
    leidos = conceptos._leer_grid_con_scroll(_sesion_falsa(grid), grid,
                                             _SelectoresFalsos)
    assert leidos == nombres


def probar_el_grid_no_repite_conceptos_al_desplazarse():
    """las filas recicladas no entran dos veces al catálogo"""
    # ng-grid reutiliza los mismos nodos: sin control, una fila leída en dos
    # posiciones distintas se contaría dos veces.
    nombres = [f"C{i}" for i in range(20)] + ["C0", "C1"]   # repetidos al final
    grid = _GridVirtual(nombres)
    leidos = conceptos._leer_grid_con_scroll(_sesion_falsa(grid), grid,
                                             _SelectoresFalsos)
    assert len(leidos) == len(set(leidos)), "hay duplicados"
    assert len(leidos) == 20


# --------------------------------------------------------------------------- #
#  La carátula siempre acaba en PDF
# --------------------------------------------------------------------------- #
# Es lo que el área entrega al banco y lo que SIPP guarda como respaldo de la
# cuenta, pero llega a menudo como foto o captura de pantalla.
def _imagen(carpeta, nombre="caratula.jpg", color=(200, 30, 30)):
    import pymupdf

    ruta = os.path.join(carpeta, nombre)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
    pix.set_rect(pix.irect, color)
    pix.save(ruta)
    return ruta


def _es_pdf(ruta) -> bool:
    with open(ruta, "rb") as fh:
        return fh.read(5) == b"%PDF-"


def probar_una_caratula_en_imagen_se_convierte_a_pdf():
    """la carátula tiene que ser un PDF, llegue como llegue"""
    import tempfile

    carpeta = tempfile.mkdtemp()
    salida = documentos.asegurar_pdf(_imagen(carpeta))
    assert salida.lower().endswith(".pdf"), salida
    assert _es_pdf(salida), "y ser un PDF de verdad, no solo llamarse así"


def probar_un_pdf_no_se_toca():
    """convertir lo que ya está bien solo puede estropearlo"""
    import tempfile

    ruta = os.path.join(tempfile.mkdtemp(), "caratula.pdf")
    with open(ruta, "wb") as fh:
        fh.write(b"%PDF-1.4\n")
    assert documentos.asegurar_pdf(ruta) == ruta


def probar_dos_imagenes_distintas_no_se_pisan():
    """«caratula.jpg» se llaman todas: sin el hash, la segunda borra a la primera"""
    import tempfile

    uno = documentos.asegurar_pdf(
        _imagen(tempfile.mkdtemp(), color=(10, 200, 10)))
    dos = documentos.asegurar_pdf(
        _imagen(tempfile.mkdtemp(), color=(10, 10, 200)))
    assert uno != dos, "cada carátula es la de una persona distinta"


def probar_la_misma_imagen_no_se_convierte_dos_veces():
    """reabrir un lote no debe rehacer el trabajo"""
    import tempfile

    ruta = _imagen(tempfile.mkdtemp())
    assert documentos.asegurar_pdf(ruta) == documentos.asegurar_pdf(ruta)


def probar_si_la_conversion_falla_se_usa_el_original():
    """quedarse sin carátula detiene la solicitud entera; un JPG no"""
    import tempfile

    roto = os.path.join(tempfile.mkdtemp(), "caratula.png")
    with open(roto, "wb") as fh:
        fh.write(b"esto no es una imagen")
    assert documentos.asegurar_pdf(roto) == roto


def probar_al_registrar_la_caratula_queda_en_pdf():
    """es el único sitio por el que pasan todas las vías de alta"""
    import tempfile

    lote = comun.lote()
    s = db.guardar_solicitud(comun.solicitud(lote.id, "ANA LOPEZ"))
    doc = documentos.registrar(s.id, _imagen(tempfile.mkdtemp()),
                               documentos.TIPO_CARATULA, lote.id)
    assert doc is not None
    assert doc.ruta.lower().endswith(".pdf")
    assert _es_pdf(doc.ruta)


def probar_el_vobo_en_imagen_tambien_queda_en_pdf():
    """suele ser una foto de celular: en PDF se reduce y queda como lo demás"""
    import tempfile

    lote = comun.lote()
    s = db.guardar_solicitud(comun.solicitud(lote.id, "ANA LOPEZ"))
    ruta = _imagen(tempfile.mkdtemp(), "vobo.png")
    doc = documentos.registrar(s.id, ruta, documentos.TIPO_VOBO, lote.id)
    assert doc is not None
    assert doc.ruta.lower().endswith(".pdf")
    assert _es_pdf(doc.ruta)


def probar_una_foto_grande_se_reduce_al_convertir():
    """SIPP corta en 10 MB y el robot solo sabía comprimir PDFs"""
    import tempfile

    import pymupdf

    ruta = os.path.join(tempfile.mkdtemp(), "vobo.png")
    # Ruido: una imagen lisa se comprime sola y no probaría nada.
    pix = pymupdf.Pixmap(pymupdf.csRGB, 4000, 3000,
                         os.urandom(4000 * 3000 * 3), False)
    pix.save(ruta)
    salida = documentos.asegurar_pdf(ruta)
    assert _es_pdf(salida)
    assert os.path.getsize(salida) < os.path.getsize(ruta) / 3, (
        os.path.getsize(salida), os.path.getsize(ruta))
    with pymupdf.open(salida) as pdf:
        lado = max(pdf[0].get_images(full=True)[0][2:4])
    assert lado <= documentos._LADO_MAXIMO, lado


# --------------------------------------------------------------------------- #
#  El mismo concepto, escrito de otra forma
# --------------------------------------------------------------------------- #
# El Excel del área trae los conceptos con acentos y el catálogo de SIPP no los
# tiene. Guardar el texto crudo dejaba la partida con un nombre que no era
# ninguna de las opciones del desplegable: aparecía vacío aunque el importe sí
# estuviera, y abrirlo y guardar borraba el concepto.
def _con_catalogo(*nombres):
    for n in nombres:
        conceptos.guardar(n)


def probar_los_acentos_no_hacen_de_un_concepto_otro():
    """«ÚTILES … ACADÉMICA» es el mismo que «UTILES … ACADEMICA»"""
    _con_catalogo("APOYO DE UTILES POR EXCELENCIA ACADEMICA")
    assert conceptos.canonico("APOYO DE ÚTILES POR EXCELENCIA ACADÉMICA") == \
        "APOYO DE UTILES POR EXCELENCIA ACADEMICA"


def probar_la_puntuacion_tampoco():
    """un punto o un guion de más no cambian de qué se está hablando"""
    _con_catalogo("PAGO IMSS")
    assert conceptos.canonico("Pago  I.M.S.S.") == "PAGO IMSS"


def probar_dos_conceptos_distintos_siguen_siendo_distintos():
    """se ignora cómo está escrito, no lo que dice"""
    _con_catalogo("PAGO IMSS", "PAGO SAR")
    assert conceptos.canonico("PAGO INFONAVIT") == "PAGO INFONAVIT"


def probar_un_concepto_que_no_esta_en_el_catalogo_se_respeta():
    """puede ser uno recién dado de alta en SIPP y aún sin importar"""
    _con_catalogo("PAGO IMSS")
    assert conceptos.canonico("CONCEPTO NUEVO") == "CONCEPTO NUEVO"


def probar_el_catalogo_reconoce_el_concepto_con_acentos():
    """si no, se marcaría «sin verificar» un concepto que sí existe"""
    _con_catalogo("APOYO DE UTILES POR EXCELENCIA ACADEMICA")
    assert conceptos.existe("APOYO DE ÚTILES POR EXCELENCIA ACADÉMICA")


def probar_un_concepto_acortado_encuentra_el_del_catalogo():
    """en el Excel se escribe «PAGO PTU» y en SIPP está como «PAGO DE PTU»"""
    _con_catalogo("PAGO DE PTU")
    assert conceptos.canonico("PAGO PTU") == "PAGO DE PTU"


def probar_con_dos_candidatos_no_se_adivina():
    """elegir mal manda el dinero a otro concepto y no lo nota nadie"""
    _con_catalogo("PAGO DE PTU 2025", "PAGO DE PTU 2026")
    assert conceptos.canonico("PAGO PTU") == "PAGO PTU"


# --------------------------------------------------------------------------- #
#  Catálogo de insumos y servicios
# --------------------------------------------------------------------------- #
# SIPP no pide lo mismo a todos: a un Proveedor le pide Insumos & Servicios y a
# un Deudor o un Acreedor, Conceptos de Pago. Son dos catálogos, y el de insumos
# no se importa del portal —es enorme— sino que lo entrega el área ya depurado.
def probar_el_catalogo_de_insumos_es_independiente_del_de_conceptos():
    """el mismo texto puede estar en los dos sin ser lo mismo"""
    from core import insumos

    insumos.vaciar()
    conceptos.guardar("VIGILANCIA")
    insumos.guardar("VIGILANCIA")
    assert conceptos.existe("VIGILANCIA")
    assert insumos.existe("VIGILANCIA")


def probar_un_insumo_se_reconoce_aunque_cambie_la_escritura():
    """misma regla que en conceptos: acentos y puntuación no cambian lo que dice"""
    from core import insumos

    insumos.vaciar()
    insumos.guardar("MANTENIMIENTO DE AIRE ACONDICIONADO")
    assert insumos.canonico("mantenimiento de aíre acondicionado.") == \
        "MANTENIMIENTO DE AIRE ACONDICIONADO"


def probar_recargar_el_catalogo_retira_lo_que_el_area_quito():
    """llega una versión depurada nueva: sustituye, no se acumula"""
    from core import insumos

    insumos.importar(["UNO", "DOS", "TRES"], reemplazar=True)
    resumen = insumos.importar(["UNO", "CUATRO"], reemplazar=True)
    assert resumen["retirados"] == 3
    assert sorted(insumos.nombres()) == ["CUATRO", "UNO"]


def probar_agregar_no_borra_lo_que_ya_habia():
    """a veces solo se añaden servicios nuevos"""
    from core import insumos

    insumos.importar(["UNO"], reemplazar=True)
    insumos.importar(["DOS"], reemplazar=False)
    assert sorted(insumos.nombres()) == ["DOS", "UNO"]


def _archivo_insumos(filas, ext=".xlsx"):
    import tempfile

    ruta = os.path.join(tempfile.mkdtemp(), "insumos" + ext)
    if ext == ".csv":
        with open(ruta, "w", encoding="utf-8", newline="") as fh:
            for fila in filas:
                fh.write(";".join(fila) + "\n")
        return ruta
    from openpyxl import Workbook

    libro = Workbook()
    for fila in filas:
        libro.active.append(list(fila))
    libro.save(ruta)
    return ruta


def probar_el_archivo_del_area_se_lee_con_encabezado():
    """lo depuran a mano: llega como llega"""
    from core import insumos

    ruta = _archivo_insumos([("Servicio",), ("LIMPIEZA",), ("FLETES",)])
    assert insumos.leer_archivo(ruta) == [("", "LIMPIEZA"), ("", "FLETES")]


def probar_el_archivo_se_lee_aunque_no_traiga_encabezado():
    """una lista de una sola columna es lo más común"""
    from core import insumos

    ruta = _archivo_insumos([("LIMPIEZA",), ("FLETES",)])
    assert insumos.leer_archivo(ruta) == [("", "LIMPIEZA"), ("", "FLETES")]


def probar_los_repetidos_del_archivo_no_entran_dos_veces():
    """una lista depurada a mano suele traerlos"""
    from core import insumos

    ruta = _archivo_insumos([("Insumo",), ("LIMPIEZA",), ("limpieza",),
                             ("FLETES",)])
    assert insumos.leer_archivo(ruta) == [("", "LIMPIEZA"), ("", "FLETES")]


def probar_el_catalogo_tambien_entra_por_csv():
    """no siempre lo entregan en Excel"""
    from core import insumos

    ruta = _archivo_insumos([("Servicio",), ("LIMPIEZA",)], ext=".csv")
    assert insumos.leer_archivo(ruta) == [("", "LIMPIEZA")]


def _plantilla_con(conceptos_lista, insumos_lista):
    """Genera la plantilla con esos dos catálogos y la devuelve abierta."""
    import tempfile

    from openpyxl import load_workbook

    from core import insumos as cat_insumos
    from core import plantilla_excel

    for nombre in conceptos_lista:
        conceptos.guardar(nombre)
    cat_insumos.importar(insumos_lista, reemplazar=True)
    ruta = os.path.join(tempfile.mkdtemp(), "plantilla.xlsx")
    plantilla_excel.generar(ruta)
    return load_workbook(ruta)


def _validacion_de(hoja, columna):
    for dv in hoja.data_validations.dataValidation:
        if any(str(r).startswith(columna) for r in dv.sqref.ranges):
            return dv.formula1 or ""
    return ""


def probar_la_plantilla_ofrece_insumos_solo_al_proveedor():
    """SIPP le pide Insumos & Servicios al Proveedor y conceptos a los demás"""
    # Ofrecer las dos listas juntas invita a elegir de la equivocada, y un
    # renglón de la clase que no toca no falla al capturarlo: falla a media
    # corrida, cuando el robot no lo encuentra en la pestaña del portal.
    libro = _plantilla_con(["PAGO IMSS"], ["LIMPIEZA"])
    formula = _validacion_de(libro["Solicitudes"], "P")
    assert "INDIRECT" in formula, "la lista tiene que depender de otra celda"
    assert '$D2="Proveedor"' in formula, "y esa celda es el tipo de beneficiario"
    assert "_INSUMOS" in formula and "_CONCEPTOS" in formula


def probar_en_el_desglose_la_lista_depende_de_la_clase_del_renglon():
    """en la hoja Partidas lo que manda es si el renglón es concepto o insumo"""
    libro = _plantilla_con(["PAGO IMSS"], ["LIMPIEZA"])
    formula = _validacion_de(libro["Partidas"], "C")
    assert '$B2="Insumo"' in formula


def probar_las_dos_listas_viajan_en_la_plantilla():
    """el desplegable las lee de ahí: sin ellas no hay de dónde elegir"""
    libro = _plantilla_con(["PAGO IMSS"], ["LIMPIEZA"])
    assert "_CONCEPTOS" in libro.defined_names
    assert "_INSUMOS" in libro.defined_names
    cat = libro["Catálogos"]
    encabezados = [cat.cell(row=2, column=i).value for i in range(1, 15)]
    assert "conceptos" in encabezados and "insumos" in encabezados


def probar_sin_catalogo_de_insumos_la_columna_no_se_queda_sin_lista():
    """mientras el área depura su catálogo, la plantilla tiene que servir igual"""
    from core import insumos as cat_insumos

    cat_insumos.vaciar()
    libro = _plantilla_con(["PAGO IMSS"], [])
    formula = _validacion_de(libro["Solicitudes"], "P")
    assert formula, "sin insumos, al menos los conceptos de siempre"
    assert "INDIRECT" not in formula, "no se puede alternar con una sola lista"


# --------------------------------------------------------------------------- #
#  El folio del insumo
# --------------------------------------------------------------------------- #
# SIPP busca el insumo por folio o por nombre. Por folio es exacto; los nombres
# del catálogo del área son larguísimos y se parecen entre sí —«… MANGUERAS DE
# COMBUSTIBLE DE GASOLINA» y «… DIESEL»—, así que buscar por nombre puede
# devolver varios y elegir mal.
def probar_el_archivo_con_folio_y_nombre_se_lee_entero():
    """es como lo entrega el área y como lo pide SIPP"""
    from core import insumos

    ruta = _archivo_insumos([("Folio Insumo", "Nombre Insumo"),
                             ("1024", "LIMPIEZA DE OFICINAS"),
                             ("2048", "FLETES FORANEOS")])
    assert insumos.leer_archivo(ruta) == [("1024", "LIMPIEZA DE OFICINAS"),
                                          ("2048", "FLETES FORANEOS")]


def probar_sin_encabezado_el_folio_se_reconoce_por_su_forma():
    """códigos cortos y sin espacios a la izquierda del nombre"""
    from core import insumos

    ruta = _archivo_insumos([("1024", "LIMPIEZA DE OFICINAS"),
                             ("2048", "FLETES FORANEOS")])
    assert insumos.leer_archivo(ruta) == [("1024", "LIMPIEZA DE OFICINAS"),
                                          ("2048", "FLETES FORANEOS")]


def probar_una_lista_de_una_sola_columna_sigue_siendo_el_nombre():
    """el caso más común, y no puede confundirse con un folio"""
    from core import insumos

    ruta = _archivo_insumos([("LIMPIEZA DE OFICINAS",), ("FLETES FORANEOS",)])
    assert insumos.leer_archivo(ruta) == [("", "LIMPIEZA DE OFICINAS"),
                                          ("", "FLETES FORANEOS")]


def probar_el_folio_que_excel_guardo_como_numero_se_limpia():
    """un «1024.0» no lo encuentra SIPP"""
    from core import insumos

    ruta = _archivo_insumos([("Folio", "Insumo"), ("1024.0", "LIMPIEZA")])
    assert insumos.leer_archivo(ruta) == [("1024", "LIMPIEZA")]


def probar_el_folio_se_guarda_y_se_puede_consultar():
    """es lo que el robot le dará a SIPP para encontrarlo"""
    from core import insumos

    insumos.importar([("1024", "LIMPIEZA DE OFICINAS")], reemplazar=True)
    assert insumos.clave_de("LIMPIEZA DE OFICINAS") == "1024"
    # Y se encuentra aunque el nombre venga escrito de otra forma.
    assert insumos.clave_de("limpieza de oficínas") == "1024"


def probar_un_insumo_sin_folio_no_rompe_nada():
    """el catálogo puede no traerlo: se busca por nombre, como antes"""
    from core import insumos

    insumos.importar(["LIMPIEZA DE OFICINAS"], reemplazar=True)
    assert insumos.clave_de("LIMPIEZA DE OFICINAS") == ""
