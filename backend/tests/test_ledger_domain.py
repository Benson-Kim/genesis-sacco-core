# ruff: noqa: I001
"""Unit tests for genesis.domain.ledger — pure, no I/O (P7 gate 1.5)."""

from collections.abc import Callable
from decimal import Decimal

import pytest

from genesis.domain.ledger import (
    MEMBER_INITIATED,
    Account,
    Channel,
    LedgerLine,
    PostingSpec,
    Side,
    TxnType,
    build_allocated_repayment_posting,
    build_deposit_interest_posting,
    build_deposit_posting,
    build_disbursement_posting,
    build_loan_interest_accrual_posting,
    build_repayment_posting,
    build_reversal_posting,
    build_share_topup_posting,
    build_unclaimed_dividend_posting,
    build_withdrawal_posting,
    member_initiated_types,
    ref_prefix,
)


# ---------------------------------------------------------------------------
# LedgerLine validation
# ---------------------------------------------------------------------------


def test_ledger_line_rejects_zero_amount() -> None:
    with pytest.raises(ValueError, match="positive"):
        LedgerLine(account=Account.CASH_MPESA, side=Side.DEBIT, amount=Decimal("0"))


def test_ledger_line_rejects_negative_amount() -> None:
    with pytest.raises(ValueError, match="positive"):
        LedgerLine(account=Account.CASH_MPESA, side=Side.DEBIT, amount=Decimal("-1"))


# ---------------------------------------------------------------------------
# PostingSpec validation
# ---------------------------------------------------------------------------


def test_posting_spec_rejects_zero_amount() -> None:
    with pytest.raises(ValueError, match="positive"):
        PostingSpec(
            txn_type=TxnType.DEPOSIT,
            channel=Channel.MPESA,
            amount=Decimal("0"),
            lines=(
                LedgerLine(Account.CASH_MPESA, Side.DEBIT, Decimal("100")),
                LedgerLine(Account.MEMBER_DEPOSITS, Side.CREDIT, Decimal("100")),
            ),
        )


def test_posting_spec_rejects_empty_lines() -> None:
    with pytest.raises(ValueError, match="at least one line"):
        PostingSpec(
            txn_type=TxnType.DEPOSIT,
            channel=Channel.MPESA,
            amount=Decimal("100"),
            lines=(),
        )


def test_assert_balanced_raises_on_imbalance() -> None:
    spec = PostingSpec(
        txn_type=TxnType.DEPOSIT,
        channel=Channel.MPESA,
        amount=Decimal("100"),
        lines=(
            LedgerLine(Account.CASH_MPESA, Side.DEBIT, Decimal("100")),
            LedgerLine(Account.MEMBER_DEPOSITS, Side.CREDIT, Decimal("99")),
        ),
    )
    with pytest.raises(ValueError, match="Unbalanced"):
        spec.assert_balanced()


# ---------------------------------------------------------------------------
# Posting factory functions — balance invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel",
    [Channel.MPESA, Channel.BANK],
)
def test_deposit_posting_is_balanced(channel: Channel) -> None:
    build_deposit_posting(Decimal("5000.00"), channel).assert_balanced()


@pytest.mark.parametrize(
    "channel",
    [Channel.MPESA, Channel.BANK],
)
def test_withdrawal_posting_is_balanced(channel: Channel) -> None:
    build_withdrawal_posting(Decimal("5000.00"), channel).assert_balanced()


def test_share_topup_posting_is_balanced() -> None:
    build_share_topup_posting(Decimal("5000.00"), Channel.MPESA).assert_balanced()


def test_disbursement_posting_is_balanced() -> None:
    build_disbursement_posting(Decimal("5000.00"), Channel.BANK).assert_balanced()


def test_repayment_posting_is_balanced() -> None:
    build_repayment_posting(Decimal("5000.00"), Channel.MPESA).assert_balanced()


def test_allocated_repayment_posting_is_balanced_with_split_legs() -> None:
    spec = build_allocated_repayment_posting(
        Decimal("150.00"), Decimal("240.00"), Decimal("610.00"), Channel.MPESA
    )
    spec.assert_balanced()
    assert spec.amount == Decimal("1000.00")
    assert spec.txn_type is TxnType.LOAN_REPAYMENT
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    cr = {ln.account: ln.amount for ln in spec.lines if ln.side is Side.CREDIT}
    assert dr[0].account is Account.CASH_MPESA
    assert dr[0].amount == Decimal("1000.00")
    assert cr[Account.PENALTY_INCOME] == Decimal("150.00")
    assert cr[Account.INTEREST_INCOME] == Decimal("240.00")
    assert cr[Account.LOANS_RECEIVABLE] == Decimal("610.00")


def test_allocated_repayment_posting_skips_zero_legs() -> None:
    spec = build_allocated_repayment_posting(
        Decimal("0"), Decimal("0"), Decimal("500.00"), Channel.BANK
    )
    spec.assert_balanced()
    assert len(spec.lines) == 2
    accounts = {ln.account for ln in spec.lines}
    assert Account.PENALTY_INCOME not in accounts
    assert Account.INTEREST_INCOME not in accounts


