"""The data quality view: every finding of the mirror quality report, grouped by kind, each pointing at its file."""
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from gtl.ingest.quality import Issue, build_report, iter_files
from soony_api.sync import CHANGELOG

# Report checks grouped into the kinds of problem a person recognises. Order is the order on screen.
KINDS = (
    ("conflicting", "Conflicting data", "Two files say different things about the same record.", ("agreement",)),
    ("corrected", "Corrected data", "Files the source replaced after publishing them, and corrections that a "
     "master file does not hold yet.", ("changes", "portal")),
    ("implausible", "Implausible values", "Numbers that are possible to read but unlikely to be right.", ("quantities",)),
    ("missing", "Missing data", "Days, records or references that should be there and are not.",
     ("series", "references", "documents")),
    ("ambiguous", "Ambiguous formats", "Numbers, dates and layouts that can be read more than one way.",
     ("values", "format")),
    ("duplicate", "Duplicate records", "The same key appears more than once.", ("keys",)),
    ("integrity", "File integrity", "The mirror itself: files missing, changed or recorded oddly.", ("mirror",)),
)
SHEET_RE = re.compile(r"^(?P<path>.+?) \[(?P<sheet>[^\]]+)\]$")
ROW_RE = re.compile(r"\brow (\d+)\b")
FILE_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{2,5})(?: \[([^\]]+)\])?")
COUNT_RE = re.compile(r"^(\d+) ")
LAST_DAY_RE = re.compile(r"\bends (\d{4}-\d{2}-\d{2});")
# Report messages as a person would title them. The first pattern that matches wins; the rest fall back to the message.
HEADLINES = tuple((re.compile(p), t, d) for p, t, d in (
    (r"have a different '(?P<column>[^']+)' in", "Files disagree on {column}", "The same record has another value elsewhere."),
    (r"not applied", "Corrections not applied", "The master file still holds the old value."),
    (r"old value does not match", "Corrections that match nothing", "The master holds neither the old nor the new value."),
    (r"source replaced this file(?P<on> on \S+?)?;", "Replaced by the source",
     "Published again{on} with other content. Figures read before may be out of date."),
    (r"five times the ordered", "Received five times the order or more", "A typo or an extra zero?"),
    (r"received more than ordered", "Received more than ordered", "Over-deliveries within a plausible range."),
    (r"in '(?P<column>[^']+)' are not defined", "Unknown ids in {column}", "No master file defines them."),
    (r"ends (?P<last>\S+); .* before (?P<today>\S+) are not logged", "Days not logged",
     "The log stops on {last}. Days up to {today} are missing."),
    (r"daily log .* gap", "Gaps in the daily log", "Days inside the log with no entry."),
    (r"in document file names appear in no table", "Documents about unknown ids", "A file name carries an id no table holds."),
    (r"grouped with dots", "Numbers grouped with dots", "'12.000' can be read as 12 or as 12,000."),
    (r"decimal comma", "Decimal commas", "'1,52' means 1.52; a reader that expects a point gets it wrong."),
    (r"stored as text next to real numbers", "Numbers stored as text", "Next to real numbers in the same column."),
    (r"slashes, ambiguous", "Dates that read both ways", "'03/04' can be 3 April or 4 March."),
    (r"slashes, (?P<order>month/day|day/month)", "Dates written {order}", "Slash dates; readers must know the order."),
    (r"dd\.mm\.yyyy", "Dates written dd.mm.yyyy", "Readers convert them to ISO dates."),
    (r"more than one format", "Mixed date formats", "One column uses more than one way to write dates."),
    (r"placeholder", "Placeholder values", "Cells hold a stand-in, not a real value."),
    (r"key column '(?P<column>[^']+)' appear more than once", "Repeated keys in {column}", "One id, more than one row."),
    (r"exact duplicate row", "Exact duplicate rows", "The same row appears twice."),
    (r"no clean table", "Sheet is not a table", "Readers see it only as text."),
    (r"delimiter is (?P<sep>'.'), not", "Delimiter is {sep}", "Not a comma; readers detect it."),
    (r"byte order mark", "Byte order mark", "Invisible marker at the start; readers strip it."),
    (r"not UTF-8", "Legacy text encoding", "Umlauts and signs may be misread."),
    (r"cannot be read as a table", "Cannot be read", "The file does not open as a table."),
    (r"file is empty", "Empty file", "Nothing to read."),
    (r"duplicate column names", "Duplicate column names", "Two columns share a name."),
    (r"absolute local path", "Local paths in the change log", "Machine-specific; names the user's home folder."),
))
QUOTED = r"'(?:[^'\\]|\\.)*'|\"[^\"]*\""
# Example lines of the report, by check, read back into named parts so the screen can draw them.
SHAPES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "changes": (("change", re.compile(rf"^(?P<id>\S+) (?P<field>\S+): (?P<master>.+?) has (?P<have>{QUOTED}), "
                                      rf"change in row (?P<row>\d+) says (?P<old>{QUOTED}) -> (?P<new>{QUOTED})$")),),
    "agreement": (("compare", re.compile(rf"^(?P<id>\S+): (?P<mine>{QUOTED}) in (?P<here>.+?), "
                                         rf"(?P<theirs>{QUOTED}) in (?P<there>.+)$")),
                  ("compare", re.compile(rf"^(?P<id>\S+): all (?P<rows>\d+) row\(s\) in (?P<here>.+?) say "
                                         rf"(?P<mine>{QUOTED}), (?P<there>.+?) says (?P<theirs>{QUOTED})$"))),
    "quantities": (("quantity", re.compile(r"^row (?P<row>\d+) \((?P<id>[^)]*)\): \S+ (?P<received>[\d.e+-]+) "
                                           r"vs \S+ (?P<ordered>[\d.e+-]+)$")),),
    "references": (("value", re.compile(r"^(?P<value>.+) \(row (?P<row>\d+)\)$")),),
    "values": (("value", re.compile(r"^row (?P<row>\d+): (?P<value>.+)$")),),
    "keys": (("value", re.compile(r"^(?P<value>.+) x(?P<times>\d+)$")),
             ("value", re.compile(r"^row (?P<row>\d+) repeats row (?P<first>\d+)$"))),
    "series": (("value", re.compile(r"^(?P<value>\d{4}-\d{2}-\d{2})$")),),
    "documents": (("value", re.compile(r"^(?P<file>.+?): (?P<value>\S+)$")),),
    "mirror": (("value", re.compile(r"^line (?P<line>\d+): (?P<value>.+)$")),),
}


