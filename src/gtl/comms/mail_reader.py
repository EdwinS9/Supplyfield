"""mail_reader: an inbound mail becomes claims with quotes; the question it answers is marked answered.

The text is read by the document reader's text route, so quotes are checked against the mail and
values follow the same conventions as every other source. The claims are then re-stamped as mail
evidence (`mail:<message_id>`).
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from gtl.comms import negotiation
from gtl.config import supplier_directory
from gtl.contracts import Claim, Context, Evidence, Post, make_key
from gtl.read.document_reader import SUPPORTED as DOC_EXTENSIONS, read_document, snapshot
from gtl.read.table_reader import TABLE_EXTENSIONS, read_table

NAME = "mail_reader"

MAIL_HINTS = """\
This text is an e-mail from a supplier, carrier or colleague: a subject line, then the body. Extract only what the mail states, as claims in the vocabulary: quantities shipped or confirmed (shipment.qty_shipped, order_confirmation.qty), dates (eta, confirmed_date, promised_date), prices and offers (offer entity: key "<supplier_id>|<doc number or mail subject>|<item>"), terms (incoterm, hold_reason) and statuses. Use the ids in the KNOWN lists when the mail names a PO, container, air waybill or part. Skip greetings and signatures. Put anything that matters but fits no entity into observation.
Set doc_date to the date the mail was sent. Every claim needs a verbatim quote copied from the mail."""

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _address(sender: str) -> str:
    match = _ADDRESS.search(sender or "")
    return match.group(0).lower() if match else (sender or "").strip().lower()


def questions_for(ctx: Context, mail: dict[str, Any]) -> list[Post]:
    """The questions this mail answers: by thread first (a bundled mail asked several), else by sender
    when only one question is open."""
    board = ctx.board
    thread = mail.get("thread_id")
    if thread:
        thread_of = getattr(ctx.ports.mail, "thread_of", None)
        for action in reversed(board.query(kind="action", status="executed", limit=100_000)):
            body = action.body
            if body.get("kind") != "send_email":
                continue
            params = body.get("params") or {}
            msg_id = body.get("message_id")
            sent_thread = thread_of(msg_id) if callable(thread_of) and msg_id else None
            if thread in (params.get("thread_id"), msg_id, sent_thread):
                ids = params.get("question_ids") or [params.get("question_id")]
                found = [q for i in ids if (q := board.get(str(i))) is not None and q.kind == "question"]
                if found:
                    return found
    sender = _address(str(mail.get("sender", "")))
    suppliers = {f.key for f in board.facts("supplier")
                 if f.field == "contact_email" and _address(str(f.value)) == sender}
    suppliers.update(vendor for vendor, entry in supplier_directory(ctx.tenant).items()
                     if sender and _address(entry.get("contact_email", "")) == sender)
    asked = [q for q in board.query(kind="question", status="asked", limit=100_000)
             if q.body.get("to_id") in suppliers]
    return asked if len(asked) == 1 else []


def _restamp(claim: Claim, message_id: str, as_of: str) -> Claim:
    evidence = tuple(Evidence(source=f"mail:{message_id}", locator=e.locator, quote=e.quote,
                              translation=e.translation)
                     for e in claim.evidence)
    return replace(claim, source_kind="mail", author=NAME, evidence=evidence, as_of=as_of)


def _read_attachment(att_path: str, ctx: Context, snap: Any, as_of: str,
                     errors: list[str]) -> list[Claim]:
    path = Path(att_path)
    if not path.is_file():
        return []
    lower = path.name.lower()
    data = path.read_bytes()
    if lower.endswith(DOC_EXTENSIONS):
        return read_document(path.name, data, ctx.llm, known=snap.known, now_iso=as_of,
                             on_error=errors.append)
    if lower.endswith(TABLE_EXTENSIONS):
        return read_table(path.name, data, ctx.llm, as_of=as_of,
                          tz=str(ctx.tenant.get("timezone", "Europe/Berlin")), on_error=errors.append)
    return []


def observation(mail: dict[str, Any], as_of: str) -> Claim:
    message_id = str(mail.get("message_id", ""))
    subject = str(mail.get("subject", "")).strip()
    text = f"Mail from {mail.get('sender', 'unknown sender')}: {subject or '(no subject)'}"
    return Claim(entity="observation", key=make_key(f"mail:{message_id}", 1), field="text", value=text,
                 as_of=as_of, source_kind="mail", author=NAME,
                 evidence=(Evidence(source=f"mail:{message_id}", quote=subject or None),))


def _claim_post(claim: Claim, depends_on: tuple[str, ...]) -> Post:
    return Post(kind="claim", status="proposed", subject=f"{claim.entity}/{claim.key}/{claim.field}",
                body=claim.to_dict(), author=NAME, depends_on=depends_on)


def read_mail(post: Post, ctx: Context) -> list[Post]:
    mail = post.body
    message_id = str(mail.get("message_id", ""))
    if not message_id:
        return []
    snap = snapshot(ctx)
    as_of = str(mail.get("received_at") or snap.now.isoformat())
    questions = questions_for(ctx, mail)
    # Include the contacted prospect in quote extraction before it enters the supplier master.
    known_ids = {row["id"] for row in snap.known["suppliers"]}
    leads = supplier_directory(ctx.tenant)
    for question in questions:
        vendor = question.body.get("to_id")
        if vendor in leads and vendor not in known_ids:
            snap.known["suppliers"].append({"id": vendor, "name": leads[vendor].get("name", vendor)})
            known_ids.add(vendor)
    hints = MAIL_HINTS
    if questions:
        asked = " | ".join(str(q.body.get("text", "")) for q in questions)
        hints += f"\nThe mail replies to this question of ours: {asked}"
    price_request = negotiation.asked_in(ctx.board, questions) if questions else None
    if price_request:
        hints += negotiation.READ_HINT
    text = f"Subject: {mail.get('subject', '')}\n\n{mail.get('body', '')}"
    errors: list[str] = []
    raw_claims = read_document(
        f"mail/{message_id}.txt", text.encode("utf-8"), ctx.llm, known=snap.known, now_iso=as_of,
        on_error=errors.append, text=text, hints=hints)
    for att in mail.get("attachments") or ():
        try:
            raw_claims.extend(_read_attachment(str(att), ctx, snap, as_of, errors))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    claims = [_restamp(c, message_id, as_of) for c in raw_claims]
    for reason in errors:
        ctx.board.log_event(NAME, "reader.failed", {"path": f"mail:{message_id}", "reason": reason})
    depends = (post.id, *(q.id for q in questions))
    out = [_claim_post(c, depends) for c in claims]
    if not questions or not claims:
        out.append(_claim_post(observation(mail, as_of), (post.id,)))
    if claims:
        for question in questions:
            if question.status == "asked":
                ctx.board.set_status(question.id, "answered", NAME, f"answered by mail:{message_id}")
    if price_request and claims:
        out.append(_price_reply(ctx, post, questions, price_request, [c.to_dict() for c in claims], message_id))
    return out


def _price_reply(ctx: Context, mail_post: Post, questions: list[Post], price_request: dict[str, Any],
                 claims: list[dict[str, Any]], message_id: str) -> Post:
    """Record what the supplier answered to our price request and hand the decision to a person."""
    to_id = str(price_request.get("to_id") or questions[0].body.get("to_id"))
    found = negotiation.terms(claims)
    ids = [q.id for q in questions]
    negotiation.log(ctx.board, NAME, "replied", ids, to_id, message_id=message_id, terms=found,
                    faults=price_request.get("faults") or [])
    name = next((f.value for f in ctx.board.facts("supplier", key_prefix=to_id)
                 if f.key == to_id and f.field == "name"), to_id)
    return negotiation.decision_question(ctx.tenant, str(name), to_id, str(questions[0].body.get("topic")),
                                         negotiation.terms_text(found), (mail_post.id, *ids), NAME)


class MailReaderAgent:
    name = NAME
    subscribes = ("event:mail.received",)

    def handle(self, post: Post, ctx: Context) -> list[Post]:
        try:
            return read_mail(post, ctx)
        except Exception as exc:  # noqa: BLE001 - one bad mail never stops the dispatcher
            ctx.board.log_event(NAME, "reader.failed", {"path": f"mail:{post.body.get('message_id')}",
                                                        "reason": f"{type(exc).__name__}: {exc}"})
            return []


AGENTS = [MailReaderAgent()]
