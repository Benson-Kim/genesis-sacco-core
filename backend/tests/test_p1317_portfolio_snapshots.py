"""P13.17(a) / DSA-1 — month-end portfolio snapshots (FM1 suite).

Named failure mode FM1: snapshot-vs-reconstruction divergence. Every
figure asserted below is HAND-COMPUTED in comments from the seeded
loans/schedules/repayments — never read back from the implementation
(anti-reward-hacking, MASTER_PROMPT §4). Falsifiability, per test:

  * equality property — fails if the snapshot writer ever computes
    with anything but NPL_TREND_MONTH_SQL (or the export re-derives a
    different figure);
  * snapshot-read probe — fails if the export silently goes back to
    re-scanning months whose snapshot exists (the DSA-1 regression);
  * write-once probes — fail when the 0027 trigger is removed (direct
    SQL UPDATE/DELETE through the app role would then succeed);
  * divergence 409 — fails if a diverging snapshot is silently
    trusted or self-healed instead of aborting the close;
  * re-run no-op — fails if a repeated backfill writes anything
    (side-effect counts, v1.1 rule 8).
"""

import asyncio
import csv
import io
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from functools import partial

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import drain_export_queue, seed_actor
from genesis.application import accounting_periods as periods_service
from genesis.application import portfolio_snapshots as snapshots_service
from genesis.application.portfolio_reconstruction import reconstruct_month
from genesis.application.portfolio_snapshots import month_end_of
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _month_ends_back(n: int) -> list[date]:
    """The last n fully elapsed calendar month ends, oldest first."""
    today = datetime.now(UTC).date()
    ends: list[date] = []
    cursor = today.replace(day=1) - timedelta(days=1)  # last month's end
    for _ in range(n):
        ends.append(cursor)
        cursor = cursor.replace(day=1) - timedelta(days=1)
    ends.reverse()
    return ends


async def _seed_portfolio(tid: uuid.UUID) -> tuple[date, date, date, date]:
    """One loan whose month-by-month state is hand-computable.

    m0..m3 = the last four fully elapsed month ends. Loan L1:
    principal 10000.00, disbursed at (m1 - 40 days), installment 1 of
    5000.00 due at (m1 - 80 days), repayment of 2000.00 (with its
    loans.receivable credit leg) at (m3 - 5 days).

    Hand-computed month-end state (dpd = month end minus due date):
      m0: loan exists (disbursed m1-40 < m0 end + 1); paid 0; dpd =
          80 - (days in m1's month: 28..31) = 49..52 <= 90 -> NOT NPL;
          gross 10000.00.
      m1: dpd exactly 80 <= 90 -> NOT NPL; gross 10000.00.
      m2: dpd = 80 + 28..31 = 108..111 > 90 -> NPL; gross 10000.00.
      m3: repayment landed (m3 - 5); principal repaid 2000.00 ->
          gross 8000.00; dpd = 80 + 59..62 > 90 -> NPL 8000.00.
    """
    m0, m1, m2, m3 = _month_ends_back(4)
    loan_id = uuid.uuid4()
    product_id = uuid.uuid4()
    app_id = uuid.uuid4()
    member_id = uuid.uuid4()
    disbursed_at = datetime.combine(m1 - timedelta(days=40), time(12, 0), tzinfo=UTC)
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Snapshot Member')"
            ),
            {"id": str(member_id), "tid": str(tid), "no": f"GP-{member_id.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(product_id), "tid": str(tid), "name": f"Snap-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '10000.00', 12, '12.00', 'disbursed', '0.00')"
            ),
            {"id": str(app_id), "tid": str(tid), "mid": str(member_id), "pid": str(product_id)},
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, principal, "
                " balance, rate_pct, term_months, disbursed_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), '10000.00', '10000.00', "
                "'12.00', 12, :disbursed)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(app_id),
                "mid": str(member_id),
                "pid": str(product_id),
                "disbursed": disbursed_at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "1, :due, 5000, 0, 5000)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "lid": str(loan_id),
                "due": m1 - timedelta(days=80),
            },
        )
        txn_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, txn_ref, type, amount, channel, occurred_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, 'loan_repayment', "
                "'2000.00', 'bank', :ts)"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "ref": f"RP-{uuid.uuid4().hex[:6].upper()}",
                "ts": datetime.combine(m3 - timedelta(days=5), time(12, 0), tzinfo=UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "CAST(:txn AS uuid), '2000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "lid": str(loan_id), "txn": str(txn_id)},
        )
        for account, side in (("cash.bank", "debit"), ("loans.receivable", "credit")):
            await session.execute(
                text(
                    "INSERT INTO ledger_entries "
                    "(id, tenant_id, transaction_id, account, side, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:txn AS uuid), "
                    ":account, :side, '2000.00')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "txn": str(txn_id),
                    "account": account,
                    "side": side,
                },
            )
    return m0, m1, m2, m3


