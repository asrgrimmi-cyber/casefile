# Casefile reference (details behind SKILL.md)

Sections: v2 tool details · item refs · patch ops · verify · backends · legacy frame-tool workflow (v1) · spec cheat-sheet · lexicon · charts · timing · review checklist · errors.

## v2 tools (answers are compact; every error is `{error, hint}`)

| tool | returns | budget |
|---|---|---|
| `analyze_recording(video, focus?)` | inline summary when cached / done within 4 s, else `{job_id, backend, state}` | ≤ 3k tok |
| `wait_job(job_id, max_wait=50)` | `{state, progress, elapsed}` while running; on done: analyze → `{steps, titles}`, render → `{path, srt, duration, frames, size_mb}`, doc → `{paths, pages, steps}`; `warnings` ≤ 3 | ≤ 400 tok |
| `query_timeline(video, questions[])` | one line per answer `q \| id \| text \| x0,y0,x1,y1 \| t`; overflow → `N more in file <path>` | ≤ 500 tok |
| `locate(video, items[])` | `text \| Y/N \| id \| box \| t`; items are strings or `{step, text}` | ≤ 500 tok |
| `make_video(spec, preview?, out?)` | `{job_id, backend, out}`; spec validated before the job starts (errors come back at once) | — |
| `make_doc(video, doc, formats?, out?)` | `{job_id, backend}`; formats md, docx, pdf, confluence (default all) | — |
| `patch(path, ops[], why)` | `{ok, changed, logged, total, timing:"estimate", shots:"i:T0+dur ..."}` | ≤ 200 tok |
| `verify(path)` | `{pass, fail, lines:["PASS s1.o0 t=3.2 want='NTN' got='NTN' (1.0)"], crops?}` + ≤ 2 crop images | ≤ 400 tok |
| `worker_status()` | `{backend:"gpu"\|"local", gpu, vram_free, ram_free, queue, caps}` | ≤ 150 tok |
| `look(video, times, crop?)` | one contact-sheet image. Last resort | 1 image |
| `get_lessons(context)` / `log_change(target, before, after, why)` / `report_gap(tool, need, example?)` | lessons text / ids | — |

Outputs default to `~/Downloads/casefile/<name>/` (or `<CASEFILE_WORKDIR>/casefile_out/<name>/` in a sandbox) unless
you pass `out`. Paths in results are always local (worker outputs are downloaded).

## Item refs (video specs)

`{"type":"box","item":"s5.pusch_snr","label":"SNR"}`: the server fills `rect` from the timeline item. With no
`from`/`to`, the box shows while the shot plays that step (`[t0, t1]` of the step, mapped through the shot's `src`,
speed-ups included). Give `from`/`to` to override. Works on `box` and `blur`. Unknown ids fail at `make_video`
with suggestions. The expected text for `verify` is the item text.

## Doc JSON (make_doc)

```json
{"title": "TC-017 UE attach over NTN", "summary": "One paragraph: what was tested, result.",
 "steps": [{"step": "s1", "heading": "Open the NTN tab", "body": "Short **markdown** text.",
            "highlight": ["s1.ntn", {"item": "s1.n256", "label": "Band"}], "crop": "auto"}]}
```
Use `sections: [{heading, steps:[...]}]` for longer docs. Terminal steps render commands as code blocks. Private items
(emails, IPs, hostnames, IMSI) are blurred automatically.

## patch ops

`[{op:"set", path:"/shots/3/overlays/1/label", value:"SNR"}, {op:"del", path:"/shots/2/overlays/0"},
{op:"add", path:"/shots/1/overlays/-", value:{...}}]`. JSON-pointer paths, 0-based, `-` appends, `~1` escapes `/`.
`why` is mandatory; each change is logged to the lessons DB (3 similar fixes → a lesson candidate). A spec patch
returns the new plan timing (word-rate estimate); a doc patch returns the step count.

## verify

Video: for each `box`, the frame at the middle of its visible window in the OUTPUT is OCR'd inside the box and
fuzzy-compared with the item text (or `find.query` / label). Needs the `<name>.checks.json` that `make_video`
writes. Doc: each highlight is OCR'd inside the step image. `FAIL` lines give what was read; up to 2 crops of failures
are returned as images. Fix with `patch` (move/resize the box, change `from`/`to`), re-render, re-verify.

## Backends

