"""Casefile GPU worker: FastAPI app (see docs/CONTRACT_V2.md "Worker HTTP API").

Env: CASEFILE_WORKER_TOKEN (required), CASEFILE_HOME (data dir: blobs/, jobs.db, outputs/),
CASEFILE_BIND (host:port, default 0.0.0.0:8765). Run: `casefile-worker` (uvicorn, single process).
Every endpoint requires `Authorization: Bearer <token>` (constant-time compare) -> 401 otherwise.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .blobs import BlobError, BlobStore, valid_sha
from .jobs import HANDLERS, Handler, Runner, release_models
from .queue import JobQueue

log = logging.getLogger("casefile.worker")
MAX_WAIT = 50.0
MAX_CHUNK = 64 * 2**20
VERSION = "2"


def _pkg_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("casefile-mcp")
    except Exception:
        return None


class JobIn(BaseModel):
    kind: str
    args: dict[str, Any] = Field(default_factory=dict)


def _status_payload(queue: JobQueue, handlers: dict) -> dict:
    from .. import gpu, ocr_backend, stt
    s = gpu.status()
    g = s.get("gpu")
    ocr_name = ocr_backend.backend_name()
    return {"version": VERSION, "package": _pkg_version(),
            "gpu": ({"name": g["name"], "vram_total": g["vram_total"], "vram_free": g["vram_free"],
                     "driver": g.get("driver")} if g else None),
            "ram_total": s.get("ram_total"), "ram_free": s.get("ram_free"), "queue": queue.counts(),
            "backend_caps": {"nvenc": s["nvenc"], "cuda_ocr": ocr_name == "rapidocr" and s["cuda_onnx"],
                             "ocr": ocr_name, "whisper": stt.available(),
                             "whisper_cuda": stt.available() and s["cuda_ctranslate2"]},
            "kinds": sorted(handlers)}


def create_app(home: str | Path | None = None, token: str | None = None, *,
               handlers: dict[str, Handler] | None = None, start_runner: bool = True,
               release: Callable[[], None] | None = release_models, monitor: bool = True,
               status_fn: Callable[[JobQueue, dict], dict] | None = None) -> FastAPI:
    """Build the worker app. Tests pass fake handlers / tmp home; production uses env."""
    home = Path(home or os.environ.get("CASEFILE_HOME") or Path.home() / ".casefile")
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CASEFILE_HOME", str(home))
    token = token if token is not None else os.environ.get("CASEFILE_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("CASEFILE_WORKER_TOKEN is not set; refusing to start an unauthenticated worker")
    tok = token.encode()
    handlers = handlers if handlers is not None else HANDLERS
    queue = JobQueue(home / "jobs.db")
    blobs = BlobStore(home / "blobs")
    runner = Runner(queue, blobs, home, handlers, release=release, monitor=monitor)
    status_impl = status_fn or _status_payload

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if queue.requeued:
            log.info("requeued %d job(s) interrupted by a restart", queue.requeued)
        if start_runner:
            runner.start()
        yield
        runner.stop()

    app = FastAPI(title="casefile-worker", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.queue, app.state.blobs, app.state.runner, app.state.home = queue, blobs, runner, home

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        scheme, _, given = h.partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), tok)
        if not ok:
            raise HTTPException(401, detail={"error": "unauthorized", "hint": "send Authorization: Bearer <token>"},
                                headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(BlobError)
    async def _blob_err(_req: Request, e: BlobError):
        return JSONResponse(e.to_dict(), status_code=e.status)

    @app.exception_handler(HTTPException)
    async def _http_err(_req: Request, e: HTTPException):
        body = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail), "hint": ""}
        return JSONResponse(body, status_code=e.status_code, headers=getattr(e, "headers", None))

    deps = [Depends(auth)]

    @app.get("/status", dependencies=deps)
    async def status():
        return await run_in_threadpool(status_impl, queue, handlers)

    @app.head("/blobs/{sha}", dependencies=deps)
    async def blob_head(sha: str):
        st = blobs.status(sha)
        if st["complete"]:
            return Response(status_code=200, headers={"Content-Length": str(st["size"]), "Upload-Offset": str(st["size"])})
        return Response(status_code=404, headers={"Upload-Offset": str(st["size"])})

    @app.get("/blobs/{sha}", dependencies=deps)
    async def blob_get(sha: str):
        st = blobs.status(sha)
        return JSONResponse({"sha256": valid_sha(sha), **st}, status_code=200 if st["complete"] else 404)

    @app.put("/blobs/{sha}", dependencies=deps)
    async def blob_put(sha: str, request: Request, offset: int = Query(0, ge=0), total: int | None = Query(None, ge=0),
                       final: bool = Query(False)):
        sha = valid_sha(sha)
        f = blobs.open_chunk(sha, offset)
        n = 0
        try:
            async for chunk in request.stream():
                n += len(chunk)
                if n > MAX_CHUNK:
                    raise BlobError(413, f"chunk larger than {MAX_CHUNK} bytes", "split the upload into smaller chunks")
                f.write(chunk)
        finally:
            f.close()
        received = offset + n
        if final or (total is not None and received >= total):
            if total is not None and received != total:
                raise BlobError(409, f"received {received} != total {total}", "resume from offset", offset=received)
            res = await run_in_threadpool(blobs.finalize, sha)
            return {"sha256": sha, **res}
        return {"sha256": sha, "complete": False, "size": received}

    @app.post("/jobs", dependencies=deps)
    async def job_submit(job: JobIn):
        if job.kind not in handlers:
            raise HTTPException(400, detail={"error": f"unknown kind {job.kind!r}",
                                             "hint": f"use one of {', '.join(sorted(handlers))}"})
        jid = queue.submit(job.kind, job.args)
        return {"job_id": jid, "state": "queued"}

    @app.get("/jobs/{jid}", dependencies=deps)
    async def job_get(jid: str, wait: float = Query(0, ge=0)):
        wait = min(float(wait), MAX_WAIT)
        t0 = time.monotonic()
        job = await run_in_threadpool(queue.wait, jid, wait)
        if job is None:
            raise HTTPException(404, detail={"error": f"no job {jid}", "hint": "check the job id"})
        out = {"job_id": jid, "kind": job["kind"], "state": job["state"], "progress": round(job["progress"], 3),
               "message": job["message"], "waited": round(time.monotonic() - t0, 2)}
        if job["state"] == "done":
            out["result"] = job["result"]
        elif job["state"] == "error":
            out["error"] = job["error"]
            if job["result"]:
                out["result"] = job["result"]
        elif job["state"] == "queued":
            out["position"] = queue.counts()["queued"]
        return out

    @app.get("/files/{jid}/{name:path}", dependencies=deps)
    async def files(jid: str, name: str):
        if not jid.isalnum():
            raise HTTPException(400, detail={"error": "bad job id", "hint": ""})
        root = runner.out_dir(jid).resolve()
        p = (root / name).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404, detail={"error": f"no file {name} for job {jid}", "hint": "see result.files"})
        return FileResponse(p, filename=p.name)

    return app


def main() -> None:
    """Console entry point `casefile-worker`."""
    import uvicorn
    logging.basicConfig(level=os.environ.get("CASEFILE_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bind = os.environ.get("CASEFILE_BIND", "0.0.0.0:8765")
    host, _, port = bind.rpartition(":")
    app = create_app()
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port or 8765), log_level="info", access_log=False,
                timeout_keep_alive=75)


if __name__ == "__main__":
    main()
