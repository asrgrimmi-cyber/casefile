"""MCP v2 front door: job-based, token-lean wrappers (see docs/CONTRACT_V2.md).

Every long operation is a *local* job id (client.LocalRunner). On the 'gpu' backend the job's thread proxies to
the worker (upload blob -> POST /jobs -> long-poll -> download outputs); on 'local' it runs the core function
in-process. Either way no MCP call blocks longer than it asks (~4 s for analyze, <=50 s for wait_job).
All public functions return dicts or compact line strings; errors are {error, hint}. Re-exported by casefile.api.
"""
from __future__ import annotations

import difflib
import importlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import client as C
from .errors import ToolError, check_path

SUMMARY_MAX = 12_000
QUERY_MAX = 2_000
WAIT_MAX = 1_600
PATCH_MAX = 800
VERIFY_MAX = 1_600


# ------------------------------------------------------------------ helpers
def _mod(name: str):
    """Lazy import of a sibling module written by another worker; graceful error when not there yet."""
    try:
        return importlib.import_module(f"casefile.{name}")
    except ImportError as e:
        if f"casefile.{name}" in str(e) or name in str(e):
            raise ToolError(f"casefile.{name} module not installed yet", "update casefile (pip install -U casefile-mcp)")
        raise


def _video(video: str) -> Path:
    return check_path(video, must_exist=True)


def _j(o: Any) -> str:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False, default=str)


def _r(x: Any, n: int = 2) -> Any:
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return x


def _box(b: Any) -> str:
    return ",".join(str(int(round(float(v)))) for v in b) if b else "-"


def _local_tl_path(video: Path) -> Path:
    from . import config
    return config.home_dir() / "timelines" / C.file_sha256(video)[:16] / "timeline.json"  # same key as timeline._dir


def load_tl(video: str | Path) -> tuple[dict, Path | None]:
    """(timeline dict, timeline.json path or None) for an analysed video; ToolError when not analysed."""
    v = _video(str(video))
    tlmod = _mod("timeline")
    p = _local_tl_path(v)
    try:
        tl = tlmod.load(str(p) if p.exists() else str(v))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"no timeline for {v.name}: {str(e)[:100]}", "run analyze_recording(video) + wait_job first")
    if not isinstance(tl, dict):
        raise ToolError(f"no timeline for {v.name}", "run analyze_recording(video) + wait_job first")
    path = p if p.exists() else (Path(tl["_path"]) if tl.get("_path") else None)
    if path is None and tl.get("_dir"):
        path = Path(tl["_dir"]) / "timeline.json"
    return tl, path


def fit_summary(s: Any, max_chars: int = SUMMARY_MAX) -> Any:
    """Trim a timeline summary to max_chars: drop item lists, then transcripts, then tail steps."""
    if len(_j(s)) <= max_chars or not isinstance(s, dict):
        return s
    s = json.loads(_j(s))
    steps = s.get("steps") or []
    for key in ("items", "transcript", "commands", "fields"):
        for st in steps:
            if isinstance(st, dict) and key in st:
                v = st[key]
                if key == "items" and isinstance(v, list):
                    st[key] = [it.get("id", it) if isinstance(it, dict) else it for it in v][:12]
                elif key == "transcript" and isinstance(v, str):
                    st[key] = v[:200]
                elif isinstance(v, list):
                    st[key] = v[:3]
        if len(_j(s)) <= max_chars:
            return s
    n = len(steps)
    while steps and len(_j(s)) > max_chars:
        steps.pop()
        s["steps_omitted"] = n - len(steps)
    return s


# ------------------------------------------------------------------ job plumbing
def _runner() -> C.LocalRunner:
    return C.get_runner()


def _start(kind: str, local_fn: Callable[[Callable], dict], remote: Callable[[C.WorkerClient, Callable], dict] | None,
           meta: dict) -> tuple[str, str]:
    """Start a job on the best backend -> (job_id, backend). Remote failures before submit fall back to local."""
    cl, be = C.backend() if remote is not None else (None, "local")
    meta = dict(meta, backend=be, kind=kind)
    holder: dict = {}

    def run(prog: Callable[[float], None]) -> dict:
        if cl is not None and remote is not None:
            try:
                return remote(cl, prog)
            except _Fallback as e:
                holder["job"]["meta"]["backend"] = "local"
                holder["job"]["meta"].setdefault("warnings", []).append(f"worker failed ({e}); ran locally")
        return local_fn(prog)

    jid = _runner().start(run, kind, meta, exclusive=cl is None)
    holder["job"] = _runner().get(jid)
    return jid, be


