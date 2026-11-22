"""Resolve Board provenance and render the cited region of an archived document."""
import io
import re
from pathlib import PurePosixPath

from openpyxl import load_workbook
from pypdf import PdfReader

from gtl.ingest.quality import cell_str, norm_header
from gtl.read.document_reader import read_docx
from gtl.read.table_reader import parse_json, parse_tables

TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".xlsx", ".xlsm"})
CONTEXT_ROWS = 3
MAX_CELL_CHARS = 80


def table_excerpt(source: str, content: bytes, locator: str, column: str, find: str) -> dict | None:
    """The rows around a cited place as a table, with the cited cells marked.

    The place is the row of the locator, else the first row that holds `find`. `column` names the cell
    in that row; without it the cells equal to `find` are marked. None when the file holds no such table.
    """
    if PurePosixPath(source).suffix.lower() not in TABLE_SUFFIXES or not (locator or column or find):
        return None
    parts = dict(part.split(":", 1) for part in locator.split(";") if ":" in part)
    number = re.search(r"\d+$", parts.get("row") or parts.get("cell") or "")
    wanted = norm_header(column) if column else ""
    tables = [t for t in parse_tables(source, content).tables if not parts.get("sheet") or t.sheet == parts["sheet"]]
    best = None
    for table in tables:
        names = [norm_header(h) for h in table.headers]
        # A change table may hold the new value of a field in a column of its own.
        index = next((names.index(n) for n in (wanted, wanted + "_new") if wanted and n in names), None)
        cells = [[cell_str(row.cells.get(h)).strip() for h in table.headers] for row in table.rows]

        def holds(values: list[str]) -> bool:
            return bool(find) and (values[index] == find if index is not None and not number else find in values)

        if number:
            anchor = next((i for i, row in enumerate(table.rows) if row.number == int(number[0])), None)
        else:
            anchor = next((i for i, values in enumerate(cells) if holds(values)), None)
            if anchor is None and find:
                anchor = next((i for i, values in enumerate(cells) if find in values), None)
        if anchor is None and (number or find or index is None):
            continue
        start = max(0, (anchor or 0) - CONTEXT_ROWS)
        rows = []
        for i in range(start, min(len(cells), (anchor or 0) + CONTEXT_ROWS + 1)):
            hit = anchor is not None and (i == anchor or (not number and holds(cells[i])))
            if not hit:
                marked = []
            elif index is not None:
                marked = [index]
            else:
                marked = [j for j, value in enumerate(cells[i]) if value == find]
            rows.append({"number": table.rows[i].number, "cells": [v[:MAX_CELL_CHARS] for v in cells[i]],
                         "hit": hit, "marked": marked})
        found = {"sheet": table.sheet, "headers": [str(h) for h in table.headers], "column": index,
                 "rows": rows, "located": anchor is not None}
        if anchor is not None and (index is not None or not wanted):
            return found
        best = best or found
    return best


def excerpt(source: str, content: bytes, locator: str, quote: str) -> dict:
    suffix = PurePosixPath(source).suffix.lower()
    anchor = None
    if suffix == ".pdf":
        pages = PdfReader(io.BytesIO(content)).pages
        page = re.match(r"page:([1-9]\d*)", locator)
        index = int(page[1]) - 1 if page else 0
        text = pages[index].extract_text() or ""
    elif suffix == ".docx":
        text = read_docx(content)
    elif suffix == ".xlsx":
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            sheet, _, cell = locator.removeprefix("sheet:").partition(";")
            rows = book[sheet].values
            row = re.fullmatch(r"(?:cell:[A-Z]+|row:)([1-9]\d*)", cell)
            anchor = int(row[1]) - 1 if row else None
            text = "\n".join(" | ".join(str(v) if v is not None else "" for v in values).rstrip(" |")
                             for values in rows)
        finally:
            book.close()
    elif suffix in {".csv", ".tsv", ".md", ".txt", ".json", ".jsonl", ".xml", ".eml"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252", errors="replace")
        row = re.fullmatch(r"row:([1-9]\d*)", locator)
        line = re.fullmatch(r"line:([1-9]\d*)", locator)
        if row and suffix in {".csv", ".tsv"}:
            anchor = int(row[1]) - 1
        if line:
            anchor = int(line[1]) - 1
        if row and suffix == ".json":
            tables = parse_json(text)
            for table in tables:
                for record in table.rows:
                    if record.row_locator == locator and record.text == quote:
                        return {"before": "", "match": record.text, "after": ""}
    else:
        return {"before": "", "match": "", "after": ""}

    lines = text.splitlines(keepends=True)
    if anchor is not None and 0 <= anchor < len(lines):
        return {"before": "".join(lines[max(0, anchor-2):anchor])[-1500:],
                "match": lines[anchor].rstrip("\r\n"),
                "after": "\n" + "".join(lines[anchor+1:anchor+3])[:1500]}
    words = quote.split()
    match = re.search(r"\s+".join(map(re.escape, words)), text) if words else None
    if match:
        return {"before": text[max(0, match.start()-1500):match.start()],
                "match": match[0], "after": text[match.end():match.end()+1500]}
    return {"before": text[:3000], "match": "", "after": ""}
