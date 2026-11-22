"""Follow-up rules for supplier questions: how often we ask, how long we wait, and who else could help.

The numbers are settings, not code. The tenant file gives the start values under `[follow_up]`;
the newest `config.changed` event on the Board overrides them, so a person can change them in the
control room while the system runs and the change itself stays on record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gtl.contracts import ENTITIES

CONFIG_EVENT = "config.changed"  # payload: {"follow_up": {"asks": int, "wait_hours": [number, ...]}}
FOLLOW_UP_EVENT = "supply.follow_up"  # one per email we ask for: its questions, which ask it is, when the next step is due
NO_ANSWER_EVENT = "supply.no_answer"  # the last ask went unanswered: what we looked at instead
DEFAULT_ASKS = 3  # emails per bundled mail in total: the mail and two reminders
DEFAULT_WAIT_HOURS = 4.0
MAX_ASKS = 5
MAX_WAIT_HOURS = 14 * 24.0


@dataclass(frozen=True)
class FollowUp:
    asks: int
    wait_hours: tuple[float, ...]  # wait after the 1st, 2nd, ... email; one entry per ask

    def wait_after(self, ask: int) -> float:
        """Hours to wait after email number `ask` (1-based) before the next step."""
        return self.wait_hours[min(max(ask, 1), len(self.wait_hours)) - 1]

    def to_dict(self) -> dict[str, Any]:
        return {"asks": self.asks, "wait_hours": list(self.wait_hours)}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value <= MAX_WAIT_HOURS else None


def parse(raw: Any, base: FollowUp | None = None) -> FollowUp | None:
    """Settings from a `[follow_up]` table or an event payload. None when a value is not usable.

    A missing key keeps the value of `base`. `wait_hours` may be one number for every wait or a
    list; a list shorter than `asks` repeats its last entry.
    """
    base = base or FollowUp(DEFAULT_ASKS, (DEFAULT_WAIT_HOURS,) * DEFAULT_ASKS)
    if not isinstance(raw, dict):
        return None
    asks = raw.get("asks", base.asks)
    if isinstance(asks, bool) or not isinstance(asks, int) or not 1 <= asks <= MAX_ASKS:
        return None
    waits_raw = raw.get("wait_hours", list(base.wait_hours))
    if not isinstance(waits_raw, (list, tuple)):
        waits_raw = [waits_raw]
    waits = [_number(w) for w in waits_raw]
    if not waits or any(w is None for w in waits):
        return None
    usable = [w for w in waits if w is not None][:asks]
    usable += [usable[-1]] * (asks - len(usable))
    return FollowUp(asks, tuple(usable))


def settings(tenant: dict[str, Any], events: list[dict[str, Any]] | None = None) -> FollowUp:
    """The follow-up settings in force: defaults, then the tenant file, then the newest valid change."""
    current = parse(tenant.get("follow_up") or {}) or parse({})
    assert current is not None
    for event in reversed(events or []):
        if event.get("type") != CONFIG_EVENT:
            continue
        changed = (event.get("payload") or {}).get("follow_up")
        if changed is None:
            continue
        newer = parse(changed, current)
        if newer is not None:
            return newer
    return current


# -- who else could help -----------------------------------------------------
def _parts_of(body: dict[str, Any]) -> list[str]:
    """The parts a fact, claim or conflict is about, read from its field or its key."""
    entity, key, field, value = body.get("entity"), body.get("key"), body.get("field"), body.get("value")
    if field == "part_id" and isinstance(value, str):
        return [value]
    if field == "part_ids" and isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if entity in ENTITIES and isinstance(key, str):
        names = ENTITIES[entity][0].split("|")
        pieces = key.split("|")
        if "<part_id>" in names and len(pieces) == len(names):
            return [pieces[names.index("<part_id>")]]
    return []


def parts_in_question(board: Any, context_post_ids: tuple[str, ...]) -> list[str]:
    """Part ids named by the posts a question depends on, in first-seen order."""
    seen: list[str] = []
    for post_id in context_post_ids:
        post = board.get(post_id)
        if post is None:
            continue
        bodies = [post.body]
        if post.kind == "conflict":
            bodies += [c.body for cid in post.body.get("claim_ids") or () if (c := board.get(cid)) is not None]
        for body in bodies:
            for part in _parts_of(body):
                if part and part not in seen:
                    seen.append(part)
    return seen


def other_sources(board: Any, supplier_id: str, parts: list[str]) -> dict[str, list[str]]:
    """For each part, the other suppliers on record for it: listed sources and suppliers with an offer."""
    found: dict[str, list[str]] = {}
    if not parts:
        return found
    for fact in board.facts("supplier_part"):
        other, _, part = fact.key.partition("|")
        if part in parts and other and other != supplier_id and other not in found.setdefault(part, []):
            found[part].append(other)
    for fact in board.facts("offer"):
        other = fact.key.split("|")[0]
        if fact.field == "part_id" and fact.value in parts and other and other != supplier_id \
                and other not in found.setdefault(str(fact.value), []):
            found[str(fact.value)].append(other)
    return {part: sorted(ids) for part, ids in found.items() if ids}
