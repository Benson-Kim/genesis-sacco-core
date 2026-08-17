"""Loan book endpoints: disbursement, repayments, portfolio, arrears (P10).

Every route carries a RequirePermission dependency (deny-by-default,
gate 1.6); mutations are idempotent via the Idempotency-Key middleware
(gate 1.4). Disbursement goes exclusively through the P7 atomic
contract; repayments through the documented P10 allocation service.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.api.params import require_cash_channel, resolve_as_of
from genesis.application import arrears as arrears_service
from genesis.application import loans as loans_service
from genesis.application.auth import AuthContext
from genesis.application.ledger import disburse_loan
from genesis.domain.ledger import Channel
from genesis.domain.lending import LoanClass, LoanStatus
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["loan-book"])

_book_view = RequirePermission(Module.LOAN_BOOK, Action.VIEW)
_book_create = RequirePermission(Module.LOAN_BOOK, Action.CREATE)
_book_edit = RequirePermission(Module.LOAN_BOOK, Action.EDIT)
_txn_create = RequirePermission(Module.TRANSACTIONS, Action.CREATE)

BookViewCtx = Annotated[AuthContext, Depends(_book_view)]
BookCreateCtx = Annotated[AuthContext, Depends(_book_create)]
BookEditCtx = Annotated[AuthContext, Depends(_book_edit)]
TxnCreateCtx = Annotated[AuthContext, Depends(_txn_create)]


class DisburseBody(BaseModel):
    """extra="forbid": a caller-sent disbursed_at/occurred_at (or any
    money field this contract does not own) is a 422, never silently
    ignored — the posting date is resolved server-side (issue #12)."""

    model_config = ConfigDict(extra="forbid")

    channel: Channel


class RepaymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: decimal_places=2 rejects sub-cent amounts at the contract instead
    #: of silently rounding them into the ledger.
    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=2)
    channel: Channel


class ArrearsRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date | None = None
    batch_size: int = Field(default=arrears_service.DEFAULT_BATCH_SIZE, ge=1, le=1000)


class LoanOut(BaseModel):
    id: str
    application_id: str
    member_id: str
    product_id: str
    principal: str
    balance: str
    rate_pct: str
    term_months: int
    status: str
    classification: str
    days_past_due: int
    provision_pct: str
    penalty_due: str
    disbursed_at: str | None
    closed_at: str | None
    version: int


class LoanListResponse(BaseModel):
    items: list[LoanOut]
    next_cursor: str | None


class ScheduleRowOut(BaseModel):
    installment_no: int
    due_date: str
    principal_due: str
    interest_due: str
    total_due: str
    paid_amount: str


class DisbursementOut(BaseModel):
    loan_id: str
    txn_id: str
    txn_ref: str
    schedule: list[ScheduleRowOut]


class RepaymentOut(BaseModel):
    txn_id: str
    txn_ref: str
    penalties: str
    interest: str
    principal: str
    balance_after: str
    penalty_due_after: str
    status: str


class SettlementQuoteOut(BaseModel):
    as_of: str
    principal_balance: str
    interest_due: str
    penalties_due: str
    total: str


class ClassificationSliceOut(BaseModel):
    classification: str
    count: int
    balance: str
    provisions: str


class PortfolioSummaryOut(BaseModel):
    active_loans: int
    outstanding_balance: str
    npl_balance: str
    npl_ratio_pct: str
    par30_balance: str
    par30_ratio_pct: str
    provisions: str
    penalties_due: str
    by_classification: list[ClassificationSliceOut]


class ArrearsRunOut(BaseModel):
    as_of: str
    scanned: int
    updated: int
    batches: int
    #: P13.8 — False means the tenant has not configured all three
    #: penalty keys and the run accrued nothing by design (fail closed).
    penalty_configured: bool
    penalties_accrued: int
    penalty_total: str
    #: P13.16 — recovery cases auto-closed by this run's close pass.
    cases_closed_cured: int
    cases_closed_written_off: int


