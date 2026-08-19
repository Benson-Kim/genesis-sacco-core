"""EOD external reconciliation: ledger vs M-Pesa/bank statements
(issue #9, INSTITUTIONAL_GAP_REGISTER G3).

THE control that catches "loans cleared without real payment": every
posting on an EXTERNAL channel (mpesa/bank — the only channels the
teller boundary accepts, 0043) must be evidenced by a statement line
from the rail, and every statement line must be represented in the
ledger. The institution that notices a disagreement second eats the
loss.

Workflow (single-transition status machine, every step audited
in-transaction and announced via the outbox — gates 1.2/1.5):

  1. ``ingest_statement`` — parse happens at the edge; this service
     stages structured lines append-style under an atomic
     duplicate-upload claim (UNIQUE tenant/channel/date/checksum,
     v1.1 rule 5). Re-keying the same statement is a 409; a corrected
     statement (different checksum) is a fresh batch.
  2. ``run_matching`` — deterministic, ONE-SHOT per batch (rerun is a
     409; upload a corrected statement instead — the append-only
     doctrine applied to control evidence):
       * line -> transaction by (channel, external_ref): the 0043
         partial UNIQUE guarantees at most one candidate. Equal
         magnitude -> matched; different -> amount_mismatch break.
         A reference the ledger never saw -> statement_only break.
       * ledger-only sweep: every transaction on the statement's
         channel whose occurred_at falls in the statement's EAT
         business day and that no statement line (of ANY batch) has
         matched -> ledger_only break. Postings WITHOUT an
         external_ref are deliberately swept too: an insider recording
         a repayment with no confirmation code is exactly the
         fabrication this control exists to flag.
  3. ``resolve_break`` — records the evidence linkage ONLY. Money
     corrections post through the EXISTING correction paths
     (application/corrections.py — reversing entries, maker-checker,
     authority bands); a resolution here carries the correction's
     reference, never a money mutation (the ledger doctrine).
  4. ``sign_off_statement`` — four-eyes: a DIFFERENT, non-assurance,
     tenant-vouched principal than the ingester (application/sod.py,
     reuse-first). Open breaks do NOT block sign-off — they age in the
     queue (``list_open_breaks``) until resolved; the sign-off audit
     row records how many were left open.

BUSINESS DAY: statements cover the Kenyan business day. EAT is a fixed
UTC+3 (no DST), so the day window for ``statement_date`` D is
[D 00:00 EAT, D+1 00:00 EAT) = [D-1 21:00 UTC, D 21:00 UTC).

DELIVERY SURFACE (recorded, not hidden): the API router and the EOD
cron wiring are deliberately NOT in this MR — api/app.py is owned by
open MR !3 and the cron-lock seam by !2 (merge-queue #19). This module
is the complete application layer; the thin router/cron follow-up
lands after those merge. Tests drive the service directly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import (
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_created_id_cursor,
)
from genesis.application.sod import require_distinct_non_assurance_checker
from genesis.domain.ledger import Channel
from genesis.domain.money import to_cents
from genesis.errors import ConflictError, InvalidInputError, NotFoundError

#: Cursor scope id (tenant isolation): the breaks aging queue never
#: shares positions with any other register.
BREAKS_SCOPE = "reconciliation.breaks"

#: Kenya has no DST: EAT is a fixed UTC+3 (the security_events
#: constant; kept as a module literal until a shared calendar home
#: exists — recorded, not hidden).
_EAT_OFFSET = timedelta(hours=3)

#: Ingest cap: bounds one batch's memory and transaction size. A rail
#: day larger than this ingests as multiple sources (e.g. paged API
#: pulls), each its own checksum-claimed batch.
MAX_STATEMENT_LINES = 10_000

#: The channels reconciliation covers — the EXTERNAL money rails
#: (0043: the only channels that carry operator-entered external
#: references). accrual/internal are server-job postings with no
#: external counterpart to reconcile against.
EXTERNAL_CHANNELS: frozenset[Channel] = frozenset({Channel.MPESA, Channel.BANK})


def eat_day_window(statement_date: date) -> tuple[datetime, datetime]:
    """[start, end) UTC instants of one EAT business day."""
    start = datetime.combine(statement_date, time.min, tzinfo=UTC) - _EAT_OFFSET
    return start, start + timedelta(days=1)


class StatementStatus(StrEnum):
    INGESTED = "ingested"
    MATCHED = "matched"
    SIGNED_OFF = "signed_off"


_STATEMENT_ALLOWED: dict[StatementStatus, frozenset[StatementStatus]] = {
    StatementStatus.INGESTED: frozenset({StatementStatus.MATCHED}),
    StatementStatus.MATCHED: frozenset({StatementStatus.SIGNED_OFF}),
    StatementStatus.SIGNED_OFF: frozenset(),
}


def statement_transition(current: StatementStatus, target: StatementStatus) -> None:
    """The single gatekeeper for statement status changes (gate 1.4)."""
    if target not in _STATEMENT_ALLOWED[current]:
        raise ConflictError(
            f"reconciliation batch cannot move from '{current.value}' to '{target.value}'"
        )


class MatchStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    STATEMENT_ONLY = "statement_only"


class BreakKind(StrEnum):
    LEDGER_ONLY = "ledger_only"
    STATEMENT_ONLY = "statement_only"
    AMOUNT_MISMATCH = "amount_mismatch"


class BreakStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class StatementLineIn:
    """One structured statement line (parsing happens at the edge).

    ``amount`` is the SIGNED statement figure (credit positive / debit
    negative); matching compares magnitudes against the
    always-positive transactions.amount."""

    line_no: int
    external_ref: str
    amount: Decimal
    occurred_on: date
    narrative: str | None = None


@dataclass(frozen=True)
class StatementRecord:
    id: uuid.UUID
    channel: Channel
    statement_date: date
    source: str
    checksum: str
    line_count: int
    status: StatementStatus
    created_by: uuid.UUID
    signed_off_by: uuid.UUID | None
    signed_off_at: datetime | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class MatchSummary:
    statement_id: uuid.UUID
    matched: int
    amount_mismatch: int
    statement_only: int
    ledger_only: int

    @property
    def clean(self) -> bool:
        return self.amount_mismatch == 0 and self.statement_only == 0 and self.ledger_only == 0


@dataclass(frozen=True)
class BreakRecord:
    id: uuid.UUID
    statement_id: uuid.UUID
    kind: BreakKind
    statement_line_id: uuid.UUID | None
    transaction_id: uuid.UUID | None
    external_ref: str | None
    ledger_amount: Decimal | None
    statement_amount: Decimal | None
    status: BreakStatus
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    resolution_reference: str | None
    resolution_note: str | None
    version: int
    created_at: datetime


@dataclass(frozen=True)
class BreaksPage:
    items: list[BreakRecord]
    next_cursor: str | None


_STATEMENT_COLS = (
    "id, channel, statement_date, source, checksum, line_count, status, "
    "created_by, signed_off_by, signed_off_at, version, created_at"
)

_BREAK_COLS = (
    "id, statement_id, kind, statement_line_id, transaction_id, external_ref, "
    "ledger_amount, statement_amount, status, resolved_by, resolved_at, "
    "resolution_reference, resolution_note, version, created_at"
)


def _row_to_statement(row: Any) -> StatementRecord:
    return StatementRecord(
        id=uuid.UUID(str(row[0])),
        channel=Channel(str(row[1])),
        statement_date=row[2],
        source=str(row[3]),
        checksum=str(row[4]),
        line_count=int(row[5]),
        status=StatementStatus(str(row[6])),
        created_by=uuid.UUID(str(row[7])),
        signed_off_by=uuid.UUID(str(row[8])) if row[8] is not None else None,
        signed_off_at=row[9],
        version=int(row[10]),
        created_at=row[11],
    )


def _row_to_break(row: Any) -> BreakRecord:
    return BreakRecord(
        id=uuid.UUID(str(row[0])),
        statement_id=uuid.UUID(str(row[1])),
        kind=BreakKind(str(row[2])),
        statement_line_id=uuid.UUID(str(row[3])) if row[3] is not None else None,
        transaction_id=uuid.UUID(str(row[4])) if row[4] is not None else None,
        external_ref=str(row[5]) if row[5] is not None else None,
        ledger_amount=Decimal(str(row[6])) if row[6] is not None else None,
        statement_amount=Decimal(str(row[7])) if row[7] is not None else None,
        status=BreakStatus(str(row[8])),
        resolved_by=uuid.UUID(str(row[9])) if row[9] is not None else None,
        resolved_at=row[10],
        resolution_reference=str(row[11]) if row[11] is not None else None,
        resolution_note=str(row[12]) if row[12] is not None else None,
        version=int(row[13]),
        created_at=row[14],
    )


def statement_checksum(channel: Channel, statement_date: date, lines: list[StatementLineIn]) -> str:
    """sha256 of the canonical line set: the duplicate-upload claim key.

    Line ORDER does not change identity (sorted by external_ref);
    every money-bearing field does."""
    canonical = json.dumps(
        [
            [line.external_ref, str(to_cents(line.amount)), line.occurred_on.isoformat()]
            for line in sorted(lines, key=lambda entry: entry.external_ref)
        ]
        + [[channel.value, statement_date.isoformat()]],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _require_external_channel(channel: Channel) -> None:
    if channel not in EXTERNAL_CHANNELS:
        raise InvalidInputError("reconciliation covers the external channels (mpesa, bank) only")


async def ingest_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    channel: Channel,
    statement_date: date,
    source: str,
    lines: list[StatementLineIn],
) -> StatementRecord:
    """Stage one external statement batch under the atomic
    duplicate-upload claim (v1.1 rule 5)."""
    _require_external_channel(channel)
    if not lines:
        raise InvalidInputError("a statement batch requires at least one line")
    if len(lines) > MAX_STATEMENT_LINES:
        raise InvalidInputError(
            f"a statement batch is capped at {MAX_STATEMENT_LINES} lines; "
            "ingest larger days as multiple sources"
        )
    refs = [line.external_ref for line in lines]
    if len(set(refs)) != len(refs):
        raise InvalidInputError(
            "statement lines repeat an external reference; rails do not reuse "
            "confirmation codes within a batch — fix the source file"
        )
    for line in lines:
        if line.line_no < 1:
            raise InvalidInputError("statement line_no must be positive")
        if not 2 <= len(line.external_ref) <= 40:
            raise InvalidInputError("statement external_ref must be 2..40 characters")
        if to_cents(line.amount) == Decimal("0.00"):
            raise InvalidInputError("statement line amount must be non-zero")

    checksum = statement_checksum(channel, statement_date, lines)
    statement_id = uuid.uuid4()
    claimed = (
        await session.execute(
            text(
                "INSERT INTO recon_statements "
                "(id, tenant_id, channel, statement_date, source, checksum, "
                " line_count, status, created_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ch, :day, :src, "
                " :sum, :n, :st, CAST(:actor AS uuid)) "
                "ON CONFLICT (tenant_id, channel, statement_date, checksum) "
                "DO NOTHING RETURNING id"
            ),
            {
                "id": str(statement_id),
                "tid": str(tenant_id),
                "ch": channel.value,
                "day": statement_date,
                "src": source,
                "sum": checksum,
                "n": len(lines),
                "st": StatementStatus.INGESTED.value,
                "actor": str(actor_id),
            },
        )
    ).first()
    if claimed is None:
        raise ConflictError(
            "this statement batch is already ingested for that channel and day "
            "(identical content); a corrected statement ingests as a new batch"
        )
    for line in lines:
        await session.execute(
            text(
                "INSERT INTO recon_statement_lines "
                "(id, tenant_id, statement_id, line_no, external_ref, amount, "
                " occurred_on, narrative) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:sid AS uuid), "
                " :no, :ref, :amount, :day, :narrative)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "sid": str(statement_id),
                "no": line.line_no,
                "ref": line.external_ref,
                "amount": str(to_cents(line.amount)),
                "day": line.occurred_on,
                "narrative": line.narrative,
            },
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recon.statement_ingested",
        entity="recon_statements",
        entity_id=str(statement_id),
        after={
            "channel": channel.value,
            "statement_date": statement_date.isoformat(),
            "source": source,
            "checksum": checksum,
            "line_count": len(lines),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recon.statement_ingested",
        payload={
            "statement_id": str(statement_id),
            "channel": channel.value,
            "statement_date": statement_date.isoformat(),
            "line_count": len(lines),
        },
    )
    return await get_statement(session, tenant_id, statement_id)


async def get_statement(
    session: AsyncSession, tenant_id: uuid.UUID, statement_id: uuid.UUID
) -> StatementRecord:
    row = (
        await session.execute(
            text(
                f"SELECT {_STATEMENT_COLS} FROM recon_statements "  # noqa: S608 -- code-owned column list constant
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(statement_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"reconciliation batch {statement_id} not found")
    return _row_to_statement(row)


async def run_matching(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    statement_id: uuid.UUID,
) -> MatchSummary:
    """The deterministic matching pass — ONE-SHOT per batch.

    Locks the batch FOR UPDATE (serialises against a concurrent run
    and against sign-off), classifies every line, then sweeps the
    ledger for the statement day's unmatched external postings. The
    whole pass is one transaction: a batch is never half-matched.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_STATEMENT_COLS} FROM recon_statements "  # noqa: S608 -- code-owned column list constant
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(statement_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"reconciliation batch {statement_id} not found")
    statement = _row_to_statement(row)
    statement_transition(statement.status, StatementStatus.MATCHED)

    lines = (
        await session.execute(
            text(
                "SELECT id, external_ref, amount FROM recon_statement_lines "
                "WHERE statement_id = CAST(:sid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) ORDER BY line_no"
            ),
            {"sid": str(statement_id), "tid": str(tenant_id)},
        )
    ).all()

    matched = 0
    mismatched = 0
    statement_only = 0
    for line_row in lines:
        line_id = uuid.UUID(str(line_row[0]))
        ref = str(line_row[1])
        stmt_amount = Decimal(str(line_row[2]))
        txn = (
            await session.execute(
                text(
                    # <= 1 row by the 0043 partial UNIQUE (tenant_id,
                    # channel, external_ref).
                    "SELECT id, amount, txn_ref FROM transactions "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND channel = :ch "
                    "AND external_ref = :ref"
                ),
                {"tid": str(tenant_id), "ch": statement.channel.value, "ref": ref},
            )
        ).first()
        if txn is None:
            statement_only += 1
            await _set_line_status(session, tenant_id, line_id, MatchStatus.STATEMENT_ONLY, None)
            await _insert_break(
                session,
                tenant_id,
                statement_id,
                kind=BreakKind.STATEMENT_ONLY,
                statement_line_id=line_id,
                transaction_id=None,
                external_ref=ref,
                ledger_amount=None,
                statement_amount=stmt_amount,
            )
            continue
        txn_id = uuid.UUID(str(txn[0]))
        ledger_amount = Decimal(str(txn[1]))
        if ledger_amount == abs(stmt_amount):
            matched += 1
            await _set_line_status(session, tenant_id, line_id, MatchStatus.MATCHED, txn_id)
        else:
            mismatched += 1
            await _set_line_status(session, tenant_id, line_id, MatchStatus.AMOUNT_MISMATCH, txn_id)
            await _insert_break(
                session,
                tenant_id,
                statement_id,
                kind=BreakKind.AMOUNT_MISMATCH,
                statement_line_id=line_id,
                transaction_id=txn_id,
                external_ref=ref,
                ledger_amount=ledger_amount,
                statement_amount=stmt_amount,
            )

    # The ledger-only sweep: every external posting of the statement's
    # channel and EAT business day that NO statement line (of any
    # batch) has matched. NULL external_refs are swept deliberately —
    # a posting without a confirmation code can never be evidenced by
    # the rail, which is exactly the point.
    day_start, day_end = eat_day_window(statement.statement_date)
    ledger_only_rows = (
        await session.execute(
            text(
                "SELECT t.id, t.external_ref, t.amount FROM transactions t "
                "WHERE t.tenant_id = CAST(:tid AS uuid) AND t.channel = :ch "
                "AND t.occurred_at >= :day_start AND t.occurred_at < :day_end "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM recon_statement_lines l "
                "  WHERE l.tenant_id = CAST(:tid AS uuid) "
                "  AND l.matched_transaction_id = t.id) "
                "ORDER BY t.occurred_at, t.id"
            ),
            {
                "tid": str(tenant_id),
                "ch": statement.channel.value,
                "day_start": day_start,
                "day_end": day_end,
            },
        )
    ).all()
    ledger_only = 0
    for txn_row in ledger_only_rows:
        inserted = await _insert_break(
            session,
            tenant_id,
            statement_id,
            kind=BreakKind.LEDGER_ONLY,
            statement_line_id=None,
            transaction_id=uuid.UUID(str(txn_row[0])),
            external_ref=str(txn_row[1]) if txn_row[1] is not None else None,
            ledger_amount=Decimal(str(txn_row[2])),
            statement_amount=None,
        )
        ledger_only += int(inserted)

    updated = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE recon_statements SET status = :st, version = version + 1, "
                "updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "st": StatementStatus.MATCHED.value,
                "id": str(statement_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if updated.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"reconciliation batch {statement_id} vanished mid-run")

    summary = MatchSummary(
        statement_id=statement_id,
        matched=matched,
        amount_mismatch=mismatched,
        statement_only=statement_only,
        ledger_only=ledger_only,
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recon.matched",
        entity="recon_statements",
        entity_id=str(statement_id),
        before={"status": StatementStatus.INGESTED.value},
        after={
            "status": StatementStatus.MATCHED.value,
            "matched": summary.matched,
            "amount_mismatch": summary.amount_mismatch,
            "statement_only": summary.statement_only,
            "ledger_only": summary.ledger_only,
        },
    )
    # The daily recon report per channel (issue #9): the outbox event
    # IS the alert feed — a non-clean day pages a human.
    await enqueue_event(
        session,
        tenant_id,
        event_type="recon.completed",
        payload={
            "statement_id": str(statement_id),
            "channel": statement.channel.value,
            "statement_date": statement.statement_date.isoformat(),
            "matched": summary.matched,
            "amount_mismatch": summary.amount_mismatch,
            "statement_only": summary.statement_only,
            "ledger_only": summary.ledger_only,
            "clean": summary.clean,
        },
    )
    return summary


