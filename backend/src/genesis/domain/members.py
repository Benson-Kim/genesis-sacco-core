"""Members domain: types, status machine, numbering format (P8).

Zero I/O. Status transitions mirror the prototype: Active<->Arrears and
Active/Arrears->Exited; Exited is terminal. Illegal transitions raise
(concurrency safety). Member numbers use the GP-XXXX format; the race-safe
allocation lives in the application layer.

 adds the dormancy lifecycle:

  * Active -> Dormant — the nightly dormancy job only, when no
    member-initiated transaction fell inside the tenant-configured
    window (application/dormancy.py);
  * Dormant -> Active — automatic, inside the deposit transaction
    (application/members.reactivate_dormant_member);
  * Dormant -> Exited — the real settlement workflow only.

MEMBER_OPERATIONS is the code-owned status-capability map behind every
money-movement guard (the single gatekeeper, concurrency safety /):
which member statuses may perform which money operation is decided
HERE, never by literals scattered across routes. Dormant members may
move money IN (deposit, loan repayment) and request an exit; every
outbound/credit operation (withdraw, borrow, pledge, share top-up,
share transfer either side) requires a strictly ACTIVE member. Tests
pin the exact allow-sets, so widening any of them is a failing diff
(falsifiability, the house doctrine section 4).
"""

from __future__ import annotations

import calendar
import datetime
import enum
import re

MEMBER_NO_PREFIX = "GP-"

#: Kenya mobile identifiers (item 1). Accepted INPUT formats are
#: exactly the maintainer-declared four: 07XXXXXXXX / 01XXXXXXXX and
#: their E.164 twins +2547XXXXXXXX / +2541XXXXXXXX. Storage is ALWAYS
#: E.164 (+254…) — the canonical form every downstream system accepts.
_KENYA_MSISDN_LOCAL = re.compile(r"^0([17])(\d{8})$")
_KENYA_MSISDN_E164 = re.compile(r"^\+254([17])\d{8}$")


def normalize_kenya_msisdn(raw: str) -> str | None:
    """Normalize a Kenya mobile number to E.164, or None when invalid.

    Pure and total: surrounding whitespace is tolerated (a courtesy for
    hand-typed input), everything else must match one of the four
    accepted formats EXACTLY — no digit-harvesting from free text, so
    a typo can never silently become a different number. Every accepted
    spelling of the same number maps to the SAME E.164 string
    (falsifiable oracles in tests/test_members_domain.py). The caller
    decides how a None surfaces (the API layer raises a sanitized 422 that never echoes the input —
    least disclosure).
    """
    value = raw.strip()
    if _KENYA_MSISDN_E164.match(value):
        return value
    match = _KENYA_MSISDN_LOCAL.match(value)
    if match:
        return f"+254{match.group(1)}{match.group(2)}"
    return None


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


class DividendPayout(enum.StrEnum):
    """Member dividend payout PREFERENCE vocabulary.

    CODE-OWNED, server-side — never caller-invented. Provenance: the
    prototype's membership step declares the FIELD ("Dividend payout",
    genesis_prestige_app.html vAdd()) but enumerates no option list
    anywhere, so the vocabulary is assembled from the prototype's own
    money-movement vocabulary: its two cash channels (M-Pesa / Bank —
    the tx-channel and disbursement-channel selects, mirrored by the
    as-built ledger Channel cash set) plus the two retention
    destinations the prototype/as-built domain already carries
    (deposit-account credit — the as-built distribution leg; share top-up — the prototype's SH-
    affordance).

    CONSUMED since (the preference-only fence was retired DELIBERATELY under the maintainer
    authorization recorded on, 2026-08-07): the distribution engine routes the
    member-side credit legs of the payout posting per the code-owned
    application.dividends.PAYOUT_CREDIT_ROUTING map. The retention
    destinations are implementable and route; the external cash
    channels are NOT implementable today (M-Pesa integration is, unbuilt) and fall back honestly to
    the as-built default path with
    the token recorded verbatim in the distribution audit row. NULL
    keeps the as-built behaviour byte-for-byte. The routing map is
    pinned set-equal to this enum falsifiably (a future token forces a
    deliberate routing decision — see PAYOUT_FALLBACK_TOKENS).
    """

    DEPOSIT_ACCOUNT = "deposit_account"
    SHARE_CAPITAL = "share_capital"
    MPESA = "mpesa"
    BANK = "bank"


