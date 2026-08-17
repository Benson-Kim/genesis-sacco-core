"""Income statement export (P13.10 on the P13 registry).

H1/FM2 — conservation cross-oracles: the P&L line totals reconcile TO
THE CENT against the trial-balance income/expense aggregates over the
same period (two independent statements — if either query drifts the
equality fails). The statement and the trial balance both carry the
NEW liability.unclaimed_dividends account from !36, seeded through the
REAL !36 path (mid-run P12 exit -> audited unclaimed disposition):
the payable appears on the liability side and dividend expense ==
paid + unclaimed claims (!36 reviewer note N2).

H5 — explicit period semantics (inclusive calendar dates against
transactions.occurred_at) and reversal-aware aggregation. H1 fail-loud
— a ledger account not covered by the code-owned chart map aborts the
job (status 'failed'), never a silently dropped P&L line.

Every figure is hand-computed in comments (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, count, drain_export_queue, seed_actor, seed_member
from genesis.application.dividends import DeclarationStatus, distribute_dividend
from genesis.application.ledger import (
    post_deposit_interest,
    post_loan_interest_accrual,
    post_reversal,
)
from genesis.infrastructure.tenancy import tenant_session
from test_dividend_dormant_policy import _paid_then_exited
from test_dividends import _scope, _sum
from test_reports_e2e import download, parse_csv, request_and_render

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_MONEY = re.compile(r"-?\d+\.\d{2}")

_HEADERS = ["Section", "Line", "Amount"]


async def _render_rows(token: str, tid: uuid.UUID, body: dict[str, object]) -> list[list[str]]:
    _, completed = await request_and_render(token, tid, body)
    artifact = completed["artifact"]
    assert artifact is not None
    status, _, payload = await download(token, artifact["csv_download"])
    assert status == 200
    rows = parse_csv(payload)
    assert rows[0] == _HEADERS
    # H3: every Amount cell in the canonical 2dp form (the !34 '0' vs
    # '0.00' inconsistency class — never repeated here).
    for row in rows[1:]:
        assert _MONEY.fullmatch(row[2]), row
    return rows


def _tb_balances(rows: list[list[str]]) -> dict[str, tuple[Decimal, Decimal]]:
    return {row[0]: (Decimal(row[1]), Decimal(row[2])) for row in rows[1:] if row[0] != "TOTAL"}


def test_fm2_line_totals_reconcile_to_the_trial_balance_to_the_cent() -> None:
    """HAND-COMPUTED postings (all within the as-of window):
      * loan interest accrual 150.00 -> DR interest.receivable /
        CR income.interest
      * deposit interest 40.00 -> DR interest.expense /
        CR member.deposits
    Income statement: income.interest 150.00, TOTAL INCOME 150.00;
    interest.expense 40.00, TOTAL EXPENSE 40.00; NET SURPLUS 110.00.
    Cross-oracle: the trial balance (independent statement, same
    snapshot semantics) must show income.interest CR-DR == 150.00 and
    interest.expense DR-CR == 40.00 — the FM2 conservation equality.
    Falsifiable: scope either query differently (drop the reversal
    side-netting, change the period bound) and the equality fails.
    """

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await post_loan_interest_accrual(session, tid, mid, Decimal("150.00"))
        async with tenant_session(factory(), tid) as session:
            await post_deposit_interest(session, tid, mid, Decimal("40.00"))

        rows = await _render_rows(token, tid, {"report": "income_statement"})
        assert rows[1:] == [
            ["income", "income.interest", "150.00"],
            ["income", "TOTAL INCOME", "150.00"],
            ["expense", "interest.expense", "40.00"],
            ["expense", "TOTAL EXPENSE", "40.00"],
            ["net", "NET SURPLUS", "110.00"],
        ]

        _, tb_completed = await request_and_render(token, tid, {"report": "trial_balance"})
        tb_artifact = tb_completed["artifact"]
        assert tb_artifact is not None
        _, _, tb_body = await download(token, tb_artifact["csv_download"])
        balances = _tb_balances(parse_csv(tb_body))
        # FM2: the two statements agree to the cent.
        debits, credits = balances["income.interest"]
        assert credits - debits == Decimal("150.00")
        debits, credits = balances["interest.expense"]
        assert debits - credits == Decimal("40.00")

    asyncio.run(run())


def test_h1_unclaimed_dividend_payable_carries_through_all_three_statements() -> None:
    """The !36 liability, seeded through the REAL path (declare ->
    approve -> first run pays A while B's row is locked -> B exits via
    the REAL P12 workflow -> disposition run parks B's entitlement).

    HAND-COMPUTED (the _paid_then_exited fixture): A 12,000.00 shares
    all 365 days -> dividend 600.00 PAID (DR expense.dividends / CR
    member.shares at FY end); B 6,000.00 all year -> 300.00 UNCLAIMED
    (DR expense.dividends / CR liability.unclaimed_dividends); exit
    refund 6,000.00 (DR member.shares / CR cash.bank; exit fee default
    0). So:
      * income statement: expense.dividends 900.00, TOTAL EXPENSE
        900.00, NET SURPLUS -900.00 — dividend expense == paid 600.00
        + unclaimed 300.00 (!36 reviewer note N2);
      * trial balance: liability.unclaimed_dividends CR 300.00 / DR 0
        — the payable is ON the statement, not invisible;
      * SASRA return: L200 (unclaimed payable) 300.00, X200
        (dividends) 900.00, A100 cash 12,000.00 (18,000 in - 6,000
        refund), E100 shares 12,600.00 (18,000 + 600 - 6,000), CHK1
        0.00 (12,000 + 900 - 300 - 12,600 = 0).
    Falsifiable: drop liability.unclaimed_dividends from ACCOUNT_CLASS
    / the L200 line and the exports abort (fail-loud) instead of
    balancing; route the unclaimed posting to member accounts and both
    the 300.00 payable and the CHK1 zero fail."""

    async def run() -> None:
        tid, _, _, _, record = await _paid_then_exited()
        second = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert second.unclaimed == 1
        assert second.status is DeclarationStatus.DISTRIBUTED
        _, token = await add_user(tid, "System Admin")

        # Side-effect oracle for "paid": the member.shares dividend
        # credits sum to exactly A's 600.00.
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "JOIN transactions t ON t.id = le.transaction_id "
            "WHERE t.type = 'dividend_posting' AND le.side = 'credit' "
            "AND le.account = 'member.shares'",
        ) == Decimal("600.00")

        rows = await _render_rows(token, tid, {"report": "income_statement"})
        assert rows[1:] == [
            ["income", "TOTAL INCOME", "0.00"],
            ["expense", "expense.dividends", "900.00"],
            ["expense", "TOTAL EXPENSE", "900.00"],
            ["net", "NET SURPLUS", "-900.00"],
        ]

        _, tb_completed = await request_and_render(token, tid, {"report": "trial_balance"})
        tb_artifact = tb_completed["artifact"]
        assert tb_artifact is not None
        _, _, tb_body = await download(token, tb_artifact["csv_download"])
        balances = _tb_balances(parse_csv(tb_body))
        debits, credits = balances["liability.unclaimed_dividends"]
        assert (debits, credits) == (Decimal("0"), Decimal("300.00"))
        debits, credits = balances["expense.dividends"]
        # H1: dividend expense == paid (600.00) + unclaimed (300.00).
        assert debits - credits == Decimal("900.00")

        _, sasra_completed = await request_and_render(token, tid, {"report": "sasra_return"})
        sasra_artifact = sasra_completed["artifact"]
        assert sasra_artifact is not None
        _, _, sasra_body = await download(token, sasra_artifact["csv_download"])
        lines = {row[1]: row[3] for row in parse_csv(sasra_body)[1:]}
        assert lines["A100"] == "12000.00"
        assert lines["L200"] == "300.00"
        assert lines["E100"] == "12600.00"
        assert lines["X200"] == "900.00"
        assert lines["CHK1"] == "0.00"

    asyncio.run(run())


def test_h5_period_semantics_are_inclusive_calendar_dates() -> None:
    """HAND-COMPUTED: accruals of 100.00 (2026-01-15), 50.00
    (2026-01-31 23:00 — the last hour of the boundary day) and 75.00
    (2026-03-15). Window 2026-01-01..2026-01-31 -> income.interest
    150.00 (the boundary day is INCLUDED via the half-open
    < date_to + 1 day bound); open-ended from 2026-03-01 -> 75.00.
    Falsifiable: implement date_to as occurred_at < date_to and the
    23:00 boundary posting drops, rendering 100.00."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        for occurred_at, amount in (
            (datetime(2026, 1, 15, 12, 0, tzinfo=UTC), "100.00"),
            (datetime(2026, 1, 31, 23, 0, tzinfo=UTC), "50.00"),
            (datetime(2026, 3, 15, 12, 0, tzinfo=UTC), "75.00"),
        ):
            async with tenant_session(factory(), tid) as session:
                await post_loan_interest_accrual(
                    session, tid, mid, Decimal(amount), occurred_at=occurred_at
                )

        january = await _render_rows(
            token,
            tid,
            {
                "report": "income_statement",
                "filters": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
            },
        )
        assert january[1] == ["income", "income.interest", "150.00"]
        assert january[-1] == ["net", "NET SURPLUS", "150.00"]

        from_march = await _render_rows(
            token,
            tid,
            {"report": "income_statement", "filters": {"date_from": "2026-03-01"}},
        )
        assert from_march[1] == ["income", "income.interest", "75.00"]
        assert from_march[-1] == ["net", "NET SURPLUS", "75.00"]

    asyncio.run(run())


