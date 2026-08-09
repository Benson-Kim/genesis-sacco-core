"""EXPLAIN captures for the #31 batch-8 KPI-trend reads (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for the two
statements the kpi_trends slice executes per cutoff to
backend/perf/explain_batch8.txt (CI artifact via the backend/perf/
convention; surfaced in the backend:test after_script). The SQL under
test is imported from the production modules:

  * MEMBERS_ACTIVE_AT_SQL (ledger (k), members series) — the driving
    member scan is servable by the tenant-leading members indexes
    (0001); the correlated status-fact probe by idx_audit_entity
    (0001: tenant_id, entity, entity_id).
  * NPL_TREND_MONTH_SQL at the PAR threshold (dpd_days=30, ledger (k),
    PAR-30 series) — the SAME single reconstruction statement
    test_p13_explain.py has always proven at the NPL threshold; the
    capture here re-proves the plan shape is threshold-independent (a
    bound parameter cannot change the access paths).

Index audit verdict (recorded per the batch-8 hand-off): every
relation is servable by an index that shipped with its table in 0001,
so scope (B) ships NO migration (the reserved 0040 index-only claim is
NOT exercised). Tiny CI tables make seqscan the cheaper plan; the
capture disables it for the session to prove each read is SERVABLE by
its index (plan shape at scale). Gates are STRUCTURAL per the
documented P13.11 movements-plan convention (index-served, zero
sequential scans; index NAMES are not pinned — on single-digit CI rows
the planner may serve a tenant predicate from any tenant-leading
index). Falsifiable: drop a relation's indexes and its plan must
seq-scan (enable_seqscan=off only penalises the cost), failing the
gate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor, seed_member
from genesis.application.dashboard import MEMBERS_ACTIVE_AT_SQL
from genesis.application.portfolio_reconstruction import NPL_TREND_MONTH_SQL
from genesis.domain.lending import PAR_DPD_DAYS
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_batch8.txt"

HEADER = (
    "Batch-8 KPI-trend EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
    "the migrated Postgres service under the RLS app role (#31 ledger (k)).\n"
    "enable_seqscan=off because CI tables are tiny; the assertion is\n"
    "STRUCTURAL (index-served, zero seq scans — the P13.11 movements-plan\n"
    "convention; index NAMES are not pinned): the members-at-cutoff count\n"
    "drives off the tenant-leading members indexes (0001) with the\n"
    "status-fact probe on idx_audit_entity (0001); the PAR-30 point is the\n"
    "single reconstruction statement (threshold as a bound parameter) whose\n"
    "serving indexes test_p13_explain.py pins. Scope (B) ships NO\n"
    "migration — the reserved 0040 index-only claim is NOT exercised.\n"
)


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_batch8_kpi_trend_statements_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        # One status fact so the audit probe has a row to walk.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO audit_log "
                    "(tenant_id, actor_id, action, entity, entity_id, before, after) "
                    "VALUES (CAST(:tid AS uuid), NULL, 'member.status', 'members', :eid, "
                    'CAST(\'{"status": "active"}\' AS jsonb), '
                    'CAST(\'{"status": "arrears"}\' AS jsonb))'
                ),
                {"tid": str(tid), "eid": str(mid)},
            )

        now = datetime.now(UTC)
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            members_plan = await _explain(
                session,
                MEMBERS_ACTIVE_AT_SQL,
                {"tid": str(tid), "d_next": now},
            )
            par_plan = await _explain(
                session,
                NPL_TREND_MONTH_SQL,
                {
                    "tid": str(tid),
                    "d_next": now,
                    "d_date": now.date(),
                    "receivable_account": "loans.receivable",
                    "dpd_days": PAR_DPD_DAYS,
                },
            )

        # Artifact BEFORE assertions (blocker-j convention): the CI job
        # log and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        sections = [
            ("active members at cutoff (status-transition facts)", members_plan),
            ("PAR-30 month-end reconstruction (dpd_days=30)", par_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{HEADER}\n{body}\n")

        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} fell back to a sequential scan"
            assert "Index" in plan, f"{name} is not index-served"
        # Relation anchors (the P13.9 convention): each plan must
        # actually touch its relations — a rewritten statement passing
        # the structural gate vacuously fails here.
        assert "members" in members_plan
        assert "audit_log" in members_plan
        assert "loans" in par_plan
        assert "loan_schedules" in par_plan

    asyncio.run(run())
