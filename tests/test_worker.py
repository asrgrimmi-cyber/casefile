"""GPU worker HTTP API with fake job handlers (no GPU / models needed)."""
from __future__ import annotations

import hashlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from casefile.worker.app import create_app  # noqa: E402
from casefile.worker.blobs import BlobStore  # noqa: E402
from casefile.worker.jobs import substitute_blobs  # noqa: E402
from casefile.worker.queue import JobQueue  # noqa: E402

TOKEN = "test-token-123"
H = {"Authorization": f"Bearer {TOKEN}"}


def _status(queue, handlers):
    return {"version": "2", "gpu": None, "ram_free": 1, "queue": queue.counts(),
            "backend_caps": {"nvenc": False, "cuda_ocr": False, "whisper": False}}


def make(tmp_path, handlers, **kw):
    app = create_app(tmp_path, TOKEN, handlers=handlers, release=None, monitor=False, status_fn=_status, **kw)
    return app


def test_auth_required(tmp_path):
    app = make(tmp_path, {"echo": lambda a, c: a}, start_runner=False)
    with TestClient(app) as c:
        sha = "0" * 64
        for method, url in (("GET", "/status"), ("HEAD", f"/blobs/{sha}"), ("PUT", f"/blobs/{sha}?offset=0"),
                            ("POST", "/jobs"), ("GET", "/jobs/abc"), ("GET", "/files/abc/x.mp4")):
            assert c.request(method, url).status_code == 401, url
            assert c.request(method, url, headers={"Authorization": "Bearer wrong"}).status_code == 401, url
            assert c.request(method, url, headers={"Authorization": TOKEN}).status_code == 401, url
        r = c.get("/status", headers=H)
        assert r.status_code == 200 and r.json()["version"] == "2"


def test_empty_token_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CASEFILE_WORKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        create_app(tmp_path, "")


def test_blob_resumable_upload_and_verify(tmp_path):
    app = make(tmp_path, {"echo": lambda a, c: a}, start_runner=False)
    data = bytes(range(256)) * 400  # 102400 bytes
    sha = hashlib.sha256(data).hexdigest()
    with TestClient(app) as c:
        r = c.head(f"/blobs/{sha}", headers=H)
        assert r.status_code == 404 and r.headers["upload-offset"] == "0"
        r = c.put(f"/blobs/{sha}?offset=0&total={len(data)}", content=data[:40000], headers=H)
        assert r.status_code == 200 and r.json() == {"sha256": sha, "complete": False, "size": 40000}
        # "connection dropped": client asks where to resume
        r = c.head(f"/blobs/{sha}", headers=H)
        assert r.status_code == 404 and r.headers["upload-offset"] == "40000"
        # wrong offset -> 409 with the right one
        r = c.put(f"/blobs/{sha}?offset=10&total={len(data)}", content=data[10:20], headers=H)
        assert r.status_code == 409 and r.json()["offset"] == 40000
        r = c.put(f"/blobs/{sha}?offset=40000&total={len(data)}", content=data[40000:90000], headers=H)
        assert r.json()["size"] == 90000
        r = c.put(f"/blobs/{sha}?offset=90000&total={len(data)}", content=data[90000:], headers=H)
        assert r.status_code == 200 and r.json()["complete"] is True
        r = c.head(f"/blobs/{sha}", headers=H)
        assert r.status_code == 200 and r.headers["content-length"] == str(len(data))
        assert (tmp_path / "blobs" / sha).read_bytes() == data
        # uploading again is refused (already complete)
        assert c.put(f"/blobs/{sha}?offset=0", content=b"x", headers=H).status_code == 409
        # sha mismatch -> 422 and partial removed
        bad = hashlib.sha256(b"something else").hexdigest()
        r = c.put(f"/blobs/{bad}?offset=0&final=1", content=b"not it", headers=H)
        assert r.status_code == 422
        assert not (tmp_path / "blobs" / f"{bad}.part").exists()
        assert c.head(f"/blobs/{bad}", headers=H).status_code == 404
        # malformed sha
        assert c.put("/blobs/xyz?offset=0", content=b"x", headers=H).status_code == 400


def test_blob_substitution(tmp_path):
    bs = BlobStore(tmp_path / "blobs")
    sha = hashlib.sha256(b"vid").hexdigest()
    (tmp_path / "blobs" / sha).write_bytes(b"vid")
    out = substitute_blobs({"spec": {"source": f"blob:{sha}.mp4", "x": [{"blob": sha}], "n": 3}}, bs)
    assert out["spec"]["source"].endswith(f"{sha}.mp4")
    assert open(out["spec"]["source"], "rb").read() == b"vid"
    assert out["spec"]["x"][0].endswith(sha) and out["spec"]["n"] == 3


