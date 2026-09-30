"""CLI: same JSON as the MCP tools, exit codes, --pretty, doctor."""
import json

from typer.testing import CliRunner

from casefile import api
from casefile.cli import app

runner = CliRunner()


def test_help_lists_commands():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "probe", "find-text", "ocr", "track-text", "chart", "sheet", "tts", "plan", "render"):
        assert cmd in r.output


def test_probe_matches_api(fixture_video):
    mp4, _ = fixture_video
    r = runner.invoke(app, ["probe", str(mp4)])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == api.dumps(api.probe(str(mp4)))


def test_pretty(fixture_video):
    mp4, _ = fixture_video
    r = runner.invoke(app, ["find-text", str(mp4), "2", "NTN", "--row", "--pretty"])
    assert r.exit_code == 0, r.output
    assert "\n  " in r.output and json.loads(r.output)["matches"][0]["row_box"]


def test_ocr_and_sheet(fixture_video, tmp_path):
    mp4, _ = fixture_video
    r = runner.invoke(app, ["ocr", str(mp4), "2", "--region", "40,100,620,420"])
    assert r.exit_code == 0 and json.loads(r.output)["lines"]
    png = tmp_path / "s.png"
    r = runner.invoke(app, ["sheet", str(mp4), "--times", "1,5,12", "--out", str(png)])
    assert r.exit_code == 0, r.output
    assert png.exists() and json.loads(r.output)["times"] == [1.0, 5.0, 12.0]


def test_chart(fixture_video):
    mp4, truth = fixture_video
    c = truth["chart"]
    series = [{"name": k, "rgb": v["rgb"], "tol": 50} for k, v in c["series"].items()]
    r = runner.invoke(app, ["chart", str(mp4), "12", "--plot", ",".join(map(str, c["plot"])),
                            "--series", json.dumps(series), "--x-ticks", json.dumps(c["x_ticks"]),
                            "--y-ticks", json.dumps(c["y_ticks"])])
    assert r.exit_code == 0, r.output
    assert set(json.loads(r.output)["series"]) == set(c["series"])


def test_error_exit_code():
    r = runner.invoke(app, ["probe", "missing.mp4"])
    assert r.exit_code == 2
    assert set(json.loads(r.output)) == {"error", "hint"}


def test_bad_json_arg(fixture_video):
    mp4, _ = fixture_video
    r = runner.invoke(app, ["tts", "not json"])
    assert r.exit_code == 2 and "error" in json.loads(r.output)


def test_doctor(monkeypatch):
    monkeypatch.setattr(api, "doctor", lambda download=True: {"ok": False, "checks": {"x": {"ok": False, "detail": "nope"}}})
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "FAIL" in r.output
    monkeypatch.setattr(api, "doctor", lambda download=True: {"ok": True, "checks": {"x": {"ok": True, "detail": "fine"}}})
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0 and "all checks passed" in r.output
