"""core/exports: export engine, job runner, artifact access (P13).

The only way report data leaves the system (MASTER_PROMPT 1.3):

  * run_export — keyset streaming with a hard server-side row cap;
    every batch asks for batch_size + 1 rows so truncation is detected
    exactly; rendering work runs off the event loop
    (asyncio.to_thread), so an export never stalls concurrently served
    requests (P13 blocker d and the EXIT latency test).

  * request_export — persists the export scope (report, filters,
    column allow-list) resolved SERVER-SIDE at request
    time; the artifact as-of instant is advanced to the worker snapshot
    start when rendering begins. Callers never supply money, cost,
    format, limit, or storage
    parameters (P13 blocker a); PII columns are granted only to roles
    holding members:view and the grant is frozen into the job row
    (P13 blocker e).

  * run_export_job — ONE transaction per job on a REPEATABLE READ
    tenant session (see infrastructure.tenancy.tenant_snapshot_session):
    claim (FOR UPDATE SKIP LOCKED) -> snapshot-consistent report read
    (P13 blocker h) -> render CSV + PDF off-loop -> artifact + status
    + audit + outbox. An abort at any point rolls back to zero partial
    state: no artifact, no claim, no audit, no outbox event
    (P13 blocker i). The rendered volume is bounded by the row cap, so
    the claim is held for a bounded time and it locks only the export
    job row — never a domain money row (gate 1.3 note).

  * download_artifact — unguessable random tokens, expiring links,
    requester-only access (the allow-list was resolved from the
    REQUESTER's entitlement, so nobody else may fetch the artifact),
    and an in-transaction audit row per download: exports are the
    exfiltration channel, so the audit IS the control (P13 blocker f).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application import rbac as rbac_service
from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.application.reports import (
    REPORTS,
    ExportFilters,
    ReportCursor,
    ReportDefinition,
    ReportName,
    ReportQuery,
    assert_scope_exists,
    validate_filters,
)
from genesis.domain.documents import Cell, CsvBuilder, PdfBuilder
from genesis.domain.rbac import Action, Module
from genesis.errors import AppError, NotFoundError
from genesis.settings import get_settings

type SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_EXPORT_COLS = (
    "id, report, filters, allowed_columns, status, requested_by, "
    "as_of, completed_at, version, created_at"
)

#: Job claim: oldest pending export, skipping rows a concurrent runner
#: holds (gate 1.4). Served by the partial idx_exports_requested
#: (0013); module-level so the P13 EXPLAIN test captures this exact
#: statement.
CLAIM_SQL = (
    f"SELECT {_EXPORT_COLS} FROM exports "  # noqa: S608 - static columns
    "WHERE tenant_id = CAST(:tid AS uuid) AND status = 'requested' "
    "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
)

#: Download-token resolution: served by the two UNIQUE token indexes
#: (BitmapOr) plus the explicit tenant predicate on top of RLS.
DOWNLOAD_SQL = (
    "SELECT e.id, e.report, e.as_of, e.requested_by, a.row_count, "
    "a.truncated, a.row_limit, a.expires_at, "
    "(a.csv_token = :token) AS is_csv, "
    "CASE WHEN a.csv_token = :token THEN a.csv_content "
    "ELSE a.pdf_content END AS content "
    "FROM export_artifacts a "
    "JOIN exports e ON e.id = a.export_id AND e.tenant_id = a.tenant_id "
    "WHERE a.tenant_id = CAST(:tid AS uuid) "
    "AND (a.csv_token = :token OR a.pdf_token = :token)"
)


# ---------------------------------------------------------------------------
# Server-side export configuration (P13 blocker a). Small accessors so
# tests can pin values without touching the cached Settings object.
# ---------------------------------------------------------------------------


def export_row_cap() -> int:
    return get_settings().export_row_cap


def export_batch_size() -> int:
    return get_settings().export_batch_size


def artifact_ttl_hours() -> int:
    return get_settings().export_artifact_ttl_hours


async def _between_batches() -> None:
    """No-op seam between keyset batches.

    Monkeypatched by the snapshot-consistency test to interleave a
    concurrent committed write mid-export and prove the REPEATABLE
    READ snapshot never observes it (P13 blocker h).
    """


# ---------------------------------------------------------------------------
# run_export — the streaming engine (MASTER_PROMPT 1.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportRun:
    """Outcome metadata ONLY (P13.17d / DSA-4): the run carries no
    rows - every batch is handed to on_batch and released, so worker
    memory no longer scales with a third in-memory copy of the
    export."""

    row_count: int
    truncated: bool
    row_limit: int


async def run_export(
    query: ReportQuery,
    batch_size: int,
    *,
    row_cap: int,
    project: Callable[[tuple[Cell, ...]], tuple[Cell, ...]] | None = None,
    on_batch: Callable[[list[tuple[Cell, ...]]], None] | None = None,
) -> ExportRun:
    """Stream a report in keyset batches under a hard row cap.

    Every fetch asks for one row beyond what it may keep (batch_size
    + 1), so "more rows exist" is always observed, never inferred —
    the truncation flag is exact. on_batch (CPU-bound rendering) runs
    in a worker thread via asyncio.to_thread, keeping the event loop
    responsive while the export renders (gate 1.3). Batches are NOT
    accumulated (DSA-4): on_batch is the only consumer of the cells;
    a batch is released as soon as it has been rendered.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if row_cap < 1:
        raise ValueError("row_cap must be positive")
    row_count = 0
    truncated = False
    cursor: ReportCursor | None = None
    while True:
        want = min(batch_size, row_cap - row_count)
        raw = await query.fetch(cursor, want + 1)
        batch_raw = raw[:want]
        batch_cells = [query.to_cells(item) for item in batch_raw]
        if project is not None:
            batch_cells = [project(cells) for cells in batch_cells]
        row_count += len(batch_cells)
        if on_batch is not None and batch_cells:
            await asyncio.to_thread(on_batch, batch_cells)
        if len(raw) <= want:
            break
        if row_count >= row_cap:
            truncated = True
            break
        cursor = query.cursor_key(batch_raw[-1])
        await _between_batches()
    return ExportRun(row_count=row_count, truncated=truncated, row_limit=row_cap)


