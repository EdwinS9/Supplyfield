"""The two liaisons, the system's voice.

The supply liaison writes to suppliers and carriers. The customer liaison asks
the customer's own people to fix their records. Both only post: mails and fix
requests leave as `action` posts that the gate rules on and the executor sends.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from gtl.comms import follow_up, negotiation
from gtl.config import supplier_directory
from gtl.contracts import Action, Context, Notice, Post, Question

SUPPLY = "supply_liaison"
CUSTOMER = "customer_liaison"
FIX_WINDOW_HOURS = 2  # how long a record owner gets, unless a part runs out sooner
MIN_FIX_MINUTES = 15  # a request is never due sooner than this
DEFAULT_MAX_MAILS_PER_DAY = 3
DEFAULT_MAX_ITEMS_PER_REQUEST = 10
_LIVE_ACTION = ("requested", "allowed", "needs_approval", "approved", "executed")

MAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}, "body_en": {"type": "string"}},
    "required": ["subject", "body", "body_en"],
}
FIX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "ask": {"type": "string"}},
    "required": ["title", "detail", "ask"],
}

SUPPLY_SYSTEM = (
    "You write one short business email for the procurement team of {company} to a supplier or "
    "carrier. Write `body` in the language with ISO code {language}; write `body_en` as the same "
    "mail in English. Rules: keep it short. Make one clear request; number the points if there are "
    "several. Quote the reference numbers the recipient knows (purchase order number, their "
    "document number, air waybill) exactly as given. Say by when we need the answer. Ask for the "
    "answer in a form we can read: quantities with units, dates with times, reference numbers. "
    "Never promise, order or accept anything; you only ask. Sign as the procurement team of "
    "{company}. {follow_up}{negotiation}Answer with JSON only: {{\"subject\", \"body\", \"body_en\"}}."
)
FOLLOW_UP_RULE = ("This is a reminder of the earlier mail in the same thread: shorter than the "
                  "first mail, no new requests. ")
CUSTOMER_SYSTEM = (
    "You write a request to a person at {company} who owns a record that is wrong or doubtful. "
    "Output JSON {{\"title\", \"detail\", \"ask\"}}. `title`: a headline a busy person understands "
    "in five seconds, leading with what it costs if nothing is done. `detail`: two or three "
    "sentences on what is wrong, naming the evidence (file, place, quote). If a deadline reason is given, lead the title with it. `ask`: the one thing "
    "we ask them to do. Never blame a person. Plain words, short sentences."
)


# -- small helpers -----------------------------------------------------------
def _company(ctx: Context) -> str:
    return str(ctx.tenant.get("name") or ctx.tenant.get("id") or "the customer")


def _fact_value(ctx: Context, entity: str, key: str, field: str) -> Any:
    for f in ctx.board.facts(entity, key_prefix=key):
        if f.key == key and f.field == field:
            return f.value
    return None


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


def _parse_time(text: Any, like: datetime) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=like.tzinfo)


def _notice(title: str, detail: str, author: str, depends_on: tuple[str, ...] = ()) -> Post:
    n = Notice(title=title, detail=detail, action_id="", rule_id=None)
    return Post(kind="notice", status="posted", subject=title, body=n.to_dict(), author=author,
                depends_on=depends_on)


def _question_of(post: Post) -> Question:
    b = post.body
    return Question(text=b.get("text", ""), to_kind=b["to_kind"], to_id=b["to_id"], topic=b["topic"],
                    copies_at_risk=int(b.get("copies_at_risk") or 0), deadline=b.get("deadline"),
                    channel=b.get("channel", "supply"), context_post_ids=tuple(b.get("context_post_ids") or ()))


def _llm_dict(ctx: Context, system: str, user: str, schema: dict[str, Any],
              required: tuple[str, ...]) -> dict[str, str] | None:
    """One model call. None when the model fails or leaves out a field."""
    try:
        data = ctx.llm.complete("agent", system, user, schema=schema).data
    except Exception:  # noqa: BLE001 - a dead model must not stop the liaison; a template takes over
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip() for k in required):
        return None
    return {k: data[k].strip() for k in required}


def _with_playbook(ctx: Context, agent: str, system: str) -> str:
    playbook = ctx.playbooks.get(agent, "")
    return f"{system}\n\n{playbook}" if playbook else system


def _claim_view(claim: dict[str, Any]) -> dict[str, Any]:
    return {"value": claim.get("value"), "source_kind": claim.get("source_kind"), "as_of": claim.get("as_of"),
            "evidence": [{k: e.get(k) for k in ("source", "sha256", "locator", "quote")}
                         for e in claim.get("evidence") or ()]}


def describe_post(board: Any, post_id: str) -> dict[str, Any] | None:
    """A post as plain data for a prompt: facts with values, conflicts with their claims."""
    post = board.get(post_id)
    if post is None:
        return None
    b = post.body
    out: dict[str, Any] = {"id": post.id, "kind": post.kind, "subject": post.subject}
    if post.kind == "conflict":
        out["detail"] = b.get("detail")
        out["check"] = b.get("check")
        out["field"] = b.get("field")
        claims = []
        for cid in b.get("claim_ids") or ():
            c = board.get(cid)
            if c is not None:
                claims.append({**_claim_view(c.body), "preferred": cid == b.get("preferred_claim_id")})
        out["claims"] = claims
    elif post.kind == "claim":
        out.update(_claim_view(b))
    elif post.kind == "fact":
        out.update({"value": b.get("value"), "unit": b.get("unit")})
    else:
        out["detail"] = b.get("detail") or b.get("text")
    return out


# -- supply liaison ----------------------------------------------------------
def _question_ids(params: dict[str, Any]) -> list[str]:
    """The questions a mail asks: `question_ids` for a bundled mail, `question_id` for an old single one."""
    ids = params.get("question_ids") or ([params["question_id"]] if params.get("question_id") else [])
    return [str(i) for i in ids]


def _short_hash(ids: Any) -> str:
    return hashlib.sha1("|".join(sorted(str(i) for i in ids)).encode()).hexdigest()[:8]


def _mail_actions(board: Any, to_id: str, topics: Any) -> list[Post]:
    """Live mails to this recipient about any of these topics (a bundled mail names all its topics)."""
    wanted = {topics} if isinstance(topics, str) else set(topics)
    out = []
    for p in board.query(kind="action", limit=100_000):
        params = p.body.get("params", {})
        if p.body.get("kind") != "send_email" or p.status not in _LIVE_ACTION or params.get("to_id") != to_id:
            continue
        if wanted & set(params.get("topics") or [params.get("topic")]):
            out.append(p)
    return out


def _mails_asking(board: Any, to_id: str, question_id: str) -> list[Post]:
    return [p for p in board.query(kind="action", limit=100_000)
            if p.body.get("kind") == "send_email" and p.status in _LIVE_ACTION
            and p.body.get("params", {}).get("to_id") == to_id
            and question_id in _question_ids(p.body.get("params", {}))]


def thread_for(board: Any, to_id: str, topics: Any) -> str | None:
    """The provider thread of the newest executed mail about these topics to this recipient.

    The thread id is the id the mail provider assigned; the executor records it
    on the action's sent record. A Board message id (for example an SES id) is
    not a thread id: passing one makes the provider reject the reply, which is
    how every reminder failed with "thread_id: Invalid UUID".
    """
    for p in reversed(_mail_actions(board, to_id, topics)):
        if p.status == "executed":
            sent = p.body.get("sent_mail") or {}
            return p.body.get("params", {}).get("thread_id") or sent.get("thread_id")
    return None


def _earliest(deadlines: Any) -> str | None:
    found = sorted(str(d) for d in deadlines if d)
    return found[0] if found else None


def fallback_mail(company: str, questions: list[Question], reminder: bool,
                  faults: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """An English mail built from the question texts, for when the model is down."""
    lead = "Reminder: we have not yet received your answer to our earlier mail." if reminder else ""
    if len(questions) == 1:
        points = [questions[0].text]
    else:
        points = [f"{n}. {q.text}" for n, q in enumerate(questions, 1)]
    lines = [part for part in (lead, *points) if part]
    if faults:
        lines.append(negotiation.fallback_line(faults))
    deadline = _earliest(q.deadline for q in questions)
    if deadline:
        lines.append(f"Please answer by {deadline}.")
    lines.append(f"\nRegards,\nProcurement team, {company}")
    body = "\n".join(lines)
    noun = "question" if len(questions) == 1 else "questions"
    subject = f"Reminder: {noun} from {company}" if reminder else f"{noun.capitalize()} from {company}"
    return {"subject": subject, "body": body, "body_en": body}


def compose_mail(ctx: Context, questions: list[Question], supplier_name: str, language: str, reminder: bool,
                 previous_en: str = "", faults: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Subject, body in the supplier's language, and the English copy. One model call for all questions."""
    company = _company(ctx)
    system = _with_playbook(ctx, SUPPLY, SUPPLY_SYSTEM.format(
        company=company, language=language, follow_up=FOLLOW_UP_RULE if reminder else "",
        negotiation=negotiation.MAIL_RULE if faults else ""))
    points = []
    for n, q in enumerate(questions, 1):
        context = [d for pid in q.context_post_ids if (d := describe_post(ctx.board, pid))]
        points.append(f"Point {n}: {q.text} (copies at risk: {q.copies_at_risk}; context: {context})")
    deadline = _earliest(q.deadline for q in questions)
    user = "\n".join([
        f"Recipient: {supplier_name}", f"Language: {language}",
        f"Write ONE mail that covers all {len(questions)} points below as numbered points, in this order "
        f"(most urgent first).", *points,
        *(f"Their mistake: {f['entity']} {f['key']}: {f['detail']}" for f in faults or ()),
        f"Answer needed by: {deadline or 'as soon as possible'}",
        *([f"Earlier mail (English): {previous_en}"] if previous_en else []),
    ])
    return _llm_dict(ctx, system, user, MAIL_SCHEMA, ("subject", "body", "body_en")) \
        or fallback_mail(company, questions, reminder, faults)


