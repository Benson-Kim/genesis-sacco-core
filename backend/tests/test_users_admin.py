"""P13.5 users administration suite.

Covers the EXIT criteria of BUILD_PROMPTS P13.5:
- matrix coverage of every new route for every role,
- suspended-user token refusal + OTP void (side-effect row counts),
- self-role/self-status guards and the last-admin lockout guard,
  including the concurrent-suspension race,
- optimistic locking, atomic email claim, audit rows per mutation,
- last_active_at written at token issue only,
- idempotent create by side-effect row counts.

Falsifiability (MASTER_PROMPT §4): each guard test asserts the exact
refusal status AND the untouched side-effect state, so removing the
guard (self-edit check, admin-set lock, OTP void, family revocation)
makes the corresponding test fail.
"""

import asyncio
import os
import uuid

import jwt
import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, latest_otp_code, unique_email
from export_helpers import add_user, count, seed_actor
from genesis.application import users as users_service
from genesis.application.auth import STAFF_AUDIENCE, AuthContext, issue_access_token
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import ConflictError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _role_id(tid: uuid.UUID, name: str) -> uuid.UUID:
    async with tenant_session(factory(), tid) as session:
        rid = (
            await session.execute(text("SELECT id FROM roles WHERE name = :n"), {"n": name})
        ).scalar_one()
    return uuid.UUID(str(rid))


async def _login(tid: uuid.UUID, email: str) -> dict[str, str]:
    headers = {"x-tenant-id": str(tid)}
    async with api_client() as client:
        res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
        assert res.status_code == 202
        code = await latest_otp_code(tid)
        res = await client.post(
            "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
        )
        assert res.status_code == 200
        body: dict[str, str] = res.json()
    return body


async def _create_target(
    tid: uuid.UUID, admin_token: str, *, role_name: str = "Teller"
) -> tuple[str, str]:
    """Create a user through the API; returns (user_id, email)."""
    email = unique_email()
    rid = await _role_id(tid, role_name)
    async with api_client() as client:
        res = await client.post(
            "/users",
            json={"email": email, "full_name": "Target User", "role_id": str(rid)},
            headers={"authorization": f"Bearer {admin_token}"},
        )
        assert res.status_code == 201, res.text
        user_id = str(res.json()["id"])
    return user_id, email


