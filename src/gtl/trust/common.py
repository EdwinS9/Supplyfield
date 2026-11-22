"""Shared helpers for the verifier and the data steward. Pure functions."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from gtl.contracts import SOURCE_PRIORITY, Board, Fact, Post

# Slots where the newer reading wins before the source ranking is consulted.
TIME_FIRST_SLOTS: frozenset[tuple[str, str]] = frozenset({("shipment", "status")})

QTY_TOLERANCE = 0.01


def slot_subject(entity: str, key: str, field: str) -> str:
    return f"{entity}/{key}/{field}"


def conflict_subject(check: str, entity: str, key: str, field: str) -> str:
    return f"{check}/{entity}/{key}/{field}"


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None


def _norm(v: Any) -> str:
    return str(v).strip().casefold()


def values_match(a: Any, b: Any) -> bool:
    """Numbers within 1 percent, strings equal after strip and casefold, lists as sets."""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        la = a if isinstance(a, (list, tuple)) else [a]
        lb = b if isinstance(b, (list, tuple)) else [b]
        return {_norm(x) for x in la} == {_norm(x) for x in lb}
    na, nb = to_number(a), to_number(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= QTY_TOLERANCE * max(abs(na), abs(nb))
    return _norm(a) == _norm(b)


def parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _as_of_key(body: dict[str, Any]) -> float:
    t = parse_ts(body.get("as_of"))
    return t.timestamp() if t else 0.0


SAME_TIME_SECONDS = 3600  # readings this close in time do not outrank each other by time


def pick_preferred(claims: list[tuple[str, dict[str, Any]]]) -> str | None:
    """Choose a claim id: source ranking, then newer as_of. Some slots use time first.

    For a time-first slot, only a reading that is clearly newer wins by time. Readings within
    SAME_TIME_SECONDS of the newest one (for example two files read in the same run, both dated by
    the time they were read) are decided by source ranking.
    """
    if not claims:
        return None
    if all((b.get("entity"), b.get("field")) in TIME_FIRST_SLOTS for _, b in claims):
        # Time decides first: keep the readings as new as the newest one, then rank them by source.
        newest = max(_as_of_key(b) for _, b in claims)
        claims = [c for c in claims if newest - _as_of_key(c[1]) < SAME_TIME_SECONDS]

    def rank(kind: Any) -> int:
        return SOURCE_PRIORITY.index(kind) if kind in SOURCE_PRIORITY else len(SOURCE_PRIORITY)

    return min(claims, key=lambda item: (rank(item[1].get("source_kind")), -_as_of_key(item[1])))[0]


def evidence_sources(body: dict[str, Any]) -> set[str]:
    return {str(e.get("source")) for e in body.get("evidence", []) if e.get("source")}


def fact_table(board: Board) -> dict[str, dict[str, dict[str, Fact]]]:
    """entity -> key -> field -> Fact, over current (active, not quarantined) facts."""
    out: dict[str, dict[str, dict[str, Fact]]] = defaultdict(lambda: defaultdict(dict))
    for f in board.facts():
        out[f.entity][f.key][f.field] = f
    return out


def find_fact_post(board: Board, entity: str, key: str, field: str,
                   statuses: tuple[str, ...] = ("active", "quarantined")) -> Post | None:
    """The newest fact post for a slot, trying statuses in order."""
    subject = slot_subject(entity, key, field)
    for status in statuses:
        hits = [p for p in board.query(kind="fact", status=status, subject_prefix=subject, limit=100_000)
                if p.subject == subject]
        if hits:
            return hits[-1]
    return None
