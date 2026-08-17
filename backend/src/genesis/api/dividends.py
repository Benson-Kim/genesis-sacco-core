"""Dividends & share lifecycle endpoints (P13.11, gate 1.6).

Every route carries a RequirePermission dependency (deny-by-default);
mutations are idempotent via the Idempotency-Key middleware (gate 1.4).

Permission gates (P4 matrix, decided and documented):

  * declare — transactions x EDIT: initiating a dividend declaration
    is back-office ledger governance, the POST /jobs/deposit-interest
    precedent — deliberately not transactions:create, which would let
    counter Tellers open tenant-wide payout workflows.
  * vote / void / distribute — transactions x APPROVE: the committee
    decision and the mass money-mover belong to approve-holders
    (System Admin, Branch Manager, Credit Committee per the P4 seed).
    Role-level separation of duties is not available (the matrix
    grants edit and approve to overlapping roles), so the compensating
    controls are user-level and server-side: the declaring user can
    never VOTE on nor DISTRIBUTE their own declaration (403, the P12
    precedent), and the decision needs the configured quorum (P9
    machinery).
  * view / list — transactions x VIEW.
  * share transfer — members x APPROVE: it moves member equity, the
    P12 settlement-posting precedent (deliberately not members:edit,
    which covers non-money lifecycle changes).

Money parameters NEVER travel in request bodies (v1.1 rule 1): rates
and the financial-year period are resolved server-side from tenant
configuration and the persisted approval snapshot; extra="forbid"
turns a caller-supplied rate, period or total into a 422. The share
transfer amount is the operation's subject (like a deposit amount),
bounded and 2dp-validated at the contract.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import dividends as dividends_service
from genesis.application.auth import AuthContext
from genesis.domain.committee import Vote
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["dividends"])

_txn_view = RequirePermission(Module.TRANSACTIONS, Action.VIEW)
_txn_edit = RequirePermission(Module.TRANSACTIONS, Action.EDIT)
_txn_approve = RequirePermission(Module.TRANSACTIONS, Action.APPROVE)
_members_approve = RequirePermission(Module.MEMBERS, Action.APPROVE)

TxnViewCtx = Annotated[AuthContext, Depends(_txn_view)]
TxnEditCtx = Annotated[AuthContext, Depends(_txn_edit)]
TxnApproveCtx = Annotated[AuthContext, Depends(_txn_approve)]
MembersApproveCtx = Annotated[AuthContext, Depends(_members_approve)]


class DeclareBody(BaseModel):
    """Only the batch size is caller-tunable: rates and the financial
    year come exclusively from tenant configuration (v1.1 rule 1) —
    extra="forbid" rejects any attempt to supply them (422)."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=dividends_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class DividendVoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: Vote


class DeclarationVoidBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class DistributeBody(BaseModel):
    """No money fields, ever: every figure derives from the persisted
    approval snapshot; extra="forbid" -> 422 (v1.1 rule 1)."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=dividends_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class ShareTransferBody(BaseModel):
    """The transferee and the subject amount only (2dp enforced at the
    contract; the balance check runs server-side under the locks)."""

    model_config = ConfigDict(extra="forbid")

    to_member_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=2)


class DeclarationOut(BaseModel):
    id: str
    fy_start: str
    fy_end: str
    dividend_rate_pct: str
    rebate_rate_pct: str
    eligible_members: int
    total_share_basis: str
    total_deposit_basis: str
    total_dividend: str
    total_rebate: str
    total_payout: str
    status: str
    decided_at: str | None
    distributed_at: str | None
    version: int
    created_at: str


class DeclarationListResponse(BaseModel):
    items: list[DeclarationOut]
    next_cursor: str | None


class DividendVoteResultOut(BaseModel):
    approvals: int
    rejections: int
    decision: str | None
    status: str


class DistributionRunOut(BaseModel):
    scanned: int
    claimed: int
    skipped_zero: int
    skipped_claimed: int
    dividend_total: str
    rebate_total: str
    payout_total: str
    pending_members: int
    batches: int
    status: str


class ShareTransferOut(BaseModel):
    transfer_id: str
    out_txn_ref: str
    in_txn_ref: str
    amount: str
    from_balance_after: str
    to_balance_after: str


def _declaration_out(record: dividends_service.DeclarationRecord) -> DeclarationOut:
    return DeclarationOut(
        id=str(record.id),
        fy_start=record.fy_start.isoformat(),
        fy_end=record.fy_end.isoformat(),
        dividend_rate_pct=str(record.dividend_rate_pct),
        rebate_rate_pct=str(record.rebate_rate_pct),
        eligible_members=record.eligible_members,
        total_share_basis=str(record.total_share_basis),
        total_deposit_basis=str(record.total_deposit_basis),
        total_dividend=str(record.total_dividend),
        total_rebate=str(record.total_rebate),
        total_payout=str(record.total_payout),
        status=record.status.value,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        distributed_at=record.distributed_at.isoformat() if record.distributed_at else None,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


@router.post("/dividends/declarations", status_code=201)
async def declare_dividend(body: DeclareBody, ctx: TxnEditCtx) -> DeclarationOut:
    """Declare a dividend for the last completed financial year."""
    factory = get_sessionmaker(get_settings().database_url)
    record = await dividends_service.declare_dividend(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        ctx.user_id,
        batch_size=body.batch_size,
    )
    return _declaration_out(record)


@router.get("/dividends/declarations")
async def list_declarations(
    ctx: TxnViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DeclarationListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await dividends_service.list_declarations(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return DeclarationListResponse(
        items=[_declaration_out(record) for record in items], next_cursor=next_cursor
    )


@router.get("/dividends/declarations/{declaration_id}")
async def get_declaration(declaration_id: uuid.UUID, ctx: TxnViewCtx) -> DeclarationOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await dividends_service.get_declaration(session, ctx.tenant_id, declaration_id)
    return _declaration_out(record)


@router.post("/dividends/declarations/{declaration_id}/votes", status_code=201)
async def cast_vote(
    declaration_id: uuid.UUID, body: DividendVoteBody, ctx: TxnApproveCtx
) -> DividendVoteResultOut:
    """Committee vote (P9 machinery); quorum resolved at vote time."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        tally = await dividends_service.cast_dividend_vote(
            session, ctx.tenant_id, ctx.user_id, declaration_id, body.vote
        )
    return DividendVoteResultOut(
        approvals=tally.approvals,
        rejections=tally.rejections,
        decision=tally.decision.value if tally.decision else None,
        status=tally.status.value,
    )


@router.post("/dividends/declarations/{declaration_id}/void")
async def void_declaration(
    declaration_id: uuid.UUID, body: DeclarationVoidBody, ctx: TxnApproveCtx
) -> DeclarationOut:
    """Void a drifted or withdrawn declaration (frees the FY slot)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await dividends_service.void_declaration(
            session, ctx.tenant_id, ctx.user_id, declaration_id, version=body.version
        )
    return _declaration_out(record)


@router.post("/dividends/declarations/{declaration_id}/distribution")
async def distribute(
    declaration_id: uuid.UUID, body: DistributeBody, ctx: TxnApproveCtx
) -> DistributionRunOut:
    """Run the distribution job for an approved declaration (idempotent)."""
    factory = get_sessionmaker(get_settings().database_url)
    result = await dividends_service.distribute_dividend(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        ctx.user_id,
        declaration_id,
        batch_size=body.batch_size,
    )
    return DistributionRunOut(
        scanned=result.scanned,
        claimed=result.claimed,
        skipped_zero=result.skipped_zero,
        skipped_claimed=result.skipped_claimed,
        dividend_total=str(result.dividend_total),
        rebate_total=str(result.rebate_total),
        payout_total=str(result.payout_total),
        pending_members=result.pending_members,
        batches=result.batches,
        status=result.status.value,
    )


@router.post("/members/{member_id}/share-transfers", status_code=201)
async def transfer_shares(
    member_id: uuid.UUID, body: ShareTransferBody, ctx: MembersApproveCtx
) -> ShareTransferOut:
    """Transfer share capital to an active member (the exit path)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await dividends_service.transfer_shares(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            to_member_id=body.to_member_id,
            amount=body.amount,
        )
    return ShareTransferOut(
        transfer_id=str(result.transfer_id),
        out_txn_ref=result.out_txn_ref,
        in_txn_ref=result.in_txn_ref,
        amount=str(result.amount),
        from_balance_after=str(result.from_balance_after),
        to_balance_after=str(result.to_balance_after),
    )
