"""Member KYC endpoints: profiles & document checklist (P13.12, gate 1.6).

Every route carries a RequirePermission dependency against the P4
matrix (deny-by-default; the spec-walk test covers them structurally):
profile/document creation under members:create (they are registration
wizard steps), reads under members:view — KYC fields are PII and leave
the server ONLY through these gated responses — and edits under
members:edit. Mutations are idempotent via the Idempotency-Key
middleware (gate 1.4); request bodies reject unknown fields
(extra="forbid").

The profile payload arrives as an opaque object and is validated
SERVER-SIDE against the member row's type (the type is never
caller-supplied); a wrong-type or malformed payload surfaces as 422
with the sanitized category only — submitted KYC values are never
echoed (gate 1.6).

Document uploads are deliberately absent: binary content is deferred
behind the storage decision recorded in ADR-0003; these routes manage
the checklist metadata (type, status, expiry). Every checklist read is
audited in-transaction (P13 blocker f precedent).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequirePermission
from genesis.application import member_kyc as kyc_service
from genesis.application.auth import AuthContext
from genesis.domain.member_kyc import DocumentStatus
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(prefix="/members", tags=["member-kyc"])

_view = RequirePermission(Module.MEMBERS, Action.VIEW)
_create = RequirePermission(Module.MEMBERS, Action.CREATE)
_edit = RequirePermission(Module.MEMBERS, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
CreateCtx = Annotated[AuthContext, Depends(_create)]
EditCtx = Annotated[AuthContext, Depends(_edit)]


class ProfileCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=60)
    profile: dict[str, Any]
    dpa_consent: bool


class ProfileUpdateBody(BaseModel):
    """dpa_consent accepts only true: consent can be granted, never
    withdrawn through the API (and never rewritten at all — 0018
    trigger)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    category: str | None = Field(default=None, min_length=1, max_length=60)
    profile: dict[str, Any] | None = None
    dpa_consent: Literal[True] | None = None


class ProfileOut(BaseModel):
    id: str
    member_id: str
    member_type: str
    category: str
    profile: dict[str, Any]
    dpa_consent_at: str | None
    version: int
    created_at: str


class DocumentCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    doc_type: str = Field(min_length=1, max_length=60)
    expires_at: date | None = None


class DocumentUpdateBody(BaseModel):
    """Omitted fields keep their stored values. expires_at follows
    exclude-unset semantics (review K2): omitted keeps the current
    expiry, an EXPLICIT null clears a wrongly-entered date."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    status: DocumentStatus | None = None
    expires_at: date | None = None


class DocumentOut(BaseModel):
    id: str
    member_id: str
    doc_type: str
    status: str
    expires_at: str | None
    version: int
    created_at: str


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


def _profile_out(record: kyc_service.ProfileRecord) -> ProfileOut:
    return ProfileOut(
        id=str(record.id),
        member_id=str(record.member_id),
        member_type=record.member_type.value,
        category=record.category,
        profile=record.profile,
        dpa_consent_at=(
            record.dpa_consent_at.isoformat() if record.dpa_consent_at is not None else None
        ),
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


def _document_out(record: kyc_service.DocumentRecord) -> DocumentOut:
    return DocumentOut(
        id=str(record.id),
        member_id=str(record.member_id),
        doc_type=record.doc_type,
        status=record.status.value,
        expires_at=record.expires_at.isoformat() if record.expires_at is not None else None,
        version=record.version,
        created_at=record.created_at.isoformat(),
    )


@router.post("/{member_id}/profile", status_code=201)
async def create_profile(
    member_id: uuid.UUID, body: ProfileCreateBody, ctx: CreateCtx
) -> ProfileOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await kyc_service.create_profile(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            category=body.category,
            profile=body.profile,
            dpa_consent=body.dpa_consent,
        )
    return _profile_out(record)


@router.get("/{member_id}/profile")
async def get_profile(member_id: uuid.UUID, ctx: ViewCtx) -> ProfileOut:
    """Profile read; every access writes an audit row (review K1)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await kyc_service.read_profile(session, ctx.tenant_id, ctx.user_id, member_id)
    return _profile_out(record)


@router.put("/{member_id}/profile")
async def update_profile(member_id: uuid.UUID, body: ProfileUpdateBody, ctx: EditCtx) -> ProfileOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await kyc_service.update_profile(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            version=body.version,
            category=body.category,
            profile=body.profile,
            dpa_consent=bool(body.dpa_consent),
        )
    return _profile_out(record)


@router.post("/{member_id}/documents", status_code=201)
async def create_document(
    member_id: uuid.UUID, body: DocumentCreateBody, ctx: CreateCtx
) -> DocumentOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await kyc_service.create_document(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            doc_type=body.doc_type,
            expires_at=body.expires_at,
        )
    return _document_out(record)


@router.get("/{member_id}/documents")
async def list_documents(
    member_id: uuid.UUID,
    ctx: ViewCtx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DocumentListResponse:
    """Checklist read; every access writes an audit row (blocker f)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        page = await kyc_service.list_documents(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            cursor=cursor,
            limit=limit,
        )
    return DocumentListResponse(
        items=[_document_out(r) for r in page.items],
        next_cursor=page.next_cursor,
    )


@router.put("/{member_id}/documents/{document_id}")
async def update_document(
    member_id: uuid.UUID,
    document_id: uuid.UUID,
    body: DocumentUpdateBody,
    ctx: EditCtx,
) -> DocumentOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await kyc_service.update_document(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id,
            document_id,
            version=body.version,
            status=body.status,
            # Omitted vs explicit null (review K2): only a field the
            # caller actually sent reaches the service.
            expires_at=(
                body.expires_at if "expires_at" in body.model_fields_set else kyc_service.UNSET
            ),
        )
    return _document_out(record)
