"""EXPLAIN capture for the P13.7 tenant-settings queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p137.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
module — these are the statements the settings API and its consumers
(P9 cast_vote / transition_stage, P12 cast_exit_vote) actually run.

Every settings read is a single primary-key probe of the one-row-per-
tenant tenant_settings table (PK tenant_id, 0009) — no new index is
needed and none was added. Tiny CI tables make seqscan the cheaper
plan; the capture disables it for the session to prove each query is
SERVABLE by the primary key (plan shape at scale, the P10-P13.5
precedent). Falsifiable guard: drop the tenant_settings primary key
and the no-sequential-scan gate below fails.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.tenant_settings import (
    APPROVAL_BANDS_SQL,
    COMMITTEE_QUORUM_SQL,
    settings_row_sql,
    update_settings,
)
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p137.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p137_settings_reads_are_pk_probes() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        async with tenant_session(factory(), tid) as session:
            await update_settings(
                session,
                tid,
                admin_id,
                version=0,
                changes={"committee_quorum": 3, "committee_size": 5, "exit_fee": Decimal("10")},
            )

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            params = {"tid": str(tid)}
            full_row = await _explain(session, settings_row_sql(), params)
            quorum = await _explain(session, COMMITTEE_QUORUM_SQL, params)
            bands = await _explain(session, APPROVAL_BANDS_SQL, params)

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.7 tenant-settings EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that each query is servable by the tenant_settings primary key\n"
            "(single PK probe per read — plan shape at scale).\n"
        )
        sections = [
            ("settings row (GET /settings + PUT read-back)", full_row),
            ("committee quorum (P9 cast_vote / P12 cast_exit_vote)", quorum),
            ("approval bands (P9 transition_stage / cast_vote)", bands),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        for name, plan in sections:
            assert "tenant_settings_pkey" in plan, f"{name} is not a PK probe"
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())
