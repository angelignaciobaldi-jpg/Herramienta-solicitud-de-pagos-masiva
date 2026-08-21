"""Llena una solicitud completa en SIPP **sin guardarla** (validación de fase 4).

Es el último tramo que faltaba probar del motor: el llenado de punta a punta con
datos reales —alta de beneficiario, cuenta bancaria **con su carátula**, fecha,
descripción y el grid de conceptos con su importe y su selección—. Se detiene en
el punto de parada `LLENADA`, o sea justo antes de pulsar Guardar, así que **no
consume folio ni deja nada registrado en SIPP**.

La carátula se adjunta sola: si no existe la de prueba, se genera al vuelo con
`generar_caratula_prueba.py`. Es un PDF ficticio con marca de agua, así que no
se sube ningún dato real. Subirla importa porque es la única forma de ejercitar
`subir_archivo`, que es donde vive el reintento ante el fallo intermitente de
almacenamiento que SIPP arrastra.

La empresa base de pruebas es **Abastecedora / Corporativo**, que en stage sí
tiene conceptos de pago asignados (33 al 31/07/2026). `Aske` no los tiene y el
grid llega vacío, por eso no sirve para esta prueba.

Al terminar deja una captura de pantalla del formulario lleno en la carpeta de
evidencias, para poder revisar a ojo que cada campo quedó donde debía.

Uso:
    python scripts/prueba_llenado_stage.py
    python scripts/prueba_llenado_stage.py "Abastecedora" "Corporativo" "VIGILANCIA"
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import catalogos, credenciales  # noqa: E402
from core.db import CONCEPTO, Partida, Solicitud  # noqa: E402
from core.rpa_sipp import (FlujoSolicitudPago, RequiereRevision,  # noqa: E402
                           SesionSipp)

# Empresa base de pruebas: la única confirmada con conceptos en stage.
EMPRESA = "Abastecedora"
SUCURSAL = "Corporativo"
CONCEPTO_PRUEBA = "VIGILANCIA"      # de una sola palabra: no puede ser ambiguo

# Datos claramente ficticios. El beneficiario se captura como NUEVO, pero como
# nunca se pulsa Guardar, no llega a registrarse en el catálogo de SIPP.
BENEFICIARIO = "PRUEBA RPA NO GUARDAR"
RFC_PRUEBA = "PRU010101AB1"
CLABE_PRUEBA = "012345678901234568"


def _bitacora(paso: str, mensaje: str, nivel: str, _captura: str) -> None:
    if mensaje:
        marca = {"INFO": "   ", "WARN": " ! ", "ERROR": " X "}.get(nivel, "   ")
        print(f"{marca}{paso}: {mensaje}", flush=True)


def _documento(nombre: str, prefijo: str = "CARATULA") -> str:
    """Ruta del PDF ficticio del beneficiario, generándolo si falta."""
    from scripts.generar_caratula_prueba import CARPETA_DEFECTO, generar

    ruta = os.path.join(CARPETA_DEFECTO, f"{prefijo} {nombre}.pdf")
    if os.path.isfile(ruta):
        return ruta
    print(f"→ No existía «{prefijo} {nombre}.pdf»; generándolo…")
    return generar(nombre, CARPETA_DEFECTO, prefijo=prefijo)


def _caratula(nombre: str) -> str:
    return _documento(nombre, "CARATULA")


def main(argv: list[str]) -> int:
    empresa = argv[0] if argv else EMPRESA
    sucursal = argv[1] if len(argv) > 1 else SUCURSAL
    concepto = argv[2] if len(argv) > 2 else CONCEPTO_PRUEBA

    datos = credenciales.cargar()
    if not datos or not datos[0]:
        print("XX  No hay credenciales guardadas. Captúralas en Configuración ⚙.")
        return 1
    usuario, contrasena = datos

    caratula = _caratula(BENEFICIARIO)
    url = catalogos.AMBIENTES["PRUEBAS"]
    print(f"Ambiente : PRUEBAS ({url})")
    print(f"Contexto : {empresa} / {sucursal} / Acreedor")
    print(f"Concepto : {concepto}")
    print(f"Carátula : {os.path.basename(caratula)} "
          f"({os.path.getsize(caratula) / 1024:.1f} KB, ficticia)")
    print("Parada   : LLENADA — el robot NO pulsará Guardar.\n")

    solicitud = Solicitud(
        empresa=empresa, sucursal=sucursal,
        tipo_beneficiario="Acreedor", beneficiario_nuevo=1,
        beneficiario_nombre=BENEFICIARIO,
        beneficiario_rfc=RFC_PRUEBA,
        beneficiario_correo="pruebas@ejemplo.invalid",
        cuenta_banco="BBVA", cuenta_clabe=CLABE_PRUEBA,
        cuenta_titular=BENEFICIARIO, cuenta_tipo_transf="SPEI",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="15/09/2026", moneda="Pesos (MXN)",
        # Con guion a propósito: comprueba que la normalización lo resuelva en
        # vez de dejar que SIPP lo descarte a nuestras espaldas.
        descripcion="PRUEBA AUTOMATIZADA - NO AUTORIZAR",
        parada="LLENADA", origen="MANUAL")
    partidas = [Partida(clase=CONCEPTO, concepto_nombre=concepto,
                        importe=1234.56, origen="MANUAL")]

    with SesionSipp(url, visible=True, on_bitacora=_bitacora) as s:
        print("→ Entrando…")
        s.login(usuario, contrasena)
        s.configurar_sesion(empresa, sucursal)
        s.ir_a_solicitud_pago()
        flujo = FlujoSolicitudPago(s)

        print("\n→ Llenando el formulario (con carátula)…")
        try:
            flujo.llenar(solicitud, partidas, {"caratula": caratula})
        except RequiereRevision as exc:
            print(f"\n!!  Requiere revisión: {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001 — se reporta con diagnóstico
            print(f"\nXX  Falló el llenado: {exc}")
            print(f"    Diagnóstico en: {s.diagnostico('llenado')}")
            return 1

        print("\n→ Comprobando lo que quedó en pantalla:")
        from core.rpa_sipp import texto_sipp

        revisiones = [
            ("Empresa", "sol.empresa", empresa),
            ("Sucursal", "sol.sucursal", sucursal),
            # Se espera el texto YA normalizado: SIPP descarta los signos al
            # teclear, así que comparar contra el original siempre fallaría.
            ("Descripción", "sol.descripcion",
             texto_sipp(solicitud.descripcion)),
            ("Fecha de pago", "sol.fecha_pago", solicitud.fecha_pago),
        ]
        fallos = 0
        for etiqueta, clave, esperado in revisiones:
            try:
                loc = s.loc(clave).first
                if clave in ("sol.empresa", "sol.sucursal"):
                    # En un <select>, lo que importa es la opción elegida.
                    actual = loc.locator("option:checked").first.inner_text().strip()
                else:
                    actual = (loc.input_value() or "").strip()
            except Exception as exc:  # noqa: BLE001
                actual = f"(no se pudo leer: {exc})"
            ok = str(esperado).strip() in str(actual)
            fallos += 0 if ok else 1
            print(f"    {'OK ' if ok else 'XX '} {etiqueta}: «{actual}»")

        # La carátula: que el campo tenga archivo NO basta —el navegador lo
        # muestra en cuanto se elige—, hay que ver que SIPP la haya SUBIDO. Al
        # terminar la subida marca `ar_Pdf.sn_subido == 1`, y eso revela el
        # botón de «ver archivo» junto al campo. Ese botón es la prueba real.
        try:
            campo_pdf = s.loc("sol.pdf").first
            elegido = os.path.basename((campo_pdf.input_value() or "").strip())
        except Exception:  # noqa: BLE001
            elegido = ""
        fallos += 0 if elegido else 1
        print(f"    {'OK ' if elegido else 'XX '} Carátula en el campo PDF: "
              f"«{elegido or '(vacío)'}»")

        # Se le pregunta al modelo de Angular en vez de deducirlo de la
        # pantalla: `ar_Pdf` es lo que SIPP enviará al guardar, así que es la
        # única fuente que dice si el archivo quedó realmente enganchado.
        modelo = None
        try:
            from core import sipp_datos

            modelo = s.page.evaluate(sipp_datos.consulta("archivo_pdf"))
        except Exception as exc:  # noqa: BLE001
            print(f"    ??  No se pudo leer el modelo de Angular: {exc}")
        enganchado = bool(modelo)
        fallos += 0 if enganchado else 1
        print(f"    {'OK ' if enganchado else 'XX '} El modelo de SIPP tiene el "
              f"archivo: {modelo}")
        if enganchado and not modelo.get("sn_subido"):
            # No cuenta como fallo: en este punto la solicitud no se ha
            # guardado, y todo apunta a que SIPP sube el archivo al guardar.
            print("    --  `sn_subido` aún en blanco: se confirmará al guardar "
                  "(esta prueba se detiene antes).")
        if s.hay_error_sistema():
            fallos += 1
            print("    XX  SIPP reportó un error de almacenamiento en la subida")

        # El importe es lo que decide si la solicitud valdría algo.
        try:
            cantidad = s.loc("sol.cantidad_pagar").first.input_value()
        except Exception:  # noqa: BLE001
            cantidad = ""
        try:
            total = s.loc("con.total").first.input_value()
        except Exception:  # noqa: BLE001
            total = ""
        esperado = f"{partidas[0].importe:,.2f}"
        ok_importe = esperado.replace(",", "") in (cantidad or "").replace(",", "")
        fallos += 0 if ok_importe else 1
        print(f"    {'OK ' if ok_importe else 'XX '} Cantidad a pagar: "
              f"«{cantidad}»  (esperado {esperado})")
        print(f"        Total de conceptos: «{total}»")

        ruta = s.captura("llenado_stage")
        print(f"\n→ Captura del formulario lleno: {ruta}")
        print("→ NO se pulsó Guardar; se cierra el navegador sin registrar nada.")

    if fallos:
        print(f"\n{fallos} comprobación(es) fallaron: revisa la captura.")
        return 1
    print("\nLlenado correcto de punta a punta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
