"""Integration tests for the TWO-PHASE maker-checker share transfers
(issue #31 ledger (l), MR !83 — the !77 human-review HIGH finding) and
the ledger-(m) history register.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Failure-mode coverage (each falsifiable — remove the guard and the
test fails; oracles hand-computed in comments):

  FM1  Maker can never be checker: approve/reject by the maker is a
       409 with zero money side effects (server guard), and
       maker = approver is UNREPRESENTABLE at the database (the 0040
       ck_share_transfers_sod CHECK — direct-SQL probes).
  FM2  Assurance roles (Auditor) can never be checker (!47 B2).
  FM3  Snapshot drift between the phases -> 409 posting NOTHING:
       transferor-balance drift, transferor status drift and
       transferee status drift each refuse the approval.
  FM4  Opposing concurrent APPROVALS (A->B racing B->A) do NOT
       deadlock — the id-ordered member locks are the guard (lock in
       (from, to) order instead and Postgres kills one side after
       deadlock_timeout) — and produce EXACTLY ONE posted transfer by
       side-effect ROW COUNTS (the second approval sees the first's
       balance effect as snapshot drift: a clean 409, its row stays
       pending/rejectable — never a deadlock, never a double-post).
  FM5  Concurrent double-spend across two pending transfers of the
       same balance -> exactly one posts (row counts), no overdraft.
  FM6  Population rules at REQUEST time under the locks: non-active
       transferor/transferee refused, self-transfer refused (and
       DB-unrepresentable), amount above holdings refused.
  FM7  Kill-switch atomicity: an abort after postings + balance
       updates leaves ZERO partial state; the row STAYS pending.
  FM8  Replay with the same Idempotency-Key produces exactly one
       effect (side-effect row counts, never response equality alone).
  FM9  Cross-tenant probes (issue #17): session AS the rows' tenant,
       foreign tenant argument -> zero rows touched.
  FM10 The 0040 write-once/status-machine trigger: terminal rows and
       pinned columns cannot move even via direct SQL.
  FM11 Member notices: the approval enqueues ONE outbox notice per
       member (BOTH sides — the P13.13 insider-fraud detection
       control); remove either enqueue and the counts fail.

  Register (m): pending-first band order (the 0038 pattern), keyset
       pagination against a hand-built expected order, hard max 100
       at the contract, forged cursors refused, members:view gate
       (deny leg: Credit Committee holds no members:view).
  Ledger integrity: the approved transfer posts TWO member-attributed
       legs through the clearing account, so each member's
       ledger-reconstructed share history stays exact.
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
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import dividends as dividends_module
from genesis.application.dividends import (
    ShareTransferRecord,
    ShareTransferResult,
    ShareTransferStatus,
    approve_share_transfer,
    get_share_transfer,
    list_share_transfers,
    reject_share_transfer,
    request_share_transfer,
    share_transfer_transition,
)
from genesis.application.period_balances import average_daily_balance
from genesis.errors import ConflictError, InvalidInputError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside the approval transaction, before commit."""


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


