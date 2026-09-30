# Internal module contract

This is the contract the modules were built against in parallel (see docs/BUILD_STORY.md). Keep it in sync when you change a public function.

All modules live in `casefile/`. Existing, DO NOT rewrite (extend only if essential, note it):
`config.py` (paths, brand constants, `tool_path`), `errors.py` (`ToolError(error, hint)`, `check_path`),
`fonts.py` (`font`, `text_len`, `draw_text`), `media.py` (`video_info`, `FrameStore`, `run`, `ffmpeg`, `ffprobe`),
`spec.py` (pydantic `Spec` + overlay models, `DEFAULT_LEXICON`).

Rules: type hints, docstrings on public funcs, deterministic, never load whole video in RAM (use FrameStore),
coordinates in SOURCE pixels, raise `ToolError(error, hint)` for user errors. Every public function returns
plain JSON-serialisable dicts/lists (rounded floats: 2 decimals for seconds, ints for pixels) — the MCP layer
just `json.dumps` them. Windows + Linux: use `config.tool_path('tesseract'|'ffmpeg')`, pathlib, no shell=True.

Python: `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python`; install extra deps with
`uv pip install --python .venv/Scripts/python.exe <pkg>` AND add them to pyproject.toml.
Tests: `.venv/Scripts/python.exe -m pytest tests/<your file> -q`. Fixture: `tests/fixtures/make_fixture.py`
(build via `tests/conftest.py` fixture `fixture_video` -> (Path mp4, dict truth)).

## detect.py  (Phase 1)
- `probe(video: str|Path) -> dict`: `{duration, fps, size:[w,h], frames, audio_db (mean dBFS or null),
  scenes:[t], bad_frames:[{frame,t,kind}], popups:[t], layout_changes:[t]}` — ≤ 600 tokens (chars/4).
  kind in {"black","glitch","popup"}. Cache result JSON in the FrameStore dir (`probe.json`).
- `bad_frame_list(video) -> list[int]` and `remap_for(video, bad: list[int]|"auto"|"none") -> dict[int,int]`
  (bad -> nearest EARLIER good frame).

## vision.py  (Phase 2)
- `ocr_image(img: PIL.Image, region=None) -> list[{text, box:[x0,y0,x1,y1], conf}]` words in source px
  (2x upscale + threshold internally; cache by (video key, frame, region)).
- `find_text(video, t, query, region=None, fuzzy=True, row=False, limit=5) -> {"t":..,"matches":[{text, box, conf, row_box?}]}`
  query may be multi-word (match consecutive words) or regex (`re:` prefix). row=True expands to the
  form/table row (horizontal band of similar background bounded by separators / panel edges). ≤ 300 tokens.
- `ocr_region(video, t, region) -> {"lines":[{text, box}]}` reading order. ≤ 800 tokens.
- `track_text(video, query, t0, t1, step=0.5) -> {"spans":[{t0,t1,box_start,box_end,moved:bool}]}` ≤ 500 tokens.
- `contact_sheet(video, times=None, t0=None, t1=None, n=6, crop=None, max_width=960, out=None) -> {"path", "times"}`
  one PNG, timestamps burned in.

## chart.py  (Phase 2)
- `chart_extract(video, t, plot_region, series:[{name, rgb:[r,g,b], tol=40}], x_ticks:[[px,val],..],
  y_ticks:[[py,val],..], samples=12) -> {series:{name:{points:[[x,y]..], min:[x,y], max:[x,y], zero_crossings:[x]}}}`
  values in axis units; linear interpolation from 2+ ticks. ≤ 600 tokens.

## audio.py  (Phase 3)
- `speakable(text, lexicon: dict) -> str` whole-word replacement (DEFAULT_LEXICON merged under spec lexicon).
- `tts(lines: list[{id, text}], voice="af_heart", speed=1.1, lexicon=None) -> {"durations":{id: sec}, "cached":[ids]}`
  WAVs cached at `cache_dir('tts')/<sha1(spoken+voice+speed)>.wav` (24 kHz mono float). Kokoro loaded lazily once.
  `wav_path(text_spoken, voice, speed) -> Path`.
- `narration_track(placements: list[(wav_path, start_sec)], total: float, out: Path, sr=24000) -> Path`
  mix into one mono WAV, peak-normalise to 0.89.
- loudnorm is applied at mux time by render (`config.LOUDNORM`).

## planner.py  (Phase 4)
- `load_spec(spec: dict|str|Path) -> Spec` (str/Path = JSON file path or JSON text). Pydantic errors ->
  ToolError with readable path, e.g. `shots[2].overlays[0].to: ...`.
- `plan(spec, *, resolve_find=True, synth=True) -> Plan` and `plan_json(spec) -> dict` (the MCP answer, ≤ 800 tokens).
  Line durations come from `audio.tts` (synth=True) or a word-rate estimate (2.6 words/s at speed 1.0, synth=False).
- `Plan` dataclass (consumed by render.py):
  ```
  Plan.spec: Spec; Plan.source: Path|None; Plan.src_size: (w,h)|None; Plan.fps: int; Plan.size: (W,H)
  Plan.total: float; Plan.remap: dict[int,int]; Plan.warnings: list[str]
  Plan.lines: dict[id, {caption, spoken, wav: Path|None, dur}]
  Plan.chapters: list[(T, name)]           # absolute chapter start times
  Plan.shots: list[PShot]
  PShot: idx, shot (the Shot model), T0 (abs start, xfade overlap already subtracted), dur, xfade, chapter,
         line_times: dict[id, (start_local, end_local)], overlays: list[dict]  (each overlay model_dump(by_alias)
         with 'from'/'to' RESOLVED to local float seconds, box/blur 'find' RESOLVED to 'rect', tiles[].at resolved),
         camera: list[(t_local, (x,y,w,h))]  ("full" resolved to (0,0,src_w,src_h)),
         src_time(t_local) -> float source seconds (segments with speed, then hold)
  ```
- Line timing: t = lead; each line starts at max(t, at) ; next t = end + gap. dur = max(play_len, last_end + tail).
  play_len = sum((t1-t0)/speed). T0 = prev_T_end - xfade (first shot 0). Cards: dur from lines + tail, min 3 s.
- Warnings: box off-frame, from >= to, overlapping captions, camera rect outside source, unused lines.
  Errors (ToolError): unknown line id, missing source, from after to beyond shot, find with no OCR match.

## render.py  (Phase 5)
- `render(spec, *, preview=False, out=None, jobs=None) -> {"path", "duration", "frames", "size_mb", "srt", "thumbs":[4 png paths]}`
  preview: 960x540 @15 fps. Parallel segments via multiprocessing (spawn-safe, each worker re-plans with
  synth cached), each pipes rawvideo into ffmpeg libx264 CRF18 yuv420p; concat demuxer; mux narration
  (AAC 160k, loudnorm I=-16 TP=-1.5), `-movflags +faststart`. Exact frame count = ceil(total*fps).
- `frame_at(plan, T) -> PIL.Image` (output size), `srt_for(plan) -> str`.
- Layout: content area = W x (H - bar) where bar = round(111 * H/1080); source scaled to fill it via camera
  crop (aspect locked to content area). Caption bar: chapter chip (accent) + caption; progress bar (6 px,
  accent) with chapter ticks. Box: spotlight dims outside to 58% brightness, rounded accent stroke with soft glow,
  pop-in 1.12->1 over 0.4 s, label chip. Cards use spec.brand.logo if set (never cropped), else a small text wordmark.
  Visual style: see casefile/draw.py.
