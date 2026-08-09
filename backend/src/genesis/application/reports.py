"""Report definitions and queries for exports (the house gates).

Each report declares its columns (with per-column PII entitlement, blocker e), its permitted
filters, and a builder that binds the
report query to a session/tenant/filter set. The export engine
(genesis.application.exports.run_export) drives every report through
keyset batches with a hard server-side row cap (blocker d) —
never OFFSET in SQL, never an unbounded scan. Aggregate reports
(trial balance, NPL trend, exit statement) have cardinality bounded
by construction (chart-of-accounts size / configured month count /
one document) and are computed once, then paged from memory.

Every read carries an explicit bound tenant_id predicate on top of
forced RLS (blocker c; least disclosure v1.1). Money figures are computed
from the append-only ledger or server-maintained balances — never from
request input (blocker a).

The SQL builder functions are module-level so the EXPLAIN test
captures plans for the exact statements this module executes
(blocker j).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import dividends as dividends_service
from genesis.application import member_exits as exits_service
from genesis.application.members import get_member
from genesis.application.period_rollups import (
    TRIAL_BALANCE_ROLLUP_SQL,
    account_activity_sql,
    member_balance_before,
    member_movement_sql,
)
from genesis.application.portfolio_reconstruction import (
    MonthPortfolio,
    npl_trend_month_ends,
    reconstruct_month,
)
from genesis.application.portfolio_snapshots import SNAPSHOT_LOOKUP_SQL
from genesis.domain.documents import Cell
from genesis.domain.ledger import (
    DEBIT_NORMAL_CLASSES,
    Account,
    AccountClass,
    Side,
    TxnType,
    account_class,
    member_direction,
    signed_balance,
)
from genesis.domain.money import ZERO, to_cents
from genesis.domain.sasra import SASRA_LINES, SASRA_RETURN_VERSION, line_for_account
from genesis.errors import InvalidInputError, UnprocessableError
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
    PORTFOLIO_AT_RISK_AGING = "portfolio_at_risk_aging"
    MEMBERSHIP_REGISTER = "membership_register"
    INCOME_STATEMENT = "income_statement"
    SASRA_RETURN = "sasra_return"


@dataclass(frozen=True)
class ReportColumn:
    """One output column; pii columns require members:view (blocker e)."""

    key: str
    title: str
    pii: bool = False


@dataclass(frozen=True)
class ExportFilters:
    """The only caller-suppliable report scope (blocker a).

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
    """Reject scopes the report does not define (least surprise, least disclosure)."""
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
    services (get_member / the get_exit), so a foreign tenant's id
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
    (blocker d): the underlying statements ran exactly once with
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
    """Keyset page of one member's transactions, oldest first (scalability).

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
    via the domain single source of truth (member_direction) in
    Python — the DR/CR convention is never duplicated in SQL. Since
    (b) this delegates to period_rollups.member_movement_sql
    (byte-identical output for these configurations), the SAME
    statement the DSA-5 rollup writer and the anchored opening run —
    no dual-maintained math (reuse-first).
    """
    return member_movement_sql(with_from=with_from, with_start=False)


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
        # (b) / DSA-5: anchored on the member's latest rolled
        # period when one exists (delta scan only), else the
        # full-history scan — same figures either way (equality
        # gate, tests/test_p1317_period_rollups.py).
        opening = await member_balance_before(
            session,
            tenant_id,
            member_id,
            before=filters.date_from,
            as_of=as_of,
        )

    params: dict[str, object] = {
        "tid": str(tenant_id),
        "mid": str(member_id),
        "as_of": as_of,
    }
    # UTC period contract (the R3 convention, extended to the
    # statement window in.17b): bind explicit UTC midnights — a
    # bare date is promoted to midnight of the SESSION TimeZone, which
    # would shift the window (and the anchored opening's boundary)
    # under any non-UTC session. Identical output under UTC sessions.
    if filters.date_from is not None:
        params["d_from"] = datetime.combine(filters.date_from, time.min, tzinfo=UTC)
    if filters.date_to is not None:
        params["d_to_excl"] = datetime.combine(
            filters.date_to + timedelta(days=1), time.min, tzinfo=UTC
        )

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
    # sum(credits) - sum(debits) under the member_direction
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

