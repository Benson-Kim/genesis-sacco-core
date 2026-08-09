"""P13.17(c) / DSA-3 — idempotency key expiry (FM3 suite).

Named failure mode FM3: expiry-fence gap — an expired key replaying a
stale stored response, or a takeover race turning one client retry
into TWO executed money effects. The double-effect vector this suite
pins shut: with a purge job but WITHOUT the ``expires_at > now()``
fence, replay semantics silently depend on purge timing (before the
purge an expired key still replays; after it the same key re-executes)
and a non-atomic takeover lets two concurrent retries of an expired
key both execute. Effects are asserted by SIDE-EFFECT ROW COUNTS
(otp_challenges — one row per executed request), never return values.

Falsifiability, per test (anti-reward-hacking rules):

  * fence removed (drop ``expires_at > now()`` from REPLAY_LOOKUP_SQL
    and the takeover WHERE arm of CLAIM_SQL) →
    test_expired_key_executes_as_new_request_never_a_replay fails
    (the expired key replays: challenge count stays 1, the replayed
    header appears);
  * takeover made non-atomic (SELECT-then-UPDATE instead of the single
    ON CONFLICT statement) →
    test_concurrent_takeover_of_expired_key_exactly_one_new_effect
    fails (both retries execute: challenge count 3);
  * server-set retention bypassed (drop the explicit expires_at from
    CLAIM_SQL, falling to the 0029 column default) →
    test_expires_at_is_server_set_from_retention_config fails (the
    pinned 7h retention is not observed);
  * purge predicate widened (drop ``expires_at <= now()`` from
    PURGE_BATCH_SQL) →
    test_purge_deletes_only_expired_rows_and_rerun_is_noop fails (the
    live claim is deleted and its replay is lost).
"""

import asyncio
import os
import uuid
from functools import partial

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.api import idempotency as idem_mw
from genesis.application.idempotency_purge import purge_expired_idempotency_keys
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _challenge_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (await session.execute(text("SELECT count(*) FROM otp_challenges"))).scalar_one()
    return int(count)


async def _key_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (await session.execute(text("SELECT count(*) FROM idempotency_keys"))).scalar_one()
    return int(count)


def _otp_storage_key(client_key: str) -> str:
    """The stored claim key behind this suite's OTP requests (P14.5
    FM5 scoping): the pre-auth request has no bearer, so the actor is
    the empty string; the route is POST /auth/otp/request. Direct-SQL
    reads and tampering must address the SCOPED key the middleware
    actually stores."""
    return idem_mw.scoped_storage_key("", "POST", "/auth/otp/request", client_key)


async def _force_expire(tid: uuid.UUID, key: str) -> None:
    """Direct-SQL expiry: the DBA-tamper direction the 0029 docstring
    reasons about — shortening a window degrades to a NEW request with
    one effect, never a double effect (proven below)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
                "WHERE tenant_id = CAST(:tid AS uuid) AND key = :key"
            ),
            {"tid": str(tid), "key": _otp_storage_key(key)},
        )


def test_expires_at_is_server_set_from_retention_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retention window flows from Settings (v1.1 rule 1), not the
    0029 column default: with the accessor pinned to 7 hours the row
    must carry created_at + 7h — a claim that fell back to the DB
    default (24h) fails the window assertion. No request channel
    exists for the value (it is computed in CLAIM_SQL server-side)."""
    monkeypatch.setattr(idem_mw, "idempotency_retention_hours", lambda: 7)

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        key = uuid.uuid4().hex
        headers = {"x-tenant-id": str(tid), "idempotency-key": key}
        async with api_client() as client:
            first = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
        assert first.status_code == 202
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT extract(epoch FROM (expires_at - created_at)) "
                        "FROM idempotency_keys "
                        "WHERE tenant_id = CAST(:tid AS uuid) AND key = :key"
                    ),
                    {"tid": str(tid), "key": _otp_storage_key(key)},
                )
            ).one()
        window_seconds = float(row[0])
        # Hand-computed: 7 hours = 25_200s; tolerance ±60s covers the
        # gap between the claim's now() and created_at's default now()
        # (same statement, so in practice identical).
        assert abs(window_seconds - 25_200.0) < 60.0, window_seconds

    asyncio.run(run())


