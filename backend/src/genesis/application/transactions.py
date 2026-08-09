"""Member transactions: deposits, withdrawals, share top-ups, ledger listing (P11).

Every money movement reuses the P7 posting contract (reuse-first) and runs
inside the caller's transaction: lock the account row -> validate ->
post balanced legs -> update the balance -> audit; notifications are
outbox-only via the posting services (the house gates).

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
from genesis.application.pagination import (
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_created_id_cursor,
)
from genesis.domain.ledger import (
    MEMBER_DIRECTION,
    Account,
    Channel,
    Side,
    TxnType,
    member_direction,
)
from genesis.domain.members import MemberStatus, MoneyOperation, member_may
from genesis.domain.money import ZERO, to_cents
from genesis.errors import (
    ConflictError,
    InvalidInputError,
    InvariantViolationError,
    NotFoundError,
)

#: The two account tables this module maintains. Table names are always
#: taken from this mapping (never from user input) before interpolation.
_ACCOUNT_TABLES = {"deposit": "deposit_accounts", "share": "share_accounts"}

_TXN_COLS = (
    "id, txn_ref, member_id, type, amount, channel, occurred_at, "
    "reversal_of_id, created_by, external_ref"
)

#: Cursor scope id: signed cursors are bound to this
#: endpoint and this tenant — no cross-scope replay (tenant isolation).
TXN_LIST_SCOPE = "transactions.list"


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
    #: Posting-actor attribution (migration 0036): the
    #: acting principal recorded at INSERT in ledger._post and pinned
    #: immutable by the 0004 append-only fence. None for system/job
    #: postings and for pre-0036 rows without unambiguous audit history
    #: — attribution is never invented.
    created_by: uuid.UUID | None
    #: External receipt reference (migration 0043): the
    #: operator-entered M-Pesa confirmation code / bank slip ref on
    #: external-channel teller postings. None is the honest state for
    #: every system posting and every pre-0043 row — history is never
    #: backfilled with invented references.
    external_ref: str | None


async def _require_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    operation: MoneyOperation,
    for_update: bool = False,
) -> MemberStatus:
    """Lock the member row and gate the operation on member status.

    The status policy is the code-owned domain capability map (the
    single gatekeeper): dormant/exited refusals here are by
    construction, never per-route literals. FOR SHARE holds off a
    concurrent terminal member exit (which locks the row FOR UPDATE)
    until this mutation commits, closing the TOCTOU window between the
    status check and the posting (concurrency safety; P9 pledge precedent).
    Deposits pass for_update=True instead: a deposit may have to
    reactivate a dormant member (a status WRITE), and taking FOR
    UPDATE from the start avoids a share->update lock upgrade (which
    would deadlock two concurrent deposits to the same dormant
    member). Explicit tenant predicate on top of RLS (defence in
    depth, least disclosure). Least disclosure: the refusal names the status
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
    path (defence in depth).
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
            # (defence in depth, least disclosure).
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
    external_ref: str | None = None,
) -> AccountTxnResult:
    """Post a deposit and credit the account atomically (the house gates).

    Arrears members may deposit (money in reduces risk); dormant
    members may deposit AND are reactivated by it in this same
    transaction (the deposit posting, the balance credit, the
    Dormant -> Active transition, its audit row and the member-facing
    outbox notification are one atomic unit). Exited members may not
    transact. Lock order: member row FOR UPDATE, then the deposit
    account FOR UPDATE — the established member -> accounts chain
    edge; the member FOR UPDATE is what serialises reactivation
    against the dormancy job's SKIP LOCKED scan (exactly one final
    state).
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
    posting = await post_deposit(
        session, tenant_id, member_id, amount, channel, actor_id, external_ref=external_ref
    )
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
        # the member row is already held FOR UPDATE above.
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
    external_ref: str | None = None,
) -> AccountTxnResult:
    """Withdraw under the account row lock; never overdraws (concurrency safety).

    Withdrawable funds exclude live guarantee pledges: a guarantor can
    never withdraw collateral that backs someone else's application or
    loan. The rejection message is deliberately generic (least
    disclosure, matching the P9 pledge-capacity error): the
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
    posting = await post_withdrawal(
        session, tenant_id, member_id, amount, channel, actor_id, external_ref=external_ref
    )
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
    external_ref: str | None = None,
) -> AccountTxnResult:
    """Post a share top-up and credit the share account atomically.

    Dormant members are refused (code-owned capability map):
    the ONLY money a dormant account accepts is a deposit, which also
    reactivates it — after that the member may top up shares normally.
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("share top-up amount must be positive")
    await _require_member(session, tenant_id, member_id, operation=MoneyOperation.SHARE_TOPUP)
    account_id, balance = await _lock_account(session, tenant_id, kind="share", member_id=member_id)
    posting = await post_share_topup(
        session, tenant_id, member_id, amount, channel, actor_id, external_ref=external_ref
    )
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
        created_by=uuid.UUID(str(row[8])) if row[8] is not None else None,  # type: ignore[index]
        external_ref=str(row[9]) if row[9] is not None else None,  # type: ignore[index]
    )


#: Free-text ledger search predicate — module-level so
#: the EXPLAIN gate (tests/test_txn_search.py) asserts the exact
#: production fragment (the EXPLAIN-capture convention). ONE OR of two branches:
#: (a) txn_ref PREFIX probe — system refs are uppercase by
#: construction, so the operator text probes uppercased; served
#: portably by the 0043 text_pattern_ops index idx_txns_ref_prefix
#: (the 0001 UNIQUE btree serves prefixes only under the C collation);
#: (b) member match via EXISTS probing members by PRIMARY KEY per
#: candidate row (id = transactions.member_id) — member_no EXACT
#: (uppercase domain format) or name PREFIX (case-folded); the PK
#: probe needs no new index. Every value is a bound parameter; LIKE
#: metacharacters in the operator text are escaped code-side
#: (_search_params), so '%' searches match literally nothing instead
#: of everything. Keyset order and the page cap are UNTOUCHED.
TXN_SEARCH_CLAUSE = (
    "(txn_ref LIKE :q_ref ESCAPE '\\' "
    "OR EXISTS (SELECT 1 FROM members m "
    "WHERE m.tenant_id = CAST(:tid AS uuid) "
    "AND m.id = transactions.member_id "
    "AND (m.member_no = :q_no OR lower(m.name) LIKE :q_name ESCAPE '\\')))"
)


def _search_params(search: str) -> dict[str, object]:
    """Bound parameters for TXN_SEARCH_CLAUSE (values only — never SQL).

    LIKE metacharacters are escaped so operator text is always a
    LITERAL probe (falsifiable: unescaped, a '%' search returns every
    row and the inertness leg fails).
    """
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {
        "q_ref": escaped.upper() + "%",
        "q_no": search.upper(),
        "q_name": escaped.lower() + "%",
    }


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
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[TransactionRecord], str | None]:
    """Keyset-paginated ledger listing, newest first, page cap 100 (scalability).

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
    if search is not None and search.strip() != "":
        # Ref-prefix OR member (member_no exact / name
        # prefix) — see TXN_SEARCH_CLAUSE for the index derivation.
        clauses.append(TXN_SEARCH_CLAUSE)
        params.update(_search_params(search.strip()))
    if date_from is not None:
        clauses.append("occurred_at >= :d_from")
        params["d_from"] = date_from
    if date_to is not None:
        if date_from is not None and date_to < date_from:
            raise InvalidInputError("date_to must not precede date_from")
        clauses.append("occurred_at < :d_to")
        params["d_to"] = date_to + timedelta(days=1)  # inclusive end date
    if cursor:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=TXN_LIST_SCOPE, entity="transaction"
        )
        params["c_ts"], params["c_id"] = parse_created_id_cursor(inner, entity="transaction")
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
        next_cursor = encode_cursor(
            build_created_id_cursor(last[6], last[0]),
            tenant_id=tenant_id,
            endpoint=TXN_LIST_SCOPE,
        )
    return items, next_cursor


