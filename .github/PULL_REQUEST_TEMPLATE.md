## What and why

## How it was tested
- [ ] `pytest -q -m "not slow"` passes
- [ ] `pytest -q` (full, with models) passes, or not relevant
- [ ] `ruff check .` passes
- [ ] Golden frames unchanged, **or** regenerated on purpose and explained below
- [ ] New/changed tool: response-budget test added/updated, `docs/tools.md` updated

## Numbers
Before/after for anything that affects accuracy, speed or response size.

## Checklist
- [ ] Logic lives in the core; `api.py` / `mcp_server.py` / `cli.py` stay thin
- [ ] `docs/spec.md`, `docs/CONTRACT.md` and the skill updated if behaviour changed
- [ ] `CHANGELOG.md` updated
- [ ] No recordings, models, outputs or private data committed
