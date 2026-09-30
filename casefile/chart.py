"""Chart curve extraction: colour-mask tracing of plotted series, mapped to axis units."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import ToolError, check_path
from .media import FrameStore


def _sig(v: float, n: int = 3) -> float:
    """Round to n significant digits (plain float)."""
    if v == 0 or not np.isfinite(v):
        return 0.0
    return float(f"{v:.{n}g}")


def _axis_fit(ticks: list, name: str) -> tuple[float, float]:
    """Linear px -> value fit (slope, intercept) from >= 2 [px, val] ticks."""
    if not ticks or len(ticks) < 2:
        raise ToolError(f"{name} needs at least 2 [px, value] pairs", "pass e.g. [[100,0],[1180,100]]")
    px = np.array([float(t[0]) for t in ticks])
    val = np.array([float(t[1]) for t in ticks])
    if np.ptp(px) == 0:
        raise ToolError(f"{name} pixel positions are identical", "use ticks at distinct pixel positions")
    a, b = np.polyfit(px, val, 1)
    return float(a), float(b)


def _smooth(y: np.ndarray, k: int = 5) -> np.ndarray:
    """Light moving-average smoothing with edge handling."""
    if len(y) < k:
        return y
    pad = k // 2
    yp = np.pad(y, pad, mode="edge")
    return np.convolve(yp, np.ones(k) / k, mode="valid")


def _refine_extreme(x: np.ndarray, y: np.ndarray, i: int, span: float) -> tuple[float, float]:
    """Refine an extreme at index i with a local quadratic fit.

    The window is the contiguous run around i whose y stays within 8% of the y range of the extreme
    (flat bottoms make argmin noisy), limited to +/- span x units."""
    band = 0.08 * (np.ptp(y) or 1.0)
    lo = hi = i
    while lo > 0 and abs(y[lo - 1] - y[i]) <= band and x[i] - x[lo - 1] <= span:
        lo -= 1
    while hi < len(y) - 1 and abs(y[hi + 1] - y[i]) <= band and x[hi + 1] - x[i] <= span:
        hi += 1
    sel = np.zeros(len(x), bool)
    sel[lo:hi + 1] = True
    if sel.sum() >= 5 and lo > 0 and hi < len(y) - 1:
        c2, c1, c0 = np.polyfit(x[sel], y[sel], 2)
        if c2 != 0:
            xv = -c1 / (2 * c2)
            if x[sel].min() <= xv <= x[sel].max():
                return float(xv), float(c0 + c1 * xv + c2 * xv * xv)
    return float(x[i]), float(y[i])


def _trace(img: np.ndarray, rgb: list, tol: float) -> tuple[np.ndarray, np.ndarray, int]:
    """Per-column median row of pixels within tol (euclidean RGB) of rgb. Returns (cols, rows, n_cols)."""
    d = np.sqrt(((img.astype(np.float32) - np.array(rgb, np.float32)) ** 2).sum(axis=2))
    mask = d <= tol
    cols, rows = [], []
    for c in range(mask.shape[1]):
        r = np.nonzero(mask[:, c])[0]
        if r.size:
            cols.append(c)
            rows.append(float(np.median(r)))
    return np.array(cols, float), np.array(rows, float), mask.shape[1]


def _series(img: np.ndarray, ox: int, oy: int, spec: dict, ax: tuple, ay: tuple, samples: int) -> dict:
    """Extract one series summary in axis units."""
    rgb = spec.get("rgb")
    if not rgb or len(rgb) != 3:
        return {"error": "rgb must be [r,g,b]", "hint": "pass rgb as three 0-255 ints"}
    tol = float(spec.get("tol", 40))
    cols, rows, ncols = _trace(img, rgb, tol)
    if cols.size < 3:
        return {"error": "no pixels matched", "hint": "check rgb/tol (sample the curve colour; try tol 60)"}
    rows = _smooth(rows)
    x = ax[0] * (cols + ox) + ax[1]
    y = ay[0] * (rows + oy) + ay[1]
    order = np.argsort(x)
    x, y = x[order], y[order]
    span = 0.25 * (x.max() - x.min()) or 1.0
    xs = np.linspace(x.min(), x.max(), max(2, int(samples)))
    ys = np.interp(xs, x, y)
    mn = _refine_extreme(x, y, int(np.argmin(y)), span)
    mx = _refine_extreme(x, y, int(np.argmax(y)), span)
    zc = []
    for i in range(len(y) - 1):
        if y[i] == 0:
            zc.append(x[i])
        elif y[i] * y[i + 1] < 0:
            zc.append(x[i] + (x[i + 1] - x[i]) * (-y[i]) / (y[i + 1] - y[i]))
    # merge crossings from jitter closer than 1% of x range
    merged: list[float] = []
    for z in zc:
        if merged and z - merged[-1] < 0.01 * (x.max() - x.min()):
            merged[-1] = (merged[-1] + z) / 2
        else:
            merged.append(float(z))
    return {
        "points": [[_sig(a), _sig(b)] for a, b in zip(xs, ys)],
        "min": [_sig(mn[0]), _sig(mn[1])],
        "max": [_sig(mx[0]), _sig(mx[1])],
        "zero_crossings": [_sig(z) for z in merged[:8]],
        "coverage": round(cols.size / ncols, 2),
    }


def chart_extract(video: str | Path, t: float, plot_region: list, series: list, x_ticks: list,
                  y_ticks: list, samples: int = 12) -> dict:
    """Trace coloured curves inside plot_region of the frame at t and report them in axis units.

    series: [{name, rgb:[r,g,b], tol=40}]; x_ticks/y_ticks: [[px, value], ...] (>= 2 each, linear fit).
    Returns {series:{name:{points, min, max, zero_crossings, coverage} | {error, hint}}}.
    """
    path = check_path(video)
    img = FrameStore(path).at(float(t))
    w, h = img.size
    if not plot_region or len(plot_region) != 4:
        raise ToolError("plot_region must be [x0,y0,x1,y1]", "give the plot area in source pixels")
    x0, y0, x1, y1 = (int(round(v)) for v in plot_region)
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 - x0 < 4 or y1 - y0 < 4:
        raise ToolError(f"plot_region {plot_region} outside frame {w}x{h}",
                        "use source-pixel coords with 0<=x0<x1<=width, 0<=y0<y1<=height")
    if not series:
        raise ToolError("no series given", "pass series=[{name, rgb:[r,g,b], tol}]")
    ax = _axis_fit(x_ticks, "x_ticks")
    ay = _axis_fit(y_ticks, "y_ticks")
    inset = 2  # skip the plot border
    crop = np.asarray(img.crop((x0 + inset, y0 + inset, x1 - inset, y1 - inset)).convert("RGB"))
    out: dict = {}
    for i, s in enumerate(series):
        name = str(s.get("name") or f"s{i}")
        out[name] = _series(crop, x0 + inset, y0 + inset, s, ax, ay, samples)
    return {"series": out}
