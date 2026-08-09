"""Ledger corrections, misc fees & loan write-off endpoints.

Every route carries a RequirePermission dependency on the DEDICATED
corrections module (maker-checker; deny-by-default, least disclosure) —
never generic transactions:edit; mutations are idempotent via the
Idempotency-Key middleware (concurrency safety), whose stored responses are
actor-scoped (the lesson: a cross-actor replay misses).

Permission gates (P4 matrix extension, decided and documented):

  * adjustment request / fee / write-off request — corrections x
    CREATE: the MAKER actions (Accountant, System Admin per the seed).
  * adjustment approval / rejection, write-off vote / void / posting —
    corrections x APPROVE: the CHECKER actions (Branch Manager, Credit
    Committee, System Admin). User-level separation of duties is
    server-side on top AND at the database: the maker of
    an adjustment can never check it (the 0031 SoD CHECK), assurance
    roles can never be checker (B2), and the requester of a
    write-off can never vote on nor post it.
  * adjustment / write-off view — corrections x VIEW.

Money parameters NEVER travel in request bodies:
fee amounts resolve server-side from configuration (the request
names a code-owned fee type only), adjustment figures derive from the
original transaction's append-only legs, and write-off figures come
from the persisted write-once snapshot; extra="forbid" turns any
caller-supplied amount into a 422. Corrections post with occurred_at
= NOW, server-resolved — no body carries a date.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.api.params import require_cash_channel
from genesis.application import corrections as corrections_service
from genesis.application.auth import AuthContext
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["corrections"])

_corrections_view = RequirePermission(Module.CORRECTIONS, Action.VIEW)
_corrections_create = RequirePermission(Module.CORRECTIONS, Action.CREATE)
_corrections_approve = RequirePermission(Module.CORRECTIONS, Action.APPROVE)

CorrectionsViewCtx = Annotated[AuthContext, Depends(_corrections_view)]
CorrectionsCreateCtx = Annotated[AuthContext, Depends(_corrections_create)]
CorrectionsApproveCtx = Annotated[AuthContext, Depends(_corrections_approve)]


class AdjustmentBody(BaseModel):
    """The repayment being undone and the reason — NO amounts, ever
    (rule 1): the reversal derives every figure from the
    original transaction's append-only legs."""

    model_config = ConfigDict(extra="forbid")

    repayment_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


class AdjustmentApproveBody(BaseModel):
    """Deliberately empty: the checker approves the
    PERSISTED snapshot — every figure comes from the pending
    adjustment row; extra="forbid" -> 422 on any
    caller-supplied field."""

    model_config = ConfigDict(extra="forbid")


class AdjustmentRejectBody(BaseModel):
    """The checker's decision: the optimistic version plus the REQUIRED
    rejection rationale (four-eyes practice puts the checker's reason on the record, especially
    since a rejected slot frees for a fresh request). The reason is workflow metadata, never
    a money parameter (is not implicated); it lands in the
    audit `after` payload, never in error envelopes (rule 7)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class AdjustmentRecordOut(BaseModel):
    """One adjustment workflow row: the pending request a

    checker reviews, or the terminal posted/rejected history."""

    id: str
    repayment_id: str
    loan_id: str
    original_transaction_id: str
    reversal_transaction_id: str | None
    maker_id: str
    checker_id: str | None
    reason: str
    amount: str
    penalties: str
    interest: str
    principal: str
    would_reopen_loan: bool
    status: str
    loan_balance_at_request: str | None
    loan_penalty_due_at_request: str | None
    loan_status_at_request: str | None
    decided_at: str | None
    version: int
    created_at: str


class AdjustmentListOut(BaseModel):
    """Keyset page of the pending-adjustments checker register (the human-authorized read-contract
    expansion):
    the same rows the by-id read serialises, pending-first."""

    items: list[AdjustmentRecordOut]
    next_cursor: str | None


class AdjustmentOut(BaseModel):
    adjustment_id: str
    reversal_txn_id: str
    reversal_txn_ref: str
    amount: str
    penalties: str
    interest: str
    principal: str
    balance_after: str
    penalty_due_after: str
    loan_status: str
    reopened: bool


class FeeBody(BaseModel):
    """A code-owned fee type and the cash channel — the AMOUNT resolves
    exclusively from tenant configuration server-side:
    extra="forbid" turns a caller-supplied amount into a 422."""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID
    fee_type: corrections_service.FeeType
    channel: Channel


class FeeOut(BaseModel):
    txn_id: str
    txn_ref: str
    fee_type: str
    amount: str


class WriteOffRequestBody(BaseModel):
    """The loan and the reason — the snapshot figures are read under
    the loan row lock server-side, never supplied (v1.1 rules 1/3)."""

    model_config = ConfigDict(extra="forbid")

    loan_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


class WriteOffVoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: Vote


class WriteOffVoidBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class WriteOffPostBody(BaseModel):
    """Deliberately empty: every figure comes from the persisted
    write-once snapshot; extra="forbid" -> 422 on any
    caller-supplied field."""

    model_config = ConfigDict(extra="forbid")


class WriteOffOut(BaseModel):
    id: str
    loan_id: str
    member_id: str
    balance: str
    penalty_due: str
    total_written_off: str
    classification: str
    provision_pct: str
    reason: str
    status: str
    decided_at: str | None
    posted_at: str | None
    transaction_id: str | None
    version: int
    created_at: str


class WriteOffListOut(BaseModel):
    """Keyset page of the write-off committee register (the human-authorized read-contract
    expansion):
    the same rows the by-id read serialises, live-first."""

    items: list[WriteOffOut]
    next_cursor: str | None


class WriteOffVoteResultOut(BaseModel):
    approvals: int
    rejections: int
    decision: str | None
    status: str


class WriteOffPostOut(BaseModel):
    write_off_id: str
    loan_id: str
    txn_id: str | None
    txn_ref: str | None
    total_written_off: str
    status: str


class RecoveryReceiptBody(BaseModel):
    """The cash actually received and its channel. The
    CLAIM figures — total_written_off and the receipts already
    recorded — are server-resolved from the write-once snapshot and
    the append-only loan_recoveries rows under the write-off row lock;
    a receipt exceeding the outstanding claim is a 409. extra="forbid"
    turns any other caller-supplied field into a 422."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=2)
    channel: Channel


class RecoveryReceiptOut(BaseModel):
    recovery_id: str
    write_off_id: str
    loan_id: str
    member_id: str
    txn_id: str
    txn_ref: str
    amount: str
    recovered_total: str
    outstanding_claim: str
    claim_fully_recovered: bool
    guarantees_released: int
    recovery_case_id: str | None


class RecoveryReceiptRowOut(BaseModel):
    id: str
    amount: str
    txn_id: str
    recovery_case_id: str | None
    recorded_by: str
    created_at: str


class RecoveryReceiptsOut(BaseModel):
    items: list[RecoveryReceiptRowOut]
    recovered_total: str
    outstanding_claim: str
    next_cursor: str | None