def test_users_routes_matrix_every_role() -> None:
    """Every new route x every role; expectations from the P4 seed matrix."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, admin_token = await seed_actor()
        teller_rid = await _role_id(tid, "Teller")
        accountant_rid = await _role_id(tid, "Accountant")
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            headers = {"authorization": f"Bearer {token}"}
            can_view = matrix[role_name][Module.ACCESS_CONTROL][Action.VIEW]
            can_create = matrix[role_name][Module.ACCESS_CONTROL][Action.CREATE]
            can_edit = matrix[role_name][Module.ACCESS_CONTROL][Action.EDIT]
            # Fresh target per role so versions are deterministic (v1).
            target_id, _ = await _create_target(tid, admin_token)
            async with api_client() as client:
                res = await client.get("/users", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.get(f"/users/{target_id}", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.get("/audit-log", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name

                res = await client.post(
                    "/users",
                    json={
                        "email": unique_email(),
                        "full_name": "Matrix Probe",
                        "role_id": str(teller_rid),
                    },
                    headers=headers,
                )
                assert res.status_code == (201 if can_create else 403), role_name

                res = await client.put(
                    f"/users/{target_id}",
                    json={"version": 1, "full_name": "Matrix Edited"},
                    headers=headers,
                )
                assert res.status_code == (200 if can_edit else 403), role_name
                version = res.json()["version"] if can_edit else 1

                res = await client.post(
                    f"/users/{target_id}/role",
                    json={"version": version, "role_id": str(accountant_rid)},
                    headers=headers,
                )
                assert res.status_code == (200 if can_edit else 403), role_name
                version = res.json()["version"] if can_edit else version

                res = await client.post(
                    f"/users/{target_id}/status",
                    json={"version": version, "status": "suspended"},
                    headers=headers,
                )
                assert res.status_code == (200 if can_edit else 403), role_name

                res = await client.post(f"/users/{target_id}/otp/invalidate", headers=headers)
                assert res.status_code == (200 if can_edit else 403), role_name
                res = await client.post(f"/users/{target_id}/otp/reenrol", headers=headers)
                assert res.status_code == (200 if can_edit else 403), role_name

    asyncio.run(run())


def test_create_user_email_claim_is_atomic() -> None:
    """Duplicate email -> 409; exactly one user row and one audit row.

    Hand-computed oracle: one 201 + one 409 for the same email leaves
    users=1, audit(user.create)=1, outbox(user.created)=1.
    """

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        rid = await _role_id(tid, "Teller")
        email = unique_email()
        headers = {"authorization": f"Bearer {admin_token}"}
        body = {"email": email, "full_name": "Dup Probe", "role_id": str(rid)}
        async with api_client() as client:
            first = await client.post("/users", json=body, headers=headers)
            assert first.status_code == 201
            second = await client.post("/users", json=body, headers=headers)
            assert second.status_code == 409
        assert await count(tid, "SELECT count(*) FROM users WHERE email = :e", e=email) == 1
        uid = first.json()["id"]
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.create' AND entity_id = :eid",
                eid=uid,
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'user.created' "
                "AND payload->>'user_id' = :uid",
                uid=uid,
            )
            == 1
        )

    asyncio.run(run())


def test_create_user_rejects_unknown_fields_and_foreign_role() -> None:
    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        rid = await _role_id(tid, "Teller")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            # extra="forbid": unknown fields are a 422, never accepted.
            res = await client.post(
                "/users",
                json={
                    "email": unique_email(),
                    "full_name": "Extra Field",
                    "role_id": str(rid),
                    "status": "active",
                },
                headers=headers,
            )
            assert res.status_code == 422
            # A role id from ANOTHER tenant is a 404, never a grant.
            foreign_tid, _, _ = await seed_actor()
            foreign_rid = await _role_id(foreign_tid, "System Admin")
            res = await client.post(
                "/users",
                json={
                    "email": unique_email(),
                    "full_name": "Foreign Role",
                    "role_id": str(foreign_rid),
                },
                headers=headers,
            )
            assert res.status_code == 404

    asyncio.run(run())


def test_update_user_optimistic_locking() -> None:
    """Stale version -> 409 and no second write (gate 1.4)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token)
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            first = await client.put(
                f"/users/{target_id}",
                json={"version": 1, "full_name": "Edited Once"},
                headers=headers,
            )
            assert first.status_code == 200
            assert first.json()["version"] == 2
            stale = await client.put(
                f"/users/{target_id}",
                json={"version": 1, "full_name": "Stale Write"},
                headers=headers,
            )
            assert stale.status_code == 409
        async with tenant_session(factory(), tid) as session:
            name = (
                await session.execute(
                    text("SELECT full_name FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": target_id},
                )
            ).scalar_one()
        assert str(name) == "Edited Once"
        # Exactly one audit row: the stale write never landed (gate 1.5).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.update' AND entity_id = :eid",
                eid=target_id,
            )
            == 1
        )

    asyncio.run(run())


def test_update_user_duplicate_email_conflict() -> None:
    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token)
        _, other_email = await _create_target(tid, admin_token)
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.put(
                f"/users/{target_id}",
                json={"version": 1, "email": other_email},
                headers=headers,
            )
            assert res.status_code == 409

    asyncio.run(run())


def test_suspension_refuses_tokens_and_voids_pending_otp() -> None:
    """Suspension takes effect immediately, in ONE transaction (P13.5).

    Hand-computed pre-state: the target logs in (1 active refresh
    token, challenge #1 consumed) and requests a second OTP (1 pending
    challenge). Suspension must void 1 challenge and revoke 1 refresh
    token. Falsifiability: drop the void -> the pending code still
    verifies; drop the revocation -> the refresh still rotates.
    """

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token)
        tokens = await _login(tid, email)
        tenant_headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request", json={"email": email}, headers=tenant_headers
            )
            assert res.status_code == 202
            pending_code = await latest_otp_code(tid)
            assert (
                await count(
                    tid,
                    "SELECT count(*) FROM otp_challenges "
                    "WHERE user_id = CAST(:u AS uuid) AND consumed_at IS NULL",
                    u=target_id,
                )
                == 1
            )
            res = await client.post(
                f"/users/{target_id}/status",
                json={"version": 1, "status": "suspended"},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "suspended"

            # Refresh refused: the P3 family revocation is the fence.
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
                headers=tenant_headers,
            )
            assert res.status_code == 401
            # Pending OTP voided in the same transaction.
            res = await client.post(
                "/auth/otp/verify",
                json={"email": email, "code": pending_code},
                headers=tenant_headers,
            )
            assert res.status_code == 401
            # New OTP requests never mint challenges for a suspended user
            # (P3 active-status filter, reused).
            before = await count(
                tid,
                "SELECT count(*) FROM otp_challenges WHERE user_id = CAST(:u AS uuid)",
                u=target_id,
            )
            res = await client.post(
                "/auth/otp/request", json={"email": email}, headers=tenant_headers
            )
            assert res.status_code == 202
            after = await count(
                tid,
                "SELECT count(*) FROM otp_challenges WHERE user_id = CAST(:u AS uuid)",
                u=target_id,
            )
            assert after == before

        # Side-effect row counts, never return values alone (§4).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM otp_challenges "
                "WHERE user_id = CAST(:u AS uuid) AND consumed_at IS NULL",
                u=target_id,
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM refresh_tokens "
                "WHERE user_id = CAST(:u AS uuid) AND status = 'active'",
                u=target_id,
            )
            == 0
        )
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT after FROM audit_log WHERE action = 'user.status' "
                        "AND entity_id = :eid ORDER BY at DESC LIMIT 1"
                    ),
                    {"eid": target_id},
                )
            ).scalar_one()
        assert row["voided_otp_challenges"] == 1
        assert row["revoked_refresh_tokens"] == 1

    asyncio.run(run())


