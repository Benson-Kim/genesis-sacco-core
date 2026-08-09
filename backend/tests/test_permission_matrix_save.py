"""#35 item 2 — permission-matrix save bug (user-reported: 3 failed attempts).

As-built root cause: PUT /access/roles/{id}/permissions/{module} was
UPDATE-ONLY — a missing permissions row raised NotFoundError (404) —
while both the RBAC readers (has_permission / actor_access) and the web
matrix treat a missing row as a legitimate deny-by-default zero map.
Tenants whose rows predate a later-added Module member (corrections @
P13.15, member_identity @ P14.5; seed_permissions only runs at seed
time) have such holes, so toggling one of those cells failed on EVERY
retry. These tests reproduce that exact state by deleting the seeded
row, and FAIL on the old code (the PUT 404s where 200 is asserted).

Hand-computed oracles throughout; effects proven by side-effect ROW
COUNTS (§4), never return values alone.
"""

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.rbac import seed_permissions
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: The exact bits an operator would grant on a previously-hole cell.
GRANT_VIEW_APPROVE = {
    "can_view": True,
    "can_create": False,
    "can_edit": False,
    "can_approve": True,
}

#: Deny-by-default zero map — what the server ENFORCED for the missing
#: row before the write, so it is the truthful audited before-state.
ZERO_MAP = {"can_view": False, "can_create": False, "can_edit": False, "can_approve": False}


async def _seed_admin() -> tuple[uuid.UUID, uuid.UUID, str]:
    email = unique_email()
    tid, role_id = await seed_user(email, role_name="System Admin")
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
    return tid, role_id, token


async def _delete_permission_row(tid: uuid.UUID, role_id: uuid.UUID, module: str) -> None:
    """Simulate a tenant seeded BEFORE `module` joined the Module enum."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("DELETE FROM permissions WHERE role_id = CAST(:rid AS uuid) AND module = :module"),
            {"rid": str(role_id), "module": module},
        )


async def _row_state(
    tid: uuid.UUID, role_id: uuid.UUID, module: str
) -> tuple[int, dict[str, bool] | None]:
    """(row count, bits) for the (role, module) permission row."""
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT can_view, can_create, can_edit, can_approve FROM permissions "
                    "WHERE role_id = CAST(:rid AS uuid) AND module = :module"
                ),
                {"rid": str(role_id), "module": module},
            )
        ).all()
    if not rows:
        return 0, None
    bits = {
        "can_view": bool(rows[0][0]),
        "can_create": bool(rows[0][1]),
        "can_edit": bool(rows[0][2]),
        "can_approve": bool(rows[0][3]),
    }
    return len(rows), bits


async def _audit_rows(tid: uuid.UUID, role_id: uuid.UUID, module: str) -> list[tuple[dict, dict]]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT before, after FROM audit_log "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND action = 'permission.update' AND entity = 'permissions' "
                    "AND entity_id = :eid ORDER BY at, id"
                ),
                {"tid": str(tid), "eid": f"{role_id}:{module}"},
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


def test_save_on_a_missing_permission_row_succeeds_and_audits_the_zero_map() -> None:
    """REGRESSION (fails on the old update-only code with a 404): a PUT
    to a module whose row was never seeded materializes the row and
    audits the real transition from the deny-by-default zero map."""

    async def run() -> None:
        tid, role_id, token = await _seed_admin()
        headers = {"authorization": f"Bearer {token}"}
        await _delete_permission_row(tid, role_id, "corrections")

        async with api_client() as client:
            res = await client.put(
                f"/access/roles/{role_id}/permissions/corrections",
                json=GRANT_VIEW_APPROVE,
                headers=headers,
            )
            # OLD CODE: 404 "permission row not found" — the user's
            # thrice-failed save. FIXED: the write lands.
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["module"] == "corrections"
            assert body["can_view"] is True and body["can_approve"] is True

            # EXACTLY ONE row exists with the requested bits (row-count
            # proof; falsifiable: drop the unique anchor and a second
            # materialization would double it).
            count, bits = await _row_state(tid, role_id, "corrections")
            assert (count, bits) == (1, GRANT_VIEW_APPROVE)

            # The audit trail records the REAL transition: before is the
            # deny-by-default zero map the server enforced pre-write.
            audit = await _audit_rows(tid, role_id, "corrections")
            assert len(audit) == 1
            assert audit[0][0] == ZERO_MAP
            assert audit[0][1] == GRANT_VIEW_APPROVE

            # An identical retry (the user's natural next click) stays
            # 200 and audits only a no-op echo — no drift, no dupes.
            res2 = await client.put(
                f"/access/roles/{role_id}/permissions/corrections",
                json=GRANT_VIEW_APPROVE,
                headers=headers,
            )
            assert res2.status_code == 200
            count2, bits2 = await _row_state(tid, role_id, "corrections")
            assert (count2, bits2) == (1, GRANT_VIEW_APPROVE)
            audit2 = await _audit_rows(tid, role_id, "corrections")
            assert len(audit2) == 2
            assert audit2[1][0] == audit2[1][1] == GRANT_VIEW_APPROVE

    asyncio.run(run())


def test_unknown_role_is_refused_404_with_zero_rows_created() -> None:
    """Materialization must never invent rows for a role that does not
    exist: an unknown role_id stays a sanitized 404 and writes NOTHING."""

    async def run() -> None:
        tid, _, token = await _seed_admin()
        headers = {"authorization": f"Bearer {token}"}
        ghost_role = uuid.uuid4()

        async with api_client() as client:
            res = await client.put(
                f"/access/roles/{ghost_role}/permissions/members",
                json=GRANT_VIEW_APPROVE,
                headers=headers,
            )
            assert res.status_code == 404
            # Least disclosure: the refusal echoes no permission bits.
            assert "can_view" not in json.dumps(res.json())

        count, _ = await _row_state(tid, ghost_role, "members")
        assert count == 0

    asyncio.run(run())


def test_concurrent_first_writes_to_a_missing_row_serialize_to_one_row() -> None:
    """Two concurrent PUTs race to materialize the SAME missing row: the
    insert-or-skip + re-taken FOR UPDATE serializes them — one row, one
    real transition from the zero map, never a 5xx. Falsifiable: replace
    the insert-or-skip with a plain INSERT and one racer 500s on the
    unique anchor."""

    async def run() -> None:
        tid, role_id, token = await _seed_admin()
        headers = {"authorization": f"Bearer {token}"}
        await _delete_permission_row(tid, role_id, "corrections")
        url = f"/access/roles/{role_id}/permissions/corrections"

        async with api_client() as client:
            first, second = await asyncio.gather(
                client.put(url, json=GRANT_VIEW_APPROVE, headers=headers),
                client.put(url, json=GRANT_VIEW_APPROVE, headers=headers),
            )
            assert sorted([first.status_code, second.status_code]) == [200, 200], (
                first.status_code,
                second.status_code,
            )

        count, bits = await _row_state(tid, role_id, "corrections")
        assert (count, bits) == (1, GRANT_VIEW_APPROVE)

        audit = await _audit_rows(tid, role_id, "corrections")
        # Exactly one REAL transition (zero map -> bits); the loser of
        # the row lock audited a no-op echo (bits -> bits).
        real = [row for row in audit if row[0] == ZERO_MAP and row[1] == GRANT_VIEW_APPROVE]
        echoes = [row for row in audit if row[0] == row[1] == GRANT_VIEW_APPROVE]
        assert len(audit) == 2
        assert len(real) == 1, audit
        assert len(echoes) == 1, audit

    asyncio.run(run())
