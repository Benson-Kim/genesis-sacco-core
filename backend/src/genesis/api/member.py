"""Member-facing endpoints: /member auth + guarantor self-service + reads.

The MEMBER principal's own surface. Authentication reuses the staff
endpoint SHAPES verbatim (bodies, response, rate guard, x-tenant-id pre-auth scoping — reuse-first:
the api/auth.py models and guard are imported, not copied) but issues MEMBER-audience tokens that
can never
satisfy a staff RequirePermission gate. Business routes carry
RequireMemberPrincipal — the per-request live-link re-check —
and the consent/release services re-verify the link again INSIDE the
transaction under the guarantee row lock.

Guarantor consent and self-release are acts of the member principal
(the P9 consent contract, completed): the interim email-match is
retired — these routes are where a guarantor acts for themselves.

The READ surface (ADR-0007, P17 mobile unblock): /member/me,
/member/transactions, /member/loans[/{loan_id}], /member/statement.
Member identity comes ONLY from the authenticated MemberAuthContext —
no read route accepts a member id in path, query or body (the rejected
alternative: an id parameter is an invitation to authorization bugs).
Reads reuse the staff application services through the member_portal
wrappers; signed cursors mint under member-OWN scopes so pagination
state never replays across the staff/member boundary.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.auth import (
    OtpRequestBody,
    OtpVerifyBody,
    RefreshBody,
    TokenResponse,
    _rate_guard,
    tenant_id_from_headers,
)
from genesis.api.authz import RequireMemberPrincipal
from genesis.api.loans import GuaranteeOut, _guarantee_out
from genesis.api.members import StatementLineOut, StatementResponse
from genesis.application import guarantees as guarantees_service
from genesis.application import loans as loans_service
from genesis.application import member_auth as member_auth_service
from genesis.application import member_portal as member_portal_service
from genesis.application import transactions as txn_service
from genesis.application.auth import AuthFailure, MemberAuthContext
from genesis.errors import UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/member", tags=["member"])

_member_principal = RequireMemberPrincipal()

MemberCtx = Annotated[MemberAuthContext, Depends(_member_principal)]


class MemberActBody(BaseModel):
    """Consent/self-release body: the optimistic-lock version ONLY.

    extra="forbid" (least disclosure v1.1): there is deliberately NO field for
    who consents — the principal IS the authenticated credential; a
    caller-asserted identity or consent flag is a rejected design (the lesson)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class MemberLoanSummaryOut(BaseModel):
    """Loan summary on /member/me: active-loan count + outstanding total.

    The total is the SAME figure the staff member drawer shows
    (member_aggregates — single source of truth); a canonical decimal
    string rendered verbatim by clients (no client-side money math).
    """

    count: int
    total_outstanding: str


class MemberMeOut(BaseModel):
    """The authenticated member's own profile and advisory balances.

    Least disclosure: exactly what the read surface declares —
    profile (name, member_no, status), the two account balances and the
    loan summary. No internal ids beyond the member's own, no staff
    attribution, no guarantee exposure figures.
    """

    member_no: str
    name: str
    status: str
    deposit_balance: str
    share_balance: str
    loans: MemberLoanSummaryOut


class MemberTransactionOut(BaseModel):
    """One OWN posting on the member surface.

    Least disclosure vs the staff TransactionOut: no created_by (staff
    actor attribution stays behind transactions:view), no member_id or
    display labels (the postings are the principal's own by
    construction). Amounts are canonical decimal strings rendered
    verbatim (no client-side money math).
    """

    id: str
    txn_ref: str
    type: str
    amount: str
    channel: str
    direction: str
    occurred_at: str
    is_reversal: bool
    external_ref: str | None


class MemberTransactionListResponse(BaseModel):
    items: list[MemberTransactionOut]
    next_cursor: str | None


class MemberLoanOut(BaseModel):
    """One OWN loan on the member surface.

    Least disclosure vs the staff LoanOut: no classification or
    provision_pct (prudential internals stay behind loan_book:view),
    no application/product UUIDs, no optimistic-lock version (this
    surface is read-only). days_past_due and penalty_due ARE the
    member's own arrears facts — the figures they are asked to pay.
    """

    id: str
    loan_ref: str | None
    product_name: str | None
    principal: str
    balance: str
    rate_pct: str
    term_months: int
    status: str
    days_past_due: int
    penalty_due: str
    disbursed_at: str | None
    closed_at: str | None


