"""OCR helpers over video frames: word OCR, text search (fuzzy / multi-word / regex), row expansion,
region reading, text tracking over time, and contact sheets.

All boxes are [x0, y0, x1, y1] in SOURCE pixels. OCR = tesseract (--psm 11, sparse text) on a 2x LANCZOS
upscaled grayscale image, auto-inverted when the background is dark, plus a second inverted pass that
picks up light-on-dark text (e.g. dark title bars). Per-frame results are cached as JSON under
<FrameStore dir>/ocr/.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from . import config
from .errors import ToolError, check_path
from .fonts import draw_text, text_len
from .media import FrameStore

SCALE = 2
PSM = 11
FUZZY_MIN = 0.75
PAD = 1
_OCR_VERSION = 2  # bump to invalidate cached OCR JSON
_CONFUSABLE = str.maketrans({"1": "i", "l": "i", "|": "i", "!": "i", "0": "o"})

Box = list[int]
Region = tuple[int, int, int, int] | list[int] | None


# --------------------------------------------------------------------------- tesseract

def _tess():
    import pytesseract

    exe = config.tool_path("tesseract")
    if not exe:
        raise ToolError("tesseract not found", "install Tesseract OCR 5+ and put it on PATH; run `casefile doctor`")
    pytesseract.pytesseract.tesseract_cmd = exe
    return pytesseract


def _norm_region(region: Region, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if region is None:
        return None
    if len(region) != 4:
        raise ToolError(f"bad region {region}", "region is [x0, y0, x1, y1] in source pixels")
    w, h = size
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ToolError(f"region {list(region)} is empty or outside the {w}x{h} frame",
                        "region is [x0, y0, x1, y1] in source pixels, inside the frame")
    return x0, y0, x1, y1


def _run_pass(tess, gray: Image.Image, ox: int, oy: int) -> list[dict]:
    d = tess.image_to_data(gray, config=f"--psm {PSM}", output_type=tess.Output.DICT)
    out = []
    for i, txt in enumerate(d["text"]):
        txt = (txt or "").strip()
        conf = float(d["conf"][i])
        if not txt or conf < 0:
            continue
        if not any(c.isalnum() for c in txt):
            continue
        x0, y0 = d["left"][i], d["top"][i]
        x1, y1 = x0 + d["width"][i], y0 + d["height"][i]
        # tesseract boxes sit ~1-2 px inside the glyph extent after downscaling; pad by PAD px
        out.append({"text": txt,
                    "box": [int(round(x0 / SCALE)) + ox - PAD, int(round(y0 / SCALE)) + oy - PAD,
                            int(round(x1 / SCALE)) + ox + PAD, int(round(y1 / SCALE)) + oy],
                    "conf": int(round(conf))})
    return out


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _ocr_uncached(img: Image.Image, region) -> list[dict]:
    tess = _tess()
    ox = oy = 0
    if region is not None:
        img = img.crop(region)
        ox, oy = region[0], region[1]
    g = ImageOps.grayscale(img).resize((img.width * SCALE, img.height * SCALE), Image.LANCZOS)
    if np.asarray(g).mean() < 128:  # dark theme: tesseract wants dark text on light
        g = ImageOps.invert(g)
    words = _run_pass(tess, g, ox, oy)
    # second pass on the inverted image catches light text on dark bars; keep only confident, new words
    for w in _run_pass(tess, ImageOps.invert(g), ox, oy):
        if w["conf"] >= 70 and len(w["text"]) > 1 and not any(_overlaps(w["box"], v["box"]) for v in words):
            words.append(w)
    words.sort(key=lambda w: (w["box"][1], w["box"][0]))
    return words


def ocr_image(img: Image.Image, region: Region = None, *, cache: Path | None = None) -> list[dict]:
    """OCR a PIL image -> [{text, box:[x0,y0,x1,y1], conf}] (words, source px).

    region crops first (boxes are still in full-image px). `cache` = optional JSON file path used as cache."""
    reg = _norm_region(region, img.size)
    if cache is not None and cache.exists():
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
    words = _ocr_uncached(img, reg)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(words, separators=(",", ":")))
    return words


def _store(video) -> FrameStore:
    return FrameStore(check_path(video))


def _frame_words(fs: FrameStore, n: int, region: Region) -> tuple[list[dict], tuple | None]:
    reg = _norm_region(region, fs.size)
    tag = "full" if reg is None else "_".join(map(str, reg))
    cache = fs.dir / "ocr" / f"v{_OCR_VERSION}_{n:06d}_{tag}.json"
    return ocr_image(fs.get(n), reg, cache=cache), reg


# --------------------------------------------------------------------------- lines & matching

def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines (same baseline band, small horizontal gaps), reading order."""
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["box"][0])):
        b = w["box"]
        h = b[3] - b[1]
        best = None
        for r in rows:
            lb = r[-1]["box"]
            lh = lb[3] - lb[1]
            ov = min(b[3], lb[3]) - max(b[1], lb[1])
            gap = b[0] - lb[2]
            if ov > 0.5 * min(h, lh) and -2 <= gap <= 1.6 * max(h, lh):
                best = r
                break
        if best is None:
            rows.append([w])
        else:
            best.append(w)
    rows.sort(key=lambda r: (min(w["box"][1] for w in r), r[0]["box"][0]))
    # reading order: bands of vertically-overlapping lines, left to right within a band
    out: list[list[dict]] = []
    bands: list[list[list[dict]]] = []
    for r in rows:
        y0, y1 = min(w["box"][1] for w in r), max(w["box"][3] for w in r)
        if bands:
            by0 = min(w["box"][1] for x in bands[-1] for w in x)
            by1 = max(w["box"][3] for x in bands[-1] for w in x)
            if min(y1, by1) - max(y0, by0) > 0.4 * (y1 - y0):
                bands[-1].append(r)
                continue
        bands.append([r])
    for band in bands:
        out.extend(sorted(band, key=lambda r: r[0]["box"][0]))
    return out


