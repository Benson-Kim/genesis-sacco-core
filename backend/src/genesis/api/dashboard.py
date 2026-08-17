"""Dashboard summary endpoint (P13.9).

One composite GET assembled per-permission (gate 1.6, deny by
default): the route is reachable with ANY of the four slice grants
(RequireAnyPermission — still a RequirePermission dependency for the
P4 spec-walk test), and each slice is then included ONLY when the
caller's role holds that module's view grant. Ungranted slices are
ABSENT from the response (response_model_exclude_none), never zeroed.

The endpoint accepts NO parameters: the series window and guarantor
list size are server-resolved from settings with hard caps (v1.1
rule 1; gate 1.3). All slices read from one REPEATABLE READ snapshot
(tenant_snapshot_session — the P13 blocker-h precedent) so the
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


class DashboardSummaryOut(BaseModel):
    """Composite response; ungranted slices are omitted entirely."""

    as_of: str
    deposits: DepositTotalsOut | None = None
    monthly_flows: list[MonthlyFlowOut] | None = None
    members: MembersOverviewOut | None = None
    pipeline: list[PipelineStageOut] | None = None
    guarantors: GuarantorAggregatesOut | None = None
    loan_book: PortfolioSummaryOut | None = None


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
    )


@router.get("/dashboard/summary", response_model_exclude_none=True)
async def dashboard_summary(ctx: AnySliceCtx) -> DashboardSummaryOut:
    """The prototype dashboard figures, sliced per the caller's matrix."""
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
