"""Batch revision: which artwork revision a stock batch carries, derived from its paperwork.

A stock batch id is its goods receipt number. The receipt names the purchase order and the part;
the order confirmation or delivery note for that order and part states the revision. The result
is a `stock.revision` fact that the release checks and the steward read.
"""
from __future__ import annotations

from gtl.contracts import Board, Fact, Post
from gtl.trust.common import fact_table, find_fact_post, slot_subject

DERIVED = "derived:receipt-confirmation"


def _sources(board: Board, batch: str, part: str) -> list[tuple[str, str]]:
    """(entity, key) of every record that states a revision for the batch's order and part."""
    t = fact_table(board)
    po = t.get("receipt", {}).get(batch, {}).get("po_number")
    if po is None:
        return []
    found = []
    for key, f in t.get("order_confirmation", {}).items():
        if key.split("|")[:2] == [str(po.value), part] and "revision" in f:
            found.append(("order_confirmation", key))
    for key, f in t.get("delivery_note", {}).items():
        same_part = f.get("part_id") is not None and str(f["part_id"].value) == part
        same_po = f.get("po_number") is not None and str(f["po_number"].value) == str(po.value)
        if "revision" in f and same_part and same_po:
            found.append(("delivery_note", key))
    return found


def derive_revisions(board: Board, author: str) -> list[Post]:
    """One revision fact per stock batch whose paperwork names exactly one revision, authored by `author`."""
    t, out = fact_table(board), []
    for skey, f in t.get("stock", {}).items():
        if "qty" not in f or find_fact_post(board, "stock", skey, "revision") is not None:
            continue
        _, part, batch = (skey.split("|") + ["", ""])[:3]
        sources = _sources(board, batch, part)
        rev_facts = [t[e][k]["revision"] for e, k in sources]
        if len({str(r.value).strip().casefold() for r in rev_facts}) != 1:
            continue
        deps = [p.id for e, k in sources if (p := find_fact_post(board, e, k, "revision", ("active",)))]
        po_post = find_fact_post(board, "receipt", batch, "po_number", ("active",))
        if po_post is not None:
            deps.append(po_post.id)
        claim_ids = tuple(dict.fromkeys(c for r in rev_facts for c in r.claim_ids))
        fact = Fact("stock", skey, "revision", rev_facts[0].value, max(r.as_of for r in rev_facts),
                    claim_ids, (DERIVED,))
        out.append(Post(kind="fact", status="active", subject=slot_subject("stock", skey, "revision"),
                        body=fact.to_dict(), author=author, depends_on=tuple(dict.fromkeys(deps))))
    return out
