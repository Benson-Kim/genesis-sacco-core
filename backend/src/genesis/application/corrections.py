"""Ledger corrections, misc fees & loan write-off.

The documented correction paths the P7 reversal blocks require:

  1. Repayment adjustment — TWO-PHASE maker-checker
     workflow: the MAKER requests a PENDING adjustment that captures the
     approval snapshot under the full lock set; a DISTINCT CHECKER
     approves — snapshot re-verified component-by-component, 409 on
     drift posting nothing — and only then, under the loan row lock,
     the reversing ledger legs post via P7 (A1 storno: the reversal
     carries reversal_of_id and mirrors the COMPLETE original
     allocation — penalties/interest/principal together; a partial-leg
     reversal is unrepresentable), the negative-linked repayments
     correction row is written, and loans.balance / penalty_due /
     schedule paid_amounts are RECOMPUTED from the surviving
     append-only history (v1.1 rule 2). A closed loan re-opens ONLY
     via the explicit CLOSED -> ACTIVE branch of the loan status map
    . Execution is one atomic transaction; a second LIVE
     adjustment of the same repayment is blocked by the atomic
     partial-unique claim (v1.1 rule 5 — a rejected request
     frees the slot).

  2. Misc fee posting — the prototype "Fee" drawer type. Fee amounts
     come EXCLUSIVELY from tenant configuration (v1.1 rule 1):
     the request carries a code-owned fee type, never an amount; the
     FE- reference comes from the P7 advisory-lock generator.

  3. Loan write-off — committee-approved (the P9 voting machinery)
     transition to written_off, bound to a DB-LEVEL WRITE-ONCE
     snapshot (v1.1 rule 3; the 0020-trigger precedent). Posting
     re-verifies the snapshot component-by-component under the loan
     row lock and returns 409 on drift, posting nothing.

POSTING-DATE vs VALUE-DATE (A2, standard GL practice): every
correction posts with occurred_at = NOW, server-resolved inside
ledger._post — never caller-supplied, never backdated — so the
closed-period gate applies to corrections too (a correction attempted
while today's period is closed is a 409). The ORIGINAL transaction's
date is preserved untouched via the A1 reversal_of_id linkage: a
correction of a closed-period repayment posts into the open period
REFERENCING the original, and the original row is never rewritten
(append-only, 1.5).

MAKER-CHECKER (A3, review-hardened):
corrections are the fraud channel. Every route is gated by the
DEDICATED corrections module permissions (never generic
transactions:edit); corrections audit under their own entity strings
(repayment_adjustments / loan_write_offs) so they are filterable in
review; adjustments route through the tenant-configured approval-band authority
check for their amount (reuse, 1.1). Repayment adjustments are
TWO-PHASE: the MAKER creates a PENDING adjustment
bound to a persisted approval snapshot (loan balance / penalty_due /
status at request), and a DISTINCT CHECKER approves — re-verifying
every snapshot component under the full lock set (409 on drift,
posting nothing) — before the reversal posts. Maker <> checker is
enforced server-side AND by the 0031 ck_repayment_adjustments_sod
CHECK (collusion-resistant); assurance roles (the Auditor) can never
be checker (the B2 principle). Write-off approval IS the
committee quorum — no parallel approval mechanism exists.

WRITE-OFF IS NOT FORGIVENESS (A4): written_off zeroes the performing
receivable via the WO- provisioning posting, but the legal claim on
the member survives in the write-once snapshot (balance, penalty_due,
total_written_off). The explicit recovery branch lands
(part 4 below): cash received against a POSTED write-off posts DR
cash / CR income.bad_debt_recoveries (RC- ref), recorded as an
APPEND-ONLY loan_recoveries row bound to the surviving snapshot claim
— partial recoveries tracked against total_written_off, over-recovery
refused, the loan NEVER resurrected (written_off stays terminal). A
repayment against a written_off loan remains refused loudly
(loans.record_repayment status guard), never silently allocated — the
recovery receipt is the ONLY money-in path against the claim.

GUARANTEE DISPOSITION (the documented policy):
guarantees behind a written-off loan back the SURVIVING claim and stay
untouched; they are released ONLY by the receipt that recovers the
claim IN FULL (full recovery discharges the sureties exactly like
genuine closure — the P10 release hook, reused in-transaction).
Calling a guarantee (collecting FROM the guarantor) stays a future,
separately designed path; a receipt records who paid only through its
audit trail.

EXIT INTERPLAY (the documented decision): a member
with an unresolved POSTED write-off claim (receipts < total) is
BLOCKED from exit (member_exits._compute_under_locks, under the member
row lock) — write-off is not forgiveness, so the claim behaves like an
active obligation. A committee-approved WAIVER that releases the claim
without cash is a future explicit branch, recorded as a follow-up at
close-out; until it exists, full recovery is the only unblock.

Lock order (docs/diagrams/lock-order.md, updated in this MR):
adjustment request takes transactions (T0, FOR UPDATE — serialises
against generic reversal and concurrent adjustment workflows) ->
members FOR SHARE (T1, holds off a concurrent terminal exit) -> loans
FOR UPDATE (T4, the terminal node of the money chain — the established
money-chain pattern); adjustment APPROVAL locks the pending
repayment_adjustments row FOR UPDATE FIRST (the workflow anchor,
above T0 — the WOFF/E22 anchor-first shape; nothing anywhere acquires
an adjustment row while holding T0+ locks: the request INSERTs it as
a plain write under the chain) and then retakes the SAME chain;
adjustment rejection locks the adjustment row alone (the DECL/WOFF
void pattern); write-off request/execution takes loan_write_offs (T0)
-> loans FOR UPDATE (T4); votes/voids lock the write-off row alone
(the DECL pattern); the fee posting takes members FOR SHARE (T1)
only, then the advisory posting tier. The recovery receipt takes loan_write_offs (T0, FOR
UPDATE — serialises concurrent receipts and pins the claim math) ->
members FOR SHARE (T1, holds off a concurrent terminal exit — the E20
argument) -> loans FOR UPDATE (T4, anchors the full-recovery guarantee
release, the E7 order) -> the advisory posting tier. All edges point
down the established tiers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.guarantees import release_guarantees_for_loan
from genesis.application.ledger import (
    PostingResult,
    post_fee,
    post_loan_recovery,
    post_loan_write_off,
    post_reversal,
)
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import (
    build_band_register_cursor,
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_band_register_cursor,
    parse_created_id_cursor,
)
from genesis.application.sod import require_distinct_non_assurance_checker
from genesis.application.tenant_settings import committee_quorum, enforce_authority_band

# Reuse-first: the single member-status gatekeeper —
# duplicating its FOR SHARE + capability-map logic here would fork the
# exact policy the capability map pins to one code path.
from genesis.application.transactions import _require_member
from genesis.domain.committee import Decision, Vote, decide
from genesis.domain.ledger import Account, Channel
from genesis.domain.lending import NPL_CLASSES, LoanClass, LoanStatus, loan_transition
from genesis.domain.members import MemberStatus, MoneyOperation
from genesis.domain.money import ZERO, to_cents
from genesis.domain.tenant_config import SETTINGS_REGISTRY
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError, NotFoundError

#: Cursor scope ids: signed cursors are bound to ONE
#: endpoint - the three corrections registers never share positions
#: (tenant isolation).
ADJUSTMENTS_SCOPE = "corrections.adjustments"
WRITE_OFFS_SCOPE = "corrections.write_offs"
WO_RECOVERIES_SCOPE = "corrections.write_off_recoveries"

# ---------------------------------------------------------------------------
# Misc fees
# ---------------------------------------------------------------------------


class FeeType(StrEnum):
    """Code-owned fee vocabulary: each member maps to the tenant-settings
    key its amount is resolved from (v1.1 rule 1). A caller can only
    ever name a type from this enum — never an amount."""

    REGISTRATION = "registration"


#: fee type -> tenant_settings registry key (code-owned, v1.1 rule 6).
FEE_SETTING_KEYS: dict[FeeType, str] = {
    FeeType.REGISTRATION: "registration_fee",
}

#: Consumer read for the fee amount: single PK probe, explicit tenant
#: predicate on top of forced RLS (v1.1 rule 4). The column identifier
#: comes from FEE_SETTING_KEYS/SETTINGS_REGISTRY, never caller input.
_FEE_AMOUNT_SQL = "SELECT {column} FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"


@dataclass(frozen=True)
class FeeResult:
    txn_id: uuid.UUID
    txn_ref: str
    fee_type: FeeType
    amount: Decimal


async def _resolve_fee_amount(
    session: AsyncSession, tenant_id: uuid.UUID, fee_type: FeeType
) -> Decimal:
    """Fee amount from tenant configuration; FAILS CLOSED (409) when
    unconfigured or zero — never a default, never caller-supplied."""
    column = FEE_SETTING_KEYS[fee_type]
    if column not in SETTINGS_REGISTRY:  # pragma: no cover - code-owned pin
        raise RuntimeError(f"fee setting key {column!r} is not in the registry")
    # Identifier from the code-owned mapping above (v1.1 rule 6).
    row = (
        await session.execute(
            text(_FEE_AMOUNT_SQL.format(column=column)),
            {"tid": str(tenant_id)},
        )
    ).first()
    amount = Decimal(str(row[0])) if row is not None and row[0] is not None else None
    if amount is None or amount <= ZERO:
        raise ConflictError(
            f"fee '{fee_type.value}' is not configured for this tenant "
            f"(tenant_settings.{column}); configure it before posting"
        )
    return to_cents(amount)


async def post_misc_fee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    fee_type: FeeType,
    channel: Channel,
) -> FeeResult:
    """Post a misc fee against a member (the house gates).

    Amount exclusively from tenant config (a caller-supplied
    amount is a 422 at the API contract, and this service never reads
    one). Member gated by the single capability gatekeeper (the capability
    map) under FOR SHARE; posting via P7 with a server-resolved NOW
    occurred_at (A2 — the open-period gate applies).
    """
    amount = await _resolve_fee_amount(session, tenant_id, fee_type)
    await _require_member(session, tenant_id, member_id, operation=MoneyOperation.FEE)
    posting = await post_fee(
        session, tenant_id, member_id, amount, channel, actor_id, fee_type=fee_type.value
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="correction.fee_posted",
        entity="transactions",
        entity_id=str(posting.txn_id),
        after={
            "member_id": str(member_id),
            "fee_type": fee_type.value,
            "amount": str(amount),
            "channel": channel.value,
            "txn_ref": posting.txn_ref,
        },
    )
    return FeeResult(
        txn_id=posting.txn_id, txn_ref=posting.txn_ref, fee_type=fee_type, amount=amount
    )


# ---------------------------------------------------------------------------
# Repayment adjustment (two-phase maker-checker)
# ---------------------------------------------------------------------------


class AdjustmentStatus(StrEnum):
    """The 0031 adjustment workflow machine."""

    PENDING_APPROVAL = "pending_approval"
    POSTED = "posted"
    REJECTED = "rejected"


_ADJUSTMENT_ALLOWED: dict[AdjustmentStatus, frozenset[AdjustmentStatus]] = {
    AdjustmentStatus.PENDING_APPROVAL: frozenset(
        {AdjustmentStatus.POSTED, AdjustmentStatus.REJECTED}
    ),
    AdjustmentStatus.POSTED: frozenset(),
    AdjustmentStatus.REJECTED: frozenset(),
}


def adjustment_transition(current: AdjustmentStatus, target: AdjustmentStatus) -> None:
    """THE single gatekeeper for adjustment status moves (concurrency safety).

    The only writers are approve/reject below; the regenerated 0031
    write-once trigger enforces the same machine at the database, so a
    terminal row can never move again even via direct SQL. Illegal
    moves raise.
    """
    if target not in _ADJUSTMENT_ALLOWED[current]:
        raise ConflictError(f"adjustment cannot move from '{current.value}' to '{target.value}'")


async def _require_distinct_non_assurance_checker(
    session: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID, maker_id: uuid.UUID
) -> None:
    """Segregation of duties for the CHECKER actions.

    ONE shared copy (reuse-first — the share-transfer
    maker-checker workflow is the second consumer):
    the guard body lives in application/sod.py; this wrapper keeps the
    corrections wording. The 0031 ck_repayment_adjustments_sod CHECK
    is the collusion-resistant DB backstop behind it.
    """
    await require_distinct_non_assurance_checker(
        session,
        tenant_id,
        actor_id,
        maker_id,
        subject="an adjustment",
        subject_plural="adjustments",
    )


@dataclass(frozen=True)
class AdjustmentRecord:
    id: uuid.UUID
    repayment_id: uuid.UUID
    loan_id: uuid.UUID
    original_transaction_id: uuid.UUID
    reversal_transaction_id: uuid.UUID | None
    maker_id: uuid.UUID
    checker_id: uuid.UUID | None
    reason: str
    amount: Decimal
    penalties: Decimal
    interest: Decimal
    principal: Decimal
    reopened_loan: bool
    status: AdjustmentStatus
    loan_balance_at_request: Decimal | None
    loan_penalty_due_at_request: Decimal | None
    loan_status_at_request: str | None
    decided_at: datetime | None
    version: int
    created_at: datetime


_ADJUSTMENT_COLS = (
    "id, repayment_id, loan_id, original_transaction_id, "
    "reversal_transaction_id, maker_id, checker_id, reason, amount, "
    "penalties, interest, principal, reopened_loan, status, "
    "loan_balance_at_request, loan_penalty_due_at_request, "
    "loan_status_at_request, decided_at, version, created_at"
)


def _row_to_adjustment(row: Any) -> AdjustmentRecord:
    return AdjustmentRecord(
        id=uuid.UUID(str(row[0])),
        repayment_id=uuid.UUID(str(row[1])),
        loan_id=uuid.UUID(str(row[2])),
        original_transaction_id=uuid.UUID(str(row[3])),
        reversal_transaction_id=uuid.UUID(str(row[4])) if row[4] is not None else None,
        maker_id=uuid.UUID(str(row[5])),
        checker_id=uuid.UUID(str(row[6])) if row[6] is not None else None,
        reason=str(row[7]),
        amount=Decimal(str(row[8])),
        penalties=Decimal(str(row[9])),
        interest=Decimal(str(row[10])),
        principal=Decimal(str(row[11])),
        reopened_loan=bool(row[12]),
        status=AdjustmentStatus(str(row[13])),
        loan_balance_at_request=Decimal(str(row[14])) if row[14] is not None else None,
        loan_penalty_due_at_request=Decimal(str(row[15])) if row[15] is not None else None,
        loan_status_at_request=str(row[16]) if row[16] is not None else None,
        decided_at=row[17],
        version=int(row[18]),
        created_at=row[19],
    )


async def get_adjustment(
    session: AsyncSession, tenant_id: uuid.UUID, adjustment_id: uuid.UUID
) -> AdjustmentRecord:
    """One adjustment by id; explicit tenant predicate on top of RLS."""
    row = (
        await session.execute(
            text(
                f"SELECT {_ADJUSTMENT_COLS} FROM repayment_adjustments "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(adjustment_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"repayment adjustment {adjustment_id} not found")
    return _row_to_adjustment(row)


# ---------------------------------------------------------------------------
# Corrections registers (the
# HUMAN-AUTHORIZED read-contract expansion): keyset LIST reads so the
# checker/committee no longer works from a hand-carried id. Read-only;
# no mutation or by-id semantics change.
# ---------------------------------------------------------------------------


def adjustments_register_sql(*, with_cursor: bool) -> str:
    """The pending-adjustments checker register.

    ORDER BY (status = 'pending_approval') DESC, created_at DESC,
    id DESC — the checker's job order: PENDING FIRST, newest first
    inside each band; terminal history behind. Uniform-DESC keyset row
    comparison, served by idx_repayment_adjustments_register (0038,
    shipped with this query — scalability; EXPLAIN-asserted, falsifiable
    by dropping it). Static fragments chosen in code; every value is a
    bound parameter (v1.1 rule 6); explicit tenant predicate on top of
    forced RLS (rule 4).
    """
    cursor = (
        "AND ((status = 'pending_approval'), created_at, id) "
        "< (CAST(:c_flag AS boolean), CAST(:c_ts AS timestamptz), CAST(:c_id AS uuid)) "
    )
    return (
        f"SELECT {_ADJUSTMENT_COLS} FROM repayment_adjustments "  # noqa: S608
        "WHERE tenant_id = CAST(:tid AS uuid) "
        f"{cursor if with_cursor else ''}"
        "ORDER BY (status = 'pending_approval') DESC, created_at DESC, id DESC "
        "LIMIT :limit"
    )


@dataclass(frozen=True)
class AdjustmentPage:
    items: list[AdjustmentRecord]
    next_cursor: str | None


async def list_adjustments(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
) -> AdjustmentPage:
    """Keyset adjustments register, pending-first."""
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor is not None:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext band parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=ADJUSTMENTS_SCOPE, entity="adjustment register"
        )
        c_flag, c_ts, c_id = parse_band_register_cursor(inner, entity="adjustment register")
        params["c_flag"] = c_flag
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    stmt = text(adjustments_register_sql(with_cursor=cursor is not None))
    rows = (await session.execute(stmt, params)).all()
    items = [_row_to_adjustment(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_band_register_cursor(
                last.status is AdjustmentStatus.PENDING_APPROVAL, last.created_at, last.id
            ),
            tenant_id=tenant_id,
            endpoint=ADJUSTMENTS_SCOPE,
        )
    return AdjustmentPage(items=items, next_cursor=next_cursor)


@dataclass(frozen=True)
class AdjustmentResult:
    adjustment_id: uuid.UUID
    reversal_txn_id: uuid.UUID
    reversal_txn_ref: str
    amount: Decimal
    penalties: Decimal
    interest: Decimal
    principal: Decimal
    balance_after: Decimal
    penalty_due_after: Decimal
    status: LoanStatus
    reopened: bool


async def _allocation_from_legs(
    session: AsyncSession, tenant_id: uuid.UUID, txn_id: uuid.UUID
) -> tuple[Decimal, Decimal, Decimal]:
    """(penalties, interest, principal) of one repayment transaction,
    reconstructed from its append-only ledger legs (v1.1 rule 2) —
    never from mutable state. Account literals come from the code-owned
    Account enum (v1.1 rule 6)."""
    row = (
        await session.execute(
            text(
                "SELECT "
                "COALESCE(SUM(amount) FILTER (WHERE account = :pen AND side = 'credit'), 0), "
                "COALESCE(SUM(amount) FILTER (WHERE account = :int AND side = 'credit'), 0), "
                "COALESCE(SUM(amount) FILTER (WHERE account = :prn AND side = 'credit'), 0) "
                "FROM ledger_entries "
                "WHERE transaction_id = CAST(:txn AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "pen": Account.PENALTY_INCOME.value,
                "int": Account.INTEREST_INCOME.value,
                "prn": Account.LOANS_RECEIVABLE.value,
                "txn": str(txn_id),
                "tid": str(tenant_id),
            },
        )
    ).one()
    return Decimal(str(row[0])), Decimal(str(row[1])), Decimal(str(row[2]))


async def _rebuild_schedule_paid_amounts(
    session: AsyncSession, tenant_id: uuid.UUID, loan_id: uuid.UUID
) -> None:
    """State restore RECOMPUTES (A1): replay every SURVIVING repayment
    (positive, unadjusted) over the contractual schedule, mirroring
    loans.record_repayment's forward pass exactly — cash (interest +
    principal legs) applies to installments due on or before the
    repayment's occurred_at, oldest first, capped per row. Derived
    entirely from the append-only ledger + repayments history (v1.1
    rule 2); bounded by the loan term (<= 120 rows) and the loan's own
    repayment count. Runs under the loan row lock."""
    await session.execute(
        text(
            # Explicit tenant predicate on the write (v1.1 rule 4).
            "UPDATE loan_schedules SET paid_amount = 0 "
            "WHERE loan_id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"lid": str(loan_id), "tid": str(tenant_id)},
    )
    survivors = (
        await session.execute(
            text(
                "SELECT t.occurred_at, "
                "COALESCE(SUM(le.amount) FILTER "
                " (WHERE le.account = :int AND le.side = 'credit'), 0) AS interest, "
                "COALESCE(SUM(le.amount) FILTER "
                " (WHERE le.account = :prn AND le.side = 'credit'), 0) AS principal "
                "FROM repayments r "
                "JOIN transactions t ON t.id = r.transaction_id "
                " AND t.tenant_id = r.tenant_id "
                "JOIN ledger_entries le ON le.transaction_id = t.id "
                " AND le.tenant_id = t.tenant_id "
                "WHERE r.loan_id = CAST(:lid AS uuid) "
                "AND r.tenant_id = CAST(:tid AS uuid) AND r.amount > 0 "
                "AND NOT EXISTS (SELECT 1 FROM repayment_adjustments a "
                " WHERE a.repayment_id = r.id AND a.tenant_id = r.tenant_id "
                " AND a.status = 'posted') "
                "GROUP BY r.id, t.occurred_at ORDER BY t.occurred_at, r.id"
            ),
            {
                "int": Account.INTEREST_INCOME.value,
                "prn": Account.LOANS_RECEIVABLE.value,
                "lid": str(loan_id),
                "tid": str(tenant_id),
            },
        )
    ).all()
    schedule = (
        await session.execute(
            text(
                "SELECT id, due_date, total_due FROM loan_schedules "
                "WHERE loan_id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "ORDER BY installment_no"
            ),
            {"lid": str(loan_id), "tid": str(tenant_id)},
        )
    ).all()
    paid: dict[str, Decimal] = {str(r[0]): ZERO for r in schedule}
    for occurred_at, interest_raw, principal_raw in survivors:
        remaining = to_cents(Decimal(str(interest_raw)) + Decimal(str(principal_raw)))
        as_of = occurred_at.date()
        for inst_id, due_date, total_due_raw in schedule:
            if remaining <= ZERO:
                break
            if due_date > as_of:
                continue
            open_amount = Decimal(str(total_due_raw)) - paid[str(inst_id)]
            if open_amount <= ZERO:
                continue
            pay = min(remaining, open_amount)
            paid[str(inst_id)] = to_cents(paid[str(inst_id)] + pay)
            remaining = to_cents(remaining - pay)
    for inst_id_str, amount in paid.items():
        if amount > ZERO:
            await session.execute(
                text(
                    "UPDATE loan_schedules SET paid_amount = :paid "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"paid": str(amount), "id": inst_id_str, "tid": str(tenant_id)},
            )


async def _reconstructed_balance(
    session: AsyncSession, tenant_id: uuid.UUID, loan_id: uuid.UUID, principal: Decimal
) -> Decimal:
    """loans.balance reconstructed from the append-only ledger:
    disbursed principal minus the signed net of every loans.receivable
    leg attached to this loan's repayments history (originals credit,
    reversals debit — a storno pair nets to zero by construction).

    SCOPE PROOF (review-proven): the repayments join is COMPLETE for an
    adjustable loan. Builder-by-builder over domain/ledger.py, every
    posting that carries a loans.receivable leg:

      * build_disbursement_posting (DR) — the ``principal`` starting
        term of this reconstruction (loans.principal), by definition
        outside the repayments join.
      * build_repayment_posting / build_allocated_repayment_posting
        (CR) — every caller writes a repayments row in the same
        transaction (post_repayment / post_allocated_repayment), so
        both are IN the join.
      * the adjustment reversal (DR mirror) — approve_repayment_
        adjustment writes the negative-linked repayments correction
        row for it, IN the join. Generic post_reversal refuses
        repayment-linked transactions and has no API route; the only
        other receivable carrier it could mirror is a disbursement,
        and no code path calls it with one.
      * build_exit_settlement_posting (CR) — only reachable through
        the P12 exit settlement, which terminal-EXITs the member in
        the same transaction; the adjustment chain refuses EXITED
        members before this check can run.
      * build_write_off_posting (CR) — only reachable through
        post_write_off, which moves the loan to WRITTEN_OFF in the
        same transaction; the adjustment chain refuses written-off
        loans before this check can run.

    Every other builder (fees, deposits, dividends, transfers, and
    BOTH interest postings — loan accrual uses interest.receivable,
    deposit interest uses member.deposits) never touches
    loans.receivable; test_n2_fm8_reconstruction_survives_a_loan_
    interest_accrual proves the accrual case end-to-end."""
    net = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(CASE WHEN le.side = 'credit' "
                "THEN le.amount ELSE -le.amount END), 0) "
                "FROM repayments r "
                "JOIN ledger_entries le ON le.transaction_id = r.transaction_id "
                " AND le.tenant_id = r.tenant_id "
                "WHERE r.loan_id = CAST(:lid AS uuid) "
                "AND r.tenant_id = CAST(:tid AS uuid) AND le.account = :prn"
            ),
            {
                "lid": str(loan_id),
                "tid": str(tenant_id),
                "prn": Account.LOANS_RECEIVABLE.value,
            },
        )
    ).scalar_one()
    return to_cents(principal - Decimal(str(net)))


@dataclass(frozen=True)
class _AdjustmentLockContext:
    """Everything both phases read under the FULL adjustment lock set
    (shared VERBATIM by the maker's request and the checker's approval
    — the P12 request/posting snapshot-bind-reverify precedent)."""

    repayment_id: uuid.UUID
    loan_id: uuid.UUID
    original_txn_id: uuid.UUID
    member_id: uuid.UUID
    amount: Decimal
    principal_disbursed: Decimal
    balance: Decimal
    penalty_due: Decimal
    loan_status: LoanStatus


async def _released_guarantees_exist(
    session: AsyncSession, tenant_id: uuid.UUID, loan_id: uuid.UUID
) -> bool:
    """FM10 read (review B2): released guarantees linked to this loan.

    Plain read under the caller's loan FOR UPDATE (no guarantee-row
    lock — no new lock-graph edge); release itself only ever happens
    under the same loan lock (P10 closure) or the guarantee
    workflow, so the read cannot race a concurrent release.
    """
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM guarantees "
                "WHERE loan_id = CAST(:lid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'released' LIMIT 1"
            ),
            {"lid": str(loan_id), "tid": str(tenant_id)},
        )
    ).first()
    return row is not None


async def _lock_adjustment_chain(
    session: AsyncSession, tenant_id: uuid.UUID, repayment_id: uuid.UUID
) -> _AdjustmentLockContext:
    """Take the FULL adjustment lock set and read the loan position.

    Lock order: transactions FOR UPDATE (T0) -> members FOR SHARE (T1)
    -> loans FOR UPDATE (T4) — see the module docstring and
    lock-order.md (E20/E21). The member FOR SHARE holds off a
    concurrent terminal exit (which takes the member row FOR UPDATE)
    so an adjustment can never re-open a loan underneath a posting
    settlement (A5: an EXITED member's loan is refused — the P12
    terminal-state rule). Shared verbatim by request (the maker's
    snapshot capture) and approval (the checker's re-verification).
    """
    repayment = (
        await session.execute(
            text(
                # Explicit tenant predicate on top of RLS (v1.1 rule 4).
                "SELECT loan_id, transaction_id, amount FROM repayments "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(repayment_id), "tid": str(tenant_id)},
        )
    ).first()
    if repayment is None:
        raise NotFoundError(f"repayment {repayment_id} not found")
    loan_id = uuid.UUID(str(repayment[0]))
    original_txn_id = uuid.UUID(str(repayment[1]))
    amount = Decimal(str(repayment[2]))
    if amount <= ZERO:
        raise ConflictError("a correction row cannot itself be adjusted")

    # T0: the original transaction row — serialises this adjustment
    # against a concurrent generic reversal AND a concurrent second
    # adjustment (the claim below is the durable guard).
    txn_row = (
        await session.execute(
            text(
                "SELECT member_id, reversal_of_id FROM transactions "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(original_txn_id), "tid": str(tenant_id)},
        )
    ).first()
    if txn_row is None:
        raise NotFoundError(f"transaction {original_txn_id} not found")
    member_id = uuid.UUID(str(txn_row[0]))

    # T1: member FOR SHARE — the single-gatekeeper terminal
    # check; an exited member's history is settled and immutable (A5).
    member_status = (
        await session.execute(
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR SHARE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if member_status is None:
        raise NotFoundError(f"member {member_id} not found")
    if MemberStatus(str(member_status[0])) is MemberStatus.EXITED:
        raise ConflictError(
            f"member {member_id} has exited; their settled loan history cannot be adjusted"
        )

    # T4: the loan row — the serialisation point every balance/schedule
    # writer takes (a concurrent repayment waits here).
    loan_row = (
        await session.execute(
            text(
                "SELECT member_id, principal, balance, penalty_due, status "
                "FROM loans WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if loan_row is None:
        raise NotFoundError(f"loan {loan_id} not found")
    if uuid.UUID(str(loan_row[0])) != member_id:  # pragma: no cover - FK-consistent
        raise ConflictError(f"loan {loan_id} does not belong to member {member_id}")
    principal_disbursed = Decimal(str(loan_row[1]))
    balance = Decimal(str(loan_row[2]))
    penalty_due = Decimal(str(loan_row[3]))
    status = LoanStatus(str(loan_row[4]))
    if status is LoanStatus.WRITTEN_OFF:
        # A5/A4: the receivable is derecognised and the claim lives in
        # the write-once snapshot; recovery is the explicit
        # branch, never an adjustment.
        raise ConflictError(f"loan {loan_id} is written off; adjustments are refused")
    return _AdjustmentLockContext(
        repayment_id=repayment_id,
        loan_id=loan_id,
        original_txn_id=original_txn_id,
        member_id=member_id,
        amount=amount,
        principal_disbursed=principal_disbursed,
        balance=balance,
        penalty_due=penalty_due,
        loan_status=status,
    )


async def request_repayment_adjustment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    repayment_id: uuid.UUID,
    *,
    reason: str,
) -> AdjustmentRecord:
    """Phase 1 — the MAKER requests an adjustment.

    Creates a PENDING adjustment capturing the persisted approval
    SNAPSHOT (loan balance / penalty_due / status at request — v1.1
    rule 3) under the FULL lock set; NOTHING posts here. The reversal,
    the negative correction row and the state restore happen only in
    approve_repayment_adjustment, executed by a DISTINCT checker.

    FM10 (review B2 — banking principle: a discharged surety cannot be
    unilaterally re-bound): when the CLOSED -> ACTIVE reopen branch
    would trigger and any guarantee linked to this loan is 'released',
    the request is refused (least disclosure: the category, never
    amounts or guarantors); the operator's remedy is the
    substitution / re-pledge flow. The same check re-runs at approval,
    which is the binding gate — this one fails fast.

    The one-LIVE-adjustment-per-repayment claim is atomic (v1.1
    rule 5) on the 0031 PARTIAL unique (status <> 'rejected'): a
    pending or posted adjustment blocks a second request; a rejected
    one frees the slot for a fresh request.
    """
    ctx = await _lock_adjustment_chain(session, tenant_id, repayment_id)

    if ctx.loan_status is LoanStatus.CLOSED and await _released_guarantees_exist(
        session, tenant_id, ctx.loan_id
    ):
        raise ConflictError(
            f"loan {ctx.loan_id} cannot be reopened by this adjustment: its "
            "guarantees were released at closure and the guarantors are "
            "discharged; substitute or re-pledge security "
            "before adjusting"
        )

    # The COMPLETE original allocation, reconstructed from the
    # append-only legs (A1: all components together, never a subset).
    penalties, interest, principal = await _allocation_from_legs(
        session, tenant_id, ctx.original_txn_id
    )
    if to_cents(penalties + interest + principal) != ctx.amount:
        raise ConflictError(
            f"transaction {ctx.original_txn_id} legs do not reconstruct the repayment amount"
        )

    # A3: the MAKER's band — adjustments above the configured band are
    # refused at request time (reuse of the settings check); the
    # CHECKER is band-checked again at approval (they ratify).
    await enforce_authority_band(session, tenant_id, actor_id, ctx.amount)

    # Reopened? Decided HERE, under the loan lock and BEFORE the claim
    # INSERT: reopened_loan is write-once (pinned by the 0031 trigger),
    # and approval re-verifies loan_status_at_request, so the decision
    # cannot silently drift (the ONE documented reopen branch —
    # validated against the status map here, executed at approval).
    reopened = ctx.loan_status is LoanStatus.CLOSED
    if reopened:
        loan_transition(LoanStatus.CLOSED, LoanStatus.ACTIVE)

    # The atomic one-live-adjustment-per-repayment claim (v1.1
    # rule 5) carrying the persisted approval snapshot (v1.1 rule 3).
    adjustment_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO repayment_adjustments "
                "(id, tenant_id, repayment_id, loan_id, original_transaction_id, "
                " maker_id, reason, amount, penalties, interest, principal, "
                " reopened_loan, status, loan_balance_at_request, "
                " loan_penalty_due_at_request, loan_status_at_request) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                " CAST(:lid AS uuid), CAST(:txn AS uuid), CAST(:maker AS uuid), "
                " :reason, :amount, :pen, :int, :prn, :reopened, :status, "
                " :snap_balance, :snap_penalty, :snap_status) "
                "ON CONFLICT (tenant_id, repayment_id) WHERE status <> 'rejected' "
                "DO NOTHING RETURNING id"
            ),
            {
                "id": str(adjustment_id),
                "tid": str(tenant_id),
                "rid": str(repayment_id),
                "lid": str(ctx.loan_id),
                "txn": str(ctx.original_txn_id),
                "maker": str(actor_id),
                "reason": reason,
                "amount": str(ctx.amount),
                "pen": str(penalties),
                "int": str(interest),
                "prn": str(principal),
                "reopened": reopened,
                "status": AdjustmentStatus.PENDING_APPROVAL.value,
                "snap_balance": str(ctx.balance),
                "snap_penalty": str(ctx.penalty_due),
                "snap_status": ctx.loan_status.value,
            },
        )
    ).first()
    if claimed is None:
        raise ConflictError(
            f"repayment {repayment_id} has already been adjusted or has a pending adjustment"
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="correction.adjustment_requested",
        entity="repayment_adjustments",
        entity_id=str(adjustment_id),
        after={
            "repayment_id": str(repayment_id),
            "loan_id": str(ctx.loan_id),
            "member_id": str(ctx.member_id),
            "original_txn_id": str(ctx.original_txn_id),
            "amount": str(ctx.amount),
            "penalties": str(penalties),
            "interest": str(interest),
            "principal": str(principal),
            "status": AdjustmentStatus.PENDING_APPROVAL.value,
            "loan_balance_at_request": str(ctx.balance),
            "loan_penalty_due_at_request": str(ctx.penalty_due),
            "loan_status_at_request": ctx.loan_status.value,
            "would_reopen_loan": reopened,
            "reason": reason,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="correction.adjustment_requested",
        payload={
            "adjustment_id": str(adjustment_id),
            "repayment_id": str(repayment_id),
            "loan_id": str(ctx.loan_id),
            "member_id": str(ctx.member_id),
            "amount": str(ctx.amount),
        },
    )
    return await get_adjustment(session, tenant_id, adjustment_id)


async def approve_repayment_adjustment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    adjustment_id: uuid.UUID,
) -> AdjustmentResult:
    """Phase 2 — a DISTINCT CHECKER approves and executes, atomically
    (the house gates).

    Snapshot-bind-reverify (v1.1 rule 3, the established snapshot pattern —
    sequence-snapshot-bind-reverify.md): lock the pending adjustment
    row FOR UPDATE (the workflow anchor — the WOFF pattern) -> status
    gatekeeper + segregation-of-duties checks (maker <> checker,
    server-side AND the 0031 DB CHECK behind it; assurance roles
    excluded, the B2 principle) -> retake the FULL lock set
    (transactions -> member FOR SHARE -> loan FOR UPDATE, shared
    verbatim with the request) -> re-verify EVERY snapshot component
    (balance, penalty_due, loan status) -> 409 on drift, posting
    NOTHING -> only then the storno posting, the negative correction
    row, the one-shot status/checker/decided_at/reversal fill, and the
    state restore — ONE transaction (kill-switch tested).
    """
    ts = datetime.now(UTC)
    row = (
        await session.execute(
            text(
                f"SELECT {_ADJUSTMENT_COLS} FROM repayment_adjustments "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(adjustment_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"repayment adjustment {adjustment_id} not found")
    record = _row_to_adjustment(row)
    adjustment_transition(record.status, AdjustmentStatus.POSTED)
    await _require_distinct_non_assurance_checker(session, tenant_id, actor_id, record.maker_id)

    ctx = await _lock_adjustment_chain(session, tenant_id, record.repayment_id)

    # Component-by-component re-verification against the persisted
    # snapshot (never "the current state"): any drift since the request
    # — a repayment, a penalty accrual, a closure — is a 409 and
    # NOTHING posts; reject the stale request and raise a fresh one.
    drifted = [
        name
        for name, matches in (
            ("balance", ctx.balance == record.loan_balance_at_request),
            ("penalty_due", ctx.penalty_due == record.loan_penalty_due_at_request),
            ("status", ctx.loan_status.value == record.loan_status_at_request),
        )
        if not matches
    ]
    if drifted:
        raise ConflictError(
            f"adjustment {adjustment_id} snapshot has drifted since request "
            f"({', '.join(drifted)}); reject it and request afresh"
        )
    penalties, interest, principal = await _allocation_from_legs(
        session, tenant_id, ctx.original_txn_id
    )
    if (penalties, interest, principal) != (
        record.penalties,
        record.interest,
        record.principal,
    ):  # pragma: no cover - the legs are append-only
        raise ConflictError(
            f"adjustment {adjustment_id} legs no longer reconstruct the pinned allocation"
        )

    # FM10 re-check under the loan lock — approval is the BINDING gate
    # (a guarantee release could have moved between the phases).
    reopened = record.reopened_loan
    had_released_guarantees = False
    new_status: LoanStatus = ctx.loan_status
    if reopened:
        had_released_guarantees = await _released_guarantees_exist(session, tenant_id, ctx.loan_id)
        if had_released_guarantees:
            raise ConflictError(
                f"loan {ctx.loan_id} cannot be reopened by this adjustment: its "
                "guarantees were released at closure and the guarantors are "
                "discharged; substitute or re-pledge security "
                "before adjusting"
            )
        new_status = loan_transition(LoanStatus.CLOSED, LoanStatus.ACTIVE)

    # A3: the CHECKER ratifies the money movement — the same
    # band check the maker passed at request time (reuse, 1.1).
    await enforce_authority_band(session, tenant_id, actor_id, ctx.amount)

    # A1/A2: the storno posting — mirror-image legs linked via
    # reversal_of_id, occurred_at = NOW (server-resolved in _post; the
    # open-period gate applies). The original row is never rewritten.
    reversal: PostingResult = await post_reversal(
        session, tenant_id, ctx.original_txn_id, actor_id, allow_repayment_correction=True
    )

    # The negative-linked repayments correction row (the storno pair in
    # the servicing history; conservation: original + correction = 0).
    # A NEW row, never an edit — the 0032 append-only triggers stand
    # behind this discipline.
    await session.execute(
        text(
            "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
            "CAST(:txn AS uuid), :amount)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "lid": str(ctx.loan_id),
            "txn": str(reversal.txn_id),
            "amount": str(to_cents(-ctx.amount)),
        },
    )

    # The decision write — the ONLY post-insert mutation the 0031
    # write-once trigger permits: the pending -> posted transition plus
    # the one-shot NULL -> value fills (checker_id, decided_at,
    # reversal_transaction_id). Every pinned column stays untouched;
    # the ck_repayment_adjustments_sod CHECK re-verifies maker <>
    # checker at the database. Audited money history stays intact.
    decided = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE repayment_adjustments "
                "SET status = :st, checker_id = CAST(:chk AS uuid), decided_at = :ts, "
                "reversal_transaction_id = CAST(:rev AS uuid), "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "st": AdjustmentStatus.POSTED.value,
                "chk": str(actor_id),
                "ts": ts,
                "rev": str(reversal.txn_id),
                "id": str(adjustment_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if decided.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"adjustment {adjustment_id} vanished mid-transaction")

    # State restore RECOMPUTES from the surviving history (A1):
    # the exact inverse of the forward update for the loan-row figures,
    # and a full replay for the schedule (its forward application was
    # capped per row, so add-back alone cannot restore it). The rebuild
    # runs AFTER the status write above so the now-POSTED adjustment
    # voids its repayment in the replay.
    balance_after = to_cents(ctx.balance + record.principal)
    penalty_after = to_cents(ctx.penalty_due + record.penalties)
    await _rebuild_schedule_paid_amounts(session, tenant_id, ctx.loan_id)

    await session.execute(
        text(
            # Explicit tenant predicate on the write (v1.1 rule 4).
            "UPDATE loans SET balance = :bal, penalty_due = :pen, status = :st, "
            "closed_at = CASE WHEN :reopened THEN NULL ELSE closed_at END, "
            "version = version + 1, updated_at = :ts "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "bal": str(balance_after),
            "pen": str(penalty_after),
            "st": new_status.value,
            "reopened": reopened,
            "ts": ts,
            "id": str(ctx.loan_id),
            "tid": str(tenant_id),
        },
    )

    # Conservation self-check, in-transaction: the restored balance
    # must reconstruct from the append-only ledger to the cent. A
    # mismatch aborts the WHOLE adjustment (no partial state).
    reconstructed = await _reconstructed_balance(
        session, tenant_id, ctx.loan_id, ctx.principal_disbursed
    )
    if reconstructed != balance_after:
        raise ConflictError(
            f"adjustment aborted: restored balance {balance_after} does not "
            f"reconstruct from the ledger ({reconstructed})"
        )

    audit_after: dict[str, Any] = {
        "balance": str(balance_after),
        "penalty_due": str(penalty_after),
        "status": new_status.value,
        "adjustment_status": AdjustmentStatus.POSTED.value,
        "checker_id": str(actor_id),
        "repayment_id": str(record.repayment_id),
        "original_txn_id": str(ctx.original_txn_id),
        "reversal_txn_ref": reversal.txn_ref,
        "amount": str(ctx.amount),
        "penalties": str(record.penalties),
        "interest": str(record.interest),
        "principal": str(record.principal),
        "reopened": reopened,
        "reason": record.reason,
    }
    if reopened:
        # FM10 evidence: the auditor can prove the released-guarantee
        # check RAN and found nothing — a reopen only ever proceeds on
        # this value being false (true is an unconditional 409 above).
        audit_after["had_released_guarantees"] = had_released_guarantees
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="correction.repayment_adjusted",
        entity="repayment_adjustments",
        entity_id=str(adjustment_id),
        before={
            "balance": str(ctx.balance),
            "penalty_due": str(ctx.penalty_due),
            "status": ctx.loan_status.value,
            "adjustment_status": AdjustmentStatus.PENDING_APPROVAL.value,
        },
        after=audit_after,
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="correction.repayment_adjusted",
        payload={
            "adjustment_id": str(adjustment_id),
            "loan_id": str(ctx.loan_id),
            "member_id": str(ctx.member_id),
            "repayment_id": str(record.repayment_id),
            "reversal_txn_ref": reversal.txn_ref,
            "amount": str(ctx.amount),
            "reopened": reopened,
        },
    )
    return AdjustmentResult(
        adjustment_id=adjustment_id,
        reversal_txn_id=reversal.txn_id,
        reversal_txn_ref=reversal.txn_ref,
        amount=ctx.amount,
        penalties=record.penalties,
        interest=record.interest,
        principal=record.principal,
        balance_after=balance_after,
        penalty_due_after=penalty_after,
        status=new_status,
        reopened=reopened,
    )


async def reject_repayment_adjustment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    *,
    version: int,
    reason: str,
) -> AdjustmentRecord:
    """Reject (void) a pending adjustment — optimistic-locked (the
    WOFF void shape).

    Locks the adjustment row ALONE (a single-node locker — the
    DECL/WOFF vote/void pattern; no money lock is taken because
    nothing posts). The rejection is a CHECKER decision: the maker
    cannot decide their own request (the SoD posture, server-side +
    the 0031 DB CHECK — a maker withdrawing a mistaken request asks a
    checker to reject it), and assurance roles are excluded (the
    B2 principle). Rejecting frees the one-live-adjustment slot (the
    0031 partial unique excludes rejected rows), so a corrected
    request can be raised afresh; the rejected row itself is terminal,
    write-once workflow history.

    The checker's rejection RATIONALE is required (the
    four-eyes record must show WHY the request was refused, exactly
    because the freed slot allows a fresh request). It is workflow
    metadata — never a money parameter (v1.1 rule 1 untouched) — and
    lands in the audit ``after`` payload; the outbox payload stays
    ids-only (the established least-payload posture) and no error
    envelope ever echoes it (rule 7). The adjustment row itself is
    untouched by design: its ``reason`` column is the MAKER's request
    rationale and the row is terminal write-once history (0031).
    """
    if not reason.strip():
        # Defence in depth beneath the boundary validation (the
        # Pydantic body already enforces min_length=1).
        raise InvalidInputError("a rejection reason is required")
    ts = datetime.now(UTC)
    row = (
        await session.execute(
            text(
                "SELECT status, maker_id, version FROM repayment_adjustments "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(adjustment_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"repayment adjustment {adjustment_id} not found")
    current = AdjustmentStatus(str(row[0]))
    maker_id = uuid.UUID(str(row[1]))
    adjustment_transition(current, AdjustmentStatus.REJECTED)
    await _require_distinct_non_assurance_checker(session, tenant_id, actor_id, maker_id)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE repayment_adjustments "
                "SET status = :st, checker_id = CAST(:chk AS uuid), decided_at = :ts, "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": AdjustmentStatus.REJECTED.value,
                "chk": str(actor_id),
                "ts": ts,
                "id": str(adjustment_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for adjustment {adjustment_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="correction.adjustment_rejected",
        entity="repayment_adjustments",
        entity_id=str(adjustment_id),
        before={"adjustment_status": current.value},
        after={
            "adjustment_status": AdjustmentStatus.REJECTED.value,
            "checker_id": str(actor_id),
            # The checker's rationale, on the record.
            "reason": reason,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="correction.adjustment_rejected",
        payload={"adjustment_id": str(adjustment_id)},
    )
    return await get_adjustment(session, tenant_id, adjustment_id)


# ---------------------------------------------------------------------------
# Loan write-off
# ---------------------------------------------------------------------------


class WriteOffStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"


_WRITE_OFF_ALLOWED: dict[WriteOffStatus, frozenset[WriteOffStatus]] = {
    WriteOffStatus.REQUESTED: frozenset({WriteOffStatus.APPROVED, WriteOffStatus.REJECTED}),
    WriteOffStatus.APPROVED: frozenset({WriteOffStatus.POSTED, WriteOffStatus.REJECTED}),
    WriteOffStatus.REJECTED: frozenset(),
    WriteOffStatus.POSTED: frozenset(),
}


def _wo_transition(current: WriteOffStatus, target: WriteOffStatus) -> None:
    if target not in _WRITE_OFF_ALLOWED[current]:
        raise ConflictError(f"write-off cannot move from '{current.value}' to '{target.value}'")


@dataclass(frozen=True)
class WriteOffRecord:
    id: uuid.UUID
    loan_id: uuid.UUID
    member_id: uuid.UUID
    balance: Decimal
    penalty_due: Decimal
    total_written_off: Decimal
    classification: str
    provision_pct: Decimal
    reason: str
    status: WriteOffStatus
    requested_by: uuid.UUID | None
    decided_at: datetime | None
    posted_at: datetime | None
    transaction_id: uuid.UUID | None
    version: int
    created_at: datetime


_WRITE_OFF_COLS = (
    "id, loan_id, member_id, balance, penalty_due, total_written_off, "
    "classification, provision_pct, reason, status, requested_by, "
    "decided_at, posted_at, transaction_id, version, created_at"
)


def _row_to_write_off(row: Any) -> WriteOffRecord:
    return WriteOffRecord(
        id=uuid.UUID(str(row[0])),
        loan_id=uuid.UUID(str(row[1])),
        member_id=uuid.UUID(str(row[2])),
        balance=Decimal(str(row[3])),
        penalty_due=Decimal(str(row[4])),
        total_written_off=Decimal(str(row[5])),
        classification=str(row[6]),
        provision_pct=Decimal(str(row[7])),
        reason=str(row[8]),
        status=WriteOffStatus(str(row[9])),
        requested_by=uuid.UUID(str(row[10])) if row[10] is not None else None,
        decided_at=row[11],
        posted_at=row[12],
        transaction_id=uuid.UUID(str(row[13])) if row[13] is not None else None,
        version=int(row[14]),
        created_at=row[15],
    )


async def get_write_off(
    session: AsyncSession, tenant_id: uuid.UUID, write_off_id: uuid.UUID
) -> WriteOffRecord:
    row = (
        await session.execute(
            text(
                f"SELECT {_WRITE_OFF_COLS} FROM loan_write_offs "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(write_off_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan write-off {write_off_id} not found")
    return _row_to_write_off(row)


#: The committee-actionable (LIVE) write-off statuses — the register's
#: leading band (a requested row awaits votes; an approved row awaits
#: posting). 0038 pins the same set in its index expression.
LIVE_WRITE_OFF_STATUSES: frozenset[WriteOffStatus] = frozenset(
    {WriteOffStatus.REQUESTED, WriteOffStatus.APPROVED}
)


def write_offs_register_sql(*, with_cursor: bool) -> str:
    """The write-off committee register.

    ORDER BY (status IN ('requested', 'approved')) DESC,
    created_at DESC, id DESC — the committee's job order: LIVE rows
    (awaiting votes or posting) FIRST, newest first inside each band;
    terminal history (rejected/posted) behind. Uniform-DESC keyset row
    comparison, served by idx_loan_write_offs_register (0038, shipped
    with this query — scalability; EXPLAIN-asserted, falsifiable by
    dropping it). Static fragments chosen in code; every value is a
    bound parameter (v1.1 rule 6); explicit tenant predicate on top of
    forced RLS (rule 4).
    """
    cursor = (
        "AND ((status IN ('requested', 'approved')), created_at, id) "
        "< (CAST(:c_flag AS boolean), CAST(:c_ts AS timestamptz), CAST(:c_id AS uuid)) "
    )
    return (
        f"SELECT {_WRITE_OFF_COLS} FROM loan_write_offs "  # noqa: S608
        "WHERE tenant_id = CAST(:tid AS uuid) "
        f"{cursor if with_cursor else ''}"
        "ORDER BY (status IN ('requested', 'approved')) DESC, created_at DESC, id DESC "
        "LIMIT :limit"
    )


@dataclass(frozen=True)
class WriteOffPage:
    items: list[WriteOffRecord]
    next_cursor: str | None


async def list_write_offs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
) -> WriteOffPage:
    """Keyset write-off register, live-first."""
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor is not None:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext band parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=WRITE_OFFS_SCOPE, entity="write-off register"
        )
        c_flag, c_ts, c_id = parse_band_register_cursor(inner, entity="write-off register")
        params["c_flag"] = c_flag
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    stmt = text(write_offs_register_sql(with_cursor=cursor is not None))
    rows = (await session.execute(stmt, params)).all()
    items = [_row_to_write_off(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_band_register_cursor(
                last.status in LIVE_WRITE_OFF_STATUSES, last.created_at, last.id
            ),
            tenant_id=tenant_id,
            endpoint=WRITE_OFFS_SCOPE,
        )
    return WriteOffPage(items=items, next_cursor=next_cursor)


async def request_write_off(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    reason: str,
) -> WriteOffRecord:
    """Persist the write-off approval snapshot under the loan row lock
    (v1.1 rule 3). The snapshot is DB-level WRITE-ONCE from this
    moment (0025 trigger): the committee approves THESE figures, and
    posting re-verifies them component-by-component. Concurrent
    double-requests collapse to one row (uq_loan_write_offs_open).

    PRUDENTIAL GATE (SASRA-aligned, IFRS-9-consistent):
    write-off is the LAST stage of credit deterioration — derecognition
    of an asset whose recovery is no longer reasonably expected. The
    loan's STORED classification (the arrears job's persisted output)
    must be in NPL_CLASSES (substandard/doubtful/loss); a performing
    ('normal'/'watch') loan is refused with a 409 regardless of quorum
    — a committee vote is an authorisation control, not a prudential
    one, and the 0025 CHECK on loan_write_offs.classification is the
    collusion-resistant DB backstop. A performing-loan write-off
    (death/insurance settlement) is a FUTURE, separately permissioned,
    explicitly named override — a recorded follow-up, never this path.
    Recorded limitation: posting re-verifies balance + penalty_due but NOT
    classification/provision drift — the money components are the
    snapshot contract; a classification that improves between request
    and posting is an open policy question, not a ledger risk."""
    row = (
        await session.execute(
            text(
                "SELECT member_id, balance, penalty_due, classification, provision_pct, "
                "status FROM loans WHERE id = CAST(:id AS uuid) "
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
    classification = str(row[3])
    provision_pct = Decimal(str(row[4]))
    status = LoanStatus(str(row[5]))
    if status is not LoanStatus.ACTIVE:
        raise ConflictError(f"loan {loan_id} is '{status.value}': only active loans write off")
    # The prudential gate — only a stored NPL
    # classification may be written off. Least disclosure: the error
    # names the category, never the figures (they live in the audit
    # row when a write-off does proceed).
    if LoanClass(classification) not in NPL_CLASSES:
        raise ConflictError(
            f"loan {loan_id} is not classified as non-performing; "
            "write-off requires an NPL classification "
            "(see the arrears classification job)"
        )
    total = to_cents(balance + penalty_due)
    if total <= ZERO:
        raise ConflictError(f"loan {loan_id} has nothing to write off")
    write_off_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO loan_write_offs "
                "(id, tenant_id, loan_id, member_id, balance, penalty_due, "
                " total_written_off, classification, provision_pct, reason, "
                " status, requested_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                " CAST(:mid AS uuid), :bal, :pen, :total, :cls, :prov, :reason, "
                " :status, CAST(:actor AS uuid))"
            ),
            {
                "id": str(write_off_id),
                "tid": str(tenant_id),
                "lid": str(loan_id),
                "mid": str(member_id),
                "bal": str(balance),
                "pen": str(penalty_due),
                "total": str(total),
                "cls": classification,
                "prov": str(provision_pct),
                "reason": reason,
                "status": WriteOffStatus.REQUESTED.value,
                "actor": str(actor_id),
            },
        )
    except IntegrityError as exc:
        raise ConflictError(f"a live write-off already exists for loan {loan_id}") from exc
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="write_off.requested",
        entity="loan_write_offs",
        entity_id=str(write_off_id),
        after={
            "loan_id": str(loan_id),
            "member_id": str(member_id),
            "balance": str(balance),
            "penalty_due": str(penalty_due),
            "total_written_off": str(total),
            "classification": classification,
            "reason": reason,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="write_off.requested",
        payload={
            "write_off_id": str(write_off_id),
            "loan_id": str(loan_id),
            "member_id": str(member_id),
            "total_written_off": str(total),
        },
    )
    return await get_write_off(session, tenant_id, write_off_id)


@dataclass(frozen=True)
class WriteOffVoteTally:
    approvals: int
    rejections: int
    decision: Decision | None
    status: WriteOffStatus


async def cast_write_off_vote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    voter_id: uuid.UUID,
    write_off_id: uuid.UUID,
    vote: Vote,
) -> WriteOffVoteTally:
    """Committee vote on a write-off (the P9 machinery, the established
    shape; concurrency safety). The snapshot row lock serialises voters; the DB
    UNIQUE makes double-voting impossible outside this path too.
    Separation of duties: the requester can never vote. Quorum from
    tenant config AT VOTE TIME — never retroactive."""
    row = (
        await session.execute(
            text(
                "SELECT status, requested_by FROM loan_write_offs "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(write_off_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan write-off {write_off_id} not found")
    current = WriteOffStatus(str(row[0]))
    requested_by = uuid.UUID(str(row[1])) if row[1] is not None else None
    if current is not WriteOffStatus.REQUESTED:
        raise ConflictError(f"voting is only open on requested write-offs, not '{current.value}'")
    if requested_by is not None and voter_id == requested_by:
        raise ForbiddenError("the requester of a write-off cannot vote on it")
    try:
        await session.execute(
            text(
                "INSERT INTO loan_write_off_votes (tenant_id, write_off_id, voter_id, vote) "
                "VALUES (CAST(:tid AS uuid), CAST(:wid AS uuid), CAST(:vid AS uuid), :vote)"
            ),
            {
                "tid": str(tenant_id),
                "wid": str(write_off_id),
                "vid": str(voter_id),
                "vote": vote.value,
            },
        )
    except IntegrityError as exc:
        raise ConflictError("committee member has already voted on this write-off") from exc
    tally_rows = (
        await session.execute(
            text(
                "SELECT vote, count(*) FROM loan_write_off_votes "
                "WHERE write_off_id = CAST(:wid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) GROUP BY vote"
            ),
            {"wid": str(write_off_id), "tid": str(tenant_id)},
        )
    ).all()
    counts = {str(r[0]): int(r[1]) for r in tally_rows}
    approvals = counts.get(Vote.APPROVE.value, 0)
    rejections = counts.get(Vote.REJECT.value, 0)
    await record_audit(
        session,
        tenant_id,
        voter_id,
        action="write_off.vote",
        entity="loan_write_offs",
        entity_id=str(write_off_id),
        after={"vote": vote.value, "approvals": approvals, "rejections": rejections},
    )
    decision = decide(approvals, rejections, quorum=await committee_quorum(session, tenant_id))
    status: WriteOffStatus = current
    if decision is not None:
        target = (
            WriteOffStatus.APPROVED if decision is Decision.APPROVED else WriteOffStatus.REJECTED
        )
        _wo_transition(current, target)
        decided = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE loan_write_offs SET status = :st, decided_at = now(), "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"st": target.value, "id": str(write_off_id), "tid": str(tenant_id)},
            ),
        )
        if decided.rowcount != 1:  # pragma: no cover - unreachable under the row lock
            raise ConflictError(f"write-off {write_off_id} changed during voting; retry")
        status = target
        await record_audit(
            session,
            tenant_id,
            voter_id,
            action="write_off.decided",
            entity="loan_write_offs",
            entity_id=str(write_off_id),
            before={"status": current.value},
            after={
                "status": target.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="write_off.decided",
            payload={
                "write_off_id": str(write_off_id),
                "decision": decision.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
    return WriteOffVoteTally(
        approvals=approvals, rejections=rejections, decision=decision, status=status
    )


async def void_write_off(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    write_off_id: uuid.UUID,
    *,
    version: int,
) -> WriteOffRecord:
    """Void an open write-off (requested/approved -> rejected).

    The escape hatch when an approved snapshot has drifted (posting
    returned 409): voiding frees the one-live-write-off-per-loan slot
    so a fresh request captures current figures (the snapshot is
    write-once — it is never edited). A POSTED write-off can never be
    voided (money moved; corrections are reversing entries)."""
    row = (
        await session.execute(
            text(
                "SELECT status FROM loan_write_offs "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(write_off_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan write-off {write_off_id} not found")
    current = WriteOffStatus(str(row[0]))
    _wo_transition(current, WriteOffStatus.REJECTED)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE loan_write_offs SET status = :st, decided_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": WriteOffStatus.REJECTED.value,
                "id": str(write_off_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for write-off {write_off_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="write_off.voided",
        entity="loan_write_offs",
        entity_id=str(write_off_id),
        before={"status": current.value},
        after={"status": WriteOffStatus.REJECTED.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="write_off.voided",
        payload={"write_off_id": str(write_off_id)},
    )
    return await get_write_off(session, tenant_id, write_off_id)


@dataclass(frozen=True)
class WriteOffPostResult:
    write_off_id: uuid.UUID
    loan_id: uuid.UUID
    txn_id: uuid.UUID | None
    txn_ref: str | None
    total_written_off: Decimal
    status: WriteOffStatus


async def post_write_off(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    write_off_id: uuid.UUID,
) -> WriteOffPostResult:
    """Execute an approved write-off atomically (the house gates).

    Snapshot-bind-reverify (v1.1 rule 3, the established snapshot pattern —
    sequence-snapshot-bind-reverify.md): lock the snapshot row FOR
    UPDATE -> status + separation-of-duties checks (the requester may
    never execute) -> retake the loan row FOR UPDATE and re-verify
    EVERY component (balance, penalty_due) against the write-once
    snapshot -> 409 on drift, posting NOTHING -> the terminal
    ACTIVE -> WRITTEN_OFF transition, the WO- provisioning posting
    (balance > 0 only: penalty_due has no ledger leg to derecognise),
    zeroed loan-row receivables, audit + outbox — ONE transaction.

    A4: guarantees behind the loan are deliberately left untouched —
    the claim on the member survives (see the module docstring).
    """
    ts = datetime.now(UTC)
    record_row = (
        await session.execute(
            text(
                f"SELECT {_WRITE_OFF_COLS} FROM loan_write_offs "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(write_off_id), "tid": str(tenant_id)},
        )
    ).first()
    if record_row is None:
        raise NotFoundError(f"loan write-off {write_off_id} not found")
    record = _row_to_write_off(record_row)
    _wo_transition(record.status, WriteOffStatus.POSTED)
    if record.requested_by is not None and actor_id == record.requested_by:
        raise ForbiddenError("the requester of a write-off cannot post it")

    loan_row = (
        await session.execute(
            text(
                "SELECT balance, penalty_due, status FROM loans "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(record.loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if loan_row is None:  # pragma: no cover - FK-consistent
        raise NotFoundError(f"loan {record.loan_id} not found")
    balance = Decimal(str(loan_row[0]))
    penalty_due = Decimal(str(loan_row[1]))
    status = LoanStatus(str(loan_row[2]))
    if status is not LoanStatus.ACTIVE:
        raise ConflictError(f"loan {record.loan_id} is '{status.value}' and cannot be written off")
    # Component-by-component re-verification against the write-once
    # snapshot (never "the current state"): any drift since approval —
    # a repayment, a penalty accrual, an adjustment — is a 409 and
    # NOTHING posts; void and re-request afresh.
    if balance != record.balance or penalty_due != record.penalty_due:
        raise ConflictError(
            f"write-off {write_off_id} snapshot has drifted since approval; "
            "void it and request a fresh write-off"
        )

    loan_transition(LoanStatus.ACTIVE, LoanStatus.WRITTEN_OFF)
    posting: PostingResult | None = None
    if record.balance > ZERO:
        posting = await post_loan_write_off(
            session, tenant_id, record.member_id, record.loan_id, record.balance, actor_id
        )

    await session.execute(
        text(
            # Explicit tenant predicate on the write (v1.1 rule 4).
            "UPDATE loans SET balance = 0, penalty_due = 0, status = :st, "
            "version = version + 1, updated_at = :ts "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "st": LoanStatus.WRITTEN_OFF.value,
            "ts": ts,
            "id": str(record.loan_id),
            "tid": str(tenant_id),
        },
    )
    updated = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE loan_write_offs SET status = :st, posted_at = :ts, "
                "transaction_id = CAST(:txn AS uuid), version = version + 1, "
                "updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "st": WriteOffStatus.POSTED.value,
                "ts": ts,
                "txn": str(posting.txn_id) if posting else None,
                "id": str(write_off_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if updated.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"write-off {write_off_id} changed during posting; retry")

    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="write_off.posted",
        entity="loan_write_offs",
        entity_id=str(write_off_id),
        before={
            "loan_status": LoanStatus.ACTIVE.value,
            "balance": str(record.balance),
            "penalty_due": str(record.penalty_due),
        },
        after={
            "loan_status": LoanStatus.WRITTEN_OFF.value,
            "loan_id": str(record.loan_id),
            "total_written_off": str(record.total_written_off),
            "txn_ref": posting.txn_ref if posting else None,
            # A4: the surviving claim, on the record for review.
            "claim_survives": True,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="write_off.posted",
        payload={
            "write_off_id": str(write_off_id),
            "loan_id": str(record.loan_id),
            "member_id": str(record.member_id),
            "total_written_off": str(record.total_written_off),
            "txn_ref": posting.txn_ref if posting else None,
        },
    )
    return WriteOffPostResult(
        write_off_id=write_off_id,
        loan_id=record.loan_id,
        txn_id=posting.txn_id if posting else None,
        txn_ref=posting.txn_ref if posting else None,
        total_written_off=record.total_written_off,
        status=WriteOffStatus.POSTED,
    )


# ---------------------------------------------------------------------------
# Bad-debt recovery receipts (the A4 explicit branch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryReceiptResult:
    recovery_id: uuid.UUID
    write_off_id: uuid.UUID
    loan_id: uuid.UUID
    member_id: uuid.UUID
    txn_id: uuid.UUID
    txn_ref: str
    amount: Decimal
    recovered_total: Decimal
    outstanding_claim: Decimal
    claim_fully_recovered: bool
    guarantees_released: int
    recovery_case_id: uuid.UUID | None


@dataclass(frozen=True)
class RecoveryReceiptRecord:
    id: uuid.UUID
    amount: Decimal
    txn_id: uuid.UUID
    recovery_case_id: uuid.UUID | None
    recorded_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class RecoveryReceiptPage:
    items: list[RecoveryReceiptRecord]
    recovered_total: Decimal
    outstanding_claim: Decimal
    next_cursor: str | None


async def _recovered_total(
    session: AsyncSession, tenant_id: uuid.UUID, write_off_id: uuid.UUID
) -> Decimal:
    """Cumulative receipts against one claim, reconstructed by summing
    the APPEND-ONLY loan_recoveries rows (v1.1 rule 2) — no mutable
    recovered-total column exists anywhere. Served by
    idx_loan_recoveries_write_off; explicit tenant predicate on top of
    forced RLS (rule 4)."""
    value = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM loan_recoveries "
                "WHERE write_off_id = CAST(:wid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"wid": str(write_off_id), "tid": str(tenant_id)},
        )
    ).scalar_one()
    return to_cents(Decimal(str(value)))


async def record_recovery_receipt(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    write_off_id: uuid.UUID,
    *,
    amount: Decimal,
    channel: Channel,
) -> RecoveryReceiptResult:
    """Record cash received against a POSTED write-off's surviving
    claim, atomically (the house gates).

    Lock order (docs/diagrams/lock-order.md, updated in this MR):
    loan_write_offs FOR UPDATE (T0 — the serialisation point for
    concurrent receipts, so the outstanding-claim math can never race)
    -> members FOR SHARE (T1, the single gatekeeper: holds off
    a concurrent terminal exit, the E20 argument) -> loans FOR UPDATE
    (T4, anchoring the full-recovery guarantee release in E7 order) ->
    the advisory posting tier inside _post.

    The claim figures are SERVER-RESOLVED: total_written_off from the
    write-once 0025 snapshot, the recovered total reconstructed from
    the append-only receipt rows (v1.1 rule 2). The caller supplies
    only the cash actually received; a receipt exceeding the
    outstanding claim is a 409 with zero side effects (the 0030
    constraint trigger is the direct-SQL backstop). The loan is NEVER
    resurrected: written_off stays terminal, balance stays zero — the
    RC- posting recognises recovery INCOME, it does not restore a
    receivable.

    GUARANTEE DISPOSITION (the documented policy): the
    receipt that recovers the claim IN FULL releases the loan's
    surviving guarantees in the same transaction (full recovery
    discharges the sureties exactly like genuine closure — the P10
    hook, reuse 1.1); partial receipts leave them untouched.

    RECOVERY-CASE LINKAGE: the receipt records the loan's
    closed_written_off recovery case explicitly (row column + audit
    payload) when one exists — collections against the surviving claim
    are worked under that case; a write-off without a case links NULL.

    Least disclosure (rule 7): refusals name the category only; the
    exact figures (amount, recovered total, outstanding) live in the
    audit row of the successful receipt.
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("recovery receipt amount must be positive")

    # T0: the write-once snapshot row — the claim anchor. FOR UPDATE
    # serialises concurrent receipts and pins status.
    record_row = (
        await session.execute(
            text(
                f"SELECT {_WRITE_OFF_COLS} FROM loan_write_offs "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(write_off_id), "tid": str(tenant_id)},
        )
    ).first()
    if record_row is None:
        raise NotFoundError(f"loan write-off {write_off_id} not found")
    record = _row_to_write_off(record_row)
    if record.status is not WriteOffStatus.POSTED:
        # Only a POSTED write-off has a derecognised claim to
        # recover against (the 0030 trigger is the DB backstop).
        raise ConflictError(
            f"write-off {write_off_id} is '{record.status.value}': "
            "recovery receipts apply to posted write-offs"
        )

    # T1: member FOR SHARE via the single gatekeeper (reuse,
    # 1.1) — money-in statuses; EXITED refused; blocks a concurrent
    # terminal exit until this receipt commits (the E20 argument, which
    # the exit guard relies on).
    await _require_member(session, tenant_id, record.member_id, operation=MoneyOperation.RECOVERY)

    # T4: the loan row — written_off is re-verified under its own lock,
    # and the full-recovery guarantee release below writes guarantee
    # rows while holding it (the P10/E7 order).
    loan_row = (
        await session.execute(
            text(
                "SELECT status FROM loans WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(record.loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if loan_row is None:  # pragma: no cover - FK-consistent
        raise NotFoundError(f"loan {record.loan_id} not found")
    if LoanStatus(str(loan_row[0])) is not LoanStatus.WRITTEN_OFF:
        raise ConflictError(
            f"loan {record.loan_id} is not written off; recovery receipts "
            "apply to written-off loans only"
        )

    # The outstanding claim, reconstructed from the append-only
    # receipts under the write-off row lock (v1.1 rule 2).
    recovered_before = await _recovered_total(session, tenant_id, write_off_id)
    outstanding_before = to_cents(record.total_written_off - recovered_before)
    if amount > outstanding_before:
        # Least disclosure: category only; the figures live in audit
        # rows staff are entitled to (rule 7).
        raise ConflictError(
            "recovery receipt exceeds the outstanding written-off claim; "
            "record the outstanding amount or the future waiver branch"
        )

    # The recovery-case linkage, resolved server-side (plain
    # MVCC read — closed cases are immutable, no lock needed).
    case_row = (
        await session.execute(
            text(
                "SELECT id FROM recovery_cases "
                "WHERE loan_id = CAST(:lid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'closed_written_off' "
                "ORDER BY closed_at DESC, id DESC LIMIT 1"
            ),
            {"lid": str(record.loan_id), "tid": str(tenant_id)},
        )
    ).first()
    recovery_case_id = uuid.UUID(str(case_row[0])) if case_row is not None else None

    # The RC- posting (DR cash / CR income.bad_debt_recoveries);
    # occurred_at server-resolved NOW inside _post (A2 open-period
    # gate); advisory tier last (E15/E16).
    posting: PostingResult = await post_loan_recovery(
        session,
        tenant_id,
        record.member_id,
        record.loan_id,
        amount,
        channel,
        actor_id,
        write_off_id=write_off_id,
    )

    # The APPEND-ONLY receipt row (0030 triggers block UPDATE/DELETE;
    # the constraint trigger re-checks the claim cap at the DB).
    recovery_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO loan_recoveries "
            "(id, tenant_id, write_off_id, loan_id, member_id, recovery_case_id, "
            " transaction_id, amount, recorded_by) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:wid AS uuid), "
            " CAST(:lid AS uuid), CAST(:mid AS uuid), CAST(:cid AS uuid), "
            " CAST(:txn AS uuid), :amount, CAST(:actor AS uuid))"
        ),
        {
            "id": str(recovery_id),
            "tid": str(tenant_id),
            "wid": str(write_off_id),
            "lid": str(record.loan_id),
            "mid": str(record.member_id),
            "cid": str(recovery_case_id) if recovery_case_id else None,
            "txn": str(posting.txn_id),
            "amount": str(amount),
            "actor": str(actor_id),
        },
    )

    recovered_total = to_cents(recovered_before + amount)
    outstanding_after = to_cents(record.total_written_off - recovered_total)
    fully_recovered = outstanding_after == ZERO

    # The documented guarantee-disposition policy: full recovery
    # discharges the sureties (the P10 closure hook, reused under the
    # loan lock — E7 order); partial receipts leave them untouched.
    guarantees_released = 0
    if fully_recovered:
        guarantees_released = await release_guarantees_for_loan(
            session, tenant_id, actor_id, record.loan_id
        )
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="write_off.claim_recovered",
            entity="loan_write_offs",
            entity_id=str(write_off_id),
            after={
                "loan_id": str(record.loan_id),
                "total_written_off": str(record.total_written_off),
                "recovered_total": str(recovered_total),
                "guarantees_released": guarantees_released,
            },
        )

    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="write_off.recovery_recorded",
        entity="loan_recoveries",
        entity_id=str(recovery_id),
        before={
            "recovered_total": str(recovered_before),
            "outstanding_claim": str(outstanding_before),
        },
        after={
            "write_off_id": str(write_off_id),
            "loan_id": str(record.loan_id),
            "member_id": str(record.member_id),
            "recovery_case_id": str(recovery_case_id) if recovery_case_id else None,
            "txn_ref": posting.txn_ref,
            "amount": str(amount),
            "recovered_total": str(recovered_total),
            "outstanding_claim": str(outstanding_after),
            "claim_fully_recovered": fully_recovered,
            "channel": channel.value,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="write_off.recovery_recorded",
        payload={
            "recovery_id": str(recovery_id),
            "write_off_id": str(write_off_id),
            "loan_id": str(record.loan_id),
            "member_id": str(record.member_id),
            "txn_ref": posting.txn_ref,
            "amount": str(amount),
            "recovered_total": str(recovered_total),
            "outstanding_claim": str(outstanding_after),
            "claim_fully_recovered": fully_recovered,
        },
    )
    return RecoveryReceiptResult(
        recovery_id=recovery_id,
        write_off_id=write_off_id,
        loan_id=record.loan_id,
        member_id=record.member_id,
        txn_id=posting.txn_id,
        txn_ref=posting.txn_ref,
        amount=amount,
        recovered_total=recovered_total,
        outstanding_claim=outstanding_after,
        claim_fully_recovered=fully_recovered,
        guarantees_released=guarantees_released,
        recovery_case_id=recovery_case_id,
    )


