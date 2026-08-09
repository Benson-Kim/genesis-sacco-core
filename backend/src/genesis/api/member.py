"""Member-facing endpoints: /member auth + guarantor self-service.

The MEMBER principal's own surface. Authentication reuses the staff
endpoint SHAPES verbatim (bodies, response, rate guard, x-tenant-id pre-auth scoping — reuse-first:
the api/auth.py models and guard are imported, not copied) but issues MEMBER-audience tokens that
can never
satisfy a staff RequirePermission gate. Business routes carry
RequireMemberPrincipal — the per-request live-link re-check —
and the consent/release services re-verify the link again INSIDE the
transaction under the guarantee row lock.

Guarantor consent and self-release are acts of the member principal
(the P9 consent contract, completed): the interim email-match is
retired — these routes are where a guarantor acts for themselves.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.auth import (
    OtpRequestBody,
    OtpVerifyBody,
    RefreshBody,
    TokenResponse,
    _rate_guard,
    tenant_id_from_headers,
)
from genesis.api.authz import RequireMemberPrincipal
from genesis.api.loans import GuaranteeOut, _guarantee_out
from genesis.application import guarantees as guarantees_service
from genesis.application import member_auth as member_auth_service
from genesis.application.auth import AuthFailure, MemberAuthContext
from genesis.errors import UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/member", tags=["member"])

_member_principal = RequireMemberPrincipal()

MemberCtx = Annotated[MemberAuthContext, Depends(_member_principal)]


class MemberActBody(BaseModel):
    """Consent/self-release body: the optimistic-lock version ONLY.

    extra="forbid" (least disclosure v1.1): there is deliberately NO field for
    who consents — the principal IS the authenticated credential; a
    caller-asserted identity or consent flag is a rejected design (the lesson)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


@router.post("/auth/otp/request", status_code=202, dependencies=[Depends(_rate_guard)])
async def request_member_otp(body: OtpRequestBody, request: Request) -> dict[str, str]:
    """Member OTP request (the P3 policy verbatim; never reveals
    whether a credential exists)."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        await member_auth_service.request_member_otp(session, tenant_id, body.signin_identifier)
    return {"status": "sent"}


@router.post("/auth/otp/verify", dependencies=[Depends(_rate_guard)])
async def verify_member_otp(body: OtpVerifyBody, request: Request) -> TokenResponse:
    """Verify the member OTP; issues MEMBER-audience tokens."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await member_auth_service.verify_member_otp(
            session, tenant_id, body.signin_identifier, body.code
        )
    # The transaction has committed: punitive state (attempt counters)
    # is durable even though this request fails (the house gates).
    if isinstance(outcome, AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/auth/refresh", dependencies=[Depends(_rate_guard)])
async def refresh_member_token(body: RefreshBody, request: Request) -> TokenResponse:
    """Rotate a member refresh token; reuse revokes the family (P3)."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await member_auth_service.rotate_member_refresh_token(
            session, tenant_id, body.refresh_token
        )
    # Family revocation on reuse must survive the failed request, so
    # the 401 is raised only after the transaction commits (concurrency safety).
    if isinstance(outcome, AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/guarantees/{guarantee_id}/consent")
async def consent_guarantee_as_member(
    guarantee_id: uuid.UUID,
    body: MemberActBody,
    ctx: MemberCtx,
) -> GuaranteeOut:
    """Guarantor consent as the MEMBER principal: pledged -> active.

    The link is re-verified inside the transaction under the guarantee
    row lock; the consent row carries the credential.
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.consent_guarantee_as_member(
            session,
            ctx,
            guarantee_id,
            version=body.version,
        )
    return _guarantee_out(record)


@router.post("/guarantees/{guarantee_id}/release")
async def release_guarantee_as_member(
    guarantee_id: uuid.UUID,
    body: MemberActBody,
    ctx: MemberCtx,
) -> GuaranteeOut:
    """Withdraw the member's OWN unconsented pledge (rules; the member-principal replacement for the
    retired email-match)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.release_guarantee_as_member(
            session,
            ctx,
            guarantee_id,
            version=body.version,
        )
    return _guarantee_out(record)
