"""Dividends & share lifecycle endpoints (least disclosure).

Every route carries a RequirePermission dependency (deny-by-default);
mutations are idempotent via the Idempotency-Key middleware (concurrency safety).

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
    never VOTE on nor DISTRIBUTE their own declaration (403, the precedent), and the decision needs
    the configured quorum (P9
    machinery).
  * view / list — transactions x VIEW.
  * share transfer request / approval / rejection — members x
    APPROVE: it moves member equity, the settlement-posting
    precedent (deliberately not members:edit, which covers non-money
    lifecycle changes). TWO-PHASE maker-checker since
     (the human-review HIGH finding): the
    request creates a PENDING transfer bound to a persisted snapshot;
    a DISTINCT, non-assurance checker approves or rejects (server-side
    SoD + the 0040 DB CHECK). Same-permission different-principal is
    the house posture (the /0031 precedent: the P4 matrix grants overlapping role permissions, so
    separation is per USER, server-side).
  * share-transfer register / by-id read — members x VIEW (the house read-split: corrections
  registers sit under corrections:view while their mutations need create/approve).
    Least disclosure: bare UUIDs and verbatim decimal strings — the
    request-time balance snapshot is deliberately NOT serialised.

Money parameters NEVER travel in request bodies: rates
and the financial-year period are resolved server-side from tenant
configuration and the persisted approval snapshot; extra="forbid"
turns a caller-supplied rate, period or total into a 422. The share
transfer amount is the operation's subject (like a deposit amount),
bounded and 2dp-validated at the contract; the approval and rejection
bodies carry NO money at all (figures derive from the persisted
pending row).
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
_members_view = RequirePermission(Module.MEMBERS, Action.VIEW)
_members_approve = RequirePermission(Module.MEMBERS, Action.APPROVE)

TxnViewCtx = Annotated[AuthContext, Depends(_txn_view)]
TxnEditCtx = Annotated[AuthContext, Depends(_txn_edit)]
TxnApproveCtx = Annotated[AuthContext, Depends(_txn_approve)]
MembersViewCtx = Annotated[AuthContext, Depends(_members_view)]
MembersApproveCtx = Annotated[AuthContext, Depends(_members_approve)]


class DeclareBody(BaseModel):
    """Only the batch size is caller-tunable: rates and the financial
    year come exclusively from tenant configuration —
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

    approval snapshot; extra="forbid" -> 422."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=dividends_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class ShareTransferBody(BaseModel):
    """The transferee and the subject amount only (2dp enforced at the
    contract; the balance check runs server-side under the locks)."""

    model_config = ConfigDict(extra="forbid")

    to_member_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=2)


class ShareTransferApproveBody(BaseModel):
    """NO money fields, ever: every figure derives from

    the persisted pending transfer; extra="forbid" -> 422."""

    model_config = ConfigDict(extra="forbid")


