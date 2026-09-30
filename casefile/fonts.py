"""Font loading + mixed-font text drawing (Poppins with DejaVu fallback for glyphs Poppins lacks)."""
from __future__ import annotations

from functools import lru_cache

from fontTools.ttLib import TTFont
from PIL import ImageDraw, ImageFont

from .config import FONTS_DIR

FILES = {
    "b": "Poppins-Bold.ttf", "m": "Poppins-Medium.ttf", "r": "Poppins-Regular.ttf", "l": "Poppins-Light.ttf",
    "mono": "DejaVuSansMono-Bold.ttf", "dj": "DejaVuSans-Bold.ttf", "djr": "DejaVuSans.ttf",
}


@lru_cache(maxsize=256)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Return a cached font. name in FILES keys; size in px."""
    return ImageFont.truetype(str(FONTS_DIR / FILES[name]), int(size))


@lru_cache(maxsize=1)
def _coverage() -> frozenset[int]:
    return frozenset(TTFont(str(FONTS_DIR / FILES["m"])).getBestCmap().keys())


def runs(text: str) -> list[tuple[bool, str]]:
    """Split text into (needs_fallback, substring) runs."""
    cov = _coverage()
    out: list[list] = []
    for ch in text:
        fb = ord(ch) not in cov
        if out and out[-1][0] == fb:
            out[-1][1] += ch
        else:
            out.append([fb, ch])
    return [(a, b) for a, b in out]


def text_len(text: str, name: str, size: float) -> float:
    """Pixel width of text drawn with draw_text."""
    return sum(font("dj" if fb else name, round(size)).getlength(s) for fb, s in runs(text))


def draw_text(d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, name: str, size: float, fill) -> float:
    """Draw text with glyph fallback; returns the x after the last glyph."""
    x, y = xy
    size = round(size)
    for fb, s in runs(text):
        f = font("dj" if fb else name, size)
        d.text((x, y + (size * 0.12 if fb else 0)), s, font=f, fill=fill)
        x += f.getlength(s)
    return x
