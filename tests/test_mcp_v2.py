"""MCP v2 tools: job flow, local/GPU backends (fake worker), patch/verify, response budgets (chars/4 tokens).

casefile.timeline (worker A) is replaced by a fake module built on tests/fixtures/timeline_sample, so these tests
exercise only the front door (api_v2 + client + mcp_server) and stay fast.
"""
import asyncio
import hashlib
import json
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from casefile import api_v2
from casefile import client as C
from casefile.mcp_server import server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "timeline_sample"))
import make_sample  # noqa: E402

SIZES = ROOT / "tests" / "fixtures" / "out" / "mcp_sizes.json"
V2 = ["analyze_recording", "query_timeline", "locate", "make_video", "make_doc", "wait_job", "patch", "verify",
      "worker_status", "look", "get_lessons", "log_change", "report_gap"]
LEGACY = {"probe", "find_text", "ocr_region", "track_text", "chart_extract", "contact_sheet", "tts", "plan", "render"}
BUDGET_TOK = {"analyze_recording": 3000, "query_timeline": 500, "locate": 500, "wait_job": 400, "patch": 200,
              "verify": 400, "worker_status": 150}


# ------------------------------------------------------------------ harness
async def _call(name, args):
    from mcp import Client
    async with Client(server) as c:
        return await c.call_tool(name, args)


def call(name, args):
    r = asyncio.run(_call(name, args))
    assert not r.is_error, r.content
    return r.content[0].text, r.content


def record(name, text, key=None):
    n = len(text)
    assert n / 4 <= BUDGET_TOK[name], f"{name}: {n} chars > {BUDGET_TOK[name]} tokens"
    d = json.loads(SIZES.read_text()) if SIZES.exists() else {}
    d[key or f"v2.{name}"] = n
    SIZES.parent.mkdir(parents=True, exist_ok=True)
    SIZES.write_text(json.dumps(d, indent=1, sort_keys=True))
    return text


def jcall(name, args):
    text, content = call(name, args)
    return json.loads(text), text, content


# ------------------------------------------------------------------ fake timeline module
def _summary(tl):
    return {"video": tl.get("video"), "steps": [
        {k: st.get(k) for k in ("id", "t0", "t1", "kind", "title", "fields", "commands")}
        | {"items": [f"{it['id']}={it['text']}" for it in st.get("items", [])]} for st in tl["steps"]]}


def _find(tl, text, step=None):
    t = str(text).lower().replace(" ", "_")
    for st in tl["steps"]:
        if step and st["id"] != step:
            continue
        for it in st["items"]:
            if t == it["id"] or t in it["id"] or str(text).lower() in it["text"].lower():
                return it
    return None


