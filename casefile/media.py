"""Video I/O: ffprobe metadata and a per-video JPEG frame store with LRU cache.

Frames are extracted ONCE per video (ffmpeg -> JPEG q=2) into cache_dir('frames')/<key>/NNNNNN.jpg
(0-based frame index), with index.json holding metadata. Nothing ever loads the whole video in RAM.
Detection (probe, bad frames, layout changes) lives in casefile/detect.py.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from . import config
from .errors import ToolError


def ffmpeg() -> str:
    p = config.tool_path("ffmpeg")
    if not p:
        raise ToolError("ffmpeg not found", "install ffmpeg 6+ and put it on PATH; run `casefile doctor`")
    return p


def ffprobe() -> str:
    p = config.tool_path("ffprobe")
    if not p:
        raise ToolError("ffprobe not found", "install ffmpeg 6+ (includes ffprobe); run `casefile doctor`")
    return p


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a subprocess, raising ToolError with the stderr tail on failure."""
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        err = (r.stderr or b"")[-600:]
        raise ToolError(f"{Path(cmd[0]).name} failed: {err.decode(errors='replace').strip()}", "check the input file")
    return r


def video_info(path: str | Path) -> dict:
    """{duration, fps, width, height, n_frames, has_audio} via ffprobe."""
    r = run([ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    j = json.loads(r.stdout)
    vs = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    if vs is None:
        raise ToolError(f"no video stream in {path}", "pass a video file")
    num, den = (vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    dur = float(vs.get("duration") or j["format"].get("duration") or 0)
    nf = int(vs.get("nb_frames") or 0) or int(round(dur * fps))
    return {"duration": round(dur, 3), "fps": round(fps, 3), "width": int(vs["width"]), "height": int(vs["height"]),
            "n_frames": nf, "has_audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def video_key(path: str | Path) -> str:
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


class FrameStore:
    """Random access to decoded frames of one video by 0-based index or time.

    fs = FrameStore(path); fs.fps, fs.size, fs.n_frames; fs.get(n) -> RGB PIL.Image; fs.at(t) -> image.
    fs.index(t) -> frame index for time t (round(t*fps), clamped). A remap dict (bad -> good) can be set
    via fs.remap = {...}; get() then follows it (used by the renderer for bad-frame repair).
    """

    _registry: dict[str, "FrameStore"] = {}

    def __new__(cls, path: str | Path, cache_size: int = 48):
        key = video_key(path)
        if key in cls._registry:
            return cls._registry[key]
        obj = super().__new__(cls)
        cls._registry[key] = obj
        obj._init(Path(path).resolve(), key, cache_size)
        return obj

    def __init__(self, path: str | Path, cache_size: int = 48):  # noqa: D107 - handled in _init
        pass

    def _init(self, path: Path, key: str, cache_size: int) -> None:
        self.path = path
        self.key = key
        self.dir = config.cache_dir("frames") / key
        self._lru: OrderedDict[int, Image.Image] = OrderedDict()
        self._cap = cache_size
        self.remap: dict[int, int] = {}
        idx = self.dir / "index.json"
        if idx.exists():
            meta = json.loads(idx.read_text())
        else:
            meta = self._extract()
        self.meta = meta
        self.fps: float = meta["fps"]
        self.size: tuple[int, int] = (meta["width"], meta["height"])
        self.n_frames: int = meta["n_frames"]
        self.duration: float = meta["duration"]
        self.has_audio: bool = meta["has_audio"]

    def _extract(self) -> dict:
        info = video_info(self.path)
        self.dir.mkdir(parents=True, exist_ok=True)
        # -vsync passthrough keeps one JPEG per decoded frame; start_number 0 -> 0-based names
        run([ffmpeg(), "-v", "error", "-y", "-i", str(self.path), "-map", "0:v:0", "-fps_mode", "passthrough",
             "-q:v", "2", "-start_number", "0", str(self.dir / "%06d.jpg")])
        n = len(list(self.dir.glob("*.jpg")))
        info["n_frames"] = n
        (self.dir / "index.json").write_text(json.dumps(info))
        return info

    def index(self, t: float) -> int:
        return max(0, min(self.n_frames - 1, int(round(t * self.fps))))

    def frame_path(self, n: int) -> Path:
        return self.dir / f"{n:06d}.jpg"

    def get(self, n: int, *, raw: bool = False) -> Image.Image:
        """Frame n as RGB. Follows self.remap unless raw=True."""
        n = max(0, min(self.n_frames - 1, int(n)))
        if not raw:
            n = self.remap.get(n, n)
        im = self._lru.get(n)
        if im is not None:
            self._lru.move_to_end(n)
            return im
        im = Image.open(self.frame_path(n)).convert("RGB")
        im.load()
        self._lru[n] = im
        if len(self._lru) > self._cap:
            self._lru.popitem(last=False)
        return im

    def at(self, t: float, *, raw: bool = False) -> Image.Image:
        return self.get(self.index(t), raw=raw)

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()
