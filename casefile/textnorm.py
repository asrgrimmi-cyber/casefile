"""OCR text normalisation for screen recordings.

`normalise(text, context="terminal"|"gui") -> str` repairs the typical tesseract / PP-OCR misreads on
UI and terminal text without touching real words:
- smart quotes -> ASCII, doubled leading quotes collapsed (`"“ulretx": 5` -> `"ulretx": 5`)
- box-drawing / block characters (tmux, htop, table borders) stripped
- hex look-alikes: `Ox46le` / `0x46Ie` -> `0x461e`
- digit look-alikes inside numbers: O/o/Q/D -> 0, l/I/|/i -> 1, S/s -> 5, B -> 8, Z -> 2 when the token is
  otherwise numeric (`Q01010123456789` -> `001010123456789`, `19.l` -> `19.1`) or the character sits between
  two digits (`1O5` -> `105`)
Quotes, colons, commas and braces are kept so JSON lines stay parseable.
"""
from __future__ import annotations

import re

_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
                         "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'", "\u00b4": "'", "`": "'"})
_BOX = re.compile(r"[\u2500-\u259f\u25a0-\u25ff\u2800-\u28ff]+")
_DIGIT_LIKE = {"O": "0", "o": "0", "Q": "0", "D": "0", "l": "1", "I": "1", "|": "1", "i": "1",
               "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"}
_HEX_LIKE = {"O": "0", "o": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1", "S": "5", "s": "5",
             "G": "6", "g": "9", "Z": "2"}
_HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0Oo][xX]([0-9A-Fa-fOoQlI|iSsGgZ]{2,16})(?![0-9A-Za-z])")
_TOKEN = re.compile(r"[0-9A-Za-z|.,\-+]+")


def _fix_hex(m: re.Match) -> str:
    body = "".join(_HEX_LIKE.get(c, c) for c in m.group(1))
    return "0x" + body.lower()


def _fix_number_token(tok: str, aggressive: bool) -> str:
    core = tok.rstrip(".,")
    if core != tok and core:
        return _fix_number_token(core, aggressive) + tok[len(core):]
    digits = sum(c.isdigit() for c in tok)
    if digits == 0:
        return tok
    chars = list(tok)
    alnum = [c for c in chars if c.isalnum() or c == "|"]
    looks = [c for c in alnum if c in _DIGIT_LIKE]
    others = [c for c in alnum if not c.isdigit() and c not in _DIGIT_LIKE]
    # whole token numeric apart from look-alikes: map them all (needs >= 3 real digits, few look-alikes)
    if aggressive and not others and looks and digits >= 3 and len(looks) <= max(1, digits // 3):
        return "".join(_DIGIT_LIKE.get(c, c) if c in _DIGIT_LIKE else c for c in chars)
    # numeric token with a trailing decimal look-alike: 19.l -> 19.1
    if not others and looks and re.fullmatch(r"[-+]?\d+[.,][0-9OolIS|]+", tok):
        return "".join(_DIGIT_LIKE.get(c, c) for c in chars)
    # look-alike sandwiched between digits: 1O5 -> 105
    out = chars[:]
    for i in range(1, len(chars) - 1):
        if chars[i] in _DIGIT_LIKE and chars[i - 1].isdigit() and chars[i + 1].isdigit():
            out[i] = _DIGIT_LIKE[chars[i]]
    return "".join(out)


def normalise(text: str, context: str = "gui") -> str:
    """Normalise one OCR line/phrase. context "terminal" enables the aggressive numeric repairs."""
    if not text:
        return ""
    s = text.translate(_QUOTES)
    s = _BOX.sub(" ", s)
    # doubled quotes produced by smart-quote misreads: ""key" -> "key"
    s = re.sub(r'(^|[\s{\[,(])""(?=\w)', r'\1"', s)
    s = re.sub(r'(?<=\w)""(?=\s*[:,}\]])', '"', s)
    s = _HEX_TOKEN.sub(_fix_hex, s)
    aggressive = context == "terminal"
    s = _TOKEN.sub(lambda m: _fix_number_token(m.group(0), aggressive), s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return s


def normalize(text: str, context: str = "gui") -> str:  # US spelling alias
    """Alias of :func:`normalise`."""
    return normalise(text, context)
