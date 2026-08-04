"""Solicita autorización de una solicitud ya guardada en el SIPP de PRUEBAS.

Es el último paso del motor sin validar. Para no consumir un folio nuevo,
**reutiliza una solicitud que ya esté guardada** —la que dejó
`prueba_guardado_stage.py`—: la busca en el listado, la reabre con «Editar» y
pulsa «Solicitar Autorización» desde el formulario, que es exactamente el camino
que sigue el motor cuando el punto de parada es `AUTORIZAR`.

Comprueba que el **estatus cambie** de BORRADOR a lo que SIPP asigne al
enviarla. Sin esa comprobación, pulsar el botón y que no pase nada se vería
igual que un éxito.

Ojo: enviar a autorización **no se deshace** desde aquí. Es stage, pero si esa
solicitud le sirve a alguien más, avísale antes.

Uso:
    python scripts/prueba_autorizacion_stage.py
    python scripts/prueba_autorizacion_stage.py "OTRO BENEFICIARIO"
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import catalogos, credenciales, selectores  # noqa: E402
from core.rpa_sipp import (FlujoSolicitudPago, SesionSipp,  # noqa: E402
                           _comparable, _palabras_clave, texto_sipp)
from scripts.prueba_llenado_stage import (BENEFICIARIO, EMPRESA,  # noqa: E402
                                          SUCURSAL, _bitacora, _documento)

DESCRIPCION = "PRUEBA AUTOMATIZADA NO AUTORIZAR"


def _fila_de(s, beneficiario: str):
    """Índice del renglón del listado que corresponde al beneficiario, o -1."""
    objetivo = _palabras_clave(beneficiario)
    filas = s.loc("listado.filas")
    for i in range(filas.count()):
        palabras = set(_comparable(filas.nth(i).inner_text() or "").split())
        if objetivo and objetivo.issubset(palabras):
            return i
    return -1


def _texto_fila(s, indice: int) -> str:
    partes = [p.strip() for p in
              (s.loc("listado.filas").nth(indice).inner_text() or "").split("\n")
              if p.strip() and p.strip() != "\xa0"]
    return " | ".join(partes)


def main(argv: list[str]) -> int:
    beneficiario = argv[0] if argv else BENEFICIARIO

    datos = credenciales.cargar()
    if not datos or not datos[0]:
        print("XX  No hay credenciales guardadas. Captúralas en Configuración ⚙.")
        return 1
    usuario, contrasena = datos

    print(f"Ambiente     : PRUEBAS")
    print(f"Beneficiario : {beneficiario}")
    print("Acción       : reabrir una solicitud YA guardada y enviarla a "
          "autorización.")
    print("               No consume folio nuevo. No se puede deshacer.\n")

    fallos = 0
    with SesionSipp(catalogos.AMBIENTES["PRUEBAS"], visible=True,
                    on_bitacora=_bitacora) as s:
        print("→ Entrando…")
        s.login(usuario, contrasena)
        s.configurar_sesion(EMPRESA, SUCURSAL)
        s.ir_a_solicitud_pago()
        flujo = FlujoSolicitudPago(s)

        # --- Localizar la solicitud guardada ---
        print("\n→ Buscando la solicitud en el listado…")
        s.llenar("listado.filtro_descripcion", texto_sipp(DESCRIPCION),
                 "Filtro descripción")
        s.loc("listado.buscar").first.click()
        s.page.wait_for_timeout(3000)
        indice = _fila_de(s, beneficiario)
        if indice < 0:
            print(f"XX  No se encontró ninguna solicitud de «{beneficiario}». "
                  "Corre antes scripts/prueba_guardado_stage.py.")
            return 1
        antes = _texto_fila(s, indice)
        print(f"OK  Encontrada: {antes}")
        if "BORRADOR" not in antes.upper():
            print("!   No está en BORRADOR: quizá ya se envió a autorización. "
                  "La prueba seguirá, pero el cambio de estatus no probará nada.")

        # --- Reabrirla en el formulario ---
        print("\n→ Reabriendo la solicitud con «Editar»…")
        try:
            fila = s.loc("listado.filas").nth(indice)
            fila.locator(selectores.css("listado.editar")).first.click()
            s.page.wait_for_timeout(3000)
            s.cerrar_alertas()
        except Exception as exc:  # noqa: BLE001
            print(f"XX  No se pudo reabrir: {exc}")
            print(f"    Diagnóstico en: {s.diagnostico('reabrir')}")
            return 1

        if not s.visible_("acc.autorizar"):
            print("XX  El formulario no muestra «Solicitar Autorización». "
                  "¿La solicitud no está guardada, o el usuario no tiene el "
                  "permiso?")
            s.diagnostico("sin_boton_autorizar")
            return 1
        print("OK  Formulario abierto y el botón está disponible.")

        # --- El Vo.Bo., que SIPP exige antes de dejar autorizar ---
        vobo = _documento(beneficiario, "VOBO")
        print(f"\n→ Adjuntando el Vo.Bo. ({os.path.basename(vobo)})…")
        try:
            flujo.adjuntar_respaldo(vobo)
        except Exception as exc:  # noqa: BLE001
            print(f"XX  No se pudo adjuntar: {exc}")
            print(f"    Diagnóstico en: {s.diagnostico('vobo')}")
            return 1
        # Guardar de nuevo: el documento de respaldo se sube al guardar.
        print("→ Guardando para que suba el documento…")
        try:
            flujo.guardar()
        except Exception as exc:  # noqa: BLE001
            print(f"XX  Falló al reguardar: {exc}")
            return 1
        print("OK  Vo.Bo. adjuntado.")

        # --- El paso que se está probando ---
        print("\n→ Solicitando autorización…")
        try:
            flujo.solicitar_autorizacion()
        except Exception as exc:  # noqa: BLE001
            print(f"XX  Falló: {exc}")
            print(f"    Diagnóstico en: {s.diagnostico('autorizar')}")
            return 1
        ruta = s.captura("autorizacion_stage")
        print(f"   Captura tras autorizar: {ruta}")

        # --- Comprobar que el estatus cambió de verdad ---
        print("\n→ Verificando el estatus en el listado…")
        try:
            flujo.volver_al_listado()
            s.llenar("listado.filtro_descripcion", texto_sipp(DESCRIPCION),
                     "Filtro descripción")
            s.loc("listado.buscar").first.click()
            s.page.wait_for_timeout(3000)
            indice = _fila_de(s, beneficiario)
            despues = _texto_fila(s, indice) if indice >= 0 else ""
        except Exception as exc:  # noqa: BLE001
            despues, fallos = "", fallos + 1
            print(f"XX  No se pudo releer el listado: {exc}")

        print(f"   antes  : {antes}")
        print(f"   después: {despues or '(no se encontró)'}")
        if despues and "BORRADOR" in antes.upper() \
                and "BORRADOR" not in despues.upper():
            print("\nOK  El estatus cambió: la solicitud quedó enviada a "
                  "autorización.")
        elif despues and "BORRADOR" not in antes.upper():
            print("\n--  No se puede concluir: no estaba en BORRADOR al empezar.")
        else:
            fallos += 1
            print("\nXX  El estatus NO cambió: pulsar el botón no tuvo efecto.")

    if fallos:
        print(f"\n{fallos} comprobación(es) fallaron.")
        return 1
    print("\nSolicitar Autorización validado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