def _locator(sheet: str | None, row: str | None) -> str:
    parts = [f"sheet:{sheet}"] if sheet else []
    if row:
        parts.append(f"row:{row}")
    return ";".join(parts)


def _place(issue: Issue, known: set[str]) -> tuple[str | None, str | None]:
    """The file an issue is about and its sheet. Layout notes name the sheet in the column field."""
    match = SHEET_RE.match(issue.path)
    path, sheet = (match["path"], match["sheet"]) if match else (issue.path, None)
    if path not in known:
        return None, None
    if sheet is None and issue.check == "format" and issue.where and path.endswith((".xlsx", ".xlsm")):
        sheet = issue.where
    return path, sheet


def _files(text: str, known: set[str]) -> list[tuple[str, str | None]]:
    named = [(m[1], m[2]) for m in FILE_RE.finditer(text) if m[1] in known]
    head = text.split(":", 1)[0]
    return [(head, None)] + named if head in known else named


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value


def _facts(check: str, text: str) -> tuple[str, dict[str, str]] | tuple[None, None]:
    """The parts of one example line, or nothing when the line has a shape this view does not know."""
    for shape, pattern in SHAPES.get(check, ()):
        if match := pattern.match(text):
            return shape, {k: _unquote(v) for k, v in match.groupdict().items() if v is not None}
    return None, None


def _target(shape: str | None, facts: dict[str, str] | None, column: str | None) -> tuple[str | None, str]:
    """The column and the cell text that mark the exact place of an example in its file."""
    facts = facts or {}
    if shape == "change":
        return facts["field"], facts["field"]
    if shape == "compare":
        return column, facts["id"]
    if shape == "value" and "file" not in facts and "line" not in facts and "first" not in facts:
        return column, facts["value"]
    return column, ""


