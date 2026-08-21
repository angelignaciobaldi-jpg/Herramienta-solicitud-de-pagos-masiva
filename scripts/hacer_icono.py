# -*- coding: utf-8 -*-
"""Genera el icono de la herramienta.

Se dibuja a 4x y se reduce con LANCZOS: Pillow no antialiasa los polígonos, y
a 32 px los bordes en escalera se notan mucho en la barra de tareas.
"""
import os
from io import open as io_open

from PIL import Image, ImageDraw

S = 1024          # lienzo de trabajo (4x el tamaño mayor del .ico)
AZUL = (26, 35, 126)         # #1a237e, el primario del tema
AZUL_HONDO = (0, 6, 102)     # #000666
ORO = (230, 180, 65)         # el dorado del logo actual
ORO_HONDO = (193, 140, 36)
BLANCO = (255, 255, 255)
TINTA = (120, 128, 160)


def degradado(tam, arriba, abajo):
    base = Image.new("RGB", (1, tam))
    px = base.load()
    for y in range(tam):
        t = y / max(1, tam - 1)
        px[0, y] = tuple(int(a + (b - a) * t) for a, b in zip(arriba, abajo))
    return base.resize((tam, tam), Image.NEAREST)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# --- Fondo: cuadrado redondeado con degradado (el "azulejo" de la app).
fondo = degradado(S, AZUL, AZUL_HONDO).convert("RGBA")
mascara = Image.new("L", (S, S), 0)
ImageDraw.Draw(mascara).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22),
                                          fill=255)
img.paste(fondo, (0, 0), mascara)

d = ImageDraw.Draw(img)

# --- Hoja de la solicitud, con la esquina doblada.
x0, y0, x1, y1 = int(S * .26), int(S * .18), int(S * .74), int(S * .74)
dobl = int(S * .13)                      # lado del pico doblado
d.polygon([(x0, y0), (x1 - dobl, y0), (x1, y0 + dobl), (x1, y1), (x0, y1)],
          fill=BLANCO)
# El pliegue, en gris para que se lea como papel y no como recorte.
d.polygon([(x1 - dobl, y0), (x1, y0 + dobl), (x1 - dobl, y0 + dobl)],
          fill=(214, 219, 235))

