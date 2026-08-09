"""P15 batch 5 (U6, issue #31) — advisory exit-eligibility read.

GET /member-exits/eligibility/{member_id} (members:view): the blocker
FACTS the settlement service enforces, surfaced BEFORE a request is
submitted — counts and booleans only, NEVER an amount, computed with
NO row locks. The BINDING verdict stays with the locked recompute at
request/settlement time; these tests pin that the advisory facts agree
with the binding path on a quiet system (blocker present -> the create
409s; blocker absent -> the create succeeds).

Failure modes, one falsifiable test each:

  FM-E1 fact drift from the binding rule set — every blocker leg pairs
        the advisory fact with the REAL request_exit outcome.
  FM-E2 live-set divergence — a released guarantee must NOT count
        (same live_guarantee_params source as P9 enforcement).
  FM-E3 amount disclosure — seeded balances/pledges/loan figures must
        never appear in ANY response (success, 403, 404) byte-wise.
  FM-E4 cross-tenant bleed — tenant A's token gets a 404 for tenant
        B's member; the service probe returns not-found, never mixed
        facts.
  FM-E5 RBAC bypass — a role without members:view gets the sanitized
        403 envelope.
  FM-E6 lock creep — the advisory SQL constants must stay lock-free
        (no FOR UPDATE / FOR SHARE); the advisory read can never join
        the P12 lock graph.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor
from genesis.application import member_exits as exits_service
from genesis.errors import ConflictError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session
from test_member_exits import _seed_active_loan, _seed_guarantee, _seed_member

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_FACT_KEYS = {
    "member_id",
    "member_status",
    "status_allows_exit",
    "open_exit_exists",
    "live_guarantees_count",
    "open_applications_count",
    "active_loans_count",
    "unresolved_writeoff_claim",
    "advisory_eligible",
    "as_of",
}


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _get_eligibility(token: str, member_id: uuid.UUID) -> tuple[int, dict[str, Any], str]:
    async with api_client() as client:
        res = await client.get(f"/member-exits/eligibility/{member_id}", headers=_headers(token))
    return res.status_code, dict(res.json()), res.text


async def _seed_open_application(tid: uuid.UUID, mid: uuid.UUID, *, stage: str) -> uuid.UUID:
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Elig-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '4321.09', 12, '12.00', :stage, '0.00')"
            ),
            {"id": str(aid), "tid": str(tid), "mid": str(mid), "pid": str(pid), "stage": stage},
        )
    return aid


async def _seed_posted_write_off(
    tid: uuid.UUID, mid: uuid.UUID, loan_id: uuid.UUID, *, total: str
) -> uuid.UUID:
    """The durable FACTS of a posted P13.15 write-off (the
    test_par_aging_export seeding style): terminal loan status + the
    write-once posted snapshot (balance = total, penalty 0)."""
    wid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loans SET status = 'written_off' "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(loan_id), "tid": str(tid)},
        )
        await session.execute(
            text(
                "INSERT INTO loan_write_offs "
                "(id, tenant_id, loan_id, member_id, balance, penalty_due, "
                " total_written_off, classification, provision_pct, reason, "
                " status, posted_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "CAST(:mid AS uuid), :total, '0', :total, 'loss', '100.00', "
                "'eligibility blocker fixture', 'posted', :ts)"
            ),
            {
                "id": str(wid),
                "tid": str(tid),
                "lid": str(loan_id),
                "mid": str(mid),
                "total": total,
                "ts": datetime.now(UTC),
            },
        )
    return wid


async def _seed_recovery(
    tid: uuid.UUID, mid: uuid.UUID, loan_id: uuid.UUID, write_off_id: uuid.UUID, *, amount: str
) -> None:
    """Raw recovery receipt against the surviving claim (append-only
    loan_recoveries needs its RC- transaction row — seeded raw)."""
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        actor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "INSERT INTO transactions (id, tenant_id, txn_ref, member_id, type, "
                "amount, channel, occurred_at) VALUES (CAST(:id AS uuid), "
                "CAST(:tid AS uuid), :ref, CAST(:m AS uuid), 'loan_recovery', "
                ":amount, 'bank', :ts)"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "ref": f"RC-ELIG-{uuid.uuid4().hex[:8]}",
                "m": str(mid),
                "amount": amount,
                "ts": datetime.now(UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO loan_recoveries "
                "(id, tenant_id, write_off_id, loan_id, member_id, transaction_id, "
                " amount, recorded_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:w AS uuid), "
                "CAST(:l AS uuid), CAST(:m AS uuid), CAST(:t AS uuid), :amount, "
                "CAST(:actor AS uuid))"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "w": str(write_off_id),
                "l": str(loan_id),
                "m": str(mid),
                "t": str(txn_id),
                "amount": amount,
                "actor": str(actor),
            },
        )


# ---------------------------------------------------------------------------
# Clean member: all facts zero, advisory true, create AGREES (FM-E1)
# ---------------------------------------------------------------------------


def test_clean_member_facts_zero_advisory_true_and_create_agrees() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        mid = await _seed_member(tid, deposit="1234.56", shares="777.00")

        status, body, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert set(body.keys()) == _FACT_KEYS
        assert body["member_id"] == str(mid)
        assert body["member_status"] == "active"
        assert body["status_allows_exit"] is True
        assert body["open_exit_exists"] is False
        assert body["live_guarantees_count"] == 0
        assert body["open_applications_count"] == 0
        assert body["active_loans_count"] == 0
        assert body["unresolved_writeoff_claim"] is False
        assert body["advisory_eligible"] is True

        # FM-E1: the advisory verdict agrees with the binding path on a
        # quiet system — the create succeeds.
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.request_exit(session, tid, admin_id, mid)
        assert record.status.value == "requested"

        # The open exit now occupies the member's single open slot.
        status, after, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert after["open_exit_exists"] is True
        assert after["advisory_eligible"] is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM-E2 — live-guarantee blocker: counts the LIVE set only
# ---------------------------------------------------------------------------


def test_live_guarantee_blocker_counts_live_set_and_create_409s() -> None:
    """Hand-computed: pledged (1) + active (1) count; a released row and
    a BORROWER-side row must not. With the blocker present the binding
    create 409s; releasing both rows flips the fact AND the verdict."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00")
        borrower = await _seed_member(tid, deposit="500.00")
        await _seed_guarantee(
            tid, guarantor=guarantor, borrower=borrower, amount="555.55", status="pledged"
        )
        g2 = await _seed_guarantee(
            tid, guarantor=guarantor, borrower=borrower, amount="444.45", status="active"
        )
        await _seed_guarantee(
            tid, guarantor=guarantor, borrower=borrower, amount="333.33", status="released"
        )
        # Borrower-side: a guarantee RECEIVED never blocks the receiver.
        await _seed_guarantee(
            tid, guarantor=borrower, borrower=guarantor, amount="222.22", status="active"
        )

        status, body, _ = await _get_eligibility(token, guarantor)
        assert status == 200
        assert body["live_guarantees_count"] == 2
        assert body["advisory_eligible"] is False

        with pytest.raises(ConflictError):
            async with tenant_session(factory(), tid) as session:
                await exits_service.request_exit(session, tid, admin_id, guarantor)

        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE guarantees SET status = 'released' "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND guarantor_member_id = CAST(:g AS uuid)"
                ),
                {"tid": str(tid), "g": str(guarantor)},
            )
        status, cleared, _ = await _get_eligibility(token, guarantor)
        assert status == 200
        assert cleared["live_guarantees_count"] == 0
        assert cleared["advisory_eligible"] is True
        assert g2 is not None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Open applications block; active loans inform but do NOT block
