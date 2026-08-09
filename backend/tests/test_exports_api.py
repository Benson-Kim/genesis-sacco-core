"""Export API tests: authz, allow-lists, tokens, audit, idempotency (P13).

Idempotency and audit guarantees are proven by SIDE-EFFECT ROW COUNTS
(hand-computed in comments), never by return values alone
(MASTER_PROMPT section 4; P13 blockers f, g).
"""

import asyncio
import csv
import io
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, count, drain_export_queue, seed_actor, seed_member
from genesis.application import exports as exports_service
from genesis.application import transactions as txn_service
from genesis.domain.ledger import Channel
from genesis.errors import NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_member_with_deposit(tid: uuid.UUID, *, name: str = "Api Member") -> uuid.UUID:
    mid = await seed_member(tid, name=name)
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.MPESA
        )
    return mid


def _statement_body(mid: uuid.UUID) -> dict[str, object]:
    return {"report": "member_statement", "filters": {"member_id": str(mid)}}


# ---------------------------------------------------------------------------
# Permissions (P13 blocker c: RequirePermission per the P4 matrix)
# ---------------------------------------------------------------------------


def test_export_routes_require_reports_view() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid)
        _, teller_token = await add_user(tid, "Teller")  # reports:view = False
        _, committee_token = await add_user(tid, "Credit Committee")  # reports:view = True
        async with api_client() as client:
            body = _statement_body(mid)
            refused = await client.post(
                "/exports", json=body, headers={"authorization": f"Bearer {teller_token}"}
            )
            assert refused.status_code == 403
            unauthenticated = await client.post("/exports", json=body)
            assert unauthenticated.status_code == 401
            allowed = await client.post(
                "/exports", json=body, headers={"authorization": f"Bearer {committee_token}"}
            )
            assert allowed.status_code == 201
            jobs_refused = await client.post(
                "/jobs/exports", headers={"authorization": f"Bearer {teller_token}"}
            )
            assert jobs_refused.status_code == 403
            download_refused = await client.get(
                "/exports/downloads/some-token",
                headers={"authorization": f"Bearer {teller_token}"},
            )
            assert download_refused.status_code == 403

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Column allow-lists (P13 blocker e)
# ---------------------------------------------------------------------------


def test_pii_columns_require_members_view() -> None:
    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid = await _seed_member_with_deposit(tid, name="Secret Name")
        # Credit Committee: reports:view yes, members:view NO (P4 matrix).
        _, committee_token = await add_user(tid, "Credit Committee")
        headers = {"authorization": f"Bearer {committee_token}"}
        async with api_client() as client:
            created = await client.post("/exports", json=_statement_body(mid), headers=headers)
            assert created.status_code == 201
            export = created.json()
            # The allow-list is resolved and frozen server-side.
            assert "member_name" not in export["allowed_columns"]
            assert "member_no" in export["allowed_columns"]
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export['id']}", headers=headers)
            artifact = fetched.json()["artifact"]
            downloaded = await client.get(artifact["csv_download"], headers=headers)
            assert downloaded.status_code == 200
        rows = list(csv.reader(io.StringIO(downloaded.content.decode("utf-8"))))
        # Hand-computed: 7 columns (the pii "Member" column removed).
        assert rows[0] == [
            "Member No",
            "Date",
            "Reference",
            "Type",
            "Debit",
            "Credit",
            "Balance",
        ]
        assert all("Secret Name" not in cell for row in rows for cell in row)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Requester-only access, token expiry, cross-tenant isolation
# ---------------------------------------------------------------------------