# --- Renglones del documento. El tercero es corto: da sensación de texto.
alto = int(S * .028)
for i, ancho in enumerate((.68, .68, .40)):
    ty = y0 + int(S * .12) + i * int(S * .10)
    d.rounded_rectangle(
        [x0 + int(S * .07), ty, x0 + int(S * .07) + int((x1 - x0) * ancho),
         ty + alto], radius=alto // 2, fill=TINTA)

# --- Moneda: es lo que dice «pago» de un vistazo, incluso a 16 px.
cx, cy, r = int(S * .70), int(S * .70), int(S * .21)
d.ellipse([cx - r - int(S * .022), cy - r - int(S * .022),
           cx + r + int(S * .022), cy + r + int(S * .022)], fill=AZUL_HONDO)
disco = degradado(2 * r, ORO, ORO_HONDO).convert("RGBA")
mask_disco = Image.new("L", (2 * r, 2 * r), 0)
ImageDraw.Draw(mask_disco).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
img.paste(disco, (cx - r, cy - r), mask_disco)

# --- El signo de pesos, dibujado a mano: una fuente del sistema no está
#     garantizada en el equipo que compile, y un icono no puede depender de eso.
# El «$» se dibuja con una fuente del sistema en vez de a mano con arcos:
# este script se corre UNA vez y lo que se versiona es el .ico ya generado,
# así que la fuente no es una dependencia de la compilación. A mano, los dos
# ganchos no llegaban a leerse como una S.
from PIL import ImageFont

fuente = None
for nombre in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf", "arial.ttf"):
    ruta = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", nombre)
    if os.path.exists(ruta):
        fuente = ImageFont.truetype(ruta, int(r * 1.45))
        break
if fuente is None:
    raise SystemExit("no se encontró ninguna fuente del sistema para el «$»")

izq, arr, der, aba = d.textbbox((0, 0), "$", font=fuente)
d.text((cx - (der + izq) / 2, cy - (aba + arr) / 2), "$", font=fuente,
       fill=AZUL_HONDO)

destino = os.path.join(os.getcwd(), "Imagenes")
png = os.path.join(destino, "icon_512.png")
img.resize((512, 512), Image.LANCZOS).save(png)
print("PNG:", png)

# El .ico lleva TODOS los tamaños que pide Windows: si falta el de 16, el
# explorador reescala el de 256 y se ve sucio en la barra de tareas.
tam = [16, 24, 32, 48, 64, 128, 256]
img.resize((256, 256), Image.LANCZOS).save(
    os.path.join(destino, "icon_nuevo.ico"), format="ICO",
    sizes=[(t, t) for t in tam])
print("ICO:", os.path.join(destino, "icon_nuevo.ico"))


# --------------------------------------------------------------------------- #
#  Versión vectorial
# --------------------------------------------------------------------------- #
# El SVG tiene que dar la MISMA imagen que el .ico, así que se arma con las
# mismas proporciones (todas relativas a S) y con el «$» sacado de la misma
# fuente, convertido a trazo. Con un <text> dependería de las fuentes que
# tenga instaladas quien lo abra, y dejaría de coincidir.
def glifo_peso_como_path(ruta_fuente, tam_px, cx_, cy_):
    """El «$» de la fuente como un `path` de SVG, centrado en (cx_, cy_)."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.ttLib import TTFont

    fuente_tt = TTFont(ruta_fuente)
    upem = fuente_tt["head"].unitsPerEm
    glyphset = fuente_tt.getGlyphSet()
    nombre_glifo = fuente_tt.getBestCmap()[ord("$")]
    lapiz = SVGPathPen(glyphset)
    glyphset[nombre_glifo].draw(lapiz)

    escala = tam_px / upem
    caja = fuente_tt["glyf"][nombre_glifo] if "glyf" in fuente_tt else None
    ancho = (caja.xMax + caja.xMin) / 2 if caja and caja.numberOfContours else 0
    alto = (caja.yMax + caja.yMin) / 2 if caja and caja.numberOfContours else 0
    # La Y del glifo crece hacia ARRIBA y la del SVG hacia abajo: de ahí el -1.
    return (lapiz.getCommands(),
            f"translate({cx_ - ancho * escala:.2f} {cy_ + alto * escala:.2f}) "
            f"scale({escala:.5f} {-escala:.5f})")


def _c(rgb):
    return "#%02x%02x%02x" % rgb


d_peso, transform_peso = glifo_peso_como_path(ruta, int(r * 1.45), cx, cy)
svg = f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Icono de la Herramienta Automatizadora de Solicitudes de Pago.
     Generado por scripts/hacer_icono.py, que produce también Imagenes/icon.ico:
     si se edita uno a mano, dejan de coincidir. -->
<!-- Sin width/height: solo viewBox, para que escale a cualquier tamaño
     sin recortarse. -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}">
  <defs>
    <linearGradient id="fondo" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{_c(AZUL)}"/>
      <stop offset="1" stop-color="{_c(AZUL_HONDO)}"/>
    </linearGradient>
    <linearGradient id="moneda" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{_c(ORO)}"/>
      <stop offset="1" stop-color="{_c(ORO_HONDO)}"/>
    </linearGradient>
  </defs>

  <rect x="0" y="0" width="{S}" height="{S}" rx="{int(S * .22)}" fill="url(#fondo)"/>

  <!-- Hoja de la solicitud, con el pico doblado -->
  <path fill="{_c(BLANCO)}"
        d="M {x0} {y0} L {x1 - dobl} {y0} L {x1} {y0 + dobl} L {x1} {y1} L {x0} {y1} Z"/>
  <path fill="#d6dbeb"
        d="M {x1 - dobl} {y0} L {x1} {y0 + dobl} L {x1 - dobl} {y0 + dobl} Z"/>

  <!-- Renglones -->
"""
for i, ancho_rel in enumerate((.68, .68, .40)):
    ty = y0 + int(S * .12) + i * int(S * .10)
    svg += (f'  <rect x="{x0 + int(S * .07)}" y="{ty}" '
            f'width="{int((x1 - x0) * ancho_rel)}" height="{alto}" '
            f'rx="{alto // 2}" fill="{_c(TINTA)}"/>\n')

svg += f"""
  <!-- Moneda, con su reborde para despegarla de la hoja -->
  <circle cx="{cx}" cy="{cy}" r="{r + int(S * .022)}" fill="{_c(AZUL_HONDO)}"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#moneda)"/>
  <path fill="{_c(AZUL_HONDO)}" transform="{transform_peso}" d="{d_peso}"/>
</svg>
"""
svg_ruta = os.path.join(destino, "icon.svg")
with io_open(svg_ruta, "w", encoding="utf-8") as fh:
    fh.write(svg)
print("SVG:", svg_ruta)
