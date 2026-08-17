"""Matrix-driven endpoint tests: every access endpoint x every role (P4)."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.rbac import seed_permissions
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_role_user(role_name: str) -> tuple[uuid.UUID, uuid.UUID, str]:
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
    return tid, role_id, token


def test_every_endpoint_for_every_role() -> None:
    async def run() -> None:
        matrix = seed_matrix()
        for role_name in ROLE_NAMES:
            _, role_id, token = await _seed_role_user(role_name)
            headers = {"authorization": f"Bearer {token}"}
            can_view = matrix[role_name][Module.ACCESS_CONTROL][Action.VIEW]
            can_edit = matrix[role_name][Module.ACCESS_CONTROL][Action.EDIT]
            async with api_client() as client:
                res = await client.get("/me/permissions", headers=headers)
                assert res.status_code == 200, role_name
                perms = {p["module"]: p for p in res.json()["permissions"]}
                for module in Module:
                    for action in Action:
                        expected = matrix[role_name][module][action]
                        assert perms[module.value][f"can_{action.value}"] == expected, (
                            f"{role_name} {module.value} {action.value}"
                        )

                res = await client.get("/access/roles", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name

                res = await client.get(f"/access/roles/{role_id}/permissions", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name

                res = await client.put(
                    f"/access/roles/{role_id}/permissions/members",
                    json={
                        "can_view": True,
                        "can_create": False,
                        "can_edit": False,
                        "can_approve": False,
                    },
                    headers=headers,
                )
                assert res.status_code == (200 if can_edit else 403), role_name

    asyncio.run(run())


def test_requests_without_token_are_unauthenticated() -> None:
    async def run() -> None:
        async with api_client() as client:
            res = await client.get("/access/roles")
            assert res.status_code == 401
            res = await client.get("/me/permissions")
            assert res.status_code == 401

    asyncio.run(run())


def test_permission_update_is_audited() -> None:
    async def run() -> None:
        tid, role_id, token = await _seed_role_user("System Admin")
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            res = await client.put(
                f"/access/roles/{role_id}/permissions/reports",
                json={
                    "can_view": False,
                    "can_create": False,
                    "can_edit": False,
                    "can_approve": False,
                },
                headers=headers,
            )
            assert res.status_code == 200
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE action = 'permission.update' AND entity = 'permissions' "
                        "ORDER BY at DESC LIMIT 1"
                    )
                )
            ).first()
        assert row is not None
        before, after = row
        assert before["can_view"] is True
        assert after["can_view"] is False

    asyncio.run(run())


def test_seed_is_idempotent() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email, role_name="System Admin")
        async with tenant_session(factory(), tid) as session:
            first = await seed_permissions(session, tid)
        async with tenant_session(factory(), tid) as session:
            second = await seed_permissions(session, tid)
            count = (await session.execute(text("SELECT count(*) FROM permissions"))).scalar_one()
        assert first == second
        # 7 roles x 8 modules (7 prototype + the P13.15 corrections
        # module) = 56 seeded permission rows.
        assert int(count) == 56

    asyncio.run(run())
