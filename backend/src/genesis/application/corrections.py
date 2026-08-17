"""Ledger corrections, misc fees & loan write-off (P13.15, gates 1.1-1.6).

The documented correction paths the P7 reversal blocks require:

  1. Repayment adjustment — under the loan row lock, posts the
     reversing ledger legs via P7 (A1 storno: the reversal carries
     reversal_of_id and mirrors the COMPLETE original allocation —
     penalties/interest/principal together; a partial-leg reversal is
     unrepresentable), writes the negative-linked repayments
     correction row, then RECOMPUTES loans.balance / penalty_due /
     schedule paid_amounts from the surviving append-only history
     (v1.1 rule 2). A closed loan re-opens ONLY via the explicit
     CLOSED -> ACTIVE branch of the loan status map (FM6). One atomic
     transaction; a second adjustment of the same repayment is blocked
     by the atomic repayment_adjustments claim (FM2, v1.1 rule 5).

  2. Misc fee posting — the prototype "Fee" drawer type. Fee amounts
     come EXCLUSIVELY from P13.7 tenant configuration (v1.1 rule 1):
     the request carries a code-owned fee type, never an amount; the
     FE- reference comes from the P7 advisory-lock generator.

  3. Loan write-off — committee-approved (the P9 voting machinery)
     transition to written_off, bound to a DB-LEVEL WRITE-ONCE
     snapshot (v1.1 rule 3; the !30 0020-trigger precedent). Posting
     re-verifies the snapshot component-by-component under the loan
     row lock and returns 409 on drift, posting nothing.

POSTING-DATE vs VALUE-DATE (A2, standard GL practice): every
correction posts with occurred_at = NOW, server-resolved inside
ledger._post — never caller-supplied, never backdated — so the P12.5
closed-period gate applies to corrections too (a correction attempted
while today's period is closed is a 409). The ORIGINAL transaction's
date is preserved untouched via the A1 reversal_of_id linkage: a
correction of a closed-period repayment posts into the open period
REFERENCING the original, and the original row is never rewritten
(append-only, 1.5).

MAKER-CHECKER (A3): corrections are the fraud channel. Every route is
gated by the DEDICATED corrections module permissions (never generic
transactions:edit); the adjustment records its maker distinctly and
immutably (repayment_adjustments.maker_id, write-once); corrections
audit under their own entity strings (repayment_adjustments /
loan_write_offs) so they are filterable in review; adjustments route
through the P13.7 approval-band authority check for their amount
(reuse, 1.1). Write-off approval IS the committee quorum — no parallel
approval mechanism exists.

WRITE-OFF IS NOT FORGIVENESS (A4): written_off zeroes the performing
receivable via the WO- provisioning posting, but the legal claim on
the member survives in the write-once snapshot (balance, penalty_due,
total_written_off). Post-write-off recovery (bad-debt-recovery income
posting) is a FUTURE explicit branch tracked as a follow-up issue
referencing P13.16/P19; a repayment against a written_off loan is
refused loudly today (loans.record_repayment status guard), never
silently allocated. Guarantees behind a written-off loan are left
UNTOUCHED (released only on genuine closure): the guarantors' pledges
back the surviving claim; their disposition belongs to the recovery
follow-up, and the decision is audited on the write-off row.

Lock order (docs/diagrams/lock-order.md, updated in this MR):
adjustment takes transactions (T0, FOR UPDATE — serialises against
generic reversal and concurrent adjustment) -> members FOR SHARE (T1,
holds off a concurrent terminal exit) -> loans FOR UPDATE (T4, the
terminal node of the money chain — the P10/P13.8 pattern); write-off
request/execution takes loan_write_offs (T0) -> loans FOR UPDATE (T4);
votes/voids lock the write-off row alone (the DECL pattern); the fee
posting takes members FOR SHARE (T1) only, then the advisory posting
tier. All edges point down the established tiers.
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
from genesis.application.ledger import (
    PostingResult,
    post_fee,
    post_loan_write_off,
    post_reversal,
)
from genesis.application.outbox import enqueue_event
from genesis.application.tenant_settings import committee_quorum, enforce_authority_band

# Reuse-first (gate 1.1): the P13.13 single member-status gatekeeper —
# duplicating its FOR SHARE + capability-map logic here would fork the
# exact policy P13.13 FM2 pins to one code path.
from genesis.application.transactions import _require_member
from genesis.domain.committee import Decision, Vote, decide
from genesis.domain.ledger import Account, Channel
from genesis.domain.lending import NPL_CLASSES, LoanClass, LoanStatus, loan_transition
from genesis.domain.members import MemberStatus, MoneyOperation
from genesis.domain.money import ZERO, to_cents
from genesis.domain.tenant_config import SETTINGS_REGISTRY
from genesis.errors import ConflictError, ForbiddenError, NotFoundError

# ---------------------------------------------------------------------------
# Misc fees (P13.15 part 2)
# ---------------------------------------------------------------------------


class FeeType(StrEnum):
    """Code-owned fee vocabulary: each member maps to the P13.7 settings
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
    """Post a misc fee against a member (gates 1.4, 1.5, 1.6).

    Amount exclusively from P13.7 config (FM5: a caller-supplied
    amount is a 422 at the API contract, and this service never reads
    one). Member gated by the single capability gatekeeper (P13.13
    FM2) under FOR SHARE; posting via P7 with a server-resolved NOW
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
# Repayment adjustment (P13.15 part 1)
# ---------------------------------------------------------------------------


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
                " WHERE a.repayment_id = r.id AND a.tenant_id = r.tenant_id) "
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
    """loans.balance reconstructed from the append-only ledger (FM8):
    disbursed principal minus the signed net of every loans.receivable
    leg attached to this loan's repayments history (originals credit,
    reversals debit — a storno pair nets to zero by construction).

    SCOPE PROOF (N2, review): the repayments join is COMPLETE for an
    adjustable loan. Builder-by-builder over domain/ledger.py, every
    posting that carries a loans.receivable leg:

      * build_disbursement_posting (DR) — the ``principal`` starting
        term of this reconstruction (loans.principal), by definition
        outside the repayments join.
      * build_repayment_posting / build_allocated_repayment_posting
        (CR) — every caller writes a repayments row in the same
        transaction (post_repayment / post_allocated_repayment), so
        both are IN the join.
      * the adjustment reversal (DR mirror) — adjust_repayment writes
        the negative-linked repayments correction row for it, IN the
        join. Generic post_reversal refuses repayment-linked
        transactions and has no API route; the only other receivable
        carrier it could mirror is a disbursement, and no code path
        calls it with one.
      * build_exit_settlement_posting (CR) — only reachable through
        the P12 exit settlement, which terminal-EXITs the member in
        the same transaction; adjust_repayment refuses EXITED members
        before this check can run.
      * build_write_off_posting (CR) — only reachable through
        post_write_off, which moves the loan to WRITTEN_OFF in the
        same transaction; adjust_repayment refuses written-off loans
        before this check can run.

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


