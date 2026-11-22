"""Configuration loading: explicit args > environment > .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigError

ENV_PREFIX = "SOONY_API_"
DEFAULT_BASE_URL = ""


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> dict[str, str]:
    """Minimal .env loader (no extra dependency). Returns the parsed values.

    Existing environment variables win unless ``override`` is True.
    """
    p = Path(path)
    if not p.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        values[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return values


def _find_dotenv(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for a .env file."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Settings:
    token: str
    base_url: str = DEFAULT_BASE_URL
    auth_scheme: str = "bearer"          # bearer | header | query
    auth_key_name: str = "X-API-Key"     # header / query-param name for non-bearer schemes
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 0.5
    backoff_max: float = 20.0
    user_agent: str = "soony-api-client/0.1"
    cache_dir: str = "soony-data"         # where sync() mirrors portal files
    default_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.token:
            raise ConfigError(f"Missing API token (set {ENV_PREFIX}TOKEN or pass token=...)")
        if not self.base_url:
            raise ConfigError(f"Missing base URL (set {ENV_PREFIX}BASE_URL or pass base_url=...)")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url must start with http:// or https://, got {self.base_url!r}")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")

    def __repr__(self) -> str:  # never leak the token in logs / tracebacks
        return (
            f"Settings(base_url={self.base_url!r}, token={mask(self.token)!r}, "
            f"auth_scheme={self.auth_scheme!r}, timeout={self.timeout}, max_retries={self.max_retries})"
        )

    def with_overrides(self, **kwargs: object) -> "Settings":
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None})

    @classmethod
    def from_env(cls, dotenv: bool | str | os.PathLike[str] = True, **overrides: object) -> "Settings":
        if dotenv is True:
            found = _find_dotenv()
            if found:
                load_dotenv(found)
        elif dotenv:
            load_dotenv(dotenv)

        def env(name: str, default: str | None = None) -> str | None:
            v = os.environ.get(ENV_PREFIX + name)
            return v if v not in (None, "") else default

        values: dict[str, object] = {
            "token": env("TOKEN", ""),
            "base_url": env("BASE_URL", DEFAULT_BASE_URL),
            "auth_scheme": env("AUTH_SCHEME", "bearer"),
            "auth_key_name": env("AUTH_KEY_NAME", "X-API-Key"),
            "timeout": float(env("TIMEOUT", "30")),
            "max_retries": int(env("MAX_RETRIES", "4")),
            "backoff_base": float(env("BACKOFF_BASE", "0.5")),
            "backoff_max": float(env("BACKOFF_MAX", "20")),
            "cache_dir": env("CACHE_DIR", "soony-data"),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)  # type: ignore[arg-type]


def mask(secret: str, visible: int = 4) -> str:
    """Mask credentials before logging."""
    if not secret:
        return ""
    if len(secret) <= visible * 2:
        return "*" * len(secret)
    prefix = secret.split("_", 1)[0] + "_" if "_" in secret[:10] else ""
    return f"{prefix}…{secret[-visible:]}"
