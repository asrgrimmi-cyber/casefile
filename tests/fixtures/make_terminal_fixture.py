"""Generate the synthetic 20 s GUI -> terminal fixture for timeline analysis + ground truth.

Output (default tests/fixtures/out/): terminal.mp4 (1280x720, 30 fps, 600 frames, silent AAC) and
terminal_truth.json:
  pages:   [{kind, t0, t1}]                 the two planted pages (GUI 0-7 s, terminal 7-20 s)
  chart:   [x0,y0,x1,y1]                    live sparkline strip, redrawn every frame (must be ignored)
  command: "./ws.js 127.0.0.1:9001 ue_get"  typed after the prompt `user@host:~$ `
  fields:  {rnti: "17950", pusch_snr: "19.1"}
  scroll:  {t, dy}                          terminal output scrolls up once (new lines appear at the bottom)
  email:   {text, box, t0, t1}              a private email on the GUI page
Deterministic. Run: python tests/fixtures/make_terminal_fixture.py [outdir]
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from casefile import config  # noqa: E402
from casefile.fonts import font  # noqa: E402

W, H, FPS, N = 1280, 720, 30, 600
SWITCH = 210            # 7.0 s: GUI -> terminal
SCROLL = 390            # 13.0 s: terminal scrolls up
CHART = [1000, 600, 1270, 710]
LINE_H = 30
TERM_X, TERM_Y = 40, 40
COMMAND = "./ws.js 127.0.0.1:9001 ue_get"
PROMPT = "user@host:~$ "
EMAIL = "someone@example.com"

FORM = [("Cell ID", "1"), ("PCI", "42"), ("DL ARFCN", "437000"), ("Band", "n256"), ("UE Count", "1")]

JSON_LINES = [
    '{',
    '  "message": "ue_get",',
    '  "time": 1234.5,',
    '  "ue_list": [',
    '    {',
    '      "enb_ue_id": 30,',
    '      "rnti": 17950,',
    '      "cell_id": 1,',
    '      "dl_bitrate": 10376,',
    '      "ul_bitrate": 1663,',
    '      "cqi": 15,',
    '      "ri": 1,',
    '      "dl_mcs": 27,',
    '      "ul_mcs": 20,',
    '      "pusch_snr": 19.1,',
    '      "epre": -103.2,',
    '      "turbo_decoder_avg": 1.5',
    '    }',
    '  ]',
    '}',
]
VISIBLE = 14            # JSON lines visible before the scroll (plus the prompt line)
SCROLL_LINES = 6        # lines scrolled at SCROLL


def gui_page() -> tuple[Image.Image, list[int]]:
    im = Image.new("RGB", (W, H), (246, 247, 250))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 64), fill=(17, 24, 39))
    d.text((24, 16), "Cell Configuration", font=font("b", 26), fill=(255, 255, 255))
    d.rounded_rectangle((40, 96, 760, 520), 10, fill=(255, 255, 255), outline=(220, 222, 230))
    f = font("m", 24)
    for i, (lab, val) in enumerate(FORM):
        y = 130 + i * 64
        d.text((70, y), lab, font=f, fill=(110, 116, 130))
        d.text((380, y), val, font=f, fill=(30, 34, 50))
        d.line((70, y + 46, 730, y + 46), fill=(235, 236, 240))
    fo = font("m", 22)
    d.text((70, 560), "Owner:", font=fo, fill=(110, 116, 130))
    eb = d.textbbox((170, 560), EMAIL, font=fo)
    d.text((170, 560), EMAIL, font=fo, fill=(30, 34, 50))
    return im, [int(v) for v in eb]


def term_page(scrolled: bool) -> Image.Image:
    im = Image.new("RGB", (W, H), (24, 24, 28))
    d = ImageDraw.Draw(im)
    f = font("mono", 20)
    lines = [PROMPT + COMMAND] + JSON_LINES[:VISIBLE]
    if scrolled:
        lines = ([PROMPT + COMMAND] + JSON_LINES)[SCROLL_LINES:]
    for i, ln in enumerate(lines):
        col = (120, 220, 120) if ln.startswith(PROMPT) else (225, 225, 225)
        d.text((TERM_X, TERM_Y + i * LINE_H), ln, font=f, fill=col)
    return im


def chart(im: Image.Image, n: int) -> Image.Image:
    """Live sparkline strip: redrawn every frame (scrolling sine + bars)."""
    im = im.copy()
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = CHART
    d.rectangle(CHART, fill=(255, 255, 255), outline=(180, 185, 200))
    pts = []
    for i in range(0, x1 - x0 - 8, 4):
        v = math.sin((i + n * 9) / 17.0) * 0.6 + math.sin((i * 3 + n * 23) / 29.0) * 0.4
        pts.append((x0 + 4 + i, (y0 + y1) / 2 - v * (y1 - y0) * 0.4))
    d.line(pts, fill=(59, 110, 230), width=3)
    for i in range(10):
        h = int(10 + 30 * (0.5 + 0.5 * math.sin(n * 0.7 + i * 1.3)))
        bx = x0 + 10 + i * 25
        d.rectangle((bx, y1 - 4 - h, bx + 12, y1 - 4), fill=(220, 50, 70))
    return im


def build(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    gui, email_box = gui_page()
    t0 = term_page(False)
    t1 = term_page(True)
    truth = {"size": [W, H], "fps": FPS, "n_frames": N,
             "pages": [{"kind": "gui", "t0": 0.0, "t1": SWITCH / FPS},
                       {"kind": "terminal", "t0": SWITCH / FPS, "t1": N / FPS}],
             "chart": CHART, "command": COMMAND, "prompt": PROMPT.strip(),
             "fields": {"rnti": "17950", "pusch_snr": "19.1", "dl_bitrate": "10376"},
             "scroll": {"t": SCROLL / FPS, "dy": -SCROLL_LINES * LINE_H},
             "email": {"text": EMAIL, "box": email_box, "t0": 0.0, "t1": SWITCH / FPS}}
    ff = config.tool_path("ffmpeg") or "ffmpeg"
    out = outdir / "terminal.mp4"
    cmd = [ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-g", "30",
           "-c:a", "aac", "-b:a", "64k", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for n in range(N):
        base = gui if n < SWITCH else (t0 if n < SCROLL else t1)
        p.stdin.write(chart(base, n).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed")
    (outdir / "terminal_truth.json").write_text(json.dumps(truth, indent=1))
    return truth


if __name__ == "__main__":
    od = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    build(od)
    print(f"fixture -> {od / 'terminal.mp4'}")
