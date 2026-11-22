"""table_reader: tabular files (csv, xlsx, json) and markdown notes to claims.

An LLM maps the headers of a table onto the canonical vocabulary (`ENTITIES`)
once per header signature. Mappings are looked up in this order: the reviewed
files under `tenants/<tenant id>/generated/mappings/`, then the runtime cache
`<cache_dir>/mappings/`, then the model (and the answer goes to the runtime
cache; `promote_mappings` copies it to the tenant folder for review). Code does
the rest: delimiter, encoding, decimal comma, dates, lists, canonical values,
locators. There is no file-specific parser here.

Content that is not a table of records (a sheet with weekday names and time
ranges, a notes sheet, a title line with a status date, a markdown file) is
rendered as text and read through the document reader's text route.

Pure function first: `read_table(path, data, llm, ...) -> list[Claim]`.
The agent wrapper at the bottom subscribes to `event:file.changed`.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

from gtl.config import tenant_generated_dir
from gtl.contracts import (ENTITIES, LLM, VALUES, Claim, Context, Evidence, Post, make_key,
                           normalize_value)
from gtl.read.document_reader import (QUANTITY_FIELDS, parse_clock, parse_date, parse_incoterm,
                                      parse_number, parse_revision, resolve_status_datetime,
                                      zone_info)
from gtl.read.text_sheets import TextBlock, TextSheetReader, render_markdown, render_sheet

TABLE_EXTENSIONS = (".csv", ".tsv", ".json", ".xlsx", ".xlsm", ".md")
DEFAULT_CACHE_DIR = Path("var")
DEFAULT_TENANTS_ROOT = "tenants"
DEFAULT_TZ = "Europe/Berlin"
MAX_HEADER_CHARS = 40  # a header cell longer than this is a sentence, not a column name
MAX_DISTINCT_SAMPLES = 20  # distinct values per column shown to the mapping model
MAX_EXAMPLE_IDS = 8
MAX_OBSERVATIONS = 20  # unusable values reported per file

NUMERIC_FIELDS = frozenset({
    "demand_units", "preorder_units", "packed_units", "qty_per", "rate_per_hour", "qty",
    "qty_ordered", "qty_received", "gross_weight_kg", "unit_price", "qty_shipped", "units",
    "lot_qty", "defect_rate", "surcharge_pct", "ready_hours", "transit_hours",
    "lead_time_days", "share", "transit_days",
})
DATE_FIELDS = frozenset({
    "deadline", "date", "promised_date", "confirmed_date", "etd", "eta", "eta_site",
    "status_updated_at", "valid_until", "release_date", "release_valid_until", "valid_from",
    "start", "end",
})
LIST_FIELDS = frozenset({"part_ids", "po_numbers", "approver_roles"})
BOOL_FIELDS = frozenset({"vacant", "use_up_old_revision", "critical"})
REVISION_FIELDS = frozenset({"revision", "required_revision"})
TRANSFORM_OPS = ("number", "date", "split", "pad_prefix", "strip_zeros", "upper", "lower", "map", "strip")
_ISO_START = re.compile(r"\d{4}-\d{2}-\d{2}")

MAPPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["targets"],
    "properties": {
        "source_kind": {"type": "string", "enum": ["erp", "document", "tracker", "config"]},
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entity", "key", "fields"],
                "properties": {
                    "entity": {"type": "string"},
                    "key": {"type": "array", "items": {"type": "string"}},
                    "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                    "where": {"type": "object", "additionalProperties": {"type": "string"}},
                    "as_of": {"type": ["string", "null"]},
                    "units": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        },
        "transforms": {"type": "object"},
    },
}
REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["transforms"],
    "properties": {"transforms": {"type": "object"}},
}

SYSTEM_PROMPT = """You map the columns of a business table onto a fixed vocabulary of entities.
Reply with JSON only, shaped like:
{"source_kind": "erp|tracker|config",
 "targets": [{"entity": "<entity>", "key": ["<column>", ...], "fields": {"<field>": "<column>"},
              "as_of": "<date column or null>", "units": {"<field>": "<column or const:UNIT>"}}],
 "transforms": {"<column>": {"op": "<op>", ...}}}
Rules:
- Only use entities and fields from the vocabulary. One table may feed several targets. When a
  reference table matches no entity in the vocabulary (such as a list of ports or sites/facilities),
  return "targets": [] instead of inventing an entity or forcing columns onto an unrelated one.
- "key" lists the parts of the entity key in order. A part is a column name, "path:<regex>" (the
  first match in the file path, group 1 when there is a group; use it when an id such as the supplier
  sits in the folder name, with a regex matching the id format listed under EXAMPLE IDS), or "const:<text>".
- Map a field only when a column clearly holds it. Never invent columns. Map an id field (supplier_id,
  part_id, ...) only from a column that holds ids; a column of names is not an id.
- For stock use the site, part and actual batch columns. With no batch column use an empty
  final key part ("const:"). Never invent a batch from a heading, sheet name or file path.
- A sheet of finished products with a packed or produced quantity feeds product.packed_units,
  keyed by the product ID. Map the total quantity, not a daily increment. Known product IDs
  (see EXAMPLE IDS) must not become stock parts. The title may identify what the quantity measures.
- A dated snapshot title dates its rows. Otherwise use a column that dates the value itself
  (snapshot date, valid from, status date). Never use last physical count or last updated by.
- A field may take a value derived from a column: "regex:<column>:<pattern>" gives the first group of
  the pattern matched in that column (no group: the whole match), or no value when it does not match.
  Worked example for a supplier components file whose note says "fitted by supplier into <PART-ID>"
  (match the supplier id format shown under EXAMPLE IDS, here illustrated with a synthetic V\\d{6}):
  {"source_kind": "config",
   "targets": [
     {"entity": "supplier_part", "key": ["path:(V\\d{6})", "component_id"],
      "fields": {"unit_price": "unit_price", "currency": "currency", "lead_time_days": "lead_time_days",
                 "share": "share", "note": "note"}},
     {"entity": "part", "key": ["component_id"],
      "fields": {"category": "category", "supplied_fitted_into": "regex:note:fitted .*?into ([A-Za-z0-9-]+)"}}
   ]}