class _Fallback(Exception):
    pass


def _remote_job(cl: C.WorkerClient, prog, kind: str, args: dict, upload: Path | None = None) -> tuple[str, dict]:
    """Upload (progress 0-0.2), submit, long-poll (0.2-0.95) -> (remote id, result). WorkerError before
    submit -> _Fallback (local run); job errors after submit -> ToolError."""
    try:
        if upload is not None:
            sha = cl.ensure_blob(upload, progress=lambda x: prog(0.2 * x))
            args = _sub_blob(args, sha, upload.suffix or ".mp4")
        rid = cl.submit(kind, args)
    except C.WorkerError as e:
        raise _Fallback(str(e)[:120]) from None
    misses = 0
    while True:
        try:
            st = cl.wait(rid, C.MAX_WAIT)
            misses = 0
        except C.WorkerError:
            misses += 1
            if misses > 6:
                raise ToolError(f"worker lost during job {rid}", "check worker_status(); resubmit")
            time.sleep(5)
            continue
        prog(0.2 + 0.75 * float(st.get("progress") or 0))
        if st.get("state") == "done":
            return rid, st.get("result") or {}
        if st.get("state") == "error":
            e = st.get("error")
            if isinstance(e, dict):
                raise ToolError(str(e.get("error"))[:200], str(e.get("hint") or "see worker log"))
            raise ToolError(str(e)[:200], "see worker log")


_BLOB = "__BLOB__"


def _sub_blob(obj: Any, sha: str, ext: str) -> Any:
    if obj == _BLOB:
        return f"blob:{sha}{ext}"
    if isinstance(obj, dict):
        return {k: _sub_blob(v, sha, ext) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sub_blob(v, sha, ext) for v in obj]
    return obj


def _fetch_all(cl: C.WorkerClient, rid: str, files: list[str], dest: Path, want: Callable[[str], bool]) -> dict:
    out = {}
    for name in files:
        if want(name):
            out[name] = str(cl.fetch(rid, name, dest / name))
    return out


def _remap(obj: Any, got: dict[str, str]) -> Any:
    """Replace worker-side paths by the downloaded local ones (matched on file name)."""
    by_base = {Path(k).name: v for k, v in got.items()}
    if isinstance(obj, dict):
        return {k: _remap(v, got) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_remap(v, got) for v in obj]
    if isinstance(obj, str) and ("/" in obj or "\\" in obj):
        return by_base.get(Path(obj.replace("\\", "/")).name, obj)
    return obj


# ------------------------------------------------------------------ analyze
def analyze_recording(video: str, focus: str | None = None, wait: float = 4.0) -> dict:
    """Start (or reuse) the one-time analysis -> summary when ready within `wait` s, else {job_id}."""
    v = _video(video)
    tlmod = _mod("timeline")

    def local(prog):
        try:
            tl, _ = load_tl(v)
            if not focus:
                return {"summary": tlmod.summary(tl), "cached": True}
        except ToolError:
            pass
        kw = {"focus": focus} if focus else {}
        return {"summary": tlmod.analyze(str(v), **kw)}

    def remote(cl, prog):
        args = {"video": _BLOB, **({"focus": focus} if focus else {})}
        rid, res = _remote_job(cl, prog, "analyze", args, upload=v)
        dest = _local_tl_path(v).parent
        files = res.get("files") or []
        got = {}
        for name in files:
            base = Path(name).name
            if base == "timeline.json" or base.endswith(".png"):
                sub = "timeline.json" if base == "timeline.json" else f"steps/{base}"
                got[name] = str(cl.fetch(rid, name, dest / sub))
        res.pop("files", None)
        res.pop("resources", None)
        return {"summary": res.get("summary", res), "timeline": str(dest / "timeline.json") if got else None}

    jid, be = _start("analyze", local, remote, {"video": str(v)})
    st = _runner().wait(jid, max(0.0, min(float(wait), 4.5)))
    if st["state"] == "done":
        return {"job_id": jid, "backend": _backend_of(jid), "state": "done",
                "summary": fit_summary(st["result"]["summary"], SUMMARY_MAX - 200)}
    if st["state"] == "error":
        return {"error": st["error"], "hint": st.get("hint") or "see casefile CLI for details"}
    return {"job_id": jid, "backend": be, "state": st["state"], "next": "wait_job(job_id)"}


