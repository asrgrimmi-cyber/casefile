"""Learning loop: log every human/assistant correction, promote repeats to lessons, surface them next time.

SQLite at `<CASEFILE_HOME>/lessons.db` (override with `db=` or CASEFILE_LESSONS_DB). Tables:
  changes(id, job, target, before, after, why NOT NULL, who, trigger, kind, created)
  lessons(id, rule ≤200 chars, scope json {product?, screen?, tool?}, kind, evidence_count,
          status candidate|approved|retired, action json|null, created, updated)
  gaps(id, tool, need, example, status open|closed, created)

Flow: `log_change` on every fix (why is required) -> `promote()` groups similar changes (same kind, or same
normalised target like '*.phr_box') and turns ≥3 into a *candidate* lesson -> a human `approve`s it ->
`get_lessons(context)` returns the most relevant approved rules (≤3200 chars ≈ 800 tokens) for the next job.
Auto-apply kinds carry an `action`: lexicon {written, spoken} · ocr_fix {pattern, replace} · box_pad {px} ·
speed_default {factor}; `active_actions(kind)` lists the approved ones. Nothing is seeded.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .errors import ToolError

RULE_MAX = 200
BUDGET = 3200
PROMOTE_AT = 3
STATUSES = ("candidate", "approved", "retired")
ACTION_KINDS = {
    "lexicon": ("written", "spoken"),
    "ocr_fix": ("pattern", "replace"),
    "box_pad": ("px",),
    "speed_default": ("factor",),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, target TEXT NOT NULL, before TEXT, after TEXT,
  why TEXT NOT NULL CHECK(length(trim(why)) > 0), who TEXT, trigger TEXT, kind TEXT,
  lesson_id INTEGER, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '{}', kind TEXT,
  pattern TEXT, evidence_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','approved','retired')),
  action TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gaps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, need TEXT NOT NULL, example TEXT,
  status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_changes_lesson ON changes(lesson_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_lessons_key ON lessons(pattern);
"""


def db_path(db: str | Path | None = None) -> Path:
    if db:
        return Path(db)
    env = os.environ.get("CASEFILE_LESSONS_DB")
    return Path(env) if env else config.home_dir() / "lessons.db"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _conn(db: str | Path | None = None) -> sqlite3.Connection:
    p = db_path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)


# ------------------------------------------------------------------ changes
def log_change(job: str | None, target: str, before: Any, after: Any, why: str, who: str = "claude",
               trigger: str = "preview", kind: str | None = None, *, db: str | Path | None = None) -> int:
    """Record one correction. `why` is mandatory: it becomes the reason text of any lesson learnt."""
    if not why or not str(why).strip():
        raise ToolError("why is required", "say in one sentence why the change was needed, e.g. "
                        "'box clipped the value on the right' - repeated reasons become lessons")
    if not target or not str(target).strip():
        raise ToolError("target is required", "name what changed, e.g. 's12.phr_box' or 'caption.s3'")
    if kind is not None and kind in ACTION_KINDS:
        _check_action(kind, after)
    with closing(_conn(db)) as c, c:
        cur = c.execute("INSERT INTO changes(job, target, before, after, why, who, trigger, kind, created) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (job, str(target).strip(), _s(before), _s(after), str(why).strip(), who, trigger, kind,
                         _now()))
        return int(cur.lastrowid or 0)


