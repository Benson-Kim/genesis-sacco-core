"""P13.17(b) / DSA-2+DSA-5 — closed-period rollups (FM2 suite).

Named failure mode FM2: rollup divergence — a drifted rollup that
reports a balanced book is worse than no rollup. The MERGE GATE is the
equality-with-full-scan property: on seeded multi-month history, the
trial balance and the statement opening balances served from rollups
equal the full-scan figures to the cent, with the PRE-EXISTING
reconstruction statements (reports.TRIAL_BALANCE_SQL, the P13 opening
scan) as the oracle. Falsifiability, per test:

  * equality property — fails if the rollup writer, the rolled/live
    partition, or the anchored opening ever diverges from the full
    scan by a cent;
  * write-once + late-insert probes — fail when the 0028 triggers are
    removed (direct SQL through the app role would then succeed);
  * fabricated-row tests — fail if an untrusted (unrolled) row leaks
    into a read, or if the backfill silently adopts a divergent row
    instead of 409ing;
  * re-run no-op — fails if a repeated backfill writes anything
    (side-effect counts, v1.1 rule 8).
"""

import asyncio
import csv
import io
import os
import random
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import drain_export_queue, seed_actor
from genesis.application import accounting_periods as periods_service
from genesis.application import period_rollups as rollups_service
from genesis.application.reports import TRIAL_BALANCE_SQL
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _last_months(n: int) -> list[tuple[date, date]]:
    """(start, end) of the last n fully elapsed months, oldest first."""
    today = datetime.now(UTC).date()
    months: list[tuple[date, date]] = []
    end = today.replace(day=1) - timedelta(days=1)
    for _ in range(n):
        months.append((end.replace(day=1), end))
        end = end.replace(day=1) - timedelta(days=1)
    months.reverse()
    return months


async def _seed_member(tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Rollup Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
    return mid


async def _post_txn(
    tid: uuid.UUID,
    mid: uuid.UUID | None,
    *,
    txn_type: str,
    amount: str,
    occurred_at: datetime,
    debit_account: str,
    credit_account: str,
    channel: str = "bank",
) -> None:
    """One balanced transaction + its two legs (0004/0014 triggers)."""
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, member_id, txn_ref, type, amount, channel, occurred_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                ":ref, :type, :amount, :channel, :ts)"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "mid": str(mid) if mid else None,
                "ref": f"TB-{uuid.uuid4().hex[:8].upper()}",
                "type": txn_type,
                "amount": amount,
                "channel": channel,
                "ts": occurred_at,
            },
        )
        for account, side in ((debit_account, "debit"), (credit_account, "credit")):
            await session.execute(
                text(
                    "INSERT INTO ledger_entries "
                    "(id, tenant_id, transaction_id, account, side, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:txn AS uuid), "
                    ":account, :side, :amount)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "txn": str(txn_id),
                    "account": account,
                    "side": side,
                    "amount": amount,
                },
            )


async def _seed_history(tid: uuid.UUID) -> tuple[uuid.UUID, tuple[date, date], tuple[date, date]]:
    """Hand-computable three-month scenario.

    p1 (two months back): deposit 15000.00 mpesa -> DR cash.mpesa /
    CR member.deposits. p2 (last month): withdrawal 2000.00 bank ->
    DR member.deposits / CR cash.bank. Current month: share top-up
    5000.00 bank -> DR cash.bank / CR member.shares. All for member M.

    Hand-computed trial balance (any as-of after the top-up):
      cash.bank        DR 5000.00  CR 2000.00
      cash.mpesa       DR 15000.00 CR 0.00
      member.deposits  DR 2000.00  CR 15000.00
      member.shares    DR 0.00     CR 5000.00
      TOTAL            22000.00 / 22000.00
    Hand-computed member balances: end of p1 = +15000.00 (deposit is a
    member credit); end of p2 = 15000 - 2000 = +13000.00; opening at
    the 1st of the current month = 13000.00.
    """
    (p1_start, p1_end), (p2_start, p2_end) = _last_months(2)
    mid = await _seed_member(tid)
    await _post_txn(
        tid,
        mid,
        txn_type="deposit",
        amount="15000.00",
        occurred_at=datetime.combine(p1_start + timedelta(days=10), time(12), tzinfo=UTC),
        debit_account="cash.mpesa",
        credit_account="member.deposits",
        channel="mpesa",
    )
    await _post_txn(
        tid,
        mid,
        txn_type="withdrawal",
        amount="2000.00",
        occurred_at=datetime.combine(p2_start + timedelta(days=5), time(12), tzinfo=UTC),
        debit_account="member.deposits",
        credit_account="cash.bank",
    )
    await _post_txn(
        tid,
        mid,
        txn_type="share_topup",
        amount="5000.00",
        occurred_at=datetime.now(UTC),
        debit_account="cash.bank",
        credit_account="member.shares",
    )
    return mid, (p1_start, p1_end), (p2_start, p2_end)


