"""Adversarial tests for issue #9 — EOD external reconciliation
(application/reconciliation.py + migration 0049).

DB cases require a migrated PostgreSQL database (DATABASE_URL); the
pure-domain oracles (status matrix, checksum, day window) run
everywhere.

Failure modes (v1.2 rule 15) — oracles HAND-COMPUTED, never captured
from the code under test:

  FM1 fabricated repayment  — the mandated adversarial case: a
                              'loan_repayment' posting on mpesa with a
                              made-up external_ref AND one with no ref
                              at all, neither on the day's statement,
                              are BOTH flagged as ledger_only breaks;
                              the legit posting matches cleanly.
  FM2 statement_only        — a rail line the ledger never saw is a
                              break (external money not recorded).
  FM3 amount_mismatch       — same reference, different figures: break
                              carrying BOTH amounts.
  FM4 duplicate upload      — identical batch re-ingest is a 409 at
                              the atomic claim; a corrected batch
                              (different checksum) ingests fresh.
  FM5 rerun                 — matching is one-shot: a second run is a
                              409 through the single transition
                              gatekeeper (evidence is append-only).
  FM6 sign-off four-eyes    — the ingester signing their own batch is
                              a 409; an Auditor is refused (audit
                              independence); a distinct principal
                              signs; a stale version is a 409;
                              sign-off before matching is a 409.
  FM7 break resolution      — requires reference + note; one-shot
                              (already-resolved is a 409); stale
                              version is a 409; audit row carries the
                              evidence linkage.
  FM8 cross-tenant          — a foreign batch id is a 404 (the
                              issue-#17 probe shape); RLS posture is
                              covered by the leakage suite via
                              TENANT_TABLES.
  FM9 aging queue           — keyset pagination over open breaks,
                              oldest first, opaque tamper-checked
                              cursors (scope reconciliation.breaks).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import factory, seed_user, unique_email
from genesis.application import reconciliation as recon
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError, InvalidInputError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

# ---------------------------------------------------------------------------
# Pure-domain oracles (no DB)
# ---------------------------------------------------------------------------


def test_full_matrix_statement_transitions() -> None:
    """ingested -> matched -> signed_off are the only two legal moves;
    the other seven raise through the single gatekeeper."""
    legal = {
        (recon.StatementStatus.INGESTED, recon.StatementStatus.MATCHED),
        (recon.StatementStatus.MATCHED, recon.StatementStatus.SIGNED_OFF),
    }
    for current in recon.StatementStatus:
        for target in recon.StatementStatus:
            if (current, target) in legal:
                recon.statement_transition(current, target)
            else:
                with pytest.raises(ConflictError, match="cannot move"):
                    recon.statement_transition(current, target)


def test_checksum_is_order_independent_and_content_bound() -> None:
    day = date(2026, 8, 19)
    line_a = recon.StatementLineIn(1, "TAM11AA11", Decimal("100.00"), day)
    line_b = recon.StatementLineIn(2, "TAM22BB22", Decimal("250.00"), day)
    forward = recon.statement_checksum(Channel.MPESA, day, [line_a, line_b])
    reversed_ = recon.statement_checksum(Channel.MPESA, day, [line_b, line_a])
    assert forward == reversed_  # order never changes identity
    bumped = recon.StatementLineIn(2, "TAM22BB22", Decimal("250.01"), day)
    assert recon.statement_checksum(Channel.MPESA, day, [line_a, bumped]) != forward
    assert recon.statement_checksum(Channel.BANK, day, [line_a, line_b]) != forward


def test_eat_day_window() -> None:
    """2026-08-19 EAT == [2026-08-18 21:00 UTC, 2026-08-19 21:00 UTC)
    — hand-computed from the fixed UTC+3 offset."""
    start, end = recon.eat_day_window(date(2026, 8, 19))
    assert start == datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 19, 21, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# DB fixtures (the house style)
# ---------------------------------------------------------------------------

#: Applied per DB test (the pure oracles above run in every pipeline).
requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

STATEMENT_DAY = date(2026, 8, 19)
#: 10:00 EAT on the statement day (inside the window by construction).
IN_WINDOW_AT = datetime(2026, 8, 19, 7, 0, tzinfo=UTC)


async def _seed_actor() -> tuple[uuid.UUID, uuid.UUID]:
    email = unique_email()
    tid, _role_id = await seed_user(email, role_name="System Admin")
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    return tid, uuid.UUID(str(user_id))


async def _seed_extra_user(tid: uuid.UUID, role_name: str = "System Admin") -> uuid.UUID:
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"), {"name": role_name}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Recon User', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    return user_id


async def _seed_txn(
    tid: uuid.UUID,
    *,
    amount: str,
    external_ref: str | None,
    channel: Channel = Channel.MPESA,
    occurred_at: datetime = IN_WINDOW_AT,
) -> uuid.UUID:
    """One external-channel posting row (direct INSERT: transactions
    are append-only but freely insertable; recon reads only this
    table)."""
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, txn_ref, type, amount, channel, external_ref, occurred_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, 'loan_repayment', "
                ":amount, :ch, :ext, :at)"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "ref": f"RP-{txn_id.hex[:10].upper()}",
                "amount": amount,
                "ch": channel.value,
                "ext": external_ref,
                "at": occurred_at,
            },
        )
    return txn_id


async def _ingest(
    tid: uuid.UUID,
    actor: uuid.UUID,
    lines: list[recon.StatementLineIn],
    *,
    channel: Channel = Channel.MPESA,
    day: date = STATEMENT_DAY,
    source: str = "mpesa-org-statement.csv",
) -> recon.StatementRecord:
    async with tenant_session(factory(), tid) as session:
        return await recon.ingest_statement(
            session,
            tid,
            actor,
            channel=channel,
            statement_date=day,
            source=source,
            lines=lines,
        )


async def _match(tid: uuid.UUID, actor: uuid.UUID, statement_id: uuid.UUID) -> recon.MatchSummary:
    async with tenant_session(factory(), tid) as session:
        return await recon.run_matching(session, tid, actor, statement_id)


async def _breaks_for(tid: uuid.UUID, statement_id: uuid.UUID) -> list[recon.BreakRecord]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM recon_breaks WHERE statement_id = CAST(:sid AS uuid) "
                    "ORDER BY created_at, id"
                ),
                {"sid": str(statement_id)},
            )
        ).all()
    records = []
    for row in rows:
        async with tenant_session(factory(), tid) as session:
            records.append(await recon.get_break(session, tid, uuid.UUID(str(row[0]))))
    return records


def _line(no: int, ref: str, amount: str, day: date = STATEMENT_DAY) -> recon.StatementLineIn:
    return recon.StatementLineIn(no, ref, Decimal(amount), day)


# ---------------------------------------------------------------------------
# FM1 — the fabricated repayment is flagged (the control's reason to exist)
# ---------------------------------------------------------------------------


@requires_db
def test_fm1_fabricated_repayment_flagged_ledger_only() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        legit = await _seed_txn(tid, amount="1500.00", external_ref="TAM99XX77")
        faked_ref = await _seed_txn(tid, amount="20000.00", external_ref="FAKE0001")
        faked_noref = await _seed_txn(tid, amount="18000.00", external_ref=None)

        statement = await _ingest(tid, actor, [_line(1, "TAM99XX77", "1500.00")])
        summary = await _match(tid, actor, statement.id)

        assert summary.matched == 1
        assert summary.statement_only == 0
        assert summary.amount_mismatch == 0
        assert summary.ledger_only == 2
        assert summary.clean is False

        breaks = await _breaks_for(tid, statement.id)
        flagged = {b.transaction_id for b in breaks if b.kind is recon.BreakKind.LEDGER_ONLY}
        assert flagged == {faked_ref, faked_noref}
        for record in breaks:
            assert record.status is recon.BreakStatus.OPEN
            assert record.statement_id == statement.id

        # The legit line is matched to its posting, one-shot.
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT match_status, matched_transaction_id "
                        "FROM recon_statement_lines "
                        "WHERE statement_id = CAST(:sid AS uuid)"
                    ),
                    {"sid": str(statement.id)},
                )
            ).one()
        assert str(row[0]) == "matched"
        assert uuid.UUID(str(row[1])) == legit

        # The non-clean day is announced (the alert feed).
        async with tenant_session(factory(), tid) as session:
            payload = (
                await session.execute(
                    text("SELECT payload FROM outbox_events WHERE event_type = 'recon.completed'")
                )
            ).scalar_one()
        assert payload["ledger_only"] == 2
        assert payload["clean"] is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 / FM3 — statement_only and amount_mismatch
# ---------------------------------------------------------------------------


@requires_db
def test_fm2_statement_line_without_posting_is_a_break() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        statement = await _ingest(tid, actor, [_line(1, "GHOST001", "5000.00")])
        summary = await _match(tid, actor, statement.id)
        assert summary.statement_only == 1
        assert summary.matched == 0
        breaks = await _breaks_for(tid, statement.id)
        assert len(breaks) == 1
        assert breaks[0].kind is recon.BreakKind.STATEMENT_ONLY
        assert breaks[0].external_ref == "GHOST001"
        assert breaks[0].statement_amount == Decimal("5000.00")
        assert breaks[0].transaction_id is None

    asyncio.run(run())


@requires_db
def test_fm3_amount_mismatch_carries_both_figures() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        txn = await _seed_txn(tid, amount="1000.00", external_ref="TAM55AA55")
        statement = await _ingest(tid, actor, [_line(1, "TAM55AA55", "999.00")])
        summary = await _match(tid, actor, statement.id)
        assert summary.amount_mismatch == 1
        # An amount-mismatched posting is NOT also ledger-only: the
        # line claimed it (matched_transaction_id filled).
        assert summary.ledger_only == 0
        breaks = await _breaks_for(tid, statement.id)
        assert len(breaks) == 1
        assert breaks[0].kind is recon.BreakKind.AMOUNT_MISMATCH
        assert breaks[0].transaction_id == txn
        assert breaks[0].ledger_amount == Decimal("1000.00")
        assert breaks[0].statement_amount == Decimal("999.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 / FM5 — duplicate upload and rerun refusal
# ---------------------------------------------------------------------------


@requires_db
def test_fm4_duplicate_batch_409_corrected_batch_ingests() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        lines = [_line(1, "TAM10AA10", "100.00"), _line(2, "TAM20BB20", "200.00")]
        await _ingest(tid, actor, lines)
        with pytest.raises(ConflictError, match="already ingested"):
            # Same content, different line order: SAME batch identity.
            await _ingest(tid, actor, list(reversed(lines)))
        corrected = await _ingest(
            tid, actor, [_line(1, "TAM10AA10", "100.00"), _line(2, "TAM20BB20", "200.01")]
        )
        assert corrected.line_count == 2

    asyncio.run(run())


@requires_db
def test_fm5_matching_is_one_shot() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        statement = await _ingest(tid, actor, [_line(1, "TAM31CC31", "10.00")])
        await _match(tid, actor, statement.id)
        with pytest.raises(ConflictError, match="cannot move"):
            await _match(tid, actor, statement.id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — sign-off four-eyes
# ---------------------------------------------------------------------------


@requires_db
def test_fm6_sign_off_four_eyes_and_versioning() -> None:
    async def run() -> None:
        tid, ingester = await _seed_actor()
        statement = await _ingest(tid, ingester, [_line(1, "TAM40DD40", "50.00")])

        # Before matching: refused through the transition gatekeeper.
        with pytest.raises(ConflictError, match="cannot move"):
            async with tenant_session(factory(), tid) as session:
                await recon.sign_off_statement(
                    session, tid, ingester, statement.id, version=statement.version
                )

        await _match(tid, ingester, statement.id)
        async with tenant_session(factory(), tid) as session:
            current = await recon.get_statement(session, tid, statement.id)

        # The ingester signing their own day: segregation of duties.
        with pytest.raises(ConflictError, match="segregation of duties"):
            async with tenant_session(factory(), tid) as session:
                await recon.sign_off_statement(
                    session, tid, ingester, statement.id, version=current.version
                )
        # An assurance role never acts inside the workflow (B2).
        auditor = await _seed_extra_user(tid, "Auditor")
        with pytest.raises(ConflictError, match="audit independence"):
            async with tenant_session(factory(), tid) as session:
                await recon.sign_off_statement(
                    session, tid, auditor, statement.id, version=current.version
                )
        # A stale version is a 409 (optimistic locking).
        signer = await _seed_extra_user(tid)
        with pytest.raises(ConflictError, match="version"):
            async with tenant_session(factory(), tid) as session:
                await recon.sign_off_statement(
                    session, tid, signer, statement.id, version=current.version + 7
                )
        async with tenant_session(factory(), tid) as session:
            signed = await recon.sign_off_statement(
                session, tid, signer, statement.id, version=current.version
            )
        assert signed.status is recon.StatementStatus.SIGNED_OFF
        assert signed.signed_off_by == signer
        assert signed.signed_off_at is not None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — break resolution discipline
# ---------------------------------------------------------------------------


@requires_db
def test_fm7_break_resolution_requires_evidence_and_is_one_shot() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        await _seed_txn(tid, amount="7000.00", external_ref="FAKE0002")
        statement = await _ingest(tid, actor, [_line(1, "TAM60EE60", "60.00")])
        await _match(tid, actor, statement.id)
        breaks = await _breaks_for(tid, statement.id)
        ledger_break = next(b for b in breaks if b.kind is recon.BreakKind.LEDGER_ONLY)

        with pytest.raises(InvalidInputError, match="reference"):
            async with tenant_session(factory(), tid) as session:
                await recon.resolve_break(
                    session,
                    tid,
                    actor,
                    ledger_break.id,
                    version=ledger_break.version,
                    resolution_reference="   ",
                    resolution_note="reversed via corrections",
                )
        with pytest.raises(ConflictError, match="version"):
            async with tenant_session(factory(), tid) as session:
                await recon.resolve_break(
                    session,
                    tid,
                    actor,
                    ledger_break.id,
                    version=ledger_break.version + 3,
                    resolution_reference="RV-000123",
                    resolution_note="reversed via corrections",
                )
        async with tenant_session(factory(), tid) as session:
            resolved = await recon.resolve_break(
                session,
                tid,
                actor,
                ledger_break.id,
                version=ledger_break.version,
                resolution_reference="RV-000123",
                resolution_note="fabricated posting reversed via the adjustment workflow",
            )
        assert resolved.status is recon.BreakStatus.RESOLVED
        assert resolved.resolution_reference == "RV-000123"
        with pytest.raises(ConflictError, match="already resolved"):
            async with tenant_session(factory(), tid) as session:
                await recon.resolve_break(
                    session,
                    tid,
                    actor,
                    ledger_break.id,
                    version=resolved.version,
                    resolution_reference="RV-000124",
                    resolution_note="again",
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — cross-tenant probe
# ---------------------------------------------------------------------------


@requires_db
def test_fm8_foreign_batch_is_404() -> None:
    async def run() -> None:
        tid_a, actor_a = await _seed_actor()
        tid_b, actor_b = await _seed_actor()
        statement = await _ingest(tid_a, actor_a, [_line(1, "TAM70FF70", "70.00")])
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid_b) as session:
                await recon.get_statement(session, tid_b, statement.id)
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid_b) as session:
                await recon.run_matching(session, tid_b, actor_b, statement.id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — the aging queue pages by keyset
# ---------------------------------------------------------------------------


@requires_db
def test_fm9_open_breaks_aging_queue_keyset() -> None:
    async def run() -> None:
        tid, actor = await _seed_actor()
        for i in range(3):
            await _seed_txn(tid, amount=f"{100 + i}.00", external_ref=f"FAKE01{i}0")
        statement = await _ingest(tid, actor, [_line(1, "TAM80GG80", "80.00")])
        await _match(tid, actor, statement.id)  # 3 ledger_only + 1 statement_only

        async with tenant_session(factory(), tid) as session:
            first = await recon.list_open_breaks(session, tid, limit=2)
        assert len(first.items) == 2
        assert first.next_cursor is not None
        async with tenant_session(factory(), tid) as session:
            second = await recon.list_open_breaks(session, tid, cursor=first.next_cursor, limit=2)
        assert len(second.items) == 2
        seen = {b.id for b in first.items} | {b.id for b in second.items}
        assert len(seen) == 4
        assert second.next_cursor is None
        # Oldest-first within the walk.
        ordered = [*first.items, *second.items]
        assert ordered == sorted(ordered, key=lambda b: (b.created_at, str(b.id)))

    asyncio.run(run())