def list_changes(job: str | None = None, *, db: str | Path | None = None) -> list[dict]:
    with closing(_conn(db)) as c:
        q, a = "SELECT * FROM changes", ()
        if job is not None:
            q, a = q + " WHERE job = ?", (job,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def target_pattern(target: str) -> str:
    """Normalise a change target so similar fixes group: 's12.phr_box' -> '*.phr_box',
    'caption.s3' -> 'caption.*', 'shot[4].zoom' -> 'shot[*].zoom', 'box_17_pad' -> 'box_*_pad'."""
    t = str(target).strip().lower()
    t = re.sub(r"(?<![a-z0-9])s\d+(?![a-z0-9])", "*", t)      # step ids
    t = re.sub(r"\d+(?:\.\d+)?", "*", t)                        # other numbers
    t = re.sub(r"\*(?:[._-]?\*)+", "*", t)
    return t


def _group_key(ch: sqlite3.Row) -> str:
    return f"kind:{ch['kind']}" if ch["kind"] else f"target:{target_pattern(ch['target'])}"


def _load(v: str | None) -> Any:
    if v is None:
        return None
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v


def _check_action(kind: str, after: Any) -> dict:
    a = _load(after) if isinstance(after, str) else after
    keys = ACTION_KINDS[kind]
    if not isinstance(a, dict) or any(k not in a for k in keys):
        raise ToolError(f"{kind} change needs after={{{', '.join(keys)}}}",
                        f"e.g. after={{{', '.join(f'{k!r}: ...' for k in keys)}}}")
    return {k: a[k] for k in keys}


def _auto_rule(key: str, rows: list[sqlite3.Row]) -> str:
    whys = Counter(re.sub(r"\s+", " ", r["why"]).strip().rstrip(".") for r in rows)
    top = whys.most_common(1)[0][0]
    what = key.split(":", 1)[1]
    afters = Counter(r["after"] for r in rows if r["after"] is not None)
    rule = f"{what}: {top}"
    if afters:
        val, n = afters.most_common(1)[0]
        if n >= 2 and len(val) <= 60:
            rule += f" (usually -> {val})"
    return rule[:RULE_MAX]


def _auto_action(kind: str | None, rows: list[sqlite3.Row]) -> dict | None:
    if kind not in ACTION_KINDS:
        return None
    acts = []
    for r in rows:
        try:
            acts.append(json.dumps(_check_action(kind, r["after"]), sort_keys=True))
        except ToolError:
            continue
    if not acts:
        return None
    return json.loads(Counter(acts).most_common(1)[0][0])


def _auto_scope(rows: list[sqlite3.Row]) -> dict:
    """Scope = shared job prefix (product) when every change came from jobs with the same prefix."""
    jobs = {str(r["job"] or "") for r in rows}
    prefixes = {re.split(r"[-_/:]", j, maxsplit=1)[0] for j in jobs if j}
    scope: dict = {}
    if len(prefixes) == 1 and len(jobs) > 1:
        scope["product"] = prefixes.pop()
    return scope


def promote(*, min_count: int = PROMOTE_AT, db: str | Path | None = None) -> list[dict]:
    """Group changes by kind (or normalised target) and create/refresh a candidate lesson for every group
    with ≥ min_count changes. Approved/retired lessons keep their status; evidence_count is refreshed.
    Returns the created/updated lessons."""
    out = []
    with closing(_conn(db)) as c, c:
        rows = list(c.execute("SELECT * FROM changes ORDER BY id"))
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault(_group_key(r), []).append(r)
        for key, rs in sorted(groups.items()):
            if len(rs) < min_count:
                continue
            kind = rs[0]["kind"]
            action = _auto_action(kind, rs)
            now = _now()
            ex = c.execute("SELECT * FROM lessons WHERE pattern = ?", (key,)).fetchone()
            if ex is None:
                cur = c.execute("INSERT INTO lessons(rule, scope, kind, pattern, evidence_count, status, action, "
                                "created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                                (_auto_rule(key, rs), json.dumps(_auto_scope(rs), sort_keys=True), kind, key,
                                 len(rs), "candidate", json.dumps(action, sort_keys=True) if action else None,
                                 now, now))
                lid = int(cur.lastrowid or 0)
            else:
                lid = ex["id"]
                if ex["evidence_count"] == len(rs):
                    continue
                upd = {"evidence_count": len(rs), "updated": now}
                if ex["status"] == "candidate":          # humans may have edited approved rules; keep them
                    upd["rule"] = _auto_rule(key, rs)
                    upd["action"] = json.dumps(action, sort_keys=True) if action else None
                c.execute(f"UPDATE lessons SET {', '.join(f'{k} = ?' for k in upd)} WHERE id = ?",
                          (*upd.values(), lid))
            c.execute(f"UPDATE changes SET lesson_id = ? WHERE id IN ({','.join('?' * len(rs))})",
                      (lid, *[r["id"] for r in rs]))
            out.append(_lesson(c, lid))
    return out


# ------------------------------------------------------------------ lessons
def _lesson(c: sqlite3.Connection, lid: int) -> dict:
    r = c.execute("SELECT * FROM lessons WHERE id = ?", (lid,)).fetchone()
    if r is None:
        raise ToolError(f"lesson {lid} not found", "list lessons with list_lessons()")
    d = dict(r)
    d["scope"] = _load(d["scope"]) or {}
    d["action"] = _load(d["action"])
    return d


def _set_status(lid: int, status: str, db) -> dict:
    with closing(_conn(db)) as c, c:
        _lesson(c, lid)
        c.execute("UPDATE lessons SET status = ?, updated = ? WHERE id = ?", (status, _now(), lid))
        return _lesson(c, lid)