def test_status_and_download_are_requester_only() -> None:
    async def run() -> None:
        tid, _, owner_token = await seed_actor()
        mid = await _seed_member_with_deposit(tid)
        # Same tenant, same role privileges, DIFFERENT user: the
        # allow-list was resolved for the requester, so anyone else is
        # told not-found (least disclosure).
        _, other_token = await add_user(tid, "System Admin")
        async with api_client() as client:
            created = await client.post(
                "/exports",
                json=_statement_body(mid),
                headers={"authorization": f"Bearer {owner_token}"},
            )
            export_id = created.json()["id"]
            await drain_export_queue(tid)
            owner_view = await client.get(
                f"/exports/{export_id}", headers={"authorization": f"Bearer {owner_token}"}
            )
            assert owner_view.status_code == 200
            token_path = owner_view.json()["artifact"]["csv_download"]

            other_view = await client.get(
                f"/exports/{export_id}", headers={"authorization": f"Bearer {other_token}"}
            )
            assert other_view.status_code == 404
            other_download = await client.get(
                token_path, headers={"authorization": f"Bearer {other_token}"}
            )
            assert other_download.status_code == 404
            owner_download = await client.get(
                token_path, headers={"authorization": f"Bearer {owner_token}"}
            )
            assert owner_download.status_code == 200

    asyncio.run(run())


def test_expired_and_unknown_tokens_are_not_found() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await _seed_member_with_deposit(tid)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post("/exports", json=_statement_body(mid), headers=headers)
            export_id = created.json()["id"]
            await drain_export_queue(tid)
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
            token_path = fetched.json()["artifact"]["csv_download"]

            unknown = await client.get(f"/exports/downloads/{uuid.uuid4().hex}", headers=headers)
            assert unknown.status_code == 404

            live = await client.get(token_path, headers=headers)
            assert live.status_code == 200

            # Expire the link (download links expire: P13 blocker e).
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE export_artifacts "
                        "SET expires_at = now() - interval '1 hour' "
                        "WHERE export_id = CAST(:eid AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"eid": export_id, "tid": str(tid)},
                )
            expired = await client.get(token_path, headers=headers)
            assert expired.status_code == 404

    asyncio.run(run())


def test_cross_tenant_token_is_not_found() -> None:
    async def run() -> None:
        tid_a, _, token_a = await seed_actor()
        mid = await _seed_member_with_deposit(tid_a)
        _, _, token_b = await seed_actor()  # different tenant
        async with api_client() as client:
            created = await client.post(
                "/exports",
                json=_statement_body(mid),
                headers={"authorization": f"Bearer {token_a}"},
            )
            export_id = created.json()["id"]
            await drain_export_queue(tid_a)
            fetched = await client.get(
                f"/exports/{export_id}", headers={"authorization": f"Bearer {token_a}"}
            )
            token_path = fetched.json()["artifact"]["csv_download"]

            foreign_status = await client.get(
                f"/exports/{export_id}", headers={"authorization": f"Bearer {token_b}"}
            )
            assert foreign_status.status_code == 404
            foreign_download = await client.get(
                token_path, headers={"authorization": f"Bearer {token_b}"}
            )
            assert foreign_download.status_code == 404

    asyncio.run(run())


def test_explicit_tenant_predicates_on_export_reads() -> None:
    """Issue #17 falsifiability pattern: the session is opened AS the
    row's own tenant (RLS satisfied), the service is called with a
    DIFFERENT tenant argument — only the explicit bound tenant_id
    predicate can refuse the row. Remove it and this test fails."""

    async def run() -> None:
        tid, uid, token = await seed_actor()
        mid = await _seed_member_with_deposit(tid)
        async with api_client() as client:
            created = await client.post(
                "/exports",
                json=_statement_body(mid),
                headers={"authorization": f"Bearer {token}"},
            )
            export_id = uuid.UUID(created.json()["id"])
        await drain_export_queue(tid)
        other_tenant = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            info = await exports_service.get_artifact_info(session, tid, export_id)
            assert info is not None
            with pytest.raises(NotFoundError):
                await exports_service.get_export(session, other_tenant, export_id, requester_id=uid)
            with pytest.raises(NotFoundError):
                await exports_service.download_artifact(session, other_tenant, uid, info.csv_token)
            assert (
                await exports_service.get_artifact_info(session, other_tenant, export_id)
            ) is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Audit: the export control (P13 blocker f)
# ---------------------------------------------------------------------------


