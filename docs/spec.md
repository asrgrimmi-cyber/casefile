# Edit spec reference

A Casefile walkthrough is described by **one JSON file**, the *spec*. `plan` validates it and turns it into an absolute
timeline, and `render` draws it. The authoritative schema is `casefile/spec.py` (pydantic, `extra="forbid"`: unknown keys
are errors). A small runnable example is `examples/fixture.json`, which uses the synthetic test video.

## Example

```json
{
  "source": "recordings/tc-017.mp4",
  "output": {"path": "out/tc-017_walkthrough.mp4", "size": [1920, 1080], "fps": 30},
  "brand": {"logo": null, "accent": "#4F46E5", "ink": "#111827"},
  "voice": {"engine": "kokoro", "voice": "af_heart", "speed": 1.1},
  "bad_frames": "auto",
  "lexicon": {"Koffset": "K offset"},
  "lines": {
    "intro": {"caption": "Test case TC-017: configure a cell and check the statistics",
              "say": "Test case T C seventeen: configure a cell and check the statistics."},
    "c1": {"caption": "Cell configuration: NTN = True"},
    "c2": {"caption": "Band n256 is selected"},
    "d1": {"caption": "The measured value rises, peaks and falls during the run"},
    "outro": {"caption": "The cell came up and the statistics match the expected curve"}
  },
  "shots": [
    {"card": "intro", "title": ["Cell Configuration", "and Statistics"],
     "subtitle": ["Test case TC-017", "Release candidate build"],
     "footer": "Recorded 2026-09-30", "lines": ["intro"], "chapter": "INTRO", "tail": 1.0},

    {"src": [[0.7, 6.3]], "hold": 6.3, "lines": ["c1", "c2"], "chapter": "01 · CELL", "xfade": 0.8,
     "camera": [["s:c1-0.3", "full"], ["s:c1+0.9", [90, 300, 1000, 505]], ["end-0.1", "full"]],
     "overlays": [
       {"type": "box", "rect": [190, 410, 590, 449], "label": "NTN = True", "from": "s:c1", "to": "e:c1+0.3"},
       {"type": "box", "find": {"t": 3.0, "query": "Band", "row": true}, "label": "Band n256",
        "label_side": "right", "from": "s:c2", "to": "e:c2+0.3"},
       {"type": "blur", "rect": [20, 10, 420, 40]}
     ]},

    {"src": [[32.8, 42.6, 2.0]], "hold": 60.2, "lines": ["d1"], "chapter": "02 · STATS",
     "overlays": [
       {"type": "badge", "text": "2× speed", "from": 0.1, "to": 4.8},
       {"type": "phases", "plot": [226, 362, 972, 682], "from": "s:d1+1.0", "to": "end",
        "bands": [[226, 560, "RISING", "#3B82F6"], [560, 720, "PEAK", "#FF8C1E"], [720, 972, "FALLING", "#9650DC"]]},
       {"type": "dots", "points": [[[640, 497], "peak ≈ 1,430", "below"]], "color": "#3B6EE6",
        "from": "s:d1+2", "to": "end"},
       {"type": "vline", "x": 640, "y0": 360, "y1": 682, "label": "peak", "from": "e:d1-1"}
     ]},

    {"card": "outro", "title": "What this test shows",
     "bullets": ["Cell configured on band n256", "Statistics match the expected curve"],
     "lines": [["outro", 1.6]], "chapter": "SUMMARY", "xfade": 1.0, "tail": 2.2}
  ]
}
```

## Units and coordinates

| Thing | Unit |
|---|---|
| `src` segment times, `hold`, `find.t` | **source** seconds |
| overlay `from`/`to`, line `at`, camera times, tile `at` | **local** seconds of the shot (or a TimeRef) |
| `rect`, `plot`, `points`, `x`/`y0`/`y1`, `start`/`end`, `find.region` | **source pixels**, rects as corners `[x0, y0, x1, y1]` |
| camera rect | source pixels `[x, y, w, h]`. The aspect is locked to the content area around the centre |
| `tiles.y` | output px at 1080p (scaled for other sizes) |

## Top level

| Field | Type | Default | Notes |
|---|---|---|---|
| `source` | path | – | The recording. Relative paths resolve against `CASEFILE_WORKDIR`, else the CWD. Required unless every shot is a card |
| `output.path` | path | `out/edit.mp4` | preview renders add `_preview` |
| `output.size` | [W, H] | [1920, 1080] | caption bar = round(111·H/1080) px; content area = the rest |
| `output.fps` | int | 30 | |
| `brand.logo` | path \| null | null | PNG drawn top-left on cards, scaled and never cropped. `null` draws a small "Casefile" text wordmark |
| `brand.logo_dark` | path \| null | null | reserved |
| `brand.accent` / `ink` | hex | `#4F46E5` / `#111827` | accent: boxes, chips, progress bar, card highlights. Ink: card text |
| `brand.font` | str | `Poppins` | bundled |
| `voice.engine` | `kokoro` \| `none` | `kokoro` | `none` = silent, durations estimated |
| `voice.voice` / `speed` | str / float | `af_heart` / 1.1 | |
| `bad_frames` | `"auto"` \| `"none"` \| [int] | `auto` | 0-based source frame numbers. Each is replaced by the nearest earlier good frame |
| `lexicon` | {word: spoken} | {} | merged over the built-in defaults |
| `lines` | {id: Line} | {} | narration + captions |
| `shots` | [Shot] | – | at least one |
| `fade_in` / `fade_out` | s | 0.6 / 0.8 | global fade from/to black |
| `captions` / `srt` | bool | true / true | burn-in captions / write a `.srt` next to the MP4 |

