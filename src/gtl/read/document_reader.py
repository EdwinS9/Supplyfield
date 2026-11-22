"""Document reader: PDFs, scans and photos become evidence-backed claims.

`read_document` is a pure function (no Board). `DocumentReaderAgent` wraps it with facts, a
relevance filter, a cache by file hash and a parallel prefetch.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pypdf import PdfReader

from gtl.contracts import (ENTITIES, VALUES, Claim, Context, Evidence, Post, make_key,
                           normalize_value)
from gtl.llm import LLMError, extract_json
from gtl.read import prompts

AUTHOR = "document_reader"
SUPPORTED = (".pdf", ".jpg", ".jpeg", ".png", ".docx")
_RETRY_ROLES = {"reader": "reader_retry", "vision": "vision_retry"}
MIN_TEXT_CHARS = 40  # fewer non-space characters than this means a scan
LOW_CONFIDENCE = 0.4
OLD_DAYS = 60
MAX_NOTE_AGE_DAYS = 90
PREFETCH_MAX = 12
PREFETCH_WORKERS = 4

# Numeric fields. Quantities are whole counts where "8.000" or "554.000" means 8000 / 554000; decimals keep their point.
QUANTITY_FIELDS = {"qty", "qty_ordered", "qty_received", "qty_shipped", "lot_qty",
                   "demand_units", "preorder_units", "packed_units", "units"}
DECIMAL_FIELDS = {"unit_price", "surcharge_pct", "ready_hours", "transit_hours", "defect_rate",
                  "gross_weight_kg", "qty_per", "rate_per_hour", "lead_time_days", "share",
                  "transit_days"}
DATE_FIELDS = {"date", "confirmed_date", "eta", "etd", "valid_until", "start", "end", "deadline",
               "promised_date", "release_date", "eta_site"}
DATETIME_FIELDS = {"start", "end", "deadline"}  # keep the time of day when the document gives one
LIST_FIELDS = {"part_ids", "po_numbers"}
INCOTERMS = ("EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP")

_INCOTERM_RE = re.compile(r"\b(" + "|".join(INCOTERMS) + r")\b", re.IGNORECASE)
_REVISION_RE = re.compile(r"^\s*(?:rev(?:ision)?\.?\s*)?([A-Za-z0-9]{1,4})\s*$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?::[0-5]\d)?(?!\d)")
_NUMBER_RE = re.compile(r"[-+]?\d[\d\s.,  ']*")
_NUMBER_TOKEN_RE = re.compile(r"\d{1,3}(?:[   .,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_EU_DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})(?:\s+(\d{2}):(\d{2}))?")
_WEEKDAY_TIME_RE = re.compile(
    r"\b(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
    r"\b[,\s]+(\d{1,2}):([0-5]\d)\b",
    re.IGNORECASE,
)
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_ONLY_RE = re.compile(
    r"^\s*(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\s*$",
    re.IGNORECASE,
)
_STRUCK_RE = re.compile(
    r"cross(?:ed)?[\s-]*out|struck[\s-]*out|strike[\s-]*through|durchgestrichen",
    re.IGNORECASE,
)
_QTY_LABEL_RE = re.compile(r"\b(qty|quantit|menge|ilo|pcs|szt|stk|deliver|ship|order)\w*", re.IGNORECASE)
_GROUPED_QTY_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:([., \u00a0\u202f])\d{3})+)(?!\d|[.,]\d)"
)
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(20\d{2})-?(\d{2})-?(\d{2})(?!\d)")
_LEGAL_STOPWORDS = frozenset({"sp", "z", "o", "inc", "gmbh", "ltd", "llc", "co", "corp", "sa", "ag", "bv", "srl"})


class _CentralEurope(tzinfo):
    """Fallback when the OS has no tz database (plain Windows without tzdata): EU summer time rules."""

    def _dst(self, dt: datetime) -> bool:
        def last_sunday(month: int) -> datetime:
            d = datetime(dt.year, month, 31)
            return d - timedelta(days=(d.weekday() + 1) % 7)
        start = last_sunday(3).replace(hour=2)
        end = last_sunday(10).replace(hour=3)
        return start <= dt.replace(tzinfo=None) < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=2 if dt is not None and self._dst(dt) else 1)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1 if dt is not None and self._dst(dt) else 0)


def zone_info(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _CentralEurope() if name.startswith("Europe/") else timezone.utc


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
def parse_incoterm(raw: Any) -> str | None:
    """The three-letter incoterm code in a value such as "FOB Busan", or None."""
    match = _INCOTERM_RE.search(str(raw))
    return match.group(1).upper() if match else None


def parse_revision(raw: Any) -> str | None:
    """The bare revision identifier in a value such as "rev. B", or None."""
    match = _REVISION_RE.match(str(raw))
    return match.group(1).upper() if match else None


def parse_clock(raw: Any) -> str | None:
    """A time of day as "HH:MM" from a value such as "6:00", "06:00:00" or "2026-11-21T06:00", or None."""
    match = _CLOCK_RE.search(str(raw))
    if not match or int(match.group(1)) > 23:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_number(raw: Any, *, quantity: bool) -> int | float | None:
    """A number from a value such as "8 000", "80.000", "8,000" or "5,40 EUR". None if there is none."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if float(raw).is_integer() else float(raw)
    match = _NUMBER_RE.search(str(raw))
    if not match:
        return None
    text = re.sub(r"[\s  ']", "", match.group(0)).rstrip(".,")
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        text = text.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = text.split(sep)
        thousands = len(parts) > 2 or (
            quantity and len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 3
            and parts[0] not in ("", "0", "-0", "+0")
        )
        text = "".join(parts) if thousands else text.replace(sep, ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def parse_date(raw: Any, *, keep_time: bool = False, tz: str | None = None) -> str | None:
    """ISO date, or an ISO date-time when `keep_time` and the text has a time of day."""
    text = str(raw)
    match = _ISO_DATE_RE.search(text)
    if match:
        year, month, day, hour, minute = match.groups()
    else:
        match = _EU_DATE_RE.search(text)
        if not match:
            return None
        day, month, year, hour, minute = match.groups()
    try:
        day_only = date(int(year), int(month), int(day))
        if not (keep_time and hour):
            return day_only.isoformat()
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute))
    except ValueError:
        return None
    if tz:
        moment = moment.replace(tzinfo=zone_info(tz))
    return moment.isoformat(timespec="seconds")


