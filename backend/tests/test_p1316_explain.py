"""EXPLAIN capture for the P13.16 recovery worklist queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1316.txt (CI artifact, surfaced in the job log
and pasted into the MR description). The SQL under test is imported
from the production module — worklist_sql / close_scan_sql /
notes_page_sql are the statements the API and the arrears close pass
actually run.

Index story (all shipped in the SAME migration as the queries — 0026):

  * worklist: idx_loans_dpd_worklist (tenant_id, days_past_due DESC,
    id DESC) walks loans in worklist order; the per-loan open-case
    probe is served by uq_recovery_cases_one_open (the one-open-case
    claim doubling as the join index, the 0019 precedent); the
    assignee flag probe by the users PK.
  * close pass: idx_recovery_cases_open_scan (tenant_id, id WHERE
    status='open') drives the id-keyset batch scan.
  * notes: idx_recovery_notes_case (tenant_id, case_id, created_at,
    id) serves the per-case keyset page.

Tiny CI tables make seqscan/sort the cheaper plan; the capture
disables both for the session to prove each relation is SERVABLE by
its index (plan shape at scale — the P10..P13.10 precedent).
Falsifiable guards: drop idx_loans_dpd_worklist,
uq_recovery_cases_one_open, idx_recovery_cases_open_scan or
idx_recovery_notes_case and the matching index-name / no-Seq-Scan
gate below fails.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.arrears import run_arrears_for_tenant
from genesis.application.recovery import (
    add_recovery_note,
    close_scan_sql,
    notes_page_sql,
    npl_params,
    open_recovery_case,
    worklist_sql,
)
from genesis.infrastructure.tenancy import tenant_session
from test_penalty_accrual import _disburse, _pin_first_instalment, _scope

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1316.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1316_worklist_and_close_scan_are_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        # One NPL loan with an open, noted case so every plan touches
        # real rows. dpd oracle: due 2026-01-01, as_of 2026-06-01 -> 151.
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 1, 1))
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 1))
        async with tenant_session(factory(), tid) as session:
            case = await open_recovery_case(session, tid, admin_id, loan_id=loan_id)
            await add_recovery_note(session, tid, admin_id, case_id=case.id, note="explain fixture")

        async with tenant_session(factory(), tid) as session:
            # Tiny-table planner discipline (the established precedent):
            # prove index servability, not tiny-table cost choices.
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            worklist_plan = await _explain(
                session,
                worklist_sql(with_cursor=True),
                {
                    "tid": str(tid),
                    "c_dpd": 10_000_000,
                    "c_id": str(uuid.UUID(int=(1 << 128) - 1)),
                    "limit": 50,
                },
            )
            close_plan = await _explain(
                session,
                close_scan_sql(with_after=True),
                {
                    "tid": str(tid),
                    "after": str(uuid.UUID(int=0)),
                    "limit": 200,
                    **npl_params(),
                },
            )
            notes_plan = await _explain(
                session,
                notes_page_sql(with_cursor=True),
                {
                    "tid": str(tid),
                    "cid": str(case.id),
                    "c_ts": datetime(1970, 1, 1, tzinfo=UTC),
                    "c_id": str(uuid.UUID(int=0)),
                    "limit": 50,
                },
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.16 recovery worklist EXPLAIN (ANALYZE, BUFFERS) — captured in\n"
            "CI against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan/enable_sort=off because CI tables are tiny; the\n"
            "assertion is plan shape at scale: the worklist walks\n"
            "idx_loans_dpd_worklist in order and probes\n"
            "uq_recovery_cases_one_open per loan; the close pass drives\n"
            "idx_recovery_cases_open_scan; the notes page is served by\n"
            "idx_recovery_notes_case (all shipped in 0026 with the queries).\n"
        )
        sections = [
            ("recovery worklist (keyset dpd DESC, id DESC)", worklist_plan),
            ("arrears close pass scan (open cases, id keyset)", close_plan),
            ("case notes page (created_at, id keyset)", notes_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_loans_dpd_worklist" in worklist_plan
        assert "uq_recovery_cases_one_open" in worklist_plan
        assert "Seq Scan" not in worklist_plan
        assert "idx_recovery_cases_open_scan" in close_plan
        assert "Seq Scan" not in close_plan
        assert "idx_recovery_notes_case" in notes_plan
        assert "Seq Scan" not in notes_plan

    asyncio.run(run())