async def _bump_share_balance(tid: uuid.UUID, mid: uuid.UUID, delta: str) -> None:
    """Simulate an out-of-band equity change (a dividend credit) so the
    persisted approval snapshot no longer matches (FM3)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE share_accounts SET balance = balance + :d "
                "WHERE member_id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"d": Decimal(delta), "m": str(mid), "tid": str(tid)},
        )


async def _request(
    tid: uuid.UUID, actor: uuid.UUID, from_mid: uuid.UUID, to_mid: uuid.UUID, amount: str
) -> ShareTransferRecord:
    async with tenant_session(factory(), tid) as session:
        return await request_share_transfer(
            session, tid, actor, from_mid, to_member_id=to_mid, amount=Decimal(amount)
        )


async def _approve(tid: uuid.UUID, actor: uuid.UUID, transfer_id: uuid.UUID) -> ShareTransferResult:
    async with tenant_session(factory(), tid) as session:
        return await approve_share_transfer(session, tid, actor, transfer_id)


async def _reject(
    tid: uuid.UUID, actor: uuid.UUID, transfer_id: uuid.UUID, version: int, reason: str = "no"
) -> ShareTransferRecord:
    async with tenant_session(factory(), tid) as session:
        return await reject_share_transfer(
            session, tid, actor, transfer_id, version=version, reason=reason
        )


async def _get(tid: uuid.UUID, transfer_id: uuid.UUID) -> ShareTransferRecord:
    async with tenant_session(factory(), tid) as session:
        return await get_share_transfer(session, tid, transfer_id)


async def _money_counts(tid: uuid.UUID) -> tuple[int, int, int, int]:
    """(posted transfers, ST transactions, posted audits, member notices)
    — the side-effect surfaces every money-moving guard must hold at
    zero delta when it refuses."""
    return (
        await count(tid, "SELECT count(*) FROM share_transfers WHERE status = 'posted'"),
        await count(
            tid,
            "SELECT count(*) FROM transactions "
            "WHERE type IN ('share_transfer_out', 'share_transfer_in')",
        ),
        await count(tid, "SELECT count(*) FROM audit_log WHERE action = 'share_transfer.posted'"),
        await count(
            tid,
            "SELECT count(*) FROM outbox_events WHERE event_type = 'share_transfer.member_notice'",
        ),
    )


# ---------------------------------------------------------------------------
# Phase 1 — the maker's request moves NO money
# ---------------------------------------------------------------------------


def test_request_creates_pending_row_and_moves_no_money() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        m_from = await seed_member(tid, name="Leaver", shares="10000.00")
        m_to = await seed_member(tid, name="Stayer", shares="500.00")

        record = await _request(tid, maker, m_from, m_to, "2500.00")
        assert record.status is ShareTransferStatus.PENDING
        assert record.amount == Decimal("2500.00")
        assert record.created_by == maker
        assert record.approved_by is None
        assert record.out_transaction_id is None
        assert record.in_transaction_id is None
        assert record.version == 1
        # The persisted approval snapshot (v1.1 rule 3), hand-computed:
        # the transferor held exactly 10000.00 at request.
        assert record.from_balance_at_request == Decimal("10000.00")

        # NO money moved: balances untouched, zero ST- transactions.
        assert await _share_balance(tid, m_from) == Decimal("10000.00")
        assert await _share_balance(tid, m_to) == Decimal("500.00")
        assert await _money_counts(tid) == (0, 0, 0, 0)
        assert (
            await count(tid, "SELECT count(*) FROM share_transfers WHERE status = 'pending'") == 1
        )
        assert (
            await count(
                tid, "SELECT count(*) FROM audit_log WHERE action = 'share_transfer.requested'"
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'share_transfer.requested'",
            )
            == 1
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Phase 2 — a distinct checker approves; money moves atomically
# ---------------------------------------------------------------------------


def test_approval_posts_atomically_with_member_attributed_legs_and_notices() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="Leaver", shares="10000.00")
        m_to = await seed_member(tid, name="Stayer", shares="500.00")

        pending = await _request(tid, maker, m_from, m_to, "2500.00")
        result = await _approve(tid, checker, pending.id)

        # Hand-computed: 10000 - 2500 = 7500; 500 + 2500 = 3000.
        assert result.amount == Decimal("2500.00")
        assert result.from_balance_after == Decimal("7500.00")
        assert result.to_balance_after == Decimal("3000.00")
        assert result.out_txn_ref == "ST-000001"
        assert result.in_txn_ref == "ST-000002"
        assert await _share_balance(tid, m_from) == Decimal("7500.00")
        assert await _share_balance(tid, m_to) == Decimal("3000.00")

        record = await _get(tid, pending.id)
        assert record.status is ShareTransferStatus.POSTED
        assert record.created_by == maker
        assert record.approved_by == checker
        assert record.decided_at is not None
        assert record.out_transaction_id is not None
        assert record.in_transaction_id is not None
        assert record.version == 2

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
        # share history exact for BOTH sides (falsifiable: post the
        # transfer as one netted transaction and the transferor
        # reconstructs 10,000.00 while the transferee reconstructs
        # 500.00).
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

        # Audit + operator event + FM11: ONE member notice per member,
        # BOTH sides, in the same transaction (remove either enqueue in
        # approve_share_transfer and these counts fail).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'share_transfer.posted'",
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
        for member_id in (m_from, m_to):
            assert (
                await count(
                    tid,
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'share_transfer.member_notice' "
                    "AND payload->>'notify_member_id' = :mid",
                    mid=str(member_id),
                )
                == 1
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM1 — maker can never be checker (server guard + the 0040 SoD CHECK)
# ---------------------------------------------------------------------------


def test_fm1_maker_cannot_check_own_transfer() -> None:
    """Approving or rejecting one's OWN request is a 409 with zero
    money side effects and the row stays pending. Falsifiable: drop the
    server-side maker<>checker guard and the approval posts."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "100.00")

        with pytest.raises(ConflictError, match="segregation of duties"):
            await _approve(tid, maker, pending.id)
        with pytest.raises(ConflictError, match="segregation of duties"):
            await _reject(tid, maker, pending.id, pending.version)

        assert await _money_counts(tid) == (0, 0, 0, 0)
        record = await _get(tid, pending.id)
        assert record.status is ShareTransferStatus.PENDING
        assert record.approved_by is None
        assert await _share_balance(tid, m_from) == Decimal("1000.00")

    asyncio.run(run())


