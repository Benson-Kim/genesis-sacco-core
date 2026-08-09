"""Money primitives: the single source of rounding truth (the house doctrine 1.1).

Currency is KES held as Decimal - money is NEVER float. Every module that
touches money imports these helpers instead of re-implementing rounding.
Pure functions only: no I/O, no imports from other layers.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_cents(value: Decimal) -> Decimal:
    """Round to 2 decimal places, half up. The only money rounding allowed."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def monthly_rate(annual_rate_pct: Decimal) -> Decimal:
    """Convert an annual percentage rate to a monthly fractional rate."""
    return annual_rate_pct / Decimal(100) / Decimal(12)
