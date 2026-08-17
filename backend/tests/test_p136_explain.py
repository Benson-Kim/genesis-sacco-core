"""EXPLAIN capture for the P13.6 branches queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p136.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
module — these are the statements the endpoint and the backfill
actually run.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index — i.e. the
plans stay index-backed once the tables grow (P10-P13.5 precedent).
Falsifiable guard: drop the 0016 indexes and the no-sequential-scan
gate below fails.
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
from export_helpers import seed_actor
from genesis.application.branches import backfill_scan_sql, branches_page_sql
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p136.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p136_branch_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        now = datetime.now(UTC)

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            listing = await _explain(
                session,
                branches_page_sql(with_cursor=True),
                {
                    "tid": str(tid),
                    "c_ts": now,
                    "c_id": str(uuid.uuid4()),
                    "limit": 101,
                },
            )
            backfill = await _explain(
                session,
                backfill_scan_sql(with_after=True),
                {
                    "tid": str(tid),
                    "after": str(uuid.uuid4()),
                    "limit": 200,
                },
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans (1.3).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.6 branches EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("branches page (keyset)", listing),
            ("backfill scan (anti-join on the branch_id claim key)", backfill),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_branches_created_keyset" in listing
        # The partial index predicate IS the anti-join claim key, so a
        # completed backfill re-run walks an empty index (v1.1 rule 8).
        assert "idx_users_branch_backfill" in backfill
        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())
