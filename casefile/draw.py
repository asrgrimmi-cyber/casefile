"""Frame drawing primitives for the renderer.

Everything is spec-driven: overlay dicts come from `PShot.overlays` (model_dump(by_alias) with resolved
float 'from'/'to' and 'rect'), coordinates are SOURCE px mapped through the current camera crop, and every
pixel size is authored at 1080p and scaled by `Ctx.s = H / 1080`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import config
from .fonts import draw_text
from .fonts import text_len as _text_len_raw

Color = tuple[int, int, int]
PHASE_COLORS: list[Color] = [(59, 130, 246), (255, 140, 30), (150, 80, 220), (30, 150, 90), (220, 50, 70)]
DARK: Color = (20, 24, 50)
CARD_BG: Color = (248, 249, 252)


# ------------------------------------------------------------------ small helpers
def hex_rgb(h: str | tuple | list | None, default: Color = config.ACCENT) -> Color:
    """'#rrggbb' (or 'rrggbb', or an rgb sequence) -> (r, g, b)."""
    if h is None:
        return default
    if isinstance(h, (tuple, list)):
        return tuple(int(v) for v in h[:3])  # type: ignore[return-value]
    s = str(h).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return default


def ease(u: float) -> float:
    """Smoothstep ease on [0, 1] (clamped)."""
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def fade(t: float, a: float, b: float, d: float = 0.35) -> float:
    """Visibility 0..1 of an item shown from a to b with d-second ease in/out."""
    if t < a or t > b + d:
        return 0.0
    if t > b:
        return min(ease((t - a) / d), 1 - ease((t - b) / d))
    return ease((t - a) / d)


@lru_cache(maxsize=8192)
def text_len(text: str, name: str, size: float) -> float:
    """Cached fonts.text_len (layout loops call it many times per frame with identical arguments)."""
    return _text_len_raw(text, name, size)


class OpRecorder:
    """Stand-in for ImageDraw.Draw that records primitive calls instead of rasterising them.

    Two frames whose recorded ops (and base image) are equal produce identical pixels, so the renderer can
    reuse the previous frame; `replay` rasterises the ops onto a real ImageDraw."""
    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: list = []

    def _rec(name: str):  # noqa: N805 - class-body helper
        def f(self, *a, **k):
            self.ops.append((name, a, tuple(k.items())))
        f.__name__ = name
        return f

    rounded_rectangle = _rec("rounded_rectangle")
    rectangle = _rec("rectangle")
    ellipse = _rec("ellipse")
    line = _rec("line")
    polygon = _rec("polygon")
    text = _rec("text")
    del _rec


def replay(d: ImageDraw.ImageDraw, ops: list) -> None:
    for name, a, k in ops:
        getattr(d, name)(*a, **dict(k))


def rgba(c: Color, a: float) -> tuple[int, int, int, int]:
    return (c[0], c[1], c[2], max(0, min(255, int(a))))


def rrect(d: ImageDraw.ImageDraw, box, r: float, **kw) -> None:
    x0, y0, x1, y1 = (round(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    d.rounded_rectangle([x0, y0, x1, y1], max(0, round(r)), **kw)


@dataclass
class Ctx:
    """Per-frame drawing context: output geometry, brand colours and the active camera crop."""
    W: int
    H: int
    CH: int                                   # content height (H - caption bar)
    accent: Color = config.ACCENT
    ink: Color = config.INK
    cam: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)   # source crop x, y, w, h (aspect-locked)
    logo: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def s(self) -> float:
        return self.H / 1080.0

    def px(self, v: float) -> float:
        """Scale a 1080p pixel size to this output."""
        return v * self.s

    def lw(self, v: float) -> int:
        return max(1, round(v * self.s))

    def xp(self, p) -> tuple[float, float]:
        cx, cy, cw, ch = self.cam
        return ((p[0] - cx) * self.W / cw, (p[1] - cy) * self.CH / ch)

    def xf(self, r) -> tuple[float, float, float, float]:
        x0, y0 = self.xp((r[0], r[1]))
        x1, y1 = self.xp((r[2], r[3]))
        return (x0, y0, x1, y1)


def chip(d: ImageDraw.ImageDraw, ctx: Ctx, x: float, y: float, text: str, alpha: float, size: float = 24,
         bg: Color | None = None, fg: Color = (255, 255, 255)) -> tuple[float, float]:
    """Rounded label chip; size in 1080p px. Returns (w, h) in output px."""
    s = ctx.s
    fs = size * s
    w, h = chip_size(ctx, text, size)
    rrect(d, (x, y, x + w, y + h), 9 * s, fill=rgba(bg or ctx.accent, 235 * alpha))
    draw_text(d, (x + 14 * s, y + 6 * s), text, "m", fs, rgba(fg, 255 * alpha))
    return w, h


def chip_size(ctx: Ctx, text: str, size: float = 24) -> tuple[float, float]:
    s = ctx.s
    return text_len(text, "m", size * s) + 28 * s, size * s + 18 * s


# ------------------------------------------------------------------ source frame + camera
def place_source(src: Image.Image, cam, W: int, CH: int, bg: Color = config.BAR_BG,
                 resample: int = Image.BICUBIC) -> Image.Image:
    """Render the camera crop (x, y, w, h in source px, aspect already locked) into a W x CH image.

    Parts of the crop outside the source are filled with `bg` (letter/pillar-box), never stretched."""
    sw, sh = src.size
    cx, cy, cw, ch = cam
    if cx >= -1e-6 and cy >= -1e-6 and cx + cw <= sw + 1e-6 and cy + ch <= sh + 1e-6:
        return src.resize((W, CH), resample, box=(max(0, cx), max(0, cy), min(sw, cx + cw), min(sh, cy + ch)))
    sx, sy = W / cw, CH / ch
    out = Image.new("RGB", (W, CH), bg)
    ix0, iy0, ix1, iy1 = max(0.0, cx), max(0.0, cy), min(float(sw), cx + cw), min(float(sh), cy + ch)
    if ix1 <= ix0 or iy1 <= iy0:
        return out
    dx0, dy0 = round((ix0 - cx) * sx), round((iy0 - cy) * sy)
    dx1, dy1 = round((ix1 - cx) * sx), round((iy1 - cy) * sy)
    if dx1 <= dx0 or dy1 <= dy0:
        return out
    box = (max(0.0, cx + dx0 / sx), max(0.0, cy + dy0 / sy), min(sw, cx + dx1 / sx), min(sh, cy + dy1 / sy))
    out.paste(src.resize((dx1 - dx0, dy1 - dy0), resample, box=box), (dx0, dy0))
    return out


def lock_aspect(r, aspect: float, src_size: tuple[int, int] | None = None) -> tuple[float, float, float, float]:
    """Expand w or h of (x, y, w, h) around its centre so w/h == aspect; then shift inside the source
    when it fits (so zooms near an edge do not show padding)."""
    x, y, w, h = (float(v) for v in r)
    w, h = max(w, 1.0), max(h, 1.0)
    mx, my = x + w / 2, y + h / 2
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    x, y = mx - w / 2, my - h / 2
    if src_size:
        sw, sh = src_size
        if w <= sw:
            x = min(max(0.0, x), sw - w)
        else:
            x = (sw - w) / 2
        if h <= sh:
            y = min(max(0.0, y), sh - h)
        else:
            y = (sh - h) / 2
    return (x, y, w, h)


# ------------------------------------------------------------------ cards
@lru_cache(maxsize=8)
def _logo(path: str, width: int) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


def logo_path(brand_logo: str | None) -> Path | None:
    """The user's logo image (spec.brand.logo) if it exists; None means "draw the text wordmark"."""
    if brand_logo and Path(brand_logo).exists():
        return Path(brand_logo)
    return None


