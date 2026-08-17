"""End-to-end report content tests (real Postgres, RLS, worker path).

Every figure asserted below is hand-computed in comments from the
seeded postings — never read back from the implementation (anti-
reward-hacking, MASTER_PROMPT section 4).
"""

import asyncio
import csv
import io
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import drain_export_queue, seed_actor, seed_member
from genesis.application import exports as exports_service
from genesis.application import transactions as txn_service
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def request_and_render(
    token: str,
    tid: uuid.UUID,
    body: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """POST /exports, drain the queue, GET the completed job."""
    headers = {"authorization": f"Bearer {token}"}
    async with api_client() as client:
        created = await client.post("/exports", json=body, headers=headers)
        assert created.status_code == 201, created.text
        export = created.json()
        summary = await drain_export_queue(tid)
        assert summary.failed == 0
        fetched = await client.get(f"/exports/{export['id']}", headers=headers)
        assert fetched.status_code == 200
    return export, fetched.json()


async def download(token: str, path: str) -> tuple[int, dict[str, str], bytes]:
    headers = {"authorization": f"Bearer {token}"}
    async with api_client() as client:
        response = await client.get(path, headers=headers)
    return response.status_code, dict(response.headers), response.content


def parse_csv(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8"))))


async def _seed_statement_activity(tid: uuid.UUID, mid: uuid.UUID) -> None:
    """Deposit 15000 (MP-000001), share top-up 5000 (SH-000001),
    withdrawal 2000 (WD-000001), in that order."""
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, None, mid, amount=Decimal("15000.00"), channel=Channel.MPESA
        )
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_share_topup(
            session, tid, None, mid, amount=Decimal("5000.00"), channel=Channel.BANK
        )
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_withdrawal(
            session, tid, None, mid, amount=Decimal("2000.00"), channel=Channel.BANK
        )


def test_member_statement_csv_and_pdf_end_to_end() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Jane Wanjiku")
        await _seed_statement_activity(tid, mid)

        _, completed = await request_and_render(
            token, tid, {"report": "member_statement", "filters": {"member_id": str(mid)}}
        )
        assert completed["status"] == "completed"
        artifact = completed["artifact"]
        assert artifact is not None

        status, headers, body = await download(token, artifact["csv_download"])
        assert status == 200
        assert headers["x-export-truncated"] == "false"
        # Server-side cap only (P13 blocker a): the default config value.
        assert headers["x-export-limit"] == "10000"
        assert headers["content-type"].startswith("text/csv")

        rows = parse_csv(body)
        # Hand-computed: System Admin holds members:view, so all eight
        # columns render, and the running balance is
        # 0 +15000 (deposit) +5000 (share top-up) -2000 (withdrawal).
        assert rows[0] == [
            "Member No",
            "Member",
            "Date",
            "Reference",
            "Type",
            "Debit",
            "Credit",
            "Balance",
        ]
        assert len(rows) == 4
        deposit, topup, withdrawal = rows[1], rows[2], rows[3]
        assert deposit[1] == "Jane Wanjiku"
        assert deposit[3] == "MP-000001"
        assert deposit[4] == "deposit"
        assert (deposit[5], deposit[6], deposit[7]) == ("", "15000.00", "15000.00")
        assert topup[3] == "SH-000001"
        assert (topup[5], topup[6], topup[7]) == ("", "5000.00", "20000.00")
        assert withdrawal[3] == "WD-000001"
        assert (withdrawal[5], withdrawal[6], withdrawal[7]) == ("2000.00", "", "18000.00")

        status, headers, pdf = await download(token, artifact["pdf_download"])
        assert status == 200
        assert headers["content-type"] == "application/pdf"
        assert headers["x-export-truncated"] == "false"
        assert pdf.startswith(b"%PDF-1.4")
        assert b"Jane Wanjiku" in pdf

    asyncio.run(run())


def test_member_named_hyperlink_exports_inert_csv_cell() -> None:
    """MANDATORY (P13 blocker b): the injection travels the FULL path —
    member row -> report query -> worker render -> download."""

    async def run() -> None:
        payload_name = '=HYPERLINK("http://evil.example/x","Open")'
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name=payload_name)
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )

        _, completed = await request_and_render(
            token, tid, {"report": "member_statement", "filters": {"member_id": str(mid)}}
        )
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        cell = rows[1][1]
        # Hand-computed inert form: quote-prefixed, so a spreadsheet
        # treats it as text. Falsifiable: drop escape_csv_text from the
        # renderer and cell == the raw formula, failing both asserts.
        assert cell == f"'{payload_name}"
        assert not cell.startswith("=")

    asyncio.run(run())