def resolve_status_datetime(raw: Any, ref: str | None = None, tz: str | None = None) -> str | None:
    """Resolve an explicit date-time or a weekday+time like 'Saturday 13:00' against `ref`."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    explicit = parse_date(raw, keep_time=True, tz=tz)
    if explicit is not None:
        return explicit
    match = _WEEKDAY_TIME_RE.search(raw)
    if not match or not ref:
        return None
    ref_iso = parse_date(ref)
    if not ref_iso:
        return None
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23:
        return None
    target_wd = _WEEKDAYS[match.group(1)[:3].lower()]
    ref_date = date.fromisoformat(ref_iso)
    day = ref_date - timedelta(days=(ref_date.weekday() - target_wd) % 7)
    moment = datetime(day.year, day.month, day.day, hour, minute)
    if tz:
        moment = moment.replace(tzinfo=zone_info(tz))
    return moment.isoformat(timespec="seconds")


def _coerce(entity: str, field_name: str, key: str, value: Any, tz: str | None) -> Any:
    """The value in contract form, or None when it cannot be mapped (the claim is then dropped)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if field_name in QUANTITY_FIELDS:
        return parse_number(value, quantity=True)
    if field_name in DECIMAL_FIELDS:
        return parse_number(value, quantity=False)
    if entity == "shift" and field_name in ("start", "end"):
        return parse_clock(value)
    if field_name in DATE_FIELDS:
        return parse_date(value, keep_time=field_name in DATETIME_FIELDS, tz=tz)
    if field_name in LIST_FIELDS:
        if isinstance(value, str):
            value = [part for part in re.split(r"[,;|]", value)]
        return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else None
    if field_name == "incoterm" or (entity == "contract_term" and key.endswith("|incoterm")):
        return parse_incoterm(value)
    if field_name in ("revision", "required_revision"):
        return parse_revision(value)
    value = normalize_value(entity, field_name, value)
    allowed = VALUES.get(f"{entity}.{field_name}")
    if allowed and value not in allowed:
        return "other" if "other" in allowed else None
    return value


# ---------------------------------------------------------------------------
# PDF content
# ---------------------------------------------------------------------------
@dataclass
class PdfContent:
    pages: list[str]
    images: list[tuple[int, bytes]] = field(default_factory=list)  # (page number, JPEG bytes)

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def has_text(self) -> bool:
        return len(re.sub(r"\s", "", self.text)) > MIN_TEXT_CHARS


