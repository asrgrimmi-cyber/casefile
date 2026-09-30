"""Job dispatch: blob-reference substitution, the kind -> handler table, and the single runner thread.

Blob references inside `args` (any depth):
- the string "blob:<sha256>" or "blob:<sha256>.<ext>"  -> local blob path (with that extension alias)
- a dict {"blob": "<sha256>", "ext": ".mp4"}            -> local blob path
- for render specs, `spec.source` may also be a bare 64-hex sha256 (treated as a video, alias .mp4)
Outputs go to <CASEFILE_HOME>/outputs/<job_id>/ and are listed in result["files"] (download via /files).
Every result gets result["resources"] = {sec, peak_rss_mb, peak_vram_mb, vram_baseline_mb, vram_delta_mb}.
"""
from __future__ import annotations

import gc
import json
import logging
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..errors import ToolError
from .blobs import SHA_RE, BlobError, BlobStore
from .queue import JobQueue

log = logging.getLogger("casefile.worker")


@dataclass
class JobContext:
    job_id: str
    out_dir: Path
    blobs: BlobStore
    home: Path
    queue: JobQueue | None = None

    def progress(self, value: float, message: str | None = None) -> None:
        if self.queue is not None:
            self.queue.progress(self.job_id, value, message)


Handler = Callable[[dict, JobContext], dict]


# ------------------------------------------------------------------ blob substitution
def _blob_ref(s: str) -> tuple[str, str | None] | None:
    if not s.startswith("blob:"):
        return None
    ref = s[5:]
    sha, dot, ext = ref.partition(".")
    return (sha.lower(), f".{ext}" if dot else None) if SHA_RE.match(sha.lower()) else None


def substitute_blobs(obj: Any, blobs: BlobStore) -> Any:
    """Replace blob references (see module doc) with local file paths (str)."""
    if isinstance(obj, str):
        ref = _blob_ref(obj)
        return str(blobs.local_path(*ref)) if ref else obj
    if isinstance(obj, list):
        return [substitute_blobs(v, blobs) for v in obj]
    if isinstance(obj, dict):
        if set(obj) <= {"blob", "ext"} and isinstance(obj.get("blob"), str) and SHA_RE.match(obj["blob"].lower()):
            return str(blobs.local_path(obj["blob"].lower(), obj.get("ext")))
        return {k: substitute_blobs(v, blobs) for k, v in obj.items()}
    return obj


def _video_ref(v: Any, blobs: BlobStore, ext: str = ".mp4") -> Any:
    if isinstance(v, str) and SHA_RE.match(v.lower()):
        return str(blobs.local_path(v.lower(), ext))
    return v


# ------------------------------------------------------------------ handlers
def _require(args: dict, key: str) -> Any:
    if key not in args:
        raise ToolError(f"missing args.{key}", f"POST /jobs with args.{key}")
    return args[key]


def h_analyze(args: dict, ctx: JobContext) -> dict:
    try:
        from .. import timeline
    except ImportError:
        raise ToolError("timeline module not installed", "update casefile on the worker (casefile/timeline.py)")
    video = _video_ref(_require(args, "video"), ctx.blobs)
    kw = {k: args[k] for k in ("focus", "speech", "ocr") if k in args}
    return timeline.analyze(video, **kw)


def h_render(args: dict, ctx: JobContext) -> dict:
    from .. import render as R
    spec = args.get("spec", args)
    if isinstance(spec, dict):
        spec = dict(spec)
        if "source" in spec:
            spec["source"] = _video_ref(spec["source"], ctx.blobs)
        name = Path(str((spec.get("output") or {}).get("path") or "edit.mp4")).name or "edit.mp4"
    else:
        name = "edit.mp4"
    out = ctx.out_dir / name
    ctx.progress(0.05, "planning + rendering")
    res = R.render(spec, preview=bool(args.get("preview", False)), out=out, jobs=args.get("jobs"),
                   encoder=args.get("encoder", "auto"))
    return res


def h_doc(args: dict, ctx: JobContext) -> dict:
    try:
        from .. import docs
    except ImportError:
        raise ToolError("docs module not installed", "update casefile on the worker (casefile/docs.py)")
    tl = _require(args, "timeline")
    if isinstance(tl, str) and SHA_RE.match(tl.lower()):  # sha of the analysed VIDEO -> its timeline on this host
        cand = ctx.home / "timelines" / tl.lower() / "timeline.json"
        if not cand.exists():
            raise ToolError(f"no timeline for video {tl}", "run an analyze job for that video first")
        tl = str(cand)
    kw: dict[str, Any] = {"out_dir": ctx.out_dir}
    if "formats" in args:
        kw["formats"] = args["formats"]
    return docs.make_doc(tl, _require(args, "doc"), **kw)


def h_tts(args: dict, ctx: JobContext) -> dict:
    from .. import audio
    lines = _require(args, "lines")
    voice, speed, lex = args.get("voice", "af_heart"), float(args.get("speed", 1.1)), args.get("lexicon")
    t0 = time.perf_counter()
    res = audio.tts(lines, voice=voice, speed=speed, lexicon=lex)
    res["synth_sec"] = round(time.perf_counter() - t0, 2)
    if voice not in ("none", None, ""):
        for ln in lines:
            src = audio.wav_path(audio.speakable(ln["text"], lex), voice, speed)
            if src.exists():
                shutil.copyfile(src, ctx.out_dir / f"{_safe(ln['id'])}.wav")
    return res


