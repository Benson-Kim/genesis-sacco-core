"""Member exit & settlement services (P12, gates 1.1-1.6).

Workflow (mirrors the prototype's Governance > Member exit screen):

  1. request_exit — eligibility under the member row lock, settlement
     snapshot persisted to member_exits (0001 table, 0010 columns)
  2. cast_exit_vote — committee approval reusing the P9 voting
     machinery (quorum -> approved/rejected, one vote per voter by DB
     UNIQUE, the initiator may never vote on their own request). The
     quorum is read from tenant configuration AT VOTE TIME under the
     exit row lock, falling back to the code-owned COMMITTEE_QUORUM
     (P13.7): a quorum change between votes governs the next vote's
     tally only — it can never decide an exit retroactively.

User-level separation of duties (the P4 matrix grants members:edit and
members:approve to the same roles, so the separation is per user, not
per role): the requesting user can never VOTE on their own request and
can never POST the money-moving SETTLEMENT of it (both 403). Voiding
one's own request IS allowed — it moves no money and is the documented
request-withdrawal path (see void_exit).
  3. post_settlement — ONE application-service transaction: re-verify
     every snapshot component under the full lock set (quote/approve/
     post TOCTOU defence, drift -> 409), post the balanced P7 set-off,
     zero the accounts, net + close the loans, release the guarantees
     the member RECEIVED, terminal Exited transition via the P8
     transition function, audit + outbox. No partial success.
  4. void_exit — approved/requested -> rejected escape hatch: a drifted
     snapshot is voided and a fresh request captures new balances.

Lock ordering (matches every existing money path; gate 1.4):

    member row (FOR UPDATE)
      -> deposit account (FOR UPDATE)      P9 pledge / P11 withdrawal order
        -> share account (FOR UPDATE)
          -> loan rows (FOR UPDATE)        P10 repayment lock
            -> advisory ref lock           inside the P7 posting

The member row FOR UPDATE is the serialisation point against every
concurrent mutation: P9 pledging and P11 deposits/withdrawals take the
member row FOR SHARE before touching balances, so a settlement can
never interleave with them — one side always waits and then observes
the other's terminal effect (exited member, or drifted snapshot).

Eligibility (prototype criteria):
  * active guarantees GIVEN by the member block the exit until
    released/substituted — live_pledged_total computed under the
    member's deposit-account row lock (the P9/P11 capacity rule)
  * open loan applications block the exit
  * active loans do NOT block if netted within the settlement: the
    payoff (principal + interest due + penalties, the P10 early-
    settlement rule) is deducted from the member's equity
  * negative settlements (loan payoff exceeds equity) are rejected at
    request time — documented rule in genesis.domain.exits

Fees come exclusively from tenant_settings.exit_fee (0009 pattern);
tenants without a settings row charge no fee. The request body never
carries money parameters (the P11 caller-rate lesson, gate 1.6).

Every read AND write carries an explicit tenant_id predicate on top of
forced RLS (defence in depth, gate 1.6). Rejection messages are least-
disclosure: no balances or capacities are echoed; the audit rows keep
the figures for staff entitled to them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.guarantees import live_pledged_total
from genesis.application.ledger import post_exit_settlement
from genesis.application.loans import _close_loan, _interest_due
from genesis.application.members import mark_member_exited
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import build_created_id_cursor, parse_created_id_cursor
from genesis.application.tenant_settings import committee_quorum
from genesis.application.transactions import _lock_account, _set_balance
from genesis.domain.committee import Decision, Vote, decide
from genesis.domain.exits import (
    ExitStatus,
    InvalidExitTransitionError,
    SettlementComputation,
    compute_settlement,
    exit_transition,
)
from genesis.domain.ledger import Channel
from genesis.domain.money import ZERO
from genesis.errors import ConflictError, ForbiddenError, NotFoundError

_EXIT_COLS = (
    "id, member_id, status, reason, shares_amount, deposits_amount, "
    "loan_balance, fees, net_payable, requested_by, decided_at, settled_at, "
    "settlement_transaction_id, version, created_at"
)

#: Application stages that block an exit (P8 blocker set, single list).
_OPEN_STAGES = "('submitted', 'appraisal', 'committee', 'approved')"


@dataclass(frozen=True)
class ExitRecord:
    id: uuid.UUID
    member_id: uuid.UUID
    status: ExitStatus
    reason: str | None
    shares_amount: Decimal
    deposits_amount: Decimal
    loan_balance: Decimal
    fees: Decimal
    net_payable: Decimal
    requested_by: uuid.UUID | None
    decided_at: datetime | None
    settled_at: datetime | None
    settlement_transaction_id: uuid.UUID | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class ExitVoteTally:
    approvals: int
    rejections: int
    decision: Decision | None
    status: ExitStatus


@dataclass(frozen=True)
class SettlementPostingResult:
    exit: ExitRecord
    txn_ref: str | None


@dataclass(frozen=True)
class ExitStatementDoc:
    """Exit statement document (JSON until P13 core/exports lands)."""

    exit_id: uuid.UUID
    member_id: uuid.UUID
    member_no: str
    member_name: str
    member_status: str
    exit_status: ExitStatus
    reason: str | None
    shares_amount: Decimal
    deposits_amount: Decimal
    equity: Decimal
    loan_balance: Decimal
    fees: Decimal
    net_payable: Decimal
    requested_at: datetime
    decided_at: datetime | None
    settled_at: datetime | None
    settlement_txn_ref: str | None


@dataclass(frozen=True)
class _LoanPayoff:
    loan_id: uuid.UUID
    principal: Decimal
    interest: Decimal
    penalties: Decimal


def _row_to_exit(row: Any) -> ExitRecord:
    return ExitRecord(
        id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        status=ExitStatus(str(row[2])),
        reason=str(row[3]) if row[3] is not None else None,
        shares_amount=Decimal(str(row[4])),
        deposits_amount=Decimal(str(row[5])),
        loan_balance=Decimal(str(row[6])),
        fees=Decimal(str(row[7])),
        net_payable=Decimal(str(row[8])),
        requested_by=uuid.UUID(str(row[9])) if row[9] is not None else None,
        decided_at=row[10],
        settled_at=row[11],
        settlement_transaction_id=uuid.UUID(str(row[12])) if row[12] is not None else None,
        version=int(row[13]),
        created_at=row[14],
    )


async def _exit_fee(session: AsyncSession, tenant_id: uuid.UUID) -> Decimal:
    """Tenant-configured exit fee; tenants without a settings row charge none.

    The fee is NEVER caller-supplied (gate 1.6; the P11 caller-rate
    lesson) — this lookup is the only source.
    """
    row = (
        await session.execute(
            text("SELECT exit_fee FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"),
            {"tid": str(tenant_id)},
        )
    ).first()
    return Decimal(str(row[0])) if row is not None else ZERO


async def _lock_member(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> tuple[str, int]:
    """Member row FOR UPDATE — the settlement serialisation point (gate 1.4)."""
    row = (
        await session.execute(
            text(
                "SELECT status, version FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    return str(row[0]), int(row[1])


async def _open_application_count(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> int:
    value = (
        await session.execute(
            text(
                # _OPEN_STAGES is a static literal chosen in code.
                "SELECT count(*) FROM loan_applications "  # noqa: S608
                "WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                f"AND stage IN {_OPEN_STAGES}"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).scalar_one()
    return int(value)


async def _active_loan_payoffs(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID, as_of: datetime
) -> list[_LoanPayoff]:
    """Payoff per active loan, computed under each loan's row lock.

    The P10 early-settlement rule per loan: principal balance +
    interest accrued on installments already due + penalties. Ordered
    by id for a deterministic lock order.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, balance, penalty_due FROM loans "
                "WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND status = 'active' "
                "ORDER BY id FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).all()
    payoffs: list[_LoanPayoff] = []
    for loan_id_raw, balance_raw, penalty_raw in rows:
        loan_id = uuid.UUID(str(loan_id_raw))
        interest = await _interest_due(session, tenant_id, loan_id, as_of.date())
        payoffs.append(
            _LoanPayoff(
                loan_id=loan_id,
                principal=Decimal(str(balance_raw)),
                interest=interest,
                penalties=Decimal(str(penalty_raw)),
            )
        )
    return payoffs


