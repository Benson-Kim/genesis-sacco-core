"""Guarantorship services: pledge, consent, release, substitution (P9/P13.14).

Capacity (deposit balance minus existing pledged/active guarantees) is
computed while holding the guarantor's deposit-account row lock, so
concurrent pledges - and future balance-changing operations that take
the same lock - can never over-pledge a member.

P13.14 adds the per-guarantee release/substitution the prototype's
Guarantors screen exposes. Lock order (the established P9 pledge
chain — no new lock-graph edges): application/loan row -> guarantor
member FOR SHARE -> guarantor deposit account FOR UPDATE. The member
row in this chain is always the GUARANTOR's, so it can never form a
cycle with the P12 settlement chain (member -> accounts -> loans),
which locks the EXITING member's own rows: a path holding a borrower's
application/loan never waits on locks a guarantor's settlement holds
while that settlement waits on ours — the guarantor's own loans are
disjoint from the borrower's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import rbac as rbac_service
from genesis.application.audit import record_audit
from genesis.application.loan_applications import (
    application_max_eligible,
    get_application,
    recompute_cover,
)
from genesis.application.outbox import enqueue_event
from genesis.domain.lending import ApplicationStage
from genesis.domain.money import ZERO, to_cents
from genesis.domain.rbac import Action, Module
from genesis.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    UnprocessableError,
)

#: Stages during which new pledges are accepted.
_PLEDGEABLE = frozenset(
    {ApplicationStage.SUBMITTED, ApplicationStage.APPRAISAL, ApplicationStage.COMMITTEE}
)

#: Undisbursed stages whose product cover rule guards an active-guarantee
#: release (P13.14): while the application is in flight, releasing
#: consented collateral must never drop remaining cover below the P7
#: gate. REJECTED is terminal (its pledges are bulk-released already);
#: DISBURSED guarantees carry loan_id and can only be substituted.
_COVER_GUARDED_STAGES = frozenset(
    {
        ApplicationStage.SUBMITTED,
        ApplicationStage.APPRAISAL,
        ApplicationStage.COMMITTEE,
        ApplicationStage.APPROVED,
    }
)


@dataclass(frozen=True)
class GuaranteeRecord:
    id: uuid.UUID
    application_id: uuid.UUID | None
    loan_id: uuid.UUID | None
    guarantor_member_id: uuid.UUID
    borrower_member_id: uuid.UUID
    amount: Decimal
    status: str
    version: int


async def live_pledged_total(
    session: AsyncSession, tenant_id: uuid.UUID, guarantor_member_id: uuid.UUID
) -> Decimal:
    """Sum of a member's live (pledged/active) guarantee amounts.

    Callers must hold the guarantor's deposit-account row lock whenever
    the result feeds a capacity decision (pledging or withdrawing), so
    the computation can never interleave with a concurrent balance
    change (gate 1.4). Shared by P9 pledging and P11 withdrawals. The
    explicit tenant predicate doubles the RLS fence on this money path
    (defence in depth, gate 1.6).
    """
    value = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                "WHERE guarantor_member_id = CAST(:g AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status IN ('pledged', 'active')"
            ),
            {"g": str(guarantor_member_id), "tid": str(tenant_id)},
        )
    ).scalar_one()
    return Decimal(str(value))


async def _guarantor_available_capacity(
    session: AsyncSession, tenant_id: uuid.UUID, guarantor_member_id: uuid.UUID
) -> Decimal:
    """Available pledge capacity under the P9 lock chain (gates 1.1, 1.4).

    Takes the guarantor member row FOR SHARE (holds off a concurrent
    terminal exit until the pledge commits) and the guarantor's
    deposit-account row FOR UPDATE (the serialisation point every
    capacity computation shares with P11 withdrawals), then returns
    balance minus live pledges. The single implementation behind P9
    pledging and P13.14 substitution — a second copy of this math is a
    rejected MR (gate 1.1). Callers compare and raise their own
    least-disclosure error; the capacity itself is never echoed.
    """
    guarantor_row = (
        await session.execute(
            # FOR SHARE holds off a concurrent terminal member exit
            # (which locks the row FOR UPDATE) until this pledge commits,
            # closing the TOCTOU window between the status check and the
            # insert (gate 1.4).
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR SHARE"
            ),
            {"m": str(guarantor_member_id), "tid": str(tenant_id)},
        )
    ).first()
    if guarantor_row is None:
        raise NotFoundError(f"guarantor member {guarantor_member_id} not found")
    if str(guarantor_row[0]) != "active":
        raise ConflictError(
            f"guarantor {guarantor_member_id} is '{guarantor_row[0]}': "
            "only active members may pledge"
        )
    # Serialisation point: every capacity computation for this guarantor
    # happens while holding this row lock.
    balance_row = (
        await session.execute(
            text(
                "SELECT balance FROM deposit_accounts WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(guarantor_member_id), "tid": str(tenant_id)},
        )
    ).first()
    if balance_row is None:
        raise NotFoundError(f"guarantor {guarantor_member_id} has no deposit account")
    balance = Decimal(str(balance_row[0]))
    pledged = await live_pledged_total(session, tenant_id, guarantor_member_id)
    return balance - pledged


async def pledge_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    application_id: uuid.UUID,
    guarantor_member_id: uuid.UUID,
    amount: Decimal,
) -> GuaranteeRecord:
    """Pledge under the guarantor's deposit-account row lock (gate 1.4)."""
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("guarantee amount must be positive")
    app_row = (
        await session.execute(
            text(
                "SELECT member_id, stage FROM loan_applications "
                "WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if app_row is None:
        raise NotFoundError(f"loan application {application_id} not found")
    borrower_id = uuid.UUID(str(app_row[0]))
    stage = ApplicationStage(str(app_row[1]))
    if stage not in _PLEDGEABLE:
        raise ConflictError(f"application in stage '{stage.value}' no longer accepts pledges")
    if guarantor_member_id == borrower_id:
        raise InvalidInputError("a member cannot guarantee their own loan")
    available = await _guarantor_available_capacity(session, tenant_id, guarantor_member_id)
    if amount > available:
        # Least disclosure (gate 1.6): the available capacity derives from
        # the guarantor's deposit balance and is never echoed to callers.
        raise ConflictError(
            f"insufficient guarantor capacity: requested {amount} exceeds available capacity"
        )
    guarantee_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO guarantees "
            "(id, tenant_id, guarantor_member_id, borrower_member_id, "
            " application_id, amount) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
            "CAST(:b AS uuid), CAST(:a AS uuid), :amount)"
        ),
        {
            "id": str(guarantee_id),
            "tid": str(tenant_id),
            "g": str(guarantor_member_id),
            "b": str(borrower_id),
            "a": str(application_id),
            "amount": str(amount),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.pledge",
        entity="guarantees",
        entity_id=str(guarantee_id),
        after={
            "application_id": str(application_id),
            "guarantor_member_id": str(guarantor_member_id),
            "amount": str(amount),
            "status": "pledged",
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="guarantee.pledged",
        payload={
            "guarantee_id": str(guarantee_id),
            "application_id": str(application_id),
            "guarantor_member_id": str(guarantor_member_id),
            "amount": str(amount),
        },
    )
    await recompute_cover(session, tenant_id, application_id)
    return GuaranteeRecord(
        id=guarantee_id,
        application_id=application_id,
        loan_id=None,
        guarantor_member_id=guarantor_member_id,
        borrower_member_id=borrower_id,
        amount=amount,
        status="pledged",
        version=1,
    )


async def consent_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    guarantee_id: uuid.UUID,
    *,
    version: int,
) -> GuaranteeRecord:
    """Record guarantor consent: pledged -> active (gates 1.4, 1.5)."""
    row = (
        await session.execute(
            text(
                "SELECT application_id, guarantor_member_id, borrower_member_id, "
                "amount, status, version, loan_id FROM guarantees "
                "WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(guarantee_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    status = str(row[4])
    if status != "pledged":
        raise ConflictError(f"only pledged guarantees can be consented, not '{status}'")
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6).
                "UPDATE guarantees SET status = 'active', "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {"id": str(guarantee_id), "tid": str(tenant_id), "ver": version},
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for guarantee {guarantee_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.consent",
        entity="guarantees",
        entity_id=str(guarantee_id),
        before={"status": "pledged"},
        after={"status": "active"},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="guarantee.consented",
        payload={"guarantee_id": str(guarantee_id)},
    )
    return GuaranteeRecord(
        id=guarantee_id,
        application_id=uuid.UUID(str(row[0])) if row[0] is not None else None,
        loan_id=uuid.UUID(str(row[6])) if row[6] is not None else None,
        guarantor_member_id=uuid.UUID(str(row[1])),
        borrower_member_id=uuid.UUID(str(row[2])),
        amount=Decimal(str(row[3])),
        status="active",
        version=int(row[5]) + 1,
    )


async def release_guarantees_for_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    loan_id: uuid.UUID,
) -> int:
    """Release every live guarantee behind a loan (P10 closure hook)."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 — finding 15).
                "UPDATE guarantees SET status = 'released', "
                "version = version + 1, updated_at = now() "
                "WHERE loan_id = CAST(:lid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status IN ('pledged', 'active')"
            ),
            {"lid": str(loan_id), "tid": str(tenant_id)},
        ),
    )
    released = int(result.rowcount or 0)
    if released:
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="guarantee.release",
            entity="guarantees",
            entity_id=str(loan_id),
            after={"loan_id": str(loan_id), "released": released},
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="guarantee.released",
            payload={"loan_id": str(loan_id), "released": released},
        )
    return released


# ---------------------------------------------------------------------------
# P13.14 — per-guarantee release & substitution
# ---------------------------------------------------------------------------

_G_COLS = (
    "id, application_id, loan_id, guarantor_member_id, borrower_member_id, amount, status, version"
)


@dataclass(frozen=True)
class _GuaranteeRow:
    id: uuid.UUID
    application_id: uuid.UUID | None
    loan_id: uuid.UUID | None
    guarantor_member_id: uuid.UUID
    borrower_member_id: uuid.UUID
    amount: Decimal
    status: str
    version: int


def _to_guarantee_row(row: Any) -> _GuaranteeRow:
    return _GuaranteeRow(
        id=uuid.UUID(str(row[0])),
        application_id=uuid.UUID(str(row[1])) if row[1] is not None else None,
        loan_id=uuid.UUID(str(row[2])) if row[2] is not None else None,
        guarantor_member_id=uuid.UUID(str(row[3])),
        borrower_member_id=uuid.UUID(str(row[4])),
        amount=Decimal(str(row[5])),
        status=str(row[6]),
        version=int(row[7]),
    )


async def _read_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    for_update: bool,
) -> _GuaranteeRow | None:
    """Guarantee row with an explicit tenant predicate on top of RLS.

    The lock suffix is a code-owned literal chosen by the boolean, never
    caller input (v1.1 rule 6).
    """
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        await session.execute(
            text(
                f"SELECT {_G_COLS} FROM guarantees "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                f"{suffix}"
            ),
            {"id": str(guarantee_id), "tid": str(tenant_id)},
        )
    ).first()
    return _to_guarantee_row(row) if row is not None else None


async def _lock_release_anchor(
    session: AsyncSession, tenant_id: uuid.UUID, g: _GuaranteeRow
) -> ApplicationStage | None:
    """Application (then loan) row FOR UPDATE — the P13.14 lock anchor.

    Application before loan matches the only existing path that locks
    both (P7 disbursement); no path locks a loan before its application,
    so no cycle is possible. Explicit tenant predicates on both reads
    (gate 1.6 v1.1).
    """
    stage: ApplicationStage | None = None
    if g.application_id is not None:
        row = (
            await session.execute(
                text(
                    "SELECT stage FROM loan_applications "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
                ),
                {"id": str(g.application_id), "tid": str(tenant_id)},
            )
        ).first()
        if row is not None:
            stage = ApplicationStage(str(row[0]))
    if g.loan_id is not None:
        await session.execute(
            text(
                "SELECT id FROM loans "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(g.loan_id), "tid": str(tenant_id)},
        )
    return stage


async def _actor_is_guarantor(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantor_member_id: uuid.UUID,
) -> bool:
    """Whether the calling user IS the guarantor member (P13.14).

    Interim identity link until member-facing authentication lands
    (BUILD_PROMPTS P14): the caller's users.email must equal the
    guarantor member's members.email inside the same tenant. The match
    is BYTE-EXACT (Postgres `=`, case- and whitespace-sensitive) by
    decision: no email canonicalisation exists anywhere in this
    codebase (users and members store emails verbatim), so a variant
    fails CLOSED — it can only deny the self-service path, never widen
    it (review R3; tested). The join is deny-by-default (a NULL member
    email never matches) and cannot be steered by callers below staff
    level: rewriting either email requires members:edit or
    access_control:edit, and every role holding those already holds
    applications:edit (the staff release path) — asserted against the
    SEEDED P4 matrix by test, not by comment. Both lookups carry
    explicit tenant predicates on top of RLS, so a user email-linked to
    a member of ANOTHER tenant never matches (gate 1.6 v1.1; tested).
    """
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM users u JOIN members m "
                "ON m.email = u.email AND m.tenant_id = u.tenant_id "
                "WHERE u.id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid) "
                "AND m.id = CAST(:mid AS uuid) AND m.email IS NOT NULL"
            ),
            {
                "uid": str(actor_id),
                "tid": str(tenant_id),
                "mid": str(guarantor_member_id),
            },
        )
    ).first()
    return row is not None


async def release_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
    actor_role_id: uuid.UUID,
) -> GuaranteeRecord:
    """Release one guarantee under the P13.14 rules (gates 1.4, 1.6).

    Branches (all decided under the application/loan row lock):
      * pledged (unconsented): released by staff with applications:edit
        or by the guarantor themselves (_actor_is_guarantor).
      * active + undisbursed application: staff-only; allowed only if
        remaining cover still satisfies the product rule, re-verified
        AT EXECUTION under the borrower's deposit-account row lock (the
        P7 gate math via application_max_eligible — gate 1.1, no forked
        cover logic). The release write happens first, so the check
        sees exactly the post-release state; a failure rolls the whole
        transaction back (TOCTOU-proof).
      * active + disbursed loan: never bare-released — 409; the only
        path is substitute_guarantee (atomic swap).

    Least disclosure (gate 1.6): no rejection ever echoes cover,
    capacity, or pledge figures; the audit row carries the numbers.
    """
    probe = await _read_guarantee(session, tenant_id, guarantee_id, for_update=False)
    if probe is None:
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    # Lock order: application/loan row -> (borrower deposit account for
    # the cover guard). The guarantee row lock is taken after the
    # anchor, matching P9 pledging (application FOR UPDATE first).
    stage = await _lock_release_anchor(session, tenant_id, probe)
    g = await _read_guarantee(session, tenant_id, guarantee_id, for_update=True)
    if g is None:  # pragma: no cover - the probe above already found it
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    if g.status == "released":
        raise ConflictError(f"guarantee {guarantee_id} is already released")
    staff_edit = await rbac_service.has_permission(
        session, actor_role_id, Module.APPLICATIONS, Action.EDIT
    )
    if not staff_edit and (
        g.status != "pledged"
        or not await _actor_is_guarantor(session, tenant_id, actor_id, g.guarantor_member_id)
    ):
        # Wrong actor (P13.14 failure mode 7): a guarantor may withdraw
        # only their OWN unconsented pledge; anything else needs
        # applications:edit.
        raise ForbiddenError(
            "only staff with applications:edit or the guarantor of their own "
            "unconsented pledge may release a guarantee"
        )
    disbursed = g.loan_id is not None or stage is ApplicationStage.DISBURSED
    if g.status == "active" and disbursed:
        # Failure mode 2: no release path exists for a disbursed loan's
        # collateral — substitution is the only exit.
        raise ConflictError(
            "an active guarantee behind a disbursed loan can only be substituted, never released"
        )
    updated = (
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 v1.1). RETURNING makes the
                # response version the database's word, never arithmetic
                # on caller input (review R5).
                "UPDATE guarantees SET status = 'released', "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver RETURNING version"
            ),
            {"id": str(guarantee_id), "tid": str(tenant_id), "ver": version},
        )
    ).first()
    if updated is None:
        raise ConflictError(f"stale version {version} for guarantee {guarantee_id}")
    released_version = int(updated[0])
    if g.status == "active" and stage in _COVER_GUARDED_STAGES and g.application_id is not None:
        # Cover-strip guard (failure mode 1), re-verified under the
        # borrower's deposit-account row lock AFTER the release write:
        # the P7 gate math sees exactly the remaining live guarantees.
        await session.execute(
            text(
                "SELECT balance FROM deposit_accounts WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(g.borrower_member_id), "tid": str(tenant_id)},
        )
        record = await get_application(session, tenant_id, g.application_id)
        max_eligible = await application_max_eligible(session, tenant_id, record)
        if record.amount > max_eligible:
            raise ConflictError(
                "releasing this guarantee would leave the application below the product cover rule"
            )
    if g.application_id is not None:
        await recompute_cover(session, tenant_id, g.application_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.release",
        entity="guarantees",
        entity_id=str(guarantee_id),
        before={"status": g.status},
        after={
            "status": "released",
            "application_id": str(g.application_id) if g.application_id else None,
            "loan_id": str(g.loan_id) if g.loan_id else None,
            "guarantor_member_id": str(g.guarantor_member_id),
            "amount": str(g.amount),
        },
    )
    # Outbox notifications for BOTH sides (gates 1.2, 1.5): the
    # guarantor learns their capacity is freed; the borrower learns
    # their cover changed.
    for notify in (g.guarantor_member_id, g.borrower_member_id):
        await enqueue_event(
            session,
            tenant_id,
            event_type="guarantee.released",
            payload={
                "guarantee_id": str(guarantee_id),
                "application_id": str(g.application_id) if g.application_id else None,
                "loan_id": str(g.loan_id) if g.loan_id else None,
                "guarantor_member_id": str(g.guarantor_member_id),
                "borrower_member_id": str(g.borrower_member_id),
                "amount": str(g.amount),
                "notify_member_id": str(notify),
            },
        )
    return GuaranteeRecord(
        id=g.id,
        application_id=g.application_id,
        loan_id=g.loan_id,
        guarantor_member_id=g.guarantor_member_id,
        borrower_member_id=g.borrower_member_id,
        amount=g.amount,
        status="released",
        version=released_version,
    )


async def substitute_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
    guarantor_member_id: uuid.UUID,
    consented: bool,
    consent_reference: str,
    amount: Decimal | None = None,
) -> tuple[GuaranteeRecord, GuaranteeRecord]:
    """Atomic swap for a disbursed loan's collateral (P13.14, gate 1.5).

    Release of the old guarantee and creation of the replacement
    CONSENTED pledge happen in this single transaction; any failure
    between the two writes rolls everything back (kill-switch tested —
    zero partial state). The released amount comes from the guarantee
    row, never the request; a caller-supplied replacement amount may
    only meet or exceed it. Replacement capacity is verified under the
    NEW guarantor's member FOR SHARE + deposit-account FOR UPDATE locks
    via the single P9 capacity implementation (gate 1.1). Returns
    (released, replacement).

    Consent integrity (review R1 — accepted risk, closed by P14): the
    substitute guarantor cannot act for themselves until member-facing
    authentication exists, so their consent is STAFF-ATTESTED here —
    exactly the trust model of the P9 consent route, which is likewise
    gated on applications:edit. To keep the attestation honest it is a
    first-class audited fact, not a bare boolean: the caller must cite
    the evidence (consent_reference, e.g. the signed guarantorship
    form), a dedicated guarantee.consent audit row records WHO attested
    on WHAT basis, and a consent-confirmation outbox notification goes
    to the substitute guarantor so a conscripted member finds out
    immediately (detection control).
    """
    consent_reference = consent_reference.strip()
    if not consented or not consent_reference:
        # Failure mode 4: collateral is never activated without the
        # guarantor's recorded consent (the P9 consent contract); an
        # unconsented substitute would recreate the exact hole the P7
        # step-2c gate closed. The attestation must cite its evidence.
        raise UnprocessableError(
            "the replacement pledge requires the substitute guarantor's recorded "
            "consent and a consent reference"
        )
    probe = await _read_guarantee(session, tenant_id, guarantee_id, for_update=False)
    if probe is None:
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    stage = await _lock_release_anchor(session, tenant_id, probe)
    g = await _read_guarantee(session, tenant_id, guarantee_id, for_update=True)
    if g is None:  # pragma: no cover - the probe above already found it
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    if g.status != "active":
        raise ConflictError(f"only active guarantees can be substituted, not '{g.status}'")
    if g.loan_id is None and stage is not ApplicationStage.DISBURSED:
        raise ConflictError(
            "substitution applies to guarantees behind a disbursed loan; "
            "use release before disbursement"
        )
    if guarantor_member_id == g.borrower_member_id:
        raise InvalidInputError("a member cannot guarantee their own loan")
    released_amount = g.amount
    replacement_amount = to_cents(amount) if amount is not None else released_amount
    if replacement_amount < released_amount:
        # Failure mode 3: a substitution may never shrink the loan's
        # collateral. Least disclosure: the floor figure lives on the
        # guarantee row and in the audit trail, not in this message.
        raise UnprocessableError("the replacement pledge must cover at least the released amount")
    updated = (
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 v1.1). RETURNING makes the
                # response version the database's word, never arithmetic
                # on caller input (review R5).
                "UPDATE guarantees SET status = 'released', "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver RETURNING version"
            ),
            {"id": str(guarantee_id), "tid": str(tenant_id), "ver": version},
        )
    ).first()
    if updated is None:
        raise ConflictError(f"stale version {version} for guarantee {guarantee_id}")
    released_version = int(updated[0])
    # New pledge leg — the established P9 chain: guarantor member FOR
    # SHARE -> guarantor deposit account FOR UPDATE. Runs AFTER the
    # release write so a self-substitution (same guarantor, adjusted
    # amount) prices capacity against the post-release state.
    available = await _guarantor_available_capacity(session, tenant_id, guarantor_member_id)
    if replacement_amount > available:
        # Least disclosure (gate 1.6): neither the capacity nor the
        # shortfall is echoed.
        raise ConflictError("insufficient guarantor capacity for the replacement pledge")
    replacement_id = uuid.uuid4()
    replacement_version = int(
        (
            await session.execute(
                text(
                    # RETURNING version: the schema default is the source
                    # of truth for the new row's version (review R5).
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                    " application_id, loan_id, amount, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), CAST(:a AS uuid), CAST(:l AS uuid), :amount, 'active') "
                    "RETURNING version"
                ),
                {
                    "id": str(replacement_id),
                    "tid": str(tenant_id),
                    "g": str(guarantor_member_id),
                    "b": str(g.borrower_member_id),
                    "a": str(g.application_id) if g.application_id else None,
                    "l": str(g.loan_id) if g.loan_id else None,
                    "amount": str(replacement_amount),
                },
            )
        ).scalar_one()
    )
    if g.application_id is not None:
        await recompute_cover(session, tenant_id, g.application_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.release",
        entity="guarantees",
        entity_id=str(guarantee_id),
        before={"status": "active"},
        after={
            "status": "released",
            "substituted_by": str(replacement_id),
            "guarantor_member_id": str(g.guarantor_member_id),
            "amount": str(released_amount),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.substitute",
        entity="guarantees",
        entity_id=str(replacement_id),
        after={
            "replaces": str(guarantee_id),
            "application_id": str(g.application_id) if g.application_id else None,
            "loan_id": str(g.loan_id) if g.loan_id else None,
            "guarantor_member_id": str(guarantor_member_id),
            "amount": str(replacement_amount),
            "status": "active",
        },
    )
    # Review R1: the consent attestation is a first-class audited fact
    # mirroring the P9 consent trail — WHO attested, on WHAT basis —
    # plus a confirmation notification to the substitute guarantor so
    # an attestation made in their name never goes unseen (gates 1.5,
    # 1.2; accepted risk closed by P14 member-facing auth).
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.consent",
        entity="guarantees",
        entity_id=str(replacement_id),
        after={
            "status": "active",
            "replaces": str(guarantee_id),
            "guarantor_member_id": str(guarantor_member_id),
            "attested_by": str(actor_id),
            "consent_reference": consent_reference,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="guarantee.consented",
        payload={
            "guarantee_id": str(replacement_id),
            "replaces": str(guarantee_id),
            "attested_by": str(actor_id),
            "consent_reference": consent_reference,
            "notify_member_id": str(guarantor_member_id),
        },
    )
    # Outbox notifications for BOTH sides of the swap plus the borrower
    # (gates 1.2, 1.5).
    for notify in (g.guarantor_member_id, guarantor_member_id, g.borrower_member_id):
        await enqueue_event(
            session,
            tenant_id,
            event_type="guarantee.substituted",
            payload={
                "released_guarantee_id": str(guarantee_id),
                "replacement_guarantee_id": str(replacement_id),
                "loan_id": str(g.loan_id) if g.loan_id else None,
                "released_guarantor_member_id": str(g.guarantor_member_id),
                "replacement_guarantor_member_id": str(guarantor_member_id),
                "borrower_member_id": str(g.borrower_member_id),
                "released_amount": str(released_amount),
                "replacement_amount": str(replacement_amount),
                "notify_member_id": str(notify),
            },
        )
    released = GuaranteeRecord(
        id=g.id,
        application_id=g.application_id,
        loan_id=g.loan_id,
        guarantor_member_id=g.guarantor_member_id,
        borrower_member_id=g.borrower_member_id,
        amount=released_amount,
        status="released",
        version=released_version,
    )
    replacement = GuaranteeRecord(
        id=replacement_id,
        application_id=g.application_id,
        loan_id=g.loan_id,
        guarantor_member_id=guarantor_member_id,
        borrower_member_id=g.borrower_member_id,
        amount=replacement_amount,
        status="active",
        version=replacement_version,
    )
    return released, replacement