def test_suspend_then_reactivate_allows_login_again() -> None:
    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token)
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.post(
                f"/users/{target_id}/status",
                json={"version": 1, "status": "suspended"},
                headers=headers,
            )
            assert res.status_code == 200
            # Illegal self-transition: suspended -> suspended is 409.
            res = await client.post(
                f"/users/{target_id}/status",
                json={"version": 2, "status": "suspended"},
                headers=headers,
            )
            assert res.status_code == 409
            res = await client.post(
                f"/users/{target_id}/status",
                json={"version": 2, "status": "active"},
                headers=headers,
            )
            assert res.status_code == 200
        tokens = await _login(tid, email)
        assert tokens["access_token"]

    asyncio.run(run())


def test_self_status_and_self_role_edits_are_forbidden() -> None:
    """User-level separation of duties (P12 precedent): never self-edit.

    Falsifiability: remove the self-guard and both calls succeed (a
    second admin exists, so the last-admin guard cannot mask it), so
    the 403 assertions and the untouched-state assertions fail.
    """

    async def run() -> None:
        tid, admin_id, admin_token = await seed_actor()
        # Second admin so the last-admin guard is out of the picture.
        await add_user(tid, "System Admin")
        teller_rid = await _role_id(tid, "Teller")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.post(
                f"/users/{admin_id}/status",
                json={"version": 1, "status": "suspended"},
                headers=headers,
            )
            assert res.status_code == 403
            res = await client.post(
                f"/users/{admin_id}/role",
                json={"version": 1, "role_id": str(teller_rid)},
                headers=headers,
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text("SELECT status, version FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(admin_id)},
                )
            ).first()
        assert row is not None
        assert str(row[0]) == "active"
        assert int(row[1]) == 1

    asyncio.run(run())


