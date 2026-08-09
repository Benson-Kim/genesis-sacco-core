"""Member exit & settlement endpoints (least disclosure).

Every route carries a RequirePermission dependency (deny-by-default);
mutations are idempotent via the Idempotency-Key middleware (concurrency safety).

Permission gates (P4 matrix, decided and documented):

  * create request — members x EDIT: initiating an exit is a member
    lifecycle change (System Admin, Branch Manager).
  * vote / void / settle — members x APPROVE: exit governance lives in
    the members module (there is no dedicated exit module in the
    prototype matrix), so the approving committee is the set of
    members:approve holders (System Admin, Branch Manager). The matrix
    grants members:edit and members:approve to the same roles, so
    role-level separation of duties is not available; the compensating
    controls are user-level and enforced server-side: the requesting
    user can never vote on their own request, can never post its
    money-moving settlement (both 403), and the decision needs a
    two-voter quorum (P9 machinery). Voiding one's own request stays
    allowed — it moves no money and is the documented request-
    withdrawal path (see application.member_exits.void_exit). Posting
    the settlement executes an already-quorum-approved decision and
    additionally moves money, so it stays at members:approve —
    deliberately NOT transactions:create, which would let counter
    Tellers finalise a member exit — and must come from a different
    approve-holder than the initiator.
  * view / list / statement — members x VIEW.

Money parameters never travel in request bodies (caller-rate lesson): the exit fee comes exclusively
from tenant_settings and every
settlement figure is computed server-side under locks. The only
caller-chosen values are the payout channel (cash channels only) and
the optional exit reason.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.api.params import require_cash_channel
from genesis.application import member_exits as exits_service
from genesis.application.auth import AuthContext
from genesis.domain.committee import Vote
from genesis.domain.exits import ExitStatus
from genesis.domain.ledger import Channel
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/member-exits", tags=["member-exits"])

_view = RequirePermission(Module.MEMBERS, Action.VIEW)
_edit = RequirePermission(Module.MEMBERS, Action.EDIT)
_approve = RequirePermission(Module.MEMBERS, Action.APPROVE)

ViewCtx = Annotated[AuthContext, Depends(_view)]
EditCtx = Annotated[AuthContext, Depends(_edit)]
ApproveCtx = Annotated[AuthContext, Depends(_approve)]


class ExitRequestBody(BaseModel):
    """No money fields, ever: figures are computed server-side under locks."""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=200)


class ExitVoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: Vote


class ExitVoidBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class SettlementBody(BaseModel):
    """Version pins the approved snapshot; channel picks the payout rail."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    channel: Channel


class ExitOut(BaseModel):
    id: str
    member_id: str
    status: str
    reason: str | None
    shares_amount: str
    deposits_amount: str
    loan_balance: str
    fees: str
    net_payable: str
    #: Initiator attribution (R3; the honest-limitation
    #: row): the staff principal who created the request — persisted
    #: since 0010 and already driving the self-vote/self-settle 403s,
    #: now exposed so approvers can see WHO they are checking (the
    #: maker-checker record itself, not a per-tab witness). Least
    #: disclosure (least disclosure): the bare user UUID only — never a name
    #: or email; resolving it stays behind access_control:view (the
    #:  users/audit read paths). NULL only for legacy rows
    #: written without an actor (attribution is never invented).
    requested_by: str | None
    decided_at: str | None
    settled_at: str | None
    settlement_txn_id: str | None
    version: int
    created_at: str


class ExitListResponse(BaseModel):
    items: list[ExitOut]
    next_cursor: str | None


class ExitEligibilityOut(BaseModel):
    """Advisory blocker FACTS for the up-front eligibility checklist
    (the exit-eligibility criteria rows).

    Least disclosure: counts and booleans only — NEVER an
    amount; served under members:view like every other exit read.
    ADVISORY only: computed WITHOUT locks; the binding verdict stays
    with the request/settlement locked recomputes (which also enforce
    the negative-settlement rule this read cannot see).
    """

    member_id: str
    member_status: str
    status_allows_exit: bool
    open_exit_exists: bool
    live_guarantees_count: int
    open_applications_count: int
    #: Informational, NOT a blocker — active loans net within the
    #: settlement (the early-settlement rule).
    active_loans_count: int
    unresolved_writeoff_claim: bool
    advisory_eligible: bool
    as_of: str


class ExitVoteResultOut(BaseModel):
    approvals: int
    rejections: int
    decision: str | None
    status: str


class SettlementOut(BaseModel):
    exit: ExitOut
    txn_ref: str | None


class ExitStatementOut(BaseModel):
    exit_id: str
    member_id: str
    member_no: str
    member_name: str
    member_status: str
    exit_status: str
    reason: str | None
    shares_amount: str
    deposits_amount: str
    equity: str
    loan_balance: str
    fees: str
    net_payable: str
    #: Initiator attribution on the statement document itself (issue
    #:  R3; the honest-limitation row): who requested this
    #: settlement, next to the figures it produced. Least disclosure
    #: (least disclosure): the bare user UUID only — never a name or email;
    #: resolving it stays behind access_control:view. null only for
    #: legacy rows written without an actor (never invented).
    requested_by: str | None
    requested_at: str
    decided_at: str | None
    settled_at: str | None
    settlement_txn_ref: str | None


