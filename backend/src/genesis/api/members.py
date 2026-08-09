"""Members endpoints: CRUD, status transitions, statement (P8, least disclosure).

Every route carries a RequirePermission dependency (deny-by-default);
mutations are idempotent via the Idempotency-Key middleware (concurrency safety).
Terminal exit is owned exclusively by the P12 settlement workflow
(/member-exits): direct status changes to 'exited'
are rejected by the service with 409.
"""

from __future__ import annotations

import uuid
from datetime import date
from functools import partial
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.api.params import resolve_as_of
from genesis.application import dormancy as dormancy_service
from genesis.application import members as members_service
from genesis.application.auth import AuthContext
from genesis.domain.members import MemberStatus, MemberType
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/members", tags=["members"])

_view = RequirePermission(Module.MEMBERS, Action.VIEW)
_create = RequirePermission(Module.MEMBERS, Action.CREATE)
_edit = RequirePermission(Module.MEMBERS, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
CreateCtx = Annotated[AuthContext, Depends(_create)]
EditCtx = Annotated[AuthContext, Depends(_edit)]


class MemberCreateBody(BaseModel):
    """extra="forbid" (least disclosure): unknown fields are a 422, never
    silently dropped. dividend_payout is typed as a
    bounded STRING, deliberately not the enum: the service resolves it
    against the CODE-OWNED vocabulary and an unknown value surfaces as
    the sanitized 422 category ONLY — a pydantic enum here would echo
    the permitted values in FastAPI's structural 422 (least
    disclosure). Stored PREFERENCE only; nothing routes money by it
    (the preference-only fence)."""

    model_config = ConfigDict(extra="forbid")

    type: MemberType
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    dividend_payout: str | None = Field(default=None, max_length=32)


class MemberUpdateBody(BaseModel):
    """Versioned optimistic-lock edit body (stale = 409); extra="forbid"
    and the dividend_payout string posture per MemberCreateBody."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    dividend_payout: str | None = Field(default=None, max_length=32)


class MemberStatusBody(BaseModel):
    version: int = Field(ge=1)
    status: MemberStatus


class DormancyRunBody(BaseModel):
    """extra="forbid": the dormancy period is NEVER caller-suppliable —
    it resolves exclusively from tenant settings (v1.1 rule 1); a
    period in the body is a 422."""

    model_config = ConfigDict(extra="forbid")

    as_of: date | None = None
    batch_size: int = Field(default=dormancy_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class DormancyRunOut(BaseModel):
    as_of: str
    cutoff: str
    period_months: int
    scanned: int
    transitioned: int
    batches: int


class MemberOut(BaseModel):
    id: str
    member_no: str
    type: str
    name: str
    phone: str | None
    email: str | None
    status: str
    version: int
    #: Branch attribution — expand-only NULLABLE
    #: field: the 0016 FK written ONLY by the assignment route
    #: (PUT /branches/{id}/members/{member_id}, members:edit). NULL is
    #: the honest "unassigned" state (attribution is never invented);
    #: the bare branch UUID only — resolving the branch name stays
    #: behind settings:view (GET /branches/{id}), least disclosure.
    branch_id: str | None
    #: Dividend payout PREFERENCE — nullable, NEVER
    #: optional: the key is always serialized; null is the honest
    #: "not chosen" state clients render with an explicit affordance.
    dividend_payout: str | None


class MemberAggregatesOut(BaseModel):
    """Advisory financial aggregates on the single-member read.

    All four figures are canonical decimal strings scaled by the
    database (numeric(18,2)); clients render them verbatim and never
    compute money (no client-side money math). Advisory only: every BINDING money
    decision recomputes under the established row locks.
    """

    deposits_total: str
    shares_total: str
    loans_outstanding: str
    guarantees_pledged: str


class MemberDetailOut(MemberOut):
    """MemberOut expanded with aggregates — always on the DETAIL read.

    Expand-only contract: every MemberOut field is unchanged. The
    register LIST serves the same object per row ONLY when the request
    opts in with include=aggregates (one set-based statement per page,
    never a per-row fan-out — scalability); without the parameter list
    rows stay flat.
    """

    aggregates: MemberAggregatesOut


class MemberListResponse(BaseModel):
    items: list[MemberOut]
    next_cursor: str | None


class MemberListDetailResponse(BaseModel):
    """Register page whose rows carry the advisory aggregates.

    Served ONLY when the request opts in with include=aggregates; the
    flat MemberListResponse stays byte-identical otherwise.
    """

    items: list[MemberDetailOut]
    next_cursor: str | None


class StatementLineOut(BaseModel):
    occurred_at: str
    txn_ref: str
    type: str
    channel: str
    amount: str


class StatementResponse(BaseModel):
    items: list[StatementLineOut]
    next_cursor: str | None


def _out(record: members_service.MemberRecord) -> MemberOut:
    return MemberOut(
        id=str(record.id),
        member_no=record.member_no,
        type=record.type.value,
        name=record.name,
        phone=record.phone,
        email=record.email,
        status=record.status.value,
        version=record.version,
        branch_id=str(record.branch_id) if record.branch_id is not None else None,
        dividend_payout=(
            record.dividend_payout.value if record.dividend_payout is not None else None
        ),
    )


def _aggregates_out(figures: members_service.MemberAggregates) -> MemberAggregatesOut:
    return MemberAggregatesOut(
        deposits_total=str(figures.deposits_total),
        shares_total=str(figures.shares_total),
        loans_outstanding=str(figures.loans_outstanding),
        guarantees_pledged=str(figures.guarantees_pledged),
    )


@router.post("", status_code=201)
async def create_member(body: MemberCreateBody, ctx: CreateCtx) -> MemberOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await members_service.create_member(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_type=body.type,
            name=body.name,
            phone=body.phone,
            email=body.email,
            dividend_payout=body.dividend_payout,
        )
    return _out(record)


@router.get("")
async def list_members(
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status: MemberStatus | None = None,
    member_type: Annotated[MemberType | None, Query(alias="type")] = None,
    member_no: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    include: Annotated[Literal["aggregates"] | None, Query()] = None,
) -> MemberListDetailResponse | MemberListResponse:
    """Keyset member register page (members:view).

    OPT-IN aggregates: with include=aggregates
    every row carries the same four advisory decimal-string figures as
    the detail read, computed by ONE set-based statement per page (the
    keyset page drives, LEFT JOIN LATERAL supplies the probes — never
    a per-row fan-out). Without the parameter the response is
    byte-identical to the flat register. No rejection path (403, 422)
    computes or echoes an amount.

    member_no (the posting-drawer unique-identifier
    lookup): expand-only EXACT-match filter served by the 0001 UNIQUE
    (tenant_id, member_no) key. An unknown number is an EMPTY page,
    never a 404 — no existence oracle beyond the members:view grant.
    """
    factory = get_sessionmaker(get_settings().database_url)
    if include == "aggregates":
        async with tenant_session(factory, ctx.tenant_id) as session:
            detail_page = await members_service.list_members_with_aggregates(
                session,
                ctx.tenant_id,
                cursor=cursor,
                limit=limit,
                status=status,
                member_type=member_type,
                member_no=member_no,
            )
        return MemberListDetailResponse(
            items=[
                MemberDetailOut(
                    **_out(entry.record).model_dump(),
                    aggregates=_aggregates_out(entry.aggregates),
                )
                for entry in detail_page.items
            ],
            next_cursor=detail_page.next_cursor,
        )
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await members_service.list_members(
            session,
            ctx.tenant_id,
            cursor=cursor,
            limit=limit,
            status=status,
            member_type=member_type,
            member_no=member_no,
        )
    return MemberListResponse(items=[_out(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/{member_id}/statement")
async def member_statement(
    member_id: uuid.UUID,
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StatementResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await members_service.member_statement(
            session,
            ctx.tenant_id,
            member_id,
            cursor=cursor,
            limit=limit,
        )
    return StatementResponse(
        items=[
            StatementLineOut(
                occurred_at=line.occurred_at.isoformat(),
                txn_ref=line.txn_ref,
                type=line.type,
                channel=line.channel,
                amount=line.amount,
            )
            for line in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/{member_id}")
async def get_member(member_id: uuid.UUID, ctx: ViewCtx) -> MemberDetailOut:
    """Single-member read with advisory aggregates (members:view).

    The existence check runs FIRST: unknown ids (including cross-tenant
    ids hidden by RLS) surface 404 before any aggregate is computed, so
    no rejection path ever echoes an amount (least disclosure, gate
    1.6).
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await members_service.get_member(session, ctx.tenant_id, member_id)
        aggregates = await members_service.member_aggregates(session, ctx.tenant_id, member_id)
    return MemberDetailOut(
        **_out(record).model_dump(),
        aggregates=_aggregates_out(aggregates),
    )


@router.put("/{member_id}")
async def update_member(member_id: uuid.UUID, body: MemberUpdateBody, ctx: EditCtx) -> MemberOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await members_service.update_member(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            version=body.version,
            name=body.name,
            phone=body.phone,
            email=body.email,
            dividend_payout=body.dividend_payout,
        )
    return _out(record)


@router.post("/jobs/dormancy")
async def run_dormancy(body: DormancyRunBody, ctx: EditCtx) -> DormancyRunOut:
    """Run the dormancy job for the caller's tenant (batched, idempotent).

    The nightly cycle (infrastructure/dormancy_worker.py) drives the
    same service per tenant; this route exists for operations and
    backfills (the /jobs/arrears precedent). Members with no
    MEMBER-INITIATED ledger activity inside the tenant-configured
    window transition Active -> Dormant under their row lock; each
    batch commits its own short transaction (scalability).

    Configuration (dormancy_period_months) is resolved server-side
    from tenant settings only — this body accepts none of it
    (extra="forbid"; v1.1 rule 1) — and an unconfigured or corrupt
    period REFUSES the run with 409 and zero transitions (fail closed,
    never a silent default).

    Permission (P4 matrix): members x EDIT — the job rewrites member
    status rows, the same power the manual status route carries; no
    ledger posting happens here.
    """
    as_of = resolve_as_of(body.as_of)
    factory = get_sessionmaker(get_settings().database_url)
    result = await dormancy_service.run_dormancy_for_tenant(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        as_of=as_of,
        batch_size=body.batch_size,
    )
    return DormancyRunOut(
        as_of=as_of.isoformat(),
        cutoff=result.cutoff.isoformat(),
        period_months=result.period_months,
        scanned=result.scanned,
        transitioned=result.transitioned,
        batches=result.batches,
    )


@router.post("/{member_id}/status")
async def change_status(member_id: uuid.UUID, body: MemberStatusBody, ctx: EditCtx) -> MemberOut:
    """Active<->Arrears only; 'exited' is owned by the P12 settlement workflow."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await members_service.change_member_status(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            version=body.version,
            new_status=body.status,
        )
    return _out(record)
