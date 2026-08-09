"""Transactions-ledger register export (#35 item 5 — export parity).

The prototype's ONE register-level export affordance (the Transactions
ledger "Export" button) wired through the EXISTING run_export engine:
no new engine, no new wire shape beyond the declared expand-only
filter keys (txn_type/channel/direction/ref/search alongside the
existing member_id/date range). Legs:

* FM-E1 — the export request carries the register page's ACTIVE
  filters VERBATIM: the frozen job record's filters json equals the
  hand-computed payload (oracle written before the assert).
* FM-E2 — the artifact rows match the FILTERED register: seeded
  postings with hand-computed refs/amounts/directions; rows outside
  the filter never leak in. Falsifiable: drop any clause from
  _txn_ledger_filter_clauses and the excluded rows appear.
* FM-E3 — direction-filter parity with the register list (the
  _direction_clause reuse is load-bearing, reversal flip included).
* FM-E4 — an unauthorized role (Teller: transactions yes, reports NO)
  cannot export the ledger at all (403, deny-by-default).
* FM-E5 — least disclosure: a requester without members:view (Credit
  Committee) gets NO member-name column in the frozen allow-list;
  member_no (the human register number, not PII) stays.
* FM-E6 — expand-only discipline: the new keys stay DECLARED per
  report — txn_type on a report that does not declare it is a 422.

Oracles are hand-computed in comments (MASTER_PROMPT §4).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor, seed_member
from genesis.application import transactions as txn_service
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import download, parse_csv, request_and_render

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_FULL_HEADERS = [
    "Date",
    "Ref",
    "External Ref",
    "Type",
    "Channel",
    "Direction",
    "Amount",
    "Member No",
    "Member",
]


async def _seed_ledger_activity(tid: uuid.UUID, mid: uuid.UUID) -> None:
    """Three postings, in this order (refs are the tenant sequences):
    deposit 15000 mpesa (MP-000001, member-facing CREDIT), withdrawal
    2000 bank (WD-000001, DEBIT), share top-up 5000 bank (SH-000001,
    CREDIT)."""
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, None, mid, amount=Decimal("15000.00"), channel=Channel.MPESA
        )
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_withdrawal(
            session, tid, None, mid, amount=Decimal("2000.00"), channel=Channel.BANK
        )
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_share_topup(
            session, tid, None, mid, amount=Decimal("5000.00"), channel=Channel.BANK
        )


def test_active_filters_travel_verbatim_and_rows_match_filtered_register() -> None:
    """FM-E1 + FM-E2. Hand-computed: of the three seeded postings only
    the deposit is type=deposit AND channel=mpesa, so the artifact is
    the header plus EXACTLY ONE row — MP-000001 / deposit / mpesa /
    credit / 15000.00 / the member's GP number and name. Falsifiable:
    removing the type or channel clause from _txn_ledger_filter_clauses
    leaks WD-000001/SH-000001 into the rows and fails the row count."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Ledger Oracle Member", deposit="10000")
        member_no = f"GP-{mid.hex[:6].upper()}"  # the seed_member format
        await _seed_ledger_activity(tid, mid)

        # Hand-computed ACTIVE-filter payload (the register page's
        # Type + Channel selects) — must be frozen verbatim.
        active_filters = {"txn_type": "deposit", "channel": "mpesa"}
        created, completed = await request_and_render(
            token,
            tid,
            {"report": "transactions_ledger", "filters": active_filters},
        )
        assert created["filters"] == active_filters
        artifact = completed["artifact"]
        assert artifact is not None
        status_code, _, body = await download(token, artifact["csv_download"])
        assert status_code == 200
        rows = parse_csv(body)
        assert rows[0] == _FULL_HEADERS
        assert len(rows) == 2  # header + exactly the one matching row
        row = rows[1]
        assert row[1] == "MP-000001"
        assert row[2] == ""  # no external ref on the service-level seed
        assert row[3] == "deposit"
        assert row[4] == "mpesa"
        assert row[5] == "credit"
        assert row[6] == "15000.00"
        assert row[7] == member_no
        assert row[8] == "Ledger Oracle Member"

    asyncio.run(run())


