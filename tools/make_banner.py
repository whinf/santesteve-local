"""Genera el banner decorativo de la portada (static/img/banner-*.webp). Requiere Pillow con WebP; se ejecuta en local:

py tools/make_banner.py [carpeta de los PNG originales]

Los originales (ilustraciones del autor) no están en el repo: por defecto se leen de la carpeta padre del repo.
Cada imagen se recorta a la franja con dibujo (sin márgenes lisos) en dos versiones con la misma proporción
para día y noche: escritorio (ancho) y móvil (3:1, centrada en el escudo).
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent
OUT = ROOT / "static" / "img"

# nombre: (fichero, (x0, y0, x1, y1) escritorio, x0 del recorte móvil). Los originales ya son franjas; la de noche
# lleva un borde oscuro arriba y abajo, que se quita para dejar las dos a 2172x339 (6,4:1). El móvil, desde el escudo.
SPEC = {
    "day": ("sant_esteve_sesrovires_dia_original.png", (0, 0, 2172, 339), 0),
    "night": ("sant_esteve_sesrovires_noche_original.png", (0, 12, 2172, 351), 0),
}
DESKTOP_W, MOBILE = 1800, (840, 280)


def save(im, name):
    im.save(OUT / name, "WEBP", quality=80, method=6)
    print(name, im.size, (OUT / name).stat().st_size // 1024, "KB")


OUT.mkdir(parents=True, exist_ok=True)
for name, (file, box, mx) in SPEC.items():
    im = Image.open(SRC / file).convert("RGB")
    band = im.crop(box)
    w, h = band.size
    save(band.resize((DESKTOP_W, round(DESKTOP_W * h / w)), Image.LANCZOS), f"banner-{name}.webp")
    mw = h * MOBILE[0] // MOBILE[1]
    save(band.crop((mx, 0, mx + mw, h)).resize(MOBILE, Image.LANCZOS), f"banner-{name}-m.webp")
