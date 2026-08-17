"""Integration tests for P13.16: collections & recovery worklist.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Every numbered failure mode from BUILD_PROMPTS P13.16 has a falsifiable
test here (remove the guard and the test fails), with hand-computed
oracles in comments:

  FM1  open-on-performing — a case opens only for an NPL-classified
       active loan, checked under the loan row lock; the 0026 DB CHECKs
       are probed directly (an open-on-performing row is
       unrepresentable). Falsifiable: drop the classification check in
       open_recovery_case (or the DB CHECK) and the refusal test fails.
  FM2  duplicate case — concurrent double-open lands EXACTLY one row via
       the partial UNIQUE claimed ON CONFLICT DO NOTHING + rowcount
       (v1.1 rule 5). Falsifiable: drop uq_recovery_cases_one_open and
       the row-count assertion fails (two rows land).
  FM3  close-on-cure exactly-once — the arrears job auto-closes on cure
       idempotently (re-run closes nothing new, proven by side-effect
       counts: audit rows + closed counters), keyed off the job's own
       persisted classification; a loan re-entering NPL after a cure
       gets a NEW case (history preserved). Write-off and full-closure
       outcomes are covered as-built.
  FM4  assignment to a suspended/foreign/unentitled user refused; a user
       suspended AFTER assignment is flagged unassignable in the
       worklist (never silently orphaned); reassignment audited with
       before/after; first_assigned_at is server-side and write-once.
       Review B2: an assurance-role (Auditor) assignee is refused for
       audit-independence even though it holds loan_book:view —
       falsifiable, the guard alone produces the 409.
  FM5  cross-tenant probe — the issue-#17 pattern on every new route AND
       the tenant-predicate falsifiability probe (session AS the row's
       own tenant, foreign tenant argument -> 404/zero rows).

Hand-computed dpd oracles (UTC calendar dates, the P10 derivation):
  due 2026-01-01, as_of 2026-06-01 -> dpd = 31+28+31+30+31 = 151
    -> substandard (91..180, NPL)
  due 2025-12-01 -> dpd = 31 + 151 = 182 -> doubtful (181..360, NPL)
  due 2025-01-01 -> dpd = 365 + 151 = 516 -> loss (>360, NPL)

Idempotency and exactly-once are asserted via side-effect row counts
(cases, audit rows), never via return values alone (MASTER_PROMPT §4).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory, seed_user, unique_email
from export_helpers import add_user, count, seed_actor
from genesis.application import recovery as recovery_service
from genesis.application.arrears import run_arrears_for_tenant
from genesis.domain.rbac import AUDITOR, ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import ConflictError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session
from test_penalty_accrual import _disburse, _pin_first_instalment, _scope

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

AS_OF = date(2026, 6, 1)


async def _npl_loan(tid: uuid.UUID, *, due: date = date(2026, 1, 1)) -> uuid.UUID:
    """Disburse a real loan, pin instalment 1 past due, classify via the
    REAL arrears job (the single source of truth, 1.1)."""
    loan_id = await _disburse(tid, Decimal("24000.00"))
    await _pin_first_instalment(tid, loan_id, due=due)
    await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
    return loan_id


async def _open_case_api(token: str, loan_id: uuid.UUID) -> dict[str, Any]:
    async with api_client() as client:
        res = await client.post(
            "/recovery-cases",
            json={"loan_id": str(loan_id)},
            headers={"authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201, res.text
        return dict(res.json())


async def _mark_schedule_paid(tid: uuid.UUID, loan_id: uuid.UUID, *, paid: bool) -> None:
    """Flip the repayment bookkeeping the arrears scan reads (the same
    column record_repayment maintains under the loan row lock)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loan_schedules SET paid_amount = "
                "CASE WHEN :paid THEN total_due ELSE 0 END "
                "WHERE loan_id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"paid": paid, "lid": str(loan_id), "tid": str(tid)},
        )