`CASEFILE_WORKER_URL` + `CASEFILE_WORKER_TOKEN` (or `~/.casefile_worker_token`) point at the GPU worker. The MCP
checks `/status` (2 s timeout, cached 30 s). Reachable → `backend:"gpu"`: the recording is uploaded once (sha256
content address, HEAD dedupe, 8 MB resumable chunks), the job runs on the worker, outputs are downloaded. Unreachable,
or it fails before the job is accepted → the same job runs in-process (`backend:"local"`, warning "ran locally").
Either way the MCP call returns at once with a job id. Job ids live for one MCP session.

# Legacy frame-tool workflow (v1)

## What to ask the user first

Ask briefly, and only for what you can't infer. One message is enough:

- **Topic**: what was being tested (test-case ID or name, feature, expected result).
- **Must-show values**: the fields, numbers or messages that prove the result (e.g. "status = PASSED", "UE count 1000").
- **What to hide**: hostnames, IPs, emails, keys, customer names, private menus or tabs. These become `blur` overlays or cuts.
- **Length and audience**: e.g. "about 1 minute for the team" or "3 minutes, step by step, for a new tester".
- Optional: brand (logo path, accent colour) and whether narration is wanted or captions only (`voice.engine: "none"`).

## Budgets (hard)

- **≤ 35 tool steps** for a whole walkthrough (typical: 1 probe, 10–20 locate calls, 3–6 plan/render calls).
- **≤ 10 image views** in total. Use `contact_sheet` only when OCR can't answer. Examples: you
  don't know what a screen shows yet, or you need to judge layout or a visual state. One sheet with n=6–9 frames
  beats six single frames.
- Batch independent `find_text` calls in a single turn.

## Workflow

1. **probe(video)**. Returns duration, fps, size, scenes, `bad_frames` (black/glitch/popup), `popups`, `layout_changes`.
   Scene and layout-change times are candidate shot boundaries (scene detection over-reports on live-updating charts).
   `"bad_frames": "auto"` in the spec repairs bad frames for you.
2. **Understand the screens.** Around each scene time, call `ocr_region(video, t)` to learn what is on screen. On
   large (1440p+) recordings pass a `region`, because full-frame OCR is noisy. If the text alone is ambiguous, make one
   `contact_sheet(t0, t1, n=6)`.
3. **Locate highlights.** Use `find_text(video, t, "Band", row=true)` to get `box` (the word) and `row_box` (the whole
   form row). Use row boxes for highlights. For scrolling logs and terminals, use `track_text(video, "SIB19", t0, t1)`
   to get the spans where the text is visible and whether it moved. Pick a `t` inside a span where `moved=false`.
4. **Read charts.** Call `chart_extract(video, t, plot_region, series=[{name, rgb, tol}], x_ticks, y_ticks)` to get
   min/max/zero-crossings in axis units. Take the tick pixel positions from `ocr_region` on the axis labels.
5. **Write the spec.** Keep narration in `lines` and visuals in `shots` (see the cheat-sheet). Save it as `edit.json`
   in the workdir. Narrate what was done and what the result was: it is documentation, not marketing.
6. **plan(spec).** Returns the total, per-shot T0/dur, line times and warnings. Use `synth=false` for a fast estimate
   while iterating. The final plan must use real TTS (the default) because line durations drive all timing.
7. **Re-check moving text at the exact shot times.** For every highlight on a terminal, log or any area that scrolls,
   convert the overlay's visible window to source time (from the plan's line times and the shot's `src`), and re-run
   `find_text` at those exact times. A terminal that scrolls mid-shot moves text out from under a box placed at the
   shot's start. This was the main error in real use.
8. **render(spec, preview=true).** Renders 960x540 @ 15 fps and returns 4 thumbnails. Look at 2–4 of them, no more.
9. **Fix** the spec using the review checklist. Re-plan if timing changed.
10. **render(spec).** Final 1920x1080 @ 30 fps H.264/AAC, plus `.srt` and thumbnails. Report the path, duration and size.

## Spec cheat-sheet

