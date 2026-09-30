"""Phase 1 detection: video probe, bad frames (black / glitch / popup), scene + layout changes.

All analysis runs on tiny grayscale thumbnails (160x90 uint8) that are extracted once per video
by streaming through the FrameStore JPEGs and cached as a memory-mapped ``thumbs.npy`` in the
FrameStore dir, so the whole video is never held in RAM. The probe result is cached as
``probe.json`` next to it.

Heuristics
- black:   mean luma < BLACK_LUMA.
- outlier: frame i differs strongly from both neighbours while the neighbours match each other.
           ``popup`` when the difference covers < POPUP_AREA of the frame (localised), else ``glitch``.
- scene:   large difference between consecutive GOOD frames (bad frames skipped); cuts closer than
           MIN_SCENE_GAP seconds to the previous cut are merged (fades / transitions).
- layout:  a scene cut whose edge/structure map changes (edge IoU < LAYOUT_IOU).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .errors import ToolError, check_path
from .media import FrameStore, ffmpeg

VERSION = 2
TW, TH = 160, 90            # thumbnail size
PIX_T = 12                  # per-pixel |diff| counted as "changed" (0..255)
BLACK_LUMA = 16.0
OUTLIER_FRAC = 0.03         # min changed fraction vs each neighbour for an outlier
OUTLIER_RATIO = 0.25        # neighbours-vs-each-other must be < ratio * min(side diffs)
POPUP_AREA = 0.60           # outlier changed area below this -> popup
SCENE_FRAC = 0.03           # changed fraction between consecutive good frames -> cut
SCENE_EDGE_IOU = 0.6        # or structure change (edge IoU below this with >1% pixels changed)
SCENE_MAD = 6.0             # or mean abs diff above this
MIN_SCENE_GAP = 0.5         # s
LAYOUT_IOU = 0.5
EDGE_T = 14.0
MAX_CHARS = 2400            # ~600 tokens
CAPS = {"scenes": 30, "bad_frames": 40, "popups": 20, "layout_changes": 20}


# ----------------------------------------------------------------------------- thumbnails

def _thumb(path: Path) -> np.ndarray:
    im = Image.open(path)
    im.draft("L", (TW * 2, TH * 2))          # fast JPEG DCT downscale
    im = im.convert("L").resize((TW, TH), Image.Resampling.BOX)
    return np.asarray(im, dtype=np.uint8)


def thumbnails(fs: FrameStore) -> np.ndarray:
    """(n_frames, TH, TW) uint8 memmap of grayscale thumbnails, cached as thumbs.npy in fs.dir."""
    p = fs.dir / "thumbs.npy"
    if p.exists():
        arr = np.load(p, mmap_mode="r")
        if arr.shape == (fs.n_frames, TH, TW):
            return arr
    tmp = fs.dir / "thumbs.tmp.npy"
    mm = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.uint8, shape=(fs.n_frames, TH, TW))
    for n in range(fs.n_frames):
        mm[n] = _thumb(fs.frame_path(n))
    mm.flush()
    del mm
    tmp.replace(p)
    return np.load(p, mmap_mode="r")


def _diff(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(changed fraction, mean abs diff) between two thumbnails."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return float((d > PIX_T).mean()), float(d.mean())


def _edges(a: np.ndarray) -> np.ndarray:
    f = a.astype(np.float32)
    g = np.zeros_like(f)
    g[:, 1:] += np.abs(f[:, 1:] - f[:, :-1])
    g[1:, :] += np.abs(f[1:, :] - f[:-1, :])
    e = g > EDGE_T
    d = e.copy()                                  # 1-px dilation: tolerate small shifts
    d[1:, :] |= e[:-1, :]
    d[:-1, :] |= e[1:, :]
    d[:, 1:] |= e[:, :-1]
    d[:, :-1] |= e[:, 1:]
    return d


def _edge_iou(a: np.ndarray, b: np.ndarray) -> float:
    ea, eb = _edges(a), _edges(b)
    u = (ea | eb).sum()
    return float((ea & eb).sum() / u) if u else 1.0


# ----------------------------------------------------------------------------- analysis

def _analyse(fs: FrameStore) -> dict:
    th = thumbnails(fs)
    n = fs.n_frames
    fps = fs.fps
    luma = np.array([float(th[i].mean()) for i in range(n)])
    d1 = np.zeros(n)        # changed frac i-1 -> i
    skip = np.zeros(n)      # changed frac i-1 -> i+1
    prev2, prev = None, None
    for i in range(n):
        cur = th[i]
        if prev is not None:
            d1[i] = _diff(prev, cur)[0]
        if prev2 is not None:
            skip[i - 1] = _diff(prev2, cur)[0]
        prev2, prev = prev, cur

    bad: dict[int, str] = {}
    for i in range(n):
        if luma[i] < BLACK_LUMA:
            bad[i] = "black"
    for i in range(1, n - 1):
        if i in bad or (i - 1) in bad or (i + 1) in bad:
            continue
        lo = min(d1[i], d1[i + 1])
        if lo > OUTLIER_FRAC and skip[i] < OUTLIER_RATIO * lo:
            bad[i] = "popup" if max(d1[i], d1[i + 1]) < POPUP_AREA else "glitch"

    good = [i for i in range(n) if i not in bad]
    cuts: list[int] = []
    layout: list[int] = []
    for k in range(1, len(good)):
        p, g = good[k - 1], good[k]
        frac, mad = _diff(th[p], th[g])
        if frac < SCENE_FRAC and mad < SCENE_MAD and not (frac > 0.01 and _edge_iou(th[p], th[g]) < SCENE_EDGE_IOU):
            continue
        if cuts and (g - cuts[-1]) / fps < MIN_SCENE_GAP:
            continue
        cuts.append(g)
        after = good[min(k + 2, len(good) - 1)]    # settled frame after the cut
        if _edge_iou(th[p], th[after]) < LAYOUT_IOU:
            layout.append(g)
    return {"bad": bad, "cuts": cuts, "layout": layout}


def _audio_db(path: Path) -> float | None:
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-vn",
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", (r.stderr or b"").decode(errors="replace"))
    if not m:
        return None
    return -91.0 if m.group(1) == "-inf" else round(float(m.group(1)), 1)


def _cap(out: dict, caps: dict[str, int]) -> dict:
    trunc = {}
    for k, c in caps.items():
        if len(out[k]) > c:
            trunc[k] = len(out[k]) - c
            out[k] = out[k][:c]
    if trunc:
        out["truncated"] = trunc
    return out


def _full(video: str | Path) -> tuple[FrameStore, dict]:
    path = check_path(video)
    try:
        fs = FrameStore(path)
    except ToolError:
        raise
    except Exception as e:  # pragma: no cover - decoder oddities
        raise ToolError(f"cannot read video {path}: {e}", "pass a readable video file") from e
    cache = fs.dir / "probe.json"
    if cache.exists():
        j = json.loads(cache.read_text())
        if j.get("_v") == VERSION:
            return fs, j
    a = _analyse(fs)
    fps = fs.fps

    def t(f: int) -> float:
        return round(f / fps, 2)

    j = {
        "_v": VERSION,
        "duration": round(fs.duration or fs.n_frames / fps, 2),
        "fps": round(fps, 3),
        "size": [int(fs.size[0]), int(fs.size[1])],
        "frames": fs.n_frames,
        "audio_db": _audio_db(fs.path) if fs.has_audio else None,
        "scenes": [0.0] + [t(c) for c in a["cuts"]],
        "bad_frames": [{"frame": f, "t": t(f), "kind": k} for f, k in sorted(a["bad"].items())],
        "popups": [t(f) for f, k in sorted(a["bad"].items()) if k == "popup"],
        "layout_changes": [t(c) for c in a["layout"]],
    }
    cache.write_text(json.dumps(j))
    return fs, j


# ----------------------------------------------------------------------------- public API

def probe(video: str | Path) -> dict:
    """Probe a video: metadata, audio level, scene starts, bad frames, popups and layout changes.

    Returns ``{duration, fps, size:[w,h], frames, audio_db, scenes:[t], bad_frames:[{frame,t,kind}],
    popups:[t], layout_changes:[t]}`` (+ ``truncated:{list: n_dropped}`` when lists are capped), kept
    under ~600 tokens. ``scenes`` are scene start times (first is 0.0). Cached in the FrameStore dir.
    """
    _, j = _full(video)
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in j.items() if not k.startswith("_")}
    caps = dict(CAPS)
    out = _cap(out, caps)
    while len(json.dumps(out)) > MAX_CHARS:     # shrink further if still too long
        for k in caps:
            caps[k] = max(3, caps[k] // 2)
        out = _cap({k: (list(v) if isinstance(v, list) else v) for k, v in j.items() if not k.startswith("_")},
                   caps)
        if all(c == 3 for c in caps.values()):
            break
    return out


def bad_frame_list(video: str | Path) -> list[int]:
    """Sorted 0-based indices of all detected bad frames (black / glitch / popup), uncapped."""
    _, j = _full(video)
    return [b["frame"] for b in j["bad_frames"]]


def remap_for(video: str | Path, bad: list[int] | str = "auto") -> dict[int, int]:
    """Map each bad frame -> nearest EARLIER good frame (next later good one if none earlier).

    ``bad``: explicit frame list, ``"auto"`` (detected bad frames) or ``"none"`` (empty map).
    """
    if bad == "none" or bad is None:
        return {}
    if bad == "auto":
        frames = bad_frame_list(video)
    elif isinstance(bad, str):
        raise ToolError(f"invalid bad-frame spec: {bad!r}", 'use "auto", "none" or a list of frame numbers')
    else:
        frames = [int(b) for b in bad]
    fs = FrameStore(check_path(video))
    n = fs.n_frames
    bs = {f for f in frames if 0 <= f < n}
    out: dict[int, int] = {}
    for f in sorted(bs):
        g = f - 1
        while g >= 0 and g in bs:
            g -= 1
        if g < 0:
            g = f + 1
            while g < n and g in bs:
                g += 1
            if g >= n:
                raise ToolError("every frame is marked bad", "pass a shorter bad-frame list")
        out[f] = g
    return out
