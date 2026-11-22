"""Core HTTP client: auth, retries with backoff, error mapping, hooks, streaming.

Endpoint groups live in ``soony_api.resources`` and are attached as attributes
(``api.files``). To add a new endpoint group, see ``resources/__init__.py``.
"""

from __future__ import annotations

import contextlib
import email.utils
import logging
import random
import time
import uuid
from typing import Any, Callable, Iterator, Mapping

import httpx

from .auth import build_auth
from .config import Settings, mask
from .errors import RateLimitError, SoonyAPIError, TransportError, error_for_status

log = logging.getLogger("soony_api")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

RequestHook = Callable[[httpx.Request], None]
ResponseHook = Callable[[httpx.Response], None]


class SoonyClient:
    """Sync client for the Soony Supplier Portal.

    >>> with SoonyClient.from_env() as api:          # reads .env / SOONY_API_* vars
    ...     for f in api.files.list():
    ...         print(f.path, f.sha256)
    ...     api.files.sync("soony-data")             # mirror only what changed
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        **overrides: Any,
    ) -> None:
        if settings is None:
            settings = Settings.from_env(**overrides)
        elif overrides:
            settings = settings.with_overrides(**overrides)
        self.settings = s = settings
        self._sleep = sleep
        self._request_hooks: list[RequestHook] = []
        self._response_hooks: list[ResponseHook] = []
        self._http = httpx.Client(
            base_url=s.base_url.rstrip("/") + "/",
            auth=build_auth(s.auth_scheme, s.token, s.auth_key_name),
            timeout=s.timeout,
            headers={"User-Agent": s.user_agent, "Accept": "application/json", **s.default_headers},
            transport=transport,
            follow_redirects=True,
        )
        from .resources import attach_resources

        attach_resources(self)

    @classmethod
    def from_env(cls, **overrides: Any) -> "SoonyClient":
        return cls(Settings.from_env(**overrides))

    def __enter__(self) -> "SoonyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"SoonyClient(base_url={self.settings.base_url!r}, token={mask(self.settings.token)!r})"

    def on_request(self, hook: RequestHook) -> RequestHook:
        """Register a hook (usable as decorator) called before every attempt."""
        self._request_hooks.append(hook)
        return hook

    def on_response(self, hook: ResponseHook) -> ResponseHook:
        """Register a hook called after every response, including retried ones."""
        self._response_hooks.append(hook)
        return hook

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry: bool | None = None,
        idempotency_key: str | None = None,
        raw: bool = False,
    ) -> Any:
        """Send a request; return decoded JSON / text / bytes (or the Response if ``raw``).

        ``retry``: None = auto (idempotent methods, or any method with an
        idempotency key); True/False forces it.
        """
        with self._send(method, path, params=params, json=json, data=data, files=files,
                        headers=headers, timeout=timeout, retry=retry,
                        idempotency_key=idempotency_key, stream=False) as resp:
            return resp if raw else _decode(resp)

    @contextlib.contextmanager
    def stream(self, method: str, path: str, **kw: Any) -> Iterator[httpx.Response]:
        """Like ``request`` but the body is not loaded; iterate ``resp.iter_bytes()``.

        Retries cover connecting and the status line only, not a failure halfway
        through the body (callers that need that, like ``files.download``, retry around it).
        """
        with self._send(method, path, stream=True, **kw) as resp:
            yield resp

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def ping(self) -> bool:
        """Connectivity + token check. Raises AuthenticationError on a bad token."""
        self.request("GET", "api/v1/files", retry=False)
        return True

    # -------------------------------------------------------------- internals
    @contextlib.contextmanager
    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry: bool | None = None,
        idempotency_key: str | None = None,
        stream: bool = False,
    ) -> Iterator[httpx.Response]:
        method = method.upper()
        hdrs = dict(headers or {})
        if idempotency_key:
            hdrs.setdefault("Idempotency-Key", idempotency_key)
        can_retry = retry if retry is not None else (method in IDEMPOTENT_METHODS or bool(idempotency_key))
        max_attempts = 1 + (self.settings.max_retries if can_retry else 0)
        url = path if path.startswith(("http://", "https://")) else path.lstrip("/")
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        attempt = 0
        while True:
            attempt += 1
            req = self._http.build_request(
                method, url, params=params, json=json, data=data, files=files, headers=hdrs,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            req.headers.setdefault("X-Request-ID", uuid.uuid4().hex)
            for h in self._request_hooks:
                h(req)

            started = time.monotonic()
            try:
                resp = self._http.send(req, stream=stream)
            except _TRANSPORT_ERRORS as e:
                if attempt < max_attempts:
                    delay = self._backoff(attempt)
                    log.warning("%s %s failed (%s); retry %d/%d in %.2fs",
                                method, req.url.path, type(e).__name__, attempt, max_attempts - 1, delay)
                    self._sleep(delay)
                    continue
                raise TransportError(f"{type(e).__name__}: {e}", method=method,
                                     url=self._redact(str(req.url))) from e

            log.debug("%s %s -> %d (%.0f ms, attempt %d)", method, req.url.path,
                      resp.status_code, (time.monotonic() - started) * 1000, attempt)
            for h in self._response_hooks:
                h(resp)

            if resp.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                delay = self._retry_after(resp) or self._backoff(attempt)
                log.warning("%s %s -> %d; retry %d/%d in %.2fs",
                            method, req.url.path, resp.status_code, attempt, max_attempts - 1, delay)
                resp.close()
                self._sleep(delay)
                continue

            try:
                if resp.is_error:
                    if stream:
                        resp.read()
                    raise self._to_error(resp)
                yield resp
            finally:
                resp.close()
            return

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        cap = min(self.settings.backoff_max, self.settings.backoff_base * (2 ** (attempt - 1)))
        return random.uniform(0, cap)

    def _retry_after(self, resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            secs = float(value)
        except ValueError:
            try:
                secs = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError):
                return None
        return max(0.0, min(secs, self.settings.backoff_max))

    def _to_error(self, resp: httpx.Response) -> SoonyAPIError:
        body = _decode(resp)
        message = _extract_message(body) or resp.reason_phrase or "API error"
        cls = error_for_status(resp.status_code)
        kwargs: dict[str, Any] = dict(
            status_code=resp.status_code,
            method=resp.request.method,
            url=self._redact(str(resp.request.url)),
            body=body,
            request_id=resp.headers.get("X-Request-ID") or resp.request.headers.get("X-Request-ID"),
        )
        if cls is RateLimitError:
            kwargs["retry_after"] = self._retry_after(resp)
        return cls(message, **kwargs)

    def _redact(self, text: str) -> str:
        tok = self.settings.token
        return text.replace(tok, mask(tok)) if tok else text


def _decode(resp: httpx.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype:
        try:
            return resp.json()
        except ValueError:
            return resp.text
    if ctype.startswith("text/"):
        return resp.text
    return resp.content


def _extract_message(body: Any) -> str | None:
    """Portal errors look like ``{"error": "...", "hint": "..."}``; also handle common shapes."""
    if isinstance(body, dict):
        msg = None
        for key in ("error", "message", "detail", "error_description", "title"):
            v = body.get(key)
            if isinstance(v, str) and v:
                msg = v
                break
            if isinstance(v, dict):
                msg = _extract_message(v)
                if msg:
                    break
        hint = body.get("hint")
        if msg and isinstance(hint, str) and hint:
            msg = f"{msg} (hint: {hint})"
        return msg
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return None
