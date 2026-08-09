"""Export job runner tests: atomicity, snapshot isolation, failure path.

Kill-switch (P13 blocker i) and snapshot consistency (P13 blocker h)
are proven by side-effect row counts and hand-computed document
content, never by return values (MASTER_PROMPT section 4).
"""

import asyncio
import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import count, drain_export_queue, seed_actor, seed_member
from genesis.application import exports as exports_service
from genesis.application import transactions as txn_service
from genesis.application.exports import run_export_job
from genesis.application.ledger import post_deposit
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session, tenant_snapshot_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash after the job rendered, before commit."""


async def _request_statement_export(token: str, mid: uuid.UUID) -> tuple[str, datetime]:
    async with api_client() as client:
        created = await client.post(
            "/exports",
            json={"report": "member_statement", "filters": {"member_id": str(mid)}},
            headers={"authorization": f"Bearer {token}"},
        )
        assert created.status_code == 201, created.text
        payload = created.json()
    return payload["id"], datetime.fromisoformat(payload["as_of"])


async def _deposit(tid: uuid.UUID, mid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, None, mid, amount=Decimal(amount), channel=Channel.MPESA
        )


def test_kill_switch_mid_export_job_leaves_zero_partial_state() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        await _deposit(tid, mid, "100.00")
        export_id, _ = await _request_statement_export(token, mid)

        with pytest.raises(_KillSwitchError):
            async with tenant_snapshot_session(factory(), tid) as session:
                outcome = await run_export_job(session, tid)
                assert outcome is not None  # the job DID render
                raise _KillSwitchError()  # crash inside the transaction

        # ZERO partial state (P13 blocker i): no artifact, no claim
        # (status rolled back to 'requested'), no completion audit, no
        # completion outbox event. Hand-computed: all counts 0.
        assert await count(tid, "SELECT count(*) FROM export_artifacts") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.completed'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'export.completed'",
            )
            == 0
        )
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, version FROM exports "
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"id": export_id, "tid": str(tid)},
                )
            ).one()
        assert str(row[0]) == "requested"
        assert int(row[1]) == 1

        # The same job commits cleanly afterwards — the abort above was
        # the only reason nothing landed.
        summary = await drain_export_queue(tid)
        assert summary.completed == 1
        assert await count(tid, "SELECT count(*) FROM export_artifacts") == 1

    asyncio.run(run())


def test_export_snapshot_never_observes_mid_export_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P13 blocker h: a write committed BETWEEN two export batches, with
    occurred_at inside the export's as-of window, must not appear —
    the job renders one REPEATABLE READ snapshot. Falsifiable: run the
    job on a READ COMMITTED tenant_session (or drop the isolation
    option) and the interleaved deposit shows up as a third row with a
    drifted running balance, failing every assert below."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        await _deposit(tid, mid, "100.00")
        await _deposit(tid, mid, "200.00")
        export_id, as_of = await _request_statement_export(token, mid)

        # One row per batch -> the engine crosses batch boundaries.
        monkeypatch.setattr(exports_service, "export_batch_size", lambda: 1)
        fired = False

        async def interleave() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            # Commit a deposit whose occurred_at falls INSIDE the
            # export window (backdated 1ms before as_of) — the exact
            # shape of a settlement racing an export.
            async with tenant_session(factory(), tid) as session:
                await post_deposit(
                    session,
                    tid,
                    mid,
                    Decimal("400.00"),
                    Channel.MPESA,
                    occurred_at=as_of - timedelta(milliseconds=1),
                )

        monkeypatch.setattr(exports_service, "_between_batches", interleave)

        summary = await drain_export_queue(tid)
        assert summary.completed == 1
        assert fired, "the interleaved commit never ran; the test proved nothing"

        async with tenant_session(factory(), tid) as session:
            payload = (
                await session.execute(
                    text(
                        "SELECT csv_content FROM export_artifacts "
                        "WHERE export_id = CAST(:eid AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"eid": export_id, "tid": str(tid)},
                )
            ).scalar_one()
        rows = list(csv.reader(io.StringIO(bytes(payload).decode("utf-8"))))
        # Hand-computed snapshot: exactly the two pre-export deposits,
        # running balance 100.00 then 300.00. The 400.00 deposit is
        # real and committed — but AFTER the snapshot began.
        assert len(rows) == 3  # header + 2 rows
        assert rows[1][6] == "100.00"
        assert rows[1][7] == "100.00"
        assert rows[2][6] == "200.00"
        assert rows[2][7] == "300.00"

    asyncio.run(run())


def test_failed_job_is_marked_and_never_wedges_the_queue() -> None:
    async def run() -> None:
        tid, uid, token = await seed_actor()
        mid = await seed_member(tid)
        await _deposit(tid, mid, "50.00")

        # Simulate scope drift: a job whose exit no longer resolves
        # (inserted directly, bypassing request-time validation).
        ghost_export = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO exports "
                    "(id, tenant_id, report, filters, allowed_columns, requested_by) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                    "'member_exit_statement', CAST(:filters AS jsonb), "
                    "CAST(:cols AS jsonb), CAST(:uid AS uuid))"
                ),
                {
                    "id": str(ghost_export),
                    "tid": str(tid),
                    "filters": json.dumps({"exit_id": str(uuid.uuid4())}),
                    "cols": json.dumps(["exit_id"]),
                    "uid": str(uid),
                },
            )
        # A healthy job queued behind the poisoned one.
        healthy_export, _ = await _request_statement_export(token, mid)

        summary = await drain_export_queue(tid)
        # Hand-computed: 1 failed (ghost exit), 1 completed (healthy).
        assert summary.failed == 1
        assert summary.completed == 1

        async with tenant_session(factory(), tid) as session:
            status_rows = (
                await session.execute(
                    text(
                        "SELECT id, status FROM exports "
                        "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY created_at"
                    ),
                    {"tid": str(tid)},
                )
            ).all()
        statuses = {str(r[0]): str(r[1]) for r in status_rows}
        assert statuses[str(ghost_export)] == "failed"
        assert statuses[healthy_export] == "completed"
        # The failed job produced NO artifact and a least-disclosure
        # audit row (sanitized category only).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM export_artifacts WHERE export_id = CAST(:eid AS uuid)",
                eid=str(ghost_export),
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'export.failed' AND entity_id = :eid",
                eid=str(ghost_export),
            )
            == 1
        )

    asyncio.run(run())
