"""EXPLAIN capture for the P12 hot-path queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for the new hot-path
queries to backend/perf/explain_p12.txt (CI artifact, pasted into the
MR description):

  1. exits keyset listing page (idx_exits_created_keyset, 0010)
  2. status-filtered exits keyset page (idx_exits_status_created_keyset,
     0010 — the 0001 idx_exits_status cannot serve the keyset ORDER BY)
  3. open-settlement lookup by member (uq_member_exits_open, 0010 —
     the partial unique doubles as the lookup index)
  4. exit votes tally (idx_exit_votes_exit / the vote UNIQUE, 0010)

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is servable by an index — i.e. the
plans stay index-backed once the tables grow (P10/P11 precedent).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory, seed_user, unique_email
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p12.txt"

EXITS_KEYSET_PAGE = """
SELECT id, member_id, status, net_payable, created_at
FROM member_exits
WHERE tenant_id = CAST(:tid AS uuid)
AND (created_at, id) < (:c_ts, CAST(:c_id AS uuid))
ORDER BY created_at DESC, id DESC LIMIT 21
"""

EXITS_STATUS_KEYSET_PAGE = """
SELECT id, member_id, status, net_payable, created_at
FROM member_exits
WHERE tenant_id = CAST(:tid AS uuid)
AND status = :status
AND (created_at, id) < (:c_ts, CAST(:c_id AS uuid))
ORDER BY created_at DESC, id DESC LIMIT 21
"""

OPEN_SETTLEMENT_LOOKUP = """
SELECT id, status FROM member_exits
WHERE tenant_id = CAST(:tid AS uuid)
AND member_id = CAST(:mid AS uuid)
AND status IN ('requested', 'approved')
"""

VOTES_TALLY = """
SELECT vote, count(*) FROM exit_votes
WHERE exit_id = CAST(:eid AS uuid)
AND tenant_id = CAST(:tid AS uuid) GROUP BY vote
"""


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


async def _seed_exit(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    mid = uuid.uuid4()
    exit_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Explain Exit')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO member_exits (id, tenant_id, member_id, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), 'requested')"
            ),
            {"id": str(exit_id), "tid": str(tid), "m": str(mid)},
        )
    return mid, exit_id


def test_p12_hot_path_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid, exit_id = await _seed_exit(tid)
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            page_plan = await _explain(
                session,
                EXITS_KEYSET_PAGE,
                {"tid": str(tid), "c_ts": datetime.now(UTC), "c_id": str(uuid.uuid4())},
            )
            status_page_plan = await _explain(
                session,
                EXITS_STATUS_KEYSET_PAGE,
                {
                    "tid": str(tid),
                    "status": "requested",
                    "c_ts": datetime.now(UTC),
                    "c_id": str(uuid.uuid4()),
                },
            )
            open_plan = await _explain(
                session,
                OPEN_SETTLEMENT_LOOKUP,
                {"tid": str(tid), "mid": str(mid)},
            )
            tally_plan = await _explain(
                session,
                VOTES_TALLY,
                {"eid": str(exit_id), "tid": str(tid)},
            )

        assert "idx_exits_created_keyset" in page_plan
        assert "Seq Scan" not in page_plan
        assert "idx_exits_status_created_keyset" in status_page_plan
        assert "Seq Scan" not in status_page_plan
        assert "uq_member_exits_open" in open_plan
        assert "Seq Scan" not in open_plan
        # Either the tally index or the vote UNIQUE serves the tally.
        assert "idx_exit_votes_exit" in tally_plan or "exit_votes_tenant_id" in tally_plan
        assert "Seq Scan" not in tally_plan

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P12 hot-path EXPLAIN (ANALYZE, BUFFERS) — captured in CI against the\n"
            "migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("exits keyset listing page", page_plan),
            ("status-filtered exits keyset page", status_page_plan),
            ("open-settlement lookup by member", open_plan),
            ("exit votes tally", tally_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

    asyncio.run(run())
