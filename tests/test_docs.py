"""docs.make_doc: md/docx/pdf/confluence from the hand-written sample timeline (tests/fixtures/timeline_sample)."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "timeline_sample"))
import make_sample  # noqa: E402

from casefile import config, docs  # noqa: E402
from casefile.errors import ToolError  # noqa: E402

DOC = {
    "title": "Checking an NTN UE attach",
    "summary": "Confirm a UE attached over **NTN** and read its radio stats with `ws.js`.",
    "audience": "Test engineers",
    "sections": [
        {"heading": "Web GUI", "steps": [
            {"step": "s1", "heading": "Open the NTN tab",
             "body": "Click **NTN**, then check:\n- band is `n256`\n- one UE is listed",
             "highlight": ["s1.ntn", {"item": "s1.n256", "label": "Band"}], "crop": "auto"}]},
        {"heading": "Terminal", "steps": [
            {"step": "s2", "heading": "Query the UE", "body": "Run `ue_get` and read the SNR.",
             "highlight": ["s2.pusch_snr", "s2.dl_bitrate"], "crop": None}]},
    ],
    "appendix": "both",
}


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    return make_sample.build(tmp_path_factory.mktemp("tl"))


@pytest.fixture(scope="module")
def result(sample, tmp_path_factory):
    return docs.make_doc(sample, DOC, out_dir=tmp_path_factory.mktemp("docs"))


def _accent_pixels(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB")).astype(int)
    acc = np.array(config.ACCENT)
    return int((np.abs(a - acc).sum(axis=2) < 40).sum())


def test_all_formats_written(result):
    assert set(result["paths"]) == {"md", "docx", "pdf", "confluence"}
    for p in result["paths"].values():
        assert Path(p).exists() and Path(p).stat().st_size > 200
    assert result["steps"] == 2 and result["warnings"] == []
    assert len(result["images"]) == 2 and result["pages"] >= 1


def test_markdown_structure(result):
    md = Path(result["paths"]["md"]).read_text(encoding="utf-8")
    assert md.startswith("# Checking an NTN UE attach\n")
    assert "## Web GUI" in md and "### 1. Open the NTN tab" in md and "### 2. Query the UE" in md
    assert "![Figure 1" in md and "(images/step-01.png)" in md
    assert "- band is `n256`" in md
    assert "```bash\n./ws.js 127.0.0.1:9001" in md                 # terminal command as a code block
    assert '"pusch_snr": 23.4' in md and "line 03" not in md       # excerpt holds only highlighted lines
    assert "| 00:41.6 | s2 |" in md                                # commands appendix
    assert "| s1 | RNTI | 0x461e |" in md                          # fields appendix
    assert "callbox" not in md                                     # privacy: user@host redacted in text


def test_docx_headings_and_images(result):
    from docx import Document
    d = Document(result["paths"]["docx"])
    heads = [p.text for p in d.paragraphs if p.style.name == "Heading 2"]
    steps = [h for h in heads if h[:2] in ("1.", "2.")]
    assert steps == ["1. Open the NTN tab", "2. Query the UE"]
    assert len(d.inline_shapes) == 2
    assert abs(d.inline_shapes[0].width.inches - 6.3) < 0.01
    assert any(p.style.name == "Casefile Code" for p in d.paragraphs)
    assert len(d.tables) == 2


def test_pdf_pages_and_text(result):
    from pypdf import PdfReader
    r = PdfReader(result["paths"]["pdf"])
    assert len(r.pages) >= 1 and len(r.pages) == result["pages"]
    text = "".join(pg.extract_text() for pg in r.pages)
    assert "Open the NTN tab" in text and "Page 1" in text
    assert sum(len(pg.images) for pg in r.pages) == 2


def test_confluence_well_formed(result):
    p = Path(result["paths"]["confluence"])
    body = p.read_text(encoding="utf-8")
    root = ET.fromstring('<root xmlns:ac="http://atlassian.com/content" '
                         'xmlns:ri="http://atlassian.com/resource/identifier">' + body + "</root>")
    atts = [e.get("{http://atlassian.com/resource/identifier}filename")
            for e in root.iter("{http://atlassian.com/resource/identifier}attachment")]
    assert atts == ["step-01.png", "step-02.png"]
    assert [Path(a).name for a in result["attachments"]] == atts
    manifest = json.loads((p.parent / "checking-an-ntn-ue-attach.attachments.json").read_text())
    assert manifest["attachments"] == ["images/step-01.png", "images/step-02.png"]
    assert "ac:structured-macro" in body


def test_highlight_drawn_in_crop(sample, result):
    im = Image.open(result["images"][0])
    assert 800 <= im.width < 1280 and im.height < 720              # auto crop around the highlights
    assert _accent_pixels(im) > 200
    # same step without highlights: full frame and no accent
    r2 = docs.make_doc(sample, {"title": "plain", "summary": "",
                                "steps": [{"step": "s1", "heading": "x", "highlight": []}]},
                       formats=["md"], out_dir=Path(result["dir"]).parent / "plain")
    im2 = Image.open(r2["images"][0])
    assert im2.size == (1280, 720) and _accent_pixels(im2) == 0


def test_private_box_blurred(sample, tmp_path):
    doc = {"title": "priv", "summary": "", "steps": [{"step": "s1", "heading": "x", "crop": None}]}
    on = Image.open(docs.make_doc(sample, doc, formats=["md"], out_dir=tmp_path / "on")["images"][0])
    off = Image.open(docs.make_doc(sample, doc, formats=["md"], out_dir=tmp_path / "off",
                                   privacy=False)["images"][0])
    box = (880, 660, 1250, 700)                                    # the email box of s1
    v_on = np.asarray(on.crop(box).convert("L"), dtype=float).std()
    v_off = np.asarray(off.crop(box).convert("L"), dtype=float).std()
    assert v_off > 30 and v_on < v_off * 0.5
    # outside the private box pixels are untouched
    assert np.array_equal(np.asarray(on.crop((40, 80, 400, 340))), np.asarray(off.crop((40, 80, 400, 340))))


def test_unknown_item_warns_not_crash(sample, tmp_path):
    doc = {"title": "warn", "summary": "", "steps": [
        {"step": "s1", "heading": "a", "highlight": ["s1.nope", "s2.cqi"]},
        {"step": "s9", "heading": "missing step"}]}
    r = docs.make_doc(sample, doc, formats=["md"], out_dir=tmp_path)
    w = " ".join(r["warnings"])
    assert "s1.nope" in w and "belongs to step s2" in w and "s9" in w
    assert r["steps"] == 2 and len(r["images"]) == 1


def test_deterministic(sample, tmp_path):
    a = docs.make_doc(sample, DOC, formats=["md", "confluence", "docx", "pdf"], out_dir=tmp_path / "a")
    b = docs.make_doc(sample, DOC, formats=["md", "confluence", "docx", "pdf"], out_dir=tmp_path / "b")
    for f in ("md", "confluence", "pdf"):
        assert Path(a["paths"][f]).read_bytes() == Path(b["paths"][f]).read_bytes(), f
    for ia, ib in zip(a["images"], b["images"]):
        assert Path(ia).read_bytes() == Path(ib).read_bytes()


def test_manual_crop_downscale_and_timeline_dict(sample, tmp_path):
    tl = json.loads(Path(sample).read_text(encoding="utf-8"))
    big = Image.open(Path(sample).parent / "steps/s1.png").resize((2560, 1440))
    big.save(tmp_path / "big.png")
    tl["steps"][0]["screenshot"] = "big.png"
    for it in tl["steps"][0]["items"]:
        it["box"] = [v * 2 for v in it["box"]]
    tl["steps"][0]["private"] = []
    r = docs.make_doc(tl, {"title": "big", "summary": "", "steps": [{"step": "s1", "heading": "x", "crop": None}]},
                      formats=["md"], out_dir=tmp_path / "o", base_dir=tmp_path)
    assert Image.open(r["images"][0]).width == 1600
    r = docs.make_doc(tl, {"title": "c", "summary": "", "steps": [{"step": "s1", "heading": "x",
                                                                   "crop": [0, 0, 1000, 500]}]},
                      formats=["md"], out_dir=tmp_path / "c", base_dir=tmp_path)
    assert Image.open(r["images"][0]).size == (1000, 500)


def test_errors(sample, tmp_path):
    with pytest.raises(ToolError):
        docs.make_doc(sample, {"summary": "no title"}, out_dir=tmp_path)
    with pytest.raises(ToolError):
        docs.make_doc(sample, DOC, formats=["html"], out_dir=tmp_path)
    with pytest.raises(ToolError):
        docs.make_doc(tmp_path / "missing.json", DOC, out_dir=tmp_path)


def test_parse_body():
    b = docs.parse_body("Hello **x** and `y`\n\n- a\n- b\n\n1. one\n2. two\n```\ncode here\n```")
    assert [k for k, _ in b] == ["p", "ul", "ol", "code"]
    assert b[0][1] == [("", "Hello "), ("b", "x"), ("", " and "), ("code", "y")]
    assert docs.redact_text("ssh bob@host1 10.0.0.5 127.0.0.1 001010123456789") == \
        "ssh [user@host] [ip] 127.0.0.1 [imsi]"
