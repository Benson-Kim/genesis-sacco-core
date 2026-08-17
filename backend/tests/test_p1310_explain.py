"""EXPLAIN capture for every P13.10 report query (blocker j, gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1310.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
module — the plans below are the statements the exports actually run.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index — i.e. the
plans stay index-backed once the tables grow (P10-P13 precedent).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor, seed_member
from genesis.application import transactions as txn_service
from genesis.application.reports import (
    PAR_AGING_SQL,
    account_activity_sql,
    membership_register_page_sql,
)
from genesis.domain.ledger import Account, Channel
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import _seed_loan_for_book

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1310.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1310_report_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )
        loan_id = await _seed_loan_for_book(
            tid, mid, principal="1000.00", balance="1000.00", disbursed_days_ago=90
        )
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO loan_schedules "
                    "(id, tenant_id, loan_id, installment_no, due_date, "
                    " principal_due, interest_due, total_due) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                    "1, :due, 100, 0, 100)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "lid": str(loan_id),
                    "due": (datetime.now(UTC) - timedelta(days=45)).date(),
                },
            )

        now = datetime.now(UTC)
        base: dict[str, object] = {"tid": str(tid), "as_of": now}

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            par_aging = await _explain(
                session,
                PAR_AGING_SQL,
                {
                    **base,
                    "d_date": now.date(),
                    "receivable_account": Account.LOANS_RECEIVABLE.value,
                },
            )
            register_page = await _explain(
                session,
                membership_register_page_sql(with_cursor=True),
                {
                    **base,
                    "c_ts": now,
                    "c_id": str(uuid.uuid4()),
                    "limit": 501,
                },
            )
            # Mirrors the production binds: _account_activity binds
            # explicit UTC datetimes, never bare dates (!40 review R3).
            activity_windowed = await _explain(
                session,
                account_activity_sql(with_from=True, with_to=True),
                {
                    **base,
                    "d_from": now - timedelta(days=30),
                    "d_to_excl": now + timedelta(days=1),
                },
            )
            activity_full = await _explain(
                session, account_activity_sql(with_from=False, with_to=False), base
            )

        # The file, then the assertions: the CI artifact always
        # carries the full plans, even when an assertion below trips
        # (blocker j: EXPLAIN evidence).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.10 report EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
            "the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("portfolio-at-risk aging reconstruction", par_aging),
            ("membership register page (keyset)", register_page),
            ("account activity aggregate (period-scoped)", activity_windowed),
            ("account activity aggregate (full as-of)", activity_full),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        # The register keyset page must ride its 0023 index (shipped
        # in the same MR as the query, gate 1.3).
        assert "idx_members_register_keyset" in register_page
        # PAR aging rides the NPL-trend index set (0001/0013). The
        # schedules CTE may be served by idx_schedules_due (due-led
        # range scan) or idx_schedules_loan (loan-led nested loop) —
        # the planner flips between them on near-empty CI tables (the
        # test_p13_explain disb_coll precedent; observed run-to-run on
        # identical SQL). Both are tenant-led composite indexes from
        # 0001; the falsifiable guard is the no-Seq-Scan gate below.
        # Third legitimate serve (observed pipeline 2724760307 job
        # 15662887952, where THIS assertion failed its then-two-way
        # oracle alongside the p13 twin of this CTE): the 0001 UNIQUE
        # (tenant_id, loan_id, installment_no) backing index — the same
        # tenant+loan-led nested-loop path as idx_schedules_loan on
        # cost ties. Green runs: pipeline 2724765474 job 15662909824
        # and main pipeline 2725233129 job 15665250796. Per issue
        # #20/RF4, only OBSERVED index-backed serves are accepted — an
        # unobserved plan must fail here and be dispositioned, never
        # pre-accepted.
        assert (
            "idx_schedules_due" in par_aging
            or "idx_schedules_loan" in par_aging
            or "loan_schedules_tenant_id_loan_id_installment_no_key" in par_aging
        )
        # The ledger aggregates must be index-served (trial-balance
        # precedent: any of the composite ledger/transactions indexes).
        assert "Index" in activity_windowed
        assert "Index" in activity_full
        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())