- A supplier profile feeds the supplier entity: name, contact_email, language (preferred language),
  release_status (the qualification column, not an active/inactive column), release_date,
  release_valid_until, incoterm. Supplier price lists (Material, New price, Currency, Valid from)
  feed supplier_part (key ["path:(<regex matching the supplier id in EXAMPLE IDS>)", "Material"],
  unit_price from New price, currency, as_of from Valid from). Supplier component rows feed supplier_part and, when a note says a part is
  fitted into another part, the part entity with regex:note:... as shown above.
- Shipments: origin, destination, dest_site, etd, eta, status, status_updated_at, note, qty,
  part_ids and po_numbers (a cell that lists several ids is split by the reader). Products: name;
  demand_units is the day-one or launch demand (a column like day_one_qty or "Day-one demand");
  packed_units only from a column of copies already packed or produced; a weekly or periodic plan
  quantity (weekly_plan_qty) is neither, leave it unmapped; deadline is the launch or on-sale date
  (launch_date), never the date a status was taken; a valid_from column is the row's as_of.
  Routes, receipts, stock, pack-out, bom and parts (with category, critical, use_up_old_revision)
  map the same way.
- Delta and change-log tables: when a table has old and new columns (e.g. rev_old/rev_new,
  qty_per_old/qty_per_new), map the NEW column. When rows name the changed field in one column and
  the new value in another (e.g. field/old/new), map the new-value column and set
  "where": {"<field column>": "<raw field name>"} on each target so only matching rows feed it.
- Status-like fields (listed under ALLOWED VALUES) must end up with exactly one of the allowed values.
  The reader lower-cases and joins words with "_" ("In Transit" -> "in_transit"). When a column holds
  other spellings or codes, add a "map" transform for that column with an entry for EVERY distinct
  value listed under DISTINCT VALUES that is not already an allowed value after that cleanup. A value
  with no sensible allowed value is left out of the map.
- Incoterms may be written with a place ("FOB Busan"); map the column as it is, the reader keeps the
  three-letter code. Revisions likewise ("rev. B" -> "B").
- Supplier or part ids written as bare numbers in one file but with a prefix elsewhere (see EXAMPLE
  IDS) need a pad_prefix transform with the prefix and width of the examples.
- transforms ops: number (decimal comma etc, optional "decimal"), date (python "format"),
  split ("sep"), pad_prefix ("prefix", "width": turn 19 into "ID-0019"), strip_zeros,
  upper, lower, strip, map ("values": {"raw": "canonical"}). One transform per column.
