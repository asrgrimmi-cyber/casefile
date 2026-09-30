"""Environment check: ffmpeg, tesseract, fonts, Kokoro models (downloads models if missing)."""
from __future__ import annotations

import subprocess
import urllib.request

from . import config


def _ver(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).splitlines()[0].strip()
    except Exception:
        return None


def doctor(download: bool = True) -> dict:
    """Return {ok, checks:{name:{ok, detail}}}. Downloads Kokoro files when missing and download=True."""
    checks: dict[str, dict] = {}
    for tool, arg in (("ffmpeg", "-version"), ("ffprobe", "-version"), ("tesseract", "--version")):
        p = config.tool_path(tool)
        v = _ver([p, arg]) if p else None
        checks[tool] = {"ok": bool(v), "detail": v or f"{tool} not found"}
    from .fonts import FILES
    missing = [f for f in FILES.values() if not (config.FONTS_DIR / f).exists()]
    checks["fonts"] = {"ok": not missing, "detail": "missing: " + ", ".join(missing) if missing else str(config.FONTS_DIR)}
    md = config.models_dir()
    for name, url in config.KOKORO_URLS.items():
        f = md / name
        if name == "kokoro-v1.0.int8.onnx":
            continue  # optional fallback model; fp32 is the default (faster on x86 CPUs)
        if not f.exists() and download:
            try:
                urllib.request.urlretrieve(url, f)
            except Exception as e:  # pragma: no cover - network
                checks[name] = {"ok": False, "detail": f"download failed: {e}"}
                continue
        checks[name] = {"ok": f.exists(), "detail": str(f) if f.exists() else "missing"}
    for mod in ("pytesseract", "kokoro_onnx", "soundfile", "mcp"):
        try:
            __import__(mod)
            checks[mod] = {"ok": True, "detail": "import ok"}
        except Exception as e:
            checks[mod] = {"ok": False, "detail": str(e)[:120]}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}
