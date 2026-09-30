"""Analyse a screen recording ONCE into timeline.json (docs/CONTRACT_V2.md), then answer questions from it.

Pipeline (all change detection on the cached 160x90 grayscale thumbnails from detect.thumbnails):
1. live regions: a 16x9 grid; a cell that changes (1 s lag) in > LIVE_FRAC of the samples of a sliding window
   while most of the grid is static is a live chart / clock -> ``ignored_regions`` and excluded below.
2. change points on the remaining area (5 fps samples); changes < MERGE_GAP s apart are one event; events
   between two settled states that are a pure vertical shift are scrolls (kept inside the step), events with a
   small settled difference (typing, cursor) stay inside the step, the rest split steps.
   stable_t = the sample with the lowest diff to its neighbours (after the last scroll).
3. OCR only at stable_t (+ the frame before each scroll), full resolution, 2x2 overlapping tiles for > 1600 px.
4. kind (terminal / chart / dialog / gui), items, fields, commands, transcript, private[] per step.
5. adjacent steps whose OCR text is nearly identical (selection highlight, hover) are merged.
Public: analyze, summary, query, locate, load.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from . import config, privacy
from .errors import ToolError, check_path
from .media import FrameStore
from .textnorm import normalise

SCHEMA = "1"
ALGO = 6                    # bump to invalidate cached timelines
GX, GY = 16, 9              # live-region grid
PIX_T = 12
SAMPLE_FPS = 5.0
LIVE_WIN = 30.0             # s, sliding window for live cells
LIVE_FRAC = 0.4             # cell active in > 40% of the window's 1-s bins -> live
LIVE_MAX_CELLS = 0.45       # ... only while most of the grid is static
CELL_ACTIVE = 0.02          # changed-pixel fraction for a cell to count as active
CHANGE_FRAC = 0.004         # masked changed fraction between samples -> change
MERGE_GAP = 0.7             # s
STEP_FRAC = 0.012           # settled before/after difference (masked) needed to split a step
MIN_STEP = 1.0              # s, shorter segments are absorbed into neighbours
TEXT_SIM_MERGE = 0.8        # adjacent steps with this OCR line similarity are merged
TILE_OVERLAP = 120
MAX_ITEMS = 400
MAX_SUMMARY = 12000

OcrFn = Callable[..., list]


# ----------------------------------------------------------------------------- helpers

def _home() -> Path:
    p = config.home_dir() / "timelines"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256(path: Path) -> str:
    """sha256 of a file, memoised by (path, size, mtime) in <home>/timelines/_sha.json."""
    st = path.stat()
    key = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    idx_p = _home() / "_sha.json"
    try:
        idx = json.loads(idx_p.read_text())
    except (OSError, ValueError):
        idx = {}
    if key in idx:
        return idx[key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    idx[key] = h.hexdigest()
    try:
        idx_p.write_text(json.dumps(idx))
    except OSError:
        pass
    return idx[key]


def slug(text: str, maxlen: int = 32) -> str:
    """Item-id slug: lowercase [a-z0-9_]."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:maxlen].rstrip("_") or "item")


def _union(boxes: list[list[int]]) -> list[int]:
    return [int(min(b[0] for b in boxes)), int(min(b[1] for b in boxes)),
            int(max(b[2] for b in boxes)), int(max(b[3] for b in boxes))]


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _r2(x: float) -> float:
    return round(float(x), 2)


# ----------------------------------------------------------------------------- OCR backend

def _resolve_ocr(ocr) -> tuple[OcrFn | None, str]:
    """-> (fn(img, region=None) -> words, backend name). ocr: 'auto'|'tesseract'|'rapidocr'|'none'|callable."""
    if callable(ocr):
        return ocr, getattr(ocr, "__name__", "custom")
    if ocr == "none":
        return None, "none"
    try:
        from . import ocr_backend  # provided by the GPU worker package when present
        name = ocr_backend.backend_name(ocr or "auto")

        def fn(img, region=None):
            return ocr_backend.ocr(img, region, backend=name)
        return fn, name
    except ImportError:
        from .vision import ocr_image
        return (lambda img, region=None: ocr_image(img, region)), "tesseract"


def _tiles(w: int, h: int) -> list[tuple[int, int, int, int]]:
    if w <= 1600:
        return [(0, 0, w, h)]
    hw, hh, o = w // 2, h // 2, TILE_OVERLAP
    return [(0, 0, hw + o, hh + o), (hw - o, 0, w, hh + o), (0, hh - o, hw + o, h), (hw - o, hh - o, w, h)]


