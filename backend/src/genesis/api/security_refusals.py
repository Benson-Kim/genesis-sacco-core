"""Out-of-band persistence of REFUSED privileged attempts (issue #1).

A blocked self-dealing attempt raises inside the action's transaction,
which rolls back — correct for the money (no partial success, 1.5) but
forensically silent: the in-transaction audit row dies with the
rollback. This wrapper lives in the API layer (the application layer
may not import infrastructure — the import-linter contract) and, on a
``SelfDealingError``, records the refusal in a FRESH transaction
before re-raising the 403:

  * an ``audit_log`` row, action ``security.refusal.self_dealing``,
    under the refused workflow's own entity so the corrections
    reviewers see the attempt in their filtered register;
  * a ``security.refusal`` outbox event for alerting (1.2: side
    effects via the transactional outbox only).

Persistence failures are logged and swallowed: the 403 the client is
owed must never be replaced by a 500 because the forensic write
raced a restart. The payload carries ids and signal names only —
never the matched contact details (least disclosure).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from genesis.application.outbox import enqueue_event
from genesis.application.security_events import SelfDealingError, write_security_audit
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

logger = logging.getLogger(__name__)

REFUSAL_ACTION = "security.refusal.self_dealing"


async def _record_refusal(tenant_id: uuid.UUID, actor_id: uuid.UUID, exc: SelfDealingError) -> None:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        payload: dict[str, object] = {
            "attempted_action": exc.action,
            "member_id": str(exc.member_id),
            "signals": list(exc.signals),
        }
        if exc.amount is not None:
            payload["amount"] = str(exc.amount)
        await write_security_audit(
            session,
            tenant_id,
            actor_id,
            action=REFUSAL_ACTION,
            entity=exc.entity,
            entity_id=exc.entity_id,
            after=payload,
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="security.refusal",
            payload={
                "entity": exc.entity.value,
                "entity_id": exc.entity_id,
                "actor_id": str(actor_id),
                **payload,
            },
        )


@asynccontextmanager
async def refusals_audited(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> AsyncIterator[None]:
    """Wrap a mutation route: a SelfDealingError inside the block is
    persisted out-of-band (fresh transaction) and re-raised as-is."""
    try:
        yield
    except SelfDealingError as exc:
        try:
            await _record_refusal(tenant_id, actor_id, exc)
        except Exception:  # noqa: BLE001 -- the client is owed the 403, never a 500 from the forensic write; category-only log
            logger.error("failed to persist self-dealing refusal for entity=%s", exc.entity)
        raise
