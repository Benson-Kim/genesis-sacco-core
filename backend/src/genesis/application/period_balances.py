"""Ledger-reconstructed period balances (P11 ADB, shared by P13.11).

The single implementation of the average-daily-balance basis
(MASTER_PROMPT 1.5): values derived from balances OVER A PERIOD are
computed from the append-only ledger — never from a point-in-time
balance snapshot, which is a proven exploit class (park funds on the
measurement day, withdraw the next). Extracted verbatim from the P11
deposit-interest job (gate 1.1: the P13.11 dividend basis must reuse,
not fork, this reconstruction) and generalised over the account kind:

  * deposit_accounts <-> ledger account member.deposits
  * share_accounts   <-> ledger account member.shares

Every balance writer posts its member-attributed leg through the P7
contract in the same transaction as the balance update, so

    end_of_period_balance = current_balance
                            - net(member legs at/after the cutoff)

computed in ONE statement (balance and net movement read at the same
snapshot — consistent with or without the caller holding the account
row lock), and walking backwards day by day from period end to period
start, undoing each day's net movement, yields every end-of-day
balance. The basis is the cent-rounded mean of those (positive-
clamped) balances: money held D of N days earns exactly D/N of a full
period (the P11 review-finding-14 rule; the P13.11 mid-year-joiner
oracle).

Callers computing a MONEY FIGURE from this basis do so under the
account row lock (P11/P13.11 job batches); lock-free callers (the
P13.11 declaration totals) rely on the reconstruction's timing
invariance: historical legs are append-only and the single-statement
end-anchor is snapshot-consistent, so a concurrent posting dated after
the period changes current_balance and the subtracted net equally.

Every read carries an explicit bound tenant_id predicate on top of
forced RLS (v1.1 rule 4); all values are bound parameters (rule 6).
The SQL builders are exported so the EXPLAIN captures assert against
the production statements (tests/test_p11_explain.py,
tests/test_p1311_explain.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.domain.ledger import Account
from genesis.domain.money import ZERO, to_cents

#: The two reconstructable account kinds. Table names and ledger
#: accounts come exclusively from this code-owned mapping (v1.1
#: rule 6) — never from caller input.
ACCOUNT_KINDS: dict[str, tuple[str, Account]] = {
    "deposit": ("deposit_accounts", Account.MEMBER_DEPOSITS),
    "share": ("share_accounts", Account.MEMBER_SHARES),
}


def eod_balance_sql(kind: str) -> str:
    """End-of-period balance anchor for one member's account (one statement).

    current balance MINUS the net member-attributed movement at/after
    the cutoff — both read at the same statement snapshot, so the
    anchor is consistent even without the account row lock. Table and
    account identifiers come from ACCOUNT_KINDS (code-owned).
    """
    table, _ = ACCOUNT_KINDS[kind]
    return (
        # Static fragments chosen in code; all values are bound parameters.
        f"SELECT a.balance - COALESCE((SELECT SUM(CASE WHEN le.side = 'credit' "  # noqa: S608
        "THEN le.amount ELSE -le.amount END) "
        "FROM ledger_entries le "
        "JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id "
        "WHERE le.tenant_id = a.tenant_id AND t.member_id = a.member_id "
        "AND le.account = :acct AND t.occurred_at >= :cutoff), 0) "
        f"FROM {table} a "
        "WHERE a.tenant_id = CAST(:tid AS uuid) AND a.member_id = CAST(:m AS uuid)"
    )


DAILY_MOVEMENTS_SQL = (
    "SELECT (t.occurred_at AT TIME ZONE 'UTC')::date AS day, "
    "SUM(CASE WHEN le.side = 'credit' THEN le.amount ELSE -le.amount END) "
    "FROM ledger_entries le "
    "JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id "
    "WHERE le.tenant_id = CAST(:tid AS uuid) AND t.member_id = CAST(:m AS uuid) "
    "AND le.account = :acct "
    "AND t.occurred_at >= :p_start AND t.occurred_at < :cutoff "
    "GROUP BY 1"
)


async def average_daily_balance(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    kind: str,
    start: date,
    end: date,
) -> Decimal:
    """Average daily balance over [start, end], reconstructed from the ledger.

    The P11 walk, verbatim: starting from the end-of-period anchor,
    undo each day's net movement backwards from end to start, producing
    every end-of-day balance in the period; the basis is the
    cent-rounded mean of those balances, each clamped at zero (guard
    for directly seeded fixtures without ledger history). A deposit
    parked for only the last day of the period earns exactly 1/N of it
    (P11 review finding 14) instead of the full period a point-in-time
    snapshot would have paid. A member without the account earns
    nothing (ZERO).
    """
    _, account = ACCOUNT_KINDS[kind]
    period_start_at = datetime.combine(start, time.min, tzinfo=UTC)
    cutoff = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    anchor_row = (
        await session.execute(
            text(eod_balance_sql(kind)),
            {
                "tid": str(tenant_id),
                "m": str(member_id),
                "acct": account.value,
                "cutoff": cutoff,
            },
        )
    ).first()
    if anchor_row is None:
        return ZERO
    end_balance = to_cents(Decimal(str(anchor_row[0])))
    if end_balance < ZERO:
        end_balance = ZERO
    rows = (
        await session.execute(
            text(DAILY_MOVEMENTS_SQL),
            {
                "tid": str(tenant_id),
                "m": str(member_id),
                "acct": account.value,
                "p_start": period_start_at,
                "cutoff": cutoff,
            },
        )
    ).all()
    movements: dict[date, Decimal] = {row[0]: Decimal(str(row[1])) for row in rows}
    total = ZERO
    balance = end_balance
    day = end
    while day >= start:
        if balance > ZERO:
            total += balance
        # End-of-day balance for the previous day = this day's
        # end-of-day balance minus this day's net movement.
        balance -= movements.get(day, ZERO)
        day -= timedelta(days=1)
    days_in_period = (end - start).days + 1
    return to_cents(total / days_in_period)