def test_fm1_db_check_refuses_maker_as_checker_via_direct_sql() -> None:
    """The collusion-resistant backstop: the 0040 ck_share_transfers_sod
    CHECK makes maker = approver unrepresentable even through direct
    SQL on the app role — both the one-shot UPDATE fill and a fresh
    INSERT. Falsifiable: drop the CHECK and both probes land."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "100.00")

        # The UPDATE probe: filling approved_by from NULL is the one
        # write the trigger permits — the SoD CHECK still refuses the
        # maker's own id.
        with pytest.raises(DBAPIError, match="sod"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE share_transfers SET approved_by = created_by "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending.id)},
                )
        # The INSERT probe: a fabricated self-checked pending row.
        with pytest.raises(DBAPIError, match="sod"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO share_transfers "
                        "(tenant_id, from_member_id, to_member_id, amount, status, "
                        " created_by, approved_by, from_balance_at_request) "
                        "VALUES (CAST(:tid AS uuid), CAST(:f AS uuid), CAST(:t AS uuid), "
                        "10, 'pending', CAST(:u AS uuid), CAST(:u AS uuid), 10)"
                    ),
                    {"tid": str(tid), "f": str(m_from), "t": str(m_to), "u": str(maker)},
                )
        record = await _get(tid, pending.id)
        assert record.status is ShareTransferStatus.PENDING
        assert record.approved_by is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — assurance roles can never be checker (!47 B2)
# ---------------------------------------------------------------------------


def test_fm2_assurance_role_checker_is_refused() -> None:
    """Audit independence: the Auditor reviews the transfer trail and
    can never act inside it — a 409 at the SERVICE layer (defence in
    depth beneath the RBAC matrix, which already denies members:approve
    to the Auditor). Falsifiable: drop the ASSURANCE_ROLES exclusion in
    the shared sod guard and the service call posts."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        auditor, _ = await add_user(tid, "Auditor")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "100.00")

        with pytest.raises(ConflictError, match="assurance"):
            await _approve(tid, auditor, pending.id)
        with pytest.raises(ConflictError, match="assurance"):
            await _reject(tid, auditor, pending.id, pending.version)

        assert await _money_counts(tid) == (0, 0, 0, 0)
        assert (await _get(tid, pending.id)).status is ShareTransferStatus.PENDING

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — snapshot drift between the phases: 409 posting NOTHING
# ---------------------------------------------------------------------------


