"""Explicit tenant-predicate falsifiability suite (issue #17, gate 1.6 v1.1).

The RLS leakage suite (test_tenancy_leakage.py) proves the DATABASE
fence. This suite proves the APPLICATION fence works on its own — the
explicit bound `tenant_id = :tid` predicates added per issue #17.

Falsifiability design (issue #17 acceptance criterion 2): every test
opens the session AS the row's own tenant, so RLS is satisfied and can
never mask a missing predicate. The service is then called with a
DIFFERENT tenant_id argument. Only the explicit predicate can refuse
the row — remove any predicate under test and its test fails (the
query would find the RLS-visible row and return/mutate it).

This mirrors the real threat: a misconfigured session (wrong
app.tenant_id) must not equal cross-tenant access; the query-level
predicate is the second, independent fence.
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import factory, seed_user, unique_email
from genesis.application import loan_applications as applications_service
from genesis.application import loan_products as products_service
from genesis.application import members as members_service
from genesis.application import transactions as txn_service
from genesis.application.ledger import post_reversal
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.domain.lending import ApplicationStage
from genesis.errors import NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_member(tid: uuid.UUID, *, deposit: str = "0") -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Predicate Probe')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        for table in ("deposit_accounts", "share_accounts"):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": deposit},
            )
    return mid


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Pred-{uuid.uuid4().hex[:8]}"},
        )
    return pid


async def _seed_application(
    tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID, *, stage: str = "submitted"
) -> uuid.UUID:
    aid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), 1000, 12, '12.00', :stage, '0.00')"
            ),
            {
                "id": str(aid),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(pid),
                "stage": stage,
            },
        )
    return aid


def test_member_reads_and_writes_refuse_foreign_tenant_argument() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        foreign = uuid.uuid4()
        mid = await _seed_member(tid, deposit="100.00")

        # Session AS the row's tenant (RLS passes); foreign tid argument.
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await members_service.get_member(session, foreign, mid)
            with pytest.raises(NotFoundError):
                await members_service.update_member(
                    session, foreign, None, mid, version=1, name="hijack"
                )
            with pytest.raises(NotFoundError):
                await members_service.member_statement(session, foreign, mid)
            page = await members_service.list_members(session, foreign)
            assert page.items == []
        # The write never landed.
        async with tenant_session(factory(), tid) as session:
            name = (
                await session.execute(
                    text("SELECT name FROM members WHERE id = CAST(:m AS uuid)"),
                    {"m": str(mid)},
                )
            ).scalar_one()
        assert str(name) == "Predicate Probe"

    asyncio.run(run())


def test_product_reads_and_writes_refuse_foreign_tenant_argument() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        foreign = uuid.uuid4()
        pid = await _seed_product(tid)

        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await products_service.get_product(session, foreign, pid)
            with pytest.raises(NotFoundError):
                await products_service.update_product(
                    session, foreign, None, pid, version=1, rate_pct=Decimal("99.00")
                )
            listed = await products_service.list_products(session, foreign)
            assert listed == []
        # The rate was never touched.
        async with tenant_session(factory(), tid) as session:
            rate = (
                await session.execute(
                    text("SELECT rate_pct FROM loan_products WHERE id = CAST(:p AS uuid)"),
                    {"p": str(pid)},
                )
            ).scalar_one()
        assert Decimal(str(rate)) == Decimal("12.00")

    asyncio.run(run())


def test_application_paths_refuse_foreign_tenant_argument() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        foreign = uuid.uuid4()
        mid = await _seed_member(tid, deposit="5000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, mid, pid, stage="committee")

        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await applications_service.get_application(session, foreign, aid)
            with pytest.raises(NotFoundError):
                await applications_service.transition_stage(
                    session,
                    foreign,
                    None,
                    aid,
                    version=1,
                    target=ApplicationStage.REJECTED,
                    system_actor=True,
                )
            with pytest.raises(NotFoundError):
                await applications_service.cast_vote(
                    session, foreign, uuid.uuid4(), aid, Vote.APPROVE
                )
            with pytest.raises(NotFoundError):
                await applications_service.recompute_cover(session, foreign, aid)
            items, _ = await applications_service.list_applications(session, foreign)
            assert items == []
        # Stage untouched; no vote recorded.
        async with tenant_session(factory(), tid) as session:
            stage = (
                await session.execute(
                    text("SELECT stage FROM loan_applications WHERE id = CAST(:a AS uuid)"),
                    {"a": str(aid)},
                )
            ).scalar_one()
            votes = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM committee_votes "
                        "WHERE application_id = CAST(:a AS uuid)"
                    ),
                    {"a": str(aid)},
                )
            ).scalar_one()
        assert str(stage) == "committee"
        assert int(votes) == 0

    asyncio.run(run())


def test_reversal_refuses_foreign_tenant_argument() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        foreign = uuid.uuid4()
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            posted = await txn_service.record_deposit(
                session,
                tid,
                None,
                mid,
                amount=Decimal("50.00"),
                channel=Channel.BANK,
            )

        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await post_reversal(session, foreign, posted.txn_id)
        # No reversal row was written.
        async with tenant_session(factory(), tid) as session:
            reversals = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM transactions WHERE reversal_of_id = CAST(:t AS uuid)"
                    ),
                    {"t": str(posted.txn_id)},
                )
            ).scalar_one()
        assert int(reversals) == 0

    asyncio.run(run())