def test_last_admin_cannot_be_suspended_or_reroled() -> None:
    """Self-lockout guard (P13.5). Falsifiable: remove the count check
    under the admin-set lock and the suspension returns 200; remove the
    F2 actor fence and the re-role returns 409 instead of 403.

    Review F2 note: the manager's re-role attempt now dies at the actor
    fence (403, least disclosure) BEFORE the last-admin count guard —
    the count guard itself stays covered by
    test_last_admin_rerole_guard_holds_at_service_level."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        # Branch Manager holds access_control:edit per the P4 matrix.
        _, manager_token = await add_user(tid, "Branch Manager")
        teller_rid = await _role_id(tid, "Teller")
        headers = {"authorization": f"Bearer {manager_token}"}
        async with api_client() as client:
            res = await client.post(
                f"/users/{admin_id}/status",
                json={"version": 1, "status": "suspended"},
                headers=headers,
            )
            assert res.status_code == 409
            res = await client.post(
                f"/users/{admin_id}/role",
                json={"version": 1, "role_id": str(teller_rid)},
                headers=headers,
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(admin_id)},
                )
            ).scalar_one()
        assert str(status) == "active"

    asyncio.run(run())


def test_last_admin_rerole_guard_holds_at_service_level() -> None:
    """The last-admin re-role 409 stays live behind the F2 fence.

    After F2 every API actor allowed to re-role an admin is an ACTIVE
    System Admin, which makes the target not-the-last by counting. The
    guard still matters as defense in depth: a SUSPENDED admin actor
    passes the F2 role check (role, not status) but must hit the count
    guard. Falsifiable: drop the count check under the admin-set lock
    and this service call succeeds, leaving zero active admins."""

    async def run() -> None:
        tid, admin_id, admin_token = await seed_actor()
        suspended_admin_id, _ = await add_user(tid, "System Admin")
        teller_rid = await _role_id(tid, "Teller")
        async with api_client() as client:
            res = await client.post(
                f"/users/{suspended_admin_id}/status",
                json={"version": 1, "status": "suspended"},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ConflictError):
                await users_service.assign_role(
                    session,
                    tid,
                    suspended_admin_id,
                    uuid.UUID(str(admin_id)),
                    version=1,
                    role_id=teller_rid,
                )
        remaining = await count(
            tid,
            "SELECT count(*) FROM users u JOIN roles r ON r.id = u.role_id "
            "WHERE r.name = 'System Admin' AND u.status = 'active'",
        )
        assert remaining == 1

    asyncio.run(run())


def test_concurrent_suspension_of_last_two_admins_leaves_one() -> None:
    """The race the guard exists for (P13.5 EXIT).

    Two admins suspend EACH OTHER concurrently. The ordered admin-set
    row locks serialize the two transactions; the second re-counts
    against committed state and gets 409. Hand-computed oracle:
    exactly one 200, one 409, and exactly ONE active System Admin
    remains. Falsifiability: drop the admin-set lock (count without
    locking) and both transactions count 2 admins, both succeed, and
    the final count is 0 — this test fails.
    """

    async def run() -> None:
        tid, admin1_id, admin1_token = await seed_actor()
        admin2_id, admin2_token = await add_user(tid, "System Admin")
        admin_rid = await _role_id(tid, "System Admin")
        async with api_client() as client:

            async def suspend(token: str, target: uuid.UUID | str) -> int:
                res = await client.post(
                    f"/users/{target}/status",
                    json={"version": 1, "status": "suspended"},
                    headers={"authorization": f"Bearer {token}"},
                )
                return res.status_code

            statuses = await asyncio.gather(
                suspend(admin1_token, admin2_id),
                suspend(admin2_token, admin1_id),
            )
        assert sorted(statuses) == [200, 409], statuses
        remaining = await count(
            tid,
            "SELECT count(*) FROM users WHERE role_id = CAST(:r AS uuid) AND status = 'active'",
            r=str(admin_rid),
        )
        assert remaining == 1

    asyncio.run(run())


def test_role_assignment_is_audited_and_propagates_on_refresh() -> None:
    """Role change writes before/after (gate 1.5) and the NEXT refresh
    carries the new role — hand-computed oracle: Teller -> Accountant."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token, role_name="Teller")
        tokens = await _login(tid, email)
        accountant_rid = await _role_id(tid, "Accountant")
        teller_rid = await _role_id(tid, "Teller")
        async with api_client() as client:
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 1, "role_id": str(accountant_rid)},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200
            assert res.json()["role_name"] == "Accountant"
            # Same-role reassignment is a rejected no-op.
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 2, "role_id": str(accountant_rid)},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 409
            refreshed = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
                headers={"x-tenant-id": str(tid)},
            )
            assert refreshed.status_code == 200
            # P14.5 FM1: staff tokens carry the code-owned staff audience.
            claims = jwt.decode(
                refreshed.json()["access_token"],
                os.environ["JWT_SIGNING_KEY"],
                algorithms=["HS256"],
                audience=STAFF_AUDIENCE,
            )
            assert claims["rid"] == str(accountant_rid)
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log WHERE action = 'user.role' "
                        "AND entity_id = :eid ORDER BY at DESC LIMIT 1"
                    ),
                    {"eid": target_id},
                )
            ).first()
        assert row is not None
        before, after = row
        assert before["role_id"] == str(teller_rid)
        assert before["role_name"] == "Teller"
        assert after["role_id"] == str(accountant_rid)
        assert after["role_name"] == "Accountant"

    asyncio.run(run())


