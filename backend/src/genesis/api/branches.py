"""Branches registry endpoints (P13.6, gate 1.6).

Registry CRUD sits under settings x view/create/edit per the P4 matrix
(the prototype manages branches from Settings; the spec-walk and
matrix tests cover every route, deny-by-default). Assignment routes
follow the entity being edited, not the registry: assigning a USER to
a branch is a user-administration mutation (access_control x edit, the
P13.5 users precedent), assigning a MEMBER is a member edit (members x
edit, the P8 precedent) — settings rights alone must not allow editing
people records.

Mutations are idempotent via the Idempotency-Key middleware (gate
1.4); request bodies reject unknown fields (extra="forbid") — branch
is organisational metadata only, so no money/cost/path parameter
exists here for a caller to supply, and none may ride along (1.6).

POST /jobs/branch-backfill mirrors POST /jobs/arrears: the one-off
legacy-text backfill is an operations action for the caller's own
tenant, batched and idempotent (a completed re-run is a lock-free
no-op), guarded by settings x edit like the registry it populates.
"""

from __future__ import annotations

import uuid
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import branches as branches_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["branches"])

_view = RequirePermission(Module.SETTINGS, Action.VIEW)
_create = RequirePermission(Module.SETTINGS, Action.CREATE)
_edit = RequirePermission(Module.SETTINGS, Action.EDIT)
_user_edit = RequirePermission(Module.ACCESS_CONTROL, Action.EDIT)
_member_edit = RequirePermission(Module.MEMBERS, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
CreateCtx = Annotated[AuthContext, Depends(_create)]
EditCtx = Annotated[AuthContext, Depends(_edit)]
UserEditCtx = Annotated[AuthContext, Depends(_user_edit)]
MemberEditCtx = Annotated[AuthContext, Depends(_member_edit)]


class BranchCreateBody(BaseModel):
    """str_strip_whitespace keeps API-created names consistent with the
    backfill's btrim normalisation (' HQ ' can never coexist with
    'HQ'); a whitespace-only name shrinks to '' and fails min_length."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)


class BranchUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)


class BranchAssignBody(BaseModel):
    """Optimistic-locked assignment: version of the USER/MEMBER row."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class BackfillRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=branches_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class BranchOut(BaseModel):
    id: str
    name: str
    version: int
    created_at: str


class BranchListResponse(BaseModel):
    items: list[BranchOut]
    next_cursor: str | None


class BranchAssignmentOut(BaseModel):
    entity_id: str
    branch_id: str
    branch_name: str
    version: int


class BackfillRunOut(BaseModel):
    """Side-effect counts (§4): what the run created and linked."""

    scanned: int
    branches_created: int
    users_linked: int
    batches: int


def _out(record: branches_service.BranchRecord) -> BranchOut:
    return BranchOut(
        id=str(record.id),
        name=record.name,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


def _assignment_out(result: branches_service.BranchAssignment) -> BranchAssignmentOut:
    return BranchAssignmentOut(
        entity_id=str(result.entity_id),
        branch_id=str(result.branch_id),
        branch_name=result.branch_name,
        version=result.version,
    )


@router.post("/branches", status_code=201)
async def create_branch(body: BranchCreateBody, ctx: CreateCtx) -> BranchOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await branches_service.create_branch(
            session, ctx.tenant_id, ctx.user_id, name=body.name
        )
    return _out(record)


@router.get("/branches")
async def list_branches(
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BranchListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await branches_service.list_branches(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return BranchListResponse(items=[_out(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/branches/{branch_id}")
async def get_branch(branch_id: uuid.UUID, ctx: ViewCtx) -> BranchOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await branches_service.get_branch(session, ctx.tenant_id, branch_id)
    return _out(record)


@router.put("/branches/{branch_id}")
async def update_branch(branch_id: uuid.UUID, body: BranchUpdateBody, ctx: EditCtx) -> BranchOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await branches_service.update_branch(
            session,
            ctx.tenant_id,
            ctx.user_id,
            branch_id,
            version=body.version,
            name=body.name,
        )
    return _out(record)


@router.put("/branches/{branch_id}/users/{user_id}")
async def assign_user(
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
    body: BranchAssignBody,
    ctx: UserEditCtx,
) -> BranchAssignmentOut:
    """Assign a user to a branch (access_control x edit — P13.5 precedent)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await branches_service.assign_user_branch(
            session,
            ctx.tenant_id,
            ctx.user_id,
            user_id,
            version=body.version,
            branch_id=branch_id,
        )
    return _assignment_out(result)


@router.put("/branches/{branch_id}/members/{member_id}")
async def assign_member(
    branch_id: uuid.UUID,
    member_id: uuid.UUID,
    body: BranchAssignBody,
    ctx: MemberEditCtx,
) -> BranchAssignmentOut:
    """Assign a member to a branch (members x edit — P8 precedent)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await branches_service.assign_member_branch(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            version=body.version,
            branch_id=branch_id,
        )
    return _assignment_out(result)


@router.post("/jobs/branch-backfill")
async def run_branch_backfill(body: BackfillRunBody, ctx: EditCtx) -> BackfillRunOut:
    """Backfill branches from legacy users.branch text (batched, idempotent).

    Runs for the caller's own tenant only; each batch commits its own
    short transaction via the shared batch runner (gate 1.3). A
    completed re-run scans nothing (anti-join on the branch_id claim
    key) — a lock-free no-op (v1.1 rule 8).
    """
    factory = get_sessionmaker(get_settings().database_url)
    result = await branches_service.run_branch_backfill_for_tenant(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        actor_id=ctx.user_id,
        batch_size=body.batch_size,
    )
    return BackfillRunOut(
        scanned=result.scanned,
        branches_created=result.branches_created,
        users_linked=result.users_linked,
        batches=result.batches,
    )