@lru_cache(maxsize=8)
def _card_base(W: int, CH: int, logo: str) -> Image.Image:
    a = np.empty((CH, W, 3), np.float32)
    a[:] = CARD_BG
    yy, xx = np.mgrid[0:CH, 0:W]
    g = np.clip((xx / W) * 0.6 + (yy / CH) * 0.4, 0, 1)[..., None].astype(np.float32)
    a = a * (1 - g * 0.25) + np.array([228, 230, 252], np.float32) * g * 0.25
    im = Image.fromarray(a.astype(np.uint8))
    if logo:
        s = CH / (1080 - config.BAR_H)
        lg = _logo(logo, max(1, round(440 * s)))
        im.paste(lg, (round(140 * s), round(110 * s)), lg)
    return im


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def card_base(ctx: Ctx) -> Image.Image:
    """Cached static card background (gradient + optional logo). Do not mutate; copy first."""
    return _card_base(ctx.W, ctx.CH, str(ctx.logo) if ctx.logo else "")


def draw_card(ctx: Ctx, shot: Any, t: float) -> Image.Image:
    """Intro / outro card (light background, logo or text wordmark top-left, never cropped)."""
    im = card_base(ctx).copy()
    replay(ImageDraw.Draw(im, "RGBA"), card_ops(ctx, shot, t))
    return im


