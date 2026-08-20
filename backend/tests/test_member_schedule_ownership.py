"""Issue #33 (from the !7 review): structural, not order-safe.

Item 3 falsifier: /member/loans/{loan_id} used to be safe only because
the ownership-scoped get_loan raised BEFORE the schedule read in
member_portal.get_member_loan — reorder or parallelize those awaits
and the schedule leaked by loan-id guessing. get_schedule now takes
the principal-derived member id itself, so this module calls the
schedule read ALONE (bypassing the wrapper's preceding get_loan) and
proves a non-owner gets nothing: another member's loan is
indistinguishable from a nonexistent one (404, least disclosure).

Item 1 falsifier: this module is NOT in the conftest maint-DSN
allowlist, so DATABASE_MAINT_URL must be the fenced sentinel here and
any connection attempt with it must fail loudly — the RLS-owner DSN
can never quietly serve a read outside the EXPLAIN modules.
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import MAINT_DSN_FENCE_SENTINEL
from db_helpers import factory
from export_helpers import seed_actor
from genesis.application import loans as loans_service
from genesis.errors import NotFoundError
from genesis.infrastructure.db import get_engine
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_member(session: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
            "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Sched Member')"
        ),
        {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
    )
    return mid


async def _seed_loan_with_schedule(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Owner + non-owner members; one disbursed loan (owner's) with 2 rows."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    loan_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        owner = await _seed_member(session, tid)
        other = await _seed_member(session, tid)
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"SCHED-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '200.00', 2, '12.00', 'disbursed', '0.00')"
            ),
            {"id": str(aid), "tid": str(tid), "mid": str(owner), "pid": str(pid)},
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), '200.00', '200.00', "
                "'12.00', 2, 'active')"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(aid),
                "mid": str(owner),
                "pid": str(pid),
            },
        )
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due, paid_amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                ":no, CAST(:due AS date), '100.00', '2.00', '102.00', '0.00')"
            ),
            [
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "lid": str(loan_id),
                    "no": no,
                    "due": due,
                }
                for no, due in ((1, "2026-09-01"), (2, "2026-10-01"))
            ],
        )
    return owner, other, loan_id


def test_schedule_read_alone_returns_nothing_for_a_non_owner() -> None:
    """The falsifier (issue #33 item 3): get_schedule called DIRECTLY —
    no prior ownership-scoped get_loan by the caller — must 404 a
    non-owner (never rows, never an empty 200-shaped list) and must
    still serve the owner in full."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        owner, other, loan_id = await _seed_loan_with_schedule(tid)

        async with tenant_session(factory(), tid) as session:
            # Owner: full schedule (the predicate hides nothing of one's own).
            rows = await loans_service.get_schedule(session, tid, loan_id, member_id=owner)
            assert [r.installment_no for r in rows] == [1, 2]
            assert rows[0].total_due == Decimal("102.00")

        async with tenant_session(factory(), tid) as session:
            # Non-owner: indistinguishable from a nonexistent loan.
            with pytest.raises(NotFoundError):
                await loans_service.get_schedule(session, tid, loan_id, member_id=other)

        async with tenant_session(factory(), tid) as session:
            # Unscoped staff read is unchanged (no member predicate).
            rows = await loans_service.get_schedule(session, tid, loan_id)
            assert len(rows) == 2

    asyncio.run(run())


def test_maint_dsn_is_fenced_outside_the_explain_modules() -> None:
    """The fence falsifier (issue #33 item 1): this module is not
    allowlisted, so the RLS-owner DSN must be unusable here — the env
    var carries the sentinel and connecting with it fails loudly."""
    fenced = os.environ.get("DATABASE_MAINT_URL")
    if fenced is None:
        pytest.skip("DATABASE_MAINT_URL not set (dev without the owner DSN)")
    assert fenced == MAINT_DSN_FENCE_SENTINEL

    async def probe() -> None:
        async with get_engine(fenced).connect():
            pass  # pragma: no cover — the connect above must raise

    # The fence message IS the unresolvable .invalid host, so the
    # failure names its own cause.
    with pytest.raises(OperationalError, match=r"fenced|invalid"):
        asyncio.run(probe())