def build_mail_action(ctx: Context, question_posts: list[Post], ask: int = 1) -> Post:
    """Email number `ask` for a set of questions to one supplier, or a notice when it cannot be sent."""
    reminder = ask > 1
    qs = [_question_of(p) for p in question_posts]
    first = qs[0]
    ids = [p.id for p in question_posts]
    board = ctx.board
    inboxes = ctx.tenant.get("inboxes", {})
    inbox = inboxes.get(first.channel) or inboxes.get("supply")
    lead = supplier_directory(ctx.tenant).get(first.to_id, {})
    to = _fact_value(ctx, "supplier", first.to_id, "contact_email") or lead.get("contact_email")
    if not to or not inbox:
        why = "No contact address" if not to else "No mail inbox configured"
        return _notice(f"{why} for {first.to_id}; {'question' if len(qs) == 1 else 'questions'} not sent",
                       "\n".join(q.text for q in qs), SUPPLY, tuple(ids))
    name = _fact_value(ctx, "supplier", first.to_id, "name") or lead.get("name") or first.to_id
    language = str(_fact_value(ctx, "supplier", first.to_id, "language") or lead.get("language") or "en")
    topics = list(dict.fromkeys(q.topic for q in qs))
    thread_topics = topics + ["spare:" + topic.removeprefix("recovery:") for topic in topics
                              if topic.startswith("recovery:")]
    thread = thread_for(board, first.to_id, thread_topics)
    previous = next((p.body["params"].get("body_en", "") for p in _mail_actions(board, first.to_id, topics)), "")
    faults = negotiation.faults(board, first.to_id, question_posts) if negotiation.enabled(ctx.tenant) else []
    mail = compose_mail(ctx, qs, str(name), language, reminder, previous if reminder else "", faults)
    params: dict[str, Any] = {
        "inbox": inbox, "to": to, "subject": mail["subject"], "body": mail["body"], "body_en": mail["body_en"],
        "to_id": first.to_id, "topic": first.topic, "topics": topics, "question_ids": ids,
        "language": language}
    if thread:
        params["thread_id"] = thread
    if faults:
        params["negotiation"] = {"request": "price reduction or credit note", "faults": faults}
    key = f"ask:{first.to_id}:{first.channel}:{_short_hash(ids)}:{ask}"
    action = Action(kind="send_email", params=params, requested_by=SUPPLY, reason=first.text, idempotency_key=key,
                    context_post_ids=tuple(ids))
    return Post(kind="action", status="requested", subject=key, body=action.to_dict(), author=SUPPLY,
                depends_on=tuple(ids))


