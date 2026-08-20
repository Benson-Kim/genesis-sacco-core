"""Adversarial suite for the member READ rate guard (RequireMemberReadPrincipal).

Falsifiable both ways (the house doctrine section 4):
- route-hopping across the five read routes shares ONE bucket — the N+1th
  request 429s on the wire with a ``Retry-After`` header;
- distinct credentials keep independent buckets (no cross-member collateral);
- THE POOL-PRESSURE FALSIFIER: an over-limit request is denied with ZERO
  database sessions opened — the live-link query is never called (the
  property that motivates the guard IS the property the test pins);
- a Redis outage FAILS CLOSED (429, degraded) with no logout-style
  carve-out — and still opens zero sessions;
- a staff token stays a 403 (principal-kind fence unchanged) and never
  spends the member bucket; a garbage bearer 401s at decode without
  minting a bucket;
- the consent/release POSTs keep the plain (unmetered) member gate.

No database required: the live-link machinery is stubbed at the authz
module seam so every request is decided by the guard itself.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from genesis.api import authz
from genesis.application.auth import (
    AuthContext,
    MemberAuthContext,
    issue_access_token,
    issue_member_access_token,
)
from genesis.errors import ForbiddenError, RateLimitedError, UnauthenticatedError
from genesis.infrastructure import rate_limit
from genesis.settings import get_settings


def _member_token() -> str:
    return issue_member_access_token(
        MemberAuthContext(
            credential_id=uuid.uuid4(), member_id=uuid.uuid4(), tenant_id=uuid.uuid4()
        )
    )


def _staff_token() -> str:
    return issue_access_token(
        AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4())
    )


def _request(token: str | None, path: str = "/member/me") -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("203.0.113.1", 4242),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.fixture()
def _read_limits(monkeypatch: pytest.MonkeyPatch):
    """Pin a low read limit, a fixed clock, and fresh in-process counters."""
    monkeypatch.setenv("MEMBER_READ_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit, "_local_events", {})
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000_000_000.0)
    yield
    get_settings.cache_clear()


class _LiveLinkStub:
    """Stub of the authz live-link seam, counting every session opened.

    ``sessions_opened`` counts get_sessionmaker calls (the entry point of
    every DB touch in the gate); ``live_link_queries`` counts the
    live-link lookup itself. ``links`` maps credential_id -> member_id
    for credentials that must pass the re-check; everything else fails
    it (401), which still proves the bucket was spent first.
    """

    def __init__(self) -> None:
        self.sessions_opened = 0
        self.live_link_queries = 0
        self.links: dict[uuid.UUID, uuid.UUID] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_sessionmaker(url: str) -> object:
            self.sessions_opened += 1
            return object()

        @contextlib.asynccontextmanager
        async def fake_tenant_session(factory: object, tenant_id: uuid.UUID) -> AsyncIterator[None]:
            yield None

        async def fake_live_credential_by_id(
            session: object, tenant_id: uuid.UUID, credential_id: uuid.UUID
        ) -> object | None:
            self.live_link_queries += 1
            member_id = self.links.get(credential_id)
            if member_id is None:
                return None
            return SimpleNamespace(member_id=member_id)

        monkeypatch.setattr(authz, "get_sessionmaker", fake_sessionmaker)
        monkeypatch.setattr(authz, "tenant_session", fake_tenant_session)
        monkeypatch.setattr(authz, "live_credential_by_id", fake_live_credential_by_id)


@pytest.fixture()
def _live_link(_read_limits, monkeypatch: pytest.MonkeyPatch) -> _LiveLinkStub:
    stub = _LiveLinkStub()
    stub.install(monkeypatch)
    return stub


def test_route_hopping_shares_one_bucket_and_429s_with_retry_after(
    _live_link: _LiveLinkStub, client: TestClient
) -> None:
    """ONE bucket across ALL five read routes, asserted on the wire: a
    per-route split would multiply the budget by five, so hopping routes
    must trip the SAME limit — and the 429 carries Retry-After."""
    token = _member_token()
    headers = {"authorization": f"Bearer {token}"}
    # Three allowed spends across three DIFFERENT routes (each dies at the
    # stubbed live link with a 401 — proving the spend happened FIRST).
    statuses = [
        client.get(path, headers=headers).status_code
        for path in ("/member/me", "/member/transactions", "/member/loans")
    ]
    assert statuses == [401, 401, 401]
    # The fourth request — on yet ANOTHER route — is over-limit: 429.
    res = client.get("/member/statement", headers=headers)
    assert res.status_code == 429
    assert res.headers["Retry-After"] == str(rate_limit.WINDOW_SECONDS)
    assert set(res.json().keys()) == {"category", "correlation_id"}
    # The fifth route (loan detail) is the same shared bucket too.
    res = client.get(f"/member/loans/{uuid.uuid4()}", headers=headers)
    assert res.status_code == 429


def test_distinct_credentials_keep_independent_buckets(_live_link: _LiveLinkStub) -> None:
    guard = authz.RequireMemberReadPrincipal()
    token_a = _member_token()
    token_b = _member_token()

    async def run() -> None:
        for _ in range(3):
            with pytest.raises(UnauthenticatedError):
                await guard(_request(token_a))
        with pytest.raises(RateLimitedError):
            await guard(_request(token_a))
        # Credential B is untouched by A's spend: through the bucket,
        # denied only by the stubbed live link (401, not 429).
        with pytest.raises(UnauthenticatedError):
            await guard(_request(token_b))

    asyncio.run(run())


def test_over_limit_request_opens_zero_db_sessions(_live_link: _LiveLinkStub) -> None:
    """THE POOL-PRESSURE FALSIFIER: the reason this guard exists is that
    every member read costs DB sessions — so an over-limit request must
    be denied with ZERO sessions opened and the live-link query never
    called. A guard ordered after the live-link re-check fails this."""
    guard = authz.RequireMemberReadPrincipal()
    ctx = MemberAuthContext(
        credential_id=uuid.uuid4(), member_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )
    _live_link.links[ctx.credential_id] = ctx.member_id
    token = issue_member_access_token(ctx)

    async def run() -> None:
        for _ in range(3):
            resolved = await guard(_request(token))
            assert resolved.member_id == ctx.member_id
        assert _live_link.sessions_opened == 3
        assert _live_link.live_link_queries == 3
        with pytest.raises(RateLimitedError) as excinfo:
            await guard(_request(token))
        assert excinfo.value.retry_after >= 1
        # The denial cost NOTHING downstream: no session, no query.
        assert _live_link.sessions_opened == 3
        assert _live_link.live_link_queries == 3

    asyncio.run(run())


class _OutageRedis:
    """Every command fails, as during a Redis outage."""

    async def eval(self, *args: object) -> list[int]:
        raise RedisError("connection refused")


def test_redis_outage_fails_closed_with_no_carve_out(
    _live_link: _LiveLinkStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed on the READ surface, deliberately WITHOUT the logout
    carve-out: a denied read never strands a stolen token, and fail-open
    under outage is exactly the pool-pressure scenario being prevented.
    The degraded denial also opens zero sessions."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit, "_redis_client", _OutageRedis())
    monkeypatch.setattr(rate_limit, "_last_error_window", None)
    guard = authz.RequireMemberReadPrincipal()
    ctx = MemberAuthContext(
        credential_id=uuid.uuid4(), member_id=uuid.uuid4(), tenant_id=uuid.uuid4()
    )
    _live_link.links[ctx.credential_id] = ctx.member_id
    token = issue_member_access_token(ctx)

    async def run() -> None:
        # The very FIRST request is denied: outage, not over-limit.
        with pytest.raises(RateLimitedError):
            await guard(_request(token))
        assert _live_link.sessions_opened == 0
        assert _live_link.live_link_queries == 0

    asyncio.run(run())


def test_staff_token_stays_403_and_never_spends_the_bucket(
    _live_link: _LiveLinkStub, client: TestClient
) -> None:
    """The principal-kind fence is unchanged: a staff token is refused
    with a 403 AT DECODE — before the bucket, before any session."""
    res = client.get("/member/me", headers={"authorization": f"Bearer {_staff_token()}"})
    assert res.status_code == 403
    assert rate_limit._local_events == {}
    assert _live_link.sessions_opened == 0


def test_garbage_bearer_401s_without_minting_a_bucket(_live_link: _LiveLinkStub) -> None:
    guard = authz.RequireMemberReadPrincipal()

    async def run() -> None:
        with pytest.raises(UnauthenticatedError):
            await guard(_request("not-a-jwt"))
        with pytest.raises(ForbiddenError):
            await guard(_request(_staff_token()))
        with pytest.raises(UnauthenticatedError):
            await guard(_request(None))

    asyncio.run(run())
    assert rate_limit._local_events == {}
    assert _live_link.sessions_opened == 0


def test_consent_and_release_posts_keep_the_plain_unmetered_gate(
    _live_link: _LiveLinkStub, client: TestClient
) -> None:
    """The money-moving POSTs deliberately keep RequireMemberPrincipal:
    exhausting the READ bucket must not 429 them (they die at the
    stubbed live link with a 401 — the plain gate, no bucket in front)."""
    token = _member_token()
    headers = {"authorization": f"Bearer {token}"}
    for _ in range(3):
        assert client.get("/member/me", headers=headers).status_code == 401
    assert client.get("/member/me", headers=headers).status_code == 429
    res = client.post(
        f"/member/guarantees/{uuid.uuid4()}/consent", headers=headers, json={"version": 1}
    )
    assert res.status_code == 401
    res = client.post(
        f"/member/guarantees/{uuid.uuid4()}/release", headers=headers, json={"version": 1}
    )
    assert res.status_code == 401