```json
{
  "source": "tc-017.mp4",
  "output": {"path": "out/tc-017_walkthrough.mp4", "size": [1920, 1080], "fps": 30},
  "brand": {"logo": null, "accent": "#4F46E5", "ink": "#111827"},
  "voice": {"engine": "kokoro", "voice": "af_heart", "speed": 1.1},
  "bad_frames": "auto",
  "lexicon": {"Koffset": "K offset"},
  "lines": {
    "intro": {"caption": "On-screen caption", "say": "Optional different spoken text"},
    "c1":    {"caption": "Band n256 is selected"}
  },
  "shots": [
    {"card": "intro", "title": ["Line one", "Line two (accent)"], "subtitle": ["Test case TC-017", "sub 2"],
     "footer": "small print", "lines": ["intro"], "chapter": "INTRO", "tail": 1.0},
    {"src": [[0.7, 6.3], [10, 20, 2.0]], "hold": 20, "lines": ["c1", ["c2", 5.3]], "chapter": "01 · CELL",
     "xfade": 0.8, "lead": 0.5, "tail": 0.6, "gap": 0.35,
     "camera": [["s:c1-0.3", "full"], ["s:c1+0.9", [90, 300, 1000, 505]], ["end-0.1", "full"]],
     "overlays": [{"type": "box", "rect": [190, 455, 590, 491], "label": "Band n256",
                   "label_side": "right", "from": "s:c1", "to": "e:c1+0.3"},
                  {"type": "blur", "rect": [20, 10, 420, 40]}]},
    {"card": "outro", "title": "What this test shows", "bullets": ["point 1", "point 2"],
     "lines": [["outro", 1.6]], "chapter": "SUMMARY", "xfade": 1.0, "tail": 2.2}
  ]
}
```

- `src`: `[t0, t1]` or `[t0, t1, speed]` in **source seconds**. `hold` freezes on that source time after the segments, while
  narration continues. A shot with only `hold` is a freeze-frame shot. Cut private moments by leaving them out of `src`.
- `lines`: `"id"`, or `["id", at]` to start no earlier than local second `at`. Lines play in order, separated by `gap`.
- Shot duration = max(played source length, last line end + `tail`). Cards are at least 3 s.
- `chapter` is inherited when omitted. It sets the accent chip and the progress-bar ticks.
- `camera`: keyframes `[TimeRef, "full" | [x, y, w, h]]` in source px. The renderer eases between them and locks the aspect.
- All rects are **`[x0, y0, x1, y1]` corners in source px** (camera rects are `[x, y, w, h]`).
- `brand.logo: null` draws a small "Casefile" wordmark on cards. Set a PNG path to use the user's logo.

### Time refs (local to the shot)

| Ref | Meaning |
|---|---|
| `2.5` | 2.5 s after the shot starts |
| `"s:c1+0.3"` | start of line `c1` + 0.3 s |
| `"e:c1-0.2"` | end of line `c1` − 0.2 s |
| `"end-0.5"` / `"end"` | shot end − 0.5 s / shot end |

Refs can only name lines spoken **in the same shot**. `from` must be before `to` and before the shot end.

### Overlay types

| type | key fields | use |
|---|---|---|
| `box` | `rect` or `find{t,query,row,region,index}`, `label`, `label_side` auto/above/below/left/right, `pad`, `spotlight` | highlight a UI element (dims the rest) |
| `title` | `text` | big centred banner ("▶ Start the test case") |
| `badge` | `text`, `corner` tr/tl/br/bl | small chip, e.g. "2× speed" on sped-up segments |
| `tiles` | `items[{key,value,sub,at}]`, `y` | big-number cards for log values |
| `phases` | `plot` [x0,y0,x1,y1], `bands` [[x0,x1,"LABEL","#hex"]], `stagger` | shade chart regions (rising/peak/falling) |
| `dots` | `points` [[[x,y],"label","side"]], `color`, `stagger` | mark chart points (min/max/zero) |
| `vline` | `x`, `y0`, `y1`, `label`, `color` | dashed marker. `label` draws a bottom banner |
| `arrow` | `start`, `end`, `label`, `color` | point from A to B |
| `blur` | `rect` or `find`, `radius` | hide secrets (hostnames, IPs, emails, keys, IMSI) |

`find` resolves through OCR at plan time. It is convenient, but an explicit `rect` taken from `find_text` output is more
deterministic. Prefer `rect` in final specs.

## Pronunciation lexicon

The built-in defaults are merged under the spec's `lexicon` (the spec wins). Replacement is whole-word and case-sensitive.
The defaults cover common telecom/3GPP terms: `n256`→"n two five six", `UE`→"U E", `UEs`→"U Es", `TA`→"T-A",
`ARFCN`→"A-R-F-C-N", `SIB19`→"SIB nineteen", `AS release`→"A.S. release", `NR`, `RRC`, `NAS`→"nass", `PHY`→"fye", `GNSS`,
`LEO`→"leo", `DL`→"downlink", `UL`→"uplink".

