# Casefile

**Turn a screen recording of a test session into documentation: a narrated, captioned, highlighted walkthrough video plus a subtitle file, driven by any AI assistant through MCP.**

Casefile runs locally, works offline once the models are downloaded, and is free and open source (MIT).

## The problem

People record a test run, a bug reproduction or a configuration session, and then the recording sits in a folder.
Nobody writes it up. A raw 3-minute capture is hard to review: nothing is labelled, the important value is on screen
for two seconds, and there may be a hostname you shouldn't share.

Casefile lets an AI assistant turn that recording into something a colleague can follow. The result has chapters,
narration, captions, highlight boxes on the fields that matter, blurred secrets and bad frames removed. The assistant
does not watch the video. It reads the video with cheap text tools (OCR, chart extraction) and writes one JSON edit spec.
A local renderer does the rest.

## What works today vs. roadmap

| Capability | Status |
|---|---|
| Narrated, captioned 1080p walkthrough video (H.264/AAC) + `.srt` | ✅ works (v0.1) |
| Highlight boxes, spotlight, blur, camera zoom/pan, chart annotations, intro/outro cards | ✅ works |
| Offline OCR text location (`find_text`, `ocr_region`, `track_text`) and chart reading | ✅ works |
| Offline TTS narration (Kokoro), cached per line | ✅ works |
| Bad-frame repair (black frames, glitches, popups) | ✅ works |
| MCP server (9 tools) + identical CLI | ✅ works |
| Configurable brand (logo, accent, ink colours) | ✅ works |
| Written step-by-step docs (Markdown with screenshots, Confluence export) | 🗺️ planned (v0.2) |
| Transcribing the narrator's own voice from the recording (offline STT) | 🗺️ planned (v0.3) |
| Jira integration (read test-case steps, attach outputs to the issue) | 🗺️ planned (v0.4) |
| Hosted / team tier | 🗺️ planned, not built. The core stays MIT and free |

See [ROADMAP.md](ROADMAP.md).

## 60-second quickstart

**1. System dependencies**

- **ffmpeg 6+** (`ffmpeg` and `ffprobe` on PATH). Windows: `winget install Gyan.FFmpeg`. Debian/Ubuntu: `apt install ffmpeg`. macOS: `brew install ffmpeg`.
- **Tesseract 5**. Windows: `winget install UB-Mannheim.TesseractOCR` (auto-detected in `C:\Program Files\Tesseract-OCR`, so PATH is not needed). Debian/Ubuntu: `apt install tesseract-ocr`. macOS: `brew install tesseract`.

**2. Install Casefile**

Casefile is not on PyPI yet. Install it from a checkout. The distribution name will be `casefile-mcp`.

```bash
git clone https://github.com/asrgrimmi-cyber/casefile && cd casefile
uv tool install .            # puts `casefile` and `casefile-mcp` on PATH
casefile doctor              # checks ffmpeg/tesseract/fonts, downloads the Kokoro TTS model + voices (~340 MB, once)
```

Models and caches live in `~/.casefile/`. Set `CASEFILE_HOME` to move them.

**3. Connect it to your assistant**

*Claude Code*

```bash
claude mcp add casefile -- casefile-mcp
# optional sandbox: only files under this folder can be read or written
claude mcp add casefile -e CASEFILE_WORKDIR="$PWD" -- casefile-mcp
```

