"""EXPLAIN capture for the P13.5 users/audit-log queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p135.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
modules — these are the statements the endpoints actually run.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index — i.e. the
plans stay index-backed once the tables grow (P10-P13 precedent).
Falsifiable guard: drop the 0015 indexes and the no-sequential-scan
gate below fails.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.audit_log import audit_page_sql
from genesis.application.users import users_page_sql
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p135.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p135_user_and_audit_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        now = datetime.now(UTC)

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            users_page = await _explain(
                session,
                users_page_sql(with_cursor=True, with_status=True, with_role=False),
                {
                    "tid": str(tid),
                    "status": "active",
                    "c_ts": now,
                    "c_id": str(uuid.uuid4()),
                    "limit": 101,
                },
            )
            audit_unfiltered = await _explain(
                session,
                audit_page_sql(
                    with_cursor=True,
                    with_entity=False,
                    with_actor=False,
                    with_action=False,
                    with_from=False,
                    with_to=False,
                ),
                {"tid": str(tid), "c_ts": now, "c_id": 2**40, "limit": 101},
            )
            audit_dated = await _explain(
                session,
                audit_page_sql(
                    with_cursor=True,
                    with_entity=False,
                    with_actor=False,
                    with_action=False,
                    with_from=True,
                    with_to=True,
                ),
                {
                    "tid": str(tid),
                    "d_from": (now - timedelta(days=7)).date(),
                    "d_to_excl": now.date() + timedelta(days=1),
                    "c_ts": now,
                    "c_id": 2**40,
                    "limit": 101,
                },
            )
            audit_entity = await _explain(
                session,
                audit_page_sql(
                    with_cursor=True,
                    with_entity=True,
                    with_actor=False,
                    with_action=False,
                    with_from=False,
                    with_to=False,
                ),
                {
                    "tid": str(tid),
                    "entity": "users",
                    "c_ts": now,
                    "c_id": 2**40,
                    "limit": 101,
                },
            )
            audit_actor = await _explain(
                session,
                audit_page_sql(
                    with_cursor=True,
                    with_entity=False,
                    with_actor=True,
                    with_action=False,
                    with_from=False,
                    with_to=False,
                ),
                {
                    "tid": str(tid),
                    "actor": str(admin_id),
                    "c_ts": now,
                    "c_id": 2**40,
                    "limit": 101,
                },
            )
            audit_action = await _explain(
                session,
                audit_page_sql(
                    with_cursor=True,
                    with_entity=False,
                    with_actor=False,
                    with_action=True,
                    with_from=False,
                    with_to=False,
                ),
                {
                    "tid": str(tid),
                    "action": "user.status",
                    "c_ts": now,
                    "c_id": 2**40,
                    "limit": 101,
                },
            )

        # Capture the artifact BEFORE any assertion so the CI job log and
        # backend/perf/ artifact always carry the full plans (gate 1.3).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.5 users/audit-log EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("users page (keyset + status filter)", users_page),
            ("audit-log page (keyset, unfiltered)", audit_unfiltered),
            ("audit-log page (date bounded)", audit_dated),
            ("audit-log page (entity filter)", audit_entity),
            ("audit-log page (actor filter)", audit_actor),
            ("audit-log page (action filter)", audit_action),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_users_created_keyset" in users_page
        assert "idx_audit_keyset" in audit_unfiltered
        # Date bounds ride the same keyset index (leading at column).
        assert "idx_audit_keyset" in audit_dated
        # On near-empty CI tables the planner may serve a filtered page
        # either from the filter-shaped index or by scanning the keyset
        # index in order with the filter as a condition — both are
        # tenant-led composite indexes shipped with the query (0015).
        # The falsifiable gate is the no-sequential-scan check below.
        assert "idx_audit_entity_keyset" in audit_entity or "idx_audit_keyset" in audit_entity
        assert "idx_audit_actor_keyset" in audit_actor or "idx_audit_keyset" in audit_actor
        assert "idx_audit_action_keyset" in audit_action or "idx_audit_keyset" in audit_action
        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())
