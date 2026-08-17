"""Unit tests for genesis.domain.members - pure, no I/O (P8, P13.13)."""

import datetime

import pytest

from genesis.domain.members import (
    MEMBER_OPERATIONS,
    InvalidStatusTransitionError,
    MemberStatus,
    MoneyOperation,
    dormancy_cutoff,
    format_member_no,
    member_may,
    transition,
)

_ALLOWED = {
    (MemberStatus.ACTIVE, MemberStatus.ARREARS),
    (MemberStatus.ARREARS, MemberStatus.ACTIVE),
    (MemberStatus.ACTIVE, MemberStatus.EXITED),
    (MemberStatus.ARREARS, MemberStatus.EXITED),
    # P13.13 dormancy lifecycle: Active -> Dormant (nightly job),
    # Dormant -> Active (deposit), Dormant -> Exited (P12 workflow).
    (MemberStatus.ACTIVE, MemberStatus.DORMANT),
    (MemberStatus.DORMANT, MemberStatus.ACTIVE),
    (MemberStatus.DORMANT, MemberStatus.EXITED),
}


@pytest.mark.parametrize("current", list(MemberStatus))
@pytest.mark.parametrize("target", list(MemberStatus))
def test_full_transition_matrix(current: MemberStatus, target: MemberStatus) -> None:
    """Every pair is either explicitly allowed or raises (gate 1.4)."""
    if (current, target) in _ALLOWED:
        assert transition(current, target) is target
    else:
        with pytest.raises(InvalidStatusTransitionError):
            transition(current, target)


def test_exited_is_terminal() -> None:
    for target in MemberStatus:
        with pytest.raises(InvalidStatusTransitionError):
            transition(MemberStatus.EXITED, target)


def test_member_no_formatting() -> None:
    assert format_member_no(1) == "GP-0001"
    assert format_member_no(42) == "GP-0042"
    assert format_member_no(9999) == "GP-9999"
    assert format_member_no(10000) == "GP-10000"


def test_member_no_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        format_member_no(0)
    with pytest.raises(ValueError, match="positive"):
        format_member_no(-3)


# ---------------------------------------------------------------------------
# P13.13 — the code-owned money-operation capability map (FM2)
# ---------------------------------------------------------------------------

# The EXACT allow-sets, restated by hand (never derived from the map
# under test): widening ANY of them — e.g. letting dormant members
# withdraw, borrow, pledge, top up or transfer shares — is a failing
# diff here (falsifiability, MASTER_PROMPT section 4).
_EXPECTED_OPERATIONS = {
    MoneyOperation.DEPOSIT: {MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.DORMANT},
    MoneyOperation.LOAN_REPAYMENT: {
        MemberStatus.ACTIVE,
        MemberStatus.ARREARS,
        MemberStatus.DORMANT,
    },
    MoneyOperation.SHARE_TOPUP: {MemberStatus.ACTIVE, MemberStatus.ARREARS},
    MoneyOperation.WITHDRAWAL: {MemberStatus.ACTIVE},
    MoneyOperation.BORROW: {MemberStatus.ACTIVE},
    MoneyOperation.PLEDGE: {MemberStatus.ACTIVE},
    MoneyOperation.SHARE_TRANSFER_OUT: {MemberStatus.ACTIVE},
    MoneyOperation.SHARE_TRANSFER_IN: {MemberStatus.ACTIVE},
    MoneyOperation.EXIT_REQUEST: {
        MemberStatus.ACTIVE,
        MemberStatus.ARREARS,
        MemberStatus.DORMANT,
    },
    # P13.15: a fee is money IN (the member pays it); exited members
    # are refused like every operation (terminal).
    MoneyOperation.FEE: {MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.DORMANT},
}


def test_capability_map_covers_every_operation_with_the_exact_allow_sets() -> None:
    """The single gatekeeper's policy, pinned operation by operation."""
    assert set(MEMBER_OPERATIONS) == set(MoneyOperation)
    for operation in MoneyOperation:
        assert MEMBER_OPERATIONS[operation] == _EXPECTED_OPERATIONS[operation], operation


@pytest.mark.parametrize("operation", list(MoneyOperation))
def test_exited_members_may_perform_no_money_operation(operation: MoneyOperation) -> None:
    assert not member_may(MemberStatus.EXITED, operation)


def test_dormant_members_may_only_deposit_repay_pay_fees_and_request_exit() -> None:
    # P13.15: FEE joined the money-in set (deposit/repayment statuses) —
    # a dormant member may settle a fee, never move money out.
    allowed = {op for op in MoneyOperation if member_may(MemberStatus.DORMANT, op)}
    assert allowed == {
        MoneyOperation.DEPOSIT,
        MoneyOperation.LOAN_REPAYMENT,
        MoneyOperation.FEE,
        MoneyOperation.EXIT_REQUEST,
    }


# ---------------------------------------------------------------------------
# P13.13 — dormancy_cutoff month arithmetic (hand-computed oracles)
# ---------------------------------------------------------------------------


def test_dormancy_cutoff_hand_computed_oracles() -> None:
    """HAND-COMPUTED calendar oracles, never captured from the code:

    * plain case: 2026-08-01 minus 6 months = 2026-02-01
    * year wrap:  2026-03-15 minus 6 months = 2025-09-15
    * clamping:   2026-03-31 minus 1 month  = 2026-02-28 (Feb has 28)
    * leap year:  2024-03-31 minus 1 month  = 2024-02-29 (Feb has 29)
    * 31 -> 30:   2026-07-31 minus 1 month  = 2026-06-30 (June has 30)
    * long span:  2026-01-31 minus 24 months = 2024-01-31 (no clamp)
    """
    assert dormancy_cutoff(datetime.date(2026, 8, 1), 6) == datetime.date(2026, 2, 1)
    assert dormancy_cutoff(datetime.date(2026, 3, 15), 6) == datetime.date(2025, 9, 15)
    assert dormancy_cutoff(datetime.date(2026, 3, 31), 1) == datetime.date(2026, 2, 28)
    assert dormancy_cutoff(datetime.date(2024, 3, 31), 1) == datetime.date(2024, 2, 29)
    assert dormancy_cutoff(datetime.date(2026, 7, 31), 1) == datetime.date(2026, 6, 30)
    assert dormancy_cutoff(datetime.date(2026, 1, 31), 24) == datetime.date(2024, 1, 31)


def test_dormancy_cutoff_rejects_non_positive_periods() -> None:
    with pytest.raises(ValueError, match="at least one month"):
        dormancy_cutoff(datetime.date(2026, 8, 1), 0)