def _backend_of(jid: str) -> str:
    j = _runner().get(jid)
    return (j or {}).get("meta", {}).get("backend", "local")


# ------------------------------------------------------------------ query / locate
def _full_dump(video: Path, name: str, data: Any) -> Path:
    d = _local_tl_path(video).parent
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p


def _lines_fit(lines: list[str], max_chars: int, dump: Callable[[], Path]) -> str:
    if sum(len(ln) + 1 for ln in lines) <= max_chars:
        return "\n".join(lines)
    p = dump()
    reserve = len(str(p)) + 30
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > max_chars - reserve:
            break
        out.append(ln)
        used += len(ln) + 1
    out.append(f"{len(lines) - len(out)} more in file {p}")
    return "\n".join(out)


def _clip(s: Any, n: int = 60) -> str:
    s = str(s if s is not None else "").replace("\n", " ").replace("|", "/")
    return s if len(s) <= n else s[:n - 1] + "…"


def query_timeline(video: str, questions: list[str]) -> str | dict:
    """Answers, one line each: `q | id | text | x0,y0,x1,y1 | t`."""
    if not isinstance(questions, list) or not questions:
        raise ToolError("questions must be a non-empty list of strings", 'e.g. ["pusch_snr", "stable time for s3"]')
    v = _video(video)
    tl, _ = load_tl(v)
    ans = _mod("timeline").query(tl, [str(q) for q in questions])
    lines = [" | ".join([_clip(a.get("q"), 40), str(a.get("id") or "-"), _clip(a.get("text")), _box(a.get("box")),
                         str(_r(a.get("t"), 1)) if a.get("t") is not None else "-"]) for a in ans]
    return _lines_fit(lines, QUERY_MAX, lambda: _full_dump(v, "query_last.json", ans)) or "no answers"


def locate(video: str, items: list) -> str | dict:
    """Find UI items by text -> one line each: `text | found | id | x0,y0,x1,y1 | t`."""
    if not isinstance(items, list) or not items:
        raise ToolError("items must be a non-empty list", 'e.g. ["PUSCH SNR", {"step":"s5","text":"CQI"}]')
    norm = [({"text": x} if isinstance(x, str) else dict(x)) for x in items]
    v = _video(video)
    tl, _ = load_tl(v)
    res = _mod("timeline").locate(tl, norm)
    lines = [" | ".join([_clip(r.get("text"), 40), "Y" if r.get("found") else "N", str(r.get("id") or "-"),
                         _box(r.get("box")), str(_r(r.get("t"), 1)) if r.get("t") is not None else "-"]) for r in res]
    return _lines_fit(lines, QUERY_MAX, lambda: _full_dump(v, "locate_last.json", res)) or "nothing found"


# ------------------------------------------------------------------ make_video
def _load_json_arg(spec: dict | str) -> tuple[dict, Path | None]:
    if isinstance(spec, dict):
        return spec, None
    if isinstance(spec, str) and spec.lstrip().startswith("{"):
        try:
            return json.loads(spec), None
        except json.JSONDecodeError as e:
            raise ToolError(f"not valid JSON: line {e.lineno}: {e.msg}", "fix the JSON syntax")
    p = check_path(spec, must_exist=True)
    try:
        return json.loads(p.read_text(encoding="utf-8")), p
    except json.JSONDecodeError as e:
        raise ToolError(f"{p.name} is not valid JSON: line {e.lineno}: {e.msg}", "fix the JSON syntax")


def _default_video_out(spec: dict, preview: bool) -> Path:
    name = Path(str((spec.get("output") or {}).get("path") or "edit.mp4")).stem or "edit"
    p = C.default_out_dir(name) / f"{name}{'_preview' if preview else ''}.mp4"
    return p


