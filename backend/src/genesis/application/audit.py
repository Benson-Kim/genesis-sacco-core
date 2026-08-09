"""Shared in-transaction audit writer (data integrity).

Every mutation writes its audit row inside the same transaction as the
domain change, so the trail can never disagree with the data.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def record_audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    action: str,
    entity: str,
    entity_id: str,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(tenant_id, actor_id, action, entity, entity_id, before, after) "
            "VALUES (CAST(:tid AS uuid), CAST(:actor AS uuid), :action, "
            ":entity, :eid, CAST(:before AS jsonb), CAST(:after AS jsonb))"
        ),
        {
            "tid": str(tenant_id),
            "actor": str(actor_id) if actor_id else None,
            "action": action,
            "entity": entity,
            "eid": entity_id,
            "before": json.dumps(before) if before is not None else None,
            "after": json.dumps(after) if after is not None else None,
        },
    )