#: Hand-computed month-end oracle for the _seed_portfolio scenario:
#: (loans, gross, npl_loans, npl_balance) — see _seed_portfolio's
#: docstring for the derivation.
_ORACLE = {
    0: (1, "10000.00", 0, "0.00"),
    1: (1, "10000.00", 0, "0.00"),
    2: (1, "10000.00", 1, "10000.00"),
    3: (1, "8000.00", 1, "8000.00"),
}


async def _snapshot_rows(tid: uuid.UUID) -> list[tuple[date, int, str, int, str]]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT month_end, loans, gross_outstanding, npl_loans, npl_balance "
                    "FROM portfolio_month_snapshots ORDER BY month_end"
                )
            )
        ).all()
    return [(r[0], int(r[1]), str(r[2]), int(r[3]), str(r[4])) for r in rows]


async def _count(tid: uuid.UUID, sql: str) -> int:
    async with tenant_session(factory(), tid) as session:
        value = (await session.execute(text(sql))).scalar_one()
    return int(value)


def test_backfill_writes_hand_computed_snapshots_and_rerun_is_noop() -> None:
    async def run() -> None:
        tid, uid, _ = await seed_actor()
        m0, m1, m2, m3 = await _seed_portfolio(tid)

        scope = partial(tenant_session, factory(), tid)
        result = await snapshots_service.run_snapshot_backfill_for_tenant(scope, tid, uid)
        # The worklist runs from the disbursement month (m1 - 40 days
        # lands in m0's month) through the last fully elapsed month.
        assert result.months_considered >= 4
        assert result.written == result.months_considered

        rows = await _snapshot_rows(tid)
        by_month = {row[0]: row[1:] for row in rows}
        for index, month in enumerate((m0, m1, m2, m3)):
            loans, gross, npl_loans, npl_balance = _ORACLE[index]
            assert by_month[month] == (loans, gross, npl_loans, npl_balance), (
                f"month {month} snapshot != hand oracle"
            )

        # FM1 equality with the full scan: the stored row IS the
        # reconstruction's output for every month, to the cent.
        async with tenant_session(factory(), tid) as session:
            for month in (m0, m1, m2, m3):
                full = await reconstruct_month(session, tid, month)
                assert by_month[month] == (
                    full.loans,
                    str(full.gross_outstanding),
                    full.npl_loans,
                    str(full.npl_balance),
                )

        # Re-run: lock-free no-op proven by side-effect counts.
        audits_before = await _count(
            tid, "SELECT count(*) FROM audit_log WHERE action = 'portfolio_snapshot.write'"
        )
        rerun = await snapshots_service.run_snapshot_backfill_for_tenant(scope, tid, uid)
        assert (rerun.months_considered, rerun.written, rerun.batches) == (0, 0, 0)
        assert await _snapshot_rows(tid) == rows
        audits_after = await _count(
            tid, "SELECT count(*) FROM audit_log WHERE action = 'portfolio_snapshot.write'"
        )
        assert audits_after == audits_before

    asyncio.run(run())


def test_close_period_writes_the_snapshot_in_the_same_transaction() -> None:
    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, _, _, m3 = await _seed_portfolio(tid)

        async with tenant_session(factory(), tid) as session:
            await periods_service.close_period(session, tid, uid, year=m3.year, month=m3.month)

        rows = await _snapshot_rows(tid)
        # Hand-computed m3 figures (see _ORACLE): 1 loan, gross
        # 8000.00 after the 2000.00 principal repayment, NPL 8000.00.
        assert rows == [(m3, 1, "8000.00", 1, "8000.00")]
        audits = await _count(
            tid, "SELECT count(*) FROM audit_log WHERE action = 'portfolio_snapshot.write'"
        )
        assert audits == 1

        # Closing another month after a backfill already wrote it is
        # an idempotent no-op for the snapshot (verified equal), never
        # a second row.
        m2 = m3.replace(day=1) - timedelta(days=1)
        scope = partial(tenant_session, factory(), tid)
        await snapshots_service.run_snapshot_backfill_for_tenant(scope, tid, uid)
        count_before = await _count(tid, "SELECT count(*) FROM portfolio_month_snapshots")
        async with tenant_session(factory(), tid) as session:
            await periods_service.close_period(session, tid, uid, year=m2.year, month=m2.month)
        assert await _count(tid, "SELECT count(*) FROM portfolio_month_snapshots") == count_before

    asyncio.run(run())


