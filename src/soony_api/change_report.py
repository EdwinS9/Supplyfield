"""A local, human-readable comparison of two portal snapshots."""

from __future__ import annotations

import csv
import difflib
import hashlib
import html
import io
import json
import os
import re
from itertools import islice
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from xml.etree import ElementTree
from zipfile import ZipFile

from .events import TEXT_SUFFIXES, CsvChange, decode, now_iso, profile_csv
from .resources.files import safe_local_path, sha256_file


def report_path(root: Path) -> Path:
    return root.with_name(f"{root.name}-changes.md")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as tmp:
        staged = Path(tmp.name)
    try:
        staged.write_text(body, encoding="utf-8")
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def freeze_baseline(report: Path, source: Path, manifest: dict) -> None:
    path = report.with_suffix(".baseline.json")
    if not path.exists():
        _write(path, json.dumps({"source": str(source), "captured": now_iso(),
                                 "files": {p: v["sha256"] for p, v in manifest.items()}}))


def _literal(value: str, limit: int = 420) -> str:
    """Quote untrusted source text as a Markdown code span."""
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    fence = "`" * (1 + max((len(x) for x in re.findall(r"`+", value)), default=0))
    return f"{fence} {value} {fence}"


def _preview(path: Path, previous: Path | None, highlights: list[str]) -> list[str]:
    suffix = path.suffix.lower()
    if path.stat().st_size > 8_000_000:
        return ["Open the source to inspect this large file."]
    if suffix in TEXT_SUFFIXES:
        data = path.read_bytes()
        text = decode(data)[0]
        old = previous.read_bytes() if previous and previous.stat().st_size <= 8_000_000 else None
        lines = []
        if suffix in {".csv", ".tsv"}:
            profile = profile_csv(data)
            change = CsvChange.compare(profile_csv(old) if old is not None else None, profile)
            lines.append(_literal(change.summary()))
            if old is None:
                rows = csv.reader(io.StringIO(text), delimiter=profile.delimiter)
                header = next(rows, [])
                for row in islice(rows, 4):
                    record = dict(zip(header, row))
                    note = record.get("reason") or record.get("note")
                    if note:
                        subject = record.get("child_id") or record.get("component_id") or row[0]
                        highlights.append(f"{subject}: {note}")
                    lines.append(_literal(", ".join(f"{k}={v}" for k, v in record.items() if v)))
                return lines
        if old is not None:
            diff = difflib.unified_diff(decode(old)[0].splitlines(), text.splitlines(), n=0)
            next(diff, None)
            next(diff, None)
            edits = (s for s in diff if s.startswith(("-", "+")))
            lines.extend(_literal(s) for s in islice(edits, 8))
        elif suffix == ".json":
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                for item in obj["items"][:4]:
                    if isinstance(item, dict) and item.get("headline"):
                        highlights.append(str(item["headline"]))
                        lines.append(_literal(f"{item['headline']}. {item.get('summary', '')}"))
            elif isinstance(obj, dict) and isinstance(obj.get("supplier"), dict):
                vendor = obj["supplier"]
                fields = [str(vendor[k]) for k in ("name", "status", "notes") if vendor.get(k)]
                if fields:
                    lines.append(_literal(" · ".join(fields)))
                if vendor.get("name") and vendor.get("notes"):
                    highlights.append(f"{vendor['name']}: {vendor['notes']}")
            if not lines:
                lines = [_literal(json.dumps(obj, ensure_ascii=False))]
        else:
            lines.append(_literal(text))
        return lines or ["The file bytes changed. Open the source for details."]
    if suffix == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(path).pages
        text = pages[0].extract_text() if len(pages) else ""
        return [_literal(text, 1100)] if text.strip() else ["No text was available on the first page."]
    if suffix == ".docx":
        with ZipFile(path) as doc:
            xml = ElementTree.fromstring(doc.read("word/document.xml"))
        paragraphs = []
        for para in xml.findall(".//{*}p"):
            parts = []
            for node in para.iter():
                if node.tag.endswith("}t"):
                    parts.append(node.text or "")
                elif node.tag.endswith(("}br", "}tab")):
                    parts.append("\n")
            paragraphs.extend("".join(parts).splitlines())
        excerpts = [p for p in paragraphs if len(p) >= 100]
        highlights.extend(excerpts[:1])
        return [_literal(p) for p in (excerpts or [p for p in paragraphs if p])[:4]] or [
            "No document text was available."]
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = islice(book.worksheets[0].values, 8)
            return [_literal(" | ".join(str(v) for v in row if v is not None))
                    for row in rows if any(v is not None for v in row)]
        finally:
            book.close()
    return ["Open the source to inspect this file type."]


def _original(source: Path, root: Path, name: str, digest: str) -> Path | None:
    local = safe_local_path(source, name)
    history = safe_local_path(root / ".history", name)
    history = history.with_name(f"{history.stem}.{digest[:8]}{history.suffix}")
    for candidate in (local, history):
        if candidate.is_file() and sha256_file(candidate) == digest:
            return candidate
    return None


def refresh_report(root: Path, report: Path, current: dict) -> None:
    baseline = json.loads(report.with_suffix(".baseline.json").read_text(encoding="utf-8"))
    before = baseline["files"]
    after = {p: entry["sha256"] for p, entry in current.items()}
    delta = sorted((p, before.get(p), after.get(p)) for p in before.keys() | after.keys()
                   if before.get(p) != after.get(p))
    signature = hashlib.sha256(json.dumps([baseline["captured"], delta]).encode()).hexdigest()
    marker = f"<!-- portal report v1 {signature} -->"
    if report.exists():
        with report.open(encoding="utf-8") as existing:
            if existing.readline().rstrip() == marker:
                return

    source = Path(baseline["source"])
    added = sum(old is None for _, old, _ in delta)
    removed = sum(new is None for _, _, new in delta)
    lines = [marker, "# Portal data changes", "",
             f"**{added} added · {len(delta) - added - removed} changed · {removed} removed**", "",
             f"Compared with the {len(before)}-file snapshot of {_literal(source.name)} "
             f"saved {baseline['captured']}. Report updated {now_iso()}.", ""]
    headings_end = len(lines)
    highlights = []
    if not delta:
        lines.append("The mirrored files match the baseline.")
    for name, old, new in delta:
        local = safe_local_path(root, name)
        kind = "Removed" if new is None else "Added" if old is None else "Changed"
        lines.extend([f"## {kind} · {_literal(name, 1000)}", ""])
        if new is not None:
            href = quote(os.path.relpath(local, report.parent), safe="/.")
            lines.extend([f"[Open source]({href})", ""])
            try:
                previous = _original(source, root, name, old) if old else None
                if old and previous is None:
                    lines.append("Earlier content unavailable. Showing the current version.")
                notes = []
                details = _preview(local, previous, notes)
                if notes:
                    excerpt = " ".join(notes[0].split())[:320]
                    escaped = re.sub(r"([\\`*_\[\]~|])", r"\\\1", html.escape(excerpt, quote=False))
                    highlights.append(f"- {escaped} [Source]({href})")
            except Exception:  # A malformed customer file must not interrupt the poll.
                details = ["Content preview unavailable. Open the source to inspect it."]
            lines.extend(f"- {detail}" for detail in details)
        else:
            lines.append("This path disappeared from the portal listing.")
        lines.append("")
    if highlights:
        lines[headings_end:headings_end] = ["## Highlights from source records", "", *highlights[:6], ""]
    _write(report, "\n".join(lines).rstrip() + "\n")