def approve(lid: int, *, rule: str | None = None, scope: dict | None = None, db: str | Path | None = None) -> dict:
    """Approve a candidate (optionally rewording the rule / narrowing its scope)."""
    with closing(_conn(db)) as c, c:
        _lesson(c, lid)
        if rule is not None:
            if not rule.strip():
                raise ToolError("rule must not be empty", "write the rule as one imperative sentence")
            c.execute("UPDATE lessons SET rule = ? WHERE id = ?", (rule.strip()[:RULE_MAX], lid))
        if scope is not None:
            c.execute("UPDATE lessons SET scope = ? WHERE id = ?", (json.dumps(scope, sort_keys=True), lid))
    return _set_status(lid, "approved", db)


def retire(lid: int, *, db: str | Path | None = None) -> dict:
    return _set_status(lid, "retired", db)


def add_lesson(rule: str, scope: dict | None = None, kind: str | None = None, action: dict | None = None,
               status: str = "candidate", *, db: str | Path | None = None) -> dict:
    """Create a lesson by hand (e.g. from the review page)."""
    if not rule or not rule.strip():
        raise ToolError("rule is required", "one imperative sentence, ≤200 chars")
    if status not in STATUSES:
        raise ToolError(f"bad status {status!r}", f"use one of {', '.join(STATUSES)}")
    if kind in ACTION_KINDS and action is not None:
        action = _check_action(kind, action)
    now = _now()
    with closing(_conn(db)) as c, c:
        cur = c.execute("INSERT INTO lessons(rule, scope, kind, pattern, evidence_count, status, action, created, "
                        "updated) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rule.strip()[:RULE_MAX], json.dumps(scope or {}, sort_keys=True), kind, None, 0, status,
                         json.dumps(action, sort_keys=True) if action else None, now, now))
        return _lesson(c, int(cur.lastrowid or 0))


def list_lessons(status: str | None = None, *, db: str | Path | None = None) -> list[dict]:
    with closing(_conn(db)) as c:
        q, a = "SELECT id FROM lessons", ()
        if status:
            q, a = q + " WHERE status = ?", (status,)
        return [_lesson(c, r["id"]) for r in c.execute(q + " ORDER BY id", a)]


def _ctx(context: dict | str | None) -> tuple[dict, set[str]]:
    if isinstance(context, dict):
        text = " ".join(str(v) for v in context.values())
        return {k: str(v).lower() for k, v in context.items() if v is not None}, set(re.findall(r"[a-z0-9_]+",
                                                                                                  text.lower()))
    s = str(context or "").lower()
    return {}, set(re.findall(r"[a-z0-9_]+", s))


def _score(les: dict, cdict: dict, words: set[str]) -> float | None:
    """None = scope conflicts with the context (skip); else higher is more relevant."""
    score = 0.0
    for k, v in (les["scope"] or {}).items():
        v = str(v).lower()
        if k in cdict:
            if cdict[k] != v and v not in cdict[k]:
                return None
            score += 3
        elif v in words or any(w in words for w in re.findall(r"[a-z0-9_]+", v)):
            score += 2
        # scope key absent from context: still eligible (weakly)
    rule_words = set(re.findall(r"[a-z0-9_]+", les["rule"].lower()))
    score += 0.5 * len(rule_words & words)
    score += min(3.0, les["evidence_count"] / 3)
    return score


def get_lessons(context: dict | str | None = None, limit: int = 8, *, budget: int = BUDGET,
                db: str | Path | None = None) -> dict:
    """Most relevant approved lessons for a job. context: {product?, screen?, tool?, ...} or free text.
    Returns {lessons:[{id, rule, scope, kind, action}], text, more} with text ≤ budget chars."""
    cdict, words = _ctx(context)
    ranked = []
    for les in list_lessons("approved", db=db):
        sc = _score(les, cdict, words)
        if sc is not None:
            ranked.append((-sc, -les["evidence_count"], les["id"], les))
    ranked.sort(key=lambda r: r[:3])
    picked, lines, used = [], [], 0
    for _, _, _, les in ranked[:max(0, int(limit))]:
        sc = ", ".join(f"{k}={v}" for k, v in sorted((les["scope"] or {}).items()))
        line = f"- [L{les['id']}] {les['rule']}" + (f" ({sc})" if sc else "")
        if used + len(line) + 1 > budget:
            break
        used += len(line) + 1
        lines.append(line)
        picked.append({k: les[k] for k in ("id", "rule", "scope", "kind", "action", "evidence_count")})
    return {"lessons": picked, "text": "\n".join(lines), "more": max(0, len(ranked) - len(picked))}


