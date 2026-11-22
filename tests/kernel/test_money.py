from datetime import datetime
from decimal import Decimal

import pytest

from gtl.money import line_total, normalize_currency, to_eur

AT = datetime.fromisoformat("2026-11-21T08:00:00+01:00")


def tenant(rate=8, date="2026-11-20", max_age=7, code="CNY"):
    return {"fx": {"source": "test", "date": date, "max_age_days": max_age, "rates": {code: rate}}}


def test_foreign_units_per_eur_means_divide():
    m = to_eur(80_000, "CNY", AT, tenant(8))
    assert m.eur == Decimal("10000.00") and m.fx_rate == 8 and m.unpriced_reason is None
    assert m.fx_date == "2026-11-20" and m.fx_source == "test" and m.amount == 80_000


def test_explicit_eur_needs_no_rate_table():
    m = to_eur(Decimal("1234.567"), "EUR", AT, {})
    assert m.eur == Decimal("1234.57") and m.fx_rate == 1 and m.unpriced_reason is None
    assert to_eur(5, "€", AT, {}).eur == Decimal("5.00")


def test_rounds_once_on_the_line_total_not_per_unit():
    # 0.333 per unit x 1000 x 1.10 = 366.30 EUR; rounding the unit to 0.33 first would give 363.00.
    total = line_total("0.333", 1000, 10)
    assert total == Decimal("366.3000")
    assert to_eur(total, "EUR", AT, {}).eur == Decimal("366.30")


def test_a_line_exactly_on_an_approval_threshold_is_not_pushed_off_by_early_rounding():
    unit = Decimal("0.8333333333333")  # 5 / 6
    on_threshold = to_eur(line_total(unit, 60_000, 0), "EUR", AT, {}).eur
    assert on_threshold == Decimal("50000.00")
    assert round(unit, 2) * 60_000 == Decimal("49800.00")  # rounding per unit would stay under 50,000


def test_half_up_on_the_eur_total():
    assert to_eur(Decimal("10.005"), "EUR", AT, {}).eur == Decimal("10.01")
    assert to_eur(Decimal("10.04"), "CNY", AT, tenant(8)).eur == Decimal("1.26")  # 1.255 rounds up


@pytest.mark.parametrize("raw", ["$", "", "  ", None, "EURO", "12"])
def test_unresolved_currency(raw):
    m = to_eur(10, raw, AT, tenant(8))
    assert normalize_currency(raw) is None
    assert m.eur is None and m.currency is None and "currency" in m.unpriced_reason


def test_normalize():
    assert normalize_currency(" cny ") == "CNY" and normalize_currency("€") == "EUR"


def test_each_gap_is_unpriced_with_its_own_reason():
    reasons = {
        "no rate": to_eur(10, "INR", AT, tenant(8)).unpriced_reason,
        "zero": to_eur(10, "CNY", AT, tenant(0)).unpriced_reason,
        "future": to_eur(10, "CNY", AT, tenant(8, date="2026-11-22")).unpriced_reason,
        "old": to_eur(10, "CNY", AT, tenant(8, date="2026-11-10")).unpriced_reason,
    }
    assert all(reasons.values()) and len(set(reasons.values())) == 4
    assert "no rate for INR" in reasons["no rate"] and "older" in reasons["old"]
    assert to_eur(10, "CNY", AT, tenant(0)).eur is None


def test_rate_on_the_boundary_of_max_age_is_usable():
    assert to_eur(80, "CNY", AT, tenant(8, date="2026-11-14")).eur == Decimal("10.00")
    assert to_eur(80, "CNY", AT, tenant(8, date="2026-11-13")).eur is None
