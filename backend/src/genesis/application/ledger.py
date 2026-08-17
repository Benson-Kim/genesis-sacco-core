"""Double-entry ledger posting services (MASTER_PROMPT P7, gates 1.4, 1.5).

This module owns the application-service layer for all ledger operations:

  * Reference generation — pg_advisory_xact_lock + UNIQUE + retry (gate 1.4)
  * Atomic posting — transactions + ledger_entries written together
  * Audit logging — every posting written in-transaction (gate 1.5)
  * Outbox events — side-effects via transactional outbox only (gate 1.2)
  * Disbursement — one atomic transaction: approval check + posting +
    schedule generation + outbox event (gate 1.5)

No I/O in the domain layer; all DB access lives here.
"""

from __future__ import annotations

import calendar
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.accounting_periods import assert_open_period
from genesis.application.audit import record_audit
from genesis.application.loan_applications import guarantee_total
from genesis.application.loan_products import get_product
from genesis.application.outbox import enqueue_event
from genesis.domain.ledger import (
    Account,
    Channel,
    LedgerLine,
    PostingSpec,
    Side,
    TxnType,
    build_allocated_repayment_posting,
    build_deposit_interest_posting,
    build_deposit_posting,
    build_disbursement_posting,
    build_dividend_distribution_posting,
    build_exit_settlement_posting,
    build_loan_interest_accrual_posting,
    build_repayment_posting,
    build_reversal_posting,
    build_share_topup_posting,
    build_share_transfer_in_posting,
    build_share_transfer_out_posting,
    build_unclaimed_dividend_posting,
    build_withdrawal_posting,
    ref_prefix,
)
from genesis.domain.lending import (
    ApplicationStage,
    InvalidTransitionError,
    RepaymentAllocation,
    ScheduledInstallment,
    build_schedule,
    transition,
)
from genesis.domain.money import to_cents
from genesis.errors import ConflictError, NotFoundError

# ---------------------------------------------------------------------------
# Reference generation (gate 1.4)
# ---------------------------------------------------------------------------

#: Advisory lock namespace — fits in PostgreSQL int4 (signed 32-bit).
#: "ldgr" ASCII bytes = 0x6C646772, masked to int4 positive range.
_ADVISORY_NS: int = 0x6C646772 & 0x7FFFFFFF  # 1818649458


def _advisory_key(tenant_id: uuid.UUID, prefix: str) -> int:
    """Derive a stable int4 advisory lock key from tenant + prefix.

    Both arguments to pg_advisory_xact_lock(int4, int4) must fit in a
    signed 32-bit integer.  We XOR the first 4 bytes of the tenant UUID
    with a stable hash of the prefix, then mask to the positive int4 range.
    """
    tid_int = int.from_bytes(tenant_id.bytes[:4], "big")
    # Use a stable, well-distributed hash: CRC-32 of the prefix bytes.
    # (Python's hash() is randomised per process; a naive character sum
    # collides for anagram-like prefixes such as "SH-" and "WD-", forcing
    # needless serialisation across unrelated sequences.)
    prefix_hash = zlib.crc32(prefix.encode()) & 0x7FFFFFFF
    return (tid_int ^ prefix_hash) & 0x7FFFFFFF


async def _next_ref(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    prefix: str,
) -> str:
    """Allocate the next sequential reference under an advisory lock.

    Uses pg_advisory_xact_lock to serialise concurrent callers within the
    same transaction scope, then increments the per-tenant/per-prefix
    counter atomically (gate 1.4).  The UNIQUE constraint on
    (tenant_id, txn_ref) in the transactions table is the final safety net.
    """
    lock_key = _advisory_key(tenant_id, prefix)
    # Acquire an exclusive transaction-level advisory lock.
    # Both values are computed integers (not user input), so embedding them
    # directly in the SQL literal is safe and avoids SQLAlchemy parameter-
    # binding conflicts with the PostgreSQL ::int4 cast syntax.
    await session.execute(text(f"SELECT pg_advisory_xact_lock({_ADVISORY_NS}, {lock_key})"))
    # Upsert the sequence row and return the new value.
    row = (
        await session.execute(
            text(
                "INSERT INTO txn_ref_sequences (tenant_id, prefix, last_val) "
                "VALUES (CAST(:tid AS uuid), :prefix, 1) "
                "ON CONFLICT (tenant_id, prefix) DO UPDATE "
                "SET last_val = txn_ref_sequences.last_val + 1 "
                "RETURNING last_val"
            ),
            {"tid": str(tenant_id), "prefix": prefix},
        )
    ).first()
    if row is None:
        raise RuntimeError("txn_ref_sequences upsert returned no row")
    seq: int = int(row[0])
    return f"{prefix}{seq:06d}"