### Line

| Field | Notes |
|---|---|
| `caption` | text shown in the caption bar (exact, may contain symbols such as ≈ µ → −) |
| `say` | spoken text. When omitted, the caption is spoken. The lexicon applies to both |

A caption is shown from 0.15 s before its line starts until 0.5 s after it ends. Unused lines produce a warning.

## Shot

| Field | Default | Notes |
|---|---|---|
| `card` | null | `"intro"` or `"outro"`. Draws a branded card instead of source video |
| `title` | – | intro: list of lines (odd lines in accent colour). Outro: heading string |
| `subtitle` | – | intro: string or list of lines |
| `footer` | – | small grey text |
| `bullets` | – | outro checklist (keep each ≤ ~75 chars at 1080p) |
| `src` | [] | `[[t0, t1], [t0, t1, speed], ...]` played in order |
| `hold` | null | freeze on this source time after the segments (or for the whole shot if `src` is empty) |
| `lines` | [] | `"id"` or `["id", at]` (start no earlier than local `at`) |
| `chapter` | inherited | chip text + progress tick |
| `xfade` | 0 | crossfade from the previous shot (the shots overlap by this much) |
| `lead` | 0.5 | silence before the first line |
| `tail` | 0.6 | time after the last line |
| `gap` | 0.35 | silence between lines |
| `camera` | [] | `[[TimeRef, "full" \| [x,y,w,h]], ...]`. Eased pan/zoom, held before the first and after the last keyframe |
| `overlays` | [] | see below |

**Timing.** `t = lead`. Each line starts at `max(t, at)`, then `t = end + gap`.
`dur = max(Σ (t1−t0)/speed, last_end + tail)`. Cards are at least 3 s.
`T0 = previous end − xfade`. The chapter starts at `T0 + xfade/2`. Line durations come from Kokoro TTS (`synth=true`) or an
estimate of 2.6 words/s × speed + 0.3 s.

## TimeRef

`number` \| `"s:<line>[±x]"` (line start) \| `"e:<line>[±x]"` (line end) \| `"end[±x]"` (shot end).
Refs may only name lines spoken in the same shot. `from` defaults to 0 and `to` to `"end"`.

## Overlays

Common fields: `type`, `from`, `to`. Everything fades in and out over 0.35 s.

| type | fields | draws |
|---|---|---|
| `box` | `rect` \| `find`, `label`, `label_side` (`auto`/`above`/`below`/`left`/`right`), `pad` = 8, `spotlight` = true | rounded accent box with glow and a 1.12→1 pop-in. A label chip. The spotlight dims the rest to 58% |
| `title` | `text` | large dark banner with an accent underline, centred low |
| `badge` | `text`, `corner` (`tr`,`tl`,`br`,`bl`) | small dark chip |
| `tiles` | `items[{key, value, sub, at}]`, `y` | white number cards (key in mono, big value, accent sub-text). `at` staggers them |
| `phases` | `plot` [x0,y0,x1,y1], `bands` [[x0, x1, label, "#hex"?]], `stagger` = 0.9 | translucent vertical bands over the plot's y-range with labels |
| `dots` | `points` [[[x,y], label, side]] or `{xy,label,side}`, `color`, `stagger` = 0.7 | ringed markers with label chips (side: right/left/above/below) |
| `vline` | `x`, `y0`, `y1`, `label`, `color` | dashed vertical marker drawn downward. `label` = banner at the bottom |
| `arrow` | `start` [x,y], `end` [x,y], `label`, `color` | arrow with a label chip at the start |
| `blur` | `rect` \| `find`, `radius` = 12 | Gaussian blur (applied before other overlays). Use for hostnames, emails, keys |

### find

`{"t": source_s, "query": "Band", "row": false, "region": [x0,y0,x1,y1] | null, "index": 0}`. Resolved with OCR at plan
time (`find_text`). `row=true` uses the whole form/table row. `query` may be multi-word or `re:<regex>`. Plans with
`resolve_find=false` skip it (with a warning).

## Validation

**Errors** (`{error, hint}`): schema problems (with a path such as `shots[2].overlays[0].to`), unknown line ids, time refs to
lines not in the shot, `from` after `to`, overlays that start at or after the shot end, box/blur without rect/find, inverted
rects, rects outside the source frame, `find` with no OCR match, missing source, `xfade` ≥ shot length.

**Warnings** (in `plan().warnings`): unused lines, `at` earlier than the previous line allows, captions overlapping across a
crossfade, `from == to`, times clamped to the shot, camera rects partly outside the source, unsorted camera keyframes,
`src`/`hold` beyond the source duration, unresolved `find`.
