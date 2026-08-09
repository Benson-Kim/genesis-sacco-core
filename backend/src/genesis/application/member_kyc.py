"""Member KYC application services: profiles & documents.

Persists the prototype's type-specific registration data (the recorded gap
analysis): one profile row per member (per-type JSONB validated by
domain/member_kyc.py, DB-CHECKed by 0018), the member category, the
DPA-2019 consent timestamp, and the per-type document checklist as
metadata rows. Binary document content is deferred behind the storage
decision in ADR-0003 — metadata-only is the recorded fallback.

Reuse (reuse-first): member lookups go through the P8 members service;
audit rows through the shared record_audit writer; creation claims are
INSERT ... ON CONFLICT DO NOTHING checked by rowcount (v1.1 rule 5);
edits are optimistic-locked single-statement UPDATEs surfacing 409 on
stale versions (concurrency safety); listings are keyset-paginated (scalability).
No outbox events: KYC changes have no notification consumer — there is
no side-effect to dispatch (the branches-registry precedent; reliability concerns
delivery, not mutation).

Consent immutability & TOCTOU: consent is granted inside the same
version-guarded UPDATE as the rest of the edit, after an
already-captured check — a concurrent grant bumps the version and the
stale writer gets 409, so there is no check-then-set window. The 0018
trigger is the DB backstop: not even raw SQL can rewrite or delete a
captured consent.

PII discipline (least disclosure): KYC fields are returned only through routes
gated by members:view; they never appear in error messages (validation
errors carry field locations and types only) or logs (handlers log
sanitized categories). Audit before/after payloads DO carry the exact
values — that is the trail — and are disclosed by the audit-log viewer
only to roles holding members:view (ENTITY_MODULES redaction,
the admin-audit precedent). Document access is audited like exports (P13
blocker f): every checklist read writes an in-transaction audit row
recording who accessed which member's documents — and the profile
body, the platform's highest-value PII read (ID numbers, KRA PINs,
income, next-of-kin), is access-audited the same way (review K1):
every GET of a profile writes a member_profile.access row.

Audit write amplification (review K4, acknowledged): access-auditing
browse endpoints means audit_log grows with READ traffic, not just
mutations — a polling admin UI multiplies rows per (actor, member)
unboundedly. Accepted for now: DPA-2019 traceability of PII reads
outweighs the storage cost, audit_log is append-only and cheap to
write, and no consumer scans it unindexed. Recorded follow-up (the
follow-ups list): an audit_log retention/partitioning policy with
per-(actor, member, day) aggregation for *.access rows before the
row count becomes operationally significant.

Lock ordering: these paths take NO explicit row locks — every write is
a single version-guarded UPDATE or an atomic-claim INSERT on leaf
tables outside the P12 settlement chain (member -> deposit account ->
loans) and the admin-set chain, so they cannot participate in a
lock cycle.

All reads AND writes carry explicit bound tenant_id predicates on top
of forced RLS (v1.1 rule 4; falsifiability tests in
tests/test_tenant_predicates.py style live in tests/test_member_kyc_api.py).
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import members as members_service
from genesis.application.audit import record_audit
from genesis.application.pagination import decode_cursor, encode_cursor
from genesis.domain.member_kyc import (
    DocumentStatus,
    DocumentTypeError,
    InvalidDocumentTransitionError,
    ProfileValidationError,
    document_transition,
    validate_document_type,
    validate_profile,
)
from genesis.domain.members import MemberType
from genesis.errors import ConflictError, NotFoundError, UnprocessableError

#: Cursor scope id: signed cursors are bound to this
#: endpoint and this tenant - no cross-scope replay (tenant isolation).
DOCUMENTS_LIST_SCOPE = "member_kyc.documents"


class Unset(enum.Enum):
    """Omitted-vs-null marker (review K2).

    The API layer maps a body field that was OMITTED from the request
    to UNSET ("keep the stored value") and an explicit JSON null to
    None ("clear the value") — the two must stay distinguishable or a
    wrongly-entered document expiry becomes permanent.
    """

    TOKEN = 0


#: Singleton sentinel for "field omitted from the request body".
UNSET: Final = Unset.TOKEN


@dataclass(frozen=True)
class ProfileRecord:
    id: uuid.UUID
    member_id: uuid.UUID
    member_type: MemberType
    category: str
    profile: dict[str, Any]
    dpa_consent_at: datetime | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class DocumentRecord:
    id: uuid.UUID
    member_id: uuid.UUID
    doc_type: str
    status: DocumentStatus
    expires_at: date | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class DocumentPage:
    items: list[DocumentRecord]
    next_cursor: str | None


_PROFILE_COLUMNS = (
    "p.id, p.member_id, p.member_type, p.category, p.profile, "
    "p.dpa_consent_at, p.version, p.created_at"
)

_DOCUMENT_COLUMNS = "d.id, d.member_id, d.doc_type, d.status, d.expires_at, d.version, d.created_at"


def profile_lookup_sql() -> str:
    """Profile-by-member lookup, served by uq_member_profiles_member
    (0018; EXPLAIN-asserted in tests/test_p1312_explain.py). Explicit
    tenant predicate on top of RLS (v1.1 rule 4)."""
    return (
        f"SELECT {_PROFILE_COLUMNS} FROM member_profiles p "  # noqa: S608
        "WHERE p.member_id = CAST(:mid AS uuid) AND p.tenant_id = CAST(:tid AS uuid)"
    )


def documents_page_sql(*, with_cursor: bool) -> str:
    """Documents checklist page, keyset on doc_type within one member
    (scalability), served by uq_member_documents_checklist (0018 — the
    claim constraint's index IS the listing index). Fragments are
    static literals chosen in code; every value is a bound parameter
    (v1.1 rule 6)."""
    clauses = [
        "d.tenant_id = CAST(:tid AS uuid)",
        "d.member_id = CAST(:mid AS uuid)",
    ]
    if with_cursor:
        clauses.append("d.doc_type > :cursor")
    where = " AND ".join(clauses)
    return (
        f"SELECT {_DOCUMENT_COLUMNS} FROM member_documents d "  # noqa: S608
        f"WHERE {where} ORDER BY d.doc_type LIMIT :limit"
    )


def _row_to_profile(row: Any) -> ProfileRecord:
    return ProfileRecord(
        id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        member_type=MemberType(str(row[2])),
        category=str(row[3]),
        profile=dict(row[4]),
        dpa_consent_at=row[5],
        version=int(row[6]),
        created_at=row[7],
    )


def _row_to_document(row: Any) -> DocumentRecord:
    return DocumentRecord(
        id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        doc_type=str(row[2]),
        status=DocumentStatus(str(row[3])),
        expires_at=row[4],
        version=int(row[5]),
        created_at=row[6],
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


async def get_profile(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> ProfileRecord:
    """Internal profile lookup — NOT access-audited by itself.

    API reads must go through read_profile (review K1); this helper
    also serves create/update read-backs, which are already audited as
    their own mutations and are not disclosures.
    """
    row = (
        await session.execute(
            text(profile_lookup_sql()),
            {"mid": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"profile for member {member_id} not found")
    return _row_to_profile(row)


async def read_profile(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
) -> ProfileRecord:
    """The API-facing profile read; the ACCESS is audited (review K1).

    The profile body is the platform's highest-value PII surface, so
    under DPA-2019 every read leaves an in-transaction trail exactly
    like the document-checklist reads (the list_documents pattern):
    who read whose profile, when. A 404 (no profile) writes no row —
    nothing was disclosed; a 403 never reaches this function (authz
    denies in the route dependency).
    """
    record = await get_profile(session, tenant_id, member_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_profile.access",
        entity="member_profiles",
        entity_id=str(member_id),
        after={"profile_id": str(record.id), "member_id": str(member_id)},
    )
    return record


async def create_profile(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    category: str,
    profile: Mapping[str, Any],
    dpa_consent: bool,
) -> ProfileRecord:
    """Create the member's KYC profile (registration step 2-3).

    The payload is validated against the MEMBER ROW's type — the type
    is never caller-supplied — and a mismatch surfaces as 422 with no
    echoed values (least disclosure). The (tenant_id, member_id) claim is
    atomic (v1.1 rule 5). Consent, when given, is timestamped by the
    database at capture; the 0018 trigger makes it immutable from then
    on.
    """
    member = await members_service.get_member(session, tenant_id, member_id)
    try:
        validated = validate_profile(member.type, profile)
    except ProfileValidationError as exc:
        raise UnprocessableError(str(exc)) from exc
    profile_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO member_profiles "
                "(id, tenant_id, member_id, member_type, category, profile, "
                " dpa_consent_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                ":mtype, :category, CAST(:profile AS jsonb), "
                "CASE WHEN :consent THEN now() END) "
                "ON CONFLICT (tenant_id, member_id) DO NOTHING RETURNING id"
            ),
            {
                "id": str(profile_id),
                "tid": str(tenant_id),
                "mid": str(member_id),
                "mtype": member.type.value,
                "category": category,
                "profile": _json(validated),
                "consent": dpa_consent,
            },
        )
    ).first()
    if claimed is None:
        raise ConflictError(f"profile already exists for member {member_id}")
    record = await get_profile(session, tenant_id, member_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_profile.create",
        entity="member_profiles",
        entity_id=str(profile_id),
        after={
            "member_id": str(member_id),
            "member_type": member.type.value,
            "category": category,
            "profile": validated,
            "dpa_consent_at": _iso(record.dpa_consent_at),
        },
    )
    return record


async def update_profile(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    version: int,
    category: str | None = None,
    profile: Mapping[str, Any] | None = None,
    dpa_consent: bool = False,
) -> ProfileRecord:
    """Optimistic-locked profile edit; stale versions surface 409.

    Consent may only be GRANTED (dpa_consent=True on a profile without
    one); granting again is 409, and changing/clearing is impossible by
    construction — the UPDATE keeps a captured value, the version guard
    closes the check-then-set window, and the 0018 trigger backstops
    raw SQL. Omitted fields keep their current values.
    """
    current = await get_profile(session, tenant_id, member_id)
    if dpa_consent and current.dpa_consent_at is not None:
        raise ConflictError(f"DPA consent already captured for member {member_id}")
    new_category = category if category is not None else current.category
    if profile is not None:
        try:
            validated = validate_profile(current.member_type, profile)
        except ProfileValidationError as exc:
            raise UnprocessableError(str(exc)) from exc
    else:
        validated = current.profile
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write (v1.1 rule 4).
                "UPDATE member_profiles SET category = :category, "
                "profile = CAST(:profile AS jsonb), "
                "dpa_consent_at = CASE WHEN :grant THEN now() "
                "ELSE dpa_consent_at END, "
                "version = version + 1, updated_at = now() "
                "WHERE member_id = CAST(:mid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND version = :ver"
            ),
            {
                "category": new_category,
                "profile": _json(validated),
                "grant": dpa_consent,
                "mid": str(member_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for profile of member {member_id}")
    record = await get_profile(session, tenant_id, member_id)
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_profile.update",
        entity="member_profiles",
        entity_id=str(current.id),
        before={
            "category": current.category,
            "profile": current.profile,
            "dpa_consent_at": _iso(current.dpa_consent_at),
            "version": current.version,
        },
        after={
            "category": record.category,
            "profile": record.profile,
            "dpa_consent_at": _iso(record.dpa_consent_at),
            "version": record.version,
        },
    )
    return record


# ---------------------------------------------------------------------------
# Documents (metadata only — ADR-0003)
# ---------------------------------------------------------------------------


async def create_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    doc_type: str,
    expires_at: date | None = None,
) -> DocumentRecord:
    """Open one checklist item for the member (status starts 'pending').

    The type is validated against the MEMBER ROW's checklist (422 on a
    foreign type); the (tenant_id, member_id, doc_type) claim is atomic
    (v1.1 rule 5) so a duplicate checklist row is impossible.
    """
    member = await members_service.get_member(session, tenant_id, member_id)
    try:
        validate_document_type(member.type, doc_type)
    except DocumentTypeError as exc:
        raise UnprocessableError(str(exc)) from exc
    document_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO member_documents "
                "(id, tenant_id, member_id, member_type, doc_type, expires_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                ":mtype, :dtype, :expires) "
                "ON CONFLICT (tenant_id, member_id, doc_type) DO NOTHING RETURNING id"
            ),
            {
                "id": str(document_id),
                "tid": str(tenant_id),
                "mid": str(member_id),
                "mtype": member.type.value,
                "dtype": doc_type,
                "expires": expires_at,
            },
        )
    ).first()
    if claimed is None:
        raise ConflictError(f"document {doc_type} already tracked for member {member_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_document.create",
        entity="member_documents",
        entity_id=str(document_id),
        after={
            "member_id": str(member_id),
            "doc_type": doc_type,
            "status": DocumentStatus.PENDING.value,
            "expires_at": _iso(expires_at),
        },
    )
    return await _get_document(session, tenant_id, member_id, document_id)


async def list_documents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> DocumentPage:
    """Keyset-paginated checklist for one member; the ACCESS is audited.

    Document metadata is a PII-adjacent exfiltration surface, so every
    read writes an in-transaction audit row capturing who accessed
    which member's documents (the export-download precedent, P13
    blocker f). doc_type is unique per member, so it doubles as the
    cursor and every page is one scan of the claim index (scalability).
    """
    await members_service.get_member(session, tenant_id, member_id)
    limit = max(1, min(limit, 100))
    params: dict[str, object] = {
        "tid": str(tenant_id),
        "mid": str(member_id),
        "limit": limit + 1,
    }
    if cursor:
        # Opaque signed cursor: verify+unseal before
        # the keyset predicate; the plaintext doc_type is unchanged.
        params["cursor"] = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=DOCUMENTS_LIST_SCOPE, entity="document"
        )
    rows = (
        await session.execute(text(documents_page_sql(with_cursor=cursor is not None)), params)
    ).all()
    items = [_row_to_document(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = encode_cursor(
            items[-1].doc_type, tenant_id=tenant_id, endpoint=DOCUMENTS_LIST_SCOPE
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_document.access",
        entity="member_documents",
        entity_id=str(member_id),
        after={
            "document_ids": [str(item.id) for item in items],
            "count": len(items),
        },
    )
    return DocumentPage(items=items, next_cursor=next_cursor)


async def _get_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    document_id: uuid.UUID,
) -> DocumentRecord:
    # Explicit tenant AND member predicates on top of RLS (v1.1 rule 4;
    # the member predicate keeps the path-addressed resource honest).
    row = (
        await session.execute(
            text(
                f"SELECT {_DOCUMENT_COLUMNS} FROM member_documents d "  # noqa: S608
                "WHERE d.id = CAST(:id AS uuid) "
                "AND d.member_id = CAST(:mid AS uuid) "
                "AND d.tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(document_id), "mid": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"document {document_id} not found")
    return _row_to_document(row)


async def update_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    member_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    version: int,
    status: DocumentStatus | None = None,
    expires_at: date | Unset | None = UNSET,
) -> DocumentRecord:
    """Optimistic-locked status/expiry update; stale versions surface 409.

    Status moves through the domain transition map only (concurrency safety).
    Omitted fields keep their current values; an EXPLICIT null expiry
    CLEARS a wrongly-entered date (review K2) — UNSET keeps, None
    clears, a date sets.
    """
    current = await _get_document(session, tenant_id, member_id, document_id)
    if status is not None:
        try:
            document_transition(current.status, status)
        except InvalidDocumentTransitionError as exc:
            raise ConflictError(str(exc)) from exc
    new_status = status if status is not None else current.status
    new_expires = current.expires_at if isinstance(expires_at, Unset) else expires_at
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write (v1.1 rule 4).
                "UPDATE member_documents SET status = :status, "
                "expires_at = :expires, version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) "
                "AND member_id = CAST(:mid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND version = :ver"
            ),
            {
                "status": new_status.value,
                "expires": new_expires,
                "id": str(document_id),
                "mid": str(member_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for document {document_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_document.update",
        entity="member_documents",
        entity_id=str(document_id),
        before={
            "status": current.status.value,
            "expires_at": _iso(current.expires_at),
            "version": current.version,
        },
        after={
            "status": new_status.value,
            "expires_at": _iso(new_expires),
            "version": current.version + 1,
        },
    )
    return await _get_document(session, tenant_id, member_id, document_id)


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload)


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None
