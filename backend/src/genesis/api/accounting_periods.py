"""Accounting period endpoints (issue #12, P12.5, gate 1.6).

Every route carries a RequirePermission dependency (deny-by-default);
the close mutation is idempotent via the Idempotency-Key middleware
(gate 1.4).

Permission gates (P4 matrix, decided and documented):

  * close period — transactions x APPROVE: closing the books freezes
    ledger history for a month, a control over every posting, so it
    sits with the approve holders of the transactions module (System
    Admin, Branch Manager) — deliberately NOT transactions:create/edit,
    which Tellers and Accountants hold for day-to-day posting: the
    people who post must not be able to freeze (or race) the periods
    they post into.
  * list periods — transactions x VIEW.

The close body carries only year/month — the period bounds, the
"fully elapsed" rule and every posting-date check are resolved
server-side (issue #12: never caller-backdatable). extra="forbid"
rejects anything else with a 422.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import accounting_periods as periods_service
from genesis.application import period_rollups as rollups_service
from genesis.application import portfolio_snapshots as snapshots_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/accounting-periods", tags=["accounting-periods"])

#: One-off operations jobs (the POST /jobs/arrears convention): no
#: /accounting-periods prefix so the paths sit beside the other job
#: routes.
jobs_router = APIRouter(tags=["accounting-periods"])

_view = RequirePermission(Module.TRANSACTIONS, Action.VIEW)
_approve = RequirePermission(Module.TRANSACTIONS, Action.APPROVE)

ViewCtx = Annotated[AuthContext, Depends(_view)]
ApproveCtx = Annotated[AuthContext, Depends(_approve)]


class ClosePeriodBody(BaseModel):
    """Only the calendar month is caller-chosen; everything else is server-side."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class PeriodOut(BaseModel):
    id: str
    period_start: str
    period_end: str
    status: str
    closed_at: str | None
    version: int


class PeriodListResponse(BaseModel):
    items: list[PeriodOut]
    next_cursor: str | None


def _out(record: periods_service.PeriodRecord) -> PeriodOut:
    return PeriodOut(
        id=str(record.id),
        period_start=record.period_start.isoformat(),
        period_end=record.period_end.isoformat(),
        status=record.status,
        closed_at=record.closed_at.isoformat() if record.closed_at else None,
        version=record.version,
    )


@router.post("/close", status_code=201)
async def close_period(body: ClosePeriodBody, ctx: ApproveCtx) -> PeriodOut:
    """Close a fully elapsed month (transactions:approve)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await periods_service.close_period(
            session, ctx.tenant_id, ctx.user_id, year=body.year, month=body.month
        )
    return _out(record)


@router.get("")
async def list_periods(
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PeriodListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await periods_service.list_periods(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return PeriodListResponse(items=[_out(r) for r in items], next_cursor=next_cursor)


class SnapshotBackfillBody(BaseModel):
    """Deliberately empty (P13.17a): the month worklist, cutoffs and
    batching are ALL server-resolved (v1.1 rule 1 + the insider rule —
    no caller-supplied dates or period identifiers anywhere);
    extra="forbid" makes any smuggled field a 422."""

    model_config = ConfigDict(extra="forbid")


class SnapshotBackfillOut(BaseModel):
    months_considered: int
    written: int
    batches: int


@jobs_router.post("/jobs/portfolio-snapshots")
async def run_portfolio_snapshot_backfill(
    body: SnapshotBackfillBody, ctx: ApproveCtx
) -> SnapshotBackfillOut:
    """Backfill month-end portfolio snapshots (P13.17a / DSA-1).

    Permission (P4 matrix, decided): transactions x APPROVE — the
    close-period authority. Snapshots freeze the month-end figures the
    NPL-trend export serves to auditors, exactly the control
    close_period exercises, so the same approve holders (System Admin,
    Branch Manager) own it — never the posting roles.

    Batched through the shared batch runner (one month per short
    transaction); a completed re-run is a lock-free no-op (anti-join
    on the claim key, v1.1 rule 8) proven by side-effect counts.
    """
    factory = get_sessionmaker(get_settings().database_url)
    result = await snapshots_service.run_snapshot_backfill_for_tenant(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        ctx.user_id,
    )
    return SnapshotBackfillOut(
        months_considered=result.months_considered,
        written=result.written,
        batches=result.batches,
    )


class RollupBackfillBody(BaseModel):
    """Deliberately empty (P13.17b): the worklist is the server's own
    closed-but-unrolled period set — no caller-supplied period
    identifiers anywhere (v1.1 rule 1 + the insider rule);
    extra="forbid" makes any smuggled field a 422."""

    model_config = ConfigDict(extra="forbid")


class RollupBackfillOut(BaseModel):
    periods_rolled: int
    accounts_written: int
    members_written: int
    batches: int


@jobs_router.post("/jobs/period-rollups")
async def run_period_rollup_backfill(
    body: RollupBackfillBody, ctx: ApproveCtx
) -> RollupBackfillOut:
    """Backfill rollups for periods closed before 0028 (P13.17b / DSA-2/5).

    Permission (P4 matrix, decided): transactions x APPROVE — the
    close-period authority; rollups ARE the closed-period figures the
    trial balance serves, so the posting roles must not own them.

    One period per short transaction, claimed FOR UPDATE SKIP LOCKED
    through the shared batch runner; a completed re-run scans zero
    rows and writes nothing (lock-free no-op by side-effect counts).
    """
    factory = get_sessionmaker(get_settings().database_url)
    result = await rollups_service.run_rollup_backfill_for_tenant(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        ctx.user_id,
    )
    return RollupBackfillOut(
        periods_rolled=result.periods_rolled,
        accounts_written=result.accounts_written,
        members_written=result.members_written,
        batches=result.batches,
    )
