"""EXPLAIN capture for the P12.5 hot-path queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p125.txt (CI artifact, pasted into the MR
description):

  1. closed-period lookup — runs on EVERY ledger posting
     (ledger._post -> assert_open_period); served by the UNIQUE
     (tenant_id, period_start) index shipped in 0012 (the partial-
     unique-doubles-as-lookup precedent from uq_member_exits_open).
  2. application guarantee total — the disbursement multiplier gate
     (issue #15); served by idx_guarantees_application (0001 — index
     verified as already shipped, not added).

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is servable by an index — i.e. the
plans stay index-backed once the tables grow (P10-P12 precedent).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory, seed_user, unique_email
from genesis.application import accounting_periods as periods_service
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p125.txt"

CLOSED_PERIOD_LOOKUP = """
SELECT 1 FROM accounting_periods
WHERE tenant_id = CAST(:tid AS uuid) AND status = 'closed'
AND :d >= period_start AND :d <= period_end LIMIT 1
"""

GUARANTEE_TOTAL = """
SELECT COALESCE(SUM(amount), 0) FROM guarantees
WHERE application_id = CAST(:a AS uuid)
AND tenant_id = CAST(:tid AS uuid)
AND status IN ('pledged', 'active')
"""


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p125_hot_path_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        # One closed period so the lookup has a real row to find.
        first_of_this_month = datetime.now(UTC).date().replace(day=1)
        prev = first_of_this_month - timedelta(days=1)
        async with tenant_session(factory(), tid) as session:
            await periods_service.close_period(
                session,
                tid,
                None,
                year=prev.year,
                month=prev.month,
            )
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            period_plan = await _explain(
                session,
                CLOSED_PERIOD_LOOKUP,
                {"tid": str(tid), "d": prev},
            )
            guarantee_plan = await _explain(
                session,
                GUARANTEE_TOTAL,
                {"tid": str(tid), "a": str(uuid.uuid4())},
            )

        # Capture the artifact BEFORE any assertion (the p135 house
        # rule) so the CI artifact always carries the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P12.5 hot-path EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
            "the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("closed-period lookup (every posting)", period_plan),
            ("application guarantee total (disbursement gate)", guarantee_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "accounting_periods_tenant_id_period_start_key" in period_plan
        assert "Seq Scan" not in period_plan
        # Issue-#20 flake class, re-armed by 0035: at rows=0 every
        # tenant-led guarantees index cost-ties, and the two new
        # consent-principal indexes (idx_guarantees_consent_credential /
        # _attestor) widened the tie pool — the planner was OBSERVED
        # serving this aggregate from one of them with the application
        # filter pushed down (pipeline 2731924389, job 15710350280).
        # The !46/p135 filtered-page precedent: accept any tenant-led
        # guarantees index shipped with the schema; the falsifiable
        # gate stays the no-sequential-scan check (drop the indexes and
        # a Seq Scan is the only serve, even under enable_seqscan=off).
        assert "idx_guarantees_" in guarantee_plan
        assert "Seq Scan" not in guarantee_plan

    asyncio.run(run())