def video_checks(plan: Any, size: tuple[int, int], expects: dict | None = None) -> list[dict]:
    """Highlight boxes of a plan as verify checks: [{k, T, rect (output px), expect, label}]."""
    from .draw import Ctx, lock_aspect
    from .render import bar_height, camera_at
    W, H = int(size[0]), int(size[1])
    CH = H - bar_height(H)
    out = []
    for ps in plan.shots:
        if ps.shot.card:
            continue
        for j, o in enumerate(ps.overlays):
            if o.get("type") != "box" or not o.get("rect"):
                continue
            a, b = float(o["from"]), float(o["to"])
            mid = (max(a, ps.xfade) + b) / 2 if b > ps.xfade else (a + b) / 2
            cam = lock_aspect(camera_at(ps, mid, plan.src_size), W / CH, plan.src_size)
            r = Ctx(W=W, H=H, CH=CH, cam=cam).xf(o["rect"])
            exp = (expects or {}).get((ps.idx, j)) or (o.get("find") or {}).get("query") or o.get("label") or ""
            out.append({"k": f"s{ps.idx}.o{j}", "T": round(ps.T0 + mid, 2), "rect": [round(v) for v in r],
                        "expect": exp, "label": o.get("label") or ""})
    return out


def _checks_path(mp4: Path) -> Path:
    return mp4.with_name(mp4.stem + ".checks.json")


def make_video(spec: dict | str, preview: bool = False, out: str | None = None) -> dict:
    """Render a spec (item refs resolved from the timeline) as a job -> {job_id, backend}."""
    data, _ = _load_json_arg(spec)
    planner = importlib.import_module("casefile.planner")
    expects: dict = {}
    if planner.has_item_refs(data):
        if not data.get("source"):
            raise ToolError("item refs need spec.source", "set spec.source to the analysed recording")
        tl, _ = load_tl(data["source"])
        data = planner.resolve_item_refs(data, tl, expects)
    src = check_path(data["source"], must_exist=True) if data.get("source") else None
    if src is not None:
        data["source"] = str(src)
    planner.load_spec(data)  # validate now: errors come back immediately, not from the job
    dest = check_path(out, must_exist=False, write=True) if out else _default_video_out(data, preview)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def write_checks(plan_obj, res: dict) -> None:
        try:
            size = res.get("size") or ([960, 540] if preview else list(plan_obj.size))
            ch = video_checks(plan_obj, tuple(size), expects)
            _checks_path(Path(res["path"])).write_text(_j({"checks": ch, "size": size}), encoding="utf-8")
        except Exception as e:  # verification data is best effort
            res.setdefault("warnings", []).append(f"no verify data: {str(e)[:80]}")

    def local(prog):
        from . import render as R
        plan_obj = planner.plan(data)
        prog(0.1)
        res = R.render(plan_obj, preview=preview, out=str(dest))
        write_checks(plan_obj, res)
        return res

    def remote(cl, prog):
        rspec = dict(data)
        if src is not None:
            rspec["source"] = _BLOB
        rspec["output"] = dict(rspec.get("output") or {}, path=dest.name)
        rid, res = _remote_job(cl, prog, "render", {"spec": rspec, "preview": preview}, upload=src)
        want = lambda n: n.endswith((".mp4", ".srt"))
        got = _fetch_all(cl, rid, res.get("files") or [], dest.parent, want)
        mp4 = next((v for k, v in got.items() if k.endswith(".mp4")), None)
        if mp4 and Path(mp4) != dest:
            Path(mp4).replace(dest)
            got = {k: (str(dest) if v == mp4 else v) for k, v in got.items()}
        res = _remap(res, got)
        res["path"] = str(dest)
        res.pop("thumbs", None)
        try:
            write_checks(planner.plan(data), res)
        except Exception as e:
            res.setdefault("warnings", []).append(f"no verify data: {str(e)[:80]}")
        return res

    jid, be = _start("render", local, remote, {"out": str(dest)})
    return {"job_id": jid, "backend": be, "out": str(dest), "next": "wait_job(job_id)"}


