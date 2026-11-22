"""``/api/v1/files`` and ``/api/v1/archive.zip``."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..errors import IntegrityError, SoonyAPIError, SoonyError, TransportError
from .base import Resource, register_resource

log = logging.getLogger("soony_api")

CHUNK = 1 << 16

# mkstemp creates 0600 files; downloads should get normal permissions (0666 & ~umask).
# Read the umask once at import: os.umask() is process-global and not thread-safe.
_UMASK = os.umask(0)
os.umask(_UMASK)
FILE_MODE = 0o666 & ~_UMASK


@dataclass(frozen=True)
class DataFile:
    path: str
    bytes: int
    sha256: str
    url: str

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "DataFile":
        return cls(path=d["path"], bytes=int(d["bytes"]), sha256=str(d["sha256"]).lower(), url=d["url"])

    @property
    def supplier_id(self) -> str | None:
        """Extract the supplier ID from the portal folder name."""
        parts = PurePosixPath(self.path).parts
        if len(parts) >= 2 and parts[0] == "suppliers":
            return parts[1].split("_", 1)[0]
        return None

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_local_path(root: Path, remote_path: str) -> Path:
    """Map a remote path to a file under ``root``; reject traversal / absolute paths."""
    rel = PurePosixPath(remote_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise SoonyError(f"Refusing unsafe remote path: {remote_path!r}")
    target = (root / Path(*rel.parts)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise SoonyError(f"Refusing path outside cache dir: {remote_path!r}")
    return target


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@register_resource("files")
class FilesResource(Resource):
    def list(self, prefix: str | None = None, suffix: str | None = None) -> list[DataFile]:
        """All files currently on the portal (optionally filtered client-side)."""
        body = self._get("files")
        if not isinstance(body, dict) or not isinstance(body.get("files"), list):
            raise SoonyAPIError("Invalid portal file listing: expected a files array")
        entries = body["files"]
        if "count" in body and (type(body["count"]) is not int or body["count"] != len(entries)):
            raise SoonyAPIError("Invalid portal file listing: count does not match files array")

        files = []
        paths = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise SoonyAPIError("Invalid portal file listing: expected a file object")
            path, size, digest, url = (entry.get(key) for key in ("path", "bytes", "sha256", "url"))
            if (not isinstance(path, str) or not path or path in paths
                    or type(size) is not int or size < 0
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                    or not isinstance(url, str) or not url):
                raise SoonyAPIError("Invalid portal file listing: invalid metadata or duplicate path")
            files.append(DataFile.from_api(entry))
            paths.add(path)
        if prefix:
            files = [f for f in files if f.path.startswith(prefix)]
        if suffix:
            files = [f for f in files if f.path.lower().endswith(suffix.lower())]
        return files

    def get(self, path: str) -> bytes:
        """Download one file fully into memory."""
        with self._stream(f"files/{quote(path, safe='/')}") as resp:
            return resp.read()

    def get_text(self, path: str, encoding: str | None = None) -> str:
        """Download and decode. Tries the given encoding, then utf-8-sig, then cp1252 / latin-1
        (the portal warns encodings differ between files)."""
        data = self.get(path)
        for enc in ([encoding] if encoding else []) + ["utf-8-sig", "cp1252", "latin-1"]:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        raise AssertionError("unreachable: latin-1 decodes everything")

    def download(
        self,
        path: str,
        dest: str | os.PathLike[str],
        *,
        expected_sha256: str | None = None,
        attempts: int = 3,
        on_progress: Callable[[int], None] | None = None,
    ) -> Path:
        """Stream one file to ``dest`` atomically; verify SHA-256 when given.

        Retries the whole transfer on a broken stream or hash mismatch.
        """
        return self._download_to(f"files/{quote(path, safe='/')}", Path(dest),
                                 expected_sha256=expected_sha256, attempts=attempts,
                                 on_progress=on_progress)

    def archive(self, dest: str | os.PathLike[str] = "soony-data.zip") -> Path:
        """Download everything as one zip (entries are under ``soony-data/``)."""
        return self._download_to("archive.zip", Path(dest))

    def sync(self, dest: str | os.PathLike[str] | None = None, **kw: Any):
        """Mirror the portal into ``dest``, downloading only new/changed files.
        See ``soony_api.sync.sync_files`` for options."""
        from ..sync import sync_files

        return sync_files(self, Path(dest or self._client.settings.cache_dir), **kw)

    # ----------------------------------------------------------------------
    def _download_to(
        self,
        api_path: str,
        dest: Path,
        *,
        expected_sha256: str | None = None,
        attempts: int = 3,
        on_progress: Callable[[int], None] | None = None,
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
            tmp = Path(tmp_name)
            try:
                h = hashlib.sha256()
                written = 0
                with os.fdopen(fd, "wb") as out, self._stream(api_path) as resp:
                    for chunk in resp.iter_bytes(CHUNK):
                        out.write(chunk)
                        h.update(chunk)
                        written += len(chunk)
                        if on_progress:
                            on_progress(written)
                    out.flush()
                    os.fsync(out.fileno())
                digest = h.hexdigest()
                if expected_sha256 and digest != expected_sha256.lower():
                    raise IntegrityError(
                        f"SHA-256 mismatch for {api_path}: expected {expected_sha256[:12]}…, got {digest[:12]}…"
                    )
                os.chmod(tmp, FILE_MODE)
                os.replace(tmp, dest)
                return dest
            except (IntegrityError, TransportError, httpx.TransportError) as e:
                # httpx.TransportError = stream broke mid-body (client.stream only retries the connect)
                last_err = e if isinstance(e, SoonyError) else TransportError(str(e), method="GET", url=api_path)
                log.warning("download %s attempt %d/%d failed: %s", api_path, attempt, attempts, e)
            finally:
                tmp.unlink(missing_ok=True)
        assert last_err is not None
        raise last_err
