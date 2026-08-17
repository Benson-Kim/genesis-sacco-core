"""Month-end portfolio snapshots: writer + backfill (P13.17a / DSA-1).

Replaces the NPL-trend export's months x full-history rescans with
incremental month-end snapshots (docs/DSA_HARDENING.md DSA-1) without
changing any observable money semantics:

  * every snapshot is COMPUTED by the same statement the export always
    ran (portfolio_reconstruction.NPL_TREND_MONTH_SQL — single source
    of truth, gate 1.1) for exactly one fully elapsed month;
  * write sites are close_period (same transaction, under its
    exclusive per-tenant advisory barrier — lock-order.md §6) and the
    backfill job below (shared batch runner, one month per short
    transaction, v1.1 rule 8);
  * the claim is atomic (INSERT .. ON CONFLICT DO NOTHING checked by
    rowcount, v1.1 rule 5) against UNIQUE (tenant_id, month_end), and
    rows are DB-level WRITE-ONCE (0027 trigger, FM1): a restated month
    is unrepresentable even via direct SQL through the app role;
  * a claim that loses to an EXISTING row verifies that row against
    the fresh reconstruction and raises ConflictError (409, loud) on
    ANY divergence — a diverging snapshot is investigated, never
    silently overwritten or silently trusted (insider hardening: the
    figures auditors read must never self-heal).

Re-runs of the backfill are lock-free no-ops: the month worklist
anti-joins on the claim key (v1.1 rule 8), so a fully snapshotted
tenant scans zero months and writes nothing (proven by side-effect
counts in tests/test_p1317_portfolio_snapshots.py).

Every read and write carries an explicit bound tenant_id predicate on
top of forced RLS (v1.1 rule 4); all values are bound parameters
(rule 6). Least disclosure: divergence errors name the month only —
the exact figures live in the audit row of the original write (rule 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.portfolio_reconstruction import (
    MonthPortfolio,
    reconstruct_month,
)
from genesis.errors import ConflictError

__all__ = [
    "SNAPSHOT_LOOKUP_SQL",
    "SnapshotBackfillResult",
    "month_end_of",
    "run_snapshot_backfill_for_tenant",
    "write_month_snapshot",
]

#: The export's snapshot read: N rows by month end, served by
#: uq_portfolio_snapshots_month (0027 — the index ships with this
#: query, gate 1.3). Module-level so the P13.17 EXPLAIN capture
#: asserts against the production statement.
SNAPSHOT_LOOKUP_SQL = (
    "SELECT month_end, loans, gross_outstanding, npl_loans, npl_balance "
    "FROM portfolio_month_snapshots "
    "WHERE tenant_id = CAST(:tid AS uuid) "
    "AND month_end = ANY(CAST(:months AS date[]))"
)

_EXISTING_MONTH_SQL = (
    "SELECT month_end FROM portfolio_month_snapshots WHERE tenant_id = CAST(:tid AS uuid)"
)

_FIRST_DISBURSEMENT_SQL = (
    "SELECT MIN(disbursed_at) FROM loans "
    "WHERE tenant_id = CAST(:tid AS uuid) AND disbursed_at IS NOT NULL"
)


def month_end_of(day: date) -> date:
    """The last day of day's calendar month."""
    first_next = (day.replace(day=1) + timedelta(days=31)).replace(day=1)
    return first_next - timedelta(days=1)


