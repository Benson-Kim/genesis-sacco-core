"""Retention purge for expired idempotency keys (.17c / DSA-3).

The purge is housekeeping ONLY: expiry semantics never depend on it.
The fence ``expires_at > now()`` in api/idempotency.py (replay lookup
AND claim takeover) makes an expired row behave as absent before any
purge runs — this job merely reclaims the storage (and the stored
response envelopes: the data-retention liability DSA-3 names).

Concurrency + re-run posture:

  * bounded batches through the shared batch runner — each DELETE
    claims at most batch_size rows via a FOR UPDATE SKIP LOCKED
    driving subquery (served by idx_idempotency_keys_expiry, migration
    0029) inside its OWN short transaction; no long transactions, no
    waiting on rows another worker holds;
  * ONLY expired rows (``expires_at <= now()``) are eligible — a live
    claim, stored or in flight, is never touched (asserted by
    side-effect counts in tests/test_p1317_idempotency_expiry.py). An
    expired IN-FLIGHT row is deletable by design: it is exactly the
    takeover-eligible state, and deleting it is equivalent to the
    takeover resetting it — either way the next request executes as a
    NEW request with one effect;
  * a purge racing a takeover cannot double-fire: the takeover's
    ON CONFLICT UPDATE and the purge's FOR UPDATE serialise on the
    row; whichever loses re-evaluates its predicate against the
    winner's committed state (a taken-over row is live again → the
    purge skips it; a purged row leaves the INSERT arbiter free → the
    claim proceeds as a plain INSERT);
  * re-runs are lock-free no-ops by side-effect counts: zero expired
    rows match, zero rows locked, zero rows deleted.

The idempotency claim lifecycle stays a single-node locker touching
idempotency_keys rows only — no domain locks, no new lock-graph edges
(docs/diagrams/lock-order.md, updated in this MR).
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.batch_runner import SessionScope, run_in_batches

#: Purge batch bound (scalability): at most this many rows per short
#: transaction. Config-owned module constant — the 0024
#: PURGE_BATCH_SIZE precedent.
PURGE_BATCH_SIZE = 500

#: The batched purge DELETE. The driving subquery is served by
#: idx_idempotency_keys_expiry (0029); the tenant predicate is
#: explicit on top of RLS (least disclosure defence in depth). Module-level so
#: the EXPLAIN test captures this exact statement.
PURGE_BATCH_SQL = (
    "DELETE FROM idempotency_keys WHERE id IN ("
    "SELECT id FROM idempotency_keys "
    "WHERE tenant_id = CAST(:tid AS uuid) AND expires_at <= now() "
    "LIMIT :limit FOR UPDATE SKIP LOCKED)"
)


async def purge_expired_idempotency_keys(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    batch_size: int = PURGE_BATCH_SIZE,
) -> int:
    """Delete this tenant's expired idempotency keys; returns the count.

    One bounded DELETE per short transaction via the shared batch
    runner; live claims (``expires_at > now()``) are never eligible.
    A completed re-run matches zero rows and locks nothing.
    """

    async def _purge_batch(
        session: AsyncSession, _cursor: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, None]:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(PURGE_BATCH_SQL),
                {"tid": str(tenant_id), "limit": batch_size},
            ),
        )
        return int(result.rowcount or 0), None, None

    purged, _, _ = await run_in_batches(session_scope, _purge_batch, batch_size=batch_size)
    return purged
