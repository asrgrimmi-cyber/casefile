"""casefile MCP server: 9 token-lean tools over the core library (stdio, or --http HOST:PORT).

Nothing heavy is imported at module load; each tool imports its core module lazily via casefile.api.
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

INSTRUCTIONS = ("Edit screen-recording demos without watching them: probe -> find_text/ocr_region/track_text/"
                "chart_extract/contact_sheet to locate things (source px, seconds) -> write a spec -> plan -> "
                "render(preview=true) -> render. Errors come back as {error,hint}.")

server = _Server("casefile", instructions=INSTRUCTIONS)

Video = Annotated[str, Field(description="video path (relative to workdir ok)")]
T = Annotated[float, Field(description="time in s")]
Region = Annotated[list[float] | None, Field(description="[x0,y0,x1,y1] source px; null=full frame")]
Spec = Annotated[dict[str, Any] | str, Field(description="edit spec object, JSON text, or .json path")]


def _j(d: dict) -> str:
    return api.dumps(d)


@server.tool(structured_output=False)
def probe(video: Video) -> str:
    """Video facts: duration,fps,size,frames,audio_db,scenes[t],bad_frames[{frame,t,kind}],popups[t],layout_changes[t]."""
    return _j(api.probe(video))


@server.tool(structured_output=False)
def find_text(video: Video, t: T,
              query: Annotated[str, Field(description="text (multi-word ok) or 're:<regex>'")],
              region: Region = None, fuzzy: bool = True,
              row: Annotated[bool, Field(description="also return row_box of the form/table row")] = False,
              limit: int = 5) -> str:
    """Locate text on the frame at t -> {t,matches:[{text,box,conf,row_box?}]}. Use boxes for overlays."""
    return _j(api.find_text(video, t, query, region, fuzzy, row, limit))


@server.tool(structured_output=False)
def ocr_region(video: Video, t: T, region: Region = None) -> str:
    """Read all text lines in region at t -> {t,lines:[{text,box}]} in reading order."""
    return _j(api.ocr_region(video, t, region))


@server.tool(structured_output=False)
def track_text(video: Video, query: str, t0: float, t1: float,
               step: Annotated[float, Field(description="sample interval s")] = 0.5,
               region: Region = None, fuzzy: bool = True) -> str:
    """When/where text is visible in [t0,t1] -> {query,spans:[{t0,t1,box_start,box_end,moved}]}."""
    return _j(api.track_text(video, query, t0, t1, step, region, fuzzy))


@server.tool(structured_output=False)
def chart_extract(video: Video, t: T,
                  plot_region: Annotated[list[float], Field(description="[x0,y0,x1,y1] plot area px")],
                  series: Annotated[list[dict[str, Any]], Field(description="[{name,rgb:[r,g,b],tol?:40}]")],
                  x_ticks: Annotated[list[list[float]], Field(description="[[px,value],..] >=2")],
                  y_ticks: Annotated[list[list[float]], Field(description="[[py,value],..] >=2")],
                  samples: int = 12) -> str:
    """Read coloured curves in axis units -> {series:{name:{points,min,max,zero_crossings,coverage}}}."""
    return _j(api.chart_extract(video, t, plot_region, series, x_ticks, y_ticks, samples))


@server.tool(structured_output=False)
def contact_sheet(video: Video,
                  times: Annotated[list[float] | None, Field(description="exact times; else t0/t1/n")] = None,
                  t0: float | None = None, t1: float | None = None, n: int = 6, crop: Region = None,
                  max_width: int = 960) -> list:
    """Look at frames: one PNG grid with burned-in timestamps (+ text with times)."""
    r = api.contact_sheet(video, times, t0, t1, n, crop, max_width)
    if "error" in r:
        return [_j(r)]
    return [_j({"times": r["times"], "size": r["size"]}), Image(path=r["path"])]


@server.tool(structured_output=False)
def tts(lines: Annotated[list[dict[str, str]], Field(description="[{id,text}]")],
        voice: str = "af_heart", speed: float = 1.1,
        lexicon: Annotated[dict[str, str] | None, Field(description="word->spoken form")] = None) -> str:
    """Synthesise narration (cached WAVs) -> {durations:{id:s},cached:[ids]}."""
    return _j(api.tts(lines, voice, speed, lexicon))


@server.tool(structured_output=False)
def plan(spec: Spec, synth: Annotated[bool, Field(description="real TTS durations (else estimate)")] = True) -> str:
    """Validate + time a spec -> {total,frames,shots:[{i,chapter,T0,dur,lines}],warnings}. Run before render."""
    return _j(api.plan(spec, synth))


@server.tool(structured_output=False)
def render(spec: Spec, preview: Annotated[bool, Field(description="960x540@15fps fast draft")] = False,
           out: Annotated[str | None, Field(description="output .mp4 path")] = None,
           thumbs: Annotated[bool, Field(description="also return one image tiling 4 thumbnails")] = False) -> list:
    """Render spec to MP4 (+SRT) -> {path,duration,frames,size_mb,srt,thumbs}."""
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
