"""System users administration endpoints (P13.5, gate 1.6).

The prototype Access-control "Users" tab backend. Every route carries a
RequirePermission dependency on access_control x action (deny by
default); mutations are idempotent via the Idempotency-Key middleware
(gate 1.4). Request bodies reject unknown fields (extra="forbid"):
role and status can never ride along on a profile edit — they are
separate, separately-guarded mutations. OTP endpoints never disclose
codes (the prototype's on-screen OTP is a demo artifact).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import users as users_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.domain.users import UserStatus
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/users", tags=["users"])

_view = RequirePermission(Module.ACCESS_CONTROL, Action.VIEW)
_create = RequirePermission(Module.ACCESS_CONTROL, Action.CREATE)
_edit = RequirePermission(Module.ACCESS_CONTROL, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
CreateCtx = Annotated[AuthContext, Depends(_create)]
EditCtx = Annotated[AuthContext, Depends(_edit)]


class UserCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=1, max_length=200)
    role_id: uuid.UUID
    phone: str | None = Field(default=None, max_length=32)
    branch: str | None = Field(default=None, max_length=120)


class UserUpdateBody(BaseModel):
    """Profile fields only; role/status have dedicated guarded routes."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    branch: str | None = Field(default=None, max_length=120)


class UserStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    status: UserStatus


class UserRoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    role_id: uuid.UUID


class UserOut(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str | None
    branch: str | None
    role_id: str
    role_name: str
    status: str
    last_active_at: str | None
    version: int


class UserListResponse(BaseModel):
    items: list[UserOut]
    next_cursor: str | None


class OtpInvalidateResponse(BaseModel):
    """Side-effect counts only — never challenge contents (gate 1.6)."""

    voided_otp_challenges: int


class OtpReenrolResponse(BaseModel):
    voided_otp_challenges: int
    revoked_refresh_tokens: int


def _out(record: users_service.UserRecord) -> UserOut:
    return UserOut(
        id=str(record.id),
        full_name=record.full_name,
        email=record.email,
        phone=record.phone,
        branch=record.branch,
        role_id=str(record.role_id),
        role_name=record.role_name,
        status=record.status.value,
        last_active_at=(
            record.last_active_at.isoformat() if record.last_active_at is not None else None
        ),
        version=record.version,
    )


@router.post("", status_code=201)
async def create_user(body: UserCreateBody, ctx: CreateCtx) -> UserOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await users_service.create_user(
            session,
            ctx.tenant_id,
            ctx.user_id,
            email=body.email,
            full_name=body.full_name,
            role_id=body.role_id,
            phone=body.phone,
            branch=body.branch,
        )
    return _out(record)


@router.get("")
async def list_users(
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status: UserStatus | None = None,
    role_id: uuid.UUID | None = None,
) -> UserListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await users_service.list_users(
            session,
            ctx.tenant_id,
            cursor=cursor,
            limit=limit,
            status=status,
            role_id=role_id,
        )
    return UserListResponse(items=[_out(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/{user_id}")
async def get_user(user_id: uuid.UUID, ctx: ViewCtx) -> UserOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await users_service.get_user(session, ctx.tenant_id, user_id)
    return _out(record)


@router.put("/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserUpdateBody, ctx: EditCtx) -> UserOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await users_service.update_user(
            session,
            ctx.tenant_id,
            ctx.user_id,
            user_id,
            version=body.version,
            full_name=body.full_name,
            email=body.email,
            phone=body.phone,
            branch=body.branch,
        )
    return _out(record)


@router.post("/{user_id}/status")
async def change_status(user_id: uuid.UUID, body: UserStatusBody, ctx: EditCtx) -> UserOut:
    """Activate/suspend; self-changes and last-admin suspension are refused."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await users_service.change_user_status(
            session,
            ctx.tenant_id,
            ctx.user_id,
            user_id,
            version=body.version,
            new_status=body.status,
        )
    return _out(record)


@router.post("/{user_id}/role")
async def assign_role(user_id: uuid.UUID, body: UserRoleBody, ctx: EditCtx) -> UserOut:
    """Audited role assignment; self-changes and last-admin re-roles refused."""
    # Review F2: granting System Admin, or re-roling a current System
    # Admin, additionally requires the ACTOR to be a System Admin —
    # resolved server-side in the service, never from the JWT alone.
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await users_service.assign_role(
            session,
            ctx.tenant_id,
            ctx.user_id,
            user_id,
            version=body.version,
            role_id=body.role_id,
        )
    return _out(record)


@router.post("/{user_id}/otp/invalidate")
async def invalidate_otp(user_id: uuid.UUID, ctx: EditCtx) -> OtpInvalidateResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        voided = await users_service.invalidate_otp_challenges(
            session, ctx.tenant_id, ctx.user_id, user_id
        )
    return OtpInvalidateResponse(voided_otp_challenges=voided)


@router.post("/{user_id}/otp/reenrol")
async def reenrol_otp(user_id: uuid.UUID, ctx: EditCtx) -> OtpReenrolResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        voided, revoked = await users_service.reenrol_otp(
            session, ctx.tenant_id, ctx.user_id, user_id
        )
    return OtpReenrolResponse(voided_otp_challenges=voided, revoked_refresh_tokens=revoked)
