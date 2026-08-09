"""EXPLAIN captures for the advisory exit-eligibility read (P15 batch 5
U6, gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for every statement
the advisory read executes to backend/perf/explain_exit_eligibility.txt
(CI artifact + after_script cat, the backend/perf/ convention). The SQL
under test is imported from the production module — the exact
module-level constants exit_eligibility() binds.

Index story — this feature ships NO migration; every probe rides an
index that shipped with its table:

  * member status: the members PRIMARY KEY (id) probe;
  * live guarantees given: idx_guarantees_guarantor (0001);
  * active loans: idx_loans_member (0001);
  * open applications: idx_applications_member (0001);
  * open exit slot: uq_member_exits_open (0010, partial on the open
    statuses) / idx_exits_created_keyset family;
  * unresolved write-off claim: idx_loan_write_offs_member (0025) +
    idx_loan_recoveries_write_off (0030) — the SAME statement the
    locked settlement guard executes (one copy, gate 1.1).

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each relation is SERVABLE by its index (the
structural P13.11 convention: index-served, zero sequential scans;
index NAMES are not pinned on single-digit row counts). Falsifiable:
drop a probe table's indexes and its statement must seq-scan.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.guarantees import live_guarantee_params
from genesis.application.member_exits import (
    ELIGIBILITY_GUARANTEES_SQL,
    ELIGIBILITY_LOANS_SQL,
    ELIGIBILITY_MEMBER_SQL,
    ELIGIBILITY_OPEN_EXIT_SQL,
    UNRESOLVED_WRITEOFF_CLAIM_SQL,
)
from genesis.infrastructure.tenancy import tenant_session
from test_member_exits import _seed_active_loan, _seed_guarantee, _seed_member

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_exit_eligibility.txt"

#: Open-application probe: the COUNT the shared _open_application_count
#: helper executes (its _OPEN_STAGES literal inlined verbatim — the
#: helper builds the same statement from the same code-owned list).
_OPEN_APPLICATIONS_SQL = (
    "SELECT count(*) FROM loan_applications "
    "WHERE member_id = CAST(:m AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid) "
    "AND stage IN ('submitted', 'appraisal', 'committee', 'approved')"
)


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_exit_eligibility_probes_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        member = await _seed_member(tid, deposit="1000.00", shares="500.00")
        other = await _seed_member(tid)
        await _seed_active_loan(tid, member, balance="750.00")
        await _seed_guarantee(
            tid, guarantor=member, borrower=other, amount="150.00", status="pledged"
        )

        params: dict[str, object] = {"m": str(member), "tid": str(tid)}
        statements: list[tuple[str, str, dict[str, object]]] = [
            ("member status", ELIGIBILITY_MEMBER_SQL, params),
            (
                "live guarantees given",
                ELIGIBILITY_GUARANTEES_SQL,
                {**params, **live_guarantee_params()},
            ),
            ("active loans", ELIGIBILITY_LOANS_SQL, params),
            ("open applications", _OPEN_APPLICATIONS_SQL, params),
            ("open exit slot", ELIGIBILITY_OPEN_EXIT_SQL, params),
            ("unresolved write-off claim", UNRESOLVED_WRITEOFF_CLAIM_SQL, params),
        ]

        sections: list[str] = []
        plans: list[tuple[str, str]] = []
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            for label, sql, bound in statements:
                plan = await _explain(session, sql, bound)
                plans.append((label, plan))
                sections.append(f"=== exit eligibility: {label} ===\n{plan}\n")

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry every plan.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "Exit-eligibility EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role\n"
            "(P15 batch 5 U6: the advisory, lock-free eligibility read).\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "structural (index-served, zero seq scans; the P13.11 convention;\n"
            "index names not pinned on single-digit rows). Ships NO migration:\n"
            "members PK, idx_guarantees_guarantor, idx_loans_member,\n"
            "idx_applications_member, uq_member_exits_open,\n"
            "idx_loan_write_offs_member + idx_loan_recoveries_write_off all\n"
            "pre-exist (0001/0010/0025/0030).\n"
        )
        OUT_PATH.write_text(header + "\n" + "\n".join(sections))

        for label, plan in plans:
            assert "Seq Scan" not in plan, f"{label}: fell back to a sequential scan"
            assert "Index" in plan, f"{label}: not index-served"

    asyncio.run(run())
