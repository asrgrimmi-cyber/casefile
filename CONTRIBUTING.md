# Contributing to Casefile

Thanks for helping. Bug reports, recordings that break the tools (anonymised) and small focused PRs are all welcome.

## Dev setup

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), ffmpeg 6+ and Tesseract 5 (see the README).

```bash
git clone https://github.com/asrgrimmi-cyber/casefile && cd casefile
uv venv --python 3.11
uv pip install -e ".[dev]"
.venv/Scripts/casefile doctor      # Windows; .venv/bin/casefile doctor elsewhere. Downloads the Kokoro models (~340 MB)
```

Models go to `./models/` (gitignored) if present, otherwise `~/.casefile/models/`.

## Tests and lint

```bash
.venv/Scripts/python -m pytest -q -m "not slow"   # fast: no TTS model or full render needed (what CI runs)
.venv/Scripts/python -m pytest -q                 # full: needs the Kokoro models, renders video (~2–3 min)
.venv/Scripts/ruff check .
```

The synthetic fixture (`tests/fixtures/make_fixture.py`) is built on the first test run into `tests/fixtures/out/`.
It has exact ground truth, so the gates are numeric (for example "labels within 1 px").

**Golden frames** (`tests/golden/*.png`) guard the renderer's look. If you change the visuals on purpose, delete the
affected PNGs, run `pytest tests/test_render.py` to regenerate them, look at the new images, and say so in the PR.
Never regenerate goldens to make an unexplained diff go away.

## The contract rule: one core, thin wrappers

- All logic lives in the core modules (`casefile/detect.py`, `vision.py`, `chart.py`, `audio.py`, `planner.py`,
  `render.py`, ...).
- `casefile/api.py` has one function per tool. It checks paths against the sandbox, calls the core and returns a
  plain dict. It never raises: user errors become `{"error": ..., "hint": ...}`.
- `casefile/mcp_server.py` and `casefile/cli.py` are wrappers only. They must return **the same JSON** for the same call.
- Core functions raise `ToolError(error, hint)` for user errors. The hint says what to change.
- Coordinates are source pixels `[x0, y0, x1, y1]`, times are seconds (2 decimals), and outputs are JSON-serialisable.
- Never load a whole video into RAM. Use `FrameStore`.
- Never use `shell=True`. Find external tools with `config.tool_path`.

`docs/CONTRACT.md` has the per-module signatures. Update it when you change a public function.

## Adding an overlay type

1. Add a pydantic model in `casefile/spec.py` (subclass `_Ov`, with a `type: Literal["yourtype"]`) and add it to the
   `Overlay` union.
2. If it uses TimeRefs or rects, make sure `planner.py` resolves and validates them (look at how `box`/`blur` are handled).
3. Draw it in `casefile/draw.py`. Author sizes at 1080p and scale by `ctx.s`, map source px through `ctx.xf`/`ctx.xp`,
   and draw into the `OpRecorder` so frame reuse keeps working.
4. Add a render test (a frame at a known time, with an assertion on pixels) and a planner test for validation errors.
5. Document it in `docs/spec.md` and in the skill's overlay table.

## Adding a tool

1. Implement it in a core module and return a small dict.
2. Add a function in `api.py`, then register it in `mcp_server.py` (keep the description short) and `cli.py`.
3. **Add a response-budget test** in `tests/test_mcp.py`: call the tool on the fixture through the MCP server and assert
   the response is under its budget in characters (tokens ≈ chars / 4). Also check that the total size of the tool
   descriptions and schemas stays under 10,000 chars.
4. Document it in `docs/tools.md` (inputs, output shape, budget, measured size) and in the skill if assistants should use it.

## Commit style

- Small, focused commits in the imperative mood: `vision: pad OCR boxes by 1 px`, `render: reuse identical frames`.
  Prefix with the module or area.
- One logical change per PR. Include before/after numbers for anything that affects accuracy, speed or response size.
- Don't commit recordings, rendered videos, models or caches (they are gitignored). Anonymise anything from a real
  session before attaching it to an issue.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