async def _compute_under_locks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    as_of: datetime,
) -> tuple[SettlementComputation, list[_LoanPayoff], uuid.UUID, uuid.UUID]:
    """Take the full lock set and compute the settlement components.

    Shared verbatim by request (snapshot) and posting (re-verification)
    so both sides can never diverge (gate 1.1). Returns the computation,
    the per-loan payoffs, and the locked account ids. Caller must
    already hold the member row FOR UPDATE.

    Eligibility raised here (least disclosure — no figures echoed):
      * live guarantees GIVEN by the member (under the deposit lock)
      * open loan applications
    """
    deposit_account_id, deposit_balance = await _lock_account(
        session, tenant_id, kind="deposit", member_id=member_id
    )
    share_account_id, share_balance = await _lock_account(
        session, tenant_id, kind="share", member_id=member_id
    )
    pledged = await live_pledged_total(session, tenant_id, member_id)
    if pledged > ZERO:
        raise ConflictError(
            "member has active guarantee obligations: release or substitute them before exit"
        )
    if await _open_application_count(session, tenant_id, member_id) > 0:
        raise ConflictError("member has open loan applications: resolve them before exit")
    payoffs = await _active_loan_payoffs(session, tenant_id, member_id, as_of)
    computation = compute_settlement(
        shares=share_balance,
        deposits=deposit_balance,
        loan_principal=sum((p.principal for p in payoffs), ZERO),
        loan_interest=sum((p.interest for p in payoffs), ZERO),
        loan_penalties=sum((p.penalties for p in payoffs), ZERO),
        fee=await _exit_fee(session, tenant_id),
    )
    return computation, payoffs, deposit_account_id, share_account_id


