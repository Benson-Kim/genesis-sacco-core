"""Issue #16: the P12 exit statement rides the P13 export path (blocker k).

Full end-to-end: exit request -> committee quorum -> atomic settlement
-> CSV + PDF export of the SAME document the canonical JSON endpoint
serves. Every settlement figure is hand-computed in comments.
"""

import asyncio
import csv
import io
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, drain_export_queue, seed_actor, seed_member
from genesis.application import member_exits as exits_service
from genesis.domain.committee import Vote
from genesis.domain.exits import ExitStatus
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _configure_fee(tid: uuid.UUID, fee: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenant_settings "
                "(tenant_id, deposit_interest_annual_rate_pct, exit_fee) "
                "VALUES (CAST(:tid AS uuid), '8.00', :fee) "
                "ON CONFLICT (tenant_id) DO UPDATE SET exit_fee = EXCLUDED.exit_fee"
            ),
            {"tid": str(tid), "fee": fee},
        )


async def _settled_exit(tid: uuid.UUID, requester: uuid.UUID, mid: uuid.UUID) -> uuid.UUID:
    """Request -> two-voter quorum -> settlement (P12 machinery, reused)."""
    async with tenant_session(factory(), tid) as session:
        record = await exits_service.request_exit(session, tid, requester, mid)
    exit_id = record.id
    voter_1, _ = await add_user(tid, "System Admin")
    voter_2, _ = await add_user(tid, "System Admin")
    async with tenant_session(factory(), tid) as session:
        await exits_service.cast_exit_vote(session, tid, voter_1, exit_id, Vote.APPROVE)
    async with tenant_session(factory(), tid) as session:
        tally = await exits_service.cast_exit_vote(session, tid, voter_2, exit_id, Vote.APPROVE)
    assert tally.status is ExitStatus.APPROVED
    async with tenant_session(factory(), tid) as session:
        approved = await exits_service.get_exit(session, tid, exit_id)
        await exits_service.post_settlement(
            session, tid, voter_1, exit_id, version=approved.version, channel=Channel.BANK
        )
    return exit_id


def test_exit_statement_exports_as_csv_and_pdf_end_to_end() -> None:
    async def run() -> None:
        tid, uid, token = await seed_actor()
        await _configure_fee(tid, "250.00")
        mid = await seed_member(tid, name="Departing Member", deposit="20000.00", shares="5000.00")
        exit_id = await _settled_exit(tid, uid, mid)
        headers = {"authorization": f"Bearer {token}"}

        async with api_client() as client:
            # The JSON endpoint stays the canonical data source (#16).
            canonical = await client.get(f"/member-exits/{exit_id}/statement", headers=headers)
            assert canonical.status_code == 200
            doc = canonical.json()

            created = await client.post(
                "/exports",
                json={"report": "member_exit_statement", "filters": {"exit_id": str(exit_id)}},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            export_id = created.json()["id"]
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
            artifact = fetched.json()["artifact"]
            assert artifact is not None
            # Hand-computed: the statement is ONE document row.
            assert artifact["row_count"] == 1
            assert artifact["truncated"] is False

            csv_response = await client.get(artifact["csv_download"], headers=headers)
            assert csv_response.status_code == 200
            assert csv_response.headers["x-export-truncated"] == "false"
            pdf_response = await client.get(artifact["pdf_download"], headers=headers)
            assert pdf_response.status_code == 200

        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        assert len(rows) == 2
        record = dict(zip(rows[0], rows[1], strict=True))
        # Hand-computed settlement: shares 5000 + deposits 20000 =
        # equity 25000; no loans; fee 250 (tenant_settings only);
        # net payable = 25000 - 0 - 250 = 24750. First WD- posting in
        # the tenant -> WD-000001.
        assert record["Member"] == "Departing Member"
        assert record["Exit Status"] == "settled"
        assert record["Shares"] == "5000.00"
        assert record["Deposits"] == "20000.00"
        assert record["Equity"] == "25000.00"
        assert record["Loan Balance"] == "0.00"
        assert record["Fees"] == "250.00"
        assert record["Net Payable"] == "24750.00"
        assert record["Settlement Ref"] == "WD-000001"

        # The export renders the SAME document the JSON endpoint serves
        # (issue #16: one canonical source, two representations).
        assert record["Shares"] == doc["shares_amount"]
        assert record["Deposits"] == doc["deposits_amount"]
        assert record["Net Payable"] == doc["net_payable"]
        assert record["Settlement Ref"] == doc["settlement_txn_ref"]

        # PDF carries the same statement (record-style layout).
        pdf = pdf_response.content
        assert pdf.startswith(b"%PDF-1.4")
        assert b"(Net Payable: 24750.00) Tj" in pdf

    asyncio.run(run())
