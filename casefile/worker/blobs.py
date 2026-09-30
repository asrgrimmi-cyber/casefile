"""Content-addressed blob store with resumable chunked uploads.

Layout under <CASEFILE_HOME>/blobs/: `<sha256>` (complete, verified), `<sha256>.part` (upload in progress),
`<sha256><ext>` hard link / copy made on demand so tools that care about the suffix see e.g. `.mp4`.

Upload protocol (PUT /blobs/{sha}?offset=N[&total=T][&final=1], raw bytes body):
- offset must equal the current .part size (HEAD /blobs/{sha} answers 404 + `Upload-Offset: <n>` for a partial
  upload, 200 + `Content-Length` for a complete blob) else 409 {offset}.
- the blob is finalised when offset+len(body) == total (or final=1): sha256 of the .part is verified,
  mismatch -> 422 and the .part is deleted, match -> renamed to `<sha>`.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


class BlobError(Exception):
    def __init__(self, status: int, error: str, hint: str = "", **extra):
        super().__init__(error)
        self.status, self.error, self.hint, self.extra = status, error, hint, extra

    def to_dict(self) -> dict:
        return {"error": self.error, "hint": self.hint, **self.extra}


def valid_sha(sha: str) -> str:
    s = (sha or "").lower()
    if not SHA_RE.match(s):
        raise BlobError(400, f"bad sha256 {sha!r}", "use the 64-char lowercase hex sha256 of the file")
    return s


def file_sha256(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        return self.root / valid_sha(sha)

    def part(self, sha: str) -> Path:
        return self.root / f"{valid_sha(sha)}.part"

    def exists(self, sha: str) -> bool:
        return self.path(sha).exists()

    def status(self, sha: str) -> dict:
        """{complete, size} (size = bytes received so far for partial uploads, 0 when unknown)."""
        p, pp = self.path(sha), self.part(sha)
        if p.exists():
            return {"complete": True, "size": p.stat().st_size}
        return {"complete": False, "size": pp.stat().st_size if pp.exists() else 0}

    def open_chunk(self, sha: str, offset: int):
        """Validate offset against the partial upload and return a binary file handle positioned for appending."""
        if self.exists(sha):
            raise BlobError(409, "blob already complete", "HEAD first; skip uploading", offset=self.status(sha)["size"],
                            complete=True)
        pp = self.part(sha)
        cur = pp.stat().st_size if pp.exists() else 0
        if offset != cur:
            raise BlobError(409, f"offset {offset} != received {cur}", "resume from the returned offset", offset=cur)
        return open(pp, "ab")

    def finalize(self, sha: str) -> dict:
        """Verify sha256 of the .part and publish it. Raises BlobError(422) on mismatch (part deleted)."""
        pp = self.part(sha)
        if not pp.exists():
            raise BlobError(404, "no upload in progress", "PUT the data first")
        got = file_sha256(pp)
        if got != sha:
            pp.unlink(missing_ok=True)
            raise BlobError(422, f"sha256 mismatch: got {got}", "upload again from offset 0")
        os.replace(pp, self.path(sha))
        return {"complete": True, "size": self.path(sha).stat().st_size}

    def local_path(self, sha: str, ext: str | None = None) -> Path:
        """Path of a complete blob, optionally as a `<sha><ext>` alias (hard link, copy fallback)."""
        p = self.path(sha)
        if not p.exists():
            raise BlobError(404, f"blob {sha} not uploaded", "PUT /blobs/{sha256} first")
        if not ext:
            return p
        if not EXT_RE.match(ext):
            raise BlobError(400, f"bad extension {ext!r}", "use e.g. .mp4")
        alias = self.root / f"{sha}{ext.lower()}"
        if not alias.exists():
            try:
                os.link(p, alias)
            except OSError:
                shutil.copyfile(p, alias)
        return alias
