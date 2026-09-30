"""RapidOCR (PP-OCR det+rec ONNX models) on onnxruntime CUDA, tiled for big screen recordings.

Screen frames are large (2560x1440) with small UI text; the detector downsizes its input to
`det_limit_side_len`, so the frame is cut into overlapping tiles (default 1280 px, 96 px overlap) that are each
detected at native resolution, then lines found twice in overlaps are de-duplicated.

API: `available()`, `cuda()`, `ocr_lines(img, region=None)` -> [{text, box, conf}] lines (source px, conf 0-100),
`unload()` releases the ONNX sessions (VRAM) — the worker calls it after every job.
Supports both the `rapidocr_onnxruntime` 1.x package and the newer `rapidocr` 2.x/3.x package.
"""
from __future__ import annotations

import gc
import importlib.util
import os
import threading
from typing import Any

import numpy as np
from PIL import Image

TILE = int(os.environ.get("CASEFILE_OCR_TILE", "1280"))
OVERLAP = 96
MIN_CONF = 0.5

_engine: Any = None
_engine_kind: str | None = None
_lock = threading.Lock()


def _pkg() -> str | None:
    for name in ("rapidocr_onnxruntime", "rapidocr"):
        if importlib.util.find_spec(name) is not None:
            return name
    return None


def available() -> bool:
    """A RapidOCR package is importable."""
    return _pkg() is not None


def cuda() -> bool:
    """onnxruntime has a USABLE CUDA execution provider (see casefile.gpu.cuda_onnx)."""
    from .gpu import cuda_onnx
    return cuda_onnx()


def _load(use_cuda: bool) -> tuple[Any, str]:
    global _engine, _engine_kind
    with _lock:
        if _engine is not None:
            return _engine, _engine_kind  # type: ignore[return-value]
        pkg = _pkg()
        if pkg is None:
            raise ImportError("rapidocr not installed")
        if pkg == "rapidocr_onnxruntime":
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR(det_use_cuda=use_cuda, cls_use_cuda=use_cuda, rec_use_cuda=use_cuda,
                               det_limit_side_len=TILE, det_limit_type="max", use_cls=False)
        else:
            from rapidocr import RapidOCR
            _engine = RapidOCR(params={"EngineConfig.onnxruntime.use_cuda": use_cuda,
                                       "Det.limit_side_len": TILE, "Det.limit_type": "max",
                                       "Global.use_cls": False})
        _engine_kind = pkg
        return _engine, pkg


def unload() -> None:
    """Drop the engine (and its ONNX sessions / CUDA arena)."""
    global _engine, _engine_kind
    with _lock:
        _engine = None
        _engine_kind = None
    gc.collect()


def _run(engine: Any, kind: str, arr: np.ndarray) -> list[tuple[list, str, float]]:
    if kind == "rapidocr_onnxruntime":
        res, _ = engine(arr)
        return [(r[0], r[1], float(r[2])) for r in (res or [])]
    out = engine(arr)
    boxes, txts, scores = getattr(out, "boxes", None), getattr(out, "txts", None), getattr(out, "scores", None)
    if boxes is None or txts is None:
        return []
    return [(b, t, float(s)) for b, t, s in zip(boxes, txts, scores)]


def _tiles(w: int, h: int, tile: int = TILE, overlap: int = OVERLAP) -> list[tuple[int, int, int, int]]:
    def edges(n: int) -> list[tuple[int, int]]:
        if n <= tile:
            return [(0, n)]
        k = -(-(n - overlap) // (tile - overlap))
        step = (n - tile) / (k - 1) if k > 1 else 0
        return [(round(i * step), round(i * step) + tile) for i in range(k)]
    return [(x0, y0, x1, y1) for y0, y1 in edges(h) for x0, x1 in edges(w)]


def _area(b: list[int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _dedupe(lines: list[dict]) -> list[dict]:
    """Drop lines mostly (>=60 %) covered by a longer / more confident line (tile-overlap duplicates)."""
    lines = sorted(lines, key=lambda d: (-len(d["text"]), -d["conf"]))
    keep: list[dict] = []
    for d in lines:
        b = d["box"]
        dup = False
        for k in keep:
            kb = k["box"]
            ix = max(0, min(b[2], kb[2]) - max(b[0], kb[0]))
            iy = max(0, min(b[3], kb[3]) - max(b[1], kb[1]))
            if ix * iy >= 0.6 * max(1, min(_area(b), _area(kb))):
                dup = True
                break
        if not dup:
            keep.append(d)
    keep.sort(key=lambda d: (d["box"][1], d["box"][0]))
    return keep


def ocr_lines(img: Image.Image, region: tuple[int, int, int, int] | None = None, *, use_cuda: bool | None = None,
              tile: int = TILE) -> list[dict]:
    """OCR text lines of a PIL image -> [{text, box:[x0,y0,x1,y1], conf:0-100}] in full-image px."""
    engine, kind = _load(cuda() if use_cuda is None else use_cuda)
    ox = oy = 0
    if region is not None:
        img = img.crop(tuple(region))
        ox, oy = int(region[0]), int(region[1])
    rgb = np.asarray(img.convert("RGB"))
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    h, w = bgr.shape[:2]
    out: list[dict] = []
    for x0, y0, x1, y1 in _tiles(w, h, tile):
        x1, y1 = min(x1, w), min(y1, h)
        for pts, text, score in _run(engine, kind, bgr[y0:y1, x0:x1]):
            text = (text or "").strip()
            if not text or score < MIN_CONF:
                continue
            p = np.asarray(pts, dtype=float).reshape(-1, 2)
            box = [int(p[:, 0].min()) + x0 + ox, int(p[:, 1].min()) + y0 + oy,
                   int(np.ceil(p[:, 0].max())) + x0 + ox, int(np.ceil(p[:, 1].max())) + y0 + oy]
            out.append({"text": text, "box": box, "conf": int(round(score * 100))})
    return _dedupe(out)


def split_words(lines: list[dict]) -> list[dict]:
    """Split line results into words with boxes proportional to character offsets (good for UI/mono text)."""
    words: list[dict] = []
    for ln in lines:
        text = ln["text"]
        x0, y0, x1, y1 = ln["box"]
        n = max(1, len(text))
        cw = (x1 - x0) / n
        i = 0
        for tok in text.split(" "):
            if tok:
                words.append({"text": tok, "box": [int(round(x0 + i * cw)), y0, int(round(x0 + (i + len(tok)) * cw)), y1],
                              "conf": ln["conf"]})
            i += len(tok) + 1
    return words
