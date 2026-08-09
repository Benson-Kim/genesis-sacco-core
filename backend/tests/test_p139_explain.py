"""EXPLAIN capture for the P13.9 dashboard aggregates (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p139.txt (CI artifact via the existing
backend/perf/ convention). The SQL under test is imported from the
production modules — these are the exact statements the dashboard
executes, plus LIVE_PLEDGED_SQL, the statement behind
live_pledged_total (the per-guarantor free-capacity figure).

Index story — P13.9 ships NO migration (stated in the MR per v1.2
rule 14); every aggregate rides indexes that shipped with their tables:

  * deposit/share totals: the (tenant_id, member_id) UNIQUE key
    indexes on deposit_accounts / share_accounts (0001);
  * members by type: idx_members_type (0001);
  * monthly flows: the tenant-leading occurred_at indexes
    idx_transactions_time (0001) / idx_txns_occurred_keyset (0008);
  * pipeline: idx_applications_stage (0001);
  * guarantee totals / top guarantors / live pledged:
    idx_guarantees_guarantor (0001), joins on the members PK and the
    deposit_accounts UNIQUE key.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each relation is SERVABLE by its index (plan
shape at scale — the P10..P13.11 precedent). On single-digit row
counts the planner is free to satisfy the tenant predicate from ANY
tenant-leading index of the relation (observed for members and
loan_applications), so the gates are STRUCTURAL per the documented
P13.11 movements-plan convention: every relation index-served, zero
sequential scans. Falsifiable: drop a relation's indexes and its plan
must seq-scan (enable_seqscan=off only penalises the cost), failing
the gate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.dashboard import (
    DEPOSIT_TOTAL_SQL,
    GUARANTEE_TOTALS_SQL,
    MEMBERS_BY_TYPE_SQL,
    MONTHLY_FLOWS_SQL,
    PIPELINE_SQL,
    SHARE_TOTAL_SQL,
    TOP_GUARANTORS_SQL,
)
from genesis.application.guarantees import LIVE_PLEDGED_SQL, live_guarantee_params
from genesis.domain.ledger import TxnType
from genesis.infrastructure.tenancy import tenant_session
from test_dashboard import _deposit, _pledge, _seed_application, _seed_member, _seed_product

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p139.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p139_dashboard_aggregates_are_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        m1 = await _seed_member(tid)
        await _deposit(tid, admin_id, m1, "1000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, m1, pid)
        guarantor = await _seed_member(tid, deposit="900.00")
        await _pledge(tid, admin_id, application_id=aid, guarantor=guarantor, amount="150.00")

        now = datetime.now(UTC)
        tid_param = {"tid": str(tid)}
        live = live_guarantee_params()
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            deposit_plan = await _explain(session, DEPOSIT_TOTAL_SQL, tid_param)
            share_plan = await _explain(session, SHARE_TOTAL_SQL, tid_param)
            members_plan = await _explain(session, MEMBERS_BY_TYPE_SQL, tid_param)
            flows_plan = await _explain(
                session,
                MONTHLY_FLOWS_SQL,
                {
                    **tid_param,
                    "t_dep": TxnType.DEPOSIT.value,
                    "t_dis": TxnType.LOAN_DISBURSEMENT.value,
                    "w_start": now - timedelta(days=180),
                    "w_end": now,
                },
            )
            pipeline_plan = await _explain(session, PIPELINE_SQL, tid_param)
            totals_plan = await _explain(session, GUARANTEE_TOTALS_SQL, {**tid_param, **live})
            top_plan = await _explain(session, TOP_GUARANTORS_SQL, {**tid_param, **live, "cap": 20})
            pledged_plan = await _explain(
                session, LIVE_PLEDGED_SQL, {**tid_param, **live, "g": str(guarantor)}
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.9 dashboard EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that every aggregate is index-served at scale by the\n"
            "tenant-leading indexes that shipped with the tables (0001/0008):\n"
            "the account-table (tenant_id, member_id) UNIQUE keys,\n"
            "idx_members_type/idx_members_status, the occurred_at indexes,\n"
            "idx_applications_stage and idx_guarantees_guarantor — on\n"
            "single-digit CI row counts the planner may pick any\n"
            "tenant-leading index of the relation, so the gate is\n"
            "structural (index-served, zero seq scans; the P13.11\n"
            "movements-plan convention). P13.9 ships NO migration.\n"
        )
        sections = [
            ("total deposits (balance-column aggregate)", deposit_plan),
            ("total share capital (balance-column aggregate)", share_plan),
            ("members by type + active counts", members_plan),
            ("monthly deposits-vs-disbursements window", flows_plan),
            ("applications pipeline per stage", pipeline_plan),
            ("live-guarantee totals", totals_plan),
            ("top guarantors (config-capped)", top_plan),
            ("live pledged total (per-guarantor capacity)", pledged_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} fell back to a sequential scan"
            assert "Index" in plan, f"{name} is not index-served"
        # Relation anchors: each plan must actually touch its table
        # (guards against a rewritten/misdirected statement passing the
        # structural gate above vacuously). Index NAMES are not pinned:
        # on single-digit CI rows the planner may serve the tenant
        # predicate from any tenant-leading index of the relation (the
        # documented P13.11 movements-plan convention).
        assert "members" in members_plan
        assert "loan_applications" in pipeline_plan
        assert "transactions" in flows_plan
        assert "guarantees" in totals_plan
        assert "guarantees" in top_plan
        assert "guarantees" in pledged_plan
        assert "deposit_accounts" in deposit_plan
        assert "share_accounts" in share_plan

    asyncio.run(run())