def _wait_done(c, jid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/jobs/{jid}?wait=5", headers=H).json()
        if j["state"] in ("done", "error"):
            return j
    raise AssertionError(f"job {jid} not finished")


def test_job_queue_order_longpoll_and_files(tmp_path):
    order: list[str] = []
    gate = threading.Event()

    def slow(args, ctx):
        gate.wait(10)
        order.append(args["name"])
        (ctx.out_dir / "out.txt").write_text(args["name"])
        return {"name": args["name"]}

    def boom(args, ctx):
        raise ValueError("kaput")

    app = make(tmp_path, {"slow": slow, "boom": boom})
    with TestClient(app) as c:
        ids = [c.post("/jobs", json={"kind": "slow", "args": {"name": n}}, headers=H).json()["job_id"]
               for n in ("a", "b", "c")]
        assert c.post("/jobs", json={"kind": "nope"}, headers=H).status_code == 400
        # long-poll returns within the wait window while the first job is blocked
        t0 = time.perf_counter()
        j = c.get(f"/jobs/{ids[2]}?wait=0.6", headers=H).json()
        dt = time.perf_counter() - t0
        assert j["state"] == "queued" and 0.4 < dt < 5
        j = c.get(f"/jobs/{ids[0]}", headers=H).json()
        assert j["state"] == "running"
        # waiter wakes up as soon as the job finishes (well before the wait expires)
        threading.Timer(0.3, gate.set).start()
        t0 = time.perf_counter()
        j = c.get(f"/jobs/{ids[0]}?wait=30", headers=H).json()
        assert j["state"] == "done" and time.perf_counter() - t0 < 10
        assert j["result"]["name"] == "a" and "out.txt" in j["result"]["files"]
        assert "resources" in j["result"]
        for jid in ids[1:]:
            assert _wait_done(c, jid)["state"] == "done"
        assert order == ["a", "b", "c"]
        r = c.get(f"/files/{ids[1]}/out.txt", headers=H)
        assert r.status_code == 200 and r.text == "b"
        assert c.get(f"/files/{ids[1]}/../jobs.db", headers=H).status_code in (400, 404)
        assert c.get(f"/files/{ids[1]}/missing.txt", headers=H).status_code == 404
        # crashing handler -> error state, worker keeps running
        jb = c.post("/jobs", json={"kind": "boom", "args": {}}, headers=H).json()["job_id"]
        j = _wait_done(c, jb)
        assert j["state"] == "error" and "kaput" in j["error"]["error"]
        jd = c.post("/jobs", json={"kind": "slow", "args": {"name": "d"}}, headers=H).json()["job_id"]
        assert _wait_done(c, jd)["state"] == "done"
        assert c.get("/jobs/doesnotexist", headers=H).status_code == 404
        # wait is capped at 50 s (no hang on absurd values)
        assert c.get(f"/jobs/{jd}?wait=9999", headers=H).json()["state"] == "done"


def test_one_job_at_a_time(tmp_path):
    active = []
    peak = []
    lock = threading.Lock()

    def h(args, ctx):
        with lock:
            active.append(1)
            peak.append(len(active))
        time.sleep(0.05)
        with lock:
            active.pop()
        return {}

    app = make(tmp_path, {"h": h})
    with TestClient(app) as c:
        ids = [c.post("/jobs", json={"kind": "h"}, headers=H).json()["job_id"] for _ in range(5)]
        for jid in ids:
            _wait_done(c, jid)
    assert max(peak) == 1


def test_restart_requeues_running_job(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    j1 = q.submit("x", {"n": 1})
    j2 = q.submit("x", {"n": 2})
    assert q.claim()["id"] == j1
    assert q.claim() is None  # one at a time
    q.close()  # "crash" while j1 running
    q2 = JobQueue(tmp_path / "jobs.db")
    assert q2.requeued == 1
    job = q2.get(j1)
    assert job["state"] == "queued" and "requeued" in job["message"]
    assert q2.claim()["id"] == j1  # keeps FIFO position
    q2.finish(j1, {"ok": True})
    assert q2.claim()["id"] == j2
    q2.close()

    # the app picks the requeued job up on start
    q3 = JobQueue(tmp_path / "db2" / "jobs.db")
    jid = q3.submit("x", {"n": 3})
    q3.claim()
    q3.close()
    app = make(tmp_path / "db2", {"x": lambda a, c: {"n": a["n"]}})
    with TestClient(app) as c:
        j = _wait_done(c, jid)
        assert j["state"] == "done" and j["result"]["n"] == 3


def test_analyze_without_timeline_module(tmp_path, monkeypatch):
    import builtins
    import sys

    from casefile.worker import jobs
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if fromlist and "timeline" in fromlist and level == 2:
            raise ImportError("no timeline")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "casefile.timeline", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    app = make(tmp_path, {"analyze": jobs.h_analyze})
    with TestClient(app) as c:
        jid = c.post("/jobs", json={"kind": "analyze", "args": {"video": "x.mp4"}}, headers=H).json()["job_id"]
        j = _wait_done(c, jid)
    assert j["state"] == "error" and j["error"]["error"] == "timeline module not installed"


def test_render_encoder_selection(monkeypatch):
    from casefile import gpu, render
    cmd = render._encode_cmd(1920, 1080, 30, "o.mp4", "medium", 4, "h264_nvenc")
    s = " ".join(cmd)
    assert "-c:v h264_nvenc -preset p5 -rc vbr -cq 19 -b:v 0 -pix_fmt yuv420p" in s and "libx264" not in s
    assert "-c:v libx264" in " ".join(render._encode_cmd(1920, 1080, 30, "o.mp4", "medium", 4))
    monkeypatch.setattr(gpu, "nvenc_available", lambda: False)
    assert render.resolve_encoder("auto") == "libx264"
    monkeypatch.setattr(gpu, "nvenc_available", lambda: True)
    assert render.resolve_encoder("auto") == "h264_nvenc"
    assert render.resolve_encoder("libx264") == "libx264"
    with pytest.raises(Exception):
        render.resolve_encoder("vp9")