async def list_recovery_receipts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    write_off_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
) -> RecoveryReceiptPage:
    """Receipts against one claim, oldest first (keyset, scalability).

    404s on a foreign/unknown write-off before any receipt row is
    touched (the 404-before-facts probe pattern); served by
    idx_loan_recoveries_write_off. The page totals are reconstructed
    from the append-only rows (v1.1 rule 2) so the reader always sees
    the recovered/outstanding position the service enforces.
    """
    record = await get_write_off(session, tenant_id, write_off_id)
    params: dict[str, object] = {
        "wid": str(write_off_id),
        "tid": str(tenant_id),
        "limit": limit + 1,
    }
    cursor_clause = ""
    if cursor is not None:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=WO_RECOVERIES_SCOPE, entity="recovery receipt"
        )
        c_ts, c_id = parse_created_id_cursor(inner, entity="recovery receipt")
        params["c_ts"] = c_ts
        params["c_id"] = c_id
        cursor_clause = "AND (created_at, id) > (CAST(:c_ts AS timestamptz), CAST(:c_id AS uuid)) "
    rows = (
        await session.execute(
            text(
                # Static fragments chosen in code; every value is a
                # bound parameter (v1.1 rule 6); explicit tenant
                # predicate on top of forced RLS (rule 4).
                "SELECT id, amount, transaction_id, recovery_case_id, "  # noqa: S608
                "recorded_by, created_at FROM loan_recoveries "
                "WHERE write_off_id = CAST(:wid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                f"{cursor_clause}"
                "ORDER BY created_at, id LIMIT :limit"
            ),
            params,
        )
    ).all()
    items = [
        RecoveryReceiptRecord(
            id=uuid.UUID(str(r[0])),
            amount=Decimal(str(r[1])),
            txn_id=uuid.UUID(str(r[2])),
            recovery_case_id=uuid.UUID(str(r[3])) if r[3] is not None else None,
            recorded_by=uuid.UUID(str(r[4])),
            created_at=r[5],
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            build_created_id_cursor(items[-1].created_at, items[-1].id),
            tenant_id=tenant_id,
            endpoint=WO_RECOVERIES_SCOPE,
        )
    recovered_total = await _recovered_total(session, tenant_id, write_off_id)
    return RecoveryReceiptPage(
        items=items,
        recovered_total=recovered_total,
        outstanding_claim=to_cents(record.total_written_off - recovered_total),
        next_cursor=next_cursor,
    )
