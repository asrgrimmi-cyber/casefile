"""OCR backend selection + RapidOCR tiling helpers (no GPU needed)."""
from __future__ import annotations

import importlib.util

import pytest
from PIL import Image

from casefile import ocr_backend, ocr_gpu


def test_falls_back_to_tesseract_when_rapidocr_missing(monkeypatch):
    monkeypatch.delenv("CASEFILE_OCR", raising=False)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name.startswith("rapidocr") else real(name, *a))
    assert not ocr_gpu.available()
    assert ocr_backend.backend_name("auto") == "tesseract"
    assert ocr_backend.backend_name("rapidocr") == "tesseract"  # explicit request degrades gracefully


def test_cpu_only_rapidocr_not_auto(monkeypatch):
    monkeypatch.delenv("CASEFILE_OCR", raising=False)
    monkeypatch.setattr(ocr_gpu, "available", lambda: True)
    monkeypatch.setattr(ocr_gpu, "cuda", lambda: False)
    assert ocr_backend.backend_name() == "tesseract"
    monkeypatch.setattr(ocr_gpu, "cuda", lambda: True)
    assert ocr_backend.backend_name() == "rapidocr"
    monkeypatch.setenv("CASEFILE_OCR", "tesseract")
    assert ocr_backend.backend_name() == "tesseract"


def test_bad_backend():
    with pytest.raises(Exception):
        ocr_backend.backend_name("easyocr")


def test_rapidocr_path_uses_engine(monkeypatch):
    calls = []

    def fake_lines(img, reg):
        calls.append(reg)
        return [{"text": "PUSCH SNR 23.4", "box": [100, 10, 240, 30], "conf": 97}]

    monkeypatch.setattr(ocr_backend, "backend_name", lambda b="auto": "rapidocr")
    monkeypatch.setattr(ocr_gpu, "ocr_lines", fake_lines)
    img = Image.new("RGB", (400, 100), "white")
    lines = ocr_backend.ocr(img, level="line")
    assert lines[0]["text"] == "PUSCH SNR 23.4"
    words = ocr_backend.ocr(img, [0, 0, 300, 50])
    assert [w["text"] for w in words] == ["PUSCH", "SNR", "23.4"]
    assert calls[-1] == (0, 0, 300, 50)
    assert words[0]["box"][0] == 100 and words[-1]["box"][2] == 240


def test_tiles_cover_frame():
    tiles = ocr_gpu._tiles(2560, 1440, 1280, 96)
    assert min(t[0] for t in tiles) == 0 and max(t[2] for t in tiles) == 2560
    assert min(t[1] for t in tiles) == 0 and max(t[3] for t in tiles) == 1440
    assert all(t[2] - t[0] == 1280 and t[3] - t[1] <= 1280 for t in tiles)
    assert ocr_gpu._tiles(800, 600) == [(0, 0, 800, 600)]


def test_dedupe_overlap():
    a = {"text": "Downlink bitrate", "box": [1200, 50, 1360, 70], "conf": 95}
    b = {"text": "Downlink", "box": [1200, 50, 1280, 70], "conf": 90}
    c = {"text": "Other", "box": [10, 10, 60, 30], "conf": 99}
    out = ocr_gpu._dedupe([b, a, c])
    assert [d["text"] for d in out] == ["Other", "Downlink bitrate"]


@pytest.mark.slow
def test_tesseract_backend_reads_text():
    from PIL import ImageDraw

    from casefile import config
    if not config.tool_path("tesseract"):
        pytest.skip("tesseract not installed")
    from casefile.fonts import font
    img = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(img).text((20, 30), "Hello Casefile", fill="black", font=font("r", 40))
    words = ocr_backend.ocr(img, backend="tesseract")
    assert any("Casefile" in w["text"] for w in words)
