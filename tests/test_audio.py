import time

import numpy as np
import pytest
import soundfile as sf

from casefile import audio
from casefile.errors import ToolError

LINES = [
    {"id": "c1", "text": "On the Cell page, NTN is set to True."},
    {"id": "c2", "text": "The band is n256, a 2 GHz S-band."},
    {"id": "s1", "text": "We configure one thousand UEs."},
    {"id": "s2", "text": "All on AS release seventeen."},
    {"id": "l1", "text": "The cell broadcasts SIB19 ▶ the NTN configuration."},
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEFILE_HOME", str(tmp_path))
    return tmp_path


def test_speakable_expands_lexicon():
    assert audio.speakable("Band n256 on UE with SIB19", {}) == "Band n two five six on U E with SIB nineteen"


def test_speakable_rules():
    # longest key first, keys with spaces, whole-word, case-sensitive
    assert audio.speakable("AS release 17", {}) == "A.S. release 17"
    assert audio.speakable("UEs and UE", {}) == "U Es and U E"
    assert audio.speakable("QUEUE ue", {}) == "QUEUE ue"
    # user lexicon wins over default
    assert audio.speakable("UE", {"UE": "you ee"}) == "you ee"
    # glyphs
    assert audio.speakable("▶  Start ≈ 3 × faster", {}) == "Start about 3 times faster"
    assert "→" not in audio.speakable("Logs → RRC → BCCH", {})


def test_estimate():
    assert audio.estimate("one two three four five six seven eight nine ten one two three", 1.0) == 5.3
    assert audio.estimate("a b", 2.0) < audio.estimate("a b", 1.0)


def test_engine_none(home):
    r = audio.tts(LINES, voice="none", speed=1.0)
    assert set(r["durations"]) == {ln["id"] for ln in LINES}
    assert r["cached"] == []
    assert not list((home / "cache" / "tts").glob("*.wav"))


def test_missing_models(monkeypatch, tmp_path):
    monkeypatch.setattr(audio.config, "models_dir", lambda: tmp_path)
    with pytest.raises(ToolError) as e:
        audio.model_paths()
    assert "doctor" in e.value.hint


def test_trim_removes_silence():
    sr = audio.SR
    tone = 0.5 * np.sin(np.linspace(0, 2 * np.pi * 440, sr)).astype(np.float32)
    x = np.concatenate([np.zeros(sr, np.float32), tone, np.zeros(sr, np.float32)])
    y = audio._trim(x, sr)
    assert abs(y.size / sr - 1.0) < 0.1


def test_narration_track(tmp_path):
    sr = audio.SR
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(str(a), 0.7 * np.ones(sr, np.float32), sr, subtype="FLOAT")
    sf.write(str(b), -0.6 * np.ones(sr // 2, np.float32), sr, subtype="FLOAT")
    out = audio.narration_track([(a, 0.5), (b, 1.0), (b, 3.8)], total=4.0, out=tmp_path / "n.wav")
    data, osr = sf.read(str(out), dtype="float32")
    assert osr == sr and data.ndim == 1
    assert data.size == int(4.0 * sr)
    assert np.abs(data).max() <= 0.9
    assert abs(np.abs(data).max() - 0.89) < 0.01


@pytest.mark.slow
def test_tts_synth_and_cache(home):
    t = time.perf_counter()
    r = audio.tts(LINES, voice="af_heart", speed=1.1)
    wall = time.perf_counter() - t
    assert r["cached"] == []
    d = r["durations"]
    assert set(d) == {ln["id"] for ln in LINES}
    for v in d.values():
        assert 0.5 <= v <= 15
    print(f"\nsynth: {sum(d.values()):.1f}s audio in {wall:.1f}s wall ({sum(d.values()) / wall:.2f}x)")

    for ln in LINES:
        p = audio.wav_path(audio.speakable(ln["text"], {}), "af_heart", 1.1)
        info = sf.info(str(p))
        assert info.samplerate == 24000 and info.channels == 1 and info.subtype == "FLOAT"
        assert abs(audio.line_duration(p) - d[ln["id"]]) < 0.01

    t = time.perf_counter()
    r2 = audio.tts(LINES, voice="af_heart", speed=1.1)
    assert time.perf_counter() - t < 0.5
    assert sorted(r2["cached"]) == sorted(ln["id"] for ln in LINES)
    assert r2["durations"] == d

    # mix the real lines back to back
    places, t0 = [], 0.2
    for ln in LINES:
        places.append((audio.wav_path(audio.speakable(ln["text"], {}), "af_heart", 1.1), t0))
        t0 += d[ln["id"]] + 0.4
    total = round(t0, 2)
    out = audio.narration_track(places, total, home / "narr.wav")
    data, sr = sf.read(str(out), dtype="float32")
    assert data.size == int(round(total * sr))
    assert np.abs(data).max() <= 0.9
