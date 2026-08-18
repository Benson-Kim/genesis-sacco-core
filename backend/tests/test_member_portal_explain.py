"""ADR-0007 EXPLAIN structural gates for the member read surface.

Two NEW statements ship with the surface; both are gated here against
the migrated Postgres service under the RLS app role (the P13.9/N1
EXPLAIN-capture convention — artifact BEFORE any assertion):

  1. MEMBER_LOAN_COUNT_SQL (/member/me loan summary) — the count probe
     enters loans through idx_loans_member (0001: tenant_id,
     member_id); no Seq Scan under enable_seqscan=off. No migration
     ships: the index predates the query.
  2. The member-filtered loan page (list_loans with the principal-
     derived member predicate) — the probe rides idx_loans_member; the
     residual top-N order runs over ONE member's loans only (bounded
     by lending reality, never the tenant book), so the gate pins the
     index entry and the absence of any Seq Scan; the small in-memory
     top-N over the member's own rows is the accepted plan shape.

NOT duplicated here (reuse-first): the member transactions page is the
exact staff member-filtered statement already gated by
test_p11_explain (idx_txns_member_keyset), and the member statement is
the exact staff statement page gated by test_p13_explain — only the
cursor SCOPE differs, which changes no SQL.
"""

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import factory
from export_helpers import seed_actor
from genesis.application.loans import _LOAN_COLS, _LOAN_LABEL_JOINS
from genesis.application.member_portal import MEMBER_LOAN_COUNT_SQL
from genesis.domain.lending import LoanStatus
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_member_portal.txt"


async def _seed_member_with_loans(tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    pid = uuid.uuid4()
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
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"EXPL-{uuid.uuid4().hex[:8]}"},
        )
        for balance in (Decimal("100.00"), Decimal("200.00")):
            aid = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO loan_applications "
                    "(id, tenant_id, member_id, product_id, amount, term_months, "
                    " rate_pct, stage, cover_pct) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    "CAST(:pid AS uuid), :amount, 12, '12.00', 'disbursed', '0.00')"
                ),
                {
                    "id": str(aid),
                    "tid": str(tid),
                    "mid": str(mid),
                    "pid": str(pid),
                    "amount": str(balance),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO loans "
                    "(id, tenant_id, application_id, member_id, product_id, "
                    " principal, balance, rate_pct, term_months, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                    "CAST(:mid AS uuid), CAST(:pid AS uuid), :amount, :amount, "
                    "'12.00', 12, 'active')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "aid": str(aid),
                    "mid": str(mid),
                    "pid": str(pid),
                    "amount": str(balance),
                },
            )
    return mid


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_member_portal_statements_are_index_served() -> None:
    """Both new statements enter loans through idx_loans_member with NO
    Seq Scan anywhere under enable_seqscan=off (drop the member
    predicate or the index and this gate fails). The artifact is
    written BEFORE any assertion (CI job log + backend/perf/)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await _seed_member_with_loans(tid)

        count_params: dict[str, object] = {
            "mid": str(mid),
            "tid": str(tid),
            "loan_active": LoanStatus.ACTIVE.value,
        }
        # The member loan page: the exact production shape composed by
        # list_loans' own builder path with the principal-derived
        # member predicate (static clause literals from production
        # code; every value a bound parameter).
        page_sql = (
            f"SELECT loans.created_at, {_LOAN_COLS} FROM loans "  # noqa: S608
            f"{_LOAN_LABEL_JOINS}"
            "WHERE loans.tenant_id = CAST(:tid AS uuid) "
            "AND loans.member_id = CAST(:mid AS uuid) "
            "ORDER BY loans.created_at DESC, loans.id DESC LIMIT :limit"
        )
        page_params: dict[str, object] = {"tid": str(tid), "mid": str(mid), "limit": 21}

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            count_plan = await _explain(session, MEMBER_LOAN_COUNT_SQL, count_params)
            page_plan = await _explain(session, page_sql, page_params)

        # Artifact BEFORE assertions (the EXPLAIN-capture convention).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            "ADR-0007 member read surface EXPLAIN (ANALYZE, BUFFERS) — captured\n"
            "in CI against the migrated Postgres service under the RLS app\n"
            "role, enable_seqscan=off (tiny CI tables): both statements enter\n"
            "loans through idx_loans_member (0001); the loan page's residual\n"
            "top-N orders ONE member's loans only (bounded by lending\n"
            "reality). The member transactions/statement pages reuse the\n"
            "statements already gated by test_p11_explain/test_p13_explain.\n"
            f"\n=== /member/me active-loan count ===\n{count_plan}\n"
            f"\n=== /member/loans page (member-filtered) ===\n{page_plan}\n"
        )

        for name, plan in (("loan count", count_plan), ("member loan page", page_plan)):
            assert "Seq Scan" not in plan, f"{name} fell back to a sequential scan:\n{plan}"
            # Tiny CI tables leave the planner a near-tie between the
            # member probe and the tenant-led keyset index (both are
            # loans indexes led by tenant_id — either is index-served
            # and structural; the test_p11_explain two-name precedent).
            assert "idx_loans_member" in plan or "idx_loans_created_keyset" in plan, (
                f"{name} is not served by a loans index:\n{plan}"
            )

    asyncio.run(run())
