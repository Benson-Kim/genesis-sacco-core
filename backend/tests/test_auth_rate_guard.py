"""Adversarial rate-guard suite: header rotation must not mint buckets.

Falsifiable both ways (the house doctrine section 4):
- rotating INVALID x-tenant-id values (and a missing header) all share ONE
  ``invalid`` bucket, so the tenant limit still trips;
- rotating syntactically VALID tenant ids trips the pure-IP backstop
  bucket independently;
- distinct client hosts keep independent IP buckets (no global collateral);
- the guard is actually wired into the auth routes (429 on the wire).

No database required: every request here is rejected by the guard or by
tenant_id_from_headers before any session is opened.
"""

import asyncio
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from genesis.api.auth import _logout_rate_guard, _rate_guard, resolve_client_ip
from genesis.errors import RateLimitedError
from genesis.infrastructure import rate_limit
from genesis.settings import get_settings


def _request(tenant: str | None, host: str, xff: str | None = None) -> Request:
    headers = [] if tenant is None else [(b"x-tenant-id", tenant.encode())]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/otp/request",
        "raw_path": b"/auth/otp/request",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (host, 4242),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.fixture()
def _guard_limits(monkeypatch: pytest.MonkeyPatch):
    """Pin low limits, a fixed clock, and fresh in-process counters."""
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setenv("AUTH_RATE_LIMIT_IP_PER_MINUTE", "5")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit, "_local_events", {})
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000_000_000.0)
    yield
    get_settings.cache_clear()


def test_rotating_invalid_tenant_headers_share_one_bucket(_guard_limits) -> None:
    async def run() -> None:
        for _ in range(3):
            await _rate_guard(_request(f"garbage-{uuid.uuid4().hex[:8]}", "203.0.113.1"))
        # A FOURTH distinct garbage value is still the SAME bucket: denied.
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(f"garbage-{uuid.uuid4().hex[:8]}", "203.0.113.1"))

    asyncio.run(run())


def test_missing_header_shares_the_invalid_bucket(_guard_limits) -> None:
    async def run() -> None:
        for _ in range(3):
            await _rate_guard(_request(f"garbage-{uuid.uuid4().hex[:8]}", "203.0.113.2"))
        # No header at all lands in the same shared 'invalid' bucket.
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(None, "203.0.113.2"))

    asyncio.run(run())


def test_rotating_valid_tenant_ids_trip_the_ip_bucket(_guard_limits) -> None:
    async def run() -> None:
        # Each valid UUID mints its own tenant bucket (count 1 each), so
        # ONLY the pure-IP backstop can stop this rotation — and it does.
        for _ in range(5):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.3"))
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.3"))

    asyncio.run(run())


def test_distinct_hosts_keep_independent_ip_buckets(_guard_limits) -> None:
    async def run() -> None:
        for _ in range(5):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.4"))
        # A different client host is untouched by the first host's spend.
        await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.5"))

    asyncio.run(run())


@pytest.fixture()
def _trusted_proxy(_guard_limits, monkeypatch: pytest.MonkeyPatch):
    """Layer a trusted proxy (192.0.2.10) on top of the pinned guard limits."""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "192.0.2.10")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_spoofed_xff_from_untrusted_peer_never_changes_the_bucket_key(_guard_limits) -> None:
    # No trusted proxies configured (the default): the header is inert.
    assert resolve_client_ip(_request(None, "203.0.113.6", xff="198.51.100.1")) == "203.0.113.6"

    async def run() -> None:
        # Rotating spoofed XFF values cannot mint buckets: the pure-IP
        # backstop (limit 5) still trips on the DIRECT peer address.
        for i in range(5):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.6", xff=f"198.51.100.{i}"))
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.6", xff="198.51.100.99"))

    asyncio.run(run())


def test_trusted_proxy_chain_resolves_the_real_client(_trusted_proxy) -> None:
    # Right-walk: the trusted hop is skipped, the first untrusted entry
    # from the right wins, and an attacker-prepended left entry is ignored.
    req = _request(None, "192.0.2.10", xff="6.6.6.6, 198.51.100.7, 192.0.2.10")
    assert resolve_client_ip(req) == "198.51.100.7"

    async def run() -> None:
        # Client A exhausts its per-IP bucket through the proxy...
        for _ in range(5):
            await _rate_guard(_request(str(uuid.uuid4()), "192.0.2.10", xff="198.51.100.7"))
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(str(uuid.uuid4()), "192.0.2.10", xff="198.51.100.7"))
        # ...while client B behind the SAME proxy is untouched — the
        # backstop is per-client again, not one global bucket.
        await _rate_guard(_request(str(uuid.uuid4()), "192.0.2.10", xff="198.51.100.8"))

    asyncio.run(run())