# ---------------------------------------------------------------------------
# Core posting primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostingResult:
    txn_id: uuid.UUID
    txn_ref: str


async def _post(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    spec: PostingSpec,
    actor_id: uuid.UUID | None = None,
    *,
    occurred_at: datetime | None = None,
    reversal_of_id: uuid.UUID | None = None,
) -> PostingResult:
    """Write one transaction + its ledger lines atomically.

    Caller must already hold a tenant-scoped session (tenancy middleware or
    tenant_session context manager).  The DB constraint trigger enforces
    balance at commit time.
    """
    spec.assert_balanced()

    prefix = ref_prefix(spec.txn_type, spec.channel)
    ts = occurred_at or datetime.now(UTC)

    # Issue #12 (gates 1.4-1.6): EVERY posting validates occurred_at
    # server-side before anything is written — never future-dated,
    # never inside a closed accounting period (409). The shared
    # advisory barrier inside serialises against a concurrent period
    # close; the 0012 trigger is the DB-level backstop.
    await assert_open_period(session, tenant_id, ts)

    # pg_advisory_xact_lock serialises all callers for the same tenant+prefix
    # within the transaction, so the sequence counter is always unique.
    # The UNIQUE constraint on (tenant_id, txn_ref) is the final safety net.
    txn_ref = await _next_ref(session, tenant_id, prefix)
    txn_id = uuid.uuid4()

    await session.execute(
        text(
            "INSERT INTO transactions "
            "(id, tenant_id, txn_ref, member_id, type, amount, channel, "
            " occurred_at, reversal_of_id) "
            "VALUES "
            "(CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
            " CAST(:mid AS uuid), :type, :amount, :channel, :ts, "
            " CAST(:rev_of AS uuid))"
        ),
        {
            "id": str(txn_id),
            "tid": str(tenant_id),
            "ref": txn_ref,
            "mid": str(member_id) if member_id else None,
            "type": spec.txn_type.value,
            "amount": str(spec.amount),
            "channel": spec.channel.value,
            "ts": ts,
            "rev_of": str(reversal_of_id) if reversal_of_id else None,
        },
    )

    # Insert all ledger lines.
    await _insert_lines(session, tenant_id, txn_id, spec.lines)

    # Audit log (gate 1.5) via the shared in-transaction writer.
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="ledger.post",
        entity="transactions",
        entity_id=str(txn_id),
        after={
            "txn_ref": txn_ref,
            "type": spec.txn_type.value,
            "channel": spec.channel.value,
            "amount": str(spec.amount),
            "member_id": str(member_id) if member_id else None,
        },
    )

    return PostingResult(txn_id=txn_id, txn_ref=txn_ref)


async def _insert_lines(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    txn_id: uuid.UUID,
    lines: tuple[LedgerLine, ...],
) -> None:
    for line in lines:
        await session.execute(
            text(
                "INSERT INTO ledger_entries "
                "(id, tenant_id, transaction_id, account, side, amount) "
                "VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), "
                " CAST(:txn AS uuid), :account, :side, :amount)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "txn": str(txn_id),
                "account": line.account.value,
                "side": line.side.value,
                "amount": str(line.amount),
            },
        )


# ---------------------------------------------------------------------------
# Public posting services
# ---------------------------------------------------------------------------


async def post_deposit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    amount: Decimal,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post a member deposit and enqueue a notification event."""
    spec = build_deposit_posting(amount, channel)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.deposit_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
            "channel": channel.value,
        },
    )
    return result


async def post_withdrawal(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    amount: Decimal,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post a member withdrawal."""
    spec = build_withdrawal_posting(amount, channel)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.withdrawal_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
            "channel": channel.value,
        },
    )
    return result


