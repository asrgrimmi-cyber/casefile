"""Written step-by-step documentation from a timeline: Markdown, DOCX, PDF and Confluence storage XHTML.

`make_doc(tl, doc)` turns an analysed recording (timeline.json, see docs/CONTRACT_V2.md) plus a short doc
outline written by the assistant into shareable documents. Everything is built from ONE intermediate model
(`_build`) so the four formats always carry the same content:

  title · summary · audience · [section] · numbered steps (body, screenshot with numbered highlight boxes,
  caption, commands, transcript excerpt) · appendix tables (commands / fields)

Screenshots: the step's stable frame; private[] boxes Gaussian-blurred (privacy on by default), cropped
('auto' = union of highlight boxes padded 12% of the frame width, ≥800 px wide), downscaled to ≤1600 px,
then highlight boxes drawn (rounded accent rectangle + numbered badge). Output is deterministic: no
timestamps unless `generated` is passed. No network calls; the Confluence export lists its attachments.
See docs/DOCS_OUTPUT.md for the format.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from . import config
from .draw import hex_rgb, rgba, rrect
from .errors import ToolError
from .fonts import font

FORMATS = ("md", "docx", "pdf", "confluence")
MAX_IMG_W = 1600
MIN_CROP_W = 800
CROP_PAD = 0.12
EXCERPT_MAX = 20
CONF_NS = {"ac": "http://atlassian.com/content", "ri": "http://atlassian.com/resource/identifier"}


# ------------------------------------------------------------------ helpers
def slugify(text: str, default: str = "doc") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:60].strip("-") or default


def _mmss(t: Any) -> str:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return ""
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_USERHOST = re.compile(r"\b[a-z_][\w-]*@[\w-]+\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IMSI = re.compile(r"(?<!\d)\d{15}(?!\d)")


def redact_text(s: str) -> str:
    """Mask emails, user@host, 15-digit IMSIs and non-loopback IPv4 addresses in text (commands, transcripts)."""
    s = _EMAIL.sub("[email]", s)
    s = _USERHOST.sub("[user@host]", s)
    s = _IMSI.sub("[imsi]", s)
    return _IPV4.sub(lambda m: m.group(0) if m.group(0).startswith(("127.", "0.0.0.0")) else "[ip]", s)


# ------------------------------------------------------------------ markdown-lite
Runs = list[tuple[str, str]]          # [(style, text)] style in {"", "b", "code"}
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def inline_runs(text: str) -> Runs:
    out: Runs = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append(("b", part[2:-2]))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(("code", part[1:-1]))
        else:
            out.append(("", part))
    return out


def parse_body(text: str | None) -> list[tuple[str, Any]]:
    """Markdown-lite -> blocks: ('p', runs) · ('ul', [runs]) · ('ol', [runs]) · ('code', str)."""
    blocks: list[tuple[str, Any]] = []
    para: list[str] = []
    lst: tuple[str, list] | None = None
    code: list[str] | None = None

    def flush():
        nonlocal para, lst
        if para:
            blocks.append(("p", inline_runs(" ".join(para))))
            para = []
        if lst:
            blocks.append((lst[0], lst[1]))
            lst = None

    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if code is not None:
            if line.strip().startswith("```"):
                blocks.append(("code", "\n".join(code)))
                code = None
            else:
                code.append(raw.rstrip())
            continue
        s = line.strip()
        if s.startswith("```"):
            flush()
            code = []
            continue
        m_ul = re.match(r"^[-*]\s+(.*)$", s)
        m_ol = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m_ul or m_ol:
            kind = "ul" if m_ul else "ol"
            if para:
                blocks.append(("p", inline_runs(" ".join(para))))
                para = []
            if lst and lst[0] != kind:
                blocks.append((lst[0], lst[1]))
                lst = None
            if lst is None:
                lst = (kind, [])
            lst[1].append(inline_runs((m_ul or m_ol).group(1)))
            continue
        if not s:
            flush()
            continue
        if lst:
            flush()
        para.append(s)
    if code is not None:
        blocks.append(("code", "\n".join(code)))
    flush()
    return blocks


# ------------------------------------------------------------------ model
@dataclass
class DocStep:
    n: int
    step_id: str
    heading: str
    section: str | None
    body: list
    image: Path | None = None
    image_size: tuple[int, int] | None = None
    caption: str = ""
    legend: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    excerpt: str = ""
    t0: float | None = None


@dataclass
class DocModel:
    title: str
    slug: str
    summary: list
    audience: str | None
    generated: str | None
    steps: list[DocStep]
    appendix: list[tuple[str, list[str], list[list[str]]]]    # (heading, header, rows)
    sections: bool


def load_timeline(tl: dict | str | Path, base_dir: str | Path | None = None) -> tuple[dict, Path]:
    """(timeline dict, directory screenshots are relative to)."""
    if isinstance(tl, dict):
        base = Path(base_dir or tl.get("_dir") or ".")
        return tl, base
    p = Path(tl)
    if p.is_dir():
        p = p / "timeline.json"
    if not p.exists():
        raise ToolError(f"timeline not found: {p}", "run analyze_recording first, or pass the timeline.json path")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ToolError(f"timeline is not valid JSON: {e}", "re-run analyze_recording") from e
    return data, Path(base_dir) if base_dir else p.parent


def _doc_steps(doc: dict) -> list[tuple[str | None, dict]]:
    out: list[tuple[str | None, dict]] = []
    if doc.get("sections"):
        for sec in doc["sections"]:
            for st in sec.get("steps") or []:
                out.append((sec.get("heading") or "", st))
    for st in doc.get("steps") or []:
        out.append((None, st))
    return out


def _union(boxes: list) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _crop_rect(crop: Any, boxes: list, W: int, H: int, warnings: list, sid: str) -> tuple[int, int, int, int]:
    if isinstance(crop, (list, tuple)) and len(crop) == 4:
        x0, y0, x1, y1 = (int(round(float(v))) for v in crop)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 - x0 >= 16 and y1 - y0 >= 16:
            return x0, y0, x1, y1
        warnings.append(f"{sid}: crop {list(crop)} is outside the frame; using the full frame")
        return 0, 0, W, H
    if crop != "auto" or not boxes:
        return 0, 0, W, H
    u = _union(boxes)
    pad = CROP_PAD * W
    x0, y0, x1, y1 = u[0] - pad, u[1] - pad, u[2] + pad, u[3] + pad
    min_w = min(W, MIN_CROP_W)
    if x1 - x0 < min_w:
        cx = (x0 + x1) / 2
        x0, x1 = cx - min_w / 2, cx + min_w / 2
    min_h = min(H, 0.3 * (x1 - x0))
    if y1 - y0 < min_h:
        cy = (y0 + y1) / 2
        y0, y1 = cy - min_h / 2, cy + min_h / 2
    # shift inside the frame, then clamp
    if x0 < 0:
        x1, x0 = x1 - x0, 0
    if x1 > W:
        x0, x1 = x0 - (x1 - W), W
    if y0 < 0:
        y1, y0 = y1 - y0, 0
    if y1 > H:
        y0, y1 = y0 - (y1 - H), H
    return max(0, int(x0)), max(0, int(y0)), min(W, int(round(x1))), min(H, int(round(y1)))


def blur_boxes(img: Image.Image, boxes: list) -> Image.Image:
    """Gaussian-blur each box in place (strong enough to make text unreadable)."""
    for b in boxes:
        x0, y0 = max(0, int(b[0]) - 4), max(0, int(b[1]) - 4)
        x1, y1 = min(img.width, int(round(b[2])) + 4), min(img.height, int(round(b[3])) + 4)
        if x1 <= x0 or y1 <= y0:
            continue
        rad = max(6.0, (y1 - y0) * 0.45)
        part = img.crop((x0, y0, x1, y1))
        for _ in range(2):
            part = part.filter(ImageFilter.GaussianBlur(rad))
        img.paste(part, (x0, y0))
    return img


def draw_highlights(img: Image.Image, boxes: list[tuple[list[float], str | None]], accent) -> Image.Image:
    """Rounded accent rectangles + numbered badges (and optional label chips) in image px."""
    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    s = max(0.6, img.width / 1280)
    lw = max(2, round(3 * s))
    pad = 6 * s
    r_badge = 13 * s
    for i, (b, label) in enumerate(boxes, 1):
        bx = (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
        rrect(d, (bx[0] - 4 * s, bx[1] - 4 * s, bx[2] + 4 * s, bx[3] + 4 * s), 12 * s, outline=rgba(accent, 70),
              width=max(1, round(5 * s)))
        rrect(d, bx, 8 * s, outline=rgba(accent, 255), width=lw)
        # badge sits on the outer top-left corner so it never covers the first glyph
        cx = min(max(r_badge + 1, bx[0] - r_badge * 0.6), img.width - r_badge - 1)
        cy = min(max(r_badge + 1, bx[1] - r_badge * 0.6), img.height - r_badge - 1)
        d.ellipse((cx - r_badge, cy - r_badge, cx + r_badge, cy + r_badge), fill=rgba(accent, 255),
                  outline=(255, 255, 255, 255), width=max(1, round(2 * s)))
        f = font("b", max(10, round(15 * s)))
        num = str(i)
        tw = f.getlength(num)
        d.text((cx - tw / 2, cy - 15 * s * 0.62), num, font=f, fill=(255, 255, 255, 255))
        if label:
            lf = font("m", max(10, round(15 * s)))
            w, h = lf.getlength(label) + 16 * s, 26 * s
            lx = min(max(0, bx[2] + 8 * s), img.width - w - 2)
            ly = min(max(0, bx[1]), img.height - h - 2)
            rrect(d, (lx, ly, lx + w, ly + h), 7 * s, fill=rgba(accent, 235))
            d.text((lx + 8 * s, ly + 4 * s), label, font=lf, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, ov).convert("RGB")


def _hl_entries(h: Any) -> list[tuple[str, str | None]]:
    out = []
    for e in h or []:
        if isinstance(e, str):
            out.append((e, None))
        elif isinstance(e, dict) and e.get("item"):
            out.append((str(e["item"]), e.get("label")))
    return out


def _excerpt(transcript: str, texts: list[str]) -> str:
    keys = [_norm(t) for t in texts if _norm(t)]
    if not transcript or not keys:
        return ""
    lines, seen = [], set()
    for ln in transcript.split("\n"):
        nl = _norm(ln)
        if nl and any(k in nl or nl in k for k in keys) and nl not in seen:
            seen.add(nl)
            lines.append(ln.rstrip())
        if len(lines) >= EXCERPT_MAX:
            break
    return "\n".join(lines)


def _build(tl: dict, base: Path, doc: dict, out: Path, accent, privacy: bool, generated: str | None,
           warnings: list) -> DocModel:
    title = str(doc.get("title") or "").strip()
    if not title:
        raise ToolError("doc.title is required", "pass doc={title, summary, steps:[{step:'s1', heading, body}]}")
    slug = slugify(doc.get("slug") or title)
    steps_by_id = {s.get("id"): s for s in tl.get("steps") or []}
    items: dict[str, tuple[str, dict]] = {}
    for s in tl.get("steps") or []:
        for it in s.get("items") or []:
            items[it.get("id")] = (s.get("id"), it)
    red = redact_text if privacy else (lambda x: x)

    img_dir = out / "images"
    if img_dir.exists():
        for old in img_dir.glob("step-*.png"):
            old.unlink()
    img_dir.mkdir(parents=True, exist_ok=True)

    dsteps: list[DocStep] = []
    entries = _doc_steps(doc)
    if not entries:
        warnings.append("doc has no steps; only title and summary were written")
    for n, (section, ds) in enumerate(entries, 1):
        sid = str(ds.get("step") or "")
        st = steps_by_id.get(sid)
        if st is None:
            warnings.append(f"step {sid or '?'} not in the timeline (known: {', '.join(map(str, steps_by_id))}); "
                            "text only")
            st = {}
        heading = str(ds.get("heading") or st.get("title") or f"Step {n}")
        dstep = DocStep(n=n, step_id=sid, heading=heading, section=section, body=parse_body(ds.get("body")),
                        t0=st.get("t0"))
        hl: list[tuple[list[float], str | None, str]] = []
        for iid, label in _hl_entries(ds.get("highlight")):
            if iid not in items:
                warnings.append(f"{sid}: unknown item id {iid!r}; not highlighted")
                continue
            owner, it = items[iid]
            if owner != sid:
                warnings.append(f"{sid}: item {iid!r} belongs to step {owner}; not highlighted")
                continue
            if not it.get("box") or len(it["box"]) != 4:
                warnings.append(f"{sid}: item {iid!r} has no box; not highlighted")
                continue
            hl.append(([float(v) for v in it["box"]], label, str(it.get("text", ""))))
        dstep.legend = [f"{i}: {red(lab or txt)}" for i, (_, lab, txt) in enumerate(hl, 1)]

        shot = st.get("screenshot")
        if shot:
            src = base / shot
            if not src.exists():
                warnings.append(f"{sid}: screenshot missing ({src}); no image")
            else:
                img = Image.open(src).convert("RGB")
                W, H = img.size
                if privacy:
                    img = blur_boxes(img, [p["box"] for p in st.get("private") or [] if p.get("box")])
                cx0, cy0, cx1, cy1 = _crop_rect(ds.get("crop", "auto"), [b for b, _, _ in hl], W, H, warnings, sid)
                img = img.crop((cx0, cy0, cx1, cy1))
                k = 1.0
                if img.width > MAX_IMG_W:
                    k = MAX_IMG_W / img.width
                    img = img.resize((MAX_IMG_W, max(1, round(img.height * k))), Image.LANCZOS)
                boxes = [([(b[0] - cx0) * k, (b[1] - cy0) * k, (b[2] - cx0) * k, (b[3] - cy0) * k], lab)
                         for b, lab, _ in hl]
                for (b, _), (_, _, txt) in zip(boxes, hl):
                    if b[2] < 0 or b[3] < 0 or b[0] > img.width or b[1] > img.height:
                        warnings.append(f"{sid}: highlight {txt!r} is outside the crop")
                img = draw_highlights(img, boxes, accent)
                dest = img_dir / f"step-{n:02d}.png"
                img.save(dest, optimize=False)
                dstep.image, dstep.image_size = dest, img.size
        cap = f"Figure {n}: {heading}"
        if st.get("title") and st.get("title") != heading:
            cap += f" ({st['title']})"
        if st.get("t0") is not None:
            cap += f" at {_mmss(st.get('stable_t', st.get('t0')))}"
        dstep.caption = red(cap)

        want_cmds = ds.get("command")
        if want_cmds is None:
            want_cmds = st.get("kind") == "terminal"
        if want_cmds:
            dstep.commands = [{"t": c.get("t"), "text": red(str(c.get("text", "")))}
                              for c in st.get("commands") or [] if c.get("text")]
        if st.get("kind") == "terminal" and hl:
            dstep.excerpt = red(_excerpt(st.get("transcript") or "", [t for _, _, t in hl]))
        dsteps.append(dstep)

    appendix: list[tuple[str, list[str], list[list[str]]]] = []
    ap = doc.get("appendix")
    kinds = set()
    if isinstance(ap, str):
        kinds = {"commands", "fields"} if ap in ("both", "all") else {ap}
    elif isinstance(ap, (list, tuple)):
        kinds = set(ap)
    for bad in sorted(kinds - {"commands", "fields"}):
        warnings.append(f"unknown appendix {bad!r} (use 'commands', 'fields' or 'both')")
    if "commands" in kinds:
        rows = []
        for s in tl.get("steps") or []:
            for c in s.get("commands") or []:
                rows.append((float(c.get("t") or 0), [_mmss(c.get("t")), str(s.get("id")), red(str(c.get("text", "")))]))
        rows.sort(key=lambda r: r[0])
        appendix.append(("Commands", ["Time", "Step", "Command"], [r for _, r in rows]))
    if "fields" in kinds:
        used = [d.step_id for d in dsteps if d.step_id in steps_by_id] or list(steps_by_id)
        rows2, seen = [], set()
        for sid in used:
            if sid in seen:
                continue
            seen.add(sid)
            for k2, v in (steps_by_id[sid].get("fields") or {}).items():
                rows2.append([str(sid), red(str(k2)), red(str(v))])
        appendix.append(("Fields", ["Step", "Field", "Value"], rows2))

    return DocModel(title=title, slug=slug, summary=parse_body(doc.get("summary")),
                    audience=(str(doc["audience"]) if doc.get("audience") else None), generated=generated,
                    steps=dsteps, appendix=appendix, sections=any(d.section is not None for d in dsteps))


# ------------------------------------------------------------------ Markdown
def _md_runs(runs: Runs) -> str:
    out = []
    for st, t in runs:
        out.append(f"**{t}**" if st == "b" else f"`{t}`" if st == "code" else t)
    return "".join(out)


def _md_blocks(blocks: list) -> list[str]:
    lines: list[str] = []
    for kind, v in blocks:
        if kind == "p":
            lines += [_md_runs(v), ""]
        elif kind in ("ul", "ol"):
            lines += [(f"{i}. " if kind == "ol" else "- ") + _md_runs(r) for i, r in enumerate(v, 1)] + [""]
        elif kind == "code":
            lines += ["```", v, "```", ""]
    return lines


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_md(m: DocModel, out: Path) -> Path:
    L = [f"# {m.title}", ""]
    if m.audience:
        L += [f"**Audience:** {m.audience}", ""]
    if m.generated:
        L += [f"*Generated {m.generated}*", ""]
    L += _md_blocks(m.summary)
    section = None
    hs = "###" if m.sections else "##"
    for s in m.steps:
        if s.section is not None and s.section != section:
            section = s.section
            L += [f"## {section}", ""]
        L += [f"{hs} {s.n}. {s.heading}", ""]
        L += _md_blocks(s.body)
        if s.image:
            L += [f"![{s.caption}](images/{s.image.name})", "", f"*{s.caption}*", ""]
            if s.legend:
                L += ["Highlighted: " + " · ".join(s.legend), ""]
        if s.commands:
            L += ["```bash"] + [c["text"] for c in s.commands] + ["```", ""]
        if s.excerpt:
            L += ["Output excerpt:", "", "```text", s.excerpt, "```", ""]
    if m.appendix:
        L += ["## Appendix", ""]
        for head, hdr, rows in m.appendix:
            L += [f"### {head}", ""]
            if not rows:
                L += ["_None._", ""]
                continue
            L += ["| " + " | ".join(hdr) + " |", "|" + "|".join("---" for _ in hdr) + "|"]
            L += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in rows] + [""]
    p = out / f"{m.slug}.md"
    p.write_bytes(("\n".join(L).rstrip() + "\n").encode("utf-8"))
    return p


# ------------------------------------------------------------------ DOCX
def render_docx(m: DocModel, out: Path, accent) -> Path:
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    d = Document()
    st = d.styles.add_style("Casefile Code", WD_STYLE_TYPE.PARAGRAPH)
    st.base_style = d.styles["Normal"]
    st.font.name = "Consolas"
    st.font.size = Pt(9)
    st.paragraph_format.space_after = Pt(0)
    rpr = st.element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rf.set(qn(a), "Consolas")
    ppr = st.element.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), "F3F4F6")
    ppr.append(shd)
    acc = RGBColor(*accent)

    def runs(par, rs: Runs):
        for sty, t in rs:
            r = par.add_run(t)
            if sty == "b":
                r.bold = True
            elif sty == "code":
                r.font.name = "Consolas"
                r.font.color.rgb = acc

    def blocks(bs):
        for kind, v in bs:
            if kind == "p":
                runs(d.add_paragraph(), v)
            elif kind in ("ul", "ol"):
                for r in v:
                    runs(d.add_paragraph(style="List Bullet" if kind == "ul" else "List Number"), r)
            elif kind == "code":
                code(v)

    def code(text: str):
        for ln in text.split("\n"):
            d.add_paragraph(ln or " ", style="Casefile Code")

    def table(hdr, rows):
        t = d.add_table(rows=1, cols=len(hdr))
        t.style = "Table Grid"
        for c, h in zip(t.rows[0].cells, hdr):
            c.text = ""
            c.paragraphs[0].add_run(h).bold = True
        for r in rows:
            cells = t.add_row().cells
            for c, v in zip(cells, r):
                c.text = v

    d.add_heading(m.title, level=0)
    if m.audience:
        p = d.add_paragraph()
        p.add_run("Audience: ").bold = True
        p.add_run(m.audience)
    if m.generated:
        d.add_paragraph().add_run(f"Generated {m.generated}").italic = True
    blocks(m.summary)
    section = None
    for s in m.steps:
        if s.section is not None and s.section != section:
            section = s.section
            d.add_heading(section, level=1)
        d.add_heading(f"{s.n}. {s.heading}", level=2)
        blocks(s.body)
        if s.image:
            d.add_picture(str(s.image), width=Inches(6.3))
            d.add_paragraph(s.caption, style="Caption")
            if s.legend:
                d.add_paragraph("Highlighted: " + " · ".join(s.legend))
        if s.commands:
            code("\n".join(c["text"] for c in s.commands))
        if s.excerpt:
            d.add_paragraph().add_run("Output excerpt:").italic = True
            code(s.excerpt)
    if m.appendix:
        d.add_heading("Appendix", level=1)
        for head, hdr, rows in m.appendix:
            d.add_heading(head, level=2 if not m.sections else 2)
            if rows:
                table(hdr, rows)
            else:
                d.add_paragraph("None.")
    cp = d.core_properties
    fixed = _gen_dt(m.generated)
    cp.author = "casefile"
    cp.last_modified_by = "casefile"
    cp.title = m.title
    cp.created = fixed
    cp.modified = fixed
    cp.last_printed = fixed
    cp.revision = 1
    p = out / f"{m.slug}.docx"
    d.save(str(p))
    return p


def _gen_dt(generated: str | None) -> datetime:
    if generated:
        try:
            return datetime.fromisoformat(generated).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ PDF
_PDF_FONTS = False


def _pdf_fonts() -> None:
    global _PDF_FONTS
    if _PDF_FONTS:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("CF-Sans", str(config.FONTS_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("CF-Bold", str(config.FONTS_DIR / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("CF-Mono", str(config.FONTS_DIR / "DejaVuSansMono-Bold.ttf")))
    pdfmetrics.registerFontFamily("CF-Sans", normal="CF-Sans", bold="CF-Bold", italic="CF-Sans", boldItalic="CF-Bold")
    _PDF_FONTS = True


def _x(s: str) -> str:
    return html.escape(s, quote=False)


def render_pdf(m: DocModel, out: Path, accent, ink) -> tuple[Path, int]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import (
        KeepTogether,
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    _pdf_fonts()
    A = colors.Color(*(c / 255 for c in accent))
    K = colors.Color(*(c / 255 for c in ink))
    acc_hex = "#%02x%02x%02x" % tuple(accent)
    body = ParagraphStyle("b", fontName="CF-Sans", fontSize=10, leading=14, textColor=K, spaceAfter=6)
    h0 = ParagraphStyle("h0", parent=body, fontName="CF-Bold", fontSize=22, leading=27, spaceAfter=10)
    h1 = ParagraphStyle("h1", parent=body, fontName="CF-Bold", fontSize=15, leading=19, spaceBefore=10,
                        spaceAfter=6, textColor=A)
    h2 = ParagraphStyle("h2", parent=body, fontName="CF-Bold", fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4)
    cap = ParagraphStyle("cap", parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor("#4B5563"),
                         alignment=TA_CENTER)
    mono = ParagraphStyle("mono", parent=body, fontName="CF-Mono", fontSize=7.5, leading=9.5,
                          backColor=colors.HexColor("#F3F4F6"), borderPadding=4, spaceBefore=4, spaceAfter=8)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10, spaceAfter=0)
    cellm = ParagraphStyle("cellm", parent=cell, fontName="CF-Mono", fontSize=7)

    left = right = 18 * mm
    frame_w = A4[0] - left - right
    frame_h = A4[1] - 40 * mm
    wrap = int(frame_w / (7.5 * 0.6)) - 2

    def runs(rs: Runs) -> str:
        out_ = []
        for sty, t in rs:
            t = _x(t)
            out_.append(f"<b>{t}</b>" if sty == "b" else f'<font name="CF-Mono" color="{acc_hex}">{t}</font>'
                        if sty == "code" else t)
        return "".join(out_)

    def code(text: str):
        lines = []
        for ln in text.split("\n"):
            lines += textwrap.wrap(ln, wrap, drop_whitespace=False, replace_whitespace=False) or [""]
        return Preformatted("\n".join(lines), mono)

    def blocks(bs) -> list:
        fl: list = []
        for kind, v in bs:
            if kind == "p":
                fl.append(Paragraph(runs(v), body))
            elif kind in ("ul", "ol"):
                fl.append(ListFlowable([ListItem(Paragraph(runs(r), body)) for r in v],
                                       bulletType="bullet" if kind == "ul" else "1", start=None if kind == "ol" else "•",
                                       bulletFontName="CF-Sans", bulletFontSize=9, leftIndent=14))
            elif kind == "code":
                fl.append(code(v))
        return fl

    story: list = [Paragraph(_x(m.title), h0)]
    if m.audience:
        story.append(Paragraph(f"<b>Audience:</b> {_x(m.audience)}", body))
    if m.generated:
        story.append(Paragraph(f"<i>Generated {_x(m.generated)}</i>", body))
    story += blocks(m.summary)
    section = None
    for s in m.steps:
        if s.section is not None and s.section != section:
            section = s.section
            story.append(Paragraph(_x(section), h1))
        head = Paragraph(f"{s.n}. {_x(s.heading)}", h2 if m.sections else h1)
        fl = blocks(s.body)
        story.append(KeepTogether([head] + fl[:1]))
        story += fl[1:]
        if s.image and s.image_size:
            w, h = s.image_size
            iw, ih = frame_w, frame_w * h / w
            if ih > frame_h * 0.6:
                ih = frame_h * 0.6
                iw = ih * w / h
            parts = [RLImage(str(s.image), width=iw, height=ih), Spacer(1, 3), Paragraph(_x(s.caption), cap)]
            if s.legend:
                parts.append(Paragraph("Highlighted: " + _x(" · ".join(s.legend)), cap))
            story.append(KeepTogether(parts + [Spacer(1, 6)]))
        if s.commands:
            story.append(code("\n".join(c["text"] for c in s.commands)))
        if s.excerpt:
            story.append(Paragraph("<i>Output excerpt:</i>", body))
            story.append(code(s.excerpt))
    if m.appendix:
        story.append(Paragraph("Appendix", h1))
        for head, hdr, rows in m.appendix:
            story.append(Paragraph(_x(head), h2))
            if not rows:
                story.append(Paragraph("None.", body))
                continue
            data = [[Paragraph(f"<b>{_x(h)}</b>", cell) for h in hdr]]
            for r in rows:
                data.append([Paragraph(_x(v), cellm if head == "Commands" and i == 2 else cell)
                             for i, v in enumerate(r)])
            widths = [frame_w * 0.12, frame_w * 0.1, frame_w * 0.78] if head == "Commands" else \
                [frame_w * 0.12, frame_w * 0.4, frame_w * 0.48]
            t = Table(data, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

    pages = [0]

    def on_page(canv, doc_):
        pages[0] = max(pages[0], canv.getPageNumber())
        canv.saveState()
        canv.setFont("CF-Sans", 8)
        canv.setFillColor(colors.HexColor("#6B7280"))
        canv.drawRightString(A4[0] - right, 12 * mm, f"Page {canv.getPageNumber()}")
        canv.drawString(left, 12 * mm, m.title[:90])
        canv.setStrokeColor(A)
        canv.setLineWidth(1.2)
        canv.line(left, A4[1] - 14 * mm, A4[0] - right, A4[1] - 14 * mm)
        canv.restoreState()

    p = out / f"{m.slug}.pdf"
    doc = SimpleDocTemplate(str(p), pagesize=A4, leftMargin=left, rightMargin=right, topMargin=20 * mm,
                            bottomMargin=20 * mm, title=m.title, author="casefile", creator="casefile",
                            producer="casefile", invariant=1)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return p, pages[0]


# ------------------------------------------------------------------ Confluence storage format
def _cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _c_runs(rs: Runs) -> str:
    out = []
    for sty, t in rs:
        t = html.escape(t, quote=False)
        out.append(f"<strong>{t}</strong>" if sty == "b" else f"<code>{t}</code>" if sty == "code" else t)
    return "".join(out)


def _c_code(text: str, lang: str) -> str:
    return (f'<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">{lang}</ac:parameter>'
            f"<ac:plain-text-body>{_cdata(text)}</ac:plain-text-body></ac:structured-macro>")


def _c_blocks(bs) -> list[str]:
    out = []
    for kind, v in bs:
        if kind == "p":
            out.append(f"<p>{_c_runs(v)}</p>")
        elif kind in ("ul", "ol"):
            out.append(f"<{kind}>" + "".join(f"<li>{_c_runs(r)}</li>" for r in v) + f"</{kind}>")
        elif kind == "code":
            out.append(_c_code(v, "text"))
    return out


def render_confluence(m: DocModel, out: Path) -> tuple[Path, list[Path]]:
    e = lambda s: html.escape(s, quote=True)
    X: list[str] = []
    atts: list[Path] = []
    if m.audience:
        X.append(f"<p><strong>Audience:</strong> {e(m.audience)}</p>")
    if m.generated:
        X.append(f"<p><em>Generated {e(m.generated)}</em></p>")
    X += _c_blocks(m.summary)
    section = None
    hs = "h3" if m.sections else "h2"
    for s in m.steps:
        if s.section is not None and s.section != section:
            section = s.section
            X.append(f"<h2>{e(section)}</h2>")
        X.append(f"<{hs}>{s.n}. {e(s.heading)}</{hs}>")
        X += _c_blocks(s.body)
        if s.image:
            atts.append(s.image)
            w = min(800, s.image_size[0]) if s.image_size else 800
            X.append(f'<p><ac:image ac:width="{w}" ac:alt="{e(s.caption)}">'
                     f'<ri:attachment ri:filename="{e(s.image.name)}" /></ac:image></p>')
            X.append(f"<p><em>{e(s.caption)}</em></p>")
            if s.legend:
                X.append(f"<p>Highlighted: {e(' · '.join(s.legend))}</p>")
        if s.commands:
            X.append(_c_code("\n".join(c["text"] for c in s.commands), "bash"))
        if s.excerpt:
            X.append("<p><em>Output excerpt:</em></p>")
            X.append(_c_code(s.excerpt, "text"))
    if m.appendix:
        X.append("<h2>Appendix</h2>")
        for head, hdr, rows in m.appendix:
            X.append(f"<h3>{e(head)}</h3>")
            if not rows:
                X.append("<p>None.</p>")
                continue
            t = "<table><tbody><tr>" + "".join(f"<th>{e(h)}</th>" for h in hdr) + "</tr>"
            for r in rows:
                t += "<tr>" + "".join(f"<td>{'<code>' + e(v) + '</code>' if head == 'Commands' and i == 2 else e(v)}</td>"
                                      for i, v in enumerate(r)) + "</tr>"
            X.append(t + "</tbody></table>")
    p = out / f"{m.slug}.confluence.xhtml"
    p.write_bytes(("\n".join(X) + "\n").encode("utf-8"))
    manifest = {"title": m.title, "body": p.name, "representation": "storage",
                "attachments": [f"images/{a.name}" for a in atts]}
    (out / f"{m.slug}.attachments.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return p, atts


# ------------------------------------------------------------------ entry point
def make_doc(tl: dict | str | Path, doc: dict, formats: list[str] | tuple[str, ...] | None = None,
             out_dir: str | Path | None = None, brand: dict | None = None, *, privacy: bool = True,
             generated: str | None = None, base_dir: str | Path | None = None) -> dict:
    """Build written docs from a timeline. Returns {paths:{fmt:path}, pages, steps, warnings, attachments, dir}.

    tl: timeline dict or path (screenshots resolve relative to its folder, or `base_dir` / tl['_dir']).
    doc: {title, summary, audience?, sections?:[{heading, steps:[...]}] | steps:[{step, heading, body,
         highlight:[item ids | {item, label}], crop:'auto'|[x0,y0,x1,y1]|null, command?:bool}], appendix?}.
    """
    if not isinstance(doc, dict):
        raise ToolError("doc must be an object", "pass {title, summary, steps:[{step, heading, body, highlight}]}")
    fmts = list(formats) if formats else list(FORMATS)
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        raise ToolError(f"unknown format(s): {', '.join(bad)}", f"use any of {', '.join(FORMATS)}")
    data, base = load_timeline(tl, base_dir)
    brand = brand or {}
    accent = hex_rgb(brand.get("accent"), config.ACCENT)
    ink = hex_rgb(brand.get("ink"), config.INK)
    root = Path(out_dir) if out_dir else config.home_dir() / "docs"
    warnings: list[str] = []
    slug = slugify(doc.get("slug") or doc.get("title") or "doc")
    out = root / slug
    out.mkdir(parents=True, exist_ok=True)
    m = _build(data, base, doc, out, accent, privacy, generated, warnings)
    paths: dict[str, str] = {}
    pages = None
    atts: list[Path] = []
    if "md" in fmts:
        paths["md"] = str(render_md(m, out))
    if "docx" in fmts:
        paths["docx"] = str(render_docx(m, out, accent))
    if "pdf" in fmts:
        p, pages = render_pdf(m, out, accent, ink)
        paths["pdf"] = str(p)
    if "confluence" in fmts:
        p, atts = render_confluence(m, out)
        paths["confluence"] = str(p)
    return {"paths": paths, "dir": str(out), "pages": pages, "steps": len(m.steps), "warnings": warnings,
            "images": [str(s.image) for s in m.steps if s.image], "attachments": [str(a) for a in atts]}


def copy_tree(res: dict, dest: str | Path) -> Path:
    """Copy a make_doc output folder somewhere else (e.g. for review). Returns the new folder."""
    src = Path(res["dir"])
    dst = Path(dest) / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst
