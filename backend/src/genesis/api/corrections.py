"""Ledger corrections, misc fees & loan write-off endpoints (P13.15).

Every route carries a RequirePermission dependency on the DEDICATED
corrections module (A3 maker-checker; deny-by-default, gate 1.6) —
never generic transactions:edit; mutations are idempotent via the
Idempotency-Key middleware (gate 1.4), whose stored responses are
actor-scoped (the !29 lesson: a cross-actor replay misses).

Permission gates (P4 matrix extension, decided and documented):

  * adjustment / fee / write-off request — corrections x CREATE: the
    MAKER actions (Accountant, System Admin per the seed).
  * write-off vote / void / posting — corrections x APPROVE: the
    CHECKER actions (Branch Manager, Credit Committee, System Admin).
    User-level separation of duties is server-side on top: the
    requester of a write-off can never vote on nor post it.
  * write-off view — corrections x VIEW.

Money parameters NEVER travel in request bodies (v1.1 rule 1, FM5):
fee amounts resolve server-side from P13.7 configuration (the request
names a code-owned fee type only), adjustment figures derive from the
original transaction's append-only legs, and write-off figures come
from the persisted write-once snapshot; extra="forbid" turns any
caller-supplied amount into a 422. Corrections post with occurred_at
= NOW, server-resolved (A2) — no body carries a date.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
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
    (FM5/v1.1 rule 1): the reversal derives every figure from the
    original transaction's append-only legs."""

    model_config = ConfigDict(extra="forbid")

    repayment_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


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
    exclusively from P13.7 tenant configuration server-side (FM5):
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
    write-once snapshot (v1.1 rule 3); extra="forbid" -> 422 on any
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


@router.post("/corrections/repayment-adjustments", status_code=201)
async def adjust_repayment(body: AdjustmentBody, ctx: CorrectionsCreateCtx) -> AdjustmentOut:
    """Reverse one repayment's complete allocation and restore loan state."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await corrections_service.adjust_repayment(
            session, ctx.tenant_id, ctx.user_id, body.repayment_id, reason=body.reason
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
