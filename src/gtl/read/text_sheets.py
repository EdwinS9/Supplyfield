"""Text route for tabular files: sheets and notes that are not clean tables.

A sheet with weekday names and time ranges, a notes sheet or a markdown file is not a table of records.
It is rendered as plain text (one line per row with cell references) and read by the document reader's
text route, which returns claims with verbatim quotes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Callable

from gtl.contracts import Claim, SourceKind
from gtl.read.document_reader import read_document
from gtl.read.prompts import SHEET_HINTS

# The entities the text route may emit. Other entities come from tables and documents.
TEXT_ENTITIES = ("product", "line", "shift", "observation")
MAX_SHEET_ROWS = 80


@dataclass(frozen=True)
class TextBlock:
    label: str  # evidence locator of the whole block, e.g. "sheet:Shifts"
    text: str


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def render_sheet(name: str, grid: list[list[Any]], column_letter: Callable[[int], str]) -> TextBlock:
    """One line per non-empty row: "row 4: A4=Standard | B4=FG-1". `grid` row i is spreadsheet row i + 1."""
    lines = [f"sheet: {name}"]
    shown = 0
    for index, row in enumerate(grid, 1):
        cells = [f"{column_letter(j + 1)}{index}={cell_text(v)}" for j, v in enumerate(row) if cell_text(v)]
        if not cells:
            continue
        if shown == MAX_SHEET_ROWS:
            lines.append("(rows cut)")
            break
        lines.append(f"row {index}: " + " | ".join(cells))
        shown += 1
    return TextBlock(f"sheet:{name}", "\n".join(lines))


def render_markdown(text: str) -> TextBlock:
    return TextBlock("page:1", text)


class TextSheetReader:
    """Reads rendered sheets and notes into claims through the document reader's text route."""

    def __init__(self, llm: Any, *, known: dict[str, Any] | None = None, now_iso: str | None = None,
                 author: str = "table_reader", source_kind: SourceKind = "config") -> None:
        self.llm, self.known, self.now_iso = llm, known, now_iso
        self.author, self.source_kind = author, source_kind

    def read(self, path: str, data: bytes, blocks: list[TextBlock],
             on_error: Callable[[str], None] | None = None,
             on_raw: Callable[[str], None] | None = None) -> list[Claim]:
        if not blocks:
            return []
        claims = read_document(path, data, self.llm, known=self.known, now_iso=self.now_iso,
                               on_error=on_error, on_raw=on_raw, text=[b.text for b in blocks],
                               labels=[b.label for b in blocks], hints=SHEET_HINTS)
        return [replace(c, author=self.author, source_kind=self.source_kind)
                for c in claims if c.entity in TEXT_ENTITIES]
