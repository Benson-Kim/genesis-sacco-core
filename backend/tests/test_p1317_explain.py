"""EXPLAIN capture for the P13.17 (DSA hardening) queries (gate 1.3).

Writes real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1317.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
modules — the plans below are the statements the code actually runs.
Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index (the
P10-P13 precedent).

Row-count evidence (P13.17 before/after): the NPL-trend export now
executes ONE snapshot lookup (N index probes) plus ONE current-month
reconstruction, instead of `months` full-history reconstructions —
asserted structurally in tests/test_p1317_portfolio_snapshots.py via
the snapshot-read probe.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.idempotency_purge import PURGE_BATCH_SQL
from genesis.application.period_rollups import (
    MEMBER_ANCHOR_SQL,
    TRIAL_BALANCE_ROLLUP_SQL,
)
from genesis.application.portfolio_snapshots import SNAPSHOT_LOOKUP_SQL
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1317.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1317_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        now = datetime.now(UTC)
        months = [
            (now.date().replace(day=1) - timedelta(days=1)),
            ((now.date().replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)),
        ]

        sections: list[tuple[str, str]] = []
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            snapshot_lookup = await _explain(
                session,
                SNAPSHOT_LOOKUP_SQL,
                {"tid": str(tid), "months": months},
            )
            sections.append(("DSA-1 snapshot lookup", snapshot_lookup))
            trial_rollup = await _explain(
                session,
                TRIAL_BALANCE_ROLLUP_SQL,
                {"tid": str(tid), "as_of": now},
            )
            sections.append(("DSA-2 trial balance (rollups + live remainder)", trial_rollup))
            anchor = await _explain(
                session,
                MEMBER_ANCHOR_SQL,
                {"tid": str(tid), "mid": str(tid), "before": now.date()},
            )
            sections.append(("DSA-5 statement opening anchor", anchor))
            # The purge DELETE executes against this tenant's (empty)
            # key set - zero rows removed; the plan is what matters.
            purge = await _explain(
                session,
                PURGE_BATCH_SQL,
                {"tid": str(tid), "limit": 100},
            )
            sections.append(("DSA-3 idempotency purge (batched DELETE)", purge))

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            "\n\n".join(f"-- {title}\n{plan}" for title, plan in sections),
            encoding="utf-8",
        )

        # The lookup must be served by the UNIQUE (tenant_id,
        # month_end) index, never a (forced-off) seq scan.
        assert "uq_portfolio_snapshots_month" in snapshot_lookup
        # The rolled CTE scans its UNIQUE index; the anchor probe walks
        # idx_member_period_balances_anchor backwards.
        assert "uq_account_period_balances" in trial_rollup
        assert "idx_member_period_balances_anchor" in anchor
        # The purge's driving subquery walks its 0029 index, never a
        # (forced-off) seq scan.
        assert "idx_idempotency_keys_expiry" in purge

    asyncio.run(run())
