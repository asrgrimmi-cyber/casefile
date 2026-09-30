import json

import pytest

from casefile.chart import chart_extract
from casefile.errors import ToolError


def _run(fixture_video, series=None, **kw):
    mp4, truth = fixture_video
    c = truth["chart"]
    if series is None:
        series = [{"name": k, "rgb": v["rgb"], "tol": 50} for k, v in c["series"].items()]
    args = dict(plot_region=c["plot"], series=series, x_ticks=c["x_ticks"], y_ticks=c["y_ticks"])
    args.update(kw)
    return chart_extract(mp4, 12.0, **args), c


def test_parabola_min_and_line_zero(fixture_video):
    res, c = _run(fixture_video)
    xr = c["axis"][1] - c["axis"][0]
    yr = c["axis"][3] - c["axis"][2]
    p = res["series"]["parabola"]
    truth_p = c["series"]["parabola"]
    assert abs(p["min"][0] - truth_p["min_x"]) <= 0.01 * xr
    assert abs(p["min"][1] - truth_p["min_y"]) <= 0.01 * yr
    assert p["coverage"] > 0.9
    assert len(p["points"]) == 12
    ln = res["series"]["line"]
    assert len(ln["zero_crossings"]) == 1
    assert abs(ln["zero_crossings"][0] - c["series"]["line"]["zero_x"]) <= 0.01 * xr


def test_output_size(fixture_video):
    res, _ = _run(fixture_video)
    assert len(json.dumps(res)) <= 2400


def test_bad_rgb_hint(fixture_video):
    res, _ = _run(fixture_video, series=[{"name": "ghost", "rgb": [0, 255, 0], "tol": 30}])
    g = res["series"]["ghost"]
    assert g["error"] == "no pixels matched"
    assert "rgb" in g["hint"]


def test_region_outside_frame(fixture_video):
    with pytest.raises(ToolError):
        _run(fixture_video, plot_region=[100, 120, 1400, 800])
