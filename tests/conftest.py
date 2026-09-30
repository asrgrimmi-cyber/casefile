import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

OUT = ROOT / "tests" / "fixtures" / "out"


@pytest.fixture(scope="session")
def fixture_video():
    """(Path to fixture.mp4, truth dict). Built once; rebuilt if missing."""
    mp4, tj = OUT / "fixture.mp4", OUT / "truth.json"
    if not (mp4.exists() and tj.exists()):
        import make_fixture
        make_fixture.build(OUT)
    return mp4, json.loads(tj.read_text())