*Claude Desktop*: edit `claude_desktop_config.json` (Windows `%APPDATA%\Claude\`, macOS
`~/Library/Application Support/Claude/`) and restart Claude Desktop.

```json
{
  "mcpServers": {
    "casefile": {
      "command": "casefile-mcp",
      "env": {"CASEFILE_WORKDIR": "C:\\Users\\me\\Videos\\tests"}
    }
  }
}
```

If `casefile-mcp` is not on PATH, use the full path, for example `C:\\path\\to\\casefile\\.venv\\Scripts\\casefile-mcp.exe`.

*Hermes Agent*: add this to `config.yaml` in your Hermes home, then run `hermes mcp test casefile`.

```yaml
mcp_servers:
  casefile:
    command: casefile-mcp
    env:
      CASEFILE_WORKDIR: C:/Users/me/Videos/tests
```

*Any other MCP client*: run `casefile-mcp` over stdio, or `casefile-mcp --http 127.0.0.1:8765` for streamable HTTP at `/mcp`.

**4. Give the assistant the skill (recommended)**

[`skill/casefile/SKILL.md`](skill/casefile/SKILL.md) holds the workflow, tool budgets, a spec cheat-sheet and a review
checklist. Install it as a Claude/Hermes skill, or paste it into your project instructions.

## Example prompt

> Use the casefile tools. `recordings/login-test.mp4` is a recording of test case TC-042 (login with an expired
> password). Make a walkthrough of about a minute: intro card, one chapter per screen, highlight the error message
> and the "password expired" status, blur the hostname in the address bar, and end with a summary card. Render a
> preview first and show me the plan.

The assistant will probe the video, locate the fields with OCR, write `edit.json`, plan, render a preview, check it
against the checklist and then render the final MP4 and `.srt`.

## How it works

```
 recording.mp4
      │
      ▼
 ┌──────────┐   scenes, bad frames,     ┌──────────────────────────────┐
 │  probe   │── popups, layout changes ▶│ AI assistant (any MCP client)│
 └──────────┘                           │  reads text, never watches   │
 ┌──────────────────────────────┐       │  the video                   │
 │ find_text / ocr_region /     │◀──────│                              │
 │ track_text / chart_extract / │ boxes │  writes ONE edit spec (JSON) │
 │ contact_sheet (rare, images) │──────▶│                              │
 └──────────────────────────────┘       └──────────────┬───────────────┘
                                                       │ edit.json
                                                       ▼
                         ┌──────────────┐     ┌────────────────────┐     ┌─────────────────────┐
                         │ plan         │────▶│ render preview     │────▶│ render final        │
                         │ (TTS timing, │     │ 960x540 + 4 thumbs │     │ 1920x1080 MP4 + SRT │
                         │  validation) │     └────────────────────┘     └─────────────────────┘
                         └──────────────┘
```

One core library, with the MCP server and the CLI as thin wrappers that return the same compact JSON. Details are in
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md). The spec format is in [docs/spec.md](docs/spec.md), and the tools are in
[docs/tools.md](docs/tools.md).

### CLI

Every MCP tool has a CLI twin that prints the same JSON:

```bash
casefile probe demo.mp4
casefile find-text demo.mp4 3.0 "Band" --row
casefile plan examples/fixture.json --no-synth
casefile render examples/fixture.json --preview
casefile render examples/fixture.json
```

`examples/fixture.json` renders against the synthetic test video, which `python tests/fixtures/make_fixture.py` builds.

### Branding

Cards and highlights use `spec.brand`: `{"logo": "path/to/logo.png", "accent": "#4F46E5", "ink": "#111827"}`.
With no logo, cards show a small "Casefile" text wordmark. The defaults are indigo `#4F46E5` and ink `#111827`, and the
font is Poppins (bundled).

## How well it works

Measured numbers, including the weak spots, are in [docs/RESULTS.md](docs/RESULTS.md). In short, the synthetic-fixture
gates are exact or within 1–2 px. One real 2:35 1440p recording became a 1:17 walkthrough. On that recording the
first review pass found 3 misplaced highlights, which were then fixed.

## Limitations

- Output today is **video + subtitles only**. Written docs, voice transcription and Jira are on the roadmap, not built.
- Tested on **Windows 11** so far. CI runs the fast tests on Ubuntu and Windows. macOS is untested.
- **Not validated at scale**: a synthetic fixture plus a small number of real recordings.
- The first `probe` of a 1440p recording takes minutes (frame extraction), and `track_text` costs ~2 s per frame at 1440p.
- OCR is noisy on full 1440p frames. Pass a `region`.
- Scene detection over-reports on live-updating charts.
- Narration is English (Kokoro voices). Other languages are not tested.
- The assistant still needs to review the preview. Terminals that scroll mid-shot can move text out from under a highlight.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). How the project was built with an orchestrator agent and parallel sub-agents is
in [docs/BUILD_STORY.md](docs/BUILD_STORY.md).

## License

MIT, see [LICENSE](LICENSE). The bundled fonts keep their own licenses: Poppins (SIL OFL 1.1) and DejaVu. The Kokoro
model files are downloaded by `casefile doctor` from their upstream release and are not included in this repository.