- Use transforms only when the raw values need them. Plain ISO dates and plain numbers need none.
"""

REPAIR_SYSTEM = """A column was mapped to a status-like field, but some of its values are not allowed values.
Reply with JSON only: {"transforms": {"<column>": {"op": "map", "values": {"<raw value>": "<allowed value>"}}}}
with an entry for every raw value that clearly means one of the allowed values. Leave out values that mean
none of them. Use only the allowed values listed."""

DERIVE_SYSTEM = """Some rows in this table have free-text notes that may state a structured part relationship
(for example, "fitted by supplier into <PART-ID>" -> part.supplied_fitted_into).
Reply with JSON only matching the mapping schema:
{"targets": [{"entity": "part", "key": ["<part id column>"], "fields": {"<field>": "regex:<note column>:<pattern>"}}]}
Use "targets": [] when the notes state no such relationship. Only use columns and entities shown."""


# ---------------------------------------------------------------------------
# 1. Parsing files into tables
# ---------------------------------------------------------------------------
@dataclass
class Row:
    number: int  # 1-based source row (spreadsheet row, file line, record index)
    cells: dict[str, Any]  # header -> raw value
    locators: dict[str, str]  # header -> locator of that cell
    row_locator: str
    text: str  # verbatim row text for the evidence quote


@dataclass
class Table:
    sheet: str
    headers: list[str]
    rows: list[Row] = field(default_factory=list)
    title: str = ""


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def sniff_delimiter(text: str) -> str:
    sample = text[:8192]
    first = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] == 0:
        return ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return best


def _unique_headers(raw: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, h in enumerate(raw):
        name = str(h).strip() if h is not None and str(h).strip() else f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def parse_csv(text: str) -> list[Table]:
    delim = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    headers: list[str] | None = None
    table = Table("", [])
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        if headers is None:
            headers = _unique_headers(raw)
            table.headers = headers
            continue
        n = reader.line_num
        cells = {h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(headers)}
        loc = f"row:{n}"
        table.rows.append(Row(n, cells, {h: loc for h in headers}, loc, delim.join(raw)))
    return [table] if headers else []


def _flatten(rec: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, name + "."))
        elif isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
            out[name] = json.dumps(v, ensure_ascii=False)
        else:
            out[name] = v
    return out


def _find_records(obj: Any) -> list[dict[str, Any]] | None:
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_records(v)
            if found:
                return found
    return None


def parse_json(text: str) -> list[Table]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():  # json lines
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    return []
        obj = records
    records = _find_records(obj)
    if not records and isinstance(obj, dict) and obj and not all(v == [] for v in obj.values()):
        records = [obj]
    if not records:
        return []
    flat = [_flatten(r) for r in records]
    headers: list[str] = []
    for rec in flat:
        for k in rec:
            if k not in headers:
                headers.append(k)
    table = Table("", headers)
    for i, (rec, orig) in enumerate(zip(flat, records), start=1):
        loc = f"row:{i}"
        table.rows.append(Row(i, {h: rec.get(h) for h in headers}, {h: loc for h in headers}, loc,
                              json.dumps(orig, ensure_ascii=False)))
    return [table]


@dataclass
class Parsed:
    tables: list[Table] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)  # content for the text route


def _is_header(row: list[Any]) -> bool:
    """Two or more short strings in adjacent columns."""
    cols = [j for j, c in enumerate(row) if c not in (None, "")]
    if len(cols) < 2 or cols != list(range(cols[0], cols[-1] + 1)):
        return False
    return all(isinstance(row[j], str) and len(row[j].strip()) <= MAX_HEADER_CHARS for j in cols)


def _is_record(row: list[Any], first: int, width: int) -> bool:
    """A data row fills the first column of the table and at least half of all columns."""
    filled = [j for j in range(first, first + width) if j < len(row) and row[j] not in (None, "")]
    return bool(filled) and filled[0] == first and 2 * len(filled) >= width


def _is_group_header(group_row: list[Any], next_row: list[Any]) -> bool:
    """True when `group_row` holds short group labels above a wider real header `next_row`."""
    group_vals = [c for c in group_row if c not in (None, "")]
    if not group_vals or not all(isinstance(c, str) and len(c.strip()) <= MAX_HEADER_CHARS for c in group_vals):
        return False
    if not _is_header(next_row):
        return False
    next_vals = [c for c in next_row if c not in (None, "")]
    return len(next_vals) > len(group_vals)


def _sheet_table(name: str, grid: list[list[Any]]) -> tuple[Table | None, bool]:
    """The sheet as a table (None when it is not a rectangular table of records), and whether a title
    line sits above the header."""
    from openpyxl.utils import get_column_letter

    rows = [i for i, r in enumerate(grid) if any(c not in (None, "") for c in r)]
    if not rows:
        return None, False
    first_cells = [c for c in grid[rows[0]] if c not in (None, "")]
    titled = len(first_cells) == 1 and len(rows) > 1
    title = str(first_cells[0]).strip() if titled else ""
    start = 1 if titled else 0
    if start + 1 < len(rows) and _is_group_header(grid[rows[start]], grid[rows[start + 1]]):
        hdr = rows[start + 1]
    else:
        hdr = rows[start]
    if not _is_header(grid[hdr]):
        return None, titled
    cols = [j for j, c in enumerate(grid[hdr]) if c not in (None, "")]
    first, width = cols[0], len(cols)
    data_rows = [i for i in rows if i > hdr]
    while data_rows and sum(c not in (None, "") for c in grid[data_rows[-1]]) == 1:
        data_rows.pop()  # a footnote under the records is not a record
    if len(data_rows) < 2 or not all(_is_record(grid[i], first, width) for i in data_rows):
        return None, titled
    headers = _unique_headers(grid[hdr][first:first + width])
    table = Table(name, headers, title=title)
    for i in data_rows:
        raw = grid[i]
        n = i + 1
        cells = {h: (raw[first + j] if first + j < len(raw) else None) for j, h in enumerate(headers)}
        locs = {h: f"sheet:{name};cell:{get_column_letter(first + j + 1)}{n}" for j, h in enumerate(headers)}
        text = " | ".join("" if c is None else str(c) for c in raw[first:first + width])
        table.rows.append(Row(n, cells, locs, f"sheet:{name};row:{n}", text))
    return table, titled


def parse_xlsx(data: bytes) -> Parsed:
    """Sheets that are clean tables (a title line above the header is allowed) become tables. Sheets
    that are not, and sheets with a title line, are also rendered as text for the text route."""
    import openpyxl
    from openpyxl.utils import get_column_letter

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    parsed = Parsed()
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        table, titled = _sheet_table(ws.title, grid)
        if table is not None:
            parsed.tables.append(table)
        if table is None or titled:
            block = render_sheet(ws.title, grid, get_column_letter)
            if block.text.count("\n"):
                parsed.blocks.append(block)
    return parsed


def parse_tables(path: str, data: bytes) -> Parsed:
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx(data)
    text = decode_text(data)
    if ext == ".md":
        return Parsed(blocks=[render_markdown(text)] if text.strip() else [])
    if ext == ".json":
        return Parsed(tables=parse_json(text))
    if ext in (".csv", ".tsv"):
        return Parsed(tables=parse_csv(text))
    return Parsed()


# ---------------------------------------------------------------------------
# 2. Header mapping (LLM, stored per header signature)
# ---------------------------------------------------------------------------
def header_signature(headers: list[str]) -> str:
    return json.dumps(sorted(h.strip().lower() for h in headers))


def signature_hash(headers: list[str]) -> str:
    return hashlib.sha256(header_signature(headers).encode()).hexdigest()[:16]


def _resolve_column(name: Any, headers: list[str]) -> str | None:
    if not isinstance(name, str):
        return None
    if name in headers:
        return name
    low = {h.lower(): h for h in headers}
    return low.get(name.strip().lower())


def _regex_source(rest: str, headers: list[str]) -> tuple[str, str] | None:
    """Split "<column>:<pattern>" (the text after "regex:") into a real column and a valid pattern."""
    for header in sorted(headers, key=len, reverse=True):
        if rest.lower().startswith(header.lower() + ":"):
            pattern = rest[len(header) + 1:]
            try:
                re.compile(pattern)
            except re.error:
                return None
            return header, pattern
    return None


def _resolve_source(raw: Any, headers: list[str]) -> str | None:
    """A field source: a column, or "regex:<column>:<pattern>" with the column resolved."""
    if isinstance(raw, str) and raw.startswith("regex:"):
        split = _regex_source(raw[6:], headers)
        return f"regex:{split[0]}:{split[1]}" if split else None
    return _resolve_column(raw, headers)


def split_source(source: str, headers: list[str]) -> tuple[str, re.Pattern[str] | None]:
    """(column, pattern or None) of a validated field source."""
    if source.startswith("regex:"):
        split = _regex_source(source[6:], headers)
        if split:
            return split[0], re.compile(split[1])
    return source, None


def validate_mapping(raw: Any, headers: list[str]) -> dict[str, Any]:
    """Keep only what refers to real entities, fields and columns."""
    out: dict[str, Any] = {"source_kind": None, "targets": [], "transforms": {}}
    if not isinstance(raw, dict):
        return out
    if raw.get("source_kind") in ("erp", "document", "tracker", "config", "sim"):
        out["source_kind"] = raw["source_kind"]
    transforms = raw.get("transforms") if isinstance(raw.get("transforms"), dict) else {}
    for col, spec in transforms.items():
        real = _resolve_column(col, headers)
        if isinstance(spec, str):
            spec = {"op": spec}
        if real and isinstance(spec, dict) and spec.get("op") in TRANSFORM_OPS:
            out["transforms"][real] = spec
    for t in raw.get("targets") or []:
        if not isinstance(t, dict) or t.get("entity") not in ENTITIES:
            continue
        entity = t["entity"]
        valid_fields = ENTITIES[entity][1]
        raw_key = t.get("key") or []
        key: list[str] = []
        for idx, part in enumerate(raw_key):
            if entity == "stock" and idx == 2 and (part in ("", None) or (isinstance(part, str) and part.startswith("path:"))):
                key.append("const:")
            elif isinstance(part, str) and (part.startswith("path:") or part.startswith("const:")):
                key.append(part)
            else:
                col = _resolve_column(part, headers)
                if col:
                    key.append(col)
        if not key or len(key) != len(raw_key):
            continue
        if entity == "stock" and len(key) == 2:
            key.append("const:")
        fields: dict[str, str] = {}
        for fname, col in (t.get("fields") or {}).items():
            real = _resolve_source(col, headers)
            if fname in valid_fields and real:
                fields[fname] = real
        if not fields:
            continue
        units: dict[str, str] = {}
        for fname, u in (t.get("units") or {}).items():
            if fname in fields and isinstance(u, str):
                real = _resolve_column(u, headers)
                units[fname] = real or (u[6:] if u.startswith("const:") else u)
        where: dict[str, str] = {}
        raw_where = t.get("where")
        if raw_where is not None:
            if not isinstance(raw_where, dict):
                continue
            bad_where = False
            for wcol, wval in raw_where.items():
                real_w = _resolve_column(wcol, headers)
                if not real_w or wval is None:
                    bad_where = True
                    break
                where[real_w] = str(wval).strip()
            if bad_where:
                continue
        as_of = _resolve_column(t.get("as_of"), headers)
        if as_of and re.search(r"last(?:physical)?count|lastupdated", re.sub(r"[\W_]+", "", as_of.casefold())):
            as_of = None
        out["targets"].append({"entity": entity, "key": key, "fields": fields, "units": units,
                               "where": where, "as_of": as_of})
    return out


def _distinct_values(table: Table) -> dict[str, list[str]]:
    """Up to MAX_DISTINCT_SAMPLES distinct values of each column that looks like a code or a status."""
    if len(table.rows) <= 5:
        return {}  # the sample rows already show everything
    out: dict[str, list[str]] = {}
    for h in table.headers:
        values = sorted({t for r in table.rows if (t := as_text(r.cells.get(h)))})
        if 1 <= len(values) <= MAX_DISTINCT_SAMPLES and all(len(v) <= MAX_HEADER_CHARS for v in values) \
                and not all(re.fullmatch(r"[\d.,\s-]+", v) for v in values):
            out[h] = values
    return out


def _mapping_prompt(path: str, table: Table, example_ids: dict[str, list[str]] | None) -> str:
    vocab = {e: {"key": spec[0], "fields": list(spec[1])} for e, spec in ENTITIES.items()
             if e != "observation"}
    sample = [{h: ("" if v is None else str(v)) for h, v in r.cells.items()} for r in table.rows[:5]]
    lines = [
        f"FILE: {path}", f"SHEET: {table.sheet or '-'}",
        f"TITLE: {table.title or '-'}",
        f"HEADERS: {json.dumps(table.headers, ensure_ascii=False)}",
        f"SAMPLE ROWS: {json.dumps(sample, ensure_ascii=False)}",
        f"DISTINCT VALUES: {json.dumps(_distinct_values(table), ensure_ascii=False)}",
        f"ALLOWED VALUES: {json.dumps({k: list(v) for k, v in VALUES.items()})}",
    ]
    if example_ids:
        shown = {k: v[:MAX_EXAMPLE_IDS] for k, v in example_ids.items() if v}
        lines.append(f"EXAMPLE IDS (as written in other files): {json.dumps(shown)}")
    lines.append(f"VOCABULARY: {json.dumps(vocab)}")
    return "\n".join(lines)


def _row_matches(row: Row, where: dict[str, str]) -> bool:
    return all((as_text(row.cells.get(col)) or "").lower() == val.lower() for col, val in where.items())


def _unmapped_values(mapping: dict[str, Any], table: Table) -> dict[str, dict[str, list[str]]]:
    """Per status-like field, the raw values of its column that stay outside VALUES after the transform
    and the cleanup: {"entity.field": {column: [raw values]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    for target in mapping["targets"]:
        where = target.get("where") or {}
        for fname, source in target["fields"].items():
            name = f"{target['entity']}.{fname}"
            col, pattern = split_source(source, table.headers)
            if name not in VALUES or pattern is not None:
                continue
            spec = mapping["transforms"].get(col)
            bad = sorted({raw for r in table.rows
                          if _row_matches(r, where) and (raw := as_text(r.cells.get(col)))
                          and normalize_value(target["entity"], fname, apply_transform(raw, spec))
                          not in VALUES[name]})
            if bad:
                out.setdefault(name, {})[col] = bad
    return out


def _repair(path: str, table: Table, mapping: dict[str, Any], llm: LLM) -> dict[str, Any]:
    """One more question to the model for values that are still not allowed. Never raises."""
    unmapped = _unmapped_values(mapping, table)
    if not unmapped:
        return mapping
    prompt = (f"FILE: {path}\nALLOWED VALUES: "
              f"{json.dumps({k: list(VALUES[k]) for k in unmapped})}\n"
              f"VALUES NOT ALLOWED YET: {json.dumps(unmapped, ensure_ascii=False)}")
    try:
        answer = llm.complete("reader", REPAIR_SYSTEM, prompt, schema=REPAIR_SCHEMA, max_tokens=1500).data
    except Exception:  # noqa: BLE001 - unmapped values then become observations
        return mapping
    proposed = validate_mapping({"transforms": (answer or {}).get("transforms")}, table.headers)["transforms"]
    wanted = {col for columns in unmapped.values() for col in columns}
    for col, spec in proposed.items():
        if col in wanted and spec.get("op") == "map" and isinstance(spec.get("values"), dict):
            mapping["transforms"][col] = spec
    return mapping


def _mapping_dir(root: Path | str) -> Path:
    return Path(root) / "mappings"


def _load_mapping(folder: Path, name: str, headers: list[str]) -> dict[str, Any] | None:
    try:
        stored = json.loads((folder / name).read_text(encoding="utf-8"))
        raw = stored["mapping"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    mapping = validate_mapping(raw, headers)
    if mapping["targets"]:
        return mapping
    if isinstance(raw, dict) and raw.get("unmapped"):
        return {**mapping, "unmapped": True}
    return None


def get_mapping(path: str, table: Table, llm: LLM, cache_dir: Path | str | None = None, *,
                tenant_id: str | None = None, tenants_root: str = DEFAULT_TENANTS_ROOT,
                example_ids: dict[str, list[str]] | None = None,
                on_error: Callable[[str], None] | None = None,
                on_raw: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The mapping for a table: reviewed tenant file, then runtime cache, then the model."""
    name = f"{signature_hash(table.headers)}.json"
    runtime = _mapping_dir(cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR)
    folders = [runtime]
    if tenant_id:
        folders.insert(0, _mapping_dir(tenant_generated_dir(tenant_id, tenants_root)))
    for folder in folders:
        stored = _load_mapping(folder, name, table.headers)
        if stored is not None:
            return stored
    try:
        result = llm.complete("reader", SYSTEM_PROMPT, _mapping_prompt(path, table, example_ids),
                              schema=MAPPING_SCHEMA, max_tokens=3000)
    except Exception as exc:  # noqa: BLE001 - a failed model call must not stop the dispatcher
        raw = getattr(exc, "raw", None)
        if on_raw is not None and isinstance(raw, str) and raw:
            on_raw(raw[:300])
        if on_error is not None:
            on_error(f"mapping failed for {path}: {type(exc).__name__}: {exc}")
        return validate_mapping(None, table.headers)
    raw_targets = result.data.get("targets") if isinstance(result.data, dict) else None
    mapping = validate_mapping(result.data, table.headers)
    if mapping["targets"]:
        mapping = _repair(path, table, mapping, llm)
    elif raw_targets == []:
        mapping = {**mapping, "unmapped": True}
    if mapping["targets"] or mapping.get("unmapped"):
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            record = {"headers": table.headers, "source": path, "sheet": table.sheet or None,
                      "mapping": mapping}
            (runtime / name).write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return mapping


def _derive_note_targets(path: str, table: Table, mapping: dict[str, Any], llm: LLM,
                         cache_dir: Path | str | None, example_ids: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    """Second pass when a cached part/supplier_part mapping has no regex: target yet, but this file
    actually has non-empty free-text note cells."""
    part_cols = [c for t in mapping["targets"] if t["entity"] in ("part", "supplier_part")
                 for c in t["key"] if c in table.headers]
    if not part_cols:
        return []
    already_regex = {split_source(src, table.headers)[0]
                     for t in mapping["targets"] for src in t["fields"].values() if src.startswith("regex:")}
    note_cols = [h for h in table.headers
                 if h not in already_regex and any(w in h.lower() for w in ("note", "remark", "comment"))]
    rows_with_notes = [r for r in table.rows
                       if any(as_text(r.cells.get(c)) for c in note_cols)]
    if not rows_with_notes:
        return []
    sample_notes = [{h: as_text(r.cells.get(h)) for h in [*part_cols, *note_cols] if as_text(r.cells.get(h))}
                    for r in rows_with_notes[:10]]
    key_hash = hashlib.sha256(
        (signature_hash(table.headers) + json.dumps(sample_notes, sort_keys=True, ensure_ascii=False)).encode()
    ).hexdigest()[:16]
    runtime = _mapping_dir(cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR)
    cache = runtime / f"notes-{key_hash}.json"
    if cache.exists():
        stored = _load_mapping(runtime, cache.name, table.headers)
        return stored["targets"] if stored else []
    prompt = (
        f"FILE: {path}\nHEADERS: {json.dumps(table.headers, ensure_ascii=False)}\n"
        f"PART ID COLUMN: {part_cols[0]}\n"
        f"ROWS WITH NOTES: {json.dumps(sample_notes, ensure_ascii=False)}\n"
        f"PART FIELDS: {json.dumps(list(ENTITIES['part'][1]))}"
    )
    if example_ids:
        prompt += f"\nEXAMPLE IDS: {json.dumps({k: v[:MAX_EXAMPLE_IDS] for k, v in example_ids.items() if v})}"
    try:
        res = llm.complete("reader", DERIVE_SYSTEM, prompt, schema=MAPPING_SCHEMA, max_tokens=1500)
    except Exception:  # noqa: BLE001
        return []
    derived = validate_mapping(res.data, table.headers)
    regex_targets = [t for t in derived["targets"]
                     if any(src.startswith("regex:") for src in t["fields"].values())]
    if regex_targets:
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"headers": table.headers, "source": path,
                                         "mapping": {"targets": regex_targets}}, indent=1, ensure_ascii=False),
                             encoding="utf-8")
        except OSError:
            pass
    return regex_targets


def promote_mappings(tenant_id: str, cache_dir: Path | str = DEFAULT_CACHE_DIR, *,
                     tenants_root: str = DEFAULT_TENANTS_ROOT) -> list[Path]:
    """Copy the runtime mappings into the tenant folder, for a person to review and commit.

    Each copy keeps the header list and source path of the model run and gets a `signature` and a
    `review` flag. A mapping that is already in the tenant folder is never overwritten.
    """
    source = _mapping_dir(cache_dir)
    target = _mapping_dir(tenant_generated_dir(tenant_id, tenants_root))
    copied: list[Path] = []
    for file in sorted(source.glob("*.json")) if source.is_dir() else []:
        destination = target / file.name
        if destination.exists():
            continue
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            record = {"signature": file.stem, "review": "pending", **record}
            target.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            continue
        copied.append(destination)
    return copied


# ---------------------------------------------------------------------------
# 3. Value conversion
# ---------------------------------------------------------------------------
def as_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


_GROUPED_QTY_RE = re.compile(r"^[+-]?[1-9]\d{0,2}([.,])\d{3}(?:\1\d{3})*$")
_INVENTED_BATCH_TOKENS = frozenset({
    "stock", "bookstock", "inventory", "total", "totals", "none", "null", "na", "empty", "unbatched",
    "all", "default", "summary",
})


def _extract_grouped_qty(v: str) -> int | None:
    s = v.strip()
    if _GROUPED_QTY_RE.fullmatch(s):
        return int(s.replace(".", "").replace(",", ""))
    m = re.search(r"[-+]?\d[\d\s.,\u00a0\u202f']*", v)
    if not m:
        return None
    cleaned = re.sub(r"[\s\u00a0\u202f']", "", m.group(0)).rstrip(".,")
    if _GROUPED_QTY_RE.fullmatch(cleaned):
        return int(cleaned.replace(".", "").replace(",", ""))
    return None


def _is_invented_stock_batch(value: str, path: str, table: Table) -> bool:
    tok = re.sub(r"[\W_]+", "", value.casefold())
    if not tok or tok in _INVENTED_BATCH_TOKENS:
        return True
    if len(tok) < 3:
        return False
    sources = (table.sheet, table.title, Path(path).stem)
    return any(tok in re.sub(r"[\W_]+", "", src.casefold()) for src in sources if src)


def to_number(v: Any, decimal: str | None = None, *, quantity: bool = False) -> float | int | None:
    if quantity and isinstance(v, str) and (grouped := _extract_grouped_qty(v)) is not None:
        return grouped
    if decimal is None:
        return parse_number(v, quantity=quantity)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    if v is None:
        return None
    s = re.sub(r"[\s\u00a0\u202f']", "", str(v))
    s = re.sub(r"[^\d,.\-+eE%]", "", s).rstrip("%")
    if not s or s in ("-", "+"):
        return None
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    elif decimal == ".":
        s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def parse_datetime(v: Any, fmt: str | None = None) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = as_text(v)
    if not s:
        return None
    if fmt:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            return None
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_iso(v: Any, fmt: str | None = None, tz: tzinfo | None = None) -> str | None:
    """ISO date, or ISO date-time (with the offset of `tz` when the value has none)."""
    dt = parse_datetime(v, fmt)
    if dt is None:
        return as_text(v)  # keep unparsed text; the caller decides whether it is usable
    if dt.time() == datetime.min.time() and dt.tzinfo is None:
        return dt.date().isoformat()
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt.isoformat()


def split_list(v: Any, sep: str | None = None) -> list[str]:
    s = as_text(v)
    if not s:
        return []
    parts = s.split(sep) if sep else re.split(r"[|;,]", s)
    return [p.strip() for p in parts if p.strip()]


def apply_transform(v: Any, spec: dict[str, Any] | None, *, quantity: bool = False) -> Any:
    if not spec or v is None:
        return v
    op = spec.get("op")
    if op == "number":
        return to_number(v, spec.get("decimal"), quantity=quantity)
    if op == "date":
        return to_iso(v, spec.get("format"))
    if op == "split":
        return split_list(v, spec.get("sep"))
    s = as_text(v)
    if s is None:
        return None
    if op == "pad_prefix":
        digits = re.sub(r"\D", "", s)
        return f"{spec.get('prefix', '')}{digits.zfill(int(spec.get('width', 0)))}" if digits else s
    if op == "strip_zeros":
        return s.lstrip("0") or "0"
    if op == "upper":
        return s.upper()
    if op == "lower":
        return s.lower()
    if op == "strip":
        return s.strip()
    if op == "map":
        values = spec.get("values") or {}
        return values.get(s, values.get(s.lower(), s))
    return v


def convert_field(entity: str, fname: str, v: Any, spec: dict[str, Any] | None,
                  tz: tzinfo | None = None) -> tuple[Any, bool]:
    """(value in contract form, usable). Not usable means the source had a value that cannot be put
    into the vocabulary (a status outside VALUES, a date that is not a date); the caller reports it.
    An empty source or a number that is not a number gives (None, True): there is nothing to claim."""
    quantity = fname in QUANTITY_FIELDS
    v = apply_transform(v, spec, quantity=quantity)
    if v is None or v == "" or v == []:
        return None, True
    if fname in NUMERIC_FIELDS:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return (int(v) if float(v).is_integer() else float(v)), True
        return to_number(v, quantity=quantity), True
    if entity == "shift" and fname in ("start", "end"):
        clock = parse_clock(v)
        return clock, clock is not None
    if fname in DATE_FIELDS:
        iso = to_iso(v, None, tz)
        usable = bool(iso and _ISO_START.match(iso))
        return (iso if usable else None), usable
    if fname in LIST_FIELDS:
        return (v if isinstance(v, list) else split_list(v)), True
    if fname in BOOL_FIELDS:
        return as_text(v).lower() in ("y", "yes", "true", "1", "x", "vacant", "tbd"), True  # type: ignore[union-attr]
    if isinstance(v, list):
        return v, True
    text = as_text(v)
    if text is None:
        return None, True
    if fname == "incoterm":
        return parse_incoterm(text), True
    if fname in REVISION_FIELDS:
        return parse_revision(text), True
    value = normalize_value(entity, fname, text)
    allowed = VALUES.get(f"{entity}.{fname}")
    if allowed is not None and value not in allowed:
        return None, False
    return value, True


def key_part(part: str, row: Row, path: str, transforms: dict[str, Any]) -> str | None:
    if part.startswith("const:"):
        return part[6:]
    if part.startswith("path:"):
        m = re.search(part[5:], path)
        return (m.group(1) if m.groups() else m.group(0)) if m else None
    v = apply_transform(row.cells.get(part), transforms.get(part))
    if isinstance(v, list):
        return None
    return as_text(v)


# ---------------------------------------------------------------------------
# 4. read_table
# ---------------------------------------------------------------------------
def _source_kind(path: str, mapped: str | None) -> str:
    if mapped:
        return mapped
    p = path.lower()
    if "logistics" in p or "tracker" in p:
        return "tracker"
    if "/erp/" in p or p.startswith("erp/"):
        return "erp"
    return "config"


def _as_of(value: Any, default: str, tz: tzinfo) -> str:
    dt = parse_datetime(value)
    if dt is None:
        return default
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).isoformat(timespec="seconds")

@dataclass
class _Unusable:
    """A source value that cannot go into the vocabulary; reported as an observation."""
    entity: str
    key: str
    field: str
    raw: str
    locator: str
    quote: str


def _table_claims(path: str, digest: str, table: Table, mapping: dict[str, Any], default_as_of: str,
                  zone: tzinfo, author: str, unusable: list[_Unusable], title_as_of: str | None,
                  product_ids: set[str]) -> list[Claim]:
    transforms = mapping["transforms"]
    kind = _source_kind(path, mapping["source_kind"])
    claims: list[Claim] = []
    # A header-only cache may have mapped this same quantity column as component stock.
    packed_total = re.match(r"(?:total\s+)?(?:packed|produced)\s+(?:copies|units|products)\b"
                            r"(?!\s+(?:today|this\s+shift|this\s+week)\b)", table.title, re.I)
    for target in mapping["targets"]:
        if packed_total and target["entity"] == "stock" and len(target["key"]) == 3 and "qty" in target["fields"]:
            target = {**target, "entity": "product", "key": [target["key"][1]],
                      "fields": {"packed_units": target["fields"]["qty"]},
                      "units": {"packed_units": target["units"].get("qty")}}
        entity = target["entity"]
        where = target.get("where") or {}
        sources = {fname: split_source(src, table.headers) for fname, src in target["fields"].items()}
        for row in table.rows:
            if where and not _row_matches(row, where):
                continue
            parts = [key_part(p, row, path, transforms) for p in target["key"]]
            if entity == "stock" and len(parts) == 3 and target["key"][2].startswith("const:") and parts[2]:
                if _is_invented_stock_batch(parts[2], path, table):
                    parts[2] = ""
            if entity == "stock" and len(parts) >= 2 and parts[1] in product_ids:
                continue
            if entity == "stock" and len(parts) == 3 and parts[2] is None:
                batch_column = target["key"][2]
                if batch_column in table.headers and as_text(row.cells.get(batch_column)) is None:
                    parts[2] = ""  # Stock without a batch uses the canonical site|part| key.
            if any(p is None for p in parts):
                continue
            key = make_key(*parts)
            row_as_of = title_as_of or _as_of(row.cells.get(target["as_of"]), default_as_of, zone)
            for fname, (col, pattern) in sources.items():
                raw = row.cells.get(col)
                spec = transforms.get(col)
                if pattern is not None:
                    found = pattern.search(as_text(raw) or "")
                    raw = (found.group(1) if found.groups() else found.group(0)) if found else None
                    spec = None
                value, usable = convert_field(entity, fname, raw, spec, zone)
                locator = row.locators.get(col, row.row_locator)
                if not usable:
                    unusable.append(_Unusable(entity, key, fname, as_text(raw) or "", locator, row.text))
                if value is None:
                    continue
                unit = target["units"].get(fname)
                if unit in table.headers:
                    unit = as_text(row.cells.get(unit))
                claims.append(Claim(
                    entity=entity, key=key, field=fname, value=value, as_of=row_as_of,
                    source_kind=kind, author=author, unit=unit,  # type: ignore[arg-type]
                    evidence=(Evidence(source=path, sha256=digest, locator=locator, quote=row.text),),
                ))
    return claims


def _observations(path: str, digest: str, kind: str, as_of: str, author: str,
                  unusable: list[_Unusable], start_index: int = 1) -> list[Claim]:
    out: list[Claim] = []
    for n, item in enumerate(unusable[:MAX_OBSERVATIONS], start_index):
        text = f"{item.entity} {item.key} {item.field}: value '{item.raw}' is not usable"
        out.append(Claim(entity="observation", key=make_key(path, n), field="text", value=text, as_of=as_of,
                         source_kind=kind, author=author,  # type: ignore[arg-type]
                         evidence=(Evidence(source=path, sha256=digest, locator=item.locator,
                                            quote=item.quote),)))
    if len(unusable) > MAX_OBSERVATIONS:
        more = len(unusable) - MAX_OBSERVATIONS
        overflow = unusable[MAX_OBSERVATIONS]
        out.append(Claim(entity="observation", key=make_key(path, start_index + MAX_OBSERVATIONS), field="text",
                         value=f"{more} more values in this file are not usable", as_of=as_of,
                         source_kind=kind, author=author,  # type: ignore[arg-type]
                         evidence=(Evidence(source=path, sha256=digest, locator=overflow.locator),)))
    return out


def _merge_products(base: list[dict[str, Any]] | None, claims: list[Claim]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {str(p["id"]): dict(p) for p in base or () if p.get("id")}
    for c in claims:
        if c.entity == "product":
            entry = by_id.setdefault(c.key, {"id": c.key, "name": ""})
            if c.field == "name" and c.value:
                entry["name"] = str(c.value)
            elif c.field == "deadline" and c.value:
                entry["deadline"] = str(c.value)
    return [by_id[k] for k in sorted(by_id)]


def read_table(path: str, data: bytes, llm: LLM, *, sha256: str | None = None,
               as_of: str | None = None, tz: str = DEFAULT_TZ, cache_dir: Path | str | None = None,
               tenant_id: str | None = None, tenants_root: str = DEFAULT_TENANTS_ROOT,
               site: str | None = None, example_ids: dict[str, list[str]] | None = None,
               products: list[dict[str, Any]] | None = None,
               author: str = "table_reader", on_error: Callable[[str], None] | None = None,
               on_raw: Callable[[str], None] | None = None) -> list[Claim]:
    """Read one tabular or markdown file into claims. Unknown types and unreadable files give []
    (reported through `on_error` when given); a failed model call only costs that table or sheet."""
    try:
        parsed = parse_tables(path, data)
    except Exception as exc:  # noqa: BLE001 - a damaged file must not stop the caller
        if on_error is not None:
            on_error(f"cannot parse {path}: {type(exc).__name__}: {exc}")
        return []
    zone = zone_info(tz)
    default_as_of = as_of or datetime.now(zone).isoformat(timespec="seconds")
    date_ref = path if parse_date(path) else as_of
    digest = sha256 or hashlib.sha256(data).hexdigest()
    claims: list[Claim] = []
    unusable: list[_Unusable] = []
    obs_index = 1
    kind = _source_kind(path, None)
    for table in parsed.tables:
        if not table.rows:
            continue
        title_as_of = resolve_status_datetime(table.title, ref=date_ref, tz=tz)
        product_ids = {str(p["id"]) for p in products or () if p.get("id")}
        hints = {**(example_ids or {}), "product_id": sorted(product_ids)} if product_ids else example_ids
        mapping = get_mapping(path, table, llm, cache_dir, tenant_id=tenant_id, tenants_root=tenants_root,
                              example_ids=hints, on_error=on_error, on_raw=on_raw)
        if mapping["targets"]:
            extra_targets = _derive_note_targets(path, table, mapping, llm, cache_dir, example_ids)
            if extra_targets:
                mapping = {**mapping, "targets": [*mapping["targets"], *extra_targets]}
            claims += _table_claims(path, digest, table, mapping, default_as_of, zone, author, unusable,
                                    title_as_of, product_ids)
        elif mapping.get("unmapped"):
            label = f"{path} ({table.sheet})" if table.sheet else path
            text = f"Unmapped table {label}: {len(table.rows)} rows, headers {', '.join(table.headers)}"
            row0 = table.rows[0]
            table_locator = f"sheet:{table.sheet}" if table.sheet else f"rows:1-{len(table.rows)}"
            claims.append(Claim(
                entity="observation", key=make_key(path, obs_index), field="text", value=text,
                as_of=title_as_of or default_as_of, source_kind=kind, author=author,  # type: ignore[arg-type]
                evidence=(Evidence(source=path, sha256=digest, locator=table_locator, quote=row0.text),),
            ))
            obs_index += 1
    claims += _observations(path, digest, kind, default_as_of, author, unusable, start_index=obs_index)
    if parsed.blocks:
        known = {"site": site, "timezone": tz, "products": _merge_products(products, claims)}
        reader = TextSheetReader(llm, known=known, now_iso=default_as_of,
                                 author=author, source_kind=kind)  # type: ignore[arg-type]
        seen_slots = {(c.entity, c.key, c.field) for c in claims if c.entity != "observation"}
        text_claims = reader.read(path, data, parsed.blocks, on_error, on_raw=on_raw)
        claims += [c for c in text_claims if (c.entity, c.key, c.field) not in seen_slots]
    return claims


# ---------------------------------------------------------------------------
# 5. Agent wrapper
# ---------------------------------------------------------------------------
def _example_ids(ctx: Context) -> dict[str, list[str]]:
    """Ids already on the Board, so the mapping model can match the spelling of ids in a new file."""
    return {field_name: sorted({f.key for f in ctx.board.facts(entity)})[:MAX_EXAMPLE_IDS]
            for field_name, entity in (("supplier_id", "supplier"), ("part_id", "part"))}


def _known_products(ctx: Context) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for fact in ctx.board.facts("product"):
        rows.setdefault(fact.key, {})[fact.field] = fact.value
    for post in ctx.board.query(kind="claim", status="proposed", subject_prefix="product/", limit=1000):
        b = post.body
        if b.get("entity") == "product" and b.get("key") and b.get("field") in ("name", "deadline"):
            rows.setdefault(str(b["key"]), {}).setdefault(str(b["field"]), b.get("value"))
    return [{"id": k, "name": v.get("name", ""), **({"deadline": v["deadline"]} if v.get("deadline") else {})}
            for k, v in sorted(rows.items())]


def _already_read(board: Any, ref: str) -> bool:
    """True when `ref` already has at least one non-stale claim dependent."""
    for dep_id in board.dependents(ref):
        dep = board.get(dep_id)
        if dep is not None and dep.kind == "claim" and dep.status != "stale":
            return True
    return False


def recover_skipped_table_events(db_path: Path | str, *, dry_run: bool = False) -> list[str]:
    """Unmark `table_reader` handled rows for the latest table `file.changed` events with no
    non-stale claim dependents. Leaves `document_reader`, other agents, and mail untouched."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            "SELECT id, body FROM posts WHERE kind='event' AND subject='file.changed' ORDER BY rowid"
        ).fetchall()
        latest_by_path: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in events:
            try:
                body = json.loads(row["body"])
            except (ValueError, TypeError):
                continue
            path = str(body.get("path", ""))
            if path.lower().endswith(TABLE_EXTENSIONS):
                latest_by_path[path] = (row["id"], body)

        to_replay: list[tuple[str, str]] = []
        for path, (post_id, body) in latest_by_path.items():
            if body.get("kind") not in ("added", "changed"):
                continue
            sha = body.get("sha256")
            if not sha:
                continue
            is_handled = conn.execute(
                "SELECT 1 FROM handled WHERE agent='table_reader' AND post_id=? AND topic='event:file.changed'",
                (post_id,),
            ).fetchone()
            if not is_handled:
                continue
            ref = f"file:{path}@{sha}"
            has_live_claim = conn.execute(
                "SELECT 1 FROM deps d JOIN posts p ON p.id=d.post_id "
                "WHERE d.ref=? AND p.kind='claim' AND p.status != 'stale' LIMIT 1",
                (ref,),
            ).fetchone()
            if has_live_claim:
                continue
            to_replay.append((post_id, path))

        if to_replay and not dry_run:
            with conn:
                conn.executemany(
                    "DELETE FROM handled WHERE agent='table_reader' AND post_id=? AND topic='event:file.changed'",
                    [(pid,) for pid, _ in to_replay],
                )
        return [path for _, path in to_replay]
    finally:
        conn.close()


class TableReader:
    name = "table_reader"
    subscribes = ("event:file.changed",)

    def __init__(self, cache_dir: Path | str | None = None, tenants_root: str = DEFAULT_TENANTS_ROOT) -> None:
        self.cache_dir = cache_dir
        self.tenants_root = tenants_root

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        body = post.body
        path = body.get("path", "")
        if body.get("kind") == "removed" or not path.lower().endswith(TABLE_EXTENSIONS):
            return []
        sha = body.get("sha256")
        ref = f"file:{path}@{sha}"
        if sha and _already_read(ctx.board, ref):  # this exact version already has non-stale claims
            return []
        try:
            data = ctx.ports.files.read(path, sha)
        except Exception as exc:  # noqa: BLE001 - a missing file must not stop the dispatcher
            ctx.board.log_event(
                self.name, "reader.failed",
                {"path": path, **({"sha256": sha} if sha else {}), "reason": f"{type(exc).__name__}: {exc}"},
                post_id=post.id,
            )
            return []
        errors: list[str] = []
        raws: list[str] = []
        claims = read_table(
            path, data, ctx.llm, sha256=sha, as_of=ctx.ports.clock.now().isoformat(timespec="seconds"),
            tz=ctx.tenant.get("timezone", DEFAULT_TZ),
            cache_dir=self.cache_dir or ctx.tenant.get("cache_dir", DEFAULT_CACHE_DIR),
            tenant_id=ctx.tenant.get("id"), tenants_root=self.tenants_root, site=ctx.tenant.get("site"),
            example_ids=_example_ids(ctx), products=_known_products(ctx), author=self.name,
            on_error=errors.append, on_raw=raws.append)
        for i, reason in enumerate(errors):
            payload = {"path": path, **({"sha256": sha} if sha else {}), "reason": reason,
                       **({"raw": raws[i]} if i < len(raws) and raws[i] else {})}
            ctx.board.log_event(self.name, "reader.failed", payload, post_id=post.id)
        return [Post(kind="claim", status="proposed", subject=f"{c.entity}/{c.key}/{c.field}",
                     body=c.to_dict(), author=self.name,
                     depends_on=(ref, post.id) if sha else (post.id,)) for c in claims]


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    dry = "--dry-run" in args
    positional = [a for a in args if a != "--dry-run"]
    if len(positional) == 2 and positional[0] == "recover":
        replayed = recover_skipped_table_events(positional[1], dry_run=dry)
        print(f"{'would unmark' if dry else 'unmarked'} {len(replayed)} table_reader handled event(s)")
        for p in replayed:
            print(f"  {p}")
    else:
        print("Usage: python -m gtl.read.table_reader recover <board.db> [--dry-run]", file=sys.stderr)
        raise SystemExit(2)
