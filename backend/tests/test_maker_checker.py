"""Integration tests for issue #24: maker-checker adjustments (N1)
and the repayments append-only discipline (N4).

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Failure modes (v1.2 rule 15) — every oracle HAND-COMPUTED in comments,
never captured from the code under test; every guard test fails with
its guard removed (MASTER_PROMPT §4):

  FM1 maker checks own      — approve/reject by the maker -> 409 with
                              zero side effects; direct-SQL
                              maker = checker -> the 0031 SoD CHECK
                              violation (collusion-resistant).
  FM2 assurance checker     — an Auditor approving/rejecting -> 409
                              (the !47 B2 audit-independence rule,
                              beneath RBAC as defence in depth).
  FM3 approval drift        — any snapshot component moving between
                              request and approval (balance /
                              penalty_due / loan status) -> 409
                              POSTING NOTHING, component named.
  FM4 double pend           — a second request while one is live is
                              blocked by the 0031 partial-unique claim
                              (service AND direct SQL); a REJECTED
                              request frees the slot. !52 review F2:
                              the rejection carries a REQUIRED checker
                              rationale into the audit `after` payload;
                              a missing/empty reason is a 422 with
                              zero side effects.
  FM5 repayments mutation   — direct-SQL UPDATE/DELETE of repayments
                              rows raise via the 0032 triggers (N4:
                              the sign-flip forensic hole is closed).
  FM6 terminal un-posting   — moving a posted/rejected adjustment via
                              SQL raises (0031 trigger status machine;
                              one-shot checker/decided_at fills).
  FM7 kill-switch           — covered by test_corrections.py::
                              test_fm7_kill_switch_mid_adjustment_
                              leaves_zero_partial_state (approval-
                              phase abort, zero partial state).
  FM8 authz matrix          — 7-role probes on all four adjustment
                              routes follow corrections:create/view/
                              approve exactly.
  FM9 cross-tenant          — issue-#17 probes: a foreign adjustment
                              is a 404 on every route, zero rows.
  FM10 idempotent replay    — request and approval replays produce
                              exactly one effect BY SIDE-EFFECT COUNTS.

Plus the full-matrix transition test for the new adjustment status
machine (ONE gatekeeper: corrections.adjustment_transition) and the
0031/0032 loud-refusal downgrade guards executed from the REAL
migration modules (the 0017 _DOWN_GUARD falsifiability precedent).

Golden fixture (tests/golden/loan_schedules.json): 24,000.00 at 12%/yr
over 12 months. Repaying 1,000.00 leaves balance 23,000.00.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import corrections as corrections_service
from genesis.application import loans as loans_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import disburse_loan
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import ConflictError, InvalidInputError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

AdjustmentStatus = corrections_service.AdjustmentStatus


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
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                "'MakerChecker Member')"
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
    return mid


async def _disburse(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Disburse the golden-case loan; returns (loan_id, member_id)."""
    mid = await _seed_member(tid)
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
                "CAST(:pid AS uuid), '24000.00', 12, 12.00, 'approved')"
            ),
            {"id": str(app_id), "tid": str(tid), "mid": str(mid), "pid": str(pid)},
        )
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id, mid


async def _repay(
    tid: uuid.UUID, actor: uuid.UUID, loan_id: uuid.UUID, amount: str
) -> loans_service.RepaymentResult:
    async with tenant_session(factory(), tid) as session:
        return await loans_service.record_repayment(
            session, tid, actor, loan_id, amount=Decimal(amount), channel=Channel.BANK
        )