def test_allocated_repayment_posting_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="negative"):
        build_allocated_repayment_posting(
            Decimal("-1"), Decimal("0"), Decimal("100"), Channel.MPESA
        )
    with pytest.raises(ValueError, match="positive total"):
        build_allocated_repayment_posting(Decimal("0"), Decimal("0"), Decimal("0"), Channel.MPESA)


def test_loan_interest_accrual_posting_is_balanced() -> None:
    build_loan_interest_accrual_posting(Decimal("250.50")).assert_balanced()


def test_deposit_interest_posting_is_balanced() -> None:
    build_deposit_interest_posting(Decimal("250.50")).assert_balanced()


# ---------------------------------------------------------------------------
# Interest postings — correct accounts (no suspense leg)
# ---------------------------------------------------------------------------


def test_loan_interest_accrual_dr_receivable_cr_income() -> None:
    spec = build_loan_interest_accrual_posting(Decimal("300"))
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    cr = [ln for ln in spec.lines if ln.side is Side.CREDIT]
    assert dr[0].account is Account.INTEREST_RECEIVABLE
    assert cr[0].account is Account.INTEREST_INCOME


def test_deposit_interest_dr_expense_cr_member_deposits() -> None:
    spec = build_deposit_interest_posting(Decimal("300"))
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    cr = [ln for ln in spec.lines if ln.side is Side.CREDIT]
    assert dr[0].account is Account.INTEREST_EXPENSE
    assert cr[0].account is Account.MEMBER_DEPOSITS


def test_interest_postings_never_touch_suspense() -> None:
    for spec in (
        build_loan_interest_accrual_posting(Decimal("10")),
        build_deposit_interest_posting(Decimal("10")),
    ):
        assert all(ln.account is not Account.SUSPENSE for ln in spec.lines)


# ---------------------------------------------------------------------------
# Deposit posting — correct accounts
# ---------------------------------------------------------------------------


def test_deposit_mpesa_uses_cash_mpesa() -> None:
    spec = build_deposit_posting(Decimal("1000"), Channel.MPESA)
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    cr = [ln for ln in spec.lines if ln.side is Side.CREDIT]
    assert dr[0].account is Account.CASH_MPESA
    assert cr[0].account is Account.MEMBER_DEPOSITS


def test_deposit_bank_uses_cash_bank() -> None:
    spec = build_deposit_posting(Decimal("1000"), Channel.BANK)
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    assert dr[0].account is Account.CASH_BANK


# ---------------------------------------------------------------------------
# Disbursement posting — correct accounts
# ---------------------------------------------------------------------------


def test_disbursement_dr_loans_receivable() -> None:
    spec = build_disbursement_posting(Decimal("50000"), Channel.BANK)
    dr = [ln for ln in spec.lines if ln.side is Side.DEBIT]
    cr = [ln for ln in spec.lines if ln.side is Side.CREDIT]
    assert dr[0].account is Account.LOANS_RECEIVABLE
    assert cr[0].account is Account.CASH_BANK


# ---------------------------------------------------------------------------
# Reversal posting
# ---------------------------------------------------------------------------


def test_reversal_flips_sides_and_is_balanced() -> None:
    original = build_deposit_posting(Decimal("2000"), Channel.MPESA)
    reversal = build_reversal_posting(original)
    reversal.assert_balanced()
    # Every line's side should be flipped.
    for orig_ln, rev_ln in zip(original.lines, reversal.lines, strict=True):
        assert orig_ln.account == rev_ln.account
        assert orig_ln.amount == rev_ln.amount
        assert orig_ln.side != rev_ln.side


def test_reversal_of_reversal_equals_original() -> None:
    original = build_deposit_posting(Decimal("3000"), Channel.BANK)
    double_reversal = build_reversal_posting(build_reversal_posting(original))
    assert double_reversal.lines == original.lines


# ---------------------------------------------------------------------------
# Reference prefix mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "txn_type,channel,expected_prefix",
    [
        (TxnType.DEPOSIT, Channel.MPESA, "MP-"),
        (TxnType.DEPOSIT, Channel.BANK, "BK-"),
        (TxnType.WITHDRAWAL, Channel.MPESA, "WD-"),
        (TxnType.WITHDRAWAL, Channel.BANK, "WD-"),
        (TxnType.SHARE_TOPUP, Channel.MPESA, "SH-"),
        (TxnType.LOAN_DISBURSEMENT, Channel.BANK, "LN-"),
        (TxnType.LOAN_REPAYMENT, Channel.MPESA, "RP-"),
        (TxnType.INTEREST_POSTING, Channel.ACCRUAL, "INT-"),
    ],
)
def test_ref_prefix_mapping(txn_type: TxnType, channel: Channel, expected_prefix: str) -> None:
    assert ref_prefix(txn_type, channel) == expected_prefix


@pytest.mark.parametrize("channel", [Channel.ACCRUAL, Channel.INTERNAL])
def test_ref_prefix_rejects_non_cash_deposit_channels(channel: Channel) -> None:
    with pytest.raises(ValueError, match="MPESA or BANK"):
        ref_prefix(TxnType.DEPOSIT, channel)


