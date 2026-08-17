"""RBAC use-cases: seeding, queries, and audited updates (gates 1.5, 1.6).

Tenant-predicate exemption (documented per issue #17, gate 1.6 v1.1):
role/permission queries here filter by role_id — an unguessable UUID
taken from the authenticated caller's JWT (or resolved via a name
lookup inside the tenant session), never from request input that could
name another tenant's role. Forced RLS on roles/permissions is the
tenant fence; a bound tenant predicate would add nothing because
role_id is already scoped to the token's tenant by issuance. Writes
that mutate permissions run inside the caller's tenant session and are
audited in-transaction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.domain.rbac import Action, Module, seed_matrix
from genesis.errors import NotFoundError


@dataclass(frozen=True)
class ModulePermissions:
    module: Module
    can_view: bool
    can_create: bool
    can_edit: bool
    can_approve: bool


@dataclass(frozen=True)
class RoleInfo:
    id: uuid.UUID
    name: str
    is_system: bool


_ACTION_QUERIES: dict[Action, TextClause] = {
    Action.VIEW: text(
        "SELECT can_view FROM permissions WHERE role_id = CAST(:rid AS uuid) AND module = :module"
    ),
    Action.CREATE: text(
        "SELECT can_create FROM permissions WHERE role_id = CAST(:rid AS uuid) AND module = :module"
    ),
    Action.EDIT: text(
        "SELECT can_edit FROM permissions WHERE role_id = CAST(:rid AS uuid) AND module = :module"
    ),
    Action.APPROVE: text(
        "SELECT can_approve FROM permissions "
        "WHERE role_id = CAST(:rid AS uuid) AND module = :module"
    ),
}

# Column names are code-owned literals chosen by Action, never caller
# input, so the f-string assembly below is injection-safe (v1.1 rule 6).
_ACTION_COLUMNS: dict[Action, str] = {
    Action.VIEW: "can_view",
    Action.CREATE: "can_create",
    Action.EDIT: "can_edit",
    Action.APPROVE: "can_approve",
}

_ACCESS_QUERIES: dict[Action, TextClause] = {
    action: text(
        f"SELECT u.status = 'active', COALESCE(p.{column}, false) "  # noqa: S608
        "FROM users u LEFT JOIN permissions p "
        "ON p.role_id = CAST(:rid AS uuid) AND p.module = :module "
        "WHERE u.id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid)"
    )
    for action, column in _ACTION_COLUMNS.items()
}


@dataclass(frozen=True)
class ActorAccess:
    """Per-request authorization facts, fetched in one round-trip (F3)."""

    is_active: bool
    allowed: bool


async def actor_access(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    module: Module,
    action: Action,
) -> ActorAccess | None:
    """Permission check + users.status verification, one round-trip.

    Review F3: stateless access tokens outlive a suspension by up to 15
    minutes, so authorization must also verify the actor's COMMITTED
    status. RequirePermission already pays a DB round-trip per request;
    the status rides along in the same query. Returns None when the
    user row is gone (refused as unauthenticated by the caller). Deny
    by default: a missing permission row is `allowed=False` (gate 1.6).
    The users lookup carries an explicit bound tenant predicate on top
    of forced RLS (v1.1 rule 4); role_id keeps the documented issue #17
    exemption (unguessable, JWT-scoped).
    """
    row = (
        await session.execute(
            _ACCESS_QUERIES[action],
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "rid": str(role_id),
                "module": module.value,
            },
        )
    ).first()
    if row is None:
        return None
    return ActorAccess(is_active=bool(row[0]), allowed=bool(row[1]))


async def seed_permissions(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Create the 7 system roles and their prototype matrix; idempotent."""
    role_ids: dict[str, uuid.UUID] = {}
    for role_name, modules in seed_matrix().items():
        existing = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"),
                {"name": role_name},
            )
        ).first()
        if existing is not None:
            role_id = uuid.UUID(str(existing[0]))
        else:
            role_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO roles (id, tenant_id, name, is_system) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, true)"
                ),
                {"id": str(role_id), "tid": str(tenant_id), "name": role_name},
            )
        role_ids[role_name] = role_id
        for module, actions in modules.items():
            await session.execute(
                text(
                    "INSERT INTO permissions (tenant_id, role_id, module, "
                    "can_view, can_create, can_edit, can_approve) VALUES "
                    "(CAST(:tid AS uuid), CAST(:rid AS uuid), :module, :v, :c, :e, :a) "
                    "ON CONFLICT (tenant_id, role_id, module) DO NOTHING"
                ),
                {
                    "tid": str(tenant_id),
                    "rid": str(role_id),
                    "module": module.value,
                    "v": actions[Action.VIEW],
                    "c": actions[Action.CREATE],
                    "e": actions[Action.EDIT],
                    "a": actions[Action.APPROVE],
                },
            )
    return role_ids


