"""Loan servicing: repayment allocation, loan book, settlement, portfolio (P10).

Allocation contract (documented, single source of truth in
genesis.domain.lending.allocate_repayment):

  1. penalties — loans.penalty_due is cleared first
  2. interest  — accrued unpaid interest on installments already due
     (interest not yet due is never collected; paying beyond the due
     buckets prepays principal)
  3. principal — the remainder reduces loans.balance

Every repayment runs under the loan's row lock (concurrency safety), posts split
ledger legs through the P7 contract (data integrity), and audits + notifies
in-transaction (the house gates). When the balance and penalties reach
zero the loan closes through the status machine and the P9 guarantee
release hook frees the guarantors' capacity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.guarantees import release_guarantees_for_loan
from genesis.application.ledger import post_allocated_repayment
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import (
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_created_id_cursor,
)
from genesis.domain.ledger import Channel
from genesis.domain.lending import (
    LoanClass,
    LoanStatus,
    RepaymentAllocation,
    SettlementQuote,
    allocate_repayment,
    classify,
    loan_transition,
    settlement_quote,
)
from genesis.domain.money import ZERO, to_cents
from genesis.errors import ConflictError, InvalidInputError, NotFoundError

#: Columns are table-qualified because the read statements join the
#: members registry (alias mm) and the product catalogue (alias pp)
#: for display labels; the three label columns ride PK-served joins.
_LOAN_COLS = (
    "loans.id, loans.application_id, loans.member_id, loans.product_id, "
    "loans.principal, loans.balance, loans.rate_pct, loans.term_months, "
    "loans.status, loans.classification, loans.days_past_due, "
    "loans.provision_pct, loans.penalty_due, loans.disbursed_at, "
    "loans.closed_at, loans.version, mm.member_no, mm.name, pp.name, "
    "loans.loan_ref"
)

#: Display-label joins for the read statements: each rides the joined
#: table's PRIMARY KEY per page row plus the explicit tenant predicate
#: (index-served, no new index). LEFT JOIN keeps the book honest if a
#: label row is ever absent — a missing label must never drop a loan.
_LOAN_LABEL_JOINS = (
    "LEFT JOIN members mm ON mm.tenant_id = loans.tenant_id "
    "AND mm.id = loans.member_id "
    "LEFT JOIN loan_products pp ON pp.tenant_id = loans.tenant_id "
    "AND pp.id = loans.product_id "
)

#: Cursor scope id: signed cursors are bound to this
#: endpoint and this tenant — no cross-scope replay (tenant isolation).
LOAN_BOOK_SCOPE = "loan_book.list"

#: Cursor scope id for the MEMBER self-service loan listing (ADR-0007):
#: the member surface reuses this module's list statement with the
#: principal-derived member filter but mints cursors under its OWN
#: scope, so a staff loan-book cursor is a sanitized 400 on the member
#: route and vice versa (the FM1 audience separation extended to
#: pagination state).
MEMBER_LOANS_SCOPE = "member.loans.list"


@dataclass(frozen=True)
class LoanRecord:
    id: uuid.UUID
    application_id: uuid.UUID
    member_id: uuid.UUID
    product_id: uuid.UUID
    principal: Decimal
    balance: Decimal
    rate_pct: Decimal
    term_months: int
    status: LoanStatus
    classification: LoanClass
    days_past_due: int
    provision_pct: Decimal
    penalty_due: Decimal
    disbursed_at: datetime | None
    closed_at: datetime | None
    version: int
    #: Human display labels — the borrower's member number and
    #: registered name plus the product name — resolved server-side in
    #: the SAME read statement (PK joins, no per-row lookups). None is
    #: the honest state only if a label row is absent; labels are
    #: never invented client-side.
    member_no: str | None
    member_name: str | None
    product_name: str | None
    #: Human loan reference (LN-XXXX, 0048) — minted at disbursement;
    #: None only for rows written before the backfill.
    loan_ref: str | None = None


@dataclass(frozen=True)
class ScheduleRow:
    installment_no: int
    due_date: date
    principal_due: Decimal
    interest_due: Decimal
    total_due: Decimal
    paid_amount: Decimal


@dataclass(frozen=True)
class RepaymentResult:
    txn_id: uuid.UUID
    txn_ref: str
    allocation: RepaymentAllocation
    balance_after: Decimal
    penalty_due_after: Decimal
    status: LoanStatus


@dataclass(frozen=True)
class ClassificationSlice:
    classification: LoanClass
    count: int
    balance: Decimal
    provisions: Decimal


@dataclass(frozen=True)
class PortfolioSummary:
    """Dashboard aggregates over active loans (cent-exact)."""

    active_loans: int
    outstanding_balance: Decimal
    npl_balance: Decimal
    npl_ratio_pct: Decimal
    par30_balance: Decimal
    par30_ratio_pct: Decimal
    provisions: Decimal
    penalties_due: Decimal
    by_classification: list[ClassificationSlice]


def _row_to_loan(row: Any) -> LoanRecord:
    return LoanRecord(
        id=uuid.UUID(str(row[0])),
        application_id=uuid.UUID(str(row[1])),
        member_id=uuid.UUID(str(row[2])),
        product_id=uuid.UUID(str(row[3])),
        principal=Decimal(str(row[4])),
        balance=Decimal(str(row[5])),
        rate_pct=Decimal(str(row[6])),
        term_months=int(row[7]),
        status=LoanStatus(str(row[8])),
        classification=LoanClass(str(row[9])),
        days_past_due=int(row[10]),
        provision_pct=Decimal(str(row[11])),
        penalty_due=Decimal(str(row[12])),
        disbursed_at=row[13],
        closed_at=row[14],
        version=int(row[15]),
        member_no=str(row[16]) if row[16] is not None else None,
        member_name=str(row[17]) if row[17] is not None else None,
        product_name=str(row[18]) if row[18] is not None else None,
        loan_ref=str(row[19]) if row[19] is not None else None,
    )


async def get_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    member_id: uuid.UUID | None = None,
) -> LoanRecord:
    """Single-loan read; optionally OWNERSHIP-SCOPED in the query.

    member_id (ADR-0007): when the member self-service detail route
    passes the principal-derived member id, the ownership predicate is
    part of the statement itself — never fetch-then-check — so another
    member's loan is indistinguishable from a nonexistent one (404,
    least disclosure; no figures on any rejection path).
    """
    # The ownership fragment is a static literal chosen in code; the
    # value is a bound parameter (v1.1 rule 6).
    ownership = "AND loans.member_id = CAST(:mid AS uuid) " if member_id is not None else ""
    params: dict[str, object] = {"id": str(loan_id), "tid": str(tenant_id)}
    if member_id is not None:
        params["mid"] = str(member_id)
    row = (
        await session.execute(
            # Explicit tenant predicate on top of RLS: defence in depth
            # on the money path (least disclosure).
            text(
                f"SELECT {_LOAN_COLS} FROM loans "  # noqa: S608
                f"{_LOAN_LABEL_JOINS}"
                "WHERE loans.id = CAST(:id AS uuid) "
                f"AND loans.tenant_id = CAST(:tid AS uuid) {ownership}"
            ),
            params,
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan {loan_id} not found")
    return _row_to_loan(row)


async def list_loans(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: LoanStatus | None = None,
    classification: LoanClass | None = None,
    cursor: str | None = None,
    limit: int = 20,
    member_id: uuid.UUID | None = None,
    cursor_scope: str = LOAN_BOOK_SCOPE,
) -> tuple[list[LoanRecord], str | None]:
    """Keyset-paginated loan book, newest first, page cap 100 (scalability).

    Backed by idx_loans_created_keyset (tenant_id, created_at DESC,
    id DESC) so page depth never degrades the scan.

    member_id / cursor_scope (ADR-0007): the member self-service list
    passes the principal-derived member id — ownership is a predicate
    IN the statement, never a post-fetch check — and its OWN cursor
    scope (MEMBER_LOANS_SCOPE), so member cursors never replay against
    the staff loan book. The member-filtered probe rides idx_loans_member
    (tenant_id, member_id); the residual top-N order runs over one
    member's loans only — bounded by lending reality, never the book.
    """
    limit = max(1, min(limit, 100))
    # Explicit tenant predicate on top of RLS (defence in depth, gate
    # 1.6); also the leading column of idx_loans_created_keyset.
    # Qualified because the label joins bring further tenant_id
    # columns into scope; the predicate stays the leading column of
    # idx_loans_created_keyset.
    clauses: list[str] = ["loans.tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if member_id is not None:
        clauses.append("loans.member_id = CAST(:mid AS uuid)")
        params["mid"] = str(member_id)
    if status is not None:
        clauses.append("loans.status = :status")
        params["status"] = status.value
    if classification is not None:
        clauses.append("classification = :cls")
        params["cls"] = classification.value
    if cursor:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(cursor, tenant_id=tenant_id, endpoint=cursor_scope, entity="loan")
        params["c_ts"], params["c_id"] = parse_created_id_cursor(inner, entity="loan")
        clauses.append("(loans.created_at, loans.id) < (:c_ts, CAST(:c_id AS uuid))")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    # Static fragments chosen in code; all values are bound parameters.
    rows = (
        await session.execute(
            text(
                f"SELECT loans.created_at, {_LOAN_COLS} FROM loans "  # noqa: S608
                f"{_LOAN_LABEL_JOINS}"
                f"{where}"
                "ORDER BY loans.created_at DESC, loans.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_loan(r[1:]) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            build_created_id_cursor(last[0], last[1]),
            tenant_id=tenant_id,
            endpoint=cursor_scope,
        )
    return items, next_cursor


async def get_schedule(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    member_id: uuid.UUID | None = None,
) -> list[ScheduleRow]:
    """Full amortisation schedule; bounded by the loan term (<= 120 rows).

    member_id (ADR-0007, issue #33 item 3): when the member
    self-service detail passes the principal-derived member id, this
    read is ownership-scoped STRUCTURALLY — the scoped get_loan probe
    404s a non-owner before any schedule row is read, and the schedule
    statement itself carries an ownership predicate (EXISTS against
    loans.member_id) — never order-safe reliance on a caller having
    checked first. Same style as get_loan: static clause literals
    chosen in code, every value a bound parameter (v1.1 rule 6).
    """
    # 404 before returning an empty list; ownership-scoped when the
    # member surface calls (non-owner indistinguishable from
    # nonexistent — least disclosure).
    await get_loan(session, tenant_id, loan_id, member_id=member_id)
    ownership = (
        "AND EXISTS (SELECT 1 FROM loans "
        "WHERE loans.id = loan_schedules.loan_id "
        "AND loans.tenant_id = loan_schedules.tenant_id "
        "AND loans.member_id = CAST(:mid AS uuid)) "
        if member_id is not None
        else ""
    )
    params: dict[str, object] = {"id": str(loan_id), "tid": str(tenant_id)}
    if member_id is not None:
        params["mid"] = str(member_id)
    rows = (
        await session.execute(
            text(
                "SELECT installment_no, due_date, principal_due, interest_due, "  # noqa: S608
                "total_due, paid_amount FROM loan_schedules "
                "WHERE loan_id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                f"{ownership}"
                "ORDER BY installment_no"
            ),
            params,
        )
    ).all()
    return [
        ScheduleRow(
            installment_no=int(r[0]),
            due_date=r[1],
            principal_due=Decimal(str(r[2])),
            interest_due=Decimal(str(r[3])),
            total_due=Decimal(str(r[4])),
            paid_amount=Decimal(str(r[5])),
        )
        for r in rows
    ]


async def _interest_due(
    session: AsyncSession, tenant_id: uuid.UUID, loan_id: uuid.UUID, as_of: date
) -> Decimal:
    """Accrued unpaid interest on installments due on or before as_of.

    paid_amount covers interest before principal within an installment
    (mirror of the allocation order), so the unpaid interest of one
    installment is GREATEST(interest_due - paid_amount, 0). Correct only
    because repayment cash is never applied to installments due after
    the payment date (see record_repayment): a prepayment that touched
    future rows would silently erase interest that was never collected.
    Served by the partial index idx_schedules_unpaid.
    """
    value = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(GREATEST(interest_due - paid_amount, 0)), 0) "
                "FROM loan_schedules "
                "WHERE loan_id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND due_date <= :as_of AND paid_amount < total_due"
            ),
            {"id": str(loan_id), "tid": str(tenant_id), "as_of": as_of},
        )
    ).scalar_one()
    return Decimal(str(value))


async def get_settlement_quote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> SettlementQuote:
    """Early settlement payoff: balance + due interest + penalties.

    Interest on installments not yet due is waived (documented in the
    domain SettlementQuote contract).
    """
    quote_date = as_of or datetime.now(UTC).date()
    loan = await get_loan(session, tenant_id, loan_id)
    if loan.status is not LoanStatus.ACTIVE:
        raise ConflictError(f"loan {loan_id} is '{loan.status.value}': nothing to settle")
    interest = await _interest_due(session, tenant_id, loan_id, quote_date)
    return settlement_quote(loan.balance, interest, loan.penalty_due)


async def record_repayment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    loan_id: uuid.UUID,
    *,
    amount: Decimal,
    channel: Channel,
    received_at: datetime | None = None,
) -> RepaymentResult:
    """Allocate and post one repayment atomically (the house gates).

    Lock -> compute buckets -> allocate -> post split legs -> update
    schedule and balance -> close + release guarantees when paid off.
    All inside the caller's transaction; no partial success.
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("repayment amount must be positive")
    ts = received_at or datetime.now(UTC)

    # Serialisation point: every balance/schedule writer takes this lock.
    row = (
        await session.execute(
            text(
                "SELECT member_id, balance, penalty_due, status "
                "FROM loans WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan {loan_id} not found")
    member_id = uuid.UUID(str(row[0]))
    balance = Decimal(str(row[1]))
    penalty_due = Decimal(str(row[2]))
    status = LoanStatus(str(row[3]))
    if status is not LoanStatus.ACTIVE:
        raise ConflictError(f"loan {loan_id} is '{status.value}' and cannot accept repayments")

    interest_due = await _interest_due(session, tenant_id, loan_id, ts.date())
    try:
        allocation = allocate_repayment(
            amount,
            penalties_due=penalty_due,
            interest_due=interest_due,
            principal_outstanding=balance,
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc

    posting = await post_allocated_repayment(
        session, tenant_id, member_id, loan_id, allocation, channel, actor_id, occurred_at=ts
    )

    # Apply the cash received (interest + principal) to open installments
    # that are DUE on or before the repayment date, oldest first;
    # paid_amount is servicing bookkeeping feeding the arrears scan
    # (penalties live on the loan row, not the schedule). Installments
    # due in the future are never touched: marking them paid with
    # prepaid principal would make _interest_due treat that cash as
    # interest collected, silently erasing future interest that was
    # never received (review finding 3). Cash left over after the due
    # buckets is a principal prepayment — it reduces loans.balance
    # below (settlement quotes and payoff reflect it immediately) while
    # the schedule keeps its contractual dues.
    remaining = allocation.interest + allocation.principal
    if remaining > ZERO:
        unpaid = (
            await session.execute(
                text(
                    "SELECT id, total_due, paid_amount FROM loan_schedules "
                    "WHERE loan_id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "AND due_date <= :as_of AND paid_amount < total_due "
                    "ORDER BY installment_no"
                ),
                {"id": str(loan_id), "tid": str(tenant_id), "as_of": ts.date()},
            )
        ).all()
        for inst_id, total_due_raw, paid_raw in unpaid:
            if remaining <= ZERO:
                break
            open_amount = Decimal(str(total_due_raw)) - Decimal(str(paid_raw))
            pay = min(remaining, open_amount)
            await session.execute(
                text(
                    # Explicit tenant predicate on the write, on top of
                    # RLS (defence in depth).
                    "UPDATE loan_schedules SET paid_amount = paid_amount + :pay "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"pay": str(pay), "id": str(inst_id), "tid": str(tenant_id)},
            )
            remaining -= pay

    balance_after = to_cents(balance - allocation.principal)
    penalty_after = to_cents(penalty_due - allocation.penalties)
    await session.execute(
        text(
            # Explicit tenant predicate on the write, on top of RLS
            # (defence in depth).
            "UPDATE loans SET balance = :bal, penalty_due = :pen, "
            "version = version + 1, updated_at = :ts "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "bal": str(balance_after),
            "pen": str(penalty_after),
            "ts": ts,
            "id": str(loan_id),
            "tid": str(tenant_id),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="loan.repayment",
        entity="loans",
        entity_id=str(loan_id),
        before={"balance": str(balance), "penalty_due": str(penalty_due)},
        after={
            "balance": str(balance_after),
            "penalty_due": str(penalty_after),
            "txn_ref": posting.txn_ref,
            "penalties": str(allocation.penalties),
            "interest": str(allocation.interest),
            "principal": str(allocation.principal),
        },
    )

    new_status: LoanStatus = status
    if balance_after == ZERO and penalty_after == ZERO:
        new_status = await _close_loan(session, tenant_id, actor_id, loan_id, ts)

    return RepaymentResult(
        txn_id=posting.txn_id,
        txn_ref=posting.txn_ref,
        allocation=allocation,
        balance_after=balance_after,
        penalty_due_after=penalty_after,
        status=new_status,
    )


async def _close_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    loan_id: uuid.UUID,
    ts: datetime,
) -> LoanStatus:
    """Close a fully repaid loan and release its guarantees (P10 hook).

    Runs inside the repayment transaction while the loan row lock is
    held; the status machine is the single gatekeeper (concurrency safety).
    """
    target = loan_transition(LoanStatus.ACTIVE, LoanStatus.CLOSED)
    # Fully repaid: the prudential state returns to day zero. Leaving a
    # stale classification (e.g. 'doubtful' with a 50% provision) on a
    # closed loan would misstate its record and any per-loan reporting.
    healthy = classify(0)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth).
                "UPDATE loans SET status = :st, closed_at = :ts, days_past_due = 0, "
                "classification = :cls, provision_pct = :prov, "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'active'"
            ),
            {
                "st": target.value,
                "ts": ts,
                "cls": healthy.label.value,
                "prov": str(healthy.provision_pct),
                "id": str(loan_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"loan {loan_id} changed state during closure; retry")
    await release_guarantees_for_loan(session, tenant_id, actor_id, loan_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="loan.closed",
        entity="loans",
        entity_id=str(loan_id),
        before={"status": LoanStatus.ACTIVE.value},
        after={"status": target.value, "classification": healthy.label.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="loan.closed",
        payload={"loan_id": str(loan_id)},
    )
    return target


async def portfolio_summary(session: AsyncSession, tenant_id: uuid.UUID) -> PortfolioSummary:
    """Dashboard aggregates over the active book, exact to the cent.

    Provisions are rounded per loan (ROUND(balance * provision_pct / 100, 2))
    before summing — the same rule the loan book rows display — so the
    dashboard total always equals the sum of the visible rows. Explicit
    tenant predicates on top of RLS (defence in depth
    tenant scoping).
    """
    totals = (
        await session.execute(
            text(
                "SELECT count(*), "
                "COALESCE(SUM(balance), 0), "
                "COALESCE(SUM(balance) FILTER (WHERE classification IN "
                " ('substandard', 'doubtful', 'loss')), 0), "
                "COALESCE(SUM(balance) FILTER (WHERE days_past_due > 30), 0), "
                "COALESCE(SUM(ROUND(balance * provision_pct / 100, 2)), 0), "
                "COALESCE(SUM(penalty_due), 0) "
                "FROM loans WHERE status = 'active' "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"tid": str(tenant_id)},
        )
    ).one()
    active_loans = int(totals[0])
    outstanding = Decimal(str(totals[1]))
    npl_balance = Decimal(str(totals[2]))
    par30_balance = Decimal(str(totals[3]))
    provisions = Decimal(str(totals[4]))
    penalties = Decimal(str(totals[5]))

    slices = (
        await session.execute(
            text(
                "SELECT classification, count(*), COALESCE(SUM(balance), 0), "
                "COALESCE(SUM(ROUND(balance * provision_pct / 100, 2)), 0) "
                "FROM loans WHERE status = 'active' "
                "AND tenant_id = CAST(:tid AS uuid) "
                "GROUP BY classification ORDER BY classification"
            ),
            {"tid": str(tenant_id)},
        )
    ).all()

    def ratio(part: Decimal) -> Decimal:
        if outstanding == ZERO:
            return ZERO
        return to_cents(part * Decimal("100") / outstanding)

    return PortfolioSummary(
        active_loans=active_loans,
        outstanding_balance=outstanding,
        npl_balance=npl_balance,
        npl_ratio_pct=ratio(npl_balance),
        par30_balance=par30_balance,
        par30_ratio_pct=ratio(par30_balance),
        provisions=provisions,
        penalties_due=penalties,
        by_classification=[
            ClassificationSlice(
                classification=LoanClass(str(r[0])),
                count=int(r[1]),
                balance=Decimal(str(r[2])),
                provisions=Decimal(str(r[3])),
            )
            for r in slices
        ],
    )
