#!/usr/bin/env python3
"""Draw label.ico -- the Desktop shortcut's icon. Run once; it is committed."""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZES = [16, 24, 32, 48, 64, 128, 256]
S = 256                     # draw big, let Pillow downscale into the .ico


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # A DK-1201 is a wide die-cut label: white body, rounded corners, and the
    # two ink bars stand in for text.
    body = (18, 66, S - 18, S - 66)
    d.rounded_rectangle(body, radius=22, fill="white", outline="#1f1f1f", width=10)
    d.rectangle((52, 108, S - 52, 130), fill="#1f1f1f")
    d.rectangle((52, 150, S - 92, 168), fill="#1f1f1f")

    # The sprocket holes down each edge say "roll", not "sheet of paper".
    for y in (86, S - 100):
        for x in range(44, S - 40, 36):
            d.ellipse((x, y, x + 10, y + 10), fill="#c9c9c9")
    return img


if __name__ == "__main__":
    out = HERE / "label.ico"
    draw().save(out, sizes=[(s, s) for s in SIZES])
    print("wrote", out)
