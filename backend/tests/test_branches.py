"""P13.6 branches registry suite.

Covers the EXIT criteria of BUILD_PROMPTS P13.6:
- matrix coverage of every new route for every role (the spec-walk
  test in test_rbac_matrix.py structurally covers the same routes),
- atomic (tenant_id, name) creation claim, including the concurrent
  race (v1.1 rule 5),
- optimistic locking (409 on stale version), duplicate-rename 409,
- keyset listing pagination,
- user/member assignment with audit before/after, foreign-tenant
  refusal, and tenant-predicate falsifiability (issue #17 pattern),
- backfill idempotence proven by SIDE-EFFECT ROW COUNTS: a re-run
  scans nothing (anti-join on the branch_id claim key) and changes
  nothing (v1.1 rule 8),
- Idempotency-Key replay proven by side-effect row counts (gate 1.4).

Falsifiability (MASTER_PROMPT §4): each guard test asserts the exact
refusal status AND untouched side-effect state, so removing the guard
(ON CONFLICT claim, version predicate, tenant predicate, branch_id
anti-join) makes the corresponding test fail.

Cross-tenant leakage for the branches TABLE (raw SQL through the app
role) lives in test_tenancy_leakage.py with the rest of the suite.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, unique_email
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import branches as branches_service
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _create_branch(admin_token: str, name: str) -> str:
    async with api_client() as client:
        res = await client.post(
            "/branches",
            json={"name": name},
            headers={"authorization": f"Bearer {admin_token}"},
        )
        assert res.status_code == 201, res.text
        branch_id: str = res.json()["id"]
    return branch_id


async def _seed_legacy_user(tid: uuid.UUID, branch_text: str | None) -> uuid.UUID:
    """Insert a user carrying only the legacy free-text branch column."""
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE tenant_id = CAST(:tid AS uuid) LIMIT 1"),
                {"tid": str(tid)},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email, branch) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Legacy User', :email, :branch)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
                "branch": branch_text,
            },
        )
    return user_id


async def _user_branch_state(tid: uuid.UUID, user_id: uuid.UUID) -> tuple[str | None, int]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT branch_id, version FROM users WHERE id = CAST(:id AS uuid)"),
                {"id": str(user_id)},
            )
        ).first()
    assert row is not None
    return (str(row[0]) if row[0] is not None else None, int(row[1]))


def test_branches_routes_matrix_every_role() -> None:
    """Every new route x every role; expectations from the P4 seed matrix.

    Registry CRUD sits under settings; user assignment under
    access_control x edit (the P13.5 users precedent); member
    assignment under members x edit (the P8 precedent).
    """

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, admin_token = await seed_actor()
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            headers = {"authorization": f"Bearer {token}"}
            grants = matrix[role_name]
            can_view = grants[Module.SETTINGS][Action.VIEW]
            can_create = grants[Module.SETTINGS][Action.CREATE]
            can_edit = grants[Module.SETTINGS][Action.EDIT]
            can_user_edit = grants[Module.ACCESS_CONTROL][Action.EDIT]
            can_member_edit = grants[Module.MEMBERS][Action.EDIT]
            # Fresh targets per role so versions are deterministic (v1).
            branch_id = await _create_branch(admin_token, f"Matrix-{uuid.uuid4().hex[:8]}")
            target_user, _ = await add_user(tid, "Teller")
            target_member = await seed_member(tid, name="Branch Matrix Member")
            async with api_client() as client:
                res = await client.get("/branches", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.get(f"/branches/{branch_id}", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name

                res = await client.post(
                    "/branches",
                    json={"name": f"Probe-{uuid.uuid4().hex[:8]}"},
                    headers=headers,
                )
                assert res.status_code == (201 if can_create else 403), role_name

                res = await client.put(
                    f"/branches/{branch_id}",
                    json={"version": 1, "name": f"Edited-{uuid.uuid4().hex[:8]}"},
                    headers=headers,
                )
                assert res.status_code == (200 if can_edit else 403), role_name

                res = await client.put(
                    f"/branches/{branch_id}/users/{target_user}",
                    json={"version": 1},
                    headers=headers,
                )
                assert res.status_code == (200 if can_user_edit else 403), role_name

                res = await client.put(
                    f"/branches/{branch_id}/members/{target_member}",
                    json={"version": 1},
                    headers=headers,
                )
                assert res.status_code == (200 if can_member_edit else 403), role_name

                res = await client.post(
                    "/jobs/branch-backfill", json={"batch_size": 10}, headers=headers
                )
                assert res.status_code == (200 if can_edit else 403), role_name

    asyncio.run(run())


def test_create_branch_name_claim_is_atomic() -> None:
    """Concurrent identical creates -> exactly one 201 and one 409.

    Hand-computed oracle: two racing POSTs for the same name leave
    branches=1 and audit(branch.create)=1. Falsifiability: replace the
    ON CONFLICT DO NOTHING claim with SELECT-then-INSERT and the race
    yields two rows or an unhandled IntegrityError — this test fails.
    """

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        name = f"Race-{uuid.uuid4().hex[:8]}"
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:

            async def create() -> int:
                res = await client.post("/branches", json={"name": name}, headers=headers)
                return res.status_code

            statuses = await asyncio.gather(create(), create())
        assert sorted(statuses) == [201, 409], statuses
        assert await count(tid, "SELECT count(*) FROM branches WHERE name = :n", n=name) == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'branch.create' AND after->>'name' = :n",
                n=name,
            )
            == 1
        )

    asyncio.run(run())


def test_create_branch_rejects_unknown_fields_and_blank_names() -> None:
    """extra="forbid" (gate 1.6) and non-blank names (DB CHECK mirror)."""

    async def run() -> None:
        _, _, admin_token = await seed_actor()
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.post(
                "/branches",
                json={"name": "Extra Field", "code": "HQ-1"},
                headers=headers,
            )
            assert res.status_code == 422
            res = await client.post("/branches", json={"name": "   "}, headers=headers)
            assert res.status_code == 422
            res = await client.post("/branches", json={"name": ""}, headers=headers)
            assert res.status_code == 422

    asyncio.run(run())


def test_update_branch_optimistic_locking() -> None:
    """Stale version -> 409 and no second write (gate 1.4)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        branch_id = await _create_branch(admin_token, "Lock Probe")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            first = await client.put(
                f"/branches/{branch_id}",
                json={"version": 1, "name": "Edited Once"},
                headers=headers,
            )
            assert first.status_code == 200
            assert first.json()["version"] == 2
            stale = await client.put(
                f"/branches/{branch_id}",
                json={"version": 1, "name": "Stale Write"},
                headers=headers,
            )
            assert stale.status_code == 409
        async with tenant_session(factory(), tid) as session:
            name = (
                await session.execute(
                    text("SELECT name FROM branches WHERE id = CAST(:id AS uuid)"),
                    {"id": branch_id},
                )
            ).scalar_one()
        assert str(name) == "Edited Once"
        # Exactly one audit row: the stale write never landed (gate 1.5).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'branch.update' AND entity_id = :eid",
                eid=branch_id,
            )
            == 1
        )

    asyncio.run(run())


