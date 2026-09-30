"""OCR backend selection: RapidOCR on CUDA when available, else tesseract (casefile.vision).

`ocr(img, region=None, level="word", backend="auto") -> [{text, box:[x0,y0,x1,y1], conf}]` (source px, conf 0-100).
- backend "auto": env CASEFILE_OCR (rapidocr|tesseract) wins; else rapidocr when the package is importable AND
  onnxruntime has CUDA (CPU RapidOCR is not faster than tesseract on these frames); else tesseract.
- level "word" (tesseract native; rapidocr lines split proportionally) or "line" (tesseract words merged into
  reading-order lines).
`backend_name()` tells which one "auto" picks; `unload()` frees GPU models.
"""
from __future__ import annotations

import os

from PIL import Image

from .errors import ToolError

BACKENDS = ("auto", "rapidocr", "tesseract")


def _rapid_ok() -> bool:
    try:
        from . import ocr_gpu
    except ImportError:  # numpy/PIL missing: impossible in practice
        return False
    return ocr_gpu.available() and ocr_gpu.cuda()


def backend_name(backend: str = "auto") -> str:
    """Resolve "auto" to "rapidocr" or "tesseract"."""
    b = (backend or "auto").lower()
    if b not in BACKENDS:
        raise ToolError(f"unknown OCR backend {backend!r}", f"use one of {', '.join(BACKENDS)}")
    if b == "auto":
        env = os.environ.get("CASEFILE_OCR", "").lower()
        if env in ("rapidocr", "tesseract"):
            b = env
        else:
            return "rapidocr" if _rapid_ok() else "tesseract"
    if b == "rapidocr":
        from . import ocr_gpu
        if not ocr_gpu.available():
            return "tesseract"
    return b


def _lines_from_words(words: list[dict]) -> list[dict]:
    from .vision import _lines
    out = []
    for ln in _lines(words):
        xs0, ys0 = [w["box"][0] for w in ln], [w["box"][1] for w in ln]
        xs1, ys1 = [w["box"][2] for w in ln], [w["box"][3] for w in ln]
        out.append({"text": " ".join(w["text"] for w in ln), "box": [min(xs0), min(ys0), max(xs1), max(ys1)],
                    "conf": int(round(sum(w["conf"] for w in ln) / len(ln)))})
    return out


def ocr(img: Image.Image, region=None, *, level: str = "word", backend: str = "auto") -> list[dict]:
    """OCR a PIL image with the best available backend -> [{text, box, conf}]."""
    b = backend_name(backend)
    if level not in ("word", "line"):
        raise ToolError(f"unknown level {level!r}", "use 'word' or 'line'")
    if b == "rapidocr":
        from . import ocr_gpu
        from .vision import _norm_region
        reg = _norm_region(region, img.size)
        lines = ocr_gpu.ocr_lines(img, reg)
        return lines if level == "line" else ocr_gpu.split_words(lines)
    from .vision import ocr_image
    words = ocr_image(img, region)
    return words if level == "word" else _lines_from_words(words)


def unload() -> None:
    """Release GPU OCR models (no-op for tesseract)."""
    try:
        from . import ocr_gpu
        ocr_gpu.unload()
    except ImportError:
        pass
