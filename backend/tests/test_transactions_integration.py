"""Integration tests for P11 transactions & deposit interest (real Postgres, RLS)."""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import deposit_interest as interest_service
from genesis.application import transactions as txn_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import post_deposit
from genesis.application.rbac import seed_permissions
from genesis.domain.deposits import next_quarter, previous_quarter, quarter_of
from genesis.domain.ledger import Channel, Side, TxnType
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_member(
    tid: uuid.UUID, *, deposit: str = "0", shares: str = "0", status: str = "active"
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Txn Member', :status)"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}", "status": status},
        )
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": deposit},
        )
        await session.execute(
            text(
                "INSERT INTO share_accounts (id, tenant_id, member_id, balance) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": shares},
        )
    return mid


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, str]:
    """Tenant + seeded permissions + bearer token for one role (P10 pattern)."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    token = issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )
    return tid, token


async def _configure_rate(tid: uuid.UUID, rate: str) -> None:
    """Seed the tenant's deposit-interest rate (tenant_settings, migration 0009)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenant_settings (tenant_id, deposit_interest_annual_rate_pct) "
                "VALUES (CAST(:tid AS uuid), :rate) "
                "ON CONFLICT (tenant_id) DO UPDATE "
                "SET deposit_interest_annual_rate_pct = EXCLUDED.deposit_interest_annual_rate_pct"
            ),
            {"tid": str(tid), "rate": rate},
        )


async def _seed_accrual_row(
    tid: uuid.UUID, mid: uuid.UUID, period_start: date, period_end: date, rate: str
) -> None:
    """Insert a bare accrual claim row (crash-recovery / mismatch fixtures)."""
    async with tenant_session(factory(), tid) as session:
        account_id = (
            await session.execute(
                text("SELECT id FROM deposit_accounts WHERE member_id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO deposit_interest_accruals "
                "(id, tenant_id, account_id, period_start, period_end, annual_rate_pct, amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:a AS uuid), "
                ":ps, :pe, :rate, 0)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "a": str(account_id),
                "ps": period_start,
                "pe": period_end,
                "rate": rate,
            },
        )


async def _deposit_at(tid: uuid.UUID, mid: uuid.UUID, amount: str, occurred_at: datetime) -> None:
    """Post an in-period deposit leg via the P7 contract (ADB fixtures).

    transactions is append-only (gate 1.5), so past-period history is
    written the same way production wrote it: a real posting with an
    explicit occurred_at. The account balance itself is seeded by the
    caller (the fixture's current balance already includes the amount).
    """
    async with tenant_session(factory(), tid) as session:
        await post_deposit(
            session, tid, mid, Decimal(amount), Channel.BANK, occurred_at=occurred_at
        )


async def _balance(tid: uuid.UUID, mid: uuid.UUID, table: str = "deposit_accounts") -> Decimal:
    async with tenant_session(factory(), tid) as session:
        val = (
            await session.execute(
                # Table name from test code, never user input.
                text(
                    f"SELECT balance FROM {table} "  # noqa: S608
                    "WHERE member_id = CAST(:m AS uuid)"
                ),
                {"m": str(mid)},
            )
        ).scalar_one()
    return Decimal(str(val))


async def _interest_counts(tid: uuid.UUID) -> tuple[int, int, int, int]:
    """Side-effect counts proving idempotency: (txns, audits, outbox, accruals)."""
    async with tenant_session(factory(), tid) as session:
        txns = (
            await session.execute(
                text("SELECT count(*) FROM transactions WHERE type = 'interest_posting'")
            )
        ).scalar_one()
        audits = (
            await session.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'deposit_account.interest'")
            )
        ).scalar_one()
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'ledger.deposit_interest_posted'"
                )
            )
        ).scalar_one()
        accruals = (
            await session.execute(text("SELECT count(*) FROM deposit_interest_accruals"))
        ).scalar_one()
    return int(txns), int(audits), int(events), int(accruals)


