"""Match the Board's current file versions to the reader claims that cite them."""

from gtl.contracts import Board


def source_progress(board: Board) -> list[dict]:
    versions = {}
    for change in board.query(kind="event", subject_prefix="file.changed", limit=-1):
        path = change.body.get("path")
        if change.subject != "file.changed" or not isinstance(path, str) or not path:
            continue
        versions.pop(path, None)
        digest = change.body.get("sha256")
        if change.body.get("kind") != "removed" and isinstance(digest, str) and digest:
            versions[path] = {"path": path, "sha256": digest, "changed_at": change.created_at,
                              "claims": {}, "example_id": None}

    if not versions:
        return []

    for claim in board.query(kind="claim", limit=-1):
        if claim.author not in ("document_reader", "table_reader") or claim.status == "stale":
            continue
        # One claim can cite several rows of one file or more than one file.
        matched = set()
        for evidence in claim.body.get("evidence") or ():
            if not isinstance(evidence, dict) or not isinstance(evidence.get("source"), str):
                continue
            path = evidence["source"]
            version = versions.get(path)
            if version is None or evidence.get("sha256") != version["sha256"] or path in matched:
                continue
            matched.add(path)
            counts = version["claims"]
            counts[claim.status] = counts.get(claim.status, 0) + 1
            if version["example_id"] is None or claim.status == "verified":
                version["example_id"] = claim.id
    return list(reversed(versions.values()))