- For a new term, add `"lexicon": {"Koffset": "K offset"}`. Multi-word keys work.
- For anything tricky (numbers with units, symbols such as ≈ → µ −), put a `say` on that line instead of fighting the lexicon.
  Captions can stay precise ("latency = 12.48 ms (≈ p95)") while `say` stays speakable.
- To check one word, call `tts([{id:"t", text:"..."}])`. Only the duration comes back. Listen in the preview.

## Charts

- `tol` is the RGB distance for the series colour. H.264 chroma smearing needs **`tol` 50–60** (the default of 40 misses pixels
  on real recordings). If the returned points look noisy or jump to gridlines, lower it. If they come back empty, raise it.
- Use 2+ ticks per axis, as far apart as possible. The axis maps linearly.
- Before you narrate, sanity-check the result against the axis labels you OCR'd.

## Render timing

- TTS is cached per line (hash of spoken text + voice + speed). Re-plans and re-renders only synthesise edited lines.
- Kokoro fp32 runs at about 1× realtime on CPU. The first plan of a 2–3 min narration takes about 1–2 min, later plans take seconds.
- Preview (960x540@15) is much cheaper than the final render. The final 1080p render runs at roughly 16–22 fps on 8 cores.
- The first `probe` of a 1440p recording can take ~3 min (frame extraction, cached afterwards). `track_text` costs ~2 s
  per sampled frame at 1440p, so use a `region` and a coarse `step`.
- Estimate first: `plan(spec, synth=false)` uses 2.6 words/s. Real durations usually come out slightly shorter at speed 1.1.

## Review checklist (preview thumbs + plan)

- [ ] Every highlight box sits on the right element **at the moment it is shown**. For terminals/logs, re-run `find_text`
  at the exact source times of the overlay (step 7), not only at the shot start.
- [ ] Labels don't cover the thing they describe. Use `label_side` to move them.
- [ ] Captions match what's on screen (numbers, units, names). Captions quote the UI, `say` explains it.
- [ ] Everything the user asked to hide is blurred or cut, in every shot where it appears.
- [ ] The must-show values are on screen, highlighted and named in a caption.
- [ ] Pronunciation: acronyms, IDs and numbers (add lexicon entries or `say`).
- [ ] No unwanted badges or titles. Only add a `badge` when it states a fact, like a speed-up.
- [ ] The intro/outro logo (if any) is complete and not cropped. Title, subtitle and bullets fit on one line each (keep bullets ≤ ~75 chars).
- [ ] No bad frames (black/popup) are visible. The plan's `remap` covers them.
- [ ] Plan warnings are empty or understood. Caption overlap across a crossfade is fine only when intended.
- [ ] The total length matches what the user asked for.

## Common errors → fixes

| error | fix |
|---|---|
| `invalid spec: shots[3].overlays[0].to: ...` | the path points at the bad field. Check field names in the cheat-sheet (`from`/`to`, not `start`/`end`) |
| `unknown line id X in shots[i].lines` | define `lines.X`, or fix the typo |
| `line 'X' is not spoken in this shot` | time refs can only use lines from the same shot. Use a number or `end-x` |
| `rect [...] is outside the WxH source frame` | you used output px or `[x,y,w,h]`. Rects are source-px corners |
| `from (a) is at/after the shot end` | the overlay starts too late. Shorten the offset or move it to the next shot |
| `find: no OCR match for 'Q'` | run `find_text` at that `t`, fix the query/`t`/region, or paste the `rect` |
| `spec.source missing but shots[i] is not a card` | set `source` (relative paths resolve against CASEFILE_WORKDIR) |
| `path outside CASEFILE_WORKDIR` | put the file under the workdir, or use a path relative to it |
| `xfade is longer than the shot` | reduce `xfade` below both shots' durations |
| warning `first line overlaps the previous shot's last line` | raise the `lead` of the incoming shot, or reduce `xfade` |
| warning `camera rect partly outside` | shift x/y so that x+w ≤ W and y+h ≤ H |
| highlight is right but late or early | adjust `from`/`to` offsets (`s:id+0.3`). Don't move lines |
| highlight drifts off the text mid-shot | the content scrolled. Re-locate at the exact times (step 7), split the box into two, or shorten `from`/`to` |
| TTS says "you ee" / reads symbols | add a lexicon entry or a `say` |