def card_ops(ctx: Ctx, shot: Any, t: float) -> list:
    """Recorded vector ops of a card at local time t (drawn over card_base)."""
    d = OpRecorder()
    _card_ops(d, ctx, shot, t)
    return d.ops


def _card_ops(d: Any, ctx: Ctx, shot: Any, t: float) -> None:
    CH, s = ctx.CH, ctx.s
    A, N = ctx.accent, ctx.ink
    muted = (70, 76, 100)
    kind = getattr(shot, "card", None) or "intro"
    title = _as_list(getattr(shot, "title", None))
    if ctx.logo is None:
        # neutral text wordmark when spec.brand.logo is not set
        d.rounded_rectangle((140 * s, 118 * s, 176 * s, 154 * s), round(8 * s), fill=A)
        draw_text(d, (192 * s, 112 * s), "Casefile", "b", 40 * s, rgba(N, 255))
    if kind == "intro":
        # abstract illustration: a stack of page cards drifting apart slightly
        u = ease(t / 1.2)
        R = 520 * s
        cx, cy = 1540 * s, 520 * s
        d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=rgba(A, 22))
        for k in range(3):
            off = (2 - k) * (34 + 10 * u) * s
            x0, y0 = 1300 * s - off, 250 * s + off
            x1, y1 = x0 + 460 * s, y0 + 560 * s
            d.rounded_rectangle((x0, y0, x1, y1), round(26 * s), fill=(255, 255, 255, 255),
                                outline=rgba(A, 90 + 60 * k), width=ctx.lw(3))
        x0, y0 = 1300 * s, 250 * s
        d.rounded_rectangle((x0 + 44 * s, y0 + 50 * s, x0 + 416 * s, y0 + 250 * s), round(14 * s), fill=rgba(A, 40))
        tx, ty = x0 + 230 * s, y0 + 150 * s
        d.polygon([(tx - 26 * s, ty - 34 * s), (tx - 26 * s, ty + 34 * s), (tx + 34 * s, ty)], fill=A)
        for i, w in enumerate((330, 280, 310, 200)):
            yy = y0 + (300 + i * 52) * s
            d.rounded_rectangle((x0 + 44 * s, yy, x0 + (44 + w * min(1.0, 0.4 + u)) * s, yy + 16 * s),
                                round(8 * s), fill=rgba(N, 60 if i else 110))
        u = ease(t / 0.8)
        d.rectangle((140 * s, 330 * s, 140 * s + int(120 * s * u), 338 * s), fill=A)
        y = 360 * s
        for i, line in enumerate(title or ["Demo"]):
            draw_text(d, (140 * s, y), line, "b", 86 * s, rgba(A if i % 2 else N, 255 * u))
            y += 100 * s
        u2 = ease((t - 0.5) / 0.8)
        y = max(y + 40 * s, 600 * s)
        for line in _as_list(getattr(shot, "subtitle", None)):
            draw_text(d, (144 * s, y), line, "r", 36 * s, rgba(muted, 255 * u2))
            y += 55 * s
        footer = getattr(shot, "footer", None)
        if footer:
            draw_text(d, (144 * s, max(y + 50 * s, 760 * s)), footer, "m", 28 * s, rgba((140, 146, 170), 255 * u2))
    else:
        u0 = ease(t / 0.6)
        head = title[0] if title else "What this test demonstrates"
        draw_text(d, (140 * s, 300 * s), head, "b", 64 * s, rgba(N, 255 * u0))
        d.rectangle((140 * s, 395 * s, 260 * s, 403 * s), fill=A)
        y0 = 450 * s
        for i, item in enumerate(getattr(shot, "bullets", None) or []):
            u = ease((t - 0.4 - i * 0.45) / 0.5)
            if u <= 0:
                continue
            y = y0 + i * 95 * s + (1 - u) * 20 * s
            d.ellipse((140 * s, y + 4 * s, 184 * s, y + 48 * s), fill=rgba(A, 255 * u))
            draw_text(d, (149 * s, y + 5 * s), "✓", "dj", 30 * s, rgba((255, 255, 255), 255 * u))
            draw_text(d, (210 * s, y + 2 * s), item, "m", 36 * s, rgba(N, 255 * u))
        footer = getattr(shot, "footer", None)
        if footer:
            draw_text(d, (144 * s, CH - 110 * s), footer, "m", 28 * s, (140, 146, 170, 255))


