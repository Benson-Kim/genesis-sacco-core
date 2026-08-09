"""Unit tests for the P12 exit settlement domain (pure, no I/O)."""

from decimal import Decimal

import pytest

from genesis.domain.exits import (
    ExitStatus,
    InvalidExitTransitionError,
    compute_settlement,
    exit_transition,
)
from genesis.domain.ledger import (
    Account,
    Channel,
    LedgerLine,
    Side,
    TxnType,
    build_exit_settlement_posting,
)

# ---------------------------------------------------------------------------
# Status machine: full matrix (gate 1.4)
# ---------------------------------------------------------------------------

_LEGAL = {
    (ExitStatus.REQUESTED, ExitStatus.APPROVED),
    (ExitStatus.REQUESTED, ExitStatus.REJECTED),
    (ExitStatus.APPROVED, ExitStatus.SETTLED),
    (ExitStatus.APPROVED, ExitStatus.REJECTED),
}


def test_exit_transition_full_matrix() -> None:
    for current in ExitStatus:
        for target in ExitStatus:
            if (current, target) in _LEGAL:
                assert exit_transition(current, target) is target
            else:
                with pytest.raises(InvalidExitTransitionError):
                    exit_transition(current, target)


def test_settled_and_rejected_are_terminal() -> None:
    for terminal in (ExitStatus.SETTLED, ExitStatus.REJECTED):
        for target in ExitStatus:
            with pytest.raises(InvalidExitTransitionError):
                exit_transition(terminal, target)


# ---------------------------------------------------------------------------
# Settlement arithmetic (hand-computed, never oracle-from-implementation)
# ---------------------------------------------------------------------------


def test_compute_settlement_hand_computed() -> None:
    # shares 30000 + deposits 185000 = equity 215000
    # loan: 100000 principal + 1000 interest + 500 penalties = 101500
    # fee 500 -> net = 215000 - 101500 - 500 = 113000
    comp = compute_settlement(
        shares=Decimal("30000.00"),
        deposits=Decimal("185000.00"),
        loan_principal=Decimal("100000.00"),
        loan_interest=Decimal("1000.00"),
        loan_penalties=Decimal("500.00"),
        fee=Decimal("500.00"),
    )
    assert comp.equity == Decimal("215000.00")
    assert comp.loan_total == Decimal("101500.00")
    assert comp.net_payable == Decimal("113000.00")


def test_compute_settlement_negative_net_is_representable() -> None:
    # equity 5000, loan 9000 -> net -4000: the computation itself stays
    # honest about the shortfall; the application layer rejects it at
    # request time (documented rule) and the posting builder refuses it.
    comp = compute_settlement(
        shares=Decimal("1000.00"),
        deposits=Decimal("4000.00"),
        loan_principal=Decimal("9000.00"),
        loan_interest=Decimal("0"),
        loan_penalties=Decimal("0"),
        fee=Decimal("0"),
    )
    assert comp.net_payable == Decimal("-4000.00")


