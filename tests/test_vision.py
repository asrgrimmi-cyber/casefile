"""Phase 2 OCR tests against the synthetic fixture (tests/fixtures/make_fixture.py)."""
import json

import pytest
from PIL import Image

from casefile import config, vision
from casefile.errors import ToolError

pytestmark = pytest.mark.skipif(config.tool_path("tesseract") is None, reason="tesseract not installed")


def _err(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def test_find_all_labels_within_4px(fixture_video):
    mp4, truth = fixture_video
    errs = {}
    for lab in truth["labels"]:
        r = vision.find_text(mp4, 2.0, lab["text"], limit=1)
        assert r["matches"], f"{lab['text']} not found"
        m = r["matches"][0]
        errs[lab["text"]] = _err(m["box"], lab["box"])
        assert errs[lab["text"]] <= 4, (lab["text"], m["box"], lab["box"])
        assert len(json.dumps(r)) <= 1200
    assert len(errs) == 10


@pytest.mark.parametrize("q", ["DL ARFCN", "UE Count", "AS Release"])
def test_multi_word_is_one_box(fixture_video, q):
    mp4, truth = fixture_video
    lab = next(x for x in truth["labels"] if x["text"] == q)
    m = vision.find_text(mp4, 2.0, q, limit=1)["matches"][0]
    assert m["text"] == q
    assert _err(m["box"], lab["box"]) <= 4


def test_row_expansion(fixture_video):
    mp4, truth = fixture_video
    for lab in truth["labels"]:
        m = vision.find_text(mp4, 2.0, lab["text"], row=True, limit=1)["matches"][0]
        rb = m["row_box"]
        assert _err(rb, lab["row_box"]) <= 20, (lab["text"], rb, lab["row_box"])
        x, y = lab["box"][0], lab["box"][1]
        # contains label and value ('True' etc. drawn at x+260)
        assert rb[0] <= lab["box"][0] and rb[2] >= x + 300 and rb[1] <= y and rb[3] >= lab["box"][3]


def test_fuzzy_scroll_text(fixture_video):
    mp4, truth = fixture_video
    r = vision.find_text(mp4, 2.0, truth["scroll"]["text"])
    assert r["matches"]
    b = r["matches"][0]["box"]
    assert abs(b[0] - 60) <= 4 and abs(b[1] - 600) <= 14
    exact = vision.find_text(mp4, 2.0, truth["scroll"]["text"], fuzzy=False)
    assert isinstance(exact["matches"], list)


def test_regex_query(fixture_video):
    mp4, _ = fixture_video
    texts = {m["text"] for m in vision.find_text(mp4, 2.0, r"re:^\d{6}$")["matches"]}
    assert {"437000", "399000"} <= texts


def test_no_match(fixture_video):
    mp4, _ = fixture_video
    assert vision.find_text(mp4, 2.0, "Zebra Quantum")["matches"] == []


def test_region_limits_search(fixture_video):
    mp4, truth = fixture_video
    lab = next(x for x in truth["labels"] if x["text"] == "Latitude")
    r = vision.find_text(mp4, 2.0, "Latitude", region=[640, 96, 1240, 420])
    assert _err(r["matches"][0]["box"], lab["box"]) <= 4
    assert vision.find_text(mp4, 2.0, "Latitude", region=[40, 96, 620, 420])["matches"] == []


def test_track_scroll(fixture_video):
    mp4, truth = fixture_video
    r = vision.track_text(mp4, truth["scroll"]["text"], 3.0, 9.0, step=0.5)
    assert len(json.dumps(r)) <= 2000
    moved = [s for s in r["spans"] if s["moved"]]
    assert moved, r
    s = moved[0]
    assert abs(s["box_start"][1] - 600) <= 14 and abs(s["box_end"][1] - 460) <= 14
    assert s["t0"] <= 4.0 and s["t1"] >= 8.0


def test_track_static_label(fixture_video):
    mp4, _ = fixture_video
    r = vision.track_text(mp4, "Koffset", 1.0, 3.0, step=1.0)
    assert len(r["spans"]) == 1 and r["spans"][0]["moved"] is False


def test_ocr_region_reading_order(fixture_video):
    mp4, _ = fixture_video
    r = vision.ocr_region(mp4, 2.0, [40, 96, 620, 420])
    assert len(json.dumps(r)) <= 3200
    texts = [ln["text"] for ln in r["lines"]]
    order = ["NTN", "True", "Band", "n256", "DL ARFCN", "437000", "UL ARFCN", "UE Count", "AS Release", "17"]
    idx = [texts.index(t) for t in order]
    assert idx == sorted(idx), texts
    assert not any("Latitude" in t for t in texts)
    full = vision.ocr_region(mp4, 2.0)
    assert len(json.dumps(full)) <= 3200


def test_contact_sheet(fixture_video, tmp_path):
    mp4, _ = fixture_video
    r = vision.contact_sheet(mp4, t0=0, t1=20, n=6)
    im = Image.open(r["path"])
    assert im.width <= 960 and len(r["times"]) == 6
    out = tmp_path / "s.png"
    r2 = vision.contact_sheet(mp4, times=[1, 5], crop=[40, 96, 620, 420], max_width=500, out=out)
    assert out.exists() and Image.open(out).width <= 500 and r2["times"] == [1.0, 5.0]


def test_errors(fixture_video):
    mp4, _ = fixture_video
    with pytest.raises(ToolError):
        vision.find_text(mp4, 2.0, "")
    with pytest.raises(ToolError):
        vision.find_text(mp4, 2.0, "x", region=[5000, 5000, 6000, 6000])
    with pytest.raises(ToolError):
        vision.find_text(mp4, 2.0, "re:(")
    with pytest.raises(ToolError):
        vision.track_text(mp4, "x", 5, 1)


def test_ocr_image_direct(fixture_video):
    mp4, truth = fixture_video
    from casefile.media import FrameStore
    im = FrameStore(mp4).at(2.0)
    words = vision.ocr_image(im, region=[40, 96, 620, 200])
    assert any(w["text"] == "NTN" for w in words)
    assert all(set(w) == {"text", "box", "conf"} for w in words)