async def post_share_topup(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    amount: Decimal,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post a share top-up."""
    spec = build_share_topup_posting(amount, channel)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.share_topup_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
            "channel": channel.value,
        },
    )
    return result


async def post_repayment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    loan_id: uuid.UUID,
    amount: Decimal,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post a loan repayment and record it in the repayments table.

    This is a posting primitive only: it writes the balanced ledger legs
    and the repayments row for the amount received.  Allocation of a
    repayment across interest / principal / penalties, and the resulting
    loans.balance update, are owned by P10's repayment allocation service,
    which will call this primitive with the split legs.  Do not add
    allocation logic here.
    """
    await _assert_loan_owned_by_member(session, tenant_id, loan_id, member_id)
    spec = build_repayment_posting(amount, channel)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    # Record in repayments table (rounded spec.amount so the auxiliary
    # row can never disagree with the ledger legs — gate 1.5).
    await session.execute(
        text(
            "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
            "CAST(:lid AS uuid), CAST(:txn AS uuid), :amount)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "lid": str(loan_id),
            "txn": str(result.txn_id),
            "amount": str(spec.amount),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.repayment_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "loan_id": str(loan_id),
            "amount": str(spec.amount),
            "channel": channel.value,
        },
    )
    return result


async def _assert_loan_owned_by_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    loan_id: uuid.UUID,
    member_id: uuid.UUID,
) -> None:
    """Refuse a repayment against a loan the member does not own.

    External Codex review finding, re-derived: the posting primitives
    trusted the caller's (member_id, loan_id) pair, so a coding error
    upstream could credit member A's cash against member B's loan. The
    P10 servicing path derives member_id FROM the loan so it cannot
    mismatch; this guard makes the primitive itself safe (gate 1.5).
    Explicit tenant predicate on top of RLS (gate 1.6 v1.1). Least
    disclosure: the error names ids the caller already supplied,
    never balances.
    """
    row = (
        await session.execute(
            text(
                "SELECT member_id FROM loans "
                "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"lid": str(loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan {loan_id} not found")
    if uuid.UUID(str(row[0])) != member_id:
        raise ConflictError(f"loan {loan_id} does not belong to member {member_id}")


async def post_allocated_repayment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    loan_id: uuid.UUID,
    allocation: RepaymentAllocation,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post a repayment with split legs per the P10 allocation contract.

    The allocation (penalties -> interest -> principal) is computed by
    the loan servicing service; this primitive writes the balanced
    split-leg posting, the repayments row, and the outbox event. Loan
    balance/schedule updates are owned by the caller in the same
    transaction.
    """
    await _assert_loan_owned_by_member(session, tenant_id, loan_id, member_id)
    spec = build_allocated_repayment_posting(
        allocation.penalties, allocation.interest, allocation.principal, channel
    )
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await session.execute(
        text(
            "INSERT INTO repayments (id, tenant_id, loan_id, transaction_id, amount) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
            "CAST(:lid AS uuid), CAST(:txn AS uuid), :amount)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "lid": str(loan_id),
            "txn": str(result.txn_id),
            "amount": str(allocation.total),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.repayment_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "loan_id": str(loan_id),
            "amount": str(allocation.total),
            "penalties": str(allocation.penalties),
            "interest": str(allocation.interest),
            "principal": str(allocation.principal),
            "channel": channel.value,
        },
    )
    return result


async def post_loan_interest_accrual(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    amount: Decimal,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Accrue interest earned on a loan (INT- ref).

    DR interest.receivable / CR income.interest.
    """
    spec = build_loan_interest_accrual_posting(amount)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.loan_interest_accrued",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id) if member_id else None,
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
        },
    )
    return result


async def post_deposit_interest(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID | None,
    amount: Decimal,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post interest paid on member deposits (INT- ref).

    DR interest.expense / CR member.deposits.
    """
    spec = build_deposit_interest_posting(amount)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.deposit_interest_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id) if member_id else None,
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
        },
    )
    return result


async def post_dividend_distribution(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    dividend: Decimal,
    rebate: Decimal,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post one member's dividend + rebate payout (DV- ref, P13.11).

    DR expense.dividends / CR member.shares and DR expense.rebates /
    CR member.deposits — the dividend capitalises into share capital
    so it compounds into the next financial year's basis (gate 1.5).
    Runs in the caller's transaction; the P13.11 distribution job owns
    the atomic unit (claim + posting + balance updates + audit).
    Payload carries ids and amounts only — never names (gate 1.6).
    """
    spec = build_dividend_distribution_posting(dividend, rebate)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.dividend_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "dividend": str(to_cents(dividend)),
            "rebate": str(to_cents(rebate)),
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
        },
    )
    return result