async def adjust_repayment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    repayment_id: uuid.UUID,
    *,
    reason: str,
) -> AdjustmentResult:
    """Reverse one repayment's COMPLETE allocation and restore loan
    state, atomically (FM1-FM3, FM6-FM8; gates 1.4, 1.5).

    Lock order: transactions FOR UPDATE (T0) -> members FOR SHARE (T1)
    -> loans FOR UPDATE (T4) — see the module docstring and
    lock-order.md. The member FOR SHARE holds off a concurrent
    terminal exit (which takes the member row FOR UPDATE) so an
    adjustment can never re-open a loan underneath a posting
    settlement (A5: an EXITED member's loan is refused — the P12
    terminal-state rule).

    REOPEN SECURITY GATE (FM10, review B2 — banking principle: a
    discharged surety cannot be unilaterally re-bound). P10 releases
    the loan's guarantees at closure, legally discharging the
    guarantors; an adjustment that reopens the loan would therefore
    resurrect an UNSECURED exposure. Default posture is REFUSE: when
    the CLOSED -> ACTIVE branch would trigger and any guarantee linked
    to this loan is 'released', the adjustment is a 409 (least
    disclosure: the error names the guarantee disposition, never
    amounts or guarantors). The operator's remedy is the P13.14
    substitution / re-pledge flow — re-secure the exposure FIRST, then
    adjust. The read runs under the loan FOR UPDATE (no guarantee-row
    lock is taken — no new lock-graph edge); release itself only ever
    happens under the same loan lock (P10 closure) or the P13.14
    guarantee workflow, so the plain read cannot race a concurrent
    release. If the closed loan had no released guarantees the reopen
    proceeds, and the audit row carries an explicit
    had_released_guarantees: false so an auditor can prove the check
    ran. The guarantor reinstatement/substitution policy decision this
    defers is recorded on issue #21.
    """
    ts = datetime.now(UTC)
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

    # T1: member FOR SHARE — the P13.13 single-gatekeeper terminal
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
    # writer takes (FM3: a concurrent repayment waits here).
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
        # the write-once snapshot; recovery is the future explicit
        # branch, never an adjustment.
        raise ConflictError(f"loan {loan_id} is written off; adjustments are refused")

    # FM10 (review B2): an adjustment that would REOPEN a closed loan
    # must not resurrect an unsecured exposure. P10 released the
    # guarantees at closure (guarantees.status -> 'released'), legally
    # discharging the guarantors — so if any released guarantee is
    # linked to this loan, REFUSE before any side effect. Plain read
    # under the loan FOR UPDATE (no guarantee lock — no new lock-graph
    # edge; every release path holds this same loan lock). Least
    # disclosure: the category, never amounts or guarantor identities.
    had_released_guarantees = False
    if status is LoanStatus.CLOSED:
        released_row = (
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
        had_released_guarantees = released_row is not None
        if had_released_guarantees:
            raise ConflictError(
                f"loan {loan_id} cannot be reopened by this adjustment: its "
                "guarantees were released at closure and the guarantors are "
                "discharged; substitute or re-pledge security (P13.14) "
                "before adjusting"
            )

    # The COMPLETE original allocation, reconstructed from the
    # append-only legs (A1: all components together, never a subset).
    penalties, interest, principal = await _allocation_from_legs(
        session, tenant_id, original_txn_id
    )
    if to_cents(penalties + interest + principal) != amount:
        raise ConflictError(
            f"transaction {original_txn_id} legs do not reconstruct the repayment amount"
        )

    # A3: adjustments above the configured band route through the SAME
    # authority check as approvals (reuse, 1.1) — under the loan lock.
    await enforce_authority_band(session, tenant_id, actor_id, amount)

    # Reopened? Decided HERE, under the loan lock and BEFORE the claim
    # INSERT: reopened_loan is write-once (the 0025 trigger permits no
    # post-insert change of it — only the reversal_transaction_id fill
    # from NULL), so the decision must ride the INSERT itself. FM6: the
    # ONE documented branch of the status map.
    reopened = False
    new_status: LoanStatus = status
    if status is LoanStatus.CLOSED:
        new_status = loan_transition(LoanStatus.CLOSED, LoanStatus.ACTIVE)
        reopened = True

    # FM2: the atomic one-adjustment-per-repayment claim (v1.1 rule 5).
    adjustment_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO repayment_adjustments "
                "(id, tenant_id, repayment_id, loan_id, original_transaction_id, "
                " maker_id, reason, amount, penalties, interest, principal, reopened_loan) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                " CAST(:lid AS uuid), CAST(:txn AS uuid), CAST(:maker AS uuid), "
                " :reason, :amount, :pen, :int, :prn, :reopened) "
                "ON CONFLICT (tenant_id, repayment_id) DO NOTHING RETURNING id"
            ),
            {
                "id": str(adjustment_id),
                "tid": str(tenant_id),
                "rid": str(repayment_id),
                "lid": str(loan_id),
                "txn": str(original_txn_id),
                "maker": str(actor_id),
                "reason": reason,
                "amount": str(amount),
                "pen": str(penalties),
                "int": str(interest),
                "prn": str(principal),
                "reopened": reopened,
            },
        )
    ).first()
    if claimed is None:
        raise ConflictError(f"repayment {repayment_id} has already been adjusted")

    # A1/A2: the storno posting — mirror-image legs linked via
    # reversal_of_id, occurred_at = NOW (server-resolved in _post; the
    # open-period gate applies). The original row is never rewritten.
    reversal: PostingResult = await post_reversal(
        session, tenant_id, original_txn_id, actor_id, allow_repayment_correction=True
    )

    # The negative-linked repayments correction row (the storno pair in
    # the servicing history; conservation: original + correction = 0).
    await session.execute(
        text(
            "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
            "CAST(:txn AS uuid), :amount)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "lid": str(loan_id),
            "txn": str(reversal.txn_id),
            "amount": str(to_cents(-amount)),
        },
    )

    # Fill the reversal linkage on the claim row — the ONLY post-insert
    # UPDATE the 0025 write-once trigger permits (reversal_transaction_id
    # from NULL); every other column, reopened_loan included, was final
    # at INSERT time. Audited money history stays intact.
    linked = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE repayment_adjustments "
                "SET reversal_transaction_id = CAST(:rev AS uuid) "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "rev": str(reversal.txn_id),
                "id": str(adjustment_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if linked.rowcount != 1:  # pragma: no cover - unreachable under the claim
        raise ConflictError(f"adjustment {adjustment_id} vanished mid-transaction")

    # State restore RECOMPUTES from the surviving history (A1, FM1):
    # the exact inverse of the forward update for the loan-row figures,
    # and a full replay for the schedule (its forward application was
    # capped per row, so add-back alone cannot restore it).
    balance_after = to_cents(balance + principal)
    penalty_after = to_cents(penalty_due + penalties)
    await _rebuild_schedule_paid_amounts(session, tenant_id, loan_id)

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
            "id": str(loan_id),
            "tid": str(tenant_id),
        },
    )

    # FM8 conservation self-check, in-transaction: the restored balance
    # must reconstruct from the append-only ledger to the cent. A
    # mismatch aborts the WHOLE adjustment (no partial state).
    reconstructed = await _reconstructed_balance(session, tenant_id, loan_id, principal_disbursed)
    if reconstructed != balance_after:
        raise ConflictError(
            f"adjustment aborted: restored balance {balance_after} does not "
            f"reconstruct from the ledger ({reconstructed})"
        )

    audit_after: dict[str, Any] = {
        "balance": str(balance_after),
        "penalty_due": str(penalty_after),
        "status": new_status.value,
        "repayment_id": str(repayment_id),
        "original_txn_id": str(original_txn_id),
        "reversal_txn_ref": reversal.txn_ref,
        "amount": str(amount),
        "penalties": str(penalties),
        "interest": str(interest),
        "principal": str(principal),
        "reopened": reopened,
        "reason": reason,
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
            "balance": str(balance),
            "penalty_due": str(penalty_due),
            "status": status.value,
        },
        after=audit_after,
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="correction.repayment_adjusted",
        payload={
            "adjustment_id": str(adjustment_id),
            "loan_id": str(loan_id),
            "member_id": str(member_id),
            "repayment_id": str(repayment_id),
            "reversal_txn_ref": reversal.txn_ref,
            "amount": str(amount),
            "reopened": reopened,
        },
    )
    return AdjustmentResult(
        adjustment_id=adjustment_id,
        reversal_txn_id=reversal.txn_id,
        reversal_txn_ref=reversal.txn_ref,
        amount=amount,
        penalties=penalties,
        interest=interest,
        principal=principal,
        balance_after=balance_after,
        penalty_due_after=penalty_after,
        status=new_status,
        reopened=reopened,
    )


