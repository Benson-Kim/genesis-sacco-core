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

from genesis.api.auth import _rate_guard
from genesis.errors import RateLimitedError
from genesis.infrastructure import rate_limit
from genesis.settings import get_settings


def _request(tenant: str | None, host: str) -> Request:
    headers = [] if tenant is None else [(b"x-tenant-id", tenant.encode())]
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


def test_guard_is_wired_into_the_auth_routes(_guard_limits, client: TestClient) -> None:
    statuses: list[int] = []
    for i in range(4):
        res = client.post(
            "/auth/otp/request",
            json={"email": "ghost@example.com"},
            headers={"x-tenant-id": f"rotated-{i}"},
        )
        statuses.append(res.status_code)
    # Three invalid-header 401s consume the shared bucket; the fourth is 429.
    assert statuses == [401, 401, 401, 429]