def test_last_active_at_written_at_token_issue_only() -> None:
    """No per-request write amplification (P13.5)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token, role_name="Teller")

        async def last_active() -> object:
            async with tenant_session(factory(), tid) as session:
                return (
                    await session.execute(
                        text("SELECT last_active_at FROM users WHERE id = CAST(:id AS uuid)"),
                        {"id": target_id},
                    )
                ).scalar_one()

        assert await last_active() is None
        tokens = await _login(tid, email)
        t1 = await last_active()
        assert t1 is not None
        # A regular authenticated request must NOT move the marker.
        async with api_client() as client:
            res = await client.get(
                "/members", headers={"authorization": f"Bearer {tokens['access_token']}"}
            )
            assert res.status_code == 200
        assert await last_active() == t1
        # A refresh (token issue) must move it.
        async with api_client() as client:
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
                headers={"x-tenant-id": str(tid)},
            )
            assert res.status_code == 200
        t2 = await last_active()
        assert t2 is not None and t2 > t1  # type: ignore[operator]

    asyncio.run(run())


def test_users_list_keyset_pagination_and_filters() -> None:
    async def run() -> None:
        tid, admin_id, admin_token = await seed_actor()
        created = [await _create_target(tid, admin_token) for _ in range(3)]
        headers = {"authorization": f"Bearer {admin_token}"}
        seen: list[str] = []
        cursor: str | None = None
        async with api_client() as client:
            while True:
                params: dict[str, str] = {"limit": "2"}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get("/users", params=params, headers=headers)
                assert res.status_code == 200
                page = res.json()
                assert len(page["items"]) <= 2
                seen.extend(item["id"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            # 4 users total (admin + 3 targets), no duplicates, no gaps.
            expected = {str(admin_id), *(uid for uid, _ in created)}
            assert len(seen) == 4
            assert set(seen) == expected
            # Status filter with bound parameters only.
            res = await client.get("/users?status=suspended", headers=headers)
            assert res.status_code == 200
            assert res.json()["items"] == []
            # Role filter: only the seeded System Admin matches.
            admin_rid = await _role_id(tid, "System Admin")
            res = await client.get("/users", params={"role_id": str(admin_rid)}, headers=headers)
            assert res.status_code == 200
            assert [i["id"] for i in res.json()["items"]] == [str(admin_id)]
            res = await client.get("/users?cursor=not-a-cursor", headers=headers)
            assert res.status_code == 400

    asyncio.run(run())


def test_create_user_idempotency_replay_by_side_effects() -> None:
    """Same Idempotency-Key twice -> one user, one audit row, one outbox
    event; the replay returns the stored response (gate 1.4, §4)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        rid = await _role_id(tid, "Teller")
        email = unique_email()
        headers = {
            "authorization": f"Bearer {admin_token}",
            "idempotency-key": f"user-create-{uuid.uuid4().hex}",
        }
        body = {"email": email, "full_name": "Idem Probe", "role_id": str(rid)}
        async with api_client() as client:
            first = await client.post("/users", json=body, headers=headers)
            assert first.status_code == 201
            second = await client.post("/users", json=body, headers=headers)
            assert second.status_code == 201
            assert second.headers.get("idempotency-replayed") == "true"
            assert second.json()["id"] == first.json()["id"]
        uid = first.json()["id"]
        assert await count(tid, "SELECT count(*) FROM users WHERE email = :e", e=email) == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.create' AND entity_id = :eid",
                eid=uid,
            )
            == 1
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'user.created' "
                "AND payload->>'user_id' = :uid",
                uid=uid,
            )
            == 1
        )

    asyncio.run(run())


