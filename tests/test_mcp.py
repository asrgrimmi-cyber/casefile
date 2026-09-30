"""MCP server: tool list, description budget, per-tool response budgets (chars/4 tokens), sandbox errors."""
import asyncio
import json
from pathlib import Path

import pytest

from casefile.mcp_server import server

ROOT = Path(__file__).resolve().parents[1]
SIZES = ROOT / "tests" / "fixtures" / "out" / "mcp_sizes.json"
TOOLS = {"probe", "find_text", "ocr_region", "track_text", "chart_extract", "contact_sheet", "tts", "plan", "render"}
BUDGET_TOK = {"probe": 600, "find_text": 300, "ocr_region": 800, "track_text": 500, "chart_extract": 600,
              "tts": 300, "plan": 800, "render": 400, "contact_sheet": 300}


def _run(coro):
    return asyncio.run(coro)


async def _call(name, args):
    from mcp import Client
    async with Client(server) as c:
        return await c.call_tool(name, args)


def call(name, args):
    """Call a tool in-process -> (text of first content block, all content blocks)."""
    r = _run(_call(name, args))
    assert not r.is_error, r.content
    return r.content[0].text, r.content


def record(name, text):
    """Check the token budget and record the measured size for docs/tools.md."""
    n = len(text)
    assert n / 4 <= BUDGET_TOK[name], f"{name}: {n} chars > {BUDGET_TOK[name]} tokens"
    d = json.loads(SIZES.read_text()) if SIZES.exists() else {}
    d[name] = n
    SIZES.parent.mkdir(parents=True, exist_ok=True)
    SIZES.write_text(json.dumps(d, indent=1, sort_keys=True))
    out = json.loads(text)
    assert "error" not in out, out
    return out


def test_tool_list_and_description_budget():
    async def go():
        from mcp import Client
        async with Client(server) as c:
            return (await c.list_tools()).tools
    tools = _run(go())
    assert TOOLS <= {t.name for t in tools}   # legacy 9 stay registered next to the v2 tools (test_mcp_v2.py)
    total = sum(len(t.description or "") + len(json.dumps(t.input_schema, separators=(",", ":"))) for t in tools)
    d = json.loads(SIZES.read_text()) if SIZES.exists() else {}
    d["_descriptions_and_schemas"] = total
    SIZES.parent.mkdir(parents=True, exist_ok=True)
    SIZES.write_text(json.dumps(d, indent=1, sort_keys=True))
    assert total <= 10_000, total


def test_probe(fixture_video):
    mp4, truth = fixture_video
    out = record("probe", call("probe", {"video": str(mp4)})[0])
    assert out["size"] == truth["size"] and out["frames"] == truth["n_frames"]


def test_find_text(fixture_video):
    mp4, _ = fixture_video
    out = record("find_text", call("find_text", {"video": str(mp4), "t": 2.0, "query": "NTN", "row": True})[0])
    assert out["matches"] and "row_box" in out["matches"][0]


def test_ocr_region(fixture_video):
    mp4, _ = fixture_video
    out = record("ocr_region", call("ocr_region", {"video": str(mp4), "t": 2.0})[0])
    assert out["lines"]


def test_track_text(fixture_video):
    mp4, _ = fixture_video
    out = record("track_text", call("track_text", {"video": str(mp4), "query": "Band", "t0": 0, "t1": 9, "step": 1.0})[0])
    assert out["spans"]


def test_chart_extract(fixture_video):
    mp4, truth = fixture_video
    c = truth["chart"]
    series = [{"name": k, "rgb": v["rgb"], "tol": 50} for k, v in c["series"].items()]
    out = record("chart_extract", call("chart_extract", {"video": str(mp4), "t": 12.0, "plot_region": c["plot"],
                                                         "series": series, "x_ticks": c["x_ticks"],
                                                         "y_ticks": c["y_ticks"]})[0])
    assert set(out["series"]) == set(c["series"])


def test_contact_sheet_returns_image(fixture_video):
    mp4, _ = fixture_video
    text, content = call("contact_sheet", {"video": str(mp4), "n": 4})
    out = record("contact_sheet", text)
    assert len(out["times"]) == 4
    assert [type(x).__name__ for x in content] == ["TextContent", "ImageContent"]
    assert content[1].mime_type == "image/png"


@pytest.mark.slow
def test_tts():
    text, _ = call("tts", {"lines": [{"id": "a", "text": "Hello from Casefile."}], "speed": 1.0})
    out = record("tts", text)
    assert out["durations"]["a"] > 0.5


@pytest.mark.slow
def test_plan():
    out = record("plan", call("plan", {"spec": str(ROOT / "examples" / "fixture.json")})[0])
    assert out["total"] > 10 and out["shots"]


@pytest.mark.slow
def test_render_preview_with_thumbs(tmp_path):
    spec = json.loads((ROOT / "examples" / "fixture.json").read_text(encoding="utf-8"))
    spec["source"] = str(ROOT / spec["source"])
    text, content = call("render", {"spec": spec, "preview": True, "out": str(tmp_path / "p.mp4"), "thumbs": True})
    out = record("render", text)
    assert Path(out["path"]).exists()
    assert [type(x).__name__ for x in content] == ["TextContent", "ImageContent"]


def test_errors_are_json_not_tracebacks(fixture_video):
    text, _ = call("probe", {"video": "does/not/exist.mp4"})
    out = json.loads(text)
    assert set(out) == {"error", "hint"} and "Traceback" not in text
    text, _ = call("plan", {"spec": {"shots": "nope"}})
    assert set(json.loads(text)) == {"error", "hint"}


def test_sandbox(fixture_video, tmp_path, monkeypatch):
    mp4, _ = fixture_video
    monkeypatch.setenv("CASEFILE_WORKDIR", str(tmp_path))
    for name, args in (("probe", {"video": str(mp4)}),
                       ("find_text", {"video": str(mp4), "t": 1, "query": "NTN"}),
                       ("contact_sheet", {"video": str(mp4)})):
        text, _ = call(name, args)
        out = json.loads(text)
        assert set(out) == {"error", "hint"} and "outside" in out["error"], (name, out)
