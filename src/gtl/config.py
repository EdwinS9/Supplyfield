"""Config loaders: .env, tenant and model routing files."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


def load_env(path: str = ".env") -> dict[str, str]:
    """Read KEY=VALUE lines into os.environ without overriding existing values."""
    loaded: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return loaded
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        loaded[key] = val
        os.environ.setdefault(key, val)
    return loaded


def load_tenant(tenant_id: str, root: str = "tenants") -> dict[str, Any]:
    with open(Path(root) / tenant_id / "tenant.toml", "rb") as f:
        return tomllib.load(f)


def tenant_generated_dir(tenant_id: str, root: str = "tenants") -> Path:
    """Folder for reviewed, committed files generated from a tenant's data."""
    return Path(root) / tenant_id / "generated"


def supplier_directory(tenant: dict[str, Any]) -> dict[str, dict]:
    """Contacts for prospective suppliers, indexed by the tenant's stable supplier IDs."""
    return tenant.get("sourcing", {}).get("directory", {})


def load_models(path: str = "config/models.toml") -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_playbooks(root: str = "playbooks") -> dict[str, str]:
    """Playbook text by agent name; the file stem is the agent name."""
    folder = Path(root)
    if not folder.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md"))}
