"""Privacy detection on OCR words: emails, IPv4 addresses, user@host prompts, IMSI-like numbers, secrets.

`scan_text(text) -> [{reason, text, span:(a,b)}]` on a plain string.
`scan_words(words) -> [{reason, text, box}]` on OCR words ({text, box}); matches are mapped back to the union
box of the words they cover. Reasons: email | hostname | ip | imsi | secret.
"""
from __future__ import annotations

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
USERHOST = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*(?::[~/\w.-]*)?")
IPV4 = re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?::\d{1,5})?(?![\d.])")
IMSI = re.compile(r"(?<!\d)\d{15}(?!\d)")
SECRET = re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|token|secret|api[_-]?key|auth(?:orization)?|bearer)\b"
                    r"[\"']?\s*[:=]?\s*[\"']?([^\s\"',}]{3,})")


def scan_text(text: str) -> list[dict]:
    """Private spans in text -> [{reason, text, span}] (non-overlapping, earliest reason wins)."""
    out: list[dict] = []

    def add(reason: str, a: int, b: int) -> None:
        if any(a < o["span"][1] and o["span"][0] < b for o in out):
            return
        out.append({"reason": reason, "text": text[a:b], "span": (a, b)})

    for m in EMAIL.finditer(text):
        add("email", m.start(), m.end())
    for m in USERHOST.finditer(text):
        add("hostname", m.start(), m.end())
    for m in IPV4.finditer(text):
        add("ip", m.start(), m.end())
    for m in IMSI.finditer(text):
        add("imsi", m.start(), m.end())
    for m in SECRET.finditer(text):
        v = m.group(2)
        if v.lower() in ("true", "false", "null", "none", "required", "optional"):
            continue
        add("secret", m.start(2), m.end(2))
    out.sort(key=lambda o: o["span"][0])
    return out


def _lines(words: list[dict]) -> list[list[dict]]:
    from .vision import _lines as vlines
    return vlines(words)


def scan_words(words: list[dict], lines: list[list[dict]] | None = None) -> list[dict]:
    """Private items among OCR words -> [{reason, text, box}] (boxes = union of the covered words)."""
    res: list[dict] = []
    for ln in (lines if lines is not None else _lines(words)):
        if not ln:
            continue
        pos, parts = [], []
        c = 0
        for w in ln:
            t = w["text"]
            pos.append((c, c + len(t)))
            parts.append(t)
            c += len(t) + 1
        text = " ".join(parts)
        for m in scan_text(text):
            a, b = m["span"]
            ws = [w for w, (p0, p1) in zip(ln, pos) if p0 < b and a < p1]
            if not ws:
                continue
            box = [min(w["box"][0] for w in ws), min(w["box"][1] for w in ws),
                   max(w["box"][2] for w in ws), max(w["box"][3] for w in ws)]
            res.append({"reason": m["reason"], "text": m["text"], "box": [int(v) for v in box]})
    return res