def _settings(ctx: Context) -> follow_up.FollowUp:
    return follow_up.settings(ctx.tenant, ctx.board.events_since(0))


def _mail_limit(ctx: Context) -> int:
    return int(ctx.tenant.get("autonomy", {}).get("max_mails_per_supplier_per_day", DEFAULT_MAX_MAILS_PER_DAY))


def _next_step_due(ctx: Context, question_posts: list[Post], mails: list[Post],
                   rules: follow_up.FollowUp) -> datetime | None:
    """When the next reminder (or giving up) is due, or None when no mail is on record.

    The wait after each email comes from the settings. The first reminder goes out sooner when the
    earliest deadline of the questions passes before that.
    """
    if not mails:
        return None
    now = ctx.ports.clock.now()
    last = _parse_time(mails[-1].created_at, now)
    if last is None:
        return None
    due = last + timedelta(hours=rules.wait_after(len(mails)))
    deadline = _parse_time(_earliest(p.body.get("deadline") for p in question_posts), now)
    if len(mails) == 1 and deadline is not None and last < deadline < due:
        return deadline
    return due


def _topics(question_posts: list[Post]) -> list[str]:
    return list(dict.fromkeys(str(p.body.get("topic")) for p in question_posts))


def _log_ask(ctx: Context, question_posts: list[Post], ask: int, rules: follow_up.FollowUp) -> None:
    """Put the email on record: which questions it asks, which ask it is and when we look again."""
    due = ctx.ports.clock.now() + timedelta(hours=rules.wait_after(ask))
    ctx.board.log_event(SUPPLY, follow_up.FOLLOW_UP_EVENT, {
        "question_ids": [p.id for p in question_posts], "to_id": question_posts[0].body.get("to_id"),
        "topics": _topics(question_posts), "ask": ask, "of": rules.asks,
        "next_step_due": due.isoformat()}, question_posts[0].id)