def _loan_out(loan: loans_service.LoanRecord) -> LoanOut:
    return LoanOut(
        id=str(loan.id),
        application_id=str(loan.application_id),
        member_id=str(loan.member_id),
        product_id=str(loan.product_id),
        principal=str(loan.principal),
        balance=str(loan.balance),
        rate_pct=str(loan.rate_pct),
        term_months=loan.term_months,
        status=loan.status.value,
        classification=loan.classification.value,
        days_past_due=loan.days_past_due,
        provision_pct=str(loan.provision_pct),
        penalty_due=str(loan.penalty_due),
        disbursed_at=loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        closed_at=loan.closed_at.isoformat() if loan.closed_at else None,
        version=loan.version,
    )


@router.post("/applications/{application_id}/disburse", status_code=201)
async def disburse_application(
    application_id: uuid.UUID,
    body: DisburseBody,
    ctx: BookCreateCtx,
) -> DisbursementOut:
    """Disburse an approved application via the P7 atomic contract."""
    channel = require_cash_channel(body.channel)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await disburse_loan(session, ctx.tenant_id, application_id, channel, ctx.user_id)
        schedule = await loans_service.get_schedule(session, ctx.tenant_id, result.loan_id)
    return DisbursementOut(
        loan_id=str(result.loan_id),
        txn_id=str(result.txn_id),
        txn_ref=result.txn_ref,
        schedule=[_schedule_out(row) for row in schedule],
    )


def _schedule_out(row: loans_service.ScheduleRow) -> ScheduleRowOut:
    return ScheduleRowOut(
        installment_no=row.installment_no,
        due_date=row.due_date.isoformat(),
        principal_due=str(row.principal_due),
        interest_due=str(row.interest_due),
        total_due=str(row.total_due),
        paid_amount=str(row.paid_amount),
    )


