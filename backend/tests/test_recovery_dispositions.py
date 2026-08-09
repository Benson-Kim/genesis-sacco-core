"""Integration tests for issue #23: richer recovery-case dispositions
(N2) and post-closure outcome notes (N3) — the !47 review follow-ups.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Every numbered failure mode from the !53 MR description has a
falsifiable test here (remove the guard and the test fails), with
hand-computed oracles in comments:

  FM1  illegal disposition — pause-to-pause, terminal reopen and
       self-moves are refused by the ONE domain gatekeeper with zero
       side effects. Falsifiable: widen domain/recovery._ALLOWED and
       the refusal branches fail.
  FM2  staff hand-declared cure/write-off — closed_cured /
       closed_written_off are unreachable via the disposition route
       (code-owned STAFF_DISPOSITION_TARGETS). Falsifiable: add either
       to the staff set and the 409 assertions fail (open -> closed_*
       is LEGAL in the matrix, so only this guard stands between).
  FM3  auto-close semantics for the new statuses — a paused case
       (disputed / irrecoverable_pending_write_off) closes on the same
       loan facts as an open one; idempotent re-run by side-effect
       counts. Falsifiable: restore the close scan's status='open'
       predicate and the paused closes fail.
  FM4  live-case invariant — a paused case still blocks a second case
       for the same loan (0033 partial UNIQUE + matching ON CONFLICT
       claim). Falsifiable: restore the 0026 WHERE status='open'
       predicate and the double lands.
  FM5  outcome-note abuse — none on a live case; exactly ONE per
       closed case (atomic claim, concurrent double-add lands one by
       row count); regular notes still refused on terminal cases.
       Falsifiable: drop uq_recovery_notes_one_outcome and the
       concurrent count assertion fails (two rows land).
  FM6  paused case workability — assignment refused, worklist omits
       paused rows, regular notes still land (the dispute trail).
  FM7  stale version -> 409; Idempotency-Key replay -> one audit row
       (side-effect count, never return values alone).
  FM8  authz matrix — both new routes follow loan_book:edit exactly
       for every role (a 404 on a permitted synthetic probe proves
       authorisation passed).
  FM9  cross-tenant probe — the issue-#17 pattern on both new routes:
       404, zero rows, nothing changed.
  FM10 (!53 review F1) restructure close without justification — a
       closed_restructured disposition REQUIRES the outcome note and
       writes it ATOMICALLY in the same transaction; without it, 422
       with zero side effects; kill-switch abort -> zero partial
       state; strict field-target pairing. Falsifiable: make the
       service's outcome-note gate optional (the 422s vanish) or
       write the note outside the transaction (the kill-switch
       count assertions fail).
  FM11 (!53 review F2) pause without rationale — disputed /
       irrecoverable_pending_write_off REQUIRE a reason, captured
       into the audit `after` payload (never the outbox payload).
       Falsifiable: drop the PAUSE_STATUSES gate or the audit carry.

Plus the 0033 loud-refusal downgrade guard executed from the REAL
migration module (the 0017 _DOWN_GUARD falsifiability precedent).

Hand-computed dpd oracle (UTC calendar dates, the P10 derivation):
  due 2026-01-01, as_of 2026-06-01 -> dpd = 31+28+31+30+31 = 151
    -> substandard (91..180, NPL)
  after paying the instalment: no unpaid instalment due on/before
    as_of -> dpd = 0 -> normal (cure).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor
from genesis.application import recovery as recovery_service
from genesis.application.arrears import run_arrears_for_tenant
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.domain.recovery import RecoveryCaseStatus
from genesis.errors import ConflictError, UnprocessableError
from genesis.infrastructure.tenancy import tenant_session
from test_penalty_accrual import _scope
from test_recovery_cases import AS_OF, _case_row, _mark_schedule_paid, _npl_loan, _open_case_api

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str, idem: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


class _KillSwitchError(RuntimeError):
    """Simulated crash after the service ran, before commit."""


async def _dispose(
    token: str,
    case_id: str,
    version: int,
    status: str,
    *,
    idem: str | None = None,
    reason: str | None = None,
    note: str | None = None,
) -> Any:
    """POST the disposition. `reason`/`note` are passed EXPLICITLY by
    each test (!53 F1/F2) — the helper never auto-supplies them, so a
    call site documents exactly what the requirement under test is."""
    payload: dict[str, Any] = {"version": version, "status": status}
    if reason is not None:
        payload["reason"] = reason
    if note is not None:
        payload["note"] = note
    async with api_client() as client:
        return await client.post(
            f"/recovery-cases/{case_id}/disposition",
            json=payload,
            headers=_headers(token, idem),
        )


async def _disposition_audit_count(tid: uuid.UUID, case_id: str) -> int:
    return await count(
        tid,
        "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:tenant AS uuid) "
        "AND action = 'recovery_case.disposition_set' AND entity_id = :eid",
        tenant=str(tid),
        eid=case_id,
    )


async def _outcome_note_count(tid: uuid.UUID, case_id: str) -> int:
    return await count(
        tid,
        "SELECT count(*) FROM recovery_case_notes "
        "WHERE tenant_id = CAST(:tenant AS uuid) "
        "AND case_id = CAST(:cid AS uuid) AND is_outcome",
        tenant=str(tid),
        cid=case_id,
    )


# ---------------------------------------------------------------------------
# FM1 — illegal dispositions refused with zero side effects
# ---------------------------------------------------------------------------


def test_fm1_illegal_dispositions_refused_with_zero_side_effects() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # Legal: open -> disputed (version 1 -> 2). !53 F2: pauses
        # carry a required reason.
        res = await _dispose(token, case["id"], 1, "disputed", reason="member contests arrears")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "disputed"
        assert res.json()["closed_at"] is None  # live pause, not a close

        # Illegal pause-to-pause: disputed -> irrecoverable_pending_
        # write_off must go through open (two auditable steps). A
        # valid reason is supplied so the 409 comes from the ONE
        # gatekeeper, not the input-shape gate.
        res = await _dispose(
            token,
            case["id"],
            2,
            "irrecoverable_pending_write_off",
            reason="pause-to-pause probe",
        )
        assert res.status_code == 409, res.text
        # Illegal self-move.
        res = await _dispose(token, case["id"], 2, "disputed", reason="self-move probe")
        assert res.status_code == 409, res.text
        # Zero side effects: status/version unchanged, exactly ONE
        # disposition audit row (the legal one).
        row = await _case_row(tid, case["id"])
        assert row[0] == "disputed"
        assert row[3] == 2
        assert await _disposition_audit_count(tid, case["id"]) == 1

        # Legal: disputed -> closed_restructured (terminal, closed_at
        # server-side). !53 F1: the close carries its outcome note.
        res = await _dispose(
            token, case["id"], 2, "closed_restructured", note="restructured per minute 7"
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "closed_restructured"
        assert res.json()["closed_at"] is not None

        # Illegal terminal reopen.
        res = await _dispose(token, case["id"], 3, "open")
        assert res.status_code == 409, res.text
        assert (await _case_row(tid, case["id"]))[0] == "closed_restructured"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — job-only loan-fact closes are not staff-settable
# ---------------------------------------------------------------------------


def test_fm2_job_only_closes_are_not_staff_settable() -> None:
    """open -> closed_cured / closed_written_off ARE legal in the
    transition matrix (the auto-close pass uses them), so ONLY the
    STAFF_DISPOSITION_TARGETS guard stands between a staff caller and
    a hand-declared cure — remove it and both 409s become 200s."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        for target in ("closed_cured", "closed_written_off"):
            res = await _dispose(token, case["id"], 1, target)
            assert res.status_code == 409, (target, res.text)
        # Zero side effects: still open, version 1, no disposition
        # audit rows.
        row = await _case_row(tid, case["id"])
        assert row[0] == "open"
        assert row[3] == 1
        assert await _disposition_audit_count(tid, case["id"]) == 0

        # An unknown status never reaches the service (enum at the
        # boundary, 422).
        res = await _dispose(token, case["id"], 1, "settled_by_carrier_pigeon")
        assert res.status_code == 422, res.text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — auto-close semantics for the new statuses (loan facts end a pause)