def test_truncation_headers_when_rows_exceed_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        # Pin the SERVER-side cap (callers cannot: blocker a/d).
        monkeypatch.setattr(exports_service, "export_row_cap", lambda: 2)
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        for amount in ("10.00", "20.00", "30.00"):
            async with tenant_session(factory(), tid) as session:
                await txn_service.record_deposit(
                    session, tid, None, mid, amount=Decimal(amount), channel=Channel.MPESA
                )

        _, completed = await request_and_render(
            token, tid, {"report": "member_statement", "filters": {"member_id": str(mid)}}
        )
        artifact = completed["artifact"]
        assert artifact is not None
        assert artifact["truncated"] is True
        assert artifact["row_count"] == 2

        status, headers, body = await download(token, artifact["csv_download"])
        assert status == 200
        # Hand-computed: 3 rows seeded, cap 2 -> 2 data rows, flagged.
        assert headers["x-export-truncated"] == "true"
        assert headers["x-export-limit"] == "2"
        assert len(parse_csv(body)) == 3  # header + 2 rows

    asyncio.run(run())


def test_trial_balance_balances_with_hand_computed_totals() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        await _seed_statement_activity(tid, mid)

        _, completed = await request_and_render(token, tid, {"report": "trial_balance"})
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        # Hand-computed ledger legs:
        #   deposit 15000 mpesa   -> DR cash.mpesa       / CR member.deposits
        #   share top-up 5000 bank-> DR cash.bank        / CR member.shares
        #   withdrawal 2000 bank  -> DR member.deposits  / CR cash.bank
        # Per-account (sorted) and the grand total (DR == CR == 22000).
        assert rows[0] == ["Account", "Debits", "Credits"]
        assert rows[1] == ["cash.bank", "5000.00", "2000.00"]
        # Zeroes render as canonical cents since P13.17(b) quantized
        # the builder's cells (values unchanged, formatting only —
        # rendering is now identical whether an account was served by
        # the rolled CTE or the live scan).
        assert rows[2] == ["cash.mpesa", "15000.00", "0.00"]
        assert rows[3] == ["member.deposits", "2000.00", "15000.00"]
        assert rows[4] == ["member.shares", "0.00", "5000.00"]
        assert rows[5] == ["TOTAL", "22000.00", "22000.00"]
        assert len(rows) == 6

    asyncio.run(run())


async def _seed_loan_for_book(
    tid: uuid.UUID,
    mid: uuid.UUID,
    *,
    principal: str,
    balance: str,
    classification: str = "normal",
    provision_pct: str = "1",
    days_past_due: int = 0,
    disbursed_days_ago: int = 30,
) -> uuid.UUID:
    loan_id = uuid.uuid4()
    product_id = uuid.uuid4()
    app_id = uuid.uuid4()
    disbursed_at = datetime.now(UTC) - timedelta(days=disbursed_days_ago)
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(product_id), "tid": str(tid), "name": f"Book-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, '12.00', 'disbursed', '0.00')"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(product_id),
                "amount": principal,
            },
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, principal, "
                " balance, rate_pct, term_months, classification, days_past_due, "
                " provision_pct, disbursed_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), :principal, :balance, "
                "'12.00', 12, :cls, :dpd, :prov, :disbursed)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(app_id),
                "mid": str(mid),
                "pid": str(product_id),
                "principal": principal,
                "balance": balance,
                "cls": classification,
                "dpd": days_past_due,
                "prov": provision_pct,
                "disbursed": disbursed_at,
            },
        )
    return loan_id


def test_loan_book_report_with_classification_and_provisions() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Book Member")
        await _seed_loan_for_book(
            tid,
            mid,
            principal="10000.00",
            balance="8000.00",
            classification="substandard",
            provision_pct="25",
            days_past_due=120,
        )

        _, completed = await request_and_render(token, tid, {"report": "loan_book"})
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        assert rows[0][2] == "Member"  # members:view holder sees names
        assert len(rows) == 2
        row = rows[1]
        # Hand-computed: provision = 8000.00 * 25 / 100 = 2000.00.
        assert row[2] == "Book Member"
        assert row[3] == "10000.00"
        assert row[4] == "8000.00"
        assert row[7] == "active"
        assert row[8] == "substandard"
        assert row[9] == "120"
        assert row[10] == "25.00"
        assert row[11] == "2000.00"

    asyncio.run(run())


