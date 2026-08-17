"""System-user administration services (P13.5, gates 1.1-1.6).

Reuses the existing `users` table (0001) — no parallel table. Creation
claims the (tenant_id, email) UNIQUE atomically (`INSERT ... ON
CONFLICT DO NOTHING` + rowcount, v1.1 rule 5); edits are
optimistic-locked and surface 409 on stale versions (gate 1.4); status
moves go through the single transition function in domain/users.py
under the target row lock (gate 1.4). Suspension takes effect
immediately IN THE SAME TRANSACTION: pending OTP challenges are voided
(consumed_at doubles as the void marker, reusing the P3 single-use gate
— evaluate_challenge refuses a consumed challenge forever) and every
refresh-token family is revoked (the P3 family-revocation state,
reused, not re-implemented). Access tokens are stateless and expire
within 15 minutes (P3 design); the refresh path is the durable fence.

User-level separation of duties (the P12 approver!=preparer precedent):
a caller can NEVER change their own role or status — enforced here,
server-side, before any lock is taken.

Self-lockout guard: the last ACTIVE System Admin can neither be
suspended nor re-roled away from System Admin. Checked under row locks
(below), so two concurrent suspensions of the last two admins serialize
and exactly one gets 409 — admins can never hit zero.

Lock ordering (documented per MASTER_PROMPT §5.9; matches the P3 auth
paths, which were re-ordered in this MR to agree):

    active-System-Admin user rows (ORDER BY id)   -- serialization point
      -> target user row
        -> otp_challenges rows of the target
          -> refresh_tokens rows of the target

Every status/role mutation takes the ordered admin-set lock FIRST, so
concurrent admin-reducing writers queue on the lowest-id active admin
row instead of deadlocking on each other's target rows. Token issue
(application/auth.py) locks the user row before the challenge/refresh
rows and never touches the admin set, so no cycle exists with login
traffic. Non-admin-set operations (OTP invalidation/re-enrolment) start
at the target user row and follow the same tail order.

All reads AND writes carry explicit bound tenant_id predicates on top
of forced RLS (v1.1 rule 4); every mutation writes its audit row
in-transaction via the shared writer (gate 1.5) and notifies via the
transactional outbox only (gate 1.2). OTP codes are never disclosed,
stored, or logged by any path here (least disclosure, gate 1.6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import (
    build_created_id_cursor,
    parse_created_id_cursor,
)
from genesis.domain.rbac import SYSTEM_ADMIN
from genesis.domain.users import (
    InvalidUserStatusTransitionError,
    UserStatus,
    transition,
)
from genesis.errors import ConflictError, ForbiddenError, NotFoundError


@dataclass(frozen=True)
class UserRecord:
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None
    branch: str | None
    role_id: uuid.UUID
    role_name: str
    status: UserStatus
    last_active_at: datetime | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class UserPage:
    items: list[UserRecord]
    next_cursor: str | None


_USER_COLUMNS = (
    "u.id, u.full_name, u.email, u.phone, u.branch, u.role_id, r.name, "
    "u.status, u.last_active_at, u.version, u.created_at"
)


def users_page_sql(*, with_cursor: bool, with_status: bool, with_role: bool) -> str:
    """Users listing page (keyset on created_at DESC, id DESC — gate 1.3).

    Served by idx_users_created_keyset (0015, shipped with this query).
    Fragments are static literals chosen in code; every value is a bound
    parameter, so string assembly is injection-safe (v1.1 rule 6).
    """
    clauses = ["u.tenant_id = CAST(:tid AS uuid)"]
    if with_status:
        clauses.append("u.status = :status")
    if with_role:
        clauses.append("u.role_id = CAST(:rid AS uuid)")
    if with_cursor:
        clauses.append("(u.created_at, u.id) < (:c_ts, CAST(:c_id AS uuid))")
    where = " AND ".join(clauses)
    return (
        f"SELECT {_USER_COLUMNS} "  # noqa: S608
        "FROM users u "
        "JOIN roles r ON r.id = u.role_id AND r.tenant_id = CAST(:tid AS uuid) "
        f"WHERE {where} "
        "ORDER BY u.created_at DESC, u.id DESC LIMIT :limit"
    )


def _row_to_record(row: Any) -> UserRecord:
    return UserRecord(
        id=uuid.UUID(str(row[0])),
        full_name=str(row[1]),
        email=str(row[2]),
        phone=str(row[3]) if row[3] is not None else None,
        branch=str(row[4]) if row[4] is not None else None,
        role_id=uuid.UUID(str(row[5])),
        role_name=str(row[6]),
        status=UserStatus(str(row[7])),
        last_active_at=row[8],
        version=int(row[9]),
        created_at=row[10],
    )


async def _role_name(session: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID) -> str | None:
    row = (
        await session.execute(
            text(
                "SELECT name FROM roles WHERE id = CAST(:rid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"rid": str(role_id), "tid": str(tenant_id)},
        )
    ).first()
    return str(row[0]) if row is not None else None


async def get_user(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserRecord:
    # Explicit tenant predicate on top of RLS (gate 1.6 v1.1).
    row = (
        await session.execute(
            text(
                f"SELECT {_USER_COLUMNS} FROM users u "  # noqa: S608
                "JOIN roles r ON r.id = u.role_id "
                "AND r.tenant_id = CAST(:tid AS uuid) "
                "WHERE u.id = CAST(:id AS uuid) AND u.tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(user_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"user {user_id} not found")
    return _row_to_record(row)


async def create_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    email: str,
    full_name: str,
    role_id: uuid.UUID,
    phone: str | None = None,
    branch: str | None = None,
) -> UserRecord:
    """Create a system user; the email claim is atomic (v1.1 rule 5).

    The role must exist in the caller's tenant (explicit tenant
    predicate; a foreign role id is a 404, never a cross-tenant grant).
    """
    role_name = await _role_name(session, tenant_id, role_id)
    if role_name is None:
        raise NotFoundError(f"role {role_id} not found")
    user_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, role_id, full_name, email, phone, branch) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                "CAST(:rid AS uuid), :name, :email, :phone, :branch) "
                "ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id"
            ),
            {
                "id": str(user_id),
                "tid": str(tenant_id),
                "rid": str(role_id),
                "name": full_name,
                "email": email,
                "phone": phone,
                "branch": branch,
            },
        )
    ).first()
    if claimed is None:
        # Least disclosure: the category reaches the caller; the email
        # involved lives only in this internal message (gate 1.6).
        raise ConflictError(f"email already in use for user create: {email}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.create",
        entity="users",
        entity_id=str(user_id),
        after={
            "full_name": full_name,
            "email": email,
            "role_id": str(role_id),
            "role_name": role_name,
            "branch": branch,
            "status": UserStatus.ACTIVE.value,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="user.created",
        payload={"user_id": str(user_id), "email": email},
    )
    return await get_user(session, tenant_id, user_id)


async def list_users(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
    status: UserStatus | None = None,
    role_id: uuid.UUID | None = None,
) -> UserPage:
    """Keyset-paginated listing on (created_at DESC, id DESC) (gate 1.3)."""
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        c_ts, c_id = parse_created_id_cursor(cursor, entity="user")
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    if status is not None:
        params["status"] = status.value
    if role_id is not None:
        params["rid"] = str(role_id)
    rows = (
        await session.execute(
            text(
                users_page_sql(
                    with_cursor=cursor is not None,
                    with_status=status is not None,
                    with_role=role_id is not None,
                )
            ),
            params,
        )
    ).all()
    items = [_row_to_record(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = build_created_id_cursor(last.created_at, last.id)
    return UserPage(items=items, next_cursor=next_cursor)


async def update_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    version: int,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    branch: str | None = None,
) -> UserRecord:
    """Optimistic-locked profile edit; stale versions surface 409 (gate 1.4).

    Role and status are deliberately NOT editable here: role assignment
    and activate/suspend are separate, separately-audited mutations with
    their own guards (P13.5). Omitted fields keep their current values.
    """
    current = await get_user(session, tenant_id, user_id)
    new_name = full_name if full_name is not None else current.full_name
    new_email = email if email is not None else current.email
    new_phone = phone if phone is not None else current.phone
    new_branch = branch if branch is not None else current.branch
    try:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    # Explicit tenant predicate on the write (v1.1 rule 4).
                    "UPDATE users SET full_name = :name, email = :email, "
                    "phone = :phone, branch = :branch, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) AND version = :ver"
                ),
                {
                    "name": new_name,
                    "email": new_email,
                    "phone": new_phone,
                    "branch": new_branch,
                    "id": str(user_id),
                    "tid": str(tenant_id),
                    "ver": version,
                },
            ),
        )
    except IntegrityError as exc:
        raise ConflictError(f"email already in use for user update: {new_email}") from exc
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for user {user_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.update",
        entity="users",
        entity_id=str(user_id),
        before={
            "full_name": current.full_name,
            "email": current.email,
            "phone": current.phone,
            "branch": current.branch,
            "version": current.version,
        },
        after={
            "full_name": new_name,
            "email": new_email,
            "phone": new_phone,
            "branch": new_branch,
            "version": current.version + 1,
        },
    )
    return await get_user(session, tenant_id, user_id)


async def _lock_admin_set(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """Lock every ACTIVE System Admin user row, ordered by id (gate 1.4).

    This is the serialization point of the self-lockout invariant: any
    transaction that could shrink the active-admin set must queue here,
    so a concurrent pair of suspensions re-evaluates against committed
    state instead of double-counting each other's target. Returns the
    System Admin role id, or None when the tenant has no such role
    (nothing to protect). The role name is a code-owned constant, never
    caller input.
    """
    row = (
        await session.execute(
            text("SELECT id FROM roles WHERE tenant_id = CAST(:tid AS uuid) AND name = :name"),
            {"tid": str(tenant_id), "name": SYSTEM_ADMIN},
        )
    ).first()
    if row is None:
        return None
    admin_role_id = uuid.UUID(str(row[0]))
    await session.execute(
        text(
            "SELECT id FROM users WHERE tenant_id = CAST(:tid AS uuid) "
            "AND role_id = CAST(:rid AS uuid) AND status = :active "
            "ORDER BY id FOR UPDATE"
        ),
        {"tid": str(tenant_id), "rid": str(admin_role_id), "active": UserStatus.ACTIVE.value},
    )
    return admin_role_id


async def _lock_user_row(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[uuid.UUID, UserStatus, int]:
    """SELECT ... FOR UPDATE on the target user; returns (role_id, status, version)."""
    row = (
        await session.execute(
            text(
                "SELECT role_id, status, version FROM users "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "FOR UPDATE"
            ),
            {"id": str(user_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"user {user_id} not found")
    return uuid.UUID(str(row[0])), UserStatus(str(row[1])), int(row[2])


async def _count_active_admins(
    session: AsyncSession, tenant_id: uuid.UUID, admin_role_id: uuid.UUID
) -> int:
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM users WHERE tenant_id = CAST(:tid AS uuid) "
                "AND role_id = CAST(:rid AS uuid) AND status = :active"
            ),
            {"tid": str(tenant_id), "rid": str(admin_role_id), "active": UserStatus.ACTIVE.value},
        )
    ).scalar_one()
    return int(count)


async def _void_pending_otp_challenges(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Void the target's pending OTP challenges; returns the row count.

    consumed_at is the single-use marker the P3 gatekeeper
    (domain/otp.evaluate_challenge) already refuses — setting it voids
    the challenge without a parallel state column (gate 1.1). Codes are
    hashes at rest and are never read, returned, or logged here.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE otp_challenges SET consumed_at = now() "
                "WHERE tenant_id = CAST(:tid AS uuid) "
                "AND user_id = CAST(:uid AS uuid) AND consumed_at IS NULL"
            ),
            {"tid": str(tenant_id), "uid": str(user_id)},
        ),
    )
    return int(result.rowcount or 0)


async def _revoke_refresh_families(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Revoke every refresh token of the user (P3 state machine, reused)."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE refresh_tokens SET status = 'revoked' "
                "WHERE tenant_id = CAST(:tid AS uuid) "
                "AND user_id = CAST(:uid AS uuid) AND status <> 'revoked'"
            ),
            {"tid": str(tenant_id), "uid": str(user_id)},
        ),
    )
    return int(result.rowcount or 0)


async def change_user_status(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    version: int,
    new_status: UserStatus,
) -> UserRecord:
    """Activate/suspend through the single transition function (gate 1.4).

    Self-guard first (user-level separation of duties), then the ordered
    admin-set lock, then the target row lock — see the module docstring
    for the full ordering. Suspension voids pending OTP challenges and
    revokes every refresh family IN THIS TRANSACTION, so a suspended
    user's tokens are refused at auth immediately (P13.5).
    """
    if actor_id == user_id:
        raise ForbiddenError("users cannot change their own status")
    admin_role_id = await _lock_admin_set(session, tenant_id)
    target_role_id, current_status, _ = await _lock_user_row(session, tenant_id, user_id)
    try:
        transition(current_status, new_status)
    except InvalidUserStatusTransitionError as exc:
        raise ConflictError(str(exc)) from exc
    if (
        new_status is UserStatus.SUSPENDED
        and admin_role_id is not None
        and target_role_id == admin_role_id
        and await _count_active_admins(session, tenant_id, admin_role_id) <= 1
    ):
        # Self-lockout guard: admin lockout is a denial-of-service
        # vector, not an administration feature (P13.5).
        raise ConflictError("cannot suspend the last active System Admin")
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE users SET status = :st, version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": new_status.value,
                "id": str(user_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for user {user_id}")
    voided = revoked = 0
    if new_status is UserStatus.SUSPENDED:
        voided = await _void_pending_otp_challenges(session, tenant_id, user_id)
        revoked = await _revoke_refresh_families(session, tenant_id, user_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.status",
        entity="users",
        entity_id=str(user_id),
        before={"status": current_status.value, "version": version},
        after={
            "status": new_status.value,
            "version": version + 1,
            "voided_otp_challenges": voided,
            "revoked_refresh_tokens": revoked,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="user.status_changed",
        payload={
            "user_id": str(user_id),
            "from": current_status.value,
            "to": new_status.value,
        },
    )
    return await get_user(session, tenant_id, user_id)


async def assign_role(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    version: int,
    role_id: uuid.UUID,
) -> UserRecord:
    """Audited role assignment with before/after (gate 1.5).

    Same guard set as suspension: no self-edit, and re-roling the last
    active System Admin away from System Admin is refused under the
    ordered admin-set lock — a lockout by role change is the same DoS
    as a lockout by suspension.
    """
    if actor_id == user_id:
        raise ForbiddenError("users cannot change their own role")
    new_role_name = await _role_name(session, tenant_id, role_id)
    if new_role_name is None:
        raise NotFoundError(f"role {role_id} not found")
    admin_role_id = await _lock_admin_set(session, tenant_id)
    target_role_id, current_status, _ = await _lock_user_row(session, tenant_id, user_id)
    if target_role_id == role_id:
        raise ConflictError(f"user {user_id} already holds role {role_id}")
    if (
        admin_role_id is not None
        and target_role_id == admin_role_id
        and role_id != admin_role_id
        and current_status is UserStatus.ACTIVE
        and await _count_active_admins(session, tenant_id, admin_role_id) <= 1
    ):
        raise ConflictError("cannot re-role the last active System Admin")
    old_role_name = await _role_name(session, tenant_id, target_role_id)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE users SET role_id = CAST(:rid AS uuid), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "rid": str(role_id),
                "id": str(user_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for user {user_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.role",
        entity="users",
        entity_id=str(user_id),
        before={"role_id": str(target_role_id), "role_name": old_role_name},
        after={"role_id": str(role_id), "role_name": new_role_name},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="user.role_changed",
        payload={
            "user_id": str(user_id),
            "from_role_id": str(target_role_id),
            "to_role_id": str(role_id),
        },
    )
    return await get_user(session, tenant_id, user_id)


async def invalidate_otp_challenges(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Admin-triggered OTP challenge invalidation (P13.5, gate 1.6).

    Voids every pending challenge under the target row lock; the count
    is recorded in the audit row. No code is ever disclosed — the
    prototype's on-screen OTP is a demo artifact, not a requirement.
    """
    await _lock_user_row(session, tenant_id, user_id)
    voided = await _void_pending_otp_challenges(session, tenant_id, user_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.otp_invalidate",
        entity="users",
        entity_id=str(user_id),
        after={"voided_otp_challenges": voided},
    )
    return voided


async def reenrol_otp(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[int, int]:
    """Admin-triggered OTP re-enrolment (P13.5, gate 1.6).

    Forces a fresh OTP sign-in: voids pending challenges AND revokes
    every refresh family in one transaction, then notifies the user via
    the transactional outbox. The user re-enrols by requesting a new
    OTP through the P3 login flow — no code is generated, returned, or
    delivered out-of-band here (never OTP disclosure).
    """
    await _lock_user_row(session, tenant_id, user_id)
    voided = await _void_pending_otp_challenges(session, tenant_id, user_id)
    revoked = await _revoke_refresh_families(session, tenant_id, user_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.otp_reenrol",
        entity="users",
        entity_id=str(user_id),
        after={
            "voided_otp_challenges": voided,
            "revoked_refresh_tokens": revoked,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="user.otp_reenrolment",
        payload={"user_id": str(user_id)},
    )
    return voided, revoked
