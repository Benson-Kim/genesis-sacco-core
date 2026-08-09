"""Fixed-window rate limiting for auth-sensitive endpoints (least disclosure).

Uses Redis when configured so limits hold across instances; falls back to
an in-process window for single-instance and test environments.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis

from genesis.settings import get_settings

WINDOW_SECONDS = 60
_local_counts: dict[tuple[str, int], int] = {}


async def check_rate_limit(key: str, limit: int) -> bool:
    """Return True when the call is allowed inside the current window."""
    window = int(time.time()) // WINDOW_SECONDS
    redis_url = get_settings().redis_url
    if redis_url:
        client: Redis = Redis.from_url(redis_url)
        try:
            redis_key = f"rl:{key}:{window}"
            count = int(await client.incr(redis_key))
            if count == 1:
                await client.expire(redis_key, WINDOW_SECONDS)
            return count <= limit
        finally:
            await client.aclose()
    for stale in [k for k in _local_counts if k[1] < window]:
        del _local_counts[stale]
    current = _local_counts.get((key, window), 0) + 1
    _local_counts[(key, window)] = current
    return current <= limit
