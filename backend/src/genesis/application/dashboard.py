"""Dashboard aggregates: the remaining prototype dashboard figures.

GET /dashboard/summary serves: total deposits + total share capital,
active-member count and members-by-type, the monthly deposits-vs-
disbursements series, applications-pipeline counts per stage, guarantor
aggregates (live guarantees, total pledged, per-guarantor free capacity via live_pledged_total —
reuse-first, no second capacity implementation),
and the loan-book slice via loans_service.portfolio_summary (reuse,
never a fork).

Every figure is a SQL aggregate — never a Python loop over result rows
(the only loops here run over the code-owned month keys and the
config-capped guarantor list). Every read carries an explicit bound
tenant_id predicate on top of forced RLS, all
values travel as bound parameters (rule 6), and the window/list bounds
are server-resolved from settings with hard caps (rule 1; scalability).

Consistency model (documented as ADVISORY, in contrast to the binding
gates): this module takes NO row locks — no FOR UPDATE, no FOR SHARE,
no advisory locks — and adds no lock-graph edges. The router reads all
slices from ONE REPEATABLE READ snapshot (tenant_snapshot_session, the blocker-h precedent), so the
composite is internally consistent as
of one MVCC snapshot but may be stale the instant it is returned. The
binding figures stay where they are computed under the documented lock
chains: pledge capacity under the P9 guarantor deposit-account lock,
withdrawable funds under the account lock, settlement quotes under
the chain. No caching or memoization layer exists here by design
(a tenant-shared cache is a recurring cross-tenant-poisoning incident
class); every response is a fresh point-in-time read.

Month bucketing convention (failure mode): transactions.occurred_at
is timestamptz written in UTC (application.ledger._post stamps datetime.now(UTC); the
deposit-interest and dividend jobs pin period-end postings to the period's last day in UTC, e.g.
datetime.combine(fy.end, time.max, UTC)). The series therefore buckets
by UTC calendar month — date_trunc('month', occurred_at AT TIME ZONE
'UTC') — exactly matching the NPL-trend month cutoffs
(portfolio_reconstruction.npl_trend_month_ends works on
as_of.astimezone(UTC)). A leg at
23:59:59.999999 on a month's last day and one at 00:00:00 on the 1st
land in different buckets; the FY-end pinned postings land inside the
FY-end month.

Ledger-drift posture (failure mode /): deposit and share-capital
totals aggregate the account balance columns — the documented P8/
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
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import loans as loans_service
from genesis.application.guarantees import live_guarantee_params, live_pledged_total
from genesis.application.portfolio_reconstruction import (
    npl_trend_month_ends,
    reconstruct_month,
)
from genesis.domain.ledger import TxnType
from genesis.domain.lending import PAR_DPD_DAYS, ApplicationStage
from genesis.domain.money import to_cents
from genesis.domain.rbac import Module
from genesis.settings import get_settings

#: Hard caps on the server-resolved bounds (scalability): even a
#: misconfigured environment can never widen a scan past these.
MAX_SERIES_MONTHS = 24
MAX_GUARANTOR_ROWS = 100
#: Per-KPI trend window: the same
#: server-resolved dashboard_series_months drives it, additionally
#: hard-capped here — the route accepts NO parameters, so nothing is
#: caller-tunable beyond this documented bound.
KPI_TREND_MAX_MONTHS = 12

# --- SQL (module-level so the EXPLAIN capture asserts each plan) ---

#: Total member deposits: SUM over the balance column maintained by the
#: P8/ account services (see module docstring for the drift posture).
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
#: uses (LIVE_GUARANTEE_STATUSES via live_guarantee_params).
#: Served by idx_guarantees_guarantor (0001).
GUARANTEE_TOTALS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(amount), 0), COUNT(DISTINCT guarantor_member_id) "
    "FROM guarantees "
    "WHERE tenant_id = CAST(:tid AS uuid) AND status IN (:live0, :live1)"
)

#: Candidate guarantors for the per-guarantor slice, biggest exposure
#: first, hard-capped. The grouped SUM only ORDERS the candidates; the
#: reported pledged figure comes from live_pledged_total (reuse-first).
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

#: Active-member count AS OF a past cutoff, reconstructed from
#: STATUS-TRANSITION FACTS — never from the
#: mutable members.status column alone, which only knows the present:
#: a member counts as active at:d_next when they existed
#: (created_at <:d_next — members are created ACTIVE, the P8
#: invariant application/members.create_member hardcodes) and their
#: LATEST immutable 'member.status' audit fact before the cutoff (the
#: single action every status writer records: manual transitions,
#: the exit settlement, the dormancy job and the deposit
#: reactivation) lands them on 'active' — or no transition fact
#: exists yet. audit_log is append-only (0001 trigger), so these
#: facts cannot be rewritten (the snapshot-basis exploit class
#: does not apply to a fact log). The correlated probe is served by
#: idx_audit_entity (tenant_id, entity, entity_id — 0001); the
#: driving member scan by the tenant-leading members indexes (0001).
#: Every value is a bound parameter; explicit tenant predicates on
#: BOTH relations double the RLS fence (least disclosure).
MEMBERS_ACTIVE_AT_SQL = (
    "SELECT COUNT(*) FROM members m "
    "WHERE m.tenant_id = CAST(:tid AS uuid) "
    "AND m.created_at < :d_next "
    "AND COALESCE(("
    "SELECT a.after->>'status' FROM audit_log a "
    "WHERE a.tenant_id = CAST(:tid AS uuid) "
    "AND a.entity = 'members' "
    "AND a.entity_id = CAST(m.id AS text) "
    "AND a.action = 'member.status' "
    "AND a.at < :d_next "
    "ORDER BY a.at DESC, a.id DESC LIMIT 1"
    "), 'active') = 'active'"
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
    #: Authoritative live pledge total — live_pledged_total (reuse-first).
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


# --- Per-KPI trend series -----------------------
#
# The contract follow-up recorded by the human review of /: the
# PAR-30 and members KPI cards shipped WITHOUT sparklines because the
# contract carried trend series only for the monthly deposit /
# disbursement flows. This slice serves bounded per-KPI monthly series,
# SERVER-computed end-to-end (the route-(a) precedent):
#
#   * PAR-30 points are RECOMPUTED from posting-history truth per
#     month-end cutoff — reconstruct_month (the single reconstruction
#     statement, reuse-first) at the code-owned PAR_DPD_DAYS threshold
#     from domain/lending classify(). NEVER from the mutable
#     loans.days_past_due/classification snapshot columns at past
#     cutoffs, and NEVER from portfolio_month_snapshots (
#     snapshot-basis exploit class; the stored snapshots also carry no
#     PAR-30 figure). The ratio is computed HERE in Decimal
#     (to_cents(par_balance * 100 / gross) — the same formula the
#     portfolio_summary and the NPL-trend export apply); the client
#     renders it verbatim.
#   * Members points derive from STATUS-TRANSITION FACTS
#     (MEMBERS_ACTIVE_AT_SQL above) — integers, no money.
#   * pct is share-of-window-peak geometry via scale_pct (integers
#     0..100, deliberately too coarse to reconstruct figures).
#
# Cost posture (documented honestly): one bounded statement per point
# (<= KPI_TREND_MAX_MONTHS per KPI, code-owned month walk — never a
# loop over result rows), the same per-cutoff pattern the NPL-trend
# export has always run, inside the same REPEATABLE READ snapshot as
# every sibling slice. Advisory read path; no locks (the documented
#  posture).


@dataclass(frozen=True)
class Par30TrendPoint:
    month: str  # "YYYY-MM" — same UTC month key as the flow series
    #: PAR-30 share of the gross outstanding book at the cutoff,
    #: Decimal cents (serialized verbatim as a string by the API).
    ratio_pct: Decimal
    #: Share-of-window-peak geometry, 0..100 (scale_pct).
    pct: int


@dataclass(frozen=True)
class MembersTrendPoint:
    month: str
    active: int
    pct: int


@dataclass(frozen=True)
class KpiTrends:
    """Per-KPI series; each follows its KPI card's parent grant
    (par30 <- loan_book:view, members <- members:view) and is None
    (omitted) when ungranted — deny by default, least disclosure."""

    par30: tuple[Par30TrendPoint, ...] | None
    members: tuple[MembersTrendPoint, ...] | None


async def _par30_trend(
    session: AsyncSession, tenant_id: uuid.UUID, as_of: datetime, months: int
) -> tuple[Par30TrendPoint, ...]:
    raw: list[tuple[str, Decimal]] = []
    # Code-owned month walk (bounded by config <= KPI_TREND_MAX_MONTHS,
    # never by data); the current month cuts at the as-of instant
    # exactly like the NPL-trend export.
    for month_end in npl_trend_month_ends(as_of, months):
        month = await reconstruct_month(
            session, tenant_id, month_end, as_of=as_of, dpd_days=PAR_DPD_DAYS
        )
        gross = month.gross_outstanding
        ratio = to_cents(month.npl_balance * _ONE_HUNDRED / gross) if gross > _ZERO else _ZERO_CENTS
        raw.append((f"{month_end.year:04d}-{month_end.month:02d}", ratio))
    ceiling = max([_ZERO, *(ratio for _, ratio in raw)])
    return tuple(
        Par30TrendPoint(month=key, ratio_pct=ratio, pct=scale_pct(ratio, ceiling))
        for key, ratio in raw
    )


async def _members_trend(
    session: AsyncSession, tenant_id: uuid.UUID, as_of: datetime, months: int
) -> tuple[MembersTrendPoint, ...]:
    raw: list[tuple[str, int]] = []
    for month_end in npl_trend_month_ends(as_of, months):
        cutoff = datetime(month_end.year, month_end.month, month_end.day, tzinfo=UTC)
        # The cutoff instant is the UTC end of the month-end day,
        # additionally capped at the as-of instant for the current,
        # incomplete month — the reconstruct_month convention.
        d_next = min(cutoff + timedelta(days=1), as_of + timedelta(microseconds=1))
        count = (
            await session.execute(
                text(MEMBERS_ACTIVE_AT_SQL),
                {"tid": str(tenant_id), "d_next": d_next},
            )
        ).scalar_one()
        raw.append((f"{month_end.year:04d}-{month_end.month:02d}", int(count)))
    ceiling = Decimal(max([0, *(active for _, active in raw)]))
    return tuple(
        MembersTrendPoint(month=key, active=active, pct=scale_pct(Decimal(active), ceiling))
        for key, active in raw
    )


# --- Chart-series read model -------------
#
# The RECORDED design decision, route (a): chart geometry is a
# SERVER-computed read model. Every percentage below is derived HERE,
# in Decimal arithmetic, from figures this module (or the reused
# portfolio summary) already computed inside the same REPEATABLE READ
# snapshot — the client renders the integers as visual scale ONLY and
# never performs money math (the money rule stays absolute in web/).
#
# Honesty rules:
#   * The sibling slices (monthly_flows, loan_book) remain the figures
#     of record; charts are supplements (a11y: colour-never-alone, the
#     tables stay the accessible truth).
#   * A month that nets NEGATIVE (reversal-only, MONTHLY_FLOWS_SQL)
#     clamps to 0 in the chart geometry; the SIGNED truth still
#     travels verbatim in monthly_flows.
#   * No new SQL: these are pure functions over already-fetched slice
#     data — no new hot-path query ships (no EXPLAIN owed).
#   * Percentages are integers 0..100, ROUND_HALF_UP — a resolution
#     chosen for geometry, deliberately too coarse to reconstruct any
#     money figure.

_ZERO = Decimal("0")
_ZERO_CENTS = Decimal("0.00")
_ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class FlowBarScale:
    """One month's grouped-bar heights, share-of-window-peak (0..100)."""

    month: str  # "YYYY-MM" — same key as the sibling MonthlyFlow row
    deposits_pct: int
    disbursements_pct: int


