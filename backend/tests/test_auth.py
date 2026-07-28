"""Adversarial auth suite (P3): lockout, single-use, expiry, refresh reuse."""

import asyncio
import os
import uuid

import jwt
import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, latest_otp_code, seed_user, unique_email
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def test_otp_flow_issues_short_lived_tokens() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert res.status_code == 202
            code = await latest_otp_code(tid)
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res.status_code == 200
            tokens = res.json()
        claims = jwt.decode(
            tokens["access_token"], os.environ["JWT_SIGNING_KEY"], algorithms=["HS256"]
        )
        assert claims["tid"] == str(tid)
        assert claims["exp"] - claims["iat"] <= 900
        assert tokens["expires_in"] == 900

    asyncio.run(run())


def test_brute_force_lockout_blocks_even_correct_code() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            wrong = "000000" if code != "000000" else "111111"
            for _ in range(5):
                res = await client.post(
                    "/auth/otp/verify", json={"email": email, "code": wrong}, headers=headers
                )
                assert res.status_code == 401
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res.status_code == 401

    asyncio.run(run())


def test_otp_is_single_use() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            first = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert first.status_code == 200
            second = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert second.status_code == 401

    asyncio.run(run())


def test_expired_otp_is_rejected() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("UPDATE otp_challenges SET expires_at = now() - interval '1 second'")
                )
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res.status_code == 401

    asyncio.run(run())


def test_refresh_reuse_revokes_whole_family() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            pair1 = res.json()
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": pair1["refresh_token"]},
                headers=headers,
            )
            assert res.status_code == 200
            pair2 = res.json()
            reuse = await client.post(
                "/auth/refresh",
                json={"refresh_token": pair1["refresh_token"]},
                headers=headers,
            )
            assert reuse.status_code == 401
            revoked = await client.post(
                "/auth/refresh",
                json={"refresh_token": pair2["refresh_token"]},
                headers=headers,
            )
            assert revoked.status_code == 401

    asyncio.run(run())


def test_auth_rate_limit_returns_429() -> None:
    async def run() -> None:
        headers = {"x-tenant-id": str(uuid.uuid4())}
        statuses: list[int] = []
        async with api_client() as client:
            for _ in range(130):
                res = await client.post(
                    "/auth/otp/request", json={"email": "ghost@example.com"}, headers=headers
                )
                statuses.append(res.status_code)
        assert 429 in statuses

    asyncio.run(run())


def test_logout_revokes_family_and_returns_204() -> None:
    """Covers revoke_refresh_token and the /auth/logout endpoint."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res.status_code == 200
            tokens = res.json()
            logout_res = await client.post(
                "/auth/logout",
                json={"refresh_token": tokens["refresh_token"]},
                headers=headers,
            )
            assert logout_res.status_code == 204
            refresh_after = await client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
                headers=headers,
            )
            assert refresh_after.status_code == 401

    asyncio.run(run())


def test_unknown_or_expired_refresh_token_returns_401() -> None:
    """Covers unknown refresh token and expired refresh token in rotate_refresh_token."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            # 1. Unknown refresh token
            res = await client.post(
                "/auth/refresh",
                json={"refresh_token": "a" * 48},
                headers=headers,
            )
            assert res.status_code == 401

            # 2. Expired refresh token
            await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            code = await latest_otp_code(tid)
            res_otp = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res_otp.status_code == 200
            refresh_token = res_otp.json()["refresh_token"]

            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("UPDATE refresh_tokens SET expires_at = now() - interval '1 hour'")
                )

            res_expired = await client.post(
                "/auth/refresh",
                json={"refresh_token": refresh_token},
                headers=headers,
            )
            assert res_expired.status_code == 401

    asyncio.run(run())