async def _case_row(tid: uuid.UUID, case_id: str) -> Any:
    async with tenant_session(factory(), tid) as session:
        return (
            await session.execute(
                text(
                    "SELECT status, closed_at, first_assigned_at, version "
                    "FROM recovery_cases WHERE id = CAST(:cid AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"cid": case_id, "tid": str(tid)},
            )
        ).first()


async def _closed_audit_count(tid: uuid.UUID, case_id: str) -> int:
    # The bind parameter is named :tenant (not :tid) because the shared
    # count() helper takes the tenant id as its first positional
    # parameter, also named tid — a same-named bind kwarg collides.
    return await count(
        tid,
        "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:tenant AS uuid) "
        "AND action = 'recovery_case.closed' AND entity_id = :eid",
        tenant=str(tid),
        eid=case_id,
    )


# ---------------------------------------------------------------------------
# FM1 — open-on-performing refused; NPL open records the locked snapshot
# ---------------------------------------------------------------------------


def test_fm1_open_only_for_npl_loans() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()

        # A fresh loan is 'normal' (dpd 0): open must refuse with a
        # least-disclosure envelope (category only — no dpd figure, no
        # classification, no balance).
        performing = await _disburse(tid, Decimal("24000.00"))
        async with api_client() as client:
            res = await client.post(
                "/recovery-cases",
                json={"loan_id": str(performing)},
                headers={"authorization": f"Bearer {token}"},
            )
            assert res.status_code == 409, res.text
            body = res.json()
            assert set(body) == {"category", "correlation_id"}

        # Oracle: due 2026-01-01, as_of 2026-06-01 -> dpd 151 ->
        # substandard (NPL). The open snapshot copies the values read
        # under the loan row lock.
        npl = await _npl_loan(tid)
        case = await _open_case_api(token, npl)
        assert case["status"] == "open"
        assert case["classification_at_open"] == "substandard"
        assert case["days_past_due_at_open"] == 151
        assert case["opened_at"] is not None  # server-side (A7)
        assert case["closed_at"] is None
        assert case["first_assigned_at"] is None

        # DB backstop (0026 CHECKs): an open-on-performing row is
        # unrepresentable even by direct SQL.
        for cls, dpd in (("normal", 151), ("substandard", 90)):
            with pytest.raises(DBAPIError):
                async with tenant_session(factory(), tid) as session:
                    await session.execute(
                        text(
                            "INSERT INTO recovery_cases (tenant_id, loan_id, opened_by, "
                            "classification_at_open, days_past_due_at_open) "
                            "VALUES (CAST(:tid AS uuid), CAST(:lid AS uuid), "
                            "(SELECT id FROM users LIMIT 1), :cls, :dpd)"
                        ),
                        {"tid": str(tid), "lid": str(performing), "cls": cls, "dpd": dpd},
                    )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — concurrent double-open lands exactly one row
# ---------------------------------------------------------------------------


def test_fm2_concurrent_double_open_exactly_one_case() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        loan_id = await _npl_loan(tid)

        async def attempt() -> str:
            async with tenant_session(factory(), tid) as session:
                record = await recovery_service.open_recovery_case(
                    session, tid, admin_id, loan_id=loan_id
                )
            return str(record.id)

        results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
        successes = [r for r in results if isinstance(r, str)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        assert len(successes) == 1, results
        assert len(conflicts) == 1, results
        # Side-effect count is the oracle (never return values alone):
        # exactly ONE row landed. Falsifiable: drop
        # uq_recovery_cases_one_open and both inserts land (count 2).
        rows = await count(
            tid,
            "SELECT count(*) FROM recovery_cases WHERE tenant_id = CAST(:tenant AS uuid) "
            "AND loan_id = CAST(:lid AS uuid)",
            tenant=str(tid),
            lid=str(loan_id),
        )
        assert rows == 1

        # Sequential duplicate through the API: same atomic claim, 409.
        async with api_client() as client:
            res = await client.post(
                "/recovery-cases",
                json={"loan_id": str(loan_id)},
                headers={"authorization": f"Bearer {(await _admin_token(tid))}"},
            )
            assert res.status_code == 409

    asyncio.run(run())


async def _admin_token(tid: uuid.UUID) -> str:
    _, token = await add_user(tid, "System Admin")
    return token


# ---------------------------------------------------------------------------
# FM3 — auto-close on cure, exactly once; new case after re-NPL;
#        write-off and full-closure outcomes
# ---------------------------------------------------------------------------


def test_fm3_auto_close_on_cure_exactly_once_and_new_case_after_renpl() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)

        # Still NPL: a re-run closes nothing (idempotent by counts).
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 0
        assert result.cases_closed_written_off == 0
        assert (await _case_row(tid, case["id"]))[0] == "open"

        # Cure: the instalment is fully paid, so the SAME run's classify
        # pass computes dpd 0 -> 'normal' (hand-computed: no unpaid
        # instalment due on/before as_of) and the close pass — reading
        # the classification THIS run just persisted — closes the case.
        await _mark_schedule_paid(tid, loan_id, paid=True)
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 1
        assert result.cases_closed_written_off == 0
        row = await _case_row(tid, case["id"])
        assert row[0] == "closed_cured"
        assert row[1] is not None  # closed_at server-side (A7)
        assert await _closed_audit_count(tid, case["id"]) == 1

        # Exactly-once: the re-run scans nothing and closes nothing new
        # (side-effect counts, not return values).
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 0
        assert await _closed_audit_count(tid, case["id"]) == 1

        # Re-entry into NPL gets a NEW case; the cured one is history
        # (terminal state, addendum A6).
        await _mark_schedule_paid(tid, loan_id, paid=False)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        second = await _open_case_api(token, loan_id)
        assert second["id"] != case["id"]
        rows = await count(
            tid,
            "SELECT count(*) FROM recovery_cases WHERE tenant_id = CAST(:tenant AS uuid) "
            "AND loan_id = CAST(:lid AS uuid)",
            tenant=str(tid),
            lid=str(loan_id),
        )
        assert rows == 2
        assert (await _case_row(tid, case["id"]))[0] == "closed_cured"

    asyncio.run(run())


def test_fm3_write_off_and_full_closure_outcomes() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()

        # written_off: the status exists since 0001 but is unreachable
        # through any service until P13.15 — set directly, the as-built
        # seam this MR documents in !47. The close pass must record the
        # write-off outcome, NOT a cure.
        wo_loan = await _npl_loan(tid)
        wo_case = await _open_case_api(token, wo_loan)
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
        assert (await _case_row(tid, wo_case["id"]))[0] == "closed_written_off"

        # A fully-closed loan (repaid/settled) is definitionally cured.
        cl_loan = await _npl_loan(tid, due=date(2025, 12, 1))
        cl_case = await _open_case_api(token, cl_loan)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET status = 'closed', balance = 0 "
                    "WHERE id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"lid": str(cl_loan), "tid": str(tid)},
            )
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.cases_closed_cured == 1
        assert (await _case_row(tid, cl_case["id"]))[0] == "closed_cured"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — assignment safety (addendum A4) + SLA timestamps (A7)
