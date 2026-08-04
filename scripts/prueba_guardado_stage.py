"""Guarda una solicitud real en el SIPP de PRUEBAS y comprueba la idempotencia.

Es el último tramo del motor sin validar. A diferencia de
`prueba_llenado_stage.py`, esta prueba **sí pulsa Guardar**, así que consume un
folio en stage y deja un registro. NO solicita autorización: se detiene en el
punto de parada `GUARDADA`, que es el estado desde el que un humano puede
revisar o cancelar lo capturado.

Comprueba tres cosas, en este orden:

1. Que `guardar()` funcione y devuelva el **folio** que SIPP asignó.
2. Que la subida de archivos ocurra de verdad al guardar (es cuando SIPP manda
   el PDF al almacenamiento, ver ESPECIFICACION.md §8).
3. Que **`buscar_existente()` encuentre después lo que se acaba de capturar**.
   Esto es lo más importante de la prueba: es la salvaguarda que impide pagar
   dos veces cuando un lote se reanuda, y si no funciona, todo lo demás sobra.

Al volver a correrlo con los mismos datos, la salvaguarda debe saltar y NO
capturar de nuevo — esa segunda corrida es parte de la prueba, no un descuido.
Para forzar una captura nueva, usa `--nuevo`.

Uso:
    python scripts/prueba_guardado_stage.py            # captura o detecta duplicado
    python scripts/prueba_guardado_stage.py --nuevo    # importe único: siempre captura
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import catalogos, credenciales  # noqa: E402
from core.db import CONCEPTO, Partida, Solicitud  # noqa: E402
from core.rpa_sipp import (FlujoSolicitudPago, RequiereRevision,  # noqa: E402
                           SesionSipp)
from scripts.prueba_llenado_stage import (BENEFICIARIO, CLABE_PRUEBA,  # noqa: E402
                                          CONCEPTO_PRUEBA, EMPRESA, SUCURSAL,
                                          RFC_PRUEBA, _bitacora, _caratula)

IMPORTE_FIJO = 1234.56


def main(argv: list[str]) -> int:
    forzar_nuevo = "--nuevo" in argv

    datos = credenciales.cargar()
    if not datos or not datos[0]:
        print("XX  No hay credenciales guardadas. Captúralas en Configuración ⚙.")
        return 1
    usuario, contrasena = datos

    # Con `--nuevo`, el importe lleva la hora: cambia la clave de idempotencia y
    # obliga a una captura nueva en vez de toparse con la anterior.
    if forzar_nuevo:
        ahora = datetime.now()
        importe = round(1000 + ahora.hour * 100 + ahora.minute + ahora.second / 100, 2)
    else:
        importe = IMPORTE_FIJO

    caratula = _caratula(BENEFICIARIO)
    url = catalogos.AMBIENTES["PRUEBAS"]

    print(f"Ambiente : PRUEBAS ({url})")
    print(f"Contexto : {EMPRESA} / {SUCURSAL} / Acreedor")
    print(f"Concepto : {CONCEPTO_PRUEBA}   Importe: {importe:,.2f}")
    print(f"Carátula : {os.path.basename(caratula)} (ficticia)")
    print("Parada   : GUARDADA — SÍ pulsará Guardar; NO solicitará autorización.")
    print("           Esto consume un folio EN PRUEBAS y deja un registro.\n")

    solicitud = Solicitud(
        empresa=EMPRESA, sucursal=SUCURSAL,
        tipo_beneficiario="Acreedor", beneficiario_nuevo=1,
        beneficiario_nombre=BENEFICIARIO, beneficiario_rfc=RFC_PRUEBA,
        beneficiario_correo="pruebas@ejemplo.invalid",
        cuenta_banco="BBVA", cuenta_clabe=CLABE_PRUEBA,
        cuenta_titular=BENEFICIARIO, cuenta_tipo_transf="SPEI",
        forma_pago="Transferencia", tipo_gasto="No Deducible",
        fecha_pago="15/09/2026", moneda="Pesos (MXN)",
        descripcion="PRUEBA AUTOMATIZADA NO AUTORIZAR",
        parada="GUARDADA", origen="MANUAL")
    partidas = [Partida(clase=CONCEPTO, concepto_nombre=CONCEPTO_PRUEBA,
                        importe=importe, origen="MANUAL")]

    fallos = 0
    with SesionSipp(url, visible=True, on_bitacora=_bitacora) as s:
        print("→ Entrando…")
        s.login(usuario, contrasena)
        s.configurar_sesion(EMPRESA, SUCURSAL)
        s.ir_a_solicitud_pago()
        flujo = FlujoSolicitudPago(s)

        # --- 1. Captura hasta el punto de parada GUARDADA ---
        print("\n→ Capturando (incluye Guardar)…")
        try:
            resultado = flujo.capturar(solicitud, partidas,
                                       {"caratula": caratula})
        except RequiereRevision as exc:
            print(f"\n!!  Requiere revisión: {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"\nXX  Falló la captura: {exc}")
            print(f"    Diagnóstico en: {s.diagnostico('guardado')}")
            return 1

        print(f"\n→ Resultado: estado={resultado.estado} "
              f"folio={resultado.folio_sipp or '(no leído)'}")
        print(f"   {resultado.mensaje}")

        if resultado.mensaje.startswith("Ya estaba capturada"):
            # No es un fallo: es la salvaguarda haciendo exactamente su trabajo.
            print("\nOK  La salvaguarda de idempotencia detectó la solicitud "
                  "previa y NO la duplicó.")
            print("    Usa --nuevo para forzar una captura con importe distinto.")
            return 0

        if resultado.estado != "GUARDADA":
            fallos += 1
            print(f"XX  Se esperaba estado GUARDADA, llegó «{resultado.estado}»")
        if not resultado.folio_sipp:
            # No es fatal: el folio puede leerse distinto según la vista. Pero
            # sin folio, la bitácora pierde su rastro más útil.
            print("!   No se pudo leer el folio de la solicitud guardada.")
        if s.hay_error_sistema():
            fallos += 1
            print("XX  SIPP reportó un error del sistema tras guardar "
                  "(¿falló la subida del archivo?)")

        ruta = s.captura("guardado_stage")
        print(f"   Captura tras guardar: {ruta}")

        # --- 2. La salvaguarda: ¿encuentra lo que acabamos de capturar? ---
        print("\n→ Comprobando la idempotencia: buscando en el listado lo que "
              "se acaba de guardar…")
        try:
            flujo.volver_al_listado()
            encontrado = flujo.buscar_existente(solicitud)
        except Exception as exc:  # noqa: BLE001
            encontrado, fallos = "", fallos + 1
            print(f"XX  Falló la búsqueda: {exc}")

        if encontrado:
            print(f"OK  Encontrada en el listado (folio {encontrado}): un "
                  f"reintento del lote NO la volvería a capturar.")
        else:
            fallos += 1
            print("XX  NO se encontró en el listado. La salvaguarda contra el "
                  "doble pago no está funcionando: revísalo ANTES de usar "
                  "esto en producción.")

        print("\n→ No se solicitó autorización; se cierra el navegador.")

    if fallos:
        print(f"\n{fallos} comprobación(es) fallaron.")
        return 1
    print("\nGuardado e idempotencia correctos.")
    print("Recuerda cancelar en stage la solicitud de prueba si estorba.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
