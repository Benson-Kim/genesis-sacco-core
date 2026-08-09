"""SASRA return skeleton export (P13.10 on the P13 registry).

The trial balance mapped to the regulator's line items through the
VERSIONED, code-owned mapping (genesis.domain.sasra — v1.1 rules 1/6:
callers can never supply line-item mappings; every row carries the
version tag). H1 fail-loud — a ledger account the mapping does not
cover ABORTS the job (status 'failed'): a future account can never
silently vanish from a filed return (the classic regulator-return
defect). CHK1 pins the double-entry identity (signed balances sum to
zero) on every render.

Every figure is hand-computed in comments (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import count, drain_export_queue, seed_actor, seed_member
from genesis.domain.sasra import SASRA_LINES, SASRA_RETURN_VERSION
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import (
    _seed_statement_activity,
    download,
    parse_csv,
    request_and_render,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_MONEY = re.compile(r"-?\d+\.\d{2}")

_HEADERS = ["Return Version", "Line Code", "Line Item", "Amount"]


def test_sasra_return_maps_the_trial_balance_onto_versioned_lines() -> None:
    """HAND-COMPUTED ledger legs (the P13 trial-balance seeding trio):
      deposit 15,000 mpesa   -> DR cash.mpesa      / CR member.deposits
      share top-up 5,000 bank-> DR cash.bank       / CR member.shares
      withdrawal 2,000 bank  -> DR member.deposits / CR cash.bank
    Natural-sign balances: cash.mpesa +15,000; cash.bank +3,000;
    member.deposits (CR-normal) 15,000 - 2,000 = 13,000;
    member.shares 5,000. Lines:
      A100 = 15,000 + 3,000 = 18,000.00; L100 = 13,000.00;
      E100 = 5,000.00; every other line 0.00;
      CHK1 = 18,000 - 13,000 - 5,000 = 0.00.
    Falsifiable: re-map cash.bank off A100 (or flip a side convention
    in signed_balance) and A100/CHK1 fail; strip the version column
    and the header/row pins fail."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        await _seed_statement_activity(tid, mid)

        _, completed = await request_and_render(token, tid, {"report": "sasra_return"})
        artifact = completed["artifact"]
        assert artifact is not None
        status, _, body = await download(token, artifact["csv_download"])
        assert status == 200
        rows = parse_csv(body)
        assert rows[0] == _HEADERS
        # One row per mapped line, in filing order, plus CHK1.
        assert [row[1] for row in rows[1:]] == [line.code for line in SASRA_LINES] + ["CHK1"]
        assert all(row[0] == SASRA_RETURN_VERSION for row in rows[1:])
        amounts = {row[1]: row[3] for row in rows[1:]}
        assert amounts["A100"] == "18000.00"
        assert amounts["L100"] == "13000.00"
        assert amounts["E100"] == "5000.00"
        assert amounts["CHK1"] == "0.00"
        for code, amount in amounts.items():
            # H3: canonical 2dp everywhere, including zero lines.
            assert _MONEY.fullmatch(amount), (code, amount)
            if code not in {"A100", "L100", "E100", "CHK1"}:
                assert amount == "0.00", (code, amount)

        # Export audit row for the run (P13 blocker f).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.completed' "
                "AND after->>'report' = 'sasra_return'",
            )
            == 1
        )

    asyncio.run(run())


def test_h1_unmapped_ledger_account_aborts_the_return_loudly() -> None:
    """A raw account outside the versioned mapping must FAIL the
    return render — never a return missing the account. Falsifiable:
    swallow the ValueError in _build_sasra_return (skip unmapped
    accounts) and the job completes with the account absent, failing
    the status/failed-count asserts."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        txn_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, tenant_id, txn_ref, type, amount, channel) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), 'MP-ZZ0002', "
                    "'deposit', '10.00', 'bank')"
                ),
                {"id": str(txn_id), "tid": str(tid)},
            )
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
                "/exports", json={"report": "sasra_return"}, headers=headers
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

    asyncio.run(run())