async def _close(tid: uuid.UUID, uid: uuid.UUID, month_end: date) -> None:
    async with tenant_session(factory(), tid) as session:
        await periods_service.close_period(
            session, tid, uid, year=month_end.year, month=month_end.month
        )


async def _render_csv(token: str, tid: uuid.UUID, body: dict[str, object]) -> list[list[str]]:
    headers = {"authorization": f"Bearer {token}"}
    async with api_client() as client:
        created = await client.post("/exports", json=body, headers=headers)
        assert created.status_code == 201, created.text
        export = created.json()
        summary = await drain_export_queue(tid)
        assert summary.failed == 0
        fetched = await client.get(f"/exports/{export['id']}", headers=headers)
        assert fetched.status_code == 200
        artifact = fetched.json()["artifact"]
        assert artifact is not None
        download = await client.get(artifact["csv_download"], headers=headers)
        assert download.status_code == 200
    return list(csv.reader(io.StringIO(download.content.decode("utf-8"))))


async def _full_scan_accounts(tid: uuid.UUID) -> dict[str, tuple[Decimal, Decimal]]:
    """The pre-existing reconstruction as oracle (TRIAL_BALANCE_SQL)."""
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(TRIAL_BALANCE_SQL),
                {"tid": str(tid), "as_of": datetime.now(UTC)},
            )
        ).all()
    return {str(r[0]): (Decimal(str(r[1])), Decimal(str(r[2]))) for r in rows}


async def _rollup_served_accounts(tid: uuid.UUID) -> dict[str, tuple[Decimal, Decimal]]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(rollups_service.TRIAL_BALANCE_ROLLUP_SQL),
                {"tid": str(tid), "as_of": datetime.now(UTC)},
            )
        ).all()
    return {str(r[0]): (Decimal(str(r[1])), Decimal(str(r[2]))) for r in rows}


async def _count(tid: uuid.UUID, sql: str) -> int:
    async with tenant_session(factory(), tid) as session:
        value = (await session.execute(text(sql))).scalar_one()
    return int(value)


def test_close_writes_rollups_and_trial_balance_equals_full_scan() -> None:
    async def run() -> None:
        tid, uid, token = await seed_actor()
        _mid, (p1_start, p1_end), (p2_start, p2_end) = await _seed_history(tid)

        before = await _render_csv(token, tid, {"report": "trial_balance"})
        # Hand oracle (see _seed_history) on the pre-close render.
        assert before[1] == ["cash.bank", "5000.00", "2000.00"]
        assert before[2] == ["cash.mpesa", "15000.00", "0.00"]
        assert before[3] == ["member.deposits", "2000.00", "15000.00"]
        assert before[4] == ["member.shares", "0.00", "5000.00"]
        assert before[5] == ["TOTAL", "22000.00", "22000.00"]

        await _close(tid, uid, p1_end)
        await _close(tid, uid, p2_end)

        # Rollup rows equal the hand-computed per-period activity.
        async with tenant_session(factory(), tid) as session:
            accounts = (
                await session.execute(
                    text(
                        "SELECT period_start, account, debits, credits "
                        "FROM account_period_balances ORDER BY period_start, account"
                    )
                )
            ).all()
            members = (
                await session.execute(
                    text(
                        "SELECT period_start, balance FROM member_period_balances "
                        "ORDER BY period_start"
                    )
                )
            ).all()
        assert [(r[0], str(r[1]), str(r[2]), str(r[3])) for r in accounts] == [
            (p1_start, "cash.mpesa", "15000.00", "0.00"),
            (p1_start, "member.deposits", "0.00", "15000.00"),
            (p2_start, "cash.bank", "0.00", "2000.00"),
            (p2_start, "member.deposits", "2000.00", "0.00"),
        ]
        assert [(r[0], str(r[1])) for r in members] == [
            (p1_start, "15000.00"),
            (p2_start, "13000.00"),
        ]

        # FM2 equality gate 1: the rendered document is byte-identical
        # before and after the periods were rolled.
        after = await _render_csv(token, tid, {"report": "trial_balance"})
        assert after == before, "rollup-served trial balance diverged from the full scan"

        # FM2 equality gate 2: rollup SQL vs the full-scan oracle,
        # numerically to the cent.
        assert await _rollup_served_accounts(tid) == await _full_scan_accounts(tid)

        # One rollup audit row per period; markers set (write-once).
        audits = await _count(
            tid,
            "SELECT count(*) FROM audit_log WHERE action = 'accounting_period.rollup'",
        )
        assert audits == 2
        marked = await _count(
            tid,
            "SELECT count(*) FROM accounting_periods WHERE rollup_at IS NOT NULL",
        )
        assert marked == 2

    asyncio.run(run())


