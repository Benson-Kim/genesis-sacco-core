"""#31 ledger (j).1: branch roster reads suite.

GET /branches/{id}/users (access_control:view) and GET
/branches/{id}/members (members:view) — expand-only keyset reads over
the 0016 assignment columns, mirroring the batch-4 assignment
permission split: the entity being READ decides the gate, never the
settings registry right (settings rights alone must not disclose
people records).

Covers (hand-computed oracles, MASTER_PROMPT §4):
- roster mixes proving scoping: only the probed branch's people come
  back; the other branch's and the unassigned stay invisible,
- keyset claims: page walks are exhaustive, non-overlapping and in
  the documented order (users: the house created_at DESC, id DESC
  cursor — review F4; members: member_no ASC), the last page carries
  next_cursor null,
- the permission SPLIT route x every role (P4 seed matrix): a role
  with members:view but NOT access_control:view (Teller, Accountant)
  reads the member roster and is REFUSED the user roster,
- 404-BEFORE-FACTS: unknown and cross-tenant branch ids surface 404
  before any roster row is read; rejections echo no roster fact,
- a malformed cursor on EITHER roster is a 400 (semantic validation;
  review F3 added the members leg — bounded length + the as-built
  GP-#### member_no shape), never a silently empty page.

Falsifiability: drop a tenant/branch predicate, flip an ORDER BY, or
move a roster behind the settings gate, and the exact-equality and
matrix assertions below fail.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, create_branch, seed_actor, seed_member_no
from genesis.application.branches import BRANCH_MEMBERS_SCOPE, BRANCH_USERS_SCOPE
from genesis.application.pagination import encode_cursor
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _assign_user(tid: uuid.UUID, user_id: uuid.UUID, branch_id: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE users SET branch_id = CAST(:bid AS uuid) "
                "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"bid": branch_id, "uid": str(user_id), "tid": str(tid)},
        )


def test_branch_users_roster_scoping_and_keyset() -> None:
    """Hand-computed mix: 3 users on branch A, 1 on branch B, 1
    unassigned. limit=2 walks branch A exhaustively on the house
    (created_at DESC, id DESC) keyset (review F4) with no overlap;
    B's user and the unassigned never appear."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        branch_a = await create_branch(admin_token, f"A-{uuid.uuid4().hex[:8]}")
        branch_b = await create_branch(admin_token, f"B-{uuid.uuid4().hex[:8]}")
        a_users = []
        for _ in range(3):
            uid, _ = await add_user(tid, "Teller")
            await _assign_user(tid, uid, branch_a)
            a_users.append(uid)
        b_user, _ = await add_user(tid, "Teller")
        await _assign_user(tid, b_user, branch_b)
        await add_user(tid, "Teller")  # stays unassigned

        # Hand-computed oracle: read the roster's (created_at, id)
        # pairs back and sort them the documented way — created_at
        # DESC with id DESC breaking same-instant ties (uuid byte
        # order, matching Postgres uuid comparison).
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, created_at FROM users "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND branch_id = CAST(:bid AS uuid)"
                    ),
                    {"tid": str(tid), "bid": branch_a},
                )
            ).all()
        assert {str(r[0]) for r in rows} == {str(u) for u in a_users}
        ordered = sorted(((r[1], uuid.UUID(str(r[0]))) for r in rows), reverse=True)
        expected = [str(u) for _, u in ordered]
        # The page-1 cursor is the SERVED position of its last row —
        # the house '<created_at iso>|<id>' keyset, sealed by the
        # #31 batch-13 opaque codec (deterministic encode, so this
        # stays an exact-equality oracle).
        expected_cursor = encode_cursor(
            f"{ordered[1][0].isoformat()}|{ordered[1][1]}",
            tenant_id=tid,
            endpoint=BRANCH_USERS_SCOPE,
        )

        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.get(
                f"/branches/{branch_a}/users", params={"limit": 2}, headers=headers
            )
            assert res.status_code == 200, res.text
            page1 = res.json()
            assert [u["id"] for u in page1["items"]] == expected[:2]
            assert page1["next_cursor"] == expected_cursor
            res = await client.get(
                f"/branches/{branch_a}/users",
                params={"limit": 2, "cursor": page1["next_cursor"]},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            page2 = res.json()
            assert [u["id"] for u in page2["items"]] == expected[2:]
            assert page2["next_cursor"] is None
            # Identity facts only — least disclosure (gate 1.6): the
            # keyset's created_at is a cursor position, never a
            # serialized field.
            for item in page1["items"] + page2["items"]:
                assert set(item.keys()) == {"id", "full_name", "email", "status"}
            # A malformed cursor is a semantic 400, never a 500.
            res = await client.get(
                f"/branches/{branch_a}/users",
                params={"cursor": "not-a-cursor"},
                headers=headers,
            )
            assert res.status_code == 400, res.text

    asyncio.run(run())


def test_branch_members_roster_scoping_keyset_and_status_mix() -> None:
    """Hand-computed mix on controlled member_nos: 3 members (active +
    dormant) on branch A, 1 on branch B, 1 unassigned. limit=2 walks
    branch A exhaustively in member_no ASC order; the dormant member's
    status comes back VERBATIM (the roster never filters or invents
    activity)."""

    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        branch_a = await create_branch(admin_token, f"A-{uuid.uuid4().hex[:8]}")
        branch_b = await create_branch(admin_token, f"B-{uuid.uuid4().hex[:8]}")
        await seed_member_no(tid, "GP-7001", name="Alpha", branch_id=branch_a)
        await seed_member_no(tid, "GP-7002", name="Bravo", status="dormant", branch_id=branch_a)
        await seed_member_no(tid, "GP-7003", name="Carol", branch_id=branch_a)
        await seed_member_no(tid, "GP-7004", name="Delta", branch_id=branch_b)
        await seed_member_no(tid, "GP-7005", name="Echo")  # unassigned

        headers = {"authorization": f"Bearer {admin_token}"}
        async with api_client() as client:
            res = await client.get(
                f"/branches/{branch_a}/members", params={"limit": 2}, headers=headers
            )
            assert res.status_code == 200, res.text
            page1 = res.json()
            assert [(m["member_no"], m["status"]) for m in page1["items"]] == [
                ("GP-7001", "active"),
                ("GP-7002", "dormant"),
            ]
            # #31 batch 13: the served cursor is the opaque signed
            # seal of the same member_no position.
            assert page1["next_cursor"] == encode_cursor(
                "GP-7002", tenant_id=tid, endpoint=BRANCH_MEMBERS_SCOPE
            )
            res = await client.get(
                f"/branches/{branch_a}/members",
                params={"limit": 2, "cursor": page1["next_cursor"]},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            page2 = res.json()
            assert [m["member_no"] for m in page2["items"]] == ["GP-7003"]
            assert page2["next_cursor"] is None
            # Identity facts only — no contact details, no figures.
            for item in page1["items"] + page2["items"]:
                assert set(item.keys()) == {"id", "member_no", "name", "status"}
            # Review F3 — the mirror of the users malformed-cursor
            # leg: anything outside the bounded as-built GP-####
            # member_no shape is a semantic 400, never a silently
            # empty page (a raw bind would return 200 with zero rows).
            for bad in ("not-a-member-no", "GP-", "GP-12", "G" * 64):
                res = await client.get(
                    f"/branches/{branch_a}/members",
                    params={"cursor": bad},
                    headers=headers,
                )
                assert res.status_code == 400, (bad, res.text)
                assert "GP-7001" not in res.text

    asyncio.run(run())


def test_roster_routes_matrix_every_role_permission_split() -> None:
    """Both rosters x every role — the batch-4 assignment permission
    split MIRRORED on reads: user roster iff access_control:view,
    member roster iff members:view. The seeded matrix contains roles
    holding one but not the other (Teller/Accountant: members yes,
    access_control no) — the split's live proof. Refusals echo no
    roster fact."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, admin_token = await seed_actor()
        branch = await create_branch(admin_token, f"M-{uuid.uuid4().hex[:8]}")
        roster_user, _ = await add_user(tid, "Teller")
        await _assign_user(tid, roster_user, branch)
        await seed_member_no(tid, "GP-7100", name="Matrix Roster Member", branch_id=branch)
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            headers = {"authorization": f"Bearer {token}"}
            can_users = matrix[role_name][Module.ACCESS_CONTROL][Action.VIEW]
            can_members = matrix[role_name][Module.MEMBERS][Action.VIEW]
            async with api_client() as client:
                res = await client.get(f"/branches/{branch}/users", headers=headers)
                assert res.status_code == (200 if can_users else 403), role_name
                if not can_users:
                    assert "Extra User" not in res.text, role_name
                res = await client.get(f"/branches/{branch}/members", headers=headers)
                assert res.status_code == (200 if can_members else 403), role_name
                if not can_members:
                    assert "Matrix Roster Member" not in res.text, role_name

    asyncio.run(run())


def test_roster_404_before_facts_unknown_and_cross_tenant() -> None:
    """404-BEFORE-FACTS on both rosters: an unknown branch id and a
    FOREIGN-tenant branch id surface 404 before any roster row is
    read; the rejection echoes no roster fact."""

    async def run() -> None:
        _, _, token_a = await seed_actor()
        _, _, token_b = await seed_actor()
        foreign_branch = await create_branch(token_b, f"F-{uuid.uuid4().hex[:8]}")
        headers = {"authorization": f"Bearer {token_a}"}
        async with api_client() as client:
            for path in ("users", "members"):
                res = await client.get(f"/branches/{uuid.uuid4()}/{path}", headers=headers)
                assert res.status_code == 404, res.text
                res = await client.get(f"/branches/{foreign_branch}/{path}", headers=headers)
                assert res.status_code == 404, res.text
                assert "items" not in res.json()

    asyncio.run(run())
