"""API + integration tests for the members module (P8 EXIT criteria).

Requires a migrated PostgreSQL database (DATABASE_URL env var). Tests run
through the app as a non-superuser role, so every count is tenant-scoped
by RLS.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import post_deposit
from genesis.application.members import MEMBERS_LIST_SCOPE
from genesis.application.pagination import encode_cursor
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.infrastructure.db import get_engine
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, str]:
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    token = issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )
    return tid, token


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Creation: accounts, audit, welcome event, numbering
# ---------------------------------------------------------------------------


def test_create_member_opens_accounts_audits_and_notifies() -> None:
    async def run() -> None:
        tid, token = await _seed_actor()
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Wanjiku Kamau", "phone": "+254700000001"},
                headers=_headers(token),
            )
        assert res.status_code == 201
        body = res.json()
        assert body["member_no"] == "GP-0001"
        assert body["status"] == "active"
        assert body["version"] == 1
        mid = body["id"]
        async with tenant_session(factory(), tid) as session:
            shares = (
                await session.execute(
                    text("SELECT count(*) FROM share_accounts WHERE member_id = CAST(:m AS uuid)"),
                    {"m": mid},
                )
            ).scalar_one()
            deposits = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM deposit_accounts WHERE member_id = CAST(:m AS uuid)"
                    ),
                    {"m": mid},
                )
            ).scalar_one()
            welcome = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE event_type = 'member.welcome'")
                )
            ).scalar_one()
            audits = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE action = 'member.create' AND entity_id = :m"
                    ),
                    {"m": mid},
                )
            ).scalar_one()
        assert int(shares) == 1
        assert int(deposits) == 1
        assert int(welcome) == 1
        assert int(audits) == 1

    asyncio.run(run())


def test_member_numbers_are_sequential_and_tenant_scoped() -> None:
    async def run() -> None:
        _, token_a = await _seed_actor()
        _, token_b = await _seed_actor()
        async with api_client() as client:
            first = await client.post(
                "/members",
                json={"type": "company", "name": "Acme Ltd"},
                headers=_headers(token_a),
            )
            second = await client.post(
                "/members",
                json={"type": "group", "name": "Chama One"},
                headers=_headers(token_a),
            )
            other_tenant = await client.post(
                "/members",
                json={"type": "vehicle", "name": "KDA 001A"},
                headers=_headers(token_b),
            )
        assert first.json()["member_no"] == "GP-0001"
        assert second.json()["member_no"] == "GP-0002"
        assert other_tenant.json()["member_no"] == "GP-0001"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: double-submit creates exactly one member
# ---------------------------------------------------------------------------


def test_double_submit_creates_exactly_one_member() -> None:
    async def run() -> None:
        tid, token = await _seed_actor()
        headers = {**_headers(token), "idempotency-key": uuid.uuid4().hex}
        payload = {"type": "person", "name": "Double Tap"}
        async with api_client() as client:
            first = await client.post("/members", json=payload, headers=headers)
            second = await client.post("/members", json=payload, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 201
        assert second.headers.get("idempotency-replayed") == "true"
        assert second.json() == first.json()
        async with tenant_session(factory(), tid) as session:
            count = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
        assert int(count) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: optimistic locking returns 409 on stale version
# ---------------------------------------------------------------------------


def test_stale_version_edit_returns_409() -> None:
    async def run() -> None:
        _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Original Name"},
                headers=headers,
            )
            mid = created.json()["id"]
            ok = await client.put(
                f"/members/{mid}",
                json={"version": 1, "name": "Updated Name"},
                headers=headers,
            )
            assert ok.status_code == 200
            assert ok.json()["version"] == 2
            stale = await client.put(
                f"/members/{mid}",
                json={"version": 1, "name": "Lost Update"},
                headers=headers,
            )
        assert stale.status_code == 409
        assert stale.json()["category"] == "conflict"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Status machine over the API
# ---------------------------------------------------------------------------


def test_status_transitions_and_exit_is_workflow_only() -> None:
    """Active<->Arrears over the API; 'exited' only via the P12 workflow."""

    async def run() -> None:
        _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Status Walker"},
                headers=headers,
            )
            mid = created.json()["id"]
            steps = [(1, "arrears"), (2, "active")]
            for version, status in steps:
                res = await client.post(
                    f"/members/{mid}/status",
                    json={"version": version, "status": status},
                    headers=headers,
                )
                assert res.status_code == 200, (version, status)
                assert res.json()["status"] == status
            # P12: the settlement workflow is the sole writer of the
            # terminal state — direct status changes are rejected even
            # for a System Admin (issue #14 resolution).
            direct_exit = await client.post(
                f"/members/{mid}/status",
                json={"version": 3, "status": "exited"},
                headers=headers,
            )
            assert direct_exit.status_code == 409
            still_active = await client.get(f"/members/{mid}", headers=headers)
            assert still_active.json()["status"] == "active"
            illegal = await client.post(
                f"/members/{mid}/status",
                json={"version": 3, "status": "arrears"},
                headers=headers,
            )
            assert illegal.status_code == 200  # arrears is still legal
            illegal_back = await client.post(
                f"/members/{mid}/status",
                json={"version": 3, "status": "active"},
                headers=headers,
            )
        assert illegal_back.status_code == 409  # stale version -> 409

    asyncio.run(run())


def test_get_unknown_member_returns_404() -> None:
    async def run() -> None:
        _, token = await _seed_actor()
        async with api_client() as client:
            res = await client.get(f"/members/{uuid.uuid4()}", headers=_headers(token))
        assert res.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Statement: keyset pagination over ledger transactions
# ---------------------------------------------------------------------------


def test_member_statement_keyset_pagination() -> None:
    async def run() -> None:
        tid, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Statement Member"},
                headers=headers,
            )
            mid = created.json()["id"]
        base = datetime(2026, 1, 1, tzinfo=UTC)
        async with tenant_session(factory(), tid) as session:
            for idx, amount in enumerate(("100.00", "200.00", "300.00")):
                await post_deposit(
                    session,
                    tid,
                    uuid.UUID(mid),
                    Decimal(amount),
                    Channel.MPESA,
                    occurred_at=base + timedelta(days=idx),
                )
        async with api_client() as client:
            first = await client.get(
                f"/members/{mid}/statement",
                params={"limit": 2},
                headers=headers,
            )
            assert first.status_code == 200
            page1 = first.json()
            assert [i["amount"] for i in page1["items"]] == ["300.00", "200.00"]
            assert all(i["txn_ref"].startswith("MP-") for i in page1["items"])
            assert page1["next_cursor"]
            second = await client.get(
                f"/members/{mid}/statement",
                params={"limit": 2, "cursor": page1["next_cursor"]},
                headers=headers,
            )
            assert second.status_code == 200
            page2 = second.json()
        assert [i["amount"] for i in page2["items"]] == ["100.00"]
        assert page2["next_cursor"] is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# List: keyset cursor + EXIT: flat query count (N+1 guard)
# ---------------------------------------------------------------------------


def test_list_members_keyset_cursor() -> None:
    async def run() -> None:
        tid, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            for i in range(3):
                res = await client.post(
                    "/members",
                    json={"type": "person", "name": f"Member {i}"},
                    headers=headers,
                )
                assert res.status_code == 201
            first = await client.get("/members", params={"limit": 2}, headers=headers)
            page1 = first.json()
            assert [m["member_no"] for m in page1["items"]] == ["GP-0001", "GP-0002"]
            # #31 batch 13: the served cursor is the OPAQUE signed seal
            # of the same keyset position (deterministic encode - this
            # stays an exact-equality oracle).
            assert page1["next_cursor"] == encode_cursor(
                "GP-0002", tenant_id=tid, endpoint=MEMBERS_LIST_SCOPE
            )
            second = await client.get(
                "/members",
                params={"limit": 2, "cursor": page1["next_cursor"]},
                headers=headers,
            )
            page2 = second.json()
        assert [m["member_no"] for m in page2["items"]] == ["GP-0003"]
        assert page2["next_cursor"] is None

    asyncio.run(run())


class _QueryCounter:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, *args: Any) -> None:
        self.value += 1


async def _counted_list_queries(client: Any, headers: dict[str, str]) -> tuple[int, int]:
    engine = get_engine(os.environ["DATABASE_URL"])
    counter = _QueryCounter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        res = await client.get("/members", params={"limit": 50}, headers=headers)
        assert res.status_code == 200
        return counter.value, len(res.json()["items"])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)


def test_list_members_query_count_is_flat() -> None:
    """EXIT: the list endpoint issues a constant number of queries (no N+1)."""

    async def run() -> None:
        _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            for i in range(2):
                res = await client.post(
                    "/members",
                    json={"type": "person", "name": f"Small {i}"},
                    headers=headers,
                )
                assert res.status_code == 201
            small_queries, small_rows = await _counted_list_queries(client, headers)
            assert small_rows == 2
            assert small_queries > 0
            for i in range(10):
                res = await client.post(
                    "/members",
                    json={"type": "person", "name": f"Large {i}"},
                    headers=headers,
                )
                assert res.status_code == 201
            large_queries, large_rows = await _counted_list_queries(client, headers)
        assert large_rows == 12
        assert small_queries == large_queries, (
            f"query count grew with rows: {small_queries} -> {large_queries}"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RBAC: deny-by-default per route
# ---------------------------------------------------------------------------


def test_members_rbac_enforced_per_route() -> None:
    async def run() -> None:
        _, teller_token = await _seed_actor("Teller")
        _, auditor_token = await _seed_actor("Auditor")
        teller = _headers(teller_token)
        auditor = _headers(auditor_token)
        async with api_client() as client:
            res = await client.get("/members", headers=teller)
            assert res.status_code == 200
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Nope"},
                headers=teller,
            )
            assert res.status_code == 403
            res = await client.get("/members", headers=auditor)
            assert res.status_code == 200
            res = await client.put(
                f"/members/{uuid.uuid4()}",
                json={"version": 1, "name": "X"},
                headers=auditor,
            )
        assert res.status_code == 403

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Governed exit: edit alone is not enough (issue #14)
# ---------------------------------------------------------------------------


async def _seed_actor_with_members_perms(*, edit: bool, approve: bool) -> tuple[uuid.UUID, str]:
    """Custom role with explicit members-module grants (deny elsewhere)."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name="Custom Members Role")
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO permissions "
                "(tenant_id, role_id, module, can_view, can_create, can_edit, can_approve) "
                "VALUES (CAST(:tid AS uuid), CAST(:rid AS uuid), 'members', "
                "true, true, :edit, :approve)"
            ),
            {"tid": str(tid), "rid": str(role_id), "edit": edit, "approve": approve},
        )
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    token = issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )
    return tid, token


