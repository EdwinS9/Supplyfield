"""Data quality report for a mirrored portal folder.

Deterministic checks, no model calls. The report says where the raw files
disagree with themselves or with each other before any reader or agent sees
them. Every check works from the file structure (headers, first columns, value
shapes); nothing here knows a customer's part numbers, suppliers or orders.

Checks
    mirror        manifest against the files on disk; change log paths
    format        encoding, delimiter, ragged CSV rows, sheets read only as text
    values        "12.000" style numbers, decimal commas, numbers stored as text,
                  non-ISO or ambiguous dates, placeholders such as "TBD"
    keys          duplicate keys and duplicate rows
    references    ids that no file defines in its first column
    agreement     the same id with different attribute values in two files
    changes       change tables (field/old/new, x_old/x_new) not applied to the master
    quantities    received above ordered in the same row
    series        missing days in a daily log, a log that stopped before the as-of date
    documents     ids in scan and PDF file names that no table mentions

Run: ``python -m gtl.ingest.quality soony-data --md var/quality/report.md``.
The report quotes the data, so keep it under ``var/`` (not committed).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from gtl.read.table_reader import decode_text, parse_tables, sniff_delimiter

TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".xlsx", ".xlsm"})
SEVERITIES = ("error", "warn", "info")
MANIFEST = ".soony_manifest.json"
CHANGELOG = ".soony_changes.jsonl"
MAX_EXAMPLES = 5

ID_RE = re.compile(r"^(?=[^ ]*\d)[A-Z0-9]+(?:[-_/.][A-Z0-9]+)+$|^\d{6,}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ].*)?$")
SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
DOT_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
DOT_GROUPED_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
DECIMAL_COMMA_RE = re.compile(r"^-?\d+,\d+$")
PLAIN_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
MULTI_SPLIT_RE = re.compile(r"\s*[|;,]\s*")
PLACEHOLDERS = frozenset({"tbd", "tbc", "todo", "n/a", "na", "?", "??", "unknown", "check", "xxx", "-"})
FREE_TEXT = frozenset({"note", "notes", "comment", "comments", "remark", "remarks", "reason"})
ORDERED_SUFFIXES = ("ordered",)
RECEIVED_SUFFIXES = ("received", "delivered", "shipped")


@dataclass(frozen=True)
class Issue:
    check: str
    severity: str  # error | warn | info
    path: str
    message: str
    where: str = ""
    examples: tuple[str, ...] = ()


@dataclass
class TableData:
    path: str
    sheet: str
    headers: list[str]  # normalised names, same order as the source
    rows: list[dict[str, Any]]  # normalised header -> raw cell
    row_numbers: list[int]

    @property
    def label(self) -> str:
        return f"{self.path} [{self.sheet}]" if self.sheet else self.path

    @property
    def key(self) -> str:
        return self.headers[0] if self.headers else ""


@dataclass
class Report:
    root: str
    as_of: str
    files: int = 0
    tables: int = 0
    issues: list[Issue] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(i.severity for i in self.issues)
        return {s: c.get(s, 0) for s in SEVERITIES}

    def by_check(self) -> dict[str, int]:
        return dict(sorted(Counter(i.check for i in self.issues).items()))

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "as_of": self.as_of, "files": self.files, "tables": self.tables,
                "counts": self.counts(), "by_check": self.by_check(),
                "issues": [asdict(i) for i in self.issues]}


# --------------------------------------------------------------------- values
def norm_header(h: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


def cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def is_id(s: str) -> bool:
    return bool(ID_RE.match(s)) and not ISO_DATE_RE.match(s)


def id_values(v: Any) -> list[str]:
    """Ids in a cell; a cell may list several ('a|b', 'a, b')."""
    s = cell_str(v)
    if not s:
        return []
    if is_id(s):
        return [s]
    parts = [p for p in MULTI_SPLIT_RE.split(s) if p]
    return parts if len(parts) > 1 and all(is_id(p) for p in parts) else []


def comparable(v: Any) -> str:
    """A value normalised for comparing two files: case, '_' vs ' ', number formatting, list order."""
    ids = id_values(v)
    if len(ids) > 1:
        return "|".join(sorted(ids))
    s = re.sub(r"\s+", " ", cell_str(v).replace("_", " ")).strip().lower()
    if PLAIN_NUMBER_RE.match(s):
        return repr(float(s))
    return s


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = cell_str(v).replace(" ", "")
    return float(s) if PLAIN_NUMBER_RE.match(s) else None


def to_iso_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = cell_str(v)
    if ISO_DATE_RE.match(s):
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _examples(items: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for x in items:
        if len(out) == MAX_EXAMPLES:
            break
        out.append(str(x))
    return tuple(out)


# -------------------------------------------------------------------- loading
def iter_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))


def load_tables(root: Path, files: list[Path], issues: list[Issue]) -> list[TableData]:
    tables: list[TableData] = []
    for p in files:
        if p.suffix.lower() not in TABLE_SUFFIXES:
            continue
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        if p.suffix.lower() in (".csv", ".tsv"):
            issues.extend(check_csv_format(rel, data))
        try:
            parsed = parse_tables(rel, data)
        except Exception as e:  # a broken file is a finding, not a crash
            issues.append(Issue("format", "error", rel, f"cannot be read as a table: {type(e).__name__}: {e}"))
            continue
        for block in parsed.blocks:
            sheet = block.label.removeprefix("sheet:")
            if block.label.startswith("sheet:") and not any(t.sheet == sheet for t in parsed.tables):
                issues.append(Issue("format", "info", rel, "sheet has no clean table; readers see it only as text",
                                    where=sheet))
        for t in parsed.tables:
            headers = [norm_header(h) for h in t.headers]
            rows = [{norm_header(h): r.cells.get(h) for h in t.headers} for r in t.rows]
            tables.append(TableData(rel, t.sheet, headers, rows, [r.number for r in t.rows]))
    return tables


# ---------------------------------------------------------------- the checks
def check_mirror(root: Path, files: list[Path]) -> list[Issue]:
    issues: list[Issue] = []
    mpath = root / MANIFEST
    if not mpath.is_file():
        return [Issue("mirror", "info", MANIFEST, "no manifest; mirror integrity not checked")]
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [Issue("mirror", "error", MANIFEST, f"manifest unreadable: {e}")]
    on_disk = {p.relative_to(root).as_posix(): p for p in files}
    missing = sorted(set(manifest) - set(on_disk))
    extra = sorted(set(on_disk) - set(manifest))
    if missing:
        issues.append(Issue("mirror", "error", MANIFEST, f"{len(missing)} file(s) in the manifest are missing on disk",
                            examples=_examples(missing)))
    if extra:
        issues.append(Issue("mirror", "warn", MANIFEST,
                            f"{len(extra)} file(s) on disk are not in the manifest (local edits or a partial sync)",
                            examples=_examples(extra)))
    for rel in sorted(set(manifest) & set(on_disk)):
        entry = manifest[rel] or {}
        sha = hashlib.sha256(on_disk[rel].read_bytes()).hexdigest()
        if entry.get("sha256") != sha:
            issues.append(Issue("mirror", "error", rel,
                                "content differs from the synced version (edited locally?)",
                                examples=(f"manifest {str(entry.get('sha256'))[:12]}", f"disk {sha[:12]}")))
    clog = root / CHANGELOG
    if clog.is_file():
        absolute: list[str] = []
        bad_lines: list[str] = []
        for n, line in enumerate(clog.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                bad_lines.append(f"line {n}")
                continue
            for k in ("local_path", "previous_copy"):
                val = ev.get(k)
                if isinstance(val, str) and PurePosixPath(val).is_absolute():
                    absolute.append(f"line {n}: {k}")
        if bad_lines:
            issues.append(Issue("mirror", "error", CHANGELOG, f"{len(bad_lines)} line(s) are not JSON",
                                examples=_examples(bad_lines)))
        if absolute:
            issues.append(Issue("mirror", "warn", CHANGELOG,
                                f"{len(absolute)} event(s) store an absolute local path: machine-specific and "
                                "names the user's home folder", examples=_examples(absolute)))
    return issues


def check_csv_format(rel: str, data: bytes) -> list[Issue]:
    issues: list[Issue] = []
    if not data.strip():
        return [Issue("format", "error", rel, "file is empty")]
    if data.startswith(b"\xef\xbb\xbf"):
        issues.append(Issue("format", "info", rel, "starts with a UTF-8 byte order mark"))
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        issues.append(Issue("format", "warn", rel, "not UTF-8 (legacy encoding); umlauts and signs may be misread"))
    text = decode_text(data)
    delim = sniff_delimiter(text)
    if delim != ",":
        issues.append(Issue("format", "info", rel, f"delimiter is {delim!r}, not ','"))
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]
    if rows:
        width = len(rows[0])
        ragged = [f"row {i + 1}: {len(r)} fields, header has {width}" for i, r in enumerate(rows[1:], start=1)
                  if len(r) != width]
        if ragged:
            issues.append(Issue("format", "error", rel,
                                f"{len(ragged)} row(s) have a different number of fields than the header; "
                                "cells are dropped or shifted", examples=_examples(ragged)))
        dup = [h for h, n in Counter(norm_header(h) for h in rows[0]).items() if n > 1]
        if dup:
            issues.append(Issue("format", "warn", rel, "duplicate column names", examples=_examples(dup)))
    return issues


def _has_plain_decimals(cells: list[tuple[int, Any]]) -> bool:
    """A column of real decimals (coordinates, prices): '53.540' there is not a thousands separator."""
    for _, v in cells:
        if isinstance(v, float) and not v.is_integer():
            return True
        s = cell_str(v)
        if PLAIN_NUMBER_RE.match(s) and "." in s and not DOT_GROUPED_RE.match(s):
            return True
    return False


def check_values(t: TableData) -> list[Issue]:
    issues: list[Issue] = []
    for h in t.headers:
        cells = [(n, row.get(h)) for n, row in zip(t.row_numbers, t.rows)]
        texts = [(n, v) for n, v in cells if isinstance(v, str) and v.strip()]
        grouped = [f"row {n}: {v.strip()!r}" for n, v in texts if DOT_GROUPED_RE.match(v.strip())]
        if len(grouped) >= 2 and not _has_plain_decimals(cells):
            issues.append(Issue("values", "warn", t.label, f"{len(grouped)} number(s) grouped with dots, e.g. "
                                "'12.000' (12 or 12,000?)", where=h, examples=_examples(grouped)))
        comma = [f"row {n}: {v.strip()!r}" for n, v in texts if DECIMAL_COMMA_RE.match(v.strip())]
        if comma:
            issues.append(Issue("values", "info", t.label, f"{len(comma)} number(s) with a decimal comma",
                                where=h, examples=_examples(comma)))
        if t.path.lower().endswith((".xlsx", ".xlsm")):
            numeric = sum(1 for _, v in cells if isinstance(v, (int, float)) and not isinstance(v, bool))
            as_text = [f"row {n}: {v.strip()!r}" for n, v in texts if PLAIN_NUMBER_RE.match(v.strip())]
            if as_text and numeric:
                issues.append(Issue("values", "info", t.label,
                                    f"{len(as_text)} number(s) stored as text next to real numbers",
                                    where=h, examples=_examples(as_text)))
        slash = [(n, v.strip()) for n, v in texts if SLASH_DATE_RE.match(v.strip())]
        dotted = [(n, v.strip()) for n, v in texts if DOT_DATE_RE.match(v.strip())]
        iso = [v for _, v in cells if to_iso_date(v)]
        if slash:
            parts = [SLASH_DATE_RE.match(v).groups() for _, v in slash]  # type: ignore[union-attr]
            first_max = max(int(a) for a, _, _ in parts)
            second_max = max(int(b) for _, b, _ in parts)
            order = ("day/month" if first_max > 12 else "month/day" if second_max > 12
                     else "ambiguous (day/month or month/day)")
            sev = "warn" if order.startswith("ambiguous") else "info"
            issues.append(Issue("values", sev, t.label, f"{len(slash)} date(s) written with slashes, {order}",
                                where=h, examples=_examples(f"row {n}: {v}" for n, v in slash)))
        if dotted:
            issues.append(Issue("values", "info", t.label, f"{len(dotted)} date(s) written as dd.mm.yyyy",
                                where=h, examples=_examples(f"row {n}: {v}" for n, v in dotted)))
        if sum(bool(x) for x in (slash, dotted, iso)) > 1:
            issues.append(Issue("values", "warn", t.label, "dates in more than one format in the same column",
                                where=h))
        if h not in FREE_TEXT:
            ph = [f"row {n}: {cell_str(v)!r}" for n, v in cells if cell_str(v).lower() in PLACEHOLDERS]
            if ph:
                issues.append(Issue("values", "info", t.label, f"{len(ph)} placeholder value(s)", where=h,
                                    examples=_examples(ph)))
    return issues


def check_keys(t: TableData) -> list[Issue]:
    issues: list[Issue] = []
    keys = [cell_str(r.get(t.key)) for r in t.rows]
    filled = [k for k in keys if k]
    if filled and all(is_id(k) for k in filled):
        counts = Counter(filled)
        dups = {k: n for k, n in counts.items() if n > 1}
        if dups and len(counts) >= 0.9 * len(filled):
            issues.append(Issue("keys", "warn", t.label,
                                f"{len(dups)} value(s) of the key column '{t.key}' appear more than once",
                                where=t.key, examples=_examples(f"{k} x{n}" for k, n in dups.items())))
    seen: dict[tuple[str, ...], int] = {}
    repeats: list[str] = []
    for n, r in zip(t.row_numbers, t.rows):
        sig = tuple(cell_str(r.get(h)) for h in t.headers)
        if sig in seen:
            repeats.append(f"row {n} repeats row {seen[sig]}")
        else:
            seen[sig] = n
    if repeats:
        issues.append(Issue("keys", "warn", t.label, f"{len(repeats)} exact duplicate row(s)",
                            examples=_examples(repeats)))
    return issues


Masters = dict[str, list[tuple[TableData, dict[str, Any]]]]


def defined_ids(tables: list[TableData]) -> set[str]:
    """Every id that some table holds in its first column."""
    return {k for t in tables for r in t.rows if (k := cell_str(r.get(t.key))) and is_id(k)}


def _masters(tables: list[TableData]) -> Masters:
    """id -> its record in every table whose first column holds unique ids (one row per entity)."""
    out: Masters = defaultdict(list)
    for t in tables:
        keys = [cell_str(r.get(t.key)) for r in t.rows]
        if not keys or not all(k and is_id(k) for k in keys) or len(set(keys)) != len(keys):
            continue
        for k, r in zip(keys, t.rows):
            out[k].append((t, r))
    return out


def check_references(tables: list[TableData], defined: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    for t in tables:
        for h in t.headers[1:]:
            vals: dict[str, int] = {}
            for n, r in zip(t.row_numbers, t.rows):
                for v in id_values(r.get(h)):
                    vals.setdefault(v, n)
            if len(vals) < 2:
                continue
            known = [v for v in vals if v in defined]
            orphans = [v for v in vals if v not in defined]
            if orphans and 2 * len(known) >= len(vals):
                issues.append(Issue("references", "warn", t.label,
                                    f"{len(orphans)} id(s) in '{h}' are not defined in any file's first column",
                                    where=h, examples=_examples(f"{v} (row {vals[v]})" for v in orphans)))
    return issues


def _entity_dir(path: str) -> str | None:
    """The first folder named after an id ('X-0018_Name'): files under it describe that one entity."""
    for part in PurePosixPath(path).parts[:-1]:
        if is_id(part.split("_", 1)[0]):
            return part
    return None


def _unrelated(a: TableData, b: TableData) -> bool:
    """Versions or partitions of one layout (a price list per date, a components file per supplier) differ
    on purpose, and so do files about two different entities."""
    if set(a.headers) == set(b.headers):
        return True
    da, db = _entity_dir(a.path), _entity_dir(b.path)
    return da is not None and db is not None and da != db


def _comparable_column(h: str) -> bool:
    return (h not in FREE_TEXT and not h.startswith("valid_") and not h.endswith(("date", "_at"))
            and h != "updated")


def _depends_on(t: TableData, kcol: str, h: str) -> bool:
    """True when `h` has one value per id in `kcol` across the table, seen on at least two ids with two rows."""
    groups: dict[str, set[str]] = defaultdict(set)
    sizes: Counter[str] = Counter()
    for r in t.rows:
        ids = id_values(r.get(kcol))
        v = comparable(r.get(h))
        if len(ids) == 1 and v:
            groups[ids[0]].add(v)
            sizes[ids[0]] += 1
    return (sum(1 for k in groups if sizes[k] >= 2) >= 2) and all(len(v) == 1 for v in groups.values())


def check_agreement(tables: list[TableData], masters: Masters) -> list[Issue]:
    found: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    # two files that both describe the entity (both hold it in their first column)
    for k, recs in masters.items():
        for i, (a, ra) in enumerate(recs):
            for b, rb in recs[i + 1:]:
                if _unrelated(a, b):
                    continue
                for h in a.headers[1:]:
                    if h == b.key or h not in b.headers or not _comparable_column(h):
                        continue
                    va, vb = comparable(ra.get(h)), comparable(rb.get(h))
                    if va and vb and va != vb:
                        found[(a.label, b.label, h)].append(
                            f"{k}: {cell_str(ra.get(h))!r} in {a.label}, {cell_str(rb.get(h))!r} in {b.label}")
    # a file that refers to the entity and repeats one of its attributes on every row
    for t in tables:
        for kcol in t.headers[1:]:
            rows_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in t.rows:
                ids = id_values(r.get(kcol))
                if len(ids) == 1:
                    rows_by_id[ids[0]].append(r)
            for h in t.headers:
                if h in (t.key, kcol) or not _comparable_column(h) or not _depends_on(t, kcol, h):
                    continue
                for k, rows in rows_by_id.items():
                    recs = [(m, rec) for m, rec in masters.get(k, [])
                            if m is not t and not _unrelated(m, t) and h in m.headers and h != m.key
                            and comparable(rec.get(h))]
                    master_vals = {comparable(rec.get(h)) for _, rec in recs}
                    mine = comparable(rows[0].get(h))
                    if len(master_vals) != 1 or not mine or mine in master_vals:
                        continue
                    m, rec = recs[0]
                    found[(t.label, m.label, h)].append(
                        f"{k}: all {len(rows)} row(s) in {t.label} say {cell_str(rows[0].get(h))!r}, "
                        f"{m.label} says {cell_str(rec.get(h))!r}")
    return [Issue("agreement", "warn", a, f"{len(ex)} id(s) have a different '{h}' in {b}", where=h,
                  examples=_examples(ex))
            for (a, b, h), ex in sorted(found.items())]


def _change_columns(t: TableData) -> list[tuple[str | None, str, str]]:
    """(attribute or None for a 'field' column, old column, new column)."""
    hs = set(t.headers)
    out: list[tuple[str | None, str, str]] = []
    if {"field", "old", "new"} <= hs:
        out.append((None, "old", "new"))
    for h in t.headers:
        if h.endswith("_old") and h[:-4] + "_new" in hs:
            out.append((h[:-4], h, h[:-4] + "_new"))
    return out


def check_changes(tables: list[TableData], masters: Masters) -> list[Issue]:
    issues: list[Issue] = []
    for t in tables:
        specs = _change_columns(t)
        if not specs:
            continue
        stale: list[str] = []
        neither: list[str] = []
        for n, r in zip(t.row_numbers, t.rows):
            k = cell_str(r.get(t.key))
            for attr, old_col, new_col in specs:
                name = norm_header(r.get("field")) if attr is None else attr
                old, new = comparable(r.get(old_col)), comparable(r.get(new_col))
                if not name or not new:
                    continue
                for m, rec in masters.get(k, []):
                    if m is t or name not in m.headers or _change_columns(m):
                        continue
                    have = comparable(rec.get(name))
                    if have == new:
                        continue
                    line = (f"{k} {name}: {m.label} has {cell_str(rec.get(name))!r}, "
                            f"change in row {n} says {cell_str(r.get(old_col))!r} -> {cell_str(r.get(new_col))!r}")
                    (stale if have == old else neither).append(line)
        if stale:
            issues.append(Issue("changes", "warn", t.label,
                                f"{len(stale)} change(s) not applied: a master file still holds the old value",
                                examples=_examples(stale)))
        if neither:
            issues.append(Issue("changes", "warn", t.label,
                                f"{len(neither)} change(s) whose old value does not match the master either",
                                examples=_examples(neither)))
    return issues


def _pairs(headers: list[str]) -> list[tuple[str, str]]:
    out = []
    for a in headers:
        for suf in ORDERED_SUFFIXES:
            if not a.endswith(suf):
                continue
            stem = a[: -len(suf)]
            out += [(a, stem + r) for r in RECEIVED_SUFFIXES if stem + r in headers]
    return out


def check_quantities(t: TableData) -> list[Issue]:
    issues: list[Issue] = []
    for ordered, received in _pairs(t.headers):
        over: list[str] = []
        far: list[str] = []
        for n, r in zip(t.row_numbers, t.rows):
            a, b = to_number(r.get(ordered)), to_number(r.get(received))
            if a is None or b is None or a <= 0 or b <= a:
                continue
            line = f"row {n} ({cell_str(r.get(t.key))}): {received} {b:g} vs {ordered} {a:g}"
            (far if b >= 5 * a else over).append(line)
        if far:
            issues.append(Issue("quantities", "error", t.label,
                                f"{len(far)} row(s) received at least five times the ordered quantity "
                                "(a typo or an extra zero?)", where=received, examples=_examples(far)))
        if over:
            issues.append(Issue("quantities", "info", t.label, f"{len(over)} row(s) received more than ordered",
                                where=received, examples=_examples(over)))
    return issues


def check_series(t: TableData, as_of: date) -> list[Issue]:
    days = [to_iso_date(r.get(t.key)) for r in t.rows]
    if not days or any(d is None for d in days):
        return []
    distinct = sorted(set(d for d in days if d))
    if len(distinct) < 5:
        return []
    gaps = [(b - a).days for a, b in zip(distinct, distinct[1:])]
    if sorted(gaps)[len(gaps) // 2] != 1:
        return []  # not a daily log
    weekdays_only = all(d.weekday() < 5 for d in distinct)

    def expected(d: date) -> bool:
        return not weekdays_only or d.weekday() < 5

    have = set(distinct)
    span = (distinct[-1] - distinct[0]).days
    inner = [distinct[0] + timedelta(i) for i in range(span + 1)]
    missing = [d for d in inner if expected(d) and d not in have]
    issues: list[Issue] = []
    cal = "working days" if weekdays_only else "days"
    if missing:
        issues.append(Issue("series", "warn", t.label, f"daily log ({cal}) has {len(missing)} gap(s)",
                            where=t.key, examples=_examples(d.isoformat() for d in missing)))
    tail = [distinct[-1] + timedelta(i) for i in range(1, (as_of - distinct[-1]).days)]
    tail = [d for d in tail if expected(d)]
    if tail:
        issues.append(Issue("series", "warn", t.label,
                            f"daily log ({cal}) ends {distinct[-1].isoformat()}; {len(tail)} {cal[:-1]}(s) "
                            f"before {as_of.isoformat()} are not logged", where=t.key,
                            examples=_examples(d.isoformat() for d in tail)))
    return issues


FILE_PART_RE = re.compile(r"[_\s]+")


def _is_date_token(tok: str) -> bool:
    """'20260903' or '2026-11' in a file name is a date, not an id."""
    for fmt, shape in (("%Y%m%d", r"\d{8}"), ("%Y-%m", r"\d{4}-\d{2}"), ("%Y-%m-%d", r"\d{4}-\d{2}-\d{2}")):
        if re.fullmatch(shape, tok):
            try:
                datetime.strptime(tok, fmt)
                return True
            except ValueError:
                return False
    return False


def check_documents(root: Path, files: list[Path], tables: list[TableData]) -> list[Issue]:
    """Ids in the names of scans, PDFs and other documents that no table mentions anywhere."""
    known = {v for t in tables for r in t.rows for h in t.headers for v in id_values(r.get(h))}
    unknown: list[str] = []
    named = 0
    for p in files:
        if p.suffix.lower() in TABLE_SUFFIXES:
            continue
        toks = [x for x in FILE_PART_RE.split(p.stem) if is_id(x) and not _is_date_token(x)]
        named += bool(toks)
        unknown += [f"{p.relative_to(root).as_posix()}: {x}" for x in toks if x not in known]
    if not unknown or 2 * len(unknown) > max(named, 1):
        return []  # most names carry no table ids: their tokens are titles, not references
    return [Issue("documents", "info", "(file names)",
                  f"{len(unknown)} id(s) in document file names appear in no table", examples=_examples(unknown))]


# ------------------------------------------------------------------- driver
def build_report(root: Path | str, *, as_of: date | None = None) -> Report:
    root = Path(root).resolve()
    as_of = as_of or date.today()
    report = Report(root=str(root), as_of=as_of.isoformat())
    files = iter_files(root)
    report.files = len(files)
    issues = check_mirror(root, files)
    tables = load_tables(root, files, issues)
    report.tables = len(tables)
    for t in tables:
        issues += check_values(t) + check_keys(t) + check_quantities(t) + check_series(t, as_of)
    masters = _masters(tables)
    issues += check_references(tables, defined_ids(tables))
    issues += check_agreement(tables, masters)
    issues += check_changes(tables, masters)
    issues += check_documents(root, files, tables)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    report.issues = sorted(issues, key=lambda i: (order.get(i.severity, 9), i.check, i.path, i.where))
    return report


def render_markdown(report: Report) -> str:
    c = report.counts()
    lines = [
        "# Data quality report", "",
        f"`{report.root}`, as of {report.as_of}: {report.files} files, {report.tables} tables. "
        f"**{c['error']} errors, {c['warn']} warnings, {c['info']} notes.**", "",
        "| Check | Issues |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in report.by_check().items()], "",
    ]
    for sev, title in (("error", "Errors"), ("warn", "Warnings"), ("info", "Notes")):
        items = [i for i in report.issues if i.severity == sev]
        if not items:
            continue
        lines += [f"## {title}", ""]
        for i in items:
            where = f" · `{i.where}`" if i.where else ""
            lines.append(f"- **{i.check}** · `{i.path}`{where}: {i.message}")
            lines += [f"  - {e}" for e in i.examples]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gtl.ingest.quality", description=__doc__.split("\n")[0])
    ap.add_argument("root", nargs="?", default="soony-data", help="mirrored portal folder")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="reference day (default: today)")
    ap.add_argument("--md", type=Path, help="write the Markdown report here")
    ap.add_argument("--json", type=Path, help="write the JSON report here")
    ap.add_argument("--strict", action="store_true", help="exit 1 when there is an error")
    args = ap.parse_args(argv)
    if not Path(args.root).is_dir():
        print(f"not a folder: {args.root}", file=sys.stderr)
        return 2
    report = build_report(args.root, as_of=args.as_of)
    for out, text in ((args.md, render_markdown(report)),
                      (args.json, json.dumps(report.to_dict(), indent=1, ensure_ascii=False))):
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
    c = report.counts()
    print(f"{report.files} files, {report.tables} tables: {c['error']} errors, {c['warn']} warnings, "
          f"{c['info']} notes")
    for k, v in report.by_check().items():
        print(f"  {k:<11} {v}")
    return 1 if args.strict and c["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