def _union(ws: list[dict]) -> Box:
    return [min(w["box"][0] for w in ws), min(w["box"][1] for w in ws),
            max(w["box"][2] for w in ws), max(w["box"][3] for w in ws)]


def _norm(s: str) -> str:
    s = re.sub(r"[^0-9a-z|!]+", "", s.lower())
    return s.translate(_CONFUSABLE)


def _score(query: str, cand: str, fuzzy: bool) -> float:
    q, c = _norm(query), _norm(cand)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if not fuzzy:
        return 0.0
    return difflib.SequenceMatcher(None, q, c).ratio()


def _match(words: list[dict], query: str, fuzzy: bool) -> list[dict]:
    """All candidate matches [{text, box, conf, score}] sorted best first (overlaps removed)."""
    lines = _lines(words)
    cands = []
    if query.startswith("re:"):
        try:
            rx = re.compile(query[3:], re.I)
        except re.error as e:
            raise ToolError(f"bad regex {query[3:]!r}: {e}", "use Python regex syntax after 're:'") from None
        for ln in lines:
            for k in range(1, min(6, len(ln)) + 1):
                for i in range(len(ln) - k + 1):
                    ws = ln[i:i + k]
                    txt = " ".join(w["text"] for w in ws)
                    if rx.search(txt):
                        cands.append((1.0 - 0.01 * k, ws))
    else:
        nq = len(query.split()) or 1
        for ln in lines:
            for k in {max(1, nq - 1), nq, nq + 1}:
                for i in range(len(ln) - k + 1):
                    ws = ln[i:i + k]
                    sc = _score(query, " ".join(w["text"] for w in ws), fuzzy)
                    if sc >= (FUZZY_MIN if fuzzy else 1.0):
                        cands.append((sc - 0.001 * abs(k - nq), ws))
    cands.sort(key=lambda c: -c[0])
    out: list[dict] = []
    for sc, ws in cands:
        box = _union(ws)
        if any(_overlaps(box, m["box"]) for m in out):
            continue
        out.append({"text": " ".join(w["text"] for w in ws), "box": box,
                    "conf": int(round(min(w["conf"] for w in ws))), "score": round(sc, 2)})
    return out


# --------------------------------------------------------------------------- row expansion

