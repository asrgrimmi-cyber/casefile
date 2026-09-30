# Security policy

## Supported versions

Only the latest release (currently 0.1.x) receives fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub's private vulnerability reporting on this
repository (Security → "Report a vulnerability"). Include the version, OS, how to reproduce it, and the impact you
expect. We aim to acknowledge reports within 7 days.

## Scope and design notes

Casefile processes screen recordings, which often contain sensitive information. Relevant design points:

- **Local only.** No tool uploads anything. The only network requests are `casefile doctor` downloading the Kokoro
  model files from their GitHub release.
- **Sandbox.** When `CASEFILE_WORKDIR` is set, every path argument (video, spec, output) must resolve inside it.
  A path that escapes the sandbox (for example through `..` or an absolute path) is a security bug. Please report it.
- **No shell.** ffmpeg and Tesseract are run with argument lists, never `shell=True`. Spec or tool input that leads
  to command injection is a security bug.
- **Blur is visual only.** A `blur` overlay hides the region in the rendered video. The source recording is not
  modified, and caches under `~/.casefile/cache` contain unblurred frames and OCR text. Delete the cache and the
  source if they contain secrets.
- The MCP HTTP mode (`--http`) has no authentication. Bind it to `127.0.0.1` only.