def test_otp_lifecycle_endpoints_void_without_disclosure() -> None:
    """Invalidate voids pending challenges; re-enrol also revokes the
    refresh families. Responses carry counts only — never codes."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token)
        tenant_headers = {"x-tenant-id": str(tid)}
        admin_headers = {"authorization": f"Bearer {admin_token}"}
        tokens = await _login(tid, email)
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request", json={"email": email}, headers=tenant_headers
            )
            assert res.status_code == 202
            pending_code = await latest_otp_code(tid)

            res = await client.post(f"/users/{target_id}/otp/invalidate", headers=admin_headers)
            assert res.status_code == 200
            assert res.json() == {"voided_otp_challenges": 1}
            res = await client.post(
                "/auth/otp/verify",
                json={"email": email, "code": pending_code},
                headers=tenant_headers,
            )
            assert res.status_code == 401
            # Sessions survive invalidation (only challenges die) ...
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
                headers=tenant_headers,
            )
            assert res.status_code == 200
            rotated = res.json()

            # ... but re-enrolment kills sessions too.
            res = await client.post(f"/users/{target_id}/otp/reenrol", headers=admin_headers)
            assert res.status_code == 200
            payload = res.json()
            assert payload["revoked_refresh_tokens"] >= 1
            assert set(payload) == {"voided_otp_challenges", "revoked_refresh_tokens"}
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": rotated["refresh_token"]},
                headers=tenant_headers,
            )
            assert res.status_code == 401
            # The user can still re-enrol through the login flow.
            fresh = await _login(tid, email)
            assert fresh["access_token"]
        # Audit rows for both lifecycle mutations (gate 1.5).
        for action in ("user.otp_invalidate", "user.otp_reenrol"):
            assert (
                await count(
                    tid,
                    "SELECT count(*) FROM audit_log WHERE action = :a AND entity_id = :eid",
                    a=action,
                    eid=target_id,
                )
                == 1
            )

    asyncio.run(run())


def test_user_service_tenant_predicates_refuse_foreign_tenant() -> None:
    """v1.1 rule 4 falsifiability (issue #17 pattern): session AS the
    row's tenant so RLS passes; a foreign tenant_id argument must be
    refused by the explicit predicate alone."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token)
        foreign = uuid.uuid4()
        uid = uuid.UUID(target_id)
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await users_service.get_user(session, foreign, uid)
            with pytest.raises(NotFoundError):
                await users_service.update_user(
                    session, foreign, uid, uid, version=1, full_name="hijack"
                )
            page = await users_service.list_users(session, foreign)
            assert page.items == []
        async with tenant_session(factory(), tid) as session:
            name = (
                await session.execute(
                    text("SELECT full_name FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": target_id},
                )
            ).scalar_one()
        assert str(name) == "Target User"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Review findings F1/F2/F3/F5 (P13.5 deep-review remediation)
# ---------------------------------------------------------------------------


async def _active_refresh_and_pending_otp(tid: uuid.UUID, user_id: str) -> tuple[int, int]:
    active = await count(
        tid,
        "SELECT count(*) FROM refresh_tokens "
        "WHERE user_id = CAST(:u AS uuid) AND status = 'active'",
        u=user_id,
    )
    pending = await count(
        tid,
        "SELECT count(*) FROM otp_challenges "
        "WHERE user_id = CAST(:u AS uuid) AND consumed_at IS NULL",
        u=user_id,
    )
    return active, pending


def test_email_change_on_admin_by_manager_is_403_and_untouched() -> None:
    """Review F1(b): email is the sole OTP login identifier, so a Branch
    Manager rewriting a System Admin's email is account takeover.

    Falsifiable: remove the actor fence in update_user and the PUT
    returns 200 and the row assertions on email/version fail."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        # A second admin keeps the last-admin guard out of the picture.
        await add_user(tid, "System Admin")
        _, manager_token = await add_user(tid, "Branch Manager")
        hijack = unique_email()
        async with api_client() as client:
            res = await client.put(
                f"/users/{admin_id}",
                json={"version": 1, "email": hijack},
                headers={"authorization": f"Bearer {manager_token}"},
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text("SELECT email, version FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(admin_id)},
                )
            ).first()
        assert row is not None
        assert str(row[0]) != hijack
        assert int(row[1]) == 1
        # No audit row for the refused write (gate 1.5).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.update' AND entity_id = :eid",
                eid=str(admin_id),
            )
            == 0
        )

    asyncio.run(run())


def test_email_change_voids_credentials_in_same_transaction() -> None:
    """Review F1(c): ANY email change kills the target's credentials.

    Hand-computed pre-state: the target logs in (1 active refresh
    token, challenge #1 consumed) and requests a second OTP (1 pending
    challenge). The email change must void 1 challenge and revoke 1
    refresh token, recorded in the audit `after` payload. Falsifiable:
    drop the void/revoke in update_user and the 0-count assertions and
    the dead-old-email assertions fail."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, old_email = await _create_target(tid, admin_token)
        await _login(tid, old_email)
        tenant_headers = {"x-tenant-id": str(tid)}
        admin_headers = {"authorization": f"Bearer {admin_token}"}
        new_email = unique_email()
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request", json={"email": old_email}, headers=tenant_headers
            )
            assert res.status_code == 202
            assert await _active_refresh_and_pending_otp(tid, target_id) == (1, 1)

            res = await client.put(
                f"/users/{target_id}",
                json={"version": 1, "email": new_email},
                headers=admin_headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["email"] == new_email

            # Side-effect row counts prove the fence (never return
            # values alone, MASTER_PROMPT section 4).
            assert await _active_refresh_and_pending_otp(tid, target_id) == (0, 0)

            # Old-email login is dead: no challenge is minted for an
            # email that no longer exists (P3 silent path).
            res = await client.post(
                "/auth/otp/request", json={"email": old_email}, headers=tenant_headers
            )
            assert res.status_code == 202
            assert await _active_refresh_and_pending_otp(tid, target_id) == (0, 0)

        # New-email login works end to end.
        tokens = await _login(tid, new_email)
        assert tokens["access_token"]

        # The audit row records the side-effect counts (gate 1.5).
        async with tenant_session(factory(), tid) as session:
            after = (
                await session.execute(
                    text(
                        "SELECT after FROM audit_log WHERE action = 'user.update' "
                        "AND entity_id = :eid ORDER BY at DESC LIMIT 1"
                    ),
                    {"eid": target_id},
                )
            ).scalar_one()
        assert after["voided_otp_challenges"] == 1
        assert after["revoked_refresh_tokens"] == 1

    asyncio.run(run())


def test_admin_can_change_another_admins_email() -> None:
    """Review F1: the fence refuses non-admin ACTORS, not the operation.

    A System Admin changing another System Admin's email is legitimate
    (and exercises the admin-set-lock-first ordering branch)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_admin_id, _ = await add_user(tid, "System Admin")
        new_email = unique_email()
        async with api_client() as client:
            res = await client.put(
                f"/users/{target_admin_id}",
                json={"version": 1, "email": new_email},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200, res.text
            assert res.json()["email"] == new_email

    asyncio.run(run())


def test_non_admin_cannot_grant_system_admin() -> None:
    """Review F2: privilege escalation by proxy. A Branch Manager holds
    access_control:edit but must not mint a System Admin.

    Falsifiable: remove the actor fence in assign_role and the POST
    returns 200 and the untouched-role assertion fails."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token, role_name="Teller")
        _, manager_token = await add_user(tid, "Branch Manager")
        admin_rid = await _role_id(tid, "System Admin")
        teller_rid = await _role_id(tid, "Teller")
        async with api_client() as client:
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 1, "role_id": str(admin_rid)},
                headers={"authorization": f"Bearer {manager_token}"},
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text("SELECT role_id, version FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": target_id},
                )
            ).first()
        assert row is not None
        assert uuid.UUID(str(row[0])) == teller_rid
        assert int(row[1]) == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.role' AND entity_id = :eid",
                eid=target_id,
            )
            == 0
        )

    asyncio.run(run())


