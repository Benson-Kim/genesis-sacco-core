"""Integration tests for issue #21: bad-debt recovery receipts.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Write-off is NOT forgiveness (P13.15 A4): the 0025 write-once snapshot
persists the claim; this suite proves the issue-#21 recovery branch
against it. Failure modes (v1.2 rule 15) — every oracle HAND-COMPUTED
in comments, never captured from the code under test; every guard test
fails with its guard removed (MASTER_PROMPT §4):

  FM1 premature recovery   — receipts only against a POSTED write-off
                             (service 409 + the 0030 trigger backstop).
  FM2 over-recovery        — cumulative receipts capped at the
                             write-once total_written_off (service 409
                             under the write-off row lock + the 0030
                             constraint-trigger backstop at the SQL
                             level).
  FM3 silent resurrection  — the loan stays written_off with a zero
                             balance after ANY receipt; repayments stay
                             refused; WRITTEN_OFF stays terminal in the
                             status map.
  FM4 claim walks out      — a member with an unresolved POSTED claim
                             is refused exit; full recovery unblocks.
  FM5 guarantee disposition— guarantees behind the written-off loan
                             stay UNTOUCHED on partial receipts and are
                             released exactly by the full-recovery
                             receipt (the documented issue-#21 policy).
  FM6 append-only /        — UPDATE/DELETE of a receipt row raise at
      conservation           the SQL level (0004/0014 discipline); the
                             RC- posting balances; the recovered total
                             reconstructs from the append-only rows.
  FM7 partial receipt      — kill-switch mid-receipt: zero postings,
                             zero receipt rows, zero audit, guarantees
                             untouched.
  FM8 lost P13.16 linkage  — the receipt records the loan's
                             closed_written_off recovery case
                             explicitly; no case records NULL.
  FM9 authz / tenancy /    — 7-role matrix on both new routes;
      idempotency            issue-#17 cross-tenant probes; replay =
                             one receipt by side-effect counts and a
                             cross-actor replay MISSES (the !29 rule).

Claim fixture (hand-computed): the P10 golden loan 24,000.00 is
written off with a seeded penalty_due of 150.00 -> total_written_off
= 24,000.00 + 150.00 = 24,150.00. Receipts: 10,000.00 leaves an
outstanding claim of 24,150.00 - 10,000.00 = 14,150.00; a second
receipt of 14,150.00 recovers the claim IN FULL (outstanding 0.00).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from functools import partial

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import corrections as corrections_service
from genesis.application import loans as loans_service
from genesis.application import member_exits as exits_service
from genesis.application import recovery as recovery_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import disburse_loan
from genesis.application.pagination import encode_cursor
from genesis.application.rbac import seed_permissions
from genesis.application.recovery import run_recovery_close_pass
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.domain.lending import InvalidTransitionError, LoanStatus, loan_transition
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.domain.recovery import RecoveryCaseStatus
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures (the test_corrections.py house style)
# ---------------------------------------------------------------------------


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
    """(tenant_id, user_id, token) with the full permission matrix seeded."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    token = issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )
    return tid, uuid.UUID(str(user_id)), token


async def _seed_extra_user(tid: uuid.UUID, role_name: str) -> tuple[uuid.UUID, str]:
    """Another active user in the SAME tenant; returns (user_id, token)."""
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
                "'Extra User', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    token = issue_access_token(
        AuthContext(user_id=user_id, tenant_id=tid, role_id=uuid.UUID(str(role_id)))
    )
    return user_id, token


