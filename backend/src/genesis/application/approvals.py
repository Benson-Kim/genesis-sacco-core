"""Limits/approval engine service (issue #8 / gap register G2; ADR-0008).

Maker-checker with amount-tiered approval limits, enforced in the
transaction path. UNWIRED BY DESIGN in this slice: no posting path
calls it yet (the wiring follow-up work item routes each
posting-capable operation through ``submit_operation`` after the
in-flight detective-controls MR lands). The module is fully tested
against the real schema so wiring is a call-site change, never an
engine change.

The engine's contract (ADR-0008):

  * every posting-capable operation declares (operation type, amount)
    — the code-owned ``domain/approvals.OperationType`` vocabulary,
    never a free-form caller string;
  * the required approval tier resolves from TENANT-CONFIGURABLE,
    EFFECTIVE-DATED bands (``approval_band_sets``, append-only,
    migration 0049); a tenant with no configured schedule gets the
    code-owned day-one defaults (the prototype matrix exactly);
  * BELOW-BAND PROCEEDS: a maker whose server-side-resolved role
    covers the amount posts single-actor (recorded as created_by, the
    0036 attribution); ABOVE-BAND PENDS: the engine inserts a
    ``pending_approvals`` row and the operation posts NOTHING;
  * a DIFFERENT principal of sufficient authority ratifies — the SoD
    guard is ``application/sod.require_distinct_non_assurance_checker``
    (reuse-first, ONE copy of the rule), and beneath it the DB CHECK
    ck_pending_approvals_sod (0049) makes maker-self-check
    unrepresentable even via direct SQL on the app role;
  * ratification RE-RESOLVES bands: the checker must satisfy BOTH the
    bands in force at request time and the bands in force now
    (stricter-of-the-two, ``domain/approvals.checker_may_ratify``);
  * both principals land on the resulting posting: the wired executor
    records maker as ``transactions.created_by`` (0036) and checker as
    ``transactions.checked_by`` (0049).

Concurrency (the house gates): decisions happen under the pending
row's FOR UPDATE lock; the append-only band-set claim is
INSERT ... ON CONFLICT DO NOTHING checked by rowcount (v1.1 rule 5);
the 0049 write-once trigger enforces the status machine
pending -> ratified | declined at the database. Every read and write
carries an explicit tenant_id predicate on top of forced RLS; every
mutation writes its audit row in-transaction (data integrity); errors
carry no money figures beyond what the audit trail already records
(least disclosure).

Dates are SERVER time only (v1.1 rule 1 applied to time): band
schedule selection uses the database's current date, and
``requested_at`` / ``decided_at`` are written by DEFAULT now() / now()
in SQL — no caller-supplied timestamps anywhere.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.sod import require_distinct_non_assurance_checker
from genesis.domain.approvals import (
    BandSchedule,
    OperationType,
    bands_in_force,
    checker_may_ratify,
    maker_may_proceed,
    required_tier,
)
from genesis.domain.tenant_config import (
    MAX_MONEY,
    ApprovalBand,
    BandConfigError,
    validate_approval_bands,
)
from genesis.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)

__all__ = [
    "ApprovalOutcome",
    "PendingApprovalRecord",
    "band_schedules",
    "bands_as_of",
    "configure_bands",
    "decline_pending",
    "ratify_pending",
    "submit_operation",
]


@dataclass(frozen=True)
class ApprovalOutcome:
    """submit_operation's verdict: proceed now, or wait for a checker.

    ``proceed`` True means the maker's own authority covers the amount
    (below-band): the caller may post, recording the maker as
    created_by. ``proceed`` False means a ``pending_approvals`` row
    with id ``pending_id`` now exists and the caller must post NOTHING
    until a different principal ratifies it. ``required_tier`` is the
    band index the amount resolved to (== len(bands) is the Board
    tier: no listed platform authority).
    """

    proceed: bool
    required_tier: int
    pending_id: uuid.UUID | None


@dataclass(frozen=True)
class PendingApprovalRecord:
    """One pending_approvals row as the engine exposes it."""

    id: uuid.UUID
    operation_type: str
    amount: Decimal
    maker_id: uuid.UUID
    checker_id: uuid.UUID | None
    status: str
    required_tier_at_request: int
    requested_at: datetime
    branch_id: uuid.UUID | None


async def _server_today(session: AsyncSession) -> date:
    """The database's current date — server time only (v1.1 rule 1)."""
    value = (await session.execute(text("SELECT current_date"))).scalar_one()
    return cast("date", value)


