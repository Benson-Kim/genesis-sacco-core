"""Members application services (P8, gates 1.1-1.6).

Numbering reuses the P7 advisory-lock + per-tenant sequence + UNIQUE
pattern (gate 1.1): txn_ref_sequences keyed by the 'GP-' prefix. Member
creation opens the share and deposit accounts in the same transaction
(gate 1.5); the welcome notification goes through the transactional
outbox only (gate 1.2). Edits and status changes are optimistic-locked
and surface 409 on stale versions (gate 1.4). List and statement reads
use keyset pagination so every page is one indexed query (gate 1.3).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.ledger import _ADVISORY_NS, _advisory_key
from genesis.application.outbox import enqueue_event
from genesis.domain.members import (
    MEMBER_NO_PREFIX,
    InvalidStatusTransitionError,
    MemberStatus,
    MemberType,
    format_member_no,
    transition,
)
from genesis.errors import ConflictError, InvalidInputError, NotFoundError


@dataclass(frozen=True)
class MemberRecord:
    id: uuid.UUID
    member_no: str
    type: MemberType
    name: str
    phone: str | None
    email: str | None
    status: MemberStatus
    version: int


@dataclass(frozen=True)
class MemberPage:
    items: list[MemberRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class StatementLine:
    occurred_at: datetime
    txn_ref: str
    type: str
    channel: str
    amount: str


@dataclass(frozen=True)
class StatementPage:
    items: list[StatementLine]
    next_cursor: str | None


def _row_to_record(row: Any) -> MemberRecord:
    return MemberRecord(
        id=uuid.UUID(str(row[0])),
        member_no=str(row[1]),
        type=MemberType(str(row[2])),
        name=str(row[3]),
        phone=str(row[4]) if row[4] is not None else None,
        email=str(row[5]) if row[5] is not None else None,
        status=MemberStatus(str(row[6])),
        version=int(row[7]),
    )


async def _next_member_no(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Race-safe GP-XXXX allocation reusing the P7 pattern (gate 1.4).

    pg_advisory_xact_lock serialises allocators per tenant+prefix; the
    monotonic upsert hands out the next value; UNIQUE (tenant_id,
    member_no) on members is the final safety net.
    """
    lock_key = _advisory_key(tenant_id, MEMBER_NO_PREFIX)
    await session.execute(text(f"SELECT pg_advisory_xact_lock({_ADVISORY_NS}, {lock_key})"))
    row = (
        await session.execute(
            text(
                "INSERT INTO txn_ref_sequences (tenant_id, prefix, last_val) "
                "VALUES (CAST(:tid AS uuid), :prefix, 1) "
                "ON CONFLICT (tenant_id, prefix) DO UPDATE "
                "SET last_val = txn_ref_sequences.last_val + 1 "
                "RETURNING last_val"
            ),
            {"tid": str(tenant_id), "prefix": MEMBER_NO_PREFIX},
        )
    ).first()
    if row is None:
        raise RuntimeError("member number sequence upsert returned no row")
    return format_member_no(int(row[0]))


async def _audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_id: str,
    *,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(tenant_id, actor_id, action, entity, entity_id, before, after) "
            "VALUES (CAST(:tid AS uuid), CAST(:actor AS uuid), :action, "
            "'members', :eid, CAST(:before AS jsonb), CAST(:after AS jsonb))"
        ),
        {
            "tid": str(tenant_id),
            "actor": str(actor_id) if actor_id else None,
            "action": action,
            "eid": entity_id,
            "before": json.dumps(before) if before is not None else None,
            "after": json.dumps(after) if after is not None else None,
        },
    )