# ------------------------------------------------------------------ overlays
def _span(o: dict) -> tuple[float, float]:
    a = o.get("from", 0.0)
    b = o.get("to", 1e9)
    return float(a if isinstance(a, (int, float)) else 0.0), float(b if isinstance(b, (int, float)) else 1e9)


def _box_rect(ctx: Ctx, o: dict) -> tuple[float, float, float, float] | None:
    r = o.get("rect")
    if not r or len(r) != 4:
        return None
    x0, y0, x1, y1 = ctx.xf(r)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def blur_params(ctx: Ctx, overlays: list[dict], t: float) -> list[tuple]:
    """Active blur regions at t: [(x0, y0, x1, y1, radius, alpha)] in output px (clipped to W x CH)."""
    out = []
    for o in overlays:
        if o.get("type") != "blur":
            continue
        a, b = _span(o)
        al = fade(t, a, b, 0.2)
        r = _box_rect(ctx, o)
        if al <= 0 or r is None:
            continue
        x0, y0 = max(0, int(math.floor(r[0]))), max(0, int(math.floor(r[1])))
        x1, y1 = min(ctx.W, int(math.ceil(r[2]))), min(ctx.CH, int(math.ceil(r[3])))
        if x1 <= x0 or y1 <= y0:
            continue
        zoom = ctx.W / ctx.cam[2]
        out.append((x0, y0, x1, y1, max(1.0, float(o.get("radius", 12)) * zoom), al))
    return out


def apply_blurs(img: Image.Image, ctx: Ctx, overlays: list[dict], t: float) -> Image.Image:
    """Gaussian-blur 'blur' overlay rects (radius in source px, scaled by the camera zoom)."""
    return _apply_blurs(img, blur_params(ctx, overlays, t))


def _apply_blurs(img: Image.Image, params: list[tuple]) -> Image.Image:
    for x0, y0, x1, y1, rad, al in params:
        pad = int(rad * 2)
        big = img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))
        big = big.filter(ImageFilter.GaussianBlur(rad))
        part = big.crop((x0 - max(0, x0 - pad), y0 - max(0, y0 - pad),
                         x0 - max(0, x0 - pad) + (x1 - x0), y0 - max(0, y0 - pad) + (y1 - y0)))
        if al < 1:
            part = Image.blend(img.crop((x0, y0, x1, y1)), part, al)
        img.paste(part, (x0, y0))
    return img


def spot_params(ctx: Ctx, overlays: list[dict], t: float) -> tuple | None:
    """(dim factor, [paste boxes]) of the active spotlight at t, or None."""
    spots = []
    for o in overlays:
        if o.get("type") != "box" or not o.get("spotlight", True):
            continue
        a, b = _span(o)
        al = fade(t, a, b)
        r = _box_rect(ctx, o)
        if al > 0 and r is not None:
            spots.append((r, float(o.get("pad", 8)) * ctx.s, al))
    if not spots:
        return None
    k = 1 - 0.42 * max(al for _, _, al in spots)
    boxes = []
    for (x0, y0, x1, y1), pad, _ in spots:
        bx = (max(0, round(x0 - pad)), max(0, round(y0 - pad)), min(ctx.W, round(x1 + pad)),
              min(ctx.CH, round(y1 + pad)))
        if bx[2] > bx[0] and bx[3] > bx[1]:
            boxes.append(bx)
    return k, tuple(boxes)


@lru_cache(maxsize=64)
def _dim_lut(k: float) -> list[int]:
    return [int(v * k) for v in range(256)] * 3


def _apply_spot(img: Image.Image, sp: tuple | None) -> Image.Image:
    if sp is None:
        return img
    k, boxes = sp
    out = img.point(_dim_lut(k))
    for bx in boxes:
        out.paste(img.crop(bx), bx[:2])
    return out


def apply_spotlight(img: Image.Image, ctx: Ctx, overlays: list[dict], t: float) -> Image.Image:
    """Dim everything outside active spotlight boxes to 58% brightness (scaled by box alpha)."""
    spots = []
    for o in overlays:
        if o.get("type") != "box" or not o.get("spotlight", True):
            continue
        a, b = _span(o)
        al = fade(t, a, b)
        r = _box_rect(ctx, o)
        if al > 0 and r is not None:
            spots.append((r, float(o.get("pad", 8)) * ctx.s, al))
    if not spots:
        return img
    amax = max(al for _, _, al in spots)
    k = 1 - 0.42 * amax
    out = img.point([int(v * k) for v in range(256)] * 3)
    for (x0, y0, x1, y1), pad, _ in spots:
        bx = (max(0, round(x0 - pad)), max(0, round(y0 - pad)), min(img.width, round(x1 + pad)),
              min(img.height, round(y1 + pad)))
        if bx[2] > bx[0] and bx[3] > bx[1]:
            out.paste(img.crop(bx), bx[:2])
    return out


