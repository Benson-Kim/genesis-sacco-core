"""Collections & recovery worklist (P13.16, addenda A1-A7).

Minimal recovery workflow behind the prototype's "Initiate recovery"
action and the P18 arrears worklist. NO money moves here: no ledger
rows, no balance writes — recovery_cases is workflow state only.

Design decisions (each a named gate of the prompt):

* OPEN (FM1): a case may be opened only for an NPL-classified ACTIVE
  loan, checked under the loan row LOCK (gate 1.4). NPL-ness is judged
  from loans.classification — the arrears job's PERSISTED output and
  the single source of truth (1.1) — via domain/lending.NPL_CLASSES,
  never a parallel days-past-due recomputation (v1.1 rule 2). The 0026
  DB CHECKs (classification_at_open in NPL set, days_past_due_at_open
  > 90) are the backstop that makes an open-on-performing row
  unrepresentable.
* ONE OPEN CASE PER LOAN (FM2, addendum A3): the 0026 partial UNIQUE
  uq_recovery_cases_one_open is claimed atomically with
  INSERT ... ON CONFLICT DO NOTHING checked by rowcount (v1.1 rule 5)
  — a concurrent double-open lands exactly one row.
* AUTO-CLOSE (FM3, addendum A6): the arrears job's close pass scans
  OPEN cases (case rows locked FOR UPDATE SKIP LOCKED, id-keyset via
  the shared batch runner) and closes those whose loan has left NPL:
  cure = the stored classification (the job's own output, written
  moments earlier in the same run) is no longer in NPL_CLASSES, or the
  loan reached 'closed' (fully repaid / exit-settled — definitionally
  cured); a 'written_off' loan closes as closed_written_off instead
  (the status exists on main since 0001; P13.15 makes it REACHABLE —
  linkage recorded in !47). Idempotent by side-effect counts: the scan
  matches only status='open' rows, so a re-run scans nothing and
  closes nothing new. Every close passes through the single
  domain/recovery.transition gatekeeper (addendum A1).
* ASSIGNMENT (FM4, addendum A4; review B2): the assignee must be an
  ACTIVE user of the SAME tenant whose role holds the recovery
  permission — loan_book:view, the grant that lets them see the
  worklist they work (checked via the shared rbac.actor_access, 1.1)
  — EXCLUDING assurance roles (the Auditor) for audit-independence:
  segregation of duties (three lines of defense) forbids the function
  that reviews the collections trail from being workable in it, or
  recovery_case_notes/audit_log lose their forensic value. The
  exclusion is resolved server-side from the assignee's role_id
  (never a client-supplied flag). A user suspended AFTER assignment
  stays on the case, surfaced in the worklist as
  assignee_unassignable=true (no silent orphan). Reassignment is
  audited with before/after.
* WORKLIST (addendum A5): keyset only, ORDER BY l.days_past_due DESC,
  l.id DESC (the loan id is a stable tiebreak — open cases are 1:1
  with loans under the A3 invariant), served by idx_loans_dpd_worklist
  + uq_recovery_cases_one_open (0026; EXPLAIN in
  tests/test_p1316_explain.py). Least disclosure: rows expose workflow
  fields, days-past-due and the classification pill — NO balance,
  penalty or provision figures; those live behind the loan-detail
  endpoints (P10).
* SLA TIMESTAMPS (addendum A7): opened_at / first_assigned_at /
  closed_at are written by the DATABASE (DEFAULT now() / now() in the
  UPDATE) — request bodies are extra="forbid" and carry no time fields
  (v1.1 rule 1 applied to time).
* NOTES (addendum A2): append-only rows; there is no edit/delete route
  anywhere, so the trail is forensic evidence like audit_log.

Lock order (matches docs/diagrams/lock-order.md, updated in this MR):
the NPL check at open locks the LOAN row — the terminal node of the
established chain member -> accounts -> loans (the P10/P13.8 pattern;
a "LOANS alone" single-node entry) — and only INSERTS the case row
under it. Case mutations (assign, note, close-pass) lock the CASE row
only: recovery_cases is a single-node locker with no outgoing lock
edges (assignee validation reads users/permissions as plain MVCC
snapshot, no lock). No new lock-graph edges.

Every read AND write carries an explicit bound tenant_id predicate on
top of forced RLS (v1.1 rule 4); every value is a bound parameter
(rule 6); errors are least-disclosure — the exact figures live in the
audit rows (rule 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import rbac as rbac_service
from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import build_created_id_cursor, parse_created_id_cursor
from genesis.domain.lending import NPL_CLASSES, LoanStatus
from genesis.domain.rbac import AUDITOR, Action, Module
from genesis.domain.recovery import RecoveryCaseStatus, transition
from genesis.domain.users import UserStatus
from genesis.errors import ConflictError, InvalidInputError, NotFoundError

__all__ = [
    "DEFAULT_CLOSE_BATCH_SIZE",
    "CaseNotePage",
    "CaseNoteRecord",
    "RecoveryCaseRecord",
    "RecoveryClosePassResult",
    "WorklistPage",
    "WorklistRow",
    "add_recovery_note",
    "assign_recovery_case",
    "close_scan_sql",
    "get_recovery_case",
    "list_case_notes",
    "list_worklist",
    "notes_page_sql",
    "open_recovery_case",
    "run_recovery_close_pass",
    "worklist_sql",
]

DEFAULT_CLOSE_BATCH_SIZE = 200

#: Review B2 (segregation of duties / three lines of defense): roles
#: whose FUNCTION is assurance are excluded from collections
#: assignability even though their RBAC grants include loan_book:view
#: (the Auditor views everything BY DESIGN, so the permission matrix
#: alone can never express this exclusion). Identified by role NAME:
#: names are the codebase's canonical role identity — domain/rbac
#: seeds the system matrix keyed by ROLE_NAMES, rbac.seed_permissions
#: looks roles up by name, and the roles table carries no role-type
#: flag column — so a name constant is the least-fragile mechanism
#: available. The name is resolved SERVER-SIDE from the assignee's
#: role_id (users -> roles join), never from a client-supplied flag.
_ASSURANCE_ROLES: frozenset[str] = frozenset({AUDITOR})

#: Bound-parameter set for the NPL labels (v1.1 rule 6: enum values are
#: never string-interpolated). Sorted for a deterministic SQL text so
#: the EXPLAIN capture asserts against the exact production statement.
_NPL_PARAM_NAMES: tuple[str, ...] = tuple(f"npl{i}" for i in range(len(NPL_CLASSES)))
_NPL_PLACEHOLDERS = ", ".join(f":{name}" for name in _NPL_PARAM_NAMES)


def npl_params() -> dict[str, str]:
    """The bound values for the :nplN placeholders (code-owned labels)."""
    values = sorted(c.value for c in NPL_CLASSES)
    return dict(zip(_NPL_PARAM_NAMES, values, strict=True))


@dataclass(frozen=True)
class RecoveryCaseRecord:
    id: uuid.UUID
    loan_id: uuid.UUID
    status: RecoveryCaseStatus
    assignee_id: uuid.UUID | None
    opened_by: uuid.UUID
    classification_at_open: str
    days_past_due_at_open: int
    opened_at: datetime
    first_assigned_at: datetime | None
    closed_at: datetime | None
    version: int


@dataclass(frozen=True)
class WorklistRow:
    case_id: uuid.UUID
    loan_id: uuid.UUID
    member_id: uuid.UUID
    days_past_due: int
    classification: str
    assignee_id: uuid.UUID | None
    #: Addendum A4: true when the assignee was suspended AFTER
    #: assignment — the case stays visible, flagged, never orphaned.
    assignee_unassignable: bool
    opened_at: datetime
    first_assigned_at: datetime | None
    version: int


@dataclass(frozen=True)
class WorklistPage:
    items: list[WorklistRow]
    next_cursor: str | None


@dataclass(frozen=True)
class CaseNoteRecord:
    id: uuid.UUID
    author_id: uuid.UUID
    note: str
    created_at: datetime


@dataclass(frozen=True)
class CaseNotePage:
    items: list[CaseNoteRecord]
    next_cursor: str | None


_CASE_COLUMNS = (
    "id, loan_id, status, assignee_id, opened_by, classification_at_open, "
    "days_past_due_at_open, opened_at, first_assigned_at, closed_at, version"
)


def _row_to_case(row: Any) -> RecoveryCaseRecord:
    return RecoveryCaseRecord(
        id=uuid.UUID(str(row[0])),
        loan_id=uuid.UUID(str(row[1])),
        status=RecoveryCaseStatus(str(row[2])),
        assignee_id=uuid.UUID(str(row[3])) if row[3] is not None else None,
        opened_by=uuid.UUID(str(row[4])),
        classification_at_open=str(row[5]),
        days_past_due_at_open=int(row[6]),
        opened_at=row[7],
        first_assigned_at=row[8],
        closed_at=row[9],
        version=int(row[10]),
    )


async def get_recovery_case(
    session: AsyncSession, tenant_id: uuid.UUID, case_id: uuid.UUID
) -> RecoveryCaseRecord:
    """One case by id. Explicit tenant predicate on top of RLS (rule 4)."""
    row = (
        await session.execute(
            text(
                f"SELECT {_CASE_COLUMNS} FROM recovery_cases "  # noqa: S608 - code-owned columns
                "WHERE id = CAST(:cid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"cid": str(case_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"recovery case {case_id} not found")
    return _row_to_case(row)


async def open_recovery_case(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    loan_id: uuid.UUID,
) -> RecoveryCaseRecord:
    """Open a recovery case for an NPL-classified active loan (FM1/FM2).

    The loan row is locked FOR UPDATE first (gate 1.4): the NPL check
    and the copied classification/dpd snapshot cannot race a concurrent
    repayment or the arrears job (both take the same lock). The
    one-open-case invariant is claimed atomically on the 0026 partial
    UNIQUE via ON CONFLICT DO NOTHING + rowcount (v1.1 rule 5) — under
    the loan lock two concurrent opens serialise, and the claim is the
    DB-level backstop the falsifiability test removes.

    Least disclosure (rule 7): refusals name the category only; the
    exact classification/days-past-due figures live in the audit row
    of the successful open.
    """
    row = (
        await session.execute(
            text(
                "SELECT status, classification, days_past_due FROM loans "
                "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "FOR UPDATE"
            ),
            {"lid": str(loan_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan {loan_id} not found")
    loan_status = LoanStatus(str(row[0]))
    classification = str(row[1])
    days_past_due = int(row[2])
    if loan_status is not LoanStatus.ACTIVE:
        raise ConflictError("recovery applies to active loans")
    if classification not in {c.value for c in NPL_CLASSES}:
        # FM1: open-on-performing refused. No dpd/classification figures
        # in the envelope (least disclosure, rule 7).
        raise ConflictError("loan is not NPL-classified; recovery cannot be initiated")
    case_id = uuid.uuid4()
    claim = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # The atomic one-open-case claim (addendum A3, v1.1
                # rule 5): rowcount 0 means an open case already exists
                # — never SELECT-then-INSERT.
                "INSERT INTO recovery_cases "
                "(id, tenant_id, loan_id, opened_by, classification_at_open, "
                " days_past_due_at_open) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "CAST(:actor AS uuid), :cls, :dpd) "
                "ON CONFLICT (tenant_id, loan_id) WHERE status = 'open' DO NOTHING"
            ),
            {
                "id": str(case_id),
                "tid": str(tenant_id),
                "lid": str(loan_id),
                "actor": str(actor_id),
                "cls": classification,
                "dpd": days_past_due,
            },
        ),
    )
    if claim.rowcount == 0:
        raise ConflictError("an open recovery case already exists for this loan")
    # Exact figures live in the audit row (rule 7), in-transaction
    # (gate 1.5); staff notification rides the outbox (gate 1.2).
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recovery_case.opened",
        entity="recovery_cases",
        entity_id=str(case_id),
        after={
            "loan_id": str(loan_id),
            "classification_at_open": classification,
            "days_past_due_at_open": days_past_due,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recovery_case.opened",
        payload={"case_id": str(case_id), "loan_id": str(loan_id)},
    )
    return await get_recovery_case(session, tenant_id, case_id)


async def assign_recovery_case(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    case_id: uuid.UUID,
    version: int,
    assignee_id: uuid.UUID,
) -> RecoveryCaseRecord:
    """(Re)assign an open case to a recovery-capable user (FM4, A4).

    Case mutations lock the CASE row only (the documented single-node
    lock site). The assignee must be an active user of the SAME tenant
    (explicit tenant predicate + RLS; a foreign id is a plain 404)
    whose role holds the recovery permission — loan_book:view, checked
    through the shared rbac.actor_access (1.1) — and whose role is NOT
    an assurance role (review B2: the Auditor is excluded for
    audit-independence; the role name is resolved server-side from the
    assignee's role_id, refusal is a least-disclosure 409).
    first_assigned_at is set server-side exactly once (addendum A7).
    Reassignment is audited with before/after (gate 1.5).
    """
    row = (
        await session.execute(
            text(
                "SELECT status, assignee_id, version FROM recovery_cases "
                "WHERE id = CAST(:cid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "FOR UPDATE"
            ),
            {"cid": str(case_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"recovery case {case_id} not found")
    if RecoveryCaseStatus(str(row[0])) is not RecoveryCaseStatus.OPEN:
        raise ConflictError("recovery case is closed; assignment applies to open cases")
    previous_assignee = str(row[1]) if row[1] is not None else None
    if int(row[2]) != version:
        raise ConflictError("stale version for recovery case; reload and retry")
    assignee = (
        await session.execute(
            text(
                # The role NAME is resolved server-side from the
                # assignee's role_id (review B2) — users→roles join
                # with explicit tenant predicates on both tables.
                "SELECT u.role_id, u.status, r.name FROM users u "
                "JOIN roles r ON r.id = u.role_id "
                "AND r.tenant_id = CAST(:tid AS uuid) "
                "WHERE u.id = CAST(:uid AS uuid) "
                "AND u.tenant_id = CAST(:tid AS uuid)"
            ),
            {"uid": str(assignee_id), "tid": str(tenant_id)},
        )
    ).first()
    if assignee is None:
        # Same-tenant rule (FM4): a foreign or unknown user id is
        # indistinguishable — 404, zero disclosure.
        raise NotFoundError(f"assignee {assignee_id} not found")
    if UserStatus(str(assignee[1])) is not UserStatus.ACTIVE:
        raise ConflictError("assignee is not an active user")
    access = await rbac_service.actor_access(
        session,
        tenant_id,
        assignee_id,
        uuid.UUID(str(assignee[0])),
        Module.LOAN_BOOK,
        Action.VIEW,
    )
    if access is None or not access.is_active:
        raise ConflictError("assignee is not an active user")
    if not access.allowed:
        # The recovery permission (documented interpretation): the
        # grant that lets the assignee see the worklist they work.
        raise ConflictError("assignee lacks the recovery permission (loan_book:view)")
    if str(assignee[2]) in _ASSURANCE_ROLES:
        # Review B2 — segregation of duties: the assurance function
        # (Auditor) must never be assignable operational work in the
        # process it audits, even though its grants include
        # loan_book:view. Least-disclosure category (rule 7).
        raise ConflictError("assignee role is excluded from collections work")
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Server-side SLA timestamp (A7): first_assigned_at is
                # written once, by the database. Explicit tenant
                # predicate on the write (rule 4).
                "UPDATE recovery_cases SET assignee_id = CAST(:aid AS uuid), "
                "first_assigned_at = COALESCE(first_assigned_at, now()), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:cid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'open' AND version = :ver"
            ),
            {
                "aid": str(assignee_id),
                "cid": str(case_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError("recovery case changed state during assignment; retry")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recovery_case.assigned",
        entity="recovery_cases",
        entity_id=str(case_id),
        before={"assignee_id": previous_assignee},
        after={"assignee_id": str(assignee_id)},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recovery_case.assigned",
        payload={"case_id": str(case_id), "assignee_id": str(assignee_id)},
    )
    return await get_recovery_case(session, tenant_id, case_id)


async def add_recovery_note(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    case_id: uuid.UUID,
    note: str,
) -> CaseNoteRecord:
    """Append a note to an open case (addendum A2: append-only rows).

    The case row is locked so a note can never interleave with the
    close pass (a note lands strictly before or after the close, and a
    closed case takes no further notes). Notes are never UPDATEd or
    DELETEd — no such route exists anywhere.
    """
    if not note.strip():
        raise InvalidInputError("note must not be empty")
    row = (
        await session.execute(
            text(
                "SELECT status FROM recovery_cases "
                "WHERE id = CAST(:cid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "FOR UPDATE"
            ),
            {"cid": str(case_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"recovery case {case_id} not found")
    if RecoveryCaseStatus(str(row[0])) is not RecoveryCaseStatus.OPEN:
        raise ConflictError("recovery case is closed; notes apply to open cases")
    note_id = uuid.uuid4()
    created_at = (
        await session.execute(
            text(
                "INSERT INTO recovery_case_notes (id, tenant_id, case_id, author_id, note) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:cid AS uuid), "
                "CAST(:actor AS uuid), :note) RETURNING created_at"
            ),
            {
                "id": str(note_id),
                "tid": str(tenant_id),
                "cid": str(case_id),
                "actor": str(actor_id),
                "note": note,
            },
        )
    ).scalar_one()
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recovery_case.note_added",
        entity="recovery_cases",
        entity_id=str(case_id),
        after={"note_id": str(note_id), "note": note},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recovery_case.note_added",
        payload={"case_id": str(case_id), "note_id": str(note_id)},
    )
    return CaseNoteRecord(id=note_id, author_id=actor_id, note=note, created_at=created_at)


def notes_page_sql(*, with_cursor: bool) -> str:
    """Per-case notes page, keyset (created_at, id) ASC (gate 1.3).

    Served by idx_recovery_notes_case (0026). Static fragments chosen
    in code; every value is a bound parameter (v1.1 rule 6).
    """
    cursor = "AND (created_at, id) > (CAST(:c_ts AS timestamptz), CAST(:c_id AS uuid)) "
    return (
        "SELECT id, author_id, note, created_at FROM recovery_case_notes "  # noqa: S608
        "WHERE tenant_id = CAST(:tid AS uuid) AND case_id = CAST(:cid AS uuid) "
        f"{cursor if with_cursor else ''}"
        "ORDER BY created_at, id LIMIT :limit"
    )


async def list_case_notes(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    case_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> CaseNotePage:
    """Notes of one case, oldest first (the statement-page shape)."""
    # Existence check with the explicit tenant predicate: a foreign
    # case id is a 404 before any note row is touched (FM5).
    await get_recovery_case(session, tenant_id, case_id)
    params: dict[str, object] = {"tid": str(tenant_id), "cid": str(case_id), "limit": limit + 1}
    if cursor is not None:
        c_ts, c_id = parse_created_id_cursor(cursor, entity="recovery note")
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    stmt = text(notes_page_sql(with_cursor=cursor is not None))
    rows = (await session.execute(stmt, params)).all()
    items = [
        CaseNoteRecord(
            id=uuid.UUID(str(r[0])),
            author_id=uuid.UUID(str(r[1])),
            note=str(r[2]),
            created_at=r[3],
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit and items:
        next_cursor = build_created_id_cursor(items[-1].created_at, items[-1].id)
    return CaseNotePage(items=items, next_cursor=next_cursor)


def worklist_sql(*, with_cursor: bool) -> str:
    """The keyset recovery worklist (addendum A5, gate 1.3).

    ORDER BY l.days_past_due DESC, l.id DESC — the loan id is the
    stable tiebreak (valid: open cases are 1:1 with loans under the
    one-open-case invariant). Served by idx_loans_dpd_worklist walking
    loans in order and probing uq_recovery_cases_one_open per loan
    (both 0026; EXPLAIN-asserted, falsifiable by dropping either).
    Least disclosure: NO balance/penalty/provision columns. The users
    join surfaces the addendum-A4 unassignable flag. Static fragments
    chosen in code; every value is a bound parameter (v1.1 rule 6);
    explicit tenant predicate on top of forced RLS (rule 4).
    """
    cursor = "AND (l.days_past_due, l.id) < (:c_dpd, CAST(:c_id AS uuid)) "
    return (
        "SELECT c.id, c.loan_id, l.member_id, l.days_past_due, l.classification, "  # noqa: S608
        "c.assignee_id, u.status AS assignee_status, c.opened_at, c.first_assigned_at, "
        "c.version "
        "FROM loans l "
        "JOIN recovery_cases c ON c.tenant_id = l.tenant_id AND c.loan_id = l.id "
        "AND c.status = 'open' "
        "LEFT JOIN users u ON u.tenant_id = c.tenant_id AND u.id = c.assignee_id "
        "WHERE l.tenant_id = CAST(:tid AS uuid) "
        f"{cursor if with_cursor else ''}"
        "ORDER BY l.days_past_due DESC, l.id DESC LIMIT :limit"
    )


def _parse_worklist_cursor(cursor: str) -> tuple[int, str]:
    """Parse an opaque '<days_past_due>|<loan uuid>' keyset cursor."""
    dpd_raw, _, id_raw = cursor.partition("|")
    try:
        dpd = int(dpd_raw)
        loan_id = str(uuid.UUID(id_raw))
    except ValueError as exc:
        raise InvalidInputError("invalid worklist cursor") from exc
    if dpd < 0:
        raise InvalidInputError("invalid worklist cursor")
    return dpd, loan_id


async def list_worklist(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
) -> WorklistPage:
    """Open cases ordered by days-past-due, most delinquent first."""
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor is not None:
        c_dpd, c_id = _parse_worklist_cursor(cursor)
        params["c_dpd"] = c_dpd
        params["c_id"] = c_id
    rows = (await session.execute(text(worklist_sql(with_cursor=cursor is not None)), params)).all()
    items: list[WorklistRow] = []
    for r in rows[:limit]:
        assignee_id = uuid.UUID(str(r[5])) if r[5] is not None else None
        assignee_status = str(r[6]) if r[6] is not None else None
        items.append(
            WorklistRow(
                case_id=uuid.UUID(str(r[0])),
                loan_id=uuid.UUID(str(r[1])),
                member_id=uuid.UUID(str(r[2])),
                days_past_due=int(r[3]),
                classification=str(r[4]),
                assignee_id=assignee_id,
                # Addendum A4: suspended-after-assignment is flagged,
                # never silently orphaned.
                assignee_unassignable=(
                    assignee_id is not None and assignee_status != UserStatus.ACTIVE.value
                ),
                opened_at=r[7],
                first_assigned_at=r[8],
                version=int(r[9]),
            )
        )
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = f"{last.days_past_due}|{last.loan_id}"
    return WorklistPage(items=items, next_cursor=next_cursor)


def close_scan_sql(*, with_after: bool) -> str:
    """The arrears job's auto-close scan (FM3, addendum A6).

    Drives from OPEN cases (idx_recovery_cases_open_scan, id keyset)
    and locks CASE rows only — FOR UPDATE OF c SKIP LOCKED, the shared
    batch-runner discipline; the joined loan row is read WITHOUT a
    lock (no lock-graph edge): loans.status and loans.classification
    are the arrears job's own persisted output (1.1) — this run's
    classify pass commits before the close pass starts, so a cure
    detected by THIS run's classification closes in THIS run.

    Close conditions, code-owned:
      * cure — the loan is active and its stored classification left
        the NPL set (bound :nplN parameters), or the loan reached
        'closed' (fully repaid / exit-settled);
      * write-off — the loan reached 'written_off' (unreachable until
        P13.15 wires the transition; the scan handles it as-built and
        the linkage is recorded in !47).

    A re-run scans zero rows (status='open' anti-condition) and closes
    nothing new — idempotency by side-effect counts.
    """
    after = "AND c.id > CAST(:after AS uuid) " if with_after else ""
    return (
        "SELECT c.id, c.loan_id, l.status, l.classification "  # noqa: S608
        "FROM recovery_cases c "
        "JOIN loans l ON l.tenant_id = c.tenant_id AND l.id = c.loan_id "
        "WHERE c.tenant_id = CAST(:tid AS uuid) AND c.status = 'open' "
        "AND ((l.status = 'active' AND l.classification NOT IN "
        f"({_NPL_PLACEHOLDERS})) "
        "OR l.status IN ('closed', 'written_off')) "
        f"{after}"
        "ORDER BY c.id LIMIT :limit "
        "FOR UPDATE OF c SKIP LOCKED"
    )


@dataclass(frozen=True)
class RecoveryClosePassResult:
    scanned: int
    closed_cured: int
    closed_written_off: int


async def _close_one(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    case_id: str,
    loan_id: str,
    loan_status: str,
    classification: str,
) -> RecoveryCaseStatus:
    """Close one LOCKED open case through the single gatekeeper (A1)."""
    outcome = (
        RecoveryCaseStatus.CLOSED_WRITTEN_OFF
        if LoanStatus(loan_status) is LoanStatus.WRITTEN_OFF
        else RecoveryCaseStatus.CLOSED_CURED
    )
    target = transition(RecoveryCaseStatus.OPEN, outcome)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # closed_at is server-side (addendum A7); the status
                # predicate keeps the write idempotent under the row
                # lock; explicit tenant predicate on the write (rule 4).
                "UPDATE recovery_cases SET status = :st, closed_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:cid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'open'"
            ),
            {"st": target.value, "cid": case_id, "tid": str(tenant_id)},
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"recovery case {case_id} changed state during close; retry")
    await record_audit(
        session,
        tenant_id,
        None,
        action="recovery_case.closed",
        entity="recovery_cases",
        entity_id=case_id,
        before={"status": RecoveryCaseStatus.OPEN.value},
        after={
            "status": target.value,
            "loan_id": loan_id,
            "loan_status": loan_status,
            "classification": classification,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recovery_case.closed",
        payload={"case_id": case_id, "loan_id": loan_id, "outcome": target.value},
    )
    return target


async def _close_batch(
    session: AsyncSession,
    after_id: uuid.UUID | None,
    *,
    tenant_id: uuid.UUID,
    batch_size: int,
) -> tuple[int, uuid.UUID | None, tuple[int, int]]:
    """One keyset close batch; matches the shared BatchProcessor shape."""
    params: dict[str, object] = {"tid": str(tenant_id), "limit": batch_size, **npl_params()}
    if after_id is not None:
        params["after"] = str(after_id)
    rows = (
        await session.execute(text(close_scan_sql(with_after=after_id is not None)), params)
    ).all()
    cured = 0
    written_off = 0
    for row in rows:
        outcome = await _close_one(
            session,
            tenant_id,
            case_id=str(row[0]),
            loan_id=str(row[1]),
            loan_status=str(row[2]),
            classification=str(row[3]),
        )
        if outcome is RecoveryCaseStatus.CLOSED_WRITTEN_OFF:
            written_off += 1
        else:
            cured += 1
    last_id = uuid.UUID(str(rows[-1][0])) if rows else None
    return len(rows), last_id, (cured, written_off)


async def run_recovery_close_pass(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    batch_size: int = DEFAULT_CLOSE_BATCH_SIZE,
) -> RecoveryClosePassResult:
    """Auto-close recovery cases whose loan left NPL (FM3, addendum A6).

    Runs as part of the arrears job (after the classify pass, so this
    run's cures close in this run) through the shared batch runner —
    short transactions, case rows FOR UPDATE SKIP LOCKED in id order
    (v1.1 rule 8). Idempotent by side-effect counts: a re-run scans
    zero rows and closes nothing new.
    """
    process = partial(_close_batch, tenant_id=tenant_id, batch_size=batch_size)
    scanned, _, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    return RecoveryClosePassResult(
        scanned=scanned,
        closed_cured=sum(p[0] for p in payloads),
        closed_written_off=sum(p[1] for p in payloads),
    )