# ---------------------------------------------------------------------------


def test_fm3_paused_cases_auto_close_on_loan_facts() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()

        # irrecoverable_pending_write_off + loan written off -> the
        # expected path closes as closed_written_off.
        wo_loan = await _npl_loan(tid)
        wo_case = await _open_case_api(token, wo_loan)
        res = await _dispose(
            token,
            wo_case["id"],
            1,
            "irrecoverable_pending_write_off",
            reason="recovery concluded impossible",
        )
        assert res.status_code == 200, res.text
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET status = 'written_off' "
                    "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"lid": str(wo_loan), "tid": str(tid)},
            )
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_written_off == 1
        row = await _case_row(tid, wo_case["id"])
        assert row[0] == "closed_written_off"
        assert row[1] is not None  # closed_at server-side (A7)

        # disputed + cure -> money talks: the pause ends closed_cured.
        # dpd oracle: instalment due 2026-01-01 fully paid -> dpd 0 ->
        # 'normal' -> left NPL_CLASSES -> cure.
        cure_loan = await _npl_loan(tid)
        cure_case = await _open_case_api(token, cure_loan)
        res = await _dispose(token, cure_case["id"], 1, "disputed", reason="member disputes")
        assert res.status_code == 200
        await _mark_schedule_paid(tid, cure_loan, paid=True)
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 1
        assert (await _case_row(tid, cure_case["id"]))[0] == "closed_cured"

        # Idempotent re-run: nothing scanned into a new close
        # (side-effect counts — audit rows stay at one per case).
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 0
        assert result.cases_closed_written_off == 0

        # A pause with NO warranting loan facts stays paused: dpd 151
        # (substandard, still NPL) -> the scan must not close it.
        hold_loan = await _npl_loan(tid)
        hold_case = await _open_case_api(token, hold_loan)
        res = await _dispose(token, hold_case["id"], 1, "disputed", reason="member disputes")
        assert res.status_code == 200
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 0
        assert (await _case_row(tid, hold_case["id"]))[0] == "disputed"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — the live-case invariant blocks a second case behind a pause
