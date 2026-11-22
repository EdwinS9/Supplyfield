"""LLMClient: OpenAI-compatible providers, role fallback chains, retries, thinking strip."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from gtl.config import load_models
from gtl.contracts import LLMResult

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


try:  # POSIX
    import fcntl

    def _lock(fh: Any) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock(fh: Any) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
except ImportError:  # Windows has no fcntl; lock the first byte of the file instead
    import msvcrt

    def _lock(fh: Any) -> None:
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(fh: Any) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


REASONING_HEADROOM = 4000  # completion tokens a reasoning model may spend thinking before it answers
RAW_CHARS = 300  # how much of an unusable answer an error carries, for diagnosis


class LLMError(RuntimeError):
    """A failed model call. `raw` holds the start of the last answer when the model did answer."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class RateLimiter:
    """At most `per_minute` requests in any rolling minute, shared by every process on this machine.

    The backend counts requests per user, not per process, so the Gym's worker
    processes and the live system must share one budget. The request times live
    in a small file guarded by a lock.
    """

    def __init__(self, path: str | Path, per_minute: int, *, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.per_minute = int(per_minute)
        self._sleep = sleep
        self._now = now

    def acquire(self) -> float:
        """Block until a request may be sent. Returns the seconds waited."""
        waited = 0.0
        while True:
            wait = self._try()
            if wait <= 0:
                return waited
            self._sleep(wait)
            waited += wait

    def _try(self) -> float:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as fh:
            _lock(fh)
            try:
                fh.seek(0)
                try:
                    stamps = [float(x) for x in json.loads(fh.read() or "[]")]
                except ValueError:
                    stamps = []
                now = self._now()
                stamps = [t for t in stamps if now - t < 60.0]
                if len(stamps) < self.per_minute:
                    stamps.append(now)
                    wait = 0.0
                else:
                    wait = 60.0 - (now - min(stamps)) + 0.05
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(stamps))
                return wait
            finally:
                fh.flush()
                _unlock(fh)


@dataclass
class _Provider:
    name: str
    client: Any
    model: str
    alt_model: str | None
    semaphore: threading.BoundedSemaphore
    budget_usd: float | None = None
    spent_usd: float = 0.0
    thinking_toggle: bool = True  # False once the provider rejected the request to turn thinking off
    limiter: Any = None  # RateLimiter when the provider has requests_per_minute
    price_in: float = 0.0  # USD per 1M tokens
    price_out: float = 0.0
    reasoning_api: bool = False  # OpenAI reasoning models: max_completion_tokens and reasoning_effort, no temperature
    lock: threading.Lock = field(default_factory=threading.Lock)  # guards spent_usd


def strip_thinking(text: str, reasoning_field: str = "") -> tuple[str, str]:
    """Return (answer, reasoning). Handles <think> tags and a lone closing tag."""
    parts = [reasoning_field] if reasoning_field else []
    found = _THINK_RE.findall(text)
    parts.extend(s.strip() for s in found)
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # template that opens the tag in the prompt
        head, _, text = text.partition("</think>")
        parts.append(head.strip())
    return text.strip(), "\n".join(p for p in parts if p)


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in text, tolerating code fences and chatter."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for cand in candidates:
        for m in re.finditer(r"\{", cand):
            try:
                obj, _ = decoder.raw_decode(cand[m.start():])
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object found in the reply")


def _image_url(data: bytes) -> str:
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__
    return name in ("APITimeoutError", "APIConnectionError", "Timeout", "TimeoutError") or isinstance(
        exc, (TimeoutError, ConnectionError))


