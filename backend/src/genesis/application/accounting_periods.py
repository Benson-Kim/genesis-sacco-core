"""Accounting periods: server-side posting-date validation (issue #12, P12.5).

Every ledger posting funnels through ledger._post, which calls
assert_open_period before writing anything: occurred_at must not be
future-dated beyond a small clock-skew tolerance and must not fall
inside a CLOSED period. Periods are resolved exclusively server-side —
no request body anywhere accepts occurred_at (the P11 caller-input
lesson), and even the internal parameter is now fenced.

Concurrency contract (gate 1.4): closing a period must never race an
in-flight posting into that period. Postings take a SHARED tenant-level
advisory lock (concurrent postings never block each other); the close
action takes the EXCLUSIVE lock, so it waits for in-flight postings to
commit, and postings that start during a close wait until the close
commits — after which they see the closed row and are refused with a
clean 409. The transactions_closed_period trigger (0012) is the
DB-level backstop behind this guard (gate 1.5).

Only fully elapsed calendar months can be closed: closing the current
or a future month would freeze live posting — a self-inflicted outage —
so it is refused (409).
"""

from __future__ import annotations

import calendar
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.application.period_rollups import write_period_rollups
from genesis.application.portfolio_snapshots import write_month_snapshot
from genesis.errors import ConflictError, InvalidInputError

#: Advisory lock namespace for the period barrier — distinct from the
#: ledger reference namespace (int4 range; the ledger._advisory_key
#: precedent).
_PERIOD_NS: int = zlib.crc32(b"acct_period") & 0x7FFFFFFF

#: Tolerance for clock skew between app instances before a posting
#: counts as future-dated (gate 1.5: future postings desynchronise
#: statements and trial balances from created_at).
CLOCK_SKEW = timedelta(minutes=5)

_PERIOD_COLS = "id, period_start, period_end, status, closed_at, version, created_at"


@dataclass(frozen=True)
class PeriodRecord:
    id: uuid.UUID
    period_start: date
    period_end: date
    status: str
    closed_at: datetime | None
    version: int
    created_at: datetime


def _row_to_period(row: Any) -> PeriodRecord:
    return PeriodRecord(
        id=uuid.UUID(str(row[0])),
        period_start=row[1],
        period_end=row[2],
        status=str(row[3]),
        closed_at=row[4],
        version=int(row[5]),
        created_at=row[6],
    )


def _tenant_lock_key(tenant_id: uuid.UUID) -> int:
    """Stable int4 advisory key for the tenant's period barrier."""
    return int.from_bytes(tenant_id.bytes[:4], "big") & 0x7FFFFFFF


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day (inclusive) of a calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


async def assert_open_period(
    session: AsyncSession, tenant_id: uuid.UUID, occurred_at: datetime
) -> None:
    """Refuse postings outside an open period (issue #12, gates 1.4-1.6).

    Called by ledger._post for EVERY posting before anything is
    written. Raises InvalidInputError for future-dated timestamps and
    ConflictError (409) when occurred_at falls inside a closed period.
    The error carries no period details beyond the category (least
    disclosure); the audit trail of the close action holds them.
    """
    if occurred_at.tzinfo is None:
        # Defensive: all internal callers pass tz-aware UTC timestamps.
        occurred_at = occurred_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if occurred_at > now + CLOCK_SKEW:
        raise InvalidInputError("posting occurred_at must not be in the future")

    # SHARED period barrier: concurrent postings proceed in parallel;
    # a concurrent close_period (exclusive) serialises against them,
    # closing the check-then-insert TOCTOU window (gate 1.4). Both
    # values are computed integers, never user input.
    lock_key = _tenant_lock_key(tenant_id)
    await session.execute(text(f"SELECT pg_advisory_xact_lock_shared({_PERIOD_NS}, {lock_key})"))
    posting_date = occurred_at.astimezone(UTC).date()
    closed = (
        await session.execute(
            text(
                # Explicit tenant predicate on top of RLS (defence in
                # depth, gate 1.6 v1.1); served by the UNIQUE
                # (tenant_id, period_start) index.
                "SELECT 1 FROM accounting_periods "
                "WHERE tenant_id = CAST(:tid AS uuid) AND status = 'closed' "
                "AND :d >= period_start AND :d <= period_end LIMIT 1"
            ),
            {"tid": str(tenant_id), "d": posting_date},
        )
    ).first()
    if closed is not None:
        raise ConflictError(
            "posting date falls inside a closed accounting period; "
            "post corrections in the current period"
        )


