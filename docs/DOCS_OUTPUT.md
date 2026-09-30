# Written docs output (`casefile/docs.py`)

`docs.make_doc(tl, doc, formats=None, out_dir=None, brand=None, *, privacy=True, generated=None, base_dir=None)`
turns an analysed recording (`timeline.json`, schema in [CONTRACT_V2.md](CONTRACT_V2.md)) plus a short outline
into step-by-step documentation. No network calls; output is deterministic (byte-identical Markdown,
Confluence XHTML, PDF and PNGs for the same input) unless you pass `generated="2026-09-30"`, which prints that date.

## Input

```jsonc
{
  "title": "Checking an NTN UE attach",          // required; slug = folder + file name
  "summary": "Markdown-lite text",
  "audience": "Test engineers",                   // optional
  "sections": [{"heading": "Web GUI", "steps": [ /* step objects */ ]}],   // or a flat "steps": [...]
  "appendix": "commands" | "fields" | "both"      // optional
}
```

Step object:

| key | meaning |
|---|---|
| `step` | timeline step id (`"s1"`); unknown -> warning, step rendered as text only |
| `heading` | step heading (defaults to the timeline title) |
| `body` | markdown-lite: `**bold**`, `` `code` ``, `- bullets`, `1. numbered`, fenced ```` ``` ```` code |
| `highlight` | item ids (`"s1.ntn"`) or `{"item": "s1.n256", "label": "Band"}`; unknown / other-step ids -> warning |
| `crop` | `"auto"` (default): union of highlight boxes padded 12% of the frame width, ≥800 px wide; `[x0,y0,x1,y1]` source px; `null` = full frame |
| `command` | include the step's commands as a code block (default: only for `kind == "terminal"`) |

## Image pipeline (per step)

1. Stable frame `steps/sN.png` (relative to the timeline folder, or `base_dir`).
2. **Privacy (default on)**: every `private[].box` of the step is Gaussian-blurred before cropping; emails,
   `user@host`, non-loopback IPv4 and 15-digit IMSIs in commands/transcripts/fields text are masked
   (`[email]`, `[user@host]`, `[ip]`, `[imsi]`). `privacy=False` disables both.
3. Crop (see above), then downscale to ≤1600 px wide.
4. Highlights: rounded accent rectangle (brand `accent`, default `#4F46E5`) + numbered badge at the top-left
   corner + optional label chip. The legend under the caption maps numbers to item text/labels.

Terminal steps also get an **output excerpt**: transcript lines that contain a highlighted item's text.

## Output (`out_dir/<slug>/`, default `CASEFILE_HOME/docs`)

| file | content |
|---|---|
| `<slug>.md` | GitHub Markdown; images referenced as `images/step-NN.png` |
| `images/step-NN.png` | processed screenshots (one per step with a screenshot) |
| `<slug>.docx` | python-docx: title, summary, `N. heading` (Heading 2; sections are Heading 1), 6.3 in images, captions, `Casefile Code` monospace style, grid tables |
| `<slug>.pdf` | reportlab platypus, A4, DejaVu fonts, page numbers in the footer, embedded images |
| `<slug>.confluence.xhtml` | Confluence **storage format** body: `<ac:image><ri:attachment ri:filename="step-NN.png"/></ac:image>`, `code` macros |
| `<slug>.attachments.json` | `{title, body, representation:"storage", attachments:[images/...]}` for a later uploader |

The XHTML is a fragment with `ac:`/`ri:` prefixes; wrap it in an element declaring those namespaces to
parse it with a generic XML parser (the tests do this).

Return value: `{paths:{md,docx,pdf,confluence}, dir, pages, steps, warnings, images, attachments}`.

## Adding a format

All formats render from one intermediate `DocModel` built by `_build()` (steps, parsed body blocks,
processed images, captions, legend, commands, excerpt, appendix tables). A new renderer only walks that
model: add `render_<fmt>(model, out)` and register it in `FORMATS` + `make_doc`. Keep it deterministic
(no clocks, sorted keys) and add a case to `tests/test_docs.py`, which builds its fixture from
`tests/fixtures/timeline_sample/` (`make_sample.build(dir)` draws the screenshots from the item boxes).

## Lessons (`casefile/lessons.py`)

Corrections logged with `log_change(job, target, before, after, why)` (why required) are grouped by `kind`
or by normalised target (`s12.phr_box` -> `*.phr_box`); `promote()` turns 3+ into candidate lessons; a
human `approve(id)`s them and `get_lessons(context)` serves the relevant approved rules (≤3200 chars) to
the next job. Kinds `lexicon`, `ocr_fix`, `box_pad`, `speed_default` carry an action read by
`active_actions(kind)`. `render_review_html(path)` writes a static review page of candidates and open gaps.
