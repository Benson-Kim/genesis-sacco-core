"""Dashboard summary endpoint.

One composite GET assembled per-permission (least disclosure, deny by default): the route is
reachable with ANY of the four slice grants
(RequireAnyPermission — still a RequirePermission dependency for the
P4 spec-walk test), and each slice is then included ONLY when the
caller's role holds that module's view grant. Ungranted slices are
ABSENT from the response (response_model_exclude_none), never zeroed.

The endpoint accepts NO parameters: the series window and guarantor
list size are server-resolved from settings with hard caps (scalability). All slices read from one
REPEATABLE READ snapshot
(tenant_snapshot_session — the blocker-h precedent) so the
composite can never interleave with a concurrent settlement; the reads
take no row locks and are documented as advisory versus the binding
lock-guarded gates (see application.dashboard).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from genesis.api.authz import RequireAnyPermission
from genesis.api.loan_book import PortfolioSummaryOut, portfolio_summary_out
from genesis.application import dashboard as dashboard_service
from genesis.application import rbac as rbac_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_snapshot_session
from genesis.settings import get_settings

router = APIRouter(tags=["dashboard"])

#: The four modules whose view grants map to dashboard slices. Order is
#: code-owned; the composite is reachable with any one of them.
_SLICE_MODULES: tuple[Module, ...] = (
    Module.TRANSACTIONS,
    Module.MEMBERS,
    Module.APPLICATIONS,
    Module.LOAN_BOOK,
)

_any_slice = RequireAnyPermission(*[(module, Action.VIEW) for module in _SLICE_MODULES])

AnySliceCtx = Annotated[AuthContext, Depends(_any_slice)]


class DepositTotalsOut(BaseModel):
    total_deposits: str
    total_share_capital: str


class MemberTypeCountOut(BaseModel):
    type: str
    total: int
    active: int


class MembersOverviewOut(BaseModel):
    active_members: int
    by_type: list[MemberTypeCountOut]


class MonthlyFlowOut(BaseModel):
    month: str
    deposits: str
    disbursements: str


class PipelineStageOut(BaseModel):
    stage: str
    count: int


class GuarantorCapacityOut(BaseModel):
    member_id: str
    member_no: str
    name: str
    pledged_total: str
    free_capacity: str


class GuarantorAggregatesOut(BaseModel):
    active_guarantees: int
    total_pledged: str
    distinct_guarantors: int
    guarantors: list[GuarantorCapacityOut]


class FlowBarScaleOut(BaseModel):
    """One month's grouped-bar heights (route (a)): integer
    percentages 0..100 of the window's axis_max, computed server-side
    in Decimal arithmetic — the client renders scale, never math."""

    month: str
    deposits_pct: int
    disbursements_pct: int


class FlowsChartOut(BaseModel):
    months: list[FlowBarScaleOut]
    #: The common scale ceiling, serialized verbatim (str(Decimal)) so
    #: the chart captions its scale with the server's own figure.
    axis_max: str


class ClassificationShareOut(BaseModel):
    classification: str
    pct: int


class PortfolioChartOut(BaseModel):
    performing_pct: int
    npl_pct: int
    classification: list[ClassificationShareOut]


class DashboardChartsOut(BaseModel):
    """Server-computed chart geometry; each sub-slice

    follows its parent slice's grant and is omitted when ungranted."""

    flows: FlowsChartOut | None = None
    portfolio: PortfolioChartOut | None = None


class Par30TrendPointOut(BaseModel):
    """One PAR-30 trend point: the ratio is
    RECOMPUTED from posting-history truth at the month-end cutoff
    (reconstruct_month at the code-owned PAR threshold — never the mutable snapshot columns) and
    serialized verbatim
    (str(Decimal)); pct is share-of-window-peak geometry ONLY."""

    month: str
    ratio_pct: str
    pct: int


class MembersTrendPointOut(BaseModel):
    """One active-members trend point: the count is reconstructed from
    the immutable member.status transition facts (audit_log) at the
    cutoff; pct is share-of-window-peak geometry ONLY."""

    month: str
    active: int
    pct: int


class KpiTrendsOut(BaseModel):
    """Per-KPI trend series; each series
    follows its KPI card's parent grant (par30 <- loan_book:view,
    members <- members:view) and is omitted when ungranted."""

    par30: list[Par30TrendPointOut] | None = None
    members: list[MembersTrendPointOut] | None = None


