"""Nightly arrears job: days-past-due -> classification -> provisioning
and penalty-on-arrears accrual.

Runs per tenant in bounded batches through the shared batch runner
(genesis.application.batch_runner), each batch inside its own short
transaction (scalability: no long transactions). The caller injects a
session scope (e.g. functools.partial(tenant_session, factory, tenant_id))
so this module stays free of infrastructure imports; the worker or an
admin endpoint owns scheduling.

Idempotent by construction: a loan is written only when its computed
(days_past_due, classification, provision_pct) differs from what is
stored, so re-running for the same as_of date changes nothing.

Classification thresholds and provision percentages come exclusively
from genesis.domain.lending.classify (reuse-first: single source of truth).
Derived prudential fields do not bump the optimistic version — like
cover_pct on applications, the job must never invalidate a concurrent
edit (documented precedent from P9).

Penalty accrual extends this same job path — same scan, same
loan row lock, same short batch transactions:

  * Config comes EXCLUSIVELY from the tenant settings
    (penalty_rate_pct_per_month, penalty_grace_days,
    penalty_charged_on; 0017 columns) resolved once per run (v1.1
    rule 1: never caller-supplied, no hardcoded default rate). A tenant
    missing ANY of the three keys accrues NOTHING (skipped, fail
    closed); corrupted stored values raise 409, the precedent.
  * The accrual PERIOD is one UTC calendar day — the job's as_of date,
    the same day boundary the days-past-due derivation below already
    uses. Each run accrues at most the as-of day for each loan,
    write-once via the penalty_accruals (tenant, loan, accrual_date)
    claim: INSERT... ON CONFLICT DO NOTHING checked by rowcount (v1.1
    rule 5). Re-runs and concurrent double-runs accrue nothing new
    (a NOT EXISTS anti-join keeps re-runs cheap; the claim stays the
    authoritative guard).
  * The basis is reconstructed from the SCHEDULE and its repayment
    bookkeeping under the loan row lock — instalment_in_arrears sums
    total_due - paid_amount over unpaid instalments due on or before
    as_of (paid_amount's only writer is record_repayment, under this
    same lock); full_outstanding is the principal receivable. NEITHER
    basis includes loans.penalty_due, so penalty never compounds on
    penalty (failure mode 4).
  * Grace boundary (failure mode 2): a loan accrues only when
    days_past_due > penalty_grace_days — exactly AT grace accrues
    nothing. days_past_due reuses THIS job's existing derivation
    ((as_of - oldest unpaid due date).days, UTC dates end to end, no
    timezone arithmetic); the 0019 DB CHECK days_past_due > grace_days
    makes a within-grace claim row unrepresentable.
  * Claim row + loans.penalty_due update + audit row commit in ONE
    batch transaction (failure mode 3/9): an abort mid-batch leaves no
    partial accrual state. The audit row records the exact config and
    basis figures used, so every accrued penny stays reconstructable
    after a config change; already-claimed days are never recomputed
    (failure mode 5, the quorum precedent).
  * Ledger boundary (documented per): penalty INCOME recognition
    stays ON RECEIPT — repayment allocation posts
    income.penalties when cash arrives. This job maintains ONLY the
    receivable-side loans.penalty_due and writes no ledger rows.
  * Lock ordering: the job locks LOANS ONLY (ORDER BY l.id, FOR UPDATE
    SKIP LOCKED), the exact arrears pattern. loans is the terminal
    node of the settlement chain (member -> accounts -> loans), so
    no new lock-graph edge is introduced and no cycle is possible; a
    concurrent repayment serialises on the same loan row lock (failure
    mode 8). EPQ/snapshot nuance: under READ COMMITTED the
    FOR UPDATE re-evaluation (EvalPlanQual) refreshes only the locked
    loans tuple (balance, penalty_due), while the correlated schedule
    subqueries (oldest_unpaid, arrears_amount) evaluate against the
    statement snapshot — a repayment committing between statement
    start and lock acquisition of a row can yield a dpd/basis computed
    from pre-repayment schedule state. Bounded to that one day's
    accrual and fully reconstructable from the claim row; "a repayment
    that cures the arrears before the job runs -> no accrual" is a
    snapshot-level guarantee, not a lock-level one.

Recovery auto-close: after the classify/accrue batches, the
recovery close pass (application/recovery) closes open recovery cases
whose loan has left NPL — keyed off the classification THIS job just
persisted (single source of truth, 1.1; addendum A6), locking case
rows only. See application/recovery.close_scan_sql.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.outbox import enqueue_event
from genesis.application.recovery import run_recovery_close_pass
from genesis.domain.lending import classify, daily_penalty
from genesis.domain.money import ZERO, to_cents
from genesis.domain.tenant_config import PenaltyChargedOn
from genesis.errors import ConflictError

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "PENALTY_CONFIG_SQL",
    "ArrearsRunResult",
    "PenaltyConfig",
    "SessionScope",
    "arrears_scan_sql",
    "parse_penalty_config",
    "resolve_penalty_config",
    "run_arrears_for_tenant",
]

#: Loans reclassified per transaction. Small enough to keep each
#: transaction short; large enough to keep round trips reasonable.
DEFAULT_BATCH_SIZE = 200

#: Penalty configuration read: a single PK probe of the
#: one-row-per-tenant settings table, resolved server-side once per
#: run. Explicit tenant predicate on top of forced RLS
#: Exported for the EXPLAIN capture
#: (tests/test_p138_explain.py).
PENALTY_CONFIG_SQL = (
    "SELECT penalty_rate_pct_per_month, penalty_grace_days, penalty_charged_on "
    "FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"
)


@dataclass(frozen=True)
class PenaltyConfig:
    """The tenant's penalty parameters, snapshotted at run start.

    Recorded verbatim on every claim and audit row this run writes, so
    a later config change can never obscure what a figure was computed
    from (failure mode 5).
    """

    rate_pct_per_month: Decimal
    grace_days: int
    charged_on: PenaltyChargedOn


def parse_penalty_config(
    rate_raw: object, grace_raw: object, basis_raw: object
) -> PenaltyConfig | None:
    """Fail-closed parse of the stored penalty configuration.

    None (accrue NOTHING, skipped — never a default rate, never a 500)
    when ANY of the three keys is unconfigured: a partially configured
    tenant is exactly as unconfigured as one with no settings row.
    Corrupted stored values — reachable only by manual SQL bypassing
    the 0017 DB CHECKs — raise ConflictError (409), the
    fail-closed consumer precedent: a money guard is never silently
    disabled. Least disclosure: the message names the defect category,
    never a member figure (least disclosure).
    """
    if rate_raw is None or grace_raw is None or basis_raw is None:
        return None
    try:
        rate = Decimal(str(rate_raw))
        grace = int(cast(int, grace_raw))
        basis = PenaltyChargedOn(str(basis_raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConflictError(
            "stored penalty configuration is corrupt; repair via PUT /settings"
        ) from exc
    if not rate.is_finite() or rate < 0 or rate > 100 or grace < 0 or grace > 365:
        raise ConflictError(
            "stored penalty configuration is outside its permitted bounds; repair via PUT /settings"
        )
    return PenaltyConfig(rate_pct_per_month=rate, grace_days=grace, charged_on=basis)


async def resolve_penalty_config(
    session: AsyncSession, tenant_id: uuid.UUID
) -> PenaltyConfig | None:
    """The penalty parameters the accrual must run with, or None (skip)."""
    row = (await session.execute(text(PENALTY_CONFIG_SQL), {"tid": str(tenant_id)})).first()
    if row is None:
        return None
    return parse_penalty_config(row[0], row[1], row[2])


def arrears_scan_sql(*, with_after: bool, with_penalty: bool) -> str:
    """The arrears batch scan (shape, penalty extras).

    Walks idx_loans_active_scan (tenant_id, id WHERE status='active');
    the schedule subqueries are served by idx_schedules_unpaid and the
    penalty anti-join by uq_penalty_accruals_claim (0019). Fragments
    are static literals chosen in code; every value is a bound
    parameter. Explicit tenant predicate on top of
    forced RLS. Exported so the EXPLAIN capture
    (tests/test_p138_explain.py) asserts against the production SQL.
    """
    after = "AND l.id > CAST(:after AS uuid) " if with_after else ""
    penalty_cols = ""
    if with_penalty:
        penalty_cols = (
            ", l.balance, l.penalty_due, "
            # Basis reconstruction: unpaid amounts of
            # instalments already due, from the schedule + repayment
            # bookkeeping — penalty_due is structurally excluded
            # (schedules carry principal + interest dues only).
            "(SELECT COALESCE(SUM(s.total_due - s.paid_amount), 0) FROM loan_schedules s "
            " WHERE s.tenant_id = l.tenant_id AND s.loan_id = l.id "
            " AND s.due_date <= :as_of AND s.paid_amount < s.total_due) AS arrears_amount, "
            # Anti-join: a fully accrued re-run claims nothing and the
            # claim insert is not even attempted; the ON CONFLICT claim
            # remains the authoritative guard.
            "NOT EXISTS (SELECT 1 FROM penalty_accruals pa "
            " WHERE pa.tenant_id = l.tenant_id AND pa.loan_id = l.id "
            " AND pa.accrual_date = :as_of) AS penalty_unclaimed"
        )
    return (
        # Static fragments chosen in code; all values are bound parameters.
        # Explicit tenant predicate on top of RLS (defence in depth,
        # least disclosure); also the leading column of idx_loans_active_scan.
        "SELECT l.id, l.days_past_due, l.classification, l.provision_pct, "  # noqa: S608
        "(SELECT MIN(s.due_date) FROM loan_schedules s "
        " WHERE s.tenant_id = l.tenant_id AND s.loan_id = l.id AND s.due_date <= :as_of "
        " AND s.paid_amount < s.total_due) AS oldest_unpaid"
        f"{penalty_cols} "
        "FROM loans l "
        "WHERE l.tenant_id = CAST(:tid AS uuid) "
        f"AND l.status = 'active' {after}"
        "ORDER BY l.id LIMIT :limit "
        "FOR UPDATE OF l SKIP LOCKED"
    )


@dataclass(frozen=True)
class ArrearsRunResult:
    scanned: int
    updated: int
    batches: int
    #: False when the tenant has not configured all three penalty keys
    #: — the run then accrued nothing by design (fail closed).
    penalty_configured: bool
    penalties_accrued: int
    penalty_total: Decimal
    #: recovery cases auto-closed by this run's close pass —
    #: cure (the loan left NPL per THIS job's persisted classification,
    #: or fully closed) vs write-off. Idempotent: a re-run closes
    #: nothing new (side-effect counts).
    cases_closed_cured: int
    cases_closed_written_off: int


async def _classify_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    loan_id: str,
    dpd: int,
    stored_dpd: int,
    stored_cls: str,
    stored_prov: Decimal,
) -> int:
    """classification write for one locked loan; returns 0 or 1.

    Idempotency: identical stored state is never rewritten, so a
    re-run for the same as_of changes nothing.
    """
    computed = classify(dpd)
    if (
        dpd == stored_dpd
        and computed.label.value == stored_cls
        and computed.provision_pct == stored_prov
    ):
        return 0
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of
                # RLS (defence in depth, least disclosure v1.1) —
                # the batch scan is tenant-scoped; the write matches.
                "UPDATE loans SET days_past_due = :dpd, classification = :cls, "
                "provision_pct = :prov, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'active'"
            ),
            {
                "dpd": dpd,
                "cls": computed.label.value,
                "prov": str(computed.provision_pct),
                "id": loan_id,
                "tid": str(tenant_id),
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        return 0
    await record_audit(
        session,
        tenant_id,
        None,
        action="loan.classified",
        entity="loans",
        entity_id=loan_id,
        before={
            "days_past_due": stored_dpd,
            "classification": stored_cls,
            "provision_pct": str(stored_prov),
        },
        after={
            "days_past_due": dpd,
            "classification": computed.label.value,
            "provision_pct": str(computed.provision_pct),
        },
    )
    if computed.label.value != stored_cls:
        await enqueue_event(
            session,
            tenant_id,
            event_type="loan.classification_changed",
            payload={
                "loan_id": loan_id,
                "from": stored_cls,
                "to": computed.label.value,
                "days_past_due": dpd,
                "provision_pct": str(computed.provision_pct),
            },
        )
    return 1


async def _accrue_penalty(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    config: PenaltyConfig,
    *,
    as_of: date,
    loan_id: str,
    dpd: int,
    balance: Decimal,
    penalty_due: Decimal,
    arrears_amount: Decimal,
) -> Decimal:
    """Accrue the as-of day's penalty for one LOCKED loan.

    Returns the accrued amount (ZERO when nothing accrued). Runs inside
    the batch transaction while the caller holds the loan row lock, so
    the claim insert, the penalty_due update and the audit row are one
    atomic unit (failure modes 3 and 9) and can never interleave with a
    concurrent repayment (failure mode 8).

    Grace boundary (failure mode 2): dpd must EXCEED the grace —
    exactly at grace accrues nothing. The basis NEVER includes
    penalty_due itself (failure mode 4): instalment_in_arrears comes
    from the schedule dues, full_outstanding is the principal
    receivable.

    Snapshot nuance: balance and penalty_due are
    lock-fresh (EPQ re-read of the locked loans tuple), but dpd and
    arrears_amount come from the scan STATEMENT's snapshot — a
    repayment committing between statement start and lock acquisition
    can leave this one day's accrual computed from pre-repayment
    schedule state (reconstructable from the claim row's recorded
    basis figures).
    """
    if dpd <= config.grace_days:
        return ZERO
    basis_amount = (
        arrears_amount if config.charged_on is PenaltyChargedOn.INSTALMENT_IN_ARREARS else balance
    )
    amount = daily_penalty(basis_amount, config.rate_pct_per_month)
    if amount <= ZERO:
        # A configured-but-zero rate (or an empty basis) accrues
        # nothing and claims nothing: re-runs recompute the same zero.
        return ZERO
    claim = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Single atomic claim on the UNIQUE idempotency key
                # (tenant_id, loan_id, accrual_date): rowcount 0 means
                # this day is already accrued — no SELECT-then-INSERT
                # race, no IntegrityError mid-batch. The
                # config figures are recorded verbatim so the accrual
                # stays reconstructable after a config change.
                "INSERT INTO penalty_accruals "
                "(id, tenant_id, loan_id, accrual_date, basis, basis_amount, "
                " rate_pct_per_month, grace_days, days_past_due, amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                ":day, :basis, :basis_amount, :rate, :grace, :dpd, :amount) "
                "ON CONFLICT (tenant_id, loan_id, accrual_date) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "lid": loan_id,
                "day": as_of,
                "basis": config.charged_on.value,
                "basis_amount": str(basis_amount),
                "rate": str(config.rate_pct_per_month),
                "grace": config.grace_days,
                "dpd": dpd,
                "amount": str(amount),
            },
        ),
    )
    if claim.rowcount == 0:
        # A concurrent runner claimed this day between the anti-join
        # scan and the insert; idempotency wins (failure mode 3).
        return ZERO
    penalty_after = to_cents(penalty_due + amount)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, least disclosure v1.1). Unlike
                # the derived classification fields, penalty_due is
                # money state, so the version bumps like every other
                # penalty_due writer (repayment, settlement).
                "UPDATE loans SET penalty_due = :pen, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'active'"
            ),
            {"pen": str(penalty_after), "id": loan_id, "tid": str(tenant_id)},
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"loan {loan_id} changed state during penalty accrual; retry")
    # Exact figures live here (least-disclosure rule 7): the audit row
    # carries everything needed to reconstruct the accrual — including
    # the config values in force — for staff entitled to loan_book.
    await record_audit(
        session,
        tenant_id,
        None,
        action="loan.penalty_accrued",
        entity="loans",
        entity_id=loan_id,
        before={"penalty_due": str(penalty_due)},
        after={
            "penalty_due": str(penalty_after),
            "accrual_date": as_of.isoformat(),
            "amount": str(amount),
            "basis": config.charged_on.value,
            "basis_amount": str(basis_amount),
            "rate_pct_per_month": str(config.rate_pct_per_month),
            "grace_days": config.grace_days,
            "days_past_due": dpd,
        },
    )
    return amount


async def _process_batch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    as_of: date,
    after_id: uuid.UUID | None,
    batch_size: int,
    penalty_config: PenaltyConfig | None,
) -> tuple[int, uuid.UUID | None, tuple[int, int, Decimal]]:
    """One keyset batch: classify + accrue penalties under the loan lock.

    Returns (scanned, last_id, (updated, penalties_accrued, penalty_total)).

    The scan walks idx_loans_active_scan (tenant_id, id WHERE status =
    'active'); the schedule subqueries are served by
    idx_schedules_unpaid and the penalty anti-join by
    uq_penalty_accruals_claim.

    Rows are read FOR UPDATE SKIP LOCKED (concurrency safety): days-past-due and
    the penalty accrual are computed and written under the loan row
    lock, so they can never interleave with a concurrent repayment
    (which takes the same lock). A loan mid-repayment is skipped and
    picked up by the next run; the UPDATEs re-check status = 'active'
    as defence in depth so a closed loan is never reclassified and
    never accrues (failure mode 7: closed, written-off and settled
    loans are excluded by the status predicate itself).
    """
    with_penalty = penalty_config is not None
    params: dict[str, object] = {"tid": str(tenant_id), "as_of": as_of, "limit": batch_size}
    if after_id is not None:
        params["after"] = str(after_id)
    rows = (
        await session.execute(
            text(arrears_scan_sql(with_after=after_id is not None, with_penalty=with_penalty)),
            params,
        )
    ).all()
    updated = 0
    accrued_count = 0
    accrued_total = ZERO
    for row in rows:
        loan_id = str(row[0])
        oldest_unpaid = row[4]
        # The single days-past-due derivation (reused verbatim for
        # the penalty grace check): UTC calendar-date difference — the
        # as_of date is a UTC day resolved by the caller, due_date is a
        # DATE column; no DST/timezone-dependent math anywhere.
        dpd = max(0, (as_of - oldest_unpaid).days) if oldest_unpaid is not None else 0
        updated += await _classify_loan(
            session,
            tenant_id,
            loan_id=loan_id,
            dpd=dpd,
            stored_dpd=int(row[1]),
            stored_cls=str(row[2]),
            stored_prov=Decimal(str(row[3])),
        )
        # Penalty accrual runs for every scanned loan (not only
        # reclassified ones), gated by the anti-join flag: a re-run of
        # an already-accrued day attempts no claim at all.
        if penalty_config is not None and bool(row[8]):
            amount = await _accrue_penalty(
                session,
                tenant_id,
                penalty_config,
                as_of=as_of,
                loan_id=loan_id,
                dpd=dpd,
                balance=Decimal(str(row[5])),
                penalty_due=Decimal(str(row[6])),
                arrears_amount=Decimal(str(row[7])),
            )
            if amount > ZERO:
                accrued_count += 1
                accrued_total = to_cents(accrued_total + amount)
    last_id = uuid.UUID(str(rows[-1][0])) if rows else None
    return len(rows), last_id, (updated, accrued_count, accrued_total)


async def _process_one(
    session: AsyncSession,
    after_id: uuid.UUID | None,
    *,
    tenant_id: uuid.UUID,
    as_of: date,
    batch_size: int,
    penalty_config: PenaltyConfig | None,
) -> tuple[int, uuid.UUID | None, tuple[int, int, Decimal]]:
    """Adapter matching the shared BatchProcessor signature."""
    return await _process_batch(
        session,
        tenant_id,
        as_of=as_of,
        after_id=after_id,
        batch_size=batch_size,
        penalty_config=penalty_config,
    )


async def run_arrears_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    as_of: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ArrearsRunResult:
    """Reclassify + accrue penalties for one tenant in short batches.

    The penalty configuration is resolved ONCE, before the first
    batch, from tenant settings only: a tenant without a
    complete penalty configuration accrues nothing (fail closed) while
    classification still runs. A config change mid-run affects the
    NEXT run; every claim/audit row records the values actually used.

    after the classify/accrue batches, the recovery close pass
    runs (application/recovery.run_recovery_close_pass) — it reads the
    classification THIS run just persisted (single source of truth,
    1.1), so a loan curing today closes its open recovery case today.
    The pass locks CASE rows only (no loan locks, no new lock-graph
    edges) and is idempotent by side-effect counts.
    """
    async with session_scope() as session:
        penalty_config = await resolve_penalty_config(session, tenant_id)
    process = partial(
        _process_one,
        tenant_id=tenant_id,
        as_of=as_of,
        batch_size=batch_size,
        penalty_config=penalty_config,
    )
    scanned, batches, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    total = ZERO
    for _, _, batch_total in payloads:
        total = to_cents(total + batch_total)
    close_result = await run_recovery_close_pass(session_scope, tenant_id, batch_size=batch_size)
    return ArrearsRunResult(
        scanned=scanned,
        updated=sum(p[0] for p in payloads),
        batches=batches,
        penalty_configured=penalty_config is not None,
        penalties_accrued=sum(p[1] for p in payloads),
        penalty_total=total,
        cases_closed_cured=close_result.closed_cured,
        cases_closed_written_off=close_result.closed_written_off,
    )
