"""Human loan/exit reference numbers (0048, #35) — falsifiable legs.

The loan-book and exit-statement exports printed raw row UUIDs because
no human reference existed in the schema. 0048 adds per-tenant
loans.loan_ref / member_exits.exit_ref (LN-XXXX / EX-XXXX, the
member-number format discipline), minted race-safely through the
shared P7 allocator (application/ledger.py:allocate_sequence) and
backfilled deterministically in (created_at, id) order.

Hand-computed oracles (never captured from the implementation):
  * a fresh tenant's first two disbursements mint LN-0001, LN-0002;
    its first exit request mints EX-0001 (the counter starts at 1).
  * the backfill assigns LN-0001..LN-0003 to three seeded legacy rows
    strictly in their hand-set (created_at, id) order; a second run
    changes ZERO rows (idempotency by row count); the seeded counter
    makes the NEXT live disbursement mint LN-0004 (continuation).
  * the loan-book export's first column is the human reference and no
    cell anywhere carries the loan's UUID; keyset order (created_at
    DESC, id DESC) is unchanged by the added column.

Falsifiability: drop the allocator call and FM-R1/R3 fail on NULL;
break the backfill's ORDER BY and FM-R4 fails on the swapped
references; re-point the export cell back at l.id and FM-R5 fails on
the UUID-shaped first column.
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import drain_export_queue, seed_actor, seed_member_no
from genesis.application.ledger import disburse_loan
from genesis.application.member_exits import request_exit
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_LOAN_REF_RE = re.compile(r"^LN-\d{4,}$")
_EXIT_REF_RE = re.compile(r"^EX-\d{4,}$")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _migration_0048() -> ModuleType:
    """The REAL 0048 module (its statements are the test subject)."""
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    file = path / "0048_loan_exit_human_refs.py"
    spec = importlib.util.spec_from_file_location("migration_0048_human_refs", file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed_borrower(tid: uuid.UUID, member_no: str, name: str) -> uuid.UUID:
    """Member + deposit account satisfying the multiplier gate."""
    mid = await seed_member_no(tid, member_no, name=name)
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '100000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
    return mid


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Ref Product {pid.hex[:6]}"},
        )
    return pid


async def _seed_approved_application(
    tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID, amount: str = "24000.00"
) -> uuid.UUID:
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, stage) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, 12.00, 'approved')"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(pid),
                "amount": amount,
            },
        )
    return app_id


async def _disburse(tid: uuid.UUID, app_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    async with tenant_session(factory(), tid) as session:
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id, result.loan_ref


def test_fm_r1_disbursements_mint_sequential_refs_from_one() -> None:
    """FM-R1: a fresh tenant's first two disbursements mint LN-0001
    then LN-0002 (hand-computed: the per-tenant counter starts at 1),
    persisted on the row AND returned on the disbursement result."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await _seed_borrower(tid, "GP-0301", "Ref Oracle Borrower")
        pid = await _seed_product(tid)
        app_1 = await _seed_approved_application(tid, mid, pid)
        app_2 = await _seed_approved_application(tid, mid, pid)
        loan_1, ref_1 = await _disburse(tid, app_1)
        loan_2, ref_2 = await _disburse(tid, app_2)
        assert ref_1 == "LN-0001"
        assert ref_2 == "LN-0002"
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, loan_ref FROM loans "
                        "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY created_at, id"
                    ),
                    {"tid": str(tid)},
                )
            ).all()
        stored = {uuid.UUID(str(r[0])): str(r[1]) for r in rows}
        assert stored[loan_1] == "LN-0001"
        assert stored[loan_2] == "LN-0002"

    asyncio.run(run())


