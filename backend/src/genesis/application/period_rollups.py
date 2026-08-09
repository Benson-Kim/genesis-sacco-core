"""Closed-period rollups: per-account totals + member balances (.17b).

DSA-2/DSA-5 remediation (docs/DSA_HARDENING.md) without changing any
observable money semantics:

  * the ledger is append-only and the 0012 closed-period trigger
    refuses any transaction dated inside a closed period, so the
    per-account aggregate over a CLOSED period is an immutable fact —
    it is written exactly once (at close_period, in the same
    transaction and under its exclusive per-tenant advisory barrier,
    or by the backfill below for periods closed before 0028) and the
    trial balance becomes: SUM(rollups of rolled periods) + live
    aggregate over every posting NOT covered by a rolled period
    (TRIAL_BALANCE_ROLLUP_SQL). Equality with the full scan to the
    cent is the merge gate (tests/test_p1317_period_rollups.py).

  * member_period_balances stores each active member's
    ledger-reconstructed signed balance at the period end; the
    member-statement opening balance anchors on the latest rolled
    period before the window (MEMBER_ANCHOR_SQL) and scans only the
    delta since — the DSA-5 replacement. The signed direction comes
    from the single source of truth (domain.ledger.member_direction)
    applied to the SAME grouped-movement statement the statement
    opening always ran (member_movement_sql generalises reports.member_statement_opening_sql
    byte-identically for its original configurations — no dual-maintained math, reuse-first).

  * completeness marker: accounting_periods.rollup_at is set in the
    SAME transaction that writes a period's rows. Readers trust ONLY
    marked periods; the 0028 late-insert fence refuses new rollup rows
    once the marker is set, and the write-once triggers refuse
    UPDATE/DELETE always — so a fabricated/edited rollup is
    unrepresentable after completion and collides loudly (verify +
    409, never self-healed) before it.

Concurrency: the backfill claims one period per short transaction with
FOR UPDATE SKIP LOCKED (shared batch runner) — a re-run
scans zero rows and locks nothing; rollup writes claim atomically via
ON CONFLICT (rule 5). Every read and write carries an explicit bound
tenant_id predicate on top of forced RLS (rule 4); all values are
bound parameters (rule 6); divergence errors name the period only —
exact figures live in the write-once tables and the audit row (rule 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.domain.ledger import Side, TxnType, member_direction
from genesis.domain.money import ZERO
from genesis.errors import ConflictError

__all__ = [
    "MEMBER_ANCHOR_SQL",
    "TRIAL_BALANCE_ROLLUP_SQL",
    "RollupBackfillResult",
    "account_activity_sql",
    "member_balance_before",
    "member_movement_sql",
    "run_rollup_backfill_for_tenant",
    "signed_member_movement",
    "write_period_rollups",
]


def account_activity_sql(*, with_from: bool, with_to: bool) -> str:
    """Per-account, per-side ledger activity, optionally period-scoped.

    Moved VERBATIM from application/reports.py in (b): the single
    aggregate behind the income statement, the SASRA return AND the
    per-account period rollups written at close_period — one statement,
    never dual-maintained (reuse-first). Bounded by the chart-of-accounts
    cardinality, served by idx_ledger_txn + the transactions primary
    key (the trial-balance shape). Static fragments chosen in code;
    all values are bound parameters.
    """
    clauses = [
        "le.tenant_id = CAST(:tid AS uuid)",
        "t.occurred_at <= :as_of",
    ]
    if with_from:
        clauses.append("t.occurred_at >= :d_from")
    if with_to:
        clauses.append("t.occurred_at < :d_to_excl")
    return (
        "SELECT le.account, le.side, COALESCE(SUM(le.amount), 0) "  # noqa: S608
        "FROM ledger_entries le "
        "JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id "
        f"WHERE {' AND '.join(clauses)} "
        "GROUP BY le.account, le.side ORDER BY le.account, le.side"
    )


def member_movement_sql(*, with_from: bool, with_start: bool) -> str:
    """Grouped member movement by (type, is_reversal) (shape).

    The generalisation of reports.member_statement_opening_sql (which now delegates here — for
    with_start=False the emitted SQL is byte-identical to the original): the optional lower bound
    serves the anchored delta scans of the DSA-5 opening balance and
    the rollup writer. The signed direction is applied in Python via
    member_direction (single source of truth); the DR/CR convention is
    never duplicated in SQL.
    """
    clauses = [
        "tenant_id = CAST(:tid AS uuid)",
        "member_id = CAST(:mid AS uuid)",
        "occurred_at <= :as_of",
    ]
    if with_from:
        clauses.append("occurred_at < :d_from")
    if with_start:
        clauses.append("occurred_at >= :d_start")
    return (
        "SELECT type, (reversal_of_id IS NOT NULL) AS is_reversal, "  # noqa: S608
        "COALESCE(SUM(amount), 0) "
        f"FROM transactions WHERE {' AND '.join(clauses)} "
        "GROUP BY type, (reversal_of_id IS NOT NULL)"
    )


def signed_member_movement(rows: list[Any]) -> Decimal:
    """Fold grouped movement rows into a signed total (credits +)."""
    total = ZERO
    for type_raw, is_reversal, amount_raw in rows:
        direction = member_direction(TxnType(str(type_raw)), is_reversal=bool(is_reversal))
        amount = Decimal(str(amount_raw))
        total += amount if direction is Side.CREDIT else -amount
    return total


#: Latest ROLLED period strictly before:before, per member — the
#: DSA-5 opening anchor. Served by idx_member_period_balances_anchor
#: (0028; ships with this query, scalability). Only rolled periods
#: qualify (rollup_at set): an unrolled or fabricated-pre-backfill row
#: is never trusted.
MEMBER_ANCHOR_SQL = """
SELECT b.balance, p.period_end
FROM member_period_balances b
JOIN accounting_periods p
    ON p.tenant_id = b.tenant_id AND p.period_start = b.period_start
