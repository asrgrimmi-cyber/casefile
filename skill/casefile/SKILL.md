---
name: casefile
description: Use when turning a screen recording of a test session into documentation with the Casefile MCP tools. Analyse the recording once into a timeline, then build a written doc (md/docx/pdf/confluence) and/or a narrated walkthrough video from item ids, verify, patch.
---

# Casefile: document a recorded test session

You turn a screen recording (test run, bug repro, config session) into documentation: a written step-by-step doc
and/or a narrated, highlighted 1080p video. You do **not** watch the video. The server analyses it once into a
**timeline** (steps, UI items with ids like `s5.pusch_snr`, fields, commands) and you build everything from item
ids. You never need coordinates. Details: [REFERENCE.md](REFERENCE.md).

## Ask first (one short message, only what you can't infer)

Topic/test-case, must-show values, what to hide, output (doc formats, video, length, audience), brand.

## Workflow (target ≤ 8 calls)

1. `get_lessons(context)`: approved rules from earlier corrections (skip if none apply).
2. `analyze_recording(video, focus?)`: returns the step summary if cached, else `{job_id}`.
3. `wait_job(job_id)`: long-polls ≤ 50 s; repeat only while `state` is `running`. Then call
   `analyze_recording` again for the summary (instant; cached).
4. `query_timeline(video, [...])` / `locate(video, [...])`: batch every question in **one** call. Answers are
   `q | id | text | box | t`; take the `id`.
5. Build outputs (both can run; each returns a job id at once):
   - `make_doc(video, {title, summary, steps:[{step, heading, body, highlight:[ids]}]}, formats?)`
   - `make_video(spec)`: normal spec, but boxes/blurs use `{"type":"box","item":"s5.pusch_snr","label":"SNR"}`.
     `from`/`to` default to when the shot shows that step. Write the spec to a `.json` file so you can patch it.
6. `wait_job(job_id)` for each: local paths, duration/pages, warnings.
7. `verify(path)` on the .mp4 and/or doc: OCRs every highlight in the output. All `PASS` → done.
8. On `FAIL`: `patch(path, ops, why)` (e.g. `{op:"set", path:"/shots/2/overlays/0/item", value:"s3.cqi"}`),
   re-run make_* + wait_job + verify. `why` is required and is how lessons are learnt.

Report the output paths, duration/pages and anything that failed verification.

## Rules

- Never guess coordinates or times: use ids from `query_timeline`/`locate`. An unknown id errors with suggestions.
- `backend` in replies is `gpu` (remote worker) or `local` (CPU fallback). Both behave the same, local is slower.
  `worker_status()` only when the user asks or jobs look stuck.
- `look(video, times)` returns an image. Use it only when the timeline can't answer (≤ 2 per job).
- Errors are `{error, hint}`. Follow the hint; don't retry the same arguments.
- Something missing? `report_gap(tool, need, example)`. Manual fix outside `patch`? `log_change(...)`.
- Legacy frame tools (`probe`, `find_text`, `ocr_region`, `track_text`, `chart_extract`, `contact_sheet`,
  `tts`, `plan`, `render`) still work locally for charts or edge cases; see REFERENCE.md (v1 workflow, spec
  cheat-sheet, overlays, lexicon, review checklist, error table).

## Writing

It is documentation, not marketing: say what was done and what the result was, quoting on-screen values exactly.
Captions quote the UI; use `say` (spoken text) for acronyms/numbers. Blur everything the user asked to hide;
the timeline already flags emails, IPs, hostnames and IMSIs as private.