# ---------------------------------------------------------------------------
# Loan write-off (P13.15 part 3)
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


async def request_write_off(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    reason: str,
) -> WriteOffRecord:
    """Persist the write-off approval snapshot under the loan row lock
    (v1.1 rule 3; FM4). The snapshot is DB-level WRITE-ONCE from this
    moment (0025 trigger): the committee approves THESE figures, and
    posting re-verifies them component-by-component. Concurrent
    double-requests collapse to one row (uq_loan_write_offs_open).

    PRUDENTIAL GATE (FM9, review B3 — SASRA-aligned, IFRS-9-consistent):
    write-off is the LAST stage of credit deterioration — derecognition
    of an asset whose recovery is no longer reasonably expected. The
    loan's STORED classification (the arrears job's persisted output)
    must be in NPL_CLASSES (substandard/doubtful/loss); a performing
    ('normal'/'watch') loan is refused with a 409 regardless of quorum
    — a committee vote is an authorisation control, not a prudential
    one, and the 0025 CHECK on loan_write_offs.classification is the
    collusion-resistant DB backstop. A performing-loan write-off
    (death/insurance settlement) is a FUTURE, separately permissioned,
    explicitly named override — tracked on issue #21, never this path.
    N5 (recorded): posting re-verifies balance + penalty_due but NOT
    classification/provision drift — the money components are the
    snapshot contract; a classification that improves between request
    and posting is a policy question for #21, not a ledger risk."""
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
    # FM9 (review B3): the prudential gate — only a stored NPL
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
    """Committee vote on a write-off (the P9 machinery, the P12/!30
    shape; gate 1.4). The snapshot row lock serialises voters; the DB
    UNIQUE makes double-voting impossible outside this path too.
    Separation of duties: the requester can never vote. Quorum from
    tenant config AT VOTE TIME (P13.7) — never retroactive."""
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
    """Execute an approved write-off atomically (FM4; gates 1.4, 1.5).

    Snapshot-bind-reverify (v1.1 rule 3, the P12/!30 pattern —
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
