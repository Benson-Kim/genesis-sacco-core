"""Integration tests for P12.5 accounting periods (issue #12, real Postgres, RLS).

Falsifiability (§4): the closed-period tests fail if assert_open_period
is removed from ledger._post (the posting would then either land or
surface the 0012 trigger's raw DB error instead of the clean 409); the
trigger test fails if the 0012 backstop trigger is dropped; the
concurrent-close test fails if the ON CONFLICT rowcount claim is
replaced with SELECT-then-INSERT. All idempotency/atomicity assertions
use side-effect row counts, never return values.
"""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import accounting_periods as periods_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import post_deposit, post_reversal
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError, InvalidInputError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
    """Tenant + seeded permissions + bearer token; returns (tid, user_id, token)."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    uid = uuid.UUID(str(user_id))
    token = issue_access_token(AuthContext(user_id=uid, tenant_id=tid, role_id=role_id))
    return tid, uid, token


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "idempotency-key": uuid.uuid4().hex}


async def _seed_member(tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Period Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
    return mid


def _last_month() -> tuple[int, int]:
    """The most recent fully elapsed calendar month."""
    first_of_this_month = datetime.now(UTC).date().replace(day=1)
    prev = first_of_this_month - timedelta(days=1)
    return prev.year, prev.month


def _month_before(year: int, month: int) -> tuple[int, int]:
    d = date(year, month, 1) - timedelta(days=1)
    return d.year, d.month


async def _close(tid: uuid.UUID, uid: uuid.UUID, year: int, month: int) -> None:
    async with tenant_session(factory(), tid) as session:
        await periods_service.close_period(session, tid, uid, year=year, month=month)


async def _posting_side_effects(tid: uuid.UUID) -> tuple[int, int, int, int]:
    """(transactions, ledger legs, ledger.post audits, deposit outbox events)."""
    async with tenant_session(factory(), tid) as session:
        txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        legs = (await session.execute(text("SELECT count(*) FROM ledger_entries"))).scalar_one()
        audits = (
            await session.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'ledger.post'")
            )
        ).scalar_one()
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE event_type = 'ledger.deposit_posted'"
                )
            )
        ).scalar_one()
    return int(txns), int(legs), int(audits), int(events)


# ---------------------------------------------------------------------------
# Close action: API, RBAC, validation
# ---------------------------------------------------------------------------


def test_close_period_api_writes_row_audit_and_outbox() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        year, month = _last_month()
        async with api_client() as client:
            res = await client.post(
                "/accounting-periods/close",
                json={"year": year, "month": month},
                headers=_headers(token),
            )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["status"] == "closed"
        assert body["period_start"] == date(year, month, 1).isoformat()
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(text("SELECT count(*) FROM accounting_periods"))
            ).scalar_one()
            audits = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'accounting_period.close'")
                )
            ).scalar_one()
            events = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'accounting.period_closed'"
                    )
                )
            ).scalar_one()
        assert (int(rows), int(audits), int(events)) == (1, 1, 1)

        # Closing the same month again is a 409, not a second row.
        async with api_client() as client:
            again = await client.post(
                "/accounting-periods/close",
                json={"year": year, "month": month},
                headers=_headers(token),
            )
        assert again.status_code == 409
        assert again.json()["category"] == "conflict"

    asyncio.run(run())


def test_close_period_rbac_posting_roles_cannot_close() -> None:
    """Teller and Accountant post money (transactions:create/edit) but must
    not be able to freeze the periods they post into; only the
    transactions:approve holders may (P4 matrix)."""

    async def run() -> None:
        year, month = _last_month()
        for role, expected in (
            ("Teller", 403),
            ("Accountant", 403),
            ("Branch Manager", 201),
        ):
            _, _, token = await _seed_actor(role)
            async with api_client() as client:
                res = await client.post(
                    "/accounting-periods/close",
                    json={"year": year, "month": month},
                    headers=_headers(token),
                )
            assert res.status_code == expected, f"{role}: {res.text}"

    asyncio.run(run())


def test_close_body_rejects_unknown_and_invalid_fields() -> None:
    async def run() -> None:
        _, _, token = await _seed_actor()
        year, month = _last_month()
        async with api_client() as client:
            extra = await client.post(
                "/accounting-periods/close",
                json={"year": year, "month": month, "closed_at": "2020-01-01"},
                headers=_headers(token),
            )
            bad_month = await client.post(
                "/accounting-periods/close",
                json={"year": year, "month": 13},
                headers=_headers(token),
            )
        assert extra.status_code == 422
        assert bad_month.status_code == 422

    asyncio.run(run())


def test_close_current_or_future_month_is_refused() -> None:
    """Closing the running month would refuse every live posting — a
    self-inflicted outage, so it is a 409."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        today = datetime.now(UTC).date()
        with pytest.raises(ConflictError, match="fully elapsed"):
            await _close(tid, uid, today.year, today.month)
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        with pytest.raises(ConflictError, match="fully elapsed"):
            await _close(tid, uid, nxt.year, nxt.month)

    asyncio.run(run())


