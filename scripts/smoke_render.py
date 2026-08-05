"""Smoke test de RENDER: abre la app de verdad y pinta cada pantalla.

Complementa a `smoke_import.py`, que solo comprueba que los módulos importen.
Existe por un fallo real: `ft.SegmentedButton(selected={...})` con un `set` se
construye sin quejarse en Python, pero al enviarse al cliente revienta el
empaquetado msgpack, tumba la actualización COMPLETA de la página y deja la
ventana **en blanco, sin ningún error a la vista**. Un test que solo construya
controles no lo detecta: hay que serializarlos.

Ejecuta:  python scripts/smoke_render.py
Sale con código 1 si alguna pantalla no se pudo pintar.

Requiere una sesión de escritorio (abre una ventana unos segundos). No está
conectado al CI por eso mismo; córrelo en local antes de publicar.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)
os.chdir(_RAIZ)

import flet as ft  # noqa: E402

_fallos: list[str] = []
_pasos: list[str] = []


def _ok(paso: str) -> None:
    _pasos.append(paso)
    # `flush` explícito: al redirigir la salida a un archivo, lo que quedara en
    # el búfer se perdería si la ventana no cierra y hay que matar el proceso.
    print(f"OK  {paso}", flush=True)


def _falla(paso: str, exc: BaseException) -> None:
    detalle = f"{type(exc).__name__}: {exc}"
    _fallos.append(f"{paso} -> {detalle}")
    print(f"XX  {paso}  ->  {detalle}", flush=True)


def _hijos(control) -> list:
    """Hijos de un control, mirando los nombres que usa Flet según el tipo."""
    salida = []
    for attr in ("controls", "content", "segments", "actions", "title"):
        valor = getattr(control, attr, None)
        if isinstance(valor, (list, tuple)):
            salida += [c for c in valor if isinstance(c, ft.Control)]
        elif isinstance(valor, ft.Control):
            salida.append(valor)
    return salida


def _revisar_wrap_con_expand(nombre: str, raiz) -> None:
    """Caza `Expanded` dentro de `Wrap`, que Flutter no admite.

    Un `Row(wrap=True)` con un hijo `expand` lanza un error de ParentDataWidget
    en el cliente y se dibuja como un RECTÁNGULO GRIS que tapa el resto de la
    pantalla. El error se queda del lado de Flutter y nunca vuelve a Python, así
    que pintar la pantalla no basta para detectarlo: hay que revisar el árbol.
    """
    pila = [raiz]
    vistos = set()
    while pila:
        ctrl = pila.pop()
        if id(ctrl) in vistos:
            continue
        vistos.add(id(ctrl))
        hijos = _hijos(ctrl)
        if isinstance(ctrl, ft.Row) and getattr(ctrl, "wrap", False):
            culpables = [c for c in hijos if getattr(c, "expand", None)]
            if culpables:
                _falla(
                    f"layout {nombre}",
                    ValueError(
                        f"un Row(wrap=True) tiene {len(culpables)} hijo(s) con "
                        f"expand: {[type(c).__name__ for c in culpables]}. "
                        "Anida: el Row exterior sin wrap lleva el expand; el "
                        "interior envuelve pero sin hijos expandibles."))
                return
        pila += hijos
    _ok(f"layout {nombre}")


async def _pintar(page: ft.Page, nombre: str, control: ft.Control) -> None:
    """Monta un control como ÚNICO contenido de la página y lo envía al cliente.

    El envío es lo que importa: es donde se detectan los valores que el cliente
    no puede recibir.
    """
    try:
        page.controls.clear()
        page.add(control)
        page.update()
        await asyncio.sleep(0.4)
        _ok(f"render {nombre}")
    except Exception as exc:  # noqa: BLE001 — se reporta y se sigue con la otra
        _falla(f"render {nombre}", exc)


async def _principal(page: ft.Page) -> None:
    try:
        from core import db
        from ui import tema

        page.title = "Smoke de render"
        page.window.width, page.window.height = 1280, 800
        barra = ft.ScrollbarTheme(thumb_visibility=True, thickness=12)
        page.theme = tema.construir_tema(False, barra)
        page.dark_theme = tema.construir_tema(True, barra)
        db.inicializar()
        _ok("tema y base de datos")

        import app as shell
        from ui.bitacora import SeccionBitacora
        from ui.configuracion import SeccionConfiguracion
        from ui.documentos import SeccionDocumentos
        from ui.solicitudes import SeccionSolicitudes

        # Shell a medio construir: se necesita un `app` con page, picker y la
        # lista de listeners, que es lo único que las pantallas le piden.
        app_obj = shell.AppSolicitudesPago.__new__(shell.AppSolicitudesPago)
        app_obj.page = page
        app_obj.picker = ft.FilePicker()
        page.services.append(app_obj.picker)
        app_obj._on_resize_cbs = []
        _ok("shell base")

        pantallas = []
        from ui.conceptos import SeccionConceptos

        for nombre, clase in (("Configuración", SeccionConfiguracion),
                              ("Documentos", SeccionDocumentos),
                              ("Solicitudes", SeccionSolicitudes),
                              ("Conceptos", SeccionConceptos),
                              ("Bitácora", SeccionBitacora)):
            try:
                pantallas.append((nombre, clase(app_obj)))
                _ok(f"construir {nombre}")
            except Exception as exc:  # noqa: BLE001
                _falla(f"construir {nombre}", exc)

        for nombre, pantalla in pantallas:
            contenido = getattr(pantalla, "contenido", None)
            if contenido is not None:
                _revisar_wrap_con_expand(nombre, contenido)
                await _pintar(page, nombre, contenido)
            dialogo = getattr(pantalla, "dialogo", None)
            if dialogo is not None:
                _revisar_wrap_con_expand(f"{nombre} (modal)", dialogo)
            cargar = getattr(pantalla, "cargar_desde_db", None)
            if callable(cargar):
                try:
                    cargar()
                    page.update()
                    await asyncio.sleep(0.3)
                    _ok(f"cargar_desde_db {nombre}")
                except Exception as exc:  # noqa: BLE001
                    _falla(f"cargar_desde_db {nombre}", exc)

        # Los modales viven fuera del árbol de la página: se abren aparte.
        for nombre, pantalla in pantallas:
            for attr in ("dialogo", "dialogo_alta"):
                dialogo = getattr(pantalla, attr, None)
                if dialogo is None or attr == "dialogo":
                    continue
                try:
                    _revisar_wrap_con_expand(f"{nombre} ({attr})", dialogo)
                    page.show_dialog(dialogo)
                    page.update()
                    await asyncio.sleep(0.4)
                    page.pop_dialog()
                    page.update()
                    _ok(f"modal {nombre} · {attr}")
                except Exception as exc:  # noqa: BLE001
                    _falla(f"modal {nombre} · {attr}", exc)
            dialogo = getattr(pantalla, "dialogo", None)
            if dialogo is not None:
                try:
                    page.show_dialog(dialogo)
                    page.update()
                    await asyncio.sleep(0.4)
                    page.pop_dialog()
                    page.update()
                    _ok(f"modal {nombre}")
                except Exception as exc:  # noqa: BLE001
                    _falla(f"modal {nombre}", exc)

        # Los modales pesados del proyecto: el formulario de captura y la carga
        # masiva. Van aparte porque no cuelgan del árbol de su pantalla.
        from core.db import Lote

        lote = db.listar_lotes()
        lote_id = lote[0].id if lote else db.guardar_lote(Lote()).id

        try:
            from ui.captura_solicitud import CapturaSolicitud

            captura = CapturaSolicitud(app_obj, lambda *_a: None)
            _revisar_wrap_con_expand("Captura de solicitud", captura.modal.dialogo)
            captura.abrir(lote_id)
            page.update()
            await asyncio.sleep(0.5)
            captura.modal.cerrar()
            _ok("modal Captura de solicitud (alta)")
        except Exception as exc:  # noqa: BLE001
            _falla("modal Captura de solicitud (alta)", exc)

        # EDICIÓN, y con archivos adjuntos. Abrirlo vacío no basta: hay ramas que
        # solo se ejecutan cuando la solicitud ya trae carátula o Vo.Bo., y ahí
        # se escondía un `NameError` que el alta nunca tocaba.
        try:
            import tempfile

            from core import documentos
            from core.db import CONCEPTO, Partida, Solicitud
            from ui.captura_solicitud import CapturaSolicitud

            # Se REUSA la de una corrida anterior si existe. El smoke trabaja
            # contra la base real de desarrollo, y crear siempre una solicitud
            # nueva chocaría con la clave de idempotencia la segunda vez: un
            # smoke que solo pasa la primera vez no sirve de nada.
            NOMBRE_PRUEBA = "PRUEBA DE RENDER"
            solicitud = next(
                (s for s in db.listar_solicitudes(lote_id)
                 if s.beneficiario_nombre == NOMBRE_PRUEBA), None)
            if solicitud is None:
                solicitud = Solicitud(
                    lote_id=lote_id, empresa="Aske", sucursal="Corporativo",
                    tipo_beneficiario="Acreedor",
                    beneficiario_nombre=NOMBRE_PRUEBA,
                    beneficiario_rfc="XAXX010101000",
                    beneficiario_correo="x@ejemplo.invalid",
                    cuenta_clabe="012345678901234567", cuenta_banco="BBVA",
                    forma_pago="Transferencia", tipo_gasto="No Deducible",
                    fecha_pago="20/09/2026", descripcion="Prueba de render")
                solicitud = db.guardar_solicitud(
                    solicitud, [Partida(clase=CONCEPTO,
                                        concepto_nombre="PRUEBA",
                                        importe=1000.0)])

            # Dos archivos de mentira, solo para que existan en disco.
            carpeta = tempfile.mkdtemp()
            adjuntos = []
            for tipo, nombre in ((documentos.TIPO_CARATULA, "caratula.pdf"),
                                 (documentos.TIPO_VOBO, "vobo.pdf")):
                ruta_pdf = os.path.join(carpeta, nombre)
                with open(ruta_pdf, "wb") as fh:
                    fh.write(b"%PDF-1.4\n")
                documentos.registrar(solicitud.id, ruta_pdf, tipo, lote_id)
                adjuntos.append(nombre)

            captura2 = CapturaSolicitud(app_obj, lambda *_a: None)
            captura2.abrir(lote_id, solicitud, db.listar_partidas(solicitud.id))
            page.update()
            await asyncio.sleep(0.5)
            # Y que de verdad los muestre: si la rama no se ejecutó, no sirvió.
            visto = (captura2.txt_caratula.value or "")
            if "caratula.pdf" not in visto:
                raise AssertionError(
                    f"el modal no mostró la carátula adjunta (dice «{visto}»)")
            captura2.modal.cerrar()
            _ok(f"modal Captura de solicitud (edición con {len(adjuntos)} "
                f"adjunto(s))")
            # Se limpia lo que creó: el smoke no debe dejar basura en la base
            # de desarrollo del usuario.
            db.borrar_solicitud(solicitud.id)
        except Exception as exc:  # noqa: BLE001
            _falla("modal Captura de solicitud (edición con adjuntos)", exc)

        try:
            from ui.carga_masiva import CargaMasiva

            carga = CargaMasiva(app_obj, lambda *_a: None)
            _revisar_wrap_con_expand("Carga masiva", carga.modal.dialogo)
            carga.abrir(lote_id)
            page.update()
            await asyncio.sleep(0.5)
            carga.modal.cerrar()
            _ok("modal Carga masiva")
        except Exception as exc:  # noqa: BLE001
            _falla("modal Carga masiva", exc)

        try:
            from ui.alta_caratulas import AltaDesdeCaratulas

            alta = AltaDesdeCaratulas(app_obj, lambda *_a: None)
            _revisar_wrap_con_expand("Alta desde carátulas", alta.modal.dialogo)
            alta.abrir(lote_id)
            page.update()
            await asyncio.sleep(0.5)
            alta.modal.cerrar()
            _ok("modal Alta desde carátulas")
        except Exception as exc:  # noqa: BLE001
            _falla("modal Alta desde carátulas", exc)

        try:
            from ui.asignacion_masiva import AsignacionMasiva

            masiva = AsignacionMasiva(app_obj, lambda *_a: None)
            _revisar_wrap_con_expand("Asignación masiva", masiva.modal.dialogo)
            masiva.abrir(lote_id, set())
            page.update()
            await asyncio.sleep(0.5)
            masiva.modal.cerrar()
            _ok("modal Asignación masiva")
        except Exception as exc:  # noqa: BLE001
            _falla("modal Asignación masiva", exc)

    except Exception as exc:  # noqa: BLE001 — cualquier fallo no previsto
        _falla("arranque", exc)
        traceback.print_exc()
    finally:
        await asyncio.sleep(0.2)
        # `Window.close` es una CORRUTINA: sin await, la ventana no cierra,
        # `ft.run` nunca regresa y el script se queda colgado para siempre.
        await page.window.close()


def main() -> int:
    ft.run(_principal)
    print()
    if _fallos:
        print("Pantallas que NO se pudieron pintar:")
        for f in _fallos:
            print(f"  - {f}")
        return 1
    print(f"Todas las pantallas se pintaron correctamente ({len(_pasos)} pasos).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
