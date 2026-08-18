"""Sliding-window limiter suite: local fallback, pooled-client reuse,
fail-closed Redis errors, window expiry, and the boundary-burst falsifier
that the old fixed-window algorithm fails (falsifiable both ways)."""

import asyncio
import logging
import math
import uuid

import pytest
from redis.exceptions import RedisError

from genesis.infrastructure import rate_limit
from genesis.infrastructure.rate_limit import (
    WINDOW_SECONDS,
    check_rate_limit,
    consume_rate_limit,
)
from genesis.settings import Settings, assert_redis_configured_outside_dev, get_settings


class FakeRedis:
    """In-memory stand-in for the pooled async client.

    ``eval`` mirrors the module's Lua sliding-log semantics step for step
    (prune, count, admit-or-report) — the atomicity itself is Redis's
    single-threaded script guarantee and is not re-provable here.
    """

    def __init__(self) -> None:
        self.events: dict[str, list[tuple[float, str]]] = {}

    async def eval(self, script: str, numkeys: int, key: str, *args: str) -> list[int]:
        now, window, limit, member = float(args[0]), int(args[1]), int(args[2]), args[3]
        events = [(stamp, m) for (stamp, m) in self.events.get(key, []) if stamp > now - window]
        if len(events) < limit:
            events.append((now, member))
            self.events[key] = events
            return [1, 0]
        self.events[key] = events
        return [0, max(1, math.ceil(events[0][0] + window - now))]


class BrokenRedis(FakeRedis):
    """Raises on every command, exactly as an unreachable Redis would."""

    async def eval(self, script: str, numkeys: int, key: str, *args: str) -> list[int]:
        raise RedisError("connection refused")


class DroppedConnectionRedis(FakeRedis):
    """Raises a bare OSError (ConnectionResetError) — the socket-level
    failure arm of the except clause, distinct from redis-py's own
    RedisError hierarchy."""

    async def eval(self, script: str, numkeys: int, key: str, *args: str) -> list[int]:
        raise ConnectionResetError("connection reset by peer")


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


def test_os_errors_fail_closed_and_log_once_per_window(
    _redis_env,
    clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The previously untested OSError arm of the except clause: a
    socket-level ConnectionResetError (not a RedisError) must deny and
    log once per window — same falsifiability bar as the RedisError test."""
    monkeypatch.setattr(rate_limit, "_redis_client", DroppedConnectionRedis())

    async def run(calls: int) -> list[bool]:
        return [await check_rate_limit("unit:os-fail-closed", 100) for _ in range(calls)]

    with caplog.at_level(logging.ERROR, logger="genesis.infrastructure.rate_limit"):
        assert asyncio.run(run(3)) == [False, False, False]
        clock["now"] += WINDOW_SECONDS
        assert asyncio.run(run(2)) == [False, False]

    records = [r for r in caplog.records if r.name == "genesis.infrastructure.rate_limit"]
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


def test_redis_boundary_burst_cannot_double_the_limit(
    _redis_env, clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FALSIFIER for issue #14: the old fixed-window algorithm keyed on
    ``int(time.time()) // 60``, so a burst spent just before a window edge
    could be repeated just after it — 2x the limit in seconds. This test
    FAILS against that algorithm (the second burst would be all-True) and
    passes against the sliding log."""
    monkeypatch.setattr(rate_limit, "_redis_client", FakeRedis())
    key = f"unit:{uuid.uuid4().hex}"
    # 1_000_000_020 is a multiple of 60: a fixed-window edge. Park the
    # clock one second before it.
    clock["now"] = 1_000_000_019.0

    async def run() -> tuple[list[bool], list[bool], bool]:
        before_edge = [await check_rate_limit(key, 3) for _ in range(3)]
        clock["now"] += 2.0  # crosses the old fixed-window edge
        after_edge = [await check_rate_limit(key, 3) for _ in range(3)]
        clock["now"] += WINDOW_SECONDS  # the original spend has aged out
        return before_edge, after_edge, await check_rate_limit(key, 3)

    before_edge, after_edge, recovered = asyncio.run(run())
    assert before_edge == [True, True, True]
    # The fixed window said [True, True, True] here — the burst doubled.
    assert after_edge == [False, False, False]
    assert recovered is True


def test_local_boundary_burst_cannot_double_the_limit(
    clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process fallback carries the same sliding semantics."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    key = f"unit:{uuid.uuid4().hex}"
    clock["now"] = 1_000_000_019.0

    async def run() -> tuple[list[bool], list[bool], bool]:
        before_edge = [await check_rate_limit(key, 3) for _ in range(3)]
        clock["now"] += 2.0
        after_edge = [await check_rate_limit(key, 3) for _ in range(3)]
        clock["now"] += WINDOW_SECONDS
        return before_edge, after_edge, await check_rate_limit(key, 3)

    try:
        before_edge, after_edge, recovered = asyncio.run(run())
    finally:
        get_settings.cache_clear()
    assert before_edge == [True, True, True]
    assert after_edge == [False, False, False]
    assert recovered is True


def test_denied_decisions_carry_a_retry_hint(
    clock: dict[str, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``retry_after`` counts down as the oldest event ages, never below 1,
    and is 0 on an allowed call (no bucket internals beyond the hint)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    key = f"unit:{uuid.uuid4().hex}"

    async def run() -> tuple[int, int, int]:
        first = await consume_rate_limit(key, 1)
        denied_now = await consume_rate_limit(key, 1)
        clock["now"] += 45.0
        denied_later = await consume_rate_limit(key, 1)
        return first.retry_after, denied_now.retry_after, denied_later.retry_after

    try:
        allowed_hint, hint_now, hint_later = asyncio.run(run())
    finally:
        get_settings.cache_clear()
    assert allowed_hint == 0
    assert hint_now == WINDOW_SECONDS
    assert hint_later == WINDOW_SECONDS - 45


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


def test_boot_refuses_empty_redis_url_outside_development() -> None:
    """The #15 boot guard, falsifiable both ways: (production, empty)
    refuses loudly; (development, empty) and (production, set) boot clean."""
    for environment in ("staging", "production"):
        with pytest.raises(RuntimeError, match="REDIS_URL is empty"):
            assert_redis_configured_outside_dev(Settings(environment=environment, redis_url=""))
    assert_redis_configured_outside_dev(Settings(environment="development", redis_url=""))
    assert_redis_configured_outside_dev(
        Settings(environment="production", redis_url="redis://cache:6379/0")
    )


def test_limits_are_per_key() -> None:
    async def run() -> tuple[bool, bool]:
        first = await check_rate_limit(f"unit:{uuid.uuid4().hex}", 1)
        second = await check_rate_limit(f"unit:{uuid.uuid4().hex}", 1)
        return first, second

    assert asyncio.run(run()) == (True, True)
