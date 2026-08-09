"""Idempotency-Key adversarial suite (P3, gate 1.4): exactly one effect.

P14.5 adds FM5: the stored claim is scoped (tenant, actor principal,
route) — a cross-actor replay is a MISS, never a shared response.
"""

import asyncio
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


async def _challenge_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (await session.execute(text("SELECT count(*) FROM otp_challenges"))).scalar_one()
    return int(count)


def test_replay_returns_stored_response_with_one_effect() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid), "idempotency-key": uuid.uuid4().hex}
        async with api_client() as client:
            first = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            second = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 202
        assert second.headers.get("idempotency-replayed") == "true"
        assert await _challenge_count(tid) == 1

    asyncio.run(run())


def test_same_key_different_payload_conflicts() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid), "idempotency-key": uuid.uuid4().hex}
        async with api_client() as client:
            first = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            second = await client.post(
                "/auth/otp/request", json={"email": unique_email()}, headers=headers
            )
        assert first.status_code == 202
        assert second.status_code == 409

    asyncio.run(run())


def test_concurrent_identical_keys_produce_exactly_one_effect() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid), "idempotency-key": uuid.uuid4().hex}
        async with api_client() as client:
            results = await asyncio.gather(
                client.post("/auth/otp/request", json={"email": email}, headers=headers),
                client.post("/auth/otp/request", json={"email": email}, headers=headers),
            )
        statuses = sorted(r.status_code for r in results)
        assert 202 in statuses
        assert all(s in {202, 409} for s in statuses)
        assert await _challenge_count(tid) == 1

    asyncio.run(run())


async def _member_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
    return int(count)


def test_fm5_cross_actor_replay_is_a_miss() -> None:
    """P14.5 FM5 (the !29 R4 lesson completed): the stored claim is
    scoped (tenant, actor principal, route), so a DIFFERENT actor
    presenting the SAME Idempotency-Key with the SAME body EXECUTES
    their own request — a MISS with its own new effect — and can never
    fetch the first actor's stored response. The same actor's replay
    still short-circuits (the scoping narrows, never disables).

    Hand-computed oracle: actor A creates member #1 (201, effect 1);
    actor B, same key + same body, creates member #2 (201, effect 2,
    NO replay header, a DIFFERENT id); actor A replays (201, replay
    header, member #1's exact id, still 2 rows). Falsifiable: drop the
    actor from scoped_storage_key and B replays A's stored 201 — the
    distinct-id and row-count assertions fail.
    """

    async def run() -> None:
        email = unique_email()
        tid, role_id = await seed_user(email)
        async with tenant_session(factory(), tid) as session:
            await seed_permissions(session, tid)
            actor_a = (
                await session.execute(
                    text("SELECT id FROM users WHERE email = :email"), {"email": email}
                )
            ).scalar_one()
            actor_b = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                    "'Second Actor', :email)"
                ),
                {
                    "id": str(actor_b),
                    "tid": str(tid),
                    "rid": str(role_id),
                    "email": unique_email(),
                },
            )
        token_a = issue_access_token(
            AuthContext(user_id=uuid.UUID(str(actor_a)), tenant_id=tid, role_id=role_id)
        )
        token_b = issue_access_token(AuthContext(user_id=actor_b, tenant_id=tid, role_id=role_id))
        key = uuid.uuid4().hex
        body = {"type": "person", "name": "FM5 Member"}
        async with api_client() as client:
            first = await client.post(
                "/members",
                json=body,
                headers={"authorization": f"Bearer {token_a}", "idempotency-key": key},
            )
            assert first.status_code == 201, first.text
            assert await _member_count(tid) == 1

            miss = await client.post(
                "/members",
                json=body,
                headers={"authorization": f"Bearer {token_b}", "idempotency-key": key},
            )
            assert miss.status_code == 201, miss.text
            assert miss.headers.get("idempotency-replayed") is None, (
                "a cross-actor replay must MISS, never serve the stored response"
            )
            assert miss.json()["id"] != first.json()["id"]
            assert await _member_count(tid) == 2

            replay = await client.post(
                "/members",
                json=body,
                headers={"authorization": f"Bearer {token_a}", "idempotency-key": key},
            )
            assert replay.status_code == 201
            assert replay.headers.get("idempotency-replayed") == "true"
            assert replay.json()["id"] == first.json()["id"]
            assert await _member_count(tid) == 2, "the same-actor replay adds no effect"

    asyncio.run(run())
