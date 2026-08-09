"""Transactional outbox writer: events commit with the domain change (reliability).

Dispatch happens later, off-transaction, in the outbox worker. Direct
provider calls from request handlers are forbidden (the house doctrine 1.2).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def enqueue_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Insert an outbox event inside the caller's transaction."""
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO outbox_events (id, tenant_id, event_type, payload) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :type, CAST(:payload AS jsonb))"
        ),
        {
            "id": str(event_id),
            "tid": str(tenant_id),
            "type": event_type,
            "payload": json.dumps(payload),
        },
    )
    return event_id