# ---------------------------------------------------------------------------
# Export requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportRecord:
    id: uuid.UUID
    report: ReportName
    filters: dict[str, str]
    allowed_columns: tuple[str, ...]
    status: str
    requested_by: uuid.UUID
    as_of: datetime
    completed_at: datetime | None
    version: int
    created_at: datetime


def _row_to_export(row: Any) -> ExportRecord:
    return ExportRecord(
        id=uuid.UUID(str(row[0])),
        report=ReportName(str(row[1])),
        filters=dict(row[2]),
        allowed_columns=tuple(str(key) for key in row[3]),
        status=str(row[4]),
        requested_by=uuid.UUID(str(row[5])),
        as_of=row[6],
        completed_at=row[7],
        version=int(row[8]),
        created_at=row[9],
    )


async def _allowed_columns(
    session: AsyncSession, role_id: uuid.UUID, definition: ReportDefinition
) -> tuple[str, ...]:
    """Column allow-list per role (P13 blocker e): PII needs members:view."""
    include_pii = False
    if any(column.pii for column in definition.columns):
        include_pii = await rbac_service.has_permission(
            session, role_id, Module.MEMBERS, Action.VIEW
        )
    return tuple(column.key for column in definition.columns if include_pii or not column.pii)


async def request_export(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    role_id: uuid.UUID,
    *,
    report: ReportName,
    filters: ExportFilters,
) -> ExportRecord:
    """Persist an export job with a server-resolved, frozen scope.

    The column allow-list and every rendering parameter are fixed here.
    The initial as-of is a queue timestamp for status visibility; the
    worker advances it to the REPEATABLE READ snapshot start before
    reading mutable report rows, so artifact metadata cannot claim an
    earlier cutoff than the rendered loan book.
    """
    definition = REPORTS[report]
    validate_filters(definition, filters)
    await assert_scope_exists(session, tenant_id, report, filters)
    allowed = await _allowed_columns(session, role_id, definition)
    export_id = uuid.uuid4()
    as_of = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO exports "
            "(id, tenant_id, report, filters, allowed_columns, requested_by, as_of) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :report, "
            "CAST(:filters AS jsonb), CAST(:cols AS jsonb), CAST(:actor AS uuid), :as_of)"
        ),
        {
            "id": str(export_id),
            "tid": str(tenant_id),
            "report": report.value,
            "filters": json.dumps(filters.to_json()),
            "cols": json.dumps(list(allowed)),
            "actor": str(actor_id),
            "as_of": as_of,
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="export.request",
        entity="exports",
        entity_id=str(export_id),
        after={
            "report": report.value,
            "filters": filters.to_json(),
            "allowed_columns": list(allowed),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="export.requested",
        payload={"export_id": str(export_id), "report": report.value},
    )
    return await get_export(session, tenant_id, export_id, requester_id=actor_id)


async def get_export(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    export_id: uuid.UUID,
    *,
    requester_id: uuid.UUID,
) -> ExportRecord:
    """Requester-scoped lookup (P13 blocker e): the scope was resolved
    from the requester's entitlement, so nobody else may read the job
    or its tokens. Least disclosure: everyone else sees not-found."""
    row = (
        await session.execute(
            text(
                f"SELECT {_EXPORT_COLS} FROM exports "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND requested_by = CAST(:uid AS uuid)"
            ),
            {"id": str(export_id), "tid": str(tenant_id), "uid": str(requester_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"export {export_id} not found")
    return _row_to_export(row)


@dataclass(frozen=True)
class ArtifactInfo:
    csv_token: str
    pdf_token: str
    row_count: int
    truncated: bool
    row_limit: int
    expires_at: datetime


async def get_artifact_info(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    export_id: uuid.UUID,
) -> ArtifactInfo | None:
    """Artifact metadata for a job the caller already proved they own."""
    row = (
        await session.execute(
            text(
                "SELECT csv_token, pdf_token, row_count, truncated, row_limit, expires_at "
                "FROM export_artifacts "
                "WHERE export_id = CAST(:eid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"eid": str(export_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        return None
    return ArtifactInfo(
        csv_token=str(row[0]),
        pdf_token=str(row[1]),
        row_count=int(row[2]),
        truncated=bool(row[3]),
        row_limit=int(row[4]),
        expires_at=row[5],
    )


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportJobOutcome:
    export_id: uuid.UUID
    report: ReportName
    row_count: int
    truncated: bool


class ExportJobError(AppError):
    """A job failed with a domain error; carries the job id so the
    outer loop can mark it failed in a fresh transaction."""

    def __init__(self, export_id: uuid.UUID, cause: AppError) -> None:
        super().__init__(str(cause))
        self.export_id = export_id
        self.status_code = cause.status_code
        self.category = cause.category


async def run_export_job(session: AsyncSession, tenant_id: uuid.UUID) -> ExportJobOutcome | None:
    """Render the oldest pending export in the caller's transaction.

    The caller supplies a REPEATABLE READ tenant session (P13 blocker
    h) and owns commit/rollback — everything below is atomic: claim,
    read, render, artifact, status, audit, outbox (P13 blocker i).
    Returns None when no job is pending.
    """
    claim = (await session.execute(text(CLAIM_SQL), {"tid": str(tenant_id)})).first()
    if claim is None:
        return None
    record = _row_to_export(claim)
    render_as_of = (await session.execute(text("SELECT now()"))).scalar_one()
    await session.execute(
        text(
            "UPDATE exports SET as_of = :as_of "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"as_of": render_as_of, "id": str(record.id), "tid": str(tenant_id)},
    )
    record = replace(record, as_of=render_as_of)
    definition = REPORTS[record.report]
    allowed = frozenset(record.allowed_columns)
    indexes = tuple(
        index for index, column in enumerate(definition.columns) if column.key in allowed
    )
    headers = tuple(definition.columns[index].title for index in indexes)

    def project(cells: tuple[Cell, ...]) -> tuple[Cell, ...]:
        return tuple(cells[index] for index in indexes)

    csv_builder = CsvBuilder(headers)
    pdf_builder = PdfBuilder(headers)

    def render_batch(batch: list[tuple[Cell, ...]]) -> None:
        """Both renderers fold each batch immediately (DSA-4): the
        engine keeps no copy, so worker memory is the two document
        buffers, never the row set."""
        csv_builder.add_rows(batch)
        pdf_builder.add_rows(batch)

    try:
        query = await definition.build(
            session, tenant_id, ExportFilters.from_json(record.filters), record.as_of
        )
        run = await run_export(
            query,
            export_batch_size(),
            row_cap=export_row_cap(),
            project=project,
            on_batch=render_batch,
        )
    except AppError as exc:
        raise ExportJobError(record.id, exc) from exc

    truncation_note = " | TRUNCATED" if run.truncated else ""
    meta = (
        f"As of {record.as_of.isoformat()} | rows {run.row_count} "
        f"(limit {run.row_limit}){truncation_note}"
    )
    csv_bytes = csv_builder.finish()
    pdf_bytes = await asyncio.to_thread(pdf_builder.finish, title=definition.title, meta=meta)

    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO export_artifacts "
            "(id, tenant_id, export_id, csv_token, pdf_token, csv_content, "
            " pdf_content, row_count, truncated, row_limit, expires_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:eid AS uuid), "
            ":csv_token, :pdf_token, :csv, :pdf, :rows, :truncated, :cap, :expires)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant_id),
            "eid": str(record.id),
            # Unguessable download tokens (P13 blocker e): random,
            # independent of any enumerable id.
            "csv_token": secrets.token_urlsafe(32),
            "pdf_token": secrets.token_urlsafe(32),
            "csv": csv_bytes,
            "pdf": pdf_bytes,
            "rows": run.row_count,
            "truncated": run.truncated,
            "cap": run.row_limit,
            "expires": now + _ttl(),
        },
    )
    completed = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE exports SET status = 'completed', completed_at = :now, "
                "version = version + 1, updated_at = :now "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'requested'"
            ),
            {"now": now, "id": str(record.id), "tid": str(tenant_id)},
        ),
    )
    if completed.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ExportJobError(record.id, NotFoundError(f"export {record.id} moved during render"))
    # The exfiltration audit (P13 blocker f): who exported what scope.
    await record_audit(
        session,
        tenant_id,
        record.requested_by,
        action="export.completed",
        entity="exports",
        entity_id=str(record.id),
        after={
            "report": record.report.value,
            "filters": record.filters,
            "allowed_columns": list(record.allowed_columns),
            "row_count": run.row_count,
            "truncated": run.truncated,
            "row_limit": run.row_limit,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="export.completed",
        payload={
            "export_id": str(record.id),
            "report": record.report.value,
            "row_count": run.row_count,
            "truncated": run.truncated,
        },
    )
    return ExportJobOutcome(
        export_id=record.id,
        report=record.report,
        row_count=run.row_count,
        truncated=run.truncated,
    )


def _ttl() -> timedelta:
    return timedelta(hours=artifact_ttl_hours())


def _is_serialization_failure(exc: DBAPIError) -> bool:
    return bool(getattr(exc.orig, "sqlstate", None) == "40001")


@dataclass(frozen=True)
class ExportCycleSummary:
    completed: int
    failed: int


async def run_pending_exports(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    max_jobs: int = 20,
) -> ExportCycleSummary:
    """Drain the tenant's export queue, one snapshot transaction per job.

    session_scope must yield REPEATABLE READ tenant sessions
    (infrastructure.tenancy.tenant_snapshot_session). A job that fails
    with a domain error is marked failed in a fresh transaction so the
    queue never wedges; a snapshot serialization conflict (concurrent
    runner) ends the cycle quietly — the next cycle retries.
    """
    completed = 0
    failed = 0
    for _ in range(max_jobs):
        try:
            async with session_scope() as session:
                outcome = await run_export_job(session, tenant_id)
        except ExportJobError as exc:
            failed += 1
            async with session_scope() as session:
                await _mark_failed(session, tenant_id, exc)
            continue
        except DBAPIError as exc:
            if _is_serialization_failure(exc):
                break
            raise
        if outcome is None:
            break
        completed += 1
    return ExportCycleSummary(completed=completed, failed=failed)


async def _mark_failed(session: AsyncSession, tenant_id: uuid.UUID, error: ExportJobError) -> None:
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE exports SET status = 'failed', version = version + 1, "
                "updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = 'requested'"
            ),
            {"id": str(error.export_id), "tid": str(tenant_id)},
        ),
    )
    if result.rowcount != 1:
        return  # already resolved by a concurrent runner
    await record_audit(
        session,
        tenant_id,
        None,
        action="export.failed",
        entity="exports",
        entity_id=str(error.export_id),
        # Least disclosure (gate 1.6): the sanitized category only.
        after={"category": error.category.value},
    )


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactDownload:
    export_id: uuid.UUID
    report: ReportName
    as_of: datetime
    is_csv: bool
    content: bytes
    row_count: int
    truncated: bool
    row_limit: int