def test_snapshots_are_write_once_at_the_database_level() -> None:
    """Insider probe (FM1): a direct-SQL UPDATE or DELETE through the
    app role — the classic restated-month concealment vector — must
    fail on the 0027 trigger. Falsifiable: drop the trigger and both
    probes succeed, failing this test."""

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, _, _, m3 = await _seed_portfolio(tid)
        async with tenant_session(factory(), tid) as session:
            await periods_service.close_period(session, tid, uid, year=m3.year, month=m3.month)

        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE portfolio_month_snapshots "
                        "SET npl_balance = '0.00', npl_loans = 0 "
                        "WHERE tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"tid": str(tid)},
                )
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "DELETE FROM portfolio_month_snapshots WHERE tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"tid": str(tid)},
                )
        # The tampering attempts changed nothing.
        assert (await _snapshot_rows(tid)) == [(m3, 1, "8000.00", 1, "8000.00")]

    asyncio.run(run())


def test_future_month_snapshot_insert_refused_at_the_database() -> None:
    """N1 probe (review !49): a fabricated FUTURE-month snapshot passes
    every CHECK; the write-once trigger would then freeze it
    undeletable and the month's REAL close would 409 forever on
    verify-on-conflict — an irreversible denial-of-integrity vector.
    The 0027 BEFORE INSERT fence refuses month_end >=
    date_trunc('month', now())::date at the database level, for every
    trigger-subject role including direct SQL through the app role (a
    CHECK cannot carry the rule: now() is not immutable). Falsifiable:
    drop the portfolio_month_snapshots_no_future trigger and both
    refused INSERTs succeed, failing this test. The last fully elapsed
    month must REMAIN representable — the fence closes the future,
    never the writer's own path."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        today = datetime.now(UTC).date()
        current_month_end = month_end_of(today)
        future_month_end = month_end_of(current_month_end + timedelta(days=1))
        insert_sql = text(
            "INSERT INTO portfolio_month_snapshots "
            "(id, tenant_id, month_end, loans, gross_outstanding, "
            " npl_loans, npl_balance, source) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :month_end, "
            "1, '100.00', 0, '0.00', 'backfill')"
        )
        for month in (current_month_end, future_month_end):
            with pytest.raises(DBAPIError, match="fully elapsed"):
                async with tenant_session(factory(), tid) as session:
                    await session.execute(
                        insert_sql,
                        {"id": str(uuid.uuid4()), "tid": str(tid), "month_end": month},
                    )
        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM portfolio_month_snapshots"))
            ).scalar_one()
        assert int(count) == 0, "a not-fully-elapsed month became representable"

        # Over-blocking guard: the last FULLY ELAPSED month is still
        # representable through the same direct-SQL path the fence
        # polices (values CHECK-valid; no real portfolio seeded, so no
        # writer collision).
        elapsed_month_end = _month_ends_back(1)[0]
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                insert_sql,
                {"id": str(uuid.uuid4()), "tid": str(tid), "month_end": elapsed_month_end},
            )
        async with tenant_session(factory(), tid) as session:
            kept = (
                await session.execute(text("SELECT month_end FROM portfolio_month_snapshots"))
            ).scalar_one()
        assert kept == elapsed_month_end

    asyncio.run(run())


def test_close_aborts_loudly_on_divergent_preexisting_snapshot() -> None:
    """FM1: a snapshot that does not match the reconstruction 409s and
    aborts the close with ZERO partial state — never self-heals."""

    async def run() -> None:
        tid, uid, _ = await seed_actor()
        _, _, _, m3 = await _seed_portfolio(tid)
        # Fabricate a diverging (but CHECK-valid) snapshot for m3 —
        # the direct-SQL insider INSERT vector.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO portfolio_month_snapshots "
                    "(id, tenant_id, month_end, loans, gross_outstanding, "
                    " npl_loans, npl_balance, source) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :month_end, "
                    "1, '999.00', 0, '0.00', 'backfill')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "month_end": m3},
            )

        with pytest.raises(ConflictError, match="diverges"):
            async with tenant_session(factory(), tid) as session:
                await periods_service.close_period(session, tid, uid, year=m3.year, month=m3.month)

        # Kill-switch: the failed close left no partial state.
        assert await _count(tid, "SELECT count(*) FROM accounting_periods") == 0
        assert (
            await _count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'accounting_period.close'",
            )
            == 0
        )

    asyncio.run(run())


def test_npl_trend_export_serves_snapshots_and_matches_the_oracle() -> None:
    """FM1 end-to-end: the export output over snapshots equals the
    hand-computed month figures to the cent, and a month WITHOUT a
    snapshot renders identically through the reconstruction fallback
    (no observable money-semantics change)."""

    async def run() -> None:
        tid, uid, token = await seed_actor()
        m0, m1, m2, m3 = await _seed_portfolio(tid)
        scope = partial(tenant_session, factory(), tid)

        async def render_rows() -> list[list[str]]:
            headers = {"authorization": f"Bearer {token}"}
            async with api_client() as client:
                created = await client.post(
                    "/exports", json={"report": "npl_trend"}, headers=headers
                )
                assert created.status_code == 201, created.text
                export = created.json()
                summary = await drain_export_queue(tid)
                assert summary.failed == 0
                fetched = await client.get(f"/exports/{export['id']}", headers=headers)
                assert fetched.status_code == 200
                completed = fetched.json()
                artifact = completed["artifact"]
                assert artifact is not None
                download = await client.get(artifact["csv_download"], headers=headers)
                assert download.status_code == 200
            return list(csv.reader(io.StringIO(download.content.decode("utf-8"))))

        # Render once with NO snapshots (pure reconstruction fallback),
        # then backfill and render again: byte-identical rows.
        before = await render_rows()
        result = await snapshots_service.run_snapshot_backfill_for_tenant(scope, tid, uid)
        assert result.written >= 4
        after = await render_rows()
        assert before == after, "snapshot-served export diverged from the full scan"

        # Hand oracle on the snapshot-served months (rows are oldest
        # first; the configured window is 6 months incl. current).
        by_month = {row[0]: row[1:] for row in after[1:]}
        for month, index in ((m0, 0), (m1, 1), (m2, 2), (m3, 3)):
            _loans, gross, npl_loans, npl_balance = _ORACLE[index]
            label = f"{month.year:04d}-{month.month:02d}"
            # Ratio: npl_balance * 100 / gross, cent-rounded.
            expected_ratio = {0: "0.00", 1: "0.00", 2: "100.00", 3: "100.00"}[index]
            assert by_month[label] == [gross, npl_balance, str(npl_loans), expected_ratio]

    asyncio.run(run())


def test_npl_trend_export_actually_reads_snapshots() -> None:
    """Mechanism probe (falsifies the DSA-1 regression): a snapshot row
    with distinctive figures IS what the export renders for its month.
    If the export silently went back to re-scanning every month, this
    test fails. The fabricated row is impossible through the app write
    path (writer figures always equal the reconstruction; the write-
    once trigger blocks edits) — this is a read-path probe only."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        last_month_end = _month_ends_back(1)[0]
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO portfolio_month_snapshots "
                    "(id, tenant_id, month_end, loans, gross_outstanding, "
                    " npl_loans, npl_balance, source) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :month_end, "
                    "1, '123.00', 0, '0.00', 'backfill')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "month_end": last_month_end},
            )
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post("/exports", json={"report": "npl_trend"}, headers=headers)
            assert created.status_code == 201, created.text
            export = created.json()
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export['id']}", headers=headers)
            artifact = fetched.json()["artifact"]
            assert artifact is not None
            download = await client.get(artifact["csv_download"], headers=headers)
        rows = list(csv.reader(io.StringIO(download.content.decode("utf-8"))))
        label = f"{last_month_end.year:04d}-{last_month_end.month:02d}"
        by_month = {row[0]: row[1:] for row in rows[1:]}
        assert by_month[label][0] == "123.00", "export did not read the snapshot"

    asyncio.run(run())


