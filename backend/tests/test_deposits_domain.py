"""Unit tests for genesis.domain.deposits and the direction map - pure, no I/O (P11)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from genesis.domain.deposits import next_quarter, previous_quarter, quarter_of, quarterly_interest
from genesis.domain.ledger import Side, TxnType, member_direction


def test_quarter_of_contains_date_and_boundaries() -> None:
    q = quarter_of(date(2026, 7, 29))
    assert q.start == date(2026, 7, 1)
    assert q.end == date(2026, 9, 30)
    for month in range(1, 13):
        d = date(2026, month, 15)
        q = quarter_of(d)
        assert q.start <= d <= q.end
        assert q.start.day == 1
        assert (q.end + timedelta(days=1)).day == 1  # ends the day before a month starts


def test_previous_quarter_is_contiguous_and_crosses_years() -> None:
    for month in range(1, 13):
        d = date(2026, month, 15)
        prev = previous_quarter(d)
        assert prev.end == quarter_of(d).start - timedelta(days=1)
    prev = previous_quarter(date(2026, 1, 15))
    assert prev.start == date(2025, 10, 1)
    assert prev.end == date(2025, 12, 31)


def test_next_quarter_is_contiguous_and_crosses_years() -> None:
    for month in range(1, 13):
        q = quarter_of(date(2026, month, 15))
        nq = next_quarter(q)
        assert nq.start == q.end + timedelta(days=1)  # no gap, no overlap
        assert previous_quarter(nq.start) == q  # inverse of previous_quarter
    assert next_quarter(quarter_of(date(2025, 11, 1))).start == date(2026, 1, 1)


def test_quarterly_interest_hand_computed_values() -> None:
    # Oracles computed by hand, never captured from the implementation:
    # 10000 at 8% p.a. for one quarter = 10000 * 0.08 / 4 = 200.00 exactly.
    assert quarterly_interest(Decimal("10000.00"), Decimal("8.00")) == Decimal("200.00")
    # 1000.01 at 7.25%: 1000.01 * 7.25 / 400 = 18.12518125 -> 18.13.
    assert quarterly_interest(Decimal("1000.01"), Decimal("7.25")) == Decimal("18.13")
    # Sub-cent interest rounds to zero (recorded by the job, never posted).
    assert quarterly_interest(Decimal("0.01"), Decimal("8.00")) == Decimal("0.00")
    assert quarterly_interest(Decimal("0"), Decimal("8.00")) == Decimal("0.00")


def test_quarterly_interest_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="balance"):
        quarterly_interest(Decimal("-1"), Decimal("8"))
    with pytest.raises(ValueError, match="annual_rate_pct"):
        quarterly_interest(Decimal("100"), Decimal("0"))
    with pytest.raises(ValueError, match="annual_rate_pct"):
        quarterly_interest(Decimal("100"), Decimal("100.01"))


def test_member_direction_and_reversal_flip() -> None:
    assert member_direction(TxnType.DEPOSIT) is Side.CREDIT
    assert member_direction(TxnType.SHARE_TOPUP) is Side.CREDIT
    assert member_direction(TxnType.WITHDRAWAL) is Side.DEBIT
    assert member_direction(TxnType.LOAN_DISBURSEMENT) is Side.DEBIT
    assert member_direction(TxnType.DEPOSIT, is_reversal=True) is Side.DEBIT
    assert member_direction(TxnType.WITHDRAWAL, is_reversal=True) is Side.CREDIT