#: Hard defensive cap on the legs of ONE posting.
#: The widest as-built posting builder (the P12 exit settlement)
#: writes 7 legs; 64 is comfortable headroom for any legitimate future
#: builder while keeping the unpaginated legs response bounded by a
#: GUARD, not merely "by construction". transaction_legs fetches
#: cap + 1 rows and raises InvariantViolationError (a sanitized 500,
#: no figures echoed — least disclosure) when the cap is exceeded, so a future
#: builder that unbounds a posting trips loudly instead of silently
#: widening the response (falsifiable leg in
#: tests/test_transaction_legs.py: remove the guard and the over-cap
#: posting serves 200).
MAX_TRANSACTION_LEGS = 64

#: Legs drill-down read: the per-posting
#: DR/CR legs from the append-only ledger_entries truth. Module-level
#: so the EXPLAIN capture (tests/test_batch7_explain.py) asserts
#: against the production SQL (the EXPLAIN-capture convention). Explicit tenant
#: predicate on top of forced RLS (defence in depth); every value is a
#: bound parameter (v1.1 rule 6). Served by idx_ledger_txn (0001:
#: tenant_id, transaction_id) — this read ships NO migration. The leg
#: set is bounded by construction (the widest posting builder, the P12
#: exit settlement, writes 7 legs) AND by the MAX_TRANSACTION_LEGS
#: guard above (:leg_cap is bound to cap + 1 so
#: overflow is DETECTED, never truncated silently), so no pagination
#: exists here; ORDER BY side DESC (debits first — the accountant's
#: DR-before-CR convention), account, id is a total order making the
#: response deterministic even though all legs of one posting share
#: the same transaction-stable created_at.
TRANSACTION_LEGS_SQL = (
    "SELECT account, side, amount FROM ledger_entries "
    "WHERE tenant_id = CAST(:tid AS uuid) "
    "AND transaction_id = CAST(:txn AS uuid) "
    "ORDER BY side DESC, account, id LIMIT :leg_cap"
)