class MemberLoanListResponse(BaseModel):
    items: list[MemberLoanOut]
    next_cursor: str | None


class MemberInstallmentOut(BaseModel):
    """One schedule row on the loan detail read.

    status is derived SERVER-side ('paid'/'partial'/'open' — see
    member_portal.installment_status) so the client never compares
    money; the contractual dues and paid_amount come verbatim from the
    schedule the P10 repayment allocator maintains.
    """

    installment_no: int
    due_date: str
    principal_due: str
    interest_due: str
    total_due: str
    paid_amount: str
    status: str


class MemberLoanDetailOut(MemberLoanOut):
    """MemberLoanOut expanded with the amortisation schedule.

    Unpaginated BY CONSTRUCTION: the schedule is bounded by the loan
    term (<= 120 rows), the established staff-schedule posture.
    """

    schedule: list[MemberInstallmentOut]


@router.post("/auth/otp/request", status_code=202, dependencies=[Depends(_rate_guard)])
async def request_member_otp(body: OtpRequestBody, request: Request) -> dict[str, str]:
    """Member OTP request (the P3 policy verbatim; never reveals
    whether a credential exists)."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        await member_auth_service.request_member_otp(session, tenant_id, body.signin_identifier)
    return {"status": "sent"}


@router.post("/auth/otp/verify", dependencies=[Depends(_rate_guard)])
async def verify_member_otp(body: OtpVerifyBody, request: Request) -> TokenResponse:
    """Verify the member OTP; issues MEMBER-audience tokens."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await member_auth_service.verify_member_otp(
            session, tenant_id, body.signin_identifier, body.code
        )
    # The transaction has committed: punitive state (attempt counters)
    # is durable even though this request fails (the house gates).
    if isinstance(outcome, AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/auth/refresh", dependencies=[Depends(_rate_guard)])
async def refresh_member_token(body: RefreshBody, request: Request) -> TokenResponse:
    """Rotate a member refresh token; reuse revokes the family (P3)."""
    tenant_id = tenant_id_from_headers(request)
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, tenant_id) as session:
        outcome = await member_auth_service.rotate_member_refresh_token(
            session, tenant_id, body.refresh_token
        )
    # Family revocation on reuse must survive the failed request, so
    # the 401 is raised only after the transaction commits (concurrency safety).
    if isinstance(outcome, AuthFailure):
        raise UnauthenticatedError(outcome.reason)
    return TokenResponse(
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_in=outcome.expires_in,
    )


@router.post("/guarantees/{guarantee_id}/consent")
async def consent_guarantee_as_member(
    guarantee_id: uuid.UUID,
    body: MemberActBody,
    ctx: MemberCtx,
) -> GuaranteeOut:
    """Guarantor consent as the MEMBER principal: pledged -> active.

    The link is re-verified inside the transaction under the guarantee
    row lock; the consent row carries the credential.
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.consent_guarantee_as_member(
            session,
            ctx,
            guarantee_id,
            version=body.version,
        )
    return _guarantee_out(record)


@router.post("/guarantees/{guarantee_id}/release")
async def release_guarantee_as_member(
    guarantee_id: uuid.UUID,
    body: MemberActBody,
    ctx: MemberCtx,
) -> GuaranteeOut:
    """Withdraw the member's OWN unconsented pledge (rules; the member-principal replacement for the
    retired email-match)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.release_guarantee_as_member(
            session,
            ctx,
            guarantee_id,
            version=body.version,
        )
    return _guarantee_out(record)


def _member_txn_out(t: txn_service.TransactionRecord) -> MemberTransactionOut:
    return MemberTransactionOut(
        id=str(t.id),
        txn_ref=t.txn_ref,
        type=t.txn_type.value,
        amount=str(t.amount),
        channel=t.channel.value,
        direction=t.direction.value,
        occurred_at=t.occurred_at.isoformat(),
        is_reversal=t.is_reversal,
        external_ref=t.external_ref,
    )