# ------------------------------------------------------------------ make_doc
def make_doc(video: str, doc: dict | str, formats: list[str] | None = None, out: str | None = None) -> dict:
    """Written docs (md/docx/pdf/confluence) from the timeline as a job -> {job_id, backend}."""
    d, _ = _load_json_arg(doc)
    docs = _mod("docs")
    v = _video(video)
    tl, tlp = load_tl(v)
    base = check_path(out, must_exist=False, write=True) if out else C.default_out_dir("docs").parent
    base.mkdir(parents=True, exist_ok=True)

    def sidecar(res: dict) -> None:
        try:
            dd = Path(res.get("dir") or next(iter(res.get("paths", {}).values()))).resolve()
            if dd.is_file():
                dd = dd.parent
            (dd / ".casefile_verify.json").write_text(_j({"timeline": str(tlp) if tlp else None, "doc": d}),
                                                      encoding="utf-8")
        except Exception:
            pass

    def local(prog):
        kw: dict = {"out_dir": str(base)}
        if formats:
            kw["formats"] = formats
        res = docs.make_doc(str(tlp) if tlp else tl, d, **kw)
        sidecar(res)
        return res

    def remote(cl, prog):
        args = {"timeline": C.file_sha256(v), "doc": d, **({"formats": formats} if formats else {})}
        try:
            rid, res = _remote_job(cl, prog, "doc", args)
        except ToolError as e:
            if "no timeline" in e.error:
                raise _Fallback("worker has no timeline for this video") from None
            raise
        slug = Path(str(res.get("dir") or "doc").replace("\\", "/")).name
        got = _fetch_all(cl, rid, res.get("files") or [], base, lambda n: True)  # names are <slug>/...
        res = _remap(res, got)
        res["dir"] = str(base / slug)
        sidecar(res)
        return res

    jid, be = _start("doc", local, remote, {"out": str(base)})
    return {"job_id": jid, "backend": be, "next": "wait_job(job_id)"}


# ------------------------------------------------------------------ wait_job
def _compact_result(kind: str, res: dict) -> dict:
    if kind == "analyze":
        s = res.get("summary") or {}
        steps = s.get("steps") if isinstance(s, dict) else None
        out = {"steps": len(steps) if isinstance(steps, list) else None,
               "duration": _r((s.get("video") or {}).get("duration")) if isinstance(s, dict) else None,
               "next": "query_timeline/locate, or analyze_recording(video) again for the summary"}
        if isinstance(steps, list):
            out["titles"] = [f"{st.get('id')}:{_clip(st.get('title'), 30)}" for st in steps[:12] if isinstance(st, dict)]
        return out
    if kind == "render":
        return {k: res[k] for k in ("path", "srt", "duration", "frames", "size_mb", "size", "encoder") if k in res}
    if kind == "doc":
        return {"paths": res.get("paths"), "pages": res.get("pages"), "steps": res.get("steps")}
    return {k: v for k, v in res.items() if isinstance(v, (str, int, float))}


def wait_job(job_id: str, max_wait: float = 50) -> dict:
    """Long-poll a job (<= 50 s) -> {state, progress} or the compact result."""
    r = _runner()
    job = r.get(job_id)
    if job is None:
        raise ToolError(f"unknown job {job_id}", "job ids live for this MCP session; start the job again")
    st = r.wait(job_id, max(0.0, min(float(max_wait), 50.0)))
    meta = job.get("meta") or {}
    out: dict = {"state": st["state"], "kind": meta.get("kind"), "backend": meta.get("backend", "local")}
    if st["state"] in ("queued", "running"):
        out["progress"] = st.get("progress", 0)
        out["elapsed"] = round(time.time() - job["t0"], 1)
        return out
    if st["state"] == "error":
        return {"error": st.get("error"), "hint": st.get("hint") or "fix the input and start the job again",
                "backend": out["backend"]}
    res = st.get("result") or {}
    out.update(_compact_result(meta.get("kind") or "", res))
    warns = list(meta.get("warnings") or []) + list(res.get("warnings") or [])
    if warns:
        out["warnings"] = [_clip(w, 120) for w in warns[:3]] + ([f"+{len(warns) - 3} more"] if len(warns) > 3 else [])
    while len(_j(out)) > WAIT_MAX and out.get("warnings"):
        out["warnings"].pop()
    return out