def _headers(token: str, idem: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if idem:
        headers["idempotency-key"] = idem
    return headers


async def _seed_member(tid: uuid.UUID) -> uuid.UUID:
    """A member with deposit AND share accounts (the exit path locks
    both), deposits covering the P12.5 multiplier gate."""
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                "'Recovery Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '100000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
        await session.execute(
            text(
                "INSERT INTO share_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '5000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
    return mid


async def _disburse(
    tid: uuid.UUID,
    amount: Decimal = Decimal("24000.00"),
    *,
    member_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Disburse the P10 golden-case loan; returns (loan_id, member_id)."""
    mid = member_id or await _seed_member(tid)
    pid = uuid.uuid4()
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Product-{pid.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, stage) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, 12.00, 'approved')"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(pid),
                "amount": str(amount),
            },
        )
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id, mid


async def _seed_penalty(tid: uuid.UUID, loan_id: uuid.UUID, amount: str) -> None:
    """Fixture stand-in for the P13.8 accrual (receivable-side only)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE loans SET penalty_due = :p WHERE id = CAST(:lid AS uuid)"),
            {"p": amount, "lid": str(loan_id)},
        )


async def _classify_npl(tid: uuid.UUID, loan_id: uuid.UUID) -> None:
    """Fixture stand-in for the arrears job's persisted output (FM9 of
    P13.15 requires a stored NPL class before a write-off request).
    Figures consistent with classify(120): substandard, 25%."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loans SET classification = 'substandard', days_past_due = 120, "
                "provision_pct = 25.00 WHERE id = CAST(:lid AS uuid)"
            ),
            {"lid": str(loan_id)},
        )


async def _posted_write_off(
    tid: uuid.UUID,
    requester: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    voters: tuple[uuid.UUID, uuid.UUID],
) -> corrections_service.WriteOffRecord:
    """Drive the REAL P13.15 workflow to a POSTED write-off."""
    async with tenant_session(factory(), tid) as session:
        record = await corrections_service.request_write_off(
            session, tid, requester, loan_id, reason="irrecoverable"
        )
    for voter in voters:
        async with tenant_session(factory(), tid) as session:
            await corrections_service.cast_write_off_vote(
                session, tid, voter, record.id, Vote.APPROVE
            )
    async with tenant_session(factory(), tid) as session:
        await corrections_service.post_write_off(session, tid, voters[0], record.id)
        return await corrections_service.get_write_off(session, tid, record.id)


async def _written_off_claim(
    tid: uuid.UUID, requester: uuid.UUID
) -> tuple[corrections_service.WriteOffRecord, uuid.UUID, uuid.UUID]:
    """The standard claim fixture: golden loan 24,000.00 + penalty
    150.00 written off -> total_written_off 24,150.00 (hand-computed:
    24,000.00 + 150.00). Returns (write_off, loan_id, member_id)."""
    voter1, _ = await _seed_extra_user(tid, "Credit Committee")
    voter2, _ = await _seed_extra_user(tid, "Credit Committee")
    loan_id, mid = await _disburse(tid)
    await _seed_penalty(tid, loan_id, "150.00")
    await _classify_npl(tid, loan_id)
    record = await _posted_write_off(tid, requester, loan_id, voters=(voter1, voter2))
    assert record.total_written_off == Decimal("24150.00")
    return record, loan_id, mid


async def _receipt(
    tid: uuid.UUID, actor: uuid.UUID, write_off_id: uuid.UUID, amount: str
) -> corrections_service.RecoveryReceiptResult:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.record_recovery_receipt(
            session, tid, actor, write_off_id, amount=Decimal(amount), channel=Channel.BANK
        )


async def _loan_state(tid: uuid.UUID, loan_id: uuid.UUID) -> tuple[Decimal, Decimal, str]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT balance, penalty_due, status FROM loans WHERE id = CAST(:l AS uuid)"),
                {"l": str(loan_id)},
            )
        ).one()
    return Decimal(str(row[0])), Decimal(str(row[1])), str(row[2])


async def _counts(tid: uuid.UUID) -> tuple[int, int, int, int]:
    """(transactions, ledger legs, recovery receipt rows, audits)."""
    async with tenant_session(factory(), tid) as session:
        txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        legs = (await session.execute(text("SELECT count(*) FROM ledger_entries"))).scalar_one()
        recs = (await session.execute(text("SELECT count(*) FROM loan_recoveries"))).scalar_one()
        audits = (await session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
    return int(txns), int(legs), int(recs), int(audits)


async def _guarantee_statuses(tid: uuid.UUID, loan_id: uuid.UUID) -> list[str]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text("SELECT status FROM guarantees WHERE loan_id = CAST(:l AS uuid)"),
                {"l": str(loan_id)},
            )
        ).all()
    return sorted(str(r[0]) for r in rows)


async def _seed_active_guarantee(tid: uuid.UUID, loan_id: uuid.UUID, borrower: uuid.UUID) -> None:
    """A consented (active) guarantee behind the loan, from a second
    member — the state the P9 disbursement linkage leaves behind."""
    guarantor = await _seed_member(tid)
    async with tenant_session(factory(), tid) as session:
        # P14.5 FM4: a row born 'active' must carry a consent
        # principal — seeded fixtures attest via any tenant user.
        attestor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                " loan_id, amount, status, consent_attested_by, consent_reference) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), CAST(:l AS uuid), '5000.00', 'active', "
                "CAST(:att AS uuid), 'seeded fixture (P14.5 FM4)')"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "g": str(guarantor),
                "b": str(borrower),
                "l": str(loan_id),
                "att": str(attestor),
            },
        )


class _KillSwitchError(RuntimeError):
    """Simulated crash after the receipt ran, before commit."""


# ---------------------------------------------------------------------------
# FM1 — receipts only against a POSTED write-off
# ---------------------------------------------------------------------------


def test_fm1_receipt_only_against_a_posted_write_off() -> None:
    """A requested (unposted) write-off has no derecognised claim to
    recover against -> 409 with ZERO side effects; the 0030 constraint
    trigger refuses the same INSERT via direct SQL. Falsifiable:
    remove the status guard in record_recovery_receipt (the service
    409 vanishes) or drop the trigger (the SQL probe passes)."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, mid = await _disburse(tid)
        await _seed_penalty(tid, loan_id, "150.00")
        await _classify_npl(tid, loan_id)
        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.request_write_off(
                session, tid, actor, loan_id, reason="irrecoverable"
            )
        assert record.status is corrections_service.WriteOffStatus.REQUESTED

        before = await _counts(tid)
        with pytest.raises(ConflictError, match="posted write-offs"):
            await _receipt(tid, actor, record.id, "100.00")
        assert await _counts(tid) == before  # zero side effects

        # The DB backstop (0030 constraint trigger): a direct-SQL
        # receipt against the unposted write-off raises.
        with pytest.raises(DBAPIError, match="POSTED write-offs only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO loan_recoveries "
                        "(id, tenant_id, write_off_id, loan_id, member_id, "
                        " transaction_id, amount, recorded_by) "
                        "SELECT gen_random_uuid(), CAST(:tid AS uuid), "
                        "CAST(:wid AS uuid), CAST(:lid AS uuid), CAST(:mid AS uuid), "
                        "t.id, '100.00', CAST(:actor AS uuid) "
                        "FROM transactions t LIMIT 1"
                    ),
                    {
                        "tid": str(tid),
                        "wid": str(record.id),
                        "lid": str(loan_id),
                        "mid": str(mid),
                        "actor": str(actor),
                    },
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — over-recovery refused; partials tracked against the claim
# ---------------------------------------------------------------------------


def test_fm2_over_recovery_refused_partials_tracked_to_the_cent() -> None:
    """HAND-COMPUTED: claim 24,150.00 (24,000.00 balance + 150.00
    penalty). 24,150.01 > 24,150.00 -> 409, zero side effects.
    10,000.00 lands -> outstanding 24,150.00 - 10,000.00 = 14,150.00.
    14,150.01 > 14,150.00 -> 409. 14,150.00 lands -> outstanding 0.00,
    claim fully recovered. A further 0.01 -> 409 (nothing left).
    Falsifiable: remove the outstanding-claim guard and the oversized
    receipts land, failing the count assertions; drop the 0030 trigger
    and the direct-SQL over-insert passes."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, loan_id, mid = await _written_off_claim(tid, actor)

        before = await _counts(tid)
        with pytest.raises(ConflictError, match="exceeds the outstanding"):
            await _receipt(tid, actor, record.id, "24150.01")
        assert await _counts(tid) == before

        first = await _receipt(tid, actor, record.id, "10000.00")
        assert first.recovered_total == Decimal("10000.00")
        assert first.outstanding_claim == Decimal("14150.00")
        assert not first.claim_fully_recovered

        with pytest.raises(ConflictError, match="exceeds the outstanding"):
            await _receipt(tid, actor, record.id, "14150.01")

        second = await _receipt(tid, actor, record.id, "14150.00")
        assert second.recovered_total == Decimal("24150.00")
        assert second.outstanding_claim == Decimal("0.00")
        assert second.claim_fully_recovered

        with pytest.raises(ConflictError, match="exceeds the outstanding"):
            await _receipt(tid, actor, record.id, "0.01")

        # The DB backstop: even a direct-SQL insert past the claim cap
        # raises (the 0030 constraint trigger).
        with pytest.raises(DBAPIError, match="exceed the written-off claim"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO loan_recoveries "
                        "(id, tenant_id, write_off_id, loan_id, member_id, "
                        " transaction_id, amount, recorded_by) "
                        "SELECT gen_random_uuid(), CAST(:tid AS uuid), "
                        "CAST(:wid AS uuid), CAST(:lid AS uuid), CAST(:mid AS uuid), "
                        "t.id, '0.01', CAST(:actor AS uuid) "
                        "FROM transactions t LIMIT 1"
                    ),
                    {
                        "tid": str(tid),
                        "wid": str(record.id),
                        "lid": str(loan_id),
                        "mid": str(mid),
                        "actor": str(actor),
                    },
                )

    asyncio.run(run())


def test_fm2_concurrent_direct_sql_inserts_serialise_on_the_claim_anchor() -> None:
    """!51 review N1, landed as migration 0034: two CONCURRENT
    direct-SQL receipt inserts that TOGETHER exceed the outstanding
    claim land exactly ONE row. HAND-COMPUTED: claim 24,150.00; a
    10,000.00 service receipt leaves outstanding 24,150.00 - 10,000.00
    = 14,150.00. Session A inserts 10,000.00 (total 20,000.00 <=
    24,150.00 — passes) and HOLDS its transaction open; session B
    inserts 10,000.00 concurrently. Under the 0034 parent FOR UPDATE,
    B blocks on A's claim-anchor lock; after A commits, B's SUM runs
    as a fresh statement (READ COMMITTED plpgsql: one snapshot per
    statement) seeing 10,000 + 10,000 + its own 10,000 = 30,000.00 >
    24,150.00 -> raises. Falsifiable: restore the 0030 body (FOR
    UPDATE removed) and B never blocks — each cap check runs on a
    snapshot excluding the other insert (20,000.00 <= 24,150.00 each),
    BOTH land, and the blocked/row-count/SUM assertions fail. The
    function-definition pin makes a database missing 0034 fail loudly
    here instead of passing vacuously."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, loan_id, mid = await _written_off_claim(tid, actor)
        await _receipt(tid, actor, record.id, "10000.00")  # outstanding 14,150.00

        # Pin: the live trigger function carries the 0034 locking probe.
        async with tenant_session(factory(), tid) as session:
            definition = (
                await session.execute(
                    text("SELECT pg_get_functiondef('check_recovery_within_claim()'::regprocedure)")
                )
            ).scalar_one()
        assert "FOR UPDATE OF w" in str(definition)

        insert_sql = text(
            "INSERT INTO loan_recoveries "
            "(id, tenant_id, write_off_id, loan_id, member_id, "
            " transaction_id, amount, recorded_by) "
            "SELECT gen_random_uuid(), CAST(:tid AS uuid), "
            "CAST(:wid AS uuid), CAST(:lid AS uuid), CAST(:mid AS uuid), "
            "t.id, '10000.00', CAST(:actor AS uuid) "
            "FROM transactions t LIMIT 1"
        )
        params = {
            "tid": str(tid),
            "wid": str(record.id),
            "lid": str(loan_id),
            "mid": str(mid),
            "actor": str(actor),
        }

        async def loser() -> None:
            async with tenant_session(factory(), tid) as session_b:
                await session_b.execute(insert_sql, params)

        holder_a = tenant_session(factory(), tid)
        session_a = await holder_a.__aenter__()
        try:
            # A's insert passes its own cap check and holds the claim
            # anchor (the 0034 FOR UPDATE) until commit.
            await session_a.execute(insert_sql, params)
            task = asyncio.create_task(loser())
            await asyncio.sleep(0.5)
            blocked = not task.done()
        finally:
            await holder_a.__aexit__(None, None, None)  # commit A
        assert blocked, "the second insert did not serialise on the claim anchor"
        with pytest.raises(DBAPIError, match="exceed the written-off claim"):
            await asyncio.wait_for(task, timeout=10)

        # Exactly one of the two concurrent inserts landed: the
        # service receipt + A = 2 rows, SUM 20,000.00 <= 24,150.00.
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*), COALESCE(SUM(amount), 0) FROM loan_recoveries "
                        "WHERE write_off_id = CAST(:wid AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"wid": str(record.id), "tid": str(tid)},
                )
            ).one()
        assert int(row[0]) == 2
        assert Decimal(str(row[1])) == Decimal("20000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — a receipt never resurrects the loan
# ---------------------------------------------------------------------------


def test_fm3_receipt_never_resurrects_the_loan() -> None:
    """The RC- posting recognises income; it never restores a
    receivable. After ANY receipt (partial and full) the loan row is
    byte-identical to its post-write-off state (written_off, 0.00,
    0.00), repayments stay refused loudly, and WRITTEN_OFF stays
    terminal in the domain map. Falsifiable: have the receipt credit
    loans.receivable or flip the status and these asserts fail."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, loan_id, _ = await _written_off_claim(tid, actor)
        assert await _loan_state(tid, loan_id) == (Decimal("0.00"), Decimal("0.00"), "written_off")

        await _receipt(tid, actor, record.id, "10000.00")
        assert await _loan_state(tid, loan_id) == (Decimal("0.00"), Decimal("0.00"), "written_off")

        await _receipt(tid, actor, record.id, "14150.00")  # full recovery
        assert await _loan_state(tid, loan_id) == (Decimal("0.00"), Decimal("0.00"), "written_off")

        # A4 regression: repayment against written_off stays refused.
        with pytest.raises(ConflictError, match="cannot accept repayments"):
            async with tenant_session(factory(), tid) as session:
                await loans_service.record_repayment(
                    session, tid, actor, loan_id, amount=Decimal("100.00"), channel=Channel.BANK
                )
        # The domain map keeps WRITTEN_OFF terminal — no code path may
        # widen it for recoveries.
        with pytest.raises(InvalidTransitionError):
            loan_transition(LoanStatus.WRITTEN_OFF, LoanStatus.ACTIVE)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — an unresolved claim blocks member exit
# ---------------------------------------------------------------------------


def test_fm4_exit_blocked_until_the_claim_is_fully_recovered() -> None:
    """Issue #21 item 4 (the documented decision): write-off is not
    forgiveness, so the surviving claim blocks exit like an active
    obligation. HAND-COMPUTED: claim 24,150.00; a 10,000.00 partial
    leaves 14,150.00 outstanding -> still blocked; the 14,150.00
    closing receipt zeroes the claim -> the exit request succeeds.
    Falsifiable: remove the unresolved-claim guard in
    _compute_under_locks and the FIRST exit request succeeds."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, _, mid = await _written_off_claim(tid, actor)

        with pytest.raises(ConflictError, match="unresolved written-off"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.request_exit(session, tid, actor, mid)

        await _receipt(tid, actor, record.id, "10000.00")
        with pytest.raises(ConflictError, match="unresolved written-off"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.request_exit(session, tid, actor, mid)

        await _receipt(tid, actor, record.id, "14150.00")  # full recovery
        async with tenant_session(factory(), tid) as session:
            exit_record = await exits_service.request_exit(session, tid, actor, mid)
        assert exit_record is not None  # eligibility passed

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — guarantee disposition: released ONLY on full recovery
# ---------------------------------------------------------------------------


def test_fm5_guarantees_released_exactly_by_the_full_recovery_receipt() -> None:
    """The documented issue-#21 policy: guarantees back the SURVIVING
    claim. The write-off leaves them untouched (A4), every partial
    receipt leaves them untouched, and the receipt that recovers the
    claim IN FULL releases them in the same transaction (side-effect
    count: exactly one guarantee flips to released, with its audit
    row). Falsifiable: release on partial receipts (the partial assert
    fails) or never release (the final assert fails)."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, mid = await _disburse(tid)
        await _seed_active_guarantee(tid, loan_id, mid)
        await _seed_penalty(tid, loan_id, "150.00")
        await _classify_npl(tid, loan_id)
        record = await _posted_write_off(tid, actor, loan_id, voters=(voter1, voter2))

        # A4: the write-off itself left the guarantee untouched.
        assert await _guarantee_statuses(tid, loan_id) == ["active"]

        partial = await _receipt(tid, actor, record.id, "10000.00")
        assert partial.guarantees_released == 0
        assert await _guarantee_statuses(tid, loan_id) == ["active"]

        closing = await _receipt(tid, actor, record.id, "14150.00")
        assert closing.claim_fully_recovered
        assert closing.guarantees_released == 1
        assert await _guarantee_statuses(tid, loan_id) == ["released"]

        # The discharge is audited (write_off.claim_recovered).
        async with tenant_session(factory(), tid) as session:
            recovered_audits = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log WHERE action = 'write_off.claim_recovered'"
                    )
                )
            ).scalar_one()
        assert int(recovered_audits) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — append-only receipts + ledger conservation, at the SQL level
# ---------------------------------------------------------------------------


def test_fm6_receipts_are_append_only_and_the_posting_balances() -> None:
    """HAND-COMPUTED: a 10,000.00 bank receipt posts exactly DR
    cash.bank 10,000.00 / CR income.bad_debt_recoveries 10,000.00
    (balanced; the 0014 trigger would abort anything else). The
    receipt row is money history: UPDATE and DELETE raise via the 0030
    append-only triggers, so the recovered total ALWAYS reconstructs
    from the surviving rows (v1.1 rule 2). Falsifiable: drop the 0030
    triggers and both SQL probes pass."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, _, _ = await _written_off_claim(tid, actor)
        receipt = await _receipt(tid, actor, record.id, "10000.00")
        assert receipt.txn_ref.startswith("RC-")

        async with tenant_session(factory(), tid) as session:
            legs = (
                await session.execute(
                    text(
                        "SELECT account, side, amount FROM ledger_entries "
                        "WHERE transaction_id = CAST(:t AS uuid) ORDER BY side"
                    ),
                    {"t": str(receipt.txn_id)},
                )
            ).all()
        assert [(str(r[0]), str(r[1]), Decimal(str(r[2]))) for r in legs] == [
            ("income.bad_debt_recoveries", "credit", Decimal("10000.00")),
            ("cash.bank", "debit", Decimal("10000.00")),
        ]

        # Append-only at the SQL level (the 0004/0014 discipline).
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("UPDATE loan_recoveries SET amount = '1.00' WHERE id = CAST(:id AS uuid)"),
                    {"id": str(receipt.recovery_id)},
                )
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("DELETE FROM loan_recoveries WHERE id = CAST(:id AS uuid)"),
                    {"id": str(receipt.recovery_id)},
                )

        # The recovered total reconstructs from the append-only rows.
        async with tenant_session(factory(), tid) as session:
            total = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM loan_recoveries "
                        "WHERE write_off_id = CAST(:w AS uuid)"
                    ),
                    {"w": str(record.id)},
                )
            ).scalar_one()
        assert Decimal(str(total)) == Decimal("10000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — kill-switch: abort mid-receipt leaves zero partial state
# ---------------------------------------------------------------------------


def test_fm7_kill_switch_mid_receipt_leaves_zero_partial_state() -> None:
    """The aborted receipt is the FULL-recovery one, so the abort must
    also roll back the guarantee release — the highest-stakes partial
    state this transaction can leave. HAND-COMPUTED: outstanding after
    the 10,000.00 partial is 14,150.00; the aborted receipt is
    14,150.00."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, mid = await _disburse(tid)
        await _seed_active_guarantee(tid, loan_id, mid)
        await _seed_penalty(tid, loan_id, "150.00")
        await _classify_npl(tid, loan_id)
        record = await _posted_write_off(tid, actor, loan_id, voters=(voter1, voter2))
        await _receipt(tid, actor, record.id, "10000.00")
        before = await _counts(tid)

        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.record_recovery_receipt(
                    session,
                    tid,
                    actor,
                    record.id,
                    amount=Decimal("14150.00"),
                    channel=Channel.BANK,
                )
                raise _KillSwitchError()  # crash inside the transaction

        # ZERO partial state: no posting, no receipt row, no audit, and
        # the guarantee discharge rolled back with everything else.
        assert await _counts(tid) == before
        assert await _guarantee_statuses(tid, loan_id) == ["active"]

        # The same call commits cleanly afterwards.
        closing = await _receipt(tid, actor, record.id, "14150.00")
        assert closing.claim_fully_recovered
        assert await _guarantee_statuses(tid, loan_id) == ["released"]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — the P13.16 recovery-case linkage is recorded explicitly
# ---------------------------------------------------------------------------


def test_fm8_recovery_case_linkage_recorded_explicitly() -> None:
    """Issue #21 item 2: collections against the surviving claim are
    worked under the P13.16 case; the receipt records the loan's
    closed_written_off case EXPLICITLY (row column + audit payload).
    The case is driven through the REAL paths: opened while NPL,
    auto-closed as closed_written_off by the close pass after the
    write-off posts. A claim without a case records NULL. Falsifiable:
    drop the case resolution in record_recovery_receipt and the
    linkage assertions fail."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")

        # Claim A: with a recovery case worked to closed_written_off.
        loan_a, _ = await _disburse(tid)
        await _seed_penalty(tid, loan_a, "150.00")
        await _classify_npl(tid, loan_a)
        async with tenant_session(factory(), tid) as session:
            case = await recovery_service.open_recovery_case(session, tid, actor, loan_id=loan_a)
        record_a = await _posted_write_off(tid, actor, loan_a, voters=(voter1, voter2))
        scope = partial(tenant_session, factory(), tid)
        close_result = await run_recovery_close_pass(scope, tid)
        assert close_result.closed_written_off == 1
        async with tenant_session(factory(), tid) as session:
            closed = await recovery_service.get_recovery_case(session, tid, case.id)
        assert closed.status is RecoveryCaseStatus.CLOSED_WRITTEN_OFF

        receipt_a = await _receipt(tid, actor, record_a.id, "5000.00")
        assert receipt_a.recovery_case_id == case.id
        async with tenant_session(factory(), tid) as session:
            audit_case = (
                await session.execute(
                    text(
                        "SELECT after->>'recovery_case_id' FROM audit_log "
                        "WHERE action = 'write_off.recovery_recorded' "
                        "AND entity_id = :eid"
                    ),
                    {"eid": str(receipt_a.recovery_id)},
                )
            ).scalar_one()
        assert str(audit_case) == str(case.id)

        # Claim B: no recovery case ever existed -> NULL linkage.
        record_b, _, _ = await _written_off_claim(tid, actor)
        receipt_b = await _receipt(tid, actor, record_b.id, "100.00")
        assert receipt_b.recovery_case_id is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — authz matrix, cross-tenant probes, idempotency by side effects