def test_snapshot_writer_refuses_incomplete_or_non_month_end() -> None:
    async def run() -> None:
        tid, uid, _ = await seed_actor()
        today = datetime.now(UTC).date()
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ConflictError, match="month ends"):
                await snapshots_service.write_month_snapshot(
                    session, tid, uid, today.replace(day=1), source="backfill"
                )
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ConflictError, match="fully elapsed"):
                await snapshots_service.write_month_snapshot(
                    session, tid, uid, month_end_of(today), source="backfill"
                )

    asyncio.run(run())


def test_backfill_route_rbac_and_forbidden_body() -> None:
    """The job sits with the close-period authority (transactions x
    APPROVE): posting roles must not be able to (re)establish the
    month-end figures they post into. extra="forbid" rejects smuggled
    fields (insider rule: no caller-supplied dates/periods anywhere)."""

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
                    "/jobs/portfolio-snapshots",
                    json={},
                    headers={"authorization": f"Bearer {token}"},
                )
            assert res.status_code == expected, f"{role}: {res.text}"

        _, _, token = await seed_actor()
        async with api_client() as client:
            smuggled = await client.post(
                "/jobs/portfolio-snapshots",
                json={"month_end": "2020-01-31"},
                headers={"authorization": f"Bearer {token}"},
            )
        assert smuggled.status_code == 422

    asyncio.run(run())
