"""Quarterly deposit-interest accrual job.

Runs per tenant in bounded id-keyset batches through the shared
batch runner (each batch its own short transaction, scalability). The
caller injects a session scope (e.g. functools.partial(tenant_session,
factory, tenant_id)) so this module stays free of infrastructure
imports (arrears precedent).

Interest basis (review findings 1 and 14): interest is computed from
the **average daily balance (ADB)** over the accrued period, never
from today's balance and never from a single point-in-time snapshot.
A period-end snapshot basis would still let a member deposit on the
last day of the quarter, collect a full quarter's interest and
withdraw the next day; the ADB pays that deposit exactly 1/N of the
quarter (N = days in the period). The daily balances are reconstructed
authoritatively from the ledger under the account row lock: every
deposit-balance change posts a member.deposits leg through the P7
contract, so

    balance_at_period_end = current_balance
                            - net(member.deposits legs after the period)

and walking backwards day by day from period.end to period.start,
undoing each day's net in-period movement, yields every end-of-day
balance; the basis is the cent-rounded mean of those (positive-clamped)
balances. A deposit made after the period ended earns nothing for that
period; an account emptied after the period ended still earns what its
in-period balances deserved (which is why the scan covers every
account, not only positive balances).

Rate and period (review finding 2): the annual rate comes exclusively
from tenant_settings, and the accrued period is resolved server-side
by resolve_run_parameters — the earliest quarter not yet fully
accrued, advancing one completed quarter per run, capped at the most
recent completed quarter. Callers can never supply a rate or backdate
an arbitrary period.

Idempotent at the database level (the house gates): exactly one
deposit_interest_accruals row per (tenant, account, period_start),
claimed with INSERT... ON CONFLICT DO NOTHING (no race window, no
IntegrityError mid-batch — review finding 4); rowcount 0 means a
concurrent worker claimed the period between the scan and the insert
and the account is skipped. The scan itself excludes accounts whose
accrual row for the period already exists (NOT EXISTS anti-join —
second-pass finding 16), so a fully accrued re-run scans, locks and
reconstructs nothing; the ON CONFLICT claim remains the authoritative
guard. Accruals stored at a rate different from the configured one are
surfaced as rate_mismatches by one aggregate check per run, with a
warning log, without breaking idempotency (review finding 13).
Zero-interest accounts claim their row without a posting, so re-runs
skip them too.

Accounts are read FOR UPDATE SKIP LOCKED: the INT- posting and the
balance credit happen under the same deposit-account row lock taken by
withdrawals and guarantee pledges, so interest can never interleave
with a concurrent balance change. A locked account is skipped and
picked up by a re-run, which the UNIQUE row makes safe and cheap.

The INT- posting carries occurred_at = 23:59:59.999999 UTC on the last
day of the period (review finding 5): interest is recognised at the
very end of the period it was earned in, so it sorts after that day's
real transactions in the ledger listing and is included in the next
quarter's reconstructed balance (interest compounds from the moment it
is credited).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.ledger import post_deposit_interest
from genesis.application.period_balances import average_daily_balance
from genesis.domain.deposits import (
    QuarterPeriod,
    next_quarter,
    previous_quarter,
    quarter_of,
    quarterly_interest,
)
from genesis.domain.money import ZERO, to_cents
from genesis.errors import ConflictError, InvalidInputError

logger = logging.getLogger(__name__)

#: Accounts accrued per transaction (arrears precedent: short
#: transactions, reasonable round trips).
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True)
class InterestRunResult:
    scanned: int
    posted: int
    skipped_existing: int
    rate_mismatches: int
    total_interest: Decimal
    batches: int


async def resolve_run_parameters(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    today: date | None = None,
) -> tuple[QuarterPeriod, Decimal]:
    """Resolve the (period, annual rate) the accrual job must run with.

    The rate comes exclusively from tenant_settings (least disclosure: never caller-supplied). The
    period is the earliest quarter not yet fully
    accrued, in strict order, capped at the most recent completed
    quarter:

      * no accruals yet -> the last completed quarter
      * latest accrued period has
        accounts without a row -> that period again (finish it —
                                      crash recovery, new accounts)
      * latest period fully
        claimed and older than the
        last completed quarter -> the next quarter in sequence
      * fully caught up -> the last completed quarter again
                                      (idempotent no-op re-run)

    Tenants behind by several quarters catch up one period per run, in
    order — arbitrary historical periods can never be requested.
    """
    as_of = today or datetime.now(UTC).date()
    rate_row = (
        await session.execute(
            text(
                # Explicit tenant predicate on top of RLS (defence in
                # depth, least disclosure) — money-path configuration read.
                "SELECT deposit_interest_annual_rate_pct FROM tenant_settings "
                "WHERE tenant_id = CAST(:tid AS uuid)"
            ),
            {"tid": str(tenant_id)},
        )
    ).first()
    if rate_row is None or rate_row[0] is None:
        # A settings row without a deposit rate (possible since 0017
        # made the column optional) is exactly as unconfigured as a
        # missing row — same 409, unchanged contract.
        raise ConflictError(
            "deposit interest is not configured for this tenant "
            "(tenant_settings.deposit_interest_annual_rate_pct)"
        )
    rate = Decimal(str(rate_row[0]))

    most_recent = previous_quarter(as_of)
    latest_start = (
        await session.execute(
            text(
                "SELECT MAX(period_start) FROM deposit_interest_accruals "
                "WHERE tenant_id = CAST(:tid AS uuid)"
            ),
            {"tid": str(tenant_id)},
        )
    ).scalar_one()
    if latest_start is None:
        return most_recent, rate
    latest = quarter_of(latest_start)
    if latest.start >= most_recent.start:
        return most_recent, rate
    pending = (
        await session.execute(
            text(
                "SELECT 1 FROM deposit_accounts d "
                "WHERE d.tenant_id = CAST(:tid AS uuid) AND NOT EXISTS ("
                "  SELECT 1 FROM deposit_interest_accruals a "
                "  WHERE a.tenant_id = d.tenant_id AND a.account_id = d.id "
                "  AND a.period_start = :ps"
                ") LIMIT 1"
            ),
            {"tid": str(tenant_id), "ps": latest.start},
        )
    ).first()
    if pending is not None:
        return latest, rate
    return next_quarter(latest), rate


async def _process_batch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    period: QuarterPeriod,
    annual_rate_pct: Decimal,
    after_id: uuid.UUID | None,
    batch_size: int,
) -> tuple[int, uuid.UUID | None, tuple[int, int, Decimal]]:
    """Accrue one keyset batch; returns (scanned, last_id, (posted, skipped, total)).

    The scan walks idx_deposit_accounts_keyset (tenant_id, id) over
    every account without an accrual row for the period yet — emptied
    accounts included (they still earned interest on their in-period
    average daily balance). The NOT EXISTS anti-join makes a re-run of
    a fully accrued period a cheap no-op (second-pass finding 16): no
    rows are locked, no ledger walks happen. Interest is computed and
    posted under the account row lock (concurrency safety); the accrual row is
    claimed with ON CONFLICT DO NOTHING for every scanned account —
    including zero-interest ones — so the period can never be
    double-processed even if a concurrent run claims between the scan
    and the insert (the claim stays the authoritative guard).
    """
    clause = "AND d.id > CAST(:after AS uuid) " if after_id is not None else ""
    params: dict[str, object] = {"tid": str(tenant_id), "ps": period.start, "limit": batch_size}
    if after_id is not None:
        params["after"] = str(after_id)
    rows = (
        await session.execute(
            text(
                # Static fragments chosen in code; all values are bound parameters.
                "SELECT d.id, d.member_id, d.balance FROM deposit_accounts d "  # noqa: S608
                f"WHERE d.tenant_id = CAST(:tid AS uuid) {clause}"
                "AND NOT EXISTS ("
                "  SELECT 1 FROM deposit_interest_accruals a "
                "  WHERE a.tenant_id = d.tenant_id AND a.account_id = d.id "
                "  AND a.period_start = :ps"
                ") "
                "ORDER BY d.id LIMIT :limit "
                "FOR UPDATE OF d SKIP LOCKED"
            ),
            params,
        )
    ).all()
    posted = 0
    skipped = 0
    total = ZERO
    # Interest belongs to the very end of the period it was earned in
    # (review finding 5): 23:59:59.999999 UTC on the period's last day
    # sorts after that day's real transactions and inside the period.
    posted_at = datetime.combine(period.end, time.max, tzinfo=UTC)
    for account_id_raw, member_id_raw, balance_raw in rows:
        account_id = str(account_id_raw)
        member_id = uuid.UUID(str(member_id_raw))
        balance = Decimal(str(balance_raw))
        # Average daily balance over the period (review finding 14):
        # a last-day deposit earns 1/N of the quarter, not all of it.
        # Reconstructed by the shared helper (extracted to
        # period_balances for the dividend basis, reuse-first);
        # reads run under the account row lock held by this scan.
        basis = await average_daily_balance(
            session, tenant_id, member_id, kind="deposit", start=period.start, end=period.end
        )
        interest = quarterly_interest(basis, annual_rate_pct)
        accrual_id = str(uuid.uuid4())
        claim = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    # Single atomic claim on the UNIQUE idempotency key
                    # (tenant_id, account_id, period_start): rowcount 0
                    # means already accrued — no SELECT-then-INSERT race,
                    # no IntegrityError mid-batch (review finding 4).
                    "INSERT INTO deposit_interest_accruals "
                    "(id, tenant_id, account_id, period_start, period_end, "
                    " annual_rate_pct, amount) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:a AS uuid), "
                    ":ps, :pe, :rate, :amount) "
                    "ON CONFLICT (tenant_id, account_id, period_start) DO NOTHING"
                ),
                {
                    "id": accrual_id,
                    "tid": str(tenant_id),
                    "a": account_id,
                    "ps": period.start,
                    "pe": period.end,
                    "rate": str(annual_rate_pct),
                    "amount": str(interest),
                },
            ),
        )
        if claim.rowcount == 0:
            # A concurrent run claimed this account between the
            # NOT EXISTS scan and the insert; idempotency wins. Rate
            # drift is surfaced by the per-run aggregate check
            # (findings 13 + 16), not per row.
            skipped += 1
            continue
        if interest > ZERO:
            posting = await post_deposit_interest(
                session, tenant_id, member_id, interest, None, occurred_at=posted_at
            )
            balance_after = to_cents(balance + interest)
            await session.execute(
                text(
                    "UPDATE deposit_accounts SET balance = :bal, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"bal": str(balance_after), "id": account_id, "tid": str(tenant_id)},
            )
            await session.execute(
                text(
                    "UPDATE deposit_interest_accruals SET transaction_id = CAST(:txn AS uuid) "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"txn": str(posting.txn_id), "id": accrual_id, "tid": str(tenant_id)},
            )
            await record_audit(
                session,
                tenant_id,
                None,
                action="deposit_account.interest",
                entity="deposit_accounts",
                entity_id=account_id,
                before={"balance": str(balance)},
                after={
                    "balance": str(balance_after),
                    "basis": str(basis),
                    "txn_ref": posting.txn_ref,
                    "period_start": period.start.isoformat(),
                },
            )
            posted += 1
            total += interest
    last_id = uuid.UUID(str(rows[-1][0])) if rows else None
    return len(rows), last_id, (posted, skipped, total)


async def _count_rate_mismatches(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    period: QuarterPeriod,
    annual_rate_pct: Decimal,
) -> int:
    """Accruals stored for the period at a rate other than the configured one.

    One aggregate query per run (findings 13 + 16): the NOT EXISTS scan
    no longer visits already-accrued accounts, so rate drift is checked
    against the accrual table directly instead of per skipped row.
    Idempotency still wins — mismatched rows are never re-posted, only
    surfaced.
    """
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM deposit_interest_accruals "
                "WHERE tenant_id = CAST(:tid AS uuid) AND period_start = :ps "
                "AND annual_rate_pct <> CAST(:rate AS numeric)"
            ),
            {"tid": str(tenant_id), "ps": period.start, "rate": str(annual_rate_pct)},
        )
    ).scalar_one()
    return int(count)


async def run_deposit_interest_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    period: QuarterPeriod,
    annual_rate_pct: Decimal,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> InterestRunResult:
    """Accrue one completed quarter for every deposit account.

    period and annual_rate_pct must come from resolve_run_parameters
    (tenant configuration + strict period ordering) — the API never
    forwards caller-supplied values.
    """
    # Validate the rate once up front (domain rule), not per account,
    # translated into the shared error taxonomy so non-API callers get
    # a mapped 4xx instead of an unhandled ValueError (reliability).
    try:
        quarterly_interest(ZERO, annual_rate_pct)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    process = partial(
        _process_one,
        tenant_id=tenant_id,
        period=period,
        annual_rate_pct=annual_rate_pct,
        batch_size=batch_size,
    )
    scanned, batches, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    posted = sum(p[0] for p in payloads)
    skipped = sum(p[1] for p in payloads)
    total = sum((p[2] for p in payloads), ZERO)
    async with session_scope() as session:
        mismatches = await _count_rate_mismatches(
            session, tenant_id, period=period, annual_rate_pct=annual_rate_pct
        )
    if mismatches:
        logger.warning(
            "deposit interest accruals stored at a different rate: tenant=%s "
            "period_start=%s configured_rate=%s count=%d",
            tenant_id,
            period.start,
            annual_rate_pct,
            mismatches,
        )
    return InterestRunResult(
        scanned=scanned,
        posted=posted,
        skipped_existing=skipped,
        rate_mismatches=mismatches,
        total_interest=total,
        batches=batches,
    )


async def _process_one(
    session: AsyncSession,
    after_id: uuid.UUID | None,
    *,
    tenant_id: uuid.UUID,
    period: QuarterPeriod,
    annual_rate_pct: Decimal,
    batch_size: int,
) -> tuple[int, uuid.UUID | None, tuple[int, int, Decimal]]:
    """Adapter matching the shared BatchProcessor signature."""
    return await _process_batch(
        session,
        tenant_id,
        period=period,
        annual_rate_pct=annual_rate_pct,
        after_id=after_id,
        batch_size=batch_size,
    )
