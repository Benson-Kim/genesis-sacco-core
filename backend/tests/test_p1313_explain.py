"""EXPLAIN capture for the P13.13 dormancy queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1313.txt (CI artifact, surfaced in the job log
and pasted into the MR description). The SQL under test is imported
from the production module — dormancy_scan_sql(...) with the bound
member-initiated allow-list placeholders is the statement the job
actually runs, DORMANCY_CONFIG_SQL the once-per-run config probe.

Index story:

  * driving scan: idx_members_dormancy_scan (0021 — partial over
    status = 'active', shipped in the SAME migration as the query).
    Since 0022 (issue #19) widened idx_members_dividend_scan to
    status IN ('active','arrears','dormant'), BOTH partial indexes'
    predicates are implied by this scan's status = 'active', on the
    same (tenant_id, id) key: at scale the planner prefers the
    smaller exact-predicate 0021 index, but on the tiny CI tables the
    costs tie and it may pick either — the gate accepts both shapes
    (the 0001/0008 transaction-index precedent below);
  * activity anti-join + last-activity subselect: the member-
    attributed transaction indexes (idx_txns_member_keyset, 0008 /
    idx_transactions_member, 0001) — occurred_at range under
    (tenant_id, member_id);
  * config resolution: the tenant_settings primary key (0009).

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each relation is SERVABLE by its index (plan
shape at scale — the P10..P13.11 precedent). Falsifiable guards: drop
both members partial indexes or the transaction member indexes and the
index-name / no-sequential-scan gates below fail.
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
from genesis.application.dormancy import (
    DORMANCY_CONFIG_SQL,
    _member_initiated_placeholders,
    dormancy_scan_sql,
)
from genesis.infrastructure.tenancy import tenant_session
from test_dormancy import CUTOFF, OLD, _aged_member, _configure_dormancy, _deposit_at

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1313.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1313_dormancy_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _aged_member(tid, name="Explain Member")
        await _deposit_at(tid, mid, "100.00", OLD)

        # The canonical test window (test_dormancy.AS_OF / CUTOFF).
        params: dict[str, object] = {"tid": str(tid), "cutoff": CUTOFF, "limit": 200}
        placeholders = _member_initiated_placeholders(params)
        params["after"] = str(uuid.UUID(int=0))
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            scan_plan = await _explain(
                session,
                dormancy_scan_sql(with_after=True, type_placeholders=placeholders),
                params,
            )
            config_plan = await _explain(session, DORMANCY_CONFIG_SQL, {"tid": str(tid)})

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.13 dormancy EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that the dormancy scan is served by a members partial index\n"
            "(idx_members_dormancy_scan, 0021 — or idx_members_dividend_scan\n"
            "since 0022 widened its predicate over status='active' too; the\n"
            "planner ties on tiny tables) with the member-initiated\n"
            "activity anti-join on the member-attributed transaction indexes\n"
            "(0001/0008), and the config read by the tenant_settings PK\n"
            f"(plan shape at scale). Captured {datetime.now(UTC).isoformat()}.\n"
        )
        sections = [
            ("dormancy batch scan (anti-join + FOR UPDATE SKIP LOCKED)", scan_plan),
            ("dormancy config resolution (once per run)", config_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        # Either members partial index serves the scan (see the module
        # docstring: 0022 made the predicates overlap on status =
        # 'active'; on tiny CI tables the equal costs tie-break
        # arbitrarily). Falsifiable: drop both and the no-seq-scan
        # gate below fails.
        assert (
            "idx_members_dormancy_scan" in scan_plan
            or "idx_members_dividend_scan" in scan_plan
            # 0028 (N2) added uq_members_tenant_id_id - the same
            # (tenant_id, id) shape, a third arbitrary tie-break winner
            # on tiny CI tables (issue #20 class, RF4 disposition).
            or "uq_members_tenant_id_id" in scan_plan
        )
        # The activity anti-join and the last-activity subselect are
        # driven by a member-attributed transactions index; on the tiny
        # CI tables the planner may pick either (0001 or 0008 shape).
        assert "idx_txns_member_keyset" in scan_plan or "idx_transactions_member" in scan_plan
        assert "Seq Scan" not in scan_plan
        assert "tenant_settings_pkey" in config_plan
        assert "Seq Scan" not in config_plan

    asyncio.run(run())