class InvalidStatusTransitionError(Exception):
    """Raised when a member status transition is not allowed (concurrency safety)."""


_ALLOWED: dict[MemberStatus, frozenset[MemberStatus]] = {
    MemberStatus.ACTIVE: frozenset(
        {MemberStatus.ARREARS, MemberStatus.DORMANT, MemberStatus.EXITED}
    ),
    MemberStatus.ARREARS: frozenset({MemberStatus.ACTIVE, MemberStatus.EXITED}),
    # Dormant -> Active only via a deposit; Dormant -> Exited only via
    # the workflow. Dormant -> Arrears is illegal by design: a
    # dormant member cannot originate new credit, and arrears status is
    # an active-relationship state.
    MemberStatus.DORMANT: frozenset({MemberStatus.ACTIVE, MemberStatus.EXITED}),
    MemberStatus.EXITED: frozenset(),
}


def transition(current: MemberStatus, target: MemberStatus) -> MemberStatus:
    """Validate a status transition; raise on any illegal move (concurrency safety)."""
    if target not in _ALLOWED[current]:
        raise InvalidStatusTransitionError(f"cannot move member from {current} to {target}")
    return target


class MoneyOperation(enum.StrEnum):
    """Member-facing money operations gated by member status."""

    DEPOSIT = "deposit"
    LOAN_REPAYMENT = "loan repayment"
    SHARE_TOPUP = "share top-up"
    WITHDRAWAL = "withdrawal"
    BORROW = "loan application"
    PLEDGE = "guarantee pledge"
    SHARE_TRANSFER_OUT = "share transfer out"
    SHARE_TRANSFER_IN = "share transfer in"
    EXIT_REQUEST = "exit request"
    # staff-charged misc fee (money IN — the member pays it).
    FEE = "fee posting"
    # Issue: bad-debt recovery receipt against the member's
    # surviving written-off claim (money IN).
    RECOVERY = "recovery receipt"


#: Code-owned capability map (the single gatekeeper):
#: the ONLY source of truth for which member statuses may perform each
#: money operation. Exited members appear in NO set (terminal). Money
#: IN (deposit, repayment) is allowed for arrears members (reduces
#: risk, the rule) and for dormant members (a deposit is also the
#: reactivation trigger). Everything that moves money OUT or
#: originates credit/collateral — withdrawal, borrowing, pledging,
#: share top-up, share transfer on either side (strictly-active
#: rule) — requires a strictly ACTIVE member, so 'dormant' is refused
#: by construction (the R2 allow-list precedent). Exit requests
#: are open to active, arrears AND dormant members: Dormant -> Exited
#: runs through the real workflow.
_ACTIVE_ONLY = frozenset({MemberStatus.ACTIVE})
_MONEY_IN = frozenset({MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.DORMANT})

MEMBER_OPERATIONS: dict[MoneyOperation, frozenset[MemberStatus]] = {
    MoneyOperation.DEPOSIT: _MONEY_IN,
    MoneyOperation.LOAN_REPAYMENT: _MONEY_IN,
    # a fee is money IN (the member settles it) — same statuses
    # as deposit/repayment; exited members are refused by construction.
    MoneyOperation.FEE: _MONEY_IN,
    # Issue: a recovery receipt is money IN against the surviving
    # written-off claim — active, arrears and dormant members may
    # settle it; EXITED members are refused by construction (and the
    # issue- exit guard makes exiting with an unresolved claim
    # impossible going forward, so this refusal is defence in depth).
    MoneyOperation.RECOVERY: _MONEY_IN,
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
    """First day of the dormancy window ending at ``as_of``.

    Calendar-month subtraction with end-of-month clamping: the day is
    clamped to the target month's length (2026-03-31 minus 1 month is
    2026-02-28; 2024-03-31 minus 1 month is 2024-02-29). Pure calendar
    arithmetic on UTC dates — no timezone math (the day-count convention). Member-initiated activity
    ON or AFTER this date keeps
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