def _dark_regions(img: Image.Image, min_area: float = 0.06) -> list[tuple[int, int, int, int]]:
    """Large dark panels (terminal windows) on a light / mixed screen -> boxes in source px.

    Tesseract binarises per image, so a dark terminal inside a light GUI reads badly; these panels are
    OCR'd again as separate crops."""
    gw, gh = 128, 72
    g = np.asarray(ImageOps.grayscale(img).resize((gw, gh), Image.Resampling.BOX), dtype=np.int16)
    dark = g < 70
    if dark.mean() > 0.85 or dark.mean() < min_area:
        return []
    seen = np.zeros_like(dark)
    out = []
    sx, sy = img.width / gw, img.height / gh
    for y in range(gh):
        for x in range(gw):
            if not dark[y, x] or seen[y, x]:
                continue
            stack, ys, xs = [(y, x)], [], []
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                ys.append(cy)
                xs.append(cx)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < gh and 0 <= nx < gw and dark[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
            area = (x1 - x0) * (y1 - y0)
            if area < min_area * gw * gh or len(ys) < 0.5 * area:
                continue
            out.append((int(x0 * sx), int(y0 * sy), int(min(img.width, x1 * sx)), int(min(img.height, y1 * sy))))
    return out


def _center_in(b, r) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return r[0] <= cx <= r[2] and r[1] <= cy <= r[3]


def _ocr_frame(fs: FrameStore, n: int, fn: OcrFn, tag: str) -> list[dict]:
    """Full-res OCR of frame n (tiled for big frames), de-duplicated across tile overlaps; cached."""
    cache = fs.dir / "ocr" / f"tl{ALGO}_{slug(tag)}_{n:06d}.json"
    if tag != "custom" and cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    img = fs.get(n)
    full = (0, 0, img.width, img.height)
    tiles = _tiles(*img.size)
    darks = _dark_regions(img)
    dark_tiles = [[(r[0] + t[0], r[1] + t[1], r[0] + t[2], r[1] + t[3]) for t in _tiles(r[2] - r[0], r[3] - r[1])]
                  for r in darks]

    def run(r):
        crop = img.crop(r) if tuple(r) != full else img
        out = []
        for w in fn(crop) or []:
            b = w["box"]
            out.append({"text": str(w["text"]), "conf": int(round(float(w.get("conf", 90)))),
                        "box": [int(b[0]) + r[0], int(b[1]) + r[1], int(b[2]) + r[0], int(b[3]) + r[1]]})
        return out

    jobs = tiles + [t for dt in dark_tiles for t in dt]
    if len(jobs) > 1:
        with ThreadPoolExecutor(min(8, len(jobs))) as ex:
            res = list(ex.map(run, jobs))
    else:
        res = [run(jobs[0])]

    def dedupe(parts):
        ws: list[dict] = []
        for part in parts:
            for w in part:
                if not any(_iou(w["box"], v["box"]) > 0.4 for v in ws):
                    ws.append(w)
        return ws

    words = dedupe(res[:len(tiles)])
    k = len(tiles)
    for r, dt in zip(darks, dark_tiles):
        # dark panels (terminals) re-read on their own: tesseract binarises per image, so a dark terminal inside
        # a light GUI reads badly. Dark-pass words win; whole-frame words survive where the dark pass saw nothing
        # (e.g. a highlighted / selected line).
        sub = dedupe(res[k:k + len(dt)])
        k += len(dt)
        if not sub:
            continue
        from .vision import _lines
        inside = [w for w in words if _center_in(w["box"], r)]
        keep_full: list[dict] = []
        drop_sub: set[int] = set()
        for ln in _lines(inside):          # a prompt line read better by the full pass (e.g. selected text)
            txt = " ".join(w["text"] for w in ln)
            if not (_USERHOST_PROMPT.search(txt) or _PROMPT.match(txt)):
                continue
            lb = _union([w["box"] for w in ln])
            over = [i for i, v in enumerate(sub) if _iou(lb, v["box"]) > 0 and
                    abs((v["box"][1] + v["box"][3]) / 2 - (lb[1] + lb[3]) / 2) < 0.6 * (lb[3] - lb[1])]
            stxt = " ".join(sub[i]["text"] for i in sorted(over, key=lambda i: sub[i]["box"][0]))
            if not (_USERHOST_PROMPT.search(stxt) or _PROMPT.match(stxt)):
                keep_full += ln
                drop_sub.update(over)
        sub = [v for i, v in enumerate(sub) if i not in drop_sub]
        words = [w for w in words if not _center_in(w["box"], r) or w in keep_full or
                 not any(_iou(w["box"], v["box"]) > 0 for v in sub)] + sub
    words.sort(key=lambda w: (w["box"][1], w["box"][0]))
    if tag != "custom":
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(words, separators=(",", ":")))
    return words


# ----------------------------------------------------------------------------- motion analysis

