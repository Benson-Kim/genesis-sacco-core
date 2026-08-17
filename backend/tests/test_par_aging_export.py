"""Portfolio-at-risk aging export (P13.10 on the P13 registry).

FM1/H2 — bucket-boundary oracles: loans at EXACTLY 1/30/31/90/91/
180/181/360/361 days past due land in the documented CLOSED integer
buckets [1,30] [31,90] [91,180] [181,360] [361,inf) (the half-open
real intervals (0,30] (30,90] etc. on whole days) and a dpd-0 loan
lands in its OWN "current" bucket (!40 review R1), including a
hand-computed leap-window pair. Balances and dpd are reconstructed from
schedule-vs-repayment history (the NPL-trend method, v1.1 rule 2) —
pinned by a drift-detector arm proving raw mutation of loans.balance /
days_past_due / classification does NOT change the report.

Every figure is hand-computed in comments (MASTER_PROMPT section 4);
each guard test fails with its guard removed (see the falsifiability
notes per test).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import factory
from export_helpers import count, seed_actor, seed_member
from genesis.application.exports import run_export
from genesis.application.reports import REPORTS, ExportFilters, ReportName
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import (
    _insert_txn,
    _seed_loan_for_book,
    download,
    parse_csv,
    request_and_render,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_MONEY = re.compile(r"-?\d+\.\d{2}")

_HEADERS = ["Bucket (days past due)", "Loans", "Outstanding", "% of Portfolio"]


async def _add_installment(
    tid: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    installment_no: int,
    due: date,
    total_due: str,
) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                ":no, :due, :total, 0, :total)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "lid": str(loan_id),
                "no": installment_no,
                "due": due,
                "total": total_due,
            },
        )


async def _render_par(tid: uuid.UUID, as_of: datetime) -> list[tuple[object, ...]]:
    """Drive the report through the REAL export engine (gate 1.1)."""
    async with tenant_session(factory(), tid) as session:
        query = await REPORTS[ReportName.PORTFOLIO_AT_RISK_AGING].build(
            session, tid, ExportFilters(), as_of
        )
        # DSA-4 (P13.17d): the run carries no rows — collect the
        # delivered batches, exactly as the real renderers do.
        rendered: list[tuple[object, ...]] = []
        await run_export(query, 100, row_cap=1000, on_batch=rendered.extend)
    return rendered


def test_fm1_boundary_days_land_in_the_documented_buckets() -> None:
    """FM1/H2 (+!40 review R1): nine loans, one unpaid installment
    each, due EXACTLY 1/30/31/90/91/180/181/360/361 days before the
    as-of date, plus one current loan with no schedule (dpd 0 — its
    OWN bucket, never lumped into an arrears bucket). No repayments,
    so every outstanding balance equals its principal.

    HAND-COMPUTED buckets (as-of 2026-08-01; due = as-of - N days):
      current : dpd 0 (900, nothing unmet)          -> 1 loan,    900.00
      1-30    : dpd 1 (1,100) + dpd 30 (1,000)      -> 2 loans,  2,100.00
      31-90   : dpd 31 (2,000) + dpd 90 (3,000)     -> 2 loans,  5,000.00
      91-180  : dpd 91 (4,000) + dpd 180 (5,000)    -> 2 loans,  9,000.00
      181-360 : dpd 181 (6,000) + dpd 360 (7,000)   -> 2 loans, 13,000.00
      360+    : dpd 361 (8,000)                     -> 1 loan,   8,000.00
      TOTAL   : 10 loans, 38,000.00
    Shares (2dp, half-up): 900/38000 = 2.37%, 2100/38000 = 5.53%,
    5000/38000 = 13.16%, 9000/38000 = 23.68%, 13000/38000 = 34.21%
    (the LARGEST bucket — assigned 100.00 - 65.79 = the same 34.21,
    R2), 8000/38000 = 21.05%; the column sums to exactly 100.00.
    Falsifiable: change any <= in the SQL CASE to < and the
    30/90/180/360 loans shift one bucket up; drop the dpd = 0 CASE
    arm and the current loan lands in 1-30 — both fail the vectors.
    """

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        as_of = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        d_date = as_of.date()
        for days, principal in (
            (1, "1100.00"),
            (30, "1000.00"),
            (31, "2000.00"),
            (90, "3000.00"),
            (91, "4000.00"),
            (180, "5000.00"),
            (181, "6000.00"),
            (360, "7000.00"),
            (361, "8000.00"),
        ):
            loan_id = await _seed_loan_for_book(
                tid, mid, principal=principal, balance=principal, disbursed_days_ago=2000
            )
            await _add_installment(
                tid,
                loan_id,
                installment_no=1,
                due=d_date - timedelta(days=days),
                total_due="500.00",
            )
        # The current loan: disbursed, no installment due -> dpd 0.
        await _seed_loan_for_book(
            tid, mid, principal="900.00", balance="900.00", disbursed_days_ago=2000
        )

        rows = await _render_par(tid, as_of)
        assert rows == [
            ("current", 1, Decimal("900.00"), Decimal("2.37")),
            ("1-30", 2, Decimal("2100.00"), Decimal("5.53")),
            ("31-90", 2, Decimal("5000.00"), Decimal("13.16")),
            ("91-180", 2, Decimal("9000.00"), Decimal("23.68")),
            ("181-360", 2, Decimal("13000.00"), Decimal("34.21")),
            ("360+", 1, Decimal("8000.00"), Decimal("21.05")),
            ("TOTAL", 10, Decimal("38000.00"), Decimal("100.00")),
        ]

    asyncio.run(run())


def test_h2_leap_window_days_are_counted_on_the_real_calendar() -> None:
    """H2 leap-window oracle: the SAME calendar offsets straddle a
    bucket boundary depending on whether February 29 exists in the
    window — days past due must be REAL elapsed days, never a 30-day
    month approximation.

    HAND-COMPUTED:
      * Loan L due 2024-01-30, as-of 2024-03-01 (2024 is a leap year):
        Jan 30 -> Feb 29 is 30 days, +1 -> Mar 1 = 31 dpd -> "31-90".
      * Loan M due 2025-01-30, as-of 2025-03-01 (non-leap):
        Jan 30 -> Feb 28 is 29 days, +1 -> Mar 1 = 30 dpd -> "1-30".
      * At the 2025 as-of, L is 396 dpd (366 leap-year days to
        2025-01-30, +30) -> "360+".
    M is disbursed 2024-06-01, AFTER the 2024 as-of, so the leap run
    sees only L. Falsifiable: a 30-day-month dpd approximation makes
    both loans 30 dpd and the leap-run bucket vector fails.
    """

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        as_of_leap = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
        as_of_nonleap = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
        today = datetime.now(UTC).date()

        loan_l = await _seed_loan_for_book(
            tid,
            mid,
            principal="1500.00",
            balance="1500.00",
            disbursed_days_ago=(today - date(2023, 6, 1)).days,
        )
        await _add_installment(
            tid, loan_l, installment_no=1, due=date(2024, 1, 30), total_due="500.00"
        )
        loan_m = await _seed_loan_for_book(
            tid,
            mid,
            principal="2500.00",
            balance="2500.00",
            disbursed_days_ago=(today - date(2024, 6, 1)).days,
        )
        await _add_installment(
            tid, loan_m, installment_no=1, due=date(2025, 1, 30), total_due="500.00"
        )

        leap_rows = await _render_par(tid, as_of_leap)
        assert leap_rows == [
            ("current", 0, Decimal("0.00"), Decimal("0.00")),
            ("1-30", 0, Decimal("0.00"), Decimal("0.00")),
            ("31-90", 1, Decimal("1500.00"), Decimal("100.00")),
            ("91-180", 0, Decimal("0.00"), Decimal("0.00")),
            ("181-360", 0, Decimal("0.00"), Decimal("0.00")),
            ("360+", 0, Decimal("0.00"), Decimal("0.00")),
            ("TOTAL", 1, Decimal("1500.00"), Decimal("100.00")),
        ]

        nonleap_rows = await _render_par(tid, as_of_nonleap)
        # 1500/4000 = 37.50%; 2500/4000 = 62.50% (the largest bucket:
        # 100.00 - 37.50 assigns the same 62.50, R2).
        assert nonleap_rows == [
            ("current", 0, Decimal("0.00"), Decimal("0.00")),
            ("1-30", 1, Decimal("2500.00"), Decimal("62.50")),
            ("31-90", 0, Decimal("0.00"), Decimal("0.00")),
            ("91-180", 0, Decimal("0.00"), Decimal("0.00")),
            ("181-360", 0, Decimal("0.00"), Decimal("0.00")),
            ("360+", 1, Decimal("1500.00"), Decimal("37.50")),
            ("TOTAL", 2, Decimal("4000.00"), Decimal("100.00")),
        ]

    asyncio.run(run())


def test_reconstruction_and_drift_detector_end_to_end() -> None:
    """The report is built from schedule-vs-repayment history, never
    from mutable loan-state columns (v1.1 rule 2; the P13.9 FM1
    drift-detector style).

    HAND-COMPUTED: principal 10,000.00; installments 3,000.00 due 100
    days ago and 3,000.00 due 40 days ago; one repayment of 3,000.00
    (60 days ago) with a 2,500.00 loans.receivable principal leg (the
    other 500.00 recognised as interest income). Cash paid 3,000.00
    meets installment #1 exactly (cum_due 3,000 not > 3,000); #2 is
    unmet -> dpd 40 -> bucket "31-90". Outstanding = 10,000.00 -
    2,500.00 = 7,500.00.

    Drift arm: raw-mutating loans.balance/days_past_due/classification
    afterwards changes NOTHING. Falsifiable: rebuild the report on
    loans.balance (or loans.days_past_due) and the second render shows
    1.00 / bucket 360+, failing the equality.
    """

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        today = datetime.now(UTC).date()
        loan_id = await _seed_loan_for_book(
            tid, mid, principal="10000.00", balance="10000.00", disbursed_days_ago=150
        )
        await _add_installment(
            tid, loan_id, installment_no=1, due=today - timedelta(days=100), total_due="3000.00"
        )
        await _add_installment(
            tid, loan_id, installment_no=2, due=today - timedelta(days=40), total_due="3000.00"
        )
        txn_id = await _insert_txn(
            tid, ref="RP-777001", txn_type="loan_repayment", amount="3000.00", days_ago=60
        )
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                    "CAST(:txn AS uuid), '3000.00')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "lid": str(loan_id), "txn": str(txn_id)},
            )
            # Balanced legs (0004 trigger): DR cash 3000 / CR receivable
            # 2500 + CR interest income 500.
            for account, side, amount in (
                ("cash.bank", "debit", "3000.00"),
                ("loans.receivable", "credit", "2500.00"),
                ("income.interest", "credit", "500.00"),
            ):
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

        _, completed = await request_and_render(token, tid, {"report": "portfolio_at_risk_aging"})
        artifact = completed["artifact"]
        assert artifact is not None
        status, headers, body = await download(token, artifact["csv_download"])
        assert status == 200
        assert headers["x-export-truncated"] == "false"
        rows = parse_csv(body)
        assert rows[0] == _HEADERS
        # Row 1 is "current", row 2 is "1-30" (R1); the 40-dpd loan
        # sits in "31-90" at row 3; TOTAL closes the 6 buckets.
        assert rows[3] == ["31-90", "1", "7500.00", "100.00"]
        assert rows[7] == ["TOTAL", "1", "7500.00", "100.00"]

        # H3: every money cell renders in the canonical 2dp form (the
        # !34 '0' vs '0.00' inconsistency class).
        for row in rows[1:]:
            assert _MONEY.fullmatch(row[2]), row
            assert _MONEY.fullmatch(row[3]), row

        # Export audit row for the run (P13 blocker f).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.completed' "
                "AND after->>'report' = 'portfolio_at_risk_aging'",
            )
            == 1
        )

        # Drift arm: mutate every mutable loan-state column raw.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET balance = '1.00', days_past_due = 999, "
                    "classification = 'loss' "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(loan_id), "tid": str(tid)},
            )
        _, second = await request_and_render(token, tid, {"report": "portfolio_at_risk_aging"})
        artifact_two = second["artifact"]
        assert artifact_two is not None
        _, _, body_two = await download(token, artifact_two["csv_download"])
        assert parse_csv(body_two) == rows  # reconstruction, not state

    asyncio.run(run())


def test_r2_percent_of_portfolio_foots_to_exactly_100() -> None:
    """!40 review R2 — the '% of Portfolio' column must FOOT: with
    three equal buckets, NAIVE independent rounding prints 33.33
    three times (sum 99.99) against a TOTAL row of 100.00.

    HAND-COMPUTED: three loans of 1,000.00 each — dpd 0 (current),
    dpd 40 (31-90) and dpd 100 (91-180); portfolio 3,000.00. Every
    raw share is 1000/3000 x 100 = 33.333... -> to_cents 33.33. The
    largest-remainder step picks the FIRST bucket on the outstanding
    tie (current — deterministic) and assigns it 100.00 - (33.33 +
    33.33 + 0.00 x 3) = 33.34, so the printed column sums to exactly
    100.00 while TOTAL stays 100.00. Falsifiable: restore naive
    independent rounding (share = to_cents(outstanding x 100 / total)
    for EVERY bucket) and current prints 33.33 — the row vector and
    the sum-to-100.00 assert both fail (99.99).
    """

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        as_of = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        d_date = as_of.date()
        # dpd 0: disbursed, no installment due -> current.
        await _seed_loan_for_book(
            tid, mid, principal="1000.00", balance="1000.00", disbursed_days_ago=2000
        )
        for days in (40, 100):
            loan_id = await _seed_loan_for_book(
                tid, mid, principal="1000.00", balance="1000.00", disbursed_days_ago=2000
            )
            await _add_installment(
                tid,
                loan_id,
                installment_no=1,
                due=d_date - timedelta(days=days),
                total_due="500.00",
            )

        rows = await _render_par(tid, as_of)
        assert rows == [
            ("current", 1, Decimal("1000.00"), Decimal("33.34")),
            ("1-30", 0, Decimal("0.00"), Decimal("0.00")),
            ("31-90", 1, Decimal("1000.00"), Decimal("33.33")),
            ("91-180", 1, Decimal("1000.00"), Decimal("33.33")),
            ("181-360", 0, Decimal("0.00"), Decimal("0.00")),
            ("360+", 0, Decimal("0.00"), Decimal("0.00")),
            ("TOTAL", 3, Decimal("3000.00"), Decimal("100.00")),
        ]
        bucket_share_sum = sum((row[3] for row in rows[:-1]), Decimal("0.00"))
        assert bucket_share_sum == Decimal("100.00")
        assert rows[-1][3] == Decimal("100.00")

    asyncio.run(run())


def test_r4_shared_transaction_repayments_apportion_without_fanout() -> None:
    """!40 review R4 — repayments.transaction_id carries NO DB-level
    UNIQUE (0001 ships only the FK; 0014 only a NON-unique index), so
    ONE transaction may legally carry repayment rows against several
    loans. The transaction's loans.receivable credit legs must be
    apportioned pro-rata by repayment amount — never attributed
    wholesale to every joined loan.

    HAND-COMPUTED: loan A principal 5,000.00 with installment
    2,000.00 due 40 days ago; loan B principal 3,000.00 with no
    schedule. ONE transaction of 1,200.00 carries repayments A 900.00
    + B 300.00; its balanced legs are DR cash 1,200.00 / CR
    loans.receivable 1,000.00 + CR income.interest 200.00. Pro-rata
    principal: A = 1000 x 900/1200 = 750.00 -> outstanding 4,250.00
    (cash 900 < 2,000 due -> dpd 40 -> "31-90"); B = 1000 x 300/1200
    = 250.00 -> outstanding 2,750.00 (current). TOTAL 2 loans,
    7,000.00; shares 2750/7000 = 39.29%, 4250/7000 = 60.714 -> 60.71
    (the largest bucket: 100.00 - 39.29 = the same 60.71).
    Falsifiable: restore the plain ledger->repayments join on
    transaction_id and EACH loan absorbs the full 1,000.00 of legs —
    outstanding 4,000.00 / 2,000.00 and TOTAL 6,000.00 fail the
    vector.
    """

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        today = datetime.now(UTC).date()
        loan_a = await _seed_loan_for_book(
            tid, mid, principal="5000.00", balance="5000.00", disbursed_days_ago=100
        )
        await _add_installment(
            tid, loan_a, installment_no=1, due=today - timedelta(days=40), total_due="2000.00"
        )
        loan_b = await _seed_loan_for_book(
            tid, mid, principal="3000.00", balance="3000.00", disbursed_days_ago=100
        )
        txn_id = await _insert_txn(
            tid, ref="RP-777002", txn_type="loan_repayment", amount="1200.00", days_ago=10
        )
        async with tenant_session(factory(), tid) as session:
            for loan_id, amount in ((loan_a, "900.00"), (loan_b, "300.00")):
                await session.execute(
                    text(
                        "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                        "CAST(:txn AS uuid), :amount)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "lid": str(loan_id),
                        "txn": str(txn_id),
                        "amount": amount,
                    },
                )
            # Balanced legs (0004 trigger): DR cash 1200 / CR
            # receivable 1000 + CR interest income 200.
            for account, side, amount in (
                ("cash.bank", "debit", "1200.00"),
                ("loans.receivable", "credit", "1000.00"),
                ("income.interest", "credit", "200.00"),
            ):
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

        rows = await _render_par(tid, datetime.now(UTC))
        assert rows == [
            ("current", 1, Decimal("2750.00"), Decimal("39.29")),
            ("1-30", 0, Decimal("0.00"), Decimal("0.00")),
            ("31-90", 1, Decimal("4250.00"), Decimal("60.71")),
            ("91-180", 0, Decimal("0.00"), Decimal("0.00")),
            ("181-360", 0, Decimal("0.00"), Decimal("0.00")),
            ("360+", 0, Decimal("0.00"), Decimal("0.00")),
            ("TOTAL", 2, Decimal("7000.00"), Decimal("100.00")),
        ]

    asyncio.run(run())
