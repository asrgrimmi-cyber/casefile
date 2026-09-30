"""casefile CLI: same tools as the MCP server, printing the same JSON (compact, or --pretty)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from . import api

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Casefile: turn a screen recording of a test session into a narrated walkthrough (CLI twin of the casefile MCP server).")

PRETTY = typer.Option(False, "--pretty", help="indented JSON")
DEBUG = typer.Option(False, "--debug", help="raise unexpected errors with a traceback")


def _emit(fn: Any, pretty: bool, debug: bool, *a: Any, **kw: Any) -> None:
    res = (fn.unsafe if debug else fn)(*a, **kw)
    typer.echo(api.dumps(res, pretty))
    if isinstance(res, dict) and "error" in res:
        raise typer.Exit(2)


def _jarg(s: Optional[str], name: str) -> Any:
    """Parse a JSON CLI argument (or @file.json)."""
    if s is None:
        return None
    try:
        if s.startswith("@"):
            return json.loads(Path(s[1:]).read_text(encoding="utf-8"))
        return json.loads(s)
    except (OSError, json.JSONDecodeError) as e:
        typer.echo(api.dumps({"error": f"--{name}: invalid JSON ({e})", "hint": "pass JSON, e.g. '[0,0,640,360]'"}))
        raise typer.Exit(2)


def _floats(s: Optional[str]) -> Optional[list[float]]:
    if s is None:
        return None
    return [float(x) for x in s.replace(",", " ").split()]


@app.command()
def doctor(no_download: bool = typer.Option(False, "--no-download", help="don't fetch missing models"),
           as_json: bool = typer.Option(False, "--json", help="print JSON instead of a table")) -> None:
    """Check ffmpeg, tesseract, fonts, Kokoro models and Python deps."""
    res = api.doctor(download=not no_download)
    if as_json:
        typer.echo(api.dumps(res, True))
    else:
        w = max(len(k) for k in res["checks"]) + 2
        for name, c in res["checks"].items():
            typer.echo(f"{'OK ' if c['ok'] else 'FAIL'} {name:<{w}}{c['detail']}")
        typer.echo("all checks passed" if res["ok"] else "some checks FAILED")
    if not res["ok"]:
        raise typer.Exit(1)


@app.command()
def probe(video: str, pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Video facts: duration, fps, size, scenes, bad frames, popups, layout changes."""
    _emit(api.probe, pretty, debug, video)


@app.command("find-text")
def find_text(video: str, t: float, query: str,
              region: Optional[str] = typer.Option(None, help="x0,y0,x1,y1"),
              no_fuzzy: bool = typer.Option(False, "--no-fuzzy"), row: bool = typer.Option(False, "--row"),
              limit: int = 5, pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Locate text on the frame at T."""
    _emit(api.find_text, pretty, debug, video, t, query, _floats(region), not no_fuzzy, row, limit)


@app.command()
def ocr(video: str, t: float, region: Optional[str] = typer.Option(None, help="x0,y0,x1,y1"),
        pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Read text lines in a region at T."""
    _emit(api.ocr_region, pretty, debug, video, t, _floats(region))


@app.command("track-text")
def track_text(video: str, query: str, t0: float, t1: float, step: float = 0.5,
               region: Optional[str] = typer.Option(None, help="x0,y0,x1,y1"),
               no_fuzzy: bool = typer.Option(False, "--no-fuzzy"), pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Follow text between T0 and T1."""
    _emit(api.track_text, pretty, debug, video, query, t0, t1, step, _floats(region), not no_fuzzy)


@app.command()
def chart(video: str, t: float,
          plot: str = typer.Option(..., help="plot region x0,y0,x1,y1"),
          series: str = typer.Option(..., help='JSON [{"name","rgb":[r,g,b],"tol":40}] or @file'),
          x_ticks: str = typer.Option(..., help="JSON [[px,val],..]"),
          y_ticks: str = typer.Option(..., help="JSON [[py,val],..]"),
          samples: int = 12, pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Extract coloured curves from a chart in axis units."""
    _emit(api.chart_extract, pretty, debug, video, t, _floats(plot), _jarg(series, "series"),
          _jarg(x_ticks, "x-ticks"), _jarg(y_ticks, "y-ticks"), samples)


@app.command()
def sheet(video: str, times: Optional[str] = typer.Option(None, help="t1,t2,..."),
          t0: Optional[float] = None, t1: Optional[float] = None, n: int = 6,
          crop: Optional[str] = typer.Option(None, help="x0,y0,x1,y1"), max_width: int = 960,
          out: Optional[str] = typer.Option(None, help="output PNG"), pretty: bool = PRETTY,
          debug: bool = DEBUG) -> None:
    """Contact sheet PNG of frames with timestamps."""
    _emit(api.contact_sheet, pretty, debug, video, _floats(times), t0, t1, n, _floats(crop), max_width, out)


@app.command()
def tts(lines: str = typer.Argument(..., help='JSON [{"id","text"}] or @file.json'),
        voice: str = "af_heart", speed: float = 1.1,
        lexicon: Optional[str] = typer.Option(None, help="JSON {word: spoken}"),
        pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Synthesise narration lines (cached WAVs) and print durations."""
    _emit(api.tts, pretty, debug, _jarg(lines, "lines"), voice, speed, _jarg(lexicon, "lexicon"))


@app.command()
def plan(spec: str = typer.Argument(..., help="spec .json path"),
         no_synth: bool = typer.Option(False, "--no-synth", help="estimate durations instead of TTS"),
         pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Validate and time a spec."""
    _emit(api.plan, pretty, debug, spec, not no_synth)


@app.command()
def render(spec: str = typer.Argument(..., help="spec .json path"),
           preview: bool = typer.Option(False, "--preview", help="960x540 @15fps draft"),
           out: Optional[str] = None, jobs: Optional[int] = None,
           pretty: bool = PRETTY, debug: bool = DEBUG) -> None:
    """Render a spec to MP4 (+ SRT and thumbnails)."""
    _emit(api.render, pretty, debug, spec, preview, out, jobs)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