async def has_permission(
    session: AsyncSession, role_id: uuid.UUID, module: Module, action: Action
) -> bool:
    """Deny by default: a missing row means no access (gate 1.6)."""
    row = (
        await session.execute(
            _ACTION_QUERIES[action],
            {"rid": str(role_id), "module": module.value},
        )
    ).first()
    return bool(row[0]) if row is not None else False


async def permissions_for_role(
    session: AsyncSession, role_id: uuid.UUID
) -> list[ModulePermissions]:
    rows = (
        await session.execute(
            text(
                "SELECT module, can_view, can_create, can_edit, can_approve "
                "FROM permissions WHERE role_id = CAST(:rid AS uuid) ORDER BY module"
            ),
            {"rid": str(role_id)},
        )
    ).all()
    return [
        ModulePermissions(
            module=Module(str(r[0])),
            can_view=bool(r[1]),
            can_create=bool(r[2]),
            can_edit=bool(r[3]),
            can_approve=bool(r[4]),
        )
        for r in rows
    ]


async def list_roles(session: AsyncSession) -> list[RoleInfo]:
    rows = (
        await session.execute(text("SELECT id, name, is_system FROM roles ORDER BY name"))
    ).all()
    return [RoleInfo(id=uuid.UUID(str(r[0])), name=str(r[1]), is_system=bool(r[2])) for r in rows]


async def update_permission(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    role_id: uuid.UUID,
    module: Module,
    *,
    can_view: bool,
    can_create: bool,
    can_edit: bool,
    can_approve: bool,
) -> ModulePermissions:
    """Audited permission change under a row lock (gates 1.4, 1.5)."""
    row = (
        await session.execute(
            text(
                "SELECT id, can_view, can_create, can_edit, can_approve FROM permissions "
                "WHERE role_id = CAST(:rid AS uuid) AND module = :module FOR UPDATE"
            ),
            {"rid": str(role_id), "module": module.value},
        )
    ).first()
    if row is None:
        raise NotFoundError("permission row not found")
    before = {
        "can_view": bool(row[1]),
        "can_create": bool(row[2]),
        "can_edit": bool(row[3]),
        "can_approve": bool(row[4]),
    }
    after = {
        "can_view": can_view,
        "can_create": can_create,
        "can_edit": can_edit,
        "can_approve": can_approve,
    }
    await session.execute(
        text(
            "UPDATE permissions SET can_view = :v, can_create = :c, "
            "can_edit = :e, can_approve = :a WHERE id = CAST(:id AS uuid)"
        ),
        {"v": can_view, "c": can_create, "e": can_edit, "a": can_approve, "id": str(row[0])},
    )
    await session.execute(
        text(
            "INSERT INTO audit_log (tenant_id, actor_id, action, entity, entity_id, "
            "before, after) VALUES (CAST(:tid AS uuid), CAST(:actor AS uuid), "
            "'permission.update', 'permissions', :entity_id, "
            "CAST(:before AS jsonb), CAST(:after AS jsonb))"
        ),
        {
            "tid": str(tenant_id),
            "actor": str(actor_id),
            "entity_id": f"{role_id}:{module.value}",
            "before": json.dumps(before),
            "after": json.dumps(after),
        },
    )
    return ModulePermissions(
        module=module,
        can_view=can_view,
        can_create=can_create,
        can_edit=can_edit,
        can_approve=can_approve,
    )
