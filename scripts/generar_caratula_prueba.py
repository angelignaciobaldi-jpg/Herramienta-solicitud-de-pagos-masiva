"""Genera carátulas bancarias FICTICIAS para probar el flujo sin datos reales.

Las carátulas verdaderas traen CLABE, nombre y banco de personas reales, así que
no se pueden versionar ni dejar en una carpeta de pruebas. Este script fabrica
PDF de una página, claramente marcados como prueba, con el nombre del
beneficiario en el nombre del archivo —que es de donde la herramienta lo
extrae— y una CLABE de ejemplo.

Uso:
    python scripts/generar_caratula_prueba.py
    python scripts/generar_caratula_prueba.py "JUAN PEREZ LOPEZ" "MARIA RUIZ"
    python scripts/generar_caratula_prueba.py --carpeta "C:\\ruta\\destino"
"""

from __future__ import annotations

import os
import sys

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)

from core import rutas  # noqa: E402

CARPETA_DEFECTO = os.path.join(rutas.DATOS, "caratulas_prueba")
BENEFICIARIO_DEFECTO = "PRUEBA RPA NO GUARDAR"
CLABE_DEFECTO = "012345678901234567"
BANCO_DEFECTO = "BBVA BANCOMER"


def generar(nombre: str, carpeta: str, clabe: str = CLABE_DEFECTO,
            banco: str = BANCO_DEFECTO, prefijo: str = "CARATULA") -> str:
    """Escribe la carátula ficticia y devuelve su ruta.

    `prefijo` permite generar también el Vo.Bo. (`VOBO <nombre>.pdf`), que SIPP
    exige como documento de respaldo antes de dejar solicitar autorización.
    """
    import fitz  # PyMuPDF

    os.makedirs(carpeta, exist_ok=True)
    # El nombre del archivo es el dato que la herramienta lee: se escribe tal
    # cual, sin ruido, para que la extracción sea inequívoca.
    ruta = os.path.join(carpeta, f"{prefijo} {nombre}.pdf")

    doc = fitz.open()
    pagina = doc.new_page()               # Carta por defecto
    ancho = pagina.rect.width

    # Marca de agua: que nadie confunda esto con un documento real.
    pagina.insert_textbox(
        fitz.Rect(0, 300, ancho, 400), "DOCUMENTO DE PRUEBA",
        fontsize=44, color=(0.92, 0.92, 0.92), align=fitz.TEXT_ALIGN_CENTER)

    y = 70
    pagina.insert_text((60, y), banco, fontsize=20,
                       color=(0.10, 0.14, 0.49))
    y += 26
    pagina.insert_text((60, y), "Carátula de estado de cuenta", fontsize=13,
                       color=(0.29, 0.27, 0.32))
    y += 40
    pagina.draw_line(fitz.Point(60, y), fitz.Point(ancho - 60, y),
                     color=(0.78, 0.77, 0.83))

    y += 34
    for etiqueta, valor in (
        ("Titular", nombre),
        ("CLABE interbancaria", clabe),
        ("Cuenta", clabe[-10:]),
        ("Tipo de cuenta", "Cheques / Moneda nacional"),
        ("Sucursal", "0001 CORPORATIVO"),
    ):
        pagina.insert_text((60, y), f"{etiqueta}:", fontsize=11,
                           color=(0.46, 0.46, 0.51))
        pagina.insert_text((220, y), valor, fontsize=13, color=(0.1, 0.11, 0.11))
        y += 30

    y += 20
    pagina.insert_textbox(
        fitz.Rect(60, y, ancho - 60, y + 90),
        "Este documento fue generado automáticamente para probar la "
        "Herramienta Automatizadora de Solicitudes de Pago. No corresponde a "
        "ninguna persona ni cuenta real, y no tiene validez alguna.",
        fontsize=10, color=(0.73, 0.10, 0.10))

    doc.save(ruta)
    doc.close()
    return ruta


def main(argv: list[str]) -> int:
    carpeta = CARPETA_DEFECTO
    if "--carpeta" in argv:
        i = argv.index("--carpeta")
        carpeta = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    nombres = argv or [BENEFICIARIO_DEFECTO]

    print(f"Carpeta: {carpeta}\n")
    for nombre in nombres:
        ruta = generar(nombre, carpeta)
        print(f"OK  {os.path.basename(ruta)}  "
              f"({os.path.getsize(ruta) / 1024:.1f} KB)")
    print(f"\n{len(nombres)} carátula(s) de prueba generada(s).")
    print("Son ficticias: úsalas solo contra el ambiente de PRUEBAS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