def _log_price_request(ctx: Context, question_posts: list[Post], mail: Post, ask: int) -> None:
    """Put on record that this mail also asks for a better price, and for which mistakes."""
    asked = mail.body.get("params", {}).get("negotiation")
    if asked:
        negotiation.log(ctx.board, SUPPLY, "asked" if ask == 1 else "reminded", [p.id for p in question_posts],
                        question_posts[0].body.get("to_id"), ask=ask, faults=asked["faults"])


def _sources_text(ctx: Context, sources: dict[str, list[str]]) -> str:
    """The other suppliers per part in plain words, or a sentence saying there are none."""
    if not sources:
        return "No other supplier for this is on record."
    lines = []
    for part, supplier_ids in sources.items():
        label = _fact_value(ctx, "part", part, "description") or part
        names = [str(_fact_value(ctx, "supplier", s, "name") or s) for s in supplier_ids]
        lines.append(f"{label}: {', '.join(names)}")
    return "Other suppliers on record: " + "; ".join(lines) + "."


def no_answer(ctx: Context, question_posts: list[Post], emails: int) -> list[Post]:
    """The last email went unanswered: stop asking, record what else could help, and hand over to the buyer.

    One hand-over per bundled mail, for all the questions still waiting on it.
    """
    first = _question_of(question_posts[0])
    name = _fact_value(ctx, "supplier", first.to_id, "name") or first.to_id
    ids = tuple(p.id for p in question_posts)
    context = tuple(dict.fromkeys(c for p in question_posts for c in p.body.get("context_post_ids") or ()))
    parts = follow_up.parts_in_question(ctx.board, context)
    sources = follow_up.other_sources(ctx.board, first.to_id, parts)
    options = _sources_text(ctx, sources)
    if negotiation.asked_in(ctx.board, question_posts):
        negotiation.log(ctx.board, SUPPLY, "no_answer", list(ids), first.to_id, emails=emails)
        options += " Our request for a price reduction after their mistake is also unanswered."
    for qp in question_posts:
        ctx.board.set_status(qp.id, "expired", SUPPLY, f"no answer after {emails} emails")
    ctx.board.log_event(SUPPLY, follow_up.NO_ANSWER_EVENT, {
        "question_ids": list(ids), "to_id": first.to_id, "topics": _topics(question_posts), "emails": emails,
        "part_ids": parts, "other_sources": sources}, ids[0])
    points = " | ".join(p.body.get("text", "") for p in question_posts)
    escalation = Question(
        text=(f"No answer from {name} about {first.topic} after {emails} emails. {options} "
              f"Please decide how to proceed: ask another supplier, move other orders, or call. "
              f"Unanswered: {points}"),
        to_kind="role", to_id="buyer", topic=f"supplier:no-answer:{first.to_id}:{first.topic}",
        copies_at_risk=first.copies_at_risk, deadline=first.deadline, channel="supply",
        context_post_ids=ids,
    )
    # It is already escalated to the buyer inbox, so do not route it back
    # through the customer liaison as a second notice.
    return [
        Post(kind="question", status="asked", subject=f"buyer|supplier:no-answer:{first.to_id}:{first.topic}",
             body=escalation.to_dict(), author=SUPPLY, depends_on=ids),
        _notice(f"No answer from {name} about {first.topic}",
                f"We sent {emails} emails and got no reply. Question: {points} {options}", SUPPLY, ids),
    ]