async def _set_line_status(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    line_id: uuid.UUID,
    status: MatchStatus,
    txn_id: uuid.UUID | None,
) -> None:
    await session.execute(
        text(
            "UPDATE recon_statement_lines SET match_status = :st, "
            "matched_transaction_id = CAST(:txn AS uuid) "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "st": status.value,
            "txn": str(txn_id) if txn_id else None,
            "id": str(line_id),
            "tid": str(tenant_id),
        },
    )


async def _insert_break(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    statement_id: uuid.UUID,
    *,
    kind: BreakKind,
    statement_line_id: uuid.UUID | None,
    transaction_id: uuid.UUID | None,
    external_ref: str | None,
    ledger_amount: Decimal | None,
    statement_amount: Decimal | None,
) -> bool:
    """INSERT one break; the ledger-only partial UNIQUE collapses a
    posting already flagged by another batch (returns False)."""
    inserted = (
        await session.execute(
            text(
                "INSERT INTO recon_breaks "
                "(id, tenant_id, statement_id, kind, statement_line_id, "
                " transaction_id, external_ref, ledger_amount, statement_amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:sid AS uuid), "
                " :kind, CAST(:line AS uuid), CAST(:txn AS uuid), :ref, "
                " :ledger_amount, :statement_amount) "
                "ON CONFLICT DO NOTHING RETURNING id"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tenant_id),
                "sid": str(statement_id),
                "kind": kind.value,
                "line": str(statement_line_id) if statement_line_id else None,
                "txn": str(transaction_id) if transaction_id else None,
                "ref": external_ref,
                "ledger_amount": str(ledger_amount) if ledger_amount is not None else None,
                "statement_amount": (
                    str(statement_amount) if statement_amount is not None else None
                ),
            },
        )
    ).first()
    return inserted is not None


