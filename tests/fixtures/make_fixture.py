"""Generate the synthetic 20 s UI fixture video + ground truth.

Output (default tests/fixtures/out/): fixture.mp4 (1280x720, 30 fps, 600 frames, silent AAC track)
and truth.json with:
  labels:  [{text, box:[x0,y0,x1,y1], row_box, t0, t1}]  (tight ink boxes in source px, visible t0..t1)
  chart:   {plot:[x0,y0,x1,y1], x_ticks:[[px,val],..], y_ticks:[[py,val],..],
            series:{parabola:{rgb, min_x, min_y}, line:{rgb, zero_x}}, t0, t1}
  bad_frames: [{frame, kind}]   (3 black + 1 popup)
  layout_change: [t]            (scene switch UI -> chart page at 10 s)
  scroll: {text, t0, t1, y0, y1}  (a label that scrolls up between t0 and t1)

Deterministic: no randomness. Run: python tests/fixtures/make_fixture.py [outdir]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from casefile import config  # noqa: E402
from casefile.fonts import font  # noqa: E402

W, H, FPS, N = 1280, 720, 30, 600
BLACK = [123, 257, 388]          # single-frame black glitches
POPUP = 312                      # single-frame taskbar pop-up
SWITCH = 300                     # frame where page changes (10.0 s)
BG = (246, 247, 250)
PANEL = (255, 255, 255)
TEXT = (40, 44, 60)
GREY = (120, 126, 140)

# (label text, value text, x, y) on page 1 (form rows)
FORM = [
    ("NTN", "True", 60, 120), ("Band", "n256", 60, 170), ("DL ARFCN", "437000", 60, 220),
    ("UL ARFCN", "399000", 60, 270), ("UE Count", "1000", 60, 320), ("AS Release", "17", 60, 370),
    ("Latitude", "45.428", 660, 120), ("Longitude", "-75.603", 660, 170), ("Altitude", "0", 660, 220),
    ("Koffset", "60", 660, 270),
]


def ink_box(d: ImageDraw.ImageDraw, xy, text, f) -> list[int]:
    x0, y0, x1, y1 = d.textbbox(xy, text, font=f)
    return [int(x0), int(y0), int(x1), int(y1)]


def page1(t: float, truth: dict | None) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 64), fill=(17, 24, 39))
    d.text((24, 16), "Cell Configuration", font=font("b", 24), fill=(255, 255, 255))
    d.rounded_rectangle((40, 96, 620, 420), 10, fill=PANEL, outline=(220, 222, 230))
    d.rounded_rectangle((640, 96, 1240, 420), 10, fill=PANEL, outline=(220, 222, 230))
    f = font("m", 22)
    for lab, val, x, y in FORM:
        d.text((x, y), lab, font=f, fill=GREY)
        d.text((x + 260, y), val, font=f, fill=TEXT)
        d.line((x, y + 40, x + 540, y + 40), fill=(235, 236, 240))
        if truth is not None:
            truth["labels"].append({"text": lab, "box": ink_box(d, (x, y), lab, f),
                                    "row_box": [x - 10, y - 8, x + 550, y + 38], "t0": 0.0, "t1": 10.0})
    # scrolling log line: moves up from y=600 to y=460 between 4 s and 8 s
    u = min(1.0, max(0.0, (t - 4.0) / 4.0))
    ly = int(round(600 - 140 * u))
    d.text((60, ly), "SIB19 received", font=font("m", 22), fill=TEXT)
    return im


def chart_geom():
    plot = [100, 120, 1180, 620]
    xmin, xmax, ymin, ymax = 0.0, 100.0, -50.0, 50.0
    def px(x): return plot[0] + (x - xmin) / (xmax - xmin) * (plot[2] - plot[0])
    def py(y): return plot[3] - (y - ymin) / (ymax - ymin) * (plot[3] - plot[1])
    return plot, (xmin, xmax, ymin, ymax), px, py


def page2() -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 64), fill=(17, 24, 39))
    d.text((24, 16), "Statistics", font=font("b", 24), fill=(255, 255, 255))
    plot, (xmin, xmax, ymin, ymax), px, py = chart_geom()
    d.rectangle(plot, fill=PANEL, outline=(200, 202, 210))
    for v in range(0, 101, 20):
        d.line((px(v), plot[1], px(v), plot[3]), fill=(236, 237, 242))
        d.text((px(v) - 10, plot[3] + 8), str(v), font=font("r", 16), fill=GREY)
    for v in range(-50, 51, 25):
        d.line((plot[0], py(v), plot[2], py(v)), fill=(236, 237, 242))
        d.text((plot[0] - 44, py(v) - 10), str(v), font=font("r", 16), fill=GREY)
    xs = np.linspace(xmin, xmax, 400)
    para = 0.012 * (xs - 42.0) ** 2 - 30.0          # min at x=42, y=-30
    line = -0.8 * (xs - 63.0)                        # zero at x=63
    d.line([(px(x), py(y)) for x, y in zip(xs, para)], fill=(59, 110, 230), width=3)
    d.line([(px(x), py(y)) for x, y in zip(xs, line) if ymin <= y <= ymax], fill=(220, 50, 70), width=3)
    return im


def popup(im: Image.Image) -> Image.Image:
    im = im.copy()
    d = ImageDraw.Draw(im)
    d.rectangle((300, 380, 980, 700), fill=(32, 32, 32))
    d.rectangle((320, 400, 960, 680), fill=(70, 90, 140))
    return im


def build(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    plot, (xmin, xmax, ymin, ymax), px, py = chart_geom()
    truth: dict = {"size": [W, H], "fps": FPS, "n_frames": N, "labels": [],
                   "bad_frames": [{"frame": f, "kind": "black"} for f in BLACK] + [{"frame": POPUP, "kind": "popup"}],
                   "layout_change": [SWITCH / FPS],
                   "scroll": {"text": "SIB19 received", "t0": 4.0, "t1": 8.0, "y0": 600, "y1": 460},
                   "chart": {"plot": plot, "t0": 10.0, "t1": 20.0,
                             "x_ticks": [[round(px(v), 2), v] for v in (0, 50, 100)],
                             "y_ticks": [[round(py(v), 2), v] for v in (-50, 0, 50)],
                             "axis": [xmin, xmax, ymin, ymax],
                             "series": {"parabola": {"rgb": [59, 110, 230], "min_x": 42.0, "min_y": -30.0},
                                        "line": {"rgb": [220, 50, 70], "zero_x": 63.0}}}}
    page1(0, truth)
    p2 = page2()
    ff = config.tool_path("ffmpeg") or "ffmpeg"
    out = outdir / "fixture.mp4"
    cmd = [ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-g", "30",
           "-c:a", "aac", "-b:a", "64k", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    black = Image.new("RGB", (W, H), (0, 0, 0)).tobytes()
    for n in range(N):
        t = n / FPS
        if n in BLACK:
            p.stdin.write(black)
            continue
        im = page1(t, None) if n < SWITCH else p2
        if n == POPUP:
            im = popup(im)
        p.stdin.write(im.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed")
    (outdir / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth


if __name__ == "__main__":
    od = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    build(od)
    print(f"fixture -> {od / 'fixture.mp4'}")
