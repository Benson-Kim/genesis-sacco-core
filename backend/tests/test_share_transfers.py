"""Integration tests for P13.11 §C: share transfer at exit.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Failure-mode coverage (each falsifiable — remove the guard and the
test fails):

  7a. Opposing transfers (A->B and B->A) run concurrently WITHOUT
      deadlocking — the id-ordered member locks are the guard
      (EXIT criterion; lock in (from, to) order instead and Postgres
      aborts one side with a deadlock error).
  7b. The amount is re-verified under the lock: two concurrent
      transfers spending the same balance produce exactly ONE transfer
      (side-effect counts), never an overdraft.
  7c. A non-active transferee (exited, arrears) is refused UNDER the
      lock; a non-active transferor likewise; self-transfer is
      refused and DB-unrepresentable.
  9.  Kill-switch atomicity: an abort after postings + balance updates
      leaves ZERO partial state (no transfer row, no ST- transactions,
      no balance drift, no audit, no outbox).
  Ledger integrity: the transfer posts TWO member-attributed legs
      through the clearing account, so each member's reconstructed
      share history stays exact (proven via the shared ADB helper).
  Tenant scoping: issue-#17 probe — session AS the rows' tenant,
      foreign tenant argument -> zero transfers.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import count, seed_actor, seed_member
from genesis.application import dividends as dividends_module
from genesis.application.dividends import transfer_shares
from genesis.application.period_balances import average_daily_balance
from genesis.errors import ConflictError, InvalidInputError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside the transfer transaction, before commit."""


async def _share_balance(tid: uuid.UUID, mid: uuid.UUID) -> Decimal:
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                text(
                    "SELECT balance FROM share_accounts "
                    "WHERE member_id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(mid), "tid": str(tid)},
            )
        ).scalar_one()
    return Decimal(str(value))


async def _set_status(tid: uuid.UUID, mid: uuid.UUID, status: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE members SET status = :st "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"st": status, "id": str(mid), "tid": str(tid)},
        )


async def _transfer(tid: uuid.UUID, from_mid: uuid.UUID, to_mid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await transfer_shares(
            session, tid, None, from_mid, to_member_id=to_mid, amount=Decimal(amount)
        )


def test_transfer_moves_shares_atomically_with_member_attributed_legs() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_from = await seed_member(tid, name="Leaver", shares="10000.00")
        m_to = await seed_member(tid, name="Stayer", shares="500.00")
        async with tenant_session(factory(), tid) as session:
            result = await transfer_shares(
                session, tid, None, m_from, to_member_id=m_to, amount=Decimal("2500.00")
            )
        assert result.amount == Decimal("2500.00")
        assert result.from_balance_after == Decimal("7500.00")
        assert result.to_balance_after == Decimal("3000.00")
        assert result.out_txn_ref == "ST-000001"
        assert result.in_txn_ref == "ST-000002"
        assert await _share_balance(tid, m_from) == Decimal("7500.00")
        assert await _share_balance(tid, m_to) == Decimal("3000.00")

        # TWO member-attributed transactions through the clearing
        # account, which nets to zero (hand-computed: 2500 DR = 2500 CR).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions "
                "WHERE type IN ('share_transfer_out', 'share_transfer_in')",
            )
            == 2
        )
        async with tenant_session(factory(), tid) as session:
            clearing_net = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(CASE WHEN side = 'debit' THEN amount "
                        "ELSE -amount END), 0) FROM ledger_entries "
                        "WHERE account = 'clearing.share_transfers'"
                    )
                )
            ).scalar_one()
        assert Decimal(str(clearing_net)) == Decimal("0")

        # The member-attributed legs keep the ledger-reconstructed
        # share history exact for BOTH sides: reconstructing today's
        # end-of-day balances from the ledger returns the post-transfer
        # figures (falsifiable: post the transfer as one netted
        # transaction and the transferor reconstructs 10,000.00 while
        # the transferee reconstructs 500.00).
        today = date.today()
        async with tenant_session(factory(), tid) as session:
            from_eod = await average_daily_balance(
                session, tid, m_from, kind="share", start=today, end=today
            )
            to_eod = await average_daily_balance(
                session, tid, m_to, kind="share", start=today, end=today
            )
        assert from_eod == Decimal("7500.00")
        assert to_eod == Decimal("3000.00")

        # Audit + transfer row + outbox, all in the one transaction.
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'share_transfer.post'",
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events "
                "WHERE event_type = 'ledger.share_transfer_posted'",
            )
            == 1
        )

    asyncio.run(run())


def test_opposing_concurrent_transfers_do_not_deadlock() -> None:
    """EXIT criterion (failure mode 7): A->B and B->A concurrently.
    Both member locks are taken in member-id order, so the two
    transfers serialise; lock in (from, to) order instead and Postgres
    kills one side with a deadlock error after deadlock_timeout."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_a = await seed_member(tid, name="Alpha", shares="1000.00")
        m_b = await seed_member(tid, name="Beta", shares="1000.00")

        await asyncio.wait_for(
            asyncio.gather(
                _transfer(tid, m_a, m_b, "100.00"),
                _transfer(tid, m_b, m_a, "250.00"),
            ),
            timeout=30,
        )
        # Both landed: A -100 +250 = 1150, B +100 -250 = 850.
        assert await _share_balance(tid, m_a) == Decimal("1150.00")
        assert await _share_balance(tid, m_b) == Decimal("850.00")
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 2

    asyncio.run(run())


def test_concurrent_double_spend_is_refused_under_the_lock() -> None:
    """Failure mode 7: the amount is re-verified under the account row
    lock, never by a pre-check. Two transfers spending the same
    100.00 -> exactly one succeeds; the loser sees the post-commit
    balance and is refused. Falsifiable: check the balance before
    taking the lock and both land (overdraft)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_a = await seed_member(tid, name="Spender", shares="100.00")
        m_b = await seed_member(tid, name="First", shares="0")
        m_c = await seed_member(tid, name="Second", shares="0")

        results = await asyncio.wait_for(
            asyncio.gather(
                _transfer(tid, m_a, m_b, "60.00"),
                _transfer(tid, m_a, m_c, "60.00"),
                return_exceptions=True,
            ),
            timeout=30,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], ConflictError)
        # Side effects prove exactly-once: one transfer row, 40.00 left.
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 1
        assert await _share_balance(tid, m_a) == Decimal("40.00")

    asyncio.run(run())


