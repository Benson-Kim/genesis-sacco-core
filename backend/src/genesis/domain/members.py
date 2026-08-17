"""Members domain: types, status machine, numbering format (P8, P13.13).

Zero I/O. Status transitions mirror the prototype: Active<->Arrears and
Active/Arrears->Exited; Exited is terminal. Illegal transitions raise
(gate 1.4). Member numbers use the GP-XXXX format; the race-safe
allocation lives in the application layer.

P13.13 adds the dormancy lifecycle:

  * Active -> Dormant  — the nightly dormancy job only, when no
    member-initiated transaction fell inside the tenant-configured
    window (application/dormancy.py);
  * Dormant -> Active  — automatic, inside the deposit transaction
    (application/members.reactivate_dormant_member);
  * Dormant -> Exited  — the real P12 settlement workflow only.

MEMBER_OPERATIONS is the code-owned status-capability map behind every
money-movement guard (the single gatekeeper, gate 1.4 / P13.13 FM2):
which member statuses may perform which money operation is decided
HERE, never by literals scattered across routes. Dormant members may
move money IN (deposit, loan repayment) and request an exit; every
outbound/credit operation (withdraw, borrow, pledge, share top-up,
share transfer either side) requires a strictly ACTIVE member. Tests
pin the exact allow-sets, so widening any of them is a failing diff
(falsifiability, MASTER_PROMPT section 4).
"""

from __future__ import annotations

import calendar
import datetime
import enum

MEMBER_NO_PREFIX = "GP-"


class MemberType(enum.StrEnum):
    PERSON = "person"
    COMPANY = "company"
    GROUP = "group"
    VEHICLE = "vehicle"


class MemberStatus(enum.StrEnum):
    ACTIVE = "active"
    ARREARS = "arrears"
    DORMANT = "dormant"
    EXITED = "exited"


class InvalidStatusTransitionError(Exception):
    """Raised when a member status transition is not allowed (gate 1.4)."""


_ALLOWED: dict[MemberStatus, frozenset[MemberStatus]] = {
    MemberStatus.ACTIVE: frozenset(
        {MemberStatus.ARREARS, MemberStatus.DORMANT, MemberStatus.EXITED}
    ),
    MemberStatus.ARREARS: frozenset({MemberStatus.ACTIVE, MemberStatus.EXITED}),
    # Dormant -> Active only via a deposit; Dormant -> Exited only via
    # the P12 workflow. Dormant -> Arrears is illegal by design: a
    # dormant member cannot originate new credit, and arrears status is
    # an active-relationship state.
    MemberStatus.DORMANT: frozenset({MemberStatus.ACTIVE, MemberStatus.EXITED}),
    MemberStatus.EXITED: frozenset(),
}


def transition(current: MemberStatus, target: MemberStatus) -> MemberStatus:
    """Validate a status transition; raise on any illegal move (gate 1.4)."""
    if target not in _ALLOWED[current]:
        raise InvalidStatusTransitionError(f"cannot move member from {current} to {target}")
    return target


class MoneyOperation(enum.StrEnum):
    """Member-facing money operations gated by member status (P13.13)."""

    DEPOSIT = "deposit"
    LOAN_REPAYMENT = "loan repayment"
    SHARE_TOPUP = "share top-up"
    WITHDRAWAL = "withdrawal"
    BORROW = "loan application"
    PLEDGE = "guarantee pledge"
    SHARE_TRANSFER_OUT = "share transfer out"
    SHARE_TRANSFER_IN = "share transfer in"
    EXIT_REQUEST = "exit request"
    # P13.15: staff-charged misc fee (money IN — the member pays it).
    FEE = "fee posting"


#: Code-owned capability map (P13.13 FM2 — the single gatekeeper):
#: the ONLY source of truth for which member statuses may perform each
#: money operation. Exited members appear in NO set (terminal). Money
#: IN (deposit, repayment) is allowed for arrears members (reduces
#: risk, the P11 rule) and for dormant members (a deposit is also the
#: reactivation trigger). Everything that moves money OUT or
#: originates credit/collateral — withdrawal, borrowing, pledging,
#: share top-up, share transfer on either side (!30's strictly-active
#: rule) — requires a strictly ACTIVE member, so 'dormant' is refused
#: by construction (the !29 R2 allow-list precedent). Exit requests
#: are open to active, arrears AND dormant members: Dormant -> Exited
#: runs through the real P12 workflow.
_ACTIVE_ONLY = frozenset({MemberStatus.ACTIVE})
_MONEY_IN = frozenset({MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.DORMANT})

MEMBER_OPERATIONS: dict[MoneyOperation, frozenset[MemberStatus]] = {
    MoneyOperation.DEPOSIT: _MONEY_IN,
    MoneyOperation.LOAN_REPAYMENT: _MONEY_IN,
    # P13.15: a fee is money IN (the member settles it) — same statuses
    # as deposit/repayment; exited members are refused by construction.
    MoneyOperation.FEE: _MONEY_IN,
    MoneyOperation.SHARE_TOPUP: frozenset({MemberStatus.ACTIVE, MemberStatus.ARREARS}),
    MoneyOperation.WITHDRAWAL: _ACTIVE_ONLY,
    MoneyOperation.BORROW: _ACTIVE_ONLY,
    MoneyOperation.PLEDGE: _ACTIVE_ONLY,
    MoneyOperation.SHARE_TRANSFER_OUT: _ACTIVE_ONLY,
    MoneyOperation.SHARE_TRANSFER_IN: _ACTIVE_ONLY,
    MoneyOperation.EXIT_REQUEST: frozenset(
        {MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.DORMANT}
    ),
}


def member_may(status: MemberStatus, operation: MoneyOperation) -> bool:
    """Whether a member in ``status`` may perform ``operation``.

    Allow-list, never deny-list: a status outside the map's set —
    including any FUTURE status — is refused until this code-owned map
    is deliberately widened (the member_exits eligibility precedent).
    """
    return status in MEMBER_OPERATIONS[operation]


def dormancy_cutoff(as_of: datetime.date, period_months: int) -> datetime.date:
    """First day of the dormancy window ending at ``as_of`` (P13.13).

    Calendar-month subtraction with end-of-month clamping: the day is
    clamped to the target month's length (2026-03-31 minus 1 month is
    2026-02-28; 2024-03-31 minus 1 month is 2024-02-29). Pure calendar
    arithmetic on UTC dates — no timezone math (the P13.8 day-count
    convention). Member-initiated activity ON or AFTER this date keeps
    the member active; the hand-computed boundary oracles live in
    tests/test_dormancy.py.
    """
    if period_months < 1:
        raise ValueError("dormancy period must be at least one month")
    total = as_of.year * 12 + (as_of.month - 1) - period_months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(as_of.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def format_member_no(seq: int) -> str:
    """GP-XXXX member number; grows past four digits without collision."""
    if seq <= 0:
        raise ValueError("member number sequence must be positive")
    return f"{MEMBER_NO_PREFIX}{seq:04d}"