def _out(record: exits_service.ExitRecord) -> ExitOut:
    return ExitOut(
        id=str(record.id),
        member_id=str(record.member_id),
        status=record.status.value,
        reason=record.reason,
        shares_amount=str(record.shares_amount),
        deposits_amount=str(record.deposits_amount),
        loan_balance=str(record.loan_balance),
        fees=str(record.fees),
        net_payable=str(record.net_payable),
        requested_by=str(record.requested_by) if record.requested_by else None,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        settled_at=record.settled_at.isoformat() if record.settled_at else None,
        settlement_txn_id=(
            str(record.settlement_transaction_id) if record.settlement_transaction_id else None
        ),
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


@router.post("", status_code=201)
async def create_exit_request(body: ExitRequestBody, ctx: EditCtx) -> ExitOut:
    """Snapshot the settlement for committee approval (members:edit)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await exits_service.request_exit(
            session, ctx.tenant_id, ctx.user_id, body.member_id, reason=body.reason
        )
    return _out(record)


@router.get("")
async def list_exit_requests(
    ctx: ViewCtx,
    status: ExitStatus | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ExitListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await exits_service.list_exits(
            session, ctx.tenant_id, status=status, cursor=cursor, limit=limit
        )
    return ExitListResponse(items=[_out(r) for r in items], next_cursor=next_cursor)


@router.get("/eligibility/{member_id}")
async def get_exit_eligibility(member_id: uuid.UUID, ctx: ViewCtx) -> ExitEligibilityOut:
    """Advisory eligibility facts BEFORE a request is submitted
    (members:view; human-authorized expand-only, read-only contract change).

    NO row locks: the checklist mirrors the blocker set the settlement
    service enforces (same live-guarantee status set, same open-stage list, the SAME
    unresolved-write-off SQL — reuse-first), but the
    BINDING verdict remains the locked recompute at request and
    settlement time. Facts only — no amount ever travels here.
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        facts = await exits_service.exit_eligibility(session, ctx.tenant_id, member_id)
    return ExitEligibilityOut(
        member_id=str(facts.member_id),
        member_status=facts.member_status,
        status_allows_exit=facts.status_allows_exit,
        open_exit_exists=facts.open_exit_exists,
        live_guarantees_count=facts.live_guarantees_count,
        open_applications_count=facts.open_applications_count,
        active_loans_count=facts.active_loans_count,
        unresolved_writeoff_claim=facts.unresolved_writeoff_claim,
        advisory_eligible=facts.advisory_eligible,
        as_of=facts.as_of.isoformat(),
    )


@router.get("/{exit_id}")
async def get_exit_request(exit_id: uuid.UUID, ctx: ViewCtx) -> ExitOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await exits_service.get_exit(session, ctx.tenant_id, exit_id)
    return _out(record)


@router.post("/{exit_id}/votes", status_code=201)
async def vote_on_exit(
    exit_id: uuid.UUID, body: ExitVoteBody, ctx: ApproveCtx
) -> ExitVoteResultOut:
    """Committee vote (members:approve); quorum decides (P9 machinery)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        tally = await exits_service.cast_exit_vote(
            session, ctx.tenant_id, ctx.user_id, exit_id, body.vote
        )
    return ExitVoteResultOut(
        approvals=tally.approvals,
        rejections=tally.rejections,
        decision=tally.decision.value if tally.decision else None,
        status=tally.status.value,
    )


@router.post("/{exit_id}/void")
async def void_exit_request(exit_id: uuid.UUID, body: ExitVoidBody, ctx: ApproveCtx) -> ExitOut:
    """Void a drifted/withdrawn open exit (members:approve)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await exits_service.void_exit(
            session, ctx.tenant_id, ctx.user_id, exit_id, version=body.version
        )
    return _out(record)


@router.post("/{exit_id}/settlement", status_code=201)
async def post_exit_settlement(
    exit_id: uuid.UUID, body: SettlementBody, ctx: ApproveCtx
) -> SettlementOut:
    """Atomically post the approved settlement (members:approve).

    Re-verifies every snapshot component under the full lock set; any
    drift since approval returns 409 with nothing posted.
    """
    channel = require_cash_channel(body.channel)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await exits_service.post_settlement(
            session,
            ctx.tenant_id,
            ctx.user_id,
            exit_id,
            version=body.version,
            channel=channel,
        )
    return SettlementOut(exit=_out(result.exit), txn_ref=result.txn_ref)


@router.get("/{exit_id}/statement")
async def get_exit_statement(exit_id: uuid.UUID, ctx: ViewCtx) -> ExitStatementOut:
    """Exit statement document (JSON; CSV/PDF arrives with core/exports)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        doc = await exits_service.exit_statement(session, ctx.tenant_id, exit_id)
    return ExitStatementOut(
        exit_id=str(doc.exit_id),
        member_id=str(doc.member_id),
        member_no=doc.member_no,
        member_name=doc.member_name,
        member_status=doc.member_status,
        exit_status=doc.exit_status.value,
        reason=doc.reason,
        shares_amount=str(doc.shares_amount),
        deposits_amount=str(doc.deposits_amount),
        equity=str(doc.equity),
        loan_balance=str(doc.loan_balance),
        fees=str(doc.fees),
        net_payable=str(doc.net_payable),
        requested_by=str(doc.requested_by) if doc.requested_by else None,
        requested_at=doc.requested_at.isoformat(),
        decided_at=doc.decided_at.isoformat() if doc.decided_at else None,
        settled_at=doc.settled_at.isoformat() if doc.settled_at else None,
        settlement_txn_ref=doc.settlement_txn_ref,
    )
