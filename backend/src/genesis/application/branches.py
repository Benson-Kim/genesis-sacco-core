"""Branches registry services.

The tenant-scoped registry behind the prototype's free-text branch
strings ("Nairobi CBD", "Thika", "HQ" — users list and the member
"Home branch" field). Branch is organisational metadata ONLY: nothing
here reads or writes money state, and no money path may key on
branch_id in this prompt (cash/till management is explicitly out of
scope — per the recorded gap analysis).

Reuse (reuse-first): creation claims the (tenant_id, name) UNIQUE
atomically (`INSERT ... ON CONFLICT DO NOTHING` + rowcount, v1.1
rule 5 — the email-claim pattern); edits are optimistic-locked
and surface 409 on stale versions (concurrency safety); the listing paginates on
the shared (created_at DESC, id DESC) keyset via the P11 cursor
helpers; the backfill runs through the shared batch runner (v1.1
rule 8). Every mutation writes its audit row in-transaction via the
shared writer with before/after payloads (data integrity). No outbox events:
branch changes have no notification consumer — there is no side-effect
to dispatch (reliability concerns delivery, not mutation).

Assignments: users and members get a nullable branch_id FK (0016).
Assignment is a single guarded UPDATE (explicit tenant predicate +
version check, v1.1 rule 4 / concurrency safety) — no SELECT ... FOR UPDATE is
taken, so these paths hold at most one implicit row lock and cannot
form a cycle with the established member/user lock chains (P12
settlement set, admin-set ORDER BY id). The target's legacy
free-text users.branch column is left untouched: it stays the
deprecated source for the backfill until the deferred contract
migration removes it (the migration policy).

Backfill (v1.1 rule 8): walks users in id-keyset batches (ascending id
— the same ordering convention as the admin-set locks), creating
one branch row per distinct trimmed legacy text value and linking each
scanned user. Both writes are atomic claims checked by rowcount: the
branch insert on the (tenant_id, name) UNIQUE, the user link on the
`branch_id IS NULL AND btrim(branch) = :name` guard, so a concurrent
run (or a concurrent manual assignment) can never double-create or
overwrite. The scan's anti-join claim key is `branch_id IS NULL` —
backed by the partial idx_users_branch_backfill (0016) — so a re-run
scans nothing, locks nothing and writes nothing: a lock-free no-op
proven by side-effect row counts, never return values. Users whose
branch text trims to empty are excluded by the scan (a blank name
violates the branches CHECK); they keep branch_id NULL for manual
assignment and are counted nowhere.

All reads AND writes carry explicit bound tenant_id predicates on top
of forced RLS (v1.1 rule 4). Errors are least-disclosure categories;
exact values live in the audit rows (v1.1 rule 7).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.pagination import (
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_created_id_cursor,
)
from genesis.domain.members import MEMBER_NO_PREFIX
from genesis.errors import ConflictError, InvalidInputError, NotFoundError

#: Cursor scope ids: every signed cursor is bound to ONE
#: endpoint — a branches cursor can never open a roster page and vice
#: versa (least disclosure).
BRANCHES_LIST_SCOPE = "branches.list"
BRANCH_USERS_SCOPE = "branches.users"
BRANCH_MEMBERS_SCOPE = "branches.members"

#: Users scanned per backfill transaction (P10/P11 batch precedent).
DEFAULT_BATCH_SIZE = 200

#: Semantic shape of a members-roster cursor.
#: Cursors are only ever built from SERVED member_no values (domain
#: format_member_no: 'GP-' + a zero-padded sequence of at least four
#: digits), so anything else is a forged or corrupted cursor — a 400
#: (InvalidInputError), never a silently empty page. The length bound
#: caps the regex input; 32 comfortably outlives the sequence space
#: while rejecting pathological payloads.
_MEMBER_NO_CURSOR_MAX_LEN = 32
_MEMBER_NO_CURSOR_RE = re.compile(re.escape(MEMBER_NO_PREFIX) + r"\d{4,}")


@dataclass(frozen=True)
class BranchRecord:
    id: uuid.UUID
    name: str
    version: int
    created_at: datetime


@dataclass(frozen=True)
class BranchPage:
    items: list[BranchRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class BranchAssignment:
    """Post-assignment state of the assigned user/member row."""

    entity_id: uuid.UUID
    branch_id: uuid.UUID
    branch_name: str
    version: int


@dataclass(frozen=True)
class BranchBackfillResult:
    scanned: int
    branches_created: int
    users_linked: int
    batches: int


@dataclass(frozen=True)
class BranchUserRosterRecord:
    """One user on a branch roster — identity facts only.

    created_at is the keyset position ONLY (review F4: the house
    created_at+id cursor) — the API response model never serializes
    it; the roster stays identity facts, least disclosure.
    """

    id: uuid.UUID
    full_name: str
    email: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class BranchUserRosterPage:
    items: list[BranchUserRosterRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class BranchMemberRosterRecord:
    """One member on a branch roster — identity facts only."""

    id: uuid.UUID
    member_no: str
    name: str
    status: str


@dataclass(frozen=True)
class BranchMemberRosterPage:
    items: list[BranchMemberRosterRecord]
    next_cursor: str | None


_BRANCH_COLUMNS = "b.id, b.name, b.version, b.created_at"


def branches_page_sql(*, with_cursor: bool) -> str:
    """Branches listing page (keyset on created_at DESC, id DESC — scalability).

    Served by idx_branches_created_keyset (0016, shipped with this
    query). Fragments are static literals chosen in code; every value
    is a bound parameter, so string assembly is injection-safe (v1.1
    rule 6).
    """
    clauses = ["b.tenant_id = CAST(:tid AS uuid)"]
    if with_cursor:
        clauses.append("(b.created_at, b.id) < (:c_ts, CAST(:c_id AS uuid))")
    where = " AND ".join(clauses)
    return (
        f"SELECT {_BRANCH_COLUMNS} "  # noqa: S608
        "FROM branches b "
        f"WHERE {where} "
        "ORDER BY b.created_at DESC, b.id DESC LIMIT :limit"
    )


def branch_users_roster_sql(*, with_cursor: bool) -> str:
    """USER roster of one branch — house created_at+id keyset.

    Review F4: the roster paginates on (created_at DESC, id DESC) via
    the shared P11 cursor helpers — the same convention as the
    branches listing — because a bare random-UUID id order is
    operationally meaningless; newest-created users lead. The
    equality-probed single-branch roster is a bounded top-N sort under
    either order, so the convention costs no new index and this read
    still ships NO migration.

    Expand-only read model over the 0016 assignment column: WHO is
    assigned, nothing more (least disclosure — no role, no
    last-active, no phone rides here; the full user record stays on
    the /users reads under the same access_control:view gate).
    Served by idx_users_branch (0016: tenant_id, branch_id). Static
    fragments chosen in code; every value is a bound parameter (v1.1
    rule 6); explicit tenant predicate on top of forced RLS (v1.1
    rule 4).
    """
    after = "AND (u.created_at, u.id) < (:c_ts, CAST(:c_id AS uuid)) " if with_cursor else ""
    return (
        "SELECT u.id, u.full_name, u.email, u.status, u.created_at FROM users u "  # noqa: S608
        "WHERE u.tenant_id = CAST(:tid AS uuid) "
        "AND u.branch_id = CAST(:bid AS uuid) "
        f"{after}"
        "ORDER BY u.created_at DESC, u.id DESC LIMIT :limit"
    )


def branch_members_roster_sql(*, with_cursor: bool) -> str:
    """MEMBER roster of one branch — NUMERIC member_no keyset.

    member_no is monotonic per tenant NUMERICALLY, not lexicographically
    (review-hardened): the domain format is a constant 'GP-' prefix +
    a zero-padded sequence of AT LEAST four digits, so as TEXT
    'GP-10000' < 'GP-9999' and a bare member_no order breaks past
    9,999 members. Ordering by (length(member_no), member_no) is the
    numeric order for this fixed format — longer means strictly
    bigger, equal length means zero-padded lexicographic == numeric —
    and the row-value keyset predicate walks it exhaustively and
    without overlap across the 4->5 digit boundary
    (tests/test_member_no_numeric_order.py). Least disclosure (gate
    1.6): the roster names WHO belongs to the branch — no contact
    details, no figures; the member record stays on the P8 /members
    reads under the same members:view gate. The equality-probed
    single-branch roster is a bounded top-N under idx_members_branch
    (0016) — NO new index needed here (the 0041 expression index
    serves the TENANT-WIDE listing). Static fragments chosen in code;
    every value is a bound parameter (v1.1 rule 6).
    """
    after = (
        "AND (length(m.member_no), m.member_no) > (length(:cursor), :cursor) "
        if with_cursor
        else ""
    )
    return (
        "SELECT m.id, m.member_no, m.name, m.status FROM members m "  # noqa: S608
        "WHERE m.tenant_id = CAST(:tid AS uuid) "
        "AND m.branch_id = CAST(:bid AS uuid) "
        f"{after}"
        "ORDER BY length(m.member_no), m.member_no LIMIT :limit"
    )


def backfill_scan_sql(*, with_after: bool) -> str:
    """Backfill keyset scan over unlinked legacy-branch users (v1.1 rule 8).

    The WHERE clause IS the anti-join on the claim key (branch_id is
    claimed by the link update), matching the partial
    idx_users_branch_backfill predicate (0016) so a completed backfill
    re-run touches an empty index. Static fragments chosen in code;
    all values are bound parameters (v1.1 rule 6).
    """
    after = "AND u.id > CAST(:after AS uuid) " if with_after else ""
    return (
        "SELECT u.id, btrim(u.branch) AS name FROM users u "  # noqa: S608
        "WHERE u.tenant_id = CAST(:tid AS uuid) "
        "AND u.branch IS NOT NULL AND u.branch_id IS NULL "
        f"{after}"
        "AND btrim(u.branch) <> '' "
        "ORDER BY u.id LIMIT :limit"
    )


def _row_to_record(row: Any) -> BranchRecord:
    return BranchRecord(
        id=uuid.UUID(str(row[0])),
        name=str(row[1]),
        version=int(row[2]),
        created_at=row[3],
    )


async def get_branch(
    session: AsyncSession, tenant_id: uuid.UUID, branch_id: uuid.UUID
) -> BranchRecord:
    # Explicit tenant predicate on top of RLS (defence in depth).
    row = (
        await session.execute(
            text(
                f"SELECT {_BRANCH_COLUMNS} FROM branches b "  # noqa: S608
                "WHERE b.id = CAST(:id AS uuid) AND b.tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(branch_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"branch {branch_id} not found")
    return _row_to_record(row)


async def create_branch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str,
) -> BranchRecord:
    """Create a branch; the (tenant_id, name) claim is atomic (v1.1 rule 5).

    INSERT ... ON CONFLICT DO NOTHING + rowcount — two concurrent
    creates of the same name serialize on the UNIQUE and exactly one
    wins; the loser gets 409, never an unhandled IntegrityError.
    """
    branch_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO branches (id, tenant_id, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name) "
                "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
            ),
            {"id": str(branch_id), "tid": str(tenant_id), "name": name},
        )
    ).first()
    if claimed is None:
        # Least disclosure: the caller gets the category; the name
        # involved lives only in this internal message (least disclosure).
        raise ConflictError(f"branch name already in use: {name}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="branch.create",
        entity="branches",
        entity_id=str(branch_id),
        after={"name": name, "version": 1},
    )
    return await get_branch(session, tenant_id, branch_id)


async def list_branches(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> BranchPage:
    """Keyset-paginated listing on (created_at DESC, id DESC) (scalability)."""
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=BRANCHES_LIST_SCOPE, entity="branch"
        )
        c_ts, c_id = parse_created_id_cursor(inner, entity="branch")
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    rows = (
        await session.execute(text(branches_page_sql(with_cursor=cursor is not None)), params)
    ).all()
    items = [_row_to_record(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_created_id_cursor(last.created_at, last.id),
            tenant_id=tenant_id,
            endpoint=BRANCHES_LIST_SCOPE,
        )
    return BranchPage(items=items, next_cursor=next_cursor)


async def list_branch_users(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> BranchUserRosterPage:
    """Keyset USER roster of one branch — read-only.

    404-BEFORE-FACTS: the branch existence probe runs first, so an
    unknown or cross-tenant branch id surfaces 404 before any roster
    row is read (least disclosure). The API layer gates this
    on access_control:view — the entity being READ is the user record,
    mirroring the assignment permission split (assigning a
    user is access_control:edit), never the settings registry gate.
    """
    await get_branch(session, tenant_id, branch_id)
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {
        "tid": str(tenant_id),
        "bid": str(branch_id),
        "limit": limit + 1,
    }
    if cursor:
        # Opaque signed cursor: verify+unseal first.
        # Review F4: the house cursor helper then rejects malformed
        # plaintext with InvalidInputError (defense-in-depth).
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=BRANCH_USERS_SCOPE, entity="branch user roster"
        )
        c_ts, c_id = parse_created_id_cursor(inner, entity="branch user roster")
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    rows = (
        await session.execute(text(branch_users_roster_sql(with_cursor=cursor is not None)), params)
    ).all()
    items = [
        BranchUserRosterRecord(
            id=uuid.UUID(str(r[0])),
            full_name=str(r[1]),
            email=str(r[2]),
            status=str(r[3]),
            created_at=r[4],
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_created_id_cursor(last.created_at, last.id),
            tenant_id=tenant_id,
            endpoint=BRANCH_USERS_SCOPE,
        )
    return BranchUserRosterPage(items=items, next_cursor=next_cursor)


async def list_branch_members(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> BranchMemberRosterPage:
    """Keyset MEMBER roster of one branch — read-only.

    404-BEFORE-FACTS: the branch existence probe runs first (least
    disclosure). The API layer gates this on members:view —
    the entity being READ is the member record, mirroring the earlier
    assignment permission split (assigning a member is members:edit),
    never the settings registry gate.

    Cursor guard (review F3, mirroring the users leg): cursors are
    only ever built from SERVED member_no values, so anything outside
    the bounded as-built GP-#### shape is a forged or corrupted
    cursor — a semantic 400, never a silently empty page.
    """
    await get_branch(session, tenant_id, branch_id)
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {
        "tid": str(tenant_id),
        "bid": str(branch_id),
        "limit": limit + 1,
    }
    if cursor:
        # Opaque signed cursor: verify+unseal BEFORE the
        # F3 semantic guard — the guard keeps fencing the PLAINTEXT
        # member_no shape as defense-in-depth after the tag check.
        inner = decode_cursor(
            cursor,
            tenant_id=tenant_id,
            endpoint=BRANCH_MEMBERS_SCOPE,
            entity="branch member roster",
        )
        if len(inner) > _MEMBER_NO_CURSOR_MAX_LEN or _MEMBER_NO_CURSOR_RE.fullmatch(inner) is None:
            raise InvalidInputError("invalid branch member roster cursor")
        params["cursor"] = inner
    rows = (
        await session.execute(
            text(branch_members_roster_sql(with_cursor=cursor is not None)), params
        )
    ).all()
    items = [
        BranchMemberRosterRecord(
            id=uuid.UUID(str(r[0])),
            member_no=str(r[1]),
            name=str(r[2]),
            status=str(r[3]),
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            items[-1].member_no, tenant_id=tenant_id, endpoint=BRANCH_MEMBERS_SCOPE
        )
    return BranchMemberRosterPage(items=items, next_cursor=next_cursor)


async def update_branch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    version: int,
    name: str,
) -> BranchRecord:
    """Optimistic-locked rename; stale versions surface 409 (concurrency safety)."""
    current = await get_branch(session, tenant_id, branch_id)
    try:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    # Explicit tenant predicate on the write (v1.1 rule 4).
                    "UPDATE branches SET name = :name, "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) AND version = :ver"
                ),
                {
                    "name": name,
                    "id": str(branch_id),
                    "tid": str(tenant_id),
                    "ver": version,
                },
            ),
        )
    except IntegrityError as exc:
        raise ConflictError(f"branch name already in use: {name}") from exc
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for branch {branch_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="branch.update",
        entity="branches",
        entity_id=str(branch_id),
        before={"name": current.name, "version": current.version},
        after={"name": name, "version": current.version + 1},
    )
    return await get_branch(session, tenant_id, branch_id)


# Assignment targets: static, code-owned table/label mapping — the
# table identifier is never caller input (v1.1 rule 6).
_ASSIGN_TARGETS: dict[str, str] = {
    "users": "user",
    "members": "member",
}


async def _assign_branch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    table: str,
    entity_id: uuid.UUID,
    version: int,
    branch_id: uuid.UUID,
) -> tuple[BranchAssignment, dict[str, Any], dict[str, Any]]:
    """Shared assignment path for users and members (reuse-first).

    Order of checks: branch existence in the caller's tenant (a foreign
    branch id is a 404, never a cross-tenant link), then target
    existence, then the version-guarded single-statement UPDATE. No
    explicit row lock is taken (see the module docstring on lock
    ordering).

    Returns (assignment, before, after). The audit row is written by
    each public wrapper with a static-literal entity= so the F4
    completeness scan (tests/test_audit_entity_map.py) can verify the
    redaction map statically; the wrapper shares this session, so the
    write stays in the same transaction (data integrity).
    """
    label = _ASSIGN_TARGETS[table]
    branch = await get_branch(session, tenant_id, branch_id)
    row = (
        await session.execute(
            text(
                # Code-owned identifier (see _ASSIGN_TARGETS); values bound.
                f"SELECT branch_id, version FROM {table} "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(entity_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"{label} {entity_id} not found")
    old_branch_id = uuid.UUID(str(row[0])) if row[0] is not None else None
    if old_branch_id == branch.id:
        raise ConflictError(f"{label} {entity_id} already assigned to branch {branch.id}")
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write (v1.1 rule 4).
                f"UPDATE {table} SET branch_id = CAST(:bid AS uuid), "  # noqa: S608
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND version = :ver"
            ),
            {
                "bid": str(branch.id),
                "id": str(entity_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for {label} {entity_id}")
    before: dict[str, Any] = {
        "branch_id": str(old_branch_id) if old_branch_id else None,
        "version": version,
    }
    after: dict[str, Any] = {
        "branch_id": str(branch.id),
        "branch_name": branch.name,
        "version": version + 1,
    }
    assignment = BranchAssignment(
        entity_id=entity_id,
        branch_id=branch.id,
        branch_name=branch.name,
        version=version + 1,
    )
    return assignment, before, after


async def assign_user_branch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    version: int,
    branch_id: uuid.UUID,
) -> BranchAssignment:
    """Assign (or re-assign) a user to a branch; audited (data integrity)."""
    assignment, before, after = await _assign_branch(
        session,
        tenant_id,
        table="users",
        entity_id=user_id,
        version=version,
        branch_id=branch_id,
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="user.branch_assign",
        entity="users",
        entity_id=str(user_id),
        before=before,
        after=after,
    )
    return assignment


async def assign_member_branch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    version: int,
    branch_id: uuid.UUID,
) -> BranchAssignment:
    """Assign (or re-assign) a member to a branch; audited (data integrity)."""
    assignment, before, after = await _assign_branch(
        session,
        tenant_id,
        table="members",
        entity_id=member_id,
        version=version,
        branch_id=branch_id,
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member.branch_assign",
        entity="members",
        entity_id=str(member_id),
        before=before,
        after=after,
    )
    return assignment


async def _process_backfill_batch(
    session: AsyncSession,
    after_id: uuid.UUID | None,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    batch_size: int,
) -> tuple[int, uuid.UUID | None, tuple[int, int]]:
    """Backfill one keyset batch; returns (scanned, last_id, (created, linked)).

    The scan walks the partial idx_users_branch_backfill (0016) over
    users still carrying only a legacy text branch. Both writes are
    rowcount-checked atomic claims (v1.1 rule 5): the branch insert on
    the (tenant_id, name) UNIQUE, the user link on `branch_id IS NULL
    AND btrim(branch) = :name` — so concurrent runs, or a manual
    assignment landing between scan and link, are skipped instead of
    overwritten. Users are linked in ascending id order (the scan
    order), consistent with the ordered-lock convention.
    """
    params: dict[str, object] = {"tid": str(tenant_id), "limit": batch_size}
    if after_id is not None:
        params["after"] = str(after_id)
    rows = (
        await session.execute(text(backfill_scan_sql(with_after=after_id is not None)), params)
    ).all()
    created = 0
    linked = 0
    branch_ids: dict[str, uuid.UUID] = {}
    for name in dict.fromkeys(str(r[1]) for r in rows):
        candidate = uuid.uuid4()
        claimed = (
            await session.execute(
                text(
                    "INSERT INTO branches (id, tenant_id, name) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
                ),
                {"id": str(candidate), "tid": str(tenant_id), "name": name},
            )
        ).first()
        if claimed is not None:
            branch_ids[name] = uuid.UUID(str(claimed[0]))
            created += 1
            await record_audit(
                session,
                tenant_id,
                actor_id,
                action="branch.create",
                entity="branches",
                entity_id=str(branch_ids[name]),
                after={"name": name, "version": 1, "source": "backfill"},
            )
            continue
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM branches WHERE tenant_id = CAST(:tid AS uuid) AND name = :name"
                ),
                {"tid": str(tenant_id), "name": name},
            )
        ).scalar_one()
        branch_ids[name] = uuid.UUID(str(existing))
    for user_id_raw, name_raw in rows:
        name = str(name_raw)
        branch_id = branch_ids[name]
        claim = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE users SET branch_id = CAST(:bid AS uuid), "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:uid AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) "
                    "AND branch_id IS NULL AND btrim(branch) = :name"
                ),
                {
                    "bid": str(branch_id),
                    "uid": str(user_id_raw),
                    "tid": str(tenant_id),
                    "name": name,
                },
            ),
        )
        if claim.rowcount != 1:
            # A concurrent run or manual assignment claimed this user
            # between the scan and the link; idempotency wins.
            continue
        linked += 1
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="user.branch_backfill",
            entity="users",
            entity_id=str(user_id_raw),
            before={"branch_id": None, "branch": name},
            after={"branch_id": str(branch_id), "branch_name": name},
        )
    last_id = uuid.UUID(str(rows[-1][0])) if rows else None
    return len(rows), last_id, (created, linked)


async def run_branch_backfill_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BranchBackfillResult:
    """Create branch rows from legacy users.branch text and link the users.

    Shared batch runner (v1.1 rule 8): each batch commits its own short
    transaction. A completed re-run scans nothing (anti-join on the
    branch_id claim key) and is proven a no-op by side-effect row
    counts in the tests, never by these return values alone.
    """
    process = partial(
        _process_backfill_batch,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_size=batch_size,
    )
    scanned, batches, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    return BranchBackfillResult(
        scanned=scanned,
        branches_created=sum(p[0] for p in payloads),
        users_linked=sum(p[1] for p in payloads),
        batches=batches,
    )
