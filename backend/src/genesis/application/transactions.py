"""Member transactions: deposits, withdrawals, share top-ups, ledger listing (P11).

Every money movement reuses the P7 posting contract (gate 1.1) and runs
inside the caller's transaction: lock the account row -> validate ->
post balanced legs -> update the balance -> audit; notifications are
outbox-only via the posting services (gates 1.2, 1.4, 1.5).

Withdrawal capacity honours guarantorship: withdrawable funds are the
deposit balance minus the member's live pledges. Pledging computes
capacity under the same deposit-account row lock (P9), so the two paths
can never interleave into an over-pledge or an over-withdrawal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.guarantees import live_pledged_total
from genesis.application.ledger import post_deposit, post_share_topup, post_withdrawal
from genesis.application.members import reactivate_dormant_member
from genesis.application.pagination import build_created_id_cursor, parse_created_id_cursor
from genesis.domain.ledger import MEMBER_DIRECTION, Channel, Side, TxnType, member_direction
from genesis.domain.members import MemberStatus, MoneyOperation, member_may
from genesis.domain.money import ZERO, to_cents
from genesis.errors import ConflictError, InvalidInputError, NotFoundError

#: The two account tables this module maintains. Table names are always
#: taken from this mapping (never from user input) before interpolation.
_ACCOUNT_TABLES = {"deposit": "deposit_accounts", "share": "share_accounts"}

_TXN_COLS = "id, txn_ref, member_id, type, amount, channel, occurred_at, reversal_of_id"


@dataclass(frozen=True)
class AccountTxnResult:
    txn_id: uuid.UUID
    txn_ref: str
    amount: Decimal
    balance_after: Decimal


@dataclass(frozen=True)
class TransactionRecord:
    id: uuid.UUID
    txn_ref: str
    member_id: uuid.UUID | None
    txn_type: TxnType
    amount: Decimal
    channel: Channel
    occurred_at: datetime
    direction: Side
    is_reversal: bool


async def _require_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    operation: MoneyOperation,
    for_update: bool = False,
) -> MemberStatus:
    """Lock the member row and gate the operation on member status.

    The status policy is the code-owned domain capability map (P13.13
    FM2 — the single gatekeeper): dormant/exited refusals here are by
    construction, never per-route literals. FOR SHARE holds off a
    concurrent terminal member exit (which locks the row FOR UPDATE)
    until this mutation commits, closing the TOCTOU window between the
    status check and the posting (gate 1.4; P9 pledge precedent).
    Deposits pass for_update=True instead: a deposit may have to
    reactivate a dormant member (a status WRITE), and taking FOR
    UPDATE from the start avoids a share->update lock upgrade (which
    would deadlock two concurrent deposits to the same dormant
    member). Explicit tenant predicate on top of RLS (defence in
    depth, gate 1.6). Least disclosure: the refusal names the status
    and the operation, never a balance (rule 7).
    """
    lock = "FOR UPDATE" if for_update else "FOR SHARE"
    row = (
        await session.execute(
            # Lock clause chosen from two static literals in code.
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "  # noqa: S608
                f"AND tenant_id = CAST(:tid AS uuid) {lock}"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    status = MemberStatus(str(row[0]))
    if status is MemberStatus.EXITED:
        raise ConflictError(f"member {member_id} has exited and cannot transact")
    if not member_may(status, operation):
        raise ConflictError(
            f"member {member_id} is '{status.value}': {operation.value} requires an active member"
        )
    return status


async def _lock_account(
    session: AsyncSession, tenant_id: uuid.UUID, *, kind: str, member_id: uuid.UUID
) -> tuple[uuid.UUID, Decimal]:
    """Row-lock the member's account; every balance writer takes this lock.

    The explicit tenant predicate doubles the RLS fence on this money
    path (defence in depth, gate 1.6).
    """
    table = _ACCOUNT_TABLES[kind]
    row = (
        await session.execute(
            text(
                # Table name from _ACCOUNT_TABLES, never user input.
                f"SELECT id, balance FROM {table} "  # noqa: S608
                "WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} has no {kind} account")
    return uuid.UUID(str(row[0])), Decimal(str(row[1]))


async def _set_balance(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    account_id: uuid.UUID,
    balance: Decimal,
) -> None:
    table = _ACCOUNT_TABLES[kind]
    await session.execute(
        text(
            # Table name from _ACCOUNT_TABLES, never user input.
            # Explicit tenant predicate on the write, on top of RLS
            # (defence in depth, gate 1.6 — second-pass finding 15).
            f"UPDATE {table} SET balance = :bal, version = version + 1, "  # noqa: S608
            "updated_at = now() WHERE id = CAST(:id AS uuid) "
            "AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"bal": str(balance), "id": str(account_id), "tid": str(tenant_id)},
    )


async def record_deposit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    amount: Decimal,
    channel: Channel,
) -> AccountTxnResult:
    """Post a deposit and credit the account atomically (gates 1.4, 1.5).

    Arrears members may deposit (money in reduces risk); dormant
    members may deposit AND are reactivated by it in this same
    transaction (P13.13: the deposit posting, the balance credit, the
    Dormant -> Active transition, its audit row and the member-facing
    outbox notification are one atomic unit). Exited members may not
    transact. Lock order: member row FOR UPDATE, then the deposit
    account FOR UPDATE — the established member -> accounts chain
    edge; the member FOR UPDATE is what serialises reactivation
    against the dormancy job's SKIP LOCKED scan (exactly one final
    state, P13.13 FM3).
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("deposit amount must be positive")
    status = await _require_member(
        session, tenant_id, member_id, operation=MoneyOperation.DEPOSIT, for_update=True
    )
    account_id, balance = await _lock_account(
        session, tenant_id, kind="deposit", member_id=member_id
    )
    posting = await post_deposit(session, tenant_id, member_id, amount, channel, actor_id)
    balance_after = to_cents(balance + amount)
    await _set_balance(
        session, tenant_id, kind="deposit", account_id=account_id, balance=balance_after
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="deposit_account.credit",
        entity="deposit_accounts",
        entity_id=str(account_id),
        before={"balance": str(balance)},
        after={"balance": str(balance_after), "txn_ref": posting.txn_ref},
    )
    if status is MemberStatus.DORMANT:
        # Automatic reactivation inside the deposit transaction
        # (P13.13): the member row is already held FOR UPDATE above.
        await reactivate_dormant_member(
            session, tenant_id, actor_id, member_id, txn_ref=posting.txn_ref
        )
    return AccountTxnResult(posting.txn_id, posting.txn_ref, amount, balance_after)