def _cells(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(k, GY, GX) changed fraction per grid cell between stacks a and b."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIX_T
    k, h, w = d.shape
    ch, cw = h // GY, w // GX
    d = d[:, :ch * GY, :cw * GX]
    return d.reshape(k, GY, ch, GX, cw).mean(axis=(2, 4))


def _live_mask(th: np.ndarray, sfps: float) -> np.ndarray:
    """(m, GY, GX) bool: cell is live (chart / clock) at sample i.

    Activity is binned per second (a cell is active in a bin if any consecutive-sample change touches it);
    a cell is live in a LIVE_WIN window when it is active in > LIVE_FRAC of the bins AND in every third of the
    window (steady updates, not a one-off page change) while most of the grid is static."""
    m = th.shape[0]
    live = np.zeros((m, GY, GX), bool)
    B = max(1, int(round(sfps)))
    nb = (m - 1) // B
    if nb < 3:
        return live
    act = _cells(th[1:], th[:-1]) > CELL_ACTIVE
    bins = act[:nb * B].reshape(nb, B, GY, GX).any(1)
    W = min(nb, max(3, int(round(LIVE_WIN))))
    lb = np.zeros((nb, GY, GX), bool)
    for w0 in range(0, max(1, nb - W + 1), max(1, W // 4)):
        seg = bins[w0:w0 + W]
        ok = seg.mean(0) > LIVE_FRAC
        for part in np.array_split(seg, 3):
            ok &= part.mean(0) >= 0.7 * LIVE_FRAC
        if ok.mean() > LIVE_MAX_CELLS:       # most of the screen moving: video / scrolling, not a live widget
            continue
        lb[w0:w0 + W] |= ok
    if nb - W > 0 and (nb - W) % max(1, W // 4):   # tail window
        seg = bins[nb - W:]
        ok = seg.mean(0) > LIVE_FRAC
        for part in np.array_split(seg, 3):
            ok &= part.mean(0) >= 0.7 * LIVE_FRAC
        if ok.mean() <= LIVE_MAX_CELLS:
            lb[nb - W:] |= ok
    d = lb.copy()                           # 1-cell dilation (anti-aliased chart edges bleed into neighbours)
    d[:, 1:] |= lb[:, :-1]
    d[:, :-1] |= lb[:, 1:]
    d[:, :, 1:] |= lb[:, :, :-1]
    d[:, :, :-1] |= lb[:, :, 1:]
    for i in range(m):
        live[i] = d[min(nb - 1, max(0, (i - 1) // B))]
    return live


def _pix_mask(cellmask: np.ndarray, h: int, w: int) -> np.ndarray:
    """Upscale a (GY, GX) bool mask to (h, w)."""
    ys = np.minimum(np.arange(h) * GY // h, GY - 1)
    xs = np.minimum(np.arange(w) * GX // w, GX - 1)
    return cellmask[ys][:, xs]


def _masked_frac(a: np.ndarray, b: np.ndarray, pm: np.ndarray) -> float:
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIX_T
    keep = ~pm
    return float(d[keep].mean()) if keep.any() else 0.0


def _regions(cells: np.ndarray, sw: int, sh: int) -> list[list[int]]:
    """Connected components of a (GY, GX) mask -> boxes in source px."""
    seen = np.zeros_like(cells, bool)
    out = []
    for y in range(GY):
        for x in range(GX):
            if not cells[y, x] or seen[y, x]:
                continue
            stack, comp = [(y, x)], []
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                comp.append((cy, cx))
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < GY and 0 <= nx < GX and cells[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            ys, xs = [c[0] for c in comp], [c[1] for c in comp]
            out.append([int(min(xs) * sw / GX), int(min(ys) * sh / GY),
                        int((max(xs) + 1) * sw / GX), int((max(ys) + 1) * sh / GY)])
    return out


def _gray_small(fs: FrameStore, n: int, width: int = 640) -> np.ndarray:
    im = fs.get(n)
    s = width / im.width
    g = ImageOps.grayscale(im).resize((width, max(1, int(round(im.height * s)))), Image.Resampling.BOX)
    return np.asarray(g, dtype=np.int16)


def _scroll_dy(a: np.ndarray, b: np.ndarray, ign: np.ndarray) -> int | None:
    """Vertical content shift (px in `a` scale) turning a into b, or None if the change is not a scroll."""
    a = a.copy()
    b = b.copy()
    b[ign] = a[ign]
    diff = np.abs(a - b) > 20
    if diff.mean() < 0.002:
        return None
    cols = np.flatnonzero(diff.any(0))
    rows = np.flatnonzero(diff.any(1))
    x0, x1 = cols.min(), cols.max() + 1
    y0, y1 = rows.min(), rows.max() + 1
    A, B = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    h = A.shape[0]
    if h < 20 or (x1 - x0) < 40:
        return None

    def ink(M):
        return (M.max(1) - M.min(1)) > 40

    ia, ib = ink(A), ink(B)

    def match(dy: int) -> float:
        # b[y] == a[y - dy]
        if dy >= 0:
            Bs, As, ib_, ia_ = B[dy:], A[:h - dy], ib[dy:], ia[:h - dy]
        else:
            Bs, As, ib_, ia_ = B[:h + dy], A[-dy:], ib[:h + dy], ia[-dy:]
        if len(Bs) < 0.3 * h:
            return 0.0
        rows_ink = ib_ | ia_
        if rows_ink.sum() < 5:
            return 0.0
        ok = np.abs(Bs - As).mean(1) < 6
        return float((ok & rows_ink).sum() / rows_ink.sum()) * min(1.0, len(Bs) / (0.5 * h))

    base = match(0)
    best, bdy = 0.0, 0
    for dy in range(-int(h * 0.7), int(h * 0.7) + 1):
        if abs(dy) < 3:
            continue
        s = match(dy)
        if s > best:
            best, bdy = s, dy
    if best >= 0.5 and best > 1.8 * base + 0.1:
        return bdy
    return None


# ----------------------------------------------------------------------------- text structure

_PROMPT = re.compile(r"^(?:\(?[\w.-]+\)?\s+)?(?:[\w.-]+@[\w.-]+(?::[^\s$#]*)?\s*)?[$#>%]\s+(\S.*)$")
_USERHOST_PROMPT = re.compile(r"(?:\[?[\w.-]+@[\w.-]+(?::\S*)?(?:\s+[~/][^\]$#]*)?\]?\s*|\b[a-z][\w.-]*-\d[\d.]*|\broot)"
                              r"[$#]\s*(\S.*)?$")
_JSON_KV = re.compile(r'^[{\[,\s]*"([^"]{1,48})"\s*:\s*(.+?)\s*,?\s*$')
_JSON_KV_LOOSE = re.compile(r'^[{\[,\s]*"?([A-Za-z_][\w.-]{1,47})"\s*:?\s*(-?[\w."][\w.:" -]{0,60}?)\s*,?\s*$')
_COLON_KV = re.compile(r"^([A-Za-z][\w .#/()-]{0,38}?)\s*[:=]\s+(\S.{0,60})$")
_KNOWN_CMDS = re.compile(r"^(?:\./\S+|sudo|ssh|cd|ls|cat|curl|python3?|ping|ip|ifconfig|systemctl|docker|kubectl|"
                         r"git|tail|grep|ue_get|ws\.js)\b")


def _line_text(ln: list[dict]) -> str:
    return " ".join(w["text"] for w in ln)


def _is_value(s: str) -> bool:
    s = s.strip()
    return bool(s) and len(s) <= 24 and (any(c.isdigit() for c in s) or len(s.split()) == 1)


def _is_label(s: str) -> bool:
    s = s.strip()
    return bool(s) and len(s) <= 32 and len(s.split()) <= 4 and s[0].isalpha() and not s.endswith(".")


_LONE_DIGIT = {"I": "1", "l": "1", "|": "1", "O": "0", "o": "0"}


def _clean_value(v: str) -> str:
    v = v.strip().rstrip(",").strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1]
    v = v.strip()
    return _LONE_DIGIT.get(v, v)


def _classify(img: Image.Image, lines: list[dict], live_area: float, prev_img: Image.Image | None) -> str:
    texts = [ln["text"] for ln in lines]
    prompts = sum(1 for t in texts if _USERHOST_PROMPT.search(t) or re.match(r"^\s*[$#]\s+\S", t))
    jsonish = sum(1 for t in texts if _JSON_KV.match(t) or t.strip() in ("{", "}", "[", "]", "},", "],"))
    g = np.asarray(ImageOps.grayscale(img.resize((320, max(1, img.height * 320 // img.width)))), dtype=np.float32)
    dark_frame = float(np.median(g)) < 70
    if lines and (prompts or jsonish >= 3):
        bb = _union([ln["box"] for ln in lines if _USERHOST_PROMPT.search(ln["text"]) or _JSON_KV.match(ln["text"])]
                    or [ln["box"] for ln in lines])
        s = 320 / img.width
        region = g[int(bb[1] * s):max(int(bb[1] * s) + 1, int(bb[3] * s)), int(bb[0] * s):max(int(bb[0] * s) + 1, int(bb[2] * s))]
        if dark_frame or (region.size and float(np.median(region)) < 90) or jsonish >= 3:
            return "terminal"
    if dark_frame and len(lines) >= 5:
        return "terminal"
    if prev_img is not None and prev_img.size == img.size:
        p = np.asarray(ImageOps.grayscale(prev_img.resize(g.shape[::-1])), dtype=np.float32)
        d = np.abs(p - g) > 20
        if 0.03 < d.mean() < 0.45:
            ys, xs = np.nonzero(d)
            bh, bw = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
            fill = d.sum() / (bh * bw)
            if fill > 0.55 and bh * bw < 0.5 * d.size:
                return "dialog"
    if live_area > 0.3:
        return "chart"
    return "gui"


def _structure(words: list[dict], size: tuple[int, int], context: str) -> list[dict]:
    """OCR words -> reading-order lines [{text, box, conf, words}] with normalised text."""
    from .vision import _lines
    out = []
    for ln in _lines(words):
        txt = normalise(_line_text(ln), context)
        if not txt or not any(c.isalnum() for c in txt):
            continue
        out.append({"text": txt, "box": _union([w["box"] for w in ln]),
                    "conf": int(round(sum(w["conf"] for w in ln) / len(ln))), "words": ln})
    return out


def _items_for(sid: str, lines: list[dict], t: float, kind: str, size: tuple[int, int],
               used: set[str]) -> tuple[list[dict], dict, list[dict]]:
    """-> (items, fields, commands) for one analysed frame."""
    W, H = size
    items: list[dict] = []
    fields: dict[str, str] = {}
    commands: list[dict] = []
    hts = sorted(ln["box"][3] - ln["box"][1] for ln in lines) or [10]
    med_h = hts[len(hts) // 2]

    def new_id(base: str) -> str:
        b = slug(base)
        i, cand = 2, b
        while cand in used:
            cand = f"{b}_{i}"
            i += 1
        used.add(cand)
        return f"{sid}.{cand}"

    def add(text, box, conf, role, key=None, value=None):
        it = {"id": new_id(key or text), "text": text[:200], "box": [int(v) for v in box], "t": _r2(t),
              "conf": int(conf), "role": role}
        if value is not None:
            it["value"] = value
        items.append(it)
        return it

    # pair label-left / value-right lines on the same row (GUI forms)
    paired: set[int] = set()
    if kind != "terminal":
        for i, a in enumerate(lines):
            if i in paired or not _is_label(a["text"]) or ":" in a["text"]:
                continue
            ay = (a["box"][1] + a["box"][3]) / 2
            best = None
            for j, b in enumerate(lines):
                if j == i or j in paired or b["box"][0] <= a["box"][2]:
                    continue
                by = (b["box"][1] + b["box"][3]) / 2
                if abs(by - ay) > 0.6 * max(med_h, a["box"][3] - a["box"][1]):
                    continue
                gap = b["box"][0] - a["box"][2]
                if gap > 0.35 * W or not _is_value(b["text"]):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, j)
            if best is not None:
                j = best[1]
                b = lines[j]
                paired.update((i, j))
                key, val = a["text"].strip(), _clean_value(b["text"])
                fields.setdefault(key, val)
                add(f"{key}: {val}", _union([a["box"], b["box"]]), min(a["conf"], b["conf"]), "field", key, val)

    for i, ln in enumerate(lines):
        if i in paired:
            continue
        txt = ln["text"]
        m_prompt = _PROMPT.match(txt) if (kind == "terminal" or _USERHOST_PROMPT.search(txt)) else None
        m_uh = _USERHOST_PROMPT.search(txt)
        if m_uh or m_prompt:
            cmd = (m_uh.group(1) if m_uh else None) or (m_prompt.group(1) if m_prompt else None)
            if cmd and len(cmd.strip()) > 1:
                cmd = cmd.strip()
                if not any(c["text"] == cmd for c in commands):
                    commands.append({"t": _r2(t), "text": cmd[:300]})
                add(txt, ln["box"], ln["conf"], "command", "cmd_" + cmd.split()[0])
                continue
        if kind == "terminal" and _KNOWN_CMDS.match(txt) and not _JSON_KV.match(txt):
            if not any(c["text"] == txt for c in commands):
                commands.append({"t": _r2(t), "text": txt[:300]})
        m = _JSON_KV.match(txt) or (_JSON_KV_LOOSE.match(txt) if kind == "terminal" else None)
        if m:
            key, val = re.sub(r"[\s_]+", "_", m.group(1).strip()), _clean_value(m.group(2))
            if val and val not in ("{", "["):
                fields.setdefault(key, val)
                add(f'"{key}": {val}', ln["box"], ln["conf"], "field", key, val)
                continue
        m = _COLON_KV.match(txt)
        if m and not re.match(r"^[a-z]+://", txt):
            key, val = m.group(1).strip(), _clean_value(m.group(2))
            if val:
                fields.setdefault(key, val)
                add(txt, ln["box"], ln["conf"], "field", key, val)
                continue
        h = ln["box"][3] - ln["box"][1]
        role = "text"
        if kind != "terminal" and (h > 1.35 * med_h or (ln["box"][1] < 0.1 * H and len(txt) > 3)):
            role = "heading"
        elif kind != "terminal" and len(txt.split()) <= 2 and len(txt) <= 16:
            row = [o for o in lines if abs((o["box"][1] + o["box"][3]) / 2 - (ln["box"][1] + ln["box"][3]) / 2) < 0.5 * h
                   and len(o["text"]) <= 16]
            role = "tab" if len(row) >= 3 and ln["box"][1] < 0.35 * H else "label"
        add(txt, ln["box"], ln["conf"], role)
    return items, fields, commands


_CLOCK = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d")


def _title(kind: str, items: list[dict], commands: list[dict]) -> str:
    if kind == "terminal":
        if commands:
            return f"Terminal · {commands[-1]['text'][:48]}"
        return "Terminal"
    def good(it) -> bool:
        s = it["text"]
        letters = sum(c.isalpha() for c in s)
        return (len(s) >= 4 and it.get("conf", 0) >= 60 and letters >= 0.6 * len(s.replace(" ", ""))
                and not _CLOCK.search(s))
    heads = [it["text"] for it in items if it["role"] == "heading" and good(it)]
    if not heads:
        heads = [it["text"] for it in items if it["role"] in ("tab", "label", "text") and good(it)][:2]
    t = " · ".join(h[:30] for h in heads[:3])
    pre = {"chart": "Chart", "dialog": "Dialog"}.get(kind)
    t = f"{pre} · {t}" if pre and t else (t or pre or "Screen")
    return t[:80]


# ----------------------------------------------------------------------------- speech

def _speech(video: Path, mode: str, audio_db: float | None) -> tuple[list[dict], str]:
    if mode == "none":
        return [], "off"
    if audio_db is None or audio_db < -45:
        return [], "silent"
    try:
        from . import stt  # type: ignore[attr-defined]
    except ImportError:
        return [], "unavailable"
    try:
        segs = stt.transcribe(str(video)) or []
    except Exception as e:  # noqa: BLE001 - optional model; never fail the analysis
        return [], f"error: {e}"[:80]
    out = []
    for s in segs:
        out.append({"t0": _r2(s.get("t0", s.get("start", 0))), "t1": _r2(s.get("t1", s.get("end", 0))),
                    "text": str(s.get("text", "")).strip()[:500]})
    return out, "stt"


# ----------------------------------------------------------------------------- analysis

def _segment(fs: FrameStore, bad: set[int]) -> dict:
    from .detect import thumbnails
    th_all = thumbnails(fs)
    fps = fs.fps
    S = max(1, int(round(fps / SAMPLE_FPS)))
    idx = [i for i in range(0, fs.n_frames, S)]
    idx = [i if i not in bad else next((j for j in range(i, min(fs.n_frames, i + S)) if j not in bad), i) for i in idx]
    th = np.asarray(th_all[idx])
    m = len(idx)
    sfps = fps / S
    live = _live_mask(th, sfps)
    h, w = th.shape[1:]
    pms = {}

    def pm(i):
        key = live[i].tobytes()
        if key not in pms:
            pms[key] = _pix_mask(live[i], h, w)
        return pms[key]

    d1 = np.zeros(m)
    for i in range(1, m):
        d1[i] = _masked_frac(th[i - 1], th[i], pm(i) | pm(i - 1))
    changed = d1 > CHANGE_FRAC
    # events: clusters of changed samples closer than MERGE_GAP
    events: list[list[int]] = []
    gap = max(1, int(round(MERGE_GAP * sfps)))
    for i in np.flatnonzero(changed):
        if events and i - events[-1][1] <= gap:
            events[-1][1] = int(i)
        else:
            events.append([int(i), int(i)])
    return {"idx": idx, "th": th, "live": live, "d1": d1, "events": events, "sfps": sfps, "pm": pm}


def _analyse(path: Path, sha: str, out: Path, focus, speech: str, ocr) -> dict:
    t_start = time.time()
    fs = FrameStore(path)
    from .detect import bad_frame_list, probe
    try:
        bad = set(bad_frame_list(path))
        audio_db = probe(path).get("audio_db")
    except ToolError:
        bad, audio_db = set(), None
    fn, ocr_name = _resolve_ocr(ocr)
    seg = _segment(fs, bad)
    idx, th, live, d1, events, sfps, pm = (seg[k] for k in ("idx", "th", "live", "d1", "events", "sfps", "pm"))
    m = len(idx)
    SW, SH = fs.size
    fps = fs.fps

    def tt(k: int) -> float:
        return idx[min(max(k, 0), m - 1)] / fps

    # quiet segments between events: [start_sample, end_sample]
    quiet: list[list[int]] = []
    cur = 0
    for e0, e1 in events:
        if e0 - 1 >= cur:
            quiet.append([cur, e0 - 1])
        cur = e1 + 1
    if cur <= m - 1:
        quiet.append([cur, m - 1])
    if not quiet:
        quiet = [[0, m - 1]]
    # absorb very short quiet segments into the previous one (transitions)
    minq = max(1, int(round(MIN_STEP * sfps)))
    q2: list[list[int]] = []
    for q in quiet:
        if q2 and q[1] - q[0] + 1 < minq:
            q2[-1][1] = q[1]
        else:
            q2.append(q)
    if len(q2) > 1 and q2[0][1] - q2[0][0] + 1 < minq:
        q2[1][0] = q2[0][0]
        q2.pop(0)
    quiet = q2

    # decide per boundary: split / scroll / minor
    ign_frames = {}
    frames_decoded: set[int] = set()

    def ign640(k: int, shape) -> np.ndarray:
        key = (live[k].tobytes(), shape)
        if key not in ign_frames:
            ign_frames[key] = _pix_mask(live[k], *shape)
        return ign_frames[key]

    steps: list[dict] = []
    for qi, q in enumerate(quiet):
        if qi == 0:
            steps.append({"segs": [q], "scroll": []})
            continue
        prev = steps[-1]["segs"][-1]
        a, b = prev[1], q[0]
        frac = _masked_frac(th[a], th[b], pm(a) | pm(b))
        ga, gb = _gray_small(fs, idx[a]), _gray_small(fs, idx[b])
        frames_decoded.update((idx[a], idx[b]))
        dy = _scroll_dy(ga, gb, ign640(a, ga.shape) | ign640(b, gb.shape))
        if dy is not None:
            steps[-1]["segs"].append(q)
            steps[-1]["scroll"].append({"t": _r2(tt(b)), "dy": int(round(dy * SW / ga.shape[1])), "before": idx[a]})
        elif frac < STEP_FRAC:
            steps[-1]["segs"].append(q)
        else:
            steps.append({"segs": [q], "scroll": []})

    # stable sample per step (after the last scroll)
    for st in steps:
        segs = st["segs"]
        if st["scroll"]:
            segs = [s for s in segs if tt(s[0]) >= st["scroll"][-1]["t"] - 1e-6] or segs
        best = None
        for s0, s1 in segs:
            for k in range(s0, s1 + 1):
                sc = (d1[k] if k > s0 else 0) + (d1[k + 1] if k < s1 else 0)
                edge = min(k - s0, s1 - k)
                key = (round(sc, 3), -min(edge, int(sfps)), -(s1 - s0))
                if best is None or key < best[0]:
                    best = (key, k)
        st["stable_k"] = best[1]
        st["t0"] = 0.0 if st is steps[0] else tt(st["segs"][0][0])
        st["t1"] = tt(st["segs"][-1][1])
    if steps:
        steps[-1]["t1"] = max(steps[-1]["t1"], (fs.n_frames - 1) / fps)

    # OCR stable frames (+ frames before scrolls)
    jobs = []
    for st in steps:
        jobs.append(idx[st["stable_k"]])
        jobs += [s["before"] for s in st["scroll"]]
    ocr_res: dict[int, list[dict]] = {}
    if fn is not None:
        for n in dict.fromkeys(jobs):
            ocr_res[n] = _ocr_frame(fs, n, fn, ocr_name if isinstance(ocr, str) else "custom")
    frames_decoded.update(jobs)

    steps_dir = out / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    built: list[dict] = []
    prev_img = None
    for st in steps:
        n = idx[st["stable_k"]]
        img = fs.get(n)
        words = ocr_res.get(n, [])
        live_area = float(live[st["stable_k"]].mean())
        raw_lines = _structure(words, fs.size, "gui")
        kind = _classify(img, raw_lines, live_area, prev_img)
        ctx = "terminal" if kind == "terminal" else "gui"
        lines = _structure(words, fs.size, ctx) if ctx == "terminal" else raw_lines
        used: set[str] = set()
        st_t = n / fps
        built.append({"n": n, "img": img, "kind": kind, "lines": lines, "words": words, "st": st, "t": st_t,
                      "extra": [(s["before"], s["before"] / fps) for s in st["scroll"]]})
        prev_img = img
        del used

    # merge adjacent steps with near-identical text (selection highlight, hover tooltips)
    merged: list[dict] = []
    for b in built:
        if merged:
            a = merged[-1]
            ta = [ln["text"] for ln in a["lines"]]
            tb = [ln["text"] for ln in b["lines"]]
            if ta and tb and a["kind"] == b["kind"]:
                sim = difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()
                if sim >= TEXT_SIM_MERGE:
                    keep = a if (a["st"]["t1"] - a["st"]["t0"]) >= (b["st"]["t1"] - b["st"]["t0"]) else b
                    new = dict(keep)
                    new["st"] = dict(keep["st"], t0=a["st"]["t0"], t1=b["st"]["t1"],
                                     scroll=a["st"]["scroll"] + b["st"]["scroll"])
                    new["extra"] = a["extra"] + b["extra"]
                    merged[-1] = new
                    continue
        merged.append(b)

    out_steps = []
    for i, b in enumerate(merged, 1):
        sid = f"s{i}"
        st = b["st"]
        used: set[str] = set()
        items, fields, commands = _items_for(sid, b["lines"], b["t"], b["kind"], fs.size, used)
        # scroll endpoints: text that scrolled out of view
        seen = {it["text"] for it in items}
        for n_before, t_before in b["extra"]:
            ctx = "terminal" if b["kind"] == "terminal" else "gui"
            lines_b = _structure(ocr_res.get(n_before, []), fs.size, ctx)
            lines_b = [ln for ln in lines_b if ln["text"] not in seen]
            it2, f2, c2 = _items_for(sid, lines_b, t_before, b["kind"], fs.size, used)
            items += it2
            for k, v in f2.items():
                fields.setdefault(k, v)
            for c in c2:
                if not any(c["text"] == x["text"] for x in commands):
                    commands.append(c)
            seen.update(it["text"] for it in it2)
        items = items[:MAX_ITEMS]
        shot = steps_dir / f"{sid}.png"
        b["img"].save(shot)
        priv = []
        # stable frame covers the whole step; frames before a scroll cover step start .. scroll time
        scans = [(b["words"], st["t0"], st["t1"])]
        for (n_before, _t), sc in zip(b["extra"], st["scroll"]):
            scans.append((ocr_res.get(n_before, []), st["t0"], sc["t"]))
        for words_, p_t0, p_t1 in scans:
            for p in privacy.scan_words([dict(w, text=normalise(w["text"], "gui")) for w in words_]):
                if not any(_iou(p["box"], q["box"]) > 0.6 and p["reason"] == q["reason"] for q in priv):
                    priv.append({"box": p["box"], "reason": p["reason"], "text": p["text"][:80],
                                 "t0": _r2(p_t0), "t1": _r2(p_t1)})
        step = {"id": sid, "t0": _r2(st["t0"]), "t1": _r2(st["t1"]), "stable_t": _r2(b["t"]), "kind": b["kind"],
                "title": _title(b["kind"], items, commands), "screenshot": f"steps/{sid}.png",
                "items": items, "fields": fields, "commands": commands,
                "scroll": [{"t": s["t"], "dy": s["dy"]} for s in st["scroll"]], "private": priv}
        if b["kind"] == "terminal":
            step["transcript"] = "\n".join(ln["text"] for ln in b["lines"])[:4000]
        out_steps.append(step)

    # ignored regions (cells live at any time) with their time span
    ign = []
    ever = live.mean(0) >= 0.2
    if not ever.any():
        ever = live.any(0)
    for box in _regions(ever, SW, SH):
        cx0, cy0 = box[0] * GX // SW, box[1] * GY // SH
        cx1, cy1 = max(cx0, (box[2] * GX - 1) // SW), max(cy0, (box[3] * GY - 1) // SH)
        ks = np.flatnonzero(live[:, cy0:cy1 + 1, cx0:cx1 + 1].any(axis=(1, 2)))
        ign.append({"box": box, "reason": "live_chart", "t0": _r2(tt(int(ks.min()))), "t1": _r2(tt(int(ks.max())))})

    sp, sp_state = _speech(path, speech, audio_db)
    backend = "gpu" if ocr_name == "rapidocr" else "local"
    tl = {"version": SCHEMA,
          "video": {"path": str(path), "sha256": sha, "duration": _r2(fs.duration or fs.n_frames / fps),
                    "fps": round(fps, 3), "size": [int(SW), int(SH)]},
          "steps": out_steps, "speech": sp, "ignored_regions": ign,
          "params": _params(focus, speech, ocr),
          "stats": {"frames_analysed": len(frames_decoded), "samples": m, "ocr_frames": len(ocr_res),
                    "analyse_sec": _r2(time.time() - t_start), "backend": backend, "ocr": ocr_name,
                    "speech": sp_state}}
    return tl


def _params(focus, speech, ocr) -> dict:
    return {"algo": ALGO, "focus": focus, "speech": speech, "ocr": ocr if isinstance(ocr, str) else "custom"}


# ----------------------------------------------------------------------------- public API

def timeline_dir(sha: str) -> Path:
    """<CASEFILE_HOME>/timelines/<sha256[:16]>."""
    return _home() / sha[:16]


def analyze(video: str | Path, focus=None, speech: str = "auto", ocr="auto", out_dir: str | Path | None = None,
            force: bool = False) -> dict:
    """Analyse a recording once into timeline.json; returns ``summary(tl)`` (≤ 12k chars) + ``path``.

    focus: optional keyword or list of keywords listed first in the summary. speech: "auto"|"none".
    ocr: "auto"|"tesseract"|"rapidocr"|"none" or a callable(img, region=None) -> [{text, box, conf}].
    Cached per (sha256, params): a second call returns the stored timeline without re-analysing.
    """
    path = check_path(video)
    if speech not in ("auto", "none", "on"):
        raise ToolError(f"bad speech mode {speech!r}", 'use "auto" or "none"')
    t0 = time.time()
    sha = _sha256(path)
    out = Path(out_dir) if out_dir else timeline_dir(sha)
    tj = out / "timeline.json"
    params = _params(focus, speech, ocr)
    if tj.exists() and not force and not callable(ocr):
        try:
            tl = json.loads(tj.read_text(encoding="utf-8"))
            if tl.get("params") == params and tl.get("video", {}).get("sha256") == sha:
                s = summary(tl, focus=focus)
                s["path"] = str(tj)
                s["cached"] = True
                s["load_sec"] = _r2(time.time() - t0)
                return s
        except (OSError, ValueError):
            pass
    out.mkdir(parents=True, exist_ok=True)
    tl = _analyse(path, sha, out, focus, speech, ocr)
    tmp = out / "timeline.tmp.json"
    tmp.write_text(json.dumps(tl, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(tj)
    s = summary(tl, focus=focus)
    s["path"] = str(tj)
    s["cached"] = False
    return s


def load(path_or_video: str | Path) -> dict:
    """Load a timeline from timeline.json, its directory, or the analysed video (looked up by sha256)."""
    p = check_path(path_or_video)
    if p.is_dir():
        p = p / "timeline.json"
    if p.suffix.lower() != ".json":
        cand = timeline_dir(_sha256(p)) / "timeline.json"
        if not cand.exists():
            raise ToolError(f"no timeline for {p.name}", "run analyze_recording(video) first")
        p = cand
    if not p.exists():
        raise ToolError(f"timeline not found: {p}", "run analyze_recording(video) first")
    tl = json.loads(p.read_text(encoding="utf-8"))
    tl.setdefault("_path", str(p))
    return tl


def _focus_terms(focus) -> list[str]:
    if not focus:
        return []
    if isinstance(focus, str):
        return [f for f in re.split(r"[,;]\s*|\s+", focus) if f]
    return [str(f) for f in focus]


def summary(tl: dict, focus=None, max_chars: int = MAX_SUMMARY) -> dict:
    """Compact view: steps with id/t0/t1/kind/title/fields(≤10)/commands and the first items (id+text)."""
    terms = [slug(f) for f in _focus_terms(focus if focus is not None else tl.get("params", {}).get("focus"))]
    n_items, n_fields, n_cmd = 3, 10, 5

    def build() -> dict:
        steps = []
        for s in tl["steps"]:
            items = s.get("items", [])
            if terms:
                items = sorted(items, key=lambda it: not any(t in it["id"] for t in terms))
            f = dict(list(s.get("fields", {}).items())[:n_fields])
            d = {"id": s["id"], "t0": s["t0"], "t1": s["t1"], "stable_t": s.get("stable_t"), "kind": s["kind"],
                 "title": s.get("title", "")[:80], "fields": {k[:40]: str(v)[:40] for k, v in f.items()},
                 "commands": [c["text"][:160] for c in s.get("commands", [])[:n_cmd]],
                 "items": [f"{it['id']}={it['text'][:50]}" for it in items[:n_items]],
                 "n_items": len(s.get("items", []))}
            if s.get("scroll"):
                d["scroll"] = len(s["scroll"])
            if s.get("private"):
                d["private"] = sorted({p["reason"] for p in s["private"]})
            steps.append(d)
        v = tl["video"]
        return {"version": tl.get("version"), "video": {"duration": v["duration"], "size": v["size"], "fps": v["fps"]},
                "n_steps": len(steps), "steps": steps,
                "ignored_regions": [{"box": r["box"], "reason": r["reason"]} for r in tl.get("ignored_regions", [])][:8],
                "speech": len(tl.get("speech", [])), "stats": tl.get("stats", {})}

    s = build()
    while len(json.dumps(s, ensure_ascii=False)) > max_chars and (n_items or n_fields > 2 or n_cmd > 1):
        if n_items:
            n_items -= 1
        elif n_fields > 2:
            n_fields -= 2
        else:
            n_cmd -= 1
        s = build()
    if len(json.dumps(s, ensure_ascii=False)) > max_chars:
        keep = max(1, len(s["steps"]) * max_chars // len(json.dumps(s, ensure_ascii=False)) - 1)
        s["more_steps"] = len(s["steps"]) - keep
        s["steps"] = s["steps"][:keep]
    return s


_FILLER = re.compile(r"^(?:(?:the|a|an)\s+)?(?:stable\s+time\s+(?:for|of)|when\s+is|where\s+is|value\s+of|what\s+is|"
                     r"show|find|time\s+(?:for|of)|box\s+(?:for|of))\s+(?:the\s+)?", re.I)


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _item_keys(it: dict) -> list[str]:
    base = it["id"].split(".", 1)[1]
    base = re.sub(r"_\d+$", "", base)
    return [_norm_key(base), _norm_key(it["text"])]


def _score(q: str, it: dict) -> float:
    qk = _norm_key(q)
    if not qk:
        return 0.0
    best = 0.0
    for k in _item_keys(it):
        if not k:
            continue
        if k == qk:
            return 1.0
        r = difflib.SequenceMatcher(None, qk, k).ratio()
        if len(qk) >= 3 and k.startswith(qk):
            r = max(r, 0.9)
        best = max(best, r)
    return best


def _answer(tl: dict, q: str) -> dict:
    raw = q.strip()
    steps = tl["steps"]
    for s in steps:                                   # exact item id
        for it in s["items"]:
            if it["id"] == raw:
                return _ans(raw, it, s)
    body = _FILLER.sub("", raw).strip().strip("?").strip()
    step_f = None
    m = re.match(r"^(s\d+)[.:\s]\s*(.+)$", body)
    if m:
        step_f, body = m.group(1), m.group(2)
    cands = []
    for s in steps:
        if step_f and s["id"] != step_f:
            continue
        for it in s["items"]:
            sc = _score(body, it)
            if sc >= 0.72:
                bonus = 0.05 if it["role"] == "field" else 0.0
                cands.append((sc + bonus, it.get("conf", 0), it, s))
    if not cands:
        return {"q": raw, "found": False}
    cands.sort(key=lambda c: (-c[0], -c[1], c[3]["t0"]))
    return _ans(raw, cands[0][2], cands[0][3], also=len({c[3]["id"] for c in cands}) - 1)


def _ans(q: str, it: dict, s: dict, also: int = 0) -> dict:
    d = {"q": q, "found": True, "id": it["id"], "text": it["text"], "box": it["box"], "t": it["t"], "step": s["id"],
         "stable_t": s.get("stable_t"), "step_t0": s["t0"], "step_t1": s["t1"]}
    if "value" in it:
        d["value"] = it["value"]
    if also > 0:
        d["other_steps"] = also
    return d


def query(tl: dict | str | Path, questions: list[str] | str) -> list[dict]:
    """Answer item questions ('s5.pusch_snr', 'pusch_snr', 'stable time for dl_bitrate') by fuzzy match over
    items/fields -> [{q, found, id, text, box, t, step, value?}]."""
    if not isinstance(tl, dict):
        tl = load(tl)
    if isinstance(questions, str):
        questions = [questions]
    return [_answer(tl, q) for q in questions]


def locate(tl: dict | str | Path, items: list[dict | str]) -> list[dict]:
    """Find text on the timeline -> [{text, found, id, box, t, step}]. items: [{text, step?}] or strings.

    A match inside a longer OCR line gets a box narrowed proportionally to the character span."""
    if not isinstance(tl, dict):
        tl = load(tl)
    out = []
    for spec in items:
        if isinstance(spec, str):
            spec = {"text": spec}
        text = str(spec.get("text", "")).strip()
        step = spec.get("step")
        best = None
        nq = normalise(text).lower()
        for s in tl["steps"]:
            if step and s["id"] != step:
                continue
            for it in s["items"]:
                t = it["text"].lower()
                pos = t.find(nq) if nq else -1
                if pos >= 0:
                    sc = 1.0 if len(nq) == len(t) else 0.95
                    x0, y0, x1, y1 = it["box"]
                    cw = (x1 - x0) / max(1, len(t))
                    box = [int(x0 + pos * cw), y0, int(x0 + (pos + len(nq)) * cw), y1] if sc < 1 else it["box"]
                else:
                    sc = difflib.SequenceMatcher(None, _norm_key(text), _norm_key(it["text"])).ratio()
                    box = it["box"]
                if best is None or sc > best[0]:
                    best = (sc, it, s, box)
        if best is None or best[0] < 0.75:
            out.append({"text": text, "found": False})
        else:
            sc, it, s, box = best
            out.append({"text": text, "found": True, "id": it["id"], "box": box, "t": it["t"], "step": s["id"]})
    return out
