# Casefile tools (MCP + CLI)

One core, thin wrappers: `casefile/api.py` has one function per tool. Each function sandbox-checks paths, calls the
core, returns a dict and never raises. `casefile/mcp_server.py` (server name `casefile`) and `casefile/cli.py` both
print the same compact JSON (`separators=(',',':')`). Errors are always `{"error": ..., "hint": ...}`, never a traceback.

Coordinates are **source pixels** `[x0,y0,x1,y1]`, and times are **seconds**. Relative paths resolve against
`CASEFILE_WORKDIR` (or the CWD). When that variable is set, any path outside it is rejected.

| Tool (CLI) | Inputs | Output | Budget (tokens = chars/4) | Measured on fixture (chars) |
|---|---|---|---|---|
| `probe` (`probe`) | video | `{duration,fps,size,frames,audio_db,scenes,bad_frames[{frame,t,kind}],popups,layout_changes}` | ≤ 600 (2,400 ch) | 303 |
| `find_text` (`find-text`) | video, t, query (`re:` = regex), region?, fuzzy, row, limit | `{t,matches[{text,box,conf,row_box?}]}` | ≤ 300 (1,200 ch) | 96 |
| `ocr_region` (`ocr`) | video, t, region? | `{t,lines[{text,box}],truncated?}` | ≤ 800 (3,200 ch) | 898 (whole frame) |
| `track_text` (`track-text`) | video, query, t0, t1, step, region?, fuzzy | `{query,spans[{t0,t1,box_start,box_end,moved}]}` | ≤ 500 (2,000 ch) | 116 |
| `chart_extract` (`chart`) | video, t, plot_region, series[{name,rgb,tol}], x_ticks, y_ticks, samples | `{series:{name:{points,min,max,zero_crossings,coverage}}}` | ≤ 600 (2,400 ch) | 517 |
| `contact_sheet` (`sheet`) | video, times? \| t0/t1/n, crop?, max_width | MCP: text `{times,size}` + one PNG image; CLI: `{path,times,size}` | text ≤ 300 | 49 (+ PNG ≤ 960 px wide) |
| `tts` (`tts`) | lines[{id,text}], voice, speed, lexicon? | `{durations:{id:s},cached[ids]}` | ≤ 300 (1,200 ch) | 35 (1 line) |
| `plan` (`plan`) | spec (object, JSON text or .json path), synth | `{total,frames,shots[{i,card?,chapter,T0,dur,lines}],warnings}` | ≤ 800 (3,200 ch) | 433 (`examples/fixture.json`) |
| `render` (`render`) | spec, preview, out?, thumbs (MCP only) | `{path,duration,frames,size_mb,srt,thumbs[4],render_sec,fps_render,size,fps}` (+ one 2×2 thumbnail tile image when `thumbs=true`) | text ≤ 400 (1,600 ch) | 829 (preview) |

The tool descriptions plus JSON input schemas for all 9 tools, as served by `list_tools`, come to **5,783 chars**
(budget 10,000 ≈ 2,500 tokens).

`tests/test_mcp.py` writes the measured values to `tests/fixtures/out/mcp_sizes.json` on every run and fails if a
budget is exceeded.

## CLI

```
casefile doctor [--no-download] [--json]       # check table; exit 1 if any check fails
casefile probe VIDEO [--pretty]
casefile find-text VIDEO T QUERY [--region x0,y0,x1,y1] [--row] [--no-fuzzy] [--limit N]
casefile ocr VIDEO T [--region ...]
casefile track-text VIDEO QUERY T0 T1 [--step 0.5] [--region ...]
casefile chart VIDEO T --plot x0,y0,x1,y1 --series JSON|@file --x-ticks JSON --y-ticks JSON
casefile sheet VIDEO [--times 1,5,12 | --t0 --t1 --n] [--crop ...] [--out sheet.png]
casefile tts 'JSON [{"id","text"}]'|@lines.json [--voice af_heart] [--speed 1.1]
casefile plan SPEC.json [--no-synth]
casefile render SPEC.json [--preview] [--out out.mp4] [--jobs N]
```

Every data command accepts `--pretty` (indented JSON) and `--debug` (let unexpected errors raise with a
traceback). Exit code 2 means the tool returned `{error, hint}`.

## MCP server

```
casefile-mcp                          # stdio (Claude Code, Claude Desktop, Hermes, most clients)
casefile-mcp --http 127.0.0.1:8765    # streamable HTTP at /mcp
```

Example Hermes registration:

```yaml
mcp_servers:
  casefile:
    command: casefile-mcp          # or the full path to .venv/Scripts/casefile-mcp.exe
    env:
      CASEFILE_WORKDIR: C:/Users/me/Videos/tests
    connect_timeout: 60.0
    enabled: true
```

Heavy modules (numpy, PIL, OCR, Kokoro, render) load lazily on the first tool call. Server start is dominated by
the `mcp` SDK import, about 3 s to connect in `hermes mcp test`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CASEFILE_WORKDIR` | unset | sandbox root. Relative paths resolve here. When set, paths outside it are rejected |
| `CASEFILE_HOME` | `~/.casefile` | models (if not in `./models`) and caches (frames, OCR, probe, TTS) |