def test_statement_opening_from_anchor_equals_full_scan() -> None:
    async def run() -> None:
        tid, uid, token = await seed_actor()
        mid, _, (_, p2_end) = await _seed_history(tid)
        window_from = datetime.now(UTC).date().replace(day=1)
        body: dict[str, object] = {
            "report": "member_statement",
            "filters": {"member_id": str(mid), "date_from": window_from.isoformat()},
        }
        before = await _render_csv(token, tid, body)
        # Hand oracle: opening 13000.00 (15000 - 2000), the only
        # in-window row is the 5000.00 share top-up (a member CREDIT)
        # -> running balance 18000.00.
        assert before[1][6] == "5000.00"
        assert before[1][7] == "18000.00"
        assert len(before) == 2

        await _close(tid, uid, p2_end.replace(day=1) - timedelta(days=1))  # p1
        await _close(tid, uid, p2_end)  # p2 -> the opening anchor

        after = await _render_csv(token, tid, body)
        assert after == before, "anchored opening diverged from the full scan"

    asyncio.run(run())


def test_property_random_history_rollups_equal_full_scan() -> None:
    """The FM2 merge gate on richer, seeded-RNG history: random
    postings across four months, two of them closed OUT OF ORDER, must
    yield rollup-served figures equal to the full scan to the cent —
    for the trial balance AND the anchored member opening at month and
    MID-month window starts. Oracle = the pre-existing reconstruction
    (TRIAL_BALANCE_SQL / the P13 full opening scan via
    member_movement_sql without an anchor)."""

    async def run() -> None:
        rng = random.Random(20260802)  # noqa: S311 - deterministic test data, not crypto
        tid, uid, _ = await seed_actor()
        mid = await _seed_member(tid)
        months = _last_months(3)
        spans = [*months, (datetime.now(UTC).date().replace(day=1), None)]
        moves = (
            ("deposit", "cash.mpesa", "member.deposits"),
            ("withdrawal", "member.deposits", "cash.bank"),
            ("share_topup", "cash.bank", "member.shares"),
        )
        for start, end in spans:
            for _ in range(rng.randint(2, 4)):
                txn_type, debit_account, credit_account = rng.choice(moves)
                amount = Decimal(rng.randint(1, 500_000)) / Decimal(100)
                day_cap = (end - start).days if end else 0
                offset = rng.randint(0, day_cap) if day_cap else 0
                occurred = datetime.combine(start + timedelta(days=offset), time(9), tzinfo=UTC)
                if end is None:
                    occurred = datetime.now(UTC)
                await _post_txn(
                    tid,
                    mid,
                    txn_type=txn_type,
                    amount=str(amount),
                    occurred_at=occurred,
                    debit_account=debit_account,
                    credit_account=credit_account,
                )

        # Close month 3 (most recent elapsed) FIRST, then month 1 —
        # out-of-order closes must stay exact.
        await _close(tid, uid, months[2][1])
        await _close(tid, uid, months[0][1])

        assert await _rollup_served_accounts(tid) == await _full_scan_accounts(tid)

        # Anchored opening vs full scan at several window starts,
        # including a MID-month start (anchor = an earlier month).
        as_of = datetime.now(UTC)
        probes = [
            months[1][0],
            months[2][0],
            months[2][0] + timedelta(days=14),
            datetime.now(UTC).date().replace(day=1),
        ]
        async with tenant_session(factory(), tid) as session:
            for window_from in probes:
                anchored = await rollups_service.member_balance_before(
                    session, tid, mid, before=window_from, as_of=as_of
                )
                full_rows = (
                    await session.execute(
                        text(rollups_service.member_movement_sql(with_from=True, with_start=False)),
                        {
                            "tid": str(tid),
                            "mid": str(mid),
                            "as_of": as_of,
                            "d_from": datetime.combine(window_from, time.min, tzinfo=UTC),
                        },
                    )
                ).all()
                full = rollups_service.signed_member_movement(list(full_rows))
                assert anchored == full, f"opening diverged at window start {window_from}"

    asyncio.run(run())