# ---------------------------------------------------------------------------


def test_open_application_blocks_and_active_loan_does_not() -> None:
    """An open-stage application (committee) blocks; a REJECTED one does
    not. An active loan is a FACT (count 1) but not a blocker — the
    settlement nets it (the P10 early-settlement rule); the binding
    create agrees (succeeds with equity covering the payoff)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        mid = await _seed_member(tid, deposit="9000.00", shares="1000.00")
        await _seed_open_application(tid, mid, stage="committee")
        await _seed_open_application(tid, mid, stage="rejected")

        status, body, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert body["open_applications_count"] == 1
        assert body["advisory_eligible"] is False

        with pytest.raises(ConflictError):
            async with tenant_session(factory(), tid) as session:
                await exits_service.request_exit(session, tid, admin_id, mid)

        # Resolve the open application; add an active loan instead.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loan_applications SET stage = 'rejected' "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = CAST(:m AS uuid)"
                ),
                {"tid": str(tid), "m": str(mid)},
            )
        await _seed_active_loan(tid, mid, balance="4321.09")

        status, body, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert body["open_applications_count"] == 0
        assert body["active_loans_count"] == 1
        assert body["advisory_eligible"] is True  # loans net; not a blocker

        async with tenant_session(factory(), tid) as session:
            record = await exits_service.request_exit(session, tid, admin_id, mid)
        assert record.status.value == "requested"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Unresolved write-off claim: partial recovery keeps the flag, full clears
# ---------------------------------------------------------------------------


def test_unresolved_writeoff_claim_flag_partial_then_full_recovery() -> None:
    """Hand-computed: claim 1000.00, recovered 400.00 -> unresolved
    (1000.00 > 400.00); recovering the remaining 600.00 clears it
    (1000.00 > 1000.00 is false). The SAME SQL drives the binding
    guard, so the create outcome flips with the fact."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        mid = await _seed_member(tid, deposit="2000.00")
        loan_id = await _seed_active_loan(tid, mid, balance="1000.00")
        wid = await _seed_posted_write_off(tid, mid, loan_id, total="1000.00")
        await _seed_recovery(tid, mid, loan_id, wid, amount="400.00")

        status, body, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert body["unresolved_writeoff_claim"] is True
        # The written-off loan is terminal — NOT an active loan.
        assert body["active_loans_count"] == 0
        assert body["advisory_eligible"] is False

        with pytest.raises(ConflictError):
            async with tenant_session(factory(), tid) as session:
                await exits_service.request_exit(session, tid, admin_id, mid)

        await _seed_recovery(tid, mid, loan_id, wid, amount="600.00")
        status, cleared, _ = await _get_eligibility(token, mid)
        assert status == 200
        assert cleared["unresolved_writeoff_claim"] is False
        assert cleared["advisory_eligible"] is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Status legs: exited refused; dormant/arrears allowed (P13.13 map)
