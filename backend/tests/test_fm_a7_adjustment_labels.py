"""FM-A7 — adjustment-register label VALUES, directly value-asserted.

The identifier-doctrine round joined member labels onto the
adjustments register (`_ADJUSTMENT_READ_COLS` + `_ADJUSTMENT_LABEL_JOINS`)
but recorded FM-A7 honestly as NOT value-asserted: the register suites
exercised the joined SQL end-to-end while the label VALUES were never
compared against a seeded oracle (repayment seeding was called heavy
and postponed). This suite closes that leg with the full honest chain:

    member (controlled labels) -> product -> application -> disburse
    -> repayment (hand-computed amount) -> maker-checker adjustment

and asserts the register row's `member_no` / `member_name` verbatim
against the seeded literals — labels resolved server-side in the SAME
register statement, never invented client-side.

Hand-computed oracles (never captured from the implementation):
  * labels: `GP-0107` / `Adjustment Oracle Member` (seeded verbatim).
  * penalties component: penalty_due is seeded at 150.00 and the
    repayment waterfall pays penalties FIRST, so the adjustment's
    penalties component is exactly 150.00 (min(150.00, 2500.00)).
  * amount: 2500.00 by construction (the repayment amount), and the
    0025 ck_repayment_adjustments_components identity (amount equals
    the sum of penalties, interest, principal) must hold row-wise.

Falsifiability: drop the members hop from `_ADJUSTMENT_LABEL_JOINS`
(or re-point it at the wrong key) and the verbatim-label assertions
fail on None / a mismatched literal. Weaken the waterfall and the
150.00 penalties oracle fails.

Honest-null leg: the adjustments register has NO system rows by
schema (repayment_adjustments.loan_id -> loans.member_id are both
NOT NULL FKs), so the labels-are-never-invented proof rides the
join-free LOCKED-read shape instead: the production 20-column
`_ADJUSTMENT_COLS` statement mapped through `_row_to_adjustment`
must keep `member_no` / `member_name` / `original_txn_ref` None —
labels ride ONLY the joined read statements. Falsifiable: make the
mapper fabricate a label (empty string, placeholder) and the leg
fails on `is None`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import factory, seed_user, unique_email
from export_helpers import seed_member_no
from genesis.application.corrections import (
    _ADJUSTMENT_COLS,
    AdjustmentStatus,
    _row_to_adjustment,
    approve_repayment_adjustment,
    list_adjustments,
    request_repayment_adjustment,
)
from genesis.application.ledger import disburse_loan
from genesis.application.loans import RepaymentResult, record_repayment
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: Hand-computed label oracles — the seeded literals, verbatim.
ORACLE_MEMBER_NO = "GP-0107"
ORACLE_MEMBER_NAME = "Adjustment Oracle Member"
#: Hand-computed money oracles (comma-separated prose: amount, penalties).
ORACLE_REPAY_AMOUNT = Decimal("2500.00")
ORACLE_PENALTIES = Decimal("150.00")


async def _seed_actor() -> tuple[uuid.UUID, uuid.UUID]:
    """(tenant_id, user_id) with the full permission matrix seeded."""
    email = unique_email()
    tid, _role_id = await seed_user(email, role_name="System Admin")
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    return tid, uuid.UUID(str(user_id))


async def _seed_checker(tid: uuid.UUID) -> uuid.UUID:
    """A DISTINCT same-tenant System Admin (maker <> checker SoD)."""
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"), {"name": "System Admin"}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'FM-A7 Checker', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    return user_id


async def _seed_oracle_loan(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """(loan_id, member_id): the controlled-label member with a deposit
    account satisfying the multiplier gate (24,000 principal against
    100,000 x 3.00), disbursed through the production disburse path."""
    mid = await seed_member_no(tid, ORACLE_MEMBER_NO, name=ORACLE_MEMBER_NAME)
    pid = uuid.uuid4()
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '100000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"FM-A7 Product {pid.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, stage) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), 24000.00, 12, 12.00, 'approved')"
            ),
            {"id": str(app_id), "tid": str(tid), "mid": str(mid), "pid": str(pid)},
        )
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id, mid


async def _seed_penalty(tid: uuid.UUID, loan_id: uuid.UUID) -> None:
    """Fixture stand-in for the P13.8 accrual (the test_corrections
    precedent): a receivable-side penalty on the loan row, sized to the
    hand-computed 150.00 oracle."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loans SET penalty_due = :p "
                "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"p": str(ORACLE_PENALTIES), "lid": str(loan_id), "tid": str(tid)},
        )


async def _repay(tid: uuid.UUID, actor: uuid.UUID, loan_id: uuid.UUID) -> RepaymentResult:
    async with tenant_session(factory(), tid) as session:
        return await record_repayment(
            session, tid, actor, loan_id, amount=ORACLE_REPAY_AMOUNT, channel=Channel.BANK
        )