def test_concurrent_close_collapses_to_exactly_one_row() -> None:
    """The UNIQUE (tenant_id, period_start) claim is atomic: 5 concurrent
    closes produce exactly one closed row and exactly one success."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        year, month = _last_month()

        async def close_once() -> bool:
            try:
                await _close(tid, uid, year, month)
            except ConflictError:
                return False
            return True

        results = await asyncio.gather(*(close_once() for _ in range(5)))
        assert sum(results) == 1, "exactly one close must win"
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(text("SELECT count(*) FROM accounting_periods"))
            ).scalar_one()
            audits = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'accounting_period.close'")
                )
            ).scalar_one()
        assert (int(rows), int(audits)) == (1, 1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Posting guard: every posting funnels through ledger._post
# ---------------------------------------------------------------------------


def test_posting_into_closed_period_409_with_zero_side_effects() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)
        year, month = _last_month()
        await _close(tid, uid, year, month)

        inside = datetime.combine(date(year, month, 15), time(12, 0), tzinfo=UTC)
        with pytest.raises(ConflictError, match="closed accounting period"):
            async with tenant_session(factory(), tid) as session:
                await post_deposit(
                    session, tid, mid, Decimal("100.00"), Channel.BANK, occurred_at=inside
                )
        # Nothing was written: no txn, no legs, no audit, no outbox.
        assert await _posting_side_effects(tid) == (0, 0, 0, 0)

    asyncio.run(run())


def test_posting_boundary_dates_around_closed_period() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)
        year, month = _last_month()
        await _close(tid, uid, year, month)
        _, period_end = periods_service.month_bounds(year, month)

        # Last second INSIDE the closed month -> refused.
        with pytest.raises(ConflictError, match="closed accounting period"):
            async with tenant_session(factory(), tid) as session:
                await post_deposit(
                    session,
                    tid,
                    mid,
                    Decimal("10.00"),
                    Channel.BANK,
                    occurred_at=datetime.combine(period_end, time(23, 59, 59), tzinfo=UTC),
                )
        # First instant AFTER the closed month -> posts.
        async with tenant_session(factory(), tid) as session:
            posted = await post_deposit(
                session,
                tid,
                mid,
                Decimal("10.00"),
                Channel.BANK,
                occurred_at=datetime.combine(
                    period_end + timedelta(days=1), time(0, 0), tzinfo=UTC
                ),
            )
        assert posted.txn_ref
        assert (await _posting_side_effects(tid))[0] == 1

    asyncio.run(run())


def test_future_dated_posting_rejected() -> None:
    async def run() -> None:
        tid, _, _ = await _seed_actor()
        mid = await _seed_member(tid)
        with pytest.raises(InvalidInputError, match="future"):
            async with tenant_session(factory(), tid) as session:
                await post_deposit(
                    session,
                    tid,
                    mid,
                    Decimal("100.00"),
                    Channel.BANK,
                    occurred_at=datetime.now(UTC) + timedelta(minutes=10),
                )
        assert await _posting_side_effects(tid) == (0, 0, 0, 0)

    asyncio.run(run())


def test_reversal_of_closed_period_txn_posts_in_current_period() -> None:
    """Corrections are never backdated: reversing a transaction whose
    period has since closed posts the mirror entry in the CURRENT
    period (append-only ledger; issue #12 adversarial case)."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)
        year, month = _last_month()
        inside = datetime.combine(date(year, month, 15), time(12, 0), tzinfo=UTC)
        async with tenant_session(factory(), tid) as session:
            original = await post_deposit(
                session, tid, mid, Decimal("100.00"), Channel.BANK, occurred_at=inside
            )
        await _close(tid, uid, year, month)

        async with tenant_session(factory(), tid) as session:
            reversal = await post_reversal(session, tid, original.txn_id)
        async with tenant_session(factory(), tid) as session:
            occurred = (
                await session.execute(
                    text("SELECT occurred_at FROM transactions WHERE id = CAST(:id AS uuid)"),
                    {"id": str(reversal.txn_id)},
                )
            ).scalar_one()
        _, period_end = periods_service.month_bounds(year, month)
        assert occurred.date() > period_end

    asyncio.run(run())


def test_db_backstop_trigger_blocks_raw_insert() -> None:
    """Gate 1.5: even raw SQL bypassing the application guard cannot land
    a transaction inside a closed period (0012 trigger). Falsifiable:
    drop the trigger and this test fails."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)
        year, month = _last_month()
        await _close(tid, uid, year, month)
        inside = datetime.combine(date(year, month, 10), time(9, 0), tzinfo=UTC)

        with pytest.raises(DBAPIError, match="closed accounting period"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO transactions "
                        "(id, tenant_id, txn_ref, member_id, type, amount, channel, "
                        " occurred_at) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
                        "CAST(:mid AS uuid), 'deposit', '50.00', 'bank', :ts)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "ref": f"MP-{uuid.uuid4().hex[:6].upper()}",
                        "mid": str(mid),
                        "ts": inside,
                    },
                )

    asyncio.run(run())


def test_db_backstop_trigger_serializes_with_concurrent_close() -> None:
    """Gate 1.4 (0014; external Codex review, re-derived): a raw-SQL
    writer must wait for an in-flight close_period() before the trigger
    reads accounting_periods. close_period holds the EXCLUSIVE per-
    tenant advisory lock until its transaction commits; the 0014
    trigger takes the SHARED lock, so the raw insert blocks, then sees
    the committed closed row and is refused. Falsifiable: with the 0012
    trigger body (no shared lock) the insert reads the not-yet-
    committed period table, finds nothing closed, and commits into the
    closing month — this test fails."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)
        year, month = _last_month()
        inside = datetime.combine(date(year, month, 10), time(9, 0), tzinfo=UTC)
        close_started = asyncio.Event()

        async def close_holding_lock() -> None:
            async with tenant_session(factory(), tid) as session:
                await periods_service.close_period(session, tid, uid, year=year, month=month)
                # Closed row written, advisory lock still held: give the
                # raw writer time to start and block on the shared lock.
                close_started.set()
                await asyncio.sleep(0.3)

        async def raw_insert_after_close_starts() -> bool:
            await close_started.wait()
            try:
                async with tenant_session(factory(), tid) as session:
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(id, tenant_id, txn_ref, member_id, type, amount, channel, "
                            " occurred_at) "
                            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
                            "CAST(:mid AS uuid), 'deposit', '50.00', 'bank', :ts)"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "tid": str(tid),
                            "ref": f"MP-{uuid.uuid4().hex[:6].upper()}",
                            "mid": str(mid),
                            "ts": inside,
                        },
                    )
            except DBAPIError:
                return False
            return True

        _, inserted = await asyncio.gather(close_holding_lock(), raw_insert_after_close_starts())
        assert inserted is False, "raw insert must observe the concurrently closed period"
        assert await _posting_side_effects(tid) == (0, 0, 0, 0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_periods_listing_keyset_pagination() -> None:
    async def run() -> None:
        tid, uid, token = await _seed_actor()
        y1, m1 = _last_month()
        y2, m2 = _month_before(y1, m1)
        await _close(tid, uid, y2, m2)
        await _close(tid, uid, y1, m1)

        async with api_client() as client:
            first = await client.get(
                "/accounting-periods", params={"limit": 1}, headers=_headers(token)
            )
            assert first.status_code == 200, first.text
            page1 = first.json()
            assert len(page1["items"]) == 1
            # Newest first: the later month leads.
            assert page1["items"][0]["period_start"] == date(y1, m1, 1).isoformat()
            assert page1["next_cursor"]
            second = await client.get(
                "/accounting-periods",
                params={"limit": 1, "cursor": page1["next_cursor"]},
                headers=_headers(token),
            )
            assert second.status_code == 200
            page2 = second.json()
            assert page2["items"][0]["period_start"] == date(y2, m2, 1).isoformat()
            assert page2["next_cursor"] is None
            bad = await client.get(
                "/accounting-periods",
                params={"cursor": "not-a-date"},
                headers=_headers(token),
            )
            assert bad.status_code == 400

    asyncio.run(run())
