"""Member credential-link administration endpoints.

Link create/revoke are AUDITED ADMIN MUTATIONS under the dedicated
narrow member_identity module — never self-service, never implied by
members:edit or applications:edit (deny by default, least disclosure).
Re-linking an email to another member is revoke + create: two audited
mutations, each notifying the member.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import member_identity as identity_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["member-identity"])

_identity_view = RequirePermission(Module.MEMBER_IDENTITY, Action.VIEW)
_identity_create = RequirePermission(Module.MEMBER_IDENTITY, Action.CREATE)
_identity_edit = RequirePermission(Module.MEMBER_IDENTITY, Action.EDIT)

IdentityViewCtx = Annotated[AuthContext, Depends(_identity_view)]
IdentityCreateCtx = Annotated[AuthContext, Depends(_identity_create)]
IdentityEditCtx = Annotated[AuthContext, Depends(_identity_edit)]


class CredentialCreateBody(BaseModel):
    """extra="forbid" (least disclosure v1.1): the member comes from the path,

    the status from the schema default — the email is the only input."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)


class CredentialRevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class CredentialOut(BaseModel):
    id: str
    member_id: str
    email: str
    status: str
    version: int


def _credential_out(c: identity_service.CredentialRecord) -> CredentialOut:
    return CredentialOut(
        id=str(c.id),
        member_id=str(c.member_id),
        email=c.email,
        status=c.status,
        version=c.version,
    )


@router.get("/members/{member_id}/credentials")
async def list_member_credentials(
    member_id: uuid.UUID, ctx: IdentityViewCtx
) -> list[CredentialOut]:
    """Link history for one member (member_identity:view — the
    identity surface is never disclosed under mere members:view)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        records = await identity_service.list_member_credentials(session, ctx.tenant_id, member_id)
    return [_credential_out(c) for c in records]


@router.post("/members/{member_id}/credentials", status_code=201)
async def create_member_credential(
    member_id: uuid.UUID,
    body: CredentialCreateBody,
    ctx: IdentityCreateCtx,
) -> CredentialOut:
    """Link a login email to a member (audited admin mutation; the active-email claim is atomic — a
    lost race is a 409)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await identity_service.create_credential(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id=member_id,
            email=body.email,
        )
    return _credential_out(record)


@router.post("/credentials/{credential_id}/revoke")
async def revoke_member_credential(
    credential_id: uuid.UUID,
    body: CredentialRevokeBody,
    ctx: IdentityEditCtx,
) -> CredentialOut:
    """Revoke a credential link. The row is kept (forensic
    history); every live use of the credential dies at its next
    per-request or in-transaction link re-check."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await identity_service.revoke_credential(
            session,
            ctx.tenant_id,
            ctx.user_id,
            credential_id,
            version=body.version,
        )
    return _credential_out(record)