# ---------------------------------------------------------------------------


def test_fm4_assignment_guards_and_unassignable_flag() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)
        headers = {"authorization": f"Bearer {token}"}

        officer_id, _ = await add_user(tid, "Loan Officer")  # loan_book:view -> assignable
        teller_id, _ = await add_user(tid, "Teller")  # no loan_book grant
        suspended_id, _ = await add_user(tid, "Loan Officer")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE users SET status = 'suspended' "
                    "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"uid": str(suspended_id), "tid": str(tid)},
            )
        foreign_tid, _ = await seed_user(unique_email())
        foreign_user, _ = await add_user(foreign_tid, "System Admin")

        async with api_client() as client:
            # Refusals: suspended, unentitled, foreign (404 — zero
            # disclosure), stale version.
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(suspended_id)},
                headers=headers,
            )
            assert res.status_code == 409
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(teller_id)},
                headers=headers,
            )
            assert res.status_code == 409
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(foreign_user)},
                headers=headers,
            )
            assert res.status_code == 404
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 7, "assignee_id": str(officer_id)},
                headers=headers,
            )
            assert res.status_code == 409

            # Valid assignment: first_assigned_at is server-side (A7).
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(officer_id)},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assigned = res.json()
            assert assigned["assignee_id"] == str(officer_id)
            assert assigned["first_assigned_at"] is not None
            first_assigned_at = assigned["first_assigned_at"]

            # Reassignment is audited with before/after and does NOT
            # rewrite first_assigned_at (write-once SLA timestamp).
            officer2_id, _ = await add_user(tid, "Loan Officer")
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": assigned["version"], "assignee_id": str(officer2_id)},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["first_assigned_at"] == first_assigned_at

        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'recovery_case.assigned' AND entity_id = :eid "
                        "ORDER BY at, id"
                    ),
                    {"tid": str(tid), "eid": case["id"]},
                )
            ).all()
        assert len(rows) == 2
        assert rows[1][0]["assignee_id"] == str(officer_id)  # before
        assert rows[1][1]["assignee_id"] == str(officer2_id)  # after

        # Suspended AFTER assignment: the case stays in the worklist,
        # flagged unassignable — never a silent orphan (A4).
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE users SET status = 'suspended' "
                    "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"uid": str(officer2_id), "tid": str(tid)},
            )
        async with api_client() as client:
            res = await client.get("/recovery-cases", headers=headers)
            assert res.status_code == 200
            rows_out = [r for r in res.json()["items"] if r["case_id"] == case["id"]]
            assert len(rows_out) == 1
            assert rows_out[0]["assignee_unassignable"] is True

    asyncio.run(run())


