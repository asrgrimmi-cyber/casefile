"""timeline.analyze on the synthetic GUI -> terminal fixture, plus textnorm / privacy unit tests."""
import json
import time
from pathlib import Path

import pytest

from casefile import privacy, timeline
from casefile.textnorm import normalise

OUT = Path(__file__).resolve().parent / "fixtures" / "out"


# ----------------------------------------------------------------------------- textnorm

@pytest.mark.parametrize("raw,ctx,want", [
    ("Ox46le", "terminal", "0x461e"),
    ("0x46Ie", "gui", "0x461e"),
    ('"rnti": Ox46le,', "terminal", '"rnti": 0x461e,'),
    ("Q01010123456789", "terminal", "001010123456789"),
    ('"\u201culretx": 5', "terminal", '"ulretx": 5'),
    ('"pusch_snr": 19.l,', "terminal", '"pusch_snr": 19.1,'),
    ("1O5 kbps", "gui", "105 kbps"),
    ("\u2502 dl_bitrate \u2502 10376", "terminal", "dl_bitrate 10376"),
])
def test_normalise_misreads(raw, ctx, want):
    assert normalise(raw, ctx) == want


@pytest.mark.parametrize("word", ["Oxford", "Sysadmin", "S1AP", "ue_get", "QoS", "ID", "Band"])
def test_normalise_keeps_words(word):
    assert normalise(word, "terminal") == word
    assert normalise(word, "gui") == word


def test_normalise_q_only_when_rest_digits():
    assert normalise("Q01ab0123456789", "terminal") == "Q01ab0123456789"


# ----------------------------------------------------------------------------- privacy

def test_privacy_scan_text():
    hits = privacy.scan_text("user@host:~$ ./ws.js 127.0.0.1:9001 mail someone@example.com "
                             "imsi 001010123456789 password: hunter22")
    reasons = {h["reason"]: h["text"] for h in hits}
    assert reasons["email"] == "someone@example.com"
    assert reasons["hostname"].startswith("user@host")
    assert reasons["ip"].startswith("127.0.0.1")
    assert reasons["imsi"] == "001010123456789"
    assert reasons["secret"] == "hunter22"
    assert not privacy.scan_text("dl_bitrate 10376 rnti 17950 token: true")


def test_privacy_scan_words_boxes():
    words = [{"text": "Owner:", "box": [10, 10, 60, 30]}, {"text": "someone@example.com", "box": [70, 10, 250, 30]}]
    hits = privacy.scan_words(words)
    assert hits == [{"reason": "email", "text": "someone@example.com", "box": [70, 10, 250, 30]}]


# ----------------------------------------------------------------------------- timeline on the fixture

@pytest.fixture(scope="module")
def term_video():
    mp4, tj = OUT / "terminal.mp4", OUT / "terminal_truth.json"
    if not (mp4.exists() and tj.exists()):
        import make_terminal_fixture
        make_terminal_fixture.build(OUT)
    return mp4, json.loads(tj.read_text())


@pytest.fixture(scope="module")
def analysed(term_video, tmp_path_factory):
    mp4, truth = term_video
    out = tmp_path_factory.mktemp("tl")
    s = timeline.analyze(mp4, speech="none", out_dir=out)
    return s, timeline.load(s["path"]), truth, out, mp4


def _inside(inner, outer, tol=12):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def test_steps_match_planted_pages(analysed):
    s, tl, truth, out, _ = analysed
    assert tl["version"] == "1"
    kinds = [st["kind"] for st in tl["steps"]]
    assert 2 <= len(tl["steps"]) <= 3, kinds
    assert kinds[0] == "gui" and kinds[-1] == "terminal"
    sw = truth["pages"][1]["t0"]
    assert abs(tl["steps"][-1]["t0"] - sw) < 0.8
    for st in tl["steps"]:
        assert st["t0"] <= st["stable_t"] <= st["t1"]
        assert (out / st["screenshot"]).exists()
        for it in st["items"]:
            assert it["id"].startswith(st["id"] + ".")
        assert len({it["id"] for it in st["items"]}) == len(st["items"])
    assert tl["stats"]["frames_analysed"] < 40
    assert tl["stats"]["analyse_sec"] > 0