async def request_exit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> ExitRecord:
    """Create the settlement snapshot the committee will approve (gate 1.4).

    Eligibility and every component are computed under the full lock
    set so the snapshot is internally consistent. The partial UNIQUE
    uq_member_exits_open collapses concurrent double-submits to exactly
    one open settlement per member (409 for the losers).
    """
    status, _ = await _lock_member(session, tenant_id, member_id)
    if status == "exited":
        raise ConflictError(f"member {member_id} has already exited")
    if status not in ("active", "arrears"):
        # Allow-list, not deny-list: only members in good standing or in
        # arrears may request an exit. Any other (or future) status is
        # refused with a least-disclosure message (gate 1.6) instead of
        # silently passing through a deny-list gap.
        raise ConflictError(f"member {member_id} is not eligible to request an exit")
    ts = datetime.now(UTC)
    computation, _, _, _ = await _compute_under_locks(session, tenant_id, member_id, ts)
    if computation.net_payable < ZERO:
        # Documented negative-settlement rule (genesis.domain.exits):
        # the SACCO never auto-claims funds it does not hold. Least
        # disclosure (gate 1.6): the shortfall figure is never echoed —
        # staff entitled to it can read the balances via their normal
        # endpoints. Nothing is persisted: the rejection rolls back.
        raise ConflictError(
            "the outstanding loan payoff exceeds the member's equity: "
            "reduce the loan before requesting exit"
        )
    exit_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO member_exits "
                "(id, tenant_id, member_id, status, reason, shares_amount, "
                " deposits_amount, loan_balance, fees, net_payable, requested_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                ":status, :reason, :shares, :deposits, :loan, :fees, :net, "
                "CAST(:actor AS uuid))"
            ),
            {
                "id": str(exit_id),
                "tid": str(tenant_id),
                "mid": str(member_id),
                "status": ExitStatus.REQUESTED.value,
                "reason": reason,
                "shares": str(computation.shares),
                "deposits": str(computation.deposits),
                "loan": str(computation.loan_total),
                "fees": str(computation.fee),
                "net": str(computation.net_payable),
                "actor": str(actor_id) if actor_id else None,
            },
        )
    except IntegrityError as exc:
        raise ConflictError(
            f"an open exit settlement already exists for member {member_id}"
        ) from exc
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_exit.request",
        entity="member_exits",
        entity_id=str(exit_id),
        after={
            "member_id": str(member_id),
            "status": ExitStatus.REQUESTED.value,
            "shares": str(computation.shares),
            "deposits": str(computation.deposits),
            "loan_balance": str(computation.loan_total),
            "fees": str(computation.fee),
            "net_payable": str(computation.net_payable),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_exit.requested",
        payload={
            "exit_id": str(exit_id),
            "member_id": str(member_id),
            "net_payable": str(computation.net_payable),
        },
    )
    return await get_exit(session, tenant_id, exit_id)


