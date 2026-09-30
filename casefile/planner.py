"""Edit planner: spec -> absolute timeline (Plan) consumed by render.py.

Pipeline: ``load_spec`` (pydantic validation -> readable ToolError) -> line durations (TTS or word-rate
estimate) -> per-shot line timing -> TimeRef resolution -> find->rect via OCR -> camera keyframes ->
absolute shot starts (xfade overlap subtracted) -> chapters + warnings.

Timing rules (see docs/CONTRACT.md):
  t = lead; each line starts at max(t, at); next t = end + gap.
  dur = max(play_len, last_end + tail); cards: min 3 s.  play_len = sum((t1 - t0) / speed).
  T0 = prev_T_end - xfade (first shot 0).  Chapter start = T0 + xfade / 2.

Relative paths in the spec (``source``) resolve against CASEFILE_WORKDIR when set, else the CWD
(``errors.check_path``) -- NOT against the spec file's directory.
"""
from __future__ import annotations

import importlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import ToolError, check_path
from .spec import DEFAULT_LEXICON, Shot, Spec

WORDS_PER_SEC = 2.6
MIN_CARD = 3.0
_OV_TYPES = {"box", "title", "badge", "tiles", "phases", "dots", "vline", "arrow", "blur"}
_UNION_NOISE = {"float", "str", "int", "bool", "list", "dict"}


# --------------------------------------------------------------------------- spec loading

def _loc(loc: tuple) -> str:
    out = ""
    prev = None
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            s = str(part)
            # drop discriminator tags (overlays[0].box.rect) and union-member noise (to.float)
            if (prev == "int_index" and s in _OV_TYPES) or s in _UNION_NOISE or "[" in s or "function-" in s:
                prev = s
                continue
            out += ("." if out else "") + s
        prev = "int_index" if isinstance(part, int) else part
    return out or "spec"


def _validation_error(e: ValidationError) -> ToolError:
    msgs: list[str] = []
    seen: set[str] = set()
    for err in e.errors():
        path = _loc(tuple(err.get("loc", ())))
        if path in seen:
            continue
        seen.add(path)
        msgs.append(f"{path}: {err.get('msg', 'invalid')}")
    more = f" (+{len(msgs) - 3} more)" if len(msgs) > 3 else ""
    return ToolError("invalid spec: " + "; ".join(msgs[:3]) + more,
                     "fix the listed field(s); see casefile/spec.py for the schema")


def load_spec(spec: dict | str | Path | Spec) -> Spec:
    """Parse and validate an edit spec (dict, JSON text, JSON file path, or Spec)."""
    if isinstance(spec, Spec):
        return spec
    data: Any = spec
    if isinstance(spec, Path) or (isinstance(spec, str) and not spec.lstrip().startswith("{")):
        p = check_path(spec)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ToolError(f"spec file is not valid JSON: {p.name} line {e.lineno}: {e.msg}", "fix the JSON syntax")
    elif isinstance(spec, str):
        try:
            data = json.loads(spec)
        except json.JSONDecodeError as e:
            raise ToolError(f"spec is not valid JSON: line {e.lineno}: {e.msg}", "fix the JSON syntax")
    if not isinstance(data, dict):
        raise ToolError("spec must be a JSON object", 'e.g. {"source": "demo.mp4", "shots": [...]}')
    try:
        return Spec.model_validate(data)
    except ValidationError as e:
        raise _validation_error(e) from None


# --------------------------------------------------------------------------- data classes

@dataclass
class PShot:
    """One planned shot. Times in ``line_times``/``overlays``/``camera`` are LOCAL seconds."""
    idx: int
    shot: Shot
    T0: float
    dur: float
    xfade: float
    chapter: str | None
    line_times: dict[str, tuple[float, float]] = field(default_factory=dict)
    overlays: list[dict] = field(default_factory=list)
    camera: list[tuple[float, tuple[float, float, float, float]]] = field(default_factory=list)

    @property
    def T1(self) -> float:
        """Absolute end time."""
        return self.T0 + self.dur

    @property
    def card(self) -> str | None:
        return self.shot.card

    def src_time(self, t_local: float) -> float:
        """Source seconds shown at local time t (segments with speed, then hold). Cards -> 0."""
        if self.shot.card:
            return 0.0
        t = max(0.0, float(t_local))
        segs = self.shot.src
        for seg in segs:
            a, b = float(seg[0]), float(seg[1])
            sp = float(seg[2]) if len(seg) == 3 else 1.0
            L = (b - a) / sp
            if t < L:
                return a + t * sp
            t -= L
        if self.shot.hold is not None:
            return float(self.shot.hold)
        return float(segs[-1][1]) if segs else 0.0


