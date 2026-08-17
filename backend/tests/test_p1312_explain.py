"""EXPLAIN capture for the P13.12 KYC queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1312.txt (CI artifact, pasted into the MR
description). The SQL under test is imported from the production
module — these are the statements the endpoints actually run.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is SERVABLE by an index — i.e. the
plans stay index-backed once the tables grow (P10-P13.6 precedent).
Falsifiable guard: drop the 0018 unique constraints and the
no-sequential-scan gate below fails.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor, seed_member
from genesis.application.member_kyc import documents_page_sql, profile_lookup_sql
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1312.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1312_kyc_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            profile = await _explain(
                session,
                profile_lookup_sql(),
                {"mid": str(mid), "tid": str(tid)},
            )
            documents = await _explain(
                session,
                documents_page_sql(with_cursor=True),
                {
                    "tid": str(tid),
                    "mid": str(mid),
                    "cursor": "kra_pin",
                    "limit": 101,
                },
            )
            documents_first_page = await _explain(
                session,
                documents_page_sql(with_cursor=False),
                {"tid": str(tid), "mid": str(mid), "limit": 101},
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans (1.3).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.12 member KYC EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that each query is servable by its index (plan shape at scale).\n"
        )
        sections = [
            ("profile lookup by member (uq_member_profiles_member)", profile),
            ("documents page, keyset on doc_type (uq_member_documents_checklist)", documents),
            ("documents first page (no cursor)", documents_first_page),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        # The claim constraints' indexes ARE the read indexes (0018).
        assert "uq_member_profiles_member" in profile
        assert "uq_member_documents_checklist" in documents
        assert "uq_member_documents_checklist" in documents_first_page
        for name, plan in sections:
            assert "Seq Scan" not in plan, f"{name} plan fell back to a sequential scan"

    asyncio.run(run())


def test_document_cursor_is_a_bound_parameter_only() -> None:
    """The keyset fragment is a static literal; a hostile cursor value
    can never reach the SQL text (v1.1 rule 6)."""
    sql = documents_page_sql(with_cursor=True)
    assert ":cursor" in sql
    assert "kra_pin" not in sql