@pytest.mark.parametrize("channel", [Channel.ACCRUAL, Channel.INTERNAL])
def test_build_deposit_posting_rejects_non_cash_channels(channel: Channel) -> None:
    with pytest.raises(ValueError, match="MPESA or BANK"):
        build_deposit_posting(Decimal("100"), channel)


@pytest.mark.parametrize(
    "build",
    [
        build_withdrawal_posting,
        build_share_topup_posting,
        build_disbursement_posting,
        build_repayment_posting,
    ],
)
@pytest.mark.parametrize("channel", [Channel.ACCRUAL, Channel.INTERNAL])
def test_cash_posting_factories_reject_non_cash_channels(
    build: Callable[[Decimal, Channel], PostingSpec], channel: Channel
) -> None:
    """A structural cash leg must never route to suspense (external
    Codex review, re-derived): ACCRUAL/INTERNAL on a cash factory is a
    caller bug and raises instead of hiding the posting inside the
    exceptional-items account. Falsifiable: restore the old
    `return Account.SUSPENSE` fallback in _cash_account and this test
    fails (the specs would build successfully with a suspense leg)."""
    with pytest.raises(ValueError, match="MPESA or BANK"):
        build(Decimal("100"), channel)


def test_allocated_repayment_rejects_non_cash_channels() -> None:
    """Same guard through the split-leg factory (its cash DR leg)."""
    with pytest.raises(ValueError, match="MPESA or BANK"):
        build_allocated_repayment_posting(
            Decimal("1"), Decimal("1"), Decimal("1"), Channel.INTERNAL
        )


# ---------------------------------------------------------------------------
# Money rounding — amounts are always 2 d.p.
# ---------------------------------------------------------------------------


def test_deposit_amount_rounded_to_cents() -> None:
    spec = build_deposit_posting(Decimal("999.999"), Channel.MPESA)
    assert spec.amount == Decimal("1000.00")
    for ln in spec.lines:
        assert ln.amount == Decimal("1000.00")


# ---------------------------------------------------------------------------
# P13.13 FM1 — the member-initiated activity classification
# ---------------------------------------------------------------------------


def test_unclaimed_dividend_posting_parks_the_payable_never_member_accounts() -> None:
    """Issue #19 P3: DR expense / CR liability.unclaimed_dividends,
    balanced; member accounts NEVER appear (the member exited — their
    balances are settled). HAND-COMPUTED: 600.00 dividend + 36.00
    rebate -> 636.00 credited to the payable, DV- semantics preserved
    (dividend_posting type, ACCRUAL channel). Falsifiable: point a
    credit leg back at member.shares/member.deposits and the
    account-absence asserts fail."""
    spec = build_unclaimed_dividend_posting(Decimal("600.00"), Decimal("36.00"))
    spec.assert_balanced()
    assert spec.txn_type is TxnType.DIVIDEND_POSTING
    assert spec.channel is Channel.ACCRUAL
    assert spec.amount == Decimal("636.00")
    accounts = {ln.account for ln in spec.lines}
    assert Account.UNCLAIMED_DIVIDENDS in accounts
    assert Account.MEMBER_SHARES not in accounts
    assert Account.MEMBER_DEPOSITS not in accounts
    payable = sum(
        (
            ln.amount
            for ln in spec.lines
            if ln.account is Account.UNCLAIMED_DIVIDENDS and ln.side is Side.CREDIT
        ),
        Decimal("0"),
    )
    assert payable == Decimal("636.00")


def test_unclaimed_dividend_posting_rejects_zero_and_negative_components() -> None:
    with pytest.raises(ValueError, match="zero unclaimed"):
        build_unclaimed_dividend_posting(Decimal("0"), Decimal("0"))
    with pytest.raises(ValueError, match="negative"):
        build_unclaimed_dividend_posting(Decimal("-1"), Decimal("0"))


def test_member_initiated_map_classifies_every_transaction_type() -> None:
    """Completeness pin: a NEW transaction type cannot land without a
    deliberate dormancy classification here — the universal bank bug
    is an unclassified system posting silently counting as activity."""
    assert set(MEMBER_INITIATED) == set(TxnType)


def test_member_initiated_allow_list_is_exactly_the_hand_written_set() -> None:
    """The EXACT allow-list, restated by hand (never derived from the
    map under test). Falsifiable both ways: widening it (e.g. counting
    interest or dividend postings — the P13.13 FM1 gaming vector) or
    narrowing it (dropping deposits) is a failing diff."""
    assert member_initiated_types() == (
        "deposit",
        "exit_settlement",
        "loan_disbursement",
        "loan_repayment",
        "share_topup",
        "share_transfer_out",
        "withdrawal",
    )
    # The system postings that must NEVER reset the dormancy clock.
    assert not MEMBER_INITIATED[TxnType.INTEREST_POSTING]
    assert not MEMBER_INITIATED[TxnType.DIVIDEND_POSTING]
    assert not MEMBER_INITIATED[TxnType.SHARE_TRANSFER_IN]