async def record_withdrawal(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    amount: Decimal,
    channel: Channel,
) -> AccountTxnResult:
    """Withdraw under the account row lock; never overdraws (gate 1.4).

    Withdrawable funds exclude live guarantee pledges: a guarantor can
    never withdraw collateral that backs someone else's application or
    loan. The rejection message is deliberately generic (least
    disclosure, gate 1.6, matching the P9 pledge-capacity error): the
    withdrawable amount derives from the member's pledge exposure, and
    the audit row already records the exact figures for staff who are
    entitled to them.
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("withdrawal amount must be positive")
    await _require_member(session, tenant_id, member_id, operation=MoneyOperation.WITHDRAWAL)
    account_id, balance = await _lock_account(
        session, tenant_id, kind="deposit", member_id=member_id
    )
    pledged = await live_pledged_total(session, tenant_id, member_id)
    available = balance - pledged
    if amount > available:
        raise ConflictError(
            "insufficient available funds: the requested amount exceeds the withdrawable balance"
        )
    posting = await post_withdrawal(session, tenant_id, member_id, amount, channel, actor_id)
    balance_after = to_cents(balance - amount)
    await _set_balance(
        session, tenant_id, kind="deposit", account_id=account_id, balance=balance_after
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="deposit_account.debit",
        entity="deposit_accounts",
        entity_id=str(account_id),
        before={"balance": str(balance)},
        after={
            "balance": str(balance_after),
            "txn_ref": posting.txn_ref,
            "pledged": str(pledged),
        },
    )
    return AccountTxnResult(posting.txn_id, posting.txn_ref, amount, balance_after)


async def record_share_topup(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    amount: Decimal,
    channel: Channel,
) -> AccountTxnResult:
    """Post a share top-up and credit the share account atomically.

    Dormant members are refused (P13.13, code-owned capability map):
    the ONLY money a dormant account accepts is a deposit, which also
    reactivates it — after that the member may top up shares normally.
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("share top-up amount must be positive")
    await _require_member(session, tenant_id, member_id, operation=MoneyOperation.SHARE_TOPUP)
    account_id, balance = await _lock_account(session, tenant_id, kind="share", member_id=member_id)
    posting = await post_share_topup(session, tenant_id, member_id, amount, channel, actor_id)
    balance_after = to_cents(balance + amount)
    await _set_balance(
        session, tenant_id, kind="share", account_id=account_id, balance=balance_after
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="share_account.credit",
        entity="share_accounts",
        entity_id=str(account_id),
        before={"balance": str(balance)},
        after={"balance": str(balance_after), "txn_ref": posting.txn_ref},
    )
    return AccountTxnResult(posting.txn_id, posting.txn_ref, amount, balance_after)