def test_malformed_xff_through_trusted_proxy_shares_one_bucket(_trusted_proxy) -> None:
    # Malformed and empty chains all collapse to the SAME sentinel: an
    # attacker-controlled string can never mint a fresh bucket (the MR lesson).
    assert resolve_client_ip(_request(None, "192.0.2.10", xff="not-an-ip")) == (
        "invalid-forwarded-for"
    )
    assert resolve_client_ip(_request(None, "192.0.2.10")) == "invalid-forwarded-for"
    assert resolve_client_ip(_request(None, "192.0.2.10", xff="192.0.2.10")) == (
        "invalid-forwarded-for"
    )

    async def run() -> None:
        # Rotating garbage XFF values share ONE bucket, so the limit trips.
        for i in range(5):
            await _rate_guard(_request(str(uuid.uuid4()), "192.0.2.10", xff=f"garbage-{i}"))
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(str(uuid.uuid4()), "192.0.2.10", xff="garbage-fresh"))

    asyncio.run(run())


def test_tenant_bucket_keys_on_the_resolved_ip_too(_trusted_proxy) -> None:
    """The consistency requirement: BOTH buckets embed the resolved client
    IP — one tenant's spend through the proxy must not throttle the same
    tenant arriving from a different real client."""
    tenant = str(uuid.uuid4())

    async def run() -> None:
        for _ in range(3):
            await _rate_guard(_request(tenant, "192.0.2.10", xff="198.51.100.20"))
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(tenant, "192.0.2.10", xff="198.51.100.20"))
        # SAME tenant id, different resolved client: its own tenant bucket.
        await _rate_guard(_request(tenant, "192.0.2.10", xff="198.51.100.21"))

    asyncio.run(run())


def test_request_without_client_uses_the_shared_unknown_bucket(_guard_limits) -> None:
    scope = _request(None, "203.0.113.9").scope
    scope["client"] = None
    assert resolve_client_ip(Request(scope)) == "unknown"


class _OutageRedis:
    """Every command fails, as during a Redis outage."""

    async def eval(self, *args: object) -> list[int]:
        raise RedisError("connection refused")


def test_logout_guard_allows_revocation_during_limiter_outage(
    _guard_limits, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate logout fail-policy: a limiter OUTAGE must not block
    token revocation (logout only reduces privilege), while every other
    auth endpoint stays fail-closed during the same outage."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit, "_redis_client", _OutageRedis())
    monkeypatch.setattr(rate_limit, "_last_error_window", None)

    async def run() -> None:
        # Revocation proceeds through the outage...
        await _logout_rate_guard(_request(str(uuid.uuid4()), "203.0.113.7"))
        # ...while the privilege-granting endpoints stay fail-closed.
        with pytest.raises(RateLimitedError):
            await _rate_guard(_request(str(uuid.uuid4()), "203.0.113.7"))

    asyncio.run(run())


def test_logout_guard_still_denies_a_genuine_over_limit(_guard_limits) -> None:
    """Logout is NOT unmetered: with a healthy limiter, the outage
    carve-out never fires and over-limit calls are denied as usual."""

    async def run() -> None:
        for _ in range(5):
            await _logout_rate_guard(_request(str(uuid.uuid4()), "203.0.113.8"))
        with pytest.raises(RateLimitedError):
            await _logout_rate_guard(_request(str(uuid.uuid4()), "203.0.113.8"))

    asyncio.run(run())


def test_guard_is_wired_into_the_auth_routes(_guard_limits, client: TestClient) -> None:
    statuses: list[int] = []
    res = None
    for i in range(4):
        res = client.post(
            "/auth/otp/request",
            json={"email": "ghost@example.com"},
            headers={"x-tenant-id": f"rotated-{i}"},
        )
        statuses.append(res.status_code)
    # Three invalid-header 401s consume the shared bucket; the fourth is 429.
    assert statuses == [401, 401, 401, 429]
    # The 429 carries a Retry-After hint on the wire: whole seconds only,
    # never bucket names, counts, or limits (least disclosure).
    assert res is not None
    assert res.headers["Retry-After"] == str(rate_limit.WINDOW_SECONDS)
    assert set(res.json().keys()) == {"category", "correlation_id"}