def test_trial_balance_historical_as_of_equals_full_scan() -> None:
    """B1 remediation (FM2): a rolled period participates as a rollup
    ONLY when fully covered by :as_of — (:as_of AT TIME ZONE
    'UTC')::date > period_end, applied SYMMETRICALLY in the rolled CTE
    join and the live CTE's NOT EXISTS; otherwise its postings fall
    through to the live scan, which caps at :as_of exactly.

    Oracle = reports.TRIAL_BALANCE_SQL (the pre-existing full scan) at
    the SAME :as_of, plus hand-computed pins so no probe is captured
    from the implementation under test. Falsifiable, per probe, with
    the as_of qualifier removed (the pre-fix statement):

      * (i)   as_of INSIDE rolled p2, before its mid-month activity —
              the unqualified rolled CTE adds p1's AND p2's full
              totals while the live CTE excludes their days entirely:
              both sides overstate symmetrically and STILL BALANCE
              (the FM2 'drifted rollup reporting a balanced book'
              class); the oracle here is p1's totals only;
      * (i')  intra-day as_of ON p2's period-end day, before that
              day's 18:00 posting — ALSO fails if the strict > is
              weakened to >= (a partially covered final day means the
              whole period must fall through to the live scan);
      * (ii)  as_of entirely BEFORE every rolled month — the oracle is
              an EMPTY book; the unqualified rolled CTE still returns
              full totals;
      * (iii) as_of exactly at UTC midnight of period_end + 1 — the
              period is fully covered from precisely that instant
              (the writer's d_to_excl bound is exclusive), pinning the
              <=/< boundary semantics: rolled path == full scan to
              the cent.
    """

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        mid = await _seed_member(tid)
        (p1_start, p1_end), (p2_start, p2_end) = _last_months(2)
        # Hand-computable postings: p1 deposit 15000.00 (mpesa); p2
        # withdrawal 2000.00 on day 6 AND a PERIOD-END-DAY deposit
        # 700.00 at 18:00 (the strict-> probe); current month share
        # top-up 5000.00.
        await _post_txn(
            tid,
            mid,
            txn_type="deposit",
            amount="15000.00",
            occurred_at=datetime.combine(p1_start + timedelta(days=10), time(12), tzinfo=UTC),
            debit_account="cash.mpesa",
            credit_account="member.deposits",
            channel="mpesa",
        )
        await _post_txn(
            tid,
            mid,
            txn_type="withdrawal",
            amount="2000.00",
            occurred_at=datetime.combine(p2_start + timedelta(days=5), time(12), tzinfo=UTC),
            debit_account="member.deposits",
            credit_account="cash.bank",
        )
        await _post_txn(
            tid,
            mid,
            txn_type="deposit",
            amount="700.00",
            occurred_at=datetime.combine(p2_end, time(18), tzinfo=UTC),
            debit_account="cash.bank",
            credit_account="member.deposits",
        )
        await _post_txn(
            tid,
            mid,
            txn_type="share_topup",
            amount="5000.00",
            occurred_at=datetime.now(UTC),
            debit_account="cash.bank",
            credit_account="member.shares",
        )
        await _close(tid, uid, p1_end)
        await _close(tid, uid, p2_end)

        async def both(
            as_of: datetime,
        ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, tuple[Decimal, Decimal]]]:
            def fold(rows: list[Any]) -> dict[str, tuple[Decimal, Decimal]]:
                return {str(r[0]): (Decimal(str(r[1])), Decimal(str(r[2]))) for r in rows}

            async with tenant_session(factory(), tid) as session:
                rolled = (
                    await session.execute(
                        text(rollups_service.TRIAL_BALANCE_ROLLUP_SQL),
                        {"tid": str(tid), "as_of": as_of},
                    )
                ).all()
                full = (
                    await session.execute(
                        text(TRIAL_BALANCE_SQL), {"tid": str(tid), "as_of": as_of}
                    )
                ).all()
            return fold(list(rolled)), fold(list(full))

        probes: list[tuple[str, datetime]] = [
            (
                "(i) inside rolled p2",
                datetime.combine(p2_start + timedelta(days=2), time(12), tzinfo=UTC),
            ),
            (
                "(i') intra-day on p2's period-end day",
                datetime.combine(p2_end, time(12), tzinfo=UTC),
            ),
            (
                "(ii) before every rolled month",
                datetime.combine(p1_start - timedelta(days=1), time(12), tzinfo=UTC),
            ),
            (
                "(iii) UTC midnight of p1_end + 1",
                datetime.combine(p1_end + timedelta(days=1), time.min, tzinfo=UTC),
            ),
            (
                "(iii') UTC midnight of p2_end + 1",
                datetime.combine(p2_end + timedelta(days=1), time.min, tzinfo=UTC),
            ),
            ("current instant", datetime.now(UTC)),
        ]
        for label, as_of in probes:
            rolled, full = await both(as_of)
            assert rolled == full, f"trial balance diverged from the full scan at {label}"

        # Hand pins (never captured from the implementation):
        # (i)/(iii): only p1's deposit is <= as_of -> exactly p1's
        # totals; (ii): the empty book.
        p1_totals = {
            "cash.mpesa": (Decimal("15000.00"), Decimal("0.00")),
            "member.deposits": (Decimal("0.00"), Decimal("15000.00")),
        }
        by_label = dict(probes)
        for label in (
            "(i) inside rolled p2",
            "(iii) UTC midnight of p1_end + 1",
        ):
            rolled, _ = await both(by_label[label])
            assert rolled == p1_totals, f"hand oracle mismatch at {label}"
        rolled_empty, _ = await both(by_label["(ii) before every rolled month"])
        assert rolled_empty == {}, "a pre-history as_of must render an empty book"
        # (i') pins the strict >: at noon of p2's period-end day the
        # day-6 withdrawal IS <= as_of but the 18:00 period-end-day
        # deposit is NOT — with >= the rolled CTE would add p2's full
        # totals including it. Hand oracle: p1 deposit + the 2000.00
        # withdrawal only.
        rolled_mid, _ = await both(by_label["(i') intra-day on p2's period-end day"])
        assert rolled_mid == {
            "cash.mpesa": (Decimal("15000.00"), Decimal("0.00")),
            "cash.bank": (Decimal("0.00"), Decimal("2000.00")),
            "member.deposits": (Decimal("2000.00"), Decimal("15000.00")),
        }
        # (iii'): from p2_end+1 midnight, p2 is fully covered.
        rolled_b, _ = await both(by_label["(iii') UTC midnight of p2_end + 1"])
        assert rolled_b == {
            "cash.mpesa": (Decimal("15000.00"), Decimal("0.00")),
            "cash.bank": (Decimal("700.00"), Decimal("2000.00")),
            "member.deposits": (Decimal("2000.00"), Decimal("15700.00")),
        }

    asyncio.run(run())


