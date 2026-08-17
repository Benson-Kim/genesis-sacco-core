"""Audit-log read path (P13.5, gates 1.3, 1.5, 1.6).

The governance counterpart of the audit-write gate: audit_log is
written everywhere (gate 1.5) and becomes readable here, keyset-
paginated (never OFFSET) and filterable by entity/actor/action/date.
Every filter shape is backed by an index shipped in migration 0015
(EXPLAIN-asserted in tests/test_p135_explain.py).

Least disclosure (gate 1.6, the P13 column-allow-list precedent): the
before/after payloads carry the exact figures and PII of the mutation,
so they are an exfiltration channel for anyone holding only
access_control:view. Payloads are released per role entitlement: an
entity's payload is visible only when the CALLER'S role can VIEW the
module that owns the entity, resolved server-side from the permissions
table. The entity->module map below is code-owned and deny-by-default —
an entity missing from the map is redacted for everyone. Row metadata
(who, what action, which entity, when) stays visible: that is the
audit trail itself.

Reads carry explicit bound tenant_id predicates on top of forced RLS
(v1.1 rule 4); every value is a bound parameter (v1.1 rule 6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import rbac as rbac_service
from genesis.domain.rbac import Module
from genesis.errors import InvalidInputError

# Code-owned entity -> owning module map (deny-by-default: unmapped
# entities are redacted for every caller). Values mirror the module
# each writer's routes are gated by (e.g. accounting periods live
# under transactions:*, loan products under settings:*).
ENTITY_MODULES: dict[str, Module] = {
    "members": Module.MEMBERS,
    "share_accounts": Module.MEMBERS,
    "deposit_accounts": Module.MEMBERS,
    "member_exits": Module.MEMBERS,
    "exit": Module.MEMBERS,
    "application": Module.APPLICATIONS,
    "loan_applications": Module.APPLICATIONS,
    "guarantees": Module.APPLICATIONS,
    "loan": Module.LOAN_BOOK,
    "loans": Module.LOAN_BOOK,
    "transaction": Module.TRANSACTIONS,
    "transactions": Module.TRANSACTIONS,
    "accounting_periods": Module.TRANSACTIONS,
    "loan_products": Module.SETTINGS,
    # P13.7: the tenant settings row is maintained under settings:*
    # routes; mapped here so its before/after payloads are released per
    # settings entitlement (review F4 completeness scan).
    "tenant_settings": Module.SETTINGS,
    # P13.6 (!24) ships `entity="branches"` writers under settings:*
    # routes; mapped here (review F4) so the registry's payloads are
    # released per settings entitlement once !24 rebases onto this fix.
    "branches": Module.SETTINGS,
    "exports": Module.REPORTS,
    "permissions": Module.ACCESS_CONTROL,
    "users": Module.ACCESS_CONTROL,
}


@dataclass(frozen=True)
class AuditLogEntry:
    id: int
    at: datetime
    actor_id: uuid.UUID | None
    action: str
    entity: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    redacted: bool


@dataclass(frozen=True)
class AuditLogPage:
    items: list[AuditLogEntry]
    next_cursor: str | None


def audit_page_sql(
    *,
    with_cursor: bool,
    with_entity: bool,
    with_actor: bool,
    with_action: bool,
    with_from: bool,
    with_to: bool,
) -> str:
    """Audit-log page, keyset on (at DESC, id DESC) (gate 1.3).

    Served by the 0015 indexes: idx_audit_keyset (unfiltered / date
    range) and the actor/action/entity filter shapes. Fragments are
    static literals chosen in code; every value is a bound parameter,
    so string assembly is injection-safe (v1.1 rule 6).
    """
    clauses = ["tenant_id = CAST(:tid AS uuid)"]
    if with_entity:
        clauses.append("entity = :entity")
    if with_actor:
        clauses.append("actor_id = CAST(:actor AS uuid)")
    if with_action:
        clauses.append("action = :action")
    if with_from:
        clauses.append("at >= :d_from")
    if with_to:
        clauses.append("at < :d_to_excl")
    if with_cursor:
        clauses.append("(at, id) < (:c_ts, :c_id)")
    where = " AND ".join(clauses)
    return (
        "SELECT id, at, actor_id, action, entity, entity_id, before, after "  # noqa: S608
        f"FROM audit_log WHERE {where} "
        "ORDER BY at DESC, id DESC LIMIT :limit"
    )


def _parse_cursor(cursor: str) -> tuple[datetime, int]:
    """Parse an opaque '<at iso>|<bigint id>' keyset cursor.

    audit_log ids are bigints, so the shared (created_at, uuid) helper
    does not apply; parsing mirrors its strictness (naive timestamps
    are forged/corrupted cursors and are rejected).
    """
    ts_raw, _, id_raw = cursor.partition("|")
    try:
        ts = datetime.fromisoformat(ts_raw)
        row_id = int(id_raw)
    except ValueError as exc:
        raise InvalidInputError("invalid audit-log cursor") from exc
    if ts.tzinfo is None:
        raise InvalidInputError("invalid audit-log cursor")
    return ts, row_id


async def _viewable_modules(session: AsyncSession, role_id: uuid.UUID) -> frozenset[Module]:
    perms = await rbac_service.permissions_for_role(session, role_id)
    return frozenset(p.module for p in perms if p.can_view)


async def list_audit_log(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    viewer_role_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
    entity: str | None = None,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> AuditLogPage:
    """One indexed query per page at any depth; payloads per entitlement."""
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        c_ts, c_id = _parse_cursor(cursor)
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    if entity is not None:
        params["entity"] = entity
    if actor_id is not None:
        params["actor"] = str(actor_id)
    if action is not None:
        params["action"] = action
    # Date bounds are bound as explicit UTC datetimes (review F7):
    # binding a bare `date` against the timestamptz `at` column would
    # delegate the day boundary to the session time zone. Day filters
    # are UTC days by contract, independent of any session setting.
    if date_from is not None:
        params["d_from"] = datetime.combine(date_from, time.min, tzinfo=UTC)
    if date_to is not None:
        # Inclusive end date -> exclusive upper bound on the timestamp.
        params["d_to_excl"] = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
    rows = (
        await session.execute(
            text(
                audit_page_sql(
                    with_cursor=cursor is not None,
                    with_entity=entity is not None,
                    with_actor=actor_id is not None,
                    with_action=action is not None,
                    with_from=date_from is not None,
                    with_to=date_to is not None,
                )
            ),
            params,
        )
    ).all()
    viewable = await _viewable_modules(session, viewer_role_id)
    page_rows = rows[:limit]
    items: list[AuditLogEntry] = []
    for row in page_rows:
        row_entity = str(row[4])
        module = ENTITY_MODULES.get(row_entity)
        disclose = module is not None and module in viewable
        items.append(
            AuditLogEntry(
                id=int(row[0]),
                at=row[1],
                actor_id=uuid.UUID(str(row[2])) if row[2] is not None else None,
                action=str(row[3]),
                entity=row_entity,
                entity_id=str(row[5]),
                before=row[6] if disclose else None,
                after=row[7] if disclose else None,
                redacted=not disclose,
            )
        )
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = f"{last[1].isoformat()}|{int(last[0])}"
    return AuditLogPage(items=items, next_cursor=next_cursor)
