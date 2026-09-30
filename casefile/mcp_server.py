"""casefile MCP server: v2 job tools (timeline-first) + the 9 legacy frame tools (stdio, or --http HOST:PORT).

Nothing heavy is imported at module load; each tool imports its core module lazily via casefile.api.
v2 tools return within ~5 s (or a job id; poll with wait_job). Descriptions+schemas of ALL tools <= 10k chars.
"""
from __future__ import annotations

import argparse
from typing import Annotated, Any

from pydantic import Field

try:  # mcp >= 2
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp import Image

from . import api

INSTRUCTIONS = ("Document a screen recording in <=8 calls: analyze_recording -> wait_job -> query_timeline/locate "
                "(item ids, never coordinates) -> make_doc/make_video -> wait_job -> verify -> patch if needed. "
                "Legacy frame tools are a fallback. Errors: {error,hint}.")

server = _Server("casefile", instructions=INSTRUCTIONS)

Video = Annotated[str, Field(description="video path")]
T = Annotated[float, Field(description="time s")]
Region = Annotated[list[float] | None, Field(description="[x0,y0,x1,y1] px")]
Spec = Annotated[dict[str, Any] | str, Field(description="spec object, JSON text or .json path")]


def _j(d: Any) -> str:
    return d if isinstance(d, str) else api.dumps(d)


# ================================================================== v2
@server.tool(structured_output=False)
def analyze_recording(video: Video, focus: Annotated[str | None, Field(description="what matters")] = None) -> str:
    """Analyse once (cached) -> step summary (ids,titles,fields,commands) or {job_id} if still running."""
    return _j(api.analyze_recording(video, focus))


@server.tool(structured_output=False)
def query_timeline(video: Video, questions: Annotated[list[str], Field(description="e.g. 'pusch_snr'")]) -> str:
    """Answer from the timeline, one line each: q | id | text | x0,y0,x1,y1 | t."""
    return _j(api.query_timeline(video, questions))


@server.tool(structured_output=False)
def locate(video: Video, items: Annotated[list[Any], Field(description="text or {step,text}")]) -> str:
    """Find UI items -> text | found | id | box | t per line. Use ids in specs/docs."""
    return _j(api.locate(video, items))


@server.tool(structured_output=False)
def make_video(spec: Spec, preview: bool = False, out: str | None = None) -> str:
    """Render spec as a job. Box/blur may use {"item":"s5.x"} (rect+time filled in) -> {job_id}."""
    return _j(api.make_video(spec, preview, out))


@server.tool(structured_output=False)
def make_doc(video: Video,
             doc: Annotated[dict[str, Any] | str,
                            Field(description="{title,summary,steps:[{step,heading,body,highlight:[ids]}]}")],
             formats: Annotated[list[str] | None, Field(description="md,docx,pdf,confluence")] = None,
             out: str | None = None) -> str:
    """Written doc from the timeline as a job -> {job_id}."""
    return _j(api.make_doc(video, doc, formats, out))


@server.tool(structured_output=False)
def wait_job(job_id: str, max_wait: float = 50) -> str:
    """Long-poll a job -> state/progress, or result (local paths, duration/pages, warnings)."""
    return _j(api.wait_job(job_id, max_wait))


@server.tool(structured_output=False)
def patch(path: str,
          ops: Annotated[list[dict[str, Any]], Field(description="[{op:set|del|add,path:'/shots/3/label',value}]")],
          why: Annotated[str, Field(description="required; logged")]) -> str:
    """Edit a spec/doc JSON file -> new timing summary."""
    return _j(api.patch(path, ops, why))


@server.tool(structured_output=False)
def verify(path: str) -> list:
    """OCR each highlight in a rendered .mp4/doc vs expected text -> PASS/FAIL lines (+crops of fails)."""
    r = api.verify(path)
    res: list = [_j(r)]
    crops = (r.get("crops") or []) if isinstance(r, dict) else []
    if 0 < len(crops) <= 2:
        res += [Image(path=c) for c in crops]
    return res


@server.tool(structured_output=False)
def worker_status() -> str:
    """GPU worker health/queue; backend gpu or local."""
    return _j(api.worker_status())


@server.tool(structured_output=False)
def look(video: Video, times: list[float], crop: Region = None) -> list:
    """LAST RESORT: one contact-sheet image of frames at times."""
    r = api.contact_sheet(video, times, None, None, len(times or []), crop, 960)
    if "error" in r:
        return [_j(r)]
    return [_j({"times": r["times"]}), Image(path=r["path"])]