async def close_period(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    year: int,
    month: int,
) -> PeriodRecord:
    """Close a fully elapsed calendar month (gates 1.4, 1.5).

    The EXCLUSIVE advisory lock waits for every in-flight posting
    (shared holders) to commit before the close claims the period, so a
    posting can never slip into a period that is closing underneath it.
    The UNIQUE (tenant_id, period_start) claim is made atomically via
    INSERT .. ON CONFLICT DO NOTHING checked by rowcount — concurrent
    closes collapse to exactly one closed row. Audit + outbox are
    written in the same transaction.
    """
    period_start, period_end = month_bounds(year, month)
    today = datetime.now(UTC).date()
    if period_end >= today:
        # Closing the current or a future month would refuse every live
        # posting — a self-inflicted outage, so it is a 409, not a
        # valid close.
        raise ConflictError("only fully elapsed months can be closed")

    lock_key = _tenant_lock_key(tenant_id)
    await session.execute(text(f"SELECT pg_advisory_xact_lock({_PERIOD_NS}, {lock_key})"))

    period_id = uuid.uuid4()
    now = datetime.now(UTC)
    claimed = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "INSERT INTO accounting_periods "
                "(id, tenant_id, period_start, period_end, status, closed_by, closed_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, :pe, 'closed', "
                "CAST(:actor AS uuid), :now) "
                "ON CONFLICT (tenant_id, period_start) DO NOTHING"
            ),
            {
                "id": str(period_id),
                "tid": str(tenant_id),
                "ps": period_start,
                "pe": period_end,
                "actor": str(actor_id) if actor_id else None,
                "now": now,
            },
        ),
    )
    if claimed.rowcount != 1:
        raise ConflictError(f"accounting period {period_start.isoformat()} is already closed")

    # P13.17(a): the month-end portfolio snapshot is written in the
    # SAME transaction, while the exclusive barrier guarantees no
    # posting into this month is in flight — so the reconstruction the
    # snapshot stores is final the instant the close commits. The
    # writer claims atomically (ON CONFLICT, v1.1 rule 5); if a
    # backfill already wrote this month it verifies equality and 409s
    # LOUDLY on divergence (FM1), aborting the close — a diverging
    # snapshot is never trusted, never self-healed. No row locks are
    # taken (lock-order.md: close_period stays advisory-only + claims).
    await write_month_snapshot(session, tenant_id, actor_id, period_end, source="close_period")

    # P13.17(b): the period's per-account rollups and member balances
    # are written in the SAME transaction — the 0012 trigger freezes
    # the month the instant the close commits, so the rollups are
    # immutable facts. The writer claims via ON CONFLICT, verifies the
    # stored set equals the reconstruction (409 loudly on ANY
    # divergence — FM2, never self-healed) and sets the write-once
    # rollup_at marker that arms the DB-level late-insert fence.
    await write_period_rollups(
        session,
        tenant_id,
        actor_id,
        period_id=period_id,
        period_start=period_start,
        period_end=period_end,
        source="close_period",
    )

    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="accounting_period.close",
        entity="accounting_periods",
        entity_id=str(period_id),
        after={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "status": "closed",
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="accounting.period_closed",
        payload={
            "period_id": str(period_id),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
    )
    return PeriodRecord(
        id=period_id,
        period_start=period_start,
        period_end=period_end,
        status="closed",
        closed_at=now,
        version=1,
        created_at=now,
    )


async def list_periods(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[PeriodRecord], str | None]:
    """Keyset-paginated period listing, newest first, cap 100 (gate 1.3).

    period_start is unique per tenant, so it is the whole keyset. The
    cursor is its ISO date.
    """
    limit = max(1, min(limit, 100))
    clauses = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        try:
            params["c_ps"] = date.fromisoformat(cursor)
        except ValueError as exc:
            raise InvalidInputError("malformed period cursor") from exc
        clauses.append("period_start < :c_ps")
    where = f"WHERE {' AND '.join(clauses)} "
    # Static fragments chosen in code; all values are bound parameters.
    rows = (
        await session.execute(
            text(
                f"SELECT {_PERIOD_COLS} FROM accounting_periods "  # noqa: S608
                f"{where}"
                "ORDER BY period_start DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_period(r) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        next_cursor = items[-1].period_start.isoformat()
    return items, next_cursor