def _example(check: str, text: str, issue_file: str | None, sheet: str | None, known: set[str],
             column: str | None = None) -> dict:
    """One example line: the row of the issue's own file when it gives one, else the other file it names."""
    shape, facts = _facts(check, text)
    column, find = _target(shape, facts, column)
    row = ROW_RE.search(text)
    other = next(((f, s) for f, s in _files(text, known) if f != issue_file), None)
    if other and not (row and issue_file):
        return {"text": text, "shape": shape, "facts": facts, "source": other[0], "locator": _locator(other[1], None),
                "column": column, "find": find}
    locator = _locator(sheet, row[1] if row else None) if issue_file else ""
    if facts and "line" in facts:
        locator = f"line:{facts['line']}"
    return {"text": text, "shape": shape, "facts": facts, "source": issue_file, "locator": locator,
            "column": column, "find": find}


def _headline(message: str) -> dict:
    """A short title and one line of detail for a report message; the number of cases is its leading count."""
    match = COUNT_RE.match(message)
    count = int(match[1]) if match else None
    for pattern, title, detail in HEADLINES:
        if found := pattern.search(message):
            parts = {k: v or "" for k, v in found.groupdict().items()}
            return {"count": count, "title": title.format(**parts), "detail": detail.format(**parts)}
    text = (message[match.end():] if match else message).replace("(s)", "" if count == 1 else "s")
    return {"count": count, "title": text[:1].upper() + text[1:], "detail": ""}


def _finding(issue: Issue, known: set[str]) -> dict:
    source, sheet = _place(issue, known)
    column = None if issue.check == "format" else issue.where or None
    examples = [_example(issue.check, text, source, sheet, known, column) for text in issue.examples]
    stopped = LAST_DAY_RE.search(issue.message)
    if stopped:
        for example in examples:
            example["find"] = stopped[1]
    related = sorted({f for text in (issue.message, *issue.examples) for f, _ in _files(text, known)} - {source})
    return {"severity": issue.severity, "check": issue.check, "message": issue.message, **_headline(issue.message),
            "source": source, "label": issue.path, "sheet": sheet, "column": issue.where or None,
            "locator": _locator(sheet, None), "related": related, "examples": examples}


def _counts(findings: list[dict]) -> dict:
    return {s: sum(f["severity"] == s for f in findings) for s in ("error", "warn", "info")}


def _replaced(root: Path, known: set[str]) -> list[Issue]:
    """Files the portal published again with other content: the source corrected them."""
    try:
        lines = (root / CHANGELOG).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    issues = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("kind") == "changed" and event.get("path") in known:
            stamp = event.get("detected_at") or event.get("ts") or ""
            issues.append(Issue("portal", "info", event["path"],
                                f"the source replaced this file{' on ' + stamp[:10] if stamp else ''}; "
                                "earlier figures from it may be out of date"))
    return issues


def quality_view(data_root: Path | str | None, *, as_of: date | None = None) -> dict:
    """The quality report of the mirror under data_root, ready for the screen, with every file checked."""
    root = Path(data_root).resolve() if data_root else None
    if root is None or not root.is_dir():
        return {"available": False, "files": 0, "tables": 0, "counts": {}, "kinds": [], "folders": []}
    report = build_report(root, as_of=as_of)
    files = [p.relative_to(root).as_posix() for p in iter_files(root)]
    known = set(files) | {p.relative_to(root).as_posix() for p in root.glob(".*") if p.is_file()}
    findings = [_finding(issue, known) for issue in [*report.issues, *_replaced(root, known)]]
    kinds = []
    for key, title, note, checks in KINDS:
        chosen = [f for f in findings if f["check"] in checks]
        kinds.append({"key": key, "title": title, "note": note, "findings": chosen, "counts": _counts(chosen)})
    grouped = {c for *_, checks in KINDS for c in checks}
    other = [f for f in findings if f["check"] not in grouped]
    if other:
        kinds.append({"key": "other", "title": "Other findings", "note": "Findings of checks this view does not group yet.",
                      "findings": other, "counts": _counts(other)})
    per_file: dict[str, int] = defaultdict(int)
    for f in findings:
        for path in {f["source"], *f["related"]} - {None}:
            per_file[path] += 1
    folders: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(files):
        folders["/".join(path.split("/")[:2]) if path.count("/") > 1 else path.split("/")[0]].append(
            {"path": path, "findings": per_file.get(path, 0)})
    return {"available": True, "as_of": report.as_of, "files": report.files, "tables": report.tables,
            "counts": _counts(findings), "kinds": kinds,
            "folders": [{"folder": name, "files": items, "findings": sum(i["findings"] > 0 for i in items)}
                        for name, items in folders.items()]}
