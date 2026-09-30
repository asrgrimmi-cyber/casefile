"""Phase 5 renderer: Plan -> MP4 (H.264 + AAC), SRT and thumbnails.

Pipeline
  1. plan = planner.plan(spec, synth=True)   (or a ready Plan)
  2. frames 0..ceil(total*fps)-1 are split into `jobs` contiguous chunks; each chunk is rendered by a
     spawn-safe worker process (`_render_segment`) that pipes rgb24 frames into ffmpeg: libx264 CRF 18, or
     h264_nvenc (VBR CQ 19) when `encoder="auto"` finds a working NVENC (see casefile.gpu.nvenc_available).
     Workers get a picklable payload: the pickled Plan when it pickles, otherwise the spec JSON which the
     worker re-plans (TTS/OCR caches make that cheap and deterministic).
  3. concat demuxer (-c copy) -> video.mp4
  4. narration WAV (audio.narration_track) or anullsrc silence -> AAC 160k with loudnorm, trimmed/padded to
     the exact duration, -movflags +faststart.
  5. SRT from absolute line times, 4 half-size PNG thumbnails at 20/40/60/80 %.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import config
from .draw import (
    Ctx,
    apply_overlay_plan,
    bar_strip,
    bar_top,
    card_base,
    card_ops,
    ease,
    hex_rgb,
    lerp,
    lock_aspect,
    logo_path,
    overlay_plan,
    place_source,
    replay,
)
from .errors import ToolError, check_path
from .media import FrameStore, ffmpeg, run

PREVIEW_SIZE = (960, 540)
PREVIEW_FPS = 15
MIN_CHUNK_FRAMES = 30
ENCODERS = ("auto", "libx264", "h264_nvenc")
NVENC_MAX_SESSIONS = int(os.environ.get("CASEFILE_NVENC_SESSIONS", "4"))  # consumer GeForce session cap


# ------------------------------------------------------------------ geometry
def bar_height(H: int) -> int:
    """Caption bar height for output height H (111 px at 1080p)."""
    return round(config.BAR_H * H / 1080)


def _size_fps(plan: Any, preview: bool) -> tuple[tuple[int, int], int]:
    if preview:
        return PREVIEW_SIZE, PREVIEW_FPS
    W, H = plan.size
    return (int(W), int(H)), int(plan.fps)


def n_frames(total: float, fps: int) -> int:
    """Exact output frame count: ceil(total * fps) (tolerant to float noise)."""
    return max(1, int(math.ceil(total * fps - 1e-6)))


# ------------------------------------------------------------------ per-plan runtime cache
class _Runtime:
    """Things derived from a plan that are reused for every frame (frame store, brand colours, logo)."""

    def __init__(self, plan: Any):
        spec = plan.spec
        brand = getattr(spec, "brand", None)
        self.accent = hex_rgb(getattr(brand, "accent", None), config.ACCENT)
        self.ink = hex_rgb(getattr(brand, "ink", None), config.INK)
        self.logo = logo_path(getattr(brand, "logo", None))
        self.store: FrameStore | None = None
        self.src_ids: dict[int, bytes] = {}                      # frame index -> JPEG digest (dedups repeats)
        self.scaled: list[tuple[tuple, Image.Image]] = []        # tiny LRU: (src id, cam, W, CH) -> scaled source
        self.content: list[tuple[Any, Image.Image, list]] = []   # tiny LRU: content key -> (image, [bytes])
        self.bars: dict[tuple, bytes] = {}                       # (W, H, chapter, caption, alpha8) -> bar rows
        self.strips: dict[tuple, bytes] = {}                     # (W, H, fill_x) -> progress strip rows
        self.last_frame: tuple[Any, list[bytes]] | None = None   # (frame key, parts) of the previous frame
        if getattr(plan, "source", None):
            self.store = FrameStore(plan.source)
            self.store.remap = dict(getattr(plan, "remap", {}) or {})
        ss = getattr(plan, "src_size", None)
        self.src_size = tuple(ss) if ss else (self.store.size if self.store else None)


_RT: dict[int, tuple[Any, _Runtime]] = {}


def _rt(plan: Any) -> _Runtime:
    """Runtime for plan, cached by identity (kept off the Plan object so the Plan stays picklable)."""
    hit = _RT.get(id(plan))
    if hit is not None and hit[0] is plan:
        return hit[1]
    rt = _Runtime(plan)
    if len(_RT) > 8:
        _RT.clear()
    _RT[id(plan)] = (plan, rt)
    return rt


def _lru_get(lst: list, key: Any) -> Any:
    """Linear-probe tiny LRU (keys may hold unhashable lists; == comparison)."""
    for i, item in enumerate(lst):
        if item[0] == key:
            if i:
                lst.insert(0, lst.pop(i))
            return item
    return None


def _lru_put(lst: list, item: tuple, cap: int = 3) -> None:
    lst.insert(0, item)
    del lst[cap:]


def _src_id(rt: _Runtime, n: int) -> Any:
    """Content identity of source frame n: digest of its extracted JPEG (screen recordings repeat frames)."""
    sid = rt.src_ids.get(n)
    if sid is None:
        assert rt.store is not None
        try:
            sid = hashlib.blake2b(rt.store.frame_path(n).read_bytes(), digest_size=16).digest()
        except OSError:
            sid = n
        rt.src_ids[n] = sid
    return sid


# ------------------------------------------------------------------ frame composition
def camera_at(ps: Any, t: float, src_size) -> tuple[float, float, float, float]:
    """Eased camera rect (x, y, w, h) at local time t from PShot.camera keyframes."""
    full = (0.0, 0.0, float(src_size[0]), float(src_size[1])) if src_size else (0.0, 0.0, 1.0, 1.0)
    kf = [(float(a), tuple(float(v) for v in r)) for a, r in (getattr(ps, "camera", None) or [])]
    if not kf:
        return full
    if t <= kf[0][0]:
        return kf[0][1]
    for (ta, ra), (tb, rb) in zip(kf, kf[1:]):
        if t <= tb:
            u = ease((t - ta) / max(1e-6, tb - ta))
            return tuple(lerp(x, y, u) for x, y in zip(ra, rb))  # type: ignore[return-value]
    return kf[-1][1]


def _content(plan: Any, rt: _Runtime, ps: Any, t: float, ctx: Ctx) -> tuple[Image.Image, list]:
    """(W x CH content image, [cached bytes or empty]) of shot ps at local time t.

    Everything that affects the pixels is reduced to a comparable key first (source frame digest + camera, or
    the recorded card ops, plus the recorded overlay ops), so holds, freeze frames, static cameras and settled
    overlays reuse the previous image instead of re-scaling/re-drawing it. Returned images must not be mutated."""
    shot = ps.shot
    ovs = list(getattr(ps, "overlays", None) or [])
    if getattr(shot, "card", None):
        ctx.cam = (0.0, 0.0, float(ctx.W), float(ctx.CH))
        ops = card_ops(ctx, shot, t)
        base_key: Any = ("card", ctx.W, ctx.CH, str(ctx.logo), ops)
    else:
        if rt.store is None:
            raise ToolError("shot has no source video", "set spec.source or make the shot a card")
        cam = lock_aspect(camera_at(ps, t, rt.src_size), ctx.W / ctx.CH, rt.src_size)
        ctx.cam = cam
        n = rt.store.index(ps.src_time(t))
        n = rt.store.remap.get(n, n)
        n = max(0, min(rt.store.n_frames - 1, int(n)))
        base_key = ("src", _src_id(rt, n), tuple(round(v, 3) for v in cam), ctx.W, ctx.CH)
    op = overlay_plan(ctx, ovs, t)
    key = (base_key, op)
    hit = _lru_get(rt.content, key)
    if hit is not None:
        return hit[1], hit[2]
    if base_key[0] == "card":
        img = card_base(ctx).copy()
        replay(ImageDraw.Draw(img, "RGBA"), ops)
    else:
        sc = _lru_get(rt.scaled, base_key)
        if sc is None:
            sc = (base_key, place_source(rt.store.get(n), cam, ctx.W, ctx.CH))
            _lru_put(rt.scaled, sc, 2)
        img = sc[1].copy() if op is not None else sc[1]
    img = apply_overlay_plan(img, op)
    item = (key, img, [])
    _lru_put(rt.content, item, 3)
    return img, item[2]


def _bytes_of(img: Image.Image, cache: list) -> bytes:
    if not cache:
        cache.append(img.tobytes())
    return cache[0]


def caption_at(plan: Any, ps: Any, t: float) -> tuple[str | None, float]:
    """(caption text, alpha) shown at local time t of shot ps (timing: -0.15 s .. +0.5 s)."""
    best = None
    for k, (a, b) in (getattr(ps, "line_times", None) or {}).items():
        if a - 0.15 <= t <= b + 0.5:
            best = (k, a, b)
    if best is None:
        return None, 0.0
    k, a, b = best
    al = min(ease((t - a + 0.15) / 0.25), 1 - ease((t - b - 0.2) / 0.3))
    line = (plan.lines or {}).get(k) or {}
    return line.get("caption"), al


def _active(plan: Any, T: float) -> list[Any]:
    act = [ps for ps in plan.shots if ps.T0 <= T < ps.T0 + ps.dur + 1e-9]
    return act or [plan.shots[-1]]


def frame_parts(plan: Any, T: float, *, size: tuple[int, int] | None = None) -> list[bytes]:
    """Output frame at absolute time T as raw rgb24 byte chunks (concatenated = W*H*3 bytes, row-major).

    The frame is content rows [0, CH) + caption-bar rows [CH, H-ph) + progress strip rows [H-ph, H); each part is
    cached independently (only the 6 px strip changes on most frames) and whole frames are reused when nothing
    changed. Pixel-identical to compositing onto one canvas."""
    W, H = (int(v) for v in (size or plan.size))
    CH = H - bar_height(H)
    rt = _rt(plan)
    ctx = Ctx(W=W, H=H, CH=CH, accent=rt.accent, ink=rt.ink, logo=rt.logo)
    total = float(plan.total)
    act = _active(plan, T)
    ps = act[-1]
    t = T - ps.T0
    spec = plan.spec
    # --- cheap scalar state first
    cap, cal = caption_at(plan, ps, t) if getattr(spec, "captions", True) else (None, 0.0)
    chapter = getattr(ps, "chapter", None)
    a8 = int(255 * cal) if cap and cal > 0 else -1
    bar_key = (W, H, chapter, cap if a8 >= 0 else None, a8)
    fill_x = int(W * min(1.0, T / total)) if total > 0 else None
    fi, fo = float(getattr(spec, "fade_in", 0.6)), float(getattr(spec, "fade_out", 0.8))
    g = 1.0
    if fi > 0:
        g = min(g, T / fi)
    if fo > 0:
        g = min(g, (total - T) / fo)
    # --- content (possibly a crossfade of two shots)
    img, cache = _content(plan, rt, ps, t, ctx)
    ckey: Any = rt.content[0][0]
    xf = float(getattr(ps, "xfade", 0) or 0)
    if len(act) > 1 and xf > 0 and t < xf:
        prev = act[-2]
        pimg, _ = _content(plan, rt, prev, T - prev.T0, ctx)
        pkey = rt.content[0][0]
        u = ease(t / xf)
        ckey = ("xf", pkey, ckey, u)
        hit = _lru_get(rt.content, ckey)
        if hit is None:
            hit = (ckey, Image.blend(pimg, img, u), [])
            _lru_put(rt.content, hit, 3)
        img, cache = hit[1], hit[2]
    fkey = (ckey, bar_key, fill_x, g if g < 1 else 1.0)
    if rt.last_frame is not None and rt.last_frame[0] == fkey:
        return rt.last_frame[1]
    bar = rt.bars.get(bar_key)
    if bar is None:
        if len(rt.bars) > 64:
            rt.bars.clear()
        bar = rt.bars[bar_key] = bar_top(ctx, chapter, cap, cal).tobytes()
    skey = (W, H, fill_x)
    strip = rt.strips.get(skey)
    if strip is None:
        if len(rt.strips) > 4096:
            rt.strips.clear()
        ticks = tuple(int(W * ct / total) for ct, _ in list(plan.chapters or [])[1:]) if total > 0 else ()
        strip = rt.strips[skey] = bar_strip(ctx, fill_x, ticks).tobytes()
    parts = [_bytes_of(img, cache), bar, strip]
    if g < 1:
        canvas = Image.frombytes("RGB", (W, H), b"".join(parts))
        parts = [Image.blend(Image.new("RGB", (W, H)), canvas, max(0.0, g)).tobytes()]
    rt.last_frame = (fkey, parts)
    return parts


def frame_at(plan: Any, T: float, *, size: tuple[int, int] | None = None) -> Image.Image:
    """Composite output frame at absolute time T (seconds) as an RGB PIL image of plan.size (or `size`)."""
    W, H = (int(v) for v in (size or plan.size))
    return Image.frombytes("RGB", (W, H), b"".join(frame_parts(plan, T, size=(W, H))))


# ------------------------------------------------------------------ SRT
def _srt_ts(x: float) -> str:
    ms = max(0, int(round(x * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_for(plan: Any) -> str:
    """SubRip text built from absolute line times (shot T0 + local line start/end) and line captions."""
    cues = []
    for ps in plan.shots:
        for k, (a, b) in (getattr(ps, "line_times", None) or {}).items():
            cap = ((plan.lines or {}).get(k) or {}).get("caption")
            if cap:
                cues.append((ps.T0 + a, min(ps.T0 + b, float(plan.total)), cap))
    cues.sort(key=lambda c: c[0])
    out = []
    for i, (a, b, cap) in enumerate(cues, 1):
        out.append(f"{i}\n{_srt_ts(a)} --> {_srt_ts(max(b, a + 0.1))}\n{cap}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ workers
_WORKER_PLAN: dict[bytes, Any] = {}


def _payload(plan: Any) -> tuple[str, bytes]:
    """Picklable description of a plan for spawn workers: pickled Plan, else spec JSON to re-plan."""
    try:
        blob = pickle.dumps(plan, protocol=pickle.HIGHEST_PROTOCOL)
        pickle.loads(blob)
        return ("pickle", blob)
    except Exception:
        spec = plan.spec
        js = spec.model_dump_json(by_alias=True) if hasattr(spec, "model_dump_json") else json.dumps(spec)
        return ("spec", js.encode())


def _plan_from_payload(kind: str, blob: bytes) -> Any:
    key = kind.encode() + blob[:64] + str(len(blob)).encode() + str(hash(blob)).encode()
    p = _WORKER_PLAN.get(key)
    if p is None:
        if kind == "pickle":
            p = pickle.loads(blob)
        else:
            from . import planner
            p = planner.plan(json.loads(blob.decode()), synth=True)
        _WORKER_PLAN.clear()
        _WORKER_PLAN[key] = p
    return p


def resolve_encoder(encoder: str | None = "auto") -> str:
    """'auto' -> 'h264_nvenc' when ffmpeg has a working NVENC, else 'libx264'. Explicit names pass through."""
    enc = (encoder or "auto").lower()
    if enc not in ENCODERS:
        raise ToolError(f"unknown encoder {encoder!r}", f"use one of {', '.join(ENCODERS)}")
    if enc == "auto":
        from .gpu import nvenc_available
        return "h264_nvenc" if nvenc_available() else "libx264"
    return enc


def _codec_args(encoder: str, preset: str, threads: int) -> list[str]:
    if encoder == "h264_nvenc":
        nv_preset = "p3" if preset == "veryfast" else "p5"
        return ["-c:v", "h264_nvenc", "-preset", nv_preset, "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(config.CRF), "-pix_fmt", "yuv420p",
            "-threads", str(threads)]


def _encode_cmd(W: int, H: int, fps: int, out: Path, preset: str, threads: int, encoder: str = "libx264") -> list[str]:
    return [ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
            "-i", "-", "-an", *_codec_args(encoder, preset, threads),
            "-r", str(fps), "-video_track_timescale", str(fps * 1000), str(out)]


def _pipe_frames(stdin: Any, plan: Any, W: int, H: int, fps: int, n0: int, n1: int) -> None:
    for n in range(n0, n1):
        for part in frame_parts(plan, n / fps, size=(W, H)):
            stdin.write(part)


def _render_segment(args: tuple) -> dict:
    """Worker: render frames [n0, n1) to an H.264 segment. Spawn-safe (module-level, picklable args)."""
    kind, blob, W, H, fps, n0, n1, out, preset, threads, *rest = args
    encoder = rest[0] if rest else "libx264"
    t0 = time.perf_counter()
    plan = _plan_from_payload(kind, blob)
    out = Path(out)
    errf = out.with_suffix(".log")
    with open(errf, "wb") as ef:
        p = subprocess.Popen(_encode_cmd(W, H, fps, out, preset, threads, encoder), stdin=subprocess.PIPE, stderr=ef)
        try:
            assert p.stdin is not None
            _pipe_frames(p.stdin, plan, W, H, fps, n0, n1)
            p.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            rc = p.wait()
    if rc != 0 or not out.exists():
        msg = errf.read_text(errors="replace")[-600:] if errf.exists() else ""
        raise ToolError(f"ffmpeg encode failed for frames {n0}-{n1}: {msg.strip()}", f"check ffmpeg ({encoder})")
    return {"path": str(out), "frames": n1 - n0, "sec": time.perf_counter() - t0}


def _chunks(N: int, jobs: int) -> list[tuple[int, int]]:
    k = max(1, min(jobs, N // MIN_CHUNK_FRAMES or 1))
    edges = [round(i * N / k) for i in range(k + 1)]
    return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


# ------------------------------------------------------------------ audio
def _placements(plan: Any) -> list[tuple[Path, float]]:
    out = []
    for ps in plan.shots:
        for k, (a, _b) in (getattr(ps, "line_times", None) or {}).items():
            wav = ((plan.lines or {}).get(k) or {}).get("wav")
            if wav and Path(wav).exists():
                out.append((Path(wav), float(ps.T0 + a)))
    return out


def _mix_fallback(placements: list[tuple[Path, float]], total: float, out: Path, sr: int = 24000) -> Path:
    import numpy as np
    import soundfile as sf
    buf = np.zeros(int(math.ceil(total * sr)) + 1, np.float32)
    for wav, st in placements:
        s, r = sf.read(str(wav), dtype="float32", always_2d=True)
        s = s.mean(axis=1)
        if r != sr:
            idx = np.arange(0, len(s), r / sr)
            s = np.interp(idx, np.arange(len(s)), s).astype(np.float32)
        i = int(round(st * sr))
        if i >= len(buf):
            continue
        n = min(len(s), len(buf) - i)
        buf[i:i + n] += s[:n]
    peak = float(np.abs(buf).max()) if len(buf) else 0.0
    if peak > 1e-6:
        buf = buf / peak * 0.89
    sf.write(str(out), buf, sr)
    return out


def _narration(plan: Any, total: float, out: Path) -> Path | None:
    voice = getattr(plan.spec, "voice", None)
    if getattr(voice, "engine", "kokoro") == "none":
        return None
    pl = _placements(plan)
    if not pl:
        return None
    try:
        from .audio import narration_track
    except ImportError:
        return _mix_fallback(pl, total, out)
    return Path(narration_track(pl, total, out))


def _mux(video: Path, wav: Path | None, total: float, out: Path) -> None:
    dur = f"{total:.6f}"
    cmd = [ffmpeg(), "-v", "error", "-y", "-i", str(video)]
    if wav is not None:
        cmd += ["-i", str(wav)]
        af = f"{config.LOUDNORM},aresample=48000,apad,atrim=0:{dur},asetpts=N/SR/TB"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        af = f"atrim=0:{dur},asetpts=N/SR/TB"
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", config.AAC_BITRATE,
            "-ar", "48000", "-movflags", "+faststart", str(out)]
    run(cmd)


# ------------------------------------------------------------------ public API
def _resolve_plan(spec_or_plan: Any) -> Any:
    if hasattr(spec_or_plan, "shots") and hasattr(spec_or_plan, "total") and hasattr(spec_or_plan, "spec"):
        return spec_or_plan
    try:
        from . import planner
    except ImportError as e:  # pragma: no cover - planner ships with the package
        raise ToolError(f"planner unavailable: {e}", "reinstall casefile") from e
    return planner.plan(spec_or_plan, synth=True)


def _out_path(plan: Any, out: str | Path | None, preview: bool) -> Path:
    if out is None:
        p = Path(getattr(getattr(plan.spec, "output", None), "path", None) or "out/edit.mp4")
        if preview:
            p = p.with_name(p.stem + "_preview" + (p.suffix or ".mp4"))
        out = p
    path = check_path(out, must_exist=False, write=True)
    if path.suffix.lower() != ".mp4":
        path = path.with_suffix(".mp4")
    return path


def render(spec_or_plan: Any, *, preview: bool = False, out: str | Path | None = None, jobs: int | None = None,
           encoder: str = "auto") -> dict:
    """Render a spec (dict / JSON path / JSON text) or a planner.Plan to MP4.

    encoder: "auto" (h264_nvenc when ffmpeg has a working NVENC, else libx264), "libx264" or "h264_nvenc".
    Returns {path, duration, frames, size_mb, srt, thumbs:[4 png], render_sec, fps_render, size:[W,H], fps, encoder}."""
    t_start = time.perf_counter()
    plan = _resolve_plan(spec_or_plan)
    if not getattr(plan, "shots", None):
        raise ToolError("spec has no shots", "add at least one shot")
    (W, H), fps = _size_fps(plan, preview)
    total = float(plan.total)
    N = n_frames(total, fps)
    duration = N / fps
    path = _out_path(plan, out, preview)
    rt = _rt(plan)  # extracts the source frames once, in the parent, before workers start
    del rt
    enc = resolve_encoder(encoder)
    jobs = max(1, int(jobs or os.cpu_count() or 1))
    if enc == "h264_nvenc":
        jobs = min(jobs, max(1, NVENC_MAX_SESSIONS))
    chunks = _chunks(N, jobs)
    preset = "veryfast" if preview else "medium"
    threads = max(1, (os.cpu_count() or 1) // len(chunks))
    tmp = Path(tempfile.mkdtemp(prefix="render_", dir=str(path.parent)))
    try:
        segs = [tmp / f"seg{i:03d}.mp4" for i in range(len(chunks))]
        if len(chunks) == 1:
            n0, n1 = chunks[0]
            _render_segment_local(plan, W, H, fps, n0, n1, segs[0], preset, threads, enc)
        else:
            kind, blob = _payload(plan)
            args = [(kind, blob, W, H, fps, a, b, str(s), preset, threads, enc) for (a, b), s in zip(chunks, segs)]
            with ProcessPoolExecutor(max_workers=len(chunks), mp_context=get_context("spawn")) as ex:
                list(ex.map(_render_segment, args))
        lst = tmp / "concat.txt"
        lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        video = tmp / "video.mp4"
        run([ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video)])
        wav = _narration(plan, duration, tmp / "narration.wav")
        _mux(video, wav, duration, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    srt_path = None
    if getattr(plan.spec, "srt", True):
        srt_path = path.with_suffix(".srt")
        srt_path.write_text(srt_for(plan), encoding="utf-8")
    thumbs = []
    for i, frac in enumerate((0.2, 0.4, 0.6, 0.8), 1):
        tp = path.with_name(f"{path.stem}_thumb{i}.png")
        n = min(N - 1, int(frac * N))
        frame_at(plan, n / fps, size=(W, H)).resize((W // 2, H // 2), Image.LANCZOS).save(tp)
        thumbs.append(str(tp))
    sec = time.perf_counter() - t_start
    return {"path": str(path), "duration": round(duration, 2), "frames": N,
            "size_mb": round(path.stat().st_size / 1e6, 2), "srt": str(srt_path) if srt_path else None,
            "thumbs": thumbs, "render_sec": round(sec, 2), "fps_render": round(N / sec, 1) if sec > 0 else None,
            "size": [W, H], "fps": fps, "encoder": enc, "warnings": list(getattr(plan, "warnings", []) or [])[:10]}


def _render_segment_local(plan: Any, W: int, H: int, fps: int, n0: int, n1: int, out: Path, preset: str,
                          threads: int, encoder: str = "libx264") -> None:
    """In-process variant of _render_segment (jobs=1 / tiny renders): no pickling needed."""
    errf = out.with_suffix(".log")
    with open(errf, "wb") as ef:
        p = subprocess.Popen(_encode_cmd(W, H, fps, out, preset, threads, encoder), stdin=subprocess.PIPE, stderr=ef)
        try:
            assert p.stdin is not None
            _pipe_frames(p.stdin, plan, W, H, fps, n0, n1)
            p.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            rc = p.wait()
    if rc != 0 or not out.exists():
        msg = errf.read_text(errors="replace")[-600:] if errf.exists() else ""
        raise ToolError(f"ffmpeg encode failed: {msg.strip()}", f"check ffmpeg ({encoder})")