class ShareTransferRejectBody(BaseModel):
    """Version-pinned rejection with the MANDATORY checker rationale

    (the F2 posture); workflow metadata only, never money."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


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
    #: Declarer attribution (the human-
    #: authorized read-contract expansion): the EXISTING 0020 column
    #: that already drives the server's declarer-cannot-vote/distribute
    #: 403s, exposed as the bare staff UUID under least disclosure (the
    #: / exits precedent — resolving it stays behind
    #: access_control). Nullable, never invented: a NULL (system-actor
    #: declaration) serialises as an honest null.
    requested_by: str | None
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
    #: Mid-run-exited members disposed as unclaimed by this run
    #: (P3); exact figures live in the audit rows.
    unclaimed: int
    dividend_total: str
    rebate_total: str
    payout_total: str
    pending_members: int
    batches: int
    status: str


class ShareTransferOut(BaseModel):
    """The APPROVAL result — the money actually moved (phase 2)."""

    transfer_id: str
    out_txn_ref: str
    in_txn_ref: str
    amount: str
    from_balance_after: str
    to_balance_after: str


class ShareTransferRecordOut(BaseModel):
    """The workflow record: the register row and
    the request/rejection responses. Least disclosure: bare UUIDs (the / precedent — resolving them
    stays behind the entitled modules), the amount as the verbatim decimal string, and NO
    request-time balance snapshot (the approval re-verifies it
    server-side; the audit rows carry the exact figures). Maker and
    checker attribution are nullable-never-optional server truth: a
    NULL (pre-0040 history) serialises as an honest null, never an
    invented principal."""

    id: str
    from_member_id: str
    to_member_id: str
    amount: str
    status: str
    out_transaction_id: str | None
    in_transaction_id: str | None
    created_by: str | None
    approved_by: str | None
    decided_at: str | None
    version: int
    created_at: str


class ShareTransferListOut(BaseModel):
    items: list[ShareTransferRecordOut]
    next_cursor: str | None


def _transfer_record_out(record: dividends_service.ShareTransferRecord) -> ShareTransferRecordOut:
    return ShareTransferRecordOut(
        id=str(record.id),
        from_member_id=str(record.from_member_id),
        to_member_id=str(record.to_member_id),
        amount=str(record.amount),
        status=record.status.value,
        out_transaction_id=str(record.out_transaction_id) if record.out_transaction_id else None,
        in_transaction_id=str(record.in_transaction_id) if record.in_transaction_id else None,
        created_by=str(record.created_by) if record.created_by else None,
        approved_by=str(record.approved_by) if record.approved_by else None,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


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
        requested_by=str(record.requested_by) if record.requested_by else None,
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
        unclaimed=result.unclaimed,
        dividend_total=str(result.dividend_total),
        rebate_total=str(result.rebate_total),
        payout_total=str(result.payout_total),
        pending_members=result.pending_members,
        batches=result.batches,
        status=result.status.value,
    )


@router.post("/members/{member_id}/share-transfers", status_code=201)
async def request_share_transfer(
    member_id: uuid.UUID, body: ShareTransferBody, ctx: MembersApproveCtx
) -> ShareTransferRecordOut:
    """MAKER phase ((l)): create a PENDING transfer bound to
    the persisted approval snapshot — NO money moves until a distinct
    checker approves."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await dividends_service.request_share_transfer(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            to_member_id=body.to_member_id,
            amount=body.amount,
        )
    return _transfer_record_out(record)


@router.get("/share-transfers")
async def list_share_transfers(
    ctx: MembersViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ShareTransferListOut:
    """The share-transfer history register:
    keyset, PENDING FIRST then newest first — the checker's job order
    (the 0038 band pattern). Served under members:view (the house
    read-split); explicit tenant predicate doubling RLS; bound
    parameters only."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await dividends_service.list_share_transfers(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return ShareTransferListOut(
        items=[_transfer_record_out(record) for record in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/share-transfers/{transfer_id}")
async def get_share_transfer(transfer_id: uuid.UUID, ctx: MembersViewCtx) -> ShareTransferRecordOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await dividends_service.get_share_transfer(session, ctx.tenant_id, transfer_id)
    return _transfer_record_out(record)


@router.post("/share-transfers/{transfer_id}/approval", status_code=201)
async def approve_share_transfer(
    transfer_id: uuid.UUID, body: ShareTransferApproveBody, ctx: MembersApproveCtx
) -> ShareTransferOut:
    """CHECKER phase ((l)): a DISTINCT, non-assurance
    principal re-verifies the snapshot under the full lock set (409 on
    drift, posting nothing), then posts BOTH ledger legs, updates both
    balances and notifies BOTH members via the outbox — atomically."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await dividends_service.approve_share_transfer(
            session, ctx.tenant_id, ctx.user_id, transfer_id
        )
    return ShareTransferOut(
        transfer_id=str(result.transfer_id),
        out_txn_ref=result.out_txn_ref,
        in_txn_ref=result.in_txn_ref,
        amount=str(result.amount),
        from_balance_after=str(result.from_balance_after),
        to_balance_after=str(result.to_balance_after),
    )


@router.post("/share-transfers/{transfer_id}/rejection")
async def reject_share_transfer(
    transfer_id: uuid.UUID, body: ShareTransferRejectBody, ctx: MembersApproveCtx
) -> ShareTransferRecordOut:
    """Reject a pending transfer (checker decision, optimistic-locked)
    — the checker's rationale is REQUIRED and recorded in the
    audit row, never echoed."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await dividends_service.reject_share_transfer(
            session,
            ctx.tenant_id,
            ctx.user_id,
            transfer_id,
            version=body.version,
            reason=body.reason,
        )
    return _transfer_record_out(record)