@router.get("/loans")
async def list_loans(
    ctx: BookViewCtx,
    status: LoanStatus | None = None,
    classification: LoanClass | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> LoanListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await loans_service.list_loans(
            session,
            ctx.tenant_id,
            status=status,
            classification=classification,
            cursor=cursor,
            limit=limit,
        )
    return LoanListResponse(items=[_loan_out(x) for x in items], next_cursor=next_cursor)


@router.get("/loans/{loan_id}")
async def get_loan(loan_id: uuid.UUID, ctx: BookViewCtx) -> LoanOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        loan = await loans_service.get_loan(session, ctx.tenant_id, loan_id)
    return _loan_out(loan)


@router.get("/loans/{loan_id}/schedule")
async def get_loan_schedule(loan_id: uuid.UUID, ctx: BookViewCtx) -> list[ScheduleRowOut]:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        schedule = await loans_service.get_schedule(session, ctx.tenant_id, loan_id)
    return [_schedule_out(row) for row in schedule]


@router.get("/loans/{loan_id}/settlement-quote")
async def get_settlement_quote(
    loan_id: uuid.UUID,
    ctx: BookViewCtx,
    as_of: date | None = None,
) -> SettlementQuoteOut:
    quote_date = resolve_as_of(as_of)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        quote = await loans_service.get_settlement_quote(
            session, ctx.tenant_id, loan_id, as_of=quote_date
        )
    return SettlementQuoteOut(
        as_of=quote_date.isoformat(),
        principal_balance=str(quote.principal_balance),
        interest_due=str(quote.interest_due),
        penalties_due=str(quote.penalties_due),
        total=str(quote.total),
    )


@router.post("/loans/{loan_id}/repayments", status_code=201)
async def post_repayment(
    loan_id: uuid.UUID,
    body: RepaymentBody,
    ctx: TxnCreateCtx,
) -> RepaymentOut:
    """Record a repayment allocated penalties -> interest -> principal."""
    channel = require_cash_channel(body.channel)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        result = await loans_service.record_repayment(
            session,
            ctx.tenant_id,
            ctx.user_id,
            loan_id,
            amount=body.amount,
            channel=channel,
        )
    return RepaymentOut(
        txn_id=str(result.txn_id),
        txn_ref=result.txn_ref,
        penalties=str(result.allocation.penalties),
        interest=str(result.allocation.interest),
        principal=str(result.allocation.principal),
        balance_after=str(result.balance_after),
        penalty_due_after=str(result.penalty_due_after),
        status=result.status.value,
    )


def portfolio_summary_out(summary: loans_service.PortfolioSummary) -> PortfolioSummaryOut:
    """Shared serializer: /portfolio/summary and the P13.9 dashboard
    loan-book slice render the SAME model (gate 1.1 — no forked shape)."""
    return PortfolioSummaryOut(
        active_loans=summary.active_loans,
        outstanding_balance=str(summary.outstanding_balance),
        npl_balance=str(summary.npl_balance),
        npl_ratio_pct=str(summary.npl_ratio_pct),
        par30_balance=str(summary.par30_balance),
        par30_ratio_pct=str(summary.par30_ratio_pct),
        provisions=str(summary.provisions),
        penalties_due=str(summary.penalties_due),
        by_classification=[
            ClassificationSliceOut(
                classification=s.classification.value,
                count=s.count,
                balance=str(s.balance),
                provisions=str(s.provisions),
            )
            for s in summary.by_classification
        ],
    )


@router.get("/portfolio/summary")
async def portfolio_summary(ctx: BookViewCtx) -> PortfolioSummaryOut:
    """NPL / PAR-30 / provisioning aggregates feeding the dashboard."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        summary = await loans_service.portfolio_summary(session, ctx.tenant_id)
    return portfolio_summary_out(summary)


@router.post("/jobs/arrears")
async def run_arrears(body: ArrearsRunBody, ctx: BookEditCtx) -> ArrearsRunOut:
    """Run the arrears job for the caller's tenant (batched, idempotent).

    The nightly scheduler calls the same service per tenant; this route
    exists for operations and backfills. Each batch commits its own
    short transaction (gate 1.3).

    P13.8: the same run accrues arrears penalties into
    loans.penalty_due, write-once per (loan, UTC day) via the
    penalty_accruals claim. Configuration (rate/grace/basis) is
    resolved server-side from tenant settings only — this body accepts
    none of it (extra="forbid"; v1.1 rule 1) — and an unconfigured
    tenant accrues nothing (penalty_configured=false, fail closed).
    Ledger boundary: penalty income stays recognised ON RECEIPT by the
    P10 repayment allocation (income.penalties); this job maintains
    only the receivable-side loans.penalty_due and posts no ledger
    rows.

    Permission (P4 matrix, verified): loan_book x EDIT. Reclassifying
    days-past-due/provisioning and maintaining the penalty receivable
    rewrite loan-book rows, so the job sits with the roles the matrix
    grants loan_book:edit (System Admin, Branch Manager) — not
    loan_book:approve (a committee decision power) and not any
    transactions permission (no ledger posting happens here).
    """
    as_of = resolve_as_of(body.as_of)
    factory = get_sessionmaker(get_settings().database_url)
    result = await arrears_service.run_arrears_for_tenant(
        partial(tenant_session, factory, ctx.tenant_id),
        ctx.tenant_id,
        as_of=as_of,
        batch_size=body.batch_size,
    )
    return ArrearsRunOut(
        as_of=as_of.isoformat(),
        scanned=result.scanned,
        updated=result.updated,
        batches=result.batches,
        penalty_configured=result.penalty_configured,
        penalties_accrued=result.penalties_accrued,
        penalty_total=str(result.penalty_total),
        cases_closed_cured=result.cases_closed_cured,
        cases_closed_written_off=result.cases_closed_written_off,
    )