def test_fm_r2_concurrent_disbursements_never_collide() -> None:
    """FM-R2: two CONCURRENT disbursements in one tenant mint DISTINCT
    references (the advisory lock serialises the counter; the partial
    UNIQUE uq_loans_loan_ref is the schema-level proof, asserted
    present). Falsifiable: replace the allocator with SELECT max()+1
    and this leg (or the UNIQUE net) fails."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await _seed_borrower(tid, "GP-0302", "Race Borrower")
        pid = await _seed_product(tid)
        app_1 = await _seed_approved_application(tid, mid, pid)
        app_2 = await _seed_approved_application(tid, mid, pid)

        results = await asyncio.gather(_disburse(tid, app_1), _disburse(tid, app_2))
        refs = {ref for _, ref in results}
        assert len(refs) == 2  # never a collision
        assert refs == {"LN-0001", "LN-0002"}  # counter handed out both

        # The UNIQUE net exists exactly as declared (tenant-scoped,
        # partial on NOT NULL) — the database-level guarantee.
        async with tenant_session(factory(), tid) as session:
            indexdef = (
                await session.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_loans_loan_ref'")
                )
            ).scalar_one()
        assert "UNIQUE" in str(indexdef)
        assert "tenant_id" in str(indexdef)

    asyncio.run(run())


def test_fm_r3_exit_requests_mint_sequential_exit_refs() -> None:
    """FM-R3: the first exit request in a fresh tenant mints EX-0001;
    two CONCURRENT requests (distinct members) mint DISTINCT refs."""

    async def run() -> None:
        tid, actor, _ = await seed_actor()
        mid_1 = await _seed_borrower(tid, "GP-0303", "Exit Oracle One")
        mid_2 = await _seed_borrower(tid, "GP-0304", "Exit Oracle Two")

        async def request(mid: uuid.UUID) -> str | None:
            async with tenant_session(factory(), tid) as session:
                record = await request_exit(session, tid, actor, mid)
            return record.exit_ref

        refs = set(await asyncio.gather(request(mid_1), request(mid_2)))
        assert refs == {"EX-0001", "EX-0002"}  # distinct, from one
        async with tenant_session(factory(), tid) as session:
            indexdef = (
                await session.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_member_exits_exit_ref'"
                    )
                )
            ).scalar_one()
        assert "UNIQUE" in str(indexdef)

    asyncio.run(run())


def test_fm_r4_backfill_is_deterministic_idempotent_and_continues() -> None:
    """FM-R4: the REAL 0048 backfill against seeded legacy rows.

    Three raw loans with hand-set created_at (t0 < t1 < t2, deliberately
    INSERTED out of order) backfill to LN-0001/0002/0003 strictly in
    (created_at, id) order. Second run: ZERO rows change (row-count
    idempotency). Counter seeding: the next LIVE disbursement mints
    LN-0004 — the allocator continues after the backfill."""

    async def run() -> None:
        migration = _migration_0048()
        tid, _, _ = await seed_actor()
        mid = await _seed_borrower(tid, "GP-0305", "Backfill Borrower")
        pid = await _seed_product(tid)
        base = datetime.now(UTC) - timedelta(days=30)
        # Insert out of creation order: t2 first, then t0, then t1.
        stamps = [base + timedelta(days=2), base, base + timedelta(days=1)]
        loan_ids: list[uuid.UUID] = []
        async with tenant_session(factory(), tid) as session:
            for ts in stamps:
                app_id = uuid.uuid4()
                loan_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO loan_applications "
                        "(id, tenant_id, member_id, product_id, amount, term_months, "
                        " rate_pct, stage) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                        "CAST(:pid AS uuid), 1000.00, 12, 12.00, 'disbursed')"
                    ),
                    {"id": str(app_id), "tid": str(tid), "mid": str(mid), "pid": str(pid)},
                )
                await session.execute(
                    text(
                        "INSERT INTO loans "
                        "(id, tenant_id, application_id, member_id, product_id, "
                        " principal, balance, rate_pct, term_months, created_at) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:app AS uuid), "
                        "CAST(:mid AS uuid), CAST(:pid AS uuid), "
                        "1000.00, 1000.00, '12.00', 12, :ts)"
                    ),
                    {
                        "id": str(loan_id),
                        "tid": str(tid),
                        "app": str(app_id),
                        "mid": str(mid),
                        "pid": str(pid),
                        "ts": ts,
                    },
                )
                loan_ids.append(loan_id)

        async with tenant_session(factory(), tid) as session:
            first = await session.execute(text(migration._UP_BACKFILL_LOANS))
            assert int(first.rowcount or 0) == 3  # every legacy row, once
            second = await session.execute(text(migration._UP_BACKFILL_LOANS))
            assert int(second.rowcount or 0) == 0  # idempotent re-run
            await session.execute(text(migration._UP_SEED_COUNTERS))

        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, loan_ref FROM loans "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "ORDER BY created_at, id"
                    ),
                    {"tid": str(tid)},
                )
            ).all()
        by_id = {uuid.UUID(str(r[0])): str(r[1]) for r in rows}
        # Hand-computed: t0 row (inserted second) gets LN-0001, t1 row
        # (inserted third) LN-0002, t2 row (inserted first) LN-0003.
        assert by_id[loan_ids[1]] == "LN-0001"
        assert by_id[loan_ids[2]] == "LN-0002"
        assert by_id[loan_ids[0]] == "LN-0003"

        # Continuation: the counter was seeded to 3, so the next LIVE
        # disbursement mints LN-0004 — never a UNIQUE collision.
        app_live = await _seed_approved_application(tid, mid, pid)
        _, live_ref = await _disburse(tid, app_live)
        assert live_ref == "LN-0004"

    asyncio.run(run())


def test_fm_r5_loan_book_export_prints_the_ref_and_no_raw_id() -> None:
    """FM-R5: the loan-book CSV's first column is 'Loan Ref' with the
    minted LN- reference; NO cell anywhere carries a UUID; keyset
    order (created_at DESC) is unchanged — the newer loan's row
    precedes the older one's."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await _seed_borrower(tid, "GP-0306", "Export Borrower")
        pid = await _seed_product(tid)
        app_1 = await _seed_approved_application(tid, mid, pid)
        app_2 = await _seed_approved_application(tid, mid, pid)
        loan_1, ref_1 = await _disburse(tid, app_1)  # LN-0001
        loan_2, ref_2 = await _disburse(tid, app_2)  # LN-0002
        # Deterministic keyset order: backdate loan_1 one day.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET created_at = created_at - interval '1 day' "
                    "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"lid": str(loan_1), "tid": str(tid)},
            )

        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post("/exports", json={"report": "loan_book"}, headers=headers)
            assert created.status_code == 201, created.text
            export_id = created.json()["id"]
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
            assert fetched.status_code == 200
            artifact = fetched.json()["artifact"]
            assert artifact is not None
            csv_response = await client.get(artifact["csv_download"], headers=headers)
            assert csv_response.status_code == 200

        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        assert rows[0][0] == "Loan Ref"  # the raw-id column is GONE
        assert len(rows) == 3
        # Keyset order untouched: newest first — loan_2 (today) before
        # the backdated loan_1.
        assert rows[1][0] == ref_2
        assert rows[2][0] == ref_1
        for row in rows[1:]:
            assert _LOAN_REF_RE.match(row[0])
            for cell in row:
                assert not _UUID_RE.search(cell)  # no raw id anywhere
        assert str(loan_1) not in csv_response.content.decode("utf-8")
        assert str(loan_2) not in csv_response.content.decode("utf-8")

    asyncio.run(run())