#: Full-scan aggregate over the append-only ledger as of the export
#: instant. Since (b) the builder reads TRIAL_BALANCE_ROLLUP_SQL
#: (closed rollups + live remainder); this statement is RETAINED as the
#: reconstruction ORACLE the equality gate compares against
#: (tests/test_p1317_period_rollups.py) — it is the pre-existing math,
#: unchanged. Bounded by the chart-of-accounts cardinality. Served by
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
    """Closed rollups + live remainder (.17b / DSA-2): equal to the
    full scan to the cent (merge gate) — the live CTE covers every
    posting not inside a rolled closed period, so tenants without
    closed periods render exactly as before."""
    raw = (
        await session.execute(
            text(TRIAL_BALANCE_ROLLUP_SQL), {"tid": str(tenant_id), "as_of": as_of}
        )
    ).all()
    rows: list[tuple[Cell, ...]] = []
    total_debits = ZERO
    total_credits = ZERO
    for account, debits_raw, credits_raw in raw:
        # Canonical cents (.17b): a zero coming from the rolled CTE
        # is numeric '0.00' while the live CTE's integer COALESCE is
        # '0' — quantizing makes the rendered artifact byte-identical
        # whichever path served an account (values unchanged; the
        # equality tests compare the full rendered document).
        debits = to_cents(Decimal(str(debits_raw)))
        credits = to_cents(Decimal(str(credits_raw)))
        total_debits += debits
        total_credits += credits
        rows.append((str(account), debits, credits))
    rows.append(("TOTAL", total_debits, total_credits))
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# Loan book (classification & provisions)
# ---------------------------------------------------------------------------


def loan_book_page_sql(*, with_cursor: bool) -> str:
    """Keyset page over the loan book, newest first (scalability).

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
    """Keyset page over disbursement/repayment transactions (scalability).

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

#: The month reconstruction (NPL_TREND_MONTH_SQL) and the month-end
#: walk moved to application/portfolio_reconstruction.py in.17a —
#: the single source of truth shared with the DSA-1 snapshot writer
#: (reuse-first: the math is never dual-maintained).