def _row_box(img: Image.Image, box: Box) -> Box:
    """Expand a text box to its form/table row: horizontally to panel edges / background changes,
    vertically to separator lines (max ~1x text height of padding)."""
    a = np.asarray(ImageOps.grayscale(img), dtype=np.int16)
    H, W = a.shape
    x0, y0, x1, y1 = box
    th = max(6, y1 - y0)
    pad = max(3, th // 3)
    ya, yb = max(0, y0 - pad), min(H, y1 + pad + 1)
    ring = np.concatenate([a[max(0, y0 - pad):max(0, y0 - 1), x0:x1].ravel(),
                           a[min(H, y1 + 2):yb, x0:x1].ravel()])
    bg = float(np.median(ring)) if ring.size else float(np.median(a[ya:yb, x0:x1]))
    band = np.abs(a[ya:yb] - bg) > 14
    colfrac = band.mean(axis=0)  # fraction of the band's rows that differ from bg, per column
    # a "wall" = column differing over (almost) the whole band height (panel border / background change)
    wall = colfrac >= 0.9
    lx = x0
    while lx > 0 and not wall[lx - 1]:
        lx -= 1
    rx = x1
    while rx < W - 1 and not wall[rx + 1]:
        rx += 1
    # vertical: stop at separator rows (a line spanning most of the row width) or max padding
    lim = int(round(1.0 * th))
    span = a[:, lx:rx + 1]

    def is_sep(y: int) -> bool:
        return float((np.abs(span[y] - bg) > 10).mean()) > 0.6

    ty = y0
    while ty > 0 and y0 - ty < lim and not is_sep(ty - 1):
        ty -= 1
    by = y1
    while by < H - 1 and by - y1 < lim and not is_sep(by + 1):
        by += 1
    # horizontal extent: a separator line bounding the row (below preferred) defines the row width
    for sy in (by + 1, ty - 1):
        if not (0 <= sy < H) or not is_sep(sy):
            continue
        m = np.abs(a[sy, lx:rx + 1] - bg) > 6
        if not m[max(0, x0 - lx):max(1, x1 - lx)].any():
            continue
        idx = np.flatnonzero(m)
        s0 = s1 = idx[np.argmin(np.abs(idx - (x0 - lx)))]
        # grow the run allowing small gaps (JPEG noise)
        pos = set(idx.tolist())
        while any(s0 - k in pos for k in range(1, 4)):
            s0 -= next(k for k in range(1, 4) if s0 - k in pos)
        while any(s1 + k in pos for k in range(1, 4)):
            s1 += next(k for k in range(1, 4) if s1 + k in pos)
        if s1 - s0 >= 1.5 * (x1 - x0):
            p = max(2, min(10, (s1 - s0) // 50))
            lx, rx = max(lx, lx + s0 - p), min(rx, lx + s1 + p)
            break
    return [int(lx), int(ty), int(rx), int(by)]


# --------------------------------------------------------------------------- public API

def find_text(video: str | Path, t: float, query: str, region: Region = None, fuzzy: bool = True,
              row: bool = False, limit: int = 5) -> dict:
    """Find `query` on the frame at time t -> {"t", "matches":[{text, box, conf, row_box?}]}.

    Multi-word queries match consecutive words on one line; 're:<regex>' matches regex over words/phrases;
    fuzzy tolerates OCR confusions (I/1/l, O/0) and small edits (difflib ratio >= 0.75).
    row=True adds row_box: the containing form/table row."""
    if not query or not query.strip():
        raise ToolError("empty query", "pass the text to find, e.g. 'DL ARFCN' or 're:SIB\\d+'")
    fs = _store(video)
    n = fs.index(t)
    words, _ = _frame_words(fs, n, region)
    ms = _match(words, query.strip(), fuzzy)[:max(1, int(limit))]
    img = fs.get(n) if row else None
    out = []
    for m in ms:
        d = {"text": m["text"][:60], "box": m["box"], "conf": m["conf"]}
        if row:
            d["row_box"] = _row_box(img, m["box"])
        out.append(d)
    return {"t": round(float(t), 2), "matches": out}


def ocr_region(video: str | Path, t: float, region: Region = None, max_chars: int = 3000) -> dict:
    """Read all text in region (whole frame if None) at time t -> {"lines":[{text, box}]} in reading order."""
    fs = _store(video)
    words, _ = _frame_words(fs, fs.index(t), region)
    lines, used, trunc = [], 0, False
    for ln in _lines(words):
        txt = " ".join(w["text"] for w in ln)
        item = {"text": txt, "box": _union(ln)}
        cost = len(txt) + 40
        if used + cost > max_chars:
            trunc = True
            break
        used += cost
        lines.append(item)
    res: dict = {"t": round(float(t), 2), "lines": lines}
    if trunc:
        res["truncated"] = True
    return res


def track_text(video: str | Path, query: str, t0: float, t1: float, step: float = 0.5,
               region: Region = None, fuzzy: bool = True) -> dict:
    """Follow `query` from t0 to t1 sampling every `step` s -> {"spans":[{t0,t1,box_start,box_end,moved}]}.

    Consecutive detections (one missed sample tolerated) form a span; moved=True if the box shifted > 4 px."""
    if step <= 0:
        raise ToolError("step must be > 0", "use e.g. step=0.5")
    if t1 < t0:
        raise ToolError(f"t1 ({t1}) < t0 ({t0})", "pass t0 <= t1")
    fs = _store(video)
    n_s = min(400, int(math.floor((t1 - t0) / step + 1e-6)) + 1)
    hits: list[tuple[float, Box]] = []
    for i in range(n_s):
        t = t0 + i * step
        words, _ = _frame_words(fs, fs.index(t), region)
        ms = _match(words, query.strip(), fuzzy)
        if ms:
            prev = hits[-1][1] if hits else None
            if prev is not None:  # prefer the candidate closest to the previous position
                ms.sort(key=lambda m: (-round(m["score"], 1), abs(m["box"][0] - prev[0]) + abs(m["box"][1] - prev[1])))
            hits.append((round(t, 2), ms[0]["box"]))
    spans: list[dict] = []
    cur: list[tuple[float, Box]] = []

    def close():
        if not cur:
            return
        b0 = cur[0][1]
        moved = any(max(abs(b[k] - b0[k]) for k in range(4)) > 4 for _, b in cur)
        spans.append({"t0": cur[0][0], "t1": cur[-1][0], "box_start": b0, "box_end": cur[-1][1],
                      "moved": moved})

    for t, b in hits:
        if cur and t - cur[-1][0] > 2 * step + 1e-6:
            close()
            cur = []
        cur.append((t, b))
    close()
    res: dict = {"query": query[:60], "spans": spans[:20]}
    if len(spans) > 20:
        res["more_spans"] = len(spans) - 20
    return res


def contact_sheet(video: str | Path, times: list[float] | None = None, t0: float | None = None,
                  t1: float | None = None, n: int = 6, crop: Region = None, max_width: int = 960,
                  out: str | Path | None = None) -> dict:
    """Grid of frames with burned-in timestamps -> {"path", "times"} (one PNG, width <= max_width)."""
    fs = _store(video)
    if times is None:
        a = 0.0 if t0 is None else float(t0)
        b = fs.duration if t1 is None else float(t1)
        n = max(1, int(n))
        b = min(b, max(0.0, fs.duration - 1.0 / fs.fps))
        times = [a] if n == 1 else [a + (b - a) * i / (n - 1) for i in range(n)]
    times = [round(float(t), 2) for t in times][:64]
    if not times:
        raise ToolError("no times for contact sheet", "pass times=[..] or t0/t1/n")
    reg = _norm_region(crop, fs.size)
    cw, ch = (fs.size if reg is None else (reg[2] - reg[0], reg[3] - reg[1]))
    cols = min(len(times), max(1, math.ceil(math.sqrt(len(times)))))
    if len(times) <= 3:
        cols = len(times)
    rows = math.ceil(len(times) / cols)
    gap = 6
    tw = max(40, int((int(max_width) - gap * (cols + 1)) // cols))
    th = max(1, int(round(tw * ch / cw)))
    sheet = Image.new("RGB", (gap + cols * (tw + gap), gap + rows * (th + gap)), (18, 21, 44))
    d = ImageDraw.Draw(sheet)
    fsz = max(10, min(22, tw // 14))
    for i, t in enumerate(times):
        im = fs.at(t)
        if reg is not None:
            im = im.crop(reg)
        im = im.resize((tw, th), Image.LANCZOS)
        x, y = gap + (i % cols) * (tw + gap), gap + (i // cols) * (th + gap)
        sheet.paste(im, (x, y))
        label = f"{t:.2f}s"
        lw = text_len(label, "b", fsz)
        d.rectangle((x, y, x + lw + 10, y + fsz + 8), fill=(0, 0, 0))
        draw_text(d, (x + 5, y + 2), label, "b", fsz, (255, 255, 255))
    if out is None:
        h = hashlib.sha1(json.dumps([times, reg, max_width]).encode()).hexdigest()[:10]
        path = fs.dir / "sheets" / f"sheet_{h}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = check_path(out, must_exist=False, write=True)
    sheet.save(path)
    return {"path": str(path), "times": times, "size": list(sheet.size)}