def make_fake_timeline(analyze_sec=0.0):
    m = types.ModuleType("casefile.timeline")
    m.calls = []

    def load(p):
        p = Path(p)
        if p.suffix != ".json":
            p = api_v2._local_tl_path(p)
        tl = json.loads(p.read_text(encoding="utf-8"))
        tl["_dir"] = str(p.parent)
        return tl

    def analyze(video, focus=None, speech="auto", ocr="auto"):
        m.calls.append(video)
        time.sleep(analyze_sec)
        dest = api_v2._local_tl_path(Path(video))
        make_sample.build(dest.parent)
        return _summary(load(dest))

    def query(tl, questions):
        out = []
        for q in questions:
            it = _find(tl, q)
            out.append({"q": q, "id": it and it["id"], "text": it and it["text"], "box": it and it["box"],
                        "t": it and it["t"]})
        return out

    def locate(tl, items):
        out = []
        for x in items:
            it = _find(tl, x["text"], x.get("step"))
            out.append({"text": x["text"], "found": bool(it), "id": it and it["id"], "box": it and it["box"],
                        "t": it and it["t"]})
        return out

    m.load, m.analyze, m.summary, m.query, m.locate = load, analyze, _summary, query, locate
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated CASEFILE_HOME, no worker, fresh LocalRunner, fake timeline."""
    monkeypatch.setenv("CASEFILE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CASEFILE_WORKER_URL", raising=False)
    monkeypatch.delenv("CASEFILE_WORKDIR", raising=False)
    monkeypatch.setattr(C, "_RUNNER", None)
    monkeypatch.setattr(C, "_CLIENT", None)
    fake = make_fake_timeline()
    monkeypatch.setitem(sys.modules, "casefile.timeline", fake)
    import casefile
    monkeypatch.setattr(casefile, "timeline", fake, raising=False)
    return tmp_path, fake


@pytest.fixture
def analysed(env):
    """A dummy recording with its timeline already in the cache."""
    tmp, fake = env
    v = tmp / "rec.mp4"
    v.write_bytes(b"not really a video " * 50)
    make_sample.build(api_v2._local_tl_path(v).parent)
    return v


def _wait_done(job_id, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        out, text, _ = jcall("wait_job", {"job_id": job_id, "max_wait": 5})
        if out.get("state") != "running" and out.get("state") != "queued":
            return out, text
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ tool list
def test_tool_list_v2_first_and_description_budget():
    async def go():
        from mcp import Client
        async with Client(server) as c:
            return (await c.list_tools()).tools
    tools = asyncio.run(go())
    names = [t.name for t in tools]
    assert names[:len(V2)] == V2
    assert set(names[len(V2):]) == LEGACY
    sizes = {t.name: len(t.description or "") + len(json.dumps(t.input_schema, separators=(",", ":"))) for t in tools}
    total = sum(sizes.values())
    d = json.loads(SIZES.read_text()) if SIZES.exists() else {}
    d["_descriptions_and_schemas"] = total
    d["_per_tool_desc_schema"] = sizes
    SIZES.parent.mkdir(parents=True, exist_ok=True)
    SIZES.write_text(json.dumps(d, indent=1, sort_keys=True))
    assert total <= 10_000, total
    look = next(t for t in tools if t.name == "look")
    assert "LAST RESORT" in look.description


# ------------------------------------------------------------------ analyze
def test_analyze_returns_job_id_fast_with_slow_backend(env, monkeypatch):
    tmp, _ = env
    slow = make_fake_timeline(analyze_sec=4.8)
    monkeypatch.setitem(sys.modules, "casefile.timeline", slow)
    v = tmp / "new.mp4"
    v.write_bytes(b"x" * 1000)
    t0 = time.monotonic()
    out, text, _ = jcall("analyze_recording", {"video": str(v)})
    assert time.monotonic() - t0 < 5.0
    assert out["state"] in ("queued", "running") and out["job_id"] and out["backend"] == "local"
    st, wtext = _wait_done(out["job_id"])
    record("wait_job", wtext, "v2.wait_job.analyze")
    assert st["state"] == "done" and st["kind"] == "analyze" and st["steps"] == 2
    # second call: cached -> summary inline, analysis not re-run
    out2, text2, _ = jcall("analyze_recording", {"video": str(v)})
    assert out2["state"] == "done" and out2["summary"]["steps"][0]["id"] == "s1"
    assert len(slow.calls) == 1
    record("analyze_recording", text2)


def test_analyze_summary_is_trimmed_to_budget(env, monkeypatch):
    tmp, fake = env
    big = {"steps": [{"id": f"s{i}", "title": "T" * 40, "transcript": "log line " * 400,
                      "items": [{"id": f"s{i}.it{k}", "text": "value " * 5} for k in range(40)],
                      "fields": {f"f{k}": k for k in range(10)}} for i in range(40)]}
    monkeypatch.setattr(fake, "analyze", lambda video, **kw: big)
    v = tmp / "big.mp4"
    v.write_bytes(b"y" * 100)
    out, text, _ = jcall("analyze_recording", {"video": str(v)})
    assert out["state"] == "done"
    record("analyze_recording", text, "v2.analyze_recording.trimmed")
    assert len(text) <= 12_000


def test_analyze_error_and_sandbox(env, monkeypatch, tmp_path):
    out, _, _ = jcall("analyze_recording", {"video": "nope/missing.mp4"})
    assert set(out) == {"error", "hint"}
    monkeypatch.setenv("CASEFILE_WORKDIR", str(tmp_path / "sandbox"))
    (tmp_path / "sandbox").mkdir()
    out, _, _ = jcall("query_timeline", {"video": str(ROOT / "README.md"), "questions": ["x"]})
    assert "outside" in out["error"]


def test_missing_timeline_module_is_graceful(env, monkeypatch, analysed):
    monkeypatch.setitem(sys.modules, "casefile.timeline", None)   # import -> ImportError
    out, _, _ = jcall("locate", {"video": str(analysed), "items": ["NTN"]})
    assert "not installed" in out["error"] and out["hint"]


# ------------------------------------------------------------------ query / locate
def test_query_timeline_lines(analysed):
    text, _ = call("query_timeline", {"video": str(analysed), "questions": ["pusch_snr", "ntn", "zzz_nothing"]})
    record("query_timeline", text)
    lines = text.splitlines()
    assert len(lines) == 3
    q, iid, txt, box, t = [x.strip() for x in lines[1].split("|")]
    assert (q, iid, txt) == ("ntn", "s1.ntn", "NTN") and box == "150,80,205,108" and t == "3.2"
    assert lines[2].split(" | ")[1] == "-"


def test_query_timeline_overflow_goes_to_file(analysed):
    qs = [f"band {i} " + "x" * 30 for i in range(80)]
    text, _ = call("query_timeline", {"video": str(analysed), "questions": qs})
    record("query_timeline", text, "v2.query_timeline.overflow")
    last = text.splitlines()[-1]
    assert " more in file " in last
    assert Path(last.split(" more in file ")[1]).exists()


def test_locate_lines(analysed):
    text, _ = call("locate", {"video": str(analysed), "items": ["Band", {"step": "s2", "text": "cqi"}, "Nope"]})
    record("locate", text)
    rows = [r.split(" | ") for r in text.splitlines()]
    assert rows[0][:3] == ["Band", "Y", "s1.band"] and rows[1][2] == "s2.cqi" and rows[2][1] == "N"


# ------------------------------------------------------------------ item refs (planner hook)
def test_resolve_item_refs_rect_and_default_times():
    from casefile import planner
    tl = json.loads((ROOT / "tests/fixtures/timeline_sample/timeline.json").read_text(encoding="utf-8"))
    spec = {"source": "x.mp4", "shots": [
        {"src": [[25, 35], [35, 45, 2.0]], "overlays": [
            {"type": "box", "item": "s2.pusch_snr", "label": "SNR"},
            {"type": "blur", "item": "s1.ntn", "from": 1, "to": 2}]}]}
    exp = {}
    out = planner.resolve_item_refs(spec, tl, exp)
    b = out["shots"][0]["overlays"][0]
    it = next(i for s in tl["steps"] for i in s["items"] if i["id"] == "s2.pusch_snr")
    assert b["rect"] == [float(v) for v in it["box"]] and "item" not in b
    st2 = tl["steps"][1]
    assert b["from"] == round(st2["t0"] - 25, 2) and b["to"] > b["from"]
    assert out["shots"][0]["overlays"][1]["from"] == 1
    assert exp[(0, 0)] == it["text"]
    assert "item" in spec["shots"][0]["overlays"][0]            # input untouched
    with pytest.raises(Exception, match="unknown timeline item"):
        planner.resolve_item_refs({"shots": [{"src": [[0, 1]], "overlays": [{"type": "box", "item": "s9.x"}]}]}, tl)


def test_load_spec_item_refs_without_timeline_module(monkeypatch):
    from casefile import planner
    from casefile.errors import ToolError
    monkeypatch.setitem(sys.modules, "casefile.timeline", None)
    with pytest.raises(ToolError, match="not installed"):
        planner.load_spec({"source": "x.mp4", "shots": [{"src": [[0, 1]], "overlays": [{"type": "box", "item": "s1.a"}]}]})


# ------------------------------------------------------------------ make_video + verify (render faked)
@pytest.fixture
def fixture_analysed(env, fixture_video):
    mp4, _ = fixture_video
    make_sample.build(api_v2._local_tl_path(mp4).parent)
    return mp4


def test_make_video_item_refs_job_and_verify(env, fixture_analysed, monkeypatch):
    from casefile import render as R
    tmp, _ = env
    rendered = {}

    def fake_render(plan, preview=False, out=None, **kw):
        rendered["plan"] = plan
        Path(out).write_bytes(b"mp4")
        return {"path": str(out), "duration": round(plan.total, 2), "frames": 10, "size_mb": 0.1,
                "size": [960, 540] if preview else [1920, 1080], "srt": None, "thumbs": []}
    monkeypatch.setattr(R, "render", fake_render)
    spec = {"source": str(fixture_analysed), "voice": {"engine": "none"}, "bad_frames": "none",
            "lines": {"a": {"caption": "The network type is shown here."}},
            "shots": [{"src": [[0.5, 9.5]], "lines": ["a"], "overlays": [
                {"type": "box", "item": "s1.ntn", "label": "NTN", "from": 0.2, "to": "end"},
                {"type": "box", "item": "s1.band", "label": "Band", "from": 0.2, "to": "end"}]}]}
    out_mp4 = tmp / "out" / "v.mp4"
    t0 = time.monotonic()
    out, _, _ = jcall("make_video", {"spec": spec, "out": str(out_mp4)})
    assert time.monotonic() - t0 < 5 and out["job_id"] and out["backend"] == "local"
    st, wtext = _wait_done(out["job_id"])
    record("wait_job", wtext)
    assert st["state"] == "done" and st["path"] == str(out_mp4)
    ov = rendered["plan"].shots[0].overlays[0]
    assert ov["rect"] == [150.0, 80.0, 205.0, 108.0]
    checks = json.loads(out_mp4.with_name("v.checks.json").read_text())["checks"]
    assert [c["expect"] for c in checks] == ["NTN", "Band"] and len(checks[0]["rect"]) == 4

    # verify: OCR of the output faked -> first passes, second fails (with a crop)
    from PIL import Image
    monkeypatch.setattr(api_v2, "_frame", lambda v, t, dest: Image.new("RGB", (1920, 1080), "white"))
    answers = iter(["NTN", "Latitude 12.5"])      # box drifted onto the wrong row
    monkeypatch.setattr(api_v2, "_ocr_text", lambda img, reg: next(answers))
    text, content = call("verify", {"path": str(out_mp4)})
    record("verify", text)
    v = json.loads(text)
    assert (v["pass"], v["fail"]) == (1, 1) and v["lines"][0].startswith("PASS") and v["lines"][1].startswith("FAIL")
    assert len(v["crops"]) == 1 and Path(v["crops"][0]).exists()
    assert [type(c).__name__ for c in content] == ["TextContent", "ImageContent"]


def test_make_video_bad_item_is_immediate_error(env, fixture_analysed):
    spec = {"source": str(fixture_analysed), "shots": [{"src": [[0, 2]], "overlays": [{"type": "box", "item": "s1.nope"}]}]}
    out, _, _ = jcall("make_video", {"spec": spec})
    assert "unknown timeline item" in out["error"]


def test_fuzzy_ok():
    assert api_v2.fuzzy_ok("0x461e", "RNTI: Ox46le")[0]
    assert api_v2.fuzzy_ok("PUSCH SNR", "pusch_snr 23.4")[0]
    assert not api_v2.fuzzy_ok("Band", "Latitude")[0]


# ------------------------------------------------------------------ make_doc (real docs module, local)
def test_make_doc_local_then_verify(analysed, tmp_path):
    pytest.importorskip("casefile.docs")
    doc = {"title": "TC-017 UE attach", "summary": "Checks the UE attaches.",
           "steps": [{"step": "s1", "heading": "Open NTN", "body": "Open the NTN tab.", "highlight": ["s1.ntn"]}]}
    out, _, _ = jcall("make_doc", {"video": str(analysed), "doc": doc, "formats": ["md"], "out": str(tmp_path / "d")})
    assert out["backend"] == "local"
    st, wtext = _wait_done(out["job_id"])
    record("wait_job", wtext, "v2.wait_job.doc")
    assert st["state"] == "done", st
    md = Path(st["paths"]["md"])
    assert md.exists() and md.is_relative_to(tmp_path / "d")
    text, _ = call("verify", {"path": str(md)})
    record("verify", text, "v2.verify.doc")
    v = json.loads(text)
    assert v["pass"] + v["fail"] == 1 and v["lines"][0].split()[1] == "step1"


# ------------------------------------------------------------------ GPU backend via fake worker
class DocWorker:
    """Fake worker: blob store (dedupe), analyze/doc jobs that finish on the 3rd poll, file downloads."""

    def __init__(self, fail_submit=False):
        self.blobs, self.jobs, self.polls, self.fail_submit = {}, {}, [], fail_submit

    def __call__(self, req):
        p = req.url.path
        if p == "/status":
            return httpx.Response(200, json={"version": "t", "gpu": {"name": "GTX Fake", "vram_free": 3000},
                                             "ram_free": 6000, "queue": 0, "backend_caps": {"nvenc": True, "whisper": False}})
        if p.startswith("/blobs/"):
            sha = p.rsplit("/", 1)[1]
            if req.method == "HEAD":
                return httpx.Response(200 if sha in self.blobs else 404, headers={"Upload-Offset": "0"})
            self.blobs[sha] = req.content
            return httpx.Response(200, json={"complete": True, "size": len(req.content)})
        if p == "/jobs":
            if self.fail_submit:
                return httpx.Response(503)
            b = json.loads(req.content)
            jid = f"w{len(self.jobs) + 1}"
            self.jobs[jid] = {**b, "n": 0}
            return httpx.Response(200, json={"job_id": jid})
        if p.startswith("/jobs/"):
            j = self.jobs[p.rsplit("/", 1)[1]]
            self.polls.append(req.url.params.get("wait"))
            j["n"] += 1
            if j["n"] < 3:
                return httpx.Response(200, json={"state": "running", "progress": 0.4})
            if j["kind"] == "doc":
                res = {"paths": {"md": "/srv/outputs/" + p.rsplit("/", 1)[1] + "/tc-017/tc-017.md"},
                       "dir": "/srv/outputs/x/tc-017", "pages": None, "steps": 1, "warnings": ["w1"],
                       "files": ["tc-017/tc-017.md"]}
            else:
                res = {"summary": {"steps": [{"id": "s1", "title": "GUI"}], "video": {"duration": 60}},
                       "files": ["timeline.json"]}
            return httpx.Response(200, json={"state": "done", "progress": 1, "result": res})
        if p.startswith("/files/"):
            return httpx.Response(200, content=b"# TC-017\n" if p.endswith(".md") else b'{"steps":[]}')
        return httpx.Response(404)


def _use_worker(monkeypatch, fw):
    monkeypatch.setenv("CASEFILE_WORKER_URL", "http://fake:8765")
    C.set_client(C.WorkerClient("http://fake:8765", "t", transport=httpx.MockTransport(fw), backoff=0.0))


def test_make_doc_on_gpu_worker_downloads_locally(analysed, monkeypatch, tmp_path):
    fw = DocWorker()
    _use_worker(monkeypatch, fw)
    try:
        out, _, _ = jcall("make_doc", {"video": str(analysed), "doc": {"title": "TC-017"}, "out": str(tmp_path / "o")})
        assert out["backend"] == "gpu"
        st, wtext = _wait_done(out["job_id"])
        record("wait_job", wtext, "v2.wait_job.gpu_doc")
        assert st["state"] == "done" and st["backend"] == "gpu" and st["warnings"] == ["w1"]
        md = Path(st["paths"]["md"])
        assert md == tmp_path / "o" / "tc-017" / "tc-017.md" and md.read_text() == "# TC-017\n"
        assert fw.jobs["w1"]["args"]["timeline"] == hashlib.sha256(analysed.read_bytes()).hexdigest()
        assert all(w == "50.0" for w in fw.polls)                   # long-poll at the 50 s cap
        ws, wstext, _ = jcall("worker_status", {})
        record("worker_status", wstext)
        assert ws["backend"] == "gpu" and ws["gpu"] == "GTX Fake" and ws["caps"] == "nvenc"
    finally:
        C.set_client(None)


def test_analyze_on_gpu_uploads_once(env, monkeypatch):
    tmp, _ = env
    fw = DocWorker()
    _use_worker(monkeypatch, fw)
    try:
        v = tmp / "g.mp4"
        v.write_bytes(b"z" * 5000)
        out, _, _ = jcall("analyze_recording", {"video": str(v)})
        st = out if out.get("state") == "done" else _wait_done(out["job_id"])[0]
        assert st["backend"] == "gpu"
        sha = hashlib.sha256(v.read_bytes()).hexdigest()
        assert list(fw.blobs) == [sha]
        assert fw.jobs["w1"]["args"]["video"] == f"blob:{sha}.mp4"
        assert api_v2._local_tl_path(v).exists()                     # timeline downloaded into the local cache
    finally:
        C.set_client(None)


def test_fallback_to_local_when_worker_down(analysed, monkeypatch, tmp_path):
    def down(req):
        raise httpx.ConnectError("refused", request=req)
    _use_worker(monkeypatch, down)
    try:
        out, _, _ = jcall("make_doc", {"video": str(analysed), "doc": {"title": "Fallback"}, "formats": ["md"],
                                       "out": str(tmp_path / "fb")})
        assert out["backend"] == "local"
        ws, text, _ = jcall("worker_status", {})
        record("worker_status", text, "v2.worker_status.down")
        assert ws["backend"] == "local" and ws["worker"] == "unreachable"
    finally:
        C.set_client(None)


def test_fallback_when_submit_fails_midway(analysed, monkeypatch, tmp_path):
    pytest.importorskip("casefile.docs")
    _use_worker(monkeypatch, DocWorker(fail_submit=True))
    try:
        out, _, _ = jcall("make_doc", {"video": str(analysed), "doc": {"title": "Retry"}, "formats": ["md"],
                                       "out": str(tmp_path / "fb2")})
        assert out["backend"] == "gpu"
        st, _ = _wait_done(out["job_id"])
        assert st["state"] == "done" and st["backend"] == "local"
        assert any("ran locally" in w for w in st["warnings"])
    finally:
        C.set_client(None)


def test_wait_job_unknown_and_running(env):
    out, _, _ = jcall("wait_job", {"job_id": "nope"})
    assert set(out) == {"error", "hint"}
    r = C.get_runner()
    jid = r.start(lambda prog: (prog(0.25), time.sleep(1.0), {"x": 1})[2], "render", {"kind": "render"})
    out, text, _ = jcall("wait_job", {"job_id": jid, "max_wait": 0.2})
    assert out["state"] == "running" and out["progress"] == 0.25 and out["backend"] == "local"
    record("wait_job", text, "v2.wait_job.running")


# ------------------------------------------------------------------ patch
SPEC = {"voice": {"engine": "none"}, "lines": {"a": {"caption": "Hello there from the patch test."}},
        "shots": [{"card": "intro", "title": ["A", "B"], "lines": ["a"]},
                  {"card": "outro", "title": "Done", "bullets": ["x"], "lines": []}]}


def test_patch_refuses_without_why(env, tmp_path):
    p = tmp_path / "edit.json"
    p.write_text(json.dumps(SPEC))
    for why in ("", "   "):
        out, _, _ = jcall("patch", {"path": str(p), "ops": [{"op": "set", "path": "/shots/0/title", "value": "X"}],
                                    "why": why})
        assert "why is required" in out["error"]
    assert json.loads(p.read_text()) == SPEC


def test_patch_applies_logs_and_replans(env, tmp_path, monkeypatch):
    logged = []
    fake = types.ModuleType("casefile.lessons")
    fake.log_change = lambda job, target, before, after, why, **kw: logged.append((job, target, before, after, why))
    monkeypatch.setitem(sys.modules, "casefile.lessons", fake)
    p = tmp_path / "edit.json"
    p.write_text(json.dumps(SPEC))
    ops = [{"op": "set", "path": "/shots/0/title/1", "value": "Setup"},
           {"op": "add", "path": "/shots/1/bullets/-", "value": "y"},
           {"op": "del", "path": "/shots/1/bullets/0"},
           {"op": "set", "path": "/shots/1/tail", "value": 2.0}]
    out, text, _ = jcall("patch", {"path": str(p), "ops": ops, "why": "title was cut off in preview"})
    record("patch", text)
    assert out["ok"] and out["changed"] == 4 and out["logged"] == 4 and out["total"] > 3
    new = json.loads(p.read_text())
    assert new["shots"][0]["title"] == ["A", "Setup"] and new["shots"][1]["bullets"] == ["y"]
    assert new["shots"][1]["tail"] == 2.0
    assert logged[0] == ("edit", "edit.json/shots/0/title/1", "B", "Setup", "title was cut off in preview")


def test_patch_bad_path_is_error_and_file_untouched(env, tmp_path):
    p = tmp_path / "edit.json"
    p.write_text(json.dumps(SPEC))
    out, _, _ = jcall("patch", {"path": str(p), "ops": [{"op": "set", "path": "/shots/9/x", "value": 1}], "why": "t"})
    assert "path not found" in out["error"]
    assert json.loads(p.read_text()) == SPEC


def test_apply_ops_unit():
    d = {"a": [1, 2, 3], "b": {"c": 1}}
    ch = api_v2.apply_ops(d, [{"op": "add", "path": "/a/0", "value": 0}, {"op": "del", "path": "/b/c"},
                              {"op": "set", "path": "/b/d~1e", "value": 5}])
    assert d == {"a": [0, 1, 2, 3], "b": {"d/e": 5}} and len(ch) == 3


# ------------------------------------------------------------------ lessons passthrough + look
def test_lessons_tools(env):
    pytest.importorskip("casefile.lessons")
    out, _, _ = jcall("log_change", {"target": "s1.box", "before": 1, "after": 2, "why": "clipped"})
    assert out["ok"] and out["id"] >= 1
    out, _, _ = jcall("report_gap", {"tool": "make_doc", "need": "per-UE table"})
    assert out["ok"]
    out, text, _ = jcall("get_lessons", {"context": "enb gui"})
    assert "text" in out and len(text) < 3300
    out, _, _ = jcall("log_change", {"target": "s1.box", "before": 1, "after": 2, "why": ""})
    assert "why" in out["error"]


def test_look_returns_one_image(fixture_video):
    mp4, _ = fixture_video
    text, content = call("look", {"video": str(mp4), "times": [1.0, 5.0]})
    assert json.loads(text)["times"] == [1.0, 5.0]
    assert [type(c).__name__ for c in content] == ["TextContent", "ImageContent"]