@server.tool(structured_output=False)
def get_lessons(context: str | None = None) -> str:
    """Approved lessons for this job (read first)."""
    return _j(api.get_lessons(context))


@server.tool(structured_output=False)
def log_change(target: str, before: Any, after: Any, why: str) -> str:
    """Record a correction made outside patch."""
    return _j(api.log_change(target, before, after, why))


@server.tool(structured_output=False)
def report_gap(tool: str, need: str, example: str | None = None) -> str:
    """Report something a tool could not do."""
    return _j(api.report_gap(tool, need, example))


# ================================================================== legacy (local frame tools)
@server.tool(structured_output=False)
def probe(video: Video) -> str:
    """Video facts: duration,fps,size,scenes,bad_frames,popups."""
    return _j(api.probe(video))


@server.tool(structured_output=False)
def find_text(video: Video, t: T, query: Annotated[str, Field(description="text or 're:<regex>'")],
              region: Region = None, fuzzy: bool = True, row: bool = False, limit: int = 5) -> str:
    """Text boxes on frame t (+row_box)."""
    return _j(api.find_text(video, t, query, region, fuzzy, row, limit))


@server.tool(structured_output=False)
def ocr_region(video: Video, t: T, region: Region = None) -> str:
    """Text lines in region at t."""
    return _j(api.ocr_region(video, t, region))


@server.tool(structured_output=False)
def track_text(video: Video, query: str, t0: float, t1: float, step: float = 0.5, region: Region = None,
               fuzzy: bool = True) -> str:
    """Spans where text is visible in [t0,t1]."""
    return _j(api.track_text(video, query, t0, t1, step, region, fuzzy))


@server.tool(structured_output=False)
def chart_extract(video: Video, t: T, plot_region: list[float],
                  series: Annotated[list[dict[str, Any]], Field(description="[{name,rgb,tol?}]")],
                  x_ticks: Annotated[list[list[float]], Field(description="[[px,value],..]")],
                  y_ticks: list[list[float]], samples: int = 12) -> str:
    """Chart curves in axis units."""
    return _j(api.chart_extract(video, t, plot_region, series, x_ticks, y_ticks, samples))


@server.tool(structured_output=False)
def contact_sheet(video: Video, times: list[float] | None = None, t0: float | None = None, t1: float | None = None,
                  n: int = 6, crop: Region = None, max_width: int = 960) -> list:
    """PNG grid of frames with timestamps."""
    r = api.contact_sheet(video, times, t0, t1, n, crop, max_width)
    if "error" in r:
        return [_j(r)]
    return [_j({"times": r["times"], "size": r["size"]}), Image(path=r["path"])]


@server.tool(structured_output=False)
def tts(lines: Annotated[list[dict[str, str]], Field(description="[{id,text}]")], voice: str = "af_heart",
        speed: float = 1.1, lexicon: dict[str, str] | None = None) -> str:
    """Narration WAVs -> durations."""
    return _j(api.tts(lines, voice, speed, lexicon))


@server.tool(structured_output=False)
def plan(spec: Spec, synth: bool = True) -> str:
    """Validate + time a spec."""
    return _j(api.plan(spec, synth))


@server.tool(structured_output=False)
def render(spec: Spec, preview: bool = False, out: str | None = None, thumbs: bool = False) -> list:
    """Render spec to MP4 synchronously (local)."""
    r = api.render(spec, preview, out)
    res: list = [_j(r)]
    if thumbs and "error" not in r and r.get("thumbs"):
        try:
            tile = api.thumbs_tile(r["thumbs"])
            if tile:
                res.append(Image(path=tile))
        except Exception:
            pass
    return res


def main(argv: list[str] | None = None) -> None:
    """Entry point: stdio by default; --http HOST:PORT serves streamable HTTP at /mcp."""
    ap = argparse.ArgumentParser(prog="casefile-mcp", description="casefile MCP server")
    ap.add_argument("--http", metavar="HOST:PORT", help="serve streamable-http instead of stdio")
    a = ap.parse_args(argv)
    if a.http:
        host, _, port = a.http.rpartition(":")
        server.run("streamable-http", host=host or "127.0.0.1", port=int(port))
    else:
        server.run()


if __name__ == "__main__":
    main()