def test_deposit_credits_account_posts_ledger_audit_outbox() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            result = await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("500.00"), channel=Channel.MPESA
            )
        assert result.txn_ref.startswith("MP-")
        assert result.balance_after == Decimal("500.00")
        assert await _balance(tid, mid) == Decimal("500.00")
        async with tenant_session(factory(), tid) as session:
            sides = (
                await session.execute(
                    text("SELECT side, COALESCE(SUM(amount), 0) FROM ledger_entries GROUP BY side")
                )
            ).all()
            audit = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'deposit_account.credit'")
                )
            ).scalar_one()
            events = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'ledger.deposit_posted'"
                    )
                )
            ).scalar_one()
        totals = {str(r[0]): Decimal(str(r[1])) for r in sides}
        assert totals["debit"] == totals["credit"] == Decimal("500.00")
        assert int(audit) == 1
        assert int(events) == 1

    asyncio.run(run())


def test_share_topup_credits_share_account() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            result = await txn_service.record_share_topup(
                session, tid, None, mid, amount=Decimal("250.00"), channel=Channel.BANK
            )
        assert result.txn_ref.startswith("SH-")
        assert await _balance(tid, mid, table="share_accounts") == Decimal("250.00")
        assert await _balance(tid, mid) == Decimal("0.00")  # deposit account untouched

    asyncio.run(run())


def test_concurrent_withdrawals_never_overdraw() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_member(tid, deposit="5000.00")

        async def withdraw() -> bool:
            try:
                async with tenant_session(factory(), tid) as session:
                    await txn_service.record_withdrawal(
                        session, tid, None, mid, amount=Decimal("1000.00"), channel=Channel.BANK
                    )
            except ConflictError:
                return False
            return True

        results = await asyncio.gather(*(withdraw() for _ in range(10)))
        assert sum(results) == 5  # exactly the affordable withdrawals land
        assert await _balance(tid, mid) == Decimal("0.00")
        async with tenant_session(factory(), tid) as session:
            refs = (
                (
                    await session.execute(
                        text("SELECT txn_ref FROM transactions WHERE type = 'withdrawal'")
                    )
                )
                .scalars()
                .all()
            )
        assert len(refs) == 5
        assert len(set(refs)) == 5  # race-safe WD- refs, no duplicates

    asyncio.run(run())


def test_withdrawal_respects_live_pledges_and_blocks_exited_members() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        guarantor = await _seed_member(tid, deposit="1000.00")
        borrower = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), :amount)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "g": str(guarantor),
                    "b": str(borrower),
                    "amount": "600.00",
                },
            )
        # 1000 balance - 600 pledged = 400 withdrawable: 500 must fail.
        with pytest.raises(ConflictError, match="insufficient available funds"):
            async with tenant_session(factory(), tid) as session:
                await txn_service.record_withdrawal(
                    session, tid, None, guarantor, amount=Decimal("500.00"), channel=Channel.BANK
                )
        async with tenant_session(factory(), tid) as session:
            ok = await txn_service.record_withdrawal(
                session, tid, None, guarantor, amount=Decimal("400.00"), channel=Channel.BANK
            )
        assert ok.txn_ref.startswith("WD-")
        assert await _balance(tid, guarantor) == Decimal("600.00")

        exited = await _seed_member(tid, deposit="100.00", status="exited")
        with pytest.raises(ConflictError, match="exited"):
            async with tenant_session(factory(), tid) as session:
                await txn_service.record_deposit(
                    session, tid, None, exited, amount=Decimal("10.00"), channel=Channel.MPESA
                )

    asyncio.run(run())


