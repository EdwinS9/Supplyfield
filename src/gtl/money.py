"""Money: quoted amounts and their conversion to EUR (design 5.8).

Pure functions. Rates are foreign units per 1 EUR (the ECB convention), so
conversion divides. An amount that cannot be converted is unpriced, never zero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_CENT = Decimal("0.01")
_ISO = re.compile(r"^[A-Za-z]{3}$")


@dataclass(frozen=True)
class Money:
    amount: Decimal  # as quoted
    currency: str | None
    eur: Decimal | None  # None when unpriced
    fx_rate: Decimal | None = None  # foreign units per 1 EUR
    fx_date: str | None = None
    fx_source: str | None = None
    unpriced_reason: str | None = None  # None when priced


def normalize_currency(raw: Any) -> str | None:
    """A three-letter ISO 4217 code in upper case; None when it cannot be resolved."""
    text = str(raw).strip() if raw is not None else ""
    if text == "€":
        return "EUR"
    return text.upper() if _ISO.match(text) else None


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def line_total(unit_price: Any, qty: Any, surcharge_pct: Any = 0) -> Decimal:
    """Unit price times quantity times surcharge, at full precision."""
    return _dec(unit_price) * _dec(qty) * (1 + _dec(surcharge_pct or 0) / 100)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def to_eur(amount: Any, currency: Any, at: datetime, tenant: dict[str, Any]) -> Money:
    """Convert a quoted amount to EUR, rounding once, to cents."""
    quoted = _dec(amount)
    code = normalize_currency(currency)
    if code is None:
        return Money(quoted, None, None, unpriced_reason="no currency stated")
    if code == "EUR":
        return Money(quoted, "EUR", _cents(quoted), Decimal(1))

    def unpriced(why: str, rate: Decimal | None = None, day: str | None = None,
                 source: str | None = None) -> Money:
        return Money(quoted, code, None, rate, day, source, why)

    fx = tenant.get("fx") or {}
    raw = (fx.get("rates") or {}).get(code)
    if raw is None:
        return unpriced(f"no rate for {code}")
    try:
        rate = _dec(raw)
        day = str(fx.get("date") or "")
        stamp = date.fromisoformat(day)
    except (InvalidOperation, ValueError):
        return unpriced(f"unusable rate or rate date for {code}")
    source = str(fx.get("source") or "") or None
    if not rate > 0:
        return unpriced(f"rate for {code} is not positive", rate, day, source)
    today = at.date()
    if stamp > today:
        return unpriced(f"rate date {day} is after the decision date", rate, day, source)
    max_age = int(fx.get("max_age_days", 0))
    if (today - stamp).days > max_age:
        return unpriced(f"rate of {day} is older than {max_age} days", rate, day, source)
    return Money(quoted, code, _cents(quoted / rate), rate, day, source)
