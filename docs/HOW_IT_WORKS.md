# How Casefile works

## Architecture: one core, two thin wrappers

```
                ┌───────────────────── casefile/ (core library) ─────────────────────┐
                │ detect.py   probe, bad frames, scenes, popups, layout changes       │
                │ vision.py   OCR (Tesseract), find_text, ocr_region, track_text,     │
                │             contact_sheet                                            │
                │ chart.py    chart_extract (curve pixels -> axis values)              │
                │ audio.py    Kokoro TTS (cached per line), narration mix              │
                │ spec.py     pydantic edit-spec models                                │
                │ planner.py  spec -> absolute timeline (Plan), validation             │
                │ draw.py     cards, overlays, caption bar, camera                     │
                │ render.py   parallel frame rendering, ffmpeg encode + mux, SRT       │
                │ media.py    ffmpeg/ffprobe, FrameStore (cached decoded frames)       │
                └───────────────────────────────▲──────────────────────────────────────┘
                                                │
                         api.py  one function per tool: sandbox check,
                                 call core, return dict, never raise
                              ┌─────────────────┴─────────────────┐
                    mcp_server.py (casefile-mcp)          cli.py (casefile)
                    9 MCP tools, stdio or HTTP            same tools, same JSON
```

The MCP server and the CLI contain no logic. Anything you can do from an assistant, you can do from a shell, and the
output is byte-for-byte the same JSON. That makes bugs reproducible without an assistant in the loop.

## The 9 tools

| Tool | What it answers |
|---|---|
| `probe` | How long is the video, where are the scene changes, which frames are black/glitched/popups, where does the layout change? |
| `find_text` | Where exactly (source px) is the text "Band" at t = 3 s, and what is its whole form row? |
| `ocr_region` | What text is on screen at t (optionally inside a region), in reading order? |
| `track_text` | Over [t0, t1], when is "SIB19 received" visible, and does it move (scrolling logs/terminals)? |
| `chart_extract` | For a chart at t, what are the min/max/zero crossings of each coloured series, in axis units? |
| `contact_sheet` | A single PNG grid of frames with timestamps. This is the only tool that returns an image, for when text is not enough |
| `tts` | How long will each narration line take when spoken? Synthesises and caches the WAVs |
| `plan` | Validate the spec and return the timeline: shot starts, durations, line times, warnings |
| `render` | Draw and encode the video (preview 960x540 @ 15 fps, or final 1920x1080 @ 30 fps) plus `.srt` and 4 thumbnails |

Full inputs, outputs and measured sizes are in [tools.md](tools.md).

## The edit spec

The whole walkthrough is one JSON file: `lines` (captions and narration) and `shots` (source segments or intro/outro
cards, with camera keyframes and overlays). Times inside a shot can be relative to a narration line
(`"s:c1+0.3"` = 0.3 s after line `c1` starts), so re-voicing a line does not break the highlights. See [spec.md](spec.md).

```json
{"source": "tc-017.mp4",
 "lines": {"c1": {"caption": "Band n256 is selected"}},
 "shots": [{"src": [[0.7, 6.3]], "lines": ["c1"], "chapter": "01 · CELL",
            "overlays": [{"type": "box", "rect": [190, 455, 590, 491], "label": "Band",
                          "from": "s:c1", "to": "e:c1+0.3"}]}]}
```

## Pipeline

1. **probe**: extracts frames once into a cache (`~/.casefile/cache/frames/<hash>/`), then finds scenes, bad frames,
   popups and layout changes. Later calls are free.
2. **locate**: `ocr_region` to learn what each screen shows, `find_text` for exact boxes (word and row),
   `track_text` for scrolling text, and `chart_extract` for numbers on charts. OCR results are cached per frame and region.
3. **spec**: the assistant writes `edit.json` from those facts.
4. **plan**: TTS gives the real line durations (cached by text + voice + speed). The planner lays out the timeline,
   resolves `find` boxes and TimeRefs, and returns errors (`{error, hint}` with a field path) or warnings.
5. **preview**: a 960x540 @ 15 fps render plus 4 thumbnails. The assistant looks at 2–4 thumbnails and checks them
   against the review checklist in the skill.
6. **render**: final 1920x1080 @ 30 fps, H.264 CRF 18 + AAC 160k, loudness-normalised (I = −16 LUFS), `+faststart`,
   with a `.srt` next to it. Frames are rendered in parallel worker processes and concatenated with ffmpeg.

## Why text-first

An assistant that "watches" a recording pays for every frame it looks at. Even a few dozen screenshots of a 1440p
screen cost far more context than the whole edit needs. Casefile turns the video into small facts instead:

- A `find_text` answer is about 100 characters (~25 tokens). A `probe` of the fixture is about 300 characters.
- All 9 tool descriptions and schemas together are 5,783 characters (~1,450 tokens) of context.
- Images are opt-in (`contact_sheet`, render thumbnails), and the skill caps them at 10 per edit.
- Facts from OCR and chart extraction are more exact than a model's reading of a downscaled screenshot. The
  fixture gates put boxes within 1–2 px (see [RESULTS.md](RESULTS.md)).

The trade-off is that OCR can miss things a human would see, such as icons, colour-only states and very small text.
That is what `contact_sheet` is for.

## Sandbox and safety

- Setting `CASEFILE_WORKDIR` makes it the sandbox root. Every path argument (video, spec, output) is resolved and
  rejected if it falls outside the root. Relative paths resolve against it.
- No shell: ffmpeg and Tesseract are run through `subprocess` with argument lists, never `shell=True`.
- Network: only `casefile doctor` makes network requests, to download the Kokoro model files from their GitHub
  release. No tool uploads anything. After the model download, everything works offline.
- User errors come back as `{"error": ..., "hint": ...}`. The hint says what to change. Tracebacks are only shown
  in the CLI with `--debug`.