def test_update_branch_duplicate_name_conflict() -> None:
    async def run() -> None:
        _, _, admin_token = await seed_actor()
        await _create_branch(admin_token, "Taken Name")
        other_id = await _create_branch(admin_token, "Other Name")
        async with api_client() as client:
            res = await client.put(
                f"/branches/{other_id}",
                json={"version": 1, "name": "Taken Name"},
                headers={"authorization": f"Bearer {admin_token}"},
            )
            assert res.status_code == 409

    asyncio.run(run())


def test_branches_list_keyset_pagination() -> None:
    async def run() -> None:
        _, _, admin_token = await seed_actor()
        created = {await _create_branch(admin_token, f"Page-{i}") for i in range(3)}
        headers = {"authorization": f"Bearer {admin_token}"}
        seen: list[str] = []
        cursor: str | None = None
        async with api_client() as client:
            while True:
                params: dict[str, str] = {"limit": "2"}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get("/branches", params=params, headers=headers)
                assert res.status_code == 200
                page = res.json()
                assert len(page["items"]) <= 2
                seen.extend(item["id"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            # 3 branches total, no duplicates, no gaps.
            assert len(seen) == 3
            assert set(seen) == created
            res = await client.get("/branches?cursor=not-a-cursor", headers=headers)
            assert res.status_code == 400

    asyncio.run(run())


def test_create_branch_idempotency_replay_by_side_effects() -> None:
    """Same Idempotency-Key twice -> one branch row, one audit row; the
    replay returns the stored response (gate 1.4, §4)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        name = f"Idem-{uuid.uuid4().hex[:8]}"
        headers = {
            "authorization": f"Bearer {admin_token}",
            "idempotency-key": f"branch-create-{uuid.uuid4().hex}",
        }
        async with api_client() as client:
            first = await client.post("/branches", json={"name": name}, headers=headers)
            assert first.status_code == 201
            second = await client.post("/branches", json={"name": name}, headers=headers)
            assert second.status_code == 201
            assert second.headers.get("idempotency-replayed") == "true"
            assert second.json()["id"] == first.json()["id"]
        assert await count(tid, "SELECT count(*) FROM branches WHERE name = :n", n=name) == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'branch.create' AND entity_id = :eid",
                eid=first.json()["id"],
            )
            == 1
        )

    asyncio.run(run())


def test_assign_user_and_member_to_branch() -> None:
    """Assignment sets branch_id, bumps version, audits before/after.

    Hand-computed oracle: fresh rows carry version 1; a successful
    assignment leaves version 2 and an audit row with before
    branch_id=None. Re-assigning the SAME branch is a refused no-op
    (409); a stale version is a 409 that changes nothing.
    """

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        branch_a = await _create_branch(admin_token, "Assign A")
        branch_b = await _create_branch(admin_token, "Assign B")
        target_user, _ = await add_user(tid, "Teller")
        target_member = await seed_member(tid, name="Assign Member")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.put(
                f"/branches/{branch_a}/users/{target_user}",
                json={"version": 1},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["branch_id"] == branch_a
            assert body["branch_name"] == "Assign A"
            assert body["version"] == 2
            assert (await _user_branch_state(tid, target_user)) == (branch_a, 2)

            # Same-branch reassignment is a refused no-op.
            res = await client.put(
                f"/branches/{branch_a}/users/{target_user}",
                json={"version": 2},
                headers=headers,
            )
            assert res.status_code == 409
            # Stale version -> 409, nothing moves.
            res = await client.put(
                f"/branches/{branch_b}/users/{target_user}",
                json={"version": 1},
                headers=headers,
            )
            assert res.status_code == 409
            assert (await _user_branch_state(tid, target_user)) == (branch_a, 2)
            # Re-assignment to another branch with the right version.
            res = await client.put(
                f"/branches/{branch_b}/users/{target_user}",
                json={"version": 2},
                headers=headers,
            )
            assert res.status_code == 200
            assert (await _user_branch_state(tid, target_user)) == (branch_b, 3)
            # The users read surface exposes the registry FK read-only
            # (!24 review finding #3, closed in P13.7).
            res = await client.get(f"/users/{target_user}", headers=headers)
            assert res.status_code == 200
            assert res.json()["branch_id"] == branch_b

            res = await client.put(
                f"/branches/{branch_a}/members/{target_member}",
                json={"version": 1},
                headers=headers,
            )
            assert res.status_code == 200
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text("SELECT branch_id, version FROM members WHERE id = CAST(:id AS uuid)"),
                    {"id": str(target_member)},
                )
            ).first()
        assert row is not None
        assert str(row[0]) == branch_a
        assert int(row[1]) == 2
        # Audit rows with before/after for both entities (gate 1.5).
        async with tenant_session(factory(), tid) as session:
            audit = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE action = 'user.branch_assign' AND entity_id = :eid "
                        "ORDER BY at ASC LIMIT 1"
                    ),
                    {"eid": str(target_user)},
                )
            ).first()
        assert audit is not None
        before, after = audit
        assert before["branch_id"] is None
        assert after["branch_id"] == branch_a
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'member.branch_assign' AND entity_id = :eid",
                eid=str(target_member),
            )
            == 1
        )

    asyncio.run(run())


def test_assignment_refuses_foreign_branch_and_foreign_target() -> None:
    """A branch or target from ANOTHER tenant is a 404, never a link."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        foreign_tid, _, foreign_token = await seed_actor()
        our_branch = await _create_branch(admin_token, "Ours")
        foreign_branch = await _create_branch(foreign_token, "Theirs")
        target_user, _ = await add_user(tid, "Teller")
        foreign_user, _ = await add_user(foreign_tid, "Teller")
        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.put(
                f"/branches/{foreign_branch}/users/{target_user}",
                json={"version": 1},
                headers=headers,
            )
            assert res.status_code == 404
            res = await client.put(
                f"/branches/{our_branch}/users/{foreign_user}",
                json={"version": 1},
                headers=headers,
            )
            assert res.status_code == 404
        assert (await _user_branch_state(tid, target_user)) == (None, 1)
        assert (await _user_branch_state(foreign_tid, foreign_user)) == (None, 1)

    asyncio.run(run())


def test_branch_service_tenant_predicates_refuse_foreign_tenant() -> None:
    """v1.1 rule 4 falsifiability (issue #17 pattern): session AS the
    row's tenant so RLS passes; a foreign tenant_id argument must be
    refused by the explicit predicate alone."""

    async def run() -> None:
        tid, admin_id, admin_token = await seed_actor()
        branch_id = await _create_branch(admin_token, "Predicate Probe")
        target_user, _ = await add_user(tid, "Teller")
        foreign = uuid.uuid4()
        bid = uuid.UUID(branch_id)
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await branches_service.get_branch(session, foreign, bid)
            with pytest.raises(NotFoundError):
                await branches_service.update_branch(
                    session, foreign, admin_id, bid, version=1, name="hijack"
                )
            with pytest.raises(NotFoundError):
                await branches_service.assign_user_branch(
                    session, foreign, admin_id, target_user, version=1, branch_id=bid
                )
            page = await branches_service.list_branches(session, foreign)
            assert page.items == []
        async with tenant_session(factory(), tid) as session:
            name = (
                await session.execute(
                    text("SELECT name FROM branches WHERE id = CAST(:id AS uuid)"),
                    {"id": branch_id},
                )
            ).scalar_one()
        assert str(name) == "Predicate Probe"
        assert (await _user_branch_state(tid, target_user)) == (None, 1)

    asyncio.run(run())


def test_branch_backfill_creates_links_and_rerun_is_a_noop() -> None:
    """Backfill idempotence proven by side-effect row counts (P13.6 EXIT).

    Seeded state (hand-computed oracle):
      - 'Thika' branch pre-exists (created via the API, 1 audit row);
      - legacy users: 'HQ', 'Thika', ' HQ ' (trims to 'HQ'), '   '
        (whitespace-only -> excluded: a blank name violates the
        branches CHECK), plus the admin with branch NULL (excluded).
    First run (batch_size=2 to exercise multiple batches):
      scanned=3, branches_created=1 (only 'HQ'; 'Thika' existed),
      users_linked=3; both 'HQ' users share one branch row.
    Re-run: scanned=0 (anti-join on the branch_id claim key — v1.1
    rule 8), zero new rows anywhere, user versions untouched.
    Falsifiability: drop the `branch_id IS NULL` predicate from the
    scan/link and the re-run relinks (version bumps, audit rows grow)
    — the equality assertions below fail.
    """

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        await _create_branch(admin_token, "Thika")
        user_hq = await _seed_legacy_user(tid, "HQ")
        user_thika = await _seed_legacy_user(tid, "Thika")
        user_hq_padded = await _seed_legacy_user(tid, " HQ ")
        user_blank = await _seed_legacy_user(tid, "   ")
        headers = {"authorization": f"Bearer {admin_token}"}

        async def snapshot() -> tuple[int, int, int, list[tuple[str | None, int]]]:
            branches = await count(tid, "SELECT count(*) FROM branches")
            create_audits = await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'branch.create'",
            )
            link_audits = await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'user.branch_backfill'",
            )
            states = [
                await _user_branch_state(tid, uid)
                for uid in (user_hq, user_thika, user_hq_padded, user_blank)
            ]
            return branches, create_audits, link_audits, states

        async with api_client() as client:
            res = await client.post(
                "/jobs/branch-backfill", json={"batch_size": 2}, headers=headers
            )
            assert res.status_code == 200, res.text
            first = res.json()
        assert first["scanned"] == 3
        assert first["branches_created"] == 1
        assert first["users_linked"] == 3
        assert first["batches"] == 2

        branches, create_audits, link_audits, states = await snapshot()
        # 2 branches: pre-existing 'Thika' + backfilled 'HQ'.
        assert branches == 2
        # 2 branch.create audits: one API create + one backfill create.
        assert create_audits == 2
        assert link_audits == 3
        hq_state, thika_state, hq_padded_state, blank_state = states
        assert hq_state[1] == 2 and thika_state[1] == 2 and hq_padded_state[1] == 2
        # ' HQ ' trimmed into the SAME branch row as 'HQ'.
        assert hq_state[0] is not None
        assert hq_padded_state[0] == hq_state[0]
        assert thika_state[0] is not None and thika_state[0] != hq_state[0]
        # Whitespace-only text is never a branch; the user stays manual.
        assert blank_state == (None, 1)
        # The linked user's legacy text is untouched (§3: contract later).
        async with tenant_session(factory(), tid) as session:
            legacy = (
                await session.execute(
                    text("SELECT branch FROM users WHERE id = CAST(:id AS uuid)"),
                    {"id": str(user_hq_padded)},
                )
            ).scalar_one()
        assert str(legacy) == " HQ "

        # Re-run: a lock-free no-op proven by side-effect counts.
        async with api_client() as client:
            res = await client.post(
                "/jobs/branch-backfill", json={"batch_size": 2}, headers=headers
            )
            assert res.status_code == 200
            second = res.json()
        assert second == {
            "scanned": 0,
            "branches_created": 0,
            "users_linked": 0,
            "batches": 0,
        }
        assert (await snapshot()) == (branches, create_audits, link_audits, states)

    asyncio.run(run())