def test_expired_key_executes_as_new_request_never_a_replay() -> None:
    """FM3 core: an expired key is a NEW request with exactly one new
    effect — never a stale replay — even though NO purge has run (the
    fence, not the purge, owns expiry). The takeover epoch then
    behaves like any live claim: the third call replays its stored
    response without a third effect."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        key = uuid.uuid4().hex
        headers = {"x-tenant-id": str(tid), "idempotency-key": key}
        async with api_client() as client:
            first = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert first.status_code == 202
            assert await _challenge_count(tid) == 1

            await _force_expire(tid, key)

            second = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert second.status_code == 202
            # NOT a replay: the stored (now expired) response was not
            # served...
            assert second.headers.get("idempotency-replayed") is None
            # ...and exactly ONE new effect happened (side-effect count).
            assert await _challenge_count(tid) == 2

            third = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert third.status_code == 202
            assert third.headers.get("idempotency-replayed") == "true"
            assert await _challenge_count(tid) == 2, "replay must add no effect"

    asyncio.run(run())


def test_concurrent_takeover_of_expired_key_exactly_one_new_effect() -> None:
    """Exactly-one-effect per claim epoch under concurrency: the
    takeover is ONE atomic statement, so two simultaneous retries of an
    expired key serialise on the row — the loser's WHERE arm
    re-evaluates against the winner's committed fresh epoch and fails,
    landing on the replay/409 path. Total effects: the original 1 plus
    exactly 1 for the new epoch."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        key = uuid.uuid4().hex
        headers = {"x-tenant-id": str(tid), "idempotency-key": key}
        async with api_client() as client:
            first = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert first.status_code == 202

            await _force_expire(tid, key)

            results = await asyncio.gather(
                client.post("/auth/otp/request", json={"email": email}, headers=headers),
                client.post("/auth/otp/request", json={"email": email}, headers=headers),
            )
        statuses = sorted(r.status_code for r in results)
        assert 202 in statuses, statuses
        assert all(s in {202, 409} for s in statuses), statuses
        assert await _challenge_count(tid) == 2

    asyncio.run(run())


def test_purge_deletes_only_expired_rows_and_rerun_is_noop() -> None:
    """The purge is housekeeping only: it removes exactly the expired
    rows, never a live claim (stored OR in flight), causes zero domain
    side effects, and a re-run is a lock-free no-op — all asserted by
    side-effect row counts (v1.1 rule 8)."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        key_live = uuid.uuid4().hex
        key_expired = uuid.uuid4().hex
        base = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            for key in (key_live, key_expired):
                response = await client.post(
                    "/auth/otp/request",
                    json={"email": email},
                    headers={**base, "idempotency-key": key},
                )
                assert response.status_code == 202
            assert await _key_count(tid) == 2
            assert await _challenge_count(tid) == 2

            await _force_expire(tid, key_expired)

            scope = partial(tenant_session, factory(), tid)
            purged = await purge_expired_idempotency_keys(scope, tid)
            assert purged == 1
            assert await _key_count(tid) == 1, "the live claim must survive"
            assert await _challenge_count(tid) == 2, "purge causes no domain effects"

            # The surviving live claim still replays its stored
            # response — the purge changed nothing for it.
            replay = await client.post(
                "/auth/otp/request",
                json={"email": email},
                headers={**base, "idempotency-key": key_live},
            )
            assert replay.status_code == 202
            assert replay.headers.get("idempotency-replayed") == "true"
            assert await _challenge_count(tid) == 2

            # Idempotent re-run: zero rows purged, nothing changed.
            assert await purge_expired_idempotency_keys(scope, tid) == 0
            assert await _key_count(tid) == 1
            assert await _challenge_count(tid) == 2

    asyncio.run(run())