async def _resolved_role_name(
    session: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> str:
    """The actor's role NAME, resolved server-side (never the JWT).

    Fails closed (the enforce_authority_band posture): an actor the
    users table cannot vouch for holds no authority at all.
    """
    row = (
        await session.execute(
            text(
                "SELECT r.name FROM users u "
                "JOIN roles r ON r.id = u.role_id AND r.tenant_id = CAST(:tid AS uuid) "
                "WHERE u.id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid)"
            ),
            {"uid": str(actor_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise ForbiddenError(f"actor {actor_id} has no resolvable role for the approval engine")
    return str(row[0])


def _canonical_bands_json(bands: tuple[ApprovalBand, ...]) -> str:
    """Serialize a VALIDATED band tuple back to its canonical JSON."""
    return json.dumps(
        [
            {
                "authority": band.authority,
                "max_amount": str(band.max_amount) if band.max_amount is not None else None,
            }
            for band in bands
        ]
    )


async def configure_bands(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    bands: object,
    effective_from: date,
) -> uuid.UUID:
    """Append one effective-dated band matrix (never edit in place).

    The matrix is validated with the shared write/read contract
    (``validate_approval_bands`` — reuse-first) and claimed atomically
    on (tenant_id, effective_from) with INSERT ... ON CONFLICT DO
    NOTHING + rowcount: an already-claimed effective date is a 409 —
    the append-only discipline means a correction is a NEW, later
    effective date, never a rewrite. The audit row carries the exact
    matrix in-transaction.
    """
    try:
        validated = validate_approval_bands(bands)
    except BandConfigError as exc:
        raise InvalidInputError(str(exc)) from exc
    row_id = uuid.uuid4()
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            text(
                "INSERT INTO approval_band_sets "
                "(id, tenant_id, effective_from, bands, created_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :eff, "
                "CAST(:bands AS jsonb), CAST(:actor AS uuid)) "
                "ON CONFLICT (tenant_id, effective_from) DO NOTHING"
            ),
            {
                "id": str(row_id),
                "tid": str(tenant_id),
                "eff": effective_from,
                "bands": _canonical_bands_json(validated),
                "actor": str(actor_id),
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(
            f"approval bands effective {effective_from.isoformat()} are already "
            "configured (append-only: correct with a later effective date)"
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="approval_bands.configure",
        entity="approval_band_sets",
        entity_id=str(row_id),
        after={
            "effective_from": effective_from.isoformat(),
            "bands": json.loads(_canonical_bands_json(validated)),
        },
    )
    return row_id


async def band_schedules(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[BandSchedule, ...]:
    """Every configured schedule for the tenant, revalidated at READ.

    Corrupted stored bands (possible only via manual SQL, never via
    this module) fail CLOSED for consumers with a 409 — the money path
    never silently skips the authority guard (the tenant_settings
    posture).
    """
    rows = (
        await session.execute(
            text(
                "SELECT effective_from, bands FROM approval_band_sets "
                "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY effective_from"
            ),
            {"tid": str(tenant_id)},
        )
    ).all()
    schedules: list[BandSchedule] = []
    for row in rows:
        try:
            validated = validate_approval_bands(row[1])
        except BandConfigError as exc:
            raise ConflictError(
                "stored approval bands failed read-side revalidation - "
                "failing closed (repair the band schedule)"
            ) from exc
        schedules.append(BandSchedule(effective_from=row[0], bands=validated))
    return tuple(schedules)


async def bands_as_of(
    session: AsyncSession, tenant_id: uuid.UUID, as_of: date
) -> tuple[ApprovalBand, ...]:
    """The band matrix in force at a date (defaults when unconfigured)."""
    return bands_in_force(await band_schedules(session, tenant_id), as_of)


def _validated_amount(amount: Decimal) -> Decimal:
    """The engine's money-domain contract: positive, 2dp, NUMERIC(18,2)."""
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise InvalidInputError("operation amount must be a finite decimal")
    if amount != amount.quantize(Decimal("0.01")):
        raise InvalidInputError("operation amount precision exceeds 2 decimal places")
    if amount <= 0:
        raise InvalidInputError("operation amount must be positive")
    if amount > MAX_MONEY:
        raise InvalidInputError("operation amount outside the NUMERIC(18,2) domain")
    return amount


async def submit_operation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    maker_id: uuid.UUID,
    *,
    operation: OperationType,
    amount: Decimal,
    branch_id: uuid.UUID | None = None,
) -> ApprovalOutcome:
    """Declare a posting-capable operation to the engine (ADR-0008).

    Below-band proceeds: when the maker's server-side-resolved role
    covers the amount under the bands in force TODAY, the outcome is
    proceed=True and the caller posts, recording the maker as
    created_by (0036). Above-band pends: a pending_approvals row is
    inserted (write-once, 0049) and the caller posts NOTHING until a
    different principal ratifies. ``branch_id`` is the operation's
    branch (branch-scoping groundwork: staff carry a home branch and
    cross-branch action is a NAMED permission the wiring MR seeds —
    never a default).
    """
    checked = _validated_amount(amount)
    role_name = await _resolved_role_name(session, tenant_id, maker_id)
    bands = await bands_as_of(session, tenant_id, await _server_today(session))
    tier = required_tier(checked, bands)
    if maker_may_proceed(role_name, checked, bands):
        return ApprovalOutcome(proceed=True, required_tier=tier, pending_id=None)
    pending_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO pending_approvals "
            "(id, tenant_id, operation_type, amount, branch_id, maker_id, "
            "required_tier_at_request) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :op, :amount, "
            "CAST(:branch AS uuid), CAST(:maker AS uuid), :tier)"
        ),
        {
            "id": str(pending_id),
            "tid": str(tenant_id),
            "op": operation.value,
            "amount": checked,
            "branch": str(branch_id) if branch_id is not None else None,
            "maker": str(maker_id),
            "tier": tier,
        },
    )
    await record_audit(
        session,
        tenant_id,
        maker_id,
        action="approval.request",
        entity="pending_approvals",
        entity_id=str(pending_id),
        after={
            "operation_type": operation.value,
            "amount": str(checked),
            "required_tier_at_request": tier,
            "branch_id": str(branch_id) if branch_id is not None else None,
        },
    )
    return ApprovalOutcome(proceed=False, required_tier=tier, pending_id=pending_id)


async def _locked_pending(
    session: AsyncSession, tenant_id: uuid.UUID, pending_id: uuid.UUID
) -> Any:
    """The pending row under FOR UPDATE (decisions serialize on it)."""
    row = (
        await session.execute(
            text(
                "SELECT id, operation_type, amount, maker_id, status, "
                "requested_at, branch_id, required_tier_at_request "
                "FROM pending_approvals "
                "WHERE tenant_id = CAST(:tid AS uuid) AND id = CAST(:pid AS uuid) "
                "FOR UPDATE"
            ),
            {"tid": str(tenant_id), "pid": str(pending_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"pending approval {pending_id} not found")
    return row


async def _checker_bands_verdict(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    checker_id: uuid.UUID,
    *,
    maker_id: uuid.UUID,
    amount: Decimal,
    requested_at: datetime,
) -> str:
    """Shared checker gate: SoD, then the stricter-of-the-two bands.

    Reuses application/sod.require_distinct_non_assurance_checker (ONE
    copy of the maker<>checker + assurance-exclusion rule) and then
    requires the checker's server-side-resolved role to satisfy BOTH
    the bands in force at the REQUEST date and the bands in force NOW
    (ADR-0008: tightening binds in-flight requests; loosening never
    retroactively weakens one). Returns the checker's role name for
    the audit payload.
    """
    await require_distinct_non_assurance_checker(
        session,
        tenant_id,
        checker_id,
        maker_id,
        subject="a pending approval",
        subject_plural="pending approvals",
    )
    role_name = await _resolved_role_name(session, tenant_id, checker_id)
    schedules = await band_schedules(session, tenant_id)
    bands_at_request = bands_in_force(schedules, requested_at.date())
    bands_now = bands_in_force(schedules, await _server_today(session))
    if not checker_may_ratify(role_name, amount, bands_at_request, bands_now):
        raise ForbiddenError(
            f"role '{role_name}' may not ratify this amount under the approval "
            "matrix (stricter of request-time and current bands applies)"
        )
    return role_name


async def _decide_pending(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    checker_id: uuid.UUID,
    pending_id: uuid.UUID,
    *,
    status: str,
    reason: str | None,
) -> PendingApprovalRecord:
    """Move a locked pending row to a terminal status (exactly once)."""
    row = await _locked_pending(session, tenant_id, pending_id)
    if str(row[4]) != "pending":
        raise ConflictError(f"pending approval {pending_id} is already decided")
    amount = Decimal(str(row[2]))
    maker_id = uuid.UUID(str(row[3]))
    role_name = await _checker_bands_verdict(
        session,
        tenant_id,
        checker_id,
        maker_id=maker_id,
        amount=amount,
        requested_at=row[5],
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            text(
                "UPDATE pending_approvals "
                "SET status = :status, checker_id = CAST(:checker AS uuid), "
                "decided_at = now(), decision_reason = :reason "
                "WHERE tenant_id = CAST(:tid AS uuid) AND id = CAST(:pid AS uuid) "
                "AND status = 'pending'"
            ),
            {
                "status": status,
                "checker": str(checker_id),
                "reason": reason,
                "tid": str(tenant_id),
                "pid": str(pending_id),
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"pending approval {pending_id} is already decided")
    await record_audit(
        session,
        tenant_id,
        checker_id,
        action=f"approval.{'ratify' if status == 'ratified' else 'decline'}",
        entity="pending_approvals",
        entity_id=str(pending_id),
        after={
            "operation_type": str(row[1]),
            "amount": str(amount),
            "maker_id": str(maker_id),
            "checker_id": str(checker_id),
            "checker_role": role_name,
            "status": status,
            "reason": reason,
        },
    )
    decided = (
        await session.execute(
            text(
                "SELECT id, operation_type, amount, maker_id, checker_id, status, "
                "required_tier_at_request, requested_at, branch_id "
                "FROM pending_approvals "
                "WHERE tenant_id = CAST(:tid AS uuid) AND id = CAST(:pid AS uuid)"
            ),
            {"tid": str(tenant_id), "pid": str(pending_id)},
        )
    ).one()
    return PendingApprovalRecord(
        id=uuid.UUID(str(decided[0])),
        operation_type=str(decided[1]),
        amount=Decimal(str(decided[2])),
        maker_id=uuid.UUID(str(decided[3])),
        checker_id=uuid.UUID(str(decided[4])) if decided[4] is not None else None,
        status=str(decided[5]),
        required_tier_at_request=int(decided[6]),
        requested_at=decided[7],
        branch_id=uuid.UUID(str(decided[8])) if decided[8] is not None else None,
    )


async def ratify_pending(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    checker_id: uuid.UUID,
    pending_id: uuid.UUID,
) -> PendingApprovalRecord:
    """Ratify a pending approval as a DIFFERENT principal (ADR-0008).

    The wired executor then posts the operation recording BOTH
    principals: maker as transactions.created_by (0036), checker as
    transactions.checked_by (0049).
    """
    return await _decide_pending(
        session, tenant_id, checker_id, pending_id, status="ratified", reason=None
    )


async def decline_pending(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    checker_id: uuid.UUID,
    pending_id: uuid.UUID,
    *,
    reason: str,
) -> PendingApprovalRecord:
    """Decline a pending approval with a REQUIRED checker rationale.

    Declining is a checker act under the same SoD and
    stricter-of-the-two authority gate as ratifying (a decision either
    way is an exercise of the band authority). The rationale rides the
    audit `after` payload (the !52 F2 precedent).
    """
    cleaned = reason.strip()
    if not cleaned or len(cleaned) > 2000:
        raise InvalidInputError("a decline requires a checker rationale (1-2000 characters)")
    return await _decide_pending(
        session, tenant_id, checker_id, pending_id, status="declined", reason=cleaned
    )
