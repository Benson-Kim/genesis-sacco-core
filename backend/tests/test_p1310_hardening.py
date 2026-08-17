"""P13.10 cross-report hardening: read-only proof, tenant predicates,
idempotency (H6/H7 + P13 blockers c/g inherited per new report).

H6 — reports are READ-ONLY: rendering each new report writes NOTHING
except the export job/artifact/audit/outbox rows, proven by
side-effect row counts over every domain table (never return values).

H7 — issue-#17 probes: every new report builder carries an explicit
bound tenant_id predicate on top of forced RLS. Each probe opens the
session AS the data-owning tenant (so RLS can never mask a missing
predicate) and calls the builder with a FOREIGN tenant argument —
only the explicit predicate can produce the zero/empty result.
Falsifiable: remove any tenant predicate from the report SQL and its
probe fails (the RLS-visible rows leak into the foreign-argument
render).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import count, drain_export_queue, seed_actor, seed_member
from genesis.application.exports import run_export
from genesis.application.ledger import post_loan_interest_accrual
from genesis.application.reports import REPORTS, ExportFilters, ReportName
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import _seed_loan_for_book, request_and_render

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: Every tenant-owned domain table a report could conceivably touch.
_DOMAIN_TABLES = (
    "members",
    "share_accounts",
    "deposit_accounts",
    "loans",
    "loan_schedules",
    "repayments",
    "transactions",
    "ledger_entries",
    "dividend_distributions",
)

_NEW_REPORTS = (
    "portfolio_at_risk_aging",
    "membership_register",
    "income_statement",
    "sasra_return",
)


async def _table_counts(tid: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _DOMAIN_TABLES:
        # Table names from the code-owned tuple above, never input.
        counts[table] = await count(tid, f"SELECT count(*) FROM {table}")  # noqa: S608
    return counts


def test_h6_reports_write_nothing_but_export_rows() -> None:
    """Side-effect deltas for each of the four report renders: every
    domain table unchanged; exactly +1 exports row, +1 artifact,
    +2 audit rows (export.request + export.completed), +2 outbox
    events (export.requested + export.completed). Falsifiable: make
    any builder write (or lock-and-touch) a domain row and its table
    delta breaks."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Readonly Member")
        async with tenant_session(factory(), tid) as session:
            await post_loan_interest_accrual(session, tid, mid, Decimal("50.00"))
        await _seed_loan_for_book(tid, mid, principal="1000.00", balance="1000.00")

        for report in _NEW_REPORTS:
            before = await _table_counts(tid)
            exports_before = await count(tid, "SELECT count(*) FROM exports")
            artifacts_before = await count(tid, "SELECT count(*) FROM export_artifacts")
            audit_before = await count(tid, "SELECT count(*) FROM audit_log")
            outbox_before = await count(tid, "SELECT count(*) FROM outbox_events")

            _, completed = await request_and_render(token, tid, {"report": report})
            assert completed["status"] == "completed", report

            assert await _table_counts(tid) == before, report
            assert await count(tid, "SELECT count(*) FROM exports") == exports_before + 1
            artifacts_after = await count(tid, "SELECT count(*) FROM export_artifacts")
            assert artifacts_after == artifacts_before + 1
            assert await count(tid, "SELECT count(*) FROM audit_log") == audit_before + 2
            assert await count(tid, "SELECT count(*) FROM outbox_events") == outbox_before + 2

    asyncio.run(run())