def _row_to_txn(row: object) -> TransactionRecord:
    txn_type = TxnType(str(row[3]))  # type: ignore[index]
    is_reversal = row[7] is not None  # type: ignore[index]
    return TransactionRecord(
        id=uuid.UUID(str(row[0])),  # type: ignore[index]
        txn_ref=str(row[1]),  # type: ignore[index]
        member_id=uuid.UUID(str(row[2])) if row[2] is not None else None,  # type: ignore[index]
        txn_type=txn_type,
        amount=Decimal(str(row[4])),  # type: ignore[index]
        channel=Channel(str(row[5])),  # type: ignore[index]
        occurred_at=row[6],  # type: ignore[index]
        direction=member_direction(txn_type, is_reversal=is_reversal),
        is_reversal=is_reversal,
    )


def _direction_clause(direction: Side, params: dict[str, object]) -> str:
    """SQL predicate for the DR/CR filter — every value a bound parameter.

    Only the numbered placeholder names are interpolated; the enum
    values themselves travel as bound parameters (no value is ever
    string-interpolated into SQL). Reversals carry the original type
    with mirrored legs, so their member-facing direction is flipped.
    """
    same = sorted(t.value for t, s in MEMBER_DIRECTION.items() if s is direction)
    flipped = sorted(t.value for t, s in MEMBER_DIRECTION.items() if s is not direction)
    same_keys: list[str] = []
    for i, value in enumerate(same):
        key = f"dir_same_{i}"
        params[key] = value
        same_keys.append(f":{key}")
    flipped_keys: list[str] = []
    for i, value in enumerate(flipped):
        key = f"dir_flip_{i}"
        params[key] = value
        flipped_keys.append(f":{key}")
    return (
        f"((type IN ({', '.join(same_keys)}) AND reversal_of_id IS NULL) "
        f"OR (type IN ({', '.join(flipped_keys)}) AND reversal_of_id IS NOT NULL))"
    )


async def list_transactions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    member_id: uuid.UUID | None = None,
    txn_type: TxnType | None = None,
    channel: Channel | None = None,
    direction: Side | None = None,
    ref: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[TransactionRecord], str | None]:
    """Keyset-paginated ledger listing, newest first, page cap 100 (gate 1.3).

    Filters mirror the prototype columns (date, ref, member, type,
    DR/CR, channel). Backed by idx_txns_occurred_keyset and, for the
    member-filtered page, idx_txns_member_keyset.
    """
    limit = max(1, min(limit, 100))
    # Explicit tenant predicate on top of RLS (defence in depth, gate
    # 1.6); also the leading column of both keyset indexes.
    clauses: list[str] = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if member_id is not None:
        clauses.append("member_id = CAST(:mid AS uuid)")
        params["mid"] = str(member_id)
    if txn_type is not None:
        clauses.append("type = :type")
        params["type"] = txn_type.value
    if channel is not None:
        clauses.append("channel = :channel")
        params["channel"] = channel.value
    if direction is not None:
        clauses.append(_direction_clause(direction, params))
    if ref is not None:
        clauses.append("txn_ref = :ref")
        params["ref"] = ref
    if date_from is not None:
        clauses.append("occurred_at >= :d_from")
        params["d_from"] = date_from
    if date_to is not None:
        if date_from is not None and date_to < date_from:
            raise InvalidInputError("date_to must not precede date_from")
        clauses.append("occurred_at < :d_to")
        params["d_to"] = date_to + timedelta(days=1)  # inclusive end date
    if cursor:
        params["c_ts"], params["c_id"] = parse_created_id_cursor(cursor, entity="transaction")
        clauses.append("(occurred_at, id) < (:c_ts, CAST(:c_id AS uuid))")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    # Static fragments chosen in code; all values are bound parameters.
    rows = (
        await session.execute(
            text(
                f"SELECT {_TXN_COLS} FROM transactions "  # noqa: S608
                f"{where}"
                "ORDER BY occurred_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_txn(r) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = build_created_id_cursor(last[6], last[0])
    return items, next_cursor