def supply_tick(ctx: Context) -> list[Post]:
    """Per bundled mail: remind until the configured number of emails is reached, then hand over."""
    now = ctx.ports.clock.now()
    out: list[Post] = []
    groups: dict[str, list[Post]] = {}  # first mail of a question -> the questions still waiting on it
    first_mails: dict[str, list[Post]] = {}
    for qp in ctx.board.query(kind="question", status="asked", limit=100_000):
        if qp.body.get("to_kind") not in ("supplier", "carrier"):
            continue
        mails = _mails_asking(ctx.board, qp.body["to_id"], qp.id)
        if mails:
            groups.setdefault(mails[0].id, []).append(qp)
            first_mails[mails[0].id] = mails
    if not groups:
        return out
    rules = _settings(ctx)
    for mail_id, qps in groups.items():
        mails = first_mails[mail_id]
        due = _next_step_due(ctx, qps, mails, rules)
        if due is None or now < due:
            continue
        qps.sort(key=lambda p: -int(p.body.get("copies_at_risk") or 0))
        if len(mails) >= rules.asks:
            out.extend(no_answer(ctx, qps, len(mails)))
            continue
        if _mails_today(ctx, qps[0].body["to_id"]) >= _mail_limit(ctx):
            continue  # the supplier has had today's mails; the reminder goes out after midnight
        reminder = build_mail_action(ctx, qps, ask=len(mails) + 1)
        if reminder.kind == "notice":
            for qp in qps:
                ctx.board.set_status(qp.id, "expired", SUPPLY, "question could not be sent")
        else:
            _log_ask(ctx, qps, len(mails) + 1, rules)
            _log_price_request(ctx, qps, reminder, len(mails) + 1)
        out.append(reminder)
    return out


def _mails_today(ctx: Context, to_id: str) -> int:
    """Mails to this supplier asked for or sent since local midnight, on any channel."""
    now = ctx.ports.clock.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    for p in ctx.board.query(kind="action", limit=100_000):
        if p.body.get("kind") != "send_email" or p.status not in _LIVE_ACTION \
                or p.body.get("params", {}).get("to_id") != to_id:
            continue
        made = _parse_time(p.created_at, now)
        if made is not None and made >= midnight:
            count += 1
    return count


def supply_question(post: Post, ctx: Context) -> list[Post]:
    if post.body.get("to_kind") not in ("supplier", "carrier"):
        return []
    current = ctx.board.get(post.id)
    if current is None or current.status != "open":
        return []  # an earlier question of the same mail already took this one
    to_id, channel = post.body["to_id"], post.body.get("channel", "supply")
    open_qs = [p for p in ctx.board.query(kind="question", status="open", limit=100_000)
               if p.body.get("to_kind") in ("supplier", "carrier") and p.body.get("to_id") == to_id
               and p.body.get("channel", "supply") == channel]
    open_qs.sort(key=lambda p: -int(p.body.get("copies_at_risk") or 0))  # stable: ties keep their order
    limit = _mail_limit(ctx)
    if _mails_today(ctx, to_id) >= limit:
        name = _fact_value(ctx, "supplier", to_id, "name") or to_id
        day = ctx.ports.clock.now().date().isoformat()
        title = f"Mail limit reached for {name} on {day}"
        if ctx.board.query(kind="notice", subject_prefix=title, limit=1):
            return []
        return [_notice(title, f"{len(open_qs)} question(s) wait for tomorrow: at most {limit} mails a day "
                               f"go to one supplier.", SUPPLY, tuple(p.id for p in open_qs))]
    out = build_mail_action(ctx, open_qs)
    if out.kind == "notice":
        for p in open_qs:
            ctx.board.set_status(p.id, "expired", SUPPLY, "question could not be sent")
    else:
        for p in open_qs:
            ctx.board.set_status(p.id, "asked", SUPPLY, "mail requested")
        _log_ask(ctx, open_qs, 1, _settings(ctx))
        _log_price_request(ctx, open_qs, out, 1)
    return [out]


