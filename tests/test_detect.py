"""Phase 1 gate: probe reports every planted defect in the fixture with exact frame numbers."""
import json

import pytest

from casefile import detect
from casefile.errors import ToolError
from casefile.media import FrameStore


@pytest.fixture(scope="module")
def vid(fixture_video, tmp_path_factory):
    """Fixture video analysed in an isolated CASEFILE_HOME (fresh caches)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("CASEFILE_HOME", str(tmp_path_factory.mktemp("home")))
    FrameStore.clear_registry()
    yield fixture_video
    FrameStore.clear_registry()
    mp.undo()


@pytest.fixture(scope="module")
def pr(vid):
    return detect.probe(vid[0])


def test_metadata(pr, vid):
    _, truth = vid
    assert pr["size"] == truth["size"]
    assert pr["frames"] == truth["n_frames"]
    assert abs(pr["fps"] - truth["fps"]) < 0.01
    assert abs(pr["duration"] - 20.0) < 0.1
    assert pr["audio_db"] is None or pr["audio_db"] < -60


def test_bad_frames_exact(pr, vid):
    _, truth = vid
    got = {b["frame"]: b["kind"] for b in pr["bad_frames"]}
    want = {b["frame"]: b["kind"] for b in truth["bad_frames"]}
    assert got == want
    for b in pr["bad_frames"]:
        assert b["t"] == round(b["frame"] / 30, 2)
    assert pr["popups"] == [round(312 / 30, 2)]


def test_no_false_positives_in_scroll(pr, vid):
    sc = vid[1]["scroll"]
    planted = {b["frame"] for b in vid[1]["bad_frames"]}
    for b in pr["bad_frames"]:
        if b["frame"] not in planted:
            assert not (sc["t0"] - 0.1 <= b["t"] <= sc["t1"] + 0.1)
    for t in pr["scenes"][1:] + pr["layout_changes"]:
        assert not (sc["t0"] - 0.1 <= t <= sc["t1"] + 0.1)


def test_layout_change(pr, vid):
    for lc in vid[1]["layout_change"]:
        assert any(abs(t - lc) <= 1 / 30 + 1e-6 for t in pr["layout_changes"])
    assert len(pr["layout_changes"]) == 1
    assert any(abs(t - 10.0) < 0.04 for t in pr["scenes"])


def test_compact_and_cached(pr, vid):
    assert len(json.dumps(pr)) <= 2400
    fs = FrameStore(vid[0])
    assert (fs.dir / "probe.json").exists()
    assert (fs.dir / "thumbs.npy").exists()
    assert detect.probe(vid[0]) == pr


def test_remap(vid):
    v = vid[0]
    assert detect.bad_frame_list(v) == [123, 257, 312, 388]
    m = detect.remap_for(v, "auto")
    assert m == {123: 122, 257: 256, 312: 311, 388: 387}
    assert detect.remap_for(v, "none") == {}
    assert detect.remap_for(v, [0, 1, 5, 6]) == {0: 2, 1: 2, 5: 4, 6: 4}
    with pytest.raises(ToolError):
        detect.remap_for(v, "bogus")