WHERE b.tenant_id = CAST(:tid AS uuid) AND b.member_id = CAST(:mid AS uuid)
  AND p.status = 'closed' AND p.rollup_at IS NOT NULL
  AND p.period_end < :before
ORDER BY b.period_start DESC
LIMIT 1
"""

#: Trial balance = rolled closed periods + live remainder (DSA-2). The
#: rolled CTE trusts only periods whose completeness marker is set AND
#: that are FULLY COVERED by:as_of — a rolled period participates as
#: a rollup only when (as_of AT TIME ZONE 'UTC'):date > period_end
#: (strictly greater: an intra-day:as_of on the period-end day means
#: the day is only partially covered, so the whole period falls
#: through to the live scan, which caps at:as_of exactly). The live
#: CTE excludes exactly the SAME as_of-qualified periods' calendar
#: days (the 0012 trigger's UTC-date convention) — the qualifier is
#: applied SYMMETRICALLY in both CTEs, so every posting <=:as_of is
#: counted exactly once for ANY historical:as_of, not just "now"
#: (an unqualified rolled CTE summed every rolled
#: period's full totals unconditionally, overstating both sides of a
#: historical trial balance while still balancing — the class).
#: The boundary is exact against the writer's exclusive d_to_excl
#: bound: rollups cover occurred_at < UTC midnight of period_end + 1,
#: and the date qualifier admits a period exactly from that midnight.
#: Equality with the full scan (reports.TRIAL_BALANCE_SQL, kept as
#: the oracle) at historical, boundary and current:as_of instants is
#: the merge gate.
TRIAL_BALANCE_ROLLUP_SQL = """
WITH rolled AS (
    SELECT b.account,
           COALESCE(SUM(b.debits), 0) AS debits,
           COALESCE(SUM(b.credits), 0) AS credits
    FROM account_period_balances b
    JOIN accounting_periods p
        ON p.tenant_id = b.tenant_id AND p.period_start = b.period_start
       AND p.status = 'closed' AND p.rollup_at IS NOT NULL
       AND (CAST(:as_of AS timestamptz) AT TIME ZONE 'UTC')::date > p.period_end
    WHERE b.tenant_id = CAST(:tid AS uuid)
    GROUP BY b.account
),
live AS (
    SELECT le.account,
           COALESCE(SUM(le.amount) FILTER (WHERE le.side = 'debit'), 0) AS debits,
           COALESCE(SUM(le.amount) FILTER (WHERE le.side = 'credit'), 0) AS credits
    FROM ledger_entries le
    JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id
    WHERE le.tenant_id = CAST(:tid AS uuid) AND t.occurred_at <= :as_of
      AND NOT EXISTS (
          SELECT 1 FROM accounting_periods p
          WHERE p.tenant_id = le.tenant_id AND p.status = 'closed'
            AND p.rollup_at IS NOT NULL
            AND (CAST(:as_of AS timestamptz) AT TIME ZONE 'UTC')::date > p.period_end
            AND (t.occurred_at AT TIME ZONE 'UTC')::date >= p.period_start
            AND (t.occurred_at AT TIME ZONE 'UTC')::date <= p.period_end
      )
    GROUP BY le.account
)
SELECT account,
       SUM(debits) AS debits,
       SUM(credits) AS credits