def test_h5_reversal_rows_net_out_of_every_aggregate() -> None:
    """Reversal-aware (H5): a reversed 150.00 accrual leaves
    income.interest at 0.00 — the reversing entry posts the SAME
    account on the OPPOSITE side and the credit-minus-debit aggregate
    nets it exactly. HAND-COMPUTED: 150.00 CR + 150.00 DR -> 0.00; the
    line still renders (activity exists) at the canonical 0.00.
    Falsifiable: aggregate only the credit legs (or exclude
    reversal_of_id rows from one side only) and the line shows 150.00.
    """

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            posting = await post_loan_interest_accrual(session, tid, mid, Decimal("150.00"))
        async with tenant_session(factory(), tid) as session:
            await post_reversal(session, tid, posting.txn_id)

        rows = await _render_rows(token, tid, {"report": "income_statement"})
        assert rows[1:] == [
            ["income", "income.interest", "0.00"],
            ["income", "TOTAL INCOME", "0.00"],
            ["expense", "TOTAL EXPENSE", "0.00"],
            ["net", "NET SURPLUS", "0.00"],
        ]

    asyncio.run(run())


def test_h1_unknown_ledger_account_fails_the_job_loudly() -> None:
    """The classic regulator-return defect, blocked: a posting to an
    account outside the code-owned chart map makes the income
    statement job FAIL (status 'failed', audited category, no
    artifact) instead of silently dropping the line. Falsifiable:
    swallow the ValueError in _build_income_statement (skip unknown
    accounts) and the job completes, failing every assert below."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        txn_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, tenant_id, txn_ref, type, amount, channel) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), 'MP-ZZ0001', "
                    "'deposit', '10.00', 'bank')"
                ),
                {"id": str(txn_id), "tid": str(tid)},
            )
            # Balanced legs (0004 trigger): the novel account against
            # the mapped suspense account.
            for account, side in (("zz.novel", "debit"), ("suspense", "credit")):
                await session.execute(
                    text(
                        "INSERT INTO ledger_entries "
                        "(id, tenant_id, transaction_id, account, side, amount) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:txn AS uuid), "
                        ":account, :side, '10.00')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "txn": str(txn_id),
                        "account": account,
                        "side": side,
                    },
                )

        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post(
                "/exports", json={"report": "income_statement"}, headers=headers
            )
            assert created.status_code == 201
            export_id = created.json()["id"]
        summary = await drain_export_queue(tid)
        assert summary.completed == 0
        assert summary.failed == 1

        async with api_client() as client:
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
        assert fetched.json()["status"] == "failed"
        assert fetched.json()["artifact"] is None
        assert await count(tid, "SELECT count(*) FROM export_artifacts") == 0
        # Least disclosure: the failure audit carries the sanitized
        # category only — never figures, never the raw row.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.failed' "
                "AND after->>'category' = 'validation_error'",
            )
            == 1
        )

    asyncio.run(run())
