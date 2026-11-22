"""Poll the portal into a private mirror and retain immutable evidence versions."""

import json
import re
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from gtl.contracts import FileChange
from soony_api import SoonyClient
from soony_api.resources.files import safe_local_path, sha256_file
from soony_api.sync import MANIFEST, dir_lock, load_manifest


class PortalFiles:
    def __init__(self, tenant: dict[str, Any], *, client: SoonyClient | None = None,
                 var_dir: Path | str = "var") -> None:
        self.client = client or SoonyClient.from_env(cache_dir=tenant["sources"]["data_root"])
        self.root = Path(var_dir).resolve()
        self.archive = self.root / "raw"
        self.mirror = self.root / "portal"
        self.checkpoint = self.root / "portal-current.json"
        data_root = tenant.get("sources", {}).get("data_root")
        self.baseline = Path(data_root) if data_root and (Path(data_root) / MANIFEST).is_file() else None

    def _current(self) -> dict[str, str]:
        try:
            return json.loads(self.checkpoint.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _version(self, path: str, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Expected a full SHA-256 digest")
        return safe_local_path(self.archive / digest, path)

    def _archive(self, path: str, digest: str) -> None:
        version = self._version(path, digest)
        if version.is_file():
            return
        version.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=version.parent, delete=False) as tmp:
            staged = Path(tmp.name)
        try:
            shutil.copyfile(safe_local_path(self.mirror, path), staged)
            if sha256_file(staged) != digest:
                raise OSError(f"Portal file changed while archiving {path}")
            staged.replace(version)
        finally:
            staged.unlink(missing_ok=True)

    def poll(self) -> list[FileChange]:
        self.root.mkdir(parents=True, exist_ok=True)
        with dir_lock(self.root):
            previous = self._current()
            self.client.files.sync(self.mirror, prune=True, keep_history=False,
                                   human_report=True, baseline_dir=self.baseline)
            manifest = load_manifest(self.mirror)
            if manifest is None:
                raise OSError("Portal sync did not produce a readable manifest")
            current = {path: entry["sha256"] for path, entry in manifest.items()}
            changes = []
            for path in sorted(current.keys() | previous.keys()):
                old, new = previous.get(path), current.get(path)
                if new == old:
                    continue
                if new is None:
                    changes.append(FileChange(path, "removed", None, old))
                else:
                    self._archive(path, new)
                    changes.append(FileChange(path, "changed" if old else "added",
                                              new, old, manifest[path]["bytes"]))
            if changes:
                tmp = self.checkpoint.with_suffix(".tmp")
                tmp.write_text(json.dumps(current), encoding="utf-8")
                tmp.replace(self.checkpoint)
            return changes

    def local_path(self, path: str, sha256: str | None = None) -> str:
        if sha256 is None:
            sha256 = self._current().get(path)
        if sha256 is None:
            raise FileNotFoundError(path)
        version = self._version(path, sha256)
        if not version.is_file():
            raise FileNotFoundError(path)
        return str(version)

    def read(self, path: str, sha256: str | None = None) -> bytes:
        return Path(self.local_path(path, sha256)).read_bytes()
