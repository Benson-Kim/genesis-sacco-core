"""Unit tests for genesis.domain.approvals — pure, no I/O (issue #8 G2,
ADR-0008), plus the committee-quorum golden pin.

Every oracle is HAND-COMPUTED in comments (v1.2 rule 15), never
captured from the code under test. The band-boundary tests are the
<= vs < falsifiers at exactly 100k/500k/2M: flip either comparison in
required_band_index/authority_may_ratify and a test here names the
boundary that moved.

The committee golden pin (tests/golden/committee_quorum.json) freezes
the FULL decision table of domain/committee.decide at quorum 2 over
tallies 0..4 x 0..4, byte-identical: the approval engine ships beside
the committee path, and this pin proves the quorum machinery behaves
exactly as today — any drift (quorum constant, ambiguous-tally rule,
boundary) is a red test naming the changed cell.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from genesis.domain.approvals import (
    DEFAULT_APPROVAL_BANDS,
    BandSchedule,
    OperationType,
    bands_in_force,
    checker_may_ratify,
    maker_may_proceed,
    required_tier,
)
from genesis.domain.committee import COMMITTEE_QUORUM, decide
from genesis.domain.rbac import (
    BRANCH_MANAGER,
    CREDIT_COMMITTEE,
    LOAN_OFFICER,
    SYSTEM_ADMIN,
)
from genesis.domain.tenant_config import ApprovalBand

GOLDEN = Path(__file__).parent / "golden" / "committee_quorum.json"


# ---------------------------------------------------------------------------
# Day-one defaults pinned exactly (the prototype matrix)
# ---------------------------------------------------------------------------


def test_default_bands_pin_the_prototype_matrix() -> None:
    """Loan Officer <= 100k, Branch Manager <= 500k, Credit Committee
    <= 2M, Board above (the finite top band — deliberately not a
    platform role)."""
    expected = (
        ApprovalBand(authority=LOAN_OFFICER, max_amount=Decimal("100000.00")),
        ApprovalBand(authority=BRANCH_MANAGER, max_amount=Decimal("500000.00")),
        ApprovalBand(authority=CREDIT_COMMITTEE, max_amount=Decimal("2000000.00")),
    )
    assert expected == DEFAULT_APPROVAL_BANDS


def test_band_boundaries_are_inclusive_ceilings_at_100k() -> None:
    """<= vs < falsifier at the first boundary: 100,000.00 IS
    Loan-Officer business (tier 0); one cent more is tier 1."""
    assert required_tier(Decimal("99999.99"), DEFAULT_APPROVAL_BANDS) == 0
    assert required_tier(Decimal("100000.00"), DEFAULT_APPROVAL_BANDS) == 0
    assert required_tier(Decimal("100000.01"), DEFAULT_APPROVAL_BANDS) == 1


def test_band_boundaries_are_inclusive_ceilings_at_500k() -> None:
    """<= vs < falsifier at the second boundary."""
    assert required_tier(Decimal("499999.99"), DEFAULT_APPROVAL_BANDS) == 1
    assert required_tier(Decimal("500000.00"), DEFAULT_APPROVAL_BANDS) == 1
    assert required_tier(Decimal("500000.01"), DEFAULT_APPROVAL_BANDS) == 2


def test_band_boundaries_are_inclusive_ceilings_at_2m() -> None:
    """<= vs < falsifier at the top boundary: 2,000,000.00 is committee
    business; one cent more resolves to the Board tier (== len(bands)),
    which no platform role can satisfy."""
    assert required_tier(Decimal("1999999.99"), DEFAULT_APPROVAL_BANDS) == 2
    assert required_tier(Decimal("2000000.00"), DEFAULT_APPROVAL_BANDS) == 2
    assert required_tier(Decimal("2000000.01"), DEFAULT_APPROVAL_BANDS) == 3
    assert required_tier(Decimal("2000000.01"), DEFAULT_APPROVAL_BANDS) == len(
        DEFAULT_APPROVAL_BANDS
    )


def test_maker_authority_at_exact_ceilings() -> None:
    """Each default authority covers exactly its own ceiling and not a
    cent more (hand-computed against the prototype matrix)."""
    assert maker_may_proceed(LOAN_OFFICER, Decimal("100000.00"), DEFAULT_APPROVAL_BANDS)
    assert not maker_may_proceed(LOAN_OFFICER, Decimal("100000.01"), DEFAULT_APPROVAL_BANDS)
    assert maker_may_proceed(BRANCH_MANAGER, Decimal("500000.00"), DEFAULT_APPROVAL_BANDS)
    assert not maker_may_proceed(BRANCH_MANAGER, Decimal("500000.01"), DEFAULT_APPROVAL_BANDS)
    assert maker_may_proceed(CREDIT_COMMITTEE, Decimal("2000000.00"), DEFAULT_APPROVAL_BANDS)
    assert not maker_may_proceed(CREDIT_COMMITTEE, Decimal("2000000.01"), DEFAULT_APPROVAL_BANDS)


def test_unlisted_role_fails_closed_to_the_first_band() -> None:
    """A role a configured matrix does not list holds band-0 authority
    only (the authority_may_ratify fail-closed floor) — even the
    System Admin cannot ratify above 100k under the defaults."""
    assert maker_may_proceed(SYSTEM_ADMIN, Decimal("100000.00"), DEFAULT_APPROVAL_BANDS)
    assert not maker_may_proceed(SYSTEM_ADMIN, Decimal("100000.01"), DEFAULT_APPROVAL_BANDS)


def test_board_tier_has_no_platform_authority() -> None:
    """Above the finite top band NOBODY inside the platform ratifies —
    not even the highest listed authority."""
    for role in (LOAN_OFFICER, BRANCH_MANAGER, CREDIT_COMMITTEE, SYSTEM_ADMIN):
        assert not maker_may_proceed(role, Decimal("2000000.01"), DEFAULT_APPROVAL_BANDS)


# ---------------------------------------------------------------------------
# Effective-dated schedule selection (pure)
# ---------------------------------------------------------------------------

_STRICT = (
    ApprovalBand(authority=LOAN_OFFICER, max_amount=Decimal("50000.00")),
    ApprovalBand(authority=BRANCH_MANAGER, max_amount=Decimal("200000.00")),
    ApprovalBand(authority=CREDIT_COMMITTEE, max_amount=Decimal("2000000.00")),
)
_LOOSE = (
    ApprovalBand(authority=LOAN_OFFICER, max_amount=Decimal("1000000.00")),
    ApprovalBand(authority=BRANCH_MANAGER, max_amount=Decimal("1500000.00")),
    ApprovalBand(authority=CREDIT_COMMITTEE, max_amount=Decimal("2000000.00")),
)


def test_no_schedule_means_the_day_one_defaults() -> None:
    assert bands_in_force((), date(2026, 8, 19)) == DEFAULT_APPROVAL_BANDS


def test_future_dated_schedule_is_invisible_until_its_day() -> None:
    schedules = (BandSchedule(effective_from=date(2026, 9, 1), bands=_STRICT),)
    assert bands_in_force(schedules, date(2026, 8, 31)) == DEFAULT_APPROVAL_BANDS
    # ON its effective day the schedule governs (inclusive).
    assert bands_in_force(schedules, date(2026, 9, 1)) == _STRICT


def test_newest_effective_schedule_wins_regardless_of_input_order() -> None:
    schedules = (
        BandSchedule(effective_from=date(2026, 8, 10), bands=_LOOSE),
        BandSchedule(effective_from=date(2026, 8, 1), bands=_STRICT),
    )
    assert bands_in_force(schedules, date(2026, 8, 5)) == _STRICT
    assert bands_in_force(schedules, date(2026, 8, 10)) == _LOOSE
    assert bands_in_force(schedules, date(2026, 8, 19)) == _LOOSE


# ---------------------------------------------------------------------------
# Stricter-of-the-two ratification rule (pure)
# ---------------------------------------------------------------------------


def test_tightened_bands_bind_an_in_flight_request() -> None:
    """400k requested under the defaults (Branch-Manager tier); the
    tenant tightens to _STRICT (400k becomes committee tier): the
    Branch Manager may no longer ratify — the CURRENT band is the
    stricter one and it applies."""
    amount = Decimal("400000.00")
    assert not checker_may_ratify(BRANCH_MANAGER, amount, DEFAULT_APPROVAL_BANDS, _STRICT)
    assert checker_may_ratify(CREDIT_COMMITTEE, amount, DEFAULT_APPROVAL_BANDS, _STRICT)


def test_loosened_bands_never_retroactively_weaken_a_request() -> None:
    """400k requested under the defaults; the tenant loosens to _LOOSE
    (400k becomes Loan-Officer tier): the Loan Officer STILL may not
    ratify — the REQUEST-TIME band is the stricter one and it applies."""
    amount = Decimal("400000.00")
    assert not checker_may_ratify(LOAN_OFFICER, amount, DEFAULT_APPROVAL_BANDS, _LOOSE)
    assert checker_may_ratify(BRANCH_MANAGER, amount, DEFAULT_APPROVAL_BANDS, _LOOSE)


def test_both_band_sets_allowing_is_required_and_sufficient() -> None:
    amount = Decimal("40000.00")
    # 40k is Loan-Officer business under BOTH matrices.
    assert checker_may_ratify(LOAN_OFFICER, amount, DEFAULT_APPROVAL_BANDS, _STRICT)


# ---------------------------------------------------------------------------
# Operation vocabulary (code-owned, never free-form)
# ---------------------------------------------------------------------------


def test_operation_vocabulary_is_pinned() -> None:
    """Widening the vocabulary is deliberate: it ships WITH the wiring
    that consumes it (ADR-0008), so the set is pinned here."""
    assert {op.value for op in OperationType} == {
        "loan_disbursement",
        "repayment_adjustment",
        "misc_fee",
        "loan_write_off",
        "dividend_payout",
        "share_transfer",
        "member_exit_settlement",
        "withdrawal",
    }


# ---------------------------------------------------------------------------
# Committee quorum path: golden-pinned byte-identical (issue #8 EXIT)
# ---------------------------------------------------------------------------


def test_committee_quorum_decision_table_is_golden_pinned() -> None:
    """The FULL decision table at quorum 2 over tallies 0..4 x 0..4,
    byte-identical to tests/golden/committee_quorum.json.

    The golden file was written from the HAND-COMPUTED oracle
    (rejections >= 2 reject — rejection wins an ambiguous tally; else
    approvals >= 2 approve; else undecided), NOT from decide(). This
    test recomputes the table THROUGH decide() and requires the exact
    bytes to match: any change to the quorum constant, the
    conservatism rule or a boundary is a red test."""
    assert COMMITTEE_QUORUM == 2
    table = []
    for approvals in range(5):
        for rejections in range(5):
            decision = decide(approvals, rejections)
            table.append(
                {
                    "approvals": approvals,
                    "rejections": rejections,
                    "decision": decision.value if decision is not None else None,
                }
            )
    recomputed = json.dumps({"quorum": COMMITTEE_QUORUM, "table": table}, indent=2) + "\n"
    assert GOLDEN.read_bytes() == recomputed.encode(), (
        "committee quorum decision table drifted from the golden pin"
    )
