"""Access-control endpoints: roles and audited permission edits (P4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from genesis.api.authz import RequirePermission
from genesis.application import rbac as rbac_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/access", tags=["access"])

_view_access = RequirePermission(Module.ACCESS_CONTROL, Action.VIEW)
_edit_access = RequirePermission(Module.ACCESS_CONTROL, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view_access)]
EditCtx = Annotated[AuthContext, Depends(_edit_access)]


class RoleOut(BaseModel):
    id: str
    name: str
    is_system: bool


class PermissionOut(BaseModel):
    module: str
    can_view: bool
    can_create: bool
    can_edit: bool
    can_approve: bool


class PermissionUpdateBody(BaseModel):
    can_view: bool
    can_create: bool
    can_edit: bool
    can_approve: bool


@router.get("/roles")
async def get_roles(ctx: ViewCtx) -> list[RoleOut]:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        roles = await rbac_service.list_roles(session)
    return [RoleOut(id=str(r.id), name=r.name, is_system=r.is_system) for r in roles]


@router.get("/roles/{role_id}/permissions")
async def get_role_permissions(role_id: uuid.UUID, ctx: ViewCtx) -> list[PermissionOut]:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        rows = await rbac_service.permissions_for_role(session, role_id)
    return [
        PermissionOut(
            module=r.module.value,
            can_view=r.can_view,
            can_create=r.can_create,
            can_edit=r.can_edit,
            can_approve=r.can_approve,
        )
        for r in rows
    ]


@router.put("/roles/{role_id}/permissions/{module}")
async def put_role_permission(
    role_id: uuid.UUID,
    module: Module,
    body: PermissionUpdateBody,
    ctx: EditCtx,
) -> PermissionOut:
    """Permission changes are audit-logged in-transaction (data integrity)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        updated = await rbac_service.update_permission(
            session,
            ctx.tenant_id,
            ctx.user_id,
            role_id,
            module,
            can_view=body.can_view,
            can_create=body.can_create,
            can_edit=body.can_edit,
            can_approve=body.can_approve,
        )
    return PermissionOut(
        module=updated.module.value,
        can_view=updated.can_view,
        can_create=updated.can_create,
        can_edit=updated.can_edit,
        can_approve=updated.can_approve,
    )
