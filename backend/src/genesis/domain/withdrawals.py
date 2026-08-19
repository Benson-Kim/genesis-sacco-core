"""Withdrawal business controls: velocity-cap and notice-threshold logic (issue #2).

Pure functions only — no I/O, no imports from other layers (the house
doctrine 2.1). Two code-owned pieces live here:

  * the notice-hold status machine (the lending.transition convention:
    a single gatekeeper with an explicit allowed map — any transition
    outside it raises);
  * the control-evaluation function the withdrawal service calls under
    the deposit-account row lock. Keeping the decision pure makes the
    boundary cases (exactly at the cap, exactly at the threshold)
    unit-testable without a database, while ATOMICITY stays the
    service's job: the inputs are read and the decision applied inside
    one transaction holding the account lock, so there is no TOCTOU
    window between the cap check and the posting.

Decision precedence (documented and pinned by tests): the DAILY CAP is
evaluated BEFORE the notice threshold. A request that would breach the
cap is refused outright — it never parks in a notice hold that would
later fail anyway, and a hold can never be used to reserve headroom.

Boundary semantics (cap-boundary exactness, falsifiable in
tests/test_withdrawal_controls_domain.py):
  * cap: a withdrawal is allowed while day_total + amount <= cap; the
    request that lands EXACTLY on the cap succeeds, one cent more is
    refused.
  * threshold: amounts up to AND INCLUDING the threshold execute
    immediately; only amounts STRICTLY ABOVE it require notice.

Residual risk, stated honestly: the notice threshold gates a SINGLE
withdrawal's size. Splitting one large withdrawal into several
below-threshold requests bypasses the notice state BY DESIGN of a
per-transaction threshold; the daily velocity cap is the backstop that
bounds the total a splitter can move per day. Tenants that want notice
on aggregate daily volume should set daily_withdrawal_limit at (or
below) the level where they would want notice.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal


class WithdrawalHoldStatus(enum.StrEnum):
    PENDING_NOTICE = "pending_notice"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


class InvalidHoldTransitionError(Exception):
    """Raised on any hold transition not in the allowed map (gate 1.4)."""


_ALLOWED: dict[WithdrawalHoldStatus, frozenset[WithdrawalHoldStatus]] = {
    WithdrawalHoldStatus.PENDING_NOTICE: frozenset(
        {WithdrawalHoldStatus.EXECUTED, WithdrawalHoldStatus.CANCELLED}
    ),
    WithdrawalHoldStatus.EXECUTED: frozenset(),
    WithdrawalHoldStatus.CANCELLED: frozenset(),
}


def allowed_hold_transitions(
    current: WithdrawalHoldStatus,
) -> frozenset[WithdrawalHoldStatus]:
    return _ALLOWED[current]


def hold_transition(
    current: WithdrawalHoldStatus, target: WithdrawalHoldStatus
) -> WithdrawalHoldStatus:
    """The single gatekeeper for hold status changes."""
    if target not in _ALLOWED[current]:
        raise InvalidHoldTransitionError(f"{current.value} -> {target.value}")
    return target


class WithdrawalDecision(enum.StrEnum):
    """Outcome of the control evaluation, in precedence order."""

    #: The request breaches the daily velocity cap: refuse (409),
    #: audit the refusal in-transaction, post nothing.
    REFUSE_OVER_CAP = "refuse_over_cap"
    #: The request exceeds the notice threshold: park it in a
    #: pending_notice hold with an outbox notification event.
    HOLD_FOR_NOTICE = "hold_for_notice"
    #: Execute immediately through the normal posting chain.
    EXECUTE = "execute"


@dataclass(frozen=True)
class WithdrawalControls:
    """Tenant configuration snapshot, read under the account row lock.

    ``None`` means "not configured" — that control is disabled (the
    0017 fallback convention).
    """

    daily_limit: Decimal | None
    notice_threshold: Decimal | None


def evaluate_withdrawal(
    *,
    amount: Decimal,
    day_total: Decimal,
    controls: WithdrawalControls,
    bypass_notice: bool = False,
) -> WithdrawalDecision:
    """Decide one withdrawal attempt against the tenant's controls.

    ``day_total`` is the member's already-POSTED withdrawal total for
    the current UTC day (reversals excluded — a reversed withdrawal
    does NOT restore same-day headroom; conservative fraud-brake,
    documented in the service). ``bypass_notice`` is set ONLY by the
    hold-execution path: an approved hold must not re-enter the notice
    state, while the daily cap is ALWAYS re-checked at execution time
    (a hold can never be used to smuggle headroom past the cap).
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    if day_total < 0:
        raise ValueError("day_total must not be negative")
    if controls.daily_limit is not None and day_total + amount > controls.daily_limit:
        return WithdrawalDecision.REFUSE_OVER_CAP
    if (
        not bypass_notice
        and controls.notice_threshold is not None
        and amount > controls.notice_threshold
    ):
        return WithdrawalDecision.HOLD_FOR_NOTICE
    return WithdrawalDecision.EXECUTE