async def _repayment_id_for_txn(tid: uuid.UUID, txn_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(factory(), tid) as session:
        rid = (
            await session.execute(
                text(
                    "SELECT id FROM repayments WHERE transaction_id = CAST(:t AS uuid) "
                    "AND amount > 0"
                ),
                {"t": str(txn_id)},
            )
        ).scalar_one()
    return uuid.UUID(str(rid))


async def _request(
    tid: uuid.UUID, actor: uuid.UUID, repayment_id: uuid.UUID, reason: str = "keying error"
) -> corrections_service.AdjustmentRecord:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.request_repayment_adjustment(
            session, tid, actor, repayment_id, reason=reason
        )


async def _approve(
    tid: uuid.UUID, checker: uuid.UUID, adjustment_id: uuid.UUID
) -> corrections_service.AdjustmentResult:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.approve_repayment_adjustment(
            session, tid, checker, adjustment_id
        )


async def _reject(
    tid: uuid.UUID,
    checker: uuid.UUID,
    adjustment_id: uuid.UUID,
    version: int,
    reason: str = "not warranted",
) -> corrections_service.AdjustmentRecord:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.reject_repayment_adjustment(
            session, tid, checker, adjustment_id, version=version, reason=reason
        )


async def _get(tid: uuid.UUID, adjustment_id: uuid.UUID) -> corrections_service.AdjustmentRecord:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.get_adjustment(session, tid, adjustment_id)


async def _money_counts(tid: uuid.UUID) -> tuple[int, int, int]:
    """(transactions, ledger legs, repayments rows) — the MONEY
    side-effect counters (a pending workflow row is not money)."""
    async with tenant_session(factory(), tid) as session:
        txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        legs = (await session.execute(text("SELECT count(*) FROM ledger_entries"))).scalar_one()
        reps = (await session.execute(text("SELECT count(*) FROM repayments"))).scalar_one()
    return int(txns), int(legs), int(reps)


async def _loan_state(tid: uuid.UUID, loan_id: uuid.UUID) -> tuple[Decimal, Decimal, str]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT balance, penalty_due, status FROM loans WHERE id = CAST(:l AS uuid)"),
                {"l": str(loan_id)},
            )
        ).one()
    return Decimal(str(row[0])), Decimal(str(row[1])), str(row[2])


async def _pending_for_new_repayment(
    tid: uuid.UUID, actor: uuid.UUID
) -> tuple[uuid.UUID, corrections_service.AdjustmentRecord]:
    """A fresh loan with one 1,000.00 repayment and a pending
    adjustment of it; returns (loan_id, pending record)."""
    loan_id, _ = await _disburse(tid)
    repayment = await _repay(tid, actor, loan_id, "1000.00")
    repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
    pending = await _request(tid, actor, repayment_id)
    return loan_id, pending


# ---------------------------------------------------------------------------
# The status machine: full matrix through the ONE gatekeeper
# ---------------------------------------------------------------------------


def test_full_matrix_adjustment_status_transitions() -> None:
    """Every (current, target) pair is enumerated; exactly two moves
    are legal — pending_approval -> posted and pending_approval ->
    rejected; the other seven raise through the single gatekeeper
    (adjustment_transition — the only writer-facing validator; the
    0031 trigger mirrors it at the database, proven in FM6).
    Falsifiable: add a transition to the map and the illegal-set count
    breaks."""
    legal = {
        (AdjustmentStatus.PENDING_APPROVAL, AdjustmentStatus.POSTED),
        (AdjustmentStatus.PENDING_APPROVAL, AdjustmentStatus.REJECTED),
    }
    for current in AdjustmentStatus:
        for target in AdjustmentStatus:
            if (current, target) in legal:
                corrections_service.adjustment_transition(current, target)
            else:
                with pytest.raises(ConflictError, match="cannot move"):
                    corrections_service.adjustment_transition(current, target)


# ---------------------------------------------------------------------------
# FM1 — maker can never be checker (server guard + the 0031 SoD CHECK)
# ---------------------------------------------------------------------------