async def post_unclaimed_dividend(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    dividend: Decimal,
    rebate: Decimal,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Park a mid-run-exited member's entitlement as a payable (issue #19 P3).

    DV- ref: DR expense.dividends / expense.rebates, CR
    liability.unclaimed_dividends — the member's accounts are never
    touched (they exited; their balances are settled and zeroed). The
    transaction stays member-attributed so the payable is traceable to
    the member it is owed to; it carries no member-account legs, so no
    ledger-reconstructed basis anywhere changes. Runs in the caller's
    transaction; the P13.11 distribution job owns the atomic unit
    (claim + posting + audit). The outbox event is the ALERT surface
    (gate 1.2): operators watch dividend.unclaimed via
    ledger.dividend_unclaimed exactly like the other posting events.
    Payload carries ids and amounts only — never names (gate 1.6).
    """
    spec = build_unclaimed_dividend_posting(dividend, rebate)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.dividend_unclaimed",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "dividend": str(to_cents(dividend)),
            "rebate": str(to_cents(rebate)),
            "amount": str(spec.amount),  # rounded, matches the ledger (1.5)
        },
    )
    return result


async def post_share_transfer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    from_member_id: uuid.UUID,
    to_member_id: uuid.UUID,
    amount: Decimal,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> tuple[PostingResult, PostingResult]:
    """Post both legs of a member-to-member share transfer (ST- refs).

    Two member-attributed transactions through the clearing account
    (see genesis.domain.ledger for why one netted posting would blind
    the ledger-reconstructed share basis); the clearing account nets
    to zero inside this one transaction. The caller (P13.11
    transfer_shares) owns the atomic unit and the row locks. One
    outbox event describes the whole transfer (gates 1.2, 1.5).
    """
    out_spec = build_share_transfer_out_posting(amount)
    in_spec = build_share_transfer_in_posting(amount)
    out_result = await _post(
        session, tenant_id, from_member_id, out_spec, actor_id, occurred_at=occurred_at
    )
    in_result = await _post(
        session, tenant_id, to_member_id, in_spec, actor_id, occurred_at=occurred_at
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.share_transfer_posted",
        payload={
            "out_txn_id": str(out_result.txn_id),
            "out_txn_ref": out_result.txn_ref,
            "in_txn_id": str(in_result.txn_id),
            "in_txn_ref": in_result.txn_ref,
            "from_member_id": str(from_member_id),
            "to_member_id": str(to_member_id),
            "amount": str(out_spec.amount),  # rounded, matches the ledger (1.5)
        },
    )
    return out_result, in_result


async def post_exit_settlement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    shares: Decimal,
    deposits: Decimal,
    loan_principal: Decimal,
    loan_interest: Decimal,
    loan_penalties: Decimal,
    fee: Decimal,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PostingResult:
    """Post the P12 exit set-off (WD- ref) and enqueue the notification event.

    One balanced posting: the member's equity is debited, the netted
    loan payoff, exit fee and net cash refund are credited. Runs in the
    caller's transaction; the P12 settlement service owns the full
    atomic unit (gate 1.5). Payload carries ids and amounts only —
    never names or contact details (gate 1.6).
    """
    spec = build_exit_settlement_posting(
        shares=shares,
        deposits=deposits,
        loan_principal=loan_principal,
        loan_interest=loan_interest,
        loan_penalties=loan_penalties,
        fee=fee,
        channel=channel,
    )
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=occurred_at)
    net = spec.amount - loan_principal - loan_interest - loan_penalties - fee
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.exit_settlement_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "member_id": str(member_id),
            "equity": str(spec.amount),
            "net_payable": str(net),
            "channel": channel.value,
        },
    )
    return result


async def post_reversal(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    original_txn_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> PostingResult:
    """Post a reversing entry for a previous transaction (gate 1.5).

    Fetches the original lines, builds the mirror image, and posts it as a
    new transaction linked via reversal_of_id.  The original is never
    modified.  Guards:
      * a transaction that already has a reversal cannot be reversed again
        (ConflictError; the partial UNIQUE index is the final safety net)
      * a reversal itself cannot be reversed — correct by re-posting the
        original instead (ConflictError)
      * a transaction with repayment auxiliary records cannot be
        generically reversed (ConflictError; external Codex review,
        re-derived): this primitive only mirrors ledger legs — it knows
        nothing of loans.balance, penalty_due, schedule state, or the
        repayments history row, so a generic reversal would leave the
        repayment standing as a paid amount against a restored ledger
        balance. Repayment corrections need the dedicated adjustment
        service (BUILD_PROMPTS P13.15) that undoes the allocation
        component-by-component in one transaction.
    """
    # Load and lock the original transaction (FOR UPDATE serialises
    # concurrent reversal attempts; rows are append-only so no trigger fires).
    txn_row = (
        await session.execute(
            text(
                # Explicit tenant predicate on the row-lock read, on top
                # of RLS (defence in depth, gate 1.6 v1.1; issue #17).
                "SELECT member_id, type, amount, channel, reversal_of_id "
                "FROM transactions WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(original_txn_id), "tid": str(tenant_id)},
        )
    ).first()
    if txn_row is None:
        raise NotFoundError(f"transaction {original_txn_id} not found")

    member_id_raw, txn_type_str, amount_str, channel_str, orig_reversal_of = txn_row
    member_id = uuid.UUID(str(member_id_raw)) if member_id_raw else None

    if orig_reversal_of is not None:
        raise ConflictError(
            f"transaction {original_txn_id} is itself a reversal and cannot be reversed"
        )

    existing_reversal = (
        await session.execute(
            text(
                "SELECT id FROM transactions WHERE reversal_of_id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"id": str(original_txn_id), "tid": str(tenant_id)},
        )
    ).first()
    if existing_reversal is not None:
        raise ConflictError(f"transaction {original_txn_id} has already been reversed")

    repayment_row = (
        await session.execute(
            text(
                # Explicit tenant predicate on top of RLS (gate 1.6 v1.1).
                "SELECT 1 FROM repayments "
                "WHERE transaction_id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"id": str(original_txn_id), "tid": str(tenant_id)},
        )
    ).first()
    if repayment_row is not None:
        raise ConflictError(
            f"transaction {original_txn_id} has repayment records and cannot be "
            "generically reversed; use the repayment adjustment flow"
        )

    # Load original ledger lines.
    line_rows = (
        await session.execute(
            text(
                "SELECT account, side, amount FROM ledger_entries "
                "WHERE transaction_id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) ORDER BY created_at"
            ),
            {"id": str(original_txn_id), "tid": str(tenant_id)},
        )
    ).all()
    if not line_rows:
        raise NotFoundError(f"no ledger lines for transaction {original_txn_id}")

    original_lines = tuple(
        LedgerLine(
            account=Account(str(r[0])),
            side=Side(str(r[1])),
            amount=Decimal(str(r[2])),
        )
        for r in line_rows
    )
    original_spec = PostingSpec(
        txn_type=TxnType(txn_type_str),
        channel=Channel(channel_str),
        amount=Decimal(str(amount_str)),
        lines=original_lines,
    )
    reversal_spec = build_reversal_posting(original_spec)

    result = await _post(
        session,
        tenant_id,
        member_id,
        reversal_spec,
        actor_id,
        reversal_of_id=original_txn_id,
    )

    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="ledger.reversal",
        entity="transactions",
        entity_id=str(result.txn_id),
        after={
            "reversal_of": str(original_txn_id),
            "txn_ref": result.txn_ref,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="ledger.reversal_posted",
        payload={
            "txn_id": str(result.txn_id),
            "txn_ref": result.txn_ref,
            "reversal_of": str(original_txn_id),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Disbursement — atomic application-service transaction (gate 1.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisbursementResult:
    loan_id: uuid.UUID
    txn_id: uuid.UUID
    txn_ref: str
    schedule: list[ScheduledInstallment]


async def disburse_loan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    application_id: uuid.UUID,
    channel: Channel,
    actor_id: uuid.UUID | None = None,
    disbursed_at: datetime | None = None,
) -> DisbursementResult:
    """Atomic disbursement: approval check + posting + schedule + outbox.

    All steps run in the caller's transaction (gate 1.5).  If any step
    fails the whole transaction rolls back — no partial success.

    Steps:
      1. Lock the application row; verify stage == approved.
      2. Verify the deposit-multiplier eligibility under the deposit
         account row lock (issue #15), then refuse any unconsented
         (pledged) guarantee — collateral is never activated without
         the guarantor's recorded consent (external Codex review,
         re-derived); transition stage to disbursed.
      3. Create the loan record; link the application's consented
         (active) guarantees to it (issue #15).
      4. Post the disbursement ledger entry.
      5. Generate and persist the amortisation schedule.
      6. Enqueue the outbox event.
    """
    ts = disbursed_at or datetime.now(UTC)

    # Step 1: lock and verify application stage.
    app_row = (
        await session.execute(
            text(
                # Explicit tenant predicate on the row-lock read, on top
                # of RLS (defence in depth, gate 1.6 v1.1; issue #17).
                "SELECT id, member_id, product_id, amount, term_months, "
                "rate_pct, stage, version "
                "FROM loan_applications "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if app_row is None:
        raise NotFoundError(f"loan application {application_id} not found")

    (
        _,
        member_id_raw,
        product_id_raw,
        amount_str,
        term_months,
        rate_pct_str,
        stage_str,
        version,
    ) = app_row

    member_id = uuid.UUID(str(member_id_raw))
    product_id = uuid.UUID(str(product_id_raw))
    principal = Decimal(str(amount_str))
    rate_pct = Decimal(str(rate_pct_str))
    term_months = int(term_months)

    # Step 2: validate and transition stage.
    try:
        transition(ApplicationStage(stage_str), ApplicationStage.DISBURSED)
    except InvalidTransitionError as exc:
        raise ConflictError(f"cannot disburse application in stage '{stage_str}'") from exc

    # Step 2b: product deposit-multiplier eligibility under the full
    # lock set (issue #15, gate 1.4): principal <= deposits x
    # multiplier + live guarantees, re-verified at the money-moving
    # moment. The deposit-account row lock is the same serialisation
    # point P9 pledging and P11 withdrawals take, so a concurrent
    # withdrawal can never leave a disbursed loan over the cap: one
    # side waits and then observes the other's committed effect.
    # Guarantee amounts for this application only change under the
    # application row lock (pledging locks it FOR UPDATE), which step 1
    # already holds. Least disclosure (gate 1.6): the rejection never
    # echoes balances, the multiplier, or the shortfall.
    product = await get_product(session, tenant_id, product_id)
    deposit_row = (
        await session.execute(
            text(
                # Explicit tenant predicate on the row-lock read, on
                # top of RLS (defence in depth, gate 1.6 v1.1).
                "SELECT balance FROM deposit_accounts "
                "WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    deposits = Decimal(str(deposit_row[0])) if deposit_row is not None else Decimal("0")
    guarantees = await guarantee_total(session, tenant_id, application_id)
    max_eligible = to_cents(deposits * product.deposit_multiplier) + guarantees
    if principal > max_eligible:
        raise ConflictError(
            "loan amount exceeds the product deposit-multiplier eligibility; "
            "increase deposits or guarantees before disbursement"
        )

    # Step 2c: no unconsented collateral (external Codex review,
    # re-derived): a pledged guarantee may support cover% while the
    # application is in flight, but it must never be activated as
    # collateral on a live loan without the guarantor's recorded
    # consent (P9 consent contract). Any remaining 'pledged' row
    # blocks disbursement — the resolution paths are consent
    # (guarantees.consent_guarantee) or release/substitution
    # (BUILD_PROMPTS P13.14). Read under the application row lock
    # held since step 1 (pledging locks the application FOR UPDATE,
    # so the set cannot change underneath us). Least disclosure:
    # neither amounts nor guarantor identities are echoed.
    pledged_guarantee = (
        await session.execute(
            text(
                "SELECT 1 FROM guarantees "
                "WHERE application_id = CAST(:aid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'pledged' LIMIT 1"
            ),
            {"aid": str(application_id), "tid": str(tenant_id)},
        )
    ).first()
    if pledged_guarantee is not None:
        raise ConflictError("all guarantees must be consented before disbursement")

    update_result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE loan_applications "
                "SET stage = 'disbursed', version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {"ts": ts, "id": str(application_id), "tid": str(tenant_id), "ver": version},
        ),
    )
    if update_result.rowcount != 1:
        # Version moved between our SELECT ... FOR UPDATE and this UPDATE
        # (should not happen while the row lock is held, but the optimistic
        # check is the contract for editable aggregates — gate 1.4).
        raise ConflictError(
            f"stale version for loan application {application_id}; retry the disbursement"
        )

    # Step 3: create loan record.
    loan_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO loans "
            "(id, tenant_id, application_id, member_id, product_id, "
            " principal, balance, rate_pct, term_months, disbursed_at) "
            "VALUES "
            "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:app AS uuid), "
            " CAST(:mid AS uuid), CAST(:pid AS uuid), "
            " :principal, :balance, :rate, :term, :ts)"
        ),
        {
            "id": str(loan_id),
            "tid": str(tenant_id),
            "app": str(application_id),
            "mid": str(member_id),
            "pid": str(product_id),
            "principal": str(principal),
            "balance": str(principal),
            "rate": str(rate_pct),
            "term": term_months,
            "ts": ts,
        },
    )

    # Step 3b: link every consented guarantee to the loan (issue #15,
    # gate 1.5): active guarantees carry application_id with loan_id
    # NULL until this moment; from disbursement on, the guarantee
    # lifecycle follows the loan, so the P10 release-on-closure hook
    # and the P12 exit sweep always find them. Step 2c already proved
    # no 'pledged' row remains, so the filter is exactly the consented
    # set. Runs inside the same atomic disbursement transaction (P7
    # contract). Explicit tenant predicate on the write, on top of RLS
    # (gate 1.6 v1.1).
    linked = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE guarantees SET loan_id = CAST(:lid AS uuid), "
                "version = version + 1, updated_at = now() "
                "WHERE application_id = CAST(:aid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'active'"
            ),
            {"lid": str(loan_id), "aid": str(application_id), "tid": str(tenant_id)},
        ),
    )
    if int(linked.rowcount or 0):
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="guarantee.link_loan",
            entity="guarantees",
            entity_id=str(loan_id),
            after={
                "application_id": str(application_id),
                "loan_id": str(loan_id),
                "linked": int(linked.rowcount or 0),
            },
        )

    # Step 4: post the disbursement ledger entry.
    spec = build_disbursement_posting(principal, channel)
    result = await _post(session, tenant_id, member_id, spec, actor_id, occurred_at=ts)

    # Step 5: generate and persist the amortisation schedule.
    schedule = build_schedule(principal, rate_pct, term_months)
    start_date = ts.date()
    for inst in schedule:
        due_date = _add_months(start_date, inst.number)
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due) "
                "VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                " :no, :due, :pdp, :idp, :total)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "lid": str(loan_id),
                "no": inst.number,
                "due": due_date,
                "pdp": str(inst.principal_due),
                "idp": str(inst.interest_due),
                "total": str(inst.total_due),
            },
        )

    # Audit the disbursement.
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="loan.disbursed",
        entity="loans",
        entity_id=str(loan_id),
        after={
            "loan_id": str(loan_id),
            "application_id": str(application_id),
            "member_id": str(member_id),
            "principal": str(principal),
            "txn_ref": result.txn_ref,
        },
    )

    # Step 6: enqueue outbox event.
    await enqueue_event(
        session,
        tenant_id,
        event_type="loan.disbursed",
        payload={
            "loan_id": str(loan_id),
            "application_id": str(application_id),
            "member_id": str(member_id),
            "principal": str(principal),
            "txn_ref": result.txn_ref,
            "channel": channel.value,
        },
    )

    return DisbursementResult(
        loan_id=loan_id,
        txn_id=result.txn_id,
        txn_ref=result.txn_ref,
        schedule=schedule,
    )


def _add_months(d: date, months: int) -> date:
    """Add `months` calendar months to a date, clamping to month-end."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return d.replace(year=year, month=month, day=min(d.day, last_day))