def test_member_anchor_fully_before_window_and_as_of_cap_safe() -> None:
    """B1 audit of MEMBER_ANCHOR_SQL / member_balance_before (FM2).

    Pins two properties the trial-balance defect class could also hit:

      * anchor constraint — the chosen anchor is the LATEST rolled
        period with period_end < :before, i.e. FULLY before the
        window: a window opening MID-p2 anchors on p1 (never the p2
        row, whose period covers the window start); a window opening
        at the current month start anchors on p2. Falsifiable: drop
        the `p.period_end < :before` predicate (or invert the ORDER
        BY) in MEMBER_ANCHOR_SQL and the mid-p2 probe anchors on p2,
        mis-counting the delta.
      * as_of-cap safety — anchor content can never postdate the
        server-resolved as_of: close_period refuses any month with
        period_end >= today (pinned below with a live probe), so every
        rolled period's content ends strictly before UTC midnight of
        today, while the export as_of is resolved server-side to now()
        at render — the cap can therefore never precede anchor
        content. Pinned anyway: the anchored figure equals the full
        scan at as_of = now() to the cent.

    The mid-p2 probe's delta scan walks days INSIDE the later rolled
    period p2. That is safe by construction: rollups never remove or
    alter transactions rows (the ledger stays append-only; the rollup
    tables are pure additions), so the live delta over those days
    reads exactly what the full-history scan reads.
    """

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        mid, (_, p1_end), (p2_start, p2_end) = await _seed_history(tid)
        await _close(tid, uid, p1_end)
        await _close(tid, uid, p2_end)

        async with tenant_session(factory(), tid) as session:
            mid_window = p2_start + timedelta(days=14)
            anchor_mid = (
                await session.execute(
                    text(rollups_service.MEMBER_ANCHOR_SQL),
                    {"tid": str(tid), "mid": str(mid), "before": mid_window},
                )
            ).first()
            # Hand oracle (_seed_history): end of p1 = +15000.00.
            assert anchor_mid is not None
            assert (str(anchor_mid[0]), anchor_mid[1]) == ("15000.00", p1_end)

            month_start = datetime.now(UTC).date().replace(day=1)
            anchor_now = (
                await session.execute(
                    text(rollups_service.MEMBER_ANCHOR_SQL),
                    {"tid": str(tid), "mid": str(mid), "before": month_start},
                )
            ).first()
            # Hand oracle: end of p2 = 15000 - 2000 = +13000.00.
            assert anchor_now is not None
            assert (str(anchor_now[0]), anchor_now[1]) == ("13000.00", p2_end)

            # Anchored opening across a window starting INSIDE the
            # later rolled period p2 == full scan, to the cent. Hand
            # oracle: 15000 - 2000 (the day-5 withdrawal precedes the
            # day-14 window start) = 13000.00.
            as_of = datetime.now(UTC)
            anchored = await rollups_service.member_balance_before(
                session, tid, mid, before=mid_window, as_of=as_of
            )
            assert anchored == Decimal("13000.00")
            full_rows = (
                await session.execute(
                    text(rollups_service.member_movement_sql(with_from=True, with_start=False)),
                    {
                        "tid": str(tid),
                        "mid": str(mid),
                        "as_of": as_of,
                        "d_from": datetime.combine(mid_window, time.min, tzinfo=UTC),
                    },
                )
            ).all()
            assert anchored == rollups_service.signed_member_movement(list(full_rows))

        # The as_of-cap safety precondition, pinned live: a month that
        # has not fully elapsed can never be closed (so no anchor row
        # can ever cover content past the render instant).
        today = datetime.now(UTC).date()
        with pytest.raises(ConflictError, match="fully elapsed"):
            await _close(tid, uid, today.replace(day=28))

    asyncio.run(run())