@dataclass
class Plan:
    spec: Spec
    source: Path | None
    src_size: tuple[int, int] | None
    fps: int
    size: tuple[int, int]
    total: float
    remap: dict[int, int]
    warnings: list[str]
    lines: dict[str, dict]
    chapters: list[tuple[float, str]]
    shots: list[PShot]
    src_fps: float | None = None
    src_duration: float | None = None

    @property
    def frames(self) -> int:
        return int(math.ceil(self.total * self.fps - 1e-9))

    def shot_at(self, T: float) -> PShot:
        """Last shot whose start <= T (the incoming one during a crossfade)."""
        cur = self.shots[0]
        for s in self.shots:
            if s.T0 <= T:
                cur = s
        return cur


# --------------------------------------------------------------------------- helpers

def estimate(text: str, speed: float = 1.0) -> float:
    """Word-rate duration estimate: words / (2.6 * speed) + 0.3 s."""
    return len(text.split()) / (WORDS_PER_SEC * max(1e-6, speed)) + 0.3


def _speakable_local(text: str, lexicon: dict[str, str]) -> str:
    lex = {**DEFAULT_LEXICON, **(lexicon or {})}
    for k in sorted(lex, key=len, reverse=True):
        text = re.sub(rf"(?<![\w-]){re.escape(k)}(?![\w-])", lex[k], text)
    return text


def _optional(name: str):
    try:
        return importlib.import_module(f"casefile.{name}")
    except ImportError:
        return None


_REF = re.compile(r"(s|e):(.+?)\s*(?:([+-])\s*(\d+(?:\.\d*)?|\.\d+))?")
_END = re.compile(r"end\s*(?:([+-])\s*(\d+(?:\.\d*)?|\.\d+))?")


def resolve_time(ref: Any, line_times: dict[str, tuple[float, float]], dur: float, where: str) -> float:
    """TimeRef -> local seconds. number | 's:<id>[+-x]' | 'e:<id>[+-x]' | 'end[+-x]'."""
    if isinstance(ref, bool):
        raise ToolError(f"{where}: invalid time {ref!r}", "use a number, 's:<line>+x', 'e:<line>-x' or 'end-x'")
    if isinstance(ref, (int, float)):
        return float(ref)
    s = str(ref).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _END.fullmatch(s)
    if m:
        off = float(m.group(2)) * (-1 if m.group(1) == "-" else 1) if m.group(2) else 0.0
        return dur + off
    m = _REF.fullmatch(s)
    if m:
        kind, lid = m.group(1), m.group(2)
        off = float(m.group(4)) * (-1 if m.group(3) == "-" else 1) if m.group(4) else 0.0
        if lid not in line_times and s[2:] in line_times:   # ids containing +/-
            lid, off = s[2:], 0.0
        if lid not in line_times:
            known = ", ".join(line_times) or "(none)"
            raise ToolError(f"{where}: line {lid!r} is not spoken in this shot",
                            f"time refs can only use this shot's lines: {known}")
        a, b = line_times[lid]
        return (a if kind == "s" else b) + off
    raise ToolError(f"{where}: invalid time {ref!r}", "use a number, 's:<line>+x', 'e:<line>-x' or 'end-x'")


def _line_items(shot: Shot, i: int, known: dict) -> list[tuple[str, float | None]]:
    out = []
    for j, item in enumerate(shot.lines):
        if isinstance(item, str):
            lid, at = item, None
        elif isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) \
                and isinstance(item[1], (int, float)) and not isinstance(item[1], bool):
            lid, at = item[0], float(item[1])
        else:
            raise ToolError(f"shots[{i}].lines[{j}]: invalid entry {item!r}", 'use "id" or ["id", at_seconds]')
        if lid not in known:
            ks = ", ".join(sorted(known)) or "(none defined)"
            raise ToolError(f"unknown line id {lid} in shots[{i}].lines",
                            f"define it under spec.lines or use one of: {ks}")
        out.append((lid, at))
    return out


