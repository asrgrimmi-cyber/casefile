"""Planner tests: timeline math, TimeRef resolution, find->rect, errors with hints, plan_json size."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from casefile import planner
from casefile.errors import ToolError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fixture.json"

# 14-word lines -> 14/2.6 + 0.3 ; 15-word lines -> 15/2.6 + 0.3   (voice speed 1.0 in the example)
D14 = 14 / 2.6 + 0.3
D15 = 15 / 2.6 + 0.3


def _spec() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def fake_find(monkeypatch, fixture_video):
    """Mock vision.find_text with the fixture truth, so tests don't need tesseract."""
    _, truth = fixture_video
    calls = []

    def find_text(video, t, query, region=None, fuzzy=True, row=False, limit=5):
        calls.append((t, query, row))
        ms = [{"text": lb["text"], "box": lb["box"], "conf": 95, **({"row_box": lb["row_box"]} if row else {})}
              for lb in truth["labels"] if lb["text"].lower() == query.lower() and lb["t0"] <= t < lb["t1"]]
        return {"t": t, "matches": ms}

    import types
    mod = types.SimpleNamespace(find_text=find_text)
    real = planner._optional
    monkeypatch.setattr(planner, "_optional", lambda name: mod if name == "vision" else real(name))
    return calls


@pytest.fixture(autouse=True)
def _cwd(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("CASEFILE_WORKDIR", raising=False)


# --------------------------------------------------------------------------- expected timeline

def test_example_timeline(fixture_video, fake_find):
    p = planner.plan(EXAMPLE, synth=False)
    assert p.src_size == (1280, 720) and p.size == (1920, 1080) and p.fps == 30
    s0, s1, s2, s3 = p.shots
    ap = pytest.approx

    # intro card: lead 0.5 + 14 words, tail 0.6
    assert s0.T0 == 0 and s0.line_times["intro"] == (ap(0.5), ap(0.5 + D14))
    assert s0.dur == ap(0.5 + D14 + 0.6)
    # UI shot: ui1 at lead, ui2 asked for 7.0 (> ui1 end + gap) ; play_len 9 < lines
    assert s1.T0 == ap(s0.dur - 0.8)
    assert s1.line_times["ui1"] == (ap(0.5), ap(0.5 + D14))
    assert s1.line_times["ui2"] == (ap(7.0), ap(7.0 + D14))
    assert s1.dur == ap(7.0 + D14 + 0.6)
    # chart shot: play_len 4/2 + 5.5 = 7.5 < lines
    assert s2.T0 == ap(s1.T0 + s1.dur - 0.5)
    ch2_s = 0.5 + D15 + 0.35
    assert s2.line_times["ch2"] == (ap(ch2_s), ap(ch2_s + D15))
    assert s2.dur == ap(ch2_s + D15 + 0.6)
    # outro: line at 1.0, tail 2.0
    assert s3.T0 == ap(s2.T0 + s2.dur - 1.0)
    assert s3.dur == ap(1.0 + D14 + 2.0)
    assert p.total == ap(s3.T0 + s3.dur)
    assert p.total == ap(40.0423, abs=1e-3)
    assert p.frames == 1202

    # chapters at T0 + xfade/2
    assert [c for _, c in p.chapters] == ["INTRO", "01 · CONFIG", "02 · CHART", "SUMMARY"]
    assert [t for t, _ in p.chapters] == [ap(0), ap(s1.T0 + 0.4), ap(s2.T0 + 0.25), ap(s3.T0 + 0.5)]
    assert [s.chapter for s in p.shots] == ["INTRO", "01 · CONFIG", "02 · CHART", "SUMMARY"]

    # overlays resolved
    box_find, box_rect = s1.overlays
    assert box_find["rect"] == [50, 112, 610, 158]            # row_box of 'NTN'
    assert box_find["from"] == ap(1.0) and box_find["to"] == ap(0.5 + D14)
    assert box_rect["rect"] == [60, 178, 119, 194] and box_rect["to"] == ap(s1.dur - 0.5)
    assert fake_find == [(2.0, "NTN", True)]
    badge, phases, dots = s2.overlays
    assert (badge["from"], badge["to"]) == (ap(0.1), ap(1.9))
    assert phases["from"] == ap(0.8) and phases["to"] == ap(0.5 + D15)
    assert dots["to"] == ap(ch2_s + D15 - 0.2)
    # camera
    assert s2.camera == [(ap(0.5), (0, 0, 1280, 720)), (ap(ch2_s + 0.5), (100, 120, 640, 360)),
                         (ap(s2.dur - 1), (0, 0, 1280, 720))]
    # lines dict + remap from detect (fixture has 4 bad frames)
    assert p.lines["ch1"]["dur"] == ap(D15) and p.lines["intro"]["wav"] is None
    assert set(p.remap) >= {123, 257, 388}
    assert p.warnings == []


def test_src_time(fixture_video, fake_find):
    p = planner.plan(EXAMPLE, synth=False)
    s0, s1, s2, _ = p.shots
    assert s0.src_time(3) == 0
    assert s1.src_time(0) == pytest.approx(0.5)
    assert s1.src_time(4) == pytest.approx(4.5)
    assert s1.src_time(11) == pytest.approx(9.5)         # hold
    assert s2.src_time(1) == pytest.approx(12.0)         # 2x segment
    assert s2.src_time(3) == pytest.approx(15.0)         # 1x segment after 2 s
    assert s2.src_time(12) == pytest.approx(19.5)


def test_plan_json(fixture_video, fake_find):
    j = planner.plan_json(EXAMPLE, synth=False)
    assert j["total"] == 40.04 and j["frames"] == 1202
    assert [s["i"] for s in j["shots"]] == [0, 1, 2, 3]
    assert j["shots"][1]["lines"]["ui2"] == [7.0, round(7.0 + D14, 2)]
    assert len(json.dumps(j)) <= 3200


def test_plan_json_truncates_huge(fixture_video):
    sp = _spec()
    sp["shots"] = [{"src": [[0.5, 1.5]], "hold": 1.5, "chapter": f"chapter number {i}"} for i in range(200)]
    sp["lines"] = {}
    j = planner.plan_json(sp, synth=False)
    assert len(json.dumps(j, ensure_ascii=False)) <= 3200
    assert j["shots_omitted"] > 0


# --------------------------------------------------------------------------- time refs

def test_resolve_time():
    lt = {"a1": (1.0, 3.0), "x-y": (4.0, 5.0)}
    r = planner.resolve_time
    assert r(2, lt, 10, "w") == 2.0
    assert r("s:a1", lt, 10, "w") == 1.0
    assert r("s:a1+0.5", lt, 10, "w") == 1.5
    assert r("e:a1-0.25", lt, 10, "w") == 2.75
    assert r("end", lt, 10, "w") == 10
    assert r("end-0.5", lt, 10, "w") == 9.5
    assert r("e:x-y", lt, 10, "w") == 5.0
    with pytest.raises(ToolError) as e:
        r("s:zz", lt, 10, "shots[1].overlays[0].from")
    assert "zz" in e.value.error and "a1" in e.value.hint
    with pytest.raises(ToolError):
        r("later", lt, 10, "w")


# --------------------------------------------------------------------------- rejections (gate: 4 bad specs)

def test_unknown_line_id(fixture_video):
    sp = _spec()
    sp["shots"][1]["lines"] = ["ui1", "c9"]
    with pytest.raises(ToolError) as e:
        planner.plan(sp, synth=False, resolve_find=False)
    assert e.value.error == "unknown line id c9 in shots[1].lines"
    assert "ui1" in e.value.hint and "outro" in e.value.hint


def test_missing_source():
    sp = _spec()
    sp["source"] = "tests/fixtures/out/nope.mp4"
    with pytest.raises(ToolError) as e:
        planner.plan(sp, synth=False)
    assert "not found" in e.value.error and e.value.hint


def test_overlay_from_after_to(fixture_video):
    sp = _spec()
    sp["shots"][2]["overlays"][0].update({"from": "e:ch1", "to": "s:ch1"})
    with pytest.raises(ToolError) as e:
        planner.plan(sp, synth=False, resolve_find=False)
    assert "shots[2].overlays[0]" in e.value.error and "after" in e.value.error and e.value.hint


def test_box_off_frame(fixture_video):
    sp = _spec()
    sp["shots"][1]["overlays"][1]["rect"] = [1200, 600, 1400, 800]
    with pytest.raises(ToolError) as e:
        planner.plan(sp, synth=False, resolve_find=False)
    assert "outside" in e.value.error and "1280x720" in e.value.error and e.value.hint


def test_validation_error_path():
    sp = _spec()
    sp["shots"][2]["overlays"][0]["to"] = [1, 2]
    with pytest.raises(ToolError) as e:
        planner.load_spec(sp)
    assert "shots[2].overlays[0].to" in e.value.error
    sp = _spec()
    sp["shots"][0]["bogus"] = 1
    with pytest.raises(ToolError) as e:
        planner.load_spec(sp)
    assert "shots[0].bogus" in e.value.error


def test_find_no_match(fixture_video, fake_find):
    sp = _spec()
    sp["shots"][1]["overlays"][0]["find"]["query"] = "Nonexistent"
    with pytest.raises(ToolError) as e:
        planner.plan(sp, synth=False)
    assert "no OCR match" in e.value.error and "shots[1].overlays[0]" in e.value.error


def test_find_without_vision(fixture_video, monkeypatch):
    real = planner._optional
    monkeypatch.setattr(planner, "_optional", lambda n: None if n == "vision" else real(n))
    with pytest.raises(ToolError) as e:
        planner.plan(EXAMPLE, synth=False)
    assert "rect" in e.value.hint


def test_source_required_unless_all_cards():
    sp = {"shots": [{"card": "intro", "title": "Hi"}, {"card": "outro"}]}
    p = planner.plan(sp, synth=False)
    assert p.source is None and p.total == pytest.approx(6.0) and p.shots[0].src_time(1) == 0
    with pytest.raises(ToolError) as e:
        planner.plan({"shots": [{"src": [[0, 1]]}]}, synth=False)
    assert "source" in e.value.error


# --------------------------------------------------------------------------- warnings

def test_warnings(fixture_video):
    sp = _spec()
    sp["lines"]["spare"] = {"caption": "never used"}
    sp["shots"][1]["lines"] = ["ui1", ["ui2", 1.0]]                         # asked to overlap ui1
    sp["shots"][2]["camera"].append(["end", [1000, 600, 640, 360]])          # partly outside
    p = planner.plan(sp, synth=False, resolve_find=False)
    w = " | ".join(p.warnings)
    assert "unused lines: spare" in w
    assert "overlaps" in w
    assert "partly outside" in w


def test_load_spec_forms(tmp_path):
    txt = EXAMPLE.read_text(encoding="utf-8")
    assert planner.load_spec(txt).shots[0].card == "intro"
    assert planner.load_spec(str(EXAMPLE)).voice.speed == 1.0
    assert planner.load_spec(copy.deepcopy(_spec())).lines["ui1"].caption.startswith("First")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(ToolError):
        planner.load_spec(bad)


# --------------------------------------------------------------------------- integration (real OCR)

@pytest.mark.skipif(importlib.util.find_spec("casefile.vision") is None, reason="vision.py not built yet")
def test_find_real_ocr(fixture_video):
    try:
        p = planner.plan(EXAMPLE, synth=False)
    except ToolError as e:
        if "tesseract" in (e.error + e.hint).lower():
            pytest.skip("tesseract not installed")
        raise
    r = p.shots[1].overlays[0]["rect"]
    assert abs(r[1] - 112) <= 8 and abs(r[3] - 158) <= 8 and r[0] <= 70 and r[2] >= 400
