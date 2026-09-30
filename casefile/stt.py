"""Speech-to-text with faster-whisper (CTranslate2), loaded lazily and unloaded after each call.

`transcribe(path, model="small", device="auto", language=None, keep_loaded=False) -> [{t0, t1, text}]`
- device "auto": "cuda" when CTranslate2 sees a GPU, else "cpu". compute_type int8 (cuda: int8_float16 is not
  supported on Turing GTX 16xx without tensor cores -> plain int8 works everywhere).
- VAD filter on, so silent screen recordings return [] quickly.
- Models download to `<CASEFILE_HOME>/models/whisper` on first use.
"""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

from . import config
from .errors import ToolError

_model: Any = None
_model_key: tuple | None = None


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def load(model: str = "small", device: str = "auto", compute_type: str = "int8") -> Any:
    """Load (or reuse) a WhisperModel."""
    global _model, _model_key
    dev = _device(device)
    key = (model, dev, compute_type)
    if _model is not None and _model_key == key:
        return _model
    unload()
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ToolError(f"faster-whisper not installed: {e}", "pip install 'casefile-mcp[worker]' (faster-whisper)") from e
    root = config.models_dir() / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    _model = WhisperModel(model, device=dev, compute_type=compute_type, download_root=str(root))
    _model_key = key
    return _model


def unload() -> None:
    """Free the model (RAM + VRAM)."""
    global _model, _model_key
    _model = None
    _model_key = None
    gc.collect()


def load_audio(path: str | Path, sr: int = 16000):
    """Decode any audio/video file to mono float32 at `sr` Hz with ffmpeg (avoids PyAV version drift)."""
    import subprocess

    import numpy as np

    from .media import ffmpeg
    r = subprocess.run([ffmpeg(), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise ToolError(f"could not decode audio of {path}: {r.stderr.decode(errors='replace')[-300:]}",
                        "pass a file with an audio stream")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(path: str | Path, model: str = "small", device: str = "auto", language: str | None = None,
               keep_loaded: bool = False, vad: bool = True) -> list[dict]:
    """Transcribe an audio/video file -> [{t0, t1, text}] (seconds, 2 decimals)."""
    p = Path(path)
    if not p.exists():
        raise ToolError(f"file not found: {p}", "pass an audio or video file path")
    audio = load_audio(p)
    if audio.size == 0:
        return []
    m = load(model, device)
    try:
        segs, _info = m.transcribe(audio, language=language, vad_filter=vad, beam_size=5)
        out = [{"t0": round(s.start, 2), "t1": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()]
    finally:
        if not keep_loaded:
            unload()
    return out
