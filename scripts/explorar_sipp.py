"""Explora el formulario de SIPP en vivo, SIN capturar ni guardar nada.

Es el paso que las páginas guardadas no pueden cubrir: ejercita los desplegables
«chosen» de verdad —abrir el widget, teclear, elegir la opción— y saca el
catálogo de **conceptos de pago** de una empresa, que es específico de cada una y
no está documentado en ningún lado.

Sirve para dos cosas:

  1. Validar `SesionSipp.seleccionar_chosen` y `abrir_pestana` contra el portal,
     que es lo único del motor que sigue sin probarse.
  2. Averiguar qué conceptos existen realmente, para poder llenar la columna
     «Concepto de pago» de la plantilla con un valor que SIPP vaya a encontrar.

No pulsa Guardar ni Solicitar Autorización: al terminar cierra el navegador y
deja el formulario como estaba.

Uso:
    python scripts/explorar_sipp.py                       # Aske / Corporativo
    python scripts/explorar_sipp.py "Petroplazas" "Mazatlan" "Proveedor"
    python scripts/explorar_sipp.py --produccion          # OJO: contra el real
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import catalogos, credenciales, selectores  # noqa: E402
from core.rpa_sipp import FlujoSolicitudPago, SesionSipp  # noqa: E402


def _bitacora(paso: str, mensaje: str, nivel: str, _captura: str) -> None:
    if mensaje:
        marca = {"INFO": "   ", "WARN": " ! ", "ERROR": " X "}.get(nivel, "   ")
        print(f"{marca}{paso}: {mensaje}", flush=True)


def main(argv: list[str]) -> int:
    produccion = "--produccion" in argv
    args = [a for a in argv if not a.startswith("--")]
    empresa = args[0] if args else "Aske"
    sucursal = args[1] if len(args) > 1 else "Corporativo"
    beneficiario = args[2] if len(args) > 2 else "Acreedor"

    datos = credenciales.cargar()
    if not datos or not datos[0]:
        print("XX  No hay credenciales guardadas. Captúralas en Configuración ⚙ "
              "de la aplicación.")
        return 1
    usuario, contrasena = datos

    ambiente = "PRODUCCION" if produccion else "PRUEBAS"
    url = catalogos.AMBIENTES[ambiente]
    print(f"Ambiente : {ambiente}  ({url})")
    print(f"Usuario  : {usuario}")
    print(f"Contexto : {empresa} / {sucursal} / {beneficiario}\n")
    if produccion:
        # Explorar no guarda nada, pero abrir producción por error y ponerse a
        # teclear en un formulario real merece una confirmación explícita.
        if input("Vas a abrir PRODUCCIÓN. Escribe SI para continuar: ") != "SI":
            return 1

    with SesionSipp(url, visible=True, on_bitacora=_bitacora) as s:
        print("→ Entrando…")
        s.login(usuario, contrasena)
        s.configurar_sesion(empresa, sucursal)
        s.ir_a_solicitud_pago()
        flujo = FlujoSolicitudPago(s)
        flujo.asegurar_modo_agregar()
        print("OK  Formulario de captura abierto.\n")

        # --- Los desplegables «chosen», que es lo que había que probar ---
        print("→ Probando los desplegables (esto es lo que los fixtures no "
              "pueden cubrir):")
        for clave, valor, etiqueta in (
            ("sol.empresa", empresa, "Empresa"),
            ("sol.sucursal", sucursal, "Sucursal"),
            ("sol.tipo_pago", catalogos.TIPO_PAGO_SOPORTADO, "Tipo de pago"),
            ("sol.tipo_beneficiario", beneficiario, "Tipo de beneficiario"),
        ):
            try:
                s.seleccionar_chosen(clave, valor, etiqueta)
                print(f"OK  {etiqueta} = «{valor}»")
            except Exception as exc:  # noqa: BLE001 — se reporta y se sigue
                print(f"XX  {etiqueta} = «{valor}»  ->  {exc}")
                s.diagnostico(f"chosen_{clave}")
                return 1
        s.page.wait_for_timeout(1500)
        s.cerrar_alertas()

        # --- Avisos de SIPP tras elegir empresa y beneficiario ---
        avisos = s.texto_alertas()
        if avisos.strip():
            print(f"\n!   SIPP mostró un aviso: {' '.join(avisos.split())[:200]}")

        # --- Pestañas realmente presentes con esta combinación ---
        # Va ANTES que los conceptos: qué pestañas existen depende del tipo de
        # beneficiario, y saberlo explica por qué el grid siguiente puede no
        # estar. Si esto fallara, lo demás no se podría interpretar.
        tira = s.page.locator(selectores.css("tabs.tira"))
        etiquetas = [tira.nth(i).inner_text().strip() for i in range(tira.count())]
        print(f"\n→ Pestañas con {beneficiario}: {etiquetas}")
        for nombre, etiqueta in selectores.pestanas().items():
            presente = any(etiqueta.lower() in e.lower() for e in etiquetas)
            print(f"    {'OK ' if presente else '-- '} «{etiqueta}» ({nombre})")

        # --- Catálogo real de conceptos de pago de esta empresa ---
        if any("concepto" in e.lower() for e in etiquetas):
            print("\n→ Conceptos de pago disponibles para esta empresa:")
            try:
                s.abrir_pestana("conceptos")
                grid = s.loc("con.filas")
                for _ in range(24):
                    if grid.count() > 0:
                        break
                    s.page.wait_for_timeout(500)
                total = grid.count()
                if total == 0:
                    print("    (ninguno: esta empresa no tiene conceptos "
                          f"asignados para {beneficiario}; sus solicitudes se "
                          f"guardarían en $0)")
                for i in range(total):
                    try:
                        texto = grid.nth(i).locator(
                            selectores.css("con.nombre")).first.inner_text()
                        print(f"    {i + 1:>3}. {texto.strip()}")
                    except Exception:  # noqa: BLE001 — renglón ilegible
                        continue
                print(f"    → {total} concepto(s).")
            except Exception as exc:  # noqa: BLE001 — informativo, no fatal
                print(f"XX  No se pudo leer el grid de conceptos: {exc}")
        else:
            print(f"\n→ Con {beneficiario} NO hay pestaña de Conceptos de Pago: "
                  f"el desglose de este tipo de beneficiario va por otro lado.")

        # --- Grid de insumos, si su pestaña está ---
        if any("insumo" in e.lower() for e in etiquetas):
            print("\n→ Pestaña de Insumos presente; revisando su grid:")
            try:
                s.abrir_pestana("insumos")
                s.page.wait_for_timeout(1200)
                print(f"    tipo de compra visible: {s.visible_('ins.tipo_compra')}")
                print(f"    botón agregar insumo:   {s.visible_('ins.agregar')}")
                opciones = [o.strip() for o in s.loc("ins.tipo_compra").first
                            .locator("option").all_inner_texts()
                            if o.strip() and o.strip() != "Seleccionar"]
                print(f"    tipos de compra: {opciones}")
            except Exception as exc:  # noqa: BLE001
                print(f"XX  No se pudo revisar el grid de insumos: {exc}")

        # --- Opciones de los catálogos que la plantilla ofrece ---
        print("\n→ Opciones reales vs. las que ofrece la plantilla:")
        for clave, esperadas, etiqueta in (
            ("sol.forma_pago", catalogos.FORMAS_PAGO, "Forma de pago"),
            ("sol.tipo_gasto", catalogos.TIPOS_GASTO, "Tipo de gasto"),
        ):
            try:
                opciones = [o.strip() for o in
                            s.loc(clave).first.locator("option").all_inner_texts()
                            if o.strip() and o.strip() != "Seleccionar"]
                faltan = [e for e in esperadas if e not in opciones]
                print(f"    {etiqueta}: {opciones}")
                if faltan:
                    print(f"      ! la plantilla ofrece valores que SIPP no "
                          f"tiene: {faltan}")
            except Exception as exc:  # noqa: BLE001
                print(f"    {etiqueta}: no se pudo leer ({exc})")

        print("\nListo. No se guardó nada; se cierra el navegador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