def test_interest_job_posts_once_and_rerun_changes_nothing() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        m1 = await _seed_member(tid, deposit="10000.00")
        m2 = await _seed_member(tid, deposit="0.01")  # interest rounds below one cent
        await _seed_member(tid, deposit="0.00")  # zero balance: scanned, claims a row
        period = previous_quarter(datetime.now(UTC).date())
        scope = partial(tenant_session, factory(), tid)

        first = await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=period, annual_rate_pct=Decimal("8.00"), batch_size=1
        )
        assert first.scanned == 3  # every account, not only positive balances
        assert first.posted == 1
        assert first.skipped_existing == 0
        assert first.rate_mismatches == 0
        assert first.total_interest == Decimal("200.00")  # hand-computed: 10000*8/400
        assert first.batches == 3  # batch_size=1 proves the batching path
        assert await _balance(tid, m1) == Decimal("10200.00")
        assert await _balance(tid, m2) == Decimal("0.01")
        before = await _interest_counts(tid)
        assert before[3] == 3  # one accrual claim per scanned account

        second = await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=period, annual_rate_pct=Decimal("8.00"), batch_size=1
        )
        # Second-pass finding 16: already-accrued accounts are excluded
        # by the scan's NOT EXISTS anti-join, so a fully accrued re-run
        # scans (and locks) nothing at all.
        assert second.scanned == 0
        assert second.batches == 0
        assert second.posted == 0
        assert second.skipped_existing == 0
        assert second.rate_mismatches == 0
        assert second.total_interest == Decimal("0.00")
        # Idempotency proven by side effects: ledger, audit, outbox and
        # accrual-row counts are all unchanged, as are the balances.
        assert await _interest_counts(tid) == before
        assert await _balance(tid, m1) == Decimal("10200.00")

    asyncio.run(run())