class DashboardSummaryOut(BaseModel):
    """Composite response; ungranted slices are omitted entirely."""

    as_of: str
    deposits: DepositTotalsOut | None = None
    monthly_flows: list[MonthlyFlowOut] | None = None
    members: MembersOverviewOut | None = None
    pipeline: list[PipelineStageOut] | None = None
    guarantors: GuarantorAggregatesOut | None = None
    loan_book: PortfolioSummaryOut | None = None
    charts: DashboardChartsOut | None = None
    kpi_trends: KpiTrendsOut | None = None


def _to_out(summary: dashboard_service.DashboardSummary) -> DashboardSummaryOut:
    return DashboardSummaryOut(
        as_of=summary.as_of.isoformat(),
        deposits=(
            DepositTotalsOut(
                total_deposits=str(summary.deposits.total_deposits),
                total_share_capital=str(summary.deposits.total_share_capital),
            )
            if summary.deposits is not None
            else None
        ),
        monthly_flows=(
            [
                MonthlyFlowOut(
                    month=f.month,
                    deposits=str(f.deposits),
                    disbursements=str(f.disbursements),
                )
                for f in summary.monthly_flows
            ]
            if summary.monthly_flows is not None
            else None
        ),
        members=(
            MembersOverviewOut(
                active_members=summary.members.active_members,
                by_type=[
                    MemberTypeCountOut(type=t.member_type, total=t.total, active=t.active)
                    for t in summary.members.by_type
                ],
            )
            if summary.members is not None
            else None
        ),
        pipeline=(
            [PipelineStageOut(stage=p.stage.value, count=p.count) for p in summary.pipeline]
            if summary.pipeline is not None
            else None
        ),
        guarantors=(
            GuarantorAggregatesOut(
                active_guarantees=summary.guarantors.active_guarantees,
                total_pledged=str(summary.guarantors.total_pledged),
                distinct_guarantors=summary.guarantors.distinct_guarantors,
                guarantors=[
                    GuarantorCapacityOut(
                        member_id=str(g.member_id),
                        member_no=g.member_no,
                        name=g.name,
                        pledged_total=str(g.pledged_total),
                        free_capacity=str(g.free_capacity),
                    )
                    for g in summary.guarantors.guarantors
                ],
            )
            if summary.guarantors is not None
            else None
        ),
        loan_book=(
            portfolio_summary_out(summary.loan_book) if summary.loan_book is not None else None
        ),
        charts=_charts_out(summary.charts) if summary.charts is not None else None,
        kpi_trends=(
            _kpi_trends_out(summary.kpi_trends) if summary.kpi_trends is not None else None
        ),
    )


def _kpi_trends_out(trends: dashboard_service.KpiTrends) -> KpiTrendsOut:
    return KpiTrendsOut(
        par30=(
            [
                Par30TrendPointOut(month=p.month, ratio_pct=str(p.ratio_pct), pct=p.pct)
                for p in trends.par30
            ]
            if trends.par30 is not None
            else None
        ),
        members=(
            [
                MembersTrendPointOut(month=p.month, active=p.active, pct=p.pct)
                for p in trends.members
            ]
            if trends.members is not None
            else None
        ),
    )


def _charts_out(charts: dashboard_service.DashboardCharts) -> DashboardChartsOut:
    return DashboardChartsOut(
        flows=(
            FlowsChartOut(
                months=[
                    FlowBarScaleOut(
                        month=m.month,
                        deposits_pct=m.deposits_pct,
                        disbursements_pct=m.disbursements_pct,
                    )
                    for m in charts.flows.months
                ],
                axis_max=str(charts.flows.axis_max),
            )
            if charts.flows is not None
            else None
        ),
        portfolio=(
            PortfolioChartOut(
                performing_pct=charts.portfolio.performing_pct,
                npl_pct=charts.portfolio.npl_pct,
                classification=[
                    ClassificationShareOut(classification=s.classification, pct=s.pct)
                    for s in charts.portfolio.classification
                ],
            )
            if charts.portfolio is not None
            else None
        ),
    )


@router.get("/dashboard/summary", response_model_exclude_none=True)
async def dashboard_summary(ctx: AnySliceCtx) -> DashboardSummaryOut:
    """The dashboard figures, sliced per the caller's permission matrix."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_snapshot_session(factory, ctx.tenant_id) as session:
        granted: set[Module] = set()
        for module in _SLICE_MODULES:
            if await rbac_service.has_permission(session, ctx.role_id, module, Action.VIEW):
                granted.add(module)
        summary = await dashboard_service.dashboard_summary(
            session, ctx.tenant_id, granted=frozenset(granted)
        )
    return _to_out(summary)
