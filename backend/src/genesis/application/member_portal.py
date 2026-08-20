"""Member self-service READ surface (ADR-0007) — application wrappers.

The MEMBER principal's own reads: profile + balances + loan summary,
own postings, own loans (list and detail with schedule), own statement.
Reuse-first (gate 1.1): every read goes through the EXISTING staff
application service with a member-scoping wrapper — the statements,
ordering, page walks and money serialization are the ones already
proven by the staff suites; nothing is forked here.

The identity contract (ADR-0007, non-negotiable): member_id in this
module is ALWAYS the principal-derived id from the authenticated
MemberAuthContext — the API layer never accepts a member id from the
wire, so nothing here has an id to double-check. Ownership on the loan
reads is a predicate IN the query (loans.member_id = :mid), never a
fetch-then-check: another member's loan is indistinguishable from a
nonexistent one (404, least disclosure).

Signed cursors mint under member-OWN scopes (MEMBER_TXN_LIST_SCOPE,
MEMBER_LOANS_SCOPE, MEMBER_STATEMENT_SCOPE), so pagination state can
never replay across the staff/member boundary in either direction —
the FM1 audience separation extended to cursors.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import guarantees as guarantees_service
from genesis.application import loans as loans_service
from genesis.application import members as members_service
from genesis.application import transactions as txn_service
from genesis.domain.lending import LoanStatus
from genesis.domain.money import ZERO

#: Active-loan COUNT behind the /member/me loan summary, module-level
#: so the EXPLAIN structural gate (tests/test_member_portal_explain.py)
#: asserts the exact production statement (the EXPLAIN-capture
#: convention). The outstanding TOTAL deliberately comes from the
#: existing member_aggregates statement (single source of truth for the
#: figure — a second SUM here could silently diverge from the staff
#: drawer); only the count is new. Served by idx_loans_member (0001:
#: tenant_id, member_id) — this read ships NO migration. Explicit
#: tenant predicate on top of forced RLS (defence in depth); every
#: value is a bound parameter (v1.1 rule 6). The status filter reuses
#: the SAME LoanStatus.ACTIVE binding as member_aggregates, so count
#: and total can never disagree on which loans are "open".
MEMBER_LOAN_COUNT_SQL = (
    "SELECT count(*) FROM loans "
    "WHERE member_id = CAST(:mid AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid) "
    "AND status = :loan_active"
)


@dataclass(frozen=True)
class MemberOverview:
    """The /member/me read model: profile, balances, loan summary.

    Advisory read-only figures (NO row locks): every BINDING money
    decision recomputes under the established row locks. The balances
    and the outstanding total come verbatim from member_aggregates —
    the figures the staff drawer already shows for the same member.
    """

    record: members_service.MemberRecord
    deposit_balance: Decimal
    share_balance: Decimal
    loan_count: int
    loans_outstanding: Decimal


@dataclass(frozen=True)
class MemberLoanDetail:
    """One own loan with its amortisation schedule (bounded by term)."""

    loan: loans_service.LoanRecord
    schedule: list[loans_service.ScheduleRow]


def installment_status(row: loans_service.ScheduleRow) -> str:
    """Code-owned installment payment status for the detail read.

    Derived SERVER-side so the mobile client never compares money (no
    client-side money math): 'paid' when the installment is fully
    covered, 'partial' when some cash has been applied, 'open'
    otherwise. Pure function of the schedule row the P10 repayment
    allocator maintains; nothing is invented here.
    """
    if row.paid_amount >= row.total_due:
        return "paid"
    if row.paid_amount > ZERO:
        return "partial"
    return "open"


async def member_overview(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> MemberOverview:
    """Profile + balances + loan summary for the authenticated member.

    Reuse-first: the profile is get_member, the three money figures are
    the existing member_aggregates statement (the guarantees figure it
    also returns is simply not disclosed here — least disclosure keeps
    the member surface to what /member/me declares); only the active-
    loan count is a new statement (MEMBER_LOAN_COUNT_SQL).
    """
    record = await members_service.get_member(session, tenant_id, member_id)
    aggregates = await members_service.member_aggregates(session, tenant_id, member_id)
    count = (
        await session.execute(
            text(MEMBER_LOAN_COUNT_SQL),
            {
                "mid": str(member_id),
                "tid": str(tenant_id),
                "loan_active": LoanStatus.ACTIVE.value,
            },
        )
    ).scalar_one()
    return MemberOverview(
        record=record,
        deposit_balance=aggregates.deposits_total,
        share_balance=aggregates.shares_total,
        loan_count=int(count),
        loans_outstanding=aggregates.loans_outstanding,
    )


async def list_member_transactions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[txn_service.TransactionRecord], str | None]:
    """The member's OWN postings — the staff list statement, member-scoped.

    The principal-derived member filter is a predicate in the statement
    (idx_txns_member_keyset serves it); cursors mint under the member
    scope so a staff transactions cursor is a sanitized 400 here and a
    member cursor is a sanitized 400 on the staff route (ADR-0007).
    """
    return await txn_service.list_transactions(
        session,
        tenant_id,
        member_id=member_id,
        cursor=cursor,
        limit=limit,
        cursor_scope=txn_service.MEMBER_TXN_LIST_SCOPE,
    )


async def list_member_loans(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[loans_service.LoanRecord], str | None]:
    """The member's OWN loans — the staff book statement, member-scoped.

    Ownership is the loans.member_id predicate inside the statement
    (idx_loans_member), never a post-fetch check; cursors mint under
    MEMBER_LOANS_SCOPE (no staff loan-book cursor replay).
    """
    return await loans_service.list_loans(
        session,
        tenant_id,
        member_id=member_id,
        cursor=cursor,
        limit=limit,
        cursor_scope=loans_service.MEMBER_LOANS_SCOPE,
    )


async def get_member_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    loan_id: uuid.UUID,
) -> MemberLoanDetail:
    """One OWN loan with its schedule (ADR-0007 detail read).

    Ownership is enforced IN the get_loan query via the principal-
    derived member id: a loan belonging to another member (or another
    tenant, hidden by RLS) is a 404 BEFORE any schedule row is read —
    no rejection path echoes a figure (least disclosure). The schedule
    read reuses the existing get_schedule service verbatim (bounded by
    the loan term, <= 120 rows).
    """
    loan = await loans_service.get_loan(session, tenant_id, loan_id, member_id=member_id)
    schedule = await loans_service.get_schedule(session, tenant_id, loan_id)
    return MemberLoanDetail(loan=loan, schedule=schedule)


async def member_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> members_service.StatementPage:
    """The member's OWN statement — the staff service verbatim (reuse-first).

    Identical lines to GET /members/{member_id}/statement for the same
    member; the id comes from the principal and the cursors mint under
    MEMBER_STATEMENT_SCOPE (no staff statement cursor replay).
    """
    return await members_service.member_statement(
        session,
        tenant_id,
        member_id,
        cursor=cursor,
        limit=limit,
        cursor_scope=members_service.MEMBER_STATEMENT_SCOPE,
    )


async def list_member_guarantees(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[guarantees_service.MemberGuaranteeItem], str | None]:
    """The member's OWN pledges (#41 — the P17 consent-inbox source).

    Guarantor-side ONLY: ownership is the guarantees.guarantor_member_id
    predicate inside the statement (idx_guarantees_guarantor — no new
    index, no migration), never a post-fetch check; the member id is
    the principal-derived id, exactly like every other read here.
    Cursors mint under MEMBER_GUARANTEES_SCOPE (member.guarantees.list),
    so no staff cursor replays here and no guarantees cursor replays on
    any staff route (the ADR-0007 cursor-scope discipline).
    """
    return await guarantees_service.list_member_guarantees(
        session,
        tenant_id,
        member_id,
        status=status,
        cursor=cursor,
        limit=limit,
        cursor_scope=guarantees_service.MEMBER_GUARANTEES_SCOPE,
    )