@dataclass(frozen=True)
class FlowsChart:
    """Deposits-vs-disbursements bar geometry + KPI sparkline series."""

    months: tuple[FlowBarScale, ...]
    #: The COMMON scale ceiling (max of both series across the window,
    #: never below zero) — serialized verbatim so the chart can caption
    #: its scale with the server's own figure.
    axis_max: Decimal


@dataclass(frozen=True)
class ClassificationShare:
    classification: str
    pct: int  # share of outstanding_balance, 0..100


@dataclass(frozen=True)
class PortfolioChart:
    """Performing donut + classification progress-bar geometry."""

    performing_pct: int
    npl_pct: int
    classification: tuple[ClassificationShare, ...]


@dataclass(frozen=True)
class DashboardCharts:
    """Per-slice chart geometry; each sub-slice follows its parent's
    permission grant (flows <- transactions:view, portfolio <-
    loan_book:view) and is None (omitted) when ungranted."""

    flows: FlowsChart | None
    portfolio: PortfolioChart | None


def scale_pct(value: Decimal, ceiling: Decimal) -> int:
    """Share-of-ceiling as an integer percent, clamped to 0..100.

    Negative values (reversal-only months) and non-positive ceilings
    scale to 0; a value at (or, defensively, above) the ceiling is 100.
    ROUND_HALF_UP on the integer percent — Decimal end-to-end, no
    float ever touches a money-derived quantity.
    """
    if ceiling <= _ZERO or value <= _ZERO:
        return 0
    if value >= ceiling:
        return 100
    pct = (value * _ONE_HUNDRED / ceiling).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return min(int(pct), 100)