# ---------------------------------------------------------------------------


def test_fm4_live_case_invariant_blocks_second_open() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)
        res = await _dispose(token, case["id"], 1, "disputed", reason="member disputes")
        assert res.status_code == 200

        # The paused case still claims the loan: a second open is 409.
        async with api_client() as client:
            res = await client.post(
                "/recovery-cases",
                json={"loan_id": str(loan_id)},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text
        rows = await count(
            tid,
            "SELECT count(*) FROM recovery_cases WHERE tenant_id = CAST(:tenant AS uuid) "
            "AND loan_id = CAST(:lid AS uuid)",
            tenant=str(tid),
            lid=str(loan_id),
        )
        assert rows == 1

        # DB backstop: a direct-SQL second LIVE row violates the 0033
        # partial UNIQUE (falsifiable: restore the 0026 status='open'
        # predicate and this INSERT succeeds).
        with pytest.raises(IntegrityError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO recovery_cases (id, tenant_id, loan_id, opened_by, "
                        "classification_at_open, days_past_due_at_open) "
                        "VALUES (gen_random_uuid(), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                        "CAST(:actor AS uuid), 'substandard', 151)"
                    ),
                    {"tid": str(tid), "lid": str(loan_id), "actor": str(admin_id)},
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — exactly ONE outcome note, at/after closure only
# ---------------------------------------------------------------------------


def test_fm5_exactly_one_outcome_note_at_or_after_closure() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # A live case takes no outcome note (open AND paused).
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/outcome-note",
                json={"note": "premature"},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text
        res = await _dispose(token, case["id"], 1, "disputed", reason="member disputes")
        assert res.status_code == 200
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/outcome-note",
                json={"note": "still premature"},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text

        # Close (disputed -> closed_restructured): !53 F1 — the close
        # itself writes THE outcome note atomically, so the case is
        # born closed WITH its single note.
        res = await _dispose(
            token,
            case["id"],
            2,
            "closed_restructured",
            note="restructured per committee minute 7",
        )
        assert res.status_code == 200
        assert await _outcome_note_count(tid, case["id"]) == 1

        # A second outcome note through the API: 409, count stays 1.
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/outcome-note",
                json={"note": "second outcome"},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text
        assert await _outcome_note_count(tid, case["id"]) == 1

        # The standalone route still serves JOB-closed cases, and the
        # atomic claim still lands EXACTLY one row under a race
        # (side-effect count — falsifiable by dropping
        # uq_recovery_notes_one_outcome): close a second case via the
        # arrears job (closed_cured) and race two outcome notes.
        cure_loan = await _npl_loan(tid)
        cure_case = await _open_case_api(token, cure_loan)
        await _mark_schedule_paid(tid, cure_loan, paid=True)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert (await _case_row(tid, cure_case["id"]))[0] == "closed_cured"

        async def attempt(text_note: str) -> str:
            async with tenant_session(factory(), tid) as session:
                record = await recovery_service.add_outcome_note(
                    session, tid, admin_id, case_id=uuid.UUID(cure_case["id"]), note=text_note
                )
            return str(record.id)

        results = await asyncio.gather(
            attempt("cured after restructure call"),
            attempt("duplicate outcome"),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, str)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        assert len(successes) == 1, results
        assert len(conflicts) == 1, results
        assert await _outcome_note_count(tid, cure_case["id"]) == 1

        # Regular notes still refuse on a terminal case (A2 strictness
        # intact) and the listing surfaces the outcome flag.
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "regular after close"},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text
            res = await client.get(f"/recovery-cases/{case['id']}/notes", headers=_headers(token))
            assert res.status_code == 200
            flags = [item["is_outcome"] for item in res.json()["items"]]
            assert flags.count(True) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM10 (!53 F1) — closed_restructured requires the outcome note ATOMICALLY
