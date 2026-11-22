"""Read the Board relationships behind Launch and Inbox cards."""

from gtl.contracts import Board, Post


def decision_routing(board: Board, card: Post) -> dict:
    for ref in card.depends_on:
        action = board.get(ref)
        if not action or action.kind != "action":
            continue
        others = []
        for sibling_id in board.dependents(action.id):
            sibling = board.get(sibling_id)
            if not sibling or sibling.id == card.id or sibling.kind != "decision":
                continue
            body = sibling.body
            choice = body.get("chosen")
            options = body.get("options", [])
            selected = None
            if sibling.status == "answered" and isinstance(choice, int) and 0 <= choice < len(options):
                selected = options[choice].get("title")
            others.append({"role": body.get("approver_role"), "person": body.get("approver_person"),
                           "status": sibling.status, "selected": selected})
        return {"reason": action.body.get("verdict", {}).get("reason"), "action_status": action.status, "others": others}
    return {}


def action_summary(board: Board, action: Post) -> dict:
    summary = {"id": action.id, "kind": action.body.get("kind"), "status": action.status,
               "reason": action.body.get("reason"), "updated_at": action.updated_at}
    question_id = action.body.get("params", {}).get("question_id")
    if action.status == "executed" and summary["kind"] == "send_email" and question_id:
        question = board.get(question_id)
        if question and question.kind == "question":
            summary["question_status"] = question.status
    return summary


def product_inputs(board: Board, product: str, forecast: Post | None) -> list[str]:
    """Select records for this product and its bottleneck from this forecast's inputs."""
    prefix = f"product/{product}/"
    if forecast is None:
        return [p.id for p in board.query(kind="fact", status="active", subject_prefix=prefix, limit=-1)
                if not p.body.get("quarantined")]
    inputs = set(forecast.depends_on) | set(forecast.body.get("fact_ids", ()))
    part = forecast.body.get("limiting_part", {}).get(product)
    prefixes = [prefix, f"bom/{product}|"]
    if part:
        prefixes.extend([f"part/{part}/", "stock/"])
    refs = []
    for prefix in prefixes:
        for post in board.query(kind="fact", subject_prefix=prefix, limit=-1):
            if post.id not in inputs:
                continue
            if prefix == "stock/" and post.body.get("key", "").split("|")[1:2] != [part]:
                continue
            refs.append(post.id)
    return refs


def card_sources(board: Board, refs: list[str]) -> dict:
    """Follow recorded dependencies. Expand a large forecast by product on demand."""
    pending = list(reversed(refs))
    visited = set()
    sources = {}
    forecasts = []
    while pending:
        ref = pending.pop()
        if ref in visited or ref.startswith("file:"):
            continue
        visited.add(ref)
        post = board.get(ref)
        if post is None:
            continue
        if post.kind == "forecast":
            forecasts.extend({"id": post.id, "product": product} for product in post.body.get("copies", {}))
            continue
        for item in post.body.get("evidence", ()):
            if isinstance(item, dict) and item.get("source"):
                key = tuple(item.get(k) for k in ("source", "sha256", "locator", "quote", "translation"))
                sources[key] = item
        pending.extend(post.depends_on)
        for field in ("claim_ids", "context_post_ids"):
            pending.extend(post.body.get(field, ()))
    return {"sources": list(sources.values()), "forecasts": forecasts}