def _play_len(shot: Shot) -> float:
    return sum((s[1] - s[0]) / (s[2] if len(s) == 3 else 1.0) for s in shot.src)


def _durations(spec: Spec, used: list[str], synth: bool, warnings: list[str]) -> dict[str, dict]:
    audio = _optional("audio")
    speak = (lambda t: audio.speakable(t, {**DEFAULT_LEXICON, **spec.lexicon})) if audio and hasattr(audio, "speakable") \
        else (lambda t: _speakable_local(t, spec.lexicon))
    v = spec.voice
    out: dict[str, dict] = {}
    for lid, line in spec.lines.items():
        spoken = speak(line.say or line.caption)
        out[lid] = {"caption": line.caption, "spoken": spoken, "wav": None, "dur": estimate(spoken, v.speed)}
    if synth and v.engine != "none" and used:
        if audio is None or not hasattr(audio, "tts"):
            warnings.append("audio module unavailable: using word-rate duration estimates, no narration")
            return out
        res = audio.tts([{"id": k, "text": spec.lines[k].say or spec.lines[k].caption} for k in used],
                        voice=v.voice, speed=v.speed, lexicon=spec.lexicon)
        for k, d in res.get("durations", {}).items():
            if k in out:
                out[k]["dur"] = float(d)
                if hasattr(audio, "wav_path"):
                    out[k]["wav"] = Path(audio.wav_path(out[k]["spoken"], v.voice, v.speed))
    return out


def _find_rect(source: Path | None, f: dict, where: str) -> list[float]:
    if source is None:
        raise ToolError(f"{where}: find needs a source video", "set spec.source or give an explicit rect")
    vision = _optional("vision")
    if vision is None or not hasattr(vision, "find_text"):
        raise ToolError(f"{where}: OCR (vision module) unavailable", "give an explicit rect instead of find")
    res = vision.find_text(source, f["t"], f["query"], region=f.get("region"), fuzzy=True, row=bool(f.get("row")))
    ms = res.get("matches") or []
    idx = int(f.get("index", 0))
    if not ms or idx >= len(ms):
        raise ToolError(f"{where}.find: no OCR match for {f['query']!r} at t={f['t']}"
                        + (f" (only {len(ms)} matches, index {idx})" if ms else ""),
                        "check with find_text/contact_sheet at that time, fix query/t/region, or use rect")
    m = ms[idx]
    rect = m.get("row_box") if f.get("row") and m.get("row_box") else m["box"]
    return [float(x) for x in rect]


# --------------------------------------------------------------------------- planning

