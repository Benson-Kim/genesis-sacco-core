"""Loan application services: creation, stage machine, committee voting (P9).

Stage changes run under SELECT ... FOR UPDATE through the pure P6
transition function (gate 1.4). The API-facing transition set excludes
APPROVED (only committee quorum produces it) and DISBURSED (only the P7
disbursement contract produces it). Cover% is a derived field computed
from the member's deposit balance plus pledged/active guarantees.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.loan_products import get_product
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import build_created_id_cursor, parse_created_id_cursor
from genesis.application.tenant_settings import committee_quorum, enforce_authority_band
from genesis.domain.committee import Decision, Vote, decide
from genesis.domain.lending import ApplicationStage, InvalidTransitionError, transition
from genesis.domain.members import MemberStatus, MoneyOperation, member_may
from genesis.domain.money import ZERO, to_cents
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError, NotFoundError

#: Stage moves a caller may request directly. APPROVED comes only from
#: committee quorum; DISBURSED comes only from P7's disburse_loan.
API_TRANSITION_TARGETS = frozenset(
    {ApplicationStage.APPRAISAL, ApplicationStage.COMMITTEE, ApplicationStage.REJECTED}
)

_COLS = (
    "id, member_id, product_id, amount, term_months, rate_pct, purpose, stage, cover_pct, version"
)

#: cover_pct is stored as NUMERIC(6,2); values above this cap carry no
#: extra information ("covered many times over") and would overflow the
#: column, turning a valid request into an unhandled 500.
_COVER_PCT_CAP = Decimal("9999.99")


@dataclass(frozen=True)
class ApplicationRecord:
    id: uuid.UUID
    member_id: uuid.UUID
    product_id: uuid.UUID
    amount: Decimal
    term_months: int
    rate_pct: Decimal
    purpose: str | None
    stage: ApplicationStage
    cover_pct: Decimal
    version: int


@dataclass(frozen=True)
class VoteTally:
    approvals: int
    rejections: int
    decision: Decision | None
    stage: ApplicationStage


def _row_to_application(row: Any) -> ApplicationRecord:
    return ApplicationRecord(
        id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        product_id=uuid.UUID(str(row[2])),
        amount=Decimal(str(row[3])),
        term_months=int(row[4]),
        rate_pct=Decimal(str(row[5])),
        purpose=str(row[6]) if row[6] is not None else None,
        stage=ApplicationStage(str(row[7])),
        cover_pct=Decimal(str(row[8])),
        version=int(row[9]),
    )


async def _deposit_balance(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> Decimal:
    # Explicit tenant predicate on top of RLS (defence in depth,
    # gate 1.6 v1.1; issue #17).
    row = (
        await session.execute(
            text(
                "SELECT balance FROM deposit_accounts WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    return Decimal(str(row[0])) if row is not None else ZERO


async def guarantee_total(
    session: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> Decimal:
    """Sum of an application's live (pledged/active) guarantee amounts.

    Shared by cover%% recomputation, the eligibility read model and the
    P7 disbursement multiplier gate (issue #15). Callers making a money
    decision must hold the application row lock (pledging takes it FOR
    UPDATE, so the sum cannot change underneath them).
    """
    value = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                "WHERE application_id = CAST(:a AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status IN ('pledged', 'active')"
            ),
            {"a": str(application_id), "tid": str(tenant_id)},
        )
    ).scalar_one()
    return Decimal(str(value))


def _cover_pct(deposits: Decimal, guarantees: Decimal, amount: Decimal) -> Decimal:
    raw = to_cents((deposits + guarantees) * Decimal("100") / amount)
    return min(raw, _COVER_PCT_CAP)


async def _release_application_pledges(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    application_id: uuid.UUID,
) -> int:
    """Release live pledges when an application is rejected (gates 1.4, 1.5).

    Mirrors guarantees.release_guarantees_for_loan for the pre-loan
    stage. It lives here rather than in guarantees.py because that
    module imports recompute_cover from this one and the reverse import
    would be circular.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 v1.1; issue #17).
                "UPDATE guarantees SET status = 'released', "
                "version = version + 1, updated_at = now() "
                "WHERE application_id = CAST(:aid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status IN ('pledged', 'active')"
            ),
            {"aid": str(application_id), "tid": str(tenant_id)},
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
            entity_id=str(application_id),
            after={"application_id": str(application_id), "released": released},
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="guarantee.released",
            payload={"application_id": str(application_id), "released": released},
        )
    return released