class SupplyLiaisonAgent:
    name = SUPPLY
    subscribes = ("question:open", "event:tick")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        return supply_tick(ctx) if post.kind == "event" else supply_question(post, ctx)


# -- customer liaison --------------------------------------------------------
def _part_of(ctx: Context, entity: str, key: str) -> str | None:
    """The part a defect is about, read from its key or from a related fact."""
    if entity == "stock" and key.count("|") >= 1:
        return key.split("|")[1]
    if entity == "bom" and key.count("|") >= 1:
        return key.split("|")[1]
    if entity == "part":
        return key
    if entity in ("receipt", "inspection"):
        value = _fact_value(ctx, entity, key, "part_id")
        return str(value) if value else None
    return None


def fix_deadline(ctx: Context, entity: str, key: str) -> tuple[str, str]:
    """The deadline and the reason for it. Two hours, or sooner when the current forecast
    says the line runs out of this part; never earlier than a quarter of an hour from now."""
    now = ctx.ports.clock.now()
    deadline, reason = now + timedelta(hours=FIX_WINDOW_HOURS), ""
    part = _part_of(ctx, entity, key)
    forecasts = ctx.board.query(kind="forecast", status="current", limit=100_000)
    runs_out = _parse_time(forecasts[-1].body.get("runs_out_at", {}).get(part), now) if forecasts and part else None
    if runs_out is not None and runs_out < deadline:
        deadline = max(runs_out, now + timedelta(minutes=MIN_FIX_MINUTES))
        label = _fact_value(ctx, "part", part, "description") or part
        reason = f"the line runs out of {label} at {runs_out.isoformat()}"
    return deadline.isoformat(), reason


def fallback_request(items: list[dict[str, Any]]) -> dict[str, str]:
    """A plain request for when the model is down. `items` are the listed defects, most costly first."""
    first = items[0]
    risk = first["copies_at_risk"]
    cost = f"{risk} copies at risk: " if risk else ""
    if len(items) == 1:
        d = first
        return {
            "title": f"{cost}{d['entity']} {d['key']} needs a fix",
            "detail": d["detail"] or "Two records disagree.",
            "ask": (f"Please check the record and set it to {d['proposed']}." if d["proposed"] is not None
                    else f"Please check the record (now: {d['current']})."),
        }
    lines = []
    for n, d in enumerate(items, 1):
        todo = f"set to {d['proposed']}" if d["proposed"] is not None else f"check (now: {d['current']})"
        lines.append(f"{n}. {d['entity']} {d['key']}: {todo}. {d['detail']}".strip())
    return {"title": f"{cost}{len(items)} records need a fix, starting with {first['entity']} {first['key']}",
            "detail": "\n".join(lines), "ask": "Please check each record and confirm or correct it."}


def _owner_defects(ctx: Context, post: Post) -> list[Post]:
    """Open defects of this owner that no live request covers yet, most copies at risk first."""
    owner = post.body["owner_role"]
    covered: set[str] = set()
    for a in ctx.board.query(kind="action", limit=100_000):
        if a.body.get("kind") == "data_fix_request" and a.status in _LIVE_ACTION:
            p = a.body.get("params", {})
            if p.get("owner_role") == owner and a.status != "executed":
                return []  # one request per owner at a time; the rest stays queued until it is out
            covered.update(str(i) for i in p.get("defect_ids") or [p.get("defect_id")] if i)
    found = [d for d in ctx.board.query(kind="defect", status="open", limit=100_000)
             if d.body.get("owner_role") == owner and d.id not in covered]
    found.sort(key=lambda d: -int(d.body.get("copies_at_risk") or 0))
    return found


