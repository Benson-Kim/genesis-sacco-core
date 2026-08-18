"""Atomic sliding-window rate limiting for auth-sensitive endpoints (least disclosure).

Uses Redis when configured so limits hold across instances. The client is a
single module-level pooled instance, lazy-initialized from
``settings.redis_url`` (the ``db.get_engine`` caching pattern) — never a new
connection per call. A Redis failure at runtime FAILS CLOSED: the call is
denied, the error is logged once per window (never once per request), and no
exception ever propagates into a 500 — an outage must not disable the auth
guard and must not flood the logs.

The Redis path is a SLIDING LOG (a per-key ZSET of allowed-event
timestamps) executed as ONE Lua script, so prune-count-admit is atomic —
no incr-then-expire pair to race, no TTL-less keys, and no fixed-window
boundary burst (the old algorithm allowed up to 2x the limit straddling a
window edge; issue #14). Memory per key is bounded by the limit itself:
only ALLOWED events are recorded, denied calls add nothing to the log.

Without a configured Redis URL the limiter falls back to an in-process
sliding log with the same semantics. LIMITATION: the fallback counts PER
PROCESS only — under N workers the effective limit is N x the configured
limit — so it is acceptable for single-instance and test environments,
never for multi-worker production (boot refuses that combination:
``genesis.settings.assert_redis_configured_outside_dev``).
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from genesis.settings import get_settings

logger = logging.getLogger("genesis.infrastructure.rate_limit")

WINDOW_SECONDS = 60
_local_events: dict[str, list[float]] = {}
_redis_client: Redis | None = None
_last_error_window: int | None = None

# Atomic sliding-log spend: prune expired events, count, admit-or-report —
# one script, one round trip, no interleaving (the #14 atomicity fix).
# Returns {allowed(0|1), retry_after_seconds}. Only allowed events are
# ZADDed, so a key never holds more than `limit` members. PEXPIRE renews
# the TTL on every admit; a key that stops being hit expires on its own.
_SLIDING_LOG_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, math.ceil(window * 1000))
    return {1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = math.ceil(tonumber(oldest[2]) + window - now)
if retry < 1 then
    retry = 1
end
return {0, retry}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one bucket spend.

    ``retry_after`` is the whole-seconds hint until the caller may retry
    (0 when allowed). ``degraded`` marks a FAIL-CLOSED denial caused by a
    limiter-backend failure rather than by the caller's own request rate —
    callers that must distinguish an outage from a genuine over-limit
    (e.g. the logout revocation path) read this flag; everyone else
    treats any denial as a denial.
    """

    allowed: bool
    retry_after: int
    degraded: bool = False


def get_redis_client() -> Redis | None:
    """Return the shared pooled client, creating it on first use.

    ``Redis.from_url`` carries its own connection pool, so one module-level
    instance is the whole pooling story. Returns None when no Redis URL is
    configured (the in-process fallback applies).
    """
    global _redis_client
    if not get_settings().redis_url:
        return None
    if _redis_client is None:
        _redis_client = Redis.from_url(get_settings().redis_url)
    return _redis_client


def _note_redis_failure(window: int) -> None:
    """Record a Redis failure, logging it at most once per window."""
    global _last_error_window
    if _last_error_window != window:
        _last_error_window = window
        logger.exception("rate limiter redis failure; failing closed for this window")


async def consume_rate_limit(key: str, limit: int) -> RateLimitDecision:
    """Spend one event against ``key``'s sliding window and decide.

    FAIL CLOSED: when Redis is configured but errs, the call is DENIED
    (``degraded=True``) — the guard protects auth endpoints, so an outage
    must never widen them. The error is logged once per window, never once
    per request, and no exception ever propagates into a 500.
    """
    now = time.time()
    client = get_redis_client()
    if client is not None:
        try:
            result = await client.eval(
                _SLIDING_LOG_LUA,
                1,
                f"rl:{key}",
                str(now),
                str(WINDOW_SECONDS),
                str(limit),
                uuid.uuid4().hex,
            )
            return RateLimitDecision(allowed=bool(int(result[0])), retry_after=int(result[1]))
        except (RedisError, OSError):
            _note_redis_failure(int(now) // WINDOW_SECONDS)
            return RateLimitDecision(allowed=False, retry_after=WINDOW_SECONDS, degraded=True)
    # In-process sliding-log fallback: identical semantics, per-process
    # scope only (see the module docstring's LIMITATION note).
    cutoff = now - WINDOW_SECONDS
    for stale in [k for k, stamps in _local_events.items() if not stamps or stamps[-1] <= cutoff]:
        del _local_events[stale]
    events = [stamp for stamp in _local_events.get(key, []) if stamp > cutoff]
    if len(events) < limit:
        events.append(now)
        _local_events[key] = events
        return RateLimitDecision(allowed=True, retry_after=0)
    _local_events[key] = events
    retry_after = max(1, math.ceil(events[0] + WINDOW_SECONDS - now))
    return RateLimitDecision(allowed=False, retry_after=retry_after)


async def check_rate_limit(key: str, limit: int) -> bool:
    """Boolean façade over :func:`consume_rate_limit` (compat surface).

    Existing callers and tests that only need allow/deny keep this
    signature; callers needing the retry hint or the outage flag use
    ``consume_rate_limit`` directly.
    """
    return (await consume_rate_limit(key, limit)).allowed