def test_fm1_maker_cannot_check_own_adjustment() -> None:
    """The maker approving or rejecting their OWN request is a 409 with
    zero money side effects, and the row stays pending. Falsifiable:
    drop the server-side maker<>checker guard and the approval posts."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        _, pending = await _pending_for_new_repayment(tid, maker)
        before = await _money_counts(tid)

        with pytest.raises(ConflictError, match="segregation of duties"):
            await _approve(tid, maker, pending.id)
        with pytest.raises(ConflictError, match="segregation of duties"):
            await _reject(tid, maker, pending.id, pending.version)

        assert await _money_counts(tid) == before
        record = await _get(tid, pending.id)
        assert record.status is AdjustmentStatus.PENDING_APPROVAL
        assert record.checker_id is None

    asyncio.run(run())


def test_fm1_db_check_refuses_maker_as_checker_via_direct_sql() -> None:
    """The collusion-resistant backstop (issue #24 / !47 B2): the 0031
    ck_repayment_adjustments_sod CHECK makes maker = checker
    unrepresentable even through direct SQL on the app role — both the
    one-shot UPDATE fill and a fresh INSERT. Falsifiable: drop the
    CHECK and both probes land."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        _, pending = await _pending_for_new_repayment(tid, maker)

        # The UPDATE probe: filling checker_id from NULL is the one
        # write the trigger permits — the SoD CHECK still refuses the
        # maker's own id.
        with pytest.raises(DBAPIError, match="sod"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE repayment_adjustments SET checker_id = maker_id "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending.id)},
                )
        # The INSERT probe: a fabricated self-checked posted row.
        with pytest.raises(DBAPIError, match="sod"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO repayment_adjustments "
                        "(tenant_id, repayment_id, loan_id, original_transaction_id, "
                        " maker_id, checker_id, reason, amount, penalties, interest, "
                        " principal, status) "
                        "SELECT tenant_id, CAST(:rid AS uuid), loan_id, "
                        " original_transaction_id, maker_id, maker_id, 'forged', amount, "
                        " penalties, interest, principal, 'posted' "
                        "FROM repayment_adjustments WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending.id), "rid": str(uuid.uuid4())},
                )
        record = await _get(tid, pending.id)
        assert record.status is AdjustmentStatus.PENDING_APPROVAL
        assert record.checker_id is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — assurance roles can never be checker (!47 B2)
# ---------------------------------------------------------------------------


def test_fm2_assurance_role_checker_is_refused() -> None:
    """Audit independence: the Auditor reviews the corrections trail
    and can never act inside it — approving or rejecting as an Auditor
    is a 409 at the SERVICE layer (defence in depth beneath the RBAC
    matrix, which already denies corrections:approve to the Auditor).
    Falsifiable: drop the ASSURANCE_ROLES exclusion and the service
    call posts."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        auditor, _ = await _seed_extra_user(tid, "Auditor")
        _, pending = await _pending_for_new_repayment(tid, maker)
        before = await _money_counts(tid)

        with pytest.raises(ConflictError, match="assurance"):
            await _approve(tid, auditor, pending.id)
        with pytest.raises(ConflictError, match="assurance"):
            await _reject(tid, auditor, pending.id, pending.version)

        assert await _money_counts(tid) == before
        assert (await _get(tid, pending.id)).status is AdjustmentStatus.PENDING_APPROVAL

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — approval drift: 409 posting NOTHING, component by component
# ---------------------------------------------------------------------------


def test_fm3_approval_drift_409s_posting_nothing_component_by_component() -> None:
    """Each snapshot component is drifted in isolation on its own loan
    and the approval must refuse, naming the component, with ZERO money
    side effects (HAND-COMPUTED states in comments). Falsifiable: drop
    the component from the re-verification and its scenario posts.

      (a) balance: request at balance 23,000 (24,000 - 1,000); a
          second 500.00 repayment moves it to 22,500 -> drift names
          'balance'.
      (b) penalty_due: request at penalty 0.00; the P13.8 stand-in
          seeds 150.00 -> drift names 'penalty_due'.
      (c) status: request at status 'active' (balance 23,000); a full
          23,000 payoff closes the loan -> drift names 'status'.
    """

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker, _ = await _seed_extra_user(tid, "System Admin")

        # (a) balance drift.
        loan_a, pending_a = await _pending_for_new_repayment(tid, maker)
        await _repay(tid, maker, loan_a, "500.00")
        before = await _money_counts(tid)
        with pytest.raises(ConflictError, match="drifted") as exc_a:
            await _approve(tid, checker, pending_a.id)
        assert "balance" in str(exc_a.value)
        assert await _money_counts(tid) == before
        assert (await _get(tid, pending_a.id)).status is AdjustmentStatus.PENDING_APPROVAL

        # (b) penalty_due drift.
        loan_b, pending_b = await _pending_for_new_repayment(tid, maker)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE loans SET penalty_due = '150.00' WHERE id = CAST(:l AS uuid)"),
                {"l": str(loan_b)},
            )
        before = await _money_counts(tid)
        with pytest.raises(ConflictError, match="drifted") as exc_b:
            await _approve(tid, checker, pending_b.id)
        assert "penalty_due" in str(exc_b.value)
        assert await _money_counts(tid) == before

        # (c) status drift (the closing payoff also moves the balance;
        # the message must name status among the drifted components).
        loan_c, pending_c = await _pending_for_new_repayment(tid, maker)
        await _repay(tid, maker, loan_c, "23000.00")  # closes the loan
        assert (await _loan_state(tid, loan_c))[2] == "closed"
        before = await _money_counts(tid)
        with pytest.raises(ConflictError, match="drifted") as exc_c:
            await _approve(tid, checker, pending_c.id)
        assert "status" in str(exc_c.value)
        assert await _money_counts(tid) == before

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — double pend blocked; a rejected request frees the slot
# ---------------------------------------------------------------------------


def test_fm4_double_pend_blocked_and_rejected_frees_the_slot() -> None:
    """The 0031 PARTIAL unique (status <> 'rejected') claimed with
    ON CONFLICT DO NOTHING + rowcount: a live (pending) adjustment
    blocks a second request — including a concurrent pair, which lands
    exactly ONE row — and a stale-version reject is a 409. Rejecting
    with the current version frees the slot: a fresh request succeeds
    and the history keeps BOTH rows (one rejected, one pending).
    Falsifiable: widen the partial-unique predicate (or make rejection
    DELETE the row) and the counts break."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker, _ = await _seed_extra_user(tid, "System Admin")
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, maker, loan_id, "1000.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)

        # Concurrent double-request: exactly one claim lands.
        first, second = await asyncio.gather(
            _request(tid, maker, repayment_id),
            _request(tid, maker, repayment_id),
            return_exceptions=True,
        )
        outcomes = [first, second]
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        records = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(records) == 1 and len(errors) == 1
        assert isinstance(errors[0], ConflictError)
        assert "already been adjusted" in str(errors[0])
        pending = records[0]
        assert isinstance(pending, corrections_service.AdjustmentRecord)

        # A serial second request is refused too.
        with pytest.raises(ConflictError, match="already been adjusted"):
            await _request(tid, maker, repayment_id)

        # Direct-SQL second live row: the partial unique fires.
        with pytest.raises(IntegrityError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO repayment_adjustments "
                        "(tenant_id, repayment_id, loan_id, original_transaction_id, "
                        " maker_id, reason, amount, penalties, interest, principal, "
                        " status, loan_balance_at_request, loan_penalty_due_at_request, "
                        " loan_status_at_request) "
                        "SELECT tenant_id, repayment_id, loan_id, original_transaction_id, "
                        " maker_id, 'dup', amount, penalties, interest, principal, "
                        " 'pending_approval', loan_balance_at_request, "
                        " loan_penalty_due_at_request, loan_status_at_request "
                        "FROM repayment_adjustments WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending.id)},
                )

        # Stale version -> 409; current version -> rejected, with the
        # checker's rationale on the audit record (!52 F2).
        with pytest.raises(ConflictError, match="stale version"):
            await _reject(tid, checker, pending.id, pending.version + 7)
        rejected = await _reject(
            tid, checker, pending.id, pending.version, reason="duplicate of an earlier request"
        )
        assert rejected.status is AdjustmentStatus.REJECTED
        assert rejected.checker_id == checker
        assert rejected.decided_at is not None
        async with tenant_session(factory(), tid) as session:
            audit_reason = (
                await session.execute(
                    text(
                        "SELECT after->>'reason' FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'correction.adjustment_rejected' "
                        "AND entity_id = :eid"
                    ),
                    {"tid": str(tid), "eid": str(pending.id)},
                )
            ).scalar_one()
        assert audit_reason == "duplicate of an earlier request"

        # The slot is free: a fresh request lands; history keeps both.
        fresh = await _request(tid, maker, repayment_id)
        assert fresh.status is AdjustmentStatus.PENDING_APPROVAL
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM repayment_adjustments "
                        "WHERE repayment_id = CAST(:r AS uuid)"
                    ),
                    {"r": str(repayment_id)},
                )
            ).scalar_one()
        assert int(rows) == 2

    asyncio.run(run())