def test_rollups_are_write_once_and_late_inserts_fenced() -> None:
    """Insider probes (FM2): direct-SQL UPDATE/DELETE refused by the
    write-once triggers; INSERT after completion refused by the
    late-insert fence; the completeness marker cannot be cleared.
    Falsifiable: drop a 0028 trigger and its probe succeeds."""

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, (p1_start, p1_end), _ = await _seed_history(tid)
        await _close(tid, uid, p1_end)

        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE account_period_balances SET debits = '0.00' "
                        "WHERE tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"tid": str(tid)},
                )
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("DELETE FROM member_period_balances WHERE tenant_id = CAST(:tid AS uuid)"),
                    {"tid": str(tid)},
                )
        with pytest.raises(DBAPIError, match="late inserts are refused"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO account_period_balances "
                        "(id, tenant_id, period_start, account, debits, credits) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, "
                        "'income.fabricated', '1.00', '0.00')"
                    ),
                    {"id": str(uuid.uuid4()), "tid": str(tid), "ps": p1_start},
                )
        with pytest.raises(DBAPIError, match="late inserts are refused"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO member_period_balances "
                        "(id, tenant_id, period_start, member_id, balance) "
                        "SELECT CAST(:id AS uuid), CAST(:tid AS uuid), :ps, id, '9.99' "
                        "FROM members LIMIT 1"
                    ),
                    {"id": str(uuid.uuid4()), "tid": str(tid), "ps": p1_start},
                )
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE accounting_periods SET rollup_at = NULL "
                        "WHERE tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"tid": str(tid)},
                )

    asyncio.run(run())