async def create_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    member_type: MemberType,
    name: str,
    phone: str | None = None,
    email: str | None = None,
) -> MemberRecord:
    """Create a member with share + deposit accounts in one transaction.

    Numbering is serialised by an advisory lock; the UNIQUE constraint is
    the final safety net and surfaces as 409 so the client retries with a
    fresh request (gate 1.4). Welcome notification is outbox-only
    (gate 1.2).
    """
    member_no = await _next_member_no(session, tenant_id)
    member_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO members "
                "(id, tenant_id, member_no, type, name, phone, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, :type, "
                ":name, :phone, :email)"
            ),
            {
                "id": str(member_id),
                "tid": str(tenant_id),
                "no": member_no,
                "type": member_type.value,
                "name": name,
                "phone": phone,
                "email": email,
            },
        )
    except IntegrityError as exc:
        raise ConflictError(f"member number {member_no} already exists") from exc
    await session.execute(
        text(
            "INSERT INTO share_accounts (tenant_id, member_id) "
            "VALUES (CAST(:tid AS uuid), CAST(:mid AS uuid))"
        ),
        {"tid": str(tenant_id), "mid": str(member_id)},
    )
    await session.execute(
        text(
            "INSERT INTO deposit_accounts (tenant_id, member_id) "
            "VALUES (CAST(:tid AS uuid), CAST(:mid AS uuid))"
        ),
        {"tid": str(tenant_id), "mid": str(member_id)},
    )
    await _audit(
        session,
        tenant_id,
        actor_id,
        "member.create",
        str(member_id),
        after={
            "member_no": member_no,
            "type": member_type.value,
            "name": name,
            "status": MemberStatus.ACTIVE.value,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member.welcome",
        payload={"member_id": str(member_id), "member_no": member_no, "name": name},
    )
    return MemberRecord(
        id=member_id,
        member_no=member_no,
        type=member_type,
        name=name,
        phone=phone,
        email=email,
        status=MemberStatus.ACTIVE,
        version=1,
    )


async def get_member(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> MemberRecord:
    # Explicit tenant predicate on top of RLS (defence in depth,
    # gate 1.6 v1.1; issue #17).
    row = (
        await session.execute(
            text(
                "SELECT id, member_no, type, name, phone, email, status, version "
                "FROM members WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    return _row_to_record(row)


async def list_members(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
    status: MemberStatus | None = None,
    member_type: MemberType | None = None,
) -> MemberPage:
    """Keyset-paginated listing ordered by member_no (gate 1.3).

    member_no is monotonic per tenant, so it doubles as a stable cursor.
    Exactly one indexed query per page regardless of table size.
    """
    limit = max(1, min(limit, 100))
    clauses: list[str] = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        clauses.append("member_no > :cursor")
        params["cursor"] = cursor
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status.value
    if member_type is not None:
        clauses.append("type = :mtype")
        params["mtype"] = member_type.value
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    # The WHERE fragments above are static literals chosen in code; every
    # value is a bound parameter, so string assembly is injection-safe.
    rows = (
        await session.execute(
            text(
                "SELECT id, member_no, type, name, phone, email, status, version "  # noqa: S608
                f"FROM members {where}"
                "ORDER BY member_no LIMIT :limit"
            ),
            params,
        )
    ).all()
    items = [_row_to_record(r) for r in rows[:limit]]
    next_cursor = items[-1].member_no if len(rows) > limit and items else None
    return MemberPage(items=items, next_cursor=next_cursor)


async def update_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    version: int,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> MemberRecord:
    """Optimistic-locked edit; a stale version surfaces as 409 (gate 1.4).

    Omitted fields keep their current values; clearing a field is a
    deliberate follow-up feature, not an accidental null overwrite.
    """
    current = await get_member(session, tenant_id, member_id)
    new_name = name if name is not None else current.name
    new_phone = phone if phone is not None else current.phone
    new_email = email if email is not None else current.email
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 v1.1; issue #17).
                "UPDATE members SET name = :name, phone = :phone, email = :email, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "name": new_name,
                "phone": new_phone,
                "email": new_email,
                "id": str(member_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for member {member_id}")
    await _audit(
        session,
        tenant_id,
        actor_id,
        "member.update",
        str(member_id),
        before={
            "name": current.name,
            "phone": current.phone,
            "email": current.email,
            "version": current.version,
        },
        after={
            "name": new_name,
            "phone": new_phone,
            "email": new_email,
            "version": current.version + 1,
        },
    )
    return MemberRecord(
        id=current.id,
        member_no=current.member_no,
        type=current.type,
        name=new_name,
        phone=new_phone,
        email=new_email,
        status=current.status,
        version=current.version + 1,
    )


async def change_member_status(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    version: int,
    new_status: MemberStatus,
) -> MemberRecord:
    """Transition member status under a row lock (gate 1.4).

    The pure transition function validates the move; the version check
    surfaces 409 on stale edits; the change is audited and announced via
    the outbox (gates 1.2, 1.5).
    """
    row = (
        await session.execute(
            text(
                "SELECT status, version FROM members WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    current_status = MemberStatus(str(row[0]))
    if new_status is MemberStatus.EXITED:
        # P12: the settlement workflow (application/member_exits.py) is
        # the sole writer of the terminal state (issue #14 resolution).
        # Direct status changes can never bypass eligibility, committee
        # approval, and the atomic settlement posting.
        raise ConflictError(
            "member exit is processed through the exit settlement workflow, "
            "not by direct status change"
        )
    if new_status is MemberStatus.DORMANT:
        # P13.13: the nightly dormancy job (application/dormancy.py) is
        # the sole writer of the dormant state — it derives inactivity
        # from the ledger (v1.1 rule 2); a manual flag would let staff
        # park an account outside its real activity window.
        raise ConflictError(
            "dormancy is derived from ledger activity by the dormancy job, "
            "not by direct status change"
        )
    if current_status is MemberStatus.DORMANT:
        # P13.13: reactivation happens automatically inside the deposit
        # transaction (with its own audit row and member notification —
        # the insider-fraud detection control) or terminally via the
        # P12 exit workflow; never by a bare status edit.
        raise ConflictError(
            "dormant members reactivate automatically on deposit or exit "
            "through the settlement workflow, not by direct status change"
        )
    try:
        transition(current_status, new_status)
    except InvalidStatusTransitionError as exc:
        raise ConflictError(str(exc)) from exc
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6).
                "UPDATE members SET status = :st, version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {"st": new_status.value, "id": str(member_id), "tid": str(tenant_id), "ver": version},
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for member {member_id}")
    await _audit(
        session,
        tenant_id,
        actor_id,
        "member.status",
        str(member_id),
        before={"status": current_status.value},
        after={"status": new_status.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member.status_changed",
        payload={
            "member_id": str(member_id),
            "from": current_status.value,
            "to": new_status.value,
        },
    )
    return await get_member(session, tenant_id, member_id)


async def mark_member_exited(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    version: int,
) -> None:
    """Terminal Exited transition — called ONLY by the P12 settlement service.

    Runs inside the settlement transaction while the caller already
    holds the member row FOR UPDATE (re-acquiring it here is a no-op in
    the same transaction). The pure transition function validates the
    move (gate 1.4); the blockers query is defence in depth — by the
    time the settlement service calls this, it has already netted the
    loans and released the guarantees under the full lock set, so any
    hit means a logic error upstream, not a user-facing state.
    """
    row = (
        await session.execute(
            text(
                "SELECT status, version FROM members WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    current_status = MemberStatus(str(row[0]))
    try:
        transition(current_status, MemberStatus.EXITED)
    except InvalidStatusTransitionError as exc:
        raise ConflictError(str(exc)) from exc
    blockers = (
        await session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM guarantees "
                " WHERE guarantor_member_id = CAST(:m AS uuid) "
                " AND tenant_id = CAST(:tid AS uuid) "
                " AND status IN ('pledged', 'active')), "
                "(SELECT count(*) FROM loans "
                " WHERE member_id = CAST(:m AS uuid) "
                " AND tenant_id = CAST(:tid AS uuid) AND status = 'active'), "
                "(SELECT count(*) FROM loan_applications "
                " WHERE member_id = CAST(:m AS uuid) "
                " AND tenant_id = CAST(:tid AS uuid) "
                " AND stage IN ('submitted', 'appraisal', 'committee', 'approved'))"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if blockers is not None and (int(blockers[0]) or int(blockers[1]) or int(blockers[2])):
        raise ConflictError(
            "member cannot exit with live guarantees, active loans, or open loan applications"
        )
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE members SET status = :st, version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": MemberStatus.EXITED.value,
                "id": str(member_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for member {member_id}")
    await _audit(
        session,
        tenant_id,
        actor_id,
        "member.status",
        str(member_id),
        before={"status": current_status.value},
        after={"status": MemberStatus.EXITED.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member.status_changed",
        payload={
            "member_id": str(member_id),
            "from": current_status.value,
            "to": MemberStatus.EXITED.value,
        },
    )


async def reactivate_dormant_member(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    txn_ref: str,
) -> None:
    """Dormant -> Active, called ONLY inside the deposit transaction (P13.13).

    The caller (application/transactions.record_deposit) already holds
    the member row FOR UPDATE, so this status write can never race the
    dormancy job (which locks the same row SKIP LOCKED) — exactly one
    final state. The pure transition function validates the move (gate
    1.4); the audit row and the outbox notification TO THE MEMBER
    commit atomically with the deposit posting (gates 1.2/1.5). The
    notification is the insider-fraud detection control: a fraudster
    reactivating a dormant account to drain it leaves a trace the
    victim sees — removing this enqueue fails the P13.13 outbox-count
    test.

    Version note: the row was read FOR UPDATE by the caller in THIS
    transaction, so the version-less predicate is safe — nothing can
    move the row between the lock and this write; status = 'dormant'
    is re-checked as defence in depth and the version bumps like every
    other status writer.
    """
    transition(MemberStatus.DORMANT, MemberStatus.ACTIVE)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, gate 1.6 v1.1; issue #17).
                "UPDATE members SET status = :st, version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = :from_st"
            ),
            {
                "st": MemberStatus.ACTIVE.value,
                "id": str(member_id),
                "tid": str(tenant_id),
                "from_st": MemberStatus.DORMANT.value,
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"member {member_id} changed state during reactivation; retry")
    await _audit(
        session,
        tenant_id,
        actor_id,
        "member.status",
        str(member_id),
        before={"status": MemberStatus.DORMANT.value},
        after={
            "status": MemberStatus.ACTIVE.value,
            "reason": "deposit_reactivation",
            "txn_ref": txn_ref,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member.status_changed",
        payload={
            "member_id": str(member_id),
            "from": MemberStatus.DORMANT.value,
            "to": MemberStatus.ACTIVE.value,
            "reason": "deposit_reactivation",
        },
    )


async def member_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> StatementPage:
    """Keyset-paginated member statement, newest first (gate 1.3).

    Mirrors the prototype statement columns (date, ref, type, channel,
    amount). The cursor is '<occurred_at ISO>|<txn id>' so every page is
    one index scan on (tenant_id, member_id, occurred_at) at any depth.
    """
    # Existence check keeps semantics consistent with GET /members/{id}:
    # unknown ids (including cross-tenant ids hidden by RLS) surface 404
    # instead of a misleading empty page, without leaking existence.
    await get_member(session, tenant_id, member_id)
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {
        "mid": str(member_id),
        "tid": str(tenant_id),
        "limit": limit + 1,
    }
    keyset = ""
    if cursor:
        ts_raw, _, id_raw = cursor.partition("|")
        try:
            params["c_ts"] = datetime.fromisoformat(ts_raw)
            params["c_id"] = str(uuid.UUID(id_raw))
        except ValueError as exc:
            raise InvalidInputError("invalid statement cursor") from exc
        keyset = "AND (occurred_at, id) < (:c_ts, CAST(:c_id AS uuid)) "
    # The keyset fragment is a static literal chosen in code; every value
    # is a bound parameter, so string assembly is injection-safe.
    rows = (
        await session.execute(
            text(
                "SELECT occurred_at, id, txn_ref, type, channel, amount "  # noqa: S608
                "FROM transactions "
                "WHERE member_id = CAST(:mid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                f"{keyset}"
                "ORDER BY occurred_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [
        StatementLine(
            occurred_at=r[0],
            txn_ref=str(r[2]),
            type=str(r[3]),
            channel=str(r[4]),
            amount=str(r[5]),
        )
        for r in page_rows
    ]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = f"{last[0].isoformat()}|{last[1]}"
    return StatementPage(items=items, next_cursor=next_cursor)
