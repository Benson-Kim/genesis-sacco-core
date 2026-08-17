"""Deny-by-default authorization (gate 1.6).

Every business route MUST carry a RequirePermission dependency; a CI test
walks the route table and fails if any operation lacks one.
"""

from __future__ import annotations

from fastapi import Request

from genesis.application import rbac as rbac_service
from genesis.application.auth import AuthContext, decode_access_token
from genesis.domain.rbac import Action, Module
from genesis.errors import ForbiddenError, UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings


def get_auth_context(request: Request) -> AuthContext:
    """Authentication only; use RequirePermission for authorization."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise UnauthenticatedError("missing bearer token")
    return decode_access_token(header[7:])


class RequirePermission:
    """FastAPI dependency enforcing role x module x action server-side."""

    def __init__(self, module: Module, action: Action) -> None:
        self.module = module
        self.action = action

    async def __call__(self, request: Request) -> AuthContext:
        ctx = get_auth_context(request)
        factory = get_sessionmaker(get_settings().database_url)
        async with tenant_session(factory, ctx.tenant_id) as session:
            allowed = await rbac_service.has_permission(
                session, ctx.role_id, self.module, self.action
            )
        if not allowed:
            raise ForbiddenError(f"{self.module.value}:{self.action.value}")
        return ctx


class RequireAnyPermission(RequirePermission):
    """Grant when the role holds ANY of the listed module x action pairs.

    Still deny-by-default (gate 1.6): an explicit, code-owned list of
    grants is checked server-side in one tenant session; an empty
    result is a 403 naming the required permissions, never data. The
    subclass relationship keeps the P4 spec-walk test structural — a
    route carrying this dependency IS carrying a RequirePermission
    check. First use: /products discovery, which application creators
    need for the prototype's new-application form without holding
    settings:view (external Codex review, re-derived — the original
    opened a second DB session per request and duplicated the check
    inline in the router).
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
                if await rbac_service.has_permission(session, ctx.role_id, module, action):
                    return ctx
        raise ForbiddenError(
            " or ".join(f"{module.value}:{action.value}" for module, action in self.grants)
        )
