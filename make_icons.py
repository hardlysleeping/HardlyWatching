#!/usr/bin/env python3
"""Render the app's icons from the one mark it already uses.

The mark is the play triangle on near-black that the manifests have always
declared as an inline SVG. Browsers want files: a 192 and a 512 PNG for the
manifest, a maskable 512 for Android's adaptive launcher, a 180 opaque PNG for
iOS (it ignores manifest icons and will not use transparency well), and a real
.ico for the tab.

Run from anywhere: `python3 make_icons.py`. Writes beside this script.
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG = (8, 8, 12, 255)          # #08080c, the manifest's background_color
FG = (255, 140, 90, 255)      # #ff8c5a, the accent
SS = 4                        # supersample, then downscale for clean edges

# Geometry taken from the 192-viewBox SVG both manifests used:
#   rect rx 38, triangle (62,52) (136,96) (62,140)
RADIUS = 38 / 192
TRI = ((62 / 192, 52 / 192), (136 / 192, 96 / 192), (62 / 192, 140 / 192))


def render(size: int, *, rounded: bool, triangle: float = 1.0) -> Image.Image:
    """One square icon. `triangle` shrinks the mark about the centre."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if rounded:
        draw.rounded_rectangle(
            [0, 0, big - 1, big - 1], radius=int(big * RADIUS), fill=BG
        )
    else:
        # Full bleed: a maskable icon must survive being cropped to a circle,
        # so the background has to reach every edge.
        draw.rectangle([0, 0, big - 1, big - 1], fill=BG)

    cx = cy = big / 2
    points = [(cx + (x - 0.5) * big * triangle, cy + (y - 0.5) * big * triangle)
              for x, y in TRI]
    draw.polygon(points, fill=FG)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    icons = {
        "icon-192.png": render(192, rounded=True),
        # Chrome's richer install UI and Android's splash screen ask for 512.
        "icon-512.png": render(512, rounded=True),
        # Adaptive launchers crop to a circle: keep the mark inside the 80%
        # safe zone and let the background run to the edges.
        "icon-512-maskable.png": render(512, rounded=False, triangle=0.8),
        # iOS masks the corners itself and flattens transparency to black, so
        # this one is opaque and square.
        "apple-touch-icon.png": render(180, rounded=False, triangle=0.92),
    }
    for name, img in icons.items():
        img.save(HERE / name, optimize=True)
        print(f"{name}: {img.width}x{img.height}")

    # One .ico holding the sizes a tab strip actually picks from.
    base = render(256, rounded=True)
    base.save(HERE / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("favicon.ico: 16/32/48")


if __name__ == "__main__":
    main()
