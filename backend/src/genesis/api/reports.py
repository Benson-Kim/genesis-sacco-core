"""Reports & exports endpoints (P13, gates 1.3-1.6).

Permission gates (P4 matrix, decided and documented):

  * request / status / download / drain queue — reports x VIEW. An
    export is a materialised READ of the reports module: the roles the
    prototype matrix entitles to reports (System Admin, Branch
    Manager, Loan Officer, Credit Committee, Accountant, Auditor) are
    exactly the ones who may export them, and reports:create would
    exclude the Auditor/Accountant — the primary consumers.
    Compensating controls for the exfiltration risk: per-column PII
    allow-lists frozen at request time (blocker e), requester-only
    status/downloads, expiring unguessable tokens, and an audit row
    for every request, render and download (blocker f).

  * Draining the queue (POST /jobs/exports) renders scopes that were
    already fixed per-requester, so running it grants the runner
    nothing; it exists for operations, mirroring POST /jobs/arrears.

Callers never supply money, cost, format, row-limit or storage-path
parameters (blocker a): request bodies are extra="forbid" and carry
only the report name and its declared id/date filters; everything else
comes from server-side configuration.
"""

from __future__ import annotations

import uuid
from datetime import date
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from genesis.api.authz import RequirePermission
from genesis.application import exports as exports_service
from genesis.application.auth import AuthContext
from genesis.application.reports import ExportFilters, ReportName
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session, tenant_snapshot_session
from genesis.settings import get_settings

router = APIRouter(tags=["reports"])

_view = RequirePermission(Module.REPORTS, Action.VIEW)

ViewCtx = Annotated[AuthContext, Depends(_view)]


class ExportFiltersBody(BaseModel):
    """The only caller-suppliable scope (blocker a): ids and dates."""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID | None = None
    exit_id: uuid.UUID | None = None
    declaration_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None


class ExportRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: ReportName
    filters: ExportFiltersBody | None = None


class ArtifactOut(BaseModel):
    row_count: int
    truncated: bool
    row_limit: int
    expires_at: str
    csv_download: str
    pdf_download: str


class ExportOut(BaseModel):
    id: str
    report: str
    status: str
    filters: dict[str, str]
    allowed_columns: list[str]
    as_of: str
    completed_at: str | None
    created_at: str
    artifact: ArtifactOut | None


class ExportCycleOut(BaseModel):
    completed: int
    failed: int


def _out(
    record: exports_service.ExportRecord,
    artifact: exports_service.ArtifactInfo | None,
) -> ExportOut:
    artifact_out = None
    if artifact is not None:
        artifact_out = ArtifactOut(
            row_count=artifact.row_count,
            truncated=artifact.truncated,
            row_limit=artifact.row_limit,
            expires_at=artifact.expires_at.isoformat(),
            csv_download=f"/exports/downloads/{artifact.csv_token}",
            pdf_download=f"/exports/downloads/{artifact.pdf_token}",
        )
    return ExportOut(
        id=str(record.id),
        report=record.report.value,
        status=record.status,
        filters=record.filters,
        allowed_columns=list(record.allowed_columns),
        as_of=record.as_of.isoformat(),
        completed_at=record.completed_at.isoformat() if record.completed_at else None,
        created_at=record.created_at.isoformat(),
        artifact=artifact_out,
    )


@router.post("/exports", status_code=201)
async def request_export(body: ExportRequestBody, ctx: ViewCtx) -> ExportOut:
    """Queue an export; scope and entitlement are frozen server-side."""
    filters_body = body.filters or ExportFiltersBody()
    filters = ExportFilters(
        member_id=filters_body.member_id,
        exit_id=filters_body.exit_id,
        declaration_id=filters_body.declaration_id,
        date_from=filters_body.date_from,
        date_to=filters_body.date_to,
    )
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await exports_service.request_export(
            session,
            ctx.tenant_id,
            ctx.user_id,
            ctx.role_id,
            report=body.report,
            filters=filters,
        )
    return _out(record, None)


@router.get("/exports/downloads/{token}")
async def download_export(token: str, ctx: ViewCtx) -> Response:
    """Stream a rendered artifact; sets the export truncation headers."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        artifact = await exports_service.download_artifact(
            session, ctx.tenant_id, ctx.user_id, token
        )
    extension = "csv" if artifact.is_csv else "pdf"
    filename = f"{artifact.report.value}-{artifact.as_of.date().isoformat()}.{extension}"
    return Response(
        content=artifact.content,
        media_type="text/csv; charset=utf-8" if artifact.is_csv else "application/pdf",
        headers={
            "X-Export-Truncated": "true" if artifact.truncated else "false",
            "X-Export-Limit": str(artifact.row_limit),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/exports/{export_id}")
async def get_export(export_id: uuid.UUID, ctx: ViewCtx) -> ExportOut:
    """Status + download links; requester-only (least disclosure)."""
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await exports_service.get_export(
            session, ctx.tenant_id, export_id, requester_id=ctx.user_id
        )
        artifact = None
        if record.status == "completed":
            artifact = await exports_service.get_artifact_info(session, ctx.tenant_id, record.id)
    return _out(record, artifact)


@router.post("/jobs/exports")
async def run_export_jobs(ctx: ViewCtx) -> ExportCycleOut:
    """Drain the caller's tenant export queue (operations mirror of the
    worker loop); each job renders in its own snapshot transaction."""
    factory = get_sessionmaker(get_settings().database_url)
    summary = await exports_service.run_pending_exports(
        partial(tenant_snapshot_session, factory, ctx.tenant_id), ctx.tenant_id
    )
    return ExportCycleOut(completed=summary.completed, failed=summary.failed)
