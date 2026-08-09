"""EXPLAIN capture for the P14.5 member-login hot path (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p145.txt (CI artifact, pasted into the MR
description). The SQL under test is LIVE_CREDENTIAL_BY_EMAIL_SQL,
imported from application/member_auth — the exact statement every
member OTP request/verify runs first.

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove the query is SERVABLE by an index (the
P10-P13 precedent). The serving index is the 0035 partial UNIQUE
uq_member_credentials_email_active — the login handle's claim key
doubling as its lookup index (the 0019/0026 precedent); the join to
members rides the primary key. Falsifiable guard: drop the 0035
partial unique and the no-sequential-scan gate below fails.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory, seed_user, unique_email
from genesis.application.member_auth import LIVE_CREDENTIAL_BY_EMAIL_SQL
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p145.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p145_member_login_lookup_is_index_backed(capfd: pytest.CaptureFixture[str]) -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = uuid.uuid4()
        email = unique_email()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Plan Member')"
                ),
                {"id": str(mid), "tid": str(tid), "no": f"PX-{mid.hex[:6].upper()}"},
            )
            await session.execute(
                text(
                    "INSERT INTO member_credentials (tenant_id, member_id, email) "
                    "VALUES (CAST(:tid AS uuid), CAST(:m AS uuid), :email)"
                ),
                {"tid": str(tid), "m": str(mid), "email": email},
            )

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = await _explain(
                session,
                LIVE_CREDENTIAL_BY_EMAIL_SQL,
                {
                    "tid": str(tid),
                    "email": email,
                    "st0": "active",
                    "st1": "arrears",
                    "st2": "dormant",
                },
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plan.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P14.5 member-login hot path EXPLAIN (ANALYZE, BUFFERS) — captured\n"
            "in CI against the migrated Postgres service under the RLS app\n"
            "role. enable_seqscan=off because CI tables are tiny; the\n"
            "assertion is index-served-ness at scale: the 0035 partial\n"
            "UNIQUE uq_member_credentials_email_active serves the credential\n"
            "probe and members_pkey serves the status join.\n"
        )
        OUT_PATH.write_text(
            f"{header}\n=== live credential by login email "
            f"(LIVE_CREDENTIAL_BY_EMAIL_SQL) ===\n{plan}\n"
        )

        # Surface the captured plan in the CI job log as well: the
        # after_script cat list predates P14.5 and .gitlab-ci.yml is
        # untouchable by standing rule, while artifact download needs
        # an api-scope token the agent lacks (the !49 disclosure).
        with capfd.disabled():
            sys.stdout.write(f"\n{OUT_PATH.read_text()}\n")

        assert "uq_member_credentials_email_active" in plan, plan
        assert "Seq Scan" not in plan, "the member-login lookup fell back to a sequential scan"

    asyncio.run(run())
