"""EXPLAIN capture for every P13 report/export query (blocker j, gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p13.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
modules — the plans below are the statements the exports actually run.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index — i.e. the
plans stay index-backed once the tables grow (P10-P12.5 precedent).
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

from db_helpers import factory
from export_helpers import seed_actor, seed_member
from genesis.application import transactions as txn_service
from genesis.application.exports import CLAIM_SQL, DOWNLOAD_SQL
from genesis.application.reports import (
    NPL_TREND_MONTH_SQL,
    TRIAL_BALANCE_SQL,
    disbursement_collections_page_sql,
    loan_book_page_sql,
    member_statement_opening_sql,
    member_statement_page_sql,
)
from genesis.domain.ledger import Channel, TxnType
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p13.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p13_report_and_export_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )
        now = datetime.now(UTC)
        base: dict[str, object] = {"tid": str(tid), "as_of": now}

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            statement_page = await _explain(
                session,
                member_statement_page_sql(with_from=True, with_to=True, with_cursor=True),
                {
                    **base,
                    "mid": str(mid),
                    "d_from": now.date(),
                    "d_to_excl": now.date(),
                    "c_ts": now,
                    "c_id": str(uuid.uuid4()),
                    "limit": 501,
                },
            )
            statement_opening = await _explain(
                session,
                member_statement_opening_sql(with_from=True),
                {**base, "mid": str(mid), "d_from": now.date()},
            )
            trial_balance = await _explain(session, TRIAL_BALANCE_SQL, base)
            loan_book = await _explain(
                session,
                loan_book_page_sql(with_cursor=True),
                {"tid": str(tid), "c_ts": now, "c_id": str(uuid.uuid4()), "limit": 501},
            )
            disb_coll = await _explain(
                session,
                disbursement_collections_page_sql(with_from=True, with_to=True, with_cursor=True),
                {
                    **base,
                    "t_disbursement": TxnType.LOAN_DISBURSEMENT.value,
                    "t_repayment": TxnType.LOAN_REPAYMENT.value,
                    "d_from": now.date(),
                    "d_to_excl": now.date(),
                    "c_ts": now,
                    "c_id": str(uuid.uuid4()),
                    "limit": 501,
                },
            )
            npl_month = await _explain(
                session,
                NPL_TREND_MONTH_SQL,
                {
                    "tid": str(tid),
                    "d_next": now,
                    "d_date": now.date(),
                    "receivable_account": "loans.receivable",
                },
            )
            claim = await _explain(session, CLAIM_SQL, {"tid": str(tid)})
            download = await _explain(
                session, DOWNLOAD_SQL, {"tid": str(tid), "token": "probe-token"}
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans, even
        # when an assertion below trips (blocker j: EXPLAIN evidence).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13 report/export EXPLAIN (ANALYZE, BUFFERS) — captured in CI against\n"
            "the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("member statement page (keyset)", statement_page),
            ("member statement opening balance", statement_opening),
            ("trial balance aggregate", trial_balance),
            ("loan book page (keyset)", loan_book),
            ("disbursements & collections page (keyset)", disb_coll),
            ("NPL trend month-end reconstruction", npl_month),
            ("export job claim (SKIP LOCKED)", claim),
            ("artifact download token lookup", download),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        # Deterministic single-index paths assert their backing index
        # (shipped in the same MR as the query, gate 1.3); every plan
        # must be servable without a sequential scan.
        assert (
            "idx_txns_member_keyset" in statement_page
            or "idx_transactions_member" in statement_page
        )
        # The opening aggregate groups by type, so the planner may pick
        # any of the transactions composite indexes; the gate is that
        # SOME index serves it (no sequential scan, asserted below).
        assert "Index" in statement_opening
        assert "idx_loans_created_keyset" in loan_book
        # The disbursements & collections page is keyset-ordered on
        # (occurred_at, id), so the planner may serve it either from
        # idx_txns_type_occurred (0013: the selective path when the
        # two loan types are rare) or from the P11 keyset index
        # idx_txns_occurred_keyset scanned in order with type as a
        # filter — the cheaper plan on near-empty CI tables. Both are
        # composite tenant-led indexes shipped with their queries; the
        # falsifiable guard is the no-sequential-scan gate below
        # (drop both indexes and this test fails on the Seq Scan).
        assert "idx_txns_type_occurred" in disb_coll or "idx_txns_occurred_keyset" in disb_coll
        # The schedules CTE (tenant_id + due_date <= cutoff, joined per
        # loan) may be served by idx_schedules_due (the due-led range
        # scan) or by idx_schedules_loan (the loan-led nested loop) —
        # the planner flips between them on near-empty CI tables (the
        # disb_coll precedent above; observed flipping run-to-run on
        # BYTE-IDENTICAL SQL and test code, !40 pipelines 2723985005
        # vs 2724004325). Both are tenant-led composite indexes
        # shipped with the query (0001); the falsifiable guard stays
        # the no-Seq-Scan gate below (drop both indexes -> Seq Scan).
        # A third legitimate serve (observed pipeline 2724760307): the
        # 0001 UNIQUE (tenant_id, loan_id, installment_no) backing
        # index — the same tenant+loan-led nested-loop access path as
        # idx_schedules_loan, chosen on cost ties over near-empty rows.
        assert (
            "idx_schedules_due" in npl_month
            or "idx_schedules_loan" in npl_month
            or "loan_schedules_tenant_id_loan_id_installment_no_key" in npl_month
        )
        assert "idx_exports_requested" in claim
        assert "csv_token" in download
        assert "pdf_token" in download
        for name, plan in (
            ("member statement page", statement_page),
            ("member statement opening", statement_opening),
            ("trial balance", trial_balance),
            ("loan book page", loan_book),
            ("disbursements & collections page", disb_coll),
            ("npl trend month", npl_month),
            ("export job claim", claim),
            ("artifact token lookup", download),
        ):
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())
