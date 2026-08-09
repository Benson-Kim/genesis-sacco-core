"""EXPLAIN capture for the P13.8 penalty-accrual queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p138.txt (CI artifact, surfaced in the job log and
pasted into the MR description). The SQL under test is imported from
the production module — arrears_scan_sql(with_penalty=True) is the
statement the extended arrears job actually runs, PENALTY_CONFIG_SQL
is its once-per-run config probe.

Index story (all pre-existing except the claim key, which ships in the
SAME migration as the query that needs it — 0019):

  * driving scan: idx_loans_active_scan (0007) — unchanged P10 shape;
  * schedule subqueries (oldest unpaid + arrears basis):
    idx_schedules_unpaid (0007);
  * penalty anti-join: uq_penalty_accruals_claim (0019) — the UNIQUE
    idempotency claim doubles as the anti-join index;
  * config resolution: tenant_settings primary key (0009).

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each relation is SERVABLE by its index (plan
shape at scale — the P10..P13.7 precedent). Falsifiable guards: drop
uq_penalty_accruals_claim (or idx_loans_active_scan) and the
no-sequential-scan / index-name gates below fail.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.arrears import (
    PENALTY_CONFIG_SQL,
    arrears_scan_sql,
    run_arrears_for_tenant,
)
from genesis.infrastructure.tenancy import tenant_session
from test_penalty_accrual import _configure_penalty, _disburse, _pin_first_instalment, _scope

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p138.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p138_penalty_accrual_scan_is_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        # One claimed day on record so the anti-join has real rows to
        # probe when the scan runs for the following day.
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            # Issue #20 / RF4 fence: every plan captured here is sort-free on
            # all observed runs (jobs 15662887952 / 15662909824 / 15665250796),
            # so enable_sort=off cannot change today's plans; it pins the
            # shape so a rows=0 cost tie can never swap an order-preserving
            # index scan for the bitmap-scan + Sort flake (the
            # test_p135_explain audit-page class).
            await session.execute(text("SET LOCAL enable_sort = off"))
            scan_plan = await _explain(
                session,
                arrears_scan_sql(with_after=True, with_penalty=True),
                {
                    "tid": str(tid),
                    "as_of": date(2026, 6, 11),
                    "after": str(uuid.UUID(int=0)),
                    "limit": 200,
                },
            )
            config_plan = await _explain(session, PENALTY_CONFIG_SQL, {"tid": str(tid)})

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.8 penalty-accrual EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; enable_sort=off\n"
            "pins the observed sort-free shape (issue #20); the assertion is\n"
            "that the scan is served by idx_loans_active_scan, the schedule\n"
            "subqueries by an index, the claim anti-join by\n"
            "uq_penalty_accruals_claim (shipped in 0019 with the query), and\n"
            "the config read by the tenant_settings PK (plan shape at scale).\n"
        )
        sections = [
            ("penalty accrual batch scan (arrears job, with_penalty)", scan_plan),
            ("penalty config resolution (once per run)", config_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_loans_active_scan" in scan_plan
        assert "uq_penalty_accruals_claim" in scan_plan
        assert "loan_schedules" in scan_plan  # subqueries index-backed
        assert "Seq Scan" not in scan_plan
        assert "tenant_settings_pkey" in config_plan
        assert "Seq Scan" not in config_plan

    asyncio.run(run())
