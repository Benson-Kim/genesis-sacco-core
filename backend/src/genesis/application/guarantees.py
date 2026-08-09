"""Guarantorship services: pledge, consent, release, substitution (P9/).

Capacity (deposit balance minus existing pledged/active guarantees) is
computed while holding the guarantor's deposit-account row lock, so
concurrent pledges - and future balance-changing operations that take
the same lock - can never over-pledge a member.

 adds the per-guarantee release/substitution the prototype's
Guarantors screen exposes. Lock order (the established P9 pledge
chain — no new lock-graph edges): application/loan row -> guarantor
member FOR SHARE -> guarantor deposit account FOR UPDATE. The member
row in this chain is always the GUARANTOR's, so it can never form a
cycle with the settlement chain (member -> accounts -> loans),
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

from genesis.application.audit import record_audit
from genesis.application.auth import MemberAuthContext
from genesis.application.loan_applications import (
    application_max_eligible,
    get_application,
    recompute_cover,
)
from genesis.application.member_auth import live_credential_by_id
from genesis.application.outbox import enqueue_event
from genesis.domain.lending import ApplicationStage
from genesis.domain.members import MemberStatus, MoneyOperation, member_may
from genesis.domain.money import ZERO, to_cents
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

#: Statuses that make a guarantee LIVE, i.e. counted against the
#: guarantor's capacity. Single source of truth (reuse-first) shared by
#: live_pledged_total (the P9 pledge / withdrawal enforcement
#: path) and the dashboard aggregates (failure mode): an
#: aggregate filtering a different set would show members capacity the
#: pledge endpoint then refuses.
LIVE_GUARANTEE_STATUSES: tuple[str, str] = ("pledged", "active")

#: SQL behind live_pledged_total, module-level so the EXPLAIN
#: capture can assert its plan (idx_guarantees_guarantor, 0001). Every
#: value — the status set included — is a bound parameter.
LIVE_PLEDGED_SQL = (
    "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
    "WHERE guarantor_member_id = CAST(:g AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid) "
    "AND status IN (:live0, :live1)"
)


def live_guarantee_params() -> dict[str, str]:
    """Bound parameters for the LIVE status set."""
    return {"live0": LIVE_GUARANTEE_STATUSES[0], "live1": LIVE_GUARANTEE_STATUSES[1]}


#: Undisbursed stages whose product cover rule guards an active-guarantee
#: release: while the application is in flight, releasing
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
    change (concurrency safety). Shared by P9 pledging and withdrawals. The
    explicit tenant predicate doubles the RLS fence on this money path
    (defence in depth, least disclosure).
    """
    value = (
        await session.execute(
            text(LIVE_PLEDGED_SQL),
            {
                "g": str(guarantor_member_id),
                "tid": str(tenant_id),
                **live_guarantee_params(),
            },
        )
    ).scalar_one()
    return Decimal(str(value))