def test_live_chart_ignored(analysed):
    _, tl, truth, _, _ = analysed
    regs = [r for r in tl["ignored_regions"] if r["reason"] == "live_chart"]
    assert regs
    cx = (truth["chart"][0] + truth["chart"][2]) / 2
    cy = (truth["chart"][1] + truth["chart"][3]) / 2
    assert any(r["box"][0] <= cx <= r["box"][2] and r["box"][1] <= cy <= r["box"][3] for r in regs)
    w, h = tl["video"]["size"]
    for r in regs:  # a corner region, not the whole frame
        assert (r["box"][2] - r["box"][0]) * (r["box"][3] - r["box"][1]) < 0.3 * w * h


def test_terminal_command_fields_scroll(analysed):
    _, tl, truth, _, _ = analysed
    term = tl["steps"][-1]
    assert any(truth["command"] in c["text"] for c in term["commands"]), term["commands"]
    assert term["fields"].get("rnti") == "17950", term["fields"]
    assert term["fields"].get("pusch_snr") == "19.1"
    assert term["scroll"], "scroll not detected"
    sc = term["scroll"][0]
    assert abs(sc["t"] - truth["scroll"]["t"]) < 0.8 and sc["dy"] < 0
    assert term["stable_t"] >= sc["t"]
    assert "pusch_snr" in term["transcript"] and len(term["transcript"]) <= 4000


def test_private_email(analysed):
    _, tl, truth, _, _ = analysed
    priv = [p for st in tl["steps"] for p in st["private"]]
    em = [p for p in priv if p["reason"] == "email"]
    assert em, priv
    assert _inside(truth["email"]["box"], em[0]["box"], tol=15) or _inside(em[0]["box"], truth["email"]["box"], 15)
    assert em[0]["t0"] <= 1.0 and em[0]["t1"] >= 6.0
    assert any(p["reason"] == "hostname" for p in priv)


def test_query_and_locate(analysed):
    _, tl, _, _, _ = analysed
    term = tl["steps"][-1]
    a = timeline.query(tl, ["pusch_snr"])[0]
    assert a["found"] and a["step"] == term["id"] and len(a["box"]) == 4
    assert term["t0"] <= a["t"] <= term["t1"]
    b = timeline.query(tl, [f"{term['id']}.pusch_snr", "stable time for dl_bitrate", "nonexistent_zz_field"])
    assert b[0]["id"] == a["id"]
    assert b[1]["found"] and "dl_bitrate" in b[1]["id"]
    assert not b[2]["found"]
    loc = timeline.locate(tl, [{"text": "DL ARFCN"}, "rnti"])
    assert loc[0]["found"] and loc[0]["step"] == tl["steps"][0]["id"]
    assert loc[1]["found"] and loc[1]["step"] == term["id"]


def test_summary_compact_and_cache(analysed):
    s, tl, _, out, mp4 = analysed
    assert len(json.dumps(s)) <= 12000
    assert s["n_steps"] == len(tl["steps"]) and not s["cached"]
    assert len(json.dumps(timeline.summary(tl))) <= 12000
    t0 = time.time()
    s2 = timeline.analyze(mp4, speech="none", out_dir=out)
    assert s2["cached"] and time.time() - t0 < 1.0
    assert s2["steps"] == s["steps"]


def test_injected_ocr_backend(term_video, tmp_path):
    mp4, _ = term_video
    calls = []

    def fake(img, region=None):
        calls.append(img.size)
        return [{"text": "Hello", "box": [10, 10, 60, 30], "conf": 99}]

    s = timeline.analyze(mp4, speech="none", ocr=fake, out_dir=tmp_path)
    tl = timeline.load(s["path"])
    assert calls and tl["stats"]["ocr"] == "fake"
    assert all(max(sz) <= 1600 for sz in calls)
