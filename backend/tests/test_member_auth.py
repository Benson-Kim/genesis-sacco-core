"""P14.5 member-facing auth suite (FM1 + the P3 policy on the member principal).

FM1 — audience separation, falsifiable in BOTH directions: remove the
audience dispatch in application/auth.decode_principal (or decode both
kinds through one context) and the 403 assertions below fail. The
member OTP path must obey the P3 policy VERBATIM (single-use, ≤5
attempts, least disclosure) because it IS the P3 machinery — a
divergence here means the reuse mandate (gate 1.1) was broken.

All oracles are hand-computed in comments; punitive/lockout state is
asserted through the API outcome exactly like test_auth.py (the staff
twin this file mirrors, deliberately — same shapes, different
principal).
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import jwt
import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import (
    MEMBER_AUDIENCE,
    STAFF_AUDIENCE,
    AuthContext,
    MemberAuthContext,
    issue_access_token,
    issue_member_access_token,
)
from genesis.application.rbac import seed_permissions
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_member_with_credential(
    tid: uuid.UUID, *, status: str = "active", email: str | None = None
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Member + ACTIVE credential link; returns (member_id, credential_id, email)."""
    mid = uuid.uuid4()
    cid = uuid.uuid4()
    login_email = email or unique_email()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Auth Member', :st)"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"MA-{mid.hex[:6].upper()}", "st": status},
        )
        await session.execute(
            text(
                "INSERT INTO member_credentials (id, tenant_id, member_id, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :email)"
            ),
            {"id": str(cid), "tid": str(tid), "m": str(mid), "email": login_email},
        )
    return mid, cid, login_email


