"""Edit-spec pydantic models (the single JSON file Claude writes).

Times inside a shot are LOCAL to the shot. A TimeRef is a number or a string:
  "s:<line>+x"  start of line + x      "e:<line>-x"  end of line - x      "end-0.5"  shot end - 0.5
Rects are [x0, y0, x1, y1] in SOURCE pixels. Camera rects are [x, y, w, h] in source pixels (aspect is
locked to the content area by the renderer: it expands w or h around the centre).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

TimeRef = Union[float, str]
Rect = list[float]  # [x0, y0, x1, y1]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(_M):
    path: str = "out/edit.mp4"
    size: list[int] = [1920, 1080]
    fps: int = 30


class Brand(_M):
    logo: str | None = None          # path to a PNG logo; None -> small "Casefile" text wordmark
    logo_dark: str | None = None
    accent: str = "#4F46E5"
    ink: str = "#111827"
    font: str = "Poppins"


class Voice(_M):
    engine: Literal["kokoro", "none"] = "kokoro"
    voice: str = "af_heart"
    speed: float = 1.1


class Line(_M):
    caption: str
    say: str | None = None           # None -> caption text (after lexicon) is spoken


class Find(_M):
    t: float                         # SOURCE time (s) of the frame to OCR
    query: str
    row: bool = False
    region: Rect | None = None
    index: int = 0                   # which match if several


class _Ov(_M):
    from_: TimeRef = Field(0.0, alias="from")
    to: TimeRef = "end"
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BoxOv(_Ov):
    type: Literal["box"]
    rect: Rect | None = None
    find: Find | None = None
    label: str | None = None
    label_side: Literal["auto", "above", "below", "left", "right"] = "auto"
    pad: float = 8
    spotlight: bool = True


class TitleOv(_Ov):
    type: Literal["title"]
    text: str


class BadgeOv(_Ov):
    type: Literal["badge"]
    text: str
    corner: Literal["tr", "tl", "br", "bl"] = "tr"


class Tile(_M):
    key: str
    value: str
    sub: str = ""
    at: TimeRef | None = None        # appear time (default: overlay from)


class TilesOv(_Ov):
    type: Literal["tiles"]
    items: list[Tile]
    y: float | None = None           # output px of tile row top (default 560 at 1080p)


class PhasesOv(_Ov):
    type: Literal["phases"]
    plot: Rect                       # plot area in source px; bands use its y-range
    bands: list[list[Any]]           # [[x0, x1, "LABEL"], ...] or [[x0, x1, "LABEL", "#rrggbb"], ...]
    stagger: float = 0.9


class Dot(_M):
    xy: list[float]
    label: str = ""
    side: Literal["right", "left", "above", "below"] = "right"


class DotsOv(_Ov):
    type: Literal["dots"]
    points: list[Any]                # [[[x, y], "label", "side"], ...] or Dot dicts
    color: str = "#3B6EE6"
    stagger: float = 0.7


class VLineOv(_Ov):
    type: Literal["vline"]
    x: float
    y0: float
    y1: float
    label: str | None = None         # banner text drawn centred near the bottom
    color: str | None = None


class ArrowOv(_Ov):
    type: Literal["arrow"]
    start: list[float]               # source px
    end: list[float]
    label: str | None = None
    color: str | None = None


class BlurOv(_Ov):
    type: Literal["blur"]
    rect: Rect | None = None
    find: Find | None = None
    radius: float = 12


Overlay = Annotated[Union[BoxOv, TitleOv, BadgeOv, TilesOv, PhasesOv, DotsOv, VLineOv, ArrowOv, BlurOv],
                    Field(discriminator="type")]


class Shot(_M):
    card: Literal["intro", "outro"] | None = None
    title: list[str] | str | None = None       # card title lines (intro: 2 lines, 2nd in accent colour)
    subtitle: str | list[str] | None = None
    footer: str | None = None
    bullets: list[str] | None = None           # outro checklist
    src: list[list[float]] = []                # [[t0, t1, speed], ...] SOURCE seconds
    hold: float | None = None                  # SOURCE time to freeze on after src segments end
    lines: list[Union[str, list[Any]]] = []    # "id" or ["id", at_local_seconds]
    chapter: str | None = None                 # inherits previous shot's chapter when None
    xfade: float = 0.0
    lead: float = 0.5
    tail: float = 0.6
    gap: float = 0.35                          # silence between consecutive lines
    camera: list[list[Any]] = []               # [[TimeRef, "full" | [x, y, w, h]], ...]
    overlays: list[Overlay] = []

    @field_validator("src")
    @classmethod
    def _src(cls, v):
        for seg in v:
            if len(seg) not in (2, 3):
                raise ValueError("src segments are [t0, t1] or [t0, t1, speed]")
            if seg[1] <= seg[0]:
                raise ValueError(f"src segment {seg}: t1 must be > t0")
            if len(seg) == 3 and seg[2] <= 0:
                raise ValueError(f"src segment {seg}: speed must be > 0")
        return v


class Spec(_M):
    source: str | None = None
    output: Output = Output()
    brand: Brand = Brand()
    voice: Voice = Voice()
    bad_frames: Literal["auto", "none"] | list[int] = "auto"
    lexicon: dict[str, str] = {}
    lines: dict[str, Line] = {}
    shots: list[Shot]
    fade_in: float = 0.6
    fade_out: float = 0.8
    captions: bool = True
    srt: bool = True


DEFAULT_LEXICON: dict[str, str] = {
    "n256": "n two five six", "UE": "U E", "UEs": "U Es", "TA": "T-A", "ARFCN": "A-R-F-C-N",
    "SIB19": "SIB nineteen", "AS release": "A.S. release", "NR": "N R", "RRC": "R R C", "NAS": "nass",
    "PHY": "fye", "GNSS": "G N S S", "LEO": "leo", "DL": "downlink", "UL": "uplink",
}
