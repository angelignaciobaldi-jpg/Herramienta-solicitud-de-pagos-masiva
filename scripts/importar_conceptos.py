"""Importa el catálogo de conceptos de pago desde SIPP, sin abrir la aplicación.

Hace lo mismo que el botón «Importar desde SIPP» de la pestaña Conceptos, pero
desde la línea de comandos: útil para sembrar el catálogo la primera vez o para
refrescarlo cuando SIPP agrega conceptos nuevos.

Solo lee: entra al formulario, elige la empresa y copia el grid. No captura ni
guarda nada en el portal.

Uso:
    python scripts/importar_conceptos.py                  # Abastecedora
    python scripts/importar_conceptos.py "Aske" "Corporativo" "Deudor"
    python scripts/importar_conceptos.py --produccion
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import catalogos, conceptos, credenciales, db  # noqa: E402


def main(argv: list[str]) -> int:
    produccion = "--produccion" in argv
    args = [a for a in argv if not a.startswith("--")]
    empresa = args[0] if args else conceptos.EMPRESA_BASE
    sucursal = args[1] if len(args) > 1 else "Corporativo"
    beneficiario = args[2] if len(args) > 2 else "Acreedor"

    datos = credenciales.cargar()
    if not datos or not datos[0]:
        print("XX  No hay credenciales guardadas. Captúralas en Configuración ⚙.")
        return 1
    usuario, contrasena = datos

    ambiente = "PRODUCCION" if produccion else "PRUEBAS"
    db.inicializar()

    print(f"Ambiente : {ambiente}")
    print(f"Empresa  : {empresa} / {sucursal} / {beneficiario}")
    print(f"Catálogo : {len(conceptos.listar())} concepto(s) antes de importar\n")
    print("→ Entrando a SIPP a leer el grid de Conceptos de Pago…")

    resultado = conceptos.leer_de_sipp(
        usuario, contrasena, url_login=catalogos.AMBIENTES[ambiente],
        empresa=empresa, sucursal=sucursal, tipo_beneficiario=beneficiario,
        visible=True)
    if not resultado["ok"]:
        print(f"XX  {resultado['mensaje']}")
        return 1

    print(f"OK  {resultado['mensaje']}\n")
    for i, nombre in enumerate(resultado["conceptos"], 1):
        print(f"   {i:>3}. {nombre}")

    resumen = conceptos.importar(resultado["conceptos"], empresa)
    print(f"\n→ Guardados: {resumen['nuevos']} nuevo(s), "
          f"{resumen['ya_estaban']} que ya estaban.")
    print(f"→ Catálogo: {len(conceptos.listar())} concepto(s) en total.")
    print("\nYa aparecen como desplegable en la plantilla de Excel y en el "
          "formulario de captura.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