async def _build_npl_trend(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    """Snapshots + current month only (.17a / DSA-1).

    Fully elapsed months are served from portfolio_month_snapshots —
    written once at close_period or by the backfill job with the SAME
    reconstruction statement this builder used to run per month, so
    the rendered figures are unchanged to the cent (equality property,
    tests/test_p1317_portfolio_snapshots.py). A month whose
    snapshot does not exist yet (backfill not run, period never
    closed) falls back to the reconstruction — identical output,
    documented cost. Only the current, incomplete month is always
    reconstructed (its cutoff is the as-of instant, never a stored
    figure).
    """
    months = get_settings().export_npl_trend_months
    month_ends = npl_trend_month_ends(as_of, months)
    # Every entry except the last is a fully elapsed calendar month end
    # (the walk puts the as-of day last); only those may be served from
    # snapshots.
    complete = [end for end in month_ends[:-1] if end < as_of.astimezone(UTC).date()]
    snapshots: dict[date, MonthPortfolio] = {}
    if complete:
        raw_rows = (
            await session.execute(
                text(SNAPSHOT_LOOKUP_SQL),
                {"tid": str(tenant_id), "months": complete},
            )
        ).all()
        snapshots = {
            row[0]: MonthPortfolio(
                month_end=row[0],
                loans=int(row[1]),
                gross_outstanding=Decimal(str(row[2])),
                npl_loans=int(row[3]),
                npl_balance=Decimal(str(row[4])),
            )
            for row in raw_rows
        }
    rows: list[tuple[Cell, ...]] = []
    for month_end in month_ends:
        month = snapshots.get(month_end)
        if month is None:
            month = await reconstruct_month(session, tenant_id, month_end, as_of=as_of)
        gross = month.gross_outstanding
        npl_balance = month.npl_balance
        ratio = to_cents(npl_balance * Decimal("100") / gross) if gross > ZERO else Decimal("0.00")
        rows.append(
            (
                f"{month_end.year:04d}-{month_end.month:02d}",
                gross,
                npl_balance,
                month.npl_loans,
                ratio,
            )
        )
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# Member exit statement
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
    # The JSON statement service stays the canonical data source
    # the export renders the same document verbatim.
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
# Dividend & rebate schedule (/ catalogue entry)
# ---------------------------------------------------------------------------


def dividend_schedule_page_sql(*, with_cursor: bool) -> str:
    """Keyset page over one declaration's distribution claims (scalability).

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
        "d.disposition, d.created_at "
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
        return (raw[12], str(raw[0]))

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
            # Disposition (P3): 'paid' or 'unclaimed' — the
            # report is part of the unclaimed-payable alert surface.
            str(raw[11]),
        )

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Portfolio-at-risk aging
# ---------------------------------------------------------------------------

#: PAR aging buckets over WHOLE-DAY days-past-due (Postgres date
#: subtraction yields integer days). "current" is its OWN bucket
#: a loan with NO unmet installment at the cutoff
#: has dpd 0 and is performing — lumping it into an arrears bucket
#: would make the PAR>0 / PAR30 ratios underivable from the report
#: (SASRA, WOCCU PEARLS P1 and CGAP all report current separately).
#: The buckets are CLOSED integer intervals matching the domain
#: classify() thresholds (30/90/180/360):
#:   [0, 0], [1, 30], [31, 90], [91, 180], [181, 360], [361, +inf)
#: i.e. dpd 0 plus the half-open real intervals (0, 30], (30, 90],
#: (90, 180], (180, 360], (360, +inf) expressed on integer days.
#: Boundary oracles: dpd 0 -> "current", 1 -> "1-30", 30 -> "1-30",
#: 31 -> "31-90", 90 -> "31-90", 91 -> "91-180", 180 -> "91-180",
#: 181 -> "181-360", 360 -> "181-360", 361 -> "360+".
PAR_BUCKET_LABELS: tuple[str, ...] = (
    "current",
    "1-30",
    "31-90",
    "91-180",
    "181-360",
    "360+",
)

#: One as-of snapshot of the loan book bucketed by days past due,
#: reconstructed ENTIRELY from the append-only record (
#: the NPL-trend method; never loans.balance / days_past_due /
#: classification, which are mutable state):
#:   * outstanding principal per loan = disbursed principal minus the
#:     loans.receivable credit legs of its repayments up to as-of
#:     (apportioned per repayment row — see the CTE comment);
#:   * days past due = as-of date minus the earliest installment whose
#:     cumulative schedule due exceeds the cash repaid by as-of.
#: Loans closed on or before as-of are excluded (terminal postings
#: zeroed them). written_off stays OUT of the buckets — the /
#: write-off flow derecognises the receivable (CR loans.receivable),
#: so the claim is no longer portfolio outstanding — but the excluded
#: exposure is DISCLOSED as an explicit memo row (the correction
#: note, 3646076743): written-off exposure must never silently vanish
#: from the report; recoveries against it are tracked as income
#:  and never resurrect the receivable.
#: Cardinality bounded by construction: at most 6 bucket rows.
PAR_AGING_SQL = """
WITH paid AS (
    SELECT r.loan_id, COALESCE(SUM(r.amount), 0) AS paid
    FROM repayments r
    JOIN transactions t ON t.id = r.transaction_id AND t.tenant_id = r.tenant_id
    WHERE r.tenant_id = CAST(:tid AS uuid) AND t.occurred_at <= :as_of
    GROUP BY r.loan_id
),
principal_paid AS (
    -- Principal attribution WITHOUT join fan-out (review-hardened):
    -- repayments.transaction_id carries NO DB-level UNIQUE (0001
    -- ships only the FK; 0014 only a NON-unique index), so joining
    -- ledger legs to repayments on transaction_id alone would
    -- attribute every loans.receivable credit leg to EVERY repayment
    -- row sharing the transaction, silently overstating principal
    -- paid on all of the joined loans. Instead each transaction's
    -- receivable credit legs are summed ONCE (tp) and apportioned
    -- across that transaction's repayment rows pro-rata by repayment
    -- amount (tr), keyed on the repayment row itself — the attributed
    -- total equals the legs' total no matter how many repayment rows
    -- share one transaction.
    SELECT r.loan_id,
           COALESCE(SUM(tp.principal * r.amount / tr.repaid), 0) AS principal_paid
    FROM repayments r
    JOIN transactions t ON t.id = r.transaction_id AND t.tenant_id = r.tenant_id
    JOIN (
        SELECT le.transaction_id, SUM(le.amount) AS principal
        FROM ledger_entries le
        WHERE le.tenant_id = CAST(:tid AS uuid)
          AND le.account = :receivable_account AND le.side = 'credit'
        GROUP BY le.transaction_id
    ) tp ON tp.transaction_id = r.transaction_id
    JOIN (
        -- repayments.amount CHECK (amount > 0) in 0001 => repaid > 0.
        SELECT r2.transaction_id, SUM(r2.amount) AS repaid
        FROM repayments r2
        WHERE r2.tenant_id = CAST(:tid AS uuid)
        GROUP BY r2.transaction_id
    ) tr ON tr.transaction_id = r.transaction_id
    WHERE r.tenant_id = CAST(:tid AS uuid) AND t.occurred_at <= :as_of
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
),
loan_state AS (
    SELECT (l.principal - COALESCE(pp.principal_paid, 0)) AS outstanding,
           CASE WHEN fu.due IS NULL THEN 0 ELSE (:d_date - fu.due) END AS dpd
    FROM loans l
    LEFT JOIN principal_paid pp ON pp.loan_id = l.id
    LEFT JOIN first_unmet fu ON fu.loan_id = l.id
    WHERE l.tenant_id = CAST(:tid AS uuid)
      AND l.status <> 'written_off'
      AND l.disbursed_at <= :as_of
      AND (l.closed_at IS NULL OR l.closed_at > :as_of)
)
SELECT CASE WHEN dpd = 0 THEN 0
            WHEN dpd <= 30 THEN 1
            WHEN dpd <= 90 THEN 2
            WHEN dpd <= 180 THEN 3
            WHEN dpd <= 360 THEN 4
            ELSE 5 END AS bucket,
       COUNT(*) AS loans,
       COALESCE(SUM(outstanding), 0) AS outstanding
FROM loan_state
GROUP BY 1
ORDER BY 1
"""

#: The written-off disclosure behind the PAR memo row: count and total
#: of committee-approved write-offs POSTED on or before as-of, read
#: from the write-once loan_write_offs snapshot (append-only record,
#:  — total_written_off is the claim derecognised at
#: posting time; recoveries are income and never reduce it). The
#: bucket predicate excludes on the loans row's CURRENT status (a
#: write-off posted after as-of hides the loan from a historical
#: as-of render — the pre-existing, in-file-documented semantics);
#: this memo is as-of-consistent on posted_at, so the disclosed set
#: is exactly the write-offs that had happened by the snapshot date.
#: Tenant-led read served by the idx_loan_write_offs_loan prefix
#: (0025) — no new index (EXPLAIN captured in test_p1310_explain).
PAR_WRITTEN_OFF_MEMO_SQL = """
SELECT COUNT(*) AS loans, COALESCE(SUM(total_written_off), 0) AS written_off
FROM loan_write_offs
WHERE tenant_id = CAST(:tid AS uuid)
  AND status = 'posted'
  AND posted_at <= :as_of
"""


async def _build_par_aging(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    """As-of semantics: ONE statement against the worker's REPEATABLE
    READ snapshot; dpd measured to the as-of calendar date (UTC)."""
    raw = (
        await session.execute(
            text(PAR_AGING_SQL),
            {
                "tid": str(tenant_id),
                "as_of": as_of,
                "d_date": as_of.astimezone(UTC).date(),
                # Identifier from the code-owned chart.
                "receivable_account": Account.LOANS_RECEIVABLE.value,
            },
        )
    ).all()
    by_bucket: dict[int, tuple[int, Decimal]] = {
        int(bucket): (int(loans), Decimal(str(outstanding))) for bucket, loans, outstanding in raw
    }
    total_loans = sum(loans for loans, _ in by_bucket.values())
    total_outstanding = to_cents(sum((outstanding for _, outstanding in by_bucket.values()), ZERO))
    buckets: list[tuple[str, int, Decimal]] = []
    for index, label in enumerate(PAR_BUCKET_LABELS):
        loans, outstanding = by_bucket.get(index, (0, ZERO))
        buckets.append((label, loans, to_cents(outstanding)))
    # '% of Portfolio' must FOOT: rounding every
    # bucket share independently with to_cents can print a column
    # summing to 99.99/100.01 against a TOTAL row of 100.00 (three
    # equal buckets -> 33.33 * 3 = 99.99). Largest-remainder
    # discipline: every share rounds via to_cents, then the LARGEST
    # bucket (the first such index on ties — deterministic) is
    # assigned 100.00 minus the sum of the others, so the printed
    # column sums to exactly 100.00 by construction.
    if total_outstanding > ZERO:
        shares = [
            to_cents(outstanding * Decimal("100") / total_outstanding)
            for _, _, outstanding in buckets
        ]
        largest = max(range(len(buckets)), key=lambda i: (buckets[i][2], -i))
        shares[largest] = Decimal("100.00") - sum(
            (share for i, share in enumerate(shares) if i != largest), ZERO
        )
    else:
        shares = [Decimal("0.00")] * len(buckets)
    rows: list[tuple[Cell, ...]] = [
        (label, loans, outstanding, share)
        for (label, loans, outstanding), share in zip(buckets, shares, strict=True)
    ]
    rows.append(
        (
            "TOTAL",
            total_loans,
            total_outstanding,
            Decimal("100.00") if total_outstanding > ZERO else Decimal("0.00"),
        )
    )
    # Written-off disclosure (the correction note): the exposure
    # the bucket predicate excludes, as an explicit MEMO row after
    # TOTAL — count + the derecognised claim from the write-once
    # snapshot. It deliberately does NOT foot into TOTAL or the
    # '% of Portfolio' column (the claim left the portfolio at
    # posting; its share of the ACTIVE portfolio is 0.00 by
    # definition) — prudential visibility without double-counting.
    memo = (
        await session.execute(
            text(PAR_WRITTEN_OFF_MEMO_SQL),
            {"tid": str(tenant_id), "as_of": as_of},
        )
    ).one()
    rows.append(
        (
            "WRITTEN OFF (memo)",
            int(memo[0]),
            to_cents(Decimal(str(memo[1]))),
            Decimal("0.00"),
        )
    )
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# Membership register
# ---------------------------------------------------------------------------


def membership_register_page_sql(*, with_cursor: bool) -> str:
    """Keyset page over the members register, admission order (scalability).

    Served by idx_members_register_keyset (tenant_id, created_at, id;
    0023 — shipped with this query). Renders the FULL current status
    vocabulary (active/arrears/dormant/exited) verbatim from the
    status column. Static fragments chosen in code; all values are
    bound parameters.
    """
    cursor_clause = "AND (created_at, id) > (:c_ts, CAST(:c_id AS uuid)) " if with_cursor else ""
    return (
        "SELECT id, member_no, name, type, phone, email, status, created_at "  # noqa: S608
        "FROM members "
        "WHERE tenant_id = CAST(:tid AS uuid) AND created_at <= :as_of "
        f"{cursor_clause}"
        "ORDER BY created_at, id LIMIT :limit"
    )


async def _build_membership_register(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    """As-of semantics: members admitted up to the snapshot as-of, in
    (created_at, id) keyset order; the snapshot transaction guarantees
    one consistent register (blocker h)."""

    async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
        params: dict[str, object] = {"tid": str(tenant_id), "as_of": as_of, "limit": limit}
        if cursor is not None:
            params["c_ts"], params["c_id"] = cast(tuple[datetime, str], cursor)
        page_sql = membership_register_page_sql(with_cursor=cursor is not None)
        result = await session.execute(text(page_sql), params)
        return list(result.all())

    def cursor_key(raw: Any) -> ReportCursor:
        return (raw[7], str(raw[0]))

    def to_cells(raw: Any) -> tuple[Cell, ...]:
        return (
            str(raw[1]),
            str(raw[2]),
            str(raw[3]),
            str(raw[4]) if raw[4] is not None else None,
            str(raw[5]) if raw[5] is not None else None,
            str(raw[6]),
            raw[7],
        )

    return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


# ---------------------------------------------------------------------------
# Income statement
# ---------------------------------------------------------------------------


#: account_activity_sql moved VERBATIM to
#: application/period_rollups.py in (b) — the single aggregate
#: behind the income statement, the SASRA return AND the close_period
#: account rollups (reuse-first) — and is imported above.


async def _account_activity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> dict[str, tuple[Decimal, Decimal]]:
    """(debits, credits) per account string over the filter period."""
    params: dict[str, object] = {"tid": str(tenant_id), "as_of": as_of}
    # UTC period contract: date_from/date_to are UTC
    # calendar dates. Bind explicit UTC datetimes — a bare Python date
    # compared against occurred_at (timestamptz) is promoted by
    # Postgres to midnight of the SESSION TimeZone, so the period
    # boundary would silently shift under any non-UTC session. The
    # upper bound stays half-open at < date_to + 1 day (inclusive
    # calendar dates, H5c — semantics unchanged).
    if filters.date_from is not None:
        params["d_from"] = datetime.combine(filters.date_from, time.min, tzinfo=UTC)
    if filters.date_to is not None:
        params["d_to_excl"] = datetime.combine(
            filters.date_to + timedelta(days=1), time.min, tzinfo=UTC
        )
    raw = (
        await session.execute(
            text(
                account_activity_sql(
                    with_from=filters.date_from is not None,
                    with_to=filters.date_to is not None,
                )
            ),
            params,
        )
    ).all()
    activity: dict[str, tuple[Decimal, Decimal]] = {}
    for account, side, amount_raw in raw:
        debits, credits = activity.get(str(account), (ZERO, ZERO))
        amount = Decimal(str(amount_raw))
        if str(side) == Side.DEBIT.value:
            debits += amount
        else:
            credits += amount
        activity[str(account)] = (debits, credits)
    return activity


async def _build_income_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    """P&L grouping over the income/expense chart accounts.

    Period semantics (explicit, documented): date_from/date_to are
    INCLUSIVE calendar dates evaluated against transactions.occurred_at
    (from-midnight to end-of-day via a half-open < date_to + 1 day
    bound); a missing bound defaults to inception / the snapshot as-of;
    every leg is additionally capped at occurred_at <= as_of, so the
    statement can never claim activity after its own cutoff.

    Reversal-aware by construction: a reversing entry posts the SAME
    accounts on the OPPOSITE sides, so credits-minus-debits (income) /
    debits-minus-credits (expense) nets original and reversal exactly.

    Fail-loud coverage (data integrity): every account with activity must be
    covered by the code-owned ACCOUNT_CLASS map; an unknown account
    raises (the export job is marked failed) instead of silently
    dropping a P&L line.
    """
    activity = await _account_activity(session, tenant_id, filters, as_of)
    income: list[tuple[str, Decimal]] = []
    expense: list[tuple[str, Decimal]] = []
    for account in sorted(activity):
        debits, credits = activity[account]
        try:
            cls = account_class(account)
        except ValueError as exc:
            # Least disclosure: the account identifier only, no figures.
            raise UnprocessableError(str(exc)) from exc
        if cls is AccountClass.INCOME:
            income.append((account, to_cents(credits - debits)))
        elif cls is AccountClass.EXPENSE:
            expense.append((account, to_cents(debits - credits)))
    total_income = to_cents(sum((amount for _, amount in income), ZERO))
    total_expense = to_cents(sum((amount for _, amount in expense), ZERO))
    rows: list[tuple[Cell, ...]] = []
    rows.extend(("income", account, amount) for account, amount in income)
    rows.append(("income", "TOTAL INCOME", total_income))
    rows.extend(("expense", account, amount) for account, amount in expense)
    rows.append(("expense", "TOTAL EXPENSE", total_expense))
    rows.append(("net", "NET SURPLUS", to_cents(total_income - total_expense)))
    return _memory_query(rows)


# ---------------------------------------------------------------------------
# SASRA return skeleton
# ---------------------------------------------------------------------------


async def _build_sasra_return(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: ExportFilters,
    as_of: datetime,
) -> ReportQuery:
    """Trial balance mapped to the regulator's line items.

    The mapping is VERSIONED and code-owned (genesis.domain.sasra —
    v1.1 rules 1/6: never caller-supplied); every rendered row carries
    the version tag. As-of semantics: cumulative balances at the
    snapshot as-of (the trial-balance convention).

    Fail-loud coverage (data integrity): any ledger account with activity
    that the versioned mapping does not cover raises (the export job
    is marked failed) — a future account can never silently vanish
    from a filed return.
    """
    activity = await _account_activity(session, tenant_id, ExportFilters(), as_of)
    line_totals: dict[str, Decimal] = {line.code: ZERO for line in SASRA_LINES}
    residual = ZERO
    for account, (debits, credits) in activity.items():
        try:
            code = line_for_account(account)
            signed = signed_balance(account, debits, credits)
            debit_normal = account_class(account) in DEBIT_NORMAL_CLASSES
        except ValueError as exc:
            # Least disclosure: the account identifier only, no figures.
            raise UnprocessableError(str(exc)) from exc
        line_totals[code] += signed
        # The double-entry identity on natural-sign balances:
        # assets + expenses + clearing == liabilities + equity + income
        # (every posting balances, so total DR == total CR). The
        # residual is debit-normal MINUS credit-normal and must be 0.
        residual += signed if debit_normal else -signed
    rows: list[tuple[Cell, ...]] = [
        (SASRA_RETURN_VERSION, line.code, line.title, to_cents(line_totals[line.code]))
        for line in SASRA_LINES
    ]
    # Control row: a non-zero value is a filing stopper.
    rows.append(
        (
            SASRA_RETURN_VERSION,
            "CHK1",
            "Balance check (debit-normal minus credit-normal, must be zero)",
            to_cents(residual),
        )
    )
    return _memory_query(rows)


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
            ReportColumn("disposition", "Disposition"),
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
    ReportName.PORTFOLIO_AT_RISK_AGING: ReportDefinition(
        name=ReportName.PORTFOLIO_AT_RISK_AGING,
        title="Portfolio at risk — aging",
        columns=(
            ReportColumn("bucket", "Bucket (days past due)"),
            ReportColumn("loans", "Loans"),
            ReportColumn("outstanding", "Outstanding"),
            ReportColumn("pct_of_portfolio", "% of Portfolio"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_par_aging,
    ),
    ReportName.MEMBERSHIP_REGISTER: ReportDefinition(
        name=ReportName.MEMBERSHIP_REGISTER,
        title="Membership register",
        columns=(
            ReportColumn("member_no", "Member No"),
            ReportColumn("member_name", "Member", pii=True),
            ReportColumn("type", "Type"),
            ReportColumn("phone", "Phone", pii=True),
            ReportColumn("email", "Email", pii=True),
            ReportColumn("status", "Status"),
            ReportColumn("joined_at", "Joined At"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_membership_register,
    ),
    ReportName.INCOME_STATEMENT: ReportDefinition(
        name=ReportName.INCOME_STATEMENT,
        title="Income statement",
        columns=(
            ReportColumn("section", "Section"),
            ReportColumn("line", "Line"),
            ReportColumn("amount", "Amount"),
        ),
        allowed_filters=frozenset({"date_from", "date_to"}),
        required_filters=frozenset(),
        build=_build_income_statement,
    ),
    ReportName.SASRA_RETURN: ReportDefinition(
        name=ReportName.SASRA_RETURN,
        title="SASRA return (skeleton)",
        columns=(
            ReportColumn("version", "Return Version"),
            ReportColumn("line_code", "Line Code"),
            ReportColumn("line_item", "Line Item"),
            ReportColumn("amount", "Amount"),
        ),
        allowed_filters=frozenset(),
        required_filters=frozenset(),
        build=_build_sasra_return,
    ),
}