def h_ocr(args: dict, ctx: JobContext) -> dict:
    """Extra (benchmark/diagnostic): OCR an image blob -> {backend, sec, lines|words}."""
    from PIL import Image

    from .. import ocr_backend
    img = Image.open(_video_ref(_require(args, "image"), ctx.blobs, ".png"))
    backend = ocr_backend.backend_name(args.get("backend", "auto"))
    level = args.get("level", "line")
    t0 = time.perf_counter()
    items = ocr_backend.ocr(img, args.get("region"), level=level, backend=backend)
    return {"backend": backend, "sec": round(time.perf_counter() - t0, 2), "size": list(img.size), "n": len(items),
            "items": items}


def h_stt(args: dict, ctx: JobContext) -> dict:
    """Extra: faster-whisper transcription of an audio/video blob."""
    from .. import stt
    path = _video_ref(_require(args, "audio"), ctx.blobs, args.get("ext", ".wav"))
    t0 = time.perf_counter()
    segs = stt.transcribe(path, model=args.get("model", "small"), device=args.get("device", "auto"),
                          language=args.get("language"))
    return {"segments": segs, "sec": round(time.perf_counter() - t0, 2), "model": args.get("model", "small"),
            "device": stt._device(args.get("device", "auto"))}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))[:80] or "out"


HANDLERS: dict[str, Handler] = {"analyze": h_analyze, "render": h_render, "doc": h_doc, "tts": h_tts,
                                "ocr": h_ocr, "stt": h_stt}


# ------------------------------------------------------------------ model hygiene
def release_models() -> None:
    """Free every lazily loaded model (OCR sessions, whisper, Kokoro) + caches so the next job starts clean."""
    try:
        from .. import ocr_backend
        ocr_backend.unload()
    except Exception:
        pass
    try:
        from .. import stt
        stt.unload()
    except Exception:
        pass
    try:
        from .. import audio
        with audio._kokoro_lock:
            audio._kokoro = None
    except Exception:
        pass
    try:
        from .. import render
        render._RT.clear()
        render._WORKER_PLAN.clear()
    except Exception:
        pass
    gc.collect()


# ------------------------------------------------------------------ result post-processing
def _collect_files(obj: Any, out_dir: Path, found: set[str]) -> Any:
    """Copy result files living outside out_dir into it (small ones) and list all output file names."""
    if isinstance(obj, dict):
        return {k: _collect_files(v, out_dir, found) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_collect_files(v, out_dir, found) for v in obj]
    if isinstance(obj, str) and len(obj) < 1024 and ("/" in obj or "\\" in obj):
        try:
            p = Path(obj)
            if p.is_file():
                rp, od = p.resolve(), out_dir.resolve()
                if not rp.is_relative_to(od) and rp.stat().st_size < 512 * 2**20:
                    dst = od / _safe(p.name)
                    if not dst.exists():
                        shutil.copyfile(rp, dst)
                    rp = dst
                if rp.is_relative_to(od):
                    found.add(rp.relative_to(od).as_posix())
        except (OSError, ValueError):
            pass
    return obj


# ------------------------------------------------------------------ runner
class Runner:
    """Single background thread executing queued jobs one at a time."""

    def __init__(self, queue: JobQueue, blobs: BlobStore, home: Path, handlers: dict[str, Handler] | None = None,
                 release: Callable[[], None] | None = release_models, monitor: bool = True):
        self.queue, self.blobs, self.home = queue, blobs, Path(home)
        self.handlers = handlers if handlers is not None else HANDLERS
        self.release = release
        self.monitor = monitor
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def out_dir(self, job_id: str) -> Path:
        return self.home / "outputs" / job_id

    def start(self) -> None:
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, name="casefile-job-runner", daemon=True)
        self._t.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        with self.queue.changed:
            self.queue.changed.notify_all()
        if self._t:
            self._t.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.queue.claim()
            if job is None:
                with self.queue.changed:
                    self.queue.changed.wait(1.0)
                continue
            self.run_one(job)

    def run_one(self, job: dict) -> None:
        jid, kind = job["id"], job["kind"]
        od = self.out_dir(jid)
        od.mkdir(parents=True, exist_ok=True)
        ctx = JobContext(jid, od, self.blobs, self.home, self.queue)
        handler = self.handlers.get(kind)
        t0 = time.perf_counter()
        mon = None
        try:
            if handler is None:
                raise ToolError(f"unknown job kind {kind!r}", f"use one of {', '.join(self.handlers)}")
            args = substitute_blobs(job["args"], self.blobs)
            if self.monitor:
                from ..gpu import PeakMonitor
                mon = PeakMonitor()
                with mon:
                    result = handler(args, ctx)
            else:
                result = handler(args, ctx)
            if not isinstance(result, dict):
                result = {"value": result}
            found: set[str] = set()
            result = _collect_files(result, od, found)
            found |= {p.relative_to(od).as_posix() for p in od.rglob("*") if p.is_file()}
            result["files"] = sorted(found)
            result["resources"] = {"sec": round(time.perf_counter() - t0, 2), **(mon.stats() if mon else {})}
            json.dumps(result, default=str)
            self.queue.finish(jid, result)
            log.info("job %s %s done in %.1fs %s", jid, kind, time.perf_counter() - t0, result["resources"])
        except (ToolError, BlobError) as e:
            self.queue.fail(jid, e.to_dict(), self._res(t0, mon))
            log.warning("job %s %s failed: %s", jid, kind, e)
        except Exception as e:  # handler crash: keep the worker alive
            tb = traceback.format_exc(limit=6)
            self.queue.fail(jid, {"error": f"{type(e).__name__}: {e}", "hint": "see worker log", "trace": tb[-1500:]},
                            self._res(t0, mon))
            log.exception("job %s %s crashed", jid, kind)
        finally:
            if self.release:
                try:
                    self.release()
                except Exception:
                    log.exception("release_models failed")

    @staticmethod
    def _res(t0: float, mon: Any) -> dict:
        return {"resources": {"sec": round(time.perf_counter() - t0, 2), **(mon.stats() if mon else {})}}
