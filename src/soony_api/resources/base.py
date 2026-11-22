from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:
    from ..client import SoonyClient

API_PREFIX = "api/v1"

RESOURCE_REGISTRY: dict[str, type["Resource"]] = {}

R = TypeVar("R", bound=type["Resource"])


def register_resource(name: str) -> Callable[[R], R]:
    """Class decorator: expose the resource as ``client.<name>``."""

    def deco(cls: R) -> R:
        RESOURCE_REGISTRY[name] = cls
        return cls

    return deco


class Resource:
    """Base for endpoint groups. Paths are relative to ``/api/v1``."""

    prefix: str = API_PREFIX

    def __init__(self, client: "SoonyClient") -> None:
        self._client = client

    def _url(self, path: str) -> str:
        return f"{self.prefix}/{path.lstrip('/')}"

    def _get(self, path: str, **kw: Any) -> Any:
        return self._client.request("GET", self._url(path), **kw)

    def _post(self, path: str, **kw: Any) -> Any:
        return self._client.request("POST", self._url(path), **kw)

    def _stream(self, path: str, **kw: Any):
        return self._client.stream("GET", self._url(path), **kw)