async def download_artifact(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    requester_id: uuid.UUID,
    token: str,
) -> ArtifactDownload:
    """Resolve a download token; audit the download in-transaction.

    Uniform not-found for a wrong token, a foreign tenant, a foreign
    requester, or an expired link (least disclosure, gate 1.6). The
    token match is served by its UNIQUE index; the tenant predicate
    doubles the RLS fence (P13 blocker c).
    """
    row = (
        await session.execute(text(DOWNLOAD_SQL), {"token": token, "tid": str(tenant_id)})
    ).first()
    if row is None or uuid.UUID(str(row[3])) != requester_id or row[7] <= datetime.now(UTC):
        raise NotFoundError("export artifact not found")
    export_id = uuid.UUID(str(row[0]))
    is_csv = bool(row[8])
    await record_audit(
        session,
        tenant_id,
        requester_id,
        action="export.downloaded",
        entity="exports",
        entity_id=str(export_id),
        after={"report": str(row[1]), "format": "csv" if is_csv else "pdf"},
    )
    return ArtifactDownload(
        export_id=export_id,
        report=ReportName(str(row[1])),
        as_of=row[2],
        is_csv=is_csv,
        content=bytes(row[9]),
        row_count=int(row[4]),
        truncated=bool(row[5]),
        row_limit=int(row[6]),
    )


__all__ = [
    "ArtifactDownload",
    "ArtifactInfo",
    "ExportCycleSummary",
    "ExportJobError",
    "ExportJobOutcome",
    "ExportRecord",
    "ExportRun",
    "download_artifact",
    "get_artifact_info",
    "get_export",
    "request_export",
    "run_export",
    "run_export_job",
    "run_pending_exports",
]