# ---------------------------------------------------------------------------


def test_fm10_closed_restructured_requires_the_outcome_note_atomically() -> None:
    """!53 review F1, all four directions:
    (a) closed_restructured WITHOUT a note -> 422 with ZERO side
        effects (status/version untouched, no disposition audit, no
        note row). Falsifiable: make the service's outcome-note gate
        optional and the 422 becomes a 200.
    (b) the atomic path lands BY SIDE-EFFECT COUNTS: ONE status write
        (one version bump), ONE outcome-note row, ONE disposition
        audit row carrying the note id, ONE outcome-note audit row —
        all from ONE call.
    (c) kill-switch: abort after the service ran, before commit ->
        ZERO partial state (no status change, no note, no audits).
        Falsifiable: write the note outside the transaction and the
        abort leaves it behind.
    (d) strict field-target pairing: a note on a pause, a reason on
        the restructure close, and a whitespace-only note are 422s.
    """

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # (a) no note -> 422, zero side effects.
        res = await _dispose(token, case["id"], 1, "closed_restructured")
        assert res.status_code == 422, res.text
        row = await _case_row(tid, case["id"])
        assert row[0] == "open"
        assert row[3] == 1
        assert await _disposition_audit_count(tid, case["id"]) == 0
        assert await _outcome_note_count(tid, case["id"]) == 0

        # (d) mismatched pairings are 422s too, still zero side effects.
        res = await _dispose(token, case["id"], 1, "disputed", reason="r", note="stray note")
        assert res.status_code == 422, res.text
        res = await _dispose(token, case["id"], 1, "closed_restructured", note="n", reason="r")
        assert res.status_code == 422, res.text
        res = await _dispose(token, case["id"], 1, "open", reason="stray reason")
        assert res.status_code == 422, res.text
        with pytest.raises(UnprocessableError, match="outcome note is required"):
            async with tenant_session(factory(), tid) as session:
                await recovery_service.set_case_disposition(
                    session,
                    tid,
                    admin_id,
                    case_id=uuid.UUID(case["id"]),
                    version=1,
                    target=RecoveryCaseStatus.CLOSED_RESTRUCTURED,
                    outcome_note="   ",
                )
        row = await _case_row(tid, case["id"])
        assert row[0] == "open"
        assert row[3] == 1
        assert await _disposition_audit_count(tid, case["id"]) == 0

        # (c) kill-switch mid-call: the service runs fully (status
        # write + note claim + audits), then the transaction aborts —
        # zero partial state may remain.
        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await recovery_service.set_case_disposition(
                    session,
                    tid,
                    admin_id,
                    case_id=uuid.UUID(case["id"]),
                    version=1,
                    target=RecoveryCaseStatus.CLOSED_RESTRUCTURED,
                    outcome_note="aborted close",
                )
                raise _KillSwitchError()  # crash inside the transaction
        row = await _case_row(tid, case["id"])
        assert row[0] == "open"
        assert row[1] is None  # no closed_at
        assert row[3] == 1
        assert await _disposition_audit_count(tid, case["id"]) == 0
        assert await _outcome_note_count(tid, case["id"]) == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:tenant AS uuid) "
                "AND action = 'recovery_case.outcome_note_added' AND entity_id = :eid",
                tenant=str(tid),
                eid=case["id"],
            )
            == 0
        )

        # (b) the atomic path: ONE call -> exactly one of everything.
        res = await _dispose(
            token, case["id"], 1, "closed_restructured", note="restructured per minute 7"
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "closed_restructured"
        assert res.json()["closed_at"] is not None
        assert res.json()["version"] == 2  # exactly one version bump
        assert await _outcome_note_count(tid, case["id"]) == 1
        assert await _disposition_audit_count(tid, case["id"]) == 1
        outcome_audits = await count(
            tid,
            "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:tenant AS uuid) "
            "AND action = 'recovery_case.outcome_note_added' AND entity_id = :eid",
            tenant=str(tid),
            eid=case["id"],
        )
        assert outcome_audits == 1
        # The disposition audit row links THE note it wrote.
        async with tenant_session(factory(), tid) as session:
            linked = (
                await session.execute(
                    text(
                        "SELECT a.after->>'outcome_note_id' FROM audit_log a "
                        "WHERE a.tenant_id = CAST(:tid AS uuid) "
                        "AND a.action = 'recovery_case.disposition_set' "
                        "AND a.entity_id = :eid"
                    ),
                    {"tid": str(tid), "eid": case["id"]},
                )
            ).scalar_one()
            note_id = (
                await session.execute(
                    text(
                        "SELECT id FROM recovery_case_notes "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND case_id = CAST(:cid AS uuid) AND is_outcome"
                    ),
                    {"tid": str(tid), "cid": case["id"]},
                )
            ).scalar_one()
        assert linked == str(note_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM11 (!53 F2) — pause dispositions require a reason, on the audit record
# ---------------------------------------------------------------------------


def test_fm11_pause_without_reason_is_422_and_reason_lands_in_audit() -> None:
    """!53 review F2: pausing without a reason (BOTH pause targets) is
    a 422 with zero side effects; the supplied reason lands in the
    disposition audit `after` payload and NOT in the outbox payload
    (least-payload posture); resuming to open needs nothing.
    Falsifiable: drop the PAUSE_STATUSES gate (the 422s vanish) or the
    audit carry (the payload assertion fails)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        for target in ("disputed", "irrecoverable_pending_write_off"):
            res = await _dispose(token, case["id"], 1, target)
            assert res.status_code == 422, (target, res.text)
        row = await _case_row(tid, case["id"])
        assert row[0] == "open"
        assert row[3] == 1
        assert await _disposition_audit_count(tid, case["id"]) == 0

        # With the reason: 200, and the rationale is on the audit
        # record — not in the outbox payload.
        res = await _dispose(token, case["id"], 1, "disputed", reason="member contests the arrears")
        assert res.status_code == 200, res.text
        async with tenant_session(factory(), tid) as session:
            audit_reason = (
                await session.execute(
                    text(
                        "SELECT after->>'reason' FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'recovery_case.disposition_set' "
                        "AND entity_id = :eid"
                    ),
                    {"tid": str(tid), "eid": case["id"]},
                )
            ).scalar_one()
            outbox_row = (
                await session.execute(
                    text(
                        "SELECT payload->>'to', jsonb_exists(payload, 'reason') "
                        "FROM outbox_events "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND event_type = 'recovery_case.disposition_set'"
                    ),
                    {"tid": str(tid)},
                )
            ).one()
        assert audit_reason == "member contests the arrears"
        assert str(outbox_row[0]) == "disputed"
        assert outbox_row[1] is False  # the rationale never rides the outbox

        # Resume needs no rationale fields.
        assert (await _dispose(token, case["id"], 2, "open")).status_code == 200

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — paused cases are not workable (but their trail continues)
# ---------------------------------------------------------------------------


def test_fm6_paused_cases_are_not_workable() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        officer_id, _ = await add_user(tid, "Loan Officer")
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)
        res = await _dispose(
            token,
            case["id"],
            1,
            "irrecoverable_pending_write_off",
            reason="recovery concluded impossible",
        )
        assert res.status_code == 200, res.text

        async with api_client() as client:
            # Assignment refused on a paused case (open-only).
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 2, "assignee_id": str(officer_id)},
                headers=_headers(token),
            )
            assert res.status_code == 409, res.text
            # The workable worklist omits paused rows.
            res = await client.get("/recovery-cases", headers=_headers(token))
            assert res.status_code == 200
            assert case["id"] not in [row["case_id"] for row in res.json()["items"]]
            # The live trail continues: a regular note lands.
            res = await client.post(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "officer memo while pending write-off"},
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            assert res.json()["is_outcome"] is False

        # Resume: back in the worklist.
        assert (await _dispose(token, case["id"], 2, "open")).status_code == 200
        async with api_client() as client:
            res = await client.get("/recovery-cases", headers=_headers(token))
            assert case["id"] in [row["case_id"] for row in res.json()["items"]]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — stale version 409; Idempotency-Key replay is one effect
# ---------------------------------------------------------------------------


def test_fm7_stale_version_and_replay_are_safe() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # Stale version -> 409, nothing moved (the reason is valid so
        # the refusal is the optimistic lock, not the shape gate).
        res = await _dispose(token, case["id"], 99, "disputed", reason="member disputes")
        assert res.status_code == 409, res.text
        assert (await _case_row(tid, case["id"]))[0] == "open"

        # Replay: same Idempotency-Key returns the stored response and
        # lands ONE audit row (side-effect count).
        key = f"disp-{uuid.uuid4()}"
        first = await _dispose(token, case["id"], 1, "disputed", idem=key, reason="disputes")
        assert first.status_code == 200, first.text
        replay = await _dispose(token, case["id"], 1, "disputed", idem=key, reason="disputes")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert await _disposition_audit_count(tid, case["id"]) == 1
        assert (await _case_row(tid, case["id"]))[3] == 2  # one version bump

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — authz matrix on the two new routes (per role)
# ---------------------------------------------------------------------------


def test_fm8_new_routes_enforce_the_loan_book_matrix_per_role() -> None:
    """Synthetic-id probes: a 404 on a permitted probe still proves
    authorisation passed; 403 proves deny-by-default. Falsifiable:
    gate either route with a broader module and the Teller slips
    through."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await seed_actor()
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            can_edit = matrix[role_name][Module.LOAN_BOOK][Action.EDIT]
            probe = str(uuid.uuid4())
            expected = 404 if can_edit else 403
            res = await _dispose(token, probe, 1, "disputed", reason="matrix probe")
            assert res.status_code == expected, (role_name, "disposition", res.status_code)
            async with api_client() as client:
                res = await client.post(
                    f"/recovery-cases/{probe}/outcome-note",
                    json={"note": "matrix probe"},
                    headers=_headers(token),
                )
                assert res.status_code == expected, (role_name, "outcome", res.status_code)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — cross-tenant probes (issue-#17 pattern)
# ---------------------------------------------------------------------------


def test_fm9_cross_tenant_disposition_probes_are_invisible() -> None:
    async def run() -> None:
        tid_a, _, token_a = await seed_actor()
        _tid_b, _, token_b = await seed_actor()
        loan_id = await _npl_loan(tid_a)
        case = await _open_case_api(token_a, loan_id)

        res = await _dispose(token_b, case["id"], 1, "disputed", reason="foreign probe")
        assert res.status_code == 404, res.text
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/outcome-note",
                json={"note": "foreign"},
                headers=_headers(token_b),
            )
            assert res.status_code == 404, res.text
        # The foreign probes changed nothing.
        row = await _case_row(tid_a, case["id"])
        assert row[0] == "open"
        assert row[3] == 1
        assert await _outcome_note_count(tid_a, case["id"]) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Migration 0033 — loud-refusal downgrade guard (the 0017 precedent)
# ---------------------------------------------------------------------------


def _load_migration(filename: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:4]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0033_downgrade_refuses_over_disposition_history() -> None:
    """The 0033 guard REFUSES while any case holds a new status or any
    outcome note exists — exactly the history restoring the 0026
    shapes would rewrite; a tenant with only 0026-era rows passes.
    Also pins _DOWN to start with the guard. Fails with the guard
    removed."""

    async def run() -> None:
        migration = _load_migration("0033_recovery_case_dispositions.py")
        assert migration._DOWN.startswith(migration._DOWN_GUARD)
        tid, admin_id, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # 0026-era shape (an open case only): the guard passes (RLS
        # scopes it to this tenant's rows).
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))

        # A disposition writes new-status history -> refuses.
        res = await _dispose(token, case["id"], 1, "disputed", reason="member disputes")
        assert res.status_code == 200
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

        # Even resumed back to open (no new-status rows remain), an
        # OUTCOME NOTE alone still refuses.
        assert (await _dispose(token, case["id"], 2, "open")).status_code == 200
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))  # passes again
        res = await _dispose(
            token, case["id"], 3, "closed_restructured", note="restructured per minute 7"
        )
        assert res.status_code == 200
        # closed_restructured is itself a new status -> refuses; the
        # outcome-note branch is proven on a job-closed case below.
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

        # Outcome-note branch in isolation: a 0026-legal close
        # (closed_cured via the arrears job) + an outcome note.
        cure_loan = await _npl_loan(tid)
        cure_case = await _open_case_api(token, cure_loan)
        await _mark_schedule_paid(tid, cure_loan, paid=True)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert (await _case_row(tid, cure_case["id"]))[0] == "closed_cured"
        async with tenant_session(factory(), tid) as session:
            await recovery_service.add_outcome_note(
                session,
                tid,
                admin_id,
                case_id=uuid.UUID(cure_case["id"]),
                note="cured after restructure call",
            )
        # The restructured case above still trips the status branch;
        # this asserts the guard text names both conditions and the
        # outcome rows alone would refuse (two rows now exist: the !53
        # F1 atomic-close note on the restructured case + the
        # job-closed case's note added above).
        exists = await count(
            tid,
            "SELECT count(*) FROM recovery_case_notes "
            "WHERE tenant_id = CAST(:tenant AS uuid) AND is_outcome",
            tenant=str(tid),
        )
        assert exists == 2
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())