def plan(spec: dict | str | Path | Spec, *, resolve_find: bool = True, synth: bool = True) -> Plan:
    """Resolve a spec into an absolute timeline. See module docstring for the rules."""
    sp = load_spec(spec)
    warnings: list[str] = []
    if not sp.shots:
        raise ToolError("spec has no shots", "add at least one shot")

    # ---- source
    source: Path | None = None
    src_size = src_fps = src_dur = None
    needs_src = [i for i, s in enumerate(sp.shots) if not s.card]
    if sp.source:
        source = check_path(sp.source)
        from .media import video_info
        info = video_info(source)
        src_size, src_fps, src_dur = (info["width"], info["height"]), info["fps"], info["duration"]
    elif needs_src:
        raise ToolError(f"spec.source missing but shots[{needs_src[0]}] is not a card",
                        "set spec.source to the recording (or make every shot a card)")

    # ---- lines
    items_per_shot = [_line_items(s, i, sp.lines) for i, s in enumerate(sp.shots)]
    used: list[str] = []
    for items in items_per_shot:
        for lid, _ in items:
            if lid not in used:
                used.append(lid)
    unused = [k for k in sp.lines if k not in used]
    if unused:
        warnings.append(f"unused lines: {', '.join(unused)}")
    lines = _durations(sp, used, synth, warnings)

    # ---- per-shot timing
    pshots: list[PShot] = []
    chapters: list[tuple[float, str]] = []
    T_end = 0.0
    chap: str | None = None
    for i, (shot, items) in enumerate(zip(sp.shots, items_per_shot)):
        where = f"shots[{i}]"
        if not shot.card and not shot.src and shot.hold is None:
            raise ToolError(f"{where}: needs src segments or a hold time", 'e.g. "src": [[0, 8]] or "hold": 12.5')
        if not shot.card and src_dur:
            for seg in shot.src:
                if seg[1] > src_dur + 0.05:
                    warnings.append(f"{where}.src {seg[:2]} ends after the source ({src_dur:.2f} s)")
            if shot.hold is not None and shot.hold > src_dur + 0.05:
                warnings.append(f"{where}.hold {shot.hold} is after the source end ({src_dur:.2f} s)")
        t = shot.lead
        lt: dict[str, tuple[float, float]] = {}
        last_end = 0.0
        for lid, at in items:
            if lid in lt:
                raise ToolError(f"{where}.lines: line {lid} used twice in one shot", "split it into another line id")
            st = max(t, at) if at is not None else t
            if at is not None and at < t - 1e-6:
                warnings.append(f"{where}: line {lid} asked for t={at:g} but overlaps the previous line; starts at {st:.2f}")
            end = st + lines[lid]["dur"]
            lt[lid] = (st, end)
            last_end = end
            t = end + shot.gap
        dur = max(_play_len(shot), last_end + shot.tail)
        if shot.card:
            dur = max(dur, MIN_CARD)
        xf = shot.xfade if pshots else 0.0
        if xf > 0 and pshots and xf >= min(dur, pshots[-1].dur):
            raise ToolError(f"{where}.xfade {xf} is longer than the shot", "use an xfade shorter than both shots")
        T0 = T_end - xf if pshots else 0.0
        # captions overlapping across a crossfade
        if pshots and xf > 0 and lt and pshots[-1].line_times:
            prev = pshots[-1]
            p_end = max(e for _, e in prev.line_times.values()) + prev.T0
            n_start = min(s for s, _ in lt.values()) + T0
            if n_start < p_end - 1e-6:
                warnings.append(f"{where}: first line overlaps the previous shot's last line by {p_end - n_start:.2f} s")
        name = shot.chapter or chap
        if name and name != chap:
            chapters.append((T0 + xf / 2, name))
        chap = name
        ps = PShot(idx=i, shot=shot, T0=T0, dur=dur, xfade=xf, chapter=chap, line_times=lt)
        ps.overlays = _overlays(ps, source, src_size, resolve_find, warnings)
        ps.camera = _camera(ps, src_size, warnings)
        pshots.append(ps)
        T_end = T0 + dur

    remap: dict[int, int] = {}
    if source is not None and sp.bad_frames != "none":
        detect = _optional("detect")
        if detect is None or not hasattr(detect, "remap_for"):
            warnings.append("detect module unavailable: bad frames not repaired")
        else:
            remap = {int(k): int(v) for k, v in detect.remap_for(source, sp.bad_frames).items()}

    return Plan(spec=sp, source=source, src_size=src_size, fps=sp.output.fps, size=(int(sp.output.size[0]), int(sp.output.size[1])),
                total=T_end, remap=remap, warnings=warnings, lines=lines, chapters=chapters, shots=pshots,
                src_fps=src_fps, src_duration=src_dur)