def test_compute_settlement_rejects_negative_components() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        compute_settlement(
            shares=Decimal("-1.00"),
            deposits=Decimal("0"),
            loan_principal=Decimal("0"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("0"),
        )
    with pytest.raises(ValueError, match="must not be negative"):
        compute_settlement(
            shares=Decimal("0"),
            deposits=Decimal("0"),
            loan_principal=Decimal("0"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("-0.01"),
        )


# ---------------------------------------------------------------------------
# Exit settlement posting builder
# ---------------------------------------------------------------------------


def _legs(spec_lines: tuple[LedgerLine, ...]) -> dict[tuple[Account, Side], Decimal]:
    return {(line.account, line.side): line.amount for line in spec_lines}


def test_exit_posting_netted_loan_legs_hand_computed() -> None:
    # DR shares 30000 + DR deposits 185000 = 215000
    # CR loan 100000 + CR interest 1000 + CR penalties 500 + CR fee 500
    # + CR cash 113000 = 215000 (balanced by construction)
    spec = build_exit_settlement_posting(
        shares=Decimal("30000.00"),
        deposits=Decimal("185000.00"),
        loan_principal=Decimal("100000.00"),
        loan_interest=Decimal("1000.00"),
        loan_penalties=Decimal("500.00"),
        fee=Decimal("500.00"),
        channel=Channel.BANK,
    )
    spec.assert_balanced()
    assert spec.txn_type is TxnType.EXIT_SETTLEMENT
    assert spec.amount == Decimal("215000.00")
    legs = _legs(spec.lines)
    assert legs[(Account.MEMBER_SHARES, Side.DEBIT)] == Decimal("30000.00")
    assert legs[(Account.MEMBER_DEPOSITS, Side.DEBIT)] == Decimal("185000.00")
    assert legs[(Account.LOANS_RECEIVABLE, Side.CREDIT)] == Decimal("100000.00")
    assert legs[(Account.INTEREST_INCOME, Side.CREDIT)] == Decimal("1000.00")
    assert legs[(Account.PENALTY_INCOME, Side.CREDIT)] == Decimal("500.00")
    assert legs[(Account.FEE_INCOME, Side.CREDIT)] == Decimal("500.00")
    assert legs[(Account.CASH_BANK, Side.CREDIT)] == Decimal("113000.00")
    assert len(spec.lines) == 7


def test_exit_posting_no_loan_no_fee_is_a_pure_refund() -> None:
    # equity 116000, nothing netted -> single CR cash leg of 116000
    spec = build_exit_settlement_posting(
        shares=Decimal("20000.00"),
        deposits=Decimal("96000.00"),
        loan_principal=Decimal("0"),
        loan_interest=Decimal("0"),
        loan_penalties=Decimal("0"),
        fee=Decimal("0"),
        channel=Channel.MPESA,
    )
    spec.assert_balanced()
    legs = _legs(spec.lines)
    assert legs[(Account.CASH_MPESA, Side.CREDIT)] == Decimal("116000.00")
    assert len(spec.lines) == 3


def test_exit_posting_zero_net_setoff_posts_internally() -> None:
    # equity 5000 exactly consumed by the loan payoff: no cash leg, so
    # the INTERNAL channel is valid (no suspense leg is ever built).
    spec = build_exit_settlement_posting(
        shares=Decimal("0"),
        deposits=Decimal("5000.00"),
        loan_principal=Decimal("5000.00"),
        loan_interest=Decimal("0"),
        loan_penalties=Decimal("0"),
        fee=Decimal("0"),
        channel=Channel.INTERNAL,
    )
    spec.assert_balanced()
    legs = _legs(spec.lines)
    assert (Account.SUSPENSE, Side.CREDIT) not in legs
    assert (Account.SUSPENSE, Side.DEBIT) not in legs
    assert legs[(Account.LOANS_RECEIVABLE, Side.CREDIT)] == Decimal("5000.00")
    assert len(spec.lines) == 2


def test_exit_posting_guards() -> None:
    # Negative net can never be posted (final gate behind the app rule).
    with pytest.raises(ValueError, match="negative exit settlement"):
        build_exit_settlement_posting(
            shares=Decimal("0"),
            deposits=Decimal("1000.00"),
            loan_principal=Decimal("2000.00"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("0"),
            channel=Channel.BANK,
        )
    # Zero equity has nothing to post; the caller skips the ledger.
    with pytest.raises(ValueError, match="zero equity"):
        build_exit_settlement_posting(
            shares=Decimal("0"),
            deposits=Decimal("0"),
            loan_principal=Decimal("0"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("0"),
            channel=Channel.BANK,
        )
    # A net refund leaving the SACCO demands a cash channel.
    with pytest.raises(ValueError, match="cash channel"):
        build_exit_settlement_posting(
            shares=Decimal("100.00"),
            deposits=Decimal("0"),
            loan_principal=Decimal("0"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("0"),
            channel=Channel.INTERNAL,
        )
    # Negative components are refused outright.
    with pytest.raises(ValueError, match="must not be negative"):
        build_exit_settlement_posting(
            shares=Decimal("100.00"),
            deposits=Decimal("-1.00"),
            loan_principal=Decimal("0"),
            loan_interest=Decimal("0"),
            loan_penalties=Decimal("0"),
            fee=Decimal("0"),
            channel=Channel.BANK,
        )