def test_transfer_population_rules_are_enforced_under_the_locks() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_active = await seed_member(tid, name="Active", shares="1000.00")
        m_second = await seed_member(tid, name="Second", shares="1000.00")
        m_exited = await seed_member(tid, name="Exited", shares="0")
        m_arrears = await seed_member(tid, name="Arrears", shares="1000.00")
        m_dormant = await seed_member(tid, name="Dormant", shares="1000.00")
        await _set_status(tid, m_exited, "exited")
        await _set_status(tid, m_arrears, "arrears")
        await _set_status(tid, m_dormant, "dormant")

        # Transferee must be strictly ACTIVE (checked under the lock;
        # dormant refused per the P13.13 capability map).
        for target in (m_exited, m_arrears, m_dormant):
            with pytest.raises(ConflictError, match="active member"):
                await _transfer(tid, m_active, target, "100.00")

        # Transferor must be strictly ACTIVE (the withdrawal rule: an
        # arrears member may not move equity out from under their debt;
        # exited members cannot transact; dormant equity moves only
        # after a reactivating deposit — P13.13).
        for source in (m_exited, m_arrears, m_dormant):
            with pytest.raises(ConflictError, match="active members may transfer"):
                await _transfer(tid, source, m_active, "100.00")

        # Self-transfer is refused (and DB-unrepresentable by CHECK).
        with pytest.raises(InvalidInputError, match="same member"):
            await _transfer(tid, m_active, m_active, "100.00")

        # Amount above holdings is refused, least-disclosure.
        with pytest.raises(ConflictError, match="insufficient share balance"):
            await _transfer(tid, m_active, m_second, "1000.01")

        assert await count(tid, "SELECT count(*) FROM share_transfers") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions "
                "WHERE type IN ('share_transfer_out', 'share_transfer_in')",
            )
            == 0
        )

    asyncio.run(run())


def test_kill_switch_leaves_zero_partial_transfer_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXIT criterion (failure mode 9): crash after the postings and
    both balance updates, before commit (the audit writer raises) ->
    ZERO partial state. Falsifiable: post the ledger legs in their own
    transaction and the orphaned ST- postings survive."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")

        real_record_audit = dividends_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "share_transfer.post":
                raise _KillSwitchError()
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(dividends_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await _transfer(tid, m_from, m_to, "400.00")

        # ZERO partial state, by side-effect counts.
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions "
                "WHERE type IN ('share_transfer_out', 'share_transfer_in')",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events "
                "WHERE event_type = 'ledger.share_transfer_posted'",
            )
            == 0
        )
        assert await _share_balance(tid, m_from) == Decimal("1000.00")
        assert await _share_balance(tid, m_to) == Decimal("0.00")

        # The same transfer commits cleanly afterwards.
        await _transfer(tid, m_from, m_to, "400.00")
        assert await _share_balance(tid, m_from) == Decimal("600.00")
        assert await _share_balance(tid, m_to) == Decimal("400.00")

    asyncio.run(run())


def test_self_transfer_rows_are_unrepresentable_in_the_database() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid, name="Solo", shares="100.00")
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO share_transfers "
                        "(id, tenant_id, from_member_id, to_member_id, amount, "
                        " out_transaction_id, in_transaction_id) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                        "CAST(:m AS uuid), CAST(:m AS uuid), 10, "
                        "CAST(:t1 AS uuid), CAST(:t2 AS uuid))"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "m": str(mid),
                        "t1": str(uuid.uuid4()),
                        "t2": str(uuid.uuid4()),
                    },
                )

    asyncio.run(run())


def test_foreign_tenant_argument_transfers_nothing() -> None:
    """Issue-#17 probe: session AS the rows' own tenant (RLS satisfied),
    foreign tenant_id argument — only the explicit predicate refuses."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        foreign = uuid.uuid4()
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid) as session:
                await transfer_shares(
                    session, foreign, None, m_from, to_member_id=m_to, amount=Decimal("10.00")
                )
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 0
        assert await _share_balance(tid, m_from) == Decimal("1000.00")

    asyncio.run(run())


def test_share_transfer_endpoint_requires_members_approve() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post(
                f"/members/{m_from}/share-transfers",
                json={"to_member_id": str(m_to), "amount": "250.00"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["amount"] == "250.00"
            assert body["out_txn_ref"].startswith("ST-")

            # Unknown/money fields are a 422 (extra="forbid").
            forged = await client.post(
                f"/members/{m_from}/share-transfers",
                json={"to_member_id": str(m_to), "amount": "1.00", "rate_pct": "9"},
                headers=headers,
            )
            assert forged.status_code == 422
        assert await _share_balance(tid, m_from) == Decimal("750.00")

    asyncio.run(run())
