"""Change events emitted by sync / the listener, plus helpers to inspect *what* changed.

The portal warns that column names, delimiters, encodings and date formats change
on purpose, so for CSV-like files an event can tell you exactly that.
"""

from __future__ import annotations

import csv
import difflib
import io
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

ChangeKind = Literal["added", "changed", "removed", "restored"]
#   added    – new file on the portal
#   changed  – portal replaced an existing file (different SHA-256)
#   removed  – file disappeared from the portal
#   restored – local copy was deleted/edited and got re-downloaded (portal unchanged)

TEXT_SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".md", ".json", ".xml", ".yaml", ".yml"})
_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ChangeEvent:
    kind: ChangeKind
    path: str                                # remote path, e.g. "soony/erp/stock_snapshot.csv"
    sha256: str | None = None                # new hash (None for removed)
    previous_sha256: str | None = None
    bytes: int | None = None
    local_path: Path | None = None           # current local copy
    previous_copy: Path | None = None        # previous version under .history/ (if kept)
    detected_at: str = field(default_factory=now_iso)

    # ------------------------------------------------------------ metadata
    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def supplier_id(self) -> str | None:
        parts = PurePosixPath(self.path).parts
        if len(parts) >= 2 and parts[0] == "suppliers":
            return parts[1].split("_", 1)[0]
        return None

    @property
    def area(self) -> str:
        """Second-level folder: 'erp', 'master', 'order_confirmations', 'notices', ..."""
        parts = PurePosixPath(self.path).parts
        if parts and parts[0] == "suppliers":
            return parts[2] if len(parts) > 3 else "profile"
        return parts[1] if len(parts) > 2 else parts[0]

    @property
    def is_text(self) -> bool:
        return self.suffix in TEXT_SUFFIXES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["local_path"] = str(self.local_path) if self.local_path else None
        d["previous_copy"] = str(self.previous_copy) if self.previous_copy else None
        d["supplier_id"] = self.supplier_id
        return d

    def __str__(self) -> str:
        sym = {"added": "+", "changed": "~", "removed": "-", "restored": "↺"}[self.kind]
        return f"{sym} {self.path}"

    # ------------------------------------------------------------ content
    def read_bytes(self) -> bytes | None:
        return self.local_path.read_bytes() if self.local_path and self.local_path.is_file() else None

    def read_previous_bytes(self) -> bytes | None:
        return self.previous_copy.read_bytes() if self.previous_copy and self.previous_copy.is_file() else None

    def read_text(self) -> str | None:
        data = self.read_bytes()
        return decode(data)[0] if data is not None else None

    def text_diff(self, context: int = 2, max_lines: int = 200) -> str | None:
        """Unified diff old→new for text files (needs keep_history)."""
        if not self.is_text:
            return None
        old, new = self.read_previous_bytes(), self.read_bytes()
        if old is None or new is None:
            return None
        lines = list(difflib.unified_diff(
            decode(old)[0].splitlines(), decode(new)[0].splitlines(),
            fromfile=f"{self.path}@{(self.previous_sha256 or '')[:8]}",
            tofile=f"{self.path}@{(self.sha256 or '')[:8]}", lineterm="", n=context,
        ))
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more diff lines)"]
        return "\n".join(lines)

    def csv_changes(self) -> "CsvChange | None":
        """Structural comparison for CSV/TSV: delimiter, encoding, columns, row count."""
        if self.suffix not in (".csv", ".tsv"):
            return None
        new = self.read_bytes()
        if new is None:
            return None
        old = self.read_previous_bytes()
        return CsvChange.compare(profile_csv(old) if old is not None else None, profile_csv(new))


# ---------------------------------------------------------------------- CSV
@dataclass(frozen=True)
class CsvProfile:
    encoding: str
    bom: bool
    delimiter: str
    columns: tuple[str, ...]
    rows: int


@dataclass(frozen=True)
class CsvChange:
    old: CsvProfile | None
    new: CsvProfile
    columns_added: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()
    columns_reordered: bool = False
    delimiter_changed: bool = False
    encoding_changed: bool = False
    row_delta: int = 0

    @property
    def schema_changed(self) -> bool:
        return bool(self.columns_added or self.columns_removed or self.columns_reordered
                    or self.delimiter_changed or self.encoding_changed)

    @classmethod
    def compare(cls, old: CsvProfile | None, new: CsvProfile) -> "CsvChange":
        if old is None:
            return cls(old=None, new=new, row_delta=new.rows)
        o, n = set(old.columns), set(new.columns)
        return cls(
            old=old, new=new,
            columns_added=tuple(c for c in new.columns if c not in o),
            columns_removed=tuple(c for c in old.columns if c not in n),
            columns_reordered=o == n and old.columns != new.columns,
            delimiter_changed=old.delimiter != new.delimiter,
            encoding_changed=(old.encoding, old.bom) != (new.encoding, new.bom),
            row_delta=new.rows - old.rows,
        )

    def summary(self) -> str:
        if self.old is None:
            return f"{self.new.rows} rows, {len(self.new.columns)} cols, delim {self.new.delimiter!r}"
        bits = []
        if self.delimiter_changed:
            bits.append(f"delimiter {self.old.delimiter!r}→{self.new.delimiter!r}")
        if self.encoding_changed:
            bits.append(f"encoding {_enc(self.old)}→{_enc(self.new)}")
        if self.columns_added:
            bits.append(f"+cols {list(self.columns_added)}")
        if self.columns_removed:
            bits.append(f"-cols {list(self.columns_removed)}")
        if self.columns_reordered:
            bits.append("columns reordered")
        bits.append(f"rows {self.old.rows}→{self.new.rows} ({self.row_delta:+d})")
        return ", ".join(bits)


def _enc(p: CsvProfile) -> str:
    return p.encoding + ("+BOM" if p.bom else "")


def decode(data: bytes) -> tuple[str, str, bool]:
    """Return (text, encoding, had_bom)."""
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc, bom
        except UnicodeDecodeError:
            continue
    raise AssertionError("unreachable")


def profile_csv(data: bytes) -> CsvProfile:
    text, enc, bom = decode(data)
    sample = text[:8192]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        delim = max(",;\t|", key=first.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    header = tuple(c.strip() for c in rows[0]) if rows else ()
    return CsvProfile(encoding=enc, bom=bom, delimiter=delim, columns=header, rows=max(0, len(rows) - 1))
