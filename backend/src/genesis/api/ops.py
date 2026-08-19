"""Ops metrics endpoint: the auth-gated Prometheus scrape target (issue #4).

One endpoint, ``GET /ops/metrics``, rendering the minimum metric set
in Prometheus text exposition format 0.0.4:

* outbox queue depth, dead-letter depth and oldest-unprocessed-event
  age — via the SECURITY DEFINER aggregate ``outbox_queue_stats()``
  (migration 0049): the endpoint's plain session carries no tenant GUC
  and outbox_events is RLS-forced, so a definer aggregate is the ONLY
  disclosure — three numbers, never rows or payloads;
* per-worker last-successful-run timestamp and CONSECUTIVE lock-skip
  counter — the ``worker_heartbeats`` table the four cron one-shots
  write at cycle end (the countable form of the cron_lock skip log);
* rate-limit 429 trip counts and auth-failure counts — incremented at
  the error-handler seam in api.app (never inside
  infrastructure/rate_limit.py, owned by the open !3);
* DB pool saturation — read off the process's SQLAlchemy QueuePool;
* p95 latency per router — the in-process histogram the correlation
  middleware feeds (genesis.observability).

AUTH-GATED: RequirePermission(settings:view) — an admin-module grant,
so the spec-walk gate (test_rbac_matrix) sees a protected route and
anonymous scrapes die 401. Deliberately ``include_in_schema=False``:
this is an internal ops surface, not part of the generated web-client
contract (web/packages/api-client/openapi.json must not churn — the
web:spec-drift gate), and least disclosure says operational internals
never appear in the public schema.

DEGRADES, NEVER 500s: if the database is down — exactly when you are
staring at this endpoint — the DB-derived gauges are replaced by
``genesis_ops_db_scrape_error 1`` and the in-process counters and
histograms still render.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from genesis.api.authz import RequirePermission
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_engine, get_sessionmaker
from genesis.observability import metrics
from genesis.settings import get_settings

logger = logging.getLogger("genesis.api.ops")

router = APIRouter(prefix="/ops", tags=["ops"], include_in_schema=False)

_view_ops = RequirePermission(Module.SETTINGS, Action.VIEW)

#: Prometheus text exposition format version pinned by the spec.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _gauge(name: str, value: float | int, help_text: str, labels: str = "") -> list[str]:
    rendered = f"{value}" if isinstance(value, int) else repr(float(value))
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name}{labels} {rendered}",
    ]


async def _db_lines(database_url: str) -> list[str]:
    """Outbox depth/age + worker heartbeats, from one plain session."""
    lines: list[str] = []
    async with get_sessionmaker(database_url)() as session:
        row = (
            await session.execute(
                text(
                    "SELECT pending_count, dead_count, oldest_pending_age_seconds "
                    "FROM outbox_queue_stats()"
                )
            )
        ).one()
        lines += _gauge(
            "genesis_outbox_pending_events",
            int(row[0]),
            "Outbox queue depth (status = pending).",
        )
        lines += _gauge(
            "genesis_outbox_dead_events",
            int(row[1]),
            "Outbox dead-letter depth (status = dead) — alertable.",
        )
        lines += _gauge(
            "genesis_outbox_oldest_pending_age_seconds",
            float(row[2]),
            "Age of the oldest unprocessed outbox event.",
        )
        heartbeat_rows = (
            await session.execute(
                text(
                    "SELECT worker, "
                    "COALESCE(EXTRACT(EPOCH FROM last_success_at), 0), "
                    "consecutive_lock_skips "
                    "FROM worker_heartbeats ORDER BY worker"
                )
            )
        ).all()
    for worker, last_success_epoch, skips in heartbeat_rows:
        labels = f'{{worker="{worker}"}}'
        lines += _gauge(
            "genesis_worker_last_success_timestamp_seconds",
            float(last_success_epoch),
            "Unix time of the worker's last successful cycle (0 = never).",
            labels,
        )
        lines += _gauge(
            "genesis_worker_consecutive_lock_skips",
            int(skips),
            "Consecutive cron lock-skips since the last successful cycle "
            "(>= 2 means a wedged or pathologically slow cycle — page).",
            labels,
        )
    return lines


def _pool_lines(database_url: str) -> list[str]:
    """DB pool saturation for THIS process (QueuePool only)."""
    pool = get_engine(database_url).sync_engine.pool
    if not isinstance(pool, QueuePool):
        return []
    size = pool.size()
    checked_out = pool.checkedout()
    lines = _gauge("genesis_db_pool_size", size, "Configured base size of the SQLAlchemy pool.")
    lines += _gauge(
        "genesis_db_pool_checked_out",
        checked_out,
        "Connections currently checked out of the pool.",
    )
    lines += _gauge(
        "genesis_db_pool_overflow",
        max(pool.overflow(), 0),
        "Connections currently open beyond the base pool size.",
    )
    if size > 0:
        lines += _gauge(
            "genesis_db_pool_saturation_ratio",
            checked_out / size,
            "Checked-out connections over the base pool size "
            "(> 1.0 means overflow connections are in use).",
        )
    return lines


@router.get("/metrics", dependencies=[Depends(_view_ops)])
async def ops_metrics() -> PlainTextResponse:
    settings = get_settings()
    lines: list[str] = []
    if settings.database_url:
        try:
            lines += await _db_lines(settings.database_url)
        except Exception:
            # The scrape must outlive the database (issue #4: this is
            # the endpoint you read DURING the incident) — flag the
            # failure as a metric and keep the in-process series.
            logger.exception("ops metrics: database-derived gauges failed")
            lines += _gauge(
                "genesis_ops_db_scrape_error",
                1,
                "1 when the database-derived gauges failed to collect.",
            )
        lines += _pool_lines(settings.database_url)
    body = "\n".join(lines) + ("\n" if lines else "") + metrics.render_prometheus()
    return PlainTextResponse(content=body, media_type=_CONTENT_TYPE)