def _member_loan_out(loan: loans_service.LoanRecord) -> MemberLoanOut:
    return MemberLoanOut(
        id=str(loan.id),
        loan_ref=loan.loan_ref,
        product_name=loan.product_name,
        principal=str(loan.principal),
        balance=str(loan.balance),
        rate_pct=str(loan.rate_pct),
        term_months=loan.term_months,
        status=loan.status.value,
        days_past_due=loan.days_past_due,
        penalty_due=str(loan.penalty_due),
        disbursed_at=loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        closed_at=loan.closed_at.isoformat() if loan.closed_at else None,
    )


def _installment_out(row: loans_service.ScheduleRow) -> MemberInstallmentOut:
    return MemberInstallmentOut(
        installment_no=row.installment_no,
        due_date=row.due_date.isoformat(),
        principal_due=str(row.principal_due),
        interest_due=str(row.interest_due),
        total_due=str(row.total_due),
        paid_amount=str(row.paid_amount),
        status=member_portal_service.installment_status(row),
    )


@router.get("/me")
async def get_member_me(ctx: MemberCtx) -> MemberMeOut:
    """The authenticated member's profile, balances and loan summary
    (ADR-0007). Identity comes ONLY from the live-linked credential —
    there is no member id to pass and nothing to get wrong."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        overview = await member_portal_service.member_overview(
            session, ctx.tenant_id, ctx.member_id
        )
    return MemberMeOut(
        member_no=overview.record.member_no,
        name=overview.record.name,
        status=overview.record.status.value,
        deposit_balance=str(overview.deposit_balance),
        share_balance=str(overview.share_balance),
        loans=MemberLoanSummaryOut(
            count=overview.loan_count,
            total_outstanding=str(overview.loans_outstanding),
        ),
    )


@router.get("/transactions")
async def list_member_transactions(
    ctx: MemberCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemberTransactionListResponse:
    """The member's OWN postings, keyset-paginated (ADR-0007).

    The principal-derived member filter is a predicate in the reused
    staff list statement; cursors are signed under the member-own scope
    (member.transactions.list), so a staff transactions cursor is a
    sanitized 400 here — and vice versa.
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await member_portal_service.list_member_transactions(
            session, ctx.tenant_id, ctx.member_id, cursor=cursor, limit=limit
        )
    return MemberTransactionListResponse(
        items=[_member_txn_out(t) for t in items], next_cursor=next_cursor
    )


@router.get("/loans")
async def list_member_loans(
    ctx: MemberCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MemberLoanListResponse:
    """The member's OWN loans, keyset-paginated (ADR-0007).

    Ownership is the loans.member_id predicate inside the reused book
    statement — never fetch-then-check; cursors mint under the
    member-own scope (member.loans.list).
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await member_portal_service.list_member_loans(
            session, ctx.tenant_id, ctx.member_id, cursor=cursor, limit=limit
        )
    return MemberLoanListResponse(
        items=[_member_loan_out(x) for x in items], next_cursor=next_cursor
    )


@router.get("/loans/{loan_id}")
async def get_member_loan(loan_id: uuid.UUID, ctx: MemberCtx) -> MemberLoanDetailOut:
    """One OWN loan with schedule/installment status (ADR-0007).

    Ownership is enforced IN the query via the principal-derived member
    id: another member's loan is indistinguishable from a nonexistent
    one — 404 with no figures echoed (least disclosure).
    """
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        detail = await member_portal_service.get_member_loan(
            session, ctx.tenant_id, ctx.member_id, loan_id
        )
    return MemberLoanDetailOut(
        **_member_loan_out(detail.loan).model_dump(),
        schedule=[_installment_out(row) for row in detail.schedule],
    )


@router.get("/statement")
async def get_member_statement(
    ctx: MemberCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StatementResponse:
    """The member's OWN statement (ADR-0007) — the staff statement
    service verbatim (reuse-first), the member id derived from the
    principal, cursors under the member-own scope (member.statement)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await member_portal_service.member_statement(
            session, ctx.tenant_id, ctx.member_id, cursor=cursor, limit=limit
        )
    return StatementResponse(
        items=[
            StatementLineOut(
                occurred_at=line.occurred_at.isoformat(),
                txn_ref=line.txn_ref,
                type=line.type,
                channel=line.channel,
                amount=line.amount,
            )
            for line in page.items
        ],
        next_cursor=page.next_cursor,
    )
