"""Month-end portfolio reconstruction — the single source of truth (P13.17a).

NPL_TREND_MONTH_SQL and the month-end walk moved here VERBATIM from
application/reports.py (where they shipped in P13) so that the DSA-1
snapshot writer (application/portfolio_snapshots.py) and the NPL-trend
report builder execute the SAME statement — the math is never
dual-maintained (gate 1.1; P13.17 blocker d "no dual-maintained
math"). reports.py re-exports both names, so every existing import
site (tests/test_p13_explain.py, application/dashboard.py's docstring
cross-reference) is unchanged.

This module is a LEAF: it imports only domain code, so the close_period
service can depend on the snapshot writer without creating an import
cycle through reports -> member_exits -> ledger -> accounting_periods.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.domain.ledger import Account
from genesis.domain.money import to_cents

#: One month-end snapshot, reconstructed from the append-only record
#: (gate 1.5: never from current mutable state):
#:   * outstanding principal per loan = disbursed principal minus the
#:     loans.receivable credit legs of its repayments up to the cutoff
#:     (ledger-reconstructed, the deposit-interest ADB precedent);
#:   * days past due = cutoff minus the earliest installment whose
#:     cumulative schedule due exceeds the cash repaid by the cutoff;
#:   * NPL = days past due > 90 (domain classify threshold:
#:     substandard and worse).
#: Loans closed on or before the cutoff are excluded (their terminal
#: postings — repayment closure or P12 exit set-off — zeroed them);
#: written_off is excluded pending a write-off flow (none exists yet).
NPL_TREND_MONTH_SQL = """
WITH paid AS (
    SELECT r.loan_id, COALESCE(SUM(r.amount), 0) AS paid
    FROM repayments r
    JOIN transactions t ON t.id = r.transaction_id AND t.tenant_id = r.tenant_id
    WHERE r.tenant_id = CAST(:tid AS uuid) AND t.occurred_at < :d_next
    GROUP BY r.loan_id
),
principal_paid AS (
    SELECT r.loan_id, COALESCE(SUM(le.amount), 0) AS principal_paid
    FROM ledger_entries le
    JOIN repayments r
        ON r.transaction_id = le.transaction_id AND r.tenant_id = le.tenant_id
    JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id
    WHERE le.tenant_id = CAST(:tid AS uuid)
      AND le.account = :receivable_account AND le.side = 'credit'
      AND t.occurred_at < :d_next
    GROUP BY r.loan_id
),
sched AS (
    SELECT s.loan_id, s.due_date,
           SUM(s.total_due) OVER (
               PARTITION BY s.loan_id ORDER BY s.installment_no
           ) AS cum_due
    FROM loan_schedules s
    WHERE s.tenant_id = CAST(:tid AS uuid) AND s.due_date <= :d_date
),
first_unmet AS (
    SELECT sc.loan_id, MIN(sc.due_date) AS due
    FROM sched sc
    LEFT JOIN paid p ON p.loan_id = sc.loan_id
    WHERE sc.cum_due > COALESCE(p.paid, 0)
    GROUP BY sc.loan_id
)
SELECT
    COUNT(*) AS loans,
    COALESCE(SUM(l.principal - COALESCE(pp.principal_paid, 0)), 0) AS gross,
    COUNT(*) FILTER (
        WHERE fu.due IS NOT NULL AND (:d_date - fu.due) > 90
    ) AS npl_loans,
    COALESCE(SUM(l.principal - COALESCE(pp.principal_paid, 0)) FILTER (
        WHERE fu.due IS NOT NULL AND (:d_date - fu.due) > 90
    ), 0) AS npl_balance
FROM loans l
LEFT JOIN principal_paid pp ON pp.loan_id = l.id
LEFT JOIN first_unmet fu ON fu.loan_id = l.id
WHERE l.tenant_id = CAST(:tid AS uuid)
  AND l.status <> 'written_off'
  AND l.disbursed_at < :d_next
  AND (l.closed_at IS NULL OR l.closed_at >= :d_next)
"""


def npl_trend_month_ends(as_of: datetime, months: int) -> list[date]:
    """Month-end cutoffs, oldest first; the current month cuts at as_of."""
    ends: list[date] = [as_of.astimezone(UTC).date()]
    cursor = ends[0].replace(day=1)
    for _ in range(months - 1):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        ends.append((cursor + timedelta(days=31)).replace(day=1) - timedelta(days=1))
    ends.reverse()
    return ends


@dataclass(frozen=True)
class MonthPortfolio:
    """The four figures NPL_TREND_MONTH_SQL yields for one cutoff."""

    month_end: date
    loans: int
    gross_outstanding: Decimal
    npl_loans: int
    npl_balance: Decimal


async def reconstruct_month(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    month_end: date,
    *,
    as_of: datetime | None = None,
) -> MonthPortfolio:
    """Execute the reconstruction for exactly one cutoff.

    The cutoff instant is the UTC end of month_end's calendar day; an
    as_of cap (the export's snapshot instant) additionally bounds the
    CURRENT, incomplete month exactly as the P13 export always has.
    All values are bound parameters; the receivable account identifier
    comes from the code-owned chart (v1.1 rule 6).
    """
    cutoff = datetime(month_end.year, month_end.month, month_end.day, tzinfo=UTC)
    d_next = cutoff + timedelta(days=1)
    if as_of is not None:
        d_next = min(d_next, as_of + timedelta(microseconds=1))
    raw = (
        await session.execute(
            text(NPL_TREND_MONTH_SQL),
            {
                "tid": str(tenant_id),
                "d_next": d_next,
                "d_date": month_end,
                "receivable_account": Account.LOANS_RECEIVABLE.value,
            },
        )
    ).one()
    # Canonical cents (domain.money.to_cents, the single rounding
    # truth): the SQL's COALESCE(SUM(..), 0) yields a scale-0 zero for
    # empty aggregates, while the numeric(18,2) snapshot columns read
    # back at scale 2 - quantizing HERE makes the reconstruction and
    # the stored snapshot byte-identical in every rendering (values
    # were always numerically equal; pipeline 2725330879 caught the
    # '0' vs '0.00' artifact divergence the moment backend:test could
    # first run this suite).
    return MonthPortfolio(
        month_end=month_end,
        loans=int(raw[0]),
        gross_outstanding=to_cents(Decimal(str(raw[1]))),
        npl_loans=int(raw[2]),
        npl_balance=to_cents(Decimal(str(raw[3]))),
    )