# ------------------------------------------------------------------ patch
_IDX = re.compile(r"^(0|[1-9]\d*)$")


def _ptr(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ToolError(f"bad path {path!r}", "use JSON-pointer style, e.g. /shots/3/overlays/1/label")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _walk(doc: Any, parts: list[str], path: str) -> Any:
    cur = doc
    for p in parts:
        if isinstance(cur, list) and _IDX.match(p) and int(p) < len(cur):
            cur = cur[int(p)]
        elif isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            raise ToolError(f"path not found: {path} (at {p!r})", "check indices; lists are 0-based")
    return cur


def apply_ops(doc: Any, ops: list[dict]) -> list[tuple[str, Any, Any]]:
    """Apply [{op:'set'|'del'|'add', path, value}] in place -> [(path, before, after)]."""
    changes = []
    for k, o in enumerate(ops):
        if not isinstance(o, dict) or o.get("op") not in ("set", "del", "add") or "path" not in o:
            raise ToolError(f"ops[{k}] invalid", "use {op:'set'|'del'|'add', path:'/a/0/b', value}")
        parts = _ptr(o["path"])
        if not parts or parts == [""]:
            raise ToolError(f"ops[{k}]: cannot replace the whole document", "patch a field inside it")
        parent = _walk(doc, parts[:-1], o["path"])
        key = parts[-1]
        op = o["op"]
        if op != "del" and "value" not in o:
            raise ToolError(f"ops[{k}]: {op} needs value", "add a value")
        if isinstance(parent, list):
            if op == "add" and key == "-":
                parent.append(o["value"])
                changes.append((o["path"], None, o["value"]))
                continue
            if not _IDX.match(key) or int(key) > len(parent) or (op != "add" and int(key) >= len(parent)):
                raise ToolError(f"ops[{k}]: index {key} out of range (len {len(parent)})", "use '-' to append")
            i = int(key)
            if op == "add":
                parent.insert(i, o["value"])
                changes.append((o["path"], None, o["value"]))
            elif op == "set":
                changes.append((o["path"], parent[i], o["value"]))
                parent[i] = o["value"]
            else:
                changes.append((o["path"], parent.pop(i), None))
        elif isinstance(parent, dict):
            if op == "del":
                if key not in parent:
                    raise ToolError(f"ops[{k}]: {o['path']} not found", "nothing to delete")
                changes.append((o["path"], parent.pop(key), None))
            else:
                changes.append((o["path"], parent.get(key), o["value"]))
                parent[key] = o["value"]
        else:
            raise ToolError(f"ops[{k}]: parent of {o['path']} is not an object/list", "check the path")
    return changes


def _plan_brief(path: Path) -> dict:
    try:
        from . import planner
        p = planner.plan_json(str(path), synth=False, max_chars=2000)
    except ToolError as e:
        return {"plan_error": _clip(e.error, 200), "hint": _clip(e.hint, 120)}
    except Exception as e:
        return {"plan_error": _clip(f"{type(e).__name__}: {e}", 200)}
    shots = " ".join(f"{s['i']}:{s['T0']}+{s['dur']}" for s in p.get("shots", []))
    out = {"total": p.get("total"), "timing": "estimate", "shots": _clip(shots, 400)}
    if p.get("warnings"):
        out["warnings"] = [_clip(w, 100) for w in p["warnings"][:2]]
    return out


def patch(path: str, ops: list[dict], why: str) -> dict:
    """Edit a spec/doc JSON file with pointer ops; `why` is mandatory and logged as a lesson candidate."""
    if not why or not str(why).strip():
        raise ToolError("why is required", "say in one sentence why this change is needed; it is logged")
    if not isinstance(ops, list) or not ops:
        raise ToolError("ops must be a non-empty list", "e.g. [{op:'set', path:'/shots/3/overlays/1/label', value:'SNR'}]")
    p = check_path(path, must_exist=True, write=True)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ToolError(f"{p.name} is not valid JSON: line {e.lineno}", "fix the file first")
    changes = apply_ops(doc, ops)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    logged = 0
    try:
        lessons = importlib.import_module("casefile.lessons")
        for cpath, before, after in changes:
            lessons.log_change(p.stem, f"{p.name}{cpath}", before, after, str(why), who="claude", trigger="patch")
            logged += 1
    except ImportError:
        pass
    except Exception:
        pass
    out: dict = {"ok": True, "changed": len(changes), "logged": logged}
    if isinstance(doc, dict) and "shots" in doc:
        out.update(_plan_brief(p))
    elif isinstance(doc, dict):
        n = len(doc.get("steps") or []) + sum(len(s.get("steps") or []) for s in doc.get("sections") or [])
        out["doc_steps"] = n
    while len(_j(out)) > PATCH_MAX and out.get("shots"):
        out["shots"] = out["shots"][: max(0, len(out["shots"]) - 100)] + "…"
        if len(out["shots"]) < 10:
            out.pop("shots")
    return out


# ------------------------------------------------------------------ verify
def _norm_txt(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(s).lower().replace("o", "0").replace("l", "1").replace("i", "1"))


def fuzzy_ok(expect: str, got: str, thresh: float = 0.75) -> tuple[bool, float]:
    e, g = _norm_txt(expect), _norm_txt(got)
    if not e:
        return True, 1.0
    if not g:
        return False, 0.0
    if e in g:
        return True, 1.0
    best = 0.0
    n = len(e)
    for i in range(0, max(1, len(g) - n + 1)):
        best = max(best, difflib.SequenceMatcher(None, e, g[i:i + n]).ratio())
    return best >= thresh, round(best, 2)


def _frame(video: Path, t: float, dest: Path):
    from PIL import Image

    from .media import ffmpeg, run
    run([ffmpeg(), "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1", str(dest)])
    return Image.open(dest).convert("RGB")


def _ocr_text(img, region: list | None) -> str:
    from . import vision
    words = vision.ocr_image(img, region=region)
    return " ".join(w.get("text", "") for w in words)


def _pad(r: list, pad: int, W: int, H: int) -> list[int]:
    return [max(0, int(r[0]) - pad), max(0, int(r[1]) - pad), min(W, int(r[2]) + pad), min(H, int(r[3]) + pad)]


def _verify_video(mp4: Path) -> tuple[list[str], list[str]]:
    cp = _checks_path(mp4)
    if not cp.exists():
        raise ToolError(f"no verify data next to {mp4.name}", "render it with make_video (writes <name>.checks.json)")
    checks = json.loads(cp.read_text(encoding="utf-8"))["checks"]
    vd = mp4.parent / "verify"
    vd.mkdir(exist_ok=True)
    lines, fails = [], []
    for c in checks:
        if not c.get("expect"):
            lines.append(f"SKIP {c['k']} t={c['T']} (no expected text)")
            continue
        img = _frame(mp4, float(c["T"]), vd / f"{c['k']}.png")
        reg = _pad(c["rect"], 6, *img.size)
        got = _ocr_text(img, reg) if reg[2] > reg[0] and reg[3] > reg[1] else ""
        ok, sc = fuzzy_ok(c["expect"], got)
        lines.append(f"{'PASS' if ok else 'FAIL'} {c['k']} t={c['T']} want={_clip(c['expect'], 30)!r} "
                     f"got={_clip(got, 30)!r} ({sc})")
        if not ok and len(fails) < 2:
            crop = img.crop(_pad(c["rect"], 40, *img.size))
            fp = vd / f"fail_{c['k']}.png"
            crop.save(fp)
            fails.append(str(fp))
    return lines, fails


def _verify_doc(path: Path) -> tuple[list[str], list[str]]:
    from PIL import Image
    d = path if path.is_dir() else path.parent
    sc = d / ".casefile_verify.json"
    if not sc.exists():
        raise ToolError(f"no verify data in {d}", "build the doc with make_doc (writes .casefile_verify.json)")
    meta = json.loads(sc.read_text(encoding="utf-8"))
    docs = _mod("docs")
    tl, base = docs.load_timeline(meta["timeline"]) if meta.get("timeline") else ({}, d)
    items = {it["id"]: (st, it) for st in tl.get("steps") or [] for it in st.get("items") or []}
    entries = docs._doc_steps(meta["doc"]) if hasattr(docs, "_doc_steps") else \
        [(None, s) for s in meta["doc"].get("steps") or []]
    lines, fails = [], []
    for n, (_, ds) in enumerate(entries, 1):
        img_p = d / "images" / f"step-{n:02d}.png"
        hls = [(h if isinstance(h, str) else h.get("item")) for h in ds.get("highlight") or []]
        hls = [h for h in hls if h in items]
        if not hls:
            continue
        if not img_p.exists():
            lines.append(f"FAIL step{n} no image {img_p.name}")
            continue
        img = Image.open(img_p).convert("RGB")
        st = items[hls[0]][0]
        boxes = [items[h][1]["box"] for h in hls]
        shot = base / st.get("screenshot", "") if st.get("screenshot") else None
        geo = None
        if shot and shot.exists() and hasattr(docs, "_crop_rect"):
            with Image.open(shot) as full:
                W, H = full.size
            cx0, cy0, cx1, cy1 = docs._crop_rect(ds.get("crop", "auto"), boxes, W, H, [], "")
            k = min(1.0, getattr(docs, "MAX_IMG_W", 10 ** 6) / max(1, cx1 - cx0))
            geo = (cx0, cy0, k)
        for h in hls:
            it = items[h][1]
            reg = None
            if geo:
                cx0, cy0, k = geo
                b = it["box"]
                reg = _pad([(b[0] - cx0) * k, (b[1] - cy0) * k, (b[2] - cx0) * k, (b[3] - cy0) * k], 8, *img.size)
            got = _ocr_text(img, reg)
            ok, s = fuzzy_ok(it.get("text", ""), got)
            lines.append(f"{'PASS' if ok else 'FAIL'} step{n} {h} want={_clip(it.get('text'), 30)!r} "
                         f"got={_clip(got, 30)!r} ({s})")
            if not ok and len(fails) < 2:
                fp = d / "images" / f"fail_{n:02d}_{re.sub(r'[^a-z0-9_]', '_', h)}.png"
                img.crop(_pad(reg, 40, *img.size) if reg else (0, 0, img.width, img.height)).save(fp)
                fails.append(str(fp))
    return lines, fails


def verify(path: str) -> dict:
    """OCR every highlight in a rendered video/doc and compare with the expected text -> pass/fail lines."""
    p = check_path(path, must_exist=True)
    lines, fails = _verify_video(p) if p.suffix.lower() == ".mp4" else _verify_doc(p)
    npass = sum(ln.startswith("PASS") for ln in lines)
    nfail = sum(ln.startswith("FAIL") for ln in lines)
    out: dict = {"pass": npass, "fail": nfail, "lines": []}
    for ln in lines:
        if len(_j(out)) + len(ln) + 200 > VERIFY_MAX:
            out["lines"].append(f"{len(lines) - len(out['lines'])} more")
            break
        out["lines"].append(ln)
    if fails:
        out["crops"] = fails
    return out


# ------------------------------------------------------------------ misc
def worker_status() -> dict:
    cl = C.get_client()
    if not cl.configured:
        return {"backend": "local", "worker": "not configured", "hint": "set CASEFILE_WORKER_URL + token"}
    st = cl.status(refresh=True)
    if st is None:
        return {"backend": "local", "worker": "unreachable", "url": cl.url}
    g = st.get("gpu") or {}
    caps = st.get("backend_caps") or {}
    return {"backend": "gpu", "gpu": _clip(g.get("name"), 40), "vram_free": g.get("vram_free"),
            "ram_free": st.get("ram_free"), "queue": st.get("queue"),
            "caps": ",".join(k for k, v in caps.items() if v)}


def get_lessons(context: str | dict | None = None) -> dict:
    r = _mod("lessons").get_lessons(context)
    return {"text": r.get("text") or "(no approved lessons yet)", "more": r.get("more", 0)}


def log_change(target: str, before: Any, after: Any, why: str) -> dict:
    lid = _mod("lessons").log_change(None, target, before, after, why, who="claude", trigger="manual")
    return {"ok": True, "id": lid}


def report_gap(tool: str, need: str, example: str | None = None) -> dict:
    gid = _mod("lessons").report_gap(tool, need, example)
    return {"ok": True, "id": gid}
