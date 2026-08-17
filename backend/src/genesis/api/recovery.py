"""Collections & recovery worklist endpoints (P13.16, gate 1.6).

The prototype "Initiate recovery" action and the P18 arrears worklist.
Every route carries a RequirePermission dependency (deny by default);
mutations are idempotent via the Idempotency-Key middleware (gate 1.4).
Request bodies reject unknown fields (extra="forbid") — in particular
NO timestamp may ride in (opened_at/first_assigned_at/closed_at are
server-side only, addendum A7), and there is NO close route (cases
close automatically on cure or write-off in the arrears job) and NO
note edit/delete route (append-only, addendum A2).

Permissions (P4 matrix, documented interpretation): the worklist and
case reads sit on loan_book:view (the loan-detail entitlement — the
worklist itself discloses no balances); opening a case is
loan_book:create; assignment and notes are loan_book:edit. An
ASSIGNEE must be an active same-tenant user holding loan_book:view —
the grant that lets them see the worklist they work — EXCLUDING
assurance roles (the Auditor) for audit-independence: segregation of
duties forbids the collections trail's reviewer from being workable
in it (review B2; enforced in the application service against the
role resolved server-side from the assignee's role_id, addendum A4).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import recovery as recovery_service
from genesis.application.auth import AuthContext
from genesis.application.recovery import CaseNoteRecord, RecoveryCaseRecord, WorklistRow
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/recovery-cases", tags=["recovery"])

_view = RequirePermission(Module.LOAN_BOOK, Action.VIEW)
_create = RequirePermission(Module.LOAN_BOOK, Action.CREATE)
_edit = RequirePermission(Module.LOAN_BOOK, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
CreateCtx = Annotated[AuthContext, Depends(_create)]
EditCtx = Annotated[AuthContext, Depends(_edit)]


class CaseOpenBody(BaseModel):
    """extra="forbid": no caller-supplied timestamps, classification or
    days-past-due — the NPL snapshot is read under the loan row lock
    and every SLA timestamp is written server-side (addendum A7)."""

    model_config = ConfigDict(extra="forbid")

    loan_id: uuid.UUID


class CaseAssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    assignee_id: uuid.UUID


class CaseNoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000)


class CaseOut(BaseModel):
    id: str
    loan_id: str
    status: str
    assignee_id: str | None
    opened_by: str
    classification_at_open: str
    days_past_due_at_open: int
    opened_at: str
    first_assigned_at: str | None
    closed_at: str | None
    version: int


class WorklistRowOut(BaseModel):
    """Least disclosure (addendum A5): workflow fields, days-past-due
    and the classification pill only — NO balances/penalty figures;
    those live behind the P10 loan-detail endpoints."""

    case_id: str
    loan_id: str
    member_id: str
    days_past_due: int
    classification: str
    assignee_id: str | None
    assignee_unassignable: bool
    opened_at: str
    first_assigned_at: str | None
    version: int


class WorklistOut(BaseModel):
    items: list[WorklistRowOut]
    next_cursor: str | None


class NoteOut(BaseModel):
    id: str
    author_id: str
    note: str
    created_at: str


class NotesOut(BaseModel):
    items: list[NoteOut]
    next_cursor: str | None


def _case_out(record: RecoveryCaseRecord) -> CaseOut:
    return CaseOut(
        id=str(record.id),
        loan_id=str(record.loan_id),
        status=record.status.value,
        assignee_id=str(record.assignee_id) if record.assignee_id else None,
        opened_by=str(record.opened_by),
        classification_at_open=record.classification_at_open,
        days_past_due_at_open=record.days_past_due_at_open,
        opened_at=record.opened_at.isoformat(),
        first_assigned_at=(
            record.first_assigned_at.isoformat() if record.first_assigned_at else None
        ),
        closed_at=record.closed_at.isoformat() if record.closed_at else None,
        version=record.version,
    )


def _row_out(row: WorklistRow) -> WorklistRowOut:
    return WorklistRowOut(
        case_id=str(row.case_id),
        loan_id=str(row.loan_id),
        member_id=str(row.member_id),
        days_past_due=row.days_past_due,
        classification=row.classification,
        assignee_id=str(row.assignee_id) if row.assignee_id else None,
        assignee_unassignable=row.assignee_unassignable,
        opened_at=row.opened_at.isoformat(),
        first_assigned_at=row.first_assigned_at.isoformat() if row.first_assigned_at else None,
        version=row.version,
    )


def _note_out(note: CaseNoteRecord) -> NoteOut:
    return NoteOut(
        id=str(note.id),
        author_id=str(note.author_id),
        note=note.note,
        created_at=note.created_at.isoformat(),
    )


@router.post("", status_code=201)
async def open_case(body: CaseOpenBody, ctx: CreateCtx) -> CaseOut:
    """Initiate recovery on an NPL-classified active loan (FM1/FM2)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await recovery_service.open_recovery_case(
            session, ctx.tenant_id, ctx.user_id, loan_id=body.loan_id
        )
    return _case_out(record)


@router.get("")
async def worklist(
    ctx: ViewCtx,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> WorklistOut:
    """Keyset worklist, most delinquent first (addendum A5)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await recovery_service.list_worklist(
            session, ctx.tenant_id, cursor=cursor, limit=limit
        )
    return WorklistOut(items=[_row_out(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/{case_id}")
async def get_case(case_id: uuid.UUID, ctx: ViewCtx) -> CaseOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await recovery_service.get_recovery_case(session, ctx.tenant_id, case_id)
    return _case_out(record)


@router.post("/{case_id}/assign")
async def assign_case(case_id: uuid.UUID, body: CaseAssignBody, ctx: EditCtx) -> CaseOut:
    """Assign/reassign an open case to an active same-tenant user (A4)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await recovery_service.assign_recovery_case(
            session,
            ctx.tenant_id,
            ctx.user_id,
            case_id=case_id,
            version=body.version,
            assignee_id=body.assignee_id,
        )
    return _case_out(record)


@router.post("/{case_id}/notes", status_code=201)
async def add_note(case_id: uuid.UUID, body: CaseNoteBody, ctx: EditCtx) -> NoteOut:
    """Append a note (addendum A2: append-only, no edit route exists)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        note = await recovery_service.add_recovery_note(
            session, ctx.tenant_id, ctx.user_id, case_id=case_id, note=body.note
        )
    return _note_out(note)


@router.get("/{case_id}/notes")
async def list_notes(
    case_id: uuid.UUID,
    ctx: ViewCtx,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotesOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await recovery_service.list_case_notes(
            session, ctx.tenant_id, case_id=case_id, cursor=cursor, limit=limit
        )
    return NotesOut(items=[_note_out(n) for n in page.items], next_cursor=page.next_cursor)