def test_every_export_step_writes_an_audit_row() -> None:
    async def run() -> None:
        tid, uid, token = await seed_actor()
        mid = await _seed_member_with_deposit(tid)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            created = await client.post("/exports", json=_statement_body(mid), headers=headers)
            export_id = created.json()["id"]
            await drain_export_queue(tid)
            fetched = await client.get(f"/exports/{export_id}", headers=headers)
            token_path = fetched.json()["artifact"]["csv_download"]
            downloaded = await client.get(token_path, headers=headers)
            assert downloaded.status_code == 200

        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT action, actor_id, after FROM audit_log "
                        "WHERE entity = 'exports' AND entity_id = :eid ORDER BY at"
                    ),
                    {"eid": export_id},
                )
            ).all()
        actions = [str(r[0]) for r in rows]
        # Hand-computed trail: request -> render -> download.
        assert actions == ["export.request", "export.completed", "export.downloaded"]
        completed = rows[1]
        assert uuid.UUID(str(completed[1])) == uid  # who
        after = dict(completed[2])
        # What scope: report, filters, row count, truncation (blocker f).
        assert after["report"] == "member_statement"
        assert after["filters"] == {"member_id": str(mid)}
        assert after["row_count"] == 1
        assert after["truncated"] is False
        assert after["row_limit"] == 10000

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Idempotency by side-effect row counts (P13 blocker g)
# ---------------------------------------------------------------------------


def test_duplicate_export_request_produces_one_of_everything() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await _seed_member_with_deposit(tid)
        headers = {
            "authorization": f"Bearer {token}",
            "Idempotency-Key": f"exp-{uuid.uuid4().hex}",
        }
        async with api_client() as client:
            first = await client.post("/exports", json=_statement_body(mid), headers=headers)
            second = await client.post("/exports", json=_statement_body(mid), headers=headers)
            assert first.status_code == 201
            assert second.status_code == 201
            assert second.headers.get("idempotency-replayed") == "true"
            assert first.json()["id"] == second.json()["id"]

        # Drain the queue TWICE: the second cycle must find nothing.
        await drain_export_queue(tid)
        summary = await drain_export_queue(tid)
        assert summary.completed == 0

        # Hand-computed side-effect counts for ONE export end to end:
        # 1 exports row, 1 artifact (DB-UNIQUE on export_id), 1
        # export.request audit, 1 export.completed audit, 1
        # export.requested outbox event, 1 export.completed outbox
        # event. Return values alone are never trusted (gate 1.4).
        assert await count(tid, "SELECT count(*) FROM exports") == 1
        assert await count(tid, "SELECT count(*) FROM export_artifacts") == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.request'",
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'export.completed'",
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'export.requested'",
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'export.completed'",
            )
            == 1
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Filter validation (P13 blocker a: nothing beyond declared ids/dates)
# ---------------------------------------------------------------------------


def test_filter_validation_rejects_bad_scopes() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            # Required filter missing.
            missing = await client.post(
                "/exports", json={"report": "member_statement"}, headers=headers
            )
            assert missing.status_code == 400
            # Filter not declared by the report.
            extra = await client.post(
                "/exports",
                json={"report": "trial_balance", "filters": {"member_id": str(mid)}},
                headers=headers,
            )
            assert extra.status_code == 400
            # Unknown body field: extra="forbid" -> 422 (blocker a).
            forbidden = await client.post(
                "/exports",
                json={"report": "trial_balance", "row_limit": 5},
                headers=headers,
            )
            assert forbidden.status_code == 422
            forbidden_filter = await client.post(
                "/exports",
                json={"report": "member_statement", "filters": {"path": "/etc/passwd"}},
                headers=headers,
            )
            assert forbidden_filter.status_code == 422
            # Unknown report enum value.
            unknown = await client.post(
                "/exports", json={"report": "all_the_data"}, headers=headers
            )
            assert unknown.status_code == 422
            # Inverted date range.
            inverted = await client.post(
                "/exports",
                json={
                    "report": "disbursement_collections",
                    "filters": {"date_from": "2026-05-01", "date_to": "2026-04-01"},
                },
                headers=headers,
            )
            assert inverted.status_code == 400
            # Nonexistent scoped row -> fast 404 at request time.
            ghost = await client.post(
                "/exports",
                json=_statement_body(uuid.uuid4()),
                headers=headers,
            )
            assert ghost.status_code == 404

    asyncio.run(run())
