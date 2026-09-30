# Contract v2: timeline, GPU worker, job tools, written docs

Goal (from the build plan): a 2.5-min recording costs ≤150k effective Claude tokens in ≤8 tool calls.
The recording is analysed ONCE into `timeline.json`; video, written docs and answers are built from it.
Claude never sees coordinates or frames unless verification fails.

## Topology
Claude ── MCP (Windows, `casefile-mcp`) ──HTTP+token──> worker (Linux GPU, `casefile-worker`, port 8765)
If the worker is unreachable the MCP runs the same core functions locally (CPU) and says `"backend":"local"`.
Config (env on the MCP side): `CASEFILE_WORKER_URL` (e.g. http://192.168.0.110:8765), `CASEFILE_WORKER_TOKEN`.
Worker env: `CASEFILE_WORKER_TOKEN`, `CASEFILE_HOME` (data: inbox/, jobs.db, outputs/), `CASEFILE_BIND` (host:port).

## timeline.json (casefile/timeline.py builds it; schema version "1")
```
{ "version":"1", "video":{path,sha256,duration,fps,size:[w,h]},
  "steps":[ { "id":"s1", "t0":0.5, "t1":11.9, "stable_t":3.2,        # stable_t = best still frame
              "kind":"gui"|"terminal"|"chart"|"dialog"|"other",
              "title":"Web GUI · enb · Stats",                     # short auto title from OCR headings
              "screenshot":"steps/s1.png",                          # full frame at stable_t (relative to timeline dir)
              "items":[ {"id":"s1.ntn","text":"NTN","box":[x0,y0,x1,y1],"t":3.2,"conf":95,"role":"tab"|"label"|"value"|"field"|"heading"|"text"} ],
              "fields":{"UEs":"1","RNTI":"0x461e"},                   # key/value pairs parsed from OCR (label: value, JSON "k": v)
              "commands":[{"t":41.6,"text":"./ws.js 127.0.0.1:9001 '{\"message\":\"ue_get\"}'"}],
              "transcript":"...",                                   # terminal steps: merged, normalised text, ≤ 4k chars
              "scroll":[{"t":98.0,"dy":-440}],                      # terminal/page scroll events inside the step
              "private":[{"box":[..],"reason":"email|hostname|ip|imsi","t0":..,"t1":..}] } ],
  "speech":[{"t0":..,"t1":..,"text":".."}],                         # optional (faster-whisper), [] if no speech
  "ignored_regions":[{"box":[..],"reason":"live_chart"}],
  "stats":{"frames_analysed":N,"analyse_sec":S,"backend":"gpu"|"local","ocr":"rapidocr"|"tesseract"} }
```
Item ids are `<step>.<slug>` (slug from text, lowercase, `[a-z0-9_]`, unique within step).
Change points: frame-diff on a downscaled grid EXCLUDING `ignored_regions` (live charts = regions that change
every sample while the rest is static). Analyse only frames at change points + stable_t (~25 per 2.5 min).
OCR normalisation (`casefile/textnorm.py`): hex look-alikes (`Ox46le` -> `0x461e`), digit look-alikes inside numbers
(O->0, l/I->1, S->5 when surrounded by digits), keep quotes/colons in JSON, strip box-drawing noise.
Privacy detection: regex for emails, IPv4, `user@host`, 15-digit IMSI -> `private[]` (blurred by default).

## Core functions (casefile/*.py; return JSON-safe dicts)
- `timeline.analyze(video, focus=None, speech="auto", ocr="auto") -> dict` (writes `<home>/timelines/<sha>/timeline.json`, returns summary ≤ 12k chars)
- `timeline.summary(tl) -> dict` compact: steps with id/t0/t1/kind/title/fields/commands (items only by id+text).
- `timeline.query(tl, questions:list[str]) -> list[answers]` (ids, text, box, t) — e.g. "s5.pusch_snr", "stable time for dl_bitrate".
- `timeline.locate(tl, items:list[{step?, text}]) -> list[{text, found, id, box, t}]`
- `docs.make_doc(tl, doc: dict, formats=["md","docx","pdf","confluence"], out_dir) -> {paths, pages, warnings}`
  `doc` = {title, summary, steps:[{step:"s1", heading, body, highlight:["s1.ntn"], crop:"auto"|[x0,y0,x1,y1]|null}], sections?}
  Screenshot per step = stable frame, cropped + highlight boxes drawn; commands rendered as code blocks.
- Video: existing `planner/render` accept item refs: box `{"type":"box","item":"s5.pusch_snr"}` resolves rect+time from the timeline.
  `render(..., encoder="auto")` uses `h264_nvenc` when available (probe `ffmpeg -encoders`), else libx264.
- `lessons.py` (SQLite at `<home>/lessons.db`): `log_change(job, target, before, after, why, who, trigger)`,
  `get_lessons(context, limit=8) -> ≤800 tokens`, `report_gap(tool, need, example)`, promote repeats (3×) to candidates.

## Worker HTTP API (FastAPI, `casefile/worker/`), every request needs header `Authorization: Bearer <token>`
- `GET /status` -> {version, gpu:{name,vram_total,vram_free}, ram_free, queue, backend_caps:{nvenc,cuda_ocr,whisper}}
- `HEAD /blobs/{sha256}` 200/404 ; `PUT /blobs/{sha256}?offset=N` chunked resumable upload (verify sha at end)
- `POST /jobs` {kind:"analyze"|"render"|"doc"|"tts", args} -> {job_id}   (args reference blobs by sha256)
- `GET /jobs/{id}?wait=50` -> {state:"queued"|"running"|"done"|"error", progress, result|error}  (long-poll ≤ 50 s)
- `GET /files/{job_id}/{name}` download an output
One job at a time; SQLite queue; load a model, run the batch, unload (VRAM 4 GB / RAM 8 GB box). Log peak RAM/VRAM per job.

## MCP tools v2 (casefile/mcp_server.py) — each returns within ~5 s or a job id
analyze_recording(video, focus?) · query_timeline(video, questions[]) · locate(video, items[]) ·
make_video(spec) · make_doc(video, doc, formats?) · patch(path, ops[], why) · verify(path) ·
wait_job(job_id, max_wait=50) · worker_status() · look(video, times, crop?) · get_lessons(context) · log_change(...) · report_gap(...)
Legacy 9 tools stay available for local use. Total tool descriptions+schemas ≤ 10k chars.
Rules: no coordinates required from Claude (item ids), compact one-line-per-item answers with "N more in file",
retries inside the server, errors as {error, hint}.
