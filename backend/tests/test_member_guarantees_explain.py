"""EXPLAIN structural gate for the member guarantees inbox (#41).

One NEW statement ships with GET /member/guarantees; it is gated here
against the migrated Postgres service under the RLS app role (the
P13.9/N1 EXPLAIN-capture convention — artifact BEFORE any assertion):

  The member pledge page (list_member_guarantees) — the probe enters
  guarantees through idx_guarantees_guarantor (0001: tenant_id,
  guarantor_member_id); no Seq Scan anywhere under enable_seqscan=off.
  NO migration ships: the index predates the query (the idx_loans_member
  posture from the !7 gate). The residual top-N order runs over ONE
  member's pledges only — bounded by guarantorship reality, never the
  tenant book — so the small in-memory top-N over the member's own rows
  is the accepted plan shape. The loan_ref label join rides the loans
  PRIMARY KEY per page row (no N+1).
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.guarantees import (
    _MEMBER_GUARANTEE_COLS,
    _MEMBER_GUARANTEE_LOAN_JOIN,
)
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_member_guarantees.txt"


async def _seed_members(tid: uuid.UUID, count: int) -> list[uuid.UUID]:
    """Bulk member rows (executemany, one round trip)."""
    rows = []
    ids: list[uuid.UUID] = []
    for _ in range(count):
        mid = uuid.uuid4()
        ids.append(mid)
        rows.append({"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"})
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Explain Member')"
            ),
            rows,
        )
    return ids


async def _seed_pledges(
    tid: uuid.UUID,
    pairs: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """Bulk pledged guarantees (guarantor, borrower) — executemany."""
    rows = [
        {
            "id": str(uuid.uuid4()),
            "tid": str(tid),
            "g": str(g),
            "b": str(b),
        }
        for g, b in pairs
    ]
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), '25.00')"
            ),
            rows,
        )


async def _analyze_guarantees() -> None:
    """Hand the planner real row counts for the seeded rows.

    Postgres lets only the table OWNER run ANALYZE — as the
    unprivileged RLS app role the statement is a silent no-op WARNING
    and the plan choice below would assert planner noise. CI provides
    the owner DSN as DATABASE_MAINT_URL (the role migrations ran as);
    the probe itself still runs through the RLS app role session.
    """
    maint_url = os.environ.get("DATABASE_MAINT_URL", os.environ["DATABASE_URL"])
    async with get_sessionmaker(maint_url)() as session, session.begin():
        await session.execute(text("ANALYZE guarantees, loans, members"))


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_member_guarantees_page_is_index_served() -> None:
    """The pledge page enters guarantees through idx_guarantees_guarantor
    with NO Seq Scan anywhere under enable_seqscan=off (drop the
    guarantor predicate or the index and this gate fails). Cardinality
    that discriminates (the tiny-CI-tables discipline): 32 decoy
    guarantors x 32 pledges each plus real statistics make every
    tenant-led entry point strictly more expensive than the two-qual
    guarantor probe — without decoys every guarantees index ties on
    cost and the gate would assert planner noise. The artifact is
    written BEFORE any assertion (CI job log + backend/perf/)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        # The real guarantor with a handful of pledges to one borrower.
        gmid, bmid = await _seed_members(tid, 2)
        await _seed_pledges(tid, [(gmid, bmid) for _ in range(4)])
        # Decoy book: 32 guarantors x 32 pledges each (executemany).
        decoys = await _seed_members(tid, 32)
        pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
        for decoy in decoys:
            pairs.extend((decoy, bmid) for _ in range(32))
        await _seed_pledges(tid, pairs)
        await _analyze_guarantees()

        # The exact production shape composed by list_member_guarantees'
        # own builder path with the principal-derived guarantor
        # predicate (static clause literals from production code; every
        # value a bound parameter).
        page_sql = (
            f"SELECT guarantees.created_at, {_MEMBER_GUARANTEE_COLS}, "  # noqa: S608
            "ll.loan_ref FROM guarantees "
            f"{_MEMBER_GUARANTEE_LOAN_JOIN}"
            "WHERE guarantees.tenant_id = CAST(:tid AS uuid) "
            "AND guarantees.guarantor_member_id = CAST(:g AS uuid) "
            "ORDER BY guarantees.created_at DESC, guarantees.id DESC LIMIT :limit"
        )
        params: dict[str, object] = {"tid": str(tid), "g": str(gmid), "limit": 21}

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = await _explain(session, page_sql, params)

        # Artifact BEFORE assertions (the EXPLAIN-capture convention).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            "#41 member guarantees inbox EXPLAIN (ANALYZE, BUFFERS) — captured\n"
            "in CI against the migrated Postgres service under the RLS app\n"
            "role, enable_seqscan=off, a decoy pledge book seeded + ANALYZE\n"
            "(owner DSN) so the cardinality discriminates: the page enters\n"
            "guarantees through idx_guarantees_guarantor (0001 — NO migration\n"
            "ships); the residual top-N orders ONE member's pledges only\n"
            "(bounded by guarantorship reality); the loan_ref label join\n"
            "rides the loans PRIMARY KEY per page row.\n"
            f"\n=== /member/guarantees page (guarantor-filtered) ===\n{plan}\n"
        )

        assert "Seq Scan" not in plan, f"pledge page fell back to a sequential scan:\n{plan}"
        # The decoy book discriminates the cardinality, so the winning
        # plan must enter guarantees through the guarantor probe — a
        # tenant-led index with guarantor_member_id demoted to a Filter
        # (the shape that degrades with the tenant book) fails.
        assert "idx_guarantees_guarantor" in plan, (
            f"pledge page is not served by the guarantor probe:\n{plan}"
        )

    asyncio.run(run())