def test_non_admin_cannot_change_role_of_a_system_admin() -> None:
    """Review F2, second edge: demoting a current System Admin also
    requires a System Admin actor. Two admins exist, so the last-admin
    count guard cannot mask the fence (falsifiable both ways)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await add_user(tid, "System Admin")
        _, manager_token = await add_user(tid, "Branch Manager")
        teller_rid = await _role_id(tid, "Teller")
        async with api_client() as client:
            res = await client.post(
                f"/users/{admin_id}/role",
                json={"version": 1, "role_id": str(teller_rid)},
                headers={"authorization": f"Bearer {manager_token}"},
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            rid = (
                await session.execute(
                    text(
                        "SELECT r.name FROM users u JOIN roles r ON r.id = u.role_id "
                        "WHERE u.id = CAST(:id AS uuid)"
                    ),
                    {"id": str(admin_id)},
                )
            ).scalar_one()
        assert str(rid) == "System Admin"

    asyncio.run(run())


def test_admin_can_grant_and_change_system_admin_roles() -> None:
    """Review F2, allow direction: a real System Admin actor may grant
    System Admin and may re-role another admin (a second admin exists,
    so no lockout)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token, role_name="Teller")
        admin_rid = await _role_id(tid, "System Admin")
        accountant_rid = await _role_id(tid, "Accountant")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 1, "role_id": str(admin_rid)},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["role_name"] == "System Admin"
            # ... and back off again (2 active admins, so no lockout).
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 2, "role_id": str(accountant_rid)},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["role_name"] == "Accountant"

    asyncio.run(run())


