"""Draw the Smart Break icon into assets/ (needs pillow).
Run: python tools/make_icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024   # drawn large, then scaled down for smooth edges
NAVY, NAVY_LIGHT, TRACK = (22, 50, 79), (36, 75, 115), (52, 88, 124)
TEAL, MINT, WHITE = (15, 157, 138), (94, 234, 212), (255, 255, 255)
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # rounded square with a soft top-to-bottom gradient
    gradient = Image.new("RGBA", (S, S))
    g = ImageDraw.Draw(gradient)
    for y in range(S):
        t = y / S
        g.line((0, y, S, y), fill=tuple(round(NAVY_LIGHT[i] * (1 - t) + NAVY[i] * t) for i in range(3)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=255)
    img.paste(gradient, (0, 0), mask)

    # three-quarter progress ring with round caps
    d = ImageDraw.Draw(img)
    pad, width, frac = 170, 90, 0.75
    d.ellipse((pad, pad, S - pad, S - pad), outline=TRACK, width=width)
    d.arc((pad, pad, S - pad, S - pad), start=-90, end=-90 + 360 * frac, fill=TEAL, width=width)
    c, r = S / 2, (S - 2 * pad) / 2 - width / 2
    for angle in (-90, -90 + 360 * frac):
        a = math.radians(angle)
        x, y = c + r * math.cos(a), c + r * math.sin(a)
        d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=TEAL)

    # clock hands
    for x, y in ((c, c - 200), (c + 150, c + 40)):
        d.line((c, c, x, y), fill=WHITE, width=64)
        d.ellipse((x - 32, y - 32, x + 32, y + 32), fill=WHITE)
    d.ellipse((c - 56, c - 56, c + 56, c + 56), fill=MINT)
    return img


def main():
    ASSETS.mkdir(exist_ok=True)
    img = draw()
    img.resize((256, 256), Image.LANCZOS).save(ASSETS / "smart_break.png")
    img.save(ASSETS / "smart_break.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Saved icons to {ASSETS}")


if __name__ == "__main__":
    main()
