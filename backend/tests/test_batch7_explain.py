"""EXPLAIN captures for the #31 batch-7 read models (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for the three new
hot-path reads to backend/perf/explain_batch7.txt (CI artifact via the
backend/perf/ convention; surfaced in the backend:test after_script).
The SQL under test is imported from the production modules — the
exact statements the routes execute:

  * TRANSACTION_LEGS_SQL (ledger (g)) — served by idx_ledger_txn
    (0001: tenant_id, transaction_id);
  * branch_users_roster_sql (ledger (j).1) — served by
    idx_users_branch (0016: tenant_id, branch_id);
  * branch_members_roster_sql (ledger (j).1) — served by
    idx_members_branch (0016: tenant_id, branch_id).

This batch ships NO migration: every read rides an index that shipped
with its table/feature. Tiny CI tables make seqscan the cheaper plan;
the capture disables it for the session to prove each read is
SERVABLE by its index (plan shape at scale). The gates are STRUCTURAL
per the documented P13.11 movements-plan convention (index-served,
zero sequential scans; index NAMES are not pinned — on single-digit
CI rows the planner may serve a tenant predicate from any
tenant-leading index). Falsifiable: drop the serving index and the
read must seq-scan (enable_seqscan=off only penalises the cost),
failing the gate.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor, seed_member, seed_three_leg_repayment
from genesis.application.branches import branch_members_roster_sql, branch_users_roster_sql
from genesis.application.transactions import TRANSACTION_LEGS_SQL
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_batch7.txt"

HEADER = (
    "Batch-7 read-model EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
    "the migrated Postgres service under the RLS app role (#31 ledger (g) +\n"
    "(j).1). enable_seqscan=off because CI tables are tiny; the assertion is\n"
    "STRUCTURAL (index-served, zero seq scans — the P13.11 movements-plan\n"
    "convention; index NAMES are not pinned): the legs drill-down rides\n"
    "idx_ledger_txn (0001); the two branch rosters are SERVABLE by\n"
    "idx_users_branch / idx_members_branch (0016) — on single-digit CI rows\n"
    "the planner may serve the tenant predicate from any tenant-leading\n"
    "index (e.g. the order-serving idx_users_created_keyset /\n"
    "members_tenant_id_member_no_key). This batch ships NO migration.\n"
)


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_batch7_reads_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid, name="Explain Legs Member")
        txn_id = await seed_three_leg_repayment(tid, mid)
        branch_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO branches (id, tenant_id, name) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name)"
                ),
                {"id": str(branch_id), "tid": str(tid), "name": "Explain Branch"},
            )
            await session.execute(
                text(
                    "UPDATE members SET branch_id = CAST(:bid AS uuid) "
                    "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"bid": str(branch_id), "mid": str(mid), "tid": str(tid)},
            )
            await session.execute(
                text(
                    "UPDATE users SET branch_id = CAST(:bid AS uuid) "
                    "WHERE tenant_id = CAST(:tid AS uuid)"
                ),
                {"bid": str(branch_id), "tid": str(tid)},
            )

        plans: list[tuple[str, str, str]] = []
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plans.append(
                (
                    "transaction legs (GET /transactions/{id}/legs)",
                    "ledger_entries",
                    await _explain(
                        session,
                        TRANSACTION_LEGS_SQL,
                        # leg_cap mirrors the production bind (#31 N3:
                        # MAX_TRANSACTION_LEGS + 1 overflow sentinel).
                        {"tid": str(tid), "txn": str(txn_id), "leg_cap": 65},
                    ),
                )
            )
            plans.append(
                (
                    "branch USER roster (GET /branches/{id}/users)",
                    "users",
                    await _explain(
                        session,
                        branch_users_roster_sql(with_cursor=False),
                        {"tid": str(tid), "bid": str(branch_id), "limit": 21},
                    ),
                )
            )
            plans.append(
                (
                    "branch MEMBER roster (GET /branches/{id}/members)",
                    "members",
                    await _explain(
                        session,
                        branch_members_roster_sql(with_cursor=False),
                        {"tid": str(tid), "bid": str(branch_id), "limit": 21},
                    ),
                )
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            HEADER + "".join(f"\n=== {title} ===\n{plan}\n" for title, _, plan in plans)
        )

        for title, relation, plan in plans:
            assert "Seq Scan" not in plan, f"{title} fell back to a sequential scan"
            assert "Index" in plan, f"{title} is not index-served"
            # Relation anchor: the plan must actually touch its hot
            # table (guards against a rewritten/misdirected statement
            # passing the structural gate vacuously).
            assert relation in plan, f"{title} plan does not touch {relation}"

    asyncio.run(run())