def test_loan_book_as_of_matches_worker_snapshot_after_queue_delay() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Delayed Book Member")
        loan_id = await _seed_loan_for_book(
            tid,
            mid,
            principal="10000.00",
            balance="8000.00",
            classification="watch",
            provision_pct="5",
            days_past_due=45,
        )

        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post("/exports", json={"report": "loan_book"}, headers=headers)
            assert created.status_code == 201, created.text
            queued = created.json()

        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET balance = '7000.00', classification = 'substandard', "
                    "days_past_due = 120, provision_pct = '25.00' "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(loan_id), "tid": str(tid)},
            )

        summary = await drain_export_queue(tid)
        assert summary.failed == 0
        async with api_client() as client:
            fetched = await client.get(f"/exports/{queued['id']}", headers=headers)
            assert fetched.status_code == 200
            completed = fetched.json()

        assert completed["as_of"] != queued["as_of"]
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        row = parse_csv(body)[1]
        assert row[4] == "7000.00"
        assert row[8] == "substandard"
        assert row[11] == "1750.00"

    asyncio.run(run())


async def _insert_txn(
    tid: uuid.UUID,
    *,
    ref: str,
    txn_type: str,
    amount: str,
    days_ago: int,
) -> uuid.UUID:
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, txn_ref, type, amount, channel, occurred_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, :type, "
                ":amount, 'bank', :ts)"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "ref": ref,
                "type": txn_type,
                "amount": amount,
                "ts": datetime.now(UTC) - timedelta(days=days_ago),
            },
        )
    return txn_id


def test_disbursement_and_collections_report() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        await _insert_txn(
            tid, ref="LN-000001", txn_type="loan_disbursement", amount="10000.00", days_ago=9
        )
        await _insert_txn(
            tid, ref="RP-000001", txn_type="loan_repayment", amount="1500.00", days_ago=5
        )
        # A deposit must NOT appear in this report.
        await _insert_txn(tid, ref="MP-000001", txn_type="deposit", amount="99.00", days_ago=4)
        # Outside the requested window: must be filtered out.
        await _insert_txn(
            tid, ref="RP-000002", txn_type="loan_repayment", amount="777.00", days_ago=60
        )

        window_from = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        _, completed = await request_and_render(
            token,
            tid,
            {
                "report": "disbursement_collections",
                "filters": {"date_from": window_from},
            },
        )
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        # Hand-computed: two qualifying rows, oldest first.
        assert rows[0] == ["Date", "Reference", "Type", "Amount", "Channel"]
        assert len(rows) == 3
        assert rows[1][1:] == ["LN-000001", "loan_disbursement", "10000.00", "bank"]
        assert rows[2][1:] == ["RP-000001", "loan_repayment", "1500.00", "bank"]

    asyncio.run(run())


def test_npl_trend_reconstructs_month_end_state() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        loan_id = await _seed_loan_for_book(
            tid,
            mid,
            principal="10000.00",
            balance="8000.00",
            disbursed_days_ago=210,
        )
        today = datetime.now(UTC).date()
        async with tenant_session(factory(), tid) as session:
            # One installment due 200 days ago, never fully paid.
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
                    "due": today - timedelta(days=200),
                },
            )
        # A 2000.00 principal repayment 10 days ago: repayments row +
        # the loans.receivable credit leg the reconstruction reads.
        txn_id = await _insert_txn(
            tid, ref="RP-000010", txn_type="loan_repayment", amount="2000.00", days_ago=10
        )
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                    "CAST(:txn AS uuid), '2000.00')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "lid": str(loan_id),
                    "txn": str(txn_id),
                },
            )
            # Balanced legs (0004 DB trigger): DR cash / CR receivable.
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

        _, completed = await request_and_render(token, tid, {"report": "npl_trend"})
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        assert rows[0] == ["Month", "Gross Outstanding", "NPL Balance", "NPL Loans", "NPL Ratio %"]
        assert len(rows) == 7  # header + 6 configured months

        # Hand-computed, first month of the series (5 calendar months
        # back; its month-end lies 120..181 days before today): the
        # loan existed (disbursed 210 days ago) and the installment due
        # 200 days ago was 19..80 days past due — arrears but NOT yet
        # NPL (> 90); no repayment yet (it happened 10 days ago), so
        # gross = 10000.00.
        first = rows[1]
        assert first[1] == "10000.00"
        # NPL balance renders canonical cents ("0.00"; value identical
        # to the pre-P13.17 "0"): the reconstruction quantizes via
        # domain.money.to_cents so the artifact is byte-identical
        # whichever path (snapshot row vs reconstruction) serves the
        # month — the disclosed formatting-only rendering delta.
        assert (first[2], first[3], first[4]) == ("0.00", "0", "0.00")

        # Hand-computed, current month (as-of today): days past due =
        # 200 > 90 -> NPL; ledger-reconstructed outstanding = 10000 -
        # 2000 = 8000.00; ratio = 8000/8000 = 100.00%.
        current = rows[6]
        assert current[1] == "8000.00"
        assert current[2] == "8000.00"
        assert current[3] == "1"
        assert current[4] == "100.00"

    asyncio.run(run())
