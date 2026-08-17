"""Audit-log viewer endpoint (P13.5, gates 1.3, 1.6).

GET /audit-log: keyset-paginated (never OFFSET), filterable by
entity/actor/action/date, gated by access_control:view. before/after
payloads are redacted server-side per the caller's role entitlement
(application/audit_log.py) — the viewer must not become a PII/figure
exfiltration channel for roles that cannot view the owning module.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from genesis.api.authz import RequirePermission
from genesis.application import audit_log as audit_log_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["audit-log"])

_view = RequirePermission(Module.ACCESS_CONTROL, Action.VIEW)

ViewCtx = Annotated[AuthContext, Depends(_view)]


class AuditLogEntryOut(BaseModel):
    id: int
    at: str
    actor_id: str | None
    action: str
    entity: str
    entity_id: str
    before: dict[str, object] | None
    after: dict[str, object] | None
    redacted: bool


class AuditLogResponse(BaseModel):
    items: list[AuditLogEntryOut]
    next_cursor: str | None


@router.get("/audit-log")
async def list_audit_log(
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    entity: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> AuditLogResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await audit_log_service.list_audit_log(
            session,
            ctx.tenant_id,
            ctx.role_id,
            cursor=cursor,
            limit=limit,
            entity=entity,
            actor_id=actor_id,
            action=action,
            date_from=date_from,
            date_to=date_to,
        )
    return AuditLogResponse(
        items=[
            AuditLogEntryOut(
                id=entry.id,
                at=entry.at.isoformat(),
                actor_id=str(entry.actor_id) if entry.actor_id is not None else None,
                action=entry.action,
                entity=entry.entity,
                entity_id=entry.entity_id,
                before=entry.before,
                after=entry.after,
                redacted=entry.redacted,
            )
            for entry in page.items
        ],
        next_cursor=page.next_cursor,
    )