async def _guarantor_available_capacity(
    session: AsyncSession, tenant_id: uuid.UUID, guarantor_member_id: uuid.UUID
) -> Decimal:
    """Available pledge capacity under the P9 lock chain (the house gates).

    Takes the guarantor member row FOR SHARE (holds off a concurrent
    terminal exit until the pledge commits) and the guarantor's
    deposit-account row FOR UPDATE (the serialisation point every capacity computation shares with
    withdrawals), then returns
    balance minus live pledges. The single implementation behind P9
    pledging and substitution — a second copy of this math is a
    rejected MR (reuse-first). Callers compare and raise their own
    least-disclosure error; the capacity itself is never echoed.
    """
    guarantor_row = (
        await session.execute(
            # FOR SHARE holds off a concurrent terminal member exit
            # (which locks the row FOR UPDATE) until this pledge commits,
            # closing the TOCTOU window between the status check and the
            # insert (concurrency safety).
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR SHARE"
            ),
            {"m": str(guarantor_member_id), "tid": str(tenant_id)},
        )
    ).first()
    if guarantor_row is None:
        raise NotFoundError(f"guarantor member {guarantor_member_id} not found")
    guarantor_status = MemberStatus(str(guarantor_row[0]))
    if not member_may(guarantor_status, MoneyOperation.PLEDGE):
        # Code-owned capability map: pledging is strictly
        # active-only, so arrears/dormant/exited (and any future
        # status) are refused by construction — the R2 rule.
        raise ConflictError(
            f"guarantor {guarantor_member_id} is '{guarantor_status.value}': "
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
    """Pledge under the guarantor's deposit-account row lock (concurrency safety)."""
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
        # Least disclosure (least disclosure): the available capacity derives from
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


# ---------------------------------------------------------------------------
#  — consent core (pledged -> active), ONE implementation
# ---------------------------------------------------------------------------

#: Code-owned principal fragments for the consent write (
#: the fragment is chosen by the code path, never caller input; every
#: VALUE is a bound parameter). Consent rows carry their principal at
#: the DB level: the member path stamps the consenting
#: credential; the staff override stamps the attesting user + the
#: cited evidence. An UPDATE carrying NEITHER is refused by the 0035
#: guarantee_consent_requires_principal trigger.
_CONSENT_MEMBER_FRAGMENT = ", consented_by_credential_id = CAST(:cred AS uuid)"
_CONSENT_ATTESTED_FRAGMENT = (
    ", consent_attested_by = CAST(:attestor AS uuid), consent_reference = :cref"
)


async def _lock_pledged_guarantee(
    session: AsyncSession, tenant_id: uuid.UUID, guarantee_id: uuid.UUID
) -> _GuaranteeRow:
    """Lock the guarantee row ALONE (lock-order.md single-node locker — the consent posture is
    unchanged by) and require
    the pledged state."""
    g = await _read_guarantee(session, tenant_id, guarantee_id, for_update=True)
    if g is None:
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    if g.status != "pledged":
        raise ConflictError(f"only pledged guarantees can be consented, not '{g.status}'")
    return g


async def _activate_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
    principal_fragment: str,
    principal_params: dict[str, str],
) -> int:
    """The single pledged->active write (the house gates): optimistic
    version fence, explicit tenant predicate on top of RLS, RETURNING
    so the response version is the database's word. The principal
    fragment is one of the code-owned literals above."""
    updated = (
        await session.execute(
            text(
                "UPDATE guarantees SET status = 'active', "  # noqa: S608
                "version = version + 1, updated_at = now()"
                f"{principal_fragment} "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver RETURNING version"
            ),
            {
                "id": str(guarantee_id),
                "tid": str(tenant_id),
                "ver": version,
                **principal_params,
            },
        )
    ).first()
    if updated is None:
        raise ConflictError(f"stale version {version} for guarantee {guarantee_id}")
    return int(updated[0])


def _consented_record(g: _GuaranteeRow, new_version: int) -> GuaranteeRecord:
    return GuaranteeRecord(
        id=g.id,
        application_id=g.application_id,
        loan_id=g.loan_id,
        guarantor_member_id=g.guarantor_member_id,
        borrower_member_id=g.borrower_member_id,
        amount=g.amount,
        status="active",
        version=new_version,
    )