def active_actions(kind: str, context: dict | str | None = None, *, db: str | Path | None = None) -> list[dict]:
    """Approved auto-apply actions of one kind (e.g. 'lexicon' -> [{written, spoken}]), scope-filtered by context."""
    if kind not in ACTION_KINDS:
        raise ToolError(f"unknown action kind {kind!r}", f"use one of {', '.join(ACTION_KINDS)}")
    cdict, words = _ctx(context)
    out = []
    for les in list_lessons("approved", db=db):
        if les["kind"] != kind or not les["action"]:
            continue
        if context is not None and _score(les, cdict, words) is None:
            continue
        out.append(dict(les["action"], lesson_id=les["id"]))
    return out


# ------------------------------------------------------------------ gaps
def report_gap(tool: str, need: str, example: str | None = None, *, db: str | Path | None = None) -> int:
    """Record something a tool could not do (a feature request from the field)."""
    if not tool or not str(tool).strip() or not need or not str(need).strip():
        raise ToolError("tool and need are required", "e.g. report_gap('make_doc', 'table of per-UE stats', "
                        "'wanted a column per UE')")
    with closing(_conn(db)) as c, c:
        cur = c.execute("INSERT INTO gaps(tool, need, example, status, created) VALUES (?,?,?,?,?)",
                        (str(tool).strip(), str(need).strip(), example, "open", _now()))
        return int(cur.lastrowid or 0)


def list_gaps(status: str | None = "open", *, db: str | Path | None = None) -> list[dict]:
    with closing(_conn(db)) as c:
        q, a = "SELECT * FROM gaps", ()
        if status:
            q, a = q + " WHERE status = ?", (status,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def close_gap(gid: int, status: str = "closed", *, db: str | Path | None = None) -> None:
    with closing(_conn(db)) as c, c:
        if c.execute("UPDATE gaps SET status = ? WHERE id = ?", (status, gid)).rowcount == 0:
            raise ToolError(f"gap {gid} not found", "list gaps with list_gaps(status=None)")


# ------------------------------------------------------------------ review page
def render_review_html(path: str | Path, *, db: str | Path | None = None) -> Path:
    """Write a static HTML page listing candidate + approved lessons and open gaps (for human review)."""
    e = lambda v: html.escape("" if v is None else (v if isinstance(v, str) else json.dumps(v, sort_keys=True)))
    acc = "#%02x%02x%02x" % config.ACCENT
    ink = "#%02x%02x%02x" % config.INK

    def table(title: str, hdr: list[str], rows: list[list]) -> str:
        if not rows:
            return f"<h2>{e(title)}</h2><p class=none>None.</p>"
        h = "".join(f"<th>{e(x)}</th>" for x in hdr)
        b = "".join("<tr>" + "".join(f"<td>{e(x)}</td>" for x in r) + "</tr>" for r in rows)
        return f"<h2>{e(title)} ({len(rows)})</h2><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"

    ls = list_lessons(db=db)
    parts = [
        table("Candidate lessons", ["id", "rule", "scope", "kind", "evidence", "action"],
              [[x["id"], x["rule"], x["scope"], x["kind"], x["evidence_count"], x["action"]]
               for x in ls if x["status"] == "candidate"]),
        table("Approved lessons", ["id", "rule", "scope", "kind", "evidence", "action"],
              [[x["id"], x["rule"], x["scope"], x["kind"], x["evidence_count"], x["action"]]
               for x in ls if x["status"] == "approved"]),
        table("Open gaps", ["id", "tool", "need", "example", "created"],
              [[g["id"], g["tool"], g["need"], g["example"], g["created"]] for g in list_gaps("open", db=db)]),
    ]
    page = (f"<!doctype html><html><head><meta charset=utf-8><title>casefile lessons review</title><style>"
            f"body{{font-family:system-ui,sans-serif;color:{ink};margin:2em;max-width:1100px}}"
            f"h1,h2{{color:{acc}}}table{{border-collapse:collapse;width:100%;margin-bottom:1.5em}}"
            f"th,td{{border:1px solid #d1d5db;padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}}"
            f"th{{background:#eef2ff}}.none{{color:#6b7280}}</style></head><body>"
            f"<h1>Lessons review</h1><p>Approve with <code>lessons.approve(id)</code>, retire with "
            f"<code>lessons.retire(id)</code>.</p>{''.join(parts)}</body></html>\n")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")
    return p