def test_h7_issue_17_probe_every_new_report_builder() -> None:
    """Session AS tenant A (which owns a member, an income posting and
    an overdue loan), builders called with FOREIGN tenant B's id:
    every render is empty/zero — never a mixed or leaked figure."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        tid_b, _, _ = await seed_actor()
        mid = await seed_member(tid_a, name="Tenant A Member")
        async with tenant_session(factory(), tid_a) as session:
            await post_loan_interest_accrual(session, tid_a, mid, Decimal("75.00"))
        loan_id = await _seed_loan_for_book(
            tid_a, mid, principal="4000.00", balance="4000.00", disbursed_days_ago=120
        )
        async with tenant_session(factory(), tid_a) as session:
            await session.execute(
                text(
                    "INSERT INTO loan_schedules "
                    "(id, tenant_id, loan_id, installment_no, due_date, "
                    " principal_due, interest_due, total_due) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                    "1, :due, 1000, 0, 1000)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid_a),
                    "lid": str(loan_id),
                    "due": (datetime.now(UTC) - timedelta(days=45)).date(),
                },
            )

        as_of = datetime.now(UTC)

        async def render(report: ReportName, probe_tid: uuid.UUID) -> list[tuple[object, ...]]:
            # Session AS tenant A; the report argument is the probe.
            async with tenant_session(factory(), tid_a) as session:
                query = await REPORTS[report].build(session, probe_tid, ExportFilters(), as_of)
                run_result = await run_export(query, 100, row_cap=1000)
            return [tuple(row) for row in run_result.rows]

        # Control arm: tenant A's own renders DO carry its data (so
        # the zero results below cannot be vacuous). The PAR vector
        # pins the full post-R1 bucket shape: "current" is its own
        # row, the 45-dpd loan sits in "31-90" (6 buckets + TOTAL).
        assert len(await render(ReportName.MEMBERSHIP_REGISTER, tid_a)) == 1
        own_par = await render(ReportName.PORTFOLIO_AT_RISK_AGING, tid_a)
        assert own_par == [
            ("current", 0, Decimal("0.00"), Decimal("0.00")),
            ("1-30", 0, Decimal("0.00"), Decimal("0.00")),
            ("31-90", 1, Decimal("4000.00"), Decimal("100.00")),
            ("91-180", 0, Decimal("0.00"), Decimal("0.00")),
            ("181-360", 0, Decimal("0.00"), Decimal("0.00")),
            ("360+", 0, Decimal("0.00"), Decimal("0.00")),
            ("TOTAL", 1, Decimal("4000.00"), Decimal("100.00")),
        ]
        own_income = await render(ReportName.INCOME_STATEMENT, tid_a)
        assert ("income", "income.interest", Decimal("75.00")) in own_income
        own_sasra = await render(ReportName.SASRA_RETURN, tid_a)
        expected_i100 = ("SASRA-DS-2025.1", "I100", "Interest income on loans", Decimal("75.00"))
        assert expected_i100 in own_sasra

        # Probe arm: foreign tenant argument -> zero rows / all-zero
        # aggregates, although RLS would have shown A's rows.
        assert await render(ReportName.MEMBERSHIP_REGISTER, tid_b) == []
        par = await render(ReportName.PORTFOLIO_AT_RISK_AGING, tid_b)
        assert len(par) == 7  # 6 buckets (incl. "current", R1) + TOTAL
        assert all(row[1:] == (0, Decimal("0.00"), Decimal("0.00")) for row in par)
        income = await render(ReportName.INCOME_STATEMENT, tid_b)
        assert income == [
            ("income", "TOTAL INCOME", Decimal("0.00")),
            ("expense", "TOTAL EXPENSE", Decimal("0.00")),
            ("net", "NET SURPLUS", Decimal("0.00")),
        ]
        sasra = await render(ReportName.SASRA_RETURN, tid_b)
        assert all(row[3] == Decimal("0.00") for row in sasra)

    asyncio.run(run())


def test_duplicate_new_report_request_produces_one_of_everything() -> None:
    """P13 blocker g inherited for the new registry entries: an
    Idempotency-Key replay on a membership_register request queues
    exactly ONE job, proven by side-effect row counts."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        await seed_member(tid)
        headers = {
            "authorization": f"Bearer {token}",
            "Idempotency-Key": f"p1310-{uuid.uuid4().hex}",
        }
        body = {"report": "membership_register"}
        async with api_client() as client:
            first = await client.post("/exports", json=body, headers=headers)
            second = await client.post("/exports", json=body, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 201
        assert second.headers.get("idempotency-replayed") == "true"
        assert first.json()["id"] == second.json()["id"]

        await drain_export_queue(tid)
        summary = await drain_export_queue(tid)
        assert summary.completed == 0

        # Hand-computed side-effect counts for ONE export end to end.
        assert await count(tid, "SELECT count(*) FROM exports") == 1
        assert await count(tid, "SELECT count(*) FROM export_artifacts") == 1
        assert (
            await count(tid, "SELECT count(*) FROM audit_log WHERE action = 'export.request'") == 1
        )
        assert (
            await count(tid, "SELECT count(*) FROM audit_log WHERE action = 'export.completed'")
            == 1
        )

    asyncio.run(run())