# ---------------------------------------------------------------------------


def test_fm9_routes_enforce_the_corrections_matrix_per_role() -> None:
    """Both new routes follow the DEDICATED corrections matrix exactly
    (the P13.15 A3 rule): recording a receipt is corrections:create,
    reading the receipts page is corrections:view. A 404 on a
    permitted probe still proves authorisation passed (the probe id is
    synthetic). Falsifiable: gate the routes with transactions:* and
    the Teller (transactions:create) suddenly records receipts."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await _seed_actor()  # seeds the full matrix once
        for role_name in ROLE_NAMES:
            _, token = await _seed_extra_user(tid, role_name)
            can_create = matrix[role_name][Module.CORRECTIONS][Action.CREATE]
            can_view = matrix[role_name][Module.CORRECTIONS][Action.VIEW]
            probe = str(uuid.uuid4())
            async with api_client() as client:
                res = await client.post(
                    f"/corrections/write-offs/{probe}/recoveries",
                    json={"amount": "100.00", "channel": "bank"},
                    headers=_headers(token, idem=f"r-{uuid.uuid4().hex[:8]}"),
                )
                if can_create:
                    assert res.status_code == 404, (role_name, res.status_code)
                else:
                    assert res.status_code == 403, (role_name, res.status_code)
                res = await client.get(
                    f"/corrections/write-offs/{probe}/recoveries",
                    headers=_headers(token),
                )
                if can_view:
                    assert res.status_code == 404, (role_name, res.status_code)
                else:
                    assert res.status_code == 403, (role_name, res.status_code)

    asyncio.run(run())


def test_fm9_cross_tenant_probes_and_idempotent_replay() -> None:
    """Issue-#17 probes: tenant B sees zero receipt rows through raw
    SQL under its session and 404s on tenant A's write-off through the
    API. Idempotency: replaying the SAME actor's key returns the
    stored response and records exactly ONE receipt (side-effect
    count, never the return value alone); a DIFFERENT actor replaying
    the same key MISSES the stored response — since P14.5 (FM5) the
    claim key itself is scoped (tenant, actor principal, route), so
    the second actor EXECUTES their own request (their own receipt,
    their own claim row), and can never read the first actor's stored
    response (the !29 R4 lesson completed — the interim same-claim 409
    is upgraded to fully disjoint per-actor claims)."""

    async def run() -> None:
        tid_a, actor_a, token_a = await _seed_actor()
        tid_b, _, token_b = await _seed_actor()
        record, _, _ = await _written_off_claim(tid_a, actor_a)

        # Replay with the same key: exactly one receipt by row count.
        key = f"rcv-{uuid.uuid4().hex[:10]}"
        async with api_client() as client:
            first = await client.post(
                f"/corrections/write-offs/{record.id}/recoveries",
                json={"amount": "100.00", "channel": "bank"},
                headers=_headers(token_a, idem=key),
            )
            assert first.status_code == 201, first.text
            replay = await client.post(
                f"/corrections/write-offs/{record.id}/recoveries",
                json={"amount": "100.00", "channel": "bank"},
                headers=_headers(token_a, idem=key),
            )
            assert replay.status_code == 201
            assert replay.headers.get("idempotency-replayed") == "true"
            assert replay.json() == first.json()
        _, _, receipts, _ = await _counts(tid_a)
        assert receipts == 1  # side-effect count, not the response

        # Cross-actor replay MISSES (P14.5 FM5): the claim key is
        # scoped per actor principal, so the second actor's identical
        # key + body EXECUTES as their own request — a NEW receipt
        # under their own claim, never the first actor's stored
        # response. Falsifiable: drop the actor from
        # scoped_storage_key and this replays actor A's 201 verbatim
        # (same receipt id, count stuck at 1).
        _actor2, token2 = await _seed_extra_user(tid_a, "Accountant")
        async with api_client() as client:
            other = await client.post(
                f"/corrections/write-offs/{record.id}/recoveries",
                json={"amount": "100.00", "channel": "bank"},
                headers=_headers(token2, idem=key),
            )
            assert other.status_code == 201, other.text
            assert other.headers.get("idempotency-replayed") != "true"
            assert other.json()["recovery_id"] != first.json()["recovery_id"], (
                "the cross-actor replay must never serve the stored response"
            )
        _, _, receipts, _ = await _counts(tid_a)
        assert receipts == 2, "the second actor's request executes in its own right"

        # Cross-tenant: zero rows under tenant B's session; 404 via API.
        async with tenant_session(factory(), tid_b) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM loan_recoveries"))
            ).scalar_one()
        assert int(count) == 0
        async with api_client() as client:
            res = await client.get(
                f"/corrections/write-offs/{record.id}/recoveries",
                headers=_headers(token_b),
            )
            assert res.status_code == 404
            res = await client.post(
                f"/corrections/write-offs/{record.id}/recoveries",
                json={"amount": "100.00", "channel": "bank"},
                headers=_headers(token_b, idem=f"x-{uuid.uuid4().hex[:8]}"),
            )
            assert res.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Contract + member-status gate + receipts listing
# ---------------------------------------------------------------------------


def test_contract_rejects_caller_supplied_claim_figures() -> None:
    """extra="forbid" (v1.1 rule 1): the body carries ONLY the cash
    received and its channel — a caller-supplied claim figure
    (total_written_off / recovered_total / outstanding) is a 422 with
    zero side effects."""

    async def run() -> None:
        tid, actor, token = await _seed_actor()
        record, _, _ = await _written_off_claim(tid, actor)
        before = await _counts(tid)
        async with api_client() as client:
            res = await client.post(
                f"/corrections/write-offs/{record.id}/recoveries",
                json={
                    "amount": "100.00",
                    "channel": "bank",
                    "total_written_off": "1.00",
                },
                headers=_headers(token, idem=f"c-{uuid.uuid4().hex[:8]}"),
            )
        assert res.status_code == 422
        assert await _counts(tid) == before

    asyncio.run(run())


def test_exited_member_receipt_refused_by_the_single_gatekeeper() -> None:
    """Defence in depth: the P13.13 capability map holds no EXITED
    entry for MoneyOperation.RECOVERY, so a receipt against an exited
    member's claim is refused by the single gatekeeper (the FM4 exit
    guard makes this state unreachable going forward; the fixture
    forces it via SQL to prove the wiring)."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        record, _, mid = await _written_off_claim(tid, actor)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE members SET status = 'exited' WHERE id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        with pytest.raises(ConflictError, match="exited"):
            await _receipt(tid, actor, record.id, "100.00")

    asyncio.run(run())


