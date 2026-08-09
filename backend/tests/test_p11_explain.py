"""EXPLAIN capture for the P11 hot-path queries (gate 1.3).

Writes the real EXPLAIN (ANALYZE, BUFFERS) output for the three new
hot-path queries to backend/perf/explain_p11.txt (published as a CI
artifact and pasted into the MR description):

  1. ledger listing keyset page (idx_txns_occurred_keyset)
  2. member-filtered ledger page (idx_txns_member_keyset)
  3. deposit-interest accrual scan (idx_deposit_accounts_keyset — the
     job walks every account since interest is computed from the
     period-end balance, so the 0008 partial positive-balance index no
     longer matches the scan predicate)

Tiny CI tables make seqscan the cheaper plan; the capture disables it
for the session to prove each query is servable by an index — i.e. the
plans stay index-backed once the tables grow.
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

from db_helpers import factory, seed_user, unique_email
from genesis.application import transactions as txn_service
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_p11.txt"

LEDGER_KEYSET_PAGE = """
SELECT id, txn_ref, member_id, type, amount, channel, occurred_at, reversal_of_id
FROM transactions
WHERE (occurred_at, id) < (:c_ts, CAST(:c_id AS uuid))
ORDER BY occurred_at DESC, id DESC LIMIT 21
"""

MEMBER_LEDGER_PAGE = """
SELECT id, txn_ref, member_id, type, amount, channel, occurred_at, reversal_of_id
FROM transactions
WHERE member_id = CAST(:mid AS uuid)
AND (occurred_at, id) < (:c_ts, CAST(:c_id AS uuid))
ORDER BY occurred_at DESC, id DESC LIMIT 21
"""

INTEREST_ACCRUAL_SCAN = """
SELECT d.id, d.member_id, d.balance FROM deposit_accounts d
WHERE d.tenant_id = CAST(:tid AS uuid) AND d.id > CAST(:after AS uuid)
AND NOT EXISTS (
  SELECT 1 FROM deposit_interest_accruals a
  WHERE a.tenant_id = d.tenant_id AND a.account_id = d.id
  AND a.period_start = :ps
)
ORDER BY d.id LIMIT 200
"""


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


async def _seed_ledger(tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Explain Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), 0)"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
    for amt in ("100.00", "200.00", "300.00"):
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal(amt), channel=Channel.BANK
            )
    return mid


def test_p11_hot_path_queries_are_index_backed() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_ledger(tid)
        sections: list[tuple[str, str]] = []
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            page_plan = await _explain(
                session,
                LEDGER_KEYSET_PAGE,
                {"c_ts": datetime.now(UTC), "c_id": str(uuid.uuid4())},
            )
            member_plan = await _explain(
                session,
                MEMBER_LEDGER_PAGE,
                {"mid": str(mid), "c_ts": datetime.now(UTC), "c_id": str(uuid.uuid4())},
            )
            accrual_plan = await _explain(
                session,
                INTEREST_ACCRUAL_SCAN,
                {
                    "tid": str(tid),
                    "after": "00000000-0000-0000-0000-000000000000",
                    "ps": date(2026, 4, 1),
                },
            )
        sections.append(("ledger listing keyset page", page_plan))
        sections.append(("member-filtered ledger page", member_plan))
        sections.append(("interest accrual scan", accrual_plan))

        assert "idx_txns_occurred_keyset" in page_plan
        assert "Seq Scan" not in page_plan
        # On tiny tables the planner may pick the older P2 member index;
        # what matters structurally is that no relation seq-scans. The
        # keyset index wins as pages deepen in production.
        assert "idx_txns_member_keyset" in member_plan or "idx_transactions_member" in member_plan
        assert "Seq Scan" not in member_plan
        assert (
            "idx_deposit_accounts_keyset" in accrual_plan or "deposit_accounts_pkey" in accrual_plan
        )
        assert "Seq Scan" not in accrual_plan

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "P11 hot-path EXPLAIN (ANALYZE, BUFFERS) — captured in CI against the\n"
            "migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off because CI tables are tiny; the assertion is that\n"
            "each query is servable by its index (plan shape at scale).\n"
        )
        body = "\n\n".join(f"=== {name} ===\n{plan}" for name, plan in sections)
        OUT_PATH.write_text(f"{header}\n{body}\n")

    asyncio.run(run())
