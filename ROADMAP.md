# Roadmap

Only v0.1 exists today. Everything below it is planned and **not built**. Order and scope may change based on feedback.

## v0.1: video walkthroughs (current)

- [x] Screen recording → narrated, captioned 1080p walkthrough MP4 + `.srt`
- [x] Text-first tools over MCP (9 tools) and an identical CLI
- [x] Offline OCR (Tesseract), chart extraction, TTS (Kokoro)
- [x] Highlights, blur, camera moves, chart annotations, intro/outro cards, bad-frame repair
- [x] Configurable brand (logo, accent, ink)
- [x] Agent skill with workflow, budgets and review checklist

## v0.2: written documentation output

- [ ] Markdown step-by-step document from the same spec: one step per narration line, with a screenshot of the
      highlighted frame
- [ ] Confluence export (storage format), with screenshots as attachments
- [ ] Reuse the existing OCR facts for alt text and step text

## v0.3: the narrator's own voice

- [ ] Offline speech-to-text of the audio already in the recording (the tester talking while testing)
- [ ] Use spoken cues ("now I click save", "this should say 17") to propose shots, highlights and captions
- [ ] Option to keep the original voice instead of TTS

## v0.4: Jira integration

- [ ] Read a test case's steps from a Jira issue (Cloud REST API, possibly a Forge app)
- [ ] Map recording segments to test steps
- [ ] Attach the video, the written doc and the `.srt` to the issue

## Later

- [ ] More narration languages and voices
- [ ] Hosted / team tier (freemium): shared workspace, rendering off the laptop. **Planned, not built.** The core
      tool stays MIT-licensed and free to run locally.