async def consent_guarantee_as_member(
    session: AsyncSession,
    principal: MemberAuthContext,
    guarantee_id: uuid.UUID,
    *,
    version: int,
) -> GuaranteeRecord:
    """Guarantor consent as an act of the MEMBER principal.

    The credential->member LINK is re-verified INSIDE this transaction
    under the guarantee row lock (live_credential_by_id — the ONE
    implementation the auth paths use), so a link revoked or re-pointed
    after token issue can never consent: the LINK, never any email,
    decides. Least disclosure (least disclosure): every wrong-principal shape —
    dead link, exited member, someone else's guarantee — gets the SAME
    single refusal; the audit actor is the credential id.
    """
    tenant_id = principal.tenant_id
    g = await _lock_pledged_guarantee(session, tenant_id, guarantee_id)
    credential = await live_credential_by_id(session, tenant_id, principal.credential_id)
    if credential is None or credential.member_id != g.guarantor_member_id:
        raise ForbiddenError("only the guarantor's own live credential may consent")
    new_version = await _activate_guarantee(
        session,
        tenant_id,
        guarantee_id,
        version=version,
        principal_fragment=_CONSENT_MEMBER_FRAGMENT,
        principal_params={"cred": str(principal.credential_id)},
    )
    await record_audit(
        session,
        tenant_id,
        principal.credential_id,
        action="guarantee.consent",
        entity="guarantees",
        entity_id=str(guarantee_id),
        before={"status": "pledged"},
        after={
            "status": "active",
            "consented_by_credential_id": str(principal.credential_id),
            "guarantor_member_id": str(g.guarantor_member_id),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="guarantee.consented",
        payload={
            "guarantee_id": str(guarantee_id),
            "consented_by_credential_id": str(principal.credential_id),
            "notify_member_id": str(g.guarantor_member_id),
        },
    )
    return _consented_record(g, new_version)


async def consent_guarantee_override(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
    consent_reference: str,
) -> GuaranteeRecord:
    """STAFF-ATTESTED consent override (scope 4).

    Consent is the member principal's act; this path exists for the
    cases where the member cannot act (no credential yet, paper
    consent) and is deliberately NOT the P9 staff consent: it carries
    its own permission (member_identity:approve at the route — never
    applications:edit), its own audit category
    (guarantee.consent_override), a MANDATORY consent_reference citing
    the evidence, and a confirmation notification to the guarantor so
    an attestation made in their name never goes unseen (the substitution-consent lesson; detection
    control).
    """
    consent_reference = consent_reference.strip()
    if not consent_reference:
        raise UnprocessableError("the staff-attested consent override must cite its evidence")
    g = await _lock_pledged_guarantee(session, tenant_id, guarantee_id)
    new_version = await _activate_guarantee(
        session,
        tenant_id,
        guarantee_id,
        version=version,
        principal_fragment=_CONSENT_ATTESTED_FRAGMENT,
        principal_params={"attestor": str(actor_id), "cref": consent_reference},
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.consent_override",
        entity="guarantees",
        entity_id=str(guarantee_id),
        before={"status": "pledged"},
        after={
            "status": "active",
            "attested_by": str(actor_id),
            "consent_reference": consent_reference,
            "guarantor_member_id": str(g.guarantor_member_id),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="guarantee.consented",
        payload={
            "guarantee_id": str(guarantee_id),
            "attested_by": str(actor_id),
            "consent_reference": consent_reference,
            "notify_member_id": str(g.guarantor_member_id),
        },
    )
    return _consented_record(g, new_version)


async def release_guarantees_for_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    loan_id: uuid.UUID,
) -> int:
    """Release every live guarantee behind a loan (closure hook)."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, least disclosure — finding 15).
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
#  — per-guarantee release & substitution
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
    caller input.
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
    """Application (then loan) row FOR UPDATE — the lock anchor.

    Application before loan matches the only existing path that locks
    both (P7 disbursement); no path locks a loan before its application,
    so no cycle is possible. Explicit tenant predicates on both reads
    (least disclosure v1.1).
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


async def _lock_release_target(
    session: AsyncSession, tenant_id: uuid.UUID, guarantee_id: uuid.UUID
) -> tuple[_GuaranteeRow, ApplicationStage | None]:
    """probe -> anchor -> row lock: the ONE release lock acquisition.

    Lock order (lock-order.md E4/E6/E7): application/loan row first,
    then the guarantee row FOR UPDATE, matching P9 pledging
    (application FOR UPDATE first). Shared by the staff and member
    release paths so the locking discipline can never diverge.
    """
    probe = await _read_guarantee(session, tenant_id, guarantee_id, for_update=False)
    if probe is None:
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    stage = await _lock_release_anchor(session, tenant_id, probe)
    g = await _read_guarantee(session, tenant_id, guarantee_id, for_update=True)
    if g is None:  # pragma: no cover - the probe above already found it
        raise NotFoundError(f"guarantee {guarantee_id} not found")
    if g.status == "released":
        raise ConflictError(f"guarantee {guarantee_id} is already released")
    return g, stage


async def _release_locked_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    g: _GuaranteeRow,
    stage: ApplicationStage | None,
    *,
    version: int,
) -> GuaranteeRecord:
    """The single release write/guard/audit/outbox core (reuse-first).

    Caller holds the anchor + guarantee row locks
    (_lock_release_target). Rules decided here, identically for both
    principals:
      * active + disbursed loan: never bare-released — 409; the only
        path is substitute_guarantee (atomic swap).
      * active + undisbursed application: allowed only if remaining
        cover still satisfies the product rule, re-verified AT
        EXECUTION under the borrower's deposit-account row lock (the P7 gate math via
        application_max_eligible — reuse-first, no forked cover logic). The release write happens
        first, so the
        check sees exactly the post-release state; a failure rolls the
        whole transaction back (TOCTOU-proof).

    ``actor_id`` is the acting principal's id — the staff user or the
    member CREDENTIAL: audit_log.actor_id carries whichever
    principal acted.

    Least disclosure (least disclosure): no rejection ever echoes cover,
    capacity, or pledge figures; the audit row carries the numbers.
    """
    guarantee_id = g.id
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
                # (defence in depth, least disclosure v1.1). RETURNING makes the
                # response version the database's word, never arithmetic
                # on caller input.
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
    # Outbox notifications for BOTH sides (the house gates): the
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


async def release_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
) -> GuaranteeRecord:
    """STAFF release (rules; actor model).

    Gated applications:edit EXACTLY at the route — the retirement
    of the interim email-match: a guarantor acting for themselves
    is a MEMBER principal on the /member route
    (release_guarantee_as_member), never a staff user with a matching
    email. Rules and side effects live in the shared core.
    """
    g, stage = await _lock_release_target(session, tenant_id, guarantee_id)
    return await _release_locked_guarantee(session, tenant_id, actor_id, g, stage, version=version)


