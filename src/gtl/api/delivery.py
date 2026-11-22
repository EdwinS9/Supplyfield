"""Names and links the dashboards share, read once from the active facts."""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"[|:,\s]+")
_SAVES = re.compile(r"\s*Saves -?[\d,.]+ copies.*$")

def _number(post_id: str) -> int:
    tail = post_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _Lookup:
    """Names and links read once from the active facts."""

    def __init__(self, board: Any) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.parts_of: dict[str, list[str]] = {}  # product -> parts in its bill of materials
        self.names: dict[str, str] = {}  # part, supplier and product ids -> the name people use
        self.refs: dict[str, set[str]] = {}  # shipment, receipt or inspection id -> parts
        for post in board.query(kind="fact", status="active", limit=-1):
            b = post.body
            if b.get("quarantined"):
                continue
            entity, key, field, value = b.get("entity"), str(b.get("key")), b.get("field"), b.get("value")
            if entity == "product":
                self.products.setdefault(key, {})[field] = value
                if field == "name" and value:
                    self.names[key] = str(value)
            elif entity == "bom" and "|" in key:
                product, part = key.split("|", 1)
                if part not in self.parts_of.setdefault(product, []):
                    self.parts_of[product].append(part)
            elif entity == "part" and field == "description" and value:
                self.names[key] = str(value)
            elif entity == "supplier" and field == "name" and value:
                self.names[key] = str(value)
            elif entity == "shipment" and field == "part_ids" and isinstance(value, (list, tuple)):
                self.refs.setdefault(key, set()).update(str(v) for v in value)
            elif entity in ("receipt", "inspection") and field == "part_id" and value:
                self.refs.setdefault(key, set()).add(str(value))
        self.all_parts = {part for parts in self.parts_of.values() for part in parts}

    def name(self, ident: Any) -> str:
        return self.names.get(str(ident), str(ident))

    def plain(self, text: str) -> str:
        """Replace ids by the names people use; the id stays when nothing better is known."""
        for ident in sorted(self.names, key=len, reverse=True):
            if ident in text:
                text = text.replace(ident, self.names[ident])
        return text

    def parts_named(self, value: Any) -> set[str]:
        """Parts a parameter value refers to: directly, or through a shipment, receipt or inspection."""
        found: set[str] = set()
        if isinstance(value, dict):
            for inner in value.values():
                found |= self.parts_named(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                found |= self.parts_named(inner)
        elif isinstance(value, str):
            for token in _TOKEN.split(value):
                if token in self.all_parts:
                    found.add(token)
                found |= self.refs.get(token, set())
        return found


def launch_products(look: _Lookup, forecasts: list[Any]) -> set[str]:
    """The products of this launch: those a forecast covers. Every known product while there is no forecast yet.

    The product list also holds older products that are still on sale; they have no place on a launch screen.
    """
    covered = {str(product) for forecast in forecasts for product in forecast.body["shortfall"]}
    return covered or set(look.products)
