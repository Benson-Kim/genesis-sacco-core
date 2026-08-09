"""EXPLAIN capture for the queries touched by the Codex-review MR (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_codex_review.txt (CI artifact, pasted into the MR
description):

  1. stage-filtered applications keyset page — served by the NEW
     idx_applications_stage_keyset (0014). Before 0014 the scan walked
     the unfiltered keyset index discarding non-matching rows
     (docs/DSA_HARDENING.md DSA-7).
  2. unfiltered applications keyset page — must STILL be served by
     idx_applications_created_keyset (0006). This assertion is the
     regression fence against the original Codex fix, which REPLACED
     0006 with the stage-shaped index and would have forced a sort
     here.
  3. repayment-by-transaction lookup (the post_reversal repayment
     guard) — served by the NEW idx_repayments_transaction (0014;
     previously an unindexed FK).

The tables are seeded with enough rows spread across stages that the
planner's index choice is driven by shape, and enable_seqscan is
disabled for the session to prove each query is servable by its index
(plan shape at scale — P10-P12.5 precedent).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory, seed_user, unique_email
from genesis.application.loan_applications import list_applications
from genesis.domain.lending import ApplicationStage
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_codex_review.txt"

_COLS = (
    "id, member_id, product_id, amount, term_months, rate_pct, purpose, stage, cover_pct, version"
)

#: The exact statement shapes list_applications builds (values bound).
STAGE_FILTERED_PAGE = f"""
SELECT created_at, {_COLS} FROM loan_applications
WHERE tenant_id = CAST(:tid AS uuid) AND stage = :stage
ORDER BY created_at DESC, id DESC LIMIT :limit
"""  # noqa: S608 - static column list from code

UNFILTERED_PAGE = f"""
SELECT created_at, {_COLS} FROM loan_applications
WHERE tenant_id = CAST(:tid AS uuid)
ORDER BY created_at DESC, id DESC LIMIT :limit
"""  # noqa: S608 - static column list from code

REPAYMENT_BY_TXN = """
SELECT 1 FROM repayments
WHERE transaction_id = CAST(:id AS uuid)
AND tenant_id = CAST(:tid AS uuid) LIMIT 1
"""


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


async def _seed_applications(tid: uuid.UUID, per_stage: int) -> None:
    """Spread rows across every stage so index choice reflects shape."""
    mid = uuid.uuid4()
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Explain Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Explain-{pid.hex[:6]}"},
        )
        for stage in ApplicationStage:
            for _ in range(per_stage):
                await session.execute(
                    text(
                        "INSERT INTO loan_applications "
                        "(id, tenant_id, member_id, product_id, amount, term_months, "
                        " rate_pct, stage) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                        "CAST(:pid AS uuid), 1000.00, 12, 12.00, :stage)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "mid": str(mid),
                        "pid": str(pid),
                        "stage": stage.value,
                    },
                )


def test_codex_review_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        await _seed_applications(tid, per_stage=30)

        # Sanity: the service path returns a full page for the rare
        # stage too (the query under EXPLAIN is the one it runs).
        async with tenant_session(factory(), tid) as session:
            items, _cursor = await list_applications(
                session, tid, stage=ApplicationStage.APPROVED, limit=20
            )
        assert len(items) == 20

        async with tenant_session(factory(), tid) as session:
            # Freshly seeded tables have no stats (ANALYZE needs table
            # ownership, which the RLS app role deliberately lacks), so
            # the planner's row estimates are unusable for cost-based
            # index CHOICE. Disabling seqscan AND sort turns the capture
            # into the plan-SHAPE assertion that matters: each query is
            # servable by its index in index order, no sort node.
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            stage_plan = await _explain(
                session,
                STAGE_FILTERED_PAGE,
                {"tid": str(tid), "stage": "approved", "limit": 20},
            )
            unfiltered_plan = await _explain(
                session,
                UNFILTERED_PAGE,
                {"tid": str(tid), "limit": 20},
            )
            repayment_plan = await _explain(
                session,
                REPAYMENT_BY_TXN,
                {"id": str(uuid.uuid4()), "tid": str(tid)},
            )

        # 0014: the stage filter is served by the stage-leading keyset
        # index with NO sort node (order comes from the index).
        assert "idx_applications_stage_keyset" in stage_plan
        assert "Sort" not in stage_plan
        assert "Seq Scan" not in stage_plan
        # 0006 must keep serving the unfiltered page (regression fence
        # against replacing it with the stage-shaped index).
        assert "idx_applications_created_keyset" in unfiltered_plan
        assert "Sort" not in unfiltered_plan
        assert "Seq Scan" not in unfiltered_plan
        # 0014: the reversal guard lookup is index-backed.
        assert "idx_repayments_transaction" in repayment_plan
        assert "Seq Scan" not in repayment_plan

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "Codex-review MR EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
            "the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off and enable_sort=off (fresh tables carry no stats;\n"
            "the assertion is plan shape: index-served order, no sort node).\n"
        )
        sections = [
            ("stage-filtered applications keyset page (0014 index)", stage_plan),
            ("unfiltered applications keyset page (0006 index kept)", unfiltered_plan),
            ("repayment-by-transaction lookup (post_reversal guard)", repayment_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

    asyncio.run(run())
