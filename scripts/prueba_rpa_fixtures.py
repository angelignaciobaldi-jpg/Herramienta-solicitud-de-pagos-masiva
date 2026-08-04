"""Prueba del RPA contra las páginas guardadas del SIPP (fase 2).

Sirve las copias de `Paginas html/` en local y comprueba, sin tocar el sistema
real ni consumir folios:

  1. Que **cada selector del mapa resuelva** en la página que le corresponde.
     Es lo que detecta que SIPP cambió: al actualizar el fixture, esta prueba
     dice exactamente qué se rompió.
  2. Que el **acotado por panel** desambigüe los ids triplicados: varios campos
     del beneficiario existen tres veces, uno por tipo.
  3. Que las **pestañas** se localicen sin salirse del formulario.
  4. Que el **llenado de campos** funcione sobre el DOM real.

Lo que esta prueba NO puede cubrir, y hay que probar en stage:

  - Los `select` "chosen" y cualquier cosa que dependa de AngularJS: las páginas
    guardadas no traen el JavaScript del portal, así que los desplegables no
    filtran ni las listas dependientes se recargan.
  - Guardar, autorizar y subir archivos: no hay servidor detrás.
  - Los grids de Conceptos e Insumos con renglones: el fixture se capturó con un
    tipo de pago que no los muestra.

Ejecuta:  python scripts/prueba_rpa_fixtures.py
Sale con código 1 si algo falla.
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from playwright.sync_api import sync_playwright  # noqa: E402

from core import selectores  # noqa: E402
from scripts import servidor_fixtures as fx  # noqa: E402

_fallos: list[str] = []
_ok = 0


def comprobar(condicion: bool, descripcion: str, detalle: str = "") -> bool:
    global _ok
    if condicion:
        _ok += 1
        print(f"OK  {descripcion}", flush=True)
    else:
        _fallos.append(f"{descripcion}{f' — {detalle}' if detalle else ''}")
        print(f"XX  {descripcion}  {detalle}", flush=True)
    return condicion


# Qué claves deben resolver en cada página. Las que no se listan solo aparecen
# tras una interacción (un modal abierto, un grid con renglones) y no se pueden
# comprobar sobre una copia estática.
_ESPERADO: dict[str, list[str]] = {
    fx.LOGIN: ["login.usuario", "login.contrasena", "login.entrar",
               "login.modal_contrasena"],
    fx.EMPRESA: ["sesion.empresa", "sesion.sucursal", "sesion.guardar"],
    fx.AGREGAR: [
        "listado.crear", "listado.regresar", "listado.filtro_beneficiario",
        "listado.filtro_descripcion", "listado.filtro_desde",
        "listado.filtro_hasta", "listado.buscar",
        "sol.empresa", "sol.sucursal", "sol.tipo_pago", "sol.tipo_beneficiario",
        "sol.pdf", "sol.xml", "sol.tipo_gasto", "sol.forma_pago",
        "sol.cantidad_pagar", "sol.fecha_pago", "sol.moneda", "sol.descripcion",
        "ben.no_registrado", "ben.no_registrado_label", "ben.folio",
        "ben.buscar", "ben.cuentas", "ben.razon_social", "ben.rfc",
        "ben.agregar_cuenta", "ben.modal_busqueda", "ben.modal_nombre",
        "ben.modal_buscar",
        "cb.modal", "cb.banco", "cb.nombre_cuenta", "cb.moneda", "cb.clabe",
        "cb.tipo_transf", "cb.correo", "cb.caratula", "cb.guardar",
        "ins.tipo_compra", "ins.agregar", "ins.grid", "ins.modal",
        "ins.modal_folio", "ins.modal_nombre", "ins.modal_buscar",
        "cc.modal", "cc.modal_folio", "cc.modal_nombre", "cc.modal_buscar",
        "con.grid", "con.total", "con.seleccion",
        "acc.guardar", "doc.agregar", "alerta",
    ],
}


def _revisar_pagina(page, pagina: str, base: str) -> None:
    page.goto(fx.url(base, pagina), wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    ausentes = []
    for clave in _ESPERADO[pagina]:
        encontrado = False
        for css in selectores.todos(clave):
            try:
                if page.locator(css).count() > 0:
                    encontrado = True
                    break
            except Exception:  # noqa: BLE001 — selector mal formado
                pass
        if not encontrado:
            ausentes.append(f"{clave} ({selectores.todos(clave)[0]})")
    comprobar(not ausentes, f"selectores de «{pagina}» "
                            f"({len(_ESPERADO[pagina])} claves)",
              f"no resuelven: {', '.join(ausentes)}" if ausentes else "")


def _revisar_paneles(page) -> None:
    """Los ids duplicados deben dar exactamente uno dentro de su panel."""
    for clave in ("ben.rfc", "ben.razon_social", "ben.agregar_cuenta"):
        css = selectores.css(clave)
        total = page.locator(css).count()
        comprobar(total > 1,
                  f"«{clave}» está duplicado en el DOM ({total} nodos)",
                  "si dejó de estarlo, revisa si sigue haciendo falta acotar")
        for tipo, tid in (("Proveedor", 1), ("Deudor", 2), ("Acreedor", 3)):
            panel = page.locator(selectores.panel_beneficiario(tid))
            n = panel.locator(css).count()
            comprobar(n == 1, f"«{clave}» acotado al panel {tipo} → 1 nodo",
                      f"encontró {n}")
    # El correo NO existe en el panel de Proveedor: el flujo debe tolerarlo.
    correo = selectores.css("ben.correo")
    n_prov = page.locator(selectores.panel_beneficiario(1)).locator(correo).count()
    comprobar(n_prov == 0,
              "el panel Proveedor no tiene campo de correo (el flujo lo omite)",
              f"encontró {n_prov}")


def _revisar_pestanas(page) -> None:
    """La tira de pestañas debe acotarse: «Insumos» suelto pega en el menú."""
    sueltos = page.locator("li a", has_text="Insumos").count()
    comprobar(sueltos > 1,
              f"«Insumos» sin acotar encuentra {sueltos} enlaces (por eso se "
              f"acota a la tira de pestañas)")
    tira = page.locator(selectores.css("tabs.tira"))
    comprobar(tira.count() > 0,
              f"la tira de pestañas existe ({tira.count()} pestañas)")
    etiquetas = [tira.nth(i).inner_text().strip() for i in range(tira.count())]
    print(f"    pestañas visibles en el fixture: {etiquetas}")


def _revelar(page) -> None:
    """Quita las clases `ng-hide` de la copia estática.

    En el portal real, AngularJS va mostrando los bloques conforme se eligen
    empresa, tipo de pago y tipo de beneficiario. La copia guardada no trae ese
    JavaScript, así que todo lo condicional queda oculto para siempre y ningún
    campo llegaría a ser «visible». Retirar `ng-hide` deja el DOM en el estado
    que tendría con el formulario ya configurado, que es contra el que tiene
    sentido probar el llenado.
    """
    page.evaluate(
        "document.querySelectorAll('.ng-hide')"
        ".forEach(e => e.classList.remove('ng-hide'))")
    page.wait_for_timeout(300)


def _revisar_llenado(page) -> None:
    """El llenado con eventos de Angular sobre campos reales del formulario."""
    from core.rpa_sipp import SesionSipp

    _revelar(page)

    sesion = SesionSipp.__new__(SesionSipp)   # sin abrir navegador propio
    sesion.page = page
    sesion.timeout_ms = 10_000
    sesion._on_bitacora = None

    sesion.llenar("sol.descripcion", "Prueba de llenado", "Descripción")
    valor = page.locator(selectores.css("sol.descripcion")).first.input_value()
    comprobar(valor == "Prueba de llenado", "llenar la descripción",
              f"quedó «{valor}»")

    panel = page.locator(selectores.panel_beneficiario(3))   # Acreedor
    sesion.llenar("ben.rfc", "PELJ800101ABC", "RFC", dentro=panel)
    escrito = panel.locator(selectores.css("ben.rfc")).first.input_value()
    comprobar(escrito == "PELJ800101ABC", "llenar el RFC dentro del panel",
              f"quedó «{escrito}»")
    # Y el de los otros paneles NO debe haberse tocado.
    otro = page.locator(selectores.panel_beneficiario(1)).locator(
        selectores.css("ben.rfc")).first.input_value()
    comprobar(otro == "", "el RFC del panel Proveedor quedó intacto",
              f"quedó «{otro}»")

    sesion.llenar("sol.fecha_pago", "15/08/2026", "Fecha de pago")
    fecha = page.locator(selectores.css("sol.fecha_pago")).first.input_value()
    comprobar(fecha == "15/08/2026", "llenar la fecha de pago",
              f"quedó «{fecha}»")


def main() -> int:
    if not fx.hay_fixtures():
        print(f"XX  No hay páginas de referencia en «{fx.CARPETA}».")
        print("    Son datos reales del portal y no se versionan.")
        return 1

    with fx.servir() as base, sync_playwright() as pw:
        navegador = pw.chromium.launch(headless=True)
        page = navegador.new_page()
        try:
            for pagina in (fx.LOGIN, fx.EMPRESA, fx.AGREGAR):
                _revisar_pagina(page, pagina, base)
            # El resto de comprobaciones vive en la pantalla de captura.
            page.goto(fx.url(base, fx.AGREGAR), wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            _revisar_paneles(page)
            _revisar_pestanas(page)
            _revisar_llenado(page)
        finally:
            navegador.close()

    print()
    if _fallos:
        print(f"Fallos ({len(_fallos)}):")
        for f in _fallos:
            print(f"  - {f}")
        print("\nSi actualizaste los fixtures, esto es lo que cambió en SIPP.")
        return 1
    print(f"El mapa de selectores concuerda con las páginas reales "
          f"({_ok} comprobaciones).")
    print("El resto del motor —desplegables «chosen», guardar, adjuntar y "
          "autorizar— está validado contra el ambiente de PRUEBAS; ver "
          "ESPECIFICACION.md §8.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