async def _latest_member_otp_code(tid: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        code = (
            await session.execute(
                text(
                    "SELECT payload->>'code' FROM outbox_events "
                    "WHERE event_type = 'member_auth.otp_requested' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    return str(code)


async def _member_challenge_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM otp_challenges WHERE member_credential_id IS NOT NULL")
            )
        ).scalar_one()
    return int(count)


def test_member_otp_flow_issues_member_audience_tokens() -> None:
    """Happy path: the issued access token carries aud=genesis-member,
    sub=<credential id> and mid=<member id>, ≤15 minutes — the FM1
    dispatch discriminator, asserted at the CLAIM level so a regression
    in the issuer (not just the decoder) is caught."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        mid, cid, email = await _seed_member_with_credential(tid)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post(
                "/member/auth/otp/request", json={"email": email}, headers=headers
            )
            assert res.status_code == 202
            # Least disclosure: the response never says whether the
            # credential exists — same body for a real one.
            assert res.json() == {"status": "sent"}
            code = await _latest_member_otp_code(tid)
            res = await client.post(
                "/member/auth/otp/verify",
                json={"email": email, "code": code},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            tokens = res.json()
        claims = jwt.decode(
            tokens["access_token"],
            os.environ["JWT_SIGNING_KEY"],
            algorithms=["HS256"],
            audience=MEMBER_AUDIENCE,
        )
        assert claims["aud"] == MEMBER_AUDIENCE
        assert claims["sub"] == str(cid)
        assert claims["mid"] == str(mid)
        assert claims["tid"] == str(tid)
        assert claims["exp"] - claims["iat"] <= 900
        assert tokens["expires_in"] == 900

    asyncio.run(run())


def test_fm1_member_token_never_satisfies_staff_gate() -> None:
    """FM1 forward direction: a VALID member token is refused with 403
    by every staff RequirePermission gate — including the member
    identity ADMIN surface, so a member can never administer their own
    link (FM3 adjacency). Falsifiable: remove the audience dispatch in
    decode_access_token and the member token decodes as a staff
    context, turning these 403s into 200s/401s."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            await seed_permissions(session, tid)
        mid, cid, _ = await _seed_member_with_credential(tid)
        member_token = issue_member_access_token(
            MemberAuthContext(credential_id=cid, member_id=mid, tenant_id=tid)
        )
        headers = {"authorization": f"Bearer {member_token}"}
        async with api_client() as client:
            for method, path, body in (
                ("GET", "/members", None),
                ("GET", f"/members/{mid}/credentials", None),
                ("POST", f"/members/{mid}/credentials", {"email": unique_email()}),
                ("POST", f"/credentials/{cid}/revoke", {"version": 1}),
            ):
                res = await client.request(method, path, json=body, headers=headers)
                assert res.status_code == 403, f"{method} {path}: {res.status_code}"
                # Least disclosure: the envelope only, never data.
                assert set(res.json()) == {"category", "correlation_id"}

    asyncio.run(run())


def test_fm1_staff_token_never_satisfies_member_gate() -> None:
    """FM1 reverse direction: even the MOST privileged staff token
    (System Admin, every permission) is refused with 403 by the member
    gate — staff act on members through the staff surfaces (the
    attested consent override), never by impersonating the member
    principal."""

    async def run() -> None:
        email = unique_email()
        tid, role_id = await seed_user(email)
        async with tenant_session(factory(), tid) as session:
            await seed_permissions(session, tid)
            user_id = (
                await session.execute(
                    text("SELECT id FROM users WHERE email = :email"), {"email": email}
                )
            ).scalar_one()
        staff_token = issue_access_token(
            AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
        )
        headers = {"authorization": f"Bearer {staff_token}"}
        async with api_client() as client:
            for path in (
                f"/member/guarantees/{uuid.uuid4()}/consent",
                f"/member/guarantees/{uuid.uuid4()}/release",
            ):
                res = await client.post(path, json={"version": 1}, headers=headers)
                assert res.status_code == 403, f"{path}: {res.status_code}"
                assert set(res.json()) == {"category", "correlation_id"}

    asyncio.run(run())


def test_unknown_or_missing_audience_is_refused_on_both_gates() -> None:
    """Deny by default: a signed token WITHOUT an audience (the pre-
    P14.5 legacy shape) or with an alien audience is 401 on BOTH gate
    kinds — the dispatch is a code-owned two-entry mapping, never a
    fall-through to either principal."""

    async def run() -> None:
        tid, role_id = await seed_user(unique_email())
        now = int(datetime.now(UTC).timestamp())
        base = {
            "sub": str(uuid.uuid4()),
            "tid": str(tid),
            "rid": str(role_id),
            "mid": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 900,
        }
        key = os.environ["JWT_SIGNING_KEY"]
        no_aud = jwt.encode(base, key, algorithm="HS256")
        alien = jwt.encode({**base, "aud": "genesis-other"}, key, algorithm="HS256")
        async with api_client() as client:
            for token in (no_aud, alien):
                headers = {"authorization": f"Bearer {token}"}
                staff = await client.get("/members", headers=headers)
                assert staff.status_code == 401, staff.text
                member = await client.post(
                    f"/member/guarantees/{uuid.uuid4()}/release",
                    json={"version": 1},
                    headers=headers,
                )
                assert member.status_code == 401, member.text

    asyncio.run(run())


def test_member_otp_lockout_and_single_use() -> None:
    """The P3 policy verbatim on the member principal: 5 mismatches
    lock the challenge (even the correct code is then refused) and a
    consumed challenge never verifies again. Hand-computed: attempts
    0..4 increment to 5 = OTP_MAX_ATTEMPTS → locked."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        _, _, email = await _seed_member_with_credential(tid)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/member/auth/otp/request", json={"email": email}, headers=headers)
            code = await _latest_member_otp_code(tid)
            wrong = "000000" if code != "000000" else "111111"
            for _ in range(5):
                res = await client.post(
                    "/member/auth/otp/verify",
                    json={"email": email, "code": wrong},
                    headers=headers,
                )
                assert res.status_code == 401
            locked = await client.post(
                "/member/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert locked.status_code == 401, "lockout must refuse even the correct code"

            # Fresh challenge: correct once, then single-use.
            await client.post("/member/auth/otp/request", json={"email": email}, headers=headers)
            code = await _latest_member_otp_code(tid)
            first = await client.post(
                "/member/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert first.status_code == 200
            replay = await client.post(
                "/member/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert replay.status_code == 401, "a consumed member OTP must never verify again"

    asyncio.run(run())


def test_member_refresh_rotation_and_reuse_revokes_family() -> None:
    """The P3 family rule on the member principal: rotation works, the
    spent token is dead, and REUSING it kills the whole family — the
    freshly rotated token dies with it."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        _, _, email = await _seed_member_with_credential(tid)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/member/auth/otp/request", json={"email": email}, headers=headers)
            code = await _latest_member_otp_code(tid)
            login = await client.post(
                "/member/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert login.status_code == 200
            first_refresh = login.json()["refresh_token"]

            rotated = await client.post(
                "/member/auth/refresh", json={"refresh_token": first_refresh}, headers=headers
            )
            assert rotated.status_code == 200
            second_refresh = rotated.json()["refresh_token"]

            reuse = await client.post(
                "/member/auth/refresh", json={"refresh_token": first_refresh}, headers=headers
            )
            assert reuse.status_code == 401, "a spent member refresh token must be refused"

            # Family revocation: the descendant dies with the reuse.
            dead = await client.post(
                "/member/auth/refresh", json={"refresh_token": second_refresh}, headers=headers
            )
            assert dead.status_code == 401, "reuse must revoke the whole member family"

    asyncio.run(run())


def test_revoked_credential_dies_within_one_request() -> None:
    """The per-request live-link re-check (RequireMemberPrincipal) and
    the refresh-time re-check: a credential revoked AFTER token issue
    is refused on its very next use — the access token's remaining
    lifetime never bridges a committed revocation (the staff F3
    suspension rule, on the member principal)."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        _mid, cid, email = await _seed_member_with_credential(tid)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            await client.post("/member/auth/otp/request", json={"email": email}, headers=headers)
            code = await _latest_member_otp_code(tid)
            login = await client.post(
                "/member/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert login.status_code == 200
            access = login.json()["access_token"]
            refresh = login.json()["refresh_token"]

            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_credentials SET status = 'revoked', "
                        "revoked_at = now(), version = version + 1 "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(cid)},
                )

            gate = await client.post(
                f"/member/guarantees/{uuid.uuid4()}/release",
                json={"version": 1},
                headers={"authorization": f"Bearer {access}"},
            )
            assert gate.status_code == 401, "the live-link re-check must refuse a revoked link"

            minted = await client.post(
                "/member/auth/refresh", json={"refresh_token": refresh}, headers=headers
            )
            assert minted.status_code == 401, "a revoked link must never mint new tokens"

    asyncio.run(run())


def test_exited_member_cannot_authenticate_and_nothing_is_disclosed() -> None:
    """EXITED is terminal: no challenge row is even created — while the
    response stays the SAME 202 as a live request (least disclosure:
    an attacker cannot probe membership status through this endpoint).
    Oracle: member challenge rows stay at zero."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        _, _, email = await _seed_member_with_credential(tid, status="exited")
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post(
                "/member/auth/otp/request", json={"email": email}, headers=headers
            )
            assert res.status_code == 202
            assert res.json() == {"status": "sent"}
        assert await _member_challenge_count(tid) == 0

        # Unknown email: identical outward behaviour.
        async with api_client() as client:
            res = await client.post(
                "/member/auth/otp/request",
                json={"email": unique_email()},
                headers=headers,
            )
            assert res.status_code == 202
        assert await _member_challenge_count(tid) == 0

    asyncio.run(run())


def test_same_email_staff_and_member_principals_stay_disjoint() -> None:
    """The retired-identity probe: one email that is BOTH a staff user
    and a member credential yields two disjoint principals. A staff
    OTP code never verifies on the member endpoint (and vice versa) —
    the challenges hang off different principal columns (0035 XOR).
    Falsifiable: re-introduce any email-based join between the
    populations and the cross-verification below starts succeeding."""

    async def run() -> None:
        shared_email = unique_email()
        tid, _ = await seed_user(shared_email)
        await _seed_member_with_credential(tid, email=shared_email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            # Staff challenge for the shared email...
            res = await client.post(
                "/auth/otp/request", json={"email": shared_email}, headers=headers
            )
            assert res.status_code == 202
            async with tenant_session(factory(), tid) as session:
                staff_code = (
                    await session.execute(
                        text(
                            "SELECT payload->>'code' FROM outbox_events "
                            "WHERE event_type = 'auth.otp_requested' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                ).scalar_one()
            # ...must NOT verify on the member endpoint: the member
            # principal has no challenge at all yet.
            cross = await client.post(
                "/member/auth/otp/verify",
                json={"email": shared_email, "code": str(staff_code)},
                headers=headers,
            )
            assert cross.status_code == 401, "a staff OTP must never verify a member login"

            # And the member's own code must not verify on the staff
            # endpoint either.
            res = await client.post(
                "/member/auth/otp/request", json={"email": shared_email}, headers=headers
            )
            assert res.status_code == 202
            member_code = await _latest_member_otp_code(tid)
            cross = await client.post(
                "/auth/otp/verify",
                json={"email": shared_email, "code": member_code},
                headers=headers,
            )
            assert cross.status_code == 401, "a member OTP must never verify a staff login"

            # Both principals still log in fine with their OWN codes —
            # disjointness, not breakage. (The staff challenge above is
            # older than the member one only in its own principal's
            # namespace; each verify reads its own newest challenge.)
            staff_ok = await client.post(
                "/auth/otp/verify",
                json={"email": shared_email, "code": str(staff_code)},
                headers=headers,
            )
            assert staff_ok.status_code == 200, staff_ok.text
            member_ok = await client.post(
                "/member/auth/otp/verify",
                json={"email": shared_email, "code": member_code},
                headers=headers,
            )
            assert member_ok.status_code == 200, member_ok.text
        claims = jwt.decode(
            staff_ok.json()["access_token"],
            os.environ["JWT_SIGNING_KEY"],
            algorithms=["HS256"],
            audience=STAFF_AUDIENCE,
        )
        assert claims["aud"] == STAFF_AUDIENCE

    asyncio.run(run())