async def release_guarantee_as_member(
    session: AsyncSession,
    principal: MemberAuthContext,
    guarantee_id: uuid.UUID,
    *,
    version: int,
) -> GuaranteeRecord:
    """Guarantor withdraws their OWN unconsented pledge.

    The credential->member link is re-verified INSIDE the transaction
    under the guarantee row lock (the ONE live_credential_by_id
    implementation): the LINK, never any email, decides. Scope is
    deliberately minimal — the member's own PLEDGED guarantee only;
    consented (active) collateral needs the staff paths. Least
    disclosure (least disclosure): every wrong-principal shape — dead link,
    someone else's guarantee, an already-consented pledge — gets the
    SAME single refusal.
    """
    tenant_id = principal.tenant_id
    g, stage = await _lock_release_target(session, tenant_id, guarantee_id)
    credential = await live_credential_by_id(session, tenant_id, principal.credential_id)
    owner = credential is not None and credential.member_id == g.guarantor_member_id
    if not owner or g.status != "pledged":
        raise ForbiddenError("only the guarantor's own unconsented pledge may be withdrawn")
    return await _release_locked_guarantee(
        session, tenant_id, principal.credential_id, g, stage, version=version
    )


async def substitute_guarantee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    guarantee_id: uuid.UUID,
    *,
    version: int,
    guarantor_member_id: uuid.UUID,
    consent_reference: str,
    amount: Decimal | None = None,
) -> tuple[GuaranteeRecord, GuaranteeRecord]:
    """Atomic swap for a disbursed loan's collateral (data integrity).

    Release of the old guarantee and creation of the replacement
    CONSENTED pledge happen in this single transaction; any failure
    between the two writes rolls everything back (kill-switch tested —
    zero partial state). The released amount comes from the guarantee
    row, never the request; a caller-supplied replacement amount may
    only meet or exceed it. Replacement capacity is verified under the
    NEW guarantor's member FOR SHARE + deposit-account FOR UPDATE locks
    via the single P9 capacity implementation (reuse-first). Returns
    (released, replacement).

    Consent integrity (completed by): the substitute's
    consent is a STAFF-ATTESTED override — never a caller-asserted
    boolean (the lesson made structural: no `consented` field exists anywhere). The attestation must
    cite its evidence
    (consent_reference, e.g. the signed guarantorship form); the
    replacement row CARRIES the attestation columns, so the 0035
    trigger refuses an active row written without a principal; a
    dedicated guarantee.consent_override audit row records WHO attested
    on WHAT basis; and a consent-confirmation outbox notification goes
    to the substitute guarantor so a conscripted member finds out
    immediately (detection control).
    """
    consent_reference = consent_reference.strip()
    if not consent_reference:
        # Failure mode 4: collateral is never activated without the
        # guarantor's recorded consent (the P9 consent contract); an
        # unconsented substitute would recreate the exact hole the P7
        # step-2c gate closed. The attestation must cite its evidence.
        raise UnprocessableError(
            "the replacement pledge requires a consent reference citing the "
            "substitute guarantor's attested consent"
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
                # (defence in depth, least disclosure v1.1). RETURNING makes the
                # response version the database's word, never arithmetic
                # on caller input.
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
        # Least disclosure (least disclosure): neither the capacity nor the
        # shortfall is echoed.
        raise ConflictError("insufficient guarantor capacity for the replacement pledge")
    replacement_id = uuid.uuid4()
    replacement_version = int(
        (
            await session.execute(
                text(
                    # RETURNING version: the schema default is the source
                    # of truth for the new row's version.
                    # The row carries the staff attestation:
                    # the 0035 trigger refuses an active INSERT without
                    # a principal, so a substitution that lost these
                    # columns would fail AT THE DATABASE.
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                    " application_id, loan_id, amount, status, "
                    " consent_attested_by, consent_reference) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), CAST(:a AS uuid), CAST(:l AS uuid), :amount, 'active', "
                    "CAST(:attestor AS uuid), :cref) "
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
                    "attestor": str(actor_id),
                    "cref": consent_reference,
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
    # completed by: the consent attestation is a
    # first-class audited fact under the OVERRIDE category — WHO
    # attested, on WHAT basis — plus a confirmation notification to
    # the substitute guarantor so an attestation made in their name
    # never goes unseen (the house gates).
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="guarantee.consent_override",
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
    # (the house gates).
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