def test_role_fence_resolves_actor_role_server_side_not_from_jwt() -> None:
    """Review F2: the fence must read the ACTOR's role from the users
    table inside the transaction — never trust the JWT claim alone.

    Adversarial setup: a token whose `rid` claim says System Admin but
    whose users-table row is a Branch Manager. RequirePermission (JWT
    role) grants access_control:edit, so only the server-side lookup
    stands between this token and an admin grant. Falsifiable: resolve
    the actor role from ctx.role_id instead and the POST returns 200."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, _ = await _create_target(tid, admin_token, role_name="Teller")
        manager_id, _ = await add_user(tid, "Branch Manager")
        admin_rid = await _role_id(tid, "System Admin")
        teller_rid = await _role_id(tid, "Teller")
        forged = issue_access_token(
            AuthContext(user_id=manager_id, tenant_id=tid, role_id=admin_rid)
        )
        async with api_client() as client:
            res = await client.post(
                f"/users/{target_id}/role",
                json={"version": 1, "role_id": str(admin_rid)},
                headers={"authorization": f"Bearer {forged}"},
            )
            assert res.status_code == 403
        async with tenant_session(factory(), tid) as session:
            rid = (
                await session.execute(
                    text("SELECT role_id FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": target_id},
                )
            ).scalar_one()
        assert uuid.UUID(str(rid)) == teller_rid

    asyncio.run(run())


def test_suspended_user_access_token_is_refused_immediately() -> None:
    """Review F3: a still-valid (15-minute) access token dies the moment
    the suspension commits — authorization verifies users.status in the
    same round-trip as the permission row.

    Falsifiable: drop the status check in actor_access/RequirePermission
    and the post-suspension GET returns 200 (the token is still signed
    and unexpired, and the permission row still exists)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        target_id, email = await _create_target(tid, admin_token, role_name="Teller")
        tokens = await _login(tid, email)
        target_headers = {"authorization": f"Bearer {tokens['access_token']}"}
        async with api_client() as client:
            res = await client.get("/members", headers=target_headers)
            assert res.status_code == 200
            res = await client.post(
                f"/users/{target_id}/status",
                json={"version": 1, "status": "suspended"},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 200
            # Same token, same route, zero grace period: 401 (not 403 —
            # the session is dead, not under-privileged).
            res = await client.get("/members", headers=target_headers)
            assert res.status_code == 401

    asyncio.run(run())


def test_concurrent_otp_verify_vs_suspend_no_deadlock() -> None:
    """Review F5: the P13.5 lock re-ordering (user row -> challenge ->
    refresh) is proven by adversarial interleaving, not by docstring.

    Repeated race (P11 style): OTP verify and suspension run
    concurrently. The user-row lock serializes them in either order —
    verify-first mints a token pair that the suspension then revokes;
    suspend-first voids the challenge so verify gets 401. Invariant
    after every round: no deadlock 500s, the suspension committed, and
    the target holds 0 active refresh tokens and 0 pending challenges.
    Falsifiable: restore the pre-P13.5 challenge-first lock order and
    the rounds deadlock (500s); drop the void/revoke and the 0-count
    assertions fail."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        tenant_headers = {"x-tenant-id": str(tid)}
        admin_headers = {"authorization": f"Bearer {admin_token}"}
        for _ in range(4):
            target_id, email = await _create_target(tid, admin_token)
            async with api_client() as client:
                res = await client.post(
                    "/auth/otp/request", json={"email": email}, headers=tenant_headers
                )
                assert res.status_code == 202
                code = await latest_otp_code(tid)

                async def verify(c: object, em: str, co: str) -> int:
                    res = await c.post(  # type: ignore[attr-defined]
                        "/auth/otp/verify",
                        json={"email": em, "code": co},
                        headers=tenant_headers,
                    )
                    return int(res.status_code)

                async def suspend(c: object, target: str) -> int:
                    res = await c.post(  # type: ignore[attr-defined]
                        f"/users/{target}/status",
                        json={"version": 1, "status": "suspended"},
                        headers=admin_headers,
                    )
                    return int(res.status_code)

                verify_status, suspend_status = await asyncio.gather(
                    verify(client, email, code), suspend(client, target_id)
                )
            # No deadlocks: both requests resolve to domain outcomes.
            assert suspend_status == 200, (verify_status, suspend_status)
            assert verify_status in (200, 401), (verify_status, suspend_status)
            # A committed suspension always leaves zero live credentials.
            assert await _active_refresh_and_pending_otp(tid, target_id) == (0, 0)

    asyncio.run(run())


def test_concurrent_refresh_rotate_vs_suspend_no_deadlock() -> None:
    """Review F5, second pair: refresh rotation vs suspension, repeated.

    Rotation peeks the token row unlocked, then locks the USER row
    before re-validating the token row — the same order the suspension
    writer uses — so the pair serializes instead of deadlocking.
    Rotate-first mints a replacement token that the suspension then
    revokes; suspend-first revokes the family so rotation gets 401.
    Falsifiable: lock the token row before the user row (the pre-P13.5
    order) and the rounds deadlock (500s); drop the family revocation
    and the 0-count assertions fail."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        tenant_headers = {"x-tenant-id": str(tid)}
        admin_headers = {"authorization": f"Bearer {admin_token}"}
        for _ in range(4):
            target_id, email = await _create_target(tid, admin_token)
            tokens = await _login(tid, email)
            async with api_client() as client:

                async def rotate(c: object, refresh_token: str) -> int:
                    res = await c.post(  # type: ignore[attr-defined]
                        "/auth/refresh",
                        json={"refresh_token": refresh_token},
                        headers=tenant_headers,
                    )
                    return int(res.status_code)

                async def suspend(c: object, target: str) -> int:
                    res = await c.post(  # type: ignore[attr-defined]
                        f"/users/{target}/status",
                        json={"version": 1, "status": "suspended"},
                        headers=admin_headers,
                    )
                    return int(res.status_code)

                rotate_status, suspend_status = await asyncio.gather(
                    rotate(client, tokens["refresh_token"]), suspend(client, target_id)
                )
            assert suspend_status == 200, (rotate_status, suspend_status)
            assert rotate_status in (200, 401), (rotate_status, suspend_status)
            assert await _active_refresh_and_pending_otp(tid, target_id) == (0, 0)

    asyncio.run(run())
