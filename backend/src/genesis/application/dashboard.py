"""Dashboard aggregates (P13.9): the remaining prototype dashboard figures.

GET /dashboard/summary serves: total deposits + total share capital,
active-member count and members-by-type, the monthly deposits-vs-
disbursements series, applications-pipeline counts per stage, guarantor
aggregates (live guarantees, total pledged, per-guarantor free capacity
via live_pledged_total — gate 1.1, no second capacity implementation),
and the P10 loan-book slice via loans_service.portfolio_summary (reuse,
never a fork).

Every figure is a SQL aggregate — never a Python loop over result rows
(the only loops here run over the code-owned month keys and the
config-capped guarantor list). Every read carries an explicit bound
tenant_id predicate on top of forced RLS (v1.1 rule 4; issue #17), all
values travel as bound parameters (rule 6), and the window/list bounds
are server-resolved from settings with hard caps (rule 1; gate 1.3).

Consistency model (documented as ADVISORY, in contrast to the binding
gates): this module takes NO row locks — no FOR UPDATE, no FOR SHARE,
no advisory locks — and adds no lock-graph edges. The router reads all
slices from ONE REPEATABLE READ snapshot (tenant_snapshot_session, the
P13 blocker-h precedent), so the composite is internally consistent as
of one MVCC snapshot but may be stale the instant it is returned. The
binding figures stay where they are computed under the documented lock
chains: pledge capacity under the P9 guarantor deposit-account lock,
withdrawable funds under the P11 account lock, settlement quotes under
the P12 chain. No caching or memoization layer exists here by design
(a tenant-shared cache is a recurring cross-tenant-poisoning incident
class); every response is a fresh point-in-time read.

Month bucketing convention (failure mode FM4): transactions.occurred_at
is timestamptz written in UTC (application.ledger._post stamps
datetime.now(UTC); the P11 deposit-interest and P13.11 dividend jobs
pin period-end postings to the period's last day in UTC, e.g.
datetime.combine(fy.end, time.max, UTC)). The series therefore buckets
by UTC calendar month — date_trunc('month', occurred_at AT TIME ZONE
'UTC') — exactly matching the NPL-trend month cutoffs
(reports.npl_trend_month_ends works on as_of.astimezone(UTC)). A leg at
23:59:59.999999 on a month's last day and one at 00:00:00 on the 1st
land in different buckets; the FY-end pinned postings land inside the
FY-end month.

Ledger-drift posture (failure mode FM3/FM1): deposit and share-capital
totals aggregate the account balance columns — the documented P8/P11
source of truth, maintained under the account row lock in the SAME
transaction as their balanced ledger postings. The monthly series is
reconstructed from the transactions ledger itself (reversal rows carry
reversal_of_id and are subtracted). The reconciliation test proves
SUM(balance) equals the ledger reconstruction (member.deposits /
member.shares CR - DR) to the cent on seeded history, so a figure
pointed at a drifted column fails its oracle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import loans as loans_service
from genesis.application.guarantees import live_guarantee_params, live_pledged_total
from genesis.domain.ledger import TxnType
from genesis.domain.lending import ApplicationStage
from genesis.domain.rbac import Module
from genesis.settings import get_settings

#: Hard caps on the server-resolved bounds (gate 1.3): even a
#: misconfigured environment can never widen a scan past these.
MAX_SERIES_MONTHS = 24
MAX_GUARANTOR_ROWS = 100

# --- SQL (module-level so the P13.9 EXPLAIN capture asserts each plan) ---

#: Total member deposits: SUM over the balance column maintained by the
#: P8/P11 account services (see module docstring for the drift posture).
#: Served by the deposit_accounts (tenant_id, member_id) UNIQUE index.
DEPOSIT_TOTAL_SQL = (
    "SELECT COALESCE(SUM(balance), 0) FROM deposit_accounts WHERE tenant_id = CAST(:tid AS uuid)"
)

#: Total share capital — same posture, share_accounts.
SHARE_TOTAL_SQL = (
    "SELECT COALESCE(SUM(balance), 0) FROM share_accounts WHERE tenant_id = CAST(:tid AS uuid)"
)

#: Members by type with the active count riding the same aggregate.
#: Served by idx_members_type (0001).
MEMBERS_BY_TYPE_SQL = (
    "SELECT type, COUNT(*) AS total, "
    "COUNT(*) FILTER (WHERE status = 'active') AS active "
    "FROM members WHERE tenant_id = CAST(:tid AS uuid) "
    "GROUP BY type ORDER BY type"
)

#: Monthly deposits-vs-disbursements reconstructed from the transactions
#: ledger (UTC calendar months — see module docstring). Reversal rows
#: (reversal_of_id set, same type, mirrored legs) subtract in the month
#: they were POSTED, matching append-only ledger semantics. Window
#: bounds are server-resolved bound parameters. Served by
#: idx_transactions_time / idx_txns_occurred_keyset (0001/0008).
MONTHLY_FLOWS_SQL = (
    "SELECT to_char(date_trunc('month', occurred_at AT TIME ZONE 'UTC'), 'YYYY-MM') AS month, "
    "COALESCE(SUM(CASE WHEN type = :t_dep THEN "
    "  CASE WHEN reversal_of_id IS NULL THEN amount ELSE -amount END ELSE 0 END), 0), "
    "COALESCE(SUM(CASE WHEN type = :t_dis THEN "
    "  CASE WHEN reversal_of_id IS NULL THEN amount ELSE -amount END ELSE 0 END), 0) "
    "FROM transactions "
    "WHERE tenant_id = CAST(:tid AS uuid) "
    "AND type IN (:t_dep, :t_dis) "
    "AND occurred_at >= :w_start AND occurred_at < :w_end "
    "GROUP BY 1 ORDER BY 1"
)

#: Applications pipeline: one count per stage. Served by
#: idx_applications_stage (0001).
PIPELINE_SQL = (
    "SELECT stage, COUNT(*) FROM loan_applications "
    "WHERE tenant_id = CAST(:tid AS uuid) GROUP BY stage"
)

#: Live-guarantee totals over the SAME status set the enforcement path
#: uses (LIVE_GUARANTEE_STATUSES via live_guarantee_params — FM5).
#: Served by idx_guarantees_guarantor (0001).
GUARANTEE_TOTALS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(amount), 0), COUNT(DISTINCT guarantor_member_id) "
    "FROM guarantees "
    "WHERE tenant_id = CAST(:tid AS uuid) AND status IN (:live0, :live1)"
)

#: Candidate guarantors for the per-guarantor slice, biggest exposure
#: first, hard-capped. The grouped SUM only ORDERS the candidates; the
#: reported pledged figure comes from live_pledged_total (gate 1.1).
#: LEFT JOIN: a member without a deposit account reports balance 0.
TOP_GUARANTORS_SQL = (
    "SELECT g.guarantor_member_id, m.member_no, m.name, "
    "SUM(g.amount) AS pledged, COALESCE(MIN(d.balance), 0) AS balance "
    "FROM guarantees g "
    "JOIN members m ON m.id = g.guarantor_member_id AND m.tenant_id = g.tenant_id "
    "LEFT JOIN deposit_accounts d "
    "ON d.member_id = g.guarantor_member_id AND d.tenant_id = g.tenant_id "
    "WHERE g.tenant_id = CAST(:tid AS uuid) AND g.status IN (:live0, :live1) "
    "GROUP BY g.guarantor_member_id, m.member_no, m.name "
    "ORDER BY pledged DESC, g.guarantor_member_id LIMIT :cap"
)


# --- Result types ---


@dataclass(frozen=True)
class DepositTotals:
    total_deposits: Decimal
    total_share_capital: Decimal


@dataclass(frozen=True)
class MemberTypeCount:
    member_type: str
    total: int
    active: int


@dataclass(frozen=True)
class MembersOverview:
    active_members: int
    by_type: tuple[MemberTypeCount, ...]


@dataclass(frozen=True)
class MonthlyFlow:
    month: str  # "YYYY-MM", UTC calendar month
    deposits: Decimal
    disbursements: Decimal


@dataclass(frozen=True)
class PipelineStageCount:
    stage: ApplicationStage
    count: int


@dataclass(frozen=True)
class GuarantorCapacity:
    member_id: uuid.UUID
    member_no: str
    name: str
    #: Authoritative live pledge total — live_pledged_total (gate 1.1).
    pledged_total: Decimal
    #: Advisory: deposit balance minus pledged_total, read WITHOUT the
    #: P9 locks. The pledge endpoint recomputes under the lock chain.
    free_capacity: Decimal


@dataclass(frozen=True)
class GuarantorAggregates:
    active_guarantees: int
    total_pledged: Decimal
    distinct_guarantors: int
    guarantors: tuple[GuarantorCapacity, ...]


@dataclass(frozen=True)
class DashboardSummary:
    as_of: datetime
    deposits: DepositTotals | None
    monthly_flows: tuple[MonthlyFlow, ...] | None
    members: MembersOverview | None
    pipeline: tuple[PipelineStageCount, ...] | None
    guarantors: GuarantorAggregates | None
    loan_book: loans_service.PortfolioSummary | None


def dashboard_month_starts(as_of: datetime, months: int) -> list[date]:
    """First day of each of the last `months` UTC calendar months, oldest
    first — the NPL-trend month-walk (reports.npl_trend_month_ends)
    anchored on month STARTS because the flow series needs half-open
    [start, next-start) buckets rather than end-of-month cutoffs."""
    cursor = as_of.astimezone(UTC).date().replace(day=1)
    starts = [cursor]
    for _ in range(months - 1):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        starts.append(cursor)
    starts.reverse()
    return starts


async def _deposit_totals(session: AsyncSession, tenant_id: uuid.UUID) -> DepositTotals:
    params = {"tid": str(tenant_id)}
    deposits = (await session.execute(text(DEPOSIT_TOTAL_SQL), params)).scalar_one()
    shares = (await session.execute(text(SHARE_TOTAL_SQL), params)).scalar_one()
    return DepositTotals(
        total_deposits=Decimal(str(deposits)),
        total_share_capital=Decimal(str(shares)),
    )


async def _members_overview(session: AsyncSession, tenant_id: uuid.UUID) -> MembersOverview:
    rows = (await session.execute(text(MEMBERS_BY_TYPE_SQL), {"tid": str(tenant_id)})).all()
    by_type = tuple(
        MemberTypeCount(member_type=str(r[0]), total=int(r[1]), active=int(r[2])) for r in rows
    )
    return MembersOverview(
        active_members=sum(t.active for t in by_type),
        by_type=by_type,
    )


async def _monthly_flows(
    session: AsyncSession, tenant_id: uuid.UUID, as_of: datetime, months: int
) -> tuple[MonthlyFlow, ...]:
    starts = dashboard_month_starts(as_of, months)
    w_start = datetime(starts[0].year, starts[0].month, 1, tzinfo=UTC)
    rows = (
        await session.execute(
            text(MONTHLY_FLOWS_SQL),
            {
                "tid": str(tenant_id),
                "t_dep": TxnType.DEPOSIT.value,
                "t_dis": TxnType.LOAN_DISBURSEMENT.value,
                "w_start": w_start,
                "w_end": as_of,
            },
        )
    ).all()
    by_month = {str(r[0]): (Decimal(str(r[1])), Decimal(str(r[2]))) for r in rows}
    zero = Decimal("0.00")
    series: list[MonthlyFlow] = []
    # Loop over the code-owned month keys (bounded by config, never by
    # data) to zero-fill silent months — not a loop over result rows.
    for start in starts:
        key = f"{start.year:04d}-{start.month:02d}"
        deposits, disbursements = by_month.get(key, (zero, zero))
        series.append(MonthlyFlow(month=key, deposits=deposits, disbursements=disbursements))
    return tuple(series)


async def _pipeline(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[PipelineStageCount, ...]:
    rows = (await session.execute(text(PIPELINE_SQL), {"tid": str(tenant_id)})).all()
    counts = {str(r[0]): int(r[1]) for r in rows}
    return tuple(
        PipelineStageCount(stage=stage, count=counts.get(stage.value, 0))
        for stage in ApplicationStage
    )


async def _guarantor_aggregates(
    session: AsyncSession, tenant_id: uuid.UUID, cap: int
) -> GuarantorAggregates:
    params: dict[str, object] = {"tid": str(tenant_id), **live_guarantee_params()}
    totals = (await session.execute(text(GUARANTEE_TOTALS_SQL), params)).one()
    top = (await session.execute(text(TOP_GUARANTORS_SQL), {**params, "cap": cap})).all()
    guarantors: list[GuarantorCapacity] = []
    # Bounded by the config cap (never by table size). The pledged
    # figure for each listed guarantor is the SINGLE capacity
    # implementation the P9 pledge and P11 withdrawal paths call
    # (gate 1.1; FM5) — the grouped SUM above only picked candidates.
    for row in top:
        member_id = uuid.UUID(str(row[0]))
        pledged = await live_pledged_total(session, tenant_id, member_id)
        balance = Decimal(str(row[4]))
        guarantors.append(
            GuarantorCapacity(
                member_id=member_id,
                member_no=str(row[1]),
                name=str(row[2]),
                pledged_total=pledged,
                free_capacity=balance - pledged,
            )
        )
    return GuarantorAggregates(
        active_guarantees=int(totals[0]),
        total_pledged=Decimal(str(totals[1])),
        distinct_guarantors=int(totals[2]),
        guarantors=tuple(guarantors),
    )


async def dashboard_summary(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    granted: frozenset[Module],
    months: int | None = None,
    guarantor_cap: int | None = None,
    as_of: datetime | None = None,
) -> DashboardSummary:
    """Assemble the composite summary — per-permission, deny by default.

    A slice is computed ONLY when its module grant is present (gate
    1.6): ungranted slices are None (omitted from the response), never
    zeroed — a zeroed slice would still disclose its shape. Slice map:

      * transactions:view — deposit/share totals + monthly flow series
      * members:view      — active-member count + members-by-type
      * applications:view — pipeline counts + guarantor aggregates
        (guarantees are P9 applications-module data)
      * loan_book:view    — the P10 portfolio summary (reused verbatim)

    months / guarantor_cap default from server settings and are clamped
    to hard caps; the router passes neither, so no caller input reaches
    them (v1.1 rule 1).
    """
    settings = get_settings()
    months = min(max(months or settings.dashboard_series_months, 1), MAX_SERIES_MONTHS)
    cap = min(max(guarantor_cap or settings.dashboard_guarantor_cap, 1), MAX_GUARANTOR_ROWS)
    now = as_of or datetime.now(UTC)
    deposits: DepositTotals | None = None
    monthly: tuple[MonthlyFlow, ...] | None = None
    members: MembersOverview | None = None
    pipeline: tuple[PipelineStageCount, ...] | None = None
    guarantors: GuarantorAggregates | None = None
    loan_book: loans_service.PortfolioSummary | None = None
    if Module.TRANSACTIONS in granted:
        deposits = await _deposit_totals(session, tenant_id)
        monthly = await _monthly_flows(session, tenant_id, now, months)
    if Module.MEMBERS in granted:
        members = await _members_overview(session, tenant_id)
    if Module.APPLICATIONS in granted:
        pipeline = await _pipeline(session, tenant_id)
        guarantors = await _guarantor_aggregates(session, tenant_id, cap)
    if Module.LOAN_BOOK in granted:
        loan_book = await loans_service.portfolio_summary(session, tenant_id)
    return DashboardSummary(
        as_of=now,
        deposits=deposits,
        monthly_flows=monthly,
        members=members,
        pipeline=pipeline,
        guarantors=guarantors,
        loan_book=loan_book,
    )