def test_direction_filter_matches_the_register_semantics() -> None:
    """FM-E3. Hand-computed member-facing directions (MEMBER_DIRECTION):
    deposit CREDIT, share top-up CREDIT, withdrawal DEBIT — so
    direction=credit renders exactly two rows, newest first
    (SH-000001 posted after MP-000001). Falsifiable: replacing the
    reused _direction_clause with a plain type list breaks the
    reversal flip this clause encodes and desyncs export vs register."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Direction Oracle Member", deposit="10000")
        await _seed_ledger_activity(tid, mid)

        created, completed = await request_and_render(
            token,
            tid,
            {"report": "transactions_ledger", "filters": {"direction": "credit"}},
        )
        assert created["filters"] == {"direction": "credit"}
        artifact = completed["artifact"]
        assert artifact is not None
        status_code, _, body = await download(token, artifact["csv_download"])
        assert status_code == 200
        rows = parse_csv(body)
        assert [row[1] for row in rows[1:]] == ["SH-000001", "MP-000001"]
        assert {row[5] for row in rows[1:]} == {"credit"}

    asyncio.run(run())


def test_role_without_reports_view_cannot_export_the_ledger() -> None:
    """FM-E4: the Teller holds transactions:view (sees the register)
    but NOT reports:view — the export affordance's server-side gate
    refuses the request outright (deny-by-default; the web leg hides
    the affordance, this leg proves the API never relied on that)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        _, teller_token = await add_user(tid, "Teller")
        async with api_client() as client:
            res = await client.post(
                "/exports",
                json={"report": "transactions_ledger"},
                headers={"authorization": f"Bearer {teller_token}"},
            )
        assert res.status_code == 403, res.text

    asyncio.run(run())


def test_member_name_column_is_pii_gated_member_no_stays() -> None:
    """FM-E5: the Credit Committee holds reports:view but NOT
    members:view — its frozen allow-list drops member_name (PII)
    while member_no (the human register number) stays, and the
    rendered CSV carries no Member column. Falsifiable: marking
    member_name pii=False leaks the name into this artifact."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid, name="Pii Gated Member", deposit="10000")
        member_no = f"GP-{mid.hex[:6].upper()}"
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )
        _, committee_token = await add_user(tid, "Credit Committee")
        created, completed = await request_and_render(
            committee_token, tid, {"report": "transactions_ledger"}
        )
        assert "member_name" not in created["allowed_columns"]
        assert "member_no" in created["allowed_columns"]
        artifact = completed["artifact"]
        assert artifact is not None
        status_code, _, body = await download(committee_token, artifact["csv_download"])
        assert status_code == 200
        rows = parse_csv(body)
        assert rows[0] == _FULL_HEADERS[:-1]  # no "Member" column
        assert rows[1][7] == member_no
        assert "Pii Gated Member" not in body.decode("utf-8")

    asyncio.run(run())


def test_cross_tenant_probe_renders_zero_rows() -> None:
    """§4 adversarial: session AS tenant A (which owns a posting), the
    builder scoped with FOREIGN tenant B's id renders ZERO rows — the
    explicit bound tenant predicate on top of forced RLS. The control
    arm (tenant A's own render carries the row) keeps the zero result
    non-vacuous. Falsifiable: drop the tenant predicate from
    transactions_ledger_page_sql and the control row leaks into the
    probe."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        tid_b, _, _ = await seed_actor()
        mid = await seed_member(tid_a, name="Cross Tenant Member", deposit="10000")
        async with tenant_session(factory(), tid_a) as session:
            await txn_service.record_deposit(
                session, tid_a, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )
        as_of = datetime.now(UTC)

        async def render(probe_tid: uuid.UUID) -> list[tuple[object, ...]]:
            rendered: list[tuple[object, ...]] = []
            async with tenant_session(factory(), tid_a) as session:
                query = await REPORTS[ReportName.TRANSACTIONS_LEDGER].build(
                    session, probe_tid, ExportFilters(), as_of
                )
                await run_export(query, 100, row_cap=1000, on_batch=rendered.extend)
            return rendered

        assert await render(tid_a) != []  # control arm: never vacuous
        assert await render(tid_b) == []

    asyncio.run(run())


def test_register_filter_keys_stay_declared_per_report() -> None:
    """FM-E6: the expand-only keys belong to the reports that DECLARE
    them — txn_type on the trial balance is refused (422), exactly the
    pre-existing unknown-filter discipline. Falsifiable: widening
    every report's allowed_filters with the new keys passes this probe
    and fails the review."""

    async def run() -> None:
        _, _, token = await seed_actor()
        async with api_client() as client:
            res = await client.post(
                "/exports",
                json={"report": "trial_balance", "filters": {"txn_type": "deposit"}},
                headers={"authorization": f"Bearer {token}"},
            )
        assert res.status_code == 422, res.text

    asyncio.run(run())
