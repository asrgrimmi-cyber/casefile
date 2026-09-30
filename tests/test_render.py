"""Renderer tests: stub Plan (no planner needed) + one planner integration test (skipped until planner exists)."""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from casefile import config
from casefile.spec import Shot, Spec

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"


# ------------------------------------------------------------------ Plan stub (mirrors docs/CONTRACT.md)
@dataclass
class PShot:
    idx: int
    shot: Any
    T0: float
    dur: float
    xfade: float = 0.0
    chapter: str | None = None
    line_times: dict = field(default_factory=dict)
    overlays: list = field(default_factory=list)
    camera: list = field(default_factory=list)
    segs: list = field(default_factory=list)     # stub-only: [(t0, t1, speed)]
    hold: float = 0.0

    def src_time(self, t: float) -> float:
        for a, b, sp in self.segs:
            L = (b - a) / sp
            if t < L:
                return a + t * sp
            t -= L
        return self.hold


@dataclass
class Plan:
    spec: Any
    source: Path | None
    src_size: tuple | None
    fps: int
    size: tuple
    total: float
    remap: dict
    warnings: list
    lines: dict
    chapters: list
    shots: list


def _tone_wav(path: Path, dur: float, f0: float = 220.0, sr: int = 24000) -> Path:
    """Speech-ish test signal: amplitude-modulated harmonic tone."""
    import soundfile as sf
    t = np.arange(int(dur * sr)) / sr
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
    sig = (sig * env * 0.3).astype(np.float32)
    sf.write(str(path), sig, sr)
    return path


def make_plan(video: Path, wav_dir: Path | None = None) -> Plan:
    """~3 s plan: intro card (1.2 s) crossfading (0.5 s) into a source shot with every overlay type."""
    spec = Spec(source=str(video), voice={"engine": "kokoro" if wav_dir else "none"},
                shots=[Shot(card="intro", title=["Cell Configuration", "Walkthrough"], subtitle="Renderer test",
                            footer="Test case TC-001"), Shot(src=[[1.0, 3.0]])])
    lines = {"c1": {"caption": "Intro caption", "spoken": "Intro caption", "wav": None, "dur": 0.8},
             "c2": {"caption": "NTN is enabled on band n256 · S-band", "spoken": "x", "wav": None, "dur": 1.4}}
    if wav_dir:
        lines["c1"]["wav"] = _tone_wav(wav_dir / "c1.wav", 0.8)
        lines["c2"]["wav"] = _tone_wav(wav_dir / "c2.wav", 1.4, 330)
    s0 = PShot(0, spec.shots[0], 0.0, 1.2, chapter="INTRO", line_times={"c1": (0.2, 1.0)})
    ovs = [
        {"type": "blur", "from": 0.0, "to": 5.0, "rect": [660, 112, 1210, 158], "radius": 10},
        {"type": "box", "from": 0.3, "to": 1.6, "rect": [50, 112, 610, 158], "label": "NTN = True",
         "label_side": "auto", "pad": 8, "spotlight": True},
        {"type": "title", "from": 0.2, "to": 2.3, "text": "▶  Start the Test Case"},
        {"type": "badge", "from": 0.0, "to": 2.3, "text": "2× speed", "corner": "tr"},
        {"type": "tiles", "from": 0.6, "to": 2.3, "y": 120, "items": [
            {"key": "ta_common", "value": "12502.6 µs", "sub": "≈ 12.5 ms", "at": 0.6},
            {"key": "dl_shift", "value": "+34,133 Hz", "sub": "DL Doppler", "at": 0.9}]},
        {"type": "phases", "from": 0.2, "to": 2.3, "plot": [100, 420, 1180, 620], "stagger": 0.3,
         "bands": [[100, 500, "RISING"], [500, 800, "PEAK", "#FF8C1E"], [800, 1180, "SETTING"]]},
        {"type": "dots", "from": 0.4, "to": 2.3, "color": "#3B6EE6", "stagger": 0.2,
         "points": [[[300, 500], "≈ 1,750 km", "right"], {"xy": [640, 560], "label": "closest", "side": "below"}]},
        {"type": "vline", "from": 0.5, "to": 2.3, "x": 640, "y0": 420, "y1": 620, "label": "Same instant"},
        {"type": "arrow", "from": 0.5, "to": 2.3, "start": [900, 300], "end": [700, 140], "label": "look here"},
    ]
    cam = [(0.0, (0, 0, 1280, 720)), (0.8, (0, 60, 900, 500)), (2.3, (0, 60, 900, 500))]
    s1 = PShot(1, spec.shots[1], 0.7, 2.3, xfade=0.5, chapter="01 · CELL", line_times={"c2": (0.6, 2.0)},
               overlays=ovs, camera=cam, segs=[(1.0, 3.0, 1.0)], hold=3.0)
    return Plan(spec=spec, source=video, src_size=(1280, 720), fps=30, size=(1920, 1080), total=3.0,
                remap={123: 122}, warnings=[], lines=lines, chapters=[(0.0, "INTRO"), (0.95, "01 · CELL")],
                shots=[s0, s1])