def test_direct_exit_rejected_for_every_permission_level() -> None:
    """P12 sole-writer rule: not even members:approve can exit directly.

    Under P8 (issue #14) approvers could set 'exited' via the status
    route as an interim control; P12 replaces that with the settlement
    workflow, so the direct route now 409s regardless of permissions.
    """

    async def run() -> None:
        for approve in (False, True):
            _, token = await _seed_actor_with_members_perms(edit=True, approve=approve)
            headers = _headers(token)
            async with api_client() as client:
                created = await client.post(
                    "/members",
                    json={"type": "person", "name": "Governed Exit"},
                    headers=headers,
                )
                assert created.status_code == 201
                mid = created.json()["id"]
                denied = await client.post(
                    f"/members/{mid}/status",
                    json={"version": 1, "status": "exited"},
                    headers=headers,
                )
                assert denied.status_code == 409, f"approve={approve}"
                unchanged = await client.get(f"/members/{mid}", headers=headers)
                assert unchanged.json()["status"] == "active"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Statement semantics: 404 for unknown members, 400 for malformed cursors
# ---------------------------------------------------------------------------


def test_statement_of_unknown_member_returns_404() -> None:
    async def run() -> None:
        _, token = await _seed_actor()
        async with api_client() as client:
            res = await client.get(f"/members/{uuid.uuid4()}/statement", headers=_headers(token))
        assert res.status_code == 404

    asyncio.run(run())


