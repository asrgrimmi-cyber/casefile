"""WorkerClient against a fake worker (httpx.MockTransport) + LocalRunner fallback."""
import hashlib
import json
import re
import threading
import time

import httpx
import pytest

from casefile import client as C


class FakeWorker:
    """In-memory model of the worker blob/job protocol (docs/CONTRACT_V2.md, casefile/worker/blobs.py)."""

    def __init__(self, token="tok", fail_put_after=None, status_ok=True):
        self.token = token
        self.blobs: dict[str, bytes] = {}
        self.parts: dict[str, bytearray] = {}
        self.puts: list[tuple[int, int]] = []
        self.heads = 0
        self.fail_put_after = fail_put_after   # raise a transport error on this PUT number (1-based)
        self.status_ok = status_ok
        self.jobs: dict[str, dict] = {}
        self.polls: list[float] = []
        self.files = {"j1": {"out.mp4": b"MP4DATA"}}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "auth", "hint": ""})
        path = req.url.path
        if path == "/status":
            if not self.status_ok:
                raise httpx.ConnectError("down", request=req)
            return httpx.Response(200, json={"version": "t", "gpu": {"name": "Fake"}, "queue": 0})
        m = re.fullmatch(r"/blobs/([0-9a-f]{64})", path)
        if m:
            sha = m.group(1)
            if req.method == "HEAD":
                self.heads += 1
                if sha in self.blobs:
                    return httpx.Response(200, headers={"Upload-Offset": str(len(self.blobs[sha]))})
                return httpx.Response(404, headers={"Upload-Offset": str(len(self.parts.get(sha, b"")))})
            if req.method == "PUT":
                self.puts.append((int(req.url.params["offset"]), len(req.content)))
                if self.fail_put_after and len(self.puts) == self.fail_put_after:
                    # the chunk arrives, but the connection dies before the response
                    self.parts.setdefault(sha, bytearray()).extend(req.content)
                    raise httpx.ReadError("connection reset", request=req)
                off = int(req.url.params["offset"])
                cur = self.parts.setdefault(sha, bytearray())
                if off != len(cur):
                    return httpx.Response(409, json={"error": "offset", "hint": "", "offset": len(cur)})
                cur.extend(req.content)
                total = int(req.url.params.get("total", -1))
                if len(cur) >= total:
                    data = bytes(self.parts.pop(sha))
                    if hashlib.sha256(data).hexdigest() != sha:
                        return httpx.Response(422, json={"error": "sha mismatch", "hint": ""})
                    self.blobs[sha] = data
                    return httpx.Response(200, json={"sha256": sha, "complete": True, "size": len(data)})
                return httpx.Response(200, json={"sha256": sha, "complete": False, "size": len(cur)})
        if path == "/jobs" and req.method == "POST":
            body = json.loads(req.content)
            jid = f"j{len(self.jobs) + 1}"
            self.jobs[jid] = {"kind": body["kind"], "args": body["args"], "n": 0}
            return httpx.Response(200, json={"job_id": jid, "state": "queued"})
        m = re.fullmatch(r"/jobs/(\w+)", path)
        if m:
            j = self.jobs.get(m.group(1))
            if j is None:
                return httpx.Response(404, json={"error": "no job", "hint": ""})
            self.polls.append(float(req.url.params.get("wait", 0)))
            j["n"] += 1
            if j["n"] < 3:
                return httpx.Response(200, json={"state": "running", "progress": 0.3 * j["n"]})
            return httpx.Response(200, json={"state": "done", "progress": 1.0,
                                             "result": {"path": "/srv/out/out.mp4", "files": ["out.mp4"]}})
        m = re.fullmatch(r"/files/(\w+)/(.+)", path)
        if m:
            data = self.files.get(m.group(1), {}).get(m.group(2))
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        return httpx.Response(404)


def mk(fw, **kw):
    return C.WorkerClient("http://fake:8765", "tok", transport=httpx.MockTransport(fw), backoff=0.0, **kw)