async def create_application(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    member_id: uuid.UUID,
    product_id: uuid.UUID,
    amount: Decimal,
    term_months: int,
    purpose: str | None = None,
) -> ApplicationRecord:
    """Create an application obeying product rules (gate 1.6).

    The rate is derived from the product - clients never supply pricing.
    Cover% is computed at creation from the member's deposit balance
    (guarantees are pledged later and recompute it).
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("amount must be positive")
    product = await get_product(session, tenant_id, product_id)
    if not product.active:
        raise InvalidInputError(f"loan product {product_id} is inactive")
    if term_months <= 0 or term_months > product.max_term_months:
        raise InvalidInputError(
            f"term {term_months} outside product limit of {product.max_term_months} months"
        )
    member_row = (
        await session.execute(
            # FOR SHARE holds off a concurrent terminal member exit
            # (which locks the row FOR UPDATE) until this create
            # commits, closing the TOCTOU window between the status
            # check and the insert (gate 1.4; the P9 pledge / P11
            # _require_member precedent — external Codex review,
            # re-derived). Lock order: member row only, nothing after
            # it — consistent with the P12 chain (member first), so no
            # cycle with settlement (member -> accounts -> loans) is
            # possible. Explicit tenant predicate on top of RLS
            # (gate 1.6 v1.1).
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR SHARE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if member_row is None:
        raise NotFoundError(f"member {member_id} not found")
    member_status = MemberStatus(str(member_row[0]))
    if not member_may(member_status, MoneyOperation.BORROW):
        # Code-owned capability map (P13.13 FM2): borrowing is strictly
        # active-only, so arrears/dormant/exited (and any future
        # status) are refused by construction — the !29 R2 rule.
        raise ConflictError(
            f"member {member_id} is '{member_status.value}': only active members may apply"
        )
    deposits = await _deposit_balance(session, tenant_id, member_id)
    # Deliberately NO deposit-multiplier gate at creation (external
    # Codex review fix REJECTED, with reasoning): the shipped issue #15
    # eligibility is deposits x multiplier + live guarantees, and
    # guarantees are pledged AFTER creation — a deposits-only creation
    # block would foreclose guarantee-backed borrowing entirely (a
    # zero-deposit borrower with full guarantor cover is legitimate
    # and covered by the P9/P12.5 test suite). The cap is surfaced to
    # callers via max_eligible on the single-application read, and the
    # BINDING check runs at disbursement under the full lock set (P7
    # step 2b). An own-multiplier product policy independent of
    # guarantees is a tenant-configuration decision (BUILD_PROMPTS
    # P13.7), not a hard-coded creation block.
    cover = _cover_pct(deposits, ZERO, amount)
    application_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO loan_applications "
            "(id, tenant_id, member_id, product_id, amount, term_months, "
            " rate_pct, purpose, cover_pct) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
            "CAST(:pid AS uuid), :amount, :term, :rate, :purpose, :cover)"
        ),
        {
            "id": str(application_id),
            "tid": str(tenant_id),
            "mid": str(member_id),
            "pid": str(product_id),
            "amount": str(amount),
            "term": term_months,
            "rate": str(product.rate_pct),
            "purpose": purpose,
            "cover": str(cover),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="application.create",
        entity="loan_applications",
        entity_id=str(application_id),
        after={
            "member_id": str(member_id),
            "product_id": str(product_id),
            "amount": str(amount),
            "term_months": term_months,
            "rate_pct": str(product.rate_pct),
            "cover_pct": str(cover),
            "stage": ApplicationStage.SUBMITTED.value,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="loan.application_submitted",
        payload={
            "application_id": str(application_id),
            "member_id": str(member_id),
            "amount": str(amount),
        },
    )
    return ApplicationRecord(
        id=application_id,
        member_id=member_id,
        product_id=product_id,
        amount=amount,
        term_months=term_months,
        rate_pct=product.rate_pct,
        purpose=purpose,
        stage=ApplicationStage.SUBMITTED,
        cover_pct=cover,
        version=1,
    )


async def get_application(
    session: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> ApplicationRecord:
    # Explicit tenant predicate on top of RLS (defence in depth,
    # gate 1.6 v1.1; issue #17).
    row = (
        await session.execute(
            text(
                f"SELECT {_COLS} FROM loan_applications "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan application {application_id} not found")
    return _row_to_application(row)


async def list_applications(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    stage: ApplicationStage | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[ApplicationRecord], str | None]:
    """Keyset-paginated listing, newest first (gate 1.3)."""
    limit = max(1, min(limit, 100))
    clauses: list[str] = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if stage is not None:
        clauses.append("stage = :stage")
        params["stage"] = stage.value
    if cursor:
        params["c_ts"], params["c_id"] = parse_created_id_cursor(cursor, entity="application")
        clauses.append("(created_at, id) < (:c_ts, CAST(:c_id AS uuid))")
    where = f"WHERE {' AND '.join(clauses)} "
    # Static fragments chosen in code; all values are bound parameters.
    rows = (
        await session.execute(
            text(
                f"SELECT created_at, {_COLS} FROM loan_applications "  # noqa: S608
                f"{where}"
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_application(r[1:]) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = build_created_id_cursor(last[0], last[1])
    return items, next_cursor


async def transition_stage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    application_id: uuid.UUID,
    *,
    version: int,
    target: ApplicationStage,
    system_actor: bool = False,
) -> ApplicationRecord:
    """Move an application through the P6 machine under a row lock.

    Ratifying (forward) moves are additionally capped by the tenant's
    approval-authority bands (P13.7): the matrix is read from current
    committed config AFTER the application row lock is taken, so a
    config change mid-workflow governs future transitions only — moves
    already committed under the old config are never revisited (v1.1
    rule 3). Rejection stays uncapped: it moves no money.

    Deny by default (review R1): the band guard binds every attributed
    actor. An unattributed service-level caller must OPT IN to the
    bypass by passing the keyword-only ``system_actor=True`` together
    with ``actor_id=None`` — the bypass is then recorded on the
    transition's own audit row. A bare ``actor_id=None`` without the
    flag is refused before any state is read or written, so no present
    or future job/backfill/internal path can silently ratify unlimited
    amounts by passing None.
    """
    if system_actor:
        if actor_id is not None:
            raise InvalidInputError("system_actor transitions must not carry an actor_id")
    elif actor_id is None:
        raise ForbiddenError("transition without an actor requires the explicit system_actor flag")
    if target not in API_TRANSITION_TARGETS:
        raise ConflictError(
            f"stage '{target.value}' is decided by committee voting or disbursement, "
            "not by direct transition"
        )
    row = (
        await session.execute(
            text(
                # Explicit tenant predicate on the row-lock read, on top
                # of RLS (defence in depth, gate 1.6 v1.1; issue #17).
                "SELECT stage, version, amount FROM loan_applications "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan application {application_id} not found")
    current = ApplicationStage(str(row[0]))
    try:
        transition(current, target)
    except InvalidTransitionError as exc:
        raise ConflictError(str(exc)) from exc
    if actor_id is not None and target is not ApplicationStage.REJECTED:
        # P13.7 authority bands, enforced under the row lock above.
        # actor_id can be None here ONLY via the explicit system_actor
        # bypass validated at function entry (review R1).
        await enforce_authority_band(session, tenant_id, actor_id, Decimal(str(row[2])))
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE loan_applications SET stage = :st, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {"st": target.value, "id": str(application_id), "tid": str(tenant_id), "ver": version},
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for application {application_id}")
    after_payload: dict[str, object] = {"stage": target.value}
    if system_actor:
        # The band-guard bypass is deliberate and leaves evidence: the
        # transition's audit row records it (review R1).
        after_payload["system_actor"] = True
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="application.stage",
        entity="loan_applications",
        entity_id=str(application_id),
        before={"stage": current.value},
        after=after_payload,
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="loan.application_stage_changed",
        payload={
            "application_id": str(application_id),
            "from": current.value,
            "to": target.value,
        },
    )
    if target is ApplicationStage.REJECTED:
        # Terminal rejection frees the guarantors' capacity immediately.
        await _release_application_pledges(session, tenant_id, actor_id, application_id)
    return await get_application(session, tenant_id, application_id)


async def cast_vote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    voter_id: uuid.UUID,
    application_id: uuid.UUID,
    vote: Vote,
) -> VoteTally:
    """Record a committee vote; quorum decides the application (gate 1.4).

    The application row lock serialises voters, so tallies and the
    resulting decision are race-free. The UNIQUE constraint makes
    double-voting impossible even outside this code path.

    P13.7: the quorum is read from tenant configuration AT VOTE TIME,
    inside this transaction and under the row lock (fallback: the
    code-owned COMMITTEE_QUORUM). A quorum change between votes governs
    the NEXT vote's tally only — votes already tallied never decide
    retroactively, because a decision can only ever be produced by a
    vote event (v1.1 rule 3). Approve votes are additionally capped by
    the approval-authority bands: approving is the ratifying act, while
    a reject vote moves no money and stays uncapped.
    """
    row = (
        await session.execute(
            text(
                # Explicit tenant predicate on the row-lock read, on top
                # of RLS (defence in depth, gate 1.6 v1.1; issue #17).
                "SELECT stage, amount FROM loan_applications "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan application {application_id} not found")
    current = ApplicationStage(str(row[0]))
    if current is not ApplicationStage.COMMITTEE:
        raise ConflictError(f"voting is only open in committee stage, not '{current.value}'")
    if vote is Vote.APPROVE:
        # P13.7 authority bands, enforced under the row lock above.
        await enforce_authority_band(session, tenant_id, voter_id, Decimal(str(row[1])))
    try:
        await session.execute(
            text(
                "INSERT INTO committee_votes "
                "(tenant_id, application_id, voter_id, vote) "
                "VALUES (CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:vid AS uuid), :vote)"
            ),
            {
                "tid": str(tenant_id),
                "aid": str(application_id),
                "vid": str(voter_id),
                "vote": vote.value,
            },
        )
    except IntegrityError as exc:
        raise ConflictError("committee member has already voted on this application") from exc
    tally_rows = (
        await session.execute(
            text(
                "SELECT vote, count(*) FROM committee_votes "
                "WHERE application_id = CAST(:aid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) GROUP BY vote"
            ),
            {"aid": str(application_id), "tid": str(tenant_id)},
        )
    ).all()
    counts = {str(r[0]): int(r[1]) for r in tally_rows}
    approvals = counts.get(Vote.APPROVE.value, 0)
    rejections = counts.get(Vote.REJECT.value, 0)
    await record_audit(
        session,
        tenant_id,
        voter_id,
        action="application.vote",
        entity="loan_applications",
        entity_id=str(application_id),
        after={"vote": vote.value, "approvals": approvals, "rejections": rejections},
    )
    # Config read at vote time, under the application row lock (P13.7).
    decision = decide(approvals, rejections, quorum=await committee_quorum(session, tenant_id))
    stage: ApplicationStage = current
    if decision is not None:
        target = (
            ApplicationStage.APPROVED
            if decision is Decision.APPROVED
            else ApplicationStage.REJECTED
        )
        transition(current, target)
        await session.execute(
            text(
                "UPDATE loan_applications SET stage = :st, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"st": target.value, "id": str(application_id), "tid": str(tenant_id)},
        )
        stage = target
        await record_audit(
            session,
            tenant_id,
            voter_id,
            action="application.decided",
            entity="loan_applications",
            entity_id=str(application_id),
            before={"stage": current.value},
            after={
                "stage": target.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="loan.application_decided",
            payload={
                "application_id": str(application_id),
                "decision": decision.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
        if target is ApplicationStage.REJECTED:
            # Quorum rejection is terminal: free the pledged capacity.
            await _release_application_pledges(session, tenant_id, voter_id, application_id)
    return VoteTally(
        approvals=approvals,
        rejections=rejections,
        decision=decision,
        stage=stage,
    )


async def recompute_cover(
    session: AsyncSession, tenant_id: uuid.UUID, application_id: uuid.UUID
) -> Decimal:
    """Refresh the derived cover%% after guarantee changes.

    cover_pct is derived data, so this deliberately does not bump the
    optimistic version - it must never invalidate a concurrent edit.
    Every read and the write carry an explicit tenant predicate on top
    of RLS (defence in depth, gate 1.6 v1.1; issue #17).
    """
    row = (
        await session.execute(
            text(
                "SELECT member_id, amount FROM loan_applications "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan application {application_id} not found")
    member_id = uuid.UUID(str(row[0]))
    amount = Decimal(str(row[1]))
    deposits = await _deposit_balance(session, tenant_id, member_id)
    guarantees = await guarantee_total(session, tenant_id, application_id)
    cover = _cover_pct(deposits, guarantees, amount)
    await session.execute(
        text(
            "UPDATE loan_applications SET cover_pct = :c "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"c": str(cover), "id": str(application_id), "tid": str(tenant_id)},
    )
    return cover


async def application_max_eligible(
    session: AsyncSession, tenant_id: uuid.UUID, record: ApplicationRecord
) -> Decimal:
    """max_eligible = deposits x product multiplier + live guarantees.

    The issue #15 read model: the committee sees the cap the P7
    disbursement gate will enforce. A display read (no locks) — the
    binding check re-verifies under the full lock set at disbursement.
    """
    product = await get_product(session, tenant_id, record.product_id)
    deposits = await _deposit_balance(session, tenant_id, record.member_id)
    guarantees = await guarantee_total(session, tenant_id, record.id)
    return to_cents(deposits * product.deposit_multiplier) + guarantees
