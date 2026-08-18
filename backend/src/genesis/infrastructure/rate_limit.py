"""Fixed-window rate limiting for auth-sensitive endpoints (least disclosure).

Uses Redis when configured so limits hold across instances. The client is a
single module-level pooled instance, lazy-initialized from
``settings.redis_url`` (the ``db.get_engine`` caching pattern) — never a new
connection per call. A Redis failure at runtime FAILS CLOSED: the call is
denied, the error is logged once per window (never once per request), and no
exception ever propagates into a 500 — an outage must not disable the auth
guard and must not flood the logs.

Without a configured Redis URL the limiter falls back to an in-process
window. LIMITATION: the fallback counts PER PROCESS only — under N workers
the effective limit is N x the configured limit — so it is acceptable for
single-instance and test environments, never for multi-worker production.
"""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from genesis.settings import get_settings

logger = logging.getLogger("genesis.infrastructure.rate_limit")

WINDOW_SECONDS = 60
_local_counts: dict[tuple[str, int], int] = {}
_redis_client: Redis | None = None
_last_error_window: int | None = None


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


async def check_rate_limit(key: str, limit: int) -> bool:
    """Return True when the call is allowed inside the current window.

    FAIL CLOSED: when Redis is configured but errs, the call is DENIED —
    the guard protects auth endpoints, so an outage must never widen them.
    """
    window = int(time.time()) // WINDOW_SECONDS
    client = get_redis_client()
    if client is not None:
        try:
            redis_key = f"rl:{key}:{window}"
            count = int(await client.incr(redis_key))
            if count == 1:
                await client.expire(redis_key, WINDOW_SECONDS)
            return count <= limit
        except (RedisError, OSError):
            _note_redis_failure(window)
            return False
    for stale in [k for k in _local_counts if k[1] < window]:
        del _local_counts[stale]
    current = _local_counts.get((key, window), 0) + 1
    _local_counts[(key, window)] = current
    return current <= limit