def test_receipts_listing_pages_by_keyset_with_reconstructed_totals() -> None:
    """HAND-COMPUTED: three receipts 100.00 + 200.00 + 300.00 against
    the 24,150.00 claim -> recovered 600.00, outstanding 23,550.00.
    Page size 2 walks oldest-first with a cursor; totals identical on
    both pages (reconstructed from the append-only rows, rule 2)."""

    async def run() -> None:
        tid, actor, token = await _seed_actor()
        record, _, _ = await _written_off_claim(tid, actor)
        for amount in ("100.00", "200.00", "300.00"):
            await _receipt(tid, actor, record.id, amount)

        async with api_client() as client:
            page1 = await client.get(
                f"/corrections/write-offs/{record.id}/recoveries",
                params={"limit": 2},
                headers=_headers(token),
            )
            assert page1.status_code == 200
            body1 = page1.json()
            assert [r["amount"] for r in body1["items"]] == ["100.00", "200.00"]
            assert body1["recovered_total"] == "600.00"
            assert body1["outstanding_claim"] == "23550.00"
            assert body1["next_cursor"] is not None

            page2 = await client.get(
                f"/corrections/write-offs/{record.id}/recoveries",
                params={"limit": 2, "cursor": body1["next_cursor"]},
                headers=_headers(token),
            )
            assert page2.status_code == 200
            body2 = page2.json()
            assert [r["amount"] for r in body2["items"]] == ["300.00"]
            assert body2["recovered_total"] == "600.00"
            assert body2["next_cursor"] is None

            # FM9 (#31 batch 13): forged cursors are sanitized 400s —
            # garbage AND a validly signed token for a FOREIGN scope
            # (same tenant), which only the tag check can refuse.
            cross_scope = encode_cursor(body1["next_cursor"], tenant_id=tid, endpoint="tamper.test")
            for forged in ("not-a-cursor", cross_scope):
                res = await client.get(
                    f"/corrections/write-offs/{record.id}/recoveries",
                    params={"cursor": forged},
                    headers=_headers(token),
                )
                assert res.status_code == 400, (forged, res.text)
                assert set(res.json().keys()) == {"category", "correlation_id"}

    asyncio.run(run())