def _write_off_out(record: corrections_service.WriteOffRecord) -> WriteOffOut:
    return WriteOffOut(
        id=str(record.id),
        loan_id=str(record.loan_id),
        member_id=str(record.member_id),
        balance=str(record.balance),
        penalty_due=str(record.penalty_due),
        total_written_off=str(record.total_written_off),
        classification=record.classification,
        provision_pct=str(record.provision_pct),
        reason=record.reason,
        status=record.status.value,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        posted_at=record.posted_at.isoformat() if record.posted_at else None,
        transaction_id=str(record.transaction_id) if record.transaction_id else None,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


def _adjustment_out(record: corrections_service.AdjustmentRecord) -> AdjustmentRecordOut:
    return AdjustmentRecordOut(
        id=str(record.id),
        repayment_id=str(record.repayment_id),
        loan_id=str(record.loan_id),
        original_transaction_id=str(record.original_transaction_id),
        reversal_transaction_id=(
            str(record.reversal_transaction_id) if record.reversal_transaction_id else None
        ),
        maker_id=str(record.maker_id),
        checker_id=str(record.checker_id) if record.checker_id else None,
        reason=record.reason,
        amount=str(record.amount),
        penalties=str(record.penalties),
        interest=str(record.interest),
        principal=str(record.principal),
        would_reopen_loan=record.reopened_loan,
        status=record.status.value,
        loan_balance_at_request=(
            str(record.loan_balance_at_request)
            if record.loan_balance_at_request is not None
            else None
        ),
        loan_penalty_due_at_request=(
            str(record.loan_penalty_due_at_request)
            if record.loan_penalty_due_at_request is not None
            else None
        ),
        loan_status_at_request=record.loan_status_at_request,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


@router.post("/corrections/repayment-adjustments", status_code=201)
async def request_repayment_adjustment(
    body: AdjustmentBody, ctx: CorrectionsCreateCtx
) -> AdjustmentRecordOut:
    """MAKER phase: create a PENDING adjustment bound to
    the persisted approval snapshot — nothing posts until a distinct
    checker approves."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.request_repayment_adjustment(
            session, ctx.tenant_id, ctx.user_id, body.repayment_id, reason=body.reason
        )
    return _adjustment_out(record)


@router.get("/corrections/repayment-adjustments")
async def list_repayment_adjustments(
    ctx: CorrectionsViewCtx,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdjustmentListOut:
    """The pending-adjustments checker register: keyset, PENDING FIRST then newest first — the
    checker's
    job order. Served under the existing corrections view gate;
    explicit tenant predicate doubling RLS; bound parameters only."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await corrections_service.list_adjustments(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return AdjustmentListOut(
        items=[_adjustment_out(record) for record in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/corrections/repayment-adjustments/{adjustment_id}")
async def get_repayment_adjustment(
    adjustment_id: uuid.UUID, ctx: CorrectionsViewCtx
) -> AdjustmentRecordOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.get_adjustment(session, ctx.tenant_id, adjustment_id)
    return _adjustment_out(record)


@router.post("/corrections/repayment-adjustments/{adjustment_id}/approval", status_code=201)
async def approve_repayment_adjustment(
    adjustment_id: uuid.UUID, body: AdjustmentApproveBody, ctx: CorrectionsApproveCtx
) -> AdjustmentOut:
    """CHECKER phase: re-verify the snapshot under the full
    lock set (409 on drift, posting nothing), then reverse the
    repayment's complete allocation and restore loan state."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await corrections_service.approve_repayment_adjustment(
            session, ctx.tenant_id, ctx.user_id, adjustment_id
        )
    return AdjustmentOut(
        adjustment_id=str(result.adjustment_id),
        reversal_txn_id=str(result.reversal_txn_id),
        reversal_txn_ref=result.reversal_txn_ref,
        amount=str(result.amount),
        penalties=str(result.penalties),
        interest=str(result.interest),
        principal=str(result.principal),
        balance_after=str(result.balance_after),
        penalty_due_after=str(result.penalty_due_after),
        loan_status=result.status.value,
        reopened=result.reopened,
    )


@router.post("/corrections/repayment-adjustments/{adjustment_id}/reject")
async def reject_repayment_adjustment(
    adjustment_id: uuid.UUID, body: AdjustmentRejectBody, ctx: CorrectionsApproveCtx
) -> AdjustmentRecordOut:
    """Reject a pending adjustment (checker decision, optimistic-locked)
    — frees the one-live-adjustment slot for a fresh request. The
    checker's rationale is REQUIRED and recorded in the audit
    row."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.reject_repayment_adjustment(
            session,
            ctx.tenant_id,
            ctx.user_id,
            adjustment_id,
            version=body.version,
            reason=body.reason,
        )
    return _adjustment_out(record)


@router.post("/corrections/fees", status_code=201)
async def post_fee(body: FeeBody, ctx: CorrectionsCreateCtx) -> FeeOut:
    """Post a configured misc fee against a member (FE- ref)."""
    channel = require_cash_channel(body.channel)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await corrections_service.post_misc_fee(
            session,
            ctx.tenant_id,
            ctx.user_id,
            body.member_id,
            fee_type=body.fee_type,
            channel=channel,
        )
    return FeeOut(
        txn_id=str(result.txn_id),
        txn_ref=result.txn_ref,
        fee_type=result.fee_type.value,
        amount=str(result.amount),
    )


@router.post("/corrections/write-offs", status_code=201)
async def request_write_off(body: WriteOffRequestBody, ctx: CorrectionsCreateCtx) -> WriteOffOut:
    """Persist the write-once write-off snapshot for committee approval."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.request_write_off(
            session, ctx.tenant_id, ctx.user_id, body.loan_id, reason=body.reason
        )
    return _write_off_out(record)


@router.get("/corrections/write-offs")
async def list_write_offs(
    ctx: CorrectionsViewCtx,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WriteOffListOut:
    """The write-off committee register:
    keyset, LIVE (requested/approved) FIRST then newest first — the
    committee's job order. Served under the existing corrections view
    gate; explicit tenant predicate doubling RLS; bound parameters
    only. Vote/void/posting semantics are untouched."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await corrections_service.list_write_offs(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return WriteOffListOut(
        items=[_write_off_out(record) for record in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/corrections/write-offs/{write_off_id}")
async def get_write_off(write_off_id: uuid.UUID, ctx: CorrectionsViewCtx) -> WriteOffOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.get_write_off(session, ctx.tenant_id, write_off_id)
    return _write_off_out(record)


@router.post("/corrections/write-offs/{write_off_id}/votes", status_code=201)
async def cast_write_off_vote(
    write_off_id: uuid.UUID, body: WriteOffVoteBody, ctx: CorrectionsApproveCtx
) -> WriteOffVoteResultOut:
    """Committee vote (P9 machinery); quorum resolved at vote time."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        tally = await corrections_service.cast_write_off_vote(
            session, ctx.tenant_id, ctx.user_id, write_off_id, body.vote
        )
    return WriteOffVoteResultOut(
        approvals=tally.approvals,
        rejections=tally.rejections,
        decision=tally.decision.value if tally.decision else None,
        status=tally.status.value,
    )


@router.post("/corrections/write-offs/{write_off_id}/void")
async def void_write_off(
    write_off_id: uuid.UUID, body: WriteOffVoidBody, ctx: CorrectionsApproveCtx
) -> WriteOffOut:
    """Void a drifted or withdrawn write-off (frees the per-loan slot)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await corrections_service.void_write_off(
            session, ctx.tenant_id, ctx.user_id, write_off_id, version=body.version
        )
    return _write_off_out(record)


@router.post("/corrections/write-offs/{write_off_id}/posting", status_code=201)
async def post_write_off(
    write_off_id: uuid.UUID, body: WriteOffPostBody, ctx: CorrectionsApproveCtx
) -> WriteOffPostOut:
    """Execute an approved write-off (snapshot re-verified; 409 on drift)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await corrections_service.post_write_off(
            session, ctx.tenant_id, ctx.user_id, write_off_id
        )
    return WriteOffPostOut(
        write_off_id=str(result.write_off_id),
        loan_id=str(result.loan_id),
        txn_id=str(result.txn_id) if result.txn_id else None,
        txn_ref=result.txn_ref,
        total_written_off=str(result.total_written_off),
        status=result.status.value,
    )


@router.post("/corrections/write-offs/{write_off_id}/recoveries", status_code=201)
async def record_recovery_receipt(
    write_off_id: uuid.UUID, body: RecoveryReceiptBody, ctx: CorrectionsCreateCtx
) -> RecoveryReceiptOut:
    """Record a bad-debt recovery receipt against a posted write-off
    RC- posting + append-only receipt row, never a
    resurrection of the loan."""
    channel = require_cash_channel(body.channel)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await corrections_service.record_recovery_receipt(
            session,
            ctx.tenant_id,
            ctx.user_id,
            write_off_id,
            amount=body.amount,
            channel=channel,
        )
    return RecoveryReceiptOut(
        recovery_id=str(result.recovery_id),
        write_off_id=str(result.write_off_id),
        loan_id=str(result.loan_id),
        member_id=str(result.member_id),
        txn_id=str(result.txn_id),
        txn_ref=result.txn_ref,
        amount=str(result.amount),
        recovered_total=str(result.recovered_total),
        outstanding_claim=str(result.outstanding_claim),
        claim_fully_recovered=result.claim_fully_recovered,
        guarantees_released=result.guarantees_released,
        recovery_case_id=str(result.recovery_case_id) if result.recovery_case_id else None,
    )


@router.get("/corrections/write-offs/{write_off_id}/recoveries")
async def list_recovery_receipts(
    write_off_id: uuid.UUID,
    ctx: CorrectionsViewCtx,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RecoveryReceiptsOut:
    """Receipts recorded against one write-off claim (keyset, oldest
    first) with the reconstructed recovered/outstanding position."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await corrections_service.list_recovery_receipts(
            session, ctx.tenant_id, write_off_id, cursor=cursor, limit=limit
        )
    return RecoveryReceiptsOut(
        items=[
            RecoveryReceiptRowOut(
                id=str(r.id),
                amount=str(r.amount),
                txn_id=str(r.txn_id),
                recovery_case_id=str(r.recovery_case_id) if r.recovery_case_id else None,
                recorded_by=str(r.recorded_by),
                created_at=r.created_at.isoformat(),
            )
            for r in page.items
        ],
        recovered_total=str(page.recovered_total),
        outstanding_claim=str(page.outstanding_claim),
        next_cursor=page.next_cursor,
    )