async def write_month_snapshot(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    month_end: date,
    *,
    source: str,
) -> bool:
    """Reconstruct and claim one fully elapsed month's snapshot.

    Returns True when a new row was written, False when an identical
    snapshot already existed (idempotent no-op). Raises ConflictError
    (409, loud — FM1) when an existing row diverges from the fresh
    reconstruction; nothing is ever self-healed. The caller owns the
    transaction (close_period runs this inside its own; the backfill
    gives each month its own short one).
    """
    if month_end != month_end_of(month_end):
        raise ConflictError("portfolio snapshots exist only for calendar month ends")
    if month_end >= datetime.now(UTC).date():
        # Mirrors close_period's fully-elapsed rule: a snapshot of a
        # month still receiving postings would freeze a moving figure.
        raise ConflictError("only fully elapsed months can be snapshotted")
    computed = await reconstruct_month(session, tenant_id, month_end)
    claimed = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "INSERT INTO portfolio_month_snapshots "
                "(id, tenant_id, month_end, loans, gross_outstanding, "
                " npl_loans, npl_balance, source) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :month_end, "
                ":loans, :gross, :npl_loans, :npl_balance, :source) "
                "ON CONFLICT (tenant_id, month_end) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "month_end": month_end,
                "loans": computed.loans,
                "gross": str(computed.gross_outstanding),
                "npl_loans": computed.npl_loans,
                "npl_balance": str(computed.npl_balance),
                "source": source,
            },
        ),
    )
    if claimed.rowcount == 1:
        # Exact figures live in the audit row (v1.1 rule 7): the write
        # is in the same transaction, so an abort leaves neither.
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="portfolio_snapshot.write",
            entity="portfolio_month_snapshots",
            entity_id=month_end.isoformat(),
            after={
                "month_end": month_end.isoformat(),
                "loans": computed.loans,
                "gross_outstanding": str(computed.gross_outstanding),
                "npl_loans": computed.npl_loans,
                "npl_balance": str(computed.npl_balance),
                "source": source,
            },
        )
        return True
    _assert_snapshot_matches(
        computed,
        await _read_snapshot(session, tenant_id, month_end),
    )
    return False


async def _read_snapshot(
    session: AsyncSession, tenant_id: uuid.UUID, month_end: date
) -> MonthPortfolio | None:
    row = (
        await session.execute(
            text(SNAPSHOT_LOOKUP_SQL),
            {"tid": str(tenant_id), "months": [month_end]},
        )
    ).first()
    if row is None:
        return None
    return MonthPortfolio(
        month_end=row[0],
        loans=int(row[1]),
        gross_outstanding=Decimal(str(row[2])),
        npl_loans=int(row[3]),
        npl_balance=Decimal(str(row[4])),
    )


def _assert_snapshot_matches(computed: MonthPortfolio, stored: MonthPortfolio | None) -> None:
    """FM1: divergence 409s loudly and never self-heals.

    Least disclosure (v1.1 rule 7): the error names the month only;
    the figures live in the original write's audit row and in the
    write-once table itself.
    """
    if stored is None or stored != computed:
        raise ConflictError(
            f"portfolio snapshot for {computed.month_end.isoformat()} diverges "
            "from the ledger reconstruction; investigate before proceeding"
        )


@dataclass(frozen=True)
class SnapshotBackfillResult:
    months_considered: int
    written: int
    batches: int


async def run_snapshot_backfill_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> SnapshotBackfillResult:
    """Snapshot every fully elapsed month since the first disbursement.

    Worklist = calendar month ends from the tenant's earliest
    disbursed_at through the last fully elapsed month (both
    server-resolved — nothing here is caller-suppliable, v1.1 rule 1),
    MINUS the months already snapshotted (anti-join on the claim key,
    rule 8: a completed re-run scans zero months, locks nothing and
    writes nothing). Each remaining month runs through the shared
    batch runner in its own short transaction (gate 1.3), claimed via
    ON CONFLICT (rule 5); a concurrent writer that got there first is
    verified for equality and 409s on divergence (FM1).
    """
    async with session_scope() as session:
        first_raw = (
            await session.execute(text(_FIRST_DISBURSEMENT_SQL), {"tid": str(tenant_id)})
        ).scalar_one_or_none()
        existing_rows = (
            await session.execute(text(_EXISTING_MONTH_SQL), {"tid": str(tenant_id)})
        ).scalars()
        existing = {cast(date, row) for row in existing_rows}
    if first_raw is None:
        return SnapshotBackfillResult(months_considered=0, written=0, batches=0)
    first_disbursed = cast(datetime, first_raw)
    today = datetime.now(UTC).date()
    months: list[date] = []
    cursor = month_end_of(first_disbursed.astimezone(UTC).date())
    while cursor < today:
        if cursor not in existing:
            months.append(cursor)
        cursor = month_end_of(cursor + timedelta(days=1))
    considered = len(months)
    remaining = list(months)

    async def _process_one(
        session: AsyncSession, _cursor: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, int]:
        if not remaining:
            return 0, None, 0
        month = remaining.pop(0)
        written = await write_month_snapshot(session, tenant_id, actor_id, month, source="backfill")
        return 1, None, int(written)

    _, batches, payloads = await run_in_batches(session_scope, _process_one, batch_size=1)
    return SnapshotBackfillResult(
        months_considered=considered,
        written=sum(payloads),
        batches=batches,
    )
