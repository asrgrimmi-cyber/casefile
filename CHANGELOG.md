# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-30

First public release.

### Added

- MCP server `casefile-mcp` (stdio or `--http`) with 9 text-first tools: `probe`, `find_text`, `ocr_region`,
  `track_text`, `chart_extract`, `contact_sheet`, `tts`, `plan`, `render`.
- CLI `casefile`, which returns the same JSON as the MCP tools, plus `casefile doctor`.
- One-file JSON edit spec: shots, narration lines, TimeRefs, camera keyframes and overlays (`box`, `title`, `badge`,
  `tiles`, `phases`, `dots`, `vline`, `arrow`, `blur`).
- Offline narration with Kokoro TTS (fp32 default), cached per line, with a pronunciation lexicon.
- Renderer: 1080p H.264/AAC with burnt-in captions, chapter chips, progress bar, intro/outro cards, bad-frame repair,
  loudness normalisation and a `.srt`. Preview mode at 960x540 @ 15 fps with 4 thumbnails.
- Configurable brand (`spec.brand`: logo, accent, ink), with neutral indigo defaults and a text wordmark when no logo is set.
- Workdir sandbox (`CASEFILE_WORKDIR`) and a data/cache home (`CASEFILE_HOME`, default `~/.casefile`).
- Agent skill `skill/casefile/SKILL.md` with workflow, budgets, review checklist and error fixes.
- Synthetic ground-truth fixture, golden frames and response-budget tests. CI on Ubuntu and Windows.
