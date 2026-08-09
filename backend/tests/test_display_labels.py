"""Display-label resolution on the list/read models (identifier doctrine).

Operator-facing rows carry human labels — the member number and
registered name (plus the product name where a product rides the row)
— resolved server-side in the SAME list statement via PK-served
joins. Oracles are HAND-COMPUTED from the seeded literals below (never
captured from the implementation): the seeded member is `GP-0101` /
`Label Oracle Member`, the seeded product `Oracle Product`.

Falsifiability: drop the label join from any list statement and its
leg here fails on the None label; re-point the join at the wrong key
and the leg fails on the mismatched literal. The system-posting leg
proves honesty: a row without a member keeps None labels — labels are
never invented.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import factory, seed_user, unique_email
from export_helpers import seed_member_no
from genesis.application.loan_applications import list_applications
from genesis.application.loans import list_loans
from genesis.application.transactions import list_transactions
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: Hand-computed label oracles (the seeded literals, verbatim).
ORACLE_MEMBER_NO = "GP-0101"
ORACLE_MEMBER_NAME = "Label Oracle Member"
ORACLE_PRODUCT_NAME = "Oracle Product"


async def _seed_product(tid: uuid.UUID, name: str) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": name},
        )
    return pid


async def _seed_application(tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), 1000.00, 12, 12.00)"
            ),
            {"id": str(aid), "tid": str(tid), "mid": str(mid), "pid": str(pid)},
        )
    return aid


async def _seed_loan(tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID, aid: uuid.UUID) -> uuid.UUID:
    loan_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), 1000.00, 1000.00, '12.00', 12)"
            ),
            {"id": str(loan_id), "tid": str(tid), "aid": str(aid), "mid": str(mid)},
        )
    return loan_id


async def _seed_txn(tid: uuid.UUID, mid: uuid.UUID | None) -> uuid.UUID:
    """One balanced deposit posting: DR cash.mpesa 50.00 / CR
    member.deposits 50.00 (equal to transactions.amount, so the
    deferred ledger trigger accepts the commit). member_id NULL seeds
    the system-posting leg."""
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, txn_ref, member_id, type, amount, channel) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
                "CAST(:mid AS uuid), 'deposit', 50.00, 'mpesa')"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "mid": str(mid) if mid is not None else None,
                "ref": f"MP-{txn_id.hex[:8].upper()}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO ledger_entries "
                "(id, tenant_id, transaction_id, account, side, amount) VALUES "
                "(CAST(:dr AS uuid), CAST(:tid AS uuid), CAST(:txn AS uuid), "
                "'cash.mpesa', 'debit', 50.00), "
                "(CAST(:cr AS uuid), CAST(:tid AS uuid), CAST(:txn AS uuid), "
                "'member.deposits', 'credit', 50.00)"
            ),
            {
                "dr": str(uuid.uuid4()),
                "cr": str(uuid.uuid4()),
                "tid": str(tid),
                "txn": str(txn_id),
            },
        )
    return txn_id


def test_list_rows_resolve_human_labels_server_side() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await seed_member_no(tid, ORACLE_MEMBER_NO, name=ORACLE_MEMBER_NAME)
        pid = await _seed_product(tid, ORACLE_PRODUCT_NAME)
        aid = await _seed_application(tid, mid, pid)
        loan_id = await _seed_loan(tid, mid, pid, aid)
        member_txn = await _seed_txn(tid, mid)
        system_txn = await _seed_txn(tid, None)

        async with tenant_session(factory(), tid) as session:
            apps, _ = await list_applications(session, tid, limit=20)
            loans, _ = await list_loans(session, tid, limit=20)
            txns, _ = await list_transactions(session, tid, limit=20)

        # Applications register: labels equal the seeded literals.
        app = next(a for a in apps if a.id == aid)
        assert app.member_no == ORACLE_MEMBER_NO
        assert app.member_name == ORACLE_MEMBER_NAME
        assert app.product_name == ORACLE_PRODUCT_NAME

        # Loan book: same oracles on the loan row.
        loan = next(ln for ln in loans if ln.id == loan_id)
        assert loan.member_no == ORACLE_MEMBER_NO
        assert loan.member_name == ORACLE_MEMBER_NAME
        assert loan.product_name == ORACLE_PRODUCT_NAME

        # Ledger listing: the member posting carries the labels...
        posted = next(t for t in txns if t.id == member_txn)
        assert posted.member_no == ORACLE_MEMBER_NO
        assert posted.member_name == ORACLE_MEMBER_NAME
        # ...and the system posting keeps honest Nones (labels are
        # never invented for a row that carries no member).
        system = next(t for t in txns if t.id == system_txn)
        assert system.member_id is None
        assert system.member_no is None
        assert system.member_name is None

    asyncio.run(run())