def _overlays(ps: PShot, source, src_size, resolve_find: bool, warnings: list[str]) -> list[dict]:
    out = []
    for j, ov in enumerate(ps.shot.overlays):
        where = f"shots[{ps.idx}].overlays[{j}]"
        d = ov.model_dump(by_alias=True)
        a = resolve_time(d["from"], ps.line_times, ps.dur, f"{where}.from")
        b = resolve_time(d["to"], ps.line_times, ps.dur, f"{where}.to")
        if a > b + 1e-6:
            raise ToolError(f"{where}: from ({a:.2f}) is after to ({b:.2f})", "swap them or fix the time refs")
        if a >= ps.dur - 1e-6:
            raise ToolError(f"{where}: from ({a:.2f}) is at/after the shot end ({ps.dur:.2f})",
                            "overlay would never show; use an earlier time")
        if abs(a - b) <= 1e-6:
            warnings.append(f"{where}: from == to ({a:.2f}); overlay is invisible")
        if a < 0:
            warnings.append(f"{where}: from {a:.2f} < 0, clamped")
            a = 0.0
        if b > ps.dur + 1e-6:
            warnings.append(f"{where}: to {b:.2f} past shot end {ps.dur:.2f}, clamped")
            b = ps.dur
        d["from"], d["to"] = a, b
        if d["type"] == "tiles":
            for k, tile in enumerate(d["items"]):
                tile["at"] = a if tile.get("at") is None else \
                    resolve_time(tile["at"], ps.line_times, ps.dur, f"{where}.items[{k}].at")
        if d["type"] in ("box", "blur"):
            if d.get("rect") is None and d.get("find") is None:
                raise ToolError(f"{where}: {d['type']} needs rect or find", 'e.g. "rect": [x0, y0, x1, y1]')
            if d.get("rect") is None:
                if resolve_find:
                    d["rect"] = _find_rect(source, d["find"], where)
                else:
                    warnings.append(f"{where}: find not resolved (resolve_find=False)")
            r = d.get("rect")
            if r is not None:
                if len(r) != 4 or r[2] <= r[0] or r[3] <= r[1]:
                    raise ToolError(f"{where}.rect {r}: need [x0, y0, x1, y1] with x1 > x0 and y1 > y0",
                                    "rects are corner coordinates in source pixels, not [x, y, w, h]")
                if src_size and not ps.shot.card:
                    W, H = src_size
                    if r[0] < -1 or r[1] < -1 or r[2] > W + 1 or r[3] > H + 1:
                        raise ToolError(f"{where}.rect {[round(v) for v in r]} is outside the {W}x{H} source frame",
                                        "rects are [x0, y0, x1, y1] in SOURCE pixels; check with find_text")
        out.append(d)
    return out


def _camera(ps: PShot, src_size, warnings: list[str]) -> list[tuple[float, tuple]]:
    if not ps.shot.camera:
        return []
    where = f"shots[{ps.idx}].camera"
    if ps.shot.card or not src_size:
        warnings.append(f"{where}: ignored on a card shot")
        return []
    W, H = src_size
    kf = []
    for k, item in enumerate(ps.shot.camera):
        if not (isinstance(item, list) and len(item) == 2):
            raise ToolError(f"{where}[{k}]: invalid keyframe {item!r}", 'use [time, "full"] or [time, [x, y, w, h]]')
        t = resolve_time(item[0], ps.line_times, ps.dur, f"{where}[{k}]")
        r = item[1]
        if r == "full":
            rect = (0.0, 0.0, float(W), float(H))
        elif isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r):
            rect = tuple(float(v) for v in r)
            if rect[2] <= 0 or rect[3] <= 0:
                raise ToolError(f"{where}[{k}]: rect {r} needs positive w and h", "camera rects are [x, y, w, h]")
            if rect[0] < 0 or rect[1] < 0 or rect[0] + rect[2] > W or rect[1] + rect[3] > H:
                warnings.append(f"{where}[{k}]: rect {r} is partly outside the {W}x{H} source")
        else:
            raise ToolError(f"{where}[{k}]: invalid rect {r!r}", 'use "full" or [x, y, w, h] in source px')
        kf.append((t, rect))
    if any(b[0] < a[0] for a, b in zip(kf, kf[1:])):
        warnings.append(f"{where}: keyframes not in time order; sorted")
        kf.sort(key=lambda x: x[0])
    return kf


def plan_json(spec: dict | str | Path | Spec, *, synth: bool = True, resolve_find: bool = True,
              max_chars: int = 3200) -> dict:
    """Compact MCP answer: {total, frames, shots:[{i, chapter, T0, dur, lines}], warnings} (<= max_chars)."""
    p = plan(spec, synth=synth, resolve_find=resolve_find)
    r2 = lambda x: round(float(x), 2)
    shots = [{"i": s.idx, **({"card": s.card} if s.card else {}), "chapter": s.chapter, "T0": r2(s.T0),
              "dur": r2(s.dur), "lines": {k: [r2(a), r2(b)] for k, (a, b) in s.line_times.items()}}
             for s in p.shots]
    out: dict[str, Any] = {"total": r2(p.total), "frames": p.frames, "shots": shots, "warnings": p.warnings[:8]}
    if len(p.warnings) > 8:
        out["warnings"].append(f"... +{len(p.warnings) - 8} more")
    n = len(shots)
    while len(json.dumps(out, ensure_ascii=False)) > max_chars and out["shots"]:
        out["shots"] = out["shots"][:-1]
        out["shots_omitted"] = n - len(out["shots"])
    return out
