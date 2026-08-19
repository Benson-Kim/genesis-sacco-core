"""Pure approval-engine rules (issue #8 / gap register G2; ADR-0008).

The limits/approval engine's decision logic, kept pure (MASTER_PROMPT
2.1): band-schedule selection, tier resolution and the
stricter-of-the-two ratification rule are functions of their inputs
only. Persistence (the effective-dated ``approval_band_sets`` table
and the ``pending_approvals`` workflow rows, migration 0049) lives in
``genesis/application/approvals.py``.

Reuse-first: the band vocabulary and boundary semantics are NOT
re-implemented here — ``ApprovalBand``, ``required_band_index`` and
``authority_may_ratify`` come from ``genesis/domain/tenant_config``
(the P13.7 approval-matrix work), so the engine and the existing
committee consumers can never diverge on what a band means.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from genesis.domain.rbac import BRANCH_MANAGER, CREDIT_COMMITTEE, LOAN_OFFICER
from genesis.domain.tenant_config import (
    ApprovalBand,
    authority_may_ratify,
    required_band_index,
)

__all__ = [
    "DEFAULT_APPROVAL_BANDS",
    "BandSchedule",
    "OperationType",
    "bands_in_force",
    "checker_may_ratify",
    "maker_may_proceed",
    "required_tier",
]

#: Day-one defaults — the prototype's authority matrix, pinned exactly
#: (golden tests assert the boundaries at 100k/500k/2M to the cent).
#: The finite top band is deliberate: amounts above 2,000,000.00
#: resolve to the "Board" tier, which is NOT a platform role (the
#: explicit top-band rule of validate_approval_bands), so nothing
#: inside the platform can self-ratify a board-tier amount.
DEFAULT_APPROVAL_BANDS: tuple[ApprovalBand, ...] = (
    ApprovalBand(authority=LOAN_OFFICER, max_amount=Decimal("100000.00")),
    ApprovalBand(authority=BRANCH_MANAGER, max_amount=Decimal("500000.00")),
    ApprovalBand(authority=CREDIT_COMMITTEE, max_amount=Decimal("2000000.00")),
)


class OperationType(enum.StrEnum):
    """Code-owned vocabulary of posting-capable operations (ADR-0008).

    Every operation routed through the engine declares itself with one
    of these values — never a free-form caller string. The set names
    the money-mutating workflows that exist today; widening it is a
    one-line change shipped WITH the wiring that consumes it (the
    wiring follow-up work item routes each posting path through
    ``application/approvals.submit_operation``).
    """

    LOAN_DISBURSEMENT = "loan_disbursement"
    REPAYMENT_ADJUSTMENT = "repayment_adjustment"
    MISC_FEE = "misc_fee"
    LOAN_WRITE_OFF = "loan_write_off"
    DIVIDEND_PAYOUT = "dividend_payout"
    SHARE_TRANSFER = "share_transfer"
    MEMBER_EXIT_SETTLEMENT = "member_exit_settlement"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True)
class BandSchedule:
    """One effective-dated band matrix: the bands in force FROM a date.

    Rows of ``approval_band_sets`` (append-only, migration 0049): a
    tenant reconfigures its limits by appending a new schedule, never
    by editing one — so "which bands were in force on date D" is
    always answerable from history.
    """

    effective_from: date
    bands: tuple[ApprovalBand, ...]


def bands_in_force(schedules: Sequence[BandSchedule], as_of: date) -> tuple[ApprovalBand, ...]:
    """The band matrix in force at ``as_of`` — pure schedule selection.

    The newest schedule whose ``effective_from`` is on or before
    ``as_of`` wins; a future-dated schedule is invisible until its
    day arrives. With no eligible schedule the code-owned
    DEFAULT_APPROVAL_BANDS apply (day-one behaviour: a tenant that has
    never configured bands gets the prototype matrix exactly).

    Ties are impossible by construction: uq_approval_band_sets_effective
    (0049) makes (tenant_id, effective_from) unique. Linear scan; the
    schedule list is one tenant's append-only config history (bounded
    small; documented bound like the band scans it feeds).
    """
    chosen: BandSchedule | None = None
    for schedule in schedules:
        if schedule.effective_from > as_of:
            continue
        if chosen is None or schedule.effective_from > chosen.effective_from:
            chosen = schedule
    return chosen.bands if chosen is not None else DEFAULT_APPROVAL_BANDS


def required_tier(amount: Decimal, bands: tuple[ApprovalBand, ...]) -> int:
    """The band index whose authority the amount requires (pure).

    Delegates to ``tenant_config.required_band_index`` (reuse-first):
    inclusive ceilings — 100,000.00 IS Loan-Officer business under the
    defaults; 100,000.01 is not. Returns ``len(bands)`` above a finite
    top band: the explicit "no listed authority" (Board) tier.
    """
    return required_band_index(amount, bands)


def maker_may_proceed(role_name: str, amount: Decimal, bands: tuple[ApprovalBand, ...]) -> bool:
    """Below-band rule: the maker's own authority covers the amount.

    Exactly ``authority_may_ratify`` (reuse-first, including its
    fail-closed floor for roles a configured matrix does not list):
    an operation the maker could themselves ratify proceeds without a
    pending approval; anything above pends for a DIFFERENT principal.
    """
    return authority_may_ratify(role_name, amount, bands)


def checker_may_ratify(
    role_name: str,
    amount: Decimal,
    bands_at_request: tuple[ApprovalBand, ...],
    bands_at_decision: tuple[ApprovalBand, ...],
) -> bool:
    """Stricter-of-the-two rule (ADR-0008): both band sets must allow.

    A pending approval RE-RESOLVES bands at ratification time: the
    checker's authority must satisfy the bands in force when the
    operation was REQUESTED and the bands in force NOW. Tightening
    limits therefore binds in-flight requests immediately; loosening
    them never retroactively weakens an already-pended request.
    """
    return authority_may_ratify(role_name, amount, bands_at_request) and authority_may_ratify(
        role_name, amount, bands_at_decision
    )