def test_fm_r6_exit_statement_export_prints_the_exit_ref() -> None:
    """FM-R6: the exit-statement CSV carries 'Exit Ref' = EX-0001 and
    no UUID-shaped cell (the raw exit id dropped off the printed
    surface; member/txn ids never rode it)."""

    async def run() -> None:
        tid, actor, token = await seed_actor()
        mid = await _seed_borrower(tid, "GP-0307", "Exit Export Member")
        async with tenant_session(factory(), tid) as session:
            record = await request_exit(session, tid, actor, mid)
        assert record.exit_ref == "EX-0001"

        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post(
                "/exports",
                json={
                    "report": "member_exit_statement",
                    "filters": {"exit_id": str(record.id)},
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            export_id = created.json()["id"]
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
            artifact = fetched.json()["artifact"]
            assert artifact is not None
            csv_response = await client.get(artifact["csv_download"], headers=headers)
            assert csv_response.status_code == 200

        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        record_map = dict(zip(rows[0], rows[1], strict=True))
        assert "Exit" not in record_map  # the raw-id column is GONE
        assert record_map["Exit Ref"] == "EX-0001"
        assert _EXIT_REF_RE.match(record_map["Exit Ref"])
        for cell in rows[1]:
            assert not _UUID_RE.search(cell)
        # The JSON statement serves the SAME reference (one document,
        # two representations).
        async with api_client() as client:
            doc = await client.get(f"/member-exits/{record.id}/statement", headers=headers)
        assert doc.status_code == 200
        assert doc.json()["exit_ref"] == "EX-0001"

    asyncio.run(run())


def test_fm_r7_backfill_exits_deterministic() -> None:
    """FM-R4 mirror for exits: two raw legacy exits backfill to
    EX-0001/EX-0002 in (created_at, id) order; re-run changes zero."""

    async def run() -> None:
        migration = _migration_0048()
        tid, _, _ = await seed_actor()
        mid_1 = await _seed_borrower(tid, "GP-0308", "Legacy Exit One")
        mid_2 = await _seed_borrower(tid, "GP-0309", "Legacy Exit Two")
        base = datetime.now(UTC) - timedelta(days=10)
        ids: list[uuid.UUID] = []
        async with tenant_session(factory(), tid) as session:
            # Inserted newest-first; backfill must order by created_at.
            for mid, ts in ((mid_1, base + timedelta(days=1)), (mid_2, base)):
                exit_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO member_exits "
                        "(id, tenant_id, member_id, status, shares_amount, "
                        " deposits_amount, loan_balance, fees, net_payable, created_at) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                        "'requested', 0.00, :dep, 0.00, 0.00, :net, :ts)"
                    ),
                    {
                        "id": str(exit_id),
                        "tid": str(tid),
                        "mid": str(mid),
                        "dep": str(Decimal("100.00")),
                        "net": str(Decimal("100.00")),
                        "ts": ts,
                    },
                )
                ids.append(exit_id)

        async with tenant_session(factory(), tid) as session:
            first = await session.execute(text(migration._UP_BACKFILL_EXITS))
            assert int(first.rowcount or 0) == 2
            second = await session.execute(text(migration._UP_BACKFILL_EXITS))
            assert int(second.rowcount or 0) == 0

        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, exit_ref FROM member_exits "
                        "WHERE tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"tid": str(tid)},
                )
            ).all()
        by_id = {uuid.UUID(str(r[0])): str(r[1]) for r in rows}
        # Hand-computed: mid_2's exit (base, older) gets EX-0001;
        # mid_1's (base+1d) gets EX-0002.
        assert by_id[ids[1]] == "EX-0001"
        assert by_id[ids[0]] == "EX-0002"

    asyncio.run(run())