def flows_chart(monthly: tuple[MonthlyFlow, ...]) -> FlowsChart:
    """Bar/sparkline geometry for the monthly flow series.

    One COMMON ceiling across both series (the prototype's grouped-bar
    convention) so deposit and disbursement bars are comparable within
    a month; the ceiling itself is exposed as axis_max so the client
    can caption the scale with a verbatim server figure.
    """
    ceiling = max([_ZERO, *(f.deposits for f in monthly), *(f.disbursements for f in monthly)])
    months = tuple(
        FlowBarScale(
            month=f.month,
            deposits_pct=scale_pct(f.deposits, ceiling),
            disbursements_pct=scale_pct(f.disbursements, ceiling),
        )
        for f in monthly
    )
    return FlowsChart(months=months, axis_max=ceiling)


def portfolio_chart(loan_book: loans_service.PortfolioSummary) -> PortfolioChart:
    """Donut + classification-bar geometry over the summary.

    npl_pct is the NPL share of the outstanding book; performing_pct is
    its exact complement (they always sum to 100 on a non-empty book).
    An empty book (outstanding <= 0) is 0/0 — the client renders the
    honest empty state, never a fabricated full donut.
    """
    outstanding = loan_book.outstanding_balance
    if outstanding <= _ZERO:
        performing, npl = 0, 0
    else:
        npl = scale_pct(loan_book.npl_balance, outstanding)
        performing = 100 - npl
    shares = tuple(
        ClassificationShare(
            classification=s.classification.value,
            pct=scale_pct(s.balance, outstanding),
        )
        for s in loan_book.by_classification
    )
    return PortfolioChart(performing_pct=performing, npl_pct=npl, classification=shares)


