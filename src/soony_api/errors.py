"""Exception hierarchy. Catch ``SoonyAPIError`` to handle everything."""

from __future__ import annotations

from typing import Any


class SoonyError(Exception):
    """Base for every error raised by this package."""


class ConfigError(SoonyError):
    """Invalid or missing configuration (token, base URL, ...)."""


class SoonyAPIError(SoonyError):
    """The API answered with an error, or could not be reached."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        url: str | None = None,
        body: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"[HTTP {self.status_code}]")
        if self.method and self.url:
            parts.append(f"{self.method} {self.url}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)


class TransportError(SoonyAPIError):
    """Network-level failure (DNS, connection refused, timeout) after all retries."""


class IntegrityError(SoonyAPIError):
    """Downloaded bytes do not match the SHA-256 the portal advertised."""


class BadRequestError(SoonyAPIError):        # 400
    pass


class AuthenticationError(SoonyAPIError):    # 401 – token missing/invalid/expired
    pass


class PermissionDeniedError(SoonyAPIError):  # 403 – token valid but not allowed
    pass


class NotFoundError(SoonyAPIError):          # 404
    pass


class ConflictError(SoonyAPIError):          # 409
    pass


class ValidationError(SoonyAPIError):        # 422
    pass


class RateLimitError(SoonyAPIError):         # 429
    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ServerError(SoonyAPIError):            # 5xx
    pass


STATUS_TO_ERROR: dict[int, type[SoonyAPIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_for_status(status: int) -> type[SoonyAPIError]:
    if status in STATUS_TO_ERROR:
        return STATUS_TO_ERROR[status]
    if status >= 500:
        return ServerError
    return SoonyAPIError
