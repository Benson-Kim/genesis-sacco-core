"""Report definitions and queries for P13 exports (gates 1.3, 1.5, 1.6).

Each report declares its columns (with per-column PII entitlement,
P13 blocker e), its permitted filters, and a builder that binds the
report query to a session/tenant/filter set. The export engine
(genesis.application.exports.run_export) drives every report through
keyset batches with a hard server-side row cap (P13 blocker d) —
never OFFSET in SQL, never an unbounded scan. Aggregate reports
(trial balance, NPL trend, exit statement) have cardinality bounded
by construction (chart-of-accounts size / configured month count /
one document) and are computed once, then paged from memory.

Every read carries an explicit bound tenant_id predicate on top of
forced RLS (P13 blocker c; gate 1.6 v1.1). Money figures are computed
from the append-only ledger or server-maintained balances — never from
request input (P13 blocker a).

The SQL builder functions are module-level so the P13 EXPLAIN test
captures plans for the exact statements this module executes
(P13 blocker j).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import dividends as dividends_service
from genesis.application import member_exits as exits_service
from genesis.application.members import get_member
from genesis.domain.documents import Cell
from genesis.domain.ledger import Side, TxnType, member_direction
from genesis.domain.money import ZERO, to_cents
from genesis.errors import InvalidInputError
from genesis.settings import get_settings

#: Opaque keyset cursor used by the export engine; each report's
#: cursor_key produces the next one from the last consumed raw row.
type ReportCursor = tuple[datetime, str] | int

#: (cursor, limit) -> raw rows. The engine always asks for limit rows
#: and treats a full response as "more may follow".
type ReportFetch = Callable[[ReportCursor | None, int], Awaitable[list[Any]]]


class ReportName(enum.StrEnum):
    MEMBER_STATEMENT = "member_statement"
    TRIAL_BALANCE = "trial_balance"
    LOAN_BOOK = "loan_book"
    DISBURSEMENT_COLLECTIONS = "disbursement_collections"
    NPL_TREND = "npl_trend"
    MEMBER_EXIT_STATEMENT = "member_exit_statement"
    DIVIDEND_REBATE_SCHEDULE = "dividend_rebate_schedule"


@dataclass(frozen=True)
class ReportColumn:
    """One output column; pii columns require members:view (blocker e)."""

    key: str
    title: str
    pii: bool = False


@dataclass(frozen=True)
class ExportFilters:
    """The only caller-suppliable report scope (P13 blocker a).

    Ids and date ranges narrow WHICH rows a caller may read (further
    gated by RequirePermission + RLS + tenant predicates); money, cost,
    format, limit and storage parameters are never accepted.
    """

    member_id: uuid.UUID | None = None
    exit_id: uuid.UUID | None = None
    declaration_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None

    def provided_keys(self) -> frozenset[str]:
        return frozenset(
            key
            for key, value in (
                ("member_id", self.member_id),
                ("exit_id", self.exit_id),
                ("declaration_id", self.declaration_id),
                ("date_from", self.date_from),
                ("date_to", self.date_to),
            )
            if value is not None
        )

    def to_json(self) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in (
                ("member_id", self.member_id),
                ("exit_id", self.exit_id),
                ("declaration_id", self.declaration_id),
                ("date_from", self.date_from),
                ("date_to", self.date_to),
            )
            if value is not None
        }

    @staticmethod
    def from_json(raw: Mapping[str, str]) -> ExportFilters:
        return ExportFilters(
            member_id=uuid.UUID(raw["member_id"]) if "member_id" in raw else None,
            exit_id=uuid.UUID(raw["exit_id"]) if "exit_id" in raw else None,
            declaration_id=(uuid.UUID(raw["declaration_id"]) if "declaration_id" in raw else None),
            date_from=date.fromisoformat(raw["date_from"]) if "date_from" in raw else None,
            date_to=date.fromisoformat(raw["date_to"]) if "date_to" in raw else None,
        )


@dataclass(frozen=True)
class ReportQuery:
    """A report bound to one session/tenant/filter set, ready to stream."""

    fetch: ReportFetch
    cursor_key: Callable[[Any], ReportCursor]
    to_cells: Callable[[Any], tuple[Cell, ...]]


@dataclass(frozen=True)
class ReportDefinition:
    name: ReportName
    title: str
    columns: tuple[ReportColumn, ...]
    allowed_filters: frozenset[str]
    required_filters: frozenset[str]
    build: Callable[
        [AsyncSession, uuid.UUID, ExportFilters, datetime],
        Awaitable[ReportQuery],
    ]

    def column_keys(self) -> tuple[str, ...]:
        return tuple(column.key for column in self.columns)


def validate_filters(definition: ReportDefinition, filters: ExportFilters) -> None:
    """Reject scopes the report does not define (least surprise, gate 1.6)."""
    provided = filters.provided_keys()
    unknown = provided - definition.allowed_filters
    if unknown:
        raise InvalidInputError(
            f"filters not supported by {definition.name.value}: {', '.join(sorted(unknown))}"
        )
    missing = definition.required_filters - provided
    if missing:
        raise InvalidInputError(
            f"filters required by {definition.name.value}: {', '.join(sorted(missing))}"
        )
    if (
        filters.date_from is not None
        and filters.date_to is not None
        and filters.date_to < filters.date_from
    ):
        raise InvalidInputError("date_to must not precede date_from")


async def assert_scope_exists(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    report: ReportName,
    filters: ExportFilters,
) -> None:
    """Fail fast (404) when a scoped filter targets a missing row.

    Both lookups carry explicit tenant predicates inside the reused
    services (get_member / the P12 get_exit), so a foreign tenant's id
    is indistinguishable from a missing one (least disclosure).
    """
    if report is ReportName.MEMBER_STATEMENT and filters.member_id is not None:
        await get_member(session, tenant_id, filters.member_id)
    if report is ReportName.MEMBER_EXIT_STATEMENT and filters.exit_id is not None:
        await exits_service.get_exit(session, tenant_id, filters.exit_id)
    if report is ReportName.DIVIDEND_REBATE_SCHEDULE and filters.declaration_id is not None:
        await dividends_service.get_declaration(session, tenant_id, filters.declaration_id)


def _memory_query(
    rows: list[tuple[Cell, ...]],
) -> ReportQuery:
    """Page an already-computed, cardinality-bounded row list.

    In-memory slicing only — this is NOT SQL OFFSET pagination
    (P13 blocker d): the underlying statements ran exactly once with
    bounded output (accounts / configured months / one document).
    """

    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        start = cast(int, cursor) + 1 if cursor is not None else 0
        return [(index, row) for index, row in enumerate(rows)][start : start + limit]

    def cursor_key(raw: Any) -> ReportCursor:
        return int(raw[0])

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        cells: tuple[Cell, ...] = raw[1]
        return cells

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Member statement
# ---------------------------------------------------------------------------


def member_statement_page_sql(*, with_from: bool, with_to: bool, with_cursor: bool) -> str:
    """Keyset page of one member's transactions, oldest first (gate 1.3).

    Served by idx_txns_member_keyset (tenant_id, member_id,
    occurred_at DESC, id DESC; 0008) scanned backwards. Static
    fragments chosen in code; all values are bound parameters.
    """
    clauses = [
        "tenant_id = CAST(:tid AS uuid)",
        "member_id = CAST(:mid AS uuid)",
        "occurred_at <= :as_of",
    ]
    if with_from:
        clauses.append("occurred_at >= :d_from")
    if with_to:
        clauses.append("occurred_at < :d_to_excl")
    if with_cursor:
        clauses.append("(occurred_at, id) > (:c_ts, CAST(:c_id AS uuid))")
    return (
        "SELECT id, txn_ref, type, amount, occurred_at, reversal_of_id "  # noqa: S608
        f"FROM transactions WHERE {' AND '.join(clauses)} "
        "ORDER BY occurred_at, id LIMIT :limit"
    )


def member_statement_opening_sql(*, with_from: bool) -> str:
    """Opening-balance aggregate: activity before the statement window.

    Grouped by (type, is_reversal) so the signed direction is applied
    via the P11 domain single source of truth (member_direction) in
    Python — the DR/CR convention is never duplicated in SQL.
    """
    clauses = [
        "tenant_id = CAST(:tid AS uuid)",
        "member_id = CAST(:mid AS uuid)",
        "occurred_at <= :as_of",
    ]
    if with_from:
        clauses.append("occurred_at < :d_from")
    return (
        "SELECT type, (reversal_of_id IS NOT NULL) AS is_reversal, "  # noqa: S608
        "COALESCE(SUM(amount), 0) "
        f"FROM transactions WHERE {' AND '.join(clauses)} "
        "GROUP BY type, (reversal_of_id IS NOT NULL)"
    )


async def _build_member_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    member_id = filters.member_id
    if member_id is None:  # pragma: no cover - validate_filters refuses this
        raise InvalidInputError("member_statement requires member_id")
    member = await get_member(session, tenant_id, member_id)

    opening = ZERO
    if filters.date_from is not None:
        rows = (
            await session.execute(
                text(member_statement_opening_sql(with_from=True)),
                {
                    "tid": str(tenant_id),
                    "mid": str(member_id),
                    "as_of": as_of,
                    "d_from": filters.date_from,
                },
            )
        ).all()
        for type_raw, is_reversal, amount_raw in rows:
            direction = member_direction(TxnType(str(type_raw)), is_reversal=bool(is_reversal))
            amount = Decimal(str(amount_raw))
            opening += amount if direction is Side.CREDIT else -amount

    params: dict[str, object] = {
        "tid": str(tenant_id),
        "mid": str(member_id),
        "as_of": as_of,
    }
    if filters.date_from is not None:
        params["d_from"] = filters.date_from
    if filters.date_to is not None:
        params["d_to_excl"] = filters.date_to + timedelta(days=1)

    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        page_params = dict(params)
        page_params["limit"] = limit
        if cursor is not None:
            page_params["c_ts"], page_params["c_id"] = cast(tuple[datetime, str], cursor)
        page_sql = member_statement_page_sql(
            with_from=filters.date_from is not None,
            with_to=filters.date_to is not None,
            with_cursor=cursor is not None,
        )
        result = await session.execute(text(page_sql), page_params)
        return list(result.all())

    def cursor_key(raw: Any) -> ReportCursor:
        return (raw[4], str(raw[0]))

    # Running balance is sequential state across batches: opening +
    # sum(credits) - sum(debits) under the P11 member_direction
    # convention (single source of truth for the DR/CR pill; the
    # prototype's static demo rows are illustrative, not a formula).
    running = opening

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        nonlocal running
        txn_type = TxnType(str(raw[2]))
        amount = Decimal(str(raw[3]))
        direction = member_direction(txn_type, is_reversal=raw[5] is not None)
        debit = amount if direction is Side.DEBIT else None
        credit = amount if direction is Side.CREDIT else None
        running += amount if direction is Side.CREDIT else -amount
        return (
            member.member_no,
            member.name,
            raw[4],
            str(raw[1]),
            txn_type.value,
            debit,
            credit,
            running,
        )

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------

#: Aggregate over the append-only ledger as of the export instant.
#: Bounded by the chart-of-accounts cardinality. Served by
#: idx_ledger_account (tenant_id, account, created_at; 0001) with the
#: transactions join on its primary key.
TRIAL_BALANCE_SQL = """
SELECT le.account,
       COALESCE(SUM(le.amount) FILTER (WHERE le.side = 'debit'), 0) AS debits,
       COALESCE(SUM(le.amount) FILTER (WHERE le.side = 'credit'), 0) AS credits