def dashboard_charts(
    monthly: tuple[MonthlyFlow, ...] | None,
    loan_book: loans_service.PortfolioSummary | None,
) -> DashboardCharts | None:
    """Assemble the charts slice from the granted parent slices; None
    when neither parent is granted (the slice is then omitted from the response entirely —
    deny-by-default, least disclosure)."""
    flows = flows_chart(monthly) if monthly is not None else None
    portfolio = portfolio_chart(loan_book) if loan_book is not None else None
    if flows is None and portfolio is None:
        return None
    return DashboardCharts(flows=flows, portfolio=portfolio)


@dataclass(frozen=True)
class DashboardSummary:
    as_of: datetime
    deposits: DepositTotals | None
    monthly_flows: tuple[MonthlyFlow, ...] | None
    members: MembersOverview | None
    pipeline: tuple[PipelineStageCount, ...] | None
    guarantors: GuarantorAggregates | None
    loan_book: loans_service.PortfolioSummary | None
    #: Chart geometry derived from monthly_flows / loan_book (issue
    #: ); None when neither parent slice is granted.
    charts: DashboardCharts | None
    #: Per-KPI trend series; None when
    #: neither parent grant is present.
    kpi_trends: KpiTrends | None


def dashboard_month_starts(as_of: datetime, months: int) -> list[date]:
    """First day of each of the last `months` UTC calendar months, oldest
    first — the NPL-trend month-walk
    (portfolio_reconstruction.npl_trend_month_ends) anchored on month
    STARTS because the flow series needs half-open
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
    # implementation the P9 pledge and withdrawal paths call
    # (reuse-first) — the grouped SUM above only picked candidates.
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
      * members:view — active-member count + members-by-type
      * applications:view — pipeline counts + guarantor aggregates
        (guarantees are P9 applications-module data)
      * loan_book:view — the portfolio summary (reused verbatim)

    months / guarantor_cap default from server settings and are clamped
    to hard caps; the router passes neither, so no caller input reaches
    them.
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
    # Per-KPI trend window: the same
    # server-resolved series setting, additionally hard-capped at
    # KPI_TREND_MAX_MONTHS — nothing caller-tunable (the route passes
    # no widths).
    kpi_months = min(months, KPI_TREND_MAX_MONTHS)
    par30_trend: tuple[Par30TrendPoint, ...] | None = None
    members_trend: tuple[MembersTrendPoint, ...] | None = None
    if Module.MEMBERS in granted:
        members = await _members_overview(session, tenant_id)
        members_trend = await _members_trend(session, tenant_id, now, kpi_months)
    if Module.APPLICATIONS in granted:
        pipeline = await _pipeline(session, tenant_id)
        guarantors = await _guarantor_aggregates(session, tenant_id, cap)
    if Module.LOAN_BOOK in granted:
        loan_book = await loans_service.portfolio_summary(session, tenant_id)
        par30_trend = await _par30_trend(session, tenant_id, now, kpi_months)
    return DashboardSummary(
        as_of=now,
        deposits=deposits,
        monthly_flows=monthly,
        members=members,
        pipeline=pipeline,
        guarantors=guarantors,
        loan_book=loan_book,
        # Pure derivation from the two granted parents above — same
        # snapshot, no extra SQL (route (a)).
        charts=dashboard_charts(monthly, loan_book),
        kpi_trends=(
            KpiTrends(par30=par30_trend, members=members_trend)
            if par30_trend is not None or members_trend is not None
            else None
        ),
    )
