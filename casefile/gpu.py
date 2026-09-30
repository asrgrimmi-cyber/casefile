"""GPU / host capability probes and a per-job peak RAM + VRAM monitor.

- `status()` -> {gpu:{name, vram_total, vram_free, vram_used, driver}|None, ram_total, ram_free, nvenc, cuda}
  (MiB). Uses `nvidia-smi` (absent on CPU-only hosts -> gpu None).
- `nvenc_available()` -> bool: `h264_nvenc` listed by `ffmpeg -encoders` AND a 1-frame trial encode succeeds
  (Windows ffmpeg builds list nvenc even without an NVIDIA GPU). Cached per process.
- `PeakMonitor` samples this process (+ children: ffmpeg, spawn render workers) RSS and the GPU memory in use.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from functools import lru_cache
from typing import Any

from . import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: float = 10) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode(errors="replace")


def nvidia_smi() -> str | None:
    return config.tool_path("nvidia-smi")


def gpu_info() -> dict | None:
    """First NVIDIA GPU: {name, vram_total, vram_free, vram_used, driver} in MiB, or None."""
    exe = nvidia_smi()
    if not exe:
        return None
    out = _run([exe, "--query-gpu=name,memory.total,memory.free,memory.used,driver_version",
                "--format=csv,noheader,nounits"])
    if not out or not out.strip():
        return None
    parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
    try:
        return {"name": parts[0], "vram_total": int(float(parts[1])), "vram_free": int(float(parts[2])),
                "vram_used": int(float(parts[3])), "driver": parts[4] if len(parts) > 4 else None}
    except (IndexError, ValueError):
        return None


def vram_used() -> int | None:
    """GPU memory in use (MiB, whole device) or None without nvidia-smi."""
    exe = nvidia_smi()
    if not exe:
        return None
    out = _run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], timeout=5)
    try:
        return int(float(out.strip().splitlines()[0])) if out else None
    except (ValueError, IndexError):
        return None


def ram_info() -> dict:
    """{ram_total, ram_free} MiB (free = available). psutil when installed, else /proc/meminfo, else None."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"ram_total": vm.total // 2**20, "ram_free": vm.available // 2**20}
    except ImportError:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) // 1024
        return {"ram_total": info.get("MemTotal"), "ram_free": info.get("MemAvailable")}
    except OSError:
        return {"ram_total": None, "ram_free": None}


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True when ffmpeg lists h264_nvenc and a tiny trial encode works (cached)."""
    if os.environ.get("CASEFILE_NO_NVENC"):
        return False
    ff = config.tool_path("ffmpeg")
    if not ff:
        return False
    enc = _run([ff, "-hide_banner", "-encoders"])
    if not enc or "h264_nvenc" not in enc:
        return False
    trial = _run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30", "-frames:v", "2",
                  "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
    return trial is not None


@lru_cache(maxsize=1)
def cuda_onnx() -> bool:
    """onnxruntime's CUDA execution provider is listed AND its shared library loads (cuBLAS/cuDNN found).

    get_available_providers() lists CUDA even when libcublasLt/libcudnn are missing (ORT then silently falls
    back to CPU), so the provider library is dlopen'ed to be sure. No session / VRAM is used."""
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return False
    import ctypes
    from pathlib import Path
    capi = Path(ort.__file__).parent / "capi"
    libs = sorted(capi.glob("libonnxruntime_providers_cuda.so")) + sorted(capi.glob("onnxruntime_providers_cuda.dll"))
    if not libs:
        return True  # unknown layout: trust the provider list
    try:
        ctypes.CDLL(str(libs[0]))
    except OSError:
        return False
    return True


@lru_cache(maxsize=1)
def cuda_ctranslate2() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def status() -> dict:
    """Host capabilities: GPU, RAM, nvenc, CUDA for onnxruntime / ctranslate2."""
    return {"gpu": gpu_info(), **ram_info(), "nvenc": nvenc_available(), "cuda_onnx": cuda_onnx(),
            "cuda_ctranslate2": cuda_ctranslate2()}


def _proc_tree_rss() -> int | None:
    """RSS (bytes) of this process + all descendants."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            me = psutil.Process()
            total = me.memory_info().rss
            for c in me.children(recursive=True):
                try:
                    total += c.memory_info().rss
                except psutil.Error:
                    pass
            return total
        except psutil.Error:
            return None
    try:  # Linux without psutil: self only
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


class PeakMonitor:
    """Context manager sampling peak RSS (process tree) and GPU memory in use while a job runs.

    `stats()` -> {peak_rss_mb, peak_vram_mb, vram_baseline_mb, vram_delta_mb}."""

    def __init__(self, interval: float = 0.25, vram: bool = True):
        self.interval = interval
        self.do_vram = vram and nvidia_smi() is not None
        self.peak_rss = 0
        self.peak_vram: int | None = None
        self.base_vram: int | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _sample(self) -> None:
        r = _proc_tree_rss()
        if r:
            self.peak_rss = max(self.peak_rss, r)
        if self.do_vram:
            v = vram_used()
            if v is not None:
                self.peak_vram = v if self.peak_vram is None else max(self.peak_vram, v)

    def _loop(self) -> None:
        last_v = 0.0
        while not self._stop.is_set():
            r = _proc_tree_rss()
            if r:
                self.peak_rss = max(self.peak_rss, r)
            now = time.monotonic()
            if self.do_vram and now - last_v >= max(self.interval, 0.5):
                last_v = now
                v = vram_used()
                if v is not None:
                    self.peak_vram = v if self.peak_vram is None else max(self.peak_vram, v)
            self._stop.wait(self.interval)

    def __enter__(self) -> "PeakMonitor":
        if self.do_vram:
            self.base_vram = vram_used()
        self._sample()
        self._t = threading.Thread(target=self._loop, name="peak-monitor", daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
        self._sample()

    def stats(self) -> dict:
        d = {"peak_rss_mb": round(self.peak_rss / 2**20) if self.peak_rss else None,
             "peak_vram_mb": self.peak_vram, "vram_baseline_mb": self.base_vram}
        d["vram_delta_mb"] = (self.peak_vram - self.base_vram
                              if self.peak_vram is not None and self.base_vram is not None else None)
        return d
