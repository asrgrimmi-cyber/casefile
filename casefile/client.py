"""Worker HTTP client (content-addressed resumable upload, long-poll jobs) + an in-process LocalRunner fallback.

Both backends expose the same job interface: ``submit(kind, args) -> job_id`` and
``wait(job_id, max_wait) -> {state, progress, result|error}`` so the MCP layer never blocks longer than it asks.
Config: CASEFILE_WORKER_URL, CASEFILE_WORKER_TOKEN (else ~/.casefile_worker_token). See docs/CONTRACT_V2.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .errors import ToolError

CHUNK = 8 * 1024 * 1024
RETRIES = 3
STATUS_TTL = 30.0
MAX_WAIT = 50.0


# ------------------------------------------------------------------ hashing (cached by path+size+mtime)
_SHA_CACHE: dict[tuple, str] = {}
_SHA_LOCK = threading.Lock()


def _sha_index() -> Path:
    from . import config
    return config.cache_dir() / "sha256_index.json"


def file_sha256(path: str | Path) -> str:
    """Streaming sha256 of a file; memoised in memory and in <home>/cache/sha256_index.json."""
    p = Path(path).resolve()
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    with _SHA_LOCK:
        if key in _SHA_CACHE:
            return _SHA_CACHE[key]
    skey = "|".join(map(str, key))
    idx: dict = {}
    try:
        idx = json.loads(_sha_index().read_text())
        if skey in idx:
            _SHA_CACHE[key] = idx[skey]
            return idx[skey]
    except (OSError, ValueError):
        pass
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    sha = h.hexdigest()
    with _SHA_LOCK:
        _SHA_CACHE[key] = sha
        try:
            idx[skey] = sha
            if len(idx) > 500:
                idx = dict(list(idx.items())[-400:])
            _sha_index().write_text(json.dumps(idx))
        except OSError:
            pass
    return sha


def default_out_dir(name: str) -> Path:
    """Where downloads go when the user gave no path: <workdir>/casefile_out/<name> in a sandbox,
    else ~/Downloads/casefile/<name>."""
    from . import config
    wd = config.workdir()
    base = (wd / "casefile_out") if wd else (Path.home() / "Downloads" / "casefile")
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ worker client
class WorkerError(Exception):
    pass


def _read_token() -> str | None:
    t = os.environ.get("CASEFILE_WORKER_TOKEN")
    if t:
        return t.strip()
    f = Path.home() / ".casefile_worker_token"
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


class WorkerClient:
    """HTTP client for the GPU worker. `transport` (httpx transport) is for tests."""

    def __init__(self, url: str | None = None, token: str | None = None, *, transport: Any = None,
                 chunk: int = CHUNK, retries: int = RETRIES, backoff: float = 0.5):
        self.url = (url if url is not None else os.environ.get("CASEFILE_WORKER_URL", "")).rstrip("/")
        self.token = token if token is not None else _read_token()
        self.transport = transport
        self.chunk = int(chunk)
        self.retries = int(retries)
        self.backoff = float(backoff)
        self._status: tuple[float, dict | None] | None = None
        self._http = None

    # -- plumbing
    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _client(self):
        if self._http is None:
            import httpx
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            kw: dict = {"base_url": self.url, "headers": headers, "timeout": 30.0}
            if self.transport is not None:
                kw["transport"] = self.transport
            self._http = httpx.Client(**kw)
        return self._http

    def _req(self, method: str, path: str, *, ok=(200,), retry: bool = True, **kw):
        """Request with retries on transport errors / 5xx. Returns the response (status in `ok` or 404)."""
        import httpx
        last: Exception | None = None
        for i in range(self.retries if retry else 1):
            try:
                r = self._client().request(method, path, **kw)
                if r.status_code in ok or r.status_code == 404:
                    return r
                if r.status_code in (401, 403):
                    raise WorkerError(f"worker auth failed ({r.status_code})")
                if r.status_code < 500:
                    raise WorkerError(f"worker {method} {path}: HTTP {r.status_code} {r.text[:120]}")
                last = WorkerError(f"worker {method} {path}: HTTP {r.status_code}")
            except httpx.HTTPError as e:
                last = e
            if i + 1 < self.retries and retry:
                time.sleep(self.backoff * (2 ** i))
        raise WorkerError(f"worker unreachable: {type(last).__name__}: {str(last)[:120]}")

    # -- API
    def status(self, *, refresh: bool = False) -> dict | None:
        """GET /status with a 2 s timeout; cached 30 s (failures cached too). None = unreachable."""
        if not self.configured:
            return None
        now = time.monotonic()
        if not refresh and self._status and now - self._status[0] < STATUS_TTL:
            return self._status[1]
        try:
            r = self._req("GET", "/status", retry=False, timeout=2.0)
            st = r.json() if r.status_code == 200 else None
        except Exception:
            st = None
        self._status = (now, st)
        return st

    def ensure_blob(self, path: str | Path, progress: Callable[[float], None] | None = None) -> str:
        """Make sure the worker has the file; returns its sha256. HEAD dedupes; PUT resumes at an offset."""
        p = Path(path)
        sha = file_sha256(p)
        size = p.stat().st_size
        r = self._req("HEAD", f"/blobs/{sha}")
        if r.status_code == 200:
            return sha
        offset = _offset_of(r) or 0
        if offset > size:
            offset = 0
        failures = 0
        with open(p, "rb") as f:
            while offset < size or size == 0:
                f.seek(offset)
                data = f.read(self.chunk)
                try:
                    r = self._req("PUT", f"/blobs/{sha}", params={"offset": offset, "total": size}, content=data,
                                  ok=(200, 201, 202, 204, 308, 409), retry=False,
                                  headers={"Content-Type": "application/octet-stream"})
                    if r.status_code == 404:
                        raise WorkerError("blob upload rejected (404)")
                    if r.status_code == 409:  # server is elsewhere: jump to its offset (or it is already complete)
                        j = _json(r)
                        if j.get("complete"):
                            return sha
                        srv = j.get("offset")
                        if not isinstance(srv, int) or srv == offset:
                            raise WorkerError(f"upload conflict at offset {offset}")
                        offset = srv
                        continue
                except WorkerError:
                    failures += 1
                    if failures >= self.retries:
                        raise
                    time.sleep(self.backoff * failures)
                    try:  # ask the server where it is
                        h = self._req("HEAD", f"/blobs/{sha}", retry=False)
                        if h.status_code == 200:
                            return sha
                        srv = _offset_of(h)
                        if srv is not None:
                            offset = srv
                    except WorkerError:
                        pass
                    continue
                failures = 0
                srv = _offset_of(r)
                offset = srv if srv is not None else offset + len(data)
                if progress and size:
                    progress(min(1.0, offset / size))
                if size == 0:
                    break
        return sha

    def submit(self, kind: str, args: dict) -> str:
        r = self._req("POST", "/jobs", json={"kind": kind, "args": args}, ok=(200, 201, 202))
        if r.status_code == 404:
            raise WorkerError("worker has no /jobs endpoint")
        j = r.json()
        return str(j.get("job_id") or j.get("id"))

    def wait(self, job_id: str, max_wait: float = MAX_WAIT) -> dict:
        """Long-poll GET /jobs/{id}?wait=N (N <= 50) -> {state, progress, result|error}."""
        w = max(0.0, min(float(max_wait), MAX_WAIT))
        r = self._req("GET", f"/jobs/{job_id}", params={"wait": round(w, 1)}, timeout=w + 15.0)
        if r.status_code == 404:
            return {"state": "error", "error": f"unknown job {job_id}"}
        return r.json()

    def fetch(self, job_id: str, name: str, dest: str | Path) -> Path:
        """Download one output file (streamed, retried) to dest (a file path or an existing directory)."""
        import httpx
        d = Path(dest)
        if d.is_dir():
            d = d / name
        d.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for i in range(self.retries):
            try:
                with self._client().stream("GET", f"/files/{job_id}/{quote(name, safe='/')}", timeout=120.0) as r:
                    if r.status_code != 200:
                        raise WorkerError(f"download {name}: HTTP {r.status_code}")
                    tmp = d.with_name(d.name + ".part")
                    with open(tmp, "wb") as f:
                        for b in r.iter_bytes(1 << 20):
                            f.write(b)
                    tmp.replace(d)
                    return d
            except (httpx.HTTPError, WorkerError) as e:
                last = e
                time.sleep(self.backoff * (2 ** i))
        raise WorkerError(f"download failed: {last}")


def _json(r) -> dict:
    try:
        j = r.json()
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _offset_of(r) -> int | None:
    """Server-reported upload offset: header Upload-Offset / X-Offset, or JSON {offset|received}."""
    for h in ("Upload-Offset", "X-Upload-Offset", "X-Offset"):
        v = r.headers.get(h)
        if v is not None:
            try:
                return int(v)
            except ValueError:
                pass
    if r.request.method != "HEAD":
        try:
            j = r.json()
            for k in ("offset", "received", "size"):
                if isinstance(j.get(k), int):
                    return j[k]
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ local runner
class LocalRunner:
    """In-process job runner (daemon threads, one job at a time like the worker) with the worker's interface."""

    def __init__(self, handlers: dict[str, Callable[[dict, Callable[[float], None]], dict]] | None = None):
        self.handlers = dict(handlers or {})
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(1)

    def start(self, fn: Callable[[Callable[[float], None]], dict], kind: str = "job", meta: dict | None = None,
              exclusive: bool = True) -> str:
        jid = f"{kind[:3]}-{uuid.uuid4().hex[:10]}"
        job = {"id": jid, "kind": kind, "state": "queued", "progress": 0.0, "result": None, "error": None,
               "hint": None, "done": threading.Event(), "t0": time.time(), "meta": dict(meta or {})}
        with self._lock:
            self.jobs[jid] = job

        def prog(x: float) -> None:
            job["progress"] = round(max(0.0, min(1.0, float(x))), 3)

        def run() -> None:
            sem = self._sem if exclusive else None
            if sem:
                sem.acquire()
            try:
                job["state"] = "running"
                job["result"] = fn(prog)
                job["progress"] = 1.0
                job["state"] = "done"
            except ToolError as e:
                job["state"], job["error"], job["hint"] = "error", e.error, e.hint
            except Exception as e:  # never leak a traceback to the model
                job["state"], job["error"] = "error", f"{type(e).__name__}: {e}".splitlines()[0][:200]
                job["trace"] = traceback.format_exc()[-2000:]
            finally:
                job["t1"] = time.time()
                if sem:
                    sem.release()
                job["done"].set()

        threading.Thread(target=run, name=f"casefile-{jid}", daemon=True).start()
        return jid

    def submit(self, kind: str, args: dict) -> str:
        if kind not in self.handlers:
            raise ToolError(f"unknown job kind {kind!r}", f"one of: {', '.join(self.handlers) or '(none)'}")
        h = self.handlers[kind]
        return self.start(lambda prog: h(args, prog), kind)

    def get(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    def wait(self, job_id: str, max_wait: float = MAX_WAIT) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            return {"state": "error", "error": f"unknown job {job_id}", "hint": "job ids are per MCP session"}
        job["done"].wait(max(0.0, min(float(max_wait), MAX_WAIT)))
        out: dict = {"state": job["state"], "progress": job["progress"]}
        if job["state"] == "done":
            out["result"] = job["result"]
        elif job["state"] == "error":
            out["error"] = job["error"]
            if job.get("hint"):
                out["hint"] = job["hint"]
        return out


# ------------------------------------------------------------------ singletons
_CLIENT: WorkerClient | None = None
_RUNNER: LocalRunner | None = None


def get_client() -> WorkerClient:
    global _CLIENT
    url = os.environ.get("CASEFILE_WORKER_URL", "").rstrip("/")
    if _CLIENT is None or _CLIENT.url != url:
        _CLIENT = WorkerClient(url)
    return _CLIENT


def set_client(c: WorkerClient | None) -> None:
    global _CLIENT
    _CLIENT = c


def get_runner() -> LocalRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = LocalRunner()
    return _RUNNER


def backend() -> tuple[WorkerClient | None, str]:
    """(client, 'gpu') when the worker answers /status, else (None, 'local')."""
    c = get_client()
    if c.configured and c.status() is not None:
        return c, "gpu"
    return None, "local"