async def get_exit(session: AsyncSession, tenant_id: uuid.UUID, exit_id: uuid.UUID) -> ExitRecord:
    row = (
        await session.execute(
            text(
                f"SELECT {_EXIT_COLS} FROM member_exits "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(exit_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"exit settlement {exit_id} not found")
    return _row_to_exit(row)


async def list_exits(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: ExitStatus | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[ExitRecord], str | None]:
    """Keyset-paginated exits listing, newest first, page cap 100 (gate 1.3).

    Backed by idx_exits_created_keyset (tenant_id, created_at DESC,
    id DESC) shipped in 0010 with this query; the status-filtered page
    by idx_exits_status_created_keyset (tenant_id, status, created_at
    DESC, id DESC), also 0010.
    """
    limit = max(1, min(limit, 100))
    clauses: list[str] = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status.value
    if cursor:
        params["c_ts"], params["c_id"] = parse_created_id_cursor(cursor, entity="exit")
        clauses.append("(created_at, id) < (:c_ts, CAST(:c_id AS uuid))")
    where = f"WHERE {' AND '.join(clauses)} "
    # Static fragments chosen in code; all values are bound parameters.
    rows = (
        await session.execute(
            text(
                f"SELECT {_EXIT_COLS} FROM member_exits "  # noqa: S608
                f"{where}"
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_exit(r) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = build_created_id_cursor(last[14], last[0])
    return items, next_cursor


async def cast_exit_vote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    voter_id: uuid.UUID,
    exit_id: uuid.UUID,
    vote: Vote,
) -> ExitVoteTally:
    """Record a committee vote on an exit settlement (P9 machinery, gate 1.4).

    The exit row lock serialises voters; the DB UNIQUE makes double-
    voting impossible even outside this code path. Separation of
    duties: the requesting user can never vote on their own request
    (the P4 matrix grants members:edit and members:approve to the same
    roles, so the separation is enforced per user, plus the two-voter
    quorum).
    """
    row = (
        await session.execute(
            text(
                "SELECT status, requested_by FROM member_exits "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(exit_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"exit settlement {exit_id} not found")
    current = ExitStatus(str(row[0]))
    requested_by = uuid.UUID(str(row[1])) if row[1] is not None else None
    if current is not ExitStatus.REQUESTED:
        raise ConflictError(f"voting is only open on requested exits, not '{current.value}'")
    if requested_by is not None and voter_id == requested_by:
        raise ForbiddenError("the initiator of an exit request cannot vote on it")
    try:
        await session.execute(
            text(
                "INSERT INTO exit_votes (tenant_id, exit_id, voter_id, vote) "
                "VALUES (CAST(:tid AS uuid), CAST(:eid AS uuid), "
                "CAST(:vid AS uuid), :vote)"
            ),
            {
                "tid": str(tenant_id),
                "eid": str(exit_id),
                "vid": str(voter_id),
                "vote": vote.value,
            },
        )
    except IntegrityError as exc:
        raise ConflictError("committee member has already voted on this exit") from exc
    tally_rows = (
        await session.execute(
            text(
                "SELECT vote, count(*) FROM exit_votes "
                "WHERE exit_id = CAST(:eid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) GROUP BY vote"
            ),
            {"eid": str(exit_id), "tid": str(tenant_id)},
        )
    ).all()
    counts = {str(r[0]): int(r[1]) for r in tally_rows}
    approvals = counts.get(Vote.APPROVE.value, 0)
    rejections = counts.get(Vote.REJECT.value, 0)
    await record_audit(
        session,
        tenant_id,
        voter_id,
        action="member_exit.vote",
        entity="member_exits",
        entity_id=str(exit_id),
        after={"vote": vote.value, "approvals": approvals, "rejections": rejections},
    )
    # Config read at vote time, under the exit row lock (P13.7).
    decision = decide(approvals, rejections, quorum=await committee_quorum(session, tenant_id))
    status: ExitStatus = current
    if decision is not None:
        target = ExitStatus.APPROVED if decision is Decision.APPROVED else ExitStatus.REJECTED
        exit_transition(current, target)
        decided = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE member_exits SET status = :st, decided_at = now(), "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"st": target.value, "id": str(exit_id), "tid": str(tenant_id)},
            ),
        )
        if decided.rowcount != 1:  # pragma: no cover - unreachable under the row lock
            # Codebase invariant: every guarded write verifies its
            # rowcount. The exit row is locked FOR UPDATE above, so a
            # mismatch means a logic error, not a user-facing state.
            raise ConflictError(f"exit settlement {exit_id} changed during voting; retry")
        status = target
        await record_audit(
            session,
            tenant_id,
            voter_id,
            action="member_exit.decided",
            entity="member_exits",
            entity_id=str(exit_id),
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
            event_type="member_exit.decided",
            payload={
                "exit_id": str(exit_id),
                "decision": decision.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
    return ExitVoteTally(
        approvals=approvals, rejections=rejections, decision=decision, status=status
    )


async def void_exit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    exit_id: uuid.UUID,
    *,
    version: int,
) -> ExitRecord:
    """Void an open exit (requested/approved -> rejected; gate 1.4).

    The escape hatch when an approved snapshot has drifted (posting
    returned 409) or governance withdraws the request: voiding frees
    the one-open-settlement slot so a fresh request can capture the
    current balances.

    Separation-of-duties decision (documented, deliberate): the
    initiator MAY void their own request. A void moves no money and
    only ever narrows state (open -> rejected); it is the request-
    withdrawal path, the natural counterpart of creating the request.
    The user-level bans apply where value can be extracted: the
    initiator can never VOTE on (cast_exit_vote) nor SETTLE
    (post_settlement) their own request.
    """
    row = (
        await session.execute(
            text(
                "SELECT status FROM member_exits "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(exit_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"exit settlement {exit_id} not found")
    current = ExitStatus(str(row[0]))
    try:
        exit_transition(current, ExitStatus.REJECTED)
    except InvalidExitTransitionError as exc:
        raise ConflictError(str(exc)) from exc
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE member_exits SET status = :st, decided_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": ExitStatus.REJECTED.value,
                "id": str(exit_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for exit settlement {exit_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_exit.void",
        entity="member_exits",
        entity_id=str(exit_id),
        before={"status": current.value},
        after={"status": ExitStatus.REJECTED.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_exit.voided",
        payload={"exit_id": str(exit_id)},
    )
    return await get_exit(session, tenant_id, exit_id)


async def post_settlement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    exit_id: uuid.UUID,
    *,
    version: int,
    channel: Channel,
) -> SettlementPostingResult:
    """Atomically settle an approved exit (gates 1.4, 1.5). No partial success.

    All in the caller's transaction, under the full lock set:

      1. lock the exit row; refuse the requesting user (user-level
         separation of duties — the initiator can never execute the
         money-moving settlement of their own request, 403); require
         status approved + matching version
      2. lock the member row (serialisation point), then deposit ->
         share accounts -> loan rows (documented lock order)
      3. recompute every settlement component and compare with the
         approved snapshot component by component — any drift (balance
         moved, installment newly due, fee reconfigured, new pledge or
         application) is a 409 with nothing posted (TOCTOU defence)
      4. post the balanced P7 exit set-off (WD- ref); zero the deposit
         and share balances; net + close each loan through the P10
         closure (which releases the guarantees behind it); sweep any
         remaining live guarantees the member RECEIVED
      5. terminal Exited transition via the P8 transition function
      6. mark the exit row settled; audit with before/after; outbox

    Zero-equity settlements move no money: the ledger posting is
    skipped (documented; the posting builder refuses empty specs) and
    the exit still settles.
    """
    exit_row = (
        await session.execute(
            text(
                f"SELECT {_EXIT_COLS} FROM member_exits "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(exit_id), "tid": str(tenant_id)},
        )
    ).first()
    if exit_row is None:
        raise NotFoundError(f"exit settlement {exit_id} not found")
    snapshot = _row_to_exit(exit_row)
    if snapshot.requested_by is not None and actor_id == snapshot.requested_by:
        # User-level separation of duties (mirror of the vote ban): the
        # P4 matrix grants members:edit and members:approve to the same
        # roles, so without this check the requester could execute the
        # money-moving settlement of their own request. A different
        # members:approve holder must post it.
        raise ForbiddenError("the initiator of an exit request cannot post its settlement")
    try:
        exit_transition(snapshot.status, ExitStatus.SETTLED)
    except InvalidExitTransitionError as exc:
        raise ConflictError(
            f"exit settlement is '{snapshot.status.value}': only approved exits can be posted"
        ) from exc
    if snapshot.version != version:
        raise ConflictError(f"stale version {version} for exit settlement {exit_id}")

    member_id = snapshot.member_id
    member_status, member_version = await _lock_member(session, tenant_id, member_id)
    if member_status == "exited":
        raise ConflictError(f"member {member_id} has already exited")

    ts = datetime.now(UTC)
    computation, payoffs, deposit_account_id, share_account_id = await _compute_under_locks(
        session, tenant_id, member_id, ts
    )

    # TOCTOU re-verification (gate 1.4): the committee approved THIS
    # snapshot; if any component moved since, nothing is posted. Least
    # disclosure — the drift details live in the audit trail via the
    # normal transaction history, not in the error.
    if (
        computation.shares != snapshot.shares_amount
        or computation.deposits != snapshot.deposits_amount
        or computation.loan_total != snapshot.loan_balance
        or computation.fee != snapshot.fees
    ):
        raise ConflictError(
            "settlement snapshot is stale: balances changed since approval; "
            "void the settlement and request a fresh one"
        )

    txn_ref: str | None = None
    txn_id: uuid.UUID | None = None
    posting_channel: Channel | None = None
    if computation.equity > ZERO:
        posting_channel = channel if computation.net_payable > ZERO else Channel.INTERNAL
        posting = await post_exit_settlement(
            session,
            tenant_id,
            member_id,
            shares=computation.shares,
            deposits=computation.deposits,
            loan_principal=computation.loan_principal,
            loan_interest=computation.loan_interest,
            loan_penalties=computation.loan_penalties,
            fee=computation.fee,
            channel=posting_channel,
            actor_id=actor_id,
            occurred_at=ts,
        )
        txn_ref = posting.txn_ref
        txn_id = posting.txn_id

    # Zero the accounts under their held row locks (P11 writer).
    if computation.deposits > ZERO:
        await _set_balance(
            session, tenant_id, kind="deposit", account_id=deposit_account_id, balance=ZERO
        )
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="deposit_account.debit",
            entity="deposit_accounts",
            entity_id=str(deposit_account_id),
            before={"balance": str(computation.deposits)},
            after={"balance": "0", "txn_ref": txn_ref},
        )
    if computation.shares > ZERO:
        await _set_balance(
            session, tenant_id, kind="share", account_id=share_account_id, balance=ZERO
        )
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="share_account.debit",
            entity="share_accounts",
            entity_id=str(share_account_id),
            before={"balance": str(computation.shares)},
            after={"balance": "0", "txn_ref": txn_ref},
        )

    # Net and close each loan through the P10 closure hook, which also
    # releases the guarantees the member RECEIVED on that loan.
    for payoff in payoffs:
        await session.execute(
            text(
                "UPDATE loans SET balance = 0, penalty_due = 0, "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"ts": ts, "id": str(payoff.loan_id), "tid": str(tenant_id)},
        )
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="loan.exit_netted",
            entity="loans",
            entity_id=str(payoff.loan_id),
            before={
                "balance": str(payoff.principal),
                "interest_due": str(payoff.interest),
                "penalty_due": str(payoff.penalties),
            },
            after={"balance": "0", "penalty_due": "0", "txn_ref": txn_ref},
        )
        await _close_loan(session, tenant_id, actor_id, payoff.loan_id, ts)

    # Defensive sweep: release any remaining live guarantee RECEIVED by
    # the member (e.g. rows never linked to a loan). Guarantees GIVEN
    # were verified zero above.
    swept = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE guarantees SET status = 'released', "
                "version = version + 1, updated_at = now() "
                "WHERE borrower_member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "AND status IN ('pledged', 'active')"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        ),
    )
    if int(swept.rowcount or 0):
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="guarantee.release",
            entity="guarantees",
            entity_id=str(member_id),
            after={"borrower_member_id": str(member_id), "released": int(swept.rowcount or 0)},
        )

    # Terminal transition through the single P8 gatekeeper (gate 1.4).
    await mark_member_exited(session, tenant_id, actor_id, member_id, version=member_version)

    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE member_exits SET status = :st, settled_at = :ts, "
                "settlement_transaction_id = CAST(:txn AS uuid), "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": ExitStatus.SETTLED.value,
                "ts": ts,
                "txn": str(txn_id) if txn_id else None,
                "id": str(exit_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"exit settlement {exit_id} changed during posting; retry")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_exit.settled",
        entity="member_exits",
        entity_id=str(exit_id),
        before={"status": ExitStatus.APPROVED.value},
        after={
            "status": ExitStatus.SETTLED.value,
            "txn_ref": txn_ref,
            "net_payable": str(computation.net_payable),
            # Cash-trail reconstruction: the channel the caller chose and
            # the channel the posting actually used (INTERNAL for a
            # zero-net set-off; null when zero equity moved no money).
            "channel": channel.value,
            "posting_channel": posting_channel.value if posting_channel is not None else None,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_exit.settled",
        payload={
            "exit_id": str(exit_id),
            "member_id": str(member_id),
            "txn_ref": txn_ref,
            "net_payable": str(computation.net_payable),
        },
    )
    return SettlementPostingResult(
        exit=await get_exit(session, tenant_id, exit_id), txn_ref=txn_ref
    )


async def exit_statement(
    session: AsyncSession, tenant_id: uuid.UUID, exit_id: uuid.UUID
) -> ExitStatementDoc:
    """Exit statement document as JSON (single indexed lookup, gate 1.3).

    The CSV/PDF export path arrives with P13 core/exports (run_export,
    truncation headers, worker rendering) — tracked as a blocker issue
    referencing P12/P13 rather than built early (STANDING RULE).
    """
    row = (
        await session.execute(
            text(
                "SELECT e.id, e.member_id, m.member_no, m.name, m.status, "
                "e.status, e.reason, e.shares_amount, e.deposits_amount, "
                "e.loan_balance, e.fees, e.net_payable, e.created_at, "
                "e.decided_at, e.settled_at, t.txn_ref "
                "FROM member_exits e "
                "JOIN members m ON m.id = e.member_id "
                "AND m.tenant_id = e.tenant_id "
                "LEFT JOIN transactions t ON t.id = e.settlement_transaction_id "
                "AND t.tenant_id = e.tenant_id "
                "WHERE e.id = CAST(:id AS uuid) AND e.tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(exit_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"exit settlement {exit_id} not found")
    shares = Decimal(str(row[7]))
    deposits = Decimal(str(row[8]))
    return ExitStatementDoc(
        exit_id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        member_no=str(row[2]),
        member_name=str(row[3]),
        member_status=str(row[4]),
        exit_status=ExitStatus(str(row[5])),
        reason=str(row[6]) if row[6] is not None else None,
        shares_amount=shares,
        deposits_amount=deposits,
        equity=shares + deposits,
        loan_balance=Decimal(str(row[9])),
        fees=Decimal(str(row[10])),
        net_payable=Decimal(str(row[11])),
        requested_at=row[12],
        decided_at=row[13],
        settled_at=row[14],
        settlement_txn_ref=str(row[15]) if row[15] is not None else None,
    )
