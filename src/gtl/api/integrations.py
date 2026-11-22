"""Integrations: where the agents get their data, and what another company's files look like to the readers."""
import json
from collections import Counter
from pathlib import Path
from typing import Any

from gtl.config import load_tenant, tenant_generated_dir
from gtl.read.table_reader import parse_tables, signature_hash
from soony_api.sync import load_manifest

TABLE_SUFFIXES = {".csv", ".tsv", ".json", ".xlsx", ".xlsm"}
MAX_SAMPLE_FILES = 200


def _folder(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")


def _mirror(data_root: Path | None) -> dict[str, Any] | None:
    """What the portal listener has mirrored: file count, size, formats, folders and the last sync."""
    if data_root is None or not data_root.is_dir():
        return None
    manifest = load_manifest(data_root) or {}
    entries = {path: meta for path, meta in manifest.items() if isinstance(meta, dict)}
    if not entries:
        return None
    synced = [meta["synced_at"] for meta in entries.values() if isinstance(meta.get("synced_at"), str)]
    formats = Counter(Path(path).suffix.lower().lstrip(".") or "other" for path in entries)
    folders = Counter(_folder(path) for path in entries)
    return {"files": len(entries), "bytes": sum(int(meta.get("bytes") or 0) for meta in entries.values()),
            "last_synced_at": max(synced) if synced else None,
            "formats": [{"format": name, "files": n} for name, n in formats.most_common()],
            "folders": [{"folder": name, "files": n} for name, n in sorted(folders.items())]}


def _company(tenant: dict[str, Any]) -> dict[str, Any]:
    return {"id": tenant.get("id"), "name": tenant.get("name") or tenant.get("id"),
            "timezone": tenant.get("timezone"), "site": tenant.get("site")}


def connected(tenant: dict[str, Any] | None) -> dict[str, Any]:
    """The active tenant and its sources: the portal listener and the mailboxes."""
    tenant = tenant or {}
    sources = tenant.get("sources", {})
    root = Path(sources["data_root"]) if sources.get("data_root") else None
    inboxes = tenant.get("inboxes", {})
    mail = tenant.get("mail", {})
    return {
        "company": _company(tenant),
        "listener": {"kind": sources.get("kind"), "data_root": sources.get("data_root"),
                     "poll_seconds": sources.get("poll_seconds"), "active_hours": sources.get("active_hours"),
                     "mirror": _mirror(root)},
        "mailboxes": {"inboxes": [{"role": role, "address": address} for role, address in inboxes.items()
                                  if isinstance(address, str)],
                      "outbound": mail.get("outbound")},
    }


def other_companies(tenants_root: Path, active_id: str | None) -> list[dict[str, Any]]:
    """Every other tenant on file, and whether it has sample files to preview."""
    if not tenants_root.is_dir():
        return []
    companies = []
    for folder in sorted(p for p in tenants_root.iterdir() if (p / "tenant.toml").is_file() and p.name != active_id):
        try:
            tenant = load_tenant(folder.name, str(tenants_root))
        except (OSError, ValueError):
            continue
        sample = tenant.get("sources", {}).get("sample_root")
        companies.append({**_company({"id": folder.name, **tenant}), "kind": tenant.get("sources", {}).get("kind"),
                          "sample": bool(sample) and Path(sample).is_dir()})
    return companies


def _mapped(tenant_id: str, tenants_root: Path, cache_dir: Path | None, headers: list[str]) -> list[str]:
    """Entities a stored header mapping sends this table to; empty when the reader has not mapped it yet."""
    name = f"{signature_hash(headers)}.json"
    folders = [tenant_generated_dir(tenant_id, str(tenants_root)) / "mappings"]
    if cache_dir is not None:
        folders.append(cache_dir / "mappings")
    for folder in folders:
        try:
            stored = json.loads((folder / name).read_text(encoding="utf-8"))
            targets = stored["mapping"]["targets"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return sorted({t["entity"] for t in targets if isinstance(t, dict) and t.get("entity")})
    return []


def _inspect(path: Path, rel: str, tenant_id: str, tenants_root: Path, cache_dir: Path | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": rel, "format": path.suffix.lower().lstrip(".") or "other",
                             "bytes": path.stat().st_size, "tables": []}
    if path.suffix.lower() not in TABLE_SUFFIXES:
        entry["route"] = "document"
        return entry
    entry["route"] = "table"
    try:
        parsed = parse_tables(rel, path.read_bytes())
    except Exception:  # noqa: BLE001 - every parser raises its own errors on a damaged file
        entry["error"] = "The reader could not open this file."
        return entry
    entry["tables"] = [{"sheet": t.sheet or None, "rows": len(t.rows), "columns": t.headers,
                        "mapped_to": _mapped(tenant_id, tenants_root, cache_dir, t.headers)} for t in parsed.tables]
    return entry


def preview(tenants_root: Path, tenant_id: str, *, cache_dir: Path | None = None) -> dict[str, Any] | None:
    """What the readers would see in a company's sample files, without putting anything on the Board."""
    if tenant_id not in {c["id"] for c in other_companies(tenants_root, None)}:
        return None
    tenant = load_tenant(tenant_id, str(tenants_root))
    sample = tenant.get("sources", {}).get("sample_root")
    root = Path(sample) if sample else None
    if root is None or not root.is_dir():
        return {"company": _company({"id": tenant_id, **tenant}), "files": [], "available": False}
    paths = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))[:MAX_SAMPLE_FILES]
    files = [_inspect(p, p.relative_to(root).as_posix(), tenant_id, tenants_root, cache_dir) for p in paths]
    return {"company": _company({"id": tenant_id, **tenant}), "available": True, "files": files,
            "summary": {"files": len(files), "tables": sum(len(f["tables"]) for f in files),
                        "rows": sum(t["rows"] for f in files for t in f["tables"]),
                        "documents": sum(f["route"] == "document" for f in files),
                        "formats": sorted({f["format"] for f in files})}}
