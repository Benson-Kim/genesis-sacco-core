"""EXPLAIN capture for the P10 hot-path queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for the three new
hot-path queries to backend/perf/explain_p10.txt (published as a CI
artifact and pasted into the MR description):

  1. arrears batch scan (idx_loans_active_scan + idx_schedules_unpaid)
  2. loan book keyset page (idx_loans_created_keyset)
  3. portfolio aggregates (idx_loans_status)

Test data is tiny, so the planner would legitimately prefer a
sequential scan; the capture disables seqscan for the session to prove
the indexes actually serve these queries — i.e. the plans stay
index-backed once the tables grow. The assertions check exactly that.
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

from db_helpers import factory, seed_user, unique_email
from genesis.application.ledger import disburse_loan
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p10.txt"

ARREARS_BATCH_SCAN = """
SELECT l.id, l.days_past_due, l.classification, l.provision_pct,
(SELECT MIN(s.due_date) FROM loan_schedules s
 WHERE s.loan_id = l.id AND s.due_date <= :as_of
 AND s.paid_amount < s.total_due) AS oldest_unpaid
FROM loans l
WHERE l.status = 'active' AND l.id > CAST(:after AS uuid)
ORDER BY l.id LIMIT 200
"""

LOAN_BOOK_KEYSET_PAGE = """
SELECT created_at, id, application_id, member_id, product_id, principal,
balance, rate_pct, term_months, status, classification, days_past_due,
provision_pct, penalty_due, disbursed_at, closed_at, version
FROM loans
WHERE (created_at, id) < (:c_ts, CAST(:c_id AS uuid))
ORDER BY created_at DESC, id DESC LIMIT 21
"""

PORTFOLIO_AGGREGATES = """
SELECT count(*),
COALESCE(SUM(balance), 0),
COALESCE(SUM(balance) FILTER (WHERE classification IN
 ('substandard', 'doubtful', 'loss')), 0),
COALESCE(SUM(balance) FILTER (WHERE days_past_due > 30), 0),
COALESCE(SUM(ROUND(balance * provision_pct / 100, 2)), 0),
COALESCE(SUM(penalty_due), 0)
FROM loans WHERE status = 'active'
"""


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


async def _seed_book(tid: uuid.UUID) -> None:
    for n in range(3):
        mid = uuid.uuid4()
        pid = uuid.uuid4()
        app_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Explain Member')"
                ),
                {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
            )
            # Deposit account satisfying the P12.5 multiplier
            # precondition (issue #15): 100000.00 x 3.00 covers the
            # amounts disbursed below.
            await session.execute(
                text(
                    "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                    "CAST(:m AS uuid), '100000.00')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
            )
            await session.execute(
                text(
                    "INSERT INTO loan_products "
                    "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
                ),
                {"id": str(pid), "tid": str(tid), "name": f"Explain-{pid.hex[:6]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO loan_applications "
                    "(id, tenant_id, member_id, product_id, amount, term_months, "
                    " rate_pct, stage) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    "CAST(:pid AS uuid), :amount, 12, 12.00, 'approved')"
                ),
                {
                    "id": str(app_id),
                    "tid": str(tid),
                    "mid": str(mid),
                    "pid": str(pid),
                    "amount": str(Decimal(1000) * (n + 1)),
                },
            )
        async with tenant_session(factory(), tid) as session:
            await disburse_loan(session, tid, app_id, Channel.BANK)


def test_hot_path_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        await _seed_book(tid)
        sections: list[tuple[str, str]] = []
        async with tenant_session(factory(), tid) as session:
            # Tiny test tables make seqscan the cheaper plan; disabling it
            # proves the indexes can serve these queries at scale.
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            # Issue #20 / RF4 fence: every plan captured here is sort-free on
            # all observed runs (jobs 15662887952 / 15662909824 / 15665250796),
            # so enable_sort=off cannot change today's plans; it pins the
            # shape so a rows=0 cost tie can never swap an order-preserving
            # index scan for the bitmap-scan + Sort flake (the
            # test_p135_explain audit-page class).
            await session.execute(text("SET LOCAL enable_sort = off"))
            arrears_plan = await _explain(
                session,
                ARREARS_BATCH_SCAN,
                {
                    "as_of": datetime.now(UTC).date(),
                    "after": "00000000-0000-0000-0000-000000000000",
                },
            )
            keyset_plan = await _explain(
                session,
                LOAN_BOOK_KEYSET_PAGE,
                {"c_ts": datetime.now(UTC), "c_id": str(uuid.uuid4())},
            )
            aggregates_plan = await _explain(session, PORTFOLIO_AGGREGATES, {})
        sections.append(("arrears batch scan", arrears_plan))
        sections.append(("loan book keyset page", keyset_plan))
        sections.append(("portfolio aggregates", aggregates_plan))

        # The driving scans must use the P10 indexes. For the schedules
        # subquery any index-backed access is accepted: on 12-row test
        # tables the planner picks the smallest applicable index, while
        # idx_schedules_unpaid (partial on paid_amount < total_due) wins
        # once paid installments accumulate. What matters structurally is
        # that no relation falls back to a sequential scan.
        assert "idx_loans_active_scan" in arrears_plan
        assert "loan_schedules" in arrears_plan
        assert "Seq Scan" not in arrears_plan
        assert "idx_loans_created_keyset" in keyset_plan
        assert "Seq Scan" not in keyset_plan
        assert "idx_loans_active_scan" in aggregates_plan or "idx_loans_status" in aggregates_plan
        assert "Seq Scan" not in aggregates_plan

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P10 hot-path EXPLAIN (ANALYZE, BUFFERS) — captured in CI against the\n"
            "migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; enable_sort=off pins\n"
            "the observed sort-free shape (issue #20); the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

    asyncio.run(run())
