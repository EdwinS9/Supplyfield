"""Endpoint groups ("resources") attached to ``SoonyClient``.

Adding a new endpoint group when the portal grows (e.g. ``/api/v1/suppliers``):

    # soony_api/resources/suppliers.py
    from .base import Resource, register_resource

    @register_resource("suppliers")
    class SuppliersResource(Resource):
        def list(self):
            return self._get("suppliers")      # -> GET /api/v1/suppliers

Then import the module below. It is available as ``api.suppliers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import RESOURCE_REGISTRY, Resource, register_resource
from . import files as _files  # noqa: F401  (registers "files")

if TYPE_CHECKING:
    from ..client import SoonyClient


def attach_resources(client: "SoonyClient") -> None:
    for name, cls in RESOURCE_REGISTRY.items():
        setattr(client, name, cls(client))


__all__ = ["Resource", "register_resource", "attach_resources", "RESOURCE_REGISTRY"]