@dataclass(frozen=True)
class LedgerLegRecord:
    """One DR or CR leg of a posting, verbatim from ledger_entries."""

    account: Account
    side: Side
    amount: Decimal


async def transaction_legs(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    transaction_id: uuid.UUID,
) -> list[LedgerLegRecord]:
    """The DR/CR legs of one posting — read-only.

    404-BEFORE-FACTS: the transaction existence probe runs FIRST, so an
    unknown id — including a cross-tenant id hidden by RLS — surfaces
    404 before any leg is read; no rejection path ever echoes an
    account or an amount (least disclosure). The legs come
    VERBATIM from the append-only ledger_entries rows (the 0004 fence):
    nothing is summed, netted or derived here — the 0004/0014 DB
    constraint trigger already proved balance at commit time, and the
    web client renders each row verbatim without a computed total
    (no client-side money math). The response is bounded by the
    MAX_TRANSACTION_LEGS guard, not merely by
    construction.
    """
    exists = (
        await session.execute(
            text(
                # Explicit tenant predicate on top of RLS (defence in depth).
                "SELECT 1 FROM transactions WHERE id = CAST(:txn AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"txn": str(transaction_id), "tid": str(tenant_id)},
        )
    ).first()
    if exists is None:
        raise NotFoundError(f"transaction {transaction_id} not found")
    rows = (
        await session.execute(
            text(TRANSACTION_LEGS_SQL),
            {
                "tid": str(tenant_id),
                "txn": str(transaction_id),
                # cap + 1: the sentinel row proves overflow.
                "leg_cap": MAX_TRANSACTION_LEGS + 1,
            },
        )
    ).all()
    if len(rows) > MAX_TRANSACTION_LEGS:
        # Defensive bound: a legitimate posting
        # never comes close (widest builder: 7 legs); tripping this
        # means a future builder unbounded a posting. Sanitized 500 —
        # the message names the id only, never an account or a figure
        # (least disclosure); the ledger rows themselves stay intact and
        # auditable for staff entitled to them.
        raise InvariantViolationError(
            f"transaction {transaction_id} exceeds the bounded ledger-leg maximum"
        )
    return [
        LedgerLegRecord(
            account=Account(str(r[0])),
            side=Side(str(r[1])),
            amount=Decimal(str(r[2])),
        )
        for r in rows
    ]
