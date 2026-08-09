"""EXPLAIN capture for the issue-#31 batch-6 corrections registers.

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1315_registers.txt (CI artifact, surfaced in the
job log and pasted into the MR description). The SQL under test is
imported from the production module — adjustments_register_sql /
write_offs_register_sql are the statements the two new LIST routes
(ledger (a).1/(a).2) actually run.

Index story (both shipped in the SAME migration as the queries —
0038, gate 1.3; the 0025/0031 index audit found nothing serving a
register-ordered keyset walk):

  * adjustments register: idx_repayment_adjustments_register
    (tenant_id, (status = 'pending_approval') DESC, created_at DESC,
    id DESC) — the pending-first band expression IS the index's
    second key, so the uniform-DESC keyset row comparison walks it in
    register order.
  * write-off register: idx_loan_write_offs_register (tenant_id,
    (status IN ('requested', 'approved')) DESC, created_at DESC,
    id DESC) — same shape for the committee's live-first band.

Tiny CI tables make seqscan/sort the cheaper plan; the capture
disables both for the session to prove each relation is SERVABLE by
its index (plan shape at scale — the P10..P13.16 precedent).
Falsifiable guards: drop either 0038 index and the matching
index-name / no-Seq-Scan gate below fails.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from genesis.application.corrections import (
    adjustments_register_sql,
    write_offs_register_sql,
)
from genesis.infrastructure.tenancy import tenant_session
from test_corrections import (
    _classify_npl,
    _disburse,
    _repay,
    _repayment_id_for_txn,
    _request,
    _request_write_off,
    _seed_actor,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1315_registers.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_batch6_register_reads_are_index_backed() -> None:
    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        # Real rows so every plan touches data: one PENDING adjustment
        # and one REQUESTED write-off (each register's leading band).
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor, loan_id, "1000.00")
        await _request(tid, actor, await _repayment_id_for_txn(tid, repayment.txn_id))
        wo_loan, _ = await _disburse(tid)
        await _classify_npl(tid, wo_loan)
        await _request_write_off(tid, actor, wo_loan)

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
            adjustments_plan = await _explain(
                session, adjustments_register_sql(with_cursor=True), cursor_params
            )
            write_offs_plan = await _explain(
                session, write_offs_register_sql(with_cursor=True), cursor_params
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "Issue #31 batch 6 corrections registers EXPLAIN (ANALYZE,\n"
            "BUFFERS) — captured in CI against the migrated Postgres service\n"
            "under the RLS app role. enable_seqscan/enable_sort=off because\n"
            "CI tables are tiny; the assertion is plan shape at scale: each\n"
            "register keyset walk is served by its 0038 expression index\n"
            "(pending-first / live-first band DESC, created_at DESC,\n"
            "id DESC), shipped in the same MR as the queries (gate 1.3).\n"
        )
        sections = [
            ("adjustments register (pending-first keyset)", adjustments_plan),
            ("write-off register (live-first keyset)", write_offs_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_repayment_adjustments_register" in adjustments_plan
        assert "Seq Scan" not in adjustments_plan
        assert "idx_loan_write_offs_register" in write_offs_plan
        assert "Seq Scan" not in write_offs_plan

    asyncio.run(run())