def test_interest_accrues_on_period_end_balance_not_current() -> None:
    """Review findings 1 + 14: the basis is ledger-reconstructed, in-period.

    A deposit made after the period ended earns nothing for that
    period (the deposit-then-collect-then-withdraw exploit); an account
    emptied after the period ended still earns what its in-period
    balances deserved. Both fixtures held a constant balance across the
    quarter, so the average daily balance equals that constant.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        # Seeded directly (no ledger movement after the period) — the
        # 10000.00 counts as the quarter-end balance.
        m_old = await _seed_member(tid, deposit="10000.00")
        # Funded only AFTER the period ended: today's deposit posts a
        # member.deposits credit occurring after period.end.
        m_late = await _seed_member(tid, deposit="0.00")
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, m_late, amount=Decimal("50000.00"), channel=Channel.BANK
            )
        # Emptied AFTER the period ended: the withdrawal is subtracted
        # back out of today's balance when reconstructing the basis.
        m_emptied = await _seed_member(tid, deposit="4000.00")
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_withdrawal(
                session, tid, None, m_emptied, amount=Decimal("4000.00"), channel=Channel.BANK
            )
        assert await _balance(tid, m_emptied) == Decimal("0.00")

        period = previous_quarter(datetime.now(UTC).date())
        scope = partial(tenant_session, factory(), tid)
        result = await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=period, annual_rate_pct=Decimal("8.00")
        )
        assert result.scanned == 3
        assert result.posted == 2
        # Hand-computed: 10000*8/400 = 200.00 and 4000*8/400 = 80.00;
        # the 50000 deposited after period end earns exactly nothing.
        assert result.total_interest == Decimal("280.00")
        assert await _balance(tid, m_old) == Decimal("10200.00")
        assert await _balance(tid, m_late) == Decimal("50000.00")  # not 51000.00
        assert await _balance(tid, m_emptied) == Decimal("80.00")

        # The INT- posting is stamped at the very end of the period
        # (23:59:59.999999 UTC on the last day), inside the period.
        async with tenant_session(factory(), tid) as session:
            occurred = (
                await session.execute(
                    text(
                        "SELECT occurred_at FROM transactions "
                        "WHERE type = 'interest_posting' ORDER BY occurred_at DESC LIMIT 1"
                    )
                )
            ).scalar_one()
        assert occurred.date() == period.end
        assert occurred.hour == 23
        assert occurred.minute == 59

    asyncio.run(run())


def test_interest_basis_is_average_daily_balance() -> None:
    """Second-pass finding 14: the basis is the ADB, never a snapshot.

    Q2-2026 (Apr 1 - Jun 30, 91 days) at 8% p.a., hand-computed:

      * 91000.00 deposited on the LAST day of the quarter with zero
        prior balance -> ADB = 91000/91 = 1000.00 -> interest
        1000*8/400 = 20.00 — not the 1820.00 a period-end snapshot
        would have paid (deposit-on-the-last-day exploit).
      * 9100.00 deposited 10 days before quarter end -> the balance is
        9100.00 for 10 days -> ADB = 9100*10/91 = 1000.00 -> 20.00.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        period = quarter_of(date(2026, 4, 1))  # Q2-2026: Apr 1 - Jun 30
        assert (period.end - period.start).days + 1 == 91

        m_last_day = await _seed_member(tid, deposit="91000.00")
        await _deposit_at(
            tid, m_last_day, "91000.00", datetime.combine(period.end, time(12, 0), tzinfo=UTC)
        )

        m_ten_days = await _seed_member(tid, deposit="9100.00")
        await _deposit_at(
            tid,
            m_ten_days,
            "9100.00",
            datetime.combine(period.end - timedelta(days=9), time(12, 0), tzinfo=UTC),
        )

        scope = partial(tenant_session, factory(), tid)
        result = await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=period, annual_rate_pct=Decimal("8.00")
        )
        assert result.posted == 2
        assert result.total_interest == Decimal("40.00")  # 20.00 + 20.00
        assert await _balance(tid, m_last_day) == Decimal("91020.00")
        assert await _balance(tid, m_ten_days) == Decimal("9120.00")

        # The audit rows carry the ADB basis (1000.00 for both).
        async with tenant_session(factory(), tid) as session:
            bases = (
                (
                    await session.execute(
                        text(
                            "SELECT after->>'basis' FROM audit_log "
                            "WHERE action = 'deposit_account.interest'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert sorted(str(b) for b in bases) == ["1000.00", "1000.00"]

    asyncio.run(run())


def test_resolve_run_parameters_config_and_strict_period_order() -> None:
    """Review finding 2: rate from tenant config; periods accrue in order."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        m1 = await _seed_member(tid, deposit="1000.00")
        await _seed_member(tid, deposit="2000.00")
        today = datetime.now(UTC).date()
        recent = previous_quarter(today)
        older = quarter_of(recent.start - timedelta(days=1))  # two quarters back
        scope = partial(tenant_session, factory(), tid)

        # Unconfigured tenant: hard 409, never a default rate.
        with pytest.raises(ConflictError, match="not configured"):
            async with tenant_session(factory(), tid) as session:
                await interest_service.resolve_run_parameters(session, tid)

        await _configure_rate(tid, "8.00")
        # No accruals yet -> the most recent completed quarter.
        async with tenant_session(factory(), tid) as session:
            period, rate = await interest_service.resolve_run_parameters(session, tid)
        assert (period, rate) == (recent, Decimal("8.00"))

        # A partially accrued older quarter (crash mid-run) is finished
        # first: m1 has its row, the second account does not.
        await _seed_accrual_row(tid, m1, older.start, older.end, "8.00")
        async with tenant_session(factory(), tid) as session:
            period, _ = await interest_service.resolve_run_parameters(session, tid)
        assert period == older
        await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=older, annual_rate_pct=Decimal("8.00")
        )
        # Older quarter fully claimed -> advance to the next in order,
        # which here is the most recent completed quarter.
        async with tenant_session(factory(), tid) as session:
            period, _ = await interest_service.resolve_run_parameters(session, tid)
        assert period == next_quarter(older)
        assert period == recent
        await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=recent, annual_rate_pct=Decimal("8.00")
        )
        # Fully caught up -> the same period again (idempotent no-op).
        async with tenant_session(factory(), tid) as session:
            period, _ = await interest_service.resolve_run_parameters(session, tid)
        assert period == recent

    asyncio.run(run())


def test_interest_job_endpoint_config_permissions_and_no_caller_rate() -> None:
    """Review finding 2 at the API: no caller rate, no backdating, RBAC.

    The endpoint accepts only batch_size; annual_rate_pct/as_of are
    rejected with 422 (extra=forbid), an unconfigured tenant is a hard
    409, and the P4 matrix gates the run at transactions:edit — the
    Accountant runs it, the Teller (transactions:create only) cannot.
    """

    async def run() -> None:
        tid, token = await _seed_actor("Accountant")
        headers = {"authorization": f"Bearer {token}"}
        mid = await _seed_member(tid, deposit="10000.00")
        period = previous_quarter(datetime.now(UTC).date())
        async with api_client() as client:
            rate_attempt = await client.post(
                "/jobs/deposit-interest",
                json={"annual_rate_pct": "99.00"},
                headers=headers,
            )
            assert rate_attempt.status_code == 422  # caller-supplied rate refused
            backdate_attempt = await client.post(
                "/jobs/deposit-interest",
                json={"as_of": "2020-01-01"},
                headers=headers,
            )
            assert backdate_attempt.status_code == 422  # arbitrary period refused

            unconfigured = await client.post("/jobs/deposit-interest", json={}, headers=headers)
            assert unconfigured.status_code == 409  # no silent default rate

            await _configure_rate(tid, "8.00")
            res = await client.post("/jobs/deposit-interest", json={}, headers=headers)
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["period_start"] == period.start.isoformat()
            assert body["period_end"] == period.end.isoformat()
            assert body["annual_rate_pct"] == "8.00"  # from tenant_settings
            assert body["posted"] == 1
            assert body["total_interest"] == "200.00"  # hand-computed: 10000*8/400
            assert body["rate_mismatches"] == 0
        assert await _balance(tid, mid) == Decimal("10200.00")

        _, teller_token = await _seed_actor("Teller")
        async with api_client() as client:
            denied = await client.post(
                "/jobs/deposit-interest",
                json={},
                headers={"authorization": f"Bearer {teller_token}"},
            )
        assert denied.status_code == 403  # transactions:edit required (P4 matrix)

    asyncio.run(run())


def test_interest_rate_mismatch_is_surfaced_not_reposted() -> None:
    """Findings 13 + 16: rate drift is surfaced by the per-run aggregate check.

    The already-accrued account is no longer even scanned (NOT EXISTS
    anti-join), yet the run still reports that its stored rate differs
    from the configured one.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_member(tid, deposit="10000.00")
        period = previous_quarter(datetime.now(UTC).date())
        await _seed_accrual_row(tid, mid, period.start, period.end, "5.00")
        scope = partial(tenant_session, factory(), tid)
        result = await interest_service.run_deposit_interest_for_tenant(
            scope, tid, period=period, annual_rate_pct=Decimal("8.00")
        )
        # Idempotency wins — nothing is re-posted, nothing is scanned —
        # but the mismatch is visible instead of silently invisible.
        assert result.posted == 0
        assert result.scanned == 0
        assert result.skipped_existing == 0
        assert result.rate_mismatches == 1
        assert await _balance(tid, mid) == Decimal("10000.00")

    asyncio.run(run())


def test_ledger_listing_keyset_filters_and_tenant_isolation() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid = await _seed_member(tid, deposit="1000.00")
        for amt in ("100.00", "200.00", "300.00"):
            async with tenant_session(factory(), tid) as session:
                await txn_service.record_deposit(
                    session, tid, None, mid, amount=Decimal(amt), channel=Channel.BANK
                )
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_withdrawal(
                session, tid, None, mid, amount=Decimal("50.00"), channel=Channel.BANK
            )

        async with tenant_session(factory(), tid) as session:
            page1, cursor = await txn_service.list_transactions(session, tid, limit=2)
            assert len(page1) == 2
            assert cursor is not None
            assert page1[0].txn_type is TxnType.WITHDRAWAL  # newest first
            assert page1[0].direction is Side.DEBIT
            page2, cursor2 = await txn_service.list_transactions(
                session, tid, cursor=cursor, limit=2
            )
            assert len(page2) == 2
            assert cursor2 is None
            ids = {t.id for t in page1} | {t.id for t in page2}
            assert len(ids) == 4  # keyset pages never overlap

            deposits, _ = await txn_service.list_transactions(
                session, tid, txn_type=TxnType.DEPOSIT
            )
            assert len(deposits) == 3
            assert all(t.direction is Side.CREDIT for t in deposits)
            debits, _ = await txn_service.list_transactions(session, tid, direction=Side.DEBIT)
            assert [t.txn_type for t in debits] == [TxnType.WITHDRAWAL]
            by_ref, _ = await txn_service.list_transactions(session, tid, ref=page1[0].txn_ref)
            assert len(by_ref) == 1

        other_tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), other_tid) as session:
            foreign, _ = await txn_service.list_transactions(session, other_tid)
        assert foreign == []  # RLS + explicit predicate: zero rows cross-tenant

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Issue #30 R3 — posting-actor attribution on the ledger read model (FM-B/D)
# ---------------------------------------------------------------------------


def test_ledger_read_model_carries_posting_actor_least_disclosure() -> None:
    """FM-D: created_by rides the transactions read contract, gated by
    transactions:view exactly like every other field (the P4 matrix —
    no extra grant discloses it), as the bare staff UUID ONLY.

    Falsifiable: dropping the TransactionOut field (or the _txn_out /
    _row_to_txn mapping) fails the equality assertions; leaking the
    actor's name or email into the payload fails the disclosure
    assertions. FM-B leg: a system posting (no actor) reads back null —
    attribution is never invented.
    """

    async def run() -> None:
        email = unique_email()
        tid, role_id = await seed_user(email)
        async with tenant_session(factory(), tid) as session:
            await seed_permissions(session, tid)
            actor_row = (
                await session.execute(
                    text("SELECT id, full_name FROM users WHERE email = :email"),
                    {"email": email},
                )
            ).first()
        assert actor_row is not None
        actor_id = uuid.UUID(str(actor_row[0]))
        actor_name = str(actor_row[1])
        token = issue_access_token(AuthContext(user_id=actor_id, tenant_id=tid, role_id=role_id))
        headers = {"authorization": f"Bearer {token}"}
        mid = await _seed_member(tid, deposit="1000.00")

        # An attributed posting through the API (the teller flow).
        async with api_client() as client:
            posted = await client.post(
                f"/members/{mid}/deposits",
                # external_ref required on external channels (#35 item 6).
                json={"amount": "250.00", "channel": "mpesa", "external_ref": "SGH3KLM9QT"},
                headers=headers,
            )
            assert posted.status_code == 201, posted.text

        # A system posting: no acting principal, recorded as absent.
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("50.00"), channel=Channel.BANK
            )

        async with api_client() as client:
            listing = await client.get("/transactions", headers=headers)
            assert listing.status_code == 200, listing.text
            items = listing.json()["items"]
            # Channel keys the two fixtures apart (order-independent).
            teller_rows = [i for i in items if i["channel"] == "mpesa"]
            system_rows = [i for i in items if i["channel"] == "bank"]
            assert teller_rows and system_rows
            assert teller_rows[0]["created_by"] == str(actor_id)  # recorded at INSERT
            assert system_rows[0]["created_by"] is None  # FM-B: never invented
            assert "created_by" in system_rows[0]  # present even when null
            # Least disclosure (gate 1.6): the UUID only — the actor's
            # name/email appear nowhere in the money read model.
            assert actor_name not in listing.text
            assert email not in listing.text
            assert "full_name" not in listing.text

    asyncio.run(run())
