"""Tool errors carry a human fix-hint; the API layer turns them into {error, hint}."""
from __future__ import annotations

from pathlib import Path

from . import config


class ToolError(Exception):
    def __init__(self, error: str, hint: str = ""):
        super().__init__(error)
        self.error = error
        self.hint = hint

    def to_dict(self) -> dict:
        return {"error": self.error, "hint": self.hint}


def check_path(p: str | Path, *, must_exist: bool = True, write: bool = False) -> Path:
    """Resolve a path and enforce the CASEFILE_WORKDIR sandbox when set.

    Relative paths resolve against the workdir (or CWD when no workdir)."""
    wd = config.workdir()
    path = Path(p)
    if not path.is_absolute():
        path = (wd or Path.cwd()) / path
    path = path.resolve()
    if wd is not None and not path.is_relative_to(wd):
        raise ToolError(f"path outside CASEFILE_WORKDIR: {path}", f"put files under {wd}")
    if must_exist and not path.exists():
        raise ToolError(f"file not found: {path}", "check the path; relative paths resolve against the workdir")
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path
