"""lessons: change log -> candidate lessons -> approved rules for the next job; gaps; auto-apply actions."""
import pytest

from casefile import lessons as L
from casefile.errors import ToolError


@pytest.fixture
def db(tmp_path):
    return tmp_path / "lessons.db"


def test_why_required(db):
    with pytest.raises(ToolError) as e:
        L.log_change("j1", "s1.box", None, None, "   ", db=db)
    assert e.value.hint
    assert L.log_change("j1", "s1.box", "a", "b", "clipped value", db=db) == 1
    assert L.list_changes(db=db)[0]["why"] == "clipped value"


def test_target_pattern():
    assert L.target_pattern("s12.phr_box") == "*.phr_box"
    assert L.target_pattern("caption.s3") == "caption.*"
    assert L.target_pattern("shot[4].zoom") == "shot[*].zoom"


def test_promotion_after_three(db):
    for i, s in enumerate(["s3", "s7"]):
        L.log_change(f"ntn-{i}", f"{s}.phr_box", [1, 2, 3, 4], [1, 2, 9, 4], "box clipped the PHR value", db=db)
    assert L.promote(db=db) == []
    L.log_change("ntn-9", "s12.phr_box", [1, 2, 3, 4], [1, 2, 9, 4], "box clipped the PHR value.", db=db)
    got = L.promote(db=db)
    assert len(got) == 1
    les = got[0]
    assert les["status"] == "candidate" and les["evidence_count"] == 3
    assert "*.phr_box" in les["rule"] and "box clipped the PHR value" in les["rule"]
    assert len(les["rule"]) <= 200 and les["scope"] == {"product": "ntn"}
    # re-promote without new evidence: no duplicate, no change
    assert L.promote(db=db) == [] and len(L.list_lessons(db=db)) == 1
    L.log_change("ntn-10", "s2.phr_box", None, None, "box clipped the PHR value", db=db)
    assert L.promote(db=db)[0]["evidence_count"] == 4


def test_approve_get_retire(db):
    for i in range(3):
        L.log_change(f"j{i}", f"s{i}.caption", "SNR", "S N R", "TTS read SNR as a word", kind="lexicon_snr", db=db)
    lid = L.promote(db=db)[0]["id"]
    assert L.get_lessons("ntn", db=db)["lessons"] == []            # candidates are not served
    L.approve(lid, rule="Spell out SNR as 'S N R' in narration", scope={"tool": "make_video"}, db=db)
    for i in range(30):                                            # plenty of noise to test the budget
        les = L.add_lesson(f"rule number {i} " + "x" * 150, scope={"product": "other"}, status="approved", db=db)
    r = L.get_lessons({"tool": "make_video", "product": "ntn"}, db=db)
    assert r["lessons"][0]["id"] == lid
    assert len(r["text"]) <= 3200 and len(r["lessons"]) <= 8
    assert all(x["scope"].get("product") != "other" for x in r["lessons"])   # scope conflict filtered
    r2 = L.get_lessons("free text about make_video", limit=50, db=db)
    assert len(r2["text"]) <= 3200 and r2["more"] > 0
    L.retire(lid, db=db)
    assert all(x["id"] != lid for x in L.get_lessons({"tool": "make_video"}, db=db)["lessons"])
    assert les["status"] == "approved"


def test_active_actions(db):
    for i in range(3):
        L.log_change(f"j{i}", "lexicon", None, {"written": "PUSCH", "spoken": "push"}, "TTS mispronounced PUSCH",
                     kind="lexicon", db=db)
    with pytest.raises(ToolError):
        L.log_change("j", "lexicon", None, {"written": "x"}, "bad", kind="lexicon", db=db)
    les = L.promote(db=db)[0]
    assert les["action"] == {"written": "PUSCH", "spoken": "push"}
    assert L.active_actions("lexicon", db=db) == []
    L.approve(les["id"], db=db)
    assert L.active_actions("lexicon", db=db) == [{"written": "PUSCH", "spoken": "push", "lesson_id": les["id"]}]
    L.add_lesson("pad boxes", kind="box_pad", action={"px": 12}, status="approved", db=db)
    assert L.active_actions("box_pad", db=db)[0]["px"] == 12
    with pytest.raises(ToolError):
        L.active_actions("nope", db=db)


def test_gaps_and_review(db, tmp_path):
    gid = L.report_gap("make_doc", "per-UE table", "wanted a column per UE", db=db)
    assert [g["id"] for g in L.list_gaps(db=db)] == [gid]
    with pytest.raises(ToolError):
        L.report_gap("", "x", db=db)
    for i in range(3):
        L.log_change(f"j{i}", f"s{i}.zoom", 1, 2, "too small <to read>", db=db)
    L.promote(db=db)
    p = L.render_review_html(tmp_path / "review.html", db=db)
    html = p.read_text(encoding="utf-8")
    assert "per-UE table" in html and "Candidate lessons (1)" in html and "&lt;to read&gt;" in html
    L.close_gap(gid, db=db)
    assert L.list_gaps(db=db) == [] and len(L.list_gaps(None, db=db)) == 1


def test_default_path_uses_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CASEFILE_HOME", str(tmp_path))
    monkeypatch.delenv("CASEFILE_LESSONS_DB", raising=False)
    L.report_gap("t", "n")
    assert (tmp_path / "lessons.db").exists()
