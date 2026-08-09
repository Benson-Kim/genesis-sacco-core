"""Deposit interest domain: quarter periods and accrual math.

Pure functions only — no I/O. Money rounding comes from
genesis.domain.money (reuse-first). The quarterly job accrues
simple interest on the account balance for the last completed calendar
quarter: amount = balance * annual_rate_pct / 100 / 4, rounded to cents
per account before any summation (cent-exact aggregates).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from genesis.domain.money import ZERO, to_cents

#: Four calendar quarters per year: Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec.
_QUARTER_MONTHS = (1, 4, 7, 10)


@dataclass(frozen=True)
class QuarterPeriod:
    """A calendar quarter [start, end], both dates inclusive."""

    start: date
    end: date


def quarter_of(d: date) -> QuarterPeriod:
    """The calendar quarter containing d."""
    start_month = _QUARTER_MONTHS[(d.month - 1) // 3]
    start = date(d.year, start_month, 1)

    next_start = date(d.year + 1, 1, 1) if start_month == 10 else date(d.year, start_month + 3, 1)
    return QuarterPeriod(start=start, end=next_start - timedelta(days=1))


def next_quarter(period: QuarterPeriod) -> QuarterPeriod:
    """The calendar quarter immediately after period (catch-up ordering)."""
    return quarter_of(period.end + timedelta(days=1))


def previous_quarter(as_of: date) -> QuarterPeriod:
    """The last completed calendar quarter strictly before as_of's quarter.

    The job accrues only for finished periods: accruing the running
    quarter would recognise interest before it has been earned.
    """
    return quarter_of(quarter_of(as_of).start - timedelta(days=1))


def quarterly_interest(balance: Decimal, annual_rate_pct: Decimal) -> Decimal:
    """Simple quarterly interest, rounded to cents per account.

    balance * annual_rate_pct / 400. Negative balances and rates outside
    (0, 100] are domain errors, mirroring the DB CHECK constraints.
    """
    if balance < ZERO:
        raise ValueError("balance must not be negative")
    if annual_rate_pct <= ZERO or annual_rate_pct > Decimal("100"):
        raise ValueError("annual_rate_pct must be in (0, 100]")
    return to_cents(balance * annual_rate_pct / Decimal("400"))