def _label_pos(ctx: Ctx, bx, w: float, h: float, side: str) -> tuple[float, float]:
    s, W, CH = ctx.s, ctx.W, ctx.CH
    if side == "right":
        lx, ly = bx[2] + 14 * s, bx[1]
    elif side == "left" and bx[0] - w - 14 * s > 10 * s:
        lx, ly = bx[0] - w - 14 * s, bx[1]
    elif side == "below" or bx[1] - h - 10 * s < 8 * s:
        lx, ly = bx[0], bx[3] + 10 * s
    else:
        lx, ly = bx[0], bx[1] - h - 10 * s
    return min(max(8 * s, lx), W - w - 8 * s), min(max(8 * s, ly), CH - h - 8 * s)


def _box(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    r = _box_rect(ctx, o)
    if r is None:
        return
    s, A = ctx.s, ctx.accent
    x0, y0, x1, y1 = r
    pad = float(o.get("pad", 8)) * s
    g = 1 + 0.12 * (1 - ease((t - ta) / 0.4))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    bx = (mx - (mx - x0) * g - pad, my - (my - y0) * g - pad, mx + (x1 - mx) * g + pad, my + (y1 - my) * g + pad)
    rrect(d, (bx[0] - 5 * s, bx[1] - 5 * s, bx[2] + 5 * s, bx[3] + 5 * s), 14 * s, outline=rgba(A, 70 * al),
          width=ctx.lw(6))
    rrect(d, (bx[0] - 9 * s, bx[1] - 9 * s, bx[2] + 9 * s, bx[3] + 9 * s), 18 * s, outline=rgba(A, 28 * al),
          width=ctx.lw(4))
    rrect(d, bx, 10 * s, outline=rgba(A, 255 * al), width=ctx.lw(4))
    lab = o.get("label")
    if lab:
        w, h = chip_size(ctx, lab, 26)
        lx, ly = _label_pos(ctx, bx, w, h, o.get("label_side") or "auto")
        chip(d, ctx, lx, ly, lab, al, 26)


def _title(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s = ctx.s
    u = ease((t - ta) / 0.5)
    txt = o.get("text", "")
    fs = 72
    while fs > 30 and text_len(txt, "b", fs * s) + 100 * s > ctx.W - 60 * s:
        fs -= 2
    w, h = text_len(txt, "b", fs * s) + 100 * s, (fs + 78) * s
    x0 = (ctx.W - w) / 2
    y0 = min(ctx.CH / 2 - h / 2 + (1 - u) * 30 * s + 170 * s, ctx.CH - h - 20 * s)
    rrect(d, (x0, y0, x0 + w, y0 + h), 24 * s, fill=rgba(DARK, 225 * al))
    rrect(d, (x0, y0 + h - 8 * s, x0 + w, y0 + h), 4 * s, fill=rgba(ctx.accent, 255 * al))
    draw_text(d, (x0 + 50 * s, y0 + 30 * s), txt, "b", fs * s, rgba((255, 255, 255), 255 * al))


def _badge(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s = ctx.s
    txt = o.get("text", "")
    w, h = chip_size(ctx, txt, 26)
    corner = o.get("corner") or "tr"
    x = ctx.W - 20 * s - w if corner in ("tr", "br") else 20 * s
    y = 20 * s if corner in ("tr", "tl") else ctx.CH - 20 * s - h
    chip(d, ctx, x, y, txt, al, 26, bg=DARK)


def _tiles(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s, A, N = ctx.s, ctx.accent, ctx.ink
    items = o.get("items") or []
    n = len(items)
    if not n:
        return
    gap = 26 * s
    tw = min(420 * s, (ctx.W - 80 * s - (n - 1) * gap) / n)
    th = 170 * s
    X0 = (ctx.W - (n * tw + (n - 1) * gap)) / 2
    Y0 = float(o["y"]) * s if o.get("y") is not None else 560 * s
    Y0 = min(Y0, ctx.CH - th - 20 * s)
    _, b = _span(o)
    for i, it in enumerate(items):
        at = it.get("at")
        ts = float(at) if isinstance(at, (int, float)) else ta
        u = fade(t, ts, b)
        if u <= 0:
            continue
        x = X0 + i * (tw + gap)
        y = Y0 + (1 - ease((t - ts) / 0.4)) * 25 * s
        rrect(d, (x + 4 * s, y + 6 * s, x + tw + 4 * s, y + th + 6 * s), 18 * s, fill=(0, 0, 0, int(40 * u)))
        rrect(d, (x, y, x + tw, y + th), 18 * s, fill=(255, 255, 255, int(250 * u)), outline=rgba(A, 255 * u),
              width=ctx.lw(3))
        vs = 52
        while vs > 24 and text_len(str(it.get("value", "")), "b", vs * s) > tw - 48 * s:
            vs -= 2
        draw_text(d, (x + 24 * s, y + 16 * s), str(it.get("key", "")), "mono", 26 * s, (110, 116, 140, int(255 * u)))
        draw_text(d, (x + 24 * s, y + 50 * s + (52 - vs) * s * 0.5), str(it.get("value", "")), "b", vs * s,
                  rgba(N, 255 * u))
        draw_text(d, (x + 24 * s, y + 120 * s), str(it.get("sub", "")), "r", 24 * s,
                  rgba(tuple(int(v * 0.8) for v in A), 255 * u))


def _phases(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s = ctx.s
    plot = o.get("plot") or [0, 0, 0, 0]
    bands = o.get("bands") or []
    stag = float(o.get("stagger", 0.9))
    for i, band in enumerate(bands):
        if len(band) < 2:
            continue
        a0, a1 = float(band[0]), float(band[1])
        lab = str(band[2]) if len(band) > 2 and band[2] is not None else ""
        c = hex_rgb(band[3]) if len(band) > 3 else PHASE_COLORS[i % len(PHASE_COLORS)]
        u = al * ease((t - ta - i * stag) / 0.5)
        if u <= 0:
            continue
        x0, y0, x1, y1 = ctx.xf((a0, plot[1], a1, plot[3]))
        d.rectangle((x0, y0, x1, y1), fill=rgba(c, 38 * u))
        if i < len(bands) - 1:
            d.line((x1, y0, x1, y1), fill=rgba(c, 160 * u), width=ctx.lw(2))
        if lab:
            fs = 30 * s
            w = text_len(lab, "b", fs)
            vx0, vx1 = max(x0, 8 * s), min(x1, ctx.W - 8 * s)      # centre in the visible part of the band
            lx = min(max((vx0 + vx1) / 2 - w / 2, 8 * s), ctx.W - w - 8 * s)
            draw_text(d, (lx, max(y0, 0) + 10 * s), lab, "b", fs,
                      rgba(tuple(int(v * 0.8) for v in c), 255 * u))


_SIDE_ALIAS = {"r": "right", "l": "left", "t": "above", "b": "below", "r2": "right", "l2": "left", "l3": "left",
               "t2": "above", "top": "above", "bottom": "below"}


def _parse_point(p: Any) -> tuple[tuple[float, float], str, str] | None:
    if isinstance(p, dict):
        xy, lab, side = p.get("xy"), p.get("label", ""), p.get("side", "right")
    elif isinstance(p, (list, tuple)) and p:
        if isinstance(p[0], (list, tuple)):
            xy = p[0]
            lab = p[1] if len(p) > 1 else ""
            side = p[2] if len(p) > 2 else "right"
        else:
            xy, lab, side = p[:2], "", "right"
    else:
        return None
    if not xy or len(xy) < 2:
        return None
    side = _SIDE_ALIAS.get(str(side), str(side))
    return (float(xy[0]), float(xy[1])), str(lab or ""), side


def _dots(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s, W, CH = ctx.s, ctx.W, ctx.CH
    c = hex_rgb(o.get("color"), (59, 110, 230))
    stag = float(o.get("stagger", 0.7))
    for i, p in enumerate(o.get("points") or []):
        pp = _parse_point(p)
        if pp is None:
            continue
        xy, lab, side = pp
        u = al * ease((t - ta - i * stag) / 0.4)
        if u <= 0:
            continue
        x, y = ctx.xp(xy)
        r = (11 + 10 * (1 - ease((t - ta - i * stag) / 0.5))) * s
        d.ellipse((x - r - 6 * s, y - r - 6 * s, x + r + 6 * s, y + r + 6 * s), fill=rgba(c, 60 * u))
        d.ellipse((x - 11 * s, y - 11 * s, x + 11 * s, y + 11 * s), fill=(255, 255, 255, int(255 * u)),
                  outline=rgba(c, 255 * u), width=ctx.lw(5))
        if not lab:
            continue
        w, h = chip_size(ctx, lab, 28)
        off = {"right": (26 * s, -h / 2), "left": (-w - 26 * s, -h / 2), "above": (-w / 2, -h - 26 * s),
               "below": (-w / 2, 26 * s)}.get(side, (26 * s, -h / 2))
        lx = min(max(8 * s, x + off[0]), W - w - 8 * s)
        ly = min(max(8 * s, y + off[1]), CH - h - 8 * s)
        chip(d, ctx, lx, ly, lab, u, 28, bg=c)


def _banner(d, ctx: Ctx, text: str, u: float) -> None:
    s = ctx.s
    fs = 34
    while fs > 20 and text_len(text, "b", fs * s) + 60 * s > ctx.W - 40 * s:
        fs -= 1
    w, h = text_len(text, "b", fs * s) + 60 * s, (fs + 46) * s
    x0 = (ctx.W - w) / 2
    y0 = min(800 * s, ctx.CH - h - 30 * s)
    rrect(d, (x0, y0, x0 + w, y0 + h), 18 * s, fill=rgba(DARK, 235 * u))
    rrect(d, (x0, y0 + h - 7 * s, x0 + w, y0 + h), 3 * s, fill=rgba(ctx.accent, 255 * u))
    draw_text(d, (x0 + 30 * s, y0 + 14 * s), text, "b", fs * s, rgba((255, 255, 255), 255 * u))


def _vline(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s = ctx.s
    c = hex_rgb(o.get("color"), ctx.accent)
    u = al * ease((t - ta) / 0.5)
    if u > 0:
        X, Y0 = ctx.xp((float(o["x"]), float(o["y0"])))
        _, Y1 = ctx.xp((float(o["x"]), float(o["y1"])))
        Y0, Y1 = min(Y0, Y1), max(Y0, Y1)
        Yend = Y0 + (Y1 - Y0) * ease((t - ta) / 0.6)
        yy, dash, step = Y0, 14 * s, 24 * s
        while yy < Yend:
            d.line((X, yy, X, min(Yend, yy + dash)), fill=rgba(c, 255 * u), width=ctx.lw(4))
            yy += step
    lab = o.get("label")
    if lab:
        u2 = al * ease((t - ta - 0.6) / 0.5)
        if u2 > 0:
            _banner(d, ctx, lab, u2)


def _arrow(d, ctx: Ctx, o: dict, t: float, al: float, ta: float) -> None:
    s = ctx.s
    c = hex_rgb(o.get("color"), ctx.accent)
    (x0, y0), (x1, y1) = ctx.xp(o["start"]), ctx.xp(o["end"])
    g = ease((t - ta) / 0.5)
    ex, ey = lerp(x0, x1, g), lerp(y0, y1, g)
    L = math.hypot(ex - x0, ey - y0)
    if L > 1:
        ux, uy = (ex - x0) / L, (ey - y0) / L
        hl, hw = 22 * s, 13 * s
        bxp, byp = ex - ux * hl, ey - uy * hl
        d.line((x0, y0, bxp, byp), fill=rgba(c, 255 * al), width=ctx.lw(5))
        d.polygon([(ex, ey), (bxp - uy * hw, byp + ux * hw), (bxp + uy * hw, byp - ux * hw)], fill=rgba(c, 255 * al))
    r = 7 * s
    d.ellipse((x0 - r, y0 - r, x0 + r, y0 + r), fill=rgba(c, 255 * al))
    lab = o.get("label")
    if lab:
        w, h = chip_size(ctx, lab, 26)
        # label on the side of the start point away from the arrow direction
        dx, dy = x1 - x0, y1 - y0
        lx = x0 - w - 14 * s if dx > 0 else x0 + 14 * s
        ly = y0 - h - 10 * s if dy > 0 else y0 + 10 * s
        lx = min(max(8 * s, lx), ctx.W - w - 8 * s)
        ly = min(max(8 * s, ly), ctx.CH - h - 8 * s)
        chip(d, ctx, lx, ly, lab, al, 26, bg=c)


_DRAW = {"box": _box, "title": _title, "badge": _badge, "tiles": _tiles, "phases": _phases, "dots": _dots,
         "vline": _vline, "arrow": _arrow}


def overlay_plan(ctx: Ctx, overlays: list[dict], t: float) -> tuple | None:
    """Everything draw_overlays would do at t, as plain data: (blur params, spotlight params, vector ops).

    Equal plans on the same base image give identical pixels, which lets the renderer reuse frames."""
    if not overlays:
        return None
    rec = OpRecorder()
    for o in overlays:
        fn = _DRAW.get(o.get("type", ""))
        if fn is None:
            continue
        a, b = _span(o)
        al = fade(t, a, b)
        if al > 0:
            fn(rec, ctx, o, t, al, a)
    blurs, spot = blur_params(ctx, overlays, t), spot_params(ctx, overlays, t)
    if not blurs and spot is None and not rec.ops:
        return None
    return (blurs, spot, rec.ops)


def apply_overlay_plan(img: Image.Image, plan: tuple | None) -> Image.Image:
    """Apply an overlay_plan to img (mutates img in place for blur/vector ops; spotlight returns a new image)."""
    if plan is None:
        return img
    blurs, spot, ops = plan
    img = _apply_blurs(img, blurs)
    img = _apply_spot(img, spot)
    if ops:
        replay(ImageDraw.Draw(img, "RGBA"), ops)   # alpha-blend straight onto the RGB frame (no composite)
    return img


def draw_overlays(img: Image.Image, ctx: Ctx, overlays: list[dict], t: float) -> Image.Image:
    """Blur -> spotlight -> vector overlays (in list order) onto a W x CH content image."""
    return apply_overlay_plan(img, overlay_plan(ctx, overlays, t))


# ------------------------------------------------------------------ caption bar
def progress_h(ctx: Ctx) -> int:
    return max(2, round(6 * ctx.s))


def bar_strip(ctx: Ctx, fill_x: int | None, ticks: tuple[int, ...]) -> Image.Image:
    """W x progress_h progress strip (track, accent fill to column fill_x, chapter ticks) as drawn by draw_bar."""
    ph = progress_h(ctx)
    im = Image.new("RGB", (ctx.W, ph), (45, 50, 85))
    d = ImageDraw.Draw(im)
    if fill_x is not None:
        d.rectangle((0, 0, fill_x, ph), fill=ctx.accent)
        for x in ticks:
            d.rectangle((x - ctx.lw(1), 0, x + ctx.lw(1), ph), fill=config.BAR_BG)
    return im


def bar_top(ctx: Ctx, chapter: str | None, caption: str | None, cap_alpha: float) -> Image.Image:
    """Caption-bar rows CH .. H - progress_h (chapter chip + caption), pixel-identical to draw_bar."""
    canvas = Image.new("RGB", (ctx.W, ctx.H), config.BAR_BG)
    draw_bar(canvas, ctx, T=0.0, total=0.0, chapters=[], chapter=chapter, caption=caption, cap_alpha=cap_alpha)
    return canvas.crop((0, ctx.CH, ctx.W, ctx.H - progress_h(ctx)))


def draw_bar(canvas: Image.Image, ctx: Ctx, *, T: float, total: float, chapters: list, chapter: str | None,
             caption: str | None, cap_alpha: float) -> None:
    """Caption bar under the content: chapter chip, caption text, progress bar with chapter ticks."""
    W, H, CH, s = ctx.W, ctx.H, ctx.CH, ctx.s
    d = ImageDraw.Draw(canvas, "RGBA")
    d.rectangle((0, CH, W, H), fill=config.BAR_BG)
    ph = max(2, round(6 * s))
    d.rectangle((0, H - ph, W, H), fill=(45, 50, 85))
    if total > 0:
        d.rectangle((0, H - ph, int(W * min(1.0, T / total)), H), fill=ctx.accent)
        for ct, _ in list(chapters)[1:]:
            x = int(W * ct / total)
            d.rectangle((x - ctx.lw(1), H - ph, x + ctx.lw(1), H), fill=config.BAR_BG)
    cw = 0.0
    if chapter:
        cw = text_len(chapter, "b", 22 * s) + 30 * s
        rrect(d, (28 * s, CH + 30 * s, 28 * s + cw, CH + 72 * s), 8 * s, fill=ctx.accent)
        draw_text(d, (43 * s, CH + 37 * s), chapter, "b", 22 * s, (255, 255, 255))
    if caption and cap_alpha > 0:
        x = 28 * s + (cw + 30 * s if cw else 0)
        size = 34
        while text_len(caption, "m", size * s) > W - x - 60 * s and size > 22:
            size -= 1
        draw_text(d, (x, CH + 50 * s - size * s * 0.72), caption, "m", size * s,
                  (255, 255, 255, int(255 * cap_alpha)))