def test_b2_auditor_excluded_from_assignment_audit_independence() -> None:
    """Review B2 (segregation of duties / three lines of defense): an
    active same-tenant Auditor HOLDS loan_book:view by design (it views
    everything), so the RBAC matrix alone can never refuse it — this
    test fails if the explicit assurance-role exclusion guard in
    assign_recovery_case is removed (the assignment would then return
    200). A Loan Officer stays assignable (no over-blocking)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)
        headers = {"authorization": f"Bearer {token}"}

        # Precondition that makes the guard non-redundant: the seeded
        # Auditor role DOES hold the assignee permission. If this ever
        # fails the exclusion is moot and must be revisited.
        assert seed_matrix()[AUDITOR][Module.LOAN_BOOK][Action.VIEW] is True

        auditor_id, _ = await add_user(tid, AUDITOR)  # active, same tenant
        officer_id, _ = await add_user(tid, "Loan Officer")

        async with api_client() as client:
            # Assurance role refused: 409, least-disclosure envelope
            # (category only — no role details leave the service).
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(auditor_id)},
                headers=headers,
            )
            assert res.status_code == 409, res.text

            # The refused attempt left the case untouched: unassigned,
            # version unchanged, no first_assigned_at.
            res = await client.get(f"/recovery-cases/{case['id']}", headers=headers)
            assert res.status_code == 200
            body = res.json()
            assert body["assignee_id"] is None
            assert body["first_assigned_at"] is None
            assert body["version"] == 1

            # No over-blocking: a Loan Officer remains assignable with
            # the same version — the guard refused, it did not consume.
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={"version": 1, "assignee_id": str(officer_id)},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["assignee_id"] == str(officer_id)

    asyncio.run(run())


def test_sla_timestamps_are_never_caller_suppliable() -> None:
    """Addendum A7 + v1.1 rule 1 applied to time: extra=forbid rejects
    any timestamp riding in on any mutation body (422)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            res = await client.post(
                "/recovery-cases",
                json={"loan_id": str(loan_id), "opened_at": "2020-01-01T00:00:00Z"},
                headers=headers,
            )
            assert res.status_code == 422
            case = await _open_case_api(token, loan_id)
            res = await client.post(
                f"/recovery-cases/{case['id']}/assign",
                json={
                    "version": 1,
                    "assignee_id": str(uuid.uuid4()),
                    "first_assigned_at": "2020-01-01T00:00:00Z",
                },
                headers=headers,
            )
            assert res.status_code == 422
            res = await client.post(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "x", "created_at": "2020-01-01T00:00:00Z"},
                headers=headers,
            )
            assert res.status_code == 422

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Notes — append-only (addendum A2) + keyset paging
# ---------------------------------------------------------------------------


def test_notes_append_only_keyset_and_closed_case_refusal() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(token, loan_id)
        headers = {"authorization": f"Bearer {token}"}

        async with api_client() as client:
            note_ids = []
            for body in ("first contact made", "promised payment Friday"):
                res = await client.post(
                    f"/recovery-cases/{case['id']}/notes",
                    json={"note": body},
                    headers=headers,
                )
                assert res.status_code == 201, res.text
                note_ids.append(res.json()["id"])

            # Keyset paging, oldest first, cursor round-trip.
            res = await client.get(f"/recovery-cases/{case['id']}/notes?limit=1", headers=headers)
            page1 = res.json()
            assert [n["id"] for n in page1["items"]] == [note_ids[0]]
            assert page1["next_cursor"] is not None
            res = await client.get(
                f"/recovery-cases/{case['id']}/notes",
                # params= so the client URL-encodes the cursor: the
                # created_at|id cursor carries a '+00:00' offset whose
                # raw '+' would decode to a space (the audit-log-suite
                # pattern; real clients send encoded query strings).
                params={"limit": 1, "cursor": page1["next_cursor"]},
                headers=headers,
            )
            page2 = res.json()
            assert [n["id"] for n in page2["items"]] == [note_ids[1]]

            # Append-only (A2): no edit/delete route exists at all —
            # attempted in-place edits have NO route (405 from the
            # framework, never a handler).
            res = await client.put(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "rewrite history"},
                headers=headers,
            )
            assert res.status_code == 405
            res = await client.delete(f"/recovery-cases/{case['id']}/notes", headers=headers)
            assert res.status_code == 405

            # Whitespace-only note is a 400 (semantic validation).
            res = await client.post(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "   "},
                headers=headers,
            )
            assert res.status_code == 400

        # Closed case takes no further notes: cure the loan, run the
        # job, then attempt a note.
        await _mark_schedule_paid(tid, loan_id, paid=True)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=AS_OF)
        async with api_client() as client:
            res = await client.post(
                f"/recovery-cases/{case['id']}/notes",
                json={"note": "too late"},
                headers=headers,
            )
            assert res.status_code == 409

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Worklist — keyset order dpd DESC, id DESC; least disclosure (A5)
# ---------------------------------------------------------------------------


