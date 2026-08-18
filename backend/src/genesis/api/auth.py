"""Authentication endpoints: OTP step-up, refresh rotation, logout (least disclosure)."""

from __future__ import annotations

import ipaddress
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, model_validator

from genesis.application import auth as auth_service
from genesis.errors import RateLimitedError, UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.rate_limit import check_rate_limit
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class OtpIdentifierBody(BaseModel):
    """Sign-in identifier envelope.

    Exactly one of ``email`` (the earlier field, accepted for one more
    release) or ``identifier`` must be present. ``identifier`` may be
    an email address or a Kenya mobile number in local (07XX/01XX) or
    international (+2547XX/+2541XX) form.
    """

    email: str | None = Field(default=None, min_length=3, max_length=254)
    identifier: str | None = Field(default=None, min_length=3, max_length=254)

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> OtpIdentifierBody:
        """Reject bodies carrying both fields or neither."""
        if (self.email is None) == (self.identifier is None):
            msg = "provide exactly one of email or identifier"
            raise ValueError(msg)
        return self

    @property
    def signin_identifier(self) -> str:
        value = self.identifier if self.identifier is not None else self.email
        if value is None:  # pragma: no cover - forbidden by the model validator
            raise UnauthenticatedError("missing sign-in identifier")
        return value


class OtpRequestBody(OtpIdentifierBody):
    """Request a one-time password for a sign-in identifier."""


class OtpVerifyBody(OtpIdentifierBody):
    """Verify the six-digit one-time password for a sign-in identifier."""

    code: str = Field(pattern=r"^\d{6}$")


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


def tenant_id_from_headers(request: Request) -> uuid.UUID:
    """Pre-auth endpoints scope the tenant from an explicit header."""
    raw = request.headers.get("x-tenant-id", "")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise UnauthenticatedError("missing or invalid tenant header") from exc


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a candidate IP strictly; None on anything malformed."""
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def resolve_client_ip(request: Request) -> str:
    """Resolve the client IP that BOTH rate buckets key on (issue #13).

    Behind Passenger (the MochaHost deployment) ``request.client.host`` is
    the proxy, which collapses the per-IP backstop into one global bucket —
    a cheap auth-DoS primitive. ``X-Forwarded-For`` is consulted ONLY when
    the immediate peer is in ``trusted_proxy_ips`` (default EMPTY = never):
    the chain is walked from the RIGHT and the first entry not in the
    trusted set wins. Every candidate is validated with ``ipaddress`` —
    a malformed or empty chain collapses to ONE shared bucket (the MR's
    core lesson: attacker-controlled strings must never mint buckets).
    A request with no client at all keeps the shared ``unknown`` bucket.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return "unknown"
    trusted = get_settings().trusted_proxy_ips_set
    peer_ip = _parse_ip(peer)
    if not trusted or peer_ip is None or peer_ip not in trusted:
        # Direct peer (or no proxy trust configured): today's behavior.
        return peer
    hops = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        candidate = _parse_ip(hop)
        if candidate is None:
            return "invalid-forwarded-for"
        if candidate not in trusted:
            return str(candidate)
    # Empty chain, or every hop trusted: a proxy misconfiguration —
    # shared bucket, never a free pass and never a header-minted bucket.
    return "invalid-forwarded-for"


async def _rate_guard(request: Request) -> None:
    """Two-bucket sliding-window guard for the pre-auth endpoints (the house gates).

    The tenant header is VALIDATED before it becomes part of a bucket key:
    every unparseable value shares ONE ``invalid`` bucket, so rotating
    garbage ``x-tenant-id`` values can never mint a fresh bucket per
    request. A secondary pure-IP bucket with its own higher limit applies
    regardless of the header, so rotating syntactically VALID tenant ids
    does not bypass the guard either. Both buckets key on the SAME
    resolved client IP (trusted-proxy aware; resolve_client_ip). Both
    buckets must pass; both are spent on every call so neither can be
    starved of evidence.
    """
    client_ip = resolve_client_ip(request)
    raw_tenant = request.headers.get("x-tenant-id", "")
    try:
        tenant = str(uuid.UUID(raw_tenant))
    except ValueError:
        tenant = "invalid"
    settings = get_settings()
    tenant_key = f"auth:{request.url.path}:{tenant}:{client_ip}"
    ip_key = f"auth:ip:{client_ip}"
    tenant_allowed = await check_rate_limit(tenant_key, settings.auth_rate_limit_per_minute)
    ip_allowed = await check_rate_limit(ip_key, settings.auth_rate_limit_ip_per_minute)
    if not (tenant_allowed and ip_allowed):
        raise RateLimitedError("auth rate limit exceeded")


@router.post("/otp/request", status_code=202, dependencies=[Depends(_rate_guard)])
async def request_otp(body: OtpRequestBody, request: Request) -> dict[str, str]:
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        code = await auth_service.request_otp(session, tenant_id, body.signin_identifier)
    payload = {"status": "sent"}
    # DEV-ONLY (item 11, REMOVE BEFORE STAGING): with SMS/email
    # delivery unbuilt, testers read the OTP from this response. The
    # dev_otp_display flag is FAIL-CLOSED (off by default); the code is
    # never logged and never appears in any error path. The declared
    # response contract (an open string map) is unchanged.
    if code is not None and get_settings().dev_otp_display:
        payload["dev_otp"] = code
    return payload


@router.post("/otp/verify", dependencies=[Depends(_rate_guard)])
async def verify_otp(body: OtpVerifyBody, request: Request) -> TokenResponse:
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await auth_service.verify_otp(
            session, tenant_id, body.signin_identifier, body.code
        )
    # The transaction has committed: punitive state (attempt counters) is
    # durable even though this request fails (the house gates).
    if isinstance(outcome, auth_service.AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/refresh", dependencies=[Depends(_rate_guard)])
async def refresh(body: RefreshBody, request: Request) -> TokenResponse:
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await auth_service.rotate_refresh_token(session, tenant_id, body.refresh_token)
    # Family revocation on reuse must survive the failed request, so the
    # 401 is raised only after the transaction has committed (concurrency safety).
    if isinstance(outcome, auth_service.AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/logout", status_code=204, dependencies=[Depends(_rate_guard)])
async def logout(body: RefreshBody, request: Request) -> Response:
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        await auth_service.revoke_refresh_token(session, body.refresh_token)
    return Response(status_code=204)