def read_pdf(data: bytes) -> PdfContent:
    """Page texts and the embedded JPEG images (all pdfs here carry scans as JPEG). Raises ValueError."""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        content = PdfContent(pages)
        if content.has_text:
            return content
        for number, page in enumerate(reader.pages, 1):
            xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
            for ref in xobjects.values():
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Image" and obj.get("/Filter") == "/DCTDecode":
                    content.images.append((number, obj.get_data()))
        return content
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on damaged files
        raise ValueError(f"cannot read pdf: {exc}") from exc


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(data: bytes) -> str:
    """Paragraph text from a Word (.docx) file. Raises ValueError on damaged or empty files."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml_bytes = archive.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001 - zipfile / xml raise several types on damaged files
        raise ValueError(f"cannot read docx: {exc}") from exc
    lines: list[str] = []
    for para in root.iter(f"{_WORD_NS}p"):
        parts: list[str] = []
        for elem in para.iter():
            if elem.tag == f"{_WORD_NS}t" and elem.text:
                parts.append(elem.text)
            elif elem.tag in (f"{_WORD_NS}br", f"{_WORD_NS}cr"):
                parts.append("\n")
            elif elem.tag == f"{_WORD_NS}tab":
                parts.append("\t")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    if not re.sub(r"\s", "", text):
        raise ValueError("docx has no readable text")
    return text


def _collapse(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------
def _ask(llm: Any, role: str, system: str, user: str, images: tuple[bytes, ...],
         schema: dict[str, Any] | None, on_raw: Callable[[str], None] | None = None) -> tuple[Any, str | None]:
    result = llm.complete(role, system, user, schema=schema, images=images)
    model = getattr(result, "model", None)
    model_name = model if isinstance(model, str) and model else None
    if schema is None:
        return result.text, model_name
    try:
        data = result.data if isinstance(result.data, dict) else extract_json(result.text)
        if not isinstance(data.get("claims"), list):
            raise ValueError("answer has no claims list")
        return data, model_name
    except ValueError:
        raw = getattr(result, "text", None)
        if on_raw is not None and isinstance(raw, str) and raw:
            on_raw(raw[:300])
        raise


def _ask_with_retry_role(llm: Any, role: str, retry: bool, system: str, user: str,
                         images: tuple[bytes, ...], schema: dict[str, Any] | None,
                         on_raw: Callable[[str], None] | None = None) -> tuple[Any, str | None]:
    if not retry:
        return _ask(llm, role, system, user, images, schema, on_raw=on_raw)
    retry_role = _RETRY_ROLES.get(role, role)
    try:
        return _ask(llm, retry_role, system, user, images, schema, on_raw=on_raw)
    except LLMError as exc:
        if retry_role != role and "unknown role" in str(exc):
            return _ask(llm, role, system, user, images, schema, on_raw=on_raw)
        raise


def _parse_numbers_text(text: str) -> tuple[set[float], list[tuple[int | float, str]]]:
    seen: set[float] = set()
    struck: list[tuple[int | float, str]] = []
    for line in (text or "").splitlines():
        is_struck = bool(_STRUCK_RE.search(line))
        for token in _NUMBER_TOKEN_RE.findall(line):
            for quantity in (True, False):
                number = parse_number(token, quantity=quantity)
                if number is not None:
                    seen.add(float(number))
            if is_struck:
                qty_num = parse_number(token, quantity=True)
                if qty_num is not None:
                    struck.append((qty_num, line.strip()))
    return seen, struck


def _second_vision_read(llm: Any, images: tuple[bytes, ...],
                        cached_numbers: dict[str, Any] | None = None
                        ) -> tuple[set[float], list[tuple[int | float, str]], dict[str, Any]]:
    """Numbers listed by an independent read of the image, reusing a matching cached answer."""
    prompt_ver = _prompt_version(f"{prompts.NUMBERS_SYSTEM}\n{prompts.NUMBERS_USER}")
    if (isinstance(cached_numbers, dict) and cached_numbers.get("prompt") == prompt_ver
            and isinstance(cached_numbers.get("text"), str)):
        text, entry = cached_numbers["text"], cached_numbers
    else:
        text, model_name = _ask(llm, "vision", prompts.NUMBERS_SYSTEM, prompts.NUMBERS_USER, images, None)
        entry = {"prompt": prompt_ver, "model": model_name, "text": text}
    seen, struck = _parse_numbers_text(text)
    return seen, struck, entry


def _pick_struck_out(claim: Claim, struck: list[tuple[int | float, str]]) -> Claim | None:
    """Build a struck_out claim from the second vision read when the first read reported a correction."""
    candidates = [(val, line) for val, line in struck if float(val) != float(claim.value)]
    if not candidates:
        return None
    preferred = [c for c in candidates if _QTY_LABEL_RE.search(c[1])]
    val, line = (preferred or candidates)[0]
    ev = claim.evidence[0]
    return Claim(
        entity=claim.entity, key=claim.key, field=claim.field, value=val, as_of=claim.as_of,
        source_kind=claim.source_kind, author=claim.author, unit=claim.unit, flags=("struck_out",),
        evidence=(Evidence(source=ev.source, sha256=ev.sha256, locator=ev.locator, quote=line),),
    )


def _resolve_shift_key(key: str, as_of: str) -> str:
    """Turn '<site>|Saturday' into '<site>|YYYY-MM-DD' on or after the document date."""
    site, sep, day_part = key.partition("|")
    if not sep or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_part.strip()):
        return key
    match = _WEEKDAY_ONLY_RE.match(day_part)
    ref_iso = parse_date(as_of)
    if not match or not ref_iso:
        return key
    target_wd = _WEEKDAYS[match.group(1)[:3].lower()]
    ref_date = date.fromisoformat(ref_iso)
    day = ref_date + timedelta(days=(target_wd - ref_date.weekday()) % 7)
    return f"{site.strip()}|{day.isoformat()}"


def _same_digits(raw_num: int | float, grouped_int: int) -> bool:
    """True when `raw_num` is the ungrouped/decimal-stripped version of `grouped_int` (e.g. 47.4 vs 47400)."""
    val = float(raw_num)
    return any(abs(grouped_int - val * (1000 ** k)) < 1e-6 for k in range(4))


def _quantity_from_quote(value: int | float, quote: str) -> int | float:
    """Recover grouped whole numbers ('65.000', '47.400') when the model emitted a stripped float/int."""
    if not quote.strip():
        return value
    cands = [p for m in _GROUPED_QTY_RE.finditer(quote)
             if isinstance((p := parse_number(m.group(0), quantity=True)), int)]
    if not cands:
        return value
    for cand in cands:
        if cand == value:
            return cand
    for cand in cands:
        if _same_digits(value, cand):
            return cand
    return value


def _receiving_date(path: str) -> date | None:
    match = _FILENAME_DATE_RE.search(Path(path).name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _resolve_note_date(value_iso: str, path: str, receiving: date | None) -> tuple[str | None, str | None]:
    """Check delivery_note.date against receiving date, swapping ambiguous day/month or flagging invalid."""
    if receiving is None:
        return value_iso, None
    cand = date.fromisoformat(value_iso)
    fits = lambda d: 0 <= (receiving - d).days <= MAX_NOTE_AGE_DAYS
    if fits(cand):
        return value_iso, None
    if cand.month != cand.day and 1 <= cand.day <= 12:
        try:
            swapped = date(cand.year, cand.day, cand.month)
        except ValueError:
            swapped = None
        if swapped is not None and fits(swapped):
            return swapped.isoformat(), None
    return None, f"Implausible delivery_note.date {value_iso} for receiving file {Path(path).name}"


def _id_tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _LEGAL_STOPWORDS]


def _token_matches(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def _match_supplier_id(raw: Any, suppliers: list[dict[str, Any]]) -> str | None:
    text = str(raw).strip()
    by_low = {str(s["id"]).strip().lower(): str(s["id"]).strip() for s in suppliers if s.get("id")}
    if text.lower() in by_low:
        return by_low[text.lower()]
    cand_tokens = _id_tokens(text)
    if not cand_tokens:
        return None
    full_matches: list[str] = []
    subset_matches: list[str] = []
    for s in suppliers:
        sid, sname = str(s.get("id", "")).strip(), str(s.get("name", "")).strip()
        name_tokens = _id_tokens(sname)
        if not sid or not name_tokens:
            continue
        if all(any(_token_matches(nt, ct) for ct in cand_tokens) for nt in name_tokens):
            full_matches.append(sid)
        elif all(any(_token_matches(ct, nt) for nt in name_tokens) for ct in cand_tokens):
            subset_matches.append(sid)
    matches = full_matches or subset_matches
    return matches[0] if len(matches) == 1 else None


def _is_known_part_id(raw: Any, parts: list[dict[str, Any]]) -> str | None:
    text = str(raw).strip()
    by_low = {str(p["id"]).strip().lower(): str(p["id"]).strip() for p in parts if p.get("id")}
    if text.lower() in by_low:
        return by_low[text.lower()]
    prefixes = {pid.partition("-")[0] for pid in by_low.values() if "-" in pid}
    if prefixes and "-" in text and text.partition("-")[0].upper() in {p.upper() for p in prefixes}:
        return text
    return None


def _locate(quote: str, collapsed_pages: list[str]) -> int | None:
    """The page whose text holds the quote (whitespace collapsed), or None."""
    needle = _collapse(quote)
    return next((n for n, text in enumerate(collapsed_pages, 1) if needle and needle in text), None)


def _build_claims(raw: list[Any], path: str, sha: str, as_of: str, tz: str | None,
                  pages: list[str] | None, labels: list[str] | None = None,
                  known: dict[str, Any] | None = None) -> list[Claim]:
    """Validated claims. With `pages` (text route) the quote must occur in the text.

    `labels` name the pages in evidence locators (for example "sheet:Shifts"); the default is "page:<n>".
    """
    collapsed = [_collapse(p) for p in pages] if pages is not None else None
    receiving = _receiving_date(path)
    known_suppliers = [s for s in (known or {}).get("suppliers") or [] if isinstance(s, dict) and s.get("id")]
    known_parts = [p for p in (known or {}).get("parts") or [] if isinstance(p, dict) and p.get("id")]
    claims: list[Claim] = []
    observations = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        entity, field_name = str(item.get("entity", "")).strip(), str(item.get("field", "")).strip()
        if entity not in ENTITIES or field_name not in ENTITIES[entity][1]:
            continue
        key = str(item.get("key", "")).strip()
        quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
        flags = [f for f in item.get("flags") or [] if isinstance(f, str)]
        found = _locate(quote, collapsed) if collapsed is not None else None
        if not quote.strip() or (collapsed is not None and found is None):
            flags.append("low_confidence")
        page = found or (item.get("page") if isinstance(item.get("page"), int) else 1)
        locator = labels[page - 1] if labels and 1 <= page <= len(labels) else f"page:{page}"
        translation = item.get("translation") if isinstance(item.get("translation"), str) else None
        unit = item.get("unit") if isinstance(item.get("unit"), str) else None
        if entity == "observation":
            if not isinstance(item.get("value"), str) or not item["value"].strip():
                continue
            observations += 1
            key, value = make_key(path, observations), item["value"].strip()
        else:
            parts = key.split("|")
            expected = len(ENTITIES[entity][0].split("|"))
            valid_key = (1 <= len(parts) <= 2) if entity == "delivery_note" else (len(parts) == expected)
            if not valid_key or not all(p.strip() for p in parts):
                continue
            if entity == "contract_term":
                key = make_key(parts[0].strip(), parts[1].strip().lower())
            elif entity == "shift":
                key = _resolve_shift_key(key, as_of)
            value = _coerce(entity, field_name, key, item.get("value"), tz)
            if value is None:
                continue
            if field_name in QUANTITY_FIELDS:
                value = _quantity_from_quote(value, quote)
            elif field_name == "supplier_id" and known_suppliers:
                matched = _match_supplier_id(value, known_suppliers)
                if matched is None:
                    observations += 1
                    claims.append(Claim(
                        entity="observation", key=make_key(path, observations), field="text",
                        value=f"Unmatched supplier '{value}'", as_of=as_of, source_kind="document",
                        evidence=(Evidence(source=path, sha256=sha, locator=locator, quote=quote.strip() or None,
                                           translation=translation or None),),
                        author=AUTHOR, flags=tuple(dict.fromkeys(flags))))
                    continue
                value = matched
            elif field_name == "part_id" and known_parts:
                matched_part = _is_known_part_id(value, known_parts)
                if matched_part is None:
                    observations += 1
                    claims.append(Claim(
                        entity="observation", key=make_key(path, observations), field="text",
                        value=f"Foreign part number '{value}'", as_of=as_of, source_kind="document",
                        evidence=(Evidence(source=path, sha256=sha, locator=locator, quote=quote.strip() or None,
                                           translation=translation or None),),
                        author=AUTHOR, flags=tuple(dict.fromkeys(flags))))
                    continue
                value = matched_part
            elif entity == "delivery_note" and field_name == "date":
                checked, note_obs = _resolve_note_date(str(value), path, receiving)
                if checked is None:
                    observations += 1
                    claims.append(Claim(
                        entity="observation", key=make_key(path, observations), field="text",
                        value=note_obs or f"Implausible date '{value}'", as_of=as_of, source_kind="document",
                        evidence=(Evidence(source=path, sha256=sha, locator=locator, quote=quote.strip() or None,
                                           translation=translation or None),),
                        author=AUTHOR, flags=tuple(dict.fromkeys((*flags, "low_confidence")))))
                    continue
                value = checked
        claims.append(Claim(
            entity=entity, key=key, field=field_name, value=value, as_of=as_of, source_kind="document",
            evidence=(Evidence(source=path, sha256=sha, locator=locator, quote=quote.strip() or None,
                               translation=translation or None),),
            author=AUTHOR, unit=unit, flags=tuple(dict.fromkeys(flags))))
    return claims


def _mark_low(claim: Claim) -> Claim:
    flags = tuple(dict.fromkeys((*claim.flags, "low_confidence")))
    return Claim(**{**claim.__dict__, "flags": flags, "confidence": min(claim.confidence, LOW_CONFIDENCE)})


def read_document(path: str, data: bytes, llm: Any, *, known: dict[str, Any] | None = None,
                  now_iso: str | None = None, on_error: Callable[[str], None] | None = None,
                  on_retry: Callable[[str], None] | None = None,
                  on_retry_model: Callable[[str], None] | None = None,
                  on_raw: Callable[[str], None] | None = None,
                  cache_dir: Path | str | None = None,
                  text: str | list[str] | None = None, labels: list[str] | None = None,
                  hints: str | None = None) -> list[Claim]:
    """Read one document. A failure or an empty answer gives [] and, when given, calls `on_error`.

    When reading succeeds with zero claims, it retries once with the retry role (`reader_retry` or
    `vision_retry`, falling back to `reader`/`vision` if unconfigured) and calls `on_retry`. When
    `cache_dir` is given, the model's raw answer (keyed by file hash, path and prompt hash) is cached
    and claims are rebuilt from it on every read so post-processing changes take effect without model calls.

    `text` is text the caller already extracted from a non-PDF file (spreadsheet sheets, markdown): one
    string, or one per page. It takes the text route; `data` is only hashed for the evidence. `labels`
    name those pages in locators and `hints` replace the document-type hints in the prompt.
    """
    try:
        pages = [text] if isinstance(text, str) else text
        claims, entry, from_cache, dirty = _read(
            path, data, llm, known, now_iso, pages, labels, hints,
            on_raw=on_raw, cache_dir=cache_dir)
        if not claims and not from_cache:
            if on_retry is not None:
                on_retry(path)
            claims, entry, _, dirty = _read(
                path, data, llm, known, now_iso, pages, labels, hints,
                retry=True, on_model=on_retry_model, on_raw=on_raw)
        if cache_dir is not None and entry is not None and dirty:
            _raw_cache_put(cache_dir, path, hashlib.sha256(data).hexdigest(), entry)
        return claims
    except Exception as exc:  # noqa: BLE001 - a bad file or model never stops the caller
        raw = getattr(exc, "raw", None)
        if on_raw is not None and isinstance(raw, str) and raw:
            on_raw(raw[:300])
        if on_error is not None:
            on_error(f"{type(exc).__name__}: {exc}")
        return []


def _read(path: str, data: bytes, llm: Any, known: dict[str, Any] | None, now_iso: str | None,
          pages: list[str] | None = None, labels: list[str] | None = None,
          hints: str | None = None, *, retry: bool = False,
          on_model: Callable[[str], None] | None = None,
          on_raw: Callable[[str], None] | None = None,
          cache_dir: Path | str | None = None) -> tuple[list[Claim], dict[str, Any] | None, bool, bool]:
    sha = hashlib.sha256(data).hexdigest()
    images: tuple[bytes, ...] = ()
    image_pages: list[int] = []
    if pages is None and path.lower().endswith(".pdf"):
        content = read_pdf(data)
        if content.has_text:
            pages = content.pages
        elif content.images:
            images = tuple(img for _, img in content.images)
            image_pages = [n for n, _ in content.images]
        else:
            raise ValueError("pdf has no text layer and no readable image")
    elif pages is None and path.lower().endswith(".docx"):
        pages = [read_docx(data)]
    elif pages is None:
        images, image_pages = (data,), [1]
    route = "text" if pages is not None else "vision"
    role = "reader" if pages is not None else "vision"
    system = prompts.build_system(path, known, hints)
    prompt_ver = _prompt_version(prompts.build_system(path, None, hints), prompts.SCHEMA)
    cached = _raw_cache_get(cache_dir, path, sha) if (cache_dir is not None and not retry) else None
    from_cache = cached is not None and cached["prompt"] == prompt_ver and cached["route"] == route
    if from_cache:
        answer, model = cached["answer"], cached.get("model")  # type: ignore[index]
    else:
        answer, model = _ask_with_retry_role(
            llm, role, retry, system, prompts.build_user(path, pages),
            images, prompts.SCHEMA, on_raw=on_raw)
        if on_model is not None and model:
            on_model(model)

    entry: dict[str, Any] = {"prompt": prompt_ver, "route": route, "model": model, "answer": answer}
    dirty = not from_cache

    tz = (known or {}).get("timezone")
    ref = path if parse_date(path) else now_iso
    doc_date = answer.get("doc_date")
    as_of = (resolve_status_datetime(doc_date, ref=ref, tz=tz) if isinstance(doc_date, str)
             else None) or now_iso or datetime.now().astimezone().isoformat(timespec="seconds")
    claims = _build_claims(answer["claims"], path, sha, as_of, tz, pages, labels, known)

    if pages is None:
        cached_nums = cached.get("numbers") if from_cache else None  # type: ignore[union-attr]
        quantities = {float(c.value) for c in claims if c.field in QUANTITY_FIELDS}
        if quantities:
            try:
                seen, struck, nums_entry = _second_vision_read(llm, images, cached_nums)
                confirmed = {value for value in quantities if value in seen}
                entry["numbers"] = nums_entry
                if nums_entry is not cached_nums:
                    dirty = True
            except Exception:  # noqa: BLE001 - an unconfirmed quantity is flagged, not lost
                confirmed, struck = set(), []
                if isinstance(cached_nums, dict):
                    entry["numbers"] = cached_nums
            claims = [_mark_low(c) if c.field in QUANTITY_FIELDS and float(c.value) not in confirmed else c
                      for c in claims]
            for c in list(claims):
                if (c.field in QUANTITY_FIELDS and "corrected" in c.flags
                        and not any(o.slot == c.slot and "struck_out" in o.flags for o in claims)):
                    extra = _pick_struck_out(c, struck)
                    if extra is not None:
                        claims.append(extra)
        elif isinstance(cached_nums, dict):
            entry["numbers"] = cached_nums
    return [_mark_low(c) if "low_confidence" in c.flags else c for c in claims], entry, from_cache, dirty


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
def _prompt_version(system: str, schema: dict[str, Any] | None = None) -> str:
    payload = system if schema is None else f"{system}\n{json.dumps(schema, sort_keys=True)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _raw_cache_file(cache_dir: Path | str, path: str, sha: str) -> Path:
    """One cache entry per file version and path: the claims name their path in the evidence."""
    where = hashlib.sha256(path.encode()).hexdigest()[:10]
    return Path(cache_dir) / "read-cache" / f"{sha}-{where}.json"


def _raw_cache_get(cache_dir: Path | str, path: str, sha: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_raw_cache_file(cache_dir, path, sha).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answer"), dict):
        return None
    if not isinstance(data.get("route"), str) or not isinstance(data.get("prompt"), str):
        return None
    if not isinstance(data["answer"].get("claims"), list):
        return None
    return data


def _raw_cache_put(cache_dir: Path | str, path: str, sha: str, entry: dict[str, Any]) -> None:
    target = _raw_cache_file(cache_dir, path, sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.{id(entry)}.tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def _closed(status: Any) -> bool:
    return str(status) in ("delivered", "cancelled")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in re.split(r"[|,;]", value) if part]
    return [str(v) for v in value or []]


@dataclass
class Snapshot:
    """What the Board knew when the batch started. Workers read it; they never touch the Board."""
    known: dict[str, Any]
    closed_po_lines: list[dict[str, Any]]
    open_pos: set[str]
    open_refs: set[str]
    batches: set[str]
    now: datetime

    @property
    def empty(self) -> bool:
        return not (self.known["po_lines"] or self.closed_po_lines or self.known["shipments"] or self.batches)


def snapshot(ctx: Context) -> Snapshot:
    board = ctx.board

    def grouped(entity: str) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for fact in board.facts(entity):
            rows.setdefault(fact.key, {})[fact.field] = fact.value
        return rows

    parts = [{"id": k, "description": v.get("description", "")} for k, v in grouped("part").items()]
    suppliers = [{"id": k, "name": v.get("name", "")} for k, v in grouped("supplier").items()]
    shipment_rows = grouped("shipment")
    shipments = [{"id": k, "reference": v.get("reference", ""), "po_numbers": _as_list(v.get("po_numbers")),
                  "part_ids": _as_list(v.get("part_ids"))} for k, v in shipment_rows.items()]
    open_lines: list[dict[str, Any]] = []
    closed_lines: list[dict[str, Any]] = []
    for key, row in grouped("po_line").items():
        po_number, _, po_line = key.partition("|")
        line = {"po_number": po_number, "po_line": po_line, "part_id": row.get("part_id"),
                "supplier_id": row.get("supplier_id"), "status": row.get("status")}
        (closed_lines if _closed(row.get("status")) else open_lines).append(line)
    batches = {key.split("|")[2] for key, row in grouped("stock").items()
               if key.count("|") >= 2 and (row.get("qty") or 0) > 0}
    known = {"parts": parts, "suppliers": suppliers, "shipments": shipments, "po_lines": open_lines,
             "site": ctx.tenant.get("site"), "timezone": ctx.tenant.get("timezone")}
    return Snapshot(
        known=known, closed_po_lines=closed_lines,
        open_pos={line["po_number"] for line in open_lines},
        open_refs={str(row.get("reference")) for row in shipment_rows.values()
                   if row.get("reference") and not _closed(row.get("status"))},
        batches=batches, now=ctx.ports.clock.now())


def newest_date(text: str) -> date | None:
    found: list[date] = []
    for regex, order in ((_ISO_DATE_RE, (0, 1, 2)), (_EU_DATE_RE, (2, 1, 0))):
        for match in regex.finditer(text):
            try:
                y, m, d = (int(match.group(i + 1)) for i in order)
                found.append(date(y, m, d))
            except ValueError:
                continue
    return max(found) if found else None


def skip_reason(text: str, snap: Snapshot) -> str | None:
    """Why a text document can be skipped without a model call, or None when it may matter."""
    if snap.empty:
        return None
    flat = _collapse(text)
    if any(token and token in flat for token in (*snap.open_pos, *snap.open_refs, *snap.batches)):
        return None
    newest = newest_date(flat)
    if newest is None or newest >= (snap.now - timedelta(days=OLD_DAYS)).date():
        return None
    return f"mentions no open PO, open shipment or stock batch and is dated {newest.isoformat()}"


Outcome = tuple[str, Any, bool, str | None]  # ("claims" | "skipped" | "failed", payload, retried, retry_model)


class DocumentReaderAgent:
    name = "document_reader"
    subscribes = ("event:file.changed",)

    def __init__(self) -> None:
        self._tried: set[tuple[str, str]] = set()  # (path, sha) already attempted in this process

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        change = post.body
        path, sha = str(change.get("path", "")), change.get("sha256")
        if not self._wanted(change) or not sha:
            return []
        cache_dir = ctx.tenant.get("cache_dir", "var")
        snap = snapshot(ctx)
        cached = _raw_cache_get(cache_dir, path, sha)
        jobs = [(path, sha, post.id)]
        if cached is None:
            jobs += self._pending(ctx, post, sha, cache_dir)
        with ThreadPoolExecutor(PREFETCH_WORKERS) as pool:
            outcomes = list(pool.map(lambda job: self._process(ctx, snap, job[0], job[1], cache_dir=cache_dir), jobs))
        for (job_path, job_sha, job_post_id), (status, value, retried, retry_model) in zip(jobs, outcomes):
            self._tried.add((job_path, job_sha))
            if retried:
                payload = {"path": job_path, "sha256": job_sha,
                           **({"model": retry_model} if retry_model else {})}
                ctx.board.log_event(self.name, "reader.retried", payload, post_id=job_post_id)
            if status == "failed":
                reason, raw = value if isinstance(value, tuple) else (value, None)
                payload = {"path": job_path, "sha256": job_sha, "reason": reason,
                           **({"raw": raw} if raw else {})}
                ctx.board.log_event(self.name, "reader.failed", payload, post_id=job_post_id)
            elif status == "skipped":
                ctx.board.log_event(self.name, "reader.skipped",
                                    {"path": job_path, "sha256": job_sha, "reason": value},
                                    post_id=job_post_id)
        status, value, _, _ = outcomes[0]
        claims = value if status == "claims" else []
        return [Post(kind="claim", status="proposed", subject=f"{c.entity}/{c.key}/{c.field}",
                     body=c.to_dict(), author=self.name, depends_on=(f"file:{path}@{sha}", post.id))
                for c in claims]

    @staticmethod
    def _wanted(change: dict[str, Any]) -> bool:
        return (str(change.get("path", "")).lower().endswith(SUPPORTED)
                and change.get("kind") in ("added", "changed"))

    def _pending(self, ctx: Context, current: Post, current_sha: str,
                 cache_dir: Path | str) -> list[tuple[str, str, str]]:
        """Other documents waiting for a read: latest event per path, not cached, not tried yet."""
        current_path = str(current.body.get("path", ""))
        latest: dict[str, tuple[str, str]] = {}
        for event in ctx.board.query(kind="event", subject_prefix="file.changed", limit=100000):
            if event.id != current.id and self._wanted(event.body) and event.body.get("sha256"):
                latest[str(event.body["path"])] = (str(event.body["sha256"]), event.id)
        pending = [(p, s, eid) for p, (s, eid) in latest.items()
                   if (p, s) != (current_path, current_sha) and (p, s) not in self._tried
                   and _raw_cache_get(cache_dir, p, s) is None]
        return pending[:PREFETCH_MAX]

    def _process(self, ctx: Context, snap: Snapshot, path: str, sha: str,
                 *, cache_dir: Path | str | None = None) -> Outcome:
        try:
            data = ctx.ports.files.read(path, sha)
            known = snap.known
            if path.lower().endswith(".pdf"):
                content = read_pdf(data)
                if content.has_text:
                    reason = skip_reason(content.text, snap)
                    if reason:
                        return "skipped", reason, False, None
                    flat = _collapse(content.text)
                    mentioned = [line for line in snap.closed_po_lines if line["po_number"] in flat]
                    known = {**known, "po_lines": [*known["po_lines"], *mentioned]}
        except Exception as exc:  # noqa: BLE001
            return "failed", f"{type(exc).__name__}: {exc}", False, None
        errors: list[str] = []
        retried: list[str] = []
        retry_models: list[str] = []
        raws: list[str] = []
        claims = read_document(path, data, ctx.llm, known=known, now_iso=snap.now.isoformat(),
                               on_error=errors.append, on_retry=retried.append,
                               on_retry_model=retry_models.append, on_raw=raws.append,
                               cache_dir=cache_dir or ctx.tenant.get("cache_dir", "var"))
        retry_model = retry_models[0] if retry_models else None
        if errors:
            return "failed", (errors[0], raws[0] if raws else None), bool(retried), retry_model
        return "claims", claims, bool(retried), retry_model


AGENTS = [DocumentReaderAgent()]
