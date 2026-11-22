"""Prompts and the answer schema for the document reader."""
from __future__ import annotations

from typing import Any

from gtl.contracts import ENTITIES, VALUES

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "doc_type": {"type": "string"},
        "doc_date": {"type": ["string", "null"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entity", "key", "field", "value", "quote"],
                "properties": {
                    "entity": {"type": "string"},
                    "key": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {},
                    "unit": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                    "translation": {"type": ["string", "null"]},
                    "page": {"type": ["integer", "null"]},
                    "flags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

NUMBERS_SYSTEM = (
    "You read business documents. List every number on this document with its label and whether it is "
    "printed, handwritten or crossed out. One line per number, for example: 'quantity 8 000: handwritten'. "
    "Do not interpret or summarise.")

NUMBERS_USER = "List every number on the attached document."

CONVENTIONS = """\
Value conventions:
- Quantities and prices are plain numbers: no thousands separators, a decimal point. "8 000", "8.000", "8,000" and "47.400" are whole counts (8000 and 47400) for a quantity, not 8 or 47.4.
- Dates are ISO "YYYY-MM-DD". Date-times are ISO 8601 ("YYYY-MM-DDTHH:MM").
- Lists (part_ids, po_numbers) are JSON lists of strings.
- incoterm is the three-letter code only, upper case ("EXW", not "EXW Boise").
- revision is the bare identifier ("B", not "rev. B").
- contract_term keys use lower-case term names: "<supplier_id>|incoterm".
- Status-like fields use exactly the listed values."""

DOCUMENT_HINTS = """\
What to extract, by document type (one document may hold several):
- delivery note or packing list: entity delivery_note, key = "<note_number>" when it lists one item, or "<note_number>|<line>" (1, 2, ...) when it lists several items (never reuse "<note_number>" across items); fields supplier_id, part_id, qty, po_number, date, gross_weight_kg, revision (artwork or drawing revision, when stated). On a multi-line note, emit each item's own part_id and qty under its line key and repeat supplier_id, po_number and date on every line key. Never date a delivery note after the receiving date in its file path or many months before it (resolve DD/MM vs MM/DD consistently with the receiving date).
- order confirmation or acknowledgement: entity order_confirmation, key "<po_number>|<part_id>|<doc_number>"; fields supplier_id, qty, unit_price, currency, incoterm, confirmed_date, revision (artwork or drawing revision, when stated), hold_reason (when the supplier says the shipment is on hold, and why).
- shipping advice: entity shipment, key = our shipment id; fields qty_shipped, eta, note. An air waybill or container number is a shipment reference: map it to our shipment id.
- offer or rush offer: entity offer, key "<supplier_id>|<doc_number>|<item>"; fields part_id, qty, unit_price, currency, surcharge_pct, ready_hours (hours until goods are ready or loaded, e.g. "loaded within 4 h" -> 4), transit_hours (delivery or driving hours, e.g. "about 9 h driving" -> 9), valid_until. Put all fields for an offered item — including any shared transport transit_hours stated elsewhere on the offer — on that item's own key; never create a separate offer key just for transport.
- notice (downtime, bank change, price change): entity notice, key "<supplier_id>|<doc_number>"; fields kind (one of the notice.kind values), start, end, detail.
- inspection report: entity inspection, key = the goods receipt number; fields part_id, supplier_id, lot_qty, result (passed or failed), defect_rate, booked_status, required_signoff.
- contract: entity contract_term, key "<supplier_id>|incoterm" and other clearly stated terms as "<supplier_id>|<term>"; field value.
- org chart: entity role, key = lowercase snake_case role id (for example buyer, senior_buyer, category_lead, head_of_procurement, cpo, cfo, site_lead, logistics_planner, supplier_quality_lead); fields title, person, vacant (boolean), valid_from (ISO date when a future start date like "from YYYY-MM-DD" is given), reports_to (manager role id).
- approval policy: entity approval_rule, key = short rule id (for example "<action>_<scope>"); fields action, condition, approver_roles (list of role ids), source_section; and record_owner (key = entity name, fields role_id, corroborate_with) when ownership is stated."""

RULES = """\
Rules:
- Use only the entities and fields listed above.
- Keys follow the documented key format and use the KNOWN ids below. Map any supplier name to its id in KNOWN suppliers; never put a supplier name in supplier_id (if no known supplier matches, leave supplier_id out and emit an observation). Map a supplier's own part number or a description to our part id through KNOWN parts and PO lines; never put a supplier's own/foreign part number in part_id (if only a foreign number is shown and cannot be resolved, leave part_id out and put it in an observation). Map an air waybill or container number to our shipment id through KNOWN shipments. If an id cannot be resolved, emit an observation instead of guessing.
- Every claim needs a verbatim "quote" copied from the document in its original language, and a "translation" into English when the quote is not English.
- A handwritten value gets the flag "handwritten".
- When a printed value is crossed out and replaced, emit the replacement with the flags "handwritten" and "corrected", and a second claim for the same slot with the printed value flagged "struck_out".
- Anything useful that fits no entity becomes an observation: key "<file path>|<n>" (n counts from 1), field "text".
- Do not invent values. Leave out what the document does not say.
- Reply with one JSON object: {"doc_type": "...", "doc_date": "YYYY-MM-DD or null", "claims": [{"entity", "key", "field", "value", "unit", "quote", "translation", "flags", "page"}]}."""


def render_vocabulary() -> str:
    lines = ["Vocabulary (entity, key format, fields):"]
    for entity, (key_format, fields) in ENTITIES.items():
        lines.append(f"- {entity}, key {key_format}: {', '.join(fields)}")
    lines.append("Allowed values:")
    for name, allowed in VALUES.items():
        lines.append(f"- {name}: {', '.join(allowed)}")
    return "\n".join(lines)


def render_known(known: dict[str, Any] | None, limit: int = 14000) -> str:
    """Compact text for the known context, cut at `limit` characters so the prompt stays small."""
    if not known:
        return "KNOWN ids: none available."
    lines = ["KNOWN ids (use these in keys):"]
    if known.get("site"):
        lines.append(f"site: {known['site']}")
    if known.get("timezone"):
        lines.append(f"timezone: {known['timezone']}")
    for product in known.get("products", []):
        line = f"product {product.get('id')}: {product.get('name', '')}"
        if product.get("deadline"):
            line += f" (deadline {product['deadline']})"
        lines.append(line)
    for part in known.get("parts", []):
        lines.append(f"part {part.get('id')}: {part.get('description', '')}")
    for supplier in known.get("suppliers", []):
        lines.append(f"supplier {supplier.get('id')}: {supplier.get('name', '')}")
    for shipment in known.get("shipments", []):
        lines.append(f"shipment {shipment.get('id')}: reference {shipment.get('reference', '')}, "
                     f"POs {'|'.join(map(str, shipment.get('po_numbers') or []))}, "
                     f"parts {'|'.join(map(str, shipment.get('part_ids') or []))}")
    for line in known.get("po_lines", []):
        lines.append(f"po {line.get('po_number')}/{line.get('po_line')}: part {line.get('part_id')}, "
                     f"supplier {line.get('supplier_id')}, {line.get('status')}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n(list cut)"
    return text


SHEET_HINTS = """\
This text comes from spreadsheet sheets or a markdown file of the customer's own planning, not from a supplier. Each sheet is one page: a line "sheet: <name>", then one line per row with cell references such as "B4=FG-1". Other tables of the same workbook are read elsewhere, so emit ONLY these entities and use observation for anything else worth keeping:
- product: key = the product id (use ONLY ids listed under KNOWN ids; if no matching product ids are listed under KNOWN ids yet, emit no product claim and never guess or invent an id); fields demand_units (day-one demand), preorder_units, packed_units (already packed), deadline (when production must be done, for example the sales start: ISO 8601 date-time with offset, e.g. "2026-11-22T20:00:00+01:00"). A launch or on-sale date-time stated for a product line (for example in notes or a launch plan) is product.deadline for every known product of that line with a launch lifecycle (matching its name or launch date, not older running editions), as an ISO date-time with the tenant offset. A sheet row with a product column gives the numbers; the notes may give the deadline.
- line: key "<site>|<line name>" (the site is in the KNOWN ids; the line name is the name used for the line or cell in the sheets); fields product_id (the product the line packs) and rate_per_hour (units per hour of that line). Use one line entity per named line or group of lines that packs one product, and the rate from the row of that product.
- shift: key "<site>|<date>" with the date as ISO "YYYY-MM-DD"; fields start and end as "HH:MM" (24 hours). A weekday name (Saturday, Sunday) means the next such day on or after the workbook's status date (e.g. if the title line says "status 2026-11-20 18:00" [Friday], Saturday is "2026-11-21" and Sunday is "2026-11-22"). A range such as "06:00-22:00" or "06:00–22:00" gives start 06:00 and end 22:00; always emit both start and end claims for each shift row. Skip rows that are not a shift, such as totals.
- Set doc_date to the status date and time of the workbook (ISO with the time when one is given, or weekday and time such as "Saturday 13:00" when no calendar date is written) or to the date of the document.
- Every claim needs a verbatim quote copied from the line of the page it comes from. Always reply with the JSON object containing "claims" (use {"doc_type": "sheet", "doc_date": null, "claims": []} when nothing applies)."""


def build_system(path: str, known: dict[str, Any] | None, hints: str | None = None) -> str:
    return "\n\n".join([
        "You turn one business document (PDF text, scan or photo) into evidence-backed claims in a fixed "
        "vocabulary. Documents may be in any language and may contain handwriting.",
        render_vocabulary(), CONVENTIONS, hints or DOCUMENT_HINTS, render_known(known),
        f"The file path of this document is: {path}", RULES])


def build_user(path: str, pages: list[str] | None, limit: int = 40000) -> str:
    if pages is None:
        return f"Read the attached document image(s) ({path}) and answer with the JSON object."
    body = "\n".join(f"--- page {n} ---\n{text}" for n, text in enumerate(pages, 1))
    if len(body) > limit:
        body = body[:limit] + "\n(text cut)"
    return f"Document text ({path}):\n{body}\n\nAnswer with the JSON object."