FROM (SELECT * FROM rolled UNION ALL SELECT * FROM live) united
GROUP BY account
ORDER BY account
"""

_PERIOD_MEMBERS_SQL = (
    "SELECT DISTINCT member_id FROM transactions "
    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id IS NOT NULL "
    "AND occurred_at >= :d_from AND occurred_at < :d_to_excl"
)

_EXISTING_ACCOUNT_ROWS_SQL = (
    "SELECT account, debits, credits FROM account_period_balances "
    "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps"
)

_EXISTING_MEMBER_ROWS_SQL = (
    "SELECT member_id, balance FROM member_period_balances "
    "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps"
)

#: Backfill claim: oldest closed-but-unrolled period, one per short
#: transaction, skipping rows a concurrent runner holds.
#: Served by the UNIQUE (tenant_id, period_start) index (0012).
ROLLUP_BACKFILL_SCAN_SQL = (
    "SELECT id, period_start, period_end FROM accounting_periods "
    "WHERE tenant_id = CAST(:tid AS uuid) AND status = 'closed' "
    "AND rollup_at IS NULL "
    "ORDER BY period_start LIMIT 1 FOR UPDATE SKIP LOCKED"
)


def _utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def member_balance_before(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    before: date,
    as_of: datetime,
) -> Decimal:
    """Signed member balance over activity < `before` (UTC midnight).

    Anchored on the latest rolled period entirely before `before` when
    one exists — any member activity in a LATER rolled period would
    have produced a later anchor row (the writer covers every active
    member of a rolled period), so the delta scan over
    [anchor end + 1 day, before) misses nothing and double-counts
    nothing. Without an anchor this is exactly the full-history
    opening scan. Both paths additionally cap at `as_of` (the export
    snapshot instant); anchor content always predates as_of because
    only fully elapsed months can be closed.
    """
    before_at = _utc_midnight(before)
    anchor = (
        await session.execute(
            text(MEMBER_ANCHOR_SQL),
            {"tid": str(tenant_id), "mid": str(member_id), "before": before},
        )
    ).first()
    params: dict[str, object] = {
        "tid": str(tenant_id),
        "mid": str(member_id),
        "as_of": as_of,
        "d_from": before_at,
    }
    if anchor is None:
        rows = (
            await session.execute(
                text(member_movement_sql(with_from=True, with_start=False)), params
            )
        ).all()
        return signed_member_movement(list(rows))
    anchor_balance = Decimal(str(anchor[0]))
    anchor_end = cast(date, anchor[1])
    params["d_start"] = _utc_midnight(anchor_end + timedelta(days=1))
    rows = (
        await session.execute(text(member_movement_sql(with_from=True, with_start=True)), params)
    ).all()
    return anchor_balance + signed_member_movement(list(rows))


@dataclass(frozen=True)
class PeriodRollupCounts:
    accounts: int
    members: int


async def write_period_rollups(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    period_id: uuid.UUID,
    period_start: date,
    period_end: date,
    source: str,
) -> PeriodRollupCounts:
    """Write one closed period's rollups + completeness marker.

    Runs inside the caller's transaction (close_period, or one backfill
    batch holding the period row FOR UPDATE). Statement order is a
    correctness invariant, pinned by
    tests/test_p1317_period_rollups.py:

      1. INSERT the rollup rows for BOTH tables (ON CONFLICT DO
         NOTHING claims);
      2. set accounting_periods.rollup_at — THE SERIALISATION POINT:
         the marker UPDATE takes FOR NO KEY UPDATE on the period row,
         which conflicts with the FOR SHARE the 0028 late-insert
         fence takes for every in-flight direct-SQL INSERT, so the
         marker waits out every such fabricator before it can set;
      3. only THEN verify the STORED sets equal the freshly computed
         sets. Verify MUST follow the marker: with verify first (the
         pre-N3b order), a fabricator whose INSERT held FOR SHARE
         could commit invisibly AFTER the verify read but BEFORE the
         marker UPDATE unblocked — frozen inside a "complete" period
         that verify never saw. After the marker, every such row is
         committed and visible to the verify read; any divergence
         (fabricated row, extra row, wrong figure) raises
         ConflictError (409, loud) and rolls back EVERYTHING,
         including the marker. Nothing is ever self-healed.
    """
    d_from = _utc_midnight(period_start)
    d_to_excl = _utc_midnight(period_end + timedelta(days=1))

    activity_rows = (
        await session.execute(
            text(account_activity_sql(with_from=True, with_to=True)),
            {"tid": str(tenant_id), "as_of": d_to_excl, "d_from": d_from, "d_to_excl": d_to_excl},
        )
    ).all()
    accounts: dict[str, tuple[Decimal, Decimal]] = {}
    for account, side, amount_raw in activity_rows:
        debits, credits = accounts.get(str(account), (ZERO, ZERO))
        amount = Decimal(str(amount_raw))
        if str(side) == Side.DEBIT.value:
            debits += amount
        else:
            credits += amount
        accounts[str(account)] = (debits, credits)
    for account, (debits, credits) in accounts.items():
        await session.execute(
            text(
                "INSERT INTO account_period_balances "
                "(id, tenant_id, period_start, account, debits, credits) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, :acct, :dr, :cr) "
                "ON CONFLICT (tenant_id, period_start, account) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "ps": period_start,
                "acct": account,
                "dr": str(debits),
                "cr": str(credits),
            },
        )
    member_rows = (
        await session.execute(
            text(_PERIOD_MEMBERS_SQL),
            {"tid": str(tenant_id), "d_from": d_from, "d_to_excl": d_to_excl},
        )
    ).scalars()
    members: dict[uuid.UUID, Decimal] = {}
    for raw_member in member_rows:
        member_id = uuid.UUID(str(raw_member))
        members[member_id] = await member_balance_before(
            session,
            tenant_id,
            member_id,
            before=period_end + timedelta(days=1),
            as_of=d_to_excl,
        )
    for member_id, balance in members.items():
        await session.execute(
            text(
                "INSERT INTO member_period_balances "
                "(id, tenant_id, period_start, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, "
                "CAST(:mid AS uuid), :bal) "
                "ON CONFLICT (tenant_id, period_start, member_id) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "ps": period_start,
                "mid": str(member_id),
                "bal": str(balance),
            },
        )
    # The serialisation point (N3b): the marker's FOR NO KEY UPDATE
    # waits out every in-flight fenced INSERT's FOR SHARE before it
    # sets; the verifies BELOW therefore observe every row that could
    # ever be frozen inside this period.
    marked = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE accounting_periods SET rollup_at = now(), updated_at = now() "
                "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps "
                "AND rollup_at IS NULL"
            ),
            {"tid": str(tenant_id), "ps": period_start},
        ),
    )
    if marked.rowcount != 1:
        # Unreachable under the close claim / the backfill's period row
        # lock; defence in depth (a completed period is never re-rolled).
        raise ConflictError(f"period {period_start.isoformat()} is already rolled up")

    stored_accounts = {
        str(row[0]): (Decimal(str(row[1])), Decimal(str(row[2])))
        for row in (
            await session.execute(
                text(_EXISTING_ACCOUNT_ROWS_SQL),
                {"tid": str(tenant_id), "ps": period_start},
            )
        ).all()
    }
    if stored_accounts != accounts:
        raise ConflictError(
            f"account rollups for period {period_start.isoformat()} diverge "
            "from the ledger reconstruction; investigate before proceeding"
        )
    stored_members = {
        uuid.UUID(str(row[0])): Decimal(str(row[1]))
        for row in (
            await session.execute(
                text(_EXISTING_MEMBER_ROWS_SQL),
                {"tid": str(tenant_id), "ps": period_start},
            )
        ).all()
    }
    if stored_members != members:
        raise ConflictError(
            f"member balances for period {period_start.isoformat()} diverge "
            "from the ledger reconstruction; investigate before proceeding"
        )
    # One audit row per period; exact figures live in the write-once
    # rollup tables themselves.
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="accounting_period.rollup",
        entity="accounting_periods",
        entity_id=str(period_id),
        after={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "accounts": len(accounts),
            "members": len(members),
            "source": source,
        },
    )
    return PeriodRollupCounts(accounts=len(accounts), members=len(members))


@dataclass(frozen=True)
class RollupBackfillResult:
    periods_rolled: int
    accounts_written: int
    members_written: int
    batches: int


async def run_rollup_backfill_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> RollupBackfillResult:
    """Roll up every closed-but-unrolled period (pre-0028 closes).

    One period per short transaction through the shared batch runner;
    the claim is the period row itself, FOR UPDATE SKIP LOCKED (v1.1
    rule 8) — concurrent runners take disjoint periods, and a re-run
    scans zero rows, locks nothing and writes nothing (side-effect counts). Nothing here is
    caller-suppliable: the worklist is
    the rollup_at IS NULL set (rule 1).
    """

    async def _process_one(
        session: AsyncSession, _cursor: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, PeriodRollupCounts]:
        row = (
            await session.execute(text(ROLLUP_BACKFILL_SCAN_SQL), {"tid": str(tenant_id)})
        ).first()
        if row is None:
            return 0, None, PeriodRollupCounts(accounts=0, members=0)
        counts = await write_period_rollups(
            session,
            tenant_id,
            actor_id,
            period_id=uuid.UUID(str(row[0])),
            period_start=cast(date, row[1]),
            period_end=cast(date, row[2]),
            source="backfill",
        )
        return 1, None, counts

    scanned, batches, payloads = await run_in_batches(session_scope, _process_one, batch_size=1)
    return RollupBackfillResult(
        periods_rolled=scanned,
        accounts_written=sum(p.accounts for p in payloads),
        members_written=sum(p.members for p in payloads),
        batches=batches,
    )
