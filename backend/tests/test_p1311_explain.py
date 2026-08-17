"""EXPLAIN capture for the P13.11 dividend queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output to
backend/perf/explain_p1311.txt (CI artifact, surfaced in the job log
and pasted into the MR description). The SQL under test is imported
from the production modules — members_scan_sql(...) is the statement
the distribution job actually runs, eod_balance_sql/DAILY_MOVEMENTS_SQL
are the ADB reconstruction, dividend_schedule_page_sql the report page
and DIVIDEND_CONFIG_SQL the once-per-run config probe.

Index story:

  * driving scan: idx_members_dividend_scan (0020 — partial over
    active/arrears, shipped in the SAME migration as the query);
  * claim anti-join: uq_dividend_distributions_claim (0020) — the
    UNIQUE idempotency claim doubles as the anti-join index;
  * ADB reconstruction: idx_txns_member_keyset /
    idx_transactions_member (0008/0001) drive the member-attributed
    leg lookups; the account anchor is a single UNIQUE
    (tenant_id, member_id) probe;
  * report page: idx_dividend_distributions_page (0020, shipped with
    the report query);
  * config resolution: the tenant_settings primary key (0009).

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each relation is SERVABLE by its index (plan
shape at scale — the P10..P13.8 precedent). Falsifiable guards: drop
idx_members_dividend_scan, uq_dividend_distributions_claim or
idx_dividend_distributions_page and the index-name / no-sequential-scan
gates below fail.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.dividends import (
    DIVIDEND_CONFIG_SQL,
    declare_dividend,
    members_scan_sql,
)
from genesis.application.period_balances import DAILY_MOVEMENTS_SQL, eod_balance_sql
from genesis.application.reports import dividend_schedule_page_sql
from genesis.domain.ledger import Account
from genesis.infrastructure.tenancy import tenant_session
from test_dividends import FY, TODAY, _configure_dividends, _member_with_share_history
from test_dividends import _scope as _dividend_scope

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p1311.txt"


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_p1311_dividend_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        mid = await _member_with_share_history(tid, amount="12000.00", on=FY.start)
        declaration = await declare_dividend(_dividend_scope(tid), tid, admin_id, today=TODAY)

        cutoff = datetime.combine(FY.end, time.max, tzinfo=UTC)
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            scan_plan = await _explain(
                session,
                members_scan_sql(with_after=True, with_claims=True, for_update=True),
                {
                    "tid": str(tid),
                    "decl": str(declaration.id),
                    "after": str(uuid.UUID(int=0)),
                    "limit": 200,
                },
            )
            anchor_plan = await _explain(
                session,
                eod_balance_sql("share"),
                {
                    "tid": str(tid),
                    "m": str(mid),
                    "acct": Account.MEMBER_SHARES.value,
                    "cutoff": cutoff,
                },
            )
            movements_plan = await _explain(
                session,
                DAILY_MOVEMENTS_SQL,
                {
                    "tid": str(tid),
                    "m": str(mid),
                    "acct": Account.MEMBER_SHARES.value,
                    "p_start": datetime.combine(FY.start, time.min, tzinfo=UTC),
                    "cutoff": cutoff,
                },
            )
            report_plan = await _explain(
                session,
                dividend_schedule_page_sql(with_cursor=True),
                {
                    "tid": str(tid),
                    "did": str(declaration.id),
                    "as_of": datetime.now(UTC),
                    "c_ts": datetime.now(UTC),
                    "c_id": str(uuid.UUID(int=0)),
                    "limit": 100,
                },
            )
            config_plan = await _explain(session, DIVIDEND_CONFIG_SQL, {"tid": str(tid)})

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P13.11 dividend EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is\n"
            "that the distribution scan is served by idx_members_dividend_scan\n"
            "with the claim anti-join on uq_dividend_distributions_claim (both\n"
            "shipped in 0020 with their queries), the ADB reconstruction by\n"
            "the member-attributed transaction indexes, the schedule page by\n"
            "idx_dividend_distributions_page, and the config read by the\n"
            "tenant_settings PK (plan shape at scale).\n"
        )
        sections = [
            ("distribution batch scan (anti-join + FOR UPDATE SKIP LOCKED)", scan_plan),
            ("ADB end-of-period anchor (single-statement, share account)", anchor_plan),
            ("ADB daily movements (member-attributed legs)", movements_plan),
            ("dividend & rebate schedule keyset page", report_plan),
            ("dividend config resolution (once per declaration)", config_plan),
        ]
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

        assert "idx_members_dividend_scan" in scan_plan
        assert "uq_dividend_distributions_claim" in scan_plan
        assert "Seq Scan" not in scan_plan
        # The reconstruction is driven by a member-attributed
        # transactions index and the UNIQUE account probe.
        assert "share_accounts" in anchor_plan
        assert "Seq Scan" not in anchor_plan
        # On the tiny CI tables the planner is free to drive the join
        # from either side (ledger_entries via idx_ledger_account /
        # idx_ledger_txn, or transactions via its member indexes); the
        # structural gate is that every relation is index-served.
        assert "Index" in movements_plan
        assert "Seq Scan" not in movements_plan
        assert "idx_dividend_distributions_page" in report_plan
        assert "Seq Scan" not in report_plan
        assert "tenant_settings_pkey" in config_plan
        assert "Seq Scan" not in config_plan

    asyncio.run(run())
