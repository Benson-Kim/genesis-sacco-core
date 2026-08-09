"""Deny-by-default authorization (least disclosure).

Every business route MUST carry a RequirePermission dependency; a CI test
walks the route table and fails if any operation lacks one.
"""

from __future__ import annotations

from fastapi import Request

from genesis.application import rbac as rbac_service
from genesis.application.auth import (
    AuthContext,
    MemberAuthContext,
    decode_access_token,
    decode_member_access_token,
)
from genesis.application.member_auth import live_credential_by_id
from genesis.domain.rbac import Action, Module
from genesis.errors import ForbiddenError, UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise UnauthenticatedError("missing bearer token")
    return header[7:]


def get_auth_context(request: Request) -> AuthContext:
    """Authentication only; use RequirePermission for authorization."""
    return decode_access_token(_bearer_token(request))


class RequirePermission:
    """FastAPI dependency enforcing role x module x action server-side."""

    def __init__(self, module: Module, action: Action) -> None:
        self.module = module
        self.action = action

    async def __call__(self, request: Request) -> AuthContext:
        ctx = get_auth_context(request)
        factory = get_sessionmaker(get_settings().database_url)
        async with tenant_session(factory, ctx.tenant_id) as session:
            access = await rbac_service.actor_access(
                session, ctx.tenant_id, ctx.user_id, ctx.role_id, self.module, self.action
            )
        if access is None or not access.is_active:
            # a suspended (or vanished) user is refused
            # immediately — the access token's remaining lifetime never
            # bridges a committed suspension. 401, not 403: the session
            # is dead, not under-privileged. Only the error category
            # leaves the server (least disclosure, least disclosure).
            raise UnauthenticatedError("user is not active")
        if not access.allowed:
            raise ForbiddenError(f"{self.module.value}:{self.action.value}")
        return ctx


class RequireMemberPrincipal:
    """FastAPI dependency for /member routes (/).

    Two fences, both deny-by-default:
      * PRINCIPAL KIND — decode_member_access_token refuses a staff
        token with a 403: a staff RequirePermission grant can
        never satisfy the member gate, in either direction.
      * LIVE LINK — the credential->member link is re-verified against
        the database ON EVERY REQUEST (live_credential_by_id, the ONE
        implementation): a revoked link, a re-pointed credential, or
        an exited member dies within one request, exactly like the
        RequirePermission suspension re-check. 401, not
        403 — the session is dead, not under-privileged; only the
        error category leaves the server (least disclosure, least disclosure).

    Money-relevant member actions (consent/release) ADDITIONALLY
    re-verify the link INSIDE their transaction under the guarantee
    row lock — this gate is the outer fence, not the last word.
    """

    async def __call__(self, request: Request) -> MemberAuthContext:
        ctx = decode_member_access_token(_bearer_token(request))
        factory = get_sessionmaker(get_settings().database_url)
        async with tenant_session(factory, ctx.tenant_id) as session:
            credential = await live_credential_by_id(session, ctx.tenant_id, ctx.credential_id)
        if credential is None or credential.member_id != ctx.member_id:
            raise UnauthenticatedError("member credential is not active")
        return ctx


class RequireAnyPermission(RequirePermission):
    """Grant when the role holds ANY of the listed module x action pairs.

    Still deny-by-default (least disclosure): an explicit, code-owned list of
    grants is checked server-side in one tenant session; an empty
    result is a 403 naming the required permissions, never data. The
    subclass relationship keeps the P4 spec-walk test structural — a
    route carrying this dependency IS carrying a RequirePermission
    check. First use: /products discovery, which application creators
    need for the new-application form without holding settings:view
    (re-derived from an external review — the original opened a second
    DB session per request and duplicated the check inline in the
    router).
    """

    def __init__(self, *grants: tuple[Module, Action]) -> None:
        if not grants:
            raise ValueError("RequireAnyPermission needs at least one grant")
        super().__init__(grants[0][0], grants[0][1])
        self.grants = grants

    async def __call__(self, request: Request) -> AuthContext:
        ctx = get_auth_context(request)
        factory = get_sessionmaker(get_settings().database_url)
        async with tenant_session(factory, ctx.tenant_id) as session:
            for module, action in self.grants:
                access = await rbac_service.actor_access(
                    session, ctx.tenant_id, ctx.user_id, ctx.role_id, module, action
                )
                if access is None or not access.is_active:
                    # same immediate refusal as the base class.
                    raise UnauthenticatedError("user is not active")
                if access.allowed:
                    return ctx
        raise ForbiddenError(
            " or ".join(f"{module.value}:{action.value}" for module, action in self.grants)
        )
