"""Fixed-window limiter suite: local fallback, pooled-client reuse,
fail-closed Redis errors, and window rollover (falsifiable both ways)."""

import asyncio
import logging
import uuid

import pytest
from redis.exceptions import RedisError

from genesis.infrastructure import rate_limit
from genesis.infrastructure.rate_limit import WINDOW_SECONDS, check_rate_limit
from genesis.settings import get_settings


class FakeRedis:
    """In-memory stand-in for the pooled async client."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True


class BrokenRedis(FakeRedis):
    """Raises on every command, exactly as an unreachable Redis would."""

    async def incr(self, key: str) -> int:
        raise RedisError("connection refused")


@pytest.fixture()
def _redis_env(monkeypatch: pytest.MonkeyPatch):
    """Configure a Redis URL and reset the module-level limiter state."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    monkeypatch.setattr(rate_limit, "_redis_client", None)
    monkeypatch.setattr(rate_limit, "_last_error_window", None)
    yield
    get_settings.cache_clear()


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Pin time so window boundaries are deterministic, not wall-clock luck."""
    holder = {"now": 1_000_000_000.0}
    monkeypatch.setattr(rate_limit.time, "time", lambda: holder["now"])
    return holder


def test_pooled_client_is_created_once_and_reused(
    _redis_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[FakeRedis] = []

    class RecordingRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            created.append(FakeRedis())
            return created[-1]

    monkeypatch.setattr(rate_limit, "Redis", RecordingRedis)
    key = f"unit:{uuid.uuid4().hex}"

    async def run() -> list[bool]:
        return [await check_rate_limit(key, 2) for _ in range(3)]

    # Counting works through the shared client AND only one client was built.
    assert asyncio.run(run()) == [True, True, False]
    assert len(created) == 1


def test_redis_errors_fail_closed_and_log_once_per_window(
    _redis_env,
    clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(rate_limit, "_redis_client", BrokenRedis())

    async def run(calls: int) -> list[bool]:
        return [await check_rate_limit("unit:fail-closed", 100) for _ in range(calls)]

    with caplog.at_level(logging.ERROR, logger="genesis.infrastructure.rate_limit"):
        # DENIED despite a generous limit: the guard fails closed, no 500s.
        assert asyncio.run(run(3)) == [False, False, False]
        clock["now"] += WINDOW_SECONDS
        assert asyncio.run(run(2)) == [False, False]

    records = [r for r in caplog.records if r.name == "genesis.infrastructure.rate_limit"]
    # Once per window — five failing calls across two windows log twice.
    assert len(records) == 2


def test_redis_window_rollover_allows_again(
    _redis_env, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rate_limit, "_redis_client", FakeRedis())
    key = f"unit:{uuid.uuid4().hex}"

    async def run() -> list[bool]:
        first = [await check_rate_limit(key, 1) for _ in range(2)]
        clock["now"] += WINDOW_SECONDS
        return [*first, await check_rate_limit(key, 1)]

    assert asyncio.run(run()) == [True, False, True]


def test_local_window_rollover_allows_again(
    clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    key = f"unit:{uuid.uuid4().hex}"

    async def run() -> list[bool]:
        first = [await check_rate_limit(key, 1) for _ in range(2)]
        clock["now"] += WINDOW_SECONDS
        return [*first, await check_rate_limit(key, 1)]

    try:
        assert asyncio.run(run()) == [True, False, True]
    finally:
        get_settings.cache_clear()


def test_local_rate_limiter_blocks_after_limit() -> None:
    key = f"unit:{uuid.uuid4().hex}"

    async def run() -> list[bool]:
        return [await check_rate_limit(key, 3) for _ in range(5)]

    results = asyncio.run(run())
    assert results == [True, True, True, False, False]


def test_limits_are_per_key() -> None:
    async def run() -> tuple[bool, bool]:
        first = await check_rate_limit(f"unit:{uuid.uuid4().hex}", 1)
        second = await check_rate_limit(f"unit:{uuid.uuid4().hex}", 1)
        return first, second

    assert asyncio.run(run()) == (True, True)
