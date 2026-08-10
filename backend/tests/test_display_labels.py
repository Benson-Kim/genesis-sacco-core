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
from genesis.application.audit_log import list_audit_log
from genesis.application.corrections import list_write_offs
from genesis.application.dividends import list_share_transfers
from genesis.application.loan_applications import list_applications
from genesis.application.loans import list_loans
from genesis.application.member_exits import list_exits
from genesis.application.recovery import list_worklist
from genesis.application.transactions import list_transactions
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: Hand-computed label oracles (the seeded literals, verbatim).
ORACLE_MEMBER_NO = "GP-0101"
ORACLE_MEMBER_NAME = "Label Oracle Member"
ORACLE_PRODUCT_NAME = "Oracle Product"
#: Second party for the share-transfer legs.
ORACLE_PEER_NO = "GP-0102"
ORACLE_PEER_NAME = "Label Oracle Peer"


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
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(aid),
                "mid": str(mid),
                "pid": str(pid),
            },
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


async def _seed_exit(tid: uuid.UUID, mid: uuid.UUID) -> uuid.UUID:
    exit_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO member_exits (id, tenant_id, member_id, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), 'requested')"
            ),
            {"id": str(exit_id), "tid": str(tid), "m": str(mid)},
        )
    return exit_id


async def _seed_pending_transfer(
    tid: uuid.UUID, from_mid: uuid.UUID, to_mid: uuid.UUID
) -> uuid.UUID:
    """Pending workflow row (maker recorded, snapshot present — the
    pending-snapshot CHECK requires both)."""
    transfer_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        maker = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "INSERT INTO share_transfers "
                "(id, tenant_id, from_member_id, to_member_id, amount, status, "
                " created_by, from_balance_at_request) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:f AS uuid), "
                "CAST(:t AS uuid), 10.00, 'pending', CAST(:u AS uuid), 10.00)"
            ),
            {
                "id": str(transfer_id),
                "tid": str(tid),
                "f": str(from_mid),
                "t": str(to_mid),
                "u": str(maker),
            },
        )
    return transfer_id


async def _seed_recovery_case(tid: uuid.UUID, loan_id: uuid.UUID) -> None:
    async with tenant_session(factory(), tid) as session:
        opener = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "UPDATE loans SET days_past_due = 151, classification = 'substandard' "
                "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"lid": str(loan_id), "tid": str(tid)},
        )
        await session.execute(
            text(
                "INSERT INTO recovery_cases (id, tenant_id, loan_id, opened_by, "
                "classification_at_open, days_past_due_at_open) "
                "VALUES (gen_random_uuid(), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "CAST(:actor AS uuid), 'substandard', 151)"
            ),
            {"tid": str(tid), "lid": str(loan_id), "actor": str(opener)},
        )


async def _seed_write_off(tid: uuid.UUID, loan_id: uuid.UUID, mid: uuid.UUID) -> uuid.UUID:
    wid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_write_offs "
                "(id, tenant_id, loan_id, member_id, balance, penalty_due, "
                " total_written_off, classification, provision_pct, reason, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "CAST(:mid AS uuid), '100.00', '0', '100.00', 'loss', '100.00', "
                "'label oracle fixture', 'requested')"
            ),
            {"id": str(wid), "tid": str(tid), "lid": str(loan_id), "mid": str(mid)},
        )
    return wid


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


def test_workflow_registers_resolve_human_labels_server_side() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await seed_member_no(tid, ORACLE_MEMBER_NO, name=ORACLE_MEMBER_NAME)
        peer = await seed_member_no(tid, ORACLE_PEER_NO, name=ORACLE_PEER_NAME)
        pid = await _seed_product(tid, ORACLE_PRODUCT_NAME)
        aid = await _seed_application(tid, mid, pid)
        loan_id = await _seed_loan(tid, mid, pid, aid)
        exit_id = await _seed_exit(tid, peer)
        transfer_id = await _seed_pending_transfer(tid, mid, peer)
        await _seed_recovery_case(tid, loan_id)
        write_off_id = await _seed_write_off(tid, loan_id, mid)

        async with tenant_session(factory(), tid) as session:
            exits, _ = await list_exits(session, tid, limit=20)
            transfers = await list_share_transfers(session, tid, cursor=None, limit=20)
            worklist = await list_worklist(session, tid, cursor=None, limit=20)
            write_offs = await list_write_offs(session, tid, cursor=None, limit=20)

        # Exit register: the exiting peer's labels.
        exit_row = next(e for e in exits if e.id == exit_id)
        assert exit_row.member_no == ORACLE_PEER_NO
        assert exit_row.member_name == ORACLE_PEER_NAME

        # Share-transfer register: BOTH parties' labels.
        xfer = next(t for t in transfers.items if t.id == transfer_id)
        assert xfer.from_member_no == ORACLE_MEMBER_NO
        assert xfer.from_member_name == ORACLE_MEMBER_NAME
        assert xfer.to_member_no == ORACLE_PEER_NO
        assert xfer.to_member_name == ORACLE_PEER_NAME

        # Recovery worklist: the delinquent member's labels.
        case = next(w for w in worklist.items if w.loan_id == loan_id)
        assert case.member_no == ORACLE_MEMBER_NO
        assert case.member_name == ORACLE_MEMBER_NAME

        # Write-off committee register: the member's labels.
        woff = next(w for w in write_offs.items if w.id == write_off_id)
        assert woff.member_no == ORACLE_MEMBER_NO
        assert woff.member_name == ORACLE_MEMBER_NAME

    asyncio.run(run())


def test_audit_log_resolves_actor_name() -> None:
    """The audit console (the access-control-gated staff-identity
    resolution surface) labels the actor with the seeded user's full
    name — 'Test User', the db_helpers seeding literal; a system row
    (NULL actor) keeps the honest None."""

    async def run() -> None:
        tid, role_id = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            actor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO audit_log (tenant_id, actor_id, action, entity, entity_id) "
                    "VALUES (CAST(:tid AS uuid), CAST(:actor AS uuid), 'label.oracle', "
                    "'members', 'oracle-row'), "
                    "(CAST(:tid AS uuid), NULL, 'label.oracle.system', 'members', 'oracle-row')"
                ),
                {"tid": str(tid), "actor": str(actor)},
            )
        async with tenant_session(factory(), tid) as session:
            page = await list_audit_log(session, tid, role_id, limit=20)
        acted = next(e for e in page.items if e.action == "label.oracle")
        assert acted.actor_name == "Test User"
        system = next(e for e in page.items if e.action == "label.oracle.system")
        assert system.actor_id is None
        assert system.actor_name is None

    asyncio.run(run())
