# How Casefile was built

Casefile was built largely by AI agents: one orchestrator agent planned the work, split it into phases and delegated
each phase to sub-agents that ran in parallel where they could. A human set the goal, reviewed outputs at the gates and
supplied the real recordings. This page describes the process, including what went wrong.

## The shape of the work

| Phase | Work | Gate before moving on |
|---|---|---|
| 0 | Scaffold: package layout, config, fonts, media helpers, spec models, the synthetic fixture, and `docs/CONTRACT.md` | Fixture builds, spec models import |
| 1 | Detect: `probe`, bad frames, scenes, popups, layout changes | Bad frames exact on the fixture (3 black + 1 popup) |
| 2 | OCR + chart: `find_text`, `ocr_region`, `track_text`, `contact_sheet`, `chart_extract` | Labels within ±4 px; chart min / zero crossing within tolerance |
| 3 | TTS: Kokoro narration, lexicon, caching, narration mix | Durations stable, cache hits on re-run |
| 4 | Planner: spec → timeline, TimeRefs, validation errors and warnings | Exact expected timeline for `examples/fixture.json` |
| 5 | Renderer: cards, overlays, captions, camera, encode + mux, SRT | Golden frames match; exact frame count |
| 5b | Speed: frame reuse, parallel segments | Faster render without changing pixels |
| 6 | CLI + MCP: `api.py`, thin wrappers, response-size budgets | Every tool under its budget; errors are `{error, hint}` |
| 7 | Skill + docs: agent workflow, budgets, checklist | Skill used end to end on a real recording |

Phases 1, 2 and 3 were independent and ran in parallel. The planner (4) needed their interfaces but not their
implementations. The renderer (5) needed the planner's `Plan` shape.

## The contract made parallel work possible

Before any feature code, Phase 0 wrote [CONTRACT.md](CONTRACT.md). It gives every module's public functions, their
exact return shapes, the coordinate and time conventions (source pixels, seconds, 2-decimal rounding) and the error
convention (`ToolError(error, hint)`). Each sub-agent got the contract and its own module, plus the rule "do not
rewrite shared modules; extend only if essential and say so".

This worked. Parallel agents rarely conflicted, and when they did it was because the contract was ambiguous (for
example, whether overlay times are resolved before rendering). The fix was to tighten the contract, not the code.

## Test gates

Every phase had to pass its tests against the synthetic fixture before the orchestrator moved on. Because the fixture
has exact ground truth, the gates could be numeric ("within 1 px", "exact frame count") instead of "looks right".
Golden frames catch unintended visual changes in the renderer. Response-size tests keep the MCP tools cheap as
features are added.

## What went wrong

- **The int8 TTS model was slower, not faster.** The obvious choice for CPU was the quantised Kokoro model. Measured,
  it ran at 0.33× realtime against 1.09× for fp32, because dequantisation is slow on x86 ONNX Runtime. fp32 became
  the default. Lesson: measure on the target hardware before picking the "optimised" option.
- **A speed-up agent timed out.** The Phase 5b agent was given too broad a task (optimise the renderer). It ran out
  of time mid-change. The orchestrator split the work into smaller, separately gated changes (frame reuse first, then
  parallel segments), and those landed. Lesson: give sub-agents one measurable change each.
- **Highlights were misplaced on a real recording.** The first real walkthrough had 3 boxes in the wrong place,
  because a terminal scrolled during the shot and the boxes had been located at the start of the shot. The fixture
  had a scrolling line, but the gate only tested `track_text`, not that the renderer's boxes stayed on moving text.
  Lesson: the skill now says to re-run `find_text` at the exact times a highlight is shown, and a real recording is
  part of the review, not just the fixture.

## Lessons

1. Write the contract first. It is the cheapest way to let agents work in parallel.
2. Make gates numeric and ground-truthed. "Looks fine" is not a gate an agent can check.
3. Keep tools text-first and budgeted, and test the budgets, so the product stays cheap to drive.
4. Keep wrappers thin (one core, CLI + MCP), so every agent-visible behaviour can be reproduced from a shell.
5. Synthetic fixtures catch regressions. Real recordings catch the problems you did not think of. You need both.