def test_statement_with_malformed_cursor_returns_400() -> None:
    async def run() -> None:
        _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Cursor Victim"},
                headers=headers,
            )
            mid = created.json()["id"]
            res = await client.get(
                f"/members/{mid}/statement",
                params={"cursor": "not|a-cursor"},
                headers=headers,
            )
        assert res.status_code == 400
        assert res.json()["category"] == "validation_error"

    asyncio.run(run())


def test_list_members_with_status_and_type_filters() -> None:
    """Covers the status and member_type filter clauses in list_members."""

    async def run() -> None:
        _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            await client.post(
                "/members",
                json={"type": "person", "name": "Filter Person"},
                headers=headers,
            )
            await client.post(
                "/members",
                json={"type": "company", "name": "Filter Company"},
                headers=headers,
            )
            by_type = await client.get("/members", params={"type": "person"}, headers=headers)
            assert by_type.status_code == 200
            assert all(m["type"] == "person" for m in by_type.json()["items"])
            by_status = await client.get("/members", params={"status": "active"}, headers=headers)
            assert by_status.status_code == 200
            assert all(m["status"] == "active" for m in by_status.json()["items"])
            combined = await client.get(
                "/members",
                params={"type": "company", "status": "active"},
                headers=headers,
            )
            assert combined.status_code == 200
            assert all(
                m["type"] == "company" and m["status"] == "active" for m in combined.json()["items"]
            )

    asyncio.run(run())


def test_exit_blocked_by_open_loan_application() -> None:
    """An open application blocks the P12 exit request (eligibility)."""

    async def run() -> None:
        tid, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Blocked Exit"},
                headers=headers,
            )
            mid = created.json()["id"]
        async with tenant_session(factory(), tid) as session:
            product_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO loan_products "
                    "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
                ),
                {"id": str(product_id), "tid": str(tid), "name": f"Block-{uuid.uuid4().hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO loan_applications "
                    "(id, tenant_id, member_id, product_id, amount, term_months, "
                    " rate_pct, stage, cover_pct) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    "CAST(:pid AS uuid), 5000, 12, '12.00', 'submitted', '0.00')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "mid": mid,
                    "pid": str(product_id),
                },
            )
        async with api_client() as client:
            res = await client.post(
                "/member-exits",
                json={"member_id": mid},
                headers=headers,
            )
        assert res.status_code == 409

    asyncio.run(run())
