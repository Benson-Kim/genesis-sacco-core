"""Dividend & rebate schedule export (P13.11 on the P13 registry).

End-to-end through the REAL export path (request -> queue -> worker
snapshot render -> download), inheriting every P13 blocker: server-
resolved scope (a), formula-injection defence (b), tenant predicates
(c), keyset + row cap (d), per-role PII allow-list (e), export audit
rows (f), snapshot consistency (h), atomicity (i). Figures are
hand-computed from the seeded postings (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from typing import Any

import pytest

from db_helpers import api_client, factory
from export_helpers import add_user, count, drain_export_queue, seed_actor, seed_member
from genesis.application import exports as exports_service
from genesis.application import tenant_settings as settings_service
from genesis.application.dividends import (
    DeclarationStatus,
    cast_dividend_vote,
    declare_dividend,
    distribute_dividend,
)
from genesis.application.ledger import post_share_topup
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

FY_START = date(2025, 7, 1)
TODAY = date(2026, 8, 1)

_HEADERS = [
    "Member No",
    "Member",
    "Avg Share Balance",
    "Dividend %",
    "Dividend",
    "Avg Deposit Balance",
    "Rebate %",
    "Rebate",
    "Total",
    "Reference",
]


def _scope(tid: uuid.UUID) -> partial[Any]:
    return partial(tenant_session, factory(), tid)


async def _distributed_declaration(
    *, member_name: str = "Jane Wanjiku", extra_member: bool = False
) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID]:
    """One member, 12,000.00 shares from FY day one at 5%/2%:
    dividend = 600.00, rebate = 0. With extra_member, a second member
    holds 6,000.00 from day one -> dividend 300.00 (a 2-row schedule).
    Returns (tid, mid, token, decl_id)."""
    tid, admin_id, token = await seed_actor()
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
        await settings_service.update_settings(
            session,
            tid,
            admin_id,
            version=current.version,
            changes={
                "dividend_rate_pct": Decimal("5.00"),
                "deposit_rebate_rate_pct": Decimal("2.00"),
                "financial_year_end_month": 6,
            },
        )
    mid = await seed_member(tid, name=member_name, shares="12000.00")
    async with tenant_session(factory(), tid) as session:
        await post_share_topup(
            session,
            tid,
            mid,
            Decimal("12000.00"),
            Channel.BANK,
            occurred_at=datetime.combine(FY_START, time(12, 0), tzinfo=UTC),
        )
    if extra_member:
        mid_two = await seed_member(tid, name="Member Two", shares="6000.00")
        async with tenant_session(factory(), tid) as session:
            await post_share_topup(
                session,
                tid,
                mid_two,
                Decimal("6000.00"),
                Channel.BANK,
                occurred_at=datetime.combine(FY_START, time(12, 0), tzinfo=UTC),
            )
    record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
    for role in ("Branch Manager", "Credit Committee"):
        voter, _ = await add_user(tid, role)
        async with tenant_session(factory(), tid) as session:
            await cast_dividend_vote(session, tid, voter, record.id, Vote.APPROVE)
    result = await distribute_dividend(_scope(tid), tid, None, record.id)
    assert result.status is DeclarationStatus.DISTRIBUTED
    return tid, mid, token, record.id


async def _request_and_render(
    token: str, tid: uuid.UUID, declaration_id: uuid.UUID
) -> dict[str, Any]:
    headers = {"authorization": f"Bearer {token}"}
    async with api_client() as client:
        created = await client.post(
            "/exports",
            json={
                "report": "dividend_rebate_schedule",
                "filters": {"declaration_id": str(declaration_id)},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        export = created.json()
        summary = await drain_export_queue(tid)
        assert summary.failed == 0
        fetched = await client.get(f"/exports/{export['id']}", headers=headers)
        assert fetched.status_code == 200
        body: dict[str, Any] = fetched.json()
    return body


async def _download(token: str, path: str) -> bytes:
    async with api_client() as client:
        response = await client.get(path, headers={"authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.content


def _parse(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8"))))


def test_schedule_renders_the_hand_computed_figures() -> None:
    async def run() -> None:
        tid, _, token, decl_id = await _distributed_declaration()
        completed = await _request_and_render(token, tid, decl_id)
        assert completed["status"] == "completed"
        artifact = completed["artifact"]
        assert artifact is not None
        rows = _parse(await _download(token, artifact["csv_download"]))
        assert rows[0] == _HEADERS
        assert len(rows) == 2
        # Hand-computed: ADB 12,000.00 x 5% = 600.00 dividend; deposit
        # basis 0 -> rebate 0.00; total 600.00; the DV- posting ref.
        row = rows[1]
        assert row[1] == "Jane Wanjiku"
        assert row[2] == "12000.00"
        assert row[3] == "5.00"
        assert row[4] == "600.00"
        assert row[5] == "0.00"
        assert row[6] == "2.00"
        assert row[7] == "0.00"
        assert row[8] == "600.00"
        assert row[9] == "DV-000001"

        # Export audit rows exist (P13 blocker f: the audit IS the
        # control on the exfiltration channel).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.completed' "
                "AND after->>'report' = 'dividend_rebate_schedule'",
            )
            == 1
        )

        # Rendering an export changes NO domain state (idempotency by
        # side-effect counts): claims and postings are untouched.
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 1
        )

    asyncio.run(run())


def test_member_named_formula_exports_inert_csv_cell() -> None:
    """MANDATORY (P13 blocker b): the injection travels the FULL path —
    member row -> schedule query -> worker render -> download."""

    async def run() -> None:
        payload_name = '=HYPERLINK("http://evil.example/x","Open")'
        tid, _, token, decl_id = await _distributed_declaration(member_name=payload_name)
        completed = await _request_and_render(token, tid, decl_id)
        artifact = completed["artifact"]
        assert artifact is not None
        rows = _parse(await _download(token, artifact["csv_download"]))
        cell = rows[1][1]
        # Hand-computed inert form: quote-prefixed text. Falsifiable:
        # drop escape_csv_text from the renderer and cell == the raw
        # formula, failing both asserts.
        assert cell == f"'{payload_name}"
        assert not cell.startswith("=")

    asyncio.run(run())


def test_member_name_column_requires_members_view() -> None:
    """P13 blocker e: Credit Committee holds reports:view but NOT
    members:view -> the PII column is stripped from the frozen
    allow-list and the rendered artifact."""

    async def run() -> None:
        tid, _, _, decl_id = await _distributed_declaration(member_name="Secret Name")
        _, committee_token = await add_user(tid, "Credit Committee")
        completed = await _request_and_render(committee_token, tid, decl_id)
        assert "member_name" not in completed["allowed_columns"]
        artifact = completed["artifact"]
        assert artifact is not None
        rows = _parse(await _download(committee_token, artifact["csv_download"]))
        assert rows[0] == [h for h in _HEADERS if h != "Member"]
        assert all("Secret Name" not in cell for cell in rows[1])

    asyncio.run(run())


def test_schedule_pages_by_keyset_and_respects_the_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P13 blocker d: force single-row keyset batches through the
    engine (server-side batch size — callers cannot touch it) and
    prove the pages stitch together without gaps or repeats."""

    async def run() -> None:
        monkeypatch.setattr(exports_service, "export_batch_size", lambda: 1)
        tid, _, token, decl_id = await _distributed_declaration(extra_member=True)
        completed = await _request_and_render(token, tid, decl_id)
        artifact = completed["artifact"]
        assert artifact is not None
        # Two claims, fetched one per keyset batch (batch size 1 forces
        # the cursor path): the pages stitch with no gap and no repeat.
        assert artifact["row_count"] == 2
        assert artifact["truncated"] is False
        rows = _parse(await _download(token, artifact["csv_download"]))
        assert len(rows) == 3
        # Hand-computed: 12,000 -> 600.00 and 6,000 -> 300.00.
        by_name = {row[1]: row for row in rows[1:]}
        assert set(by_name) == {"Jane Wanjiku", "Member Two"}
        assert by_name["Jane Wanjiku"][4] == "600.00"
        assert by_name["Member Two"][4] == "300.00"

    asyncio.run(run())


def test_schedule_scope_validation_and_foreign_tenant_invisibility() -> None:
    async def run() -> None:
        _tid, _, token, decl_id = await _distributed_declaration()
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            # Missing required filter -> 4xx, nothing queued.
            missing = await client.post(
                "/exports", json={"report": "dividend_rebate_schedule"}, headers=headers
            )
            assert missing.status_code == 400
            # A filter the report does not declare -> 4xx.
            extra = await client.post(
                "/exports",
                json={
                    "report": "dividend_rebate_schedule",
                    "filters": {"declaration_id": str(decl_id), "member_id": str(uuid.uuid4())},
                },
                headers=headers,
            )
            assert extra.status_code == 400

        # A foreign tenant's requester cannot even see the declaration:
        # uniform 404 at request time (least disclosure).
        tid_b, _, token_b = await seed_actor()
        headers_b = {"authorization": f"Bearer {token_b}"}
        async with api_client() as client:
            foreign = await client.post(
                "/exports",
                json={
                    "report": "dividend_rebate_schedule",
                    "filters": {"declaration_id": str(decl_id)},
                },
                headers=headers_b,
            )
            assert foreign.status_code == 404
        assert await count(tid_b, "SELECT count(*) FROM exports") == 0

    asyncio.run(run())
