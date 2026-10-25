"""Draws the made-up pictures the image test documents use, into
backend/tests/fixtures/images/: a logo with a see-through background, a photo-like JPEG,
a signature (see-through), a small icon and a round stamp. Nothing comes from a real
document.
    python make_images.py     (needs Pillow: the backend's .venv has it)
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "images"
OUT.mkdir(parents=True, exist_ok=True)


def logo():
    img = Image.new("RGBA", (480, 160), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 152, 152), radius=28, fill=(31, 78, 140, 255))
    d.polygon([(40, 112), (80, 40), (120, 112)], fill=(255, 255, 255, 255))
    d.rectangle((176, 52, 464, 76), fill=(31, 78, 140, 255))
    d.rectangle((176, 92, 400, 108), fill=(120, 150, 190, 255))
    img.save(OUT / "logo.png")


def photo():
    w, h = 900, 600
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            sky = y < h * 0.55
            t = y / h
            if sky:
                px[x, y] = (int(120 + 90 * t), int(170 + 60 * t), 235)
            else:
                hill = 0.55 * h + 40 * math.sin(x / 90)
                px[x, y] = (40, int(120 + 50 * math.sin(x / 60 + y / 40)), 60) if y > hill else (150, 200, 230)
    d = ImageDraw.Draw(img)
    d.ellipse((650, 60, 760, 170), fill=(255, 215, 90))
    img = img.filter(ImageFilter.GaussianBlur(1))
    img.save(OUT / "photo.jpg", quality=88)


def signature():
    img = Image.new("RGBA", (600, 180), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = [(20 + t * 5.6, 100 - 50 * math.sin(t / 7) * math.exp(-t / 160) + 18 * math.sin(t / 2.3)) for t in range(100)]
    d.line(pts, fill=(20, 30, 110, 255), width=6, joint="curve")
    d.line([(60, 150), (560, 140)], fill=(20, 30, 110, 255), width=4)
    img.save(OUT / "signature.png")


def icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(46, 160, 90, 255))
    d.line([(18, 34), (28, 44), (46, 22)], fill=(255, 255, 255, 255), width=7)
    img.save(OUT / "icon.png")


def stamp():
    img = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 290, 290), outline=(190, 30, 40, 230), width=12)
    d.ellipse((50, 50, 250, 250), outline=(190, 30, 40, 230), width=6)
    d.rectangle((70, 135, 230, 165), fill=(190, 30, 40, 230))
    img.save(OUT / "stamp.png")


for make in (logo, photo, signature, icon, stamp):
    make()
print("pictures in", OUT)