@pytest.fixture
def blob(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEFILE_HOME", str(tmp_path / "home"))
    p = tmp_path / "rec.mp4"
    p.write_bytes(bytes(range(256)) * 100)   # 25,600 bytes
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


def test_upload_chunked(blob):
    p, sha = blob
    fw = FakeWorker()
    assert mk(fw, chunk=10_000).ensure_blob(p) == sha
    assert fw.blobs[sha] == p.read_bytes()
    assert [o for o, _ in fw.puts] == [0, 10_000, 20_000]


def test_upload_resume_after_interrupt(blob):
    """Connection dies after the first chunk was stored: client asks HEAD for the offset and resumes there."""
    p, sha = blob
    fw = FakeWorker(fail_put_after=1)
    assert mk(fw, chunk=10_000).ensure_blob(p) == sha
    assert fw.blobs[sha] == p.read_bytes()
    assert [o for o, _ in fw.puts] == [0, 10_000, 20_000]   # chunk 0 not re-sent


def test_upload_resume_partial_from_previous_session(blob):
    p, sha = blob
    fw = FakeWorker()
    fw.parts[sha] = bytearray(p.read_bytes()[:10_000])      # left over from an earlier, interrupted run
    assert mk(fw, chunk=10_000).ensure_blob(p) == sha
    assert fw.puts[0][0] == 10_000 and fw.blobs[sha] == p.read_bytes()


def test_upload_409_jumps_to_server_offset(blob):
    p, sha = blob
    fw = FakeWorker()
    c = mk(fw, chunk=10_000)
    fw.parts[sha] = bytearray(p.read_bytes()[:20_000])
    orig = fw.__call__

    def lying_head(req):   # HEAD reports 0, PUT at 0 gets 409 {offset: 20000}
        if req.method == "HEAD":
            return httpx.Response(404, headers={"Upload-Offset": "0"})
        return orig(req)
    c.transport = httpx.MockTransport(lying_head)
    assert c.ensure_blob(p) == sha
    assert [o for o, _ in fw.puts] == [0, 20_000]


def test_dedupe_head_200_skips_upload(blob):
    p, sha = blob
    fw = FakeWorker()
    fw.blobs[sha] = p.read_bytes()
    assert mk(fw).ensure_blob(p) == sha
    assert fw.puts == [] and fw.heads == 1


def test_sha_cached(blob):
    p, sha = blob
    assert C.file_sha256(p) == sha
    C._SHA_CACHE.clear()
    assert C.file_sha256(p) == sha      # from the on-disk index


def test_status_cached_and_timeout(monkeypatch):
    fw = FakeWorker()
    c = mk(fw)
    calls = []
    orig = c._req

    def spy(*a, **kw):
        calls.append(kw.get("timeout"))
        return orig(*a, **kw)
    monkeypatch.setattr(c, "_req", spy)
    assert c.status()["gpu"]["name"] == "Fake"
    assert c.status() is not None
    assert calls == [2.0]                       # one request, 2 s timeout, then cached
    fw.status_ok = False
    assert c.status() is not None               # still cached
    assert c.status(refresh=True) is None


def test_auth_failure_is_clear(blob):
    p, _ = blob
    c = C.WorkerClient("http://fake:8765", "wrong", transport=httpx.MockTransport(FakeWorker()), backoff=0.0)
    with pytest.raises(C.WorkerError, match="auth"):
        c.ensure_blob(p)


def test_submit_wait_longpoll_fetch(tmp_path):
    fw = FakeWorker()
    c = mk(fw)
    jid = c.submit("render", {"spec": {}})
    assert jid == "j1"
    assert c.wait(jid, 999)["state"] == "running"       # capped at 50 s server-side
    assert fw.polls[-1] == 50.0
    assert c.wait(jid, 5)["state"] == "running"
    done = c.wait(jid, 5)
    assert done["state"] == "done" and done["result"]["files"] == ["out.mp4"]
    got = c.fetch(jid, "out.mp4", tmp_path)
    assert got.read_bytes() == b"MP4DATA"
    assert c.wait("nope", 1)["state"] == "error"


def test_token_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CASEFILE_WORKER_TOKEN", raising=False)
    monkeypatch.setattr(C.Path, "home", lambda: tmp_path)
    (tmp_path / ".casefile_worker_token").write_text("abc\n")
    assert C.WorkerClient("http://x").token == "abc"


def test_backend_falls_back_to_local_when_status_fails(monkeypatch):
    monkeypatch.setenv("CASEFILE_WORKER_URL", "http://fake:8765")
    C.set_client(mk(FakeWorker(status_ok=False)))
    try:
        cl, be = C.backend()
        assert cl is None and be == "local"
        C.set_client(mk(FakeWorker()))
        cl, be = C.backend()
        assert be == "gpu" and cl is not None
    finally:
        C.set_client(None)
    monkeypatch.delenv("CASEFILE_WORKER_URL")
    assert C.backend() == (None, "local")


def test_local_runner_nonblocking_and_errors():
    r = C.LocalRunner({"slow": lambda a, prog: (prog(0.5), time.sleep(0.6), {"v": a["x"]})[2],
                       "bad": lambda a, prog: 1 / 0})
    t = time.monotonic()
    jid = r.submit("slow", {"x": 7})
    assert time.monotonic() - t < 0.2
    st = r.wait(jid, 0.05)
    assert st["state"] in ("queued", "running")
    st = r.wait(jid, 5)
    assert st == {"state": "done", "progress": 1.0, "result": {"v": 7}}
    e = r.wait(r.submit("bad", {}), 5)
    assert e["state"] == "error" and "ZeroDivisionError" in e["error"] and "Traceback" not in e["error"]
    assert r.wait("zzz", 0)["state"] == "error"


def test_local_runner_one_at_a_time():
    active, peak = [0], [0]
    lock = threading.Lock()

    def h(a, prog):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.1)
        with lock:
            active[0] -= 1
        return {}
    r = C.LocalRunner({"h": h})
    ids = [r.submit("h", {}) for _ in range(3)]
    for i in ids:
        assert r.wait(i, 5)["state"] == "done"
    assert peak[0] == 1
