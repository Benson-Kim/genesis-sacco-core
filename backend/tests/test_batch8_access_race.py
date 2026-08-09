"""#31 batch 8, ledger (h4) — the permissions SELF-EDIT race.

An operator whose role holds access_control:edit fires TWO CONCURRENT,
IDENTICAL revocations of their OWN role's access_control edit bit (the
"saw off the branch you sit on" edit), then a THIRD attempt with the
same still-valid token.

Effects are proven by SIDE-EFFECT ROW COUNTS (§4 — never by return
values alone):

- The single (role, access_control) permissions row ends with
  can_edit = false — one row, the revoked state.
- EXACTLY ONE audit row records the REAL transition
  (before.can_edit = true -> after.can_edit = false). The FOR UPDATE in
  rbac.update_permission serializes the writers, so the second writer
  (when it is authorized at all) reads the ALREADY-REVOKED before-state
  and its audit row is a no-op echo (before == after). FALSIFIABLE:
  drop the FOR UPDATE and both writers read can_edit = true — TWO
  real-transition audit rows appear and the count assertion fails.
- Each racer returns 200 or 403 (403 when its per-request authz check
  ran after the first commit — the grant is NEVER cached in the token),
  never a 5xx; at least one racer succeeds.
- The third attempt is 403 with ZERO new side-effect rows: the
  self-revocation binds immediately for the very principal that made
  it.

Hand-computed oracles throughout; nothing is captured from the
implementation under test.
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

#: The revocation body: access_control keeps view (so reads keep
#: working) and loses edit — the flags an admin would flip to demote
#: their own role to read-only access control.
REVOKE_EDIT_BODY = {
    "can_view": True,
    "can_create": False,
    "can_edit": False,
    "can_approve": False,
}


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


async def _side_effect_counts(tid: uuid.UUID, role_id: uuid.UUID) -> tuple[int, int, bool]:
    """(real-transition audit rows, total audit rows, row can_edit) for
    the raced (role, access_control) permission."""
    async with tenant_session(factory(), tid) as session:
        real_transitions = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND action = 'permission.update' AND entity = 'permissions' "
                    "AND entity_id = :eid "
                    "AND before->>'can_edit' = 'true' AND after->>'can_edit' = 'false'"
                ),
                {"tid": str(tid), "eid": f"{role_id}:access_control"},
            )
        ).scalar_one()
        total_rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND action = 'permission.update' AND entity = 'permissions' "
                    "AND entity_id = :eid"
                ),
                {"tid": str(tid), "eid": f"{role_id}:access_control"},
            )
        ).scalar_one()
        can_edit = (
            await session.execute(
                text(
                    "SELECT can_edit FROM permissions "
                    "WHERE role_id = CAST(:rid AS uuid) AND module = 'access_control'"
                ),
                {"rid": str(role_id)},
            )
        ).scalar_one()
    return int(real_transitions), int(total_rows), bool(can_edit)


def test_self_revocation_race_exactly_one_real_transition() -> None:
    async def run() -> None:
        tid, role_id, token = await _seed_admin()
        headers = {"authorization": f"Bearer {token}"}
        url = f"/access/roles/{role_id}/permissions/access_control"

        async with api_client() as client:
            first, second = await asyncio.gather(
                client.put(url, json=REVOKE_EDIT_BODY, headers=headers),
                client.put(url, json=REVOKE_EDIT_BODY, headers=headers),
            )

            statuses = sorted([first.status_code, second.status_code])
            # Every legal interleaving; NEVER an unhandled error.
            assert statuses in ([200, 200], [200, 403]), statuses

            real, total, can_edit = await _side_effect_counts(tid, role_id)
            # The revoked state landed…
            assert can_edit is False
            # …through EXACTLY ONE real transition (the FOR UPDATE
            # guard); any second 200 audited a no-op echo only.
            assert real == 1, (real, total, statuses)
            assert total == statuses.count(200)

            # THIRD attempt, same still-valid token: the revocation
            # binds immediately — 403, zero new side-effect rows.
            third = await client.put(url, json=REVOKE_EDIT_BODY, headers=headers)
            assert third.status_code == 403
            # Least disclosure: the refusal echoes no permission flags.
            assert "can_edit" not in json.dumps(third.json())

            real_after, total_after, can_edit_after = await _side_effect_counts(tid, role_id)
            assert (real_after, total_after, can_edit_after) == (real, total, False)

    asyncio.run(run())


def test_concurrent_opposing_edits_serialize_with_truthful_audit_chain() -> None:
    """Two operators race OPPOSING edits of the same permission row: the
    winner's after-state is the loser's before-state (the FOR UPDATE
    chain), so the audit trail replays to the final row state with NO
    gap. Falsifiable: without the row lock both read the seeded state
    and the chain breaks (a before-state appears that no prior
    after-state produced)."""

    async def run() -> None:
        tid, role_id, token = await _seed_admin()
        headers = {"authorization": f"Bearer {token}"}
        # Race a DIFFERENT module's row (reports) so authorization
        # (access_control:edit) stays intact for both racers — this leg
        # isolates write-write serialization from the authz race.
        url = f"/access/roles/{role_id}/permissions/reports"
        grant_all = {"can_view": True, "can_create": True, "can_edit": True, "can_approve": True}
        revoke_all = {
            "can_view": False,
            "can_create": False,
            "can_edit": False,
            "can_approve": False,
        }

        async with api_client() as client:
            first, second = await asyncio.gather(
                client.put(url, json=grant_all, headers=headers),
                client.put(url, json=revoke_all, headers=headers),
            )
            assert first.status_code == 200 and second.status_code == 200

        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'permission.update' AND entity = 'permissions' "
                        "AND entity_id = :eid ORDER BY at, id"
                    ),
                    {"tid": str(tid), "eid": f"{role_id}:reports"},
                )
            ).all()
            final = (
                await session.execute(
                    text(
                        "SELECT can_view, can_create, can_edit, can_approve "
                        "FROM permissions "
                        "WHERE role_id = CAST(:rid AS uuid) AND module = 'reports'"
                    ),
                    {"rid": str(role_id)},
                )
            ).first()

        assert len(rows) == 2
        # Chain truthfulness: each writer's before is the row state its
        # FOR UPDATE actually observed — the second before equals the
        # first after, and the final row equals the last after.
        assert rows[1][0] == rows[0][1], (rows[0], rows[1])
        assert final is not None
        final_state = {
            "can_view": bool(final[0]),
            "can_create": bool(final[1]),
            "can_edit": bool(final[2]),
            "can_approve": bool(final[3]),
        }
        assert final_state == rows[1][1]

    asyncio.run(run())
