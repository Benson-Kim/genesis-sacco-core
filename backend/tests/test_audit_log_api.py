"""P13.5 audit-log viewer suite: keyset paging, filters, redaction.

Redaction falsifiability (gate 1.6, the P13 column-allow-list
precedent): a probe role holding ONLY access_control:view must see row
metadata but NEVER the before/after payloads of entities whose module
it cannot view. Remove the entitlement branch in
application/audit_log.py and these tests fail on the leaked payloads.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, unique_email
from export_helpers import seed_actor
from genesis.application.auth import AuthContext, issue_access_token
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _insert_audit_row(
    tid: uuid.UUID,
    *,
    entity: str,
    action: str,
    entity_id: str = "probe",
    actor_id: uuid.UUID | None = None,
    at: datetime | None = None,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "(tenant_id, actor_id, action, entity, entity_id, before, after, at) "
                "VALUES (CAST(:tid AS uuid), CAST(:actor AS uuid), :action, :entity, "
                ":eid, CAST(:before AS jsonb), CAST(:after AS jsonb), :at)"
            ),
            {
                "tid": str(tid),
                "actor": str(actor_id) if actor_id else None,
                "action": action,
                "entity": entity,
                "eid": entity_id,
                "before": json.dumps(before) if before is not None else None,
                "after": json.dumps(after) if after is not None else None,
                "at": at or datetime.now(UTC),
            },
        )


async def _access_only_viewer(tid: uuid.UUID) -> str:
    """User whose role can view access_control but NO other module."""
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO roles (id, tenant_id, name, is_system) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :name, false)"
            ),
            {"id": str(role_id), "tid": str(tid), "name": f"Audit Probe {role_id.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO permissions (tenant_id, role_id, module, can_view) VALUES "
                "(CAST(:tid AS uuid), CAST(:rid AS uuid), 'access_control', true)"
            ),
            {"tid": str(tid), "rid": str(role_id)},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Audit Probe', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    return issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=role_id))


def test_audit_log_keyset_pagination_never_skips_or_repeats() -> None:
    """Hand-computed oracle: 5 rows inserted at known timestamps must
    come back newest-first across pages of 2, no overlap, no gap."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        base = datetime.now(UTC)
        for i in range(5):
            await _insert_audit_row(
                tid,
                entity="members",
                action="probe.page",
                entity_id=f"row-{i}",
                at=base + timedelta(seconds=i),
            )
        headers = {"authorization": f"Bearer {token}"}
        collected: list[str] = []
        cursor: str | None = None
        async with api_client() as client:
            while True:
                params: dict[str, str] = {"limit": "2", "action": "probe.page"}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get("/audit-log", params=params, headers=headers)
                assert res.status_code == 200
                page = res.json()
                assert len(page["items"]) <= 2
                collected.extend(item["entity_id"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        # Newest first: row-4, row-3, row-2, row-1, row-0.
        assert collected == [f"row-{i}" for i in range(4, -1, -1)]

    asyncio.run(run())


def test_audit_log_filters_entity_actor_action_date() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        other_actor = uuid.uuid4()
        old = datetime.now(UTC) - timedelta(days=10)
        await _insert_audit_row(
            tid, entity="members", action="probe.a", actor_id=admin_id, entity_id="m1"
        )
        await _insert_audit_row(
            tid, entity="loans", action="probe.b", actor_id=other_actor, entity_id="l1"
        )
        await _insert_audit_row(tid, entity="members", action="probe.a", entity_id="m-old", at=old)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            res = await client.get("/audit-log", params={"entity": "loans"}, headers=headers)
            assert res.status_code == 200
            assert [i["entity_id"] for i in res.json()["items"]] == ["l1"]

            res = await client.get(
                "/audit-log", params={"actor_id": str(other_actor)}, headers=headers
            )
            assert [i["entity_id"] for i in res.json()["items"]] == ["l1"]

            res = await client.get("/audit-log", params={"action": "probe.a"}, headers=headers)
            assert [i["entity_id"] for i in res.json()["items"]] == ["m1", "m-old"]

            day = datetime.now(UTC).date().isoformat()
            res = await client.get(
                "/audit-log",
                params={"action": "probe.a", "date_from": day, "date_to": day},
                headers=headers,
            )
            assert [i["entity_id"] for i in res.json()["items"]] == ["m1"]

            old_day = old.date().isoformat()
            res = await client.get(
                "/audit-log",
                params={"action": "probe.a", "date_to": old_day},
                headers=headers,
            )
            assert [i["entity_id"] for i in res.json()["items"]] == ["m-old"]

            res = await client.get("/audit-log", params={"cursor": "garbage"}, headers=headers)
            assert res.status_code == 400

    asyncio.run(run())


def test_audit_payloads_redacted_per_role_entitlement() -> None:
    """System Admin (members:view) sees payloads; the access-only probe
    role sees metadata but redacted payloads (least disclosure)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        secret = {"name": "Jane Member", "balance": "12345.67"}
        await _insert_audit_row(
            tid,
            entity="members",
            action="probe.redact",
            entity_id="m-secret",
            before=secret,
            after=secret,
        )
        probe_token = await _access_only_viewer(tid)
        params = {"action": "probe.redact"}
        async with api_client() as client:
            res = await client.get(
                "/audit-log",
                params=params,
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200
            entitled = res.json()["items"][0]
            assert entitled["redacted"] is False
            assert entitled["before"] == secret
            assert entitled["after"] == secret

            res = await client.get(
                "/audit-log",
                params=params,
                headers={"authorization": f"Bearer {probe_token}"},
            )
            assert res.status_code == 200
            redacted = res.json()["items"][0]
            # Metadata IS the audit trail and stays visible ...
            assert redacted["entity_id"] == "m-secret"
            assert redacted["action"] == "probe.redact"
            # ... but the payloads never leave the server (gate 1.6).
            assert redacted["redacted"] is True
            assert redacted["before"] is None
            assert redacted["after"] is None

    asyncio.run(run())


def test_audit_unmapped_entity_is_redacted_for_everyone() -> None:
    """Deny-by-default entity->module map: an entity nobody mapped is
    redacted even for System Admin."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        await _insert_audit_row(
            tid,
            entity="mystery_table",
            action="probe.unmapped",
            after={"secret": "value"},
        )
        async with api_client() as client:
            res = await client.get(
                "/audit-log",
                params={"action": "probe.unmapped"},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200
            item = res.json()["items"][0]
            assert item["redacted"] is True
            assert item["before"] is None
            assert item["after"] is None

    asyncio.run(run())


def test_audit_log_is_tenant_scoped() -> None:
    """Tenant B's viewer never sees tenant A's audit rows (RLS + the
    explicit bound tenant predicate, v1.1 rule 4)."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        _, _, token_b = await seed_actor()
        marker = f"cross-{uuid.uuid4().hex[:8]}"
        await _insert_audit_row(tid_a, entity="members", action=marker)
        async with api_client() as client:
            res = await client.get(
                "/audit-log",
                params={"action": marker},
                headers={"authorization": f"Bearer {token_b}"},
            )
            assert res.status_code == 200
            assert res.json()["items"] == []

    asyncio.run(run())
