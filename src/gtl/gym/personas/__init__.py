"""Fact-grounded LLM counterparts for simulated suppliers and staff."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Iterator

from gtl.contracts import HumanAnswer, Post
from gtl.gym.scripted import scripted_answer, scripted_reply

_NUMBER = re.compile(r"(?<!\w)[+-]?\d[\d,]*(?:\.\d+)?%?(?!\w)")
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?:[Tt ][0-2]?\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_LOCAL_DATE = re.compile(r"\b(?P<year>\d{4})[./年년](?P<month>\d{1,2})[./月월](?P<day>\d{1,2})日?\b")
_DAY_FIRST_DATE = re.compile(r"\b(?P<first>\d{1,2})[./](?P<second>\d{1,2})[./](?P<year>\d{4})\b")
_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._/-]*[A-Za-z])(?=[A-Za-z0-9._/-]*\d)"
    r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*(?![A-Za-z0-9])"
)
_COMMITMENT_WORDS = re.compile(
    r"\b(?:we\s+(?:can|will|shall)|i\s+(?:can|will)|promise|commit(?:ment)?|guarantee|"
    r"możemy\s+(?:wysłać|dostarczyć|zapewnić)|wyślemy|dostarczymy|zobowiązujemy się)\b"
    r"|(?:我们|我們)?(?:可以|将|將|会|會)(?:发货|發貨|提供|交付)|承诺|承諾"
    r"|(?:배송|공급|제공)(?:할 수 있습니다|하겠습니다|가능합니다)|약속",
    re.IGNORECASE,
)
_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_paths": {"type": "array", "items": {"type": "string"}},
        "commitment_paths": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fact_paths", "commitment_paths"],
    "additionalProperties": False,
}
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
_PRIVATE_FACT_FIELDS = {"language", "forthcoming", "honest"}
_LANGUAGES = {"en": "English", "ko": "Korean", "zh": "Chinese", "pl": "Polish"}
_TOPIC_WORDS = {
    "time": re.compile(r"\b(?:when|date|eta|etd|arrival|arrive\w*|deliver\w*|ready|transit)\b", re.I),
    "quantity": re.compile(r"\b(?:how many|quantity|qty|amount|stock|units?|available)\b", re.I),
    "price": re.compile(r"\b(?:price|cost|charge|how much|eur|usd|currency|surcharge)\b", re.I),
    "status": re.compile(r"\b(?:status|shipped|shipment|delivered|hold|held|release|terms)\b", re.I),
    "offer": re.compile(r"\b(?:offer|quote|quotation)\b", re.I),
}
_TOPIC_FIELDS = {
    "time": {"arrives_at", "arrival", "arrival_date", "date", "deadline", "eta", "eta_site", "etd",
             "ready_hours", "transit_hours", "valid_until"},
    "quantity": {"amount", "available", "qty", "qty_ordered", "qty_shipped", "quantity", "spare_stock",
                  "stock", "units"},
    "price": {"cost", "currency", "price", "surcharge_pct", "unit_price"},
    "status": {"delivered", "hold_reason", "holds", "incoterm", "released", "status", "terms"},
}
_TIME_QUESTION = re.compile(r"^\s*(?:when|by what date|what date|what time|how soon)\b", re.I)
_DIRECT_COMMITMENT_REQUEST = re.compile(
    r"\b(?:offer|quote|quotation|commit|promise|confirm the offer|can you (?:ship|supply|provide)|"
    r"will you (?:ship|supply|provide))\b",
    re.I,
)
_OPPOSITE_APPROVAL = re.compile(r"\b(?:decline|declined|reject|rejected|not approve|cannot approve)\b", re.I)
_OPPOSITE_DECLINE = re.compile(r"\b(?:approve|approved|accept|accepted|agree|agreed)\b", re.I)


def _data(result: Any) -> dict[str, Any]:
    if isinstance(getattr(result, "data", None), dict):
        return result.data
    from gtl.llm import extract_json

    return extract_json(str(getattr(result, "text", "")))


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten leaf values into paths the structure step may select."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            out.update(_flatten(child, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def _nodes(value: Any, prefix: str = "") -> dict[str, Any]:
    """Index containers too, so a commitment can point to an offer object."""
    out = {prefix: value} if prefix else {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_nodes(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            out.update(_nodes(child, f"{prefix}[{index}]"))
    return out


def _field(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path.rsplit(".", 1)[-1]).lower()


def _disclosable(path: str) -> bool:
    return _field(path) not in _PRIVATE_FACT_FIELDS


def _string_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _number_value(token: str) -> tuple[Decimal, bool] | None:
    percent = token.endswith("%")
    raw = token[:-1] if percent else token
    if "," in raw:
        if "." in raw:
            raw = raw.replace(",", "")
        else:
            whole, separator, fraction = raw.rpartition(",")
            if separator and len(fraction) in (1, 2):
                raw = f"{whole.replace(',', '')}.{fraction}"
            else:
                raw = raw.replace(",", "")
    try:
        return Decimal(raw), percent
    except InvalidOperation:
        return None


def _number_values(value: Any) -> set[tuple[Decimal, bool]]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, dict):
        return set().union(*(_number_values(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_number_values(item) for item in value)) if value else set()
    out: set[tuple[Decimal, bool]] = set()
    for token in _NUMBER.findall(str(value)):
        parsed = _number_value(token)
        if parsed is not None:
            out.add(parsed)
    return out


def _date_values(value: str) -> set[str]:
    out: set[str] = set()
    for match in _ISO_DATE.finditer(value):
        out.add(f"{int(match['year']):04d}-{int(match['month']):02d}-{int(match['day']):02d}")
    for match in _LOCAL_DATE.finditer(value):
        out.add(f"{int(match['year']):04d}-{int(match['month']):02d}-{int(match['day']):02d}")
    for match in _DAY_FIRST_DATE.finditer(value):
        first, second = int(match["first"]), int(match["second"])
        day, month = (first, second) if first > 12 else (second, first)
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.add(f"{int(match['year']):04d}-{month:02d}-{day:02d}")
    return out


def _reference_values(value: str) -> set[str]:
    return {item.upper() for item in _REFERENCE.findall(value)}


def _grounding_material(value: Any, paths: tuple[str, ...] = ()) -> tuple[set[tuple[Decimal, bool]], set[str], set[str]]:
    numbers = _number_values(value)
    dates: set[str] = set()
    references: set[str] = set()
    for item in _string_values(value):
        found_dates = _date_values(item)
        dates.update(found_dates)
        for date in found_dates:
            numbers.update(_number_values(date))
        references.update(_reference_values(item))
    for path in paths:
        for segment in re.split(r"[.\[\]]+", path):
            references.update(_reference_values(segment))
    return numbers, dates, references


def _is_grounded(text: str, material: tuple[set[tuple[Decimal, bool]], set[str], set[str]]) -> bool:
    allowed_numbers, allowed_dates, allowed_references = material
    if not _number_values(text).issubset(allowed_numbers):
        return False
    if not _date_values(text).issubset(allowed_dates):
        return False
    if not _reference_values(text).issubset(allowed_references):
        return False
    return True


def _mail_body(mail: Any) -> str:
    if isinstance(mail, dict):
        return str(mail.get("body", ""))
    return str(getattr(mail, "body", ""))


def _asked_topics(question: str) -> set[str]:
    return {topic for topic, pattern in _TOPIC_WORDS.items() if pattern.search(question)}


def _asked_path(path: str, topics: set[str]) -> bool:
    field = _field(path)
    return "offer" in topics and path.startswith("offers.") or any(
        field in _TOPIC_FIELDS.get(topic, set()) for topic in topics
    )


def _requested_commitment(question: str) -> bool:
    if _TIME_QUESTION.search(question):
        return False
    return bool(_DIRECT_COMMITMENT_REQUEST.search(question))


def _forthcoming(traits: dict) -> float:
    try:
        return max(0.0, min(1.0, float(traits.get("forthcoming", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def _language(sheet: dict, traits: dict) -> tuple[str, str]:
    code = str(sheet.get("language") or traits.get("language") or "en").strip().lower()
    return code, _LANGUAGES.get(code, f"the language identified as {code}")


def _reply_structure(llm: Any, sheet: dict, traits: dict, mail: Any) -> dict[str, Any]:
    result = llm.complete(
        "persona",
        "You are a simulated supplier. Return JSON structure only. Select fact_paths that directly answer the email. "
        "List a commitment_path only when the fact sheet contains an offer you are willing to make. "
        "Every path must be copied exactly from the fact sheet; do not write facts, values, dates, numbers, or prose.",
        json.dumps({"fact_sheet": sheet, "traits": traits, "mail": mail.to_dict() if hasattr(mail, "to_dict") else mail},
                   ensure_ascii=False, default=str),
        schema=_REPLY_SCHEMA,
        max_tokens=700,
    )
    data = _data(result)
    if set(data) != {"fact_paths", "commitment_paths"}:
        raise ValueError("persona structure has unexpected fields")
    return data


def _checked_reply_structure(sheet: dict, traits: dict, mail: Any, structure: dict[str, Any]) -> tuple[dict, list[str]]:
    leaves = _flatten(sheet)
    nodes = _nodes(sheet)
    fact_paths = structure["fact_paths"]
    commitment_paths = structure["commitment_paths"]
    if not isinstance(fact_paths, list) or not isinstance(commitment_paths, list):
        raise ValueError("persona paths must be arrays")
    if any(not isinstance(path, str) or path not in leaves or not _disclosable(path) for path in fact_paths):
        raise ValueError("persona selected a fact outside the fact sheet")
    if any(not isinstance(path, str) or not path.startswith("offers.") or path not in nodes
           or not isinstance(nodes[path], dict) or not nodes[path] for path in commitment_paths):
        raise ValueError("persona selected a commitment without a fact-sheet offer")

    selected = list(dict.fromkeys(fact_paths))
    commitments = list(dict.fromkeys(commitment_paths))
    if _forthcoming(traits) < 0.35:
        question = _mail_body(mail)
        topics = _asked_topics(question)
        selected = [path for path in selected if _asked_path(path, topics)]
        if not _requested_commitment(question):
            commitments = []

    verified = {path: leaves[path] for path in selected}
    for path in commitments:
        verified[path] = nodes[path]
    return verified, commitments


def _llm_reply(llm: Any, sheet: dict, traits: dict, mail: Any) -> str:
    structure = _reply_structure(llm, sheet, traits, mail)
    verified, commitments = _checked_reply_structure(sheet, traits, mail, structure)

    language_code, language_name = _language(sheet, traits)
    final = llm.complete(
        "persona",
        f"Write a concise supplier email in {language_name} (language code {language_code}). Use only the checked "
        "facts and listed commitments in the input. Preserve every number, date, and reference exactly as supplied. "
        "Do not add facts, explanations, promises, or commitments that are absent from the checked structure. "
        "If no checked fact answers the question, say briefly that you cannot confirm it.",
        json.dumps({"verified_facts": verified, "verified_commitments": commitments,
                    "question": _mail_body(mail),
                    "style": "terse" if _forthcoming(traits) < 0.35 else "normal"},
                   ensure_ascii=False, default=str),
        max_tokens=500,
    )
    text = str(getattr(final, "text", "")).strip()
    material = _grounding_material(verified, tuple(verified))
    has_commitments = bool(commitments)
    if not text or not _is_grounded(text, material):
        return scripted_reply(sheet, traits, mail)
    if _COMMITMENT_WORDS.search(text) and not has_commitments:
        return scripted_reply(sheet, traits, mail)
    return text


def reply_to_mail(fact_sheet: dict, traits: dict, mail: Any, llm: Any) -> str:
    """Build a supplier reply from checked fact paths and offer commitments."""
    # Keep compatibility with the runner on main until its call order is aligned.
    if hasattr(fact_sheet, "complete"):
        fact_sheet, traits, mail, llm = traits, mail, llm, fact_sheet
    try:
        return _llm_reply(llm, fact_sheet, traits, mail)
    except Exception:  # a persona failure must not derail a Gym episode
        return scripted_reply(fact_sheet, traits, mail)


def _answer_person(person: Any) -> str:
    if isinstance(person, dict):
        return str(person.get("role_id", "unknown role"))
    return str(getattr(person, "role_id", "unknown role"))


def _customer_description(person: Any, post: Post) -> str:
    keys = ("company", "company_name", "customer", "customer_name")
    for source in (person, post.body):
        for key in keys:
            value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
            if isinstance(value, dict):
                value = value.get("name") or value.get("company")
            if isinstance(value, str) and value.strip():
                return f"an employee of {value.strip()}"
    return "an employee of the customer"


def _scripted_person(person: Any) -> Any:
    if isinstance(person, dict):
        return SimpleNamespace(role_id=person.get("role_id", "unknown role"))
    return person


def answer_item(person: Any, post: Post, llm: Any) -> HumanAnswer:
    """Let the role speak, while code makes the cost-based approval decision."""
    # Compatibility with the runner on main, which still passes llm first.
    if hasattr(person, "complete"):
        person, post, llm = post, llm, person
    fallback = scripted_answer(_scripted_person(person), post)
    role_id = _answer_person(person)
    customer = _customer_description(person, post)
    try:
        result = llm.complete(
            "persona",
            f"You are {customer} in role {role_id}. Write in first person as that role. "
            "Do not claim to be another person or role. The decision shown in the input is final; write a concise "
            "matching response using only the supplied post.",
            json.dumps({"role_id": role_id, "decision": {"choice": fallback.choice,
                        "approved": fallback.approved}, "post": post.to_dict()},
                       ensure_ascii=False, default=str),
            schema=_ANSWER_SCHEMA,
            max_tokens=300,
        )
        data = _data(result)
        if set(data) != {"text"} or not isinstance(data["text"], str):
            return fallback
        text = data["text"].strip()
        material = _grounding_material(post.body)
        if not text or not _is_grounded(text, material):
            return fallback
        if fallback.approved and _OPPOSITE_APPROVAL.search(text):
            return fallback
        if fallback.approved is False and _OPPOSITE_DECLINE.search(text):
            return fallback
        return HumanAnswer(post_id=post.id, answered_by=role_id, choice=fallback.choice,
                           approved=fallback.approved, text=text)
    except Exception:  # scripted behavior keeps the simulation moving
        return fallback


__all__ = ["reply_to_mail", "answer_item"]
