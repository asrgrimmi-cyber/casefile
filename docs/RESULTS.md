# How well it works

These are measured numbers, not targets. Where something is weak, this page says so. Unless noted, measurements were
taken on one 8-core x86 Windows 11 laptop.

## Fixture gates (synthetic ground truth)

`tests/fixtures/make_fixture.py` generates a 1280x720, 20 s, 30 fps video with known ground truth: a form page with
10 labelled rows, a scrolling log line, a page switch at 10 s, a chart page with two series of known shape, 3 black
frames and 1 popup frame. The test suite checks the tools against that truth.

| Gate | Target | Result |
|---|---|---|
| Bad-frame detection | exact | **exact**: 3 black + 1 popup found, no false positives |
| `find_text` label boxes | within ±4 px | **10/10 labels within 1 px** |
| `find_text` row boxes | – | **within 2 px** |
| `chart_extract` minimum of the parabola | [42, −30] | **[42.0, −29.9]** |
| `chart_extract` zero crossing of the line | 63 | **63.2** (0.2% of the x range) |

A synthetic fixture is the easy case: clean fonts, no compression artefacts beyond one H.264 encode, no anti-aliased
dashboards. Real recordings are noisier (see below).

## Response sizes (what the assistant pays)

Measured on the fixture, in characters of JSON returned (≈ tokens × 4):

| Tool | chars |
|---|---|
| `probe` | 303 |
| `find_text` | 96 |
| `ocr_region` (whole frame) | 898 |
| `track_text` | 116 |
| `chart_extract` | 517 |
| `tts` (1 line) | 35 |
| `plan` (`examples/fixture.json`) | 433 |
| `render` (preview) | 829 |

All 9 tool descriptions plus input schemas: **5,783 chars (~1,450 tokens)**. Budgets are enforced by `tests/test_mcp.py`.

Sizes that contain file paths (`render`, `plan`) vary slightly with the length of the checkout path. In a checkout at
a different path, `render` measured 901 and `plan` 436.

## Speed

| Step | Measured |
|---|---|
| Kokoro TTS, fp32 model | **1.09× realtime** on CPU (1 s of speech in about 0.9 s) |
| Kokoro TTS, int8 model | **0.33× realtime**, about 3× slower on x86 ONNX Runtime, so fp32 is the default |
| Final render, 1080p | **~16–22 fps** on 8 cores; a 77 s video rendered in **~2 min 10 s** |
| First `probe` of a 1440p recording | **~3 min** (one-off frame extraction; later calls hit the cache) |
| `track_text` at 1440p | **~2 s per sampled frame** |

TTS is cached per line, so after the first plan only edited lines are re-synthesised.

## Real-recording case study (anonymised)

- **Input:** a 2:35 screen recording at 2560x1440 of a web dashboard plus a terminal, from a real test session.
- **Output:** a 1:17 narrated walkthrough with 12 narration lines and 6 shots. Hostnames were blurred, and a private
  browser menu that appeared mid-recording was cut.
- **First-pass review:** 3 highlights were misplaced. The terminal had scrolled during the shot, so the text the
  box was placed on at one moment had moved by the time the box appeared.
- **Fix:** re-run `find_text` at the exact local times each highlight is visible (not just at the start of the shot),
  or use `track_text` to pick a moment where the text does not move. The skill now makes this a checklist item.

This is one recording. It shows the workflow works end to end on real footage. It is not evidence of reliability
across many kinds of recordings.

## Known weaknesses

- **First probe of 1440p is slow** (~3 min), because every frame is extracted once. It is cached afterwards.
- **`track_text` is slow at 1440p** (~2 s per sampled frame). Use a `region` and a coarser `step`.
- **Scene detection over-reports** on screens with live-updating charts or counters. Treat `scenes` as candidates.
- **OCR is noisy on full-screen 1440p frames.** Pass a `region` around the panel you care about.
- **Tested only on Windows 11** so far. CI runs the fast test suite on Ubuntu and Windows. macOS has not been tried.
- **Not validated at scale.** Results so far come from the synthetic fixture and a small number of real recordings.