def test_fm3_approval_drift_409s_posting_nothing() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")

        # (a) Transferor-balance drift: an out-of-band 0.01 credit (a
        # dividend) between the phases — approval must bind to the
        # persisted snapshot (1000.00), see 1000.01 and refuse.
        pending = await _request(tid, maker, m_from, m_to, "100.00")
        await _bump_share_balance(tid, m_from, "0.01")
        with pytest.raises(ConflictError, match="drifted"):
            await _approve(tid, checker, pending.id)
        assert await _money_counts(tid) == (0, 0, 0, 0)
        assert (await _get(tid, pending.id)).status is ShareTransferStatus.PENDING
        await _bump_share_balance(tid, m_from, "-0.01")

        # (b) Transferor status drift: dormant members may not move
        # equity out (the P13.13 capability map, re-checked under the
        # member lock at approval).
        await _set_status(tid, m_from, "dormant")
        with pytest.raises(ConflictError, match="active members may transfer"):
            await _approve(tid, checker, pending.id)
        assert await _money_counts(tid) == (0, 0, 0, 0)
        await _set_status(tid, m_from, "active")

        # (c) Transferee status drift: the recipient exited between the
        # phases — the recipient-ACTIVE check re-runs under the lock.
        await _set_status(tid, m_to, "exited")
        with pytest.raises(ConflictError, match="active member"):
            await _approve(tid, checker, pending.id)
        assert await _money_counts(tid) == (0, 0, 0, 0)
        await _set_status(tid, m_to, "active")

        # With every drift healed the SAME pending row approves cleanly
        # (hand-computed: 1000 - 100 = 900; 0 + 100 = 100).
        result = await _approve(tid, checker, pending.id)
        assert result.from_balance_after == Decimal("900.00")
        assert result.to_balance_after == Decimal("100.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — opposing concurrent approvals: no deadlock, exactly ONE effect
# ---------------------------------------------------------------------------


def test_fm4_opposing_concurrent_approvals_do_not_deadlock_exactly_one_posts() -> None:
    """EXIT criterion: A->B racing B->A. Both member locks are taken in
    global member-id order, so the two approvals SERIALISE; lock in
    (from, to) order instead and Postgres kills one side with a
    deadlock error after deadlock_timeout (falsifiable). The serialised
    loser then sees the winner's balance effect as snapshot drift and
    409s cleanly — EXACTLY ONE posted transfer, proven by side-effect
    ROW COUNTS (hand-computed below), never a deadlock, never a
    double-post, and the loser's row stays pending (reject + re-request
    is the documented remedy)."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_a = await seed_member(tid, name="Alpha", shares="1000.00")
        m_b = await seed_member(tid, name="Beta", shares="1000.00")

        p_ab = await _request(tid, maker, m_a, m_b, "100.00")
        p_ba = await _request(tid, maker, m_b, m_a, "250.00")

        results = await asyncio.wait_for(
            asyncio.gather(
                _approve(tid, checker, p_ab.id),
                _approve(tid, checker, p_ba.id),
                return_exceptions=True,
            ),
            timeout=30,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], ConflictError)
        # The loser is a clean snapshot-drift 409 — NOT a deadlock.
        assert "drifted" in str(failures[0])

        # Exactly-one-effect by ROW COUNTS: 1 posted row, 2 ST-
        # transactions, 1 posted audit, 2 member notices.
        assert await _money_counts(tid) == (1, 2, 1, 2)
        # Hand-computed balances for WHICHEVER side won: either
        # A->B 100 (A 900, B 1100) or B->A 250 (A 1250, B 750) — and
        # conservation holds regardless: A + B = 2000.00.
        bal_a = await _share_balance(tid, m_a)
        bal_b = await _share_balance(tid, m_b)
        assert (bal_a, bal_b) in {
            (Decimal("900.00"), Decimal("1100.00")),
            (Decimal("1250.00"), Decimal("750.00")),
        }
        # The loser's row is still pending and can be rejected afresh.
        statuses = {(await _get(tid, p.id)).status for p in (p_ab, p_ba)}
        assert statuses == {ShareTransferStatus.POSTED, ShareTransferStatus.PENDING}

    asyncio.run(run())


def test_fm5_concurrent_double_spend_posts_exactly_one() -> None:
    """Two pending transfers spending the SAME 100.00 (both snapshots
    read 100.00 at request — no money had moved). Concurrent approvals:
    the winner posts 60.00 leaving 40.00; the loser re-verifies 40.00
    against its 100.00 snapshot under the account lock and 409s.
    Falsifiable: verify against the live balance instead of the
    snapshot and both land (overdraft: 100 - 120 = -20)."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_a = await seed_member(tid, name="Spender", shares="100.00")
        m_b = await seed_member(tid, name="First", shares="0")
        m_c = await seed_member(tid, name="Second", shares="0")

        p1 = await _request(tid, maker, m_a, m_b, "60.00")
        p2 = await _request(tid, maker, m_a, m_c, "60.00")

        results = await asyncio.wait_for(
            asyncio.gather(
                _approve(tid, checker, p1.id),
                _approve(tid, checker, p2.id),
                return_exceptions=True,
            ),
            timeout=30,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(failures) == 1
        assert isinstance(failures[0], ConflictError)
        # Side effects prove exactly-once: one posted transfer, 40.00
        # left (hand-computed: 100 - 60), never -20.
        assert await _money_counts(tid) == (1, 2, 1, 2)
        assert await _share_balance(tid, m_a) == Decimal("40.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — population rules at request time, under the locks
# ---------------------------------------------------------------------------


def test_fm6_request_population_rules_are_enforced_under_the_locks() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
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
                await _request(tid, maker, m_active, target, "100.00")

        # Transferor must be strictly ACTIVE (the withdrawal rule: an
        # arrears member may not move equity out from under their debt;
        # exited members cannot transact; dormant equity moves only
        # after a reactivating deposit — P13.13).
        for source in (m_exited, m_arrears, m_dormant):
            with pytest.raises(ConflictError, match="active members may transfer"):
                await _request(tid, maker, source, m_active, "100.00")

        # Self-transfer is refused (and DB-unrepresentable by CHECK).
        with pytest.raises(InvalidInputError, match="same member"):
            await _request(tid, maker, m_active, m_active, "100.00")

        # Amount above holdings is refused, least-disclosure (no
        # figures echoed — asserted on the message).
        with pytest.raises(ConflictError, match="insufficient share balance") as excinfo:
            await _request(tid, maker, m_active, m_second, "1000.01")
        assert "1000" not in str(excinfo.value)

        # NOTHING was created by any refusal: zero rows of any status.
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 0
        assert await _money_counts(tid) == (0, 0, 0, 0)

    asyncio.run(run())


def test_fm6_self_transfer_rows_are_unrepresentable_in_the_database() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        mid = await seed_member(tid, name="Solo", shares="100.00")
        with pytest.raises(DBAPIError, match="distinct_members"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO share_transfers "
                        "(tenant_id, from_member_id, to_member_id, amount, status, "
                        " created_by, from_balance_at_request) "
                        "VALUES (CAST(:tid AS uuid), CAST(:m AS uuid), CAST(:m AS uuid), "
                        "10, 'pending', CAST(:u AS uuid), 100)"
                    ),
                    {"tid": str(tid), "m": str(mid), "u": str(maker)},
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — kill-switch atomicity: abort mid-approval, ZERO partial state
# ---------------------------------------------------------------------------


def test_fm7_kill_switch_leaves_zero_partial_approval_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash after the postings and both balance updates, before commit
    (the audit writer raises) -> ZERO partial state: the pending row is
    UNTOUCHED (still pending, version 1, no decision fill), no ST-
    postings, no balance drift, no audit, no notices. Falsifiable: post
    the ledger legs in their own transaction and the orphaned ST-
    postings survive."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "400.00")

        real_record_audit = dividends_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "share_transfer.posted":
                raise _KillSwitchError()
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(dividends_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await _approve(tid, checker, pending.id)

        # ZERO partial state, by side-effect counts.
        assert await _money_counts(tid) == (0, 0, 0, 0)
        record = await _get(tid, pending.id)
        assert record.status is ShareTransferStatus.PENDING
        assert record.approved_by is None
        assert record.version == 1
        assert await _share_balance(tid, m_from) == Decimal("1000.00")
        assert await _share_balance(tid, m_to) == Decimal("0.00")

        # The same approval commits cleanly afterwards (hand-computed:
        # 1000 - 400 = 600; 0 + 400 = 400).
        result = await _approve(tid, checker, pending.id)
        assert result.from_balance_after == Decimal("600.00")
        assert await _share_balance(tid, m_to) == Decimal("400.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Rejection — version-pinned checker decision with mandatory rationale
# ---------------------------------------------------------------------------


def test_rejection_is_version_pinned_terminal_and_frees_nothing_money() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "100.00")

        # Stale version -> 409, row untouched.
        with pytest.raises(ConflictError, match="stale version"):
            await _reject(tid, checker, pending.id, pending.version + 1)
        assert (await _get(tid, pending.id)).status is ShareTransferStatus.PENDING

        # Blank rationale -> refused (the !52 F2 posture).
        with pytest.raises(InvalidInputError, match="reason"):
            await _reject(tid, checker, pending.id, pending.version, reason="   ")

        rejected = await _reject(
            tid, checker, pending.id, pending.version, reason="wrong recipient"
        )
        assert rejected.status is ShareTransferStatus.REJECTED
        assert rejected.approved_by == checker
        assert rejected.decided_at is not None
        assert rejected.version == 2
        # No money moved; the audit row carries the rationale.
        assert await _money_counts(tid) == (0, 0, 0, 0)
        assert await _share_balance(tid, m_from) == Decimal("1000.00")
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'share_transfer.rejected' "
                "AND after->>'reason' = 'wrong recipient'",
            )
            == 1
        )

        # Terminal: approving or re-rejecting the rejected row is a 409
        # (the status-machine gatekeeper).
        with pytest.raises(ConflictError, match="cannot move"):
            await _approve(tid, checker, pending.id)
        with pytest.raises(ConflictError, match="cannot move"):
            await _reject(tid, checker, pending.id, rejected.version)

    asyncio.run(run())


def test_transfer_status_machine_is_the_single_gatekeeper() -> None:
    legal = {
        (ShareTransferStatus.PENDING, ShareTransferStatus.POSTED),
        (ShareTransferStatus.PENDING, ShareTransferStatus.REJECTED),
    }
    for current in ShareTransferStatus:
        for target in ShareTransferStatus:
            if (current, target) in legal:
                share_transfer_transition(current, target)
            else:
                with pytest.raises(ConflictError, match="cannot move"):
                    share_transfer_transition(current, target)


# ---------------------------------------------------------------------------
# FM10 — the 0040 write-once/status-machine trigger (direct SQL)
# ---------------------------------------------------------------------------


def test_fm10_write_once_trigger_pins_history_via_direct_sql() -> None:
    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "100.00")
        await _approve(tid, checker, pending.id)

        async def probe(sql: str, match: str) -> None:
            with pytest.raises(DBAPIError, match=match):
                async with tenant_session(factory(), tid) as session:
                    await session.execute(text(sql), {"id": str(pending.id)})

        # Un-posting a posted transfer is unrepresentable.
        await probe(
            "UPDATE share_transfers SET status = 'pending' WHERE id = CAST(:id AS uuid)",
            "cannot move",
        )
        # Pinned money column.
        await probe(
            "UPDATE share_transfers SET amount = amount + 1 WHERE id = CAST(:id AS uuid)",
            "write-once",
        )
        # Second fill of a one-shot decision column.
        await probe(
            "UPDATE share_transfers SET decided_at = now() + interval '1 day' "
            "WHERE id = CAST(:id AS uuid)",
            "write-once",
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — cross-tenant probes (issue #17): the explicit predicate fence
# ---------------------------------------------------------------------------


def test_fm9_foreign_tenant_argument_touches_nothing() -> None:
    """Session AS the rows' own tenant (RLS satisfied), foreign
    tenant_id argument — only the explicit predicates refuse."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        foreign = uuid.uuid4()

        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid) as session:
                await request_share_transfer(
                    session, foreign, maker, m_from, to_member_id=m_to, amount=Decimal("10.00")
                )
        assert await count(tid, "SELECT count(*) FROM share_transfers") == 0

        pending = await _request(tid, maker, m_from, m_to, "10.00")
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid) as session:
                await approve_share_transfer(session, foreign, checker, pending.id)
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid) as session:
                await reject_share_transfer(
                    session, foreign, checker, pending.id, version=1, reason="x"
                )
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid) as session:
                await get_share_transfer(session, foreign, pending.id)
        async with tenant_session(factory(), tid) as session:
            page = await list_share_transfers(session, foreign, cursor=None, limit=50)
        assert page.items == []
        assert (await _get(tid, pending.id)).status is ShareTransferStatus.PENDING
        assert await _share_balance(tid, m_from) == Decimal("1000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Register (m) — pending-first band keyset (the 0038 pattern)
# ---------------------------------------------------------------------------


def test_register_walks_pending_first_then_history_with_stable_keyset() -> None:
    """Order oracle hand-built: pending rows first (newest first), then
    terminal rows (newest first) — the checker's job order. The keyset
    walk at limit=1 reproduces the full page byte-for-byte (no skips,
    no duplicates in a static register)."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        checker, _ = await add_user(tid, "System Admin")
        m_a = await seed_member(tid, name="A", shares="10000.00")
        m_b = await seed_member(tid, name="B", shares="10000.00")

        # Chronology: t1 posted, t2 rejected, t3 pending, t4 pending.
        t1 = await _request(tid, maker, m_a, m_b, "10.00")
        await _approve(tid, checker, t1.id)
        t2 = await _request(tid, maker, m_a, m_b, "20.00")
        await _reject(tid, checker, t2.id, t2.version, reason="dup")
        t3 = await _request(tid, maker, m_a, m_b, "30.00")
        t4 = await _request(tid, maker, m_b, m_a, "40.00")

        # Expected order: pending newest-first (t4, t3), then terminal
        # newest-first (t2, t1).
        expected = [t4.id, t3.id, t2.id, t1.id]

        async with tenant_session(factory(), tid) as session:
            page = await list_share_transfers(session, tid, cursor=None, limit=50)
        assert [r.id for r in page.items] == expected
        assert page.next_cursor is None
        # Amounts render as verbatim decimal strings on the API model;
        # here the service returns exact decimals (hand-computed).
        assert [str(r.amount) for r in page.items] == ["40.00", "30.00", "20.00", "10.00"]

        # Keyset walk at limit=1 reproduces the same order exactly.
        walked: list[uuid.UUID] = []
        cursor: str | None = None
        for _ in range(10):
            async with tenant_session(factory(), tid) as session:
                page = await list_share_transfers(session, tid, cursor=cursor, limit=1)
            walked.extend(r.id for r in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert walked == expected

        # Forged cursors are refused loudly.
        for forged in ("2|2020-01-01T00:00:00+00:00|" + str(uuid.uuid4()), "1|not-a-ts|x", "zz"):
            with pytest.raises(InvalidInputError, match="cursor"):
                async with tenant_session(factory(), tid) as session:
                    await list_share_transfers(session, tid, cursor=forged, limit=1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# API contract legs: permissions, SoD over HTTP, idempotency by row
# counts, boundary validation
# ---------------------------------------------------------------------------


def test_share_transfer_endpoints_full_workflow_over_http() -> None:
    async def run() -> None:
        tid, _, maker_token = await seed_actor()
        _, checker_token = await add_user(tid, "System Admin")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        maker_headers = {"authorization": f"Bearer {maker_token}"}
        checker_headers = {"authorization": f"Bearer {checker_token}"}

        async with api_client() as client:
            # Phase 1 (maker): 201 PENDING, no balances disclosed, no
            # snapshot serialised (least disclosure).
            created = await client.post(
                f"/members/{m_from}/share-transfers",
                json={"to_member_id": str(m_to), "amount": "250.00"},
                headers=maker_headers,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["status"] == "pending"
            assert body["amount"] == "250.00"
            assert "from_balance_at_request" not in body
            assert body["out_transaction_id"] is None
            transfer_id = body["id"]

            # Unknown/money fields are a 422 (extra="forbid").
            forged = await client.post(
                f"/members/{m_from}/share-transfers",
                json={"to_member_id": str(m_to), "amount": "1.00", "rate_pct": "9"},
                headers=maker_headers,
            )
            assert forged.status_code == 422

            # SoD over HTTP: the MAKER's approval is a 409.
            self_approve = await client.post(
                f"/share-transfers/{transfer_id}/approval",
                json={},
                headers=maker_headers,
            )
            assert self_approve.status_code == 409

            # Approval body refuses smuggled money fields (v1.1 rule 1).
            smuggled = await client.post(
                f"/share-transfers/{transfer_id}/approval",
                json={"amount": "999.00"},
                headers=checker_headers,
            )
            assert smuggled.status_code == 422

            # FM8 — idempotent approval by side-effect ROW COUNTS: the
            # same Idempotency-Key replays the stored response and the
            # database holds exactly ONE transfer's worth of effects.
            idem_key = str(uuid.uuid4())
            first = await client.post(
                f"/share-transfers/{transfer_id}/approval",
                json={},
                headers={**checker_headers, "Idempotency-Key": idem_key},
            )
            assert first.status_code == 201, first.text
            replay = await client.post(
                f"/share-transfers/{transfer_id}/approval",
                json={},
                headers={**checker_headers, "Idempotency-Key": idem_key},
            )
            assert replay.status_code == 201
            assert replay.json() == first.json()
            assert first.json()["from_balance_after"] == "750.00"
            assert first.json()["out_txn_ref"].startswith("ST-")

            # Register + by-id reads under members:view.
            register = await client.get("/share-transfers", headers=checker_headers)
            assert register.status_code == 200
            items = register.json()["items"]
            assert [r["id"] for r in items] == [transfer_id]
            assert items[0]["status"] == "posted"
            assert items[0]["amount"] == "250.00"
            by_id = await client.get(f"/share-transfers/{transfer_id}", headers=checker_headers)
            assert by_id.status_code == 200
            assert by_id.json()["approved_by"] is not None

            # Contract cap: limit above the hard max 100 is a 422.
            capped = await client.get(
                "/share-transfers", params={"limit": 101}, headers=checker_headers
            )
            assert capped.status_code == 422

        # Exactly ONE transfer's side effects despite the replay
        # (hand-computed: 1000 - 250 = 750).
        assert await _money_counts(tid) == (1, 2, 1, 2)
        assert await _share_balance(tid, m_from) == Decimal("750.00")

    asyncio.run(run())


def test_share_transfer_reads_are_denied_without_members_view() -> None:
    """Deny-by-default gate: the Credit Committee holds NO members:view
    (P4 matrix), so the register and by-id reads are 403 — and holds NO
    members:approve, so the workflow mutations are 403 too."""

    async def run() -> None:
        tid, maker, _ = await seed_actor()
        _, cc_token = await add_user(tid, "Credit Committee")
        m_from = await seed_member(tid, name="From", shares="1000.00")
        m_to = await seed_member(tid, name="To", shares="0")
        pending = await _request(tid, maker, m_from, m_to, "10.00")
        cc_headers = {"authorization": f"Bearer {cc_token}"}

        async with api_client() as client:
            assert (await client.get("/share-transfers", headers=cc_headers)).status_code == 403
            assert (
                await client.get(f"/share-transfers/{pending.id}", headers=cc_headers)
            ).status_code == 403
            assert (
                await client.post(
                    f"/members/{m_from}/share-transfers",
                    json={"to_member_id": str(m_to), "amount": "1.00"},
                    headers=cc_headers,
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/share-transfers/{pending.id}/approval", json={}, headers=cc_headers
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/share-transfers/{pending.id}/rejection",
                    json={"version": 1, "reason": "x"},
                    headers=cc_headers,
                )
            ).status_code == 403

    asyncio.run(run())