async def _repayment_id_for_txn(tid: uuid.UUID, txn_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(factory(), tid) as session:
        rid = (
            await session.execute(
                text(
                    "SELECT id FROM repayments WHERE transaction_id = CAST(:t AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) AND amount > 0"
                ),
                {"t": str(txn_id), "tid": str(tid)},
            )
        ).scalar_one()
    return uuid.UUID(str(rid))


def test_fm_a7_register_labels_resolve_to_the_seeded_oracle_verbatim() -> None:
    """FM-A7 (value leg): the full seeded chain, then the register row's
    label VALUES verbatim — for BOTH a pending and an approved
    adjustment (labels survive the status move)."""

    async def run() -> None:
        tid, maker = await _seed_actor()
        loan_id, _mid = await _seed_oracle_loan(tid)
        await _seed_penalty(tid, loan_id)

        # Repayment 1 -> maker-checker adjustment through APPROVAL.
        repay_1 = await _repay(tid, maker, loan_id)
        repayment_1 = await _repayment_id_for_txn(tid, repay_1.txn_id)
        async with tenant_session(factory(), tid) as session:
            pending_1 = await request_repayment_adjustment(
                session, tid, maker, repayment_1, reason="FM-A7 oracle leg (approved)"
            )
        checker = await _seed_checker(tid)
        async with tenant_session(factory(), tid) as session:
            await approve_repayment_adjustment(session, tid, checker, pending_1.id)

        # Repayment 2 -> a second adjustment left PENDING (the register
        # orders pending first; both bands must carry the labels).
        repay_2 = await _repay(tid, maker, loan_id)
        repayment_2 = await _repayment_id_for_txn(tid, repay_2.txn_id)
        async with tenant_session(factory(), tid) as session:
            pending_2 = await request_repayment_adjustment(
                session, tid, maker, repayment_2, reason="FM-A7 oracle leg (pending)"
            )

        async with tenant_session(factory(), tid) as session:
            page = await list_adjustments(session, tid, cursor=None, limit=10)
        rows = {row.id: row for row in page.items}
        assert pending_2.id in rows and pending_1.id in rows

        # Pending-first order (the register's job order) — the pending
        # row precedes the approved one.
        order = [row.id for row in page.items]
        assert order.index(pending_2.id) < order.index(pending_1.id)

        for adj_id in (pending_1.id, pending_2.id):
            row = rows[adj_id]
            # THE FM-A7 oracle: label VALUES verbatim, never invented.
            assert row.member_no == ORACLE_MEMBER_NO
            assert row.member_name == ORACLE_MEMBER_NAME
        # Status split honest: one posted, one pending.
        assert rows[pending_1.id].status is AdjustmentStatus.POSTED
        assert rows[pending_2.id].status is AdjustmentStatus.PENDING_APPROVAL

        # Hand-computed money legs (approved row): the repayment amount
        # by construction; penalties paid FIRST so exactly 150.00; the
        # 0025 component identity holds.
        approved = rows[pending_1.id]
        assert approved.amount == ORACLE_REPAY_AMOUNT
        assert approved.penalties == ORACLE_PENALTIES
        assert approved.penalties + approved.interest + approved.principal == approved.amount

        # Original-txn linkage: the register's txn_ref is the adjusted
        # repayment's ref (server-minted RP- ref carried by the row).
        assert approved.original_txn_ref == repay_1.txn_ref
        assert rows[pending_2.id].original_txn_ref == repay_2.txn_ref

    asyncio.run(run())


def test_fm_a7_locked_read_shape_keeps_honest_null_labels() -> None:
    """FM-A7 (honest-null leg): the join-free 20-column locked-read
    shape maps to None labels — labels ride ONLY the joined read
    statements and are never fabricated by the mapper. (The register
    itself has no system rows by schema: loan_id and loans.member_id
    are NOT NULL FKs, so the no-member case cannot be seeded — this is
    the honest equivalent of the FM-A2 system-row leg.)"""

    async def run() -> None:
        tid, maker = await _seed_actor()
        loan_id, _mid = await _seed_oracle_loan(tid)
        await _seed_penalty(tid, loan_id)
        repay = await _repay(tid, maker, loan_id)
        repayment_id = await _repayment_id_for_txn(tid, repay.txn_id)
        async with tenant_session(factory(), tid) as session:
            pending = await request_repayment_adjustment(
                session, tid, maker, repayment_id, reason="FM-A7 honest-null leg"
            )

        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {_ADJUSTMENT_COLS} FROM repayment_adjustments "  # noqa: S608
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"id": str(pending.id), "tid": str(tid)},
                )
            ).one()
        record = _row_to_adjustment(row)
        assert record.id == pending.id
        assert record.member_no is None
        assert record.member_name is None
        assert record.original_txn_ref is None

    asyncio.run(run())
