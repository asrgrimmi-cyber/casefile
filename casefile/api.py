"""JSON-safe tool wrappers shared by the CLI and the MCP server (one function per tool).

Every function validates paths against the CASEFILE_WORKDIR sandbox, calls the core library and
returns a plain dict. Errors never raise: ToolError -> {error, hint}; anything else -> a short message.
Heavy modules are imported lazily inside each function so importing this module stays cheap.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Callable

from .errors import ToolError, check_path

UNEXPECTED_HINT = "unexpected error; rerun with casefile CLI for details"


def _safe(fn: Callable[..., dict]) -> Callable[..., dict]:
    @functools.wraps(fn)
    def wrap(*a: Any, **kw: Any) -> dict:
        try:
            return fn(*a, **kw)
        except ToolError as e:
            return e.to_dict()
        except Exception as e:  # never leak a traceback to the model
            msg = f"{type(e).__name__}: {e}".strip()
            return {"error": msg.splitlines()[0][:200], "hint": UNEXPECTED_HINT}
    wrap.unsafe = fn  # type: ignore[attr-defined]  # CLI --debug path
    return wrap


def dumps(obj: Any, pretty: bool = False) -> str:
    """Compact JSON (the MCP wire format) or indented JSON for humans."""
    if pretty:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _video(video: str) -> str:
    return str(check_path(video, must_exist=True))


def _out(out: str | None) -> str | None:
    return None if out in (None, "") else str(check_path(out, must_exist=False, write=True))


def _spec(spec: dict | str) -> dict | str:
    """dict -> as is; str -> JSON text or a .json path (sandbox-checked)."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str) and not spec.lstrip().startswith("{"):
        return str(check_path(spec, must_exist=True))
    return spec


def _region(r: list | None) -> list | None:
    if r is None:
        return None
    if len(r) != 4:
        raise ToolError("region must be [x0,y0,x1,y1]", "give source-pixel coordinates")
    return [int(round(float(v))) for v in r]


@_safe
def probe(video: str) -> dict:
    """Video facts: duration, fps, size, frames, audio_db, scenes, bad_frames, popups, layout_changes."""
    from . import detect
    return detect.probe(_video(video))


@_safe
def find_text(video: str, t: float, query: str, region: list | None = None, fuzzy: bool = True,
              row: bool = False, limit: int = 5) -> dict:
    """Locate text on the frame at t -> {t, matches:[{text, box, conf, row_box?}]}."""
    from . import vision
    return vision.find_text(_video(video), float(t), query, region=_region(region), fuzzy=fuzzy, row=row,
                            limit=int(limit))


@_safe
def ocr_region(video: str, t: float, region: list | None = None) -> dict:
    """Read text lines in a region at t -> {t, lines:[{text, box}]}."""
    from . import vision
    return vision.ocr_region(_video(video), float(t), region=_region(region))


@_safe
def track_text(video: str, query: str, t0: float, t1: float, step: float = 0.5, region: list | None = None,
               fuzzy: bool = True) -> dict:
    """Follow text over [t0,t1] -> {query, spans:[{t0,t1,box_start,box_end,moved}]}."""
    from . import vision
    return vision.track_text(_video(video), query, float(t0), float(t1), step=float(step),
                             region=_region(region), fuzzy=fuzzy)


@_safe
def chart_extract(video: str, t: float, plot_region: list, series: list, x_ticks: list, y_ticks: list,
                  samples: int = 12) -> dict:
    """Trace coloured curves in plot_region -> {series:{name:{points,min,max,zero_crossings}}}."""
    from . import chart
    return chart.chart_extract(_video(video), float(t), plot_region, series, x_ticks, y_ticks, samples=int(samples))


@_safe
def contact_sheet(video: str, times: list | None = None, t0: float | None = None, t1: float | None = None,
                  n: int = 6, crop: list | None = None, max_width: int = 960, out: str | None = None) -> dict:
    """PNG grid of frames with burned-in times -> {path, times, size}."""
    from . import vision
    return vision.contact_sheet(_video(video), times=times, t0=t0, t1=t1, n=int(n), crop=_region(crop),
                                max_width=min(int(max_width), 1920), out=_out(out))


@_safe
def tts(lines: list, voice: str = "af_heart", speed: float = 1.1, lexicon: dict | None = None) -> dict:
    """Synthesise narration lines [{id,text}] -> {durations:{id:s}, cached:[ids]}."""
    if not isinstance(lines, list) or not all(isinstance(x, dict) and "id" in x and "text" in x for x in lines):
        raise ToolError("lines must be [{id, text}, ...]", 'e.g. [{"id":"intro","text":"Hello"}]')
    from . import audio
    return audio.tts(lines, voice=voice, speed=float(speed), lexicon=lexicon)


@_safe
def plan(spec: dict | str, synth: bool = True) -> dict:
    """Validate + time a spec -> {total, frames, shots:[{i,chapter,T0,dur,lines}], warnings}."""
    from . import planner
    return planner.plan_json(_spec(spec), synth=synth)


@_safe
def render(spec: dict | str, preview: bool = False, out: str | None = None, jobs: int | None = None) -> dict:
    """Render spec to MP4 -> {path, duration, frames, size_mb, srt, thumbs:[4 png], ...}."""
    from . import render as _render
    return _render.render(_spec(spec), preview=preview, out=_out(out), jobs=jobs)


def thumbs_tile(thumbs: list[str], max_width: int = 960) -> str | None:
    """Combine render thumbnails into one small 2x2 PNG next to the first thumb; returns its path."""
    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in thumbs if Path(p).exists()][:4]
    if not ims:
        return None
    cols = 2 if len(ims) > 1 else 1
    rows = (len(ims) + cols - 1) // cols
    gap = 4
    tw = (max_width - gap * (cols + 1)) // cols
    th = round(tw * ims[0].height / ims[0].width)
    tile = Image.new("RGB", (gap + cols * (tw + gap), gap + rows * (th + gap)), (18, 21, 44))
    for i, im in enumerate(ims):
        tile.paste(im.resize((tw, th), Image.LANCZOS), (gap + (i % cols) * (tw + gap), gap + (i // cols) * (th + gap)))
    out = Path(thumbs[0]).with_name(Path(thumbs[0]).stem.rsplit("_", 1)[0] + "_thumbs_tile.png")
    tile.save(out)
    return str(out)


# ------------------------------------------------------------------ v2 (job-based; implementation in api_v2.py)
def _v2(name: str) -> Callable[..., Any]:
    def call(*a: Any, **kw: Any) -> Any:
        from . import api_v2
        return getattr(api_v2, name)(*a, **kw)
    call.__name__ = name
    return _safe(call)  # type: ignore[arg-type]


analyze_recording = _v2("analyze_recording")
query_timeline = _v2("query_timeline")
locate = _v2("locate")
make_video = _v2("make_video")
make_doc = _v2("make_doc")
wait_job = _v2("wait_job")
patch = _v2("patch")
verify = _v2("verify")
worker_status = _v2("worker_status")
get_lessons = _v2("get_lessons")
log_change = _v2("log_change")
report_gap = _v2("report_gap")


def doctor(download: bool = True) -> dict:
    """Environment check (never raises)."""
    try:
        from . import doctor as _doctor
        return _doctor.doctor(download=download)
    except Exception as e:
        return {"ok": False, "checks": {"doctor": {"ok": False, "detail": str(e)[:200]}}}
