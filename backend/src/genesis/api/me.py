"""Authenticated self-service endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from genesis.api.authz import get_auth_context
from genesis.application import rbac as rbac_service
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["me"])


class ModulePermissionsOut(BaseModel):
    module: str
    can_view: bool
    can_create: bool
    can_edit: bool
    can_approve: bool


class MyPermissionsResponse(BaseModel):
    role_id: str
    permissions: list[ModulePermissionsOut]


@router.get("/me/permissions")
async def my_permissions(request: Request) -> MyPermissionsResponse:
    """Drives client route guards; the API still enforces every call (least disclosure)."""
    ctx = get_auth_context(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        rows = await rbac_service.permissions_for_role(session, ctx.role_id)
    return MyPermissionsResponse(
        role_id=str(ctx.role_id),
        permissions=[
            ModulePermissionsOut(
                module=r.module.value,
                can_view=r.can_view,
                can_create=r.can_create,
                can_edit=r.can_edit,
                can_approve=r.can_approve,
            )
            for r in rows
        ],
    )