# ---------------------------------------------------------------------------


def test_status_legs_exited_refused_dormant_and_arrears_allowed() -> None:
    async def run() -> None:
        tid, _admin_id, token = await seed_actor()
        exited = await _seed_member(tid, status="exited")
        dormant = await _seed_member(tid)
        arrears = await _seed_member(tid, status="arrears")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE members SET status = 'dormant' "
                    "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(dormant), "tid": str(tid)},
            )

        status, body, _ = await _get_eligibility(token, exited)
        assert status == 200
        assert body["member_status"] == "exited"
        assert body["status_allows_exit"] is False
        assert body["advisory_eligible"] is False

        for mid, expected_status in ((dormant, "dormant"), (arrears, "arrears")):
            status, body, _ = await _get_eligibility(token, mid)
            assert status == 200
            assert body["member_status"] == expected_status
            assert body["status_allows_exit"] is True
            assert body["advisory_eligible"] is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM-E3 — least disclosure: NO amount on any path
# ---------------------------------------------------------------------------


def test_no_amount_ever_travels_on_any_path() -> None:
    """Seeded figures (deposit 1234.56, shares 777.00, pledge 555.55,
    loan 4321.09) must not appear byte-wise in the success body, the
    403 envelope or the 404 envelope."""

    async def run() -> None:
        tid, _admin_id, token = await seed_actor()
        mid = await _seed_member(tid, deposit="1234.56", shares="777.00")
        borrower = await _seed_member(tid, deposit="100.00")
        await _seed_guarantee(
            tid, guarantor=mid, borrower=borrower, amount="555.55", status="active"
        )
        await _seed_active_loan(tid, mid, balance="4321.09")
        amounts = ("1234.56", "777.00", "555.55", "4321.09")

        status, body, raw = await _get_eligibility(token, mid)
        assert status == 200
        assert set(body.keys()) == _FACT_KEYS
        for amount in amounts:
            assert amount not in raw

        # FM-E5: a role without members:view (Credit Committee) gets
        # the sanitized envelope only.
        _, cc_token = await add_user(tid, "Credit Committee")
        async with api_client() as client:
            res = await client.get(f"/member-exits/eligibility/{mid}", headers=_headers(cc_token))
        assert res.status_code == 403
        assert set(res.json().keys()) == {"category", "correlation_id"}
        for amount in amounts:
            assert amount not in res.text

        # Unknown member: 404 envelope, no facts, no amounts.
        status, envelope, raw404 = await _get_eligibility(token, uuid.uuid4())
        assert status == 404
        assert set(envelope.keys()) == {"category", "correlation_id"}
        for amount in amounts:
            assert amount not in raw404

        # No token at all: 401 envelope.
        async with api_client() as client:
            res = await client.get(f"/member-exits/eligibility/{mid}")
        assert res.status_code == 401
        assert set(res.json().keys()) == {"category", "correlation_id"}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM-E4 — cross-tenant bleed
# ---------------------------------------------------------------------------


def test_cross_tenant_member_is_not_found_never_mixed_facts() -> None:
    async def run() -> None:
        tid_a, _admin_a, token_a = await seed_actor()
        tid_b, _admin_b, _token_b = await seed_actor()
        foreign = await _seed_member(tid_b, deposit="999.99")

        status, envelope, raw = await _get_eligibility(token_a, foreign)
        assert status == 404
        assert set(envelope.keys()) == {"category", "correlation_id"}
        assert "999.99" not in raw

        # Issue-#17 probe: session AS tenant A, tenant-B argument — the
        # explicit bound tenant predicate alone must yield not-found.
        with pytest.raises(NotFoundError):
            async with tenant_session(factory(), tid_a) as session:
                await exits_service.exit_eligibility(session, tid_b, foreign)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM-E6 — the advisory read must stay lock-free
# ---------------------------------------------------------------------------


def test_advisory_sql_constants_take_no_locks() -> None:
    """Structural gate: the advisory read may never join the P12 lock
    graph. Falsifiable: add FOR UPDATE/FOR SHARE to any constant and
    this fails."""
    constants = (
        exits_service.ELIGIBILITY_MEMBER_SQL,
        exits_service.ELIGIBILITY_GUARANTEES_SQL,
        exits_service.ELIGIBILITY_LOANS_SQL,
        exits_service.ELIGIBILITY_OPEN_EXIT_SQL,
        exits_service.UNRESOLVED_WRITEOFF_CLAIM_SQL,
    )
    for sql in constants:
        upper = sql.upper()
        assert "FOR UPDATE" not in upper
        assert "FOR SHARE" not in upper
        assert "ADVISORY" not in upper  # no pg_advisory_* creep either
        assert "CAST(:TID AS UUID)" in upper  # explicit tenant predicate
