"""Shared contracts: the types and interfaces every lane builds against.

Owned by lane A. Do not change this file in a lane PR. To request a change,
open a `contract` thread on the team board (`tools/team post --type contract`).

Everything here is stdlib only and JSON-serializable through `to_dict`.
Design reference: docs/design.md. Field vocabulary: ENTITIES below.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 1. Canonical vocabulary
# ---------------------------------------------------------------------------
# A claim is an assertion about one field of one canonical entity:
#   (entity, key, field) = value
# Keys join their parts with "|". Readers map every source onto this
# vocabulary; the verifier compares claims that share (entity, key, field).
# Extend only through a `contract` thread.

ENTITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    # entity: (key format, fields)
    "product": ("<product_id>", ("name", "demand_units", "preorder_units", "packed_units", "deadline")),
    "bom": ("<product_id>|<part_id>", ("qty_per",)),
    "line": ("<site>|<line_id>", ("product_id", "rate_per_hour")),
    "shift": ("<site>|<date>", ("start", "end")),
    "part": ("<part_id>", ("description", "category", "required_revision", "use_up_old_revision", "critical", "supplied_fitted_into")),
    "stock": ("<site>|<part_id>|<batch>", ("qty", "quality_status", "revision")),
    "receipt": ("<gr_number>", ("po_number", "po_line", "supplier_id", "part_id", "qty_ordered", "qty_received", "delivery_note", "date", "remark")),
    # One key per line item: "<note_number>" for a note with one item, "<note_number>|<line>" (1, 2, ...) when it lists several.
    "delivery_note": ("<note_number>", ("supplier_id", "part_id", "qty", "po_number", "date", "gross_weight_kg", "revision")),
    "po_line": ("<po_number>|<po_line>", ("supplier_id", "part_id", "qty", "unit_price", "currency", "incoterm", "promised_date", "status")),
    "order_confirmation": ("<po_number>|<part_id>|<doc_number>", ("supplier_id", "qty", "unit_price", "currency", "incoterm", "confirmed_date", "revision", "hold_reason")),
    "shipment": ("<shipment_id>", ("supplier_id", "carrier_id", "mode", "reference", "part_ids", "po_numbers", "qty", "qty_shipped", "origin", "destination", "dest_site", "etd", "eta", "eta_site", "status", "status_updated_at", "note")),
    "packout": ("<site>|<product_id>|<date>", ("units",)),
    "inspection": ("<gr_number>", ("part_id", "supplier_id", "lot_qty", "result", "defect_rate", "booked_status", "required_signoff", "concession")),
    "offer": ("<supplier_id>|<doc_number>|<item>", ("part_id", "qty", "unit_price", "currency", "surcharge_pct", "ready_hours", "transit_hours", "valid_until")),
    "supplier": ("<supplier_id>", ("name", "contact_email", "language", "release_status", "release_date", "release_valid_until", "incoterm")),
    "supplier_part": ("<supplier_id>|<part_id>", ("unit_price", "currency", "lead_time_days", "share", "revision", "note")),
    "contract_term": ("<supplier_id>|<term>", ("value",)),
    "notice": ("<supplier_id>|<doc_number>", ("kind", "start", "end", "detail")),
    "route": ("<route_id>", ("origin", "destination", "mode", "carrier_id", "transit_days")),
    "role": ("<role_id>", ("title", "person", "vacant", "valid_from", "reports_to")),
    "approval_rule": ("<rule_id>", ("action", "condition", "approver_roles", "source_section")),
    "record_owner": ("<entity>", ("role_id", "corroborate_with")),
    # Catch-all for anything a reader cannot map. Decision agents may read it.
    "observation": ("<source>|<n>", ("text",)),
}

# Value conventions. Readers map every source value onto these before they
# emit a claim; verifier, planner, gate and resolver rely on them.
#   - Quantities and prices: numbers (no thousands separators, decimal point).
#   - Dates: ISO "YYYY-MM-DD"; date-times: ISO 8601 with offset.
#   - Lists (part_ids, po_numbers): JSON lists of strings.
#   - incoterm: the three-letter code only, upper case ("EXW", not "EXW Boise").
#   - revision: the bare identifier ("B", not "rev. B").
#   - contract_term keys use lower-case term names: "<supplier_id>|incoterm".
#   - Status-like fields use exactly the values below.
VALUES: dict[str, tuple[str, ...]] = {
    "shipment.status": ("booked", "in_transit", "in_delivery", "arrived_port", "delayed", "delivered", "cancelled"),
    "shipment.mode": ("sea", "air", "road", "rail"),
    "stock.quality_status": ("unrestricted", "blocked"),
    "inspection.result": ("passed", "failed"),
    "inspection.booked_status": ("unrestricted", "blocked"),
    "supplier.release_status": ("qualified", "conditional", "blocked"),
    "po_line.status": ("open", "delivered", "cancelled", "awaiting_approval"),
    "notice.kind": ("downtime", "bank_change", "price_change", "offer", "other"),
}

# Slots whose value only grows over time. A newer, higher reading replaces the old one
# whatever its source; an older, lower reading is history, not a conflict.
COUNTERS: frozenset[tuple[str, str]] = frozenset({("product", "packed_units")})


def normalize_value(entity: str, field_name: str, raw: Any) -> Any:
    """Map a raw status-like value onto VALUES when it matches after cleanup.

    "In Transit" -> "in_transit". Returns the cleaned string unchanged when it
    is not in the allowed set, so the caller can decide (an LLM mapping, or an
    `observation`).
    """
    allowed = VALUES.get(f"{entity}.{field_name}")
    if allowed is None or not isinstance(raw, str):
        return raw
    cleaned = "_".join(raw.strip().lower().replace("-", " ").split())
    return cleaned


SourceKind = Literal["erp", "document", "tracker", "mail", "human", "config", "sim"]
# Verifier trust order when sources disagree and nothing else decides:
# a physical document or a direct answer beats a system booking.
SOURCE_PRIORITY: tuple[str, ...] = ("human", "mail", "document", "erp", "tracker", "config")


def make_key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


class _Dictable:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# 2. Evidence, claims, facts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Evidence(_Dictable):
    source: str  # data-root-relative file path, "mail:<message_id>", "human:<role_id>", "sim:<id>"
    sha256: str | None = None  # file version; None for mail/human/sim
    locator: str | None = None  # "row:12", "sheet:Parts;cell:F5", "page:1", "page:1;bbox:x0,y0,x1,y1"
    quote: str | None = None  # verbatim original text, original language
    translation: str | None = None  # English, when the quote is not English


@dataclass(frozen=True)
class Claim(_Dictable):
    entity: str
    key: str
    field: str
    value: Any  # JSON scalar or list of scalars
    as_of: str  # ISO 8601 with offset: when the source says this was true
    source_kind: SourceKind
    evidence: tuple[Evidence, ...]
    author: str  # agent name
    unit: str | None = None
    confidence: float = 1.0
    flags: tuple[str, ...] = ()  # e.g. "handwritten", "corrected", "low_confidence", "translated"

    @property
    def slot(self) -> tuple[str, str, str]:
        return (self.entity, self.key, self.field)


@dataclass(frozen=True)
class Fact(_Dictable):
    """A verified value for one slot. Only facts feed the planner."""

    entity: str
    key: str
    field: str
    value: Any
    as_of: str
    claim_ids: tuple[str, ...]  # supporting claim posts
    checks_passed: tuple[str, ...]
    unit: str | None = None
    quarantined: bool = False  # disputed: visible, but the planner must not use it


@dataclass(frozen=True)
class Conflict(_Dictable):
    entity: str
    key: str
    field: str
    claim_ids: tuple[str, ...]  # the claims that disagree
    check: str  # which verifier check failed
    detail: str
    preferred_claim_id: str | None = None  # by SOURCE_PRIORITY or a specific check
    group: tuple[str, ...] = ()  # keys of the records compared as a sum; the steward does not auto-correct these


DefectClass = Literal["format", "content", "missing", "stale"]


@dataclass(frozen=True)
class Defect(_Dictable):
    """A data-quality problem with an owner. Created by the data steward."""

    defect_class: DefectClass
    entity: str
    key: str
    detail: str
    conflict_id: str | None
    owner_role: str | None  # role_id of the record owner, None if a supplier owns it
    owner_supplier: str | None
    proposed_value: Any = None
    copies_at_risk: int = 0


# ---------------------------------------------------------------------------
# 3. Planning
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Arrival(_Dictable):
    part_id: str
    qty: float
    at_hour: int  # index into PlanInput.hours; parts are usable from this hour
    probability: float = 1.0
    ref: str = ""  # shipment_id or offer key


@dataclass(frozen=True)
class PlanInput(_Dictable):
    hours: tuple[str, ...]  # ISO start of each remaining pack-out hour, in order
    products: dict[str, float]  # product_id -> units still to pack
    lines: dict[str, tuple[str, float]]  # line_id -> (product_id, rate_per_hour)
    bom: dict[str, dict[str, float]]  # product_id -> {part_id: qty_per}
    stock: dict[str, float]  # part_id -> usable units now (quarantined and blocked excluded)
    arrivals: tuple[Arrival, ...]
    value_per_copy: dict[str, float]  # product_id -> EUR
    priority: tuple[str, ...] = ()  # product ids, first gets shared parts first when values tie
    consumed_since_snapshot: dict[str, float] = field(default_factory=dict)  # part_id -> units taken off stock
    stock_basis: dict[str, dict[str, Any]] = field(default_factory=dict)  # part_id -> {total, as_of, known_bad}
    not_rolled_forward: tuple[str, ...] = ()  # product ids with no packed reading at the stock snapshot time


@dataclass(frozen=True)
class Forecast(_Dictable):
    copies: dict[str, float]  # product_id -> units packed by the deadline (this run-out only)
    shortfall: dict[str, float]  # product_id -> units still missing at the deadline
    limiting_part: dict[str, str | None]  # product_id -> part that binds first, or None (line capacity)
    runs_out_at: dict[str, str | None]  # part_id -> ISO hour when it is exhausted, None if never
    shadow_price: dict[str, float]  # part_id -> EUR value of one more unit now
    per_hour: dict[str, tuple[float, ...]]  # product_id -> units per hour
    value_eur: float
    fact_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 4. Questions, options, actions, decisions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Question(_Dictable):
    text: str
    to_kind: Literal["supplier", "role", "carrier"]
    to_id: str  # supplier_id, role_id or carrier_id
    topic: str  # stable topic id: one open thread per (to_id, topic)
    copies_at_risk: int = 0
    deadline: str | None = None
    channel: Literal["supply", "quality"] = "supply"  # which inbox asks
    context_post_ids: tuple[str, ...] = ()


ActionKind = Literal[
    "send_email",  # params: to, subject, body, body_en, inbox, thread_id?
    "accept_offer",  # params: supplier_id, offer_key, part_id, qty, cost_eur
    "place_order",  # params: supplier_id, part_id, qty, unit_price, currency, cost_eur
    "confirm_terms",  # params: supplier_id, po_number, term, value
    "request_pickup",  # params: shipment_id
    "request_dock_slot",  # params: shipment_id or ref, when
    "request_concession",  # params: gr_number, part_id, qty
    "book_freight",  # params: mode, from, to, part_id, qty, cost_eur
    "data_fix_request",  # params: owner_role, entity, key, field, current, proposed
    "quarantine_batch",  # params: site, part_id, batch
    "set_priority",  # params: order (list of product ids)
]


@dataclass(frozen=True)
class Action(_Dictable):
    kind: ActionKind
    params: dict[str, Any]
    requested_by: str  # agent name
    reason: str
    idempotency_key: str
    cost_eur: float = 0.0  # committed spend, 0 for requests and questions
    context_post_ids: tuple[str, ...] = ()


GateOutcome = Literal["allowed", "needs_approval", "blocked"]


@dataclass(frozen=True)
class GateVerdict(_Dictable):
    outcome: GateOutcome
    rule_id: str | None
    reason: str
    approver_role: str | None = None  # resolved role (after vacancy routing)
    approver_person: str | None = None


@dataclass(frozen=True)
class Option(_Dictable):
    title: str
    actions: tuple[Action, ...]
    copies_saved: dict[str, float]  # product_id -> units, priced by the planner
    cost_eur: float
    net_value_eur: float
    notes: str = ""


@dataclass(frozen=True)
class Decision(_Dictable):
    """A card for a person in the control room."""

    problem: str  # one sentence
    options: tuple[Option, ...]
    recommended: int  # index into options
    approver_role: str
    approver_person: str | None
    deadline: str | None
    rule_id: str | None = None
    chosen: int | None = None  # set when answered
    answered_by: str | None = None


@dataclass(frozen=True)
class Notice(_Dictable):
    """Something an agent did alone, shown in the control room."""

    title: str
    detail: str
    action_id: str
    rule_id: str | None


@dataclass(frozen=True)
class BeliefDiff(_Dictable):
    trigger: str  # event id or post id
    changed: tuple[dict[str, Any], ...]  # {"slot": [...], "before": x, "after": y}
    forecast_before: dict[str, float]
    forecast_after: dict[str, float]
    summary: str


# ---------------------------------------------------------------------------
# 5. The Board (runtime, SQLite). Not the team board used by coding agents.
# ---------------------------------------------------------------------------
PostKind = Literal[
    "event", "claim", "fact", "conflict", "defect", "question", "forecast",
    "option", "decision", "action", "notice", "belief_diff",
]

# Events are posts of kind "event" whose subject is the event type and whose
# body is the payload (FileChange, InboundMail, HumanAnswer, or {} for ticks).
EVENT_TYPES: tuple[str, ...] = (
    "file.changed",  # body: FileChange
    "mail.received",  # body: InboundMail
    "human.answered",  # body: HumanAnswer
    "tick",  # body: {"now": iso}; hourly, and when a part is about to run out
    "settled",  # body: {}; the queue is empty and facts changed since the last forecast
    "control.pause",  # body: {}; kill switch on: the executor sends nothing
    "control.resume",  # body: {}; kill switch off: the executor sends what was held back
)

# Allowed statuses per kind. "stale" is set by the propagator on anything.
STATUSES: dict[str, tuple[str, ...]] = {
    "event": ("new",),
    "claim": ("proposed", "verified", "conflicted", "rejected", "stale"),
    "fact": ("active", "quarantined", "superseded", "stale"),
    "conflict": ("open", "resolved", "stale"),
    "defect": ("open", "requested", "fixed", "dismissed", "stale"),
    "question": ("open", "asked", "answered", "expired", "stale"),
    "forecast": ("current", "superseded"),
    "option": ("proposed", "chosen", "discarded", "stale"),
    "decision": ("open", "answered", "expired", "stale"),
    "action": ("requested", "allowed", "needs_approval", "approved", "rejected", "blocked", "executed", "failed", "stale"),
    "notice": ("posted", "stopped"),
    "belief_diff": ("posted",),
}


@dataclass
class Post(_Dictable):
    kind: PostKind
    status: str
    subject: str  # short human label; for slot-based kinds use "entity/key/field"
    body: dict[str, Any]  # to_dict() of the matching dataclass above
    author: str
    id: str = ""  # assigned by the board: "<kind>_<n>"
    created_at: str = ""
    updated_at: str = ""
    depends_on: tuple[str, ...] = ()  # post ids and "file:<path>@<sha256>"
    supersedes: str | None = None


@runtime_checkable
class Board(Protocol):
    def append(self, post: Post) -> Post: ...  # assigns id and timestamps, logs an event
    def get(self, post_id: str) -> Post | None: ...
    def query(self, kind: str | None = None, status: str | None = None,
              subject_prefix: str | None = None, since_event: int = 0, limit: int = 1000) -> list[Post]: ...
    def set_status(self, post_id: str, status: str, actor: str, note: str = "") -> Post: ...
    def facts(self, entity: str | None = None, key_prefix: str | None = None) -> list[Fact]: ...  # active, not quarantined
    def events_since(self, event_id: int) -> list[dict[str, Any]]: ...  # for SSE and replay
    def dependents(self, ref: str) -> list[str]: ...  # post ids that depend on a post id or file ref


# ---------------------------------------------------------------------------
# 6. Ports: the only way agents touch the outside world.
#    One real adapter and one simulated adapter per port (WORLD=real|sim).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileChange(_Dictable):
    path: str  # data-root-relative, e.g. "soony/erp/stock_snapshot.csv"
    kind: Literal["added", "changed", "removed"]
    sha256: str | None
    previous_sha256: str | None
    bytes: int = 0


class FileSource(Protocol):
    def poll(self) -> list[FileChange]: ...  # changes since the last poll; first poll returns everything as "added"
    def read(self, path: str, sha256: str | None = None) -> bytes: ...  # any version ever seen
    def local_path(self, path: str, sha256: str | None = None) -> str: ...


@dataclass(frozen=True)
class OutboundMail(_Dictable):
    inbox: str  # one of the tenant's agent inboxes
    to: str
    subject: str
    body: str
    thread_id: str | None = None
    idempotency_key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)  # {"action_kind": ..., "action_id": ..., "params": {...}} for commitments


@dataclass(frozen=True)
class InboundMail(_Dictable):
    message_id: str
    thread_id: str
    inbox: str
    sender: str
    subject: str
    body: str
    received_at: str
    attachments: tuple[str, ...] = ()  # local paths


class Mailbox(Protocol):
    def send(self, mail: OutboundMail) -> str: ...  # returns message_id; same idempotency_key never sends twice
    def poll(self) -> list[InboundMail]: ...  # new inbound mail since the last poll


@dataclass(frozen=True)
class HumanAnswer(_Dictable):
    post_id: str  # the decision or defect post being answered
    answered_by: str  # role_id
    choice: int | None = None  # option index for decisions
    approved: bool | None = None  # for approvals and data-fix requests
    text: str = ""
    answered_at: str = ""


class ControlRoom(Protocol):
    def publish(self, post: Post) -> None: ...  # decision, defect (data-fix request) or notice
    def poll(self) -> list[HumanAnswer]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware, tenant timezone


@dataclass(frozen=True)
class LLMResult(_Dictable):
    text: str  # the answer with any thinking removed
    data: dict[str, Any] | None  # parsed JSON when a schema was given
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning: str = ""  # stripped thinking (reasoning_content or <think> tags); for traces only
    provider: str = ""  # which entry of the role's fallback chain answered


class LLM(Protocol):
    def complete(self, role: str, system: str, user: str, *, schema: dict[str, Any] | None = None,
                 images: tuple[bytes, ...] = (), max_tokens: int | None = None) -> LLMResult: ...
    # role is a key in config/models.toml: reader | vision | agent | persona | reflect.
    # Implementations retry with backoff, limit in-flight requests and walk the
    # role's fallback chain. Callers never see thinking in `text`.


# ---------------------------------------------------------------------------
# 7. Agents
# ---------------------------------------------------------------------------
@dataclass
class Ports:
    files: FileSource
    mail: Mailbox
    control_room: ControlRoom
    clock: Clock


@dataclass
class Context:
    board: Board
    ports: Ports
    llm: LLM
    tenant: dict[str, Any]  # parsed tenants/<id>/tenant.toml
    world: Literal["real", "sim"]
    playbooks: dict[str, str] = field(default_factory=dict)  # agent name -> playbook text


class Agent(Protocol):
    """One unit of work on the Board.

    The dispatcher calls `handle` once per (agent, post, topic) for every
    post whose topic is in `subscribes`. Return new posts;
    never call another agent and never touch the outside world except through
    `ctx.ports` (and for outgoing actions, never directly: post an `action`).
    """

    name: str
    subscribes: tuple[str, ...]  # topics, e.g. "claim:proposed", "event:file.changed"

    def handle(self, post: Post, ctx: Context) -> list[Post]: ...


def topic(post: Post) -> str:
    """The topic a post is delivered under. A status change is a new topic."""
    return f"event:{post.subject}" if post.kind == "event" else f"{post.kind}:{post.status}"


AGENT_NAMES: tuple[str, ...] = (
    "watcher", "table_reader", "document_reader", "mail_reader", "verifier",
    "data_steward", "planner", "resolver", "supply_liaison", "customer_liaison",
    "gate", "executor", "propagator",
)
# Agents whose playbooks evolve in the Gym.
EVOLVING_AGENTS: tuple[str, ...] = ("resolver", "data_steward", "supply_liaison", "customer_liaison")
