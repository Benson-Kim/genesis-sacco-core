"""#35 item 1 — E.164 phone storage at the API boundary.

Falsifiable legs:
- every accepted input format STORES the identical +254… string
  (members AND users write paths — the row is read back and compared
  to the hand-computed oracle);
- invalid input is a sanitized 422: category + correlation id ONLY,
  the offending identifier is NEVER echoed (gate 1.6) and NO row is
  written (side-effect row count);
- the optimistic-locked member update normalizes too, and an invalid
  phone refuses BEFORE the version bump (version unchanged).
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

#: (input, stored) oracles — all four maintainer-declared formats.
ACCEPTED = [
    ("0712345678", "+254712345678"),
    ("+254712345678", "+254712345678"),
    ("0110000000", "+254110000000"),
    ("+254110000000", "+254110000000"),
]

INVALID_PHONE = "0812345678"  # 08 is not a Kenya mobile prefix here


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


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "idempotency-key": uuid.uuid4().hex}


async def _stored_phone(tid: uuid.UUID, table: str, row_id: str) -> str | None:
    assert table in {"members", "users"}  # code-owned literal
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                text(f"SELECT phone FROM {table} WHERE id = CAST(:id AS uuid)"),  # noqa: S608
                {"id": row_id},
            )
        ).scalar_one()
    return str(value) if value is not None else None


def test_member_create_stores_every_accepted_format_as_the_same_e164() -> None:
    async def run() -> None:
        tid, _, token = await _seed_admin()
        async with api_client() as client:
            for raw, stored in ACCEPTED:
                res = await client.post(
                    "/members",
                    json={"type": "person", "name": f"E164 {raw}", "phone": raw},
                    headers=_headers(token),
                )
                assert res.status_code == 201, (raw, res.text)
                body = res.json()
                assert body["phone"] == stored, raw
                assert await _stored_phone(tid, "members", body["id"]) == stored

    asyncio.run(run())


def test_member_create_invalid_phone_is_sanitized_422_and_writes_nothing() -> None:
    async def run() -> None:
        tid, _, token = await _seed_admin()
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Bad Phone", "phone": INVALID_PHONE},
                headers=_headers(token),
            )
            assert res.status_code == 422
            payload = res.json()
            assert payload["category"] == "validation_error"
            # Least disclosure: the identifier is NEVER echoed back.
            assert INVALID_PHONE not in json.dumps(payload)
        async with tenant_session(factory(), tid) as session:
            count = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
        assert int(count) == 0

    asyncio.run(run())


def test_member_update_normalizes_and_refuses_invalid_before_version_bump() -> None:
    async def run() -> None:
        tid, _, token = await _seed_admin()
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Update Me", "phone": "0712345678"},
                headers=_headers(token),
            )
            assert created.status_code == 201
            member = created.json()

            # Local-format update normalizes on write.
            updated = await client.put(
                f"/members/{member['id']}",
                json={"version": member["version"], "phone": "0110000000"},
                headers=_headers(token),
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["phone"] == "+254110000000"
            assert await _stored_phone(tid, "members", member["id"]) == "+254110000000"

            # Invalid phone refuses 422 BEFORE the compare-and-swap:
            # the version is unchanged (a retry with the same version
            # still works) and the stored phone is untouched.
            version_now = updated.json()["version"]
            refused = await client.put(
                f"/members/{member['id']}",
                json={"version": version_now, "phone": INVALID_PHONE},
                headers=_headers(token),
            )
            assert refused.status_code == 422
            assert INVALID_PHONE not in json.dumps(refused.json())
            assert await _stored_phone(tid, "members", member["id"]) == "+254110000000"
            retry = await client.put(
                f"/members/{member['id']}",
                json={"version": version_now, "phone": "0712345678"},
                headers=_headers(token),
            )
            assert retry.status_code == 200, retry.text
            assert retry.json()["phone"] == "+254712345678"

    asyncio.run(run())


def test_user_create_normalizes_phone_and_refuses_invalid_sanitized() -> None:
    async def run() -> None:
        tid, role_id, token = await _seed_admin()
        async with api_client() as client:
            res = await client.post(
                "/users",
                json={
                    "email": unique_email(),
                    "full_name": "Phone User",
                    "role_id": str(role_id),
                    "phone": "0712345678",
                },
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            body = res.json()
            assert body["phone"] == "+254712345678"
            assert await _stored_phone(tid, "users", body["id"]) == "+254712345678"

            refused = await client.post(
                "/users",
                json={
                    "email": unique_email(),
                    "full_name": "Bad Phone User",
                    "role_id": str(role_id),
                    "phone": INVALID_PHONE,
                },
                headers=_headers(token),
            )
            assert refused.status_code == 422
            assert INVALID_PHONE not in json.dumps(refused.json())

    asyncio.run(run())