def test_fm4_reject_without_a_reason_is_refused_with_zero_side_effects() -> None:
    """!52 review F2: the checker's rejection rationale is REQUIRED —
    a reject with the reason missing or empty is a 422 at the boundary
    (extra fields stay refused too) and the pending row is untouched:
    still pending_approval, no checker attribution, zero
    adjustment_rejected audit rows. The service backstop refuses a
    whitespace-only reason for direct callers. Falsifiable: make
    `reason` optional on AdjustmentRejectBody (or drop it from the
    audit payload — the FM4 audit assertion above) and this fails."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker, checker_token = await _seed_extra_user(tid, "System Admin")
        _, pending = await _pending_for_new_repayment(tid, maker)

        async with api_client() as client:
            for body in (
                {"version": pending.version},  # reason missing
                {"version": pending.version, "reason": ""},  # below min_length
                {  # unknown field still refused (extra="forbid" intact)
                    "version": pending.version,
                    "reason": "ok",
                    "unexpected": "x",
                },
            ):
                res = await client.post(
                    f"/corrections/repayment-adjustments/{pending.id}/reject",
                    json=body,
                    headers=_headers(checker_token, idem=f"nr-{uuid.uuid4().hex[:8]}"),
                )
                assert res.status_code == 422, (body, res.status_code)

        # Whitespace-only: the service's own defence-in-depth check.
        with pytest.raises(InvalidInputError, match="reason is required"):
            await _reject(tid, checker, pending.id, pending.version, reason="   ")

        # Zero side effects across all refusals.
        record = await _get(tid, pending.id)
        assert record.status is AdjustmentStatus.PENDING_APPROVAL
        assert record.checker_id is None
        async with tenant_session(factory(), tid) as session:
            audits = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'correction.adjustment_rejected'"
                    ),
                    {"tid": str(tid)},
                )
            ).scalar_one()
        assert int(audits) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 (N4) — repayments rows are append-only at the SQL level
# ---------------------------------------------------------------------------


def test_fm5_repayments_rows_are_append_only_at_the_sql_level() -> None:
    """The N4 forensic hole, closed: a direct-SQL UPDATE (the sign
    flip that would edit servicing history without touching the
    ledger) and DELETE both raise via the 0032 triggers. Falsifiable:
    drop repayments_no_update / repayments_no_delete and the probes
    land."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _repay(tid, actor, loan_id, "1000.00")

        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("UPDATE repayments SET amount = -amount"))
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("DELETE FROM repayments"))
        # The history survived the refused probes.
        async with tenant_session(factory(), tid) as session:
            amount = (
                await session.execute(
                    text("SELECT amount FROM repayments WHERE loan_id = CAST(:l AS uuid)"),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
        assert Decimal(str(amount)) == Decimal("1000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — terminal adjustment states never move again (0031 trigger)
# ---------------------------------------------------------------------------


def test_fm6_terminal_adjustment_states_never_move_via_sql() -> None:
    """Un-posting a POSTED adjustment, re-deciding a REJECTED one, and
    re-attributing a decided checker/decided_at all raise at the
    DATABASE (the regenerated 0031 write-once trigger). Falsifiable:
    drop the trigger's status-machine branch (or its one-shot fill
    branch) and the probes land."""

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker, _ = await _seed_extra_user(tid, "System Admin")
        other, _ = await _seed_extra_user(tid, "System Admin")

        # A POSTED adjustment through the real two-phase flow.
        _, pending = await _pending_for_new_repayment(tid, maker)
        await _approve(tid, checker, pending.id)

        for probe in (
            "UPDATE repayment_adjustments SET status = 'pending_approval' "
            "WHERE id = CAST(:id AS uuid)",
            "UPDATE repayment_adjustments SET status = 'rejected' WHERE id = CAST(:id AS uuid)",
        ):
            with pytest.raises(DBAPIError, match="cannot move"):
                async with tenant_session(factory(), tid) as session:
                    await session.execute(text(probe), {"id": str(pending.id)})

        # One-shot fields: re-attribution of a decided row raises.
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE repayment_adjustments SET checker_id = CAST(:u AS uuid) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"u": str(other), "id": str(pending.id)},
                )
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE repayment_adjustments SET decided_at = now() "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending.id)},
                )

        # A REJECTED adjustment can never become posted.
        _, pending2 = await _pending_for_new_repayment(tid, maker)
        await _reject(tid, checker, pending2.id, pending2.version)
        with pytest.raises(DBAPIError, match="cannot move"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE repayment_adjustments SET status = 'posted' "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(pending2.id)},
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — 7-role matrix on all four adjustment routes
# ---------------------------------------------------------------------------


def test_fm8_adjustment_routes_enforce_the_corrections_matrix_per_role() -> None:
    """Every role probes all four routes with synthetic ids: outcomes
    follow corrections:create / view / approve EXACTLY (a 404 on a
    permitted probe still proves authorisation passed). Falsifiable:
    gate any route with transactions:* and the Teller slips through."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await _seed_actor()  # seeds the full matrix once
        for role_name in ROLE_NAMES:
            _, token = await _seed_extra_user(tid, role_name)
            grants = matrix[role_name][Module.CORRECTIONS]
            probe = str(uuid.uuid4())
            async with api_client() as client:
                res = await client.post(
                    "/corrections/repayment-adjustments",
                    json={"repayment_id": probe, "reason": "matrix probe"},
                    headers=_headers(token, idem=f"rq-{uuid.uuid4().hex[:8]}"),
                )
                expected = 404 if grants[Action.CREATE] else 403
                assert res.status_code == expected, (role_name, "request", res.status_code)

                res = await client.get(
                    f"/corrections/repayment-adjustments/{probe}", headers=_headers(token)
                )
                expected = 404 if grants[Action.VIEW] else 403
                assert res.status_code == expected, (role_name, "view", res.status_code)

                res = await client.post(
                    f"/corrections/repayment-adjustments/{probe}/approval",
                    json={},
                    headers=_headers(token, idem=f"ap-{uuid.uuid4().hex[:8]}"),
                )
                expected = 404 if grants[Action.APPROVE] else 403
                assert res.status_code == expected, (role_name, "approval", res.status_code)

                res = await client.post(
                    f"/corrections/repayment-adjustments/{probe}/reject",
                    json={"version": 1, "reason": "matrix probe"},
                    headers=_headers(token, idem=f"rj-{uuid.uuid4().hex[:8]}"),
                )
                expected = 404 if grants[Action.APPROVE] else 403
                assert res.status_code == expected, (role_name, "reject", res.status_code)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — cross-tenant probes (issue-#17 pattern)
# ---------------------------------------------------------------------------


def test_fm9_cross_tenant_adjustments_are_invisible() -> None:
    async def run() -> None:
        tid_a, maker_a, _ = await _seed_actor()
        tid_b, _, token_b = await _seed_actor()
        _, pending = await _pending_for_new_repayment(tid_a, maker_a)

        async with tenant_session(factory(), tid_b) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM repayment_adjustments"))
            ).scalar_one()
            assert int(count) == 0
        async with api_client() as client:
            res = await client.get(
                f"/corrections/repayment-adjustments/{pending.id}", headers=_headers(token_b)
            )
            assert res.status_code == 404
            res = await client.post(
                f"/corrections/repayment-adjustments/{pending.id}/approval",
                json={},
                headers=_headers(token_b, idem=f"xa-{uuid.uuid4().hex[:8]}"),
            )
            assert res.status_code == 404
            res = await client.post(
                f"/corrections/repayment-adjustments/{pending.id}/reject",
                json={"version": 1, "reason": "foreign probe"},
                headers=_headers(token_b, idem=f"xr-{uuid.uuid4().hex[:8]}"),
            )
            assert res.status_code == 404
        # The foreign probes changed nothing.
        assert (await _get(tid_a, pending.id)).status is AdjustmentStatus.PENDING_APPROVAL

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM10 — idempotent replay by side-effect counts
# ---------------------------------------------------------------------------


def test_fm10_request_and_approval_replays_produce_one_effect() -> None:
    """Replays of BOTH mutations return the stored response and land
    exactly one effect, proven by row counts (adjustments, reversal
    transactions, repayments), never return values alone."""

    async def run() -> None:
        tid, admin_id, maker_token = await _seed_actor()
        _, checker_token = await _seed_extra_user(tid, "Branch Manager")
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, admin_id, loan_id, "1000.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)

        body = {"repayment_id": str(repayment_id), "reason": "replay proof"}
        rq_key = f"rq-{uuid.uuid4().hex[:10]}"
        async with api_client() as client:
            first = await client.post(
                "/corrections/repayment-adjustments",
                json=body,
                headers=_headers(maker_token, idem=rq_key),
            )
            assert first.status_code == 201, first.text
            adjustment_id = first.json()["id"]
            counts_after_request = await _money_counts(tid)

            replayed = await client.post(
                "/corrections/repayment-adjustments",
                json=body,
                headers=_headers(maker_token, idem=rq_key),
            )
            assert replayed.status_code == 201
            assert replayed.headers.get("idempotency-replayed") == "true"
            assert replayed.json()["id"] == adjustment_id
            assert await _money_counts(tid) == counts_after_request
            async with tenant_session(factory(), tid) as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM repayment_adjustments "
                            "WHERE repayment_id = CAST(:r AS uuid)"
                        ),
                        {"r": str(repayment_id)},
                    )
                ).scalar_one()
            assert int(rows) == 1  # exactly one claim row

            ap_key = f"ap-{uuid.uuid4().hex[:10]}"
            approved = await client.post(
                f"/corrections/repayment-adjustments/{adjustment_id}/approval",
                json={},
                headers=_headers(checker_token, idem=ap_key),
            )
            assert approved.status_code == 201, approved.text
            counts_after_approve = await _money_counts(tid)

            approved_replay = await client.post(
                f"/corrections/repayment-adjustments/{adjustment_id}/approval",
                json={},
                headers=_headers(checker_token, idem=ap_key),
            )
            assert approved_replay.status_code == 201
            assert approved_replay.headers.get("idempotency-replayed") == "true"
            # ONE reversal, ONE negative row — by counts, not payloads.
            assert await _money_counts(tid) == counts_after_approve

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Migration contracts: the 0031/0032 loud-refusal downgrade guards
# ---------------------------------------------------------------------------


def _load_migration(filename: str) -> Any:
    """The real revision module (not a copy — the 0017 falsifiability
    precedent)."""
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:4]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0031_downgrade_refuses_over_workflow_history() -> None:
    """The 0031 guard REFUSES while any adjustment is non-posted OR
    checker-attributed: a pending request refuses, and so does a row
    posted through the new two-phase flow (its checker attribution is
    the N1 control). Also pins _DOWN to start with the guard. Fails
    with the guard removed."""

    async def run() -> None:
        migration = _load_migration("0031_adjustment_maker_checker.py")
        assert migration._DOWN.startswith(migration._DOWN_GUARD)
        tid, maker, _ = await _seed_actor()
        checker, _ = await _seed_extra_user(tid, "System Admin")
        _, pending = await _pending_for_new_repayment(tid, maker)

        # Pending history refuses.
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

        # Posted-with-checker history refuses too.
        await _approve(tid, checker, pending.id)
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())


def test_migration_0032_downgrade_refuses_over_negative_correction_rows() -> None:
    """The 0032 guard REFUSES while a negative (storno) repayments row
    exists — exactly the forensic history the triggers protect; a
    tenant with only positive rows passes. Also pins _DOWN to start
    with the guard. Fails with the guard removed."""

    async def run() -> None:
        migration = _load_migration("0032_repayments_append_only.py")
        assert migration._DOWN.startswith(migration._DOWN_GUARD)
        tid, maker, _ = await _seed_actor()
        checker, _ = await _seed_extra_user(tid, "System Admin")
        loan_id, _ = await _disburse(tid)
        await _repay(tid, maker, loan_id, "1000.00")

        # Positive-only history: the guard passes (RLS scopes it to
        # this tenant's rows).
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))

        # A real correction writes the negative row -> the guard
        # refuses from then on.
        second = await _repay(tid, maker, loan_id, "500.00")
        repayment_id = await _repayment_id_for_txn(tid, second.txn_id)
        pending = await _request(tid, maker, repayment_id)
        await _approve(tid, checker, pending.id)
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())
