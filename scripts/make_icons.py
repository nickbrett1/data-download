"""Regenerate the home-screen icons in `src/data_download/static/`.

The icons are committed, so this only needs running when the mark changes:

    pip install pillow && python3 scripts/make_icons.py

Pillow is deliberately *not* a project dependency - the app never touches it,
it only serves the files this script writes.

The mark is a download arrow over a shelf, in the dashboard's own palette:
`--bg #0f172a` ground, `--accent #7dd3fc` glyph. Squares are full-bleed with no
rounded corners and no transparency because iOS and Android apply their own
masks: a pre-rounded tile shows hairline gaps at the corners.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "data_download" / "static"

BG = (15, 23, 42, 255)  # --bg
GLYPH = (125, 211, 252, 255)  # --accent
SS = 4  # supersample factor


def _arrow(draw: ImageDraw.ImageDraw, s: int) -> None:
    """Download arrow (shaft + head) centred horizontally, in a 1024 box."""
    def box(*v: float) -> tuple[float, float, float, float]:
        return tuple(round(x * s / 1024) for x in v)  # type: ignore[return-value]

    # Shaft, rounded ends.
    draw.rounded_rectangle(box(468, 214, 556, 606), radius=round(44 * s / 1024), fill=GLYPH)
    # Head: a wide triangle whose top edge overlaps the shaft.
    draw.polygon(
        [
            (round(324 * s / 1024), round(560 * s / 1024)),
            (round(700 * s / 1024), round(560 * s / 1024)),
            (round(512 * s / 1024), round(742 * s / 1024)),
        ],
        fill=GLYPH,
    )
    # Shelf the download lands on.
    draw.rounded_rectangle(box(254, 810, 770, 886), radius=round(38 * s / 1024), fill=GLYPH)


def render(size: int) -> Image.Image:
    px = size * SS
    img = Image.new("RGBA", (px, px), BG)
    _arrow(ImageDraw.Draw(img), px)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size, name in (
        (180, "apple-touch-icon.png"),
        (192, "icon-192.png"),
        (512, "icon-512.png"),
        (32, "favicon-32.png"),
        (16, "favicon-16.png"),
    ):
        render(size).save(OUT / name, optimize=True)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
