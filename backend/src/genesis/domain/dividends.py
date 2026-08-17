"""Dividend & rebate domain: financial-year periods and payout math (P13.11).

Pure functions only — no I/O. Money rounding comes from
genesis.domain.money (gate 1.1: reuse-first).

Financial-year convention (documented, pinned by tests): a tenant's
financial year ENDS on the last day of its configured
``financial_year_end_month`` and starts on the first day of the
following month, one year earlier. Only the most recently COMPLETED
financial year is ever declarable: the year ending on fy_end is
complete once ``as_of`` is strictly past fy_end. FY boundaries are
resolved exclusively server-side from tenant configuration — a caller
can never supply or backdate a period (v1.1 rule 1).

Day-count convention for the pro-ration (documented, pinned by the
mid-year-joiner and leap-year oracles): the basis is the AVERAGE DAILY
BALANCE over the financial year — the arithmetic mean of the
member's end-of-day balances across every calendar day of the year
(365 days, or 366 in a financial year containing 29 February). A
balance held for D of N days therefore earns exactly D/N of a full
year's payout; the day the money arrives counts (its end-of-day
balance includes it), the days before it do not.

Rounding (one documented rule at one place, P13.11 failure mode 1):
each member's per-component figure is
``to_cents(basis * rate_pct / 100)`` — rounded exactly ONCE by the
single money primitive, from a basis that is itself a stored
NUMERIC(18,2) figure. Declaration totals are DERIVED as the sum of
the already-rounded per-member figures (never allocated down from a
pre-rounded pot), so SUM(member postings) equals the declared total
identically and the residue is zero by construction.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from genesis.domain.money import ZERO, to_cents

#: Percentage bounds mirrored by the 0017/0020 DB CHECKs.
_MAX_RATE = Decimal("100")


@dataclass(frozen=True)
class FinancialYear:
    """A tenant financial year [start, end], both dates inclusive."""

    start: date
    end: date

    @property
    def days(self) -> int:
        """Calendar days in the year: 365, or 366 across a 29 February."""
        return (self.end - self.start).days + 1


def financial_year_ending(end_month: int, end_year: int) -> FinancialYear:
    """The financial year whose last month is end_month of end_year."""
    if not 1 <= end_month <= 12:
        raise ValueError("financial_year_end_month must be in 1..12")
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    # The start is the first day of the following month, one year
    # earlier (a December year-end starts on 1 January of end_year).
    start = date(end_year - 1 if end_month < 12 else end_year, (end_month % 12) + 1, 1)
    return FinancialYear(start=start, end=end)


def last_completed_financial_year(as_of: date, end_month: int) -> FinancialYear:
    """The most recently completed financial year as of ``as_of``.

    The year ending in end_month of year Y is complete once as_of is
    strictly past its last day — declaring ON the closing day would
    recognise a year whose final day has not finished (the P11
    previous_quarter convention).
    """
    candidate = financial_year_ending(end_month, as_of.year)
    if as_of > candidate.end:
        return candidate
    return financial_year_ending(end_month, as_of.year - 1)


def entitlement_amount(basis: Decimal, rate_pct: Decimal) -> Decimal:
    """One member's payout component: basis x rate_pct / 100, cent-rounded.

    THE single rounding point for dividend/rebate figures (failure
    mode 1): rounded exactly once via the shared money primitive.
    Bounds mirror the DB CHECKs; violations are domain errors.
    """
    if basis < ZERO:
        raise ValueError("basis must not be negative")
    if rate_pct < ZERO or rate_pct > _MAX_RATE:
        raise ValueError("rate_pct must be in [0, 100]")
    return to_cents(basis * rate_pct / Decimal("100"))