FROM ledger_entries le
JOIN transactions t ON t.id = le.transaction_id AND t.tenant_id = le.tenant_id
WHERE le.tenant_id = CAST(:tid AS uuid) AND t.occurred_at <= :as_of
GROUP BY le.account
ORDER BY le.account
"""


async def _build_trial_balance(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    raw = (
        await session.execute(text(TRIAL_BALANCE_SQL), {"tid": str(tenant_id), "as_of": as_of})
    ).all()
    rows: list[tuple[Cell, ...]] = []
    total_debits = ZERO
    total_credits = ZERO
    for account, debits_raw, credits_raw in raw:
        debits = Decimal(str(debits_raw))
        credits = Decimal(str(credits_raw))
        total_debits += debits
        total_credits += credits
        rows.append((str(account), debits, credits))
    rows.append(("TOTAL", total_debits, total_credits))
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# Loan book (classification & provisions)
# ---------------------------------------------------------------------------


def loan_book_page_sql(*, with_cursor: bool) -> str:
    """Keyset page over the loan book, newest first (gate 1.3).

    Served by idx_loans_created_keyset (0007); the member join runs on
    the members primary key. Static fragments chosen in code.
    """
    cursor_clause = (
        "AND (l.created_at, l.id) < (:c_ts, CAST(:c_id AS uuid)) " if with_cursor else ""
    )
    return (
        "SELECT l.id, m.member_no, m.name, l.principal, l.balance, "  # noqa: S608
        "l.rate_pct, l.term_months, l.status, l.classification, "
        "l.days_past_due, l.provision_pct, l.created_at "
        "FROM loans l "
        "JOIN members m ON m.id = l.member_id AND m.tenant_id = l.tenant_id "
        "WHERE l.tenant_id = CAST(:tid AS uuid) "
        f"{cursor_clause}"
        "ORDER BY l.created_at DESC, l.id DESC LIMIT :limit"
    )


async def _build_loan_book(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        params: dict[str, object] = {"tid": str(tenant_id), "limit": limit}
        if cursor is not None:
            params["c_ts"], params["c_id"] = cast(tuple[datetime, str], cursor)
        page_sql = loan_book_page_sql(with_cursor=cursor is not None)
        result = await session.execute(text(page_sql), params)
        return list(result.all())

    def cursor_key(raw: Any) -> ReportCursor:
        return (raw[11], str(raw[0]))

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        balance = Decimal(str(raw[4]))
        provision_pct = Decimal(str(raw[10]))
        # Provision amount mirrors the prototype loan-book maths:
        # balance * provision_pct / 100, rounded to cents.
        provision = to_cents(balance * provision_pct / Decimal("100"))
        return (
            str(raw[0]),
            str(raw[1]),
            str(raw[2]),
            Decimal(str(raw[3])),
            balance,
            Decimal(str(raw[5])),
            int(raw[6]),
            str(raw[7]),
            str(raw[8]),
            int(raw[9]),
            provision_pct,
            provision,
        )

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Disbursements & collections
# ---------------------------------------------------------------------------


def disbursement_collections_page_sql(*, with_from: bool, with_to: bool, with_cursor: bool) -> str:
    """Keyset page over disbursement/repayment transactions (gate 1.3).

    Served by idx_txns_type_occurred (0013, shipped with this query).
    Type values travel as bound parameters, never interpolated.
    """
    clauses = [
        "tenant_id = CAST(:tid AS uuid)",
        "type IN (:t_disbursement, :t_repayment)",
        "occurred_at <= :as_of",
    ]
    if with_from:
        clauses.append("occurred_at >= :d_from")
    if with_to:
        clauses.append("occurred_at < :d_to_excl")
    if with_cursor:
        clauses.append("(occurred_at, id) > (:c_ts, CAST(:c_id AS uuid))")
    return (
        "SELECT id, txn_ref, type, amount, channel, occurred_at "  # noqa: S608
        f"FROM transactions WHERE {' AND '.join(clauses)} "
        "ORDER BY occurred_at, id LIMIT :limit"
    )


async def _build_disbursement_collections(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    params: dict[str, object] = {
        "tid": str(tenant_id),
        "as_of": as_of,
        "t_disbursement": TxnType.LOAN_DISBURSEMENT.value,
        "t_repayment": TxnType.LOAN_REPAYMENT.value,
    }
    if filters.date_from is not None:
        params["d_from"] = filters.date_from
    if filters.date_to is not None:
        params["d_to_excl"] = filters.date_to + timedelta(days=1)

    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        page_params = dict(params)
        page_params["limit"] = limit
        if cursor is not None:
            page_params["c_ts"], page_params["c_id"] = cast(tuple[datetime, str], cursor)
        page_sql = disbursement_collections_page_sql(
            with_from=filters.date_from is not None,
            with_to=filters.date_to is not None,
            with_cursor=cursor is not None,
        )
        result = await session.execute(text(page_sql), page_params)
        return list(result.all())

    def cursor_key(raw: Any) -> ReportCursor:
        return (raw[5], str(raw[0]))

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        return (raw[5], str(raw[1]), str(raw[2]), Decimal(str(raw[3])), str(raw[4]))

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# NPL trend (monthly series, prototype bars)
# ---------------------------------------------------------------------------

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


async def _build_npl_trend(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    months = get_settings().export_npl_trend_months
    rows: list[tuple[Cell, ...]] = []
    for month_end in npl_trend_month_ends(as_of, months):
        cutoff = datetime(month_end.year, month_end.month, month_end.day, tzinfo=UTC)
        d_next = cutoff + timedelta(days=1)
        raw = (
            await session.execute(
                text(NPL_TREND_MONTH_SQL),
                {
                    "tid": str(tenant_id),
                    "d_next": min(d_next, as_of + timedelta(microseconds=1)),
                    "d_date": month_end,
                    "receivable_account": "loans.receivable",
                },
            )
        ).one()
        gross = Decimal(str(raw[1]))
        npl_balance = Decimal(str(raw[3]))
        ratio = to_cents(npl_balance * Decimal("100") / gross) if gross > ZERO else Decimal("0.00")
        rows.append(
            (f"{month_end.year:04d}-{month_end.month:02d}", gross, npl_balance, int(raw[2]), ratio)
        )
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# Member exit statement (issue #16)
# ---------------------------------------------------------------------------


async def _build_member_exit_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    exit_id = filters.exit_id
    if exit_id is None:  # pragma: no cover - validate_filters refuses this
        raise InvalidInputError("member_exit_statement requires exit_id")
    # The P12 JSON statement service stays the canonical data source
    # (issue #16): the export renders the same document verbatim.
    doc = await exits_service.exit_statement(session, tenant_id, exit_id)
    row: tuple[Cell, ...] = (
        str(doc.exit_id),
        doc.member_no,
        doc.member_name,
        doc.member_status,
        doc.exit_status.value,
        doc.reason,
        doc.shares_amount,
        doc.deposits_amount,
        doc.equity,
        doc.loan_balance,
        doc.fees,
        doc.net_payable,
        doc.requested_at,
        doc.decided_at,
        doc.settled_at,
        doc.settlement_txn_ref,
    )
    return _memory_query([row])


# ---------------------------------------------------------------------------
# Dividend & rebate schedule (P13.11 / P13.10 catalogue entry)
# ---------------------------------------------------------------------------


def dividend_schedule_page_sql(*, with_cursor: bool) -> str:
    """Keyset page over one declaration's distribution claims (gate 1.3).

    Served by idx_dividend_distributions_page (tenant_id,
    declaration_id, created_at, id; 0020 — shipped with this query);
    the members join runs on the members primary key and the
    transactions join resolves the posting reference. Static fragments
    chosen in code; all values are bound parameters.
    """
    cursor_clause = (
        "AND (d.created_at, d.id) > (:c_ts, CAST(:c_id AS uuid)) " if with_cursor else ""
    )
    return (
        "SELECT d.id, m.member_no, m.name, d.share_basis, "  # noqa: S608
        "d.dividend_rate_pct, d.dividend_amount, d.deposit_basis, "
        "d.rebate_rate_pct, d.rebate_amount, d.total_amount, t.txn_ref, "
        "d.created_at "
        "FROM dividend_distributions d "
        "JOIN members m ON m.id = d.member_id AND m.tenant_id = d.tenant_id "
        "LEFT JOIN transactions t ON t.id = d.transaction_id "
        "AND t.tenant_id = d.tenant_id "
        "WHERE d.tenant_id = CAST(:tid AS uuid) "
        "AND d.declaration_id = CAST(:did AS uuid) "
        "AND d.created_at <= :as_of "
        f"{cursor_clause}"
        "ORDER BY d.created_at, d.id LIMIT :limit"
    )


async def _build_dividend_rebate_schedule(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    declaration_id = filters.declaration_id
    if declaration_id is None:  # pragma: no cover - validate_filters refuses this
        raise InvalidInputError("dividend_rebate_schedule requires declaration_id")
    # Existence check inside the snapshot transaction (404 for a
    # foreign tenant's id — least disclosure via the reused service).
    await dividends_service.get_declaration(session, tenant_id, declaration_id)

    params: dict[str, object] = {
        "tid": str(tenant_id),
        "did": str(declaration_id),
        "as_of": as_of,
    }

    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        page_params = dict(params)
        page_params["limit"] = limit
        if cursor is not None:
            page_params["c_ts"], page_params["c_id"] = cast(tuple[datetime, str], cursor)
        page_sql = dividend_schedule_page_sql(with_cursor=cursor is not None)
        result = await session.execute(text(page_sql), page_params)
        return list(result.all())

    def cursor_key(raw: Any) -> ReportCursor:
        return (raw[11], str(raw[0]))

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        return (
            str(raw[1]),
            str(raw[2]),
            Decimal(str(raw[3])),
            Decimal(str(raw[4])),
            Decimal(str(raw[5])),
            Decimal(str(raw[6])),
            Decimal(str(raw[7])),
            Decimal(str(raw[8])),
            Decimal(str(raw[9])),
            str(raw[10]) if raw[10] is not None else None,
        )

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REPORTS: dict[ReportName, ReportDefinition] = {
    ReportName.MEMBER_STATEMENT: ReportDefinition(
        name=ReportName.MEMBER_STATEMENT,
        title="Member statement",
        columns=(
            ReportColumn("member_no", "Member No"),
            ReportColumn("member_name", "Member", pii=True),
            ReportColumn("date", "Date"),
            ReportColumn("txn_ref", "Reference"),
            ReportColumn("type", "Type"),
            ReportColumn("debit", "Debit"),
            ReportColumn("credit", "Credit"),
            ReportColumn("balance", "Balance"),
        ),
        allowed_filters=frozenset({"member_id", "date_from", "date_to"}),
        required_filters=frozenset({"member_id"}),
        build=_build_member_statement,
    ),
    ReportName.TRIAL_BALANCE: ReportDefinition(
        name=ReportName.TRIAL_BALANCE,
        title="Trial balance",
        columns=(
            ReportColumn("account", "Account"),
            ReportColumn("debits", "Debits"),
            ReportColumn("credits", "Credits"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_trial_balance,
    ),
    ReportName.LOAN_BOOK: ReportDefinition(
        name=ReportName.LOAN_BOOK,
        title="Loan book — classification & provisions",
        columns=(
            ReportColumn("loan_id", "Loan"),
            ReportColumn("member_no", "Member No"),
            ReportColumn("member_name", "Member", pii=True),
            ReportColumn("principal", "Principal"),
            ReportColumn("balance", "Outstanding"),
            ReportColumn("rate_pct", "Rate %"),
            ReportColumn("term_months", "Term"),
            ReportColumn("status", "Status"),
            ReportColumn("classification", "Classification"),
            ReportColumn("days_past_due", "Days Past Due"),
            ReportColumn("provision_pct", "Provision %"),
            ReportColumn("provision", "Provision"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_loan_book,
    ),
    ReportName.DISBURSEMENT_COLLECTIONS: ReportDefinition(
        name=ReportName.DISBURSEMENT_COLLECTIONS,
        title="Disbursements & collections",
        columns=(
            ReportColumn("date", "Date"),
            ReportColumn("txn_ref", "Reference"),
            ReportColumn("type", "Type"),
            ReportColumn("amount", "Amount"),
            ReportColumn("channel", "Channel"),
        ),
        allowed_filters=frozenset({"date_from", "date_to"}),
        required_filters=frozenset(),
        build=_build_disbursement_collections,
    ),
    ReportName.NPL_TREND: ReportDefinition(
        name=ReportName.NPL_TREND,
        title="NPL trend (monthly)",
        columns=(
            ReportColumn("month", "Month"),
            ReportColumn("gross_outstanding", "Gross Outstanding"),
            ReportColumn("npl_balance", "NPL Balance"),
            ReportColumn("npl_loans", "NPL Loans"),
            ReportColumn("npl_ratio_pct", "NPL Ratio %"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_npl_trend,
    ),
    ReportName.DIVIDEND_REBATE_SCHEDULE: ReportDefinition(
        name=ReportName.DIVIDEND_REBATE_SCHEDULE,
        title="Dividend & rebate schedule",
        columns=(
            ReportColumn("member_no", "Member No"),
            ReportColumn("member_name", "Member", pii=True),
            ReportColumn("share_basis", "Avg Share Balance"),
            ReportColumn("dividend_rate_pct", "Dividend %"),
            ReportColumn("dividend", "Dividend"),
            ReportColumn("deposit_basis", "Avg Deposit Balance"),
            ReportColumn("rebate_rate_pct", "Rebate %"),
            ReportColumn("rebate", "Rebate"),
            ReportColumn("total", "Total"),
            ReportColumn("txn_ref", "Reference"),
        ),
        allowed_filters=frozenset({"declaration_id"}),
        required_filters=frozenset({"declaration_id"}),
        build=_build_dividend_rebate_schedule,
    ),
    ReportName.MEMBER_EXIT_STATEMENT: ReportDefinition(
        name=ReportName.MEMBER_EXIT_STATEMENT,
        title="Member exit statement",
        columns=(
            ReportColumn("exit_id", "Exit"),
            ReportColumn("member_no", "Member No"),
            ReportColumn("member_name", "Member", pii=True),
            ReportColumn("member_status", "Member Status"),
            ReportColumn("exit_status", "Exit Status"),
            ReportColumn("reason", "Reason"),
            ReportColumn("shares", "Shares"),
            ReportColumn("deposits", "Deposits"),
            ReportColumn("equity", "Equity"),
            ReportColumn("loan_balance", "Loan Balance"),
            ReportColumn("fees", "Fees"),
            ReportColumn("net_payable", "Net Payable"),
            ReportColumn("requested_at", "Requested At"),
            ReportColumn("decided_at", "Decided At"),
            ReportColumn("settled_at", "Settled At"),
            ReportColumn("settlement_txn_ref", "Settlement Ref"),
        ),
        allowed_filters=frozenset({"exit_id"}),
        required_filters=frozenset({"exit_id"}),
        build=_build_member_exit_statement,
    ),
}