class LLMClient:
    def __init__(self, models: dict[str, Any] | None = None, env: Mapping[str, str] | None = None,
                 clients: dict[str, Any] | None = None,
                 on_call: Callable[[dict[str, Any]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.models = models if models is not None else load_models()
        self.env = env if env is not None else os.environ
        self.on_call = on_call
        self._sleep = sleep
        retry = self.models.get("retry", {})
        self.backoff: list[float] = list(retry.get("backoff_seconds", [2, 4, 8]))
        self.max_attempts: int = int(retry.get("max_attempts", len(self.backoff) + 1))
        self.timeout: float = float(retry.get("timeout_seconds", 120))
        limits = self.models.get("limits", {})
        self.max_prompt_tokens: int | None = limits.get("max_prompt_tokens")
        self.max_output_tokens: int | None = limits.get("max_output_tokens")
        self.providers: dict[str, _Provider] = {}
        for name, cfg in self.models.get("providers", {}).items():
            self._build_provider(name, cfg, clients)

    def _env(self, key: str | None) -> str | None:
        if not key:
            return None
        val = self.env.get(key)
        if not val and key == "LLM_API_KEY":
            val = self.env.get("RADEON_API_KEY")
        return val or None

    def _build_provider(self, name: str, cfg: dict[str, Any], clients: dict[str, Any] | None) -> None:
        model = self._env(cfg.get("model_env"))
        if not model:
            return
        client = (clients or {}).get(name)
        if client is None:
            base_url = self._env(cfg.get("base_url_env"))
            api_key = self._env(cfg.get("api_key_env"))
            if not base_url or not api_key:
                return
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=self.timeout)
        p = _Provider(name=name, client=client, model=model, alt_model=self._env(cfg.get("alt_model_env")),
                      semaphore=threading.BoundedSemaphore(int(cfg.get("max_in_flight", 4))),
                      budget_usd=cfg.get("budget_usd"), reasoning_api=bool(cfg.get("reasoning_api", False)))
        rpm = cfg.get("requests_per_minute")
        if rpm:
            var = self.env.get("GTL_VAR_DIR", "var")
            p.limiter = RateLimiter(Path(var) / f"llm-rate-{name}.json", int(rpm), sleep=self._sleep)
        if name == "strong":
            p.price_in = float(self.env.get("LLM_STRONG_PRICE_IN", 0) or 0)
            p.price_out = float(self.env.get("LLM_STRONG_PRICE_OUT", 0) or 0)
        self.providers[name] = p

    def spent(self, provider: str = "strong") -> float:
        p = self.providers.get(provider)
        if p is None:
            return 0.0
        with p.lock:
            return p.spent_usd

    @staticmethod
    def _over_budget(p: _Provider) -> bool:
        with p.lock:
            return p.budget_usd is not None and p.spent_usd >= p.budget_usd

    def _chain(self, role: str) -> list[tuple[_Provider, str]]:
        entries = self.models.get("roles", {}).get(role)
        if entries is None:
            raise LLMError(f"unknown role {role!r}")
        chain: list[tuple[_Provider, str]] = []
        for entry in entries:
            pname, _, variant = entry.partition(":")
            p = self.providers.get(pname)
            if p is None:
                continue
            if self._over_budget(p):
                continue
            model = p.alt_model if variant == "alt" else p.model
            if model:
                chain.append((p, model))
        return chain

    # -- public ------------------------------------------------------------
    def complete(self, role: str, system: str, user: str, *, schema: dict[str, Any] | None = None,
                 images: tuple[bytes, ...] = (), max_tokens: int | None = None) -> LLMResult:
        if max_tokens is None:
            max_tokens = self.max_output_tokens or 2000
        chain = self._chain(role)
        if not chain:
            raise LLMError(f"no available provider for role {role!r}")
        if schema is not None:
            system = (f"{system}\n\nReply with one JSON object only, matching this JSON schema:\n"
                      f"{json.dumps(schema)}")
        estimate = (len(system) + len(user)) // 4  # about 4 characters per token; images are not counted
        if self.max_prompt_tokens is not None and estimate > self.max_prompt_tokens:
            raise LLMError(f"prompt for role {role!r} is about {estimate} tokens, over the limit of "
                           f"{self.max_prompt_tokens}; shorten it before calling")
        last: Exception | None = None
        for provider, model in chain:
            if self._over_budget(provider):
                continue
            try:
                return self._one(role, provider, model, system, user, schema, images, max_tokens)
            except Exception as exc:  # noqa: BLE001 - try the next chain entry
                last = exc
        raise LLMError(f"all providers failed for role {role!r}: {last}", getattr(last, "raw", None)) from last

    # -- internals ---------------------------------------------------------
    def _one(self, role: str, provider: _Provider, model: str, system: str, user: str,
             schema: dict[str, Any] | None, images: tuple[bytes, ...], max_tokens: int) -> LLMResult:
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": _image_url(b)}} for b in images]
        messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                          {"role": "user", "content": content}]
        t_in = t_out = 0
        stats = {"retries": 0}
        reasoning_all: list[str] = []
        start = time.monotonic()
        attempts = 2 if schema is not None else 1
        text, data, error = "", None, ""
        try:
            for attempt in range(attempts):
                raw, reasoning, tin, tout = self._ask(provider, model, messages, max_tokens, stats,
                                                      self._extra(role, provider),
                                                      self.models.get("temperature", {}).get(role))
                t_in += tin
                t_out += tout
                text, think = strip_thinking(raw, reasoning)
                if think:
                    reasoning_all.append(think)
                if schema is None:
                    break
                try:
                    data = extract_json(text)
                    missing = [k for k in schema.get("required", []) if k not in data]
                    if missing:
                        raise ValueError(f"missing required keys: {missing}")
                    error = ""
                    break
                except ValueError as exc:
                    error, data = str(exc), None
                    messages = messages + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": f"Your reply was not valid: {error}. "
                                                    "Reply with one JSON object only."}]
        except Exception as exc:
            self._log(role, provider, model, t_in, t_out, start, stats["retries"], 0.0, ok=False,
                      error=type(exc).__name__, status_code=getattr(exc, "status_code", None))
            raise
        cost = (t_in * provider.price_in + t_out * provider.price_out) / 1_000_000
        with provider.lock:
            provider.spent_usd += cost
        self._log(role, provider, model, t_in, t_out, start, stats["retries"], cost, ok=not error,
                  error="invalid_json" if error else None)
        if error:
            raise LLMError(f"invalid JSON from {provider.name}: {error}", text[:RAW_CHARS])
        return LLMResult(text=text, data=data, model=model, input_tokens=t_in, output_tokens=t_out,
                         reasoning="\n".join(reasoning_all), provider=provider.name)

    def _log(self, role: str, provider: _Provider, model: str, t_in: int, t_out: int, start: float,
             retries: int, cost: float, *, ok: bool, error: str | None = None,
             status_code: int | None = None) -> None:
        if not self.on_call:
            return
        payload: dict[str, Any] = {
            "role": role, "provider": provider.name, "model": model, "input_tokens": t_in,
            "output_tokens": t_out, "latency_s": round(time.monotonic() - start, 3),
            "retries": retries, "cost_usd": cost, "ok": ok}
        if error:
            payload["error"] = error
        if status_code is not None:
            payload["status_code"] = status_code
        self.on_call(payload)

    def _extra(self, role: str, provider: _Provider) -> dict[str, Any] | None:
        """Extra request body for a role. Roles with `thinking = false` ask the model not to think."""
        if self.models.get("thinking", {}).get(role) is False and provider.thinking_toggle:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return None

    def _ask(self, provider: _Provider, model: str, messages: list[dict[str, Any]], max_tokens: int,
             stats: dict[str, int], extra: dict[str, Any] | None = None,
             temperature: float | None = None) -> tuple[str, str, int, int]:
        """One request. An empty answer cut off by the length limit gets one retry with more room."""
        raw, reasoning, tin, tout, finish = self._request(provider, model, messages, max_tokens, stats, extra,
                                                          temperature)
        if strip_thinking(raw, reasoning)[0] or finish != "length":
            return raw, reasoning, tin, tout
        bigger = min(max_tokens * 2, (self.max_output_tokens or max_tokens) * 2)
        if bigger <= max_tokens:
            return raw, reasoning, tin, tout
        raw, reasoning, tin2, tout2, _ = self._request(provider, model, messages, bigger, stats, extra,
                                                       temperature)
        return raw, reasoning, tin + tin2, tout + tout2

    def _request(self, provider: _Provider, model: str, messages: list[dict[str, Any]],
                 max_tokens: int, stats: dict[str, int], extra: dict[str, Any] | None = None,
                 temperature: float | None = None) -> tuple[str, str, int, int, str | None]:
        retries = 0
        sampling = {} if temperature is None else {"temperature": float(temperature)}
        limit: dict[str, Any] = {"max_tokens": max_tokens}
        if provider.reasoning_api:
            # Reasoning models think within the completion budget and take no sampling temperature. A role
            # that asks not to think (the thinking switch in `extra`) gets low effort, the others medium.
            limit = {"max_completion_tokens": max_tokens + REASONING_HEADROOM,
                     "reasoning_effort": "low" if extra else "medium"}
            sampling, extra = {}, None
        while True:
            more = {"extra_body": extra} if extra and provider.thinking_toggle else {}
            try:
                if provider.limiter is not None:
                    provider.limiter.acquire()
                with provider.semaphore:
                    resp = provider.client.chat.completions.create(
                        model=model, messages=messages, **limit, **more, **sampling)
                choice = resp.choices[0]
                msg = choice.message
                reasoning = (getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or "")
                usage = getattr(resp, "usage", None)
                tin = int(getattr(usage, "prompt_tokens", 0) or 0)
                tout = int(getattr(usage, "completion_tokens", 0) or 0)
                return (msg.content or ""), str(reasoning), tin, tout, getattr(choice, "finish_reason", None)
            except Exception as exc:  # noqa: BLE001
                if more and getattr(exc, "status_code", None) == 400:
                    provider.thinking_toggle = False  # this backend does not take the switch; ask plainly
                    continue
                if not _retryable(exc) or retries + 1 >= self.max_attempts:
                    raise
                delay = self.backoff[min(retries, len(self.backoff) - 1)] if self.backoff else 0
                retries += 1
                stats["retries"] += 1
                self._sleep(delay)
