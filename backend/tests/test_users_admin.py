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
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import NotFoundError
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
    under the admin-set lock and both calls return 200."""

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
            assert res.status_code == 409
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(admin_id)},
                )
            ).scalar_one()
        assert str(status) == "active"

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
            claims = jwt.decode(
                refreshed.json()["access_token"],
                os.environ["JWT_SIGNING_KEY"],
                algorithms=["HS256"],
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
