"""Paths, constants and the workdir sandbox shared by every module."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent


def _first_existing(*cands: Path) -> Path:
    for c in cands:
        if c.exists():
            return c
    return cands[0]


ASSETS_DIR = _first_existing(PKG_DIR / "_assets", REPO_DIR / "assets")
FONTS_DIR = ASSETS_DIR / "fonts"


def home_dir() -> Path:
    """User data dir: models + caches. Override with CASEFILE_HOME."""
    p = Path(os.environ.get("CASEFILE_HOME", Path.home() / ".casefile"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    repo_models = REPO_DIR / "models"
    if (repo_models / "voices-v1.0.bin").exists():
        return repo_models
    p = home_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir(sub: str = "") -> Path:
    p = home_dir() / "cache" / sub if sub else home_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workdir() -> Path | None:
    w = os.environ.get("CASEFILE_WORKDIR")
    return Path(w).resolve() if w else None


KOKORO_URLS = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

# Brand + output defaults (neutral; override per video with spec.brand)
ACCENT = (79, 70, 229)        # #4F46E5 indigo
INK = (17, 24, 39)            # #111827
BAR_BG = (17, 20, 33)
OUT_W, OUT_H, OUT_FPS = 1920, 1080, 30
BAR_H = 111                   # caption bar height at 1080p
CRF = 18
AAC_BITRATE = "160k"
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def tool_path(name: str) -> str | None:
    """Locate ffmpeg / ffprobe / tesseract, including the default Windows Tesseract install."""
    found = shutil.which(name)
    if found:
        return found
    if name == "tesseract":
        for c in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"):
            if c.exists():
                return str(c)
    return None
