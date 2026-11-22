"""Pluggable auth strategies.

We don't yet know exactly how the platform expects the token, so the scheme is
configurable (SOONY_API_AUTH_SCHEME). To support a new scheme (e.g. OAuth token
exchange, HMAC request signing), subclass ``TokenAuth`` and register it with
``register_auth``.
"""

from __future__ import annotations

from typing import Callable, Generator

import httpx

from .config import mask
from .errors import ConfigError


class TokenAuth(httpx.Auth):
    """Base class: holds the token and keeps it out of repr()."""

    def __init__(self, token: str, key_name: str | None = None) -> None:
        self.token = token
        self.key_name = key_name

    def __repr__(self) -> str:
        return f"{type(self).__name__}(token={mask(self.token)!r})"


class BearerAuth(TokenAuth):
    """``Authorization: Bearer <token>``"""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class HeaderAuth(TokenAuth):
    """``<key_name>: <token>`` (default header ``X-API-Key``)."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers[self.key_name or "X-API-Key"] = self.token
        yield request


class QueryAuth(TokenAuth):
    """``?<key_name>=<token>`` (default param ``api_key``). Avoid if the API allows headers."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        name = self.key_name if self.key_name and self.key_name != "X-API-Key" else "api_key"
        request.url = request.url.copy_merge_params({name: self.token})
        yield request


AuthFactory = Callable[[str, "str | None"], httpx.Auth]

_AUTH_REGISTRY: dict[str, AuthFactory] = {
    "bearer": BearerAuth,
    "header": HeaderAuth,
    "query": QueryAuth,
}


def register_auth(name: str, factory: AuthFactory) -> None:
    _AUTH_REGISTRY[name.lower()] = factory


def build_auth(scheme: str, token: str, key_name: str | None = None) -> httpx.Auth:
    try:
        factory = _AUTH_REGISTRY[scheme.lower()]
    except KeyError:
        raise ConfigError(
            f"Unknown auth scheme {scheme!r}; available: {sorted(_AUTH_REGISTRY)}"
        ) from None
    return factory(token, key_name)