async def resolve_break(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    break_id: uuid.UUID,
    *,
    version: int,
    resolution_reference: str,
    resolution_note: str,
) -> BreakRecord:
    """Close one break with its evidence linkage (optimistic-locked).

    NEVER a money mutation: the reference cites the correction posted
    through the existing correction paths (an RV-/FE-/RC- txn_ref) or
    the external evidence (a rail-side reversal id). The DB CHECKs
    refuse a resolution without resolver/timestamp/reference/note.
    """
    reference = resolution_reference.strip()
    note = resolution_note.strip()
    if not reference or len(reference) > 60:
        raise InvalidInputError("a break resolution requires a reference (1..60 chars)")
    if not note or len(note) > 500:
        raise InvalidInputError("a break resolution requires a note (1..500 chars)")
    row = (
        await session.execute(
            text(
                f"SELECT {_BREAK_COLS} FROM recon_breaks "  # noqa: S608 -- code-owned column list constant
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(break_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"reconciliation break {break_id} not found")
    record = _row_to_break(row)
    if record.status is not BreakStatus.OPEN:
        raise ConflictError(f"reconciliation break {break_id} is already resolved")
    if record.version != version:
        raise ConflictError(
            f"reconciliation break {break_id} changed (version {record.version}); reload"
        )
    await session.execute(
        text(
            "UPDATE recon_breaks SET status = :st, resolved_by = CAST(:actor AS uuid), "
            "resolved_at = now(), resolution_reference = :ref, resolution_note = :note, "
            "version = version + 1, updated_at = now() "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "st": BreakStatus.RESOLVED.value,
            "actor": str(actor_id),
            "ref": reference,
            "note": note,
            "id": str(break_id),
            "tid": str(tenant_id),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recon.break_resolved",
        entity="recon_breaks",
        entity_id=str(break_id),
        before={"status": BreakStatus.OPEN.value, "kind": record.kind.value},
        after={
            "status": BreakStatus.RESOLVED.value,
            "resolution_reference": reference,
            "resolution_note": note,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recon.break_resolved",
        payload={"break_id": str(break_id), "kind": record.kind.value},
    )
    return await get_break(session, tenant_id, break_id)


async def get_break(
    session: AsyncSession, tenant_id: uuid.UUID, break_id: uuid.UUID
) -> BreakRecord:
    row = (
        await session.execute(
            text(
                f"SELECT {_BREAK_COLS} FROM recon_breaks "  # noqa: S608 -- code-owned column list constant
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(break_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"reconciliation break {break_id} not found")
    return _row_to_break(row)


async def sign_off_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    statement_id: uuid.UUID,
    *,
    version: int,
) -> StatementRecord:
    """Four-eyes daily sign-off: a DIFFERENT, non-assurance principal
    than the ingester attests the day was reviewed. Open breaks do not
    block — they age in the queue; their count goes on the record."""
    row = (
        await session.execute(
            text(
                f"SELECT {_STATEMENT_COLS} FROM recon_statements "  # noqa: S608 -- code-owned column list constant
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(statement_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"reconciliation batch {statement_id} not found")
    statement = _row_to_statement(row)
    statement_transition(statement.status, StatementStatus.SIGNED_OFF)
    if statement.version != version:
        raise ConflictError(
            f"reconciliation batch {statement_id} changed (version {statement.version}); reload"
        )
    await require_distinct_non_assurance_checker(
        session,
        tenant_id,
        actor_id,
        statement.created_by,
        subject="a reconciliation batch",
        subject_plural="reconciliation batches",
    )
    open_breaks = (
        await session.execute(
            text(
                "SELECT count(*) FROM recon_breaks "
                "WHERE statement_id = CAST(:sid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND status = 'open'"
            ),
            {"sid": str(statement_id), "tid": str(tenant_id)},
        )
    ).scalar_one()
    await session.execute(
        text(
            "UPDATE recon_statements SET status = :st, "
            "signed_off_by = CAST(:actor AS uuid), signed_off_at = now(), "
            "version = version + 1, updated_at = now() "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {
            "st": StatementStatus.SIGNED_OFF.value,
            "actor": str(actor_id),
            "id": str(statement_id),
            "tid": str(tenant_id),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="recon.signed_off",
        entity="recon_statements",
        entity_id=str(statement_id),
        before={"status": StatementStatus.MATCHED.value},
        after={
            "status": StatementStatus.SIGNED_OFF.value,
            "open_breaks_at_sign_off": int(open_breaks),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="recon.signed_off",
        payload={
            "statement_id": str(statement_id),
            "open_breaks_at_sign_off": int(open_breaks),
        },
    )
    return await get_statement(session, tenant_id, statement_id)


async def list_open_breaks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 50,
) -> BreaksPage:
    """The aging queue: OPEN breaks, oldest first (keyset — gate 1.3;
    served by idx_recon_breaks_queue). limit is capped by the API
    contract at 100 like every other register."""
    limit = max(1, min(int(limit), 100))
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    keyset = ""
    if cursor is not None:
        inner = decode_cursor(cursor, tenant_id=tenant_id, endpoint=BREAKS_SCOPE, entity="break")
        after_at, after_id = parse_created_id_cursor(inner, entity="break")
        keyset = "AND (created_at, id) > (:after_at, CAST(:after_id AS uuid)) "
        params["after_at"] = after_at
        params["after_id"] = after_id
    rows = (
        await session.execute(
            text(
                f"SELECT {_BREAK_COLS} FROM recon_breaks "  # noqa: S608 -- code-owned column list constant + code-owned keyset clause
                "WHERE tenant_id = CAST(:tid AS uuid) AND status = 'open' "
                f"{keyset}"
                "ORDER BY created_at, id LIMIT :limit"
            ),
            params,
        )
    ).all()
    items = [_row_to_break(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_created_id_cursor(last.created_at, last.id),
            tenant_id=tenant_id,
            endpoint=BREAKS_SCOPE,
        )
    return BreaksPage(items=items, next_cursor=next_cursor)