def _probe(path: str) -> dict:
    r = subprocess.run([config.tool_path("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-show_format",
                        "-of", "json", path], capture_output=True, check=True)
    return json.loads(r.stdout)


def _loudness(path: str) -> float:
    r = subprocess.run([config.tool_path("ffmpeg"), "-hide_banner", "-nostats", "-i", path, "-map", "0:a",
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True)
    m = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", r.stderr.decode(errors="replace"))
    return float(m[-1]) if m else -math.inf


# ------------------------------------------------------------------ unit
def test_frame_at_size_and_golden(fixture_video):
    from casefile.render import frame_at
    plan = make_plan(fixture_video[0])
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for T in (0.3, 0.9, 1.3, 1.7, 2.2, 2.9):
        im = frame_at(plan, T)
        assert im.size == (1920, 1080) and im.mode == "RGB"
        g = GOLDEN / f"frame_{T:.1f}.png"
        if not g.exists():
            im.save(g)
            continue
        diff = np.abs(np.asarray(im, np.int16) - np.asarray(Image.open(g).convert("RGB"), np.int16)).mean()
        assert diff < 2, f"golden {g.name} drifted: mean abs diff {diff:.2f}"
    small = frame_at(plan, 1.7, size=(960, 540))
    assert small.size == (960, 540)


def test_frame_at_visuals(fixture_video):
    from casefile.render import frame_at
    plan = make_plan(fixture_video[0])
    im = np.asarray(frame_at(plan, 1.7), np.int16)
    # caption bar is dark navy with an orange chapter chip
    assert tuple(im[1080 - 60, 1900]) == config.BAR_BG
    chip = im[969 + 40:969 + 65, 30:60].reshape(-1, 3)
    assert (np.abs(chip - np.array(config.ACCENT)).sum(1) < 30).mean() > 0.5
    # progress bar: orange at the left, grey at the right
    assert np.abs(im[1078, 5] - np.array(config.ACCENT)).sum() < 30
    assert np.abs(im[1078, 1910] - np.array((45, 50, 85))).sum() < 30
    # global fade: first frame is black
    assert np.asarray(frame_at(plan, 0.0)).max() == 0


def test_srt(fixture_video):
    from casefile.render import srt_for
    s = srt_for(make_plan(fixture_video[0]))
    assert "00:00:00,200 --> 00:00:01,000\nIntro caption" in s
    assert "00:00:01,300 --> 00:00:02,700\nNTN is enabled" in s


def test_chunks_cover_all_frames():
    from casefile.render import _chunks, n_frames
    assert n_frames(3.0, 30) == 90 and n_frames(3.01, 30) == 91
    for N, j in ((90, 8), (1000, 8), (7, 4), (200, 3)):
        ch = _chunks(N, j)
        assert ch[0][0] == 0 and ch[-1][1] == N and all(a[1] == b[0] for a, b in zip(ch, ch[1:]))


# ------------------------------------------------------------------ full renders
@pytest.mark.slow
def test_render_full_parallel_with_speech(fixture_video, tmp_path):
    from casefile.render import render
    plan = make_plan(fixture_video[0], wav_dir=tmp_path)
    t0 = time.perf_counter()
    res = render(plan, out=tmp_path / "full.mp4", jobs=3)
    sec = time.perf_counter() - t0
    print(f"\n1080p render: {res['frames']} frames in {sec:.2f}s = {res['frames'] / sec:.1f} fps (jobs=3)")
    assert res["frames"] == 90 and res["duration"] == 3.0
    j = _probe(res["path"])
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    a = next(s for s in j["streams"] if s["codec_type"] == "audio")
    assert v["codec_name"] == "h264" and (v["width"], v["height"]) == (1920, 1080)
    assert int(v["nb_read_frames"]) == 90 and v["pix_fmt"] == "yuv420p" and v["r_frame_rate"] == "30/1"
    assert a["codec_name"] == "aac"
    assert abs(float(a["duration"]) - 3.0) < 0.1
    lufs = _loudness(res["path"])
    assert -20 < lufs < -12, lufs
    srt = Path(res["srt"]).read_text(encoding="utf-8")
    assert "NTN is enabled" in srt
    assert len(res["thumbs"]) == 4
    for tp in res["thumbs"]:
        assert Image.open(tp).size == (960, 540)
    head = Path(res["path"]).read_bytes()[:4096]
    assert head.find(b"moov") != -1 and head.find(b"moov") < max(head.find(b"mdat"), 1e9)  # faststart


@pytest.mark.slow
def test_render_preview_silent(fixture_video, tmp_path):
    from casefile.render import render
    plan = make_plan(fixture_video[0])
    res = render(plan, preview=True, out=tmp_path / "prev.mp4")
    assert res["frames"] == 45 and res["size"] == [960, 540] and res["fps"] == 15
    j = _probe(res["path"])
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (960, 540) and v["r_frame_rate"] == "15/1"
    assert int(v["nb_read_frames"]) == 45
    assert any(s["codec_type"] == "audio" and s["codec_name"] == "aac" for s in j["streams"])


@pytest.mark.slow
def test_render_speed_1080p(fixture_video, tmp_path):
    """Benchmark (default jobs = cpu_count) on a 6 s plan; prints frames/sec."""
    from casefile.render import render
    plan = make_plan(fixture_video[0])
    plan.total = 6.0
    plan.shots[1].dur = 5.3
    res = render(plan, out=tmp_path / "bench.mp4")
    print(f"\n1080p benchmark: {res['frames']} frames, {res['render_sec']}s, {res['fps_render']} fps (all cores)")
    assert res["frames"] == 180


@pytest.mark.slow
def test_render_via_planner(fixture_video, tmp_path):
    planner = pytest.importorskip("casefile.planner")
    ex = ROOT / "examples" / "fixture.json"
    if not ex.exists():
        pytest.skip("examples/fixture.json not written yet")
    from casefile.render import render
    spec = json.loads(ex.read_text(encoding="utf-8"))
    spec["source"] = str(fixture_video[0])
    plan = planner.plan(spec, synth=True)
    res = render(spec, preview=True, out=tmp_path / "planner.mp4")
    assert res["frames"] == math.ceil(plan.total * 15 - 1e-6)
    j = _probe(res["path"])
    assert int(next(s for s in j["streams"] if s["codec_type"] == "video")["nb_read_frames"]) == res["frames"]
    assert any(s["codec_type"] == "audio" for s in j["streams"])