def test_worklist_keyset_order_and_least_disclosure() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        # Hand-computed dpd ladder (module docstring): 151 / 182 / 516.
        l151 = await _npl_loan(tid, due=date(2026, 1, 1))
        l182 = await _npl_loan(tid, due=date(2025, 12, 1))
        l516 = await _npl_loan(tid, due=date(2025, 1, 1))
        for lid in (l151, l182, l516):
            await _open_case_api(token, lid)
        headers = {"authorization": f"Bearer {token}"}

        async with api_client() as client:
            res = await client.get("/recovery-cases?limit=2", headers=headers)
            assert res.status_code == 200
            page1 = res.json()
            assert [r["days_past_due"] for r in page1["items"]] == [516, 182]
            assert [r["loan_id"] for r in page1["items"]] == [str(l516), str(l182)]
            assert page1["next_cursor"] is not None
            res = await client.get(
                f"/recovery-cases?limit=2&cursor={page1['next_cursor']}", headers=headers
            )
            page2 = res.json()
            assert [r["days_past_due"] for r in page2["items"]] == [151]
            assert page2["next_cursor"] is None

            # Least disclosure (A5): workflow fields + dpd +
            # classification pill only — NO balance/penalty/provision
            # figures in any worklist row.
            row_keys = set(page1["items"][0])
            assert row_keys == {
                "case_id",
                "loan_id",
                "member_id",
                "days_past_due",
                "classification",
                "assignee_id",
                "assignee_unassignable",
                "opened_at",
                "first_assigned_at",
                "version",
            }

            # Malformed cursor -> sanitized 400, never a 500.
            res = await client.get("/recovery-cases?cursor=garbage", headers=headers)
            assert res.status_code == 400

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — cross-tenant probes (issue #17) on every new route + the
#        explicit-predicate falsifiability probe
# ---------------------------------------------------------------------------


def test_fm5_cross_tenant_probes_and_explicit_predicates() -> None:
    async def run() -> None:
        tid_a, admin_a, token_a = await seed_actor()
        loan_a = await _npl_loan(tid_a)
        case_a = await _open_case_api(token_a, loan_a)
        _, _, token_b = await seed_actor()
        headers_b = {"authorization": f"Bearer {token_b}"}

        async with api_client() as client:
            res = await client.get("/recovery-cases", headers=headers_b)
            assert res.status_code == 200
            assert res.json()["items"] == []
            res = await client.get(f"/recovery-cases/{case_a['id']}", headers=headers_b)
            assert res.status_code == 404
            res = await client.get(f"/recovery-cases/{case_a['id']}/notes", headers=headers_b)
            assert res.status_code == 404
            res = await client.post(
                "/recovery-cases", json={"loan_id": str(loan_a)}, headers=headers_b
            )
            assert res.status_code == 404
            res = await client.post(
                f"/recovery-cases/{case_a['id']}/assign",
                json={"version": 1, "assignee_id": str(admin_a)},
                headers=headers_b,
            )
            assert res.status_code == 404
            res = await client.post(
                f"/recovery-cases/{case_a['id']}/notes",
                json={"note": "hijack"},
                headers=headers_b,
            )
            assert res.status_code == 404

        # Explicit-predicate falsifiability (issue #17): the session IS
        # tenant A (RLS satisfied, cannot mask a missing predicate); the
        # service is called with a FOREIGN tenant argument. Only the
        # bound tenant_id predicate can refuse — remove any predicate
        # under test and its probe fails.
        foreign = uuid.uuid4()
        async with tenant_session(factory(), tid_a) as session:
            with pytest.raises(NotFoundError):
                await recovery_service.get_recovery_case(session, foreign, uuid.UUID(case_a["id"]))
            with pytest.raises(NotFoundError):
                await recovery_service.open_recovery_case(session, foreign, admin_a, loan_id=loan_a)
            with pytest.raises(NotFoundError):
                await recovery_service.assign_recovery_case(
                    session,
                    foreign,
                    admin_a,
                    case_id=uuid.UUID(case_a["id"]),
                    version=1,
                    assignee_id=admin_a,
                )
            with pytest.raises(NotFoundError):
                await recovery_service.add_recovery_note(
                    session, foreign, admin_a, case_id=uuid.UUID(case_a["id"]), note="x"
                )
            page = await recovery_service.list_worklist(session, foreign, cursor=None, limit=50)
            assert page.items == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RBAC — full 7-role matrix on every new route (P4 seed matrix)
# ---------------------------------------------------------------------------


def test_recovery_routes_matrix_every_role() -> None:
    async def run() -> None:
        matrix = seed_matrix()
        tid, _, admin_token = await seed_actor()
        # Shared fixtures: one open case for reads/mutations, plus an
        # assignable officer.
        loan_id = await _npl_loan(tid)
        case = await _open_case_api(admin_token, loan_id)
        officer_id, _ = await add_user(tid, "Loan Officer")

        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            headers = {"authorization": f"Bearer {token}"}
            can_view = matrix[role_name][Module.LOAN_BOOK][Action.VIEW]
            can_create = matrix[role_name][Module.LOAN_BOOK][Action.CREATE]
            can_edit = matrix[role_name][Module.LOAN_BOOK][Action.EDIT]

            async with api_client() as client:
                res = await client.get("/recovery-cases", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.get(f"/recovery-cases/{case['id']}", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.get(f"/recovery-cases/{case['id']}/notes", headers=headers)
                assert res.status_code == (200 if can_view else 403), role_name

                if can_create:
                    # A fresh NPL loan per allowed role: the open lands.
                    fresh = await _npl_loan(tid, due=date(2025, 12, 1))
                    res = await client.post(
                        "/recovery-cases", json={"loan_id": str(fresh)}, headers=headers
                    )
                    assert res.status_code == 201, (role_name, res.text)
                else:
                    res = await client.post(
                        "/recovery-cases",
                        json={"loan_id": str(uuid.uuid4())},
                        headers=headers,
                    )
                    assert res.status_code == 403, role_name

                if can_edit:
                    res = await client.get(f"/recovery-cases/{case['id']}", headers=headers)
                    version = res.json()["version"]
                    res = await client.post(
                        f"/recovery-cases/{case['id']}/assign",
                        json={"version": version, "assignee_id": str(officer_id)},
                        headers=headers,
                    )
                    assert res.status_code == 200, (role_name, res.text)
                    res = await client.post(
                        f"/recovery-cases/{case['id']}/notes",
                        json={"note": f"note by {role_name}"},
                        headers=headers,
                    )
                    assert res.status_code == 201, (role_name, res.text)
                else:
                    res = await client.post(
                        f"/recovery-cases/{case['id']}/assign",
                        json={"version": 1, "assignee_id": str(officer_id)},
                        headers=headers,
                    )
                    assert res.status_code == 403, role_name
                    res = await client.post(
                        f"/recovery-cases/{case['id']}/notes",
                        json={"note": "forbidden"},
                        headers=headers,
                    )
                    assert res.status_code == 403, role_name

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Idempotency-Key replay on the open mutation (gate 1.4)
# ---------------------------------------------------------------------------


def test_open_case_idempotency_key_replay_is_one_effect() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        loan_id = await _npl_loan(tid)
        headers = {
            "authorization": f"Bearer {token}",
            "Idempotency-Key": f"open-{uuid.uuid4()}",
        }
        async with api_client() as client:
            first = await client.post(
                "/recovery-cases", json={"loan_id": str(loan_id)}, headers=headers
            )
            assert first.status_code == 201
            replay = await client.post(
                "/recovery-cases", json={"loan_id": str(loan_id)}, headers=headers
            )
            assert replay.status_code == 201
            assert replay.json() == first.json()
        # Side-effect count proves one effect (never return values).
        rows = await count(
            tid,
            "SELECT count(*) FROM recovery_cases WHERE tenant_id = CAST(:tenant AS uuid) "
            "AND loan_id = CAST(:lid AS uuid)",
            tenant=str(tid),
            lid=str(loan_id),
        )
        assert rows == 1

    asyncio.run(run())