def customer_defect(post: Post, ctx: Context) -> list[Post]:
    if not post.body.get("owner_role"):
        return []
    current_post = ctx.board.get(post.id)
    if current_post is None or current_post.status != "open":
        return []
    eligible = _owner_defects(ctx, post)
    if not eligible:
        return []
    cap = int(ctx.tenant.get("autonomy", {}).get("max_items_per_request", DEFAULT_MAX_ITEMS_PER_REQUEST))
    chosen, queued = eligible[:cap], len(eligible) - min(cap, len(eligible))
    owner = post.body["owner_role"]
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any] | None] = []
    for dp in chosen:
        d = dp.body
        conflict = describe_post(ctx.board, d["conflict_id"]) if d.get("conflict_id") else None
        claims = (conflict or {}).get("claims", [])
        wrong = next((c for c in claims if not c["preferred"]), None)
        conflicts.append(conflict)
        items.append({"defect_id": dp.id, "entity": d["entity"], "key": d["key"], "detail": d.get("detail") or "",
                      "current": wrong["value"] if wrong else None, "proposed": d.get("proposed_value"),
                      "copies_at_risk": int(d.get("copies_at_risk") or 0)})
    deadlines = [fix_deadline(ctx, i["entity"], i["key"]) for i in items]
    deadline, deadline_reason = min(deadlines, key=lambda t: t[0])
    top = chosen[0].body
    conflict = conflicts[0]
    claims = (conflict or {}).get("claims", [])
    preferred = next((c for c in claims if c["preferred"]), None)
    system = _with_playbook(ctx, CUSTOMER, CUSTOMER_SYSTEM.format(company=_company(ctx)))
    listing = [f"Defect {n}: {dp.body} Conflict: {c}" for n, (dp, c) in enumerate(zip(chosen, conflicts), 1)]
    user = "\n".join([
        f"Write ONE request that covers all {len(chosen)} defects, most costly first.", *listing,
        f"Copies at risk, top defect: {top.get('copies_at_risk', 0)}",
        f"Deadline: {deadline}" + (f" ({deadline_reason})" if deadline_reason else "")])
    text = _llm_dict(ctx, system, user, FIX_SCHEMA, ("title", "detail", "ask")) or fallback_request(items)
    if queued:
        text["detail"] += f" {queued} more {'record is' if queued == 1 else 'records are'} queued and follow after these."
    ids = [dp.id for dp in chosen]
    context_ids = tuple(dict.fromkeys([*ids, *(dp.body["conflict_id"] for dp in chosen if dp.body.get("conflict_id"))]))
    field = (conflict or {}).get("field")
    params: dict[str, Any] = {
        "owner_role": owner, "entity": top["entity"], "key": top["key"],
        **({"field": field} if field else {}),
        "current": items[0]["current"], "proposed": top.get("proposed_value"), "defect_id": ids[0],
        "defect_ids": ids, "items": items, "queued": queued, **text,
        "copies_at_risk": top.get("copies_at_risk", 0), "deadline": deadline,
        "deadline_reason": deadline_reason,
        "evidence": (preferred or {}).get("evidence", []),
    }
    action = Action(kind="data_fix_request", params=params, requested_by=CUSTOMER, reason=text["title"],
                    idempotency_key=f"fix:{owner}:{_short_hash(ids)}", context_post_ids=context_ids)
    return [Post(kind="action", status="requested", subject=action.idempotency_key, body=action.to_dict(),
                 author=CUSTOMER, depends_on=tuple(ids))]


def customer_question(post: Post, ctx: Context) -> list[Post]:
    """A question for one of our own roles reaches a person as a notice."""
    if post.body.get("to_kind") != "role":
        return []
    text = post.body.get("text", "")
    ctx.board.set_status(post.id, "asked", CUSTOMER, "shown as a notice")
    return [_notice(f"Question for {post.body['to_id']}: {_first_sentence(text)}", text, CUSTOMER, (post.id,))]


class CustomerLiaisonAgent:
    name = CUSTOMER
    subscribes = ("defect:open", "question:open")

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        return customer_defect(post, ctx) if post.kind == "defect" else customer_question(post, ctx)


AGENTS = [SupplyLiaisonAgent(), CustomerLiaisonAgent()]
