"""EXPLAIN capture for the issue-#31 batch-10 share-transfer register.

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_batch10.txt (CI artifact, surfaced in the job log
and pasted into the MR description). The SQL under test is imported
from the production module — share_transfers_register_sql is the
statement the ledger-(m) LIST route actually runs.

Index story (shipped in the SAME migration as the query — 0040, gate
1.3; the 0020 index audit found (tenant_id, from_member_id, ...) and
(tenant_id, to_member_id, ...) — neither serves a tenant-wide
register-ordered keyset walk):

  * share-transfer register: idx_share_transfers_register (tenant_id,
    (status = 'pending') DESC, created_at DESC, id DESC) — the
    pending-first band expression IS the index's second key, so the
    uniform-DESC keyset row comparison walks it in register order (the
    0038 pattern).

Tiny CI tables make seqscan/sort the cheaper plan; the capture
disables both for the session to prove the relation is SERVABLE by
its index (plan shape at scale — the P10..P13.16 precedent).
Falsifiable guard: drop the 0040 index and the index-name /
no-Seq-Scan gates below fail.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import add_user, seed_actor, seed_member
from genesis.application.dividends import (
    approve_share_transfer,
    request_share_transfer,
    share_transfers_register_sql,
)
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_batch10.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_batch10_register_read_is_index_backed() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_a = await seed_member(tid, name="A", shares="1000.00")
        m_b = await seed_member(tid, name="B", shares="1000.00")
        # Real rows in BOTH bands so the plan touches data: one POSTED
        # (terminal band) and one PENDING (the register's leading band).
        async with tenant_session(factory(), tid) as session:
            posted = await request_share_transfer(
                session, tid, maker, m_a, to_member_id=m_b, amount=Decimal("10.00")
            )
        async with tenant_session(factory(), tid) as session:
            await approve_share_transfer(session, tid, checker, posted.id)
        async with tenant_session(factory(), tid) as session:
            await request_share_transfer(
                session, tid, maker, m_b, to_member_id=m_a, amount=Decimal("20.00")
            )

        # Keyset params matching everything under uniform-DESC order:
        # (true, +inf, max-uuid) is after every row.
        cursor_params: dict[str, object] = {
            "tid": str(tid),
            "c_flag": True,
            "c_ts": datetime(9999, 1, 1, tzinfo=UTC),
            "c_id": str(uuid.UUID(int=(1 << 128) - 1)),
            "limit": 50,
        }
        async with tenant_session(factory(), tid) as session:
            # Tiny-table planner discipline (the established precedent):
            # prove index servability, not tiny-table cost choices.
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            register_plan = await _explain(
                session, share_transfers_register_sql(with_cursor=True), cursor_params
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plan.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "Issue #31 batch 10 share-transfer register EXPLAIN (ANALYZE,\n"
            "BUFFERS) — captured in CI against the migrated Postgres service\n"
            "under the RLS app role. enable_seqscan/enable_sort=off because\n"
            "CI tables are tiny; the assertion is plan shape at scale: the\n"
            "register keyset walk is served by its 0040 expression index\n"
            "(pending-first band DESC, created_at DESC, id DESC), shipped\n"
            "in the same MR as the query (gate 1.3).\n"
        )
        OUT_PATH.write_text(
            f"{header}\n=== share-transfer register (pending-first keyset) ===\n{register_plan}\n"
        )

        assert "idx_share_transfers_register" in register_plan
        assert "Seq Scan" not in register_plan

    asyncio.run(run())