async def _insert_legacy_closed_period(tid: uuid.UUID, start: date, end: date) -> None:
    """Simulate a period closed BEFORE migration 0028 (no rollups)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO accounting_periods "
                "(id, tenant_id, period_start, period_end, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, :pe, 'closed')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "ps": start, "pe": end},
        )


def test_backfill_rolls_legacy_periods_and_rerun_is_noop() -> None:
    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, (p1_start, p1_end), _ = await _seed_history(tid)
        await _insert_legacy_closed_period(tid, p1_start, p1_end)

        scope = partial(tenant_session, factory(), tid)
        result = await rollups_service.run_rollup_backfill_for_tenant(scope, tid, uid)
        assert result.periods_rolled == 1
        # Hand oracle: p1 holds the 15000.00 deposit -> two account
        # rows + one member balance.
        assert result.accounts_written == 2
        assert result.members_written == 1
        assert await _rollup_served_accounts(tid) == await _full_scan_accounts(tid)

        rows_before = await _count(tid, "SELECT count(*) FROM account_period_balances")
        audits_before = await _count(
            tid,
            "SELECT count(*) FROM audit_log WHERE action = 'accounting_period.rollup'",
        )
        rerun = await rollups_service.run_rollup_backfill_for_tenant(scope, tid, uid)
        assert (rerun.periods_rolled, rerun.accounts_written, rerun.batches) == (0, 0, 0)
        assert await _count(tid, "SELECT count(*) FROM account_period_balances") == rows_before
        audits_after = await _count(
            tid,
            "SELECT count(*) FROM audit_log WHERE action = 'accounting_period.rollup'",
        )
        assert audits_after == audits_before

    asyncio.run(run())


def test_backfill_409s_on_fabricated_divergent_rollup_row() -> None:
    """FM2: a fabricated pre-backfill row is (1) never trusted by any
    read (its period is unrolled) and (2) makes the backfill abort
    LOUDLY instead of adopting or overwriting it."""

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, (p1_start, p1_end), _ = await _seed_history(tid)
        await _insert_legacy_closed_period(tid, p1_start, p1_end)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO account_period_balances "
                    "(id, tenant_id, period_start, account, debits, credits) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, "
                    "'cash.mpesa', '999.00', '0.00')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "ps": p1_start},
            )

        # The unrolled fabricated row is invisible to the trial balance
        # (live scan still serves the period; oracle equality holds).
        assert await _rollup_served_accounts(tid) == await _full_scan_accounts(tid)

        scope = partial(tenant_session, factory(), tid)
        with pytest.raises(ConflictError, match="diverge"):
            await rollups_service.run_rollup_backfill_for_tenant(scope, tid, uid)
        # Nothing adopted, nothing healed: marker still unset, reads
        # still exact.
        marked = await _count(
            tid, "SELECT count(*) FROM accounting_periods WHERE rollup_at IS NOT NULL"
        )
        assert marked == 0
        assert await _rollup_served_accounts(tid) == await _full_scan_accounts(tid)

    asyncio.run(run())


def test_write_period_rollups_marker_precedes_verify() -> None:
    """N3b pin (review !49 + supervisor finding): write_period_rollups
    statement order is inserts (both tables) -> rollup_at marker
    UPDATE -> verify BOTH stored sets. The marker UPDATE is the
    serialisation point — its FOR NO KEY UPDATE on the period row
    waits out every in-flight fenced INSERT's FOR SHARE (0028
    trigger) — so a verify that ran BEFORE it could miss a fabricated
    row that commits while the marker waits: fabricator INSERTs
    (trigger FOR SHARE, held to commit) -> verify passes under READ
    COMMITTED (uncommitted row invisible) -> marker UPDATE blocks on
    the FOR SHARE -> fabricator commits -> marker proceeds -> the
    fabricated row is frozen inside a "complete" period that verify
    never saw. With verify AFTER the marker, the fabricated row is
    committed and visible, and ConflictError rolls back everything
    including the marker. Falsifiable: restore the pre-N3b order
    (verify before marker) and the order assertions below fail.

    This is the statement-order pin (the two-session race itself is
    not deterministically schedulable from the test harness — the
    trigger's conflicting lock mode is pinned deterministically by
    test_late_insert_fence_lock_conflicts_with_marker_update)."""

    class _RecordingSession:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.statements: list[str] = []

        async def execute(self, statement: Any, params: Any = None) -> Any:
            self.statements.append(str(statement))
            return await self._inner.execute(statement, params)

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, (p1_start, p1_end), _ = await _seed_history(tid)
        await _insert_legacy_closed_period(tid, p1_start, p1_end)
        async with tenant_session(factory(), tid) as session:
            period_id = (
                await session.execute(
                    text(
                        "SELECT id FROM accounting_periods "
                        "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps"
                    ),
                    {"tid": str(tid), "ps": p1_start},
                )
            ).scalar_one()
        async with tenant_session(factory(), tid) as session:
            recorder = _RecordingSession(session)
            counts = await rollups_service.write_period_rollups(
                recorder,  # type: ignore[arg-type]
                tid,
                uid,
                period_id=uuid.UUID(str(period_id)),
                period_start=p1_start,
                period_end=p1_end,
                source="backfill",
            )
        # Hand oracle (_seed_history): p1 holds the 15000.00 deposit ->
        # two account rows, one member balance.
        assert (counts.accounts, counts.members) == (2, 1)

        recorded = recorder.statements

        def first_index(fragment: str) -> int:
            return next(i for i, s in enumerate(recorded) if fragment in s)

        account_inserts = [
            i for i, s in enumerate(recorded) if s.startswith("INSERT INTO account_period_balances")
        ]
        member_inserts = [
            i for i, s in enumerate(recorded) if s.startswith("INSERT INTO member_period_balances")
        ]
        marker = first_index("SET rollup_at = now()")
        verify_accounts = first_index(
            "SELECT account, debits, credits FROM account_period_balances"
        )
        verify_members = first_index("SELECT member_id, balance FROM member_period_balances")
        assert account_inserts and member_inserts, "expected rollup INSERTs were not executed"
        assert max(account_inserts) < marker, "account INSERTs must precede the marker UPDATE"
        assert max(member_inserts) < marker, "member INSERTs must precede the marker UPDATE"
        assert marker < verify_accounts, "verify (accounts) must FOLLOW the marker UPDATE"
        assert marker < verify_members, "verify (members) must FOLLOW the marker UPDATE"

    asyncio.run(run())


def test_late_insert_fence_lock_conflicts_with_marker_update() -> None:
    """N3 pin (review !49, corrected lock mode), deterministic via
    NOWAIT — no timing dependence:

      * session A INSERTs a rollup row for an unrolled closed period
        and holds it uncommitted: the 0028 fence's period probe holds
        SELECT .. FOR SHARE on the period row until A ends;
      * session B then probes the period row with FOR KEY SHARE
        NOWAIT — it SUCCEEDS, proving the review's prescribed
        FOR KEY SHARE would NOT have conflicted with a concurrent
        fence holder (nor, symmetrically, would a KEY-SHARE fence
        have blocked the marker's FOR NO KEY UPDATE: the MVCC window
        would have stayed open);
      * session C probes with FOR NO KEY UPDATE NOWAIT — the EXACT
        lock the rollup_at marker UPDATE acquires — and fails with
        'could not obtain lock': the fence really serialises against
        the marker.

    Falsifiable: revert the fence probe to a plain SELECT (or to
    FOR KEY SHARE) and session C acquires the lock, failing this
    test. Session A is rolled back; no fabricated row survives."""

    class _AbortError(Exception):
        pass

    async def run() -> None:
        tid, _, _ = await seed_actor()
        _, (p1_start, p1_end), _ = await _seed_history(tid)
        await _insert_legacy_closed_period(tid, p1_start, p1_end)

        period_probe = (
            "SELECT 1 FROM accounting_periods "
            "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps "
        )
        with pytest.raises(_AbortError):
            async with tenant_session(factory(), tid) as session_a:
                await session_a.execute(
                    text(
                        "INSERT INTO account_period_balances "
                        "(id, tenant_id, period_start, account, debits, credits) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, "
                        "'cash.bank', '1.00', '0.00')"
                    ),
                    {"id": str(uuid.uuid4()), "tid": str(tid), "ps": p1_start},
                )
                async with tenant_session(factory(), tid) as session_b:
                    await session_b.execute(
                        text(period_probe + "FOR KEY SHARE NOWAIT"),
                        {"tid": str(tid), "ps": p1_start},
                    )
                with pytest.raises(DBAPIError, match="could not obtain lock"):
                    async with tenant_session(factory(), tid) as session_c:
                        await session_c.execute(
                            text(period_probe + "FOR NO KEY UPDATE NOWAIT"),
                            {"tid": str(tid), "ps": p1_start},
                        )
                raise _AbortError  # roll A back: the row must not land

        rows = await _count(tid, "SELECT count(*) FROM account_period_balances")
        assert rows == 0, "the fabricated probe row must not survive the rollback"

    asyncio.run(run())


def test_member_rollup_cross_tenant_member_unrepresentable() -> None:
    """N2 probe (review !49): referential-integrity checks BYPASS RLS,
    so with the original plain REFERENCES members(id) a direct-SQL row
    pairing tenant A's tenant_id with tenant B's member_id was
    representable. The 0028 composite FK (tenant_id, member_id) ->
    members (tenant_id, id) (backed by uq_members_tenant_id_id, the
    0014 precedent) makes it unrepresentable at the database level.
    Falsifiable: relax the FK back to member_id -> members(id) and the
    cross-tenant INSERT succeeds (member B exists by id), failing this
    test. The same-tenant control INSERT proves the fence does not
    over-block."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        tid_b, _, _ = await seed_actor()
        member_a = await _seed_member(tid_a)
        member_b = await _seed_member(tid_b)
        (p1_start, p1_end), _ = _last_months(2)
        # An UNROLLED closed period in tenant A so the period FK is
        # satisfied and the late-insert fence stays disarmed - this
        # probe isolates the member FK.
        await _insert_legacy_closed_period(tid_a, p1_start, p1_end)

        insert_sql = text(
            "INSERT INTO member_period_balances "
            "(id, tenant_id, period_start, member_id, balance) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, "
            "CAST(:mid AS uuid), '9.99')"
        )
        with pytest.raises(DBAPIError, match="fk_member_period_balances_member"):
            async with tenant_session(factory(), tid_a) as session:
                await session.execute(
                    insert_sql,
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid_a),
                        "ps": p1_start,
                        "mid": str(member_b),
                    },
                )
        async with tenant_session(factory(), tid_a) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM member_period_balances"))
            ).scalar_one()
        assert int(count) == 0, "a cross-tenant member rollup row became representable"

        # Same-tenant control: the identical INSERT with tenant A's own
        # member passes the composite FK (the period is still unrolled,
        # so no fence fires).
        async with tenant_session(factory(), tid_a) as session:
            await session.execute(
                insert_sql,
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid_a),
                    "ps": p1_start,
                    "mid": str(member_a),
                },
            )
        async with tenant_session(factory(), tid_a) as session:
            kept = (
                await session.execute(text("SELECT member_id FROM member_period_balances"))
            ).scalar_one()
        assert str(kept) == str(member_a)

    asyncio.run(run())


def test_rollup_backfill_route_rbac_and_forbidden_body() -> None:
    async def run() -> None:
        for role, expected in (
            ("Teller", 403),
            ("Accountant", 403),
            ("Auditor", 403),
            ("Branch Manager", 200),
            ("System Admin", 200),
        ):
            _, _, token = await seed_actor(role)
            async with api_client() as client:
                res = await client.post(
                    "/jobs/period-rollups",
                    json={},
                    headers={"authorization": f"Bearer {token}"},
                )
            assert res.status_code == expected, f"{role}: {res.text}"

        _, _, token = await seed_actor()
        async with api_client() as client:
            smuggled = await client.post(
                "/jobs/period-rollups",
                json={"period_start": "2020-01-01"},
                headers={"authorization": f"Bearer {token}"},
            )
        assert smuggled.status_code == 422

    asyncio.run(run())
