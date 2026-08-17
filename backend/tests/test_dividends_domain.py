"""Pure-domain tests for P13.11: FY resolution, rounding, config parse.

Every oracle is hand-computed in comments — never captured from the
implementation under test (MASTER_PROMPT section 4). Runs without a
database so these gates fail in every pipeline.
"""

from datetime import date
from decimal import Decimal

import pytest

from genesis.application.dividends import parse_dividend_config
from genesis.domain.dividends import (
    entitlement_amount,
    financial_year_ending,
    last_completed_financial_year,
)
from genesis.errors import ConflictError

# ---------------------------------------------------------------------------
# Financial-year resolution (server-side only; v1.1 rule 1)
# ---------------------------------------------------------------------------


def test_financial_year_boundaries_for_a_june_year_end() -> None:
    # FY end month 6 of 2026: 2025-07-01 .. 2026-06-30, 365 days.
    fy = financial_year_ending(6, 2026)
    assert fy.start == date(2025, 7, 1)
    assert fy.end == date(2026, 6, 30)
    assert fy.days == 365


def test_financial_year_december_year_end_is_the_calendar_year() -> None:
    fy = financial_year_ending(12, 2024)
    assert fy.start == date(2024, 1, 1)
    assert fy.end == date(2024, 12, 31)
    assert fy.days == 366  # leap year: the day-count convention covers it


def test_leap_day_inside_a_mid_year_fy() -> None:
    # FY end month 6 of 2024 contains 2024-02-29: 2023-07-01..2024-06-30.
    fy = financial_year_ending(6, 2024)
    assert fy.days == 366


def test_last_completed_year_excludes_the_running_and_the_closing_day() -> None:
    # As of 2026-08-01 (June year-end): the year ending 2026-06-30 is
    # complete -> 2025-07-01..2026-06-30.
    fy = last_completed_financial_year(date(2026, 8, 1), 6)
    assert (fy.start, fy.end) == (date(2025, 7, 1), date(2026, 6, 30))
    # ON the closing day the year is not yet complete (the P11
    # previous_quarter convention): fall back one year.
    fy = last_completed_financial_year(date(2026, 6, 30), 6)
    assert (fy.start, fy.end) == (date(2024, 7, 1), date(2025, 6, 30))
    # The day after, it is.
    fy = last_completed_financial_year(date(2026, 7, 1), 6)
    assert (fy.start, fy.end) == (date(2025, 7, 1), date(2026, 6, 30))


def test_month_out_of_bounds_is_a_domain_error() -> None:
    with pytest.raises(ValueError, match=r"1\.\.12"):
        financial_year_ending(0, 2026)
    with pytest.raises(ValueError, match=r"1\.\.12"):
        financial_year_ending(13, 2026)


# ---------------------------------------------------------------------------
# The single rounding point (failure mode 1)
# ---------------------------------------------------------------------------


def test_entitlement_amount_rounds_once_half_up() -> None:
    # Hand-computed: 5950.68 * 5 / 100 = 297.534 -> 297.53 (half-up).
    assert entitlement_amount(Decimal("5950.68"), Decimal("5.00")) == Decimal("297.53")
    # 12000 * 5 / 100 = 600 exactly.
    assert entitlement_amount(Decimal("12000.00"), Decimal("5.00")) == Decimal("600.00")
    # Half-up boundary: 101.00 * 2.5 / 100 = 2.525 -> 2.53.
    assert entitlement_amount(Decimal("101.00"), Decimal("2.50")) == Decimal("2.53")
    # A sub-half-cent entitlement rounds to zero: 0.09 * 5 / 100 =
    # 0.0045 -> 0.00 (such members are skipped upstream, never posted).
    assert entitlement_amount(Decimal("0.09"), Decimal("5.00")) == Decimal("0.00")
    # Zero rate is configured-but-zero: everything rounds to 0.00.
    assert entitlement_amount(Decimal("12000.00"), Decimal("0.00")) == Decimal("0.00")


def test_entitlement_amount_bounds_mirror_the_db_checks() -> None:
    with pytest.raises(ValueError, match="negative"):
        entitlement_amount(Decimal("-1"), Decimal("5"))
    with pytest.raises(ValueError, match="0, 100"):
        entitlement_amount(Decimal("1"), Decimal("-0.01"))
    with pytest.raises(ValueError, match="0, 100"):
        entitlement_amount(Decimal("1"), Decimal("100.01"))


# ---------------------------------------------------------------------------
# Fail-closed config parse (failure mode 6; the P13.7/P13.8 precedent)
# ---------------------------------------------------------------------------


def test_missing_keys_are_unconfigured_not_corrupt() -> None:
    # ANY missing key -> None (declaration refused as unconfigured;
    # never a defaulted rate). A tenant that pays no rebate must
    # configure 0.00 explicitly.
    assert parse_dividend_config(None, Decimal("2"), 6) is None
    assert parse_dividend_config(Decimal("5"), None, 6) is None
    assert parse_dividend_config(Decimal("5"), Decimal("2"), None) is None


def test_corrupt_stored_config_fails_closed_with_409() -> None:
    """Corruption is reachable only by manual SQL past the 0017/0020
    CHECKs; the money guard is never silently disabled or defaulted."""
    with pytest.raises(ConflictError, match="corrupt"):
        parse_dividend_config("not-a-rate", Decimal("2"), 6)
    with pytest.raises(ConflictError, match="corrupt"):
        parse_dividend_config(Decimal("5"), Decimal("2"), "not-a-month")
    with pytest.raises(ConflictError, match="bounds"):
        parse_dividend_config(Decimal("-0.01"), Decimal("2"), 6)
    with pytest.raises(ConflictError, match="bounds"):
        parse_dividend_config(Decimal("100.01"), Decimal("2"), 6)
    with pytest.raises(ConflictError, match="bounds"):
        parse_dividend_config(Decimal("5"), Decimal("-1"), 6)
    with pytest.raises(ConflictError, match="bounds"):
        parse_dividend_config(Decimal("5"), Decimal("2"), 0)
    with pytest.raises(ConflictError, match="bounds"):
        parse_dividend_config(Decimal("5"), Decimal("2"), 13)


def test_valid_config_parses_verbatim() -> None:
    config = parse_dividend_config(Decimal("5.00"), Decimal("2.00"), 6)
    assert config is not None
    assert config.dividend_rate_pct == Decimal("5.00")
    assert config.rebate_rate_pct == Decimal("2.00")
    assert config.fy_end_month == 6
