"""Offline narration: Kokoro ONNX TTS with a pronunciation lexicon, a WAV cache and track mixing.

- `speakable(text, lexicon)` turns caption-ish text into what the voice should say.
- `tts(lines, voice, speed, lexicon)` synthesises (or reuses cached) WAVs and returns durations.
- `narration_track(placements, total, out)` mixes the per-line WAVs into one mono track.
- `estimate(text, speed)` is the word-rate fallback used when the voice engine is "none".
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from pathlib import Path

import numpy as np

from . import config
from .errors import ToolError
from .spec import DEFAULT_LEXICON

SR = 24000
MODEL_FILES = ("kokoro-v1.0.onnx", "kokoro-v1.0.int8.onnx")  # fp32 default: 3.3x faster than int8 on x86 CPU (DECISIONS.md)
VOICES_FILE = "voices-v1.0.bin"
SILENCE_DB = -45.0
TRIM_PAD = 0.03          # seconds of breathing room kept at each end after trimming
PEAK = 0.89
WORDS_PER_SEC = 2.6      # at speed 1.0, used by the "none" engine

_kokoro = None
_kokoro_lock = threading.Lock()
_synth_lock = threading.Lock()

# glyphs that appear in captions but must not be read aloud
_GLYPHS = {
    "≈": " about ", "×": " times ", "→": ", ", "←": ", ", "·": ", ", "•": ", ", "|": ", ",
    "▶": " ", "►": " ", "◀": " ", "✓": " ", "✔": " ", "★": " ", "—": ", ", "–": " to ",
}


# ---------------------------------------------------------------------------- text

def merged_lexicon(lexicon: dict | None = None) -> dict[str, str]:
    lex = dict(DEFAULT_LEXICON)
    lex.update(lexicon or {})
    return {k: v for k, v in lex.items() if k}


def speakable(text: str, lexicon: dict | None = None) -> str:
    """Apply the (default + user) lexicon as whole-word, case-sensitive replacements and drop glyphs."""
    out = text
    for g, rep in _GLYPHS.items():
        out = out.replace(g, rep)
    lex = merged_lexicon(lexicon)
    if lex:
        keys = sorted(lex, key=len, reverse=True)  # longest first: "AS release" before "AS"
        pat = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)")
        out = pat.sub(lambda m: lex[m.group(0)], out)  # single pass: replacements are never re-expanded
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r",(\s*,)+", ",", out)
    out = re.sub(r"^[\s,]+|[\s,]+$", "", out)
    return out


def estimate(text: str, speed: float = 1.0) -> float:
    """Duration estimate (s) for engine 'none': words / (2.6 * speed) + 0.3."""
    words = len(re.findall(r"\S+", text))
    return round(words / (WORDS_PER_SEC * max(speed, 0.1)) + 0.3, 2)


# ---------------------------------------------------------------------------- cache

def _key(spoken: str, voice: str, speed: float) -> str:
    return hashlib.sha1(f"{spoken}|{voice}|{float(speed):g}".encode("utf-8")).hexdigest()


def wav_path(spoken: str, voice: str = "af_heart", speed: float = 1.1) -> Path:
    """Cache location for already-speakable text (does not synthesise)."""
    return config.cache_dir("tts") / f"{_key(spoken, voice, speed)}.wav"


def line_duration(path: str | Path) -> float:
    import soundfile as sf
    info = sf.info(str(path))
    return info.frames / float(info.samplerate)


# ---------------------------------------------------------------------------- engine

def model_paths() -> tuple[Path, Path]:
    md = config.models_dir()
    model = next((md / m for m in MODEL_FILES if (md / m).exists()), None)
    voices = md / VOICES_FILE
    if model is None or not voices.exists():
        missing = [n for n in (MODEL_FILES[0], VOICES_FILE) if not (md / n).exists()]
        raise ToolError(f"Kokoro model files missing in {md}: {', '.join(missing)}",
                        "run `casefile doctor` to download the TTS models")
    return model, voices


def _get_kokoro():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            model, voices = model_paths()
            try:
                from kokoro_onnx import Kokoro
            except ImportError as e:
                raise ToolError(f"kokoro-onnx not importable: {e}",
                                "pip install kokoro-onnx soundfile (or reinstall casefile)") from e
            try:
                _kokoro = Kokoro(str(model), str(voices))
            except Exception as e:  # corrupt download, espeak problems...
                raise ToolError(f"could not load Kokoro TTS: {e}",
                                "run `casefile doctor` (re-download models / check espeak-ng)") from e
    return _kokoro


def _trim(samples: np.ndarray, sr: int = SR) -> np.ndarray:
    """Remove leading/trailing audio below SILENCE_DB (dBFS), keeping a short pad."""
    if samples.size == 0:
        return samples
    thr = 10 ** (SILENCE_DB / 20.0)
    win = max(1, int(sr * 0.01))
    n = samples.size // win
    if n == 0:
        return samples
    env = np.abs(samples[: n * win]).reshape(n, win).max(axis=1)
    loud = np.nonzero(env > thr)[0]
    if loud.size == 0:
        return samples[:0]
    pad = int(sr * TRIM_PAD)
    a = max(0, loud[0] * win - pad)
    b = min(samples.size, (loud[-1] + 1) * win + pad)
    return samples[a:b]


def _write_atomic(path: Path, samples: np.ndarray, sr: int, subtype: str = "FLOAT") -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.wav")
    sf.write(str(tmp), samples, sr, subtype=subtype)
    os.replace(tmp, path)  # atomic: concurrent processes never see a half-written cache file


def synth(spoken: str, voice: str = "af_heart", speed: float = 1.1) -> Path:
    """Synthesise already-speakable text into the cache (no-op on hit). Returns the WAV path."""
    path = wav_path(spoken, voice, speed)
    if path.exists():
        return path
    k = _get_kokoro()
    try:
        with _synth_lock:
            samples, sr = k.create(spoken, voice=voice, speed=float(speed), lang="en-us")
    except (KeyError, ValueError, AssertionError) as e:
        raise ToolError(f"TTS failed for {spoken[:60]!r}: {e}",
                        f"check voice {voice!r} exists (e.g. af_heart) and speed is 0.5-2.0") from e
    samples = _trim(np.asarray(samples, dtype=np.float32), sr)
    if samples.size == 0:
        raise ToolError(f"TTS produced silence for {spoken[:60]!r}", "check the line text is not empty")
    _write_atomic(path, samples, sr)
    return path


def tts(lines: list[dict], voice: str = "af_heart", speed: float = 1.1, lexicon: dict | None = None,
        apply_lexicon: bool = True) -> dict:
    """Synthesise lines [{id, text}] -> {"durations": {id: sec}, "cached": [ids already on disk]}.

    voice "none" (engine none) returns word-rate estimates and synthesises nothing."""
    durations: dict[str, float] = {}
    cached: list[str] = []
    for ln in lines:
        lid, text = ln["id"], ln["text"]
        spoken = speakable(text, lexicon) if apply_lexicon else text
        if not spoken.strip():
            raise ToolError(f"line {lid!r} has no speakable text", "give the line some words")
        if voice in ("none", None, ""):
            durations[lid] = estimate(spoken, speed)
            continue
        p = wav_path(spoken, voice, speed)
        if p.exists():
            cached.append(lid)
        else:
            synth(spoken, voice, speed)
        durations[lid] = round(line_duration(p), 2)
    return {"durations": durations, "cached": cached}


# ---------------------------------------------------------------------------- mixing

def narration_track(placements: list[tuple[Path, float]], total: float, out: Path, sr: int = SR) -> Path:
    """Mix [(wav, start_sec)] into one mono WAV of exactly round(total*sr) samples, peak-normalised to 0.89."""
    import soundfile as sf
    n = int(round(total * sr))
    mix = np.zeros(n, dtype=np.float32)
    for wav, start in placements:
        wav = Path(wav)
        if not wav.exists():
            raise ToolError(f"narration WAV missing: {wav}", "re-run tts / plan to regenerate the cache")
        data, wsr = sf.read(str(wav), dtype="float32", always_2d=True)
        data = data.mean(axis=1)
        if wsr != sr:
            idx = np.arange(0, data.size, wsr / sr)
            data = np.interp(idx, np.arange(data.size), data).astype(np.float32)
        s = int(round(max(0.0, float(start)) * sr))
        if s >= n:
            continue
        seg = data[: n - s]
        mix[s: s + seg.size] += seg
    peak = float(np.abs(mix).max()) if n else 0.0
    if peak > 0:
        mix *= PEAK / peak
    out = Path(out)
    _write_atomic(out, mix, sr, subtype="PCM_16")
    return out
