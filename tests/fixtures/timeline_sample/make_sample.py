"""Build the sample timeline fixture: copy timeline.json and draw steps/s1.png (GUI) + steps/s2.png (terminal).

Screenshots are drawn from the item boxes in timeline.json so boxes and pixels always agree. Deterministic.
Run: python tests/fixtures/timeline_sample/make_sample.py <outdir>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from casefile.fonts import font  # noqa: E402

PRIVATE_TEXT = {"email": "alice.tester@example.com", "hostname": "user@callbox", "ip": "192.168.0.110",
                "imsi": "001010123456789"}


def _fit(d: ImageDraw.ImageDraw, box, text: str, name: str, fill) -> None:
    x0, y0, x1, y1 = box
    size = max(8, int((y1 - y0) * 0.85))
    while size > 8 and font(name, size).getlength(text) > (x1 - x0):
        size -= 1
    d.text((x0, y0), text, font=font(name, size), fill=fill)


def _gui(step: dict, size) -> Image.Image:
    im = Image.new("RGB", size, (246, 247, 250))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, size[0], 56], fill=(17, 24, 39))
    d.rectangle([30, 140, size[0] - 30, 360], fill=(255, 255, 255), outline=(220, 224, 232))
    for it in step["items"]:
        fill = (255, 255, 255) if it["role"] == "heading" else (40, 44, 60)
        _fit(d, it["box"], it["text"], "b" if it["role"] in ("heading", "tab") else "r", fill)
    for i in range(12):  # some chart-like texture
        d.line([60 + i * 90, 600, 120 + i * 90, 480 + (i * 37) % 90], fill=(59, 130, 246), width=3)
    for p in step.get("private", []):
        _fit(d, [p["box"][0] + 6, p["box"][1] + 4, p["box"][2] - 6, p["box"][3] - 4],
             PRIVATE_TEXT.get(p["reason"], "secret"), "r", (20, 20, 20))
    return im


def _terminal(step: dict, size) -> Image.Image:
    im = Image.new("RGB", size, (12, 14, 20))
    d = ImageDraw.Draw(im)
    for it in step["items"]:
        _fit(d, it["box"], it["text"], "mono", (200, 230, 200))
    for i in range(8):
        d.text((40, 300 + i * 30), f"  line {i:02d}: tx_ok={1000 + i * 17} rx_ok={900 + i * 13}",
               font=font("mono", 18), fill=(150, 160, 170))
    return im


def build(out: Path) -> Path:
    """Write timeline.json + steps/*.png under out; returns the timeline.json path."""
    out = Path(out)
    (out / "steps").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "timeline.json", out / "timeline.json")
    tl = json.loads((HERE / "timeline.json").read_text(encoding="utf-8"))
    size = tuple(tl["video"]["size"])
    for st in tl["steps"]:
        im = _terminal(st, size) if st["kind"] == "terminal" else _gui(st, size)
        im.save(out / st["screenshot"])
    return out / "timeline.json"


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "out")))
