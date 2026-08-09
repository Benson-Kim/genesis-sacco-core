"""Members application services .

Numbering reuses the P7 advisory-lock + per-tenant sequence + UNIQUE
pattern (reuse-first): txn_ref_sequences keyed by the 'GP-' prefix. Member
creation opens the share and deposit accounts in the same transaction
(data integrity); the welcome notification goes through the transactional
outbox only (reliability). Edits and status changes are optimistic-locked
and surface 409 on stale versions (concurrency safety). List and statement reads
use keyset pagination so every page is one indexed query (scalability).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.guarantees import live_guarantee_params
from genesis.application.ledger import _ADVISORY_NS, _advisory_key
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import decode_cursor, encode_cursor
from genesis.domain.lending import LoanStatus
from genesis.domain.members import (
    MEMBER_NO_PREFIX,
    DividendPayout,
    InvalidStatusTransitionError,
    MemberStatus,
    MemberType,
    format_member_no,
    normalize_kenya_msisdn,
    transition,
)
from genesis.errors import ConflictError, InvalidInputError, NotFoundError, UnprocessableError

#: Cursor scope ids: every signed cursor is bound to ONE
#: endpoint — the two shapes of GET /members (plain / aggregates) share
#: one scope because they share one endpoint and one keyset.
MEMBERS_LIST_SCOPE = "members.list"
STATEMENT_SCOPE = "members.statement"


def parse_phone(raw: str | None) -> str | None:
    """Normalize a caller-supplied phone to E.164 storage.

    None stays None (phone is optional). An invalid value surfaces as
    422 via UnprocessableError with the sanitized category ONLY — the
    offending identifier is NEVER echoed or logged (least disclosure; the
    parse_dividend_payout precedent, and exactly why this validation
    lives here rather than in a Pydantic field: FastAPI's structural
    422 would echo the input back).
    """
    if raw is None:
        return None
    normalized = normalize_kenya_msisdn(raw)
    if normalized is None:
        raise UnprocessableError("invalid phone format")
    return normalized


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
    #: Branch attribution: the 0016 nullable FK
    #: written ONLY by the assignment route (PUT
    #: /branches/{id}/members/{member_id}) and the registry service it
    #: calls — never invented here. NULL is the honest "unassigned"
    #: state every member starts in (expand-only read fact).
    branch_id: uuid.UUID | None
    #: Stored dividend payout PREFERENCE; NULL is the
    #: honest "not chosen" state. Preference ONLY — the
    #: distribution engine does not consume it (the preference-only fence).
    dividend_payout: DividendPayout | None


@dataclass(frozen=True)
class MemberPage:
    items: list[MemberRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class MemberWithAggregates:
    record: MemberRecord
    aggregates: MemberAggregates


@dataclass(frozen=True)
class MemberAggregatesPage:
    items: list[MemberWithAggregates]
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


@dataclass(frozen=True)
class MemberAggregates:
    """Per-member financial aggregates for the single-member read.

    deposits_total, shares_total read the balance columns the P11
    account services maintain under the account row locks (one row per
    member per tenant). loans_outstanding sums the member's ACTIVE loan
    balances (closed rows are zero by the closure rule; written-off
    rows belong to the recovery module). guarantees_pledged sums LIVE
    guarantee amounts using the single P9 status set via
    live_guarantee_params() (a diverging filter would show figures the
    pledge endpoint refuses). The API layer serializes all four as
    canonical decimal strings.
    """

    deposits_total: Decimal
    shares_total: Decimal
    loans_outstanding: Decimal
    guarantees_pledged: Decimal


#: SQL behind member_aggregates, module-level so the EXPLAIN capture
#: can assert its plan (the EXPLAIN-capture convention). One SELECT of four
#: scalar subqueries, each carrying an explicit tenant predicate that
#: doubles the RLS fence (least disclosure); every value is a bound parameter
#: (v1.1 rule 6). Each subquery is servable by an index that shipped
#: with its table in 0001: the deposit/share (tenant_id, member_id)
#: UNIQUE-key probes, idx_loans_member, idx_guarantees_guarantor —
#: this feature ships NO migration. The outer CAST normalises the
#: empty-SUM zero to column scale so a zero-activity member serializes
#: '0.00', never bare '0'.
MEMBER_AGGREGATES_SQL = (
    # Columns carry explicit AS labels: the read
    # path maps them BY NAME, so a reordered or widened SELECT list
    # can never silently misalign a money figure.
    "SELECT "
    "CAST(COALESCE((SELECT balance FROM deposit_accounts "
    "WHERE member_id = CAST(:mid AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid)), 0) AS numeric(18,2)) AS deposits_total, "
    "CAST(COALESCE((SELECT balance FROM share_accounts "
    "WHERE member_id = CAST(:mid AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid)), 0) AS numeric(18,2)) AS shares_total, "
    "CAST((SELECT COALESCE(SUM(balance), 0) FROM loans "
    "WHERE member_id = CAST(:mid AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid) "
    "AND status = :loan_active) AS numeric(18,2)) AS loans_outstanding, "
    "CAST((SELECT COALESCE(SUM(amount), 0) FROM guarantees "
    "WHERE guarantor_member_id = CAST(:mid AS uuid) "
    "AND tenant_id = CAST(:tid AS uuid) "
    "AND status IN (:live0, :live1)) AS numeric(18,2)) AS guarantees_pledged"
)


#: SQL template behind list_members, module-level so the EXPLAIN gate
#: (tests/test_member_no_numeric_order.py) asserts the exact production
#: statement (the EXPLAIN-capture convention). Ordering is NUMERIC-aware for the
#: fixed domain member_no format 'GP-' + zero-padded sequence of AT
#: LEAST four digits (review-hardened): as bare TEXT,
#: 'GP-10000' < 'GP-9999', so past 9,999 members a lexicographic order
#: is wrong and its keyset walk skips/duplicates rows.
#: (length(member_no), member_no) IS the numeric order for this format
#: — a longer value is strictly bigger, equal lengths compare
#: zero-padded-lexicographically == numerically — and the matching
#: row-value predicate in _member_list_clauses walks it exhaustively
#: across the 4->5 digit boundary. Served by the 0041 expression index
#: (tenant_id, length(member_no), member_no), shipped with this query
#: (scalability); the EXPLAIN gate proves the plan carries NO Sort node.
#: The {where} slot only ever receives the static clause literals from
#: _member_list_clauses; every value is a bound parameter (v1.1 rule 6).
MEMBER_LIST_SQL = (
    "SELECT id, member_no, type, name, phone, email, status, version, branch_id, "
    "dividend_payout "
    "FROM members WHERE {where} "
    "ORDER BY length(member_no), member_no LIMIT :limit"
)


#: SQL template behind list_members_with_aggregates (the
#: authorized LIST expansion), module-level so the EXPLAIN
#: capture can assert its plan (the EXPLAIN-capture convention). ONE set-based
#: statement per page: the keyset members page is the driving relation
#: and each row LEFT JOIN LATERALs onto the four aggregate probes, so
#: page rows and their figures come from a single snapshot (no
#: two-statement skew) and there is NO per-row round trip. Every
#: relation carries an explicit tenant predicate doubling the RLS
#: fence (least disclosure); every value is a bound parameter (v1.1 rule 6);
#: the {where} slot only ever receives the static clause literals
#: assembled in _member_list_clauses below. Each probe is servable by
#: an index that shipped with its table in 0001: the deposit/share
#: (tenant_id, member_id) UNIQUE-key probes, idx_loans_member,
#: idx_guarantees_guarantor, and the driving keyset rides the 0041
#: expression index (tenant_id, length(member_no), member_no) — the
#: NUMERIC member_no order (see MEMBER_LIST_SQL for
#: the derivation). The CAST normalises the empty aggregate to column
#: scale so zero-activity rows serialize '0.00', never bare '0'.
MEMBER_LIST_AGGREGATES_SQL = (
    # Aggregate columns carry explicit AS labels:
    # the read path maps BY NAME, so a widened SELECT list (e.g.
    # dividend_payout) can never silently misalign a money figure.
    "SELECT m.id, m.member_no, m.type, m.name, m.phone, m.email, m.status, m.version, "
    "m.branch_id, m.dividend_payout, "
    "CAST(COALESCE(d.balance, 0) AS numeric(18,2)) AS deposits_total, "
    "CAST(COALESCE(s.balance, 0) AS numeric(18,2)) AS shares_total, "
    "CAST(COALESCE(l.total, 0) AS numeric(18,2)) AS loans_outstanding, "
    "CAST(COALESCE(g.total, 0) AS numeric(18,2)) AS guarantees_pledged "
    "FROM members m "
    "LEFT JOIN LATERAL (SELECT balance FROM deposit_accounts "
    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = m.id) d ON true "
    "LEFT JOIN LATERAL (SELECT balance FROM share_accounts "
    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = m.id) s ON true "
    "LEFT JOIN LATERAL (SELECT COALESCE(SUM(balance), 0) AS total FROM loans "
    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = m.id "
    "AND status = :loan_active) l ON true "
    "LEFT JOIN LATERAL (SELECT COALESCE(SUM(amount), 0) AS total FROM guarantees "
    "WHERE tenant_id = CAST(:tid AS uuid) AND guarantor_member_id = m.id "
    "AND status IN (:live0, :live1)) g ON true "
    "WHERE {where} "
    "ORDER BY length(m.member_no), m.member_no LIMIT :limit"
)


async def member_aggregates(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> MemberAggregates:
    """Read-only advisory figures for the single-member read — NO row locks.

    Every BINDING money decision (pledge capacity, exit settlement)
    recomputes its figures under the established row locks; these
    aggregates only inform the register's member drawer. Least
    disclosure (least disclosure): served only by the members:view route, the
    response carries the four amounts and nothing else, and no
    rejection path echoes them.
    """
    # Named column access: a widened SELECT list
    # can never silently misalign a money figure.
    result = await session.execute(
        text(MEMBER_AGGREGATES_SQL),
        {
            "mid": str(member_id),
            "tid": str(tenant_id),
            "loan_active": LoanStatus.ACTIVE.value,
            **live_guarantee_params(),
        },
    )
    row = result.mappings().one()
    return MemberAggregates(
        deposits_total=Decimal(str(row["deposits_total"])),
        shares_total=Decimal(str(row["shares_total"])),
        loans_outstanding=Decimal(str(row["loans_outstanding"])),
        guarantees_pledged=Decimal(str(row["guarantees_pledged"])),
    )


def _row_to_record(row: RowMapping) -> MemberRecord:
    """Build a MemberRecord by COLUMN NAME.

    Positional indexes silently misalign whenever a column joins the
    SELECT list — past widenings shifted them, and new columns
    shift them again. Named access makes an added
    column a no-op here and a missing one a LOUD KeyError, so a money
    aggregate can never be read from the wrong slot. Callers pass
    .mappings() rows (behaviour-identical: every existing member suite
    stays green unchanged).
    """
    return MemberRecord(
        id=uuid.UUID(str(row["id"])),
        member_no=str(row["member_no"]),
        type=MemberType(str(row["type"])),
        name=str(row["name"]),
        phone=str(row["phone"]) if row["phone"] is not None else None,
        email=str(row["email"]) if row["email"] is not None else None,
        status=MemberStatus(str(row["status"])),
        version=int(row["version"]),
        branch_id=uuid.UUID(str(row["branch_id"])) if row["branch_id"] is not None else None,
        dividend_payout=(
            DividendPayout(str(row["dividend_payout"]))
            if row["dividend_payout"] is not None
            else None
        ),
    )


def parse_dividend_payout(raw: str | None) -> DividendPayout | None:
    """Resolve a caller-supplied preference against the CODE-OWNED
    vocabulary.

    An unknown value surfaces as 422 via UnprocessableError — the
    client receives the sanitized category ONLY: the vocabulary is
    never echoed (least disclosure — the established precedent;
    this is exactly why the API body types the field as a bounded
    string, not the enum: FastAPI's structural 422 would enumerate the
    permitted values). None stays None — the honest "not chosen".
    """
    if raw is None:
        return None
    try:
        return DividendPayout(raw)
    except ValueError as exc:
        raise UnprocessableError("unknown dividend payout preference") from exc


async def _next_member_no(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Race-safe GP-XXXX allocation reusing the P7 pattern (concurrency safety).

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
    dividend_payout: str | None = None,
) -> MemberRecord:
    """Create a member with share + deposit accounts in one transaction.

    Numbering is serialised by an advisory lock; the UNIQUE constraint is
    the final safety net and surfaces as 409 so the client retries with a
    fresh request (concurrency safety). Welcome notification is outbox-only
    (reliability). The dividend payout PREFERENCE
    resolves against the code-owned vocabulary — an unknown value is a
    422 BEFORE any row is written; omitted stays NULL, the honest
    "not chosen" state (never a backfilled default).
    """
    payout = parse_dividend_payout(dividend_payout)
    # E.164 storage normalization on write: every accepted
    # spelling stores the same +254… string; invalid input is a
    # sanitized 422 BEFORE any row is written.
    phone = parse_phone(phone)
    member_no = await _next_member_no(session, tenant_id)
    member_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO members "
                "(id, tenant_id, member_no, type, name, phone, email, dividend_payout) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, :type, "
                ":name, :phone, :email, :payout)"
            ),
            {
                "id": str(member_id),
                "tid": str(tenant_id),
                "no": member_no,
                "type": member_type.value,
                "name": name,
                "phone": phone,
                "email": email,
                "payout": payout.value if payout is not None else None,
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
            # The stored preference is part of the mutation's audit
            # truth; None records the honest
            # "not chosen" state.
            "dividend_payout": payout.value if payout is not None else None,
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
        # Every member starts unassigned; only the assignment
        # route writes the 0016 column (attribution never invented).
        branch_id=None,
        dividend_payout=payout,
    )


async def get_member(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> MemberRecord:
    # Explicit tenant predicate on top of RLS (defence in depth,
    # tenant scoping).
    result = await session.execute(
        text(
            "SELECT id, member_no, type, name, phone, email, status, version, "
            "branch_id, dividend_payout "
            "FROM members WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"id": str(member_id), "tid": str(tenant_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    return _row_to_record(row)


def _member_list_clauses(
    tenant_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
    status: MemberStatus | None,
    member_type: MemberType | None,
    member_no: str | None = None,
    col: str = "",
) -> tuple[list[str], dict[str, object]]:
    """Shared keyset WHERE builder for the members register page.

    Every fragment is a static literal chosen in code; every value is a
    bound parameter, so string assembly is injection-safe. `col`
    prefixes the column references (the aggregates variant qualifies
    them with the driving-relation alias).

    member_no (the posting-drawer lookup) is an
    EXACT-match probe served by the 0001 UNIQUE (tenant_id, member_no)
    key — no new index, no new statement; an unknown number yields an
    empty page (200, zero rows — no existence oracle beyond what the
    members:view grant already discloses).
    """
    clauses: list[str] = [f"{col}tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if member_no is not None:
        clauses.append(f"{col}member_no = :member_no")
        params["member_no"] = member_no
    if cursor:
        # The NUMERIC row-value keyset for the fixed
        # 'GP-' + zero-padded-digits format (see MEMBER_LIST_SQL) — a
        # bare text comparison would skip every 5-digit member_no when
        # resuming from a 4-digit cursor. length() runs on the BOUND
        # parameter server-side; nothing is interpolated.
        clauses.append(f"(length({col}member_no), {col}member_no) > (length(:cursor), :cursor)")
        params["cursor"] = cursor
    if status is not None:
        clauses.append(f"{col}status = :status")
        params["status"] = status.value
    if member_type is not None:
        clauses.append(f"{col}type = :mtype")
        params["mtype"] = member_type.value
    return clauses, params


async def list_members(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
    status: MemberStatus | None = None,
    member_type: MemberType | None = None,
    member_no: str | None = None,
) -> MemberPage:
    """Keyset-paginated listing in NUMERIC member_no order (scalability).

    member_no is monotonic per tenant NUMERICALLY:
    the (length(member_no), member_no) order in MEMBER_LIST_SQL is the
    numeric order for the fixed 'GP-' + zero-padded-digits format, and
    the served member_no doubles as a stable cursor. Exactly one
    indexed query per page regardless of table size (the 0041
    expression index). The optional member_no EXACT-match probe (the
    posting-drawer lookup) rides the 0001 UNIQUE
    (tenant_id, member_no) key.
    """
    limit = max(1, min(limit, 100))
    # Opaque signed cursor: verify+unseal BEFORE the
    # keyset predicate; the plaintext member_no keyset is unchanged.
    if cursor:
        cursor = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=MEMBERS_LIST_SCOPE, entity="member"
        )
    clauses, params = _member_list_clauses(
        tenant_id,
        cursor=cursor,
        limit=limit,
        status=status,
        member_type=member_type,
        member_no=member_no,
    )
    # The {where} slot only ever receives the static clause literals
    # from _member_list_clauses; every value is a bound parameter.
    # Named row access via .mappings().
    result = await session.execute(
        text(MEMBER_LIST_SQL.format(where=" AND ".join(clauses))),
        params,
    )
    rows = result.mappings().all()
    items = [_row_to_record(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            items[-1].member_no, tenant_id=tenant_id, endpoint=MEMBERS_LIST_SCOPE
        )
    return MemberPage(items=items, next_cursor=next_cursor)


async def list_members_with_aggregates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
    status: MemberStatus | None = None,
    member_type: MemberType | None = None,
    member_no: str | None = None,
) -> MemberAggregatesPage:
    """Register page WITH the four advisory aggregates.

    ONE set-based statement per page (MEMBER_LIST_AGGREGATES_SQL): the
    keyset members page drives, LEFT JOIN LATERAL supplies the four
    probes, so page rows and their figures come from a single snapshot
    and there is never a per-row round trip (scalability). Read-only
    advisory figures, NO row locks — every BINDING money decision
    (pledge capacity, exit settlement) recomputes its deposits, shares,
    loans, guarantees figures under the established row locks. Least
    disclosure (least disclosure): served only by the members:view route and no
    rejection path echoes an amount.
    """
    limit = max(1, min(limit, 100))
    # Opaque signed cursor: same scope as the plain
    # shape — one endpoint, one keyset, interchangeable positions.
    if cursor:
        cursor = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=MEMBERS_LIST_SCOPE, entity="member"
        )
    clauses, params = _member_list_clauses(
        tenant_id,
        cursor=cursor,
        limit=limit,
        status=status,
        member_type=member_type,
        member_no=member_no,
        col="m.",
    )
    params["loan_active"] = LoanStatus.ACTIVE.value
    params.update(live_guarantee_params())
    # The {where} slot only ever receives the static clause literals
    # from _member_list_clauses; every value is a bound parameter.
    # Named row access: the four money aggregates
    # are read by their AS labels, so a widened SELECT list (e.g.
    # dividend_payout) can never silently shift them.
    result = await session.execute(
        text(MEMBER_LIST_AGGREGATES_SQL.format(where=" AND ".join(clauses))),
        params,
    )
    rows = result.mappings().all()
    page_rows = rows[:limit]
    items = [
        MemberWithAggregates(
            record=_row_to_record(row),
            aggregates=MemberAggregates(
                deposits_total=Decimal(str(row["deposits_total"])),
                shares_total=Decimal(str(row["shares_total"])),
                loans_outstanding=Decimal(str(row["loans_outstanding"])),
                guarantees_pledged=Decimal(str(row["guarantees_pledged"])),
            ),
        )
        for row in page_rows
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            items[-1].record.member_no, tenant_id=tenant_id, endpoint=MEMBERS_LIST_SCOPE
        )
    return MemberAggregatesPage(items=items, next_cursor=next_cursor)


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
    dividend_payout: str | None = None,
) -> MemberRecord:
    """Optimistic-locked edit; a stale version surfaces as 409 (concurrency safety).

    Omitted fields keep their current values; clearing a field is a
    deliberate follow-up feature, not an accidental null overwrite.
    The dividend payout PREFERENCE updates ONLY
    through this versioned optimistic-lock path — an unknown value is
    a 422 BEFORE the compare-and-swap; a stale version is a 409.
    """
    payout = parse_dividend_payout(dividend_payout)
    current = await get_member(session, tenant_id, member_id)
    new_name = name if name is not None else current.name
    # E.164 normalization on write; an untouched field
    # keeps its stored value (legacy formats are read-tolerated — the
    # 0042 backfill migrates them; clearing stays a follow-up feature).
    new_phone = parse_phone(phone) if phone is not None else current.phone
    new_email = email if email is not None else current.email
    new_payout = payout if payout is not None else current.dividend_payout
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth).
                "UPDATE members SET name = :name, phone = :phone, email = :email, "
                "dividend_payout = :payout, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "name": new_name,
                "phone": new_phone,
                "email": new_email,
                "payout": new_payout.value if new_payout is not None else None,
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
            "dividend_payout": (
                current.dividend_payout.value if current.dividend_payout is not None else None
            ),
            "version": current.version,
        },
        after={
            "name": new_name,
            "phone": new_phone,
            "email": new_email,
            "dividend_payout": new_payout.value if new_payout is not None else None,
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
        # The profile edit never touches branch attribution (the
        # assignment route is the sole writer of the column).
        branch_id=current.branch_id,
        dividend_payout=new_payout,
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
    """Transition member status under a row lock (concurrency safety).

    The pure transition function validates the move; the version check
    surfaces 409 on stale edits; the change is audited and announced via
    the outbox (the house gates).
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
        # the sole writer of the terminal state (the documented resolution).
        # Direct status changes can never bypass eligibility, committee
        # approval, and the atomic settlement posting.
        raise ConflictError(
            "member exit is processed through the exit settlement workflow, "
            "not by direct status change"
        )
    if new_status is MemberStatus.DORMANT:
        # The nightly dormancy job (application/dormancy.py) is
        # the sole writer of the dormant state — it derives inactivity
        # from the ledger (v1.1 rule 2); a manual flag would let staff
        # park an account outside its real activity window.
        raise ConflictError(
            "dormancy is derived from ledger activity by the dormancy job, "
            "not by direct status change"
        )
    if current_status is MemberStatus.DORMANT:
        # Reactivation happens automatically inside the deposit
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
                # (defence in depth).
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
    move (concurrency safety); the blockers query is defence in depth — by the
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
    """Dormant -> Active, called ONLY inside the deposit transaction.

    The caller (application/transactions.record_deposit) already holds
    the member row FOR UPDATE, so this status write can never race the
    dormancy job (which locks the same row SKIP LOCKED) — exactly one
    final state. The pure transition function validates the move (gate
    1.4); the audit row and the outbox notification TO THE MEMBER
    commit atomically with the deposit posting (the house gates). The
    notification is the insider-fraud detection control: a fraudster
    reactivating a dormant account to drain it leaves a trace the
    victim sees — removing this enqueue fails the outbox-count
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
                # (defence in depth).
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
    """Keyset-paginated member statement, newest first (scalability).

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
        # Opaque signed cursor: verify+unseal first;
        # the inner '<occurred_at ISO>|<txn id>' parse stays as
        # defense-in-depth on the plaintext.
        cursor = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=STATEMENT_SCOPE, entity="statement"
        )
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
        next_cursor = encode_cursor(
            f"{last[0].isoformat()}|{last[1]}", tenant_id=tenant_id, endpoint=STATEMENT_SCOPE
        )
    return StatementPage(items=items, next_cursor=next_cursor)
