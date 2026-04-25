"""Convierte Limiter_icon.png -> Limitador.ico con recorte cuadrado automatico."""
from __future__ import annotations
import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("[ERROR] Pillow no instalado. Ejecuta: pip install pillow")
    sys.exit(1)


SRC = Path(__file__).parent / "Limiter_icon.png"
DST = Path(__file__).parent / "Limitador.ico"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def resize_to_square_with_padding(img: Image.Image, size: int = 256) -> Image.Image:
    """Redimensiona la imagen a un cuadrado agregando padding transparente."""
    # Redimensionar manteniendo la proporcion
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    
    # Crear un lienzo cuadrado con fondo transparente
    square_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    
    # Pegar la imagen redimensionada en el centro
    x_offset = (size - img.width) // 2
    y_offset = (size - img.height) // 2
    square_img.paste(img, (x_offset, y_offset))
    
    return square_img


def trim_transparent(img: Image.Image) -> Image.Image:
    """Recorta bordes 100% transparentes si el PNG tiene alpha."""
    if img.mode != "RGBA":
        return img
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def main() -> int:
    if not SRC.exists():
        print(f"[ERROR] {SRC} no encontrado.")
        return 1

    img = Image.open(SRC).convert("RGBA")
    print(f"[*] Origen: {img.size}")

    # Redimensionar a cuadrado con padding transparente
    img = resize_to_square_with_padding(img, size=256)
    print(f"[*] Tras redimensionar: {img.size}")

    # Guardar con todas las resoluciones
    img.save(DST, format="ICO", sizes=SIZES)
    print(f"[OK] Generado {DST} con {len(SIZES)} resoluciones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
