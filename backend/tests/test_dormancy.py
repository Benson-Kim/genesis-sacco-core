"""Integration tests for P13.13: the dormancy lifecycle (real Postgres, RLS).

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Every numbered banking-grade failure mode from BUILD_PROMPTS P13.13 has
its own falsifiable test here (remove the guard and the test fails):

  FM1  Activity gaming — a member whose ONLY in-window activity is
       system postings (INT- interest, DV- dividends) or a reversal
       row goes dormant on schedule; an identical member with one real
       deposit does not. Falsifiable by widening the code-owned
       MEMBER_INITIATED allow-list (the domain pin in
       test_ledger_domain.py fails first).
  FM2  Dormant money movement — withdraw / borrow / pledge / share
       top-up all refuse a dormant member through the single
       capability-map gatekeeper (share transfers are pinned in
       test_share_transfers.py; substitution in
       test_guarantee_release.py); a deposit succeeds AND reactivates.
  FM3  Reactivation race — a deposit holding the member row FOR UPDATE
       is SKIP LOCKED-skipped by a concurrent job run; the job winning
       first is reactivated by the following deposit. Exactly one
       final state, proven by status + audit side-effect counts.
  FM4  Idempotent re-run — scanned == 0 (the status + ledger-derived
       anti-joins, v1.1 rule 8) and unchanged audit/outbox/version
       side-effect counts.
  FM5  Kill-switch mid-batch — zero partial transitions; the same run
       commits cleanly afterwards.
  FM6  Tenant scoping — a foreign-tenant argument transitions nothing
       (issue-#17 probe).
  FM7  Exit-of-dormant — end-to-end through the REAL P12 workflow
       (request -> quorum -> settlement) with hand-computed figures.
  FM8  Config fail-closed — missing dormancy_period_months refuses the
       run loudly (409, zero transitions), never a silent default;
       corrupt values refuse identically; the nightly cycle logs the
       refusal and still serves the other tenants.

Boundary oracles are HAND-COMPUTED in comments (never captured from
the implementation); idempotency is asserted via side-effect row
counts (audit, outbox, versions), never return values alone
(MASTER_PROMPT section 4).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import dormancy as dormancy_module
from genesis.application import member_exits as exits_service
from genesis.application import tenant_settings as settings_service
from genesis.application.dormancy import (
    parse_dormancy_period,
    run_dormancy_for_tenant,
)
from genesis.application.guarantees import pledge_guarantee
from genesis.application.ledger import post_deposit, post_deposit_interest, post_reversal
from genesis.application.loan_applications import create_application
from genesis.application.members import change_member_status
from genesis.application.transactions import (
    record_deposit,
    record_share_topup,
    record_withdrawal,
)
from genesis.domain.committee import Vote
from genesis.domain.exits import ExitStatus
from genesis.domain.ledger import Channel
from genesis.domain.members import MemberStatus
from genesis.errors import ConflictError
from genesis.infrastructure import dormancy_worker
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside a batch transaction, before commit."""


#: The canonical run date used across these tests. With the default
#: period of 6 months the window start is HAND-COMPUTED:
#: 2026-08-01 minus 6 months = 2026-02-01, i.e. cutoff instant
#: 2026-02-01T00:00:00Z — activity at or after that instant keeps the
#: member active.
AS_OF = date(2026, 8, 1)
CUTOFF = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)

#: Timestamps relative to the hand-computed window.
OLD = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)  # before the window
IN_WINDOW = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)  # inside the window
MEMBER_SINCE = datetime(2025, 1, 1, 9, 0, 0, tzinfo=UTC)  # admission long ago


def _scope(tid: uuid.UUID) -> partial[Any]:
    return partial(tenant_session, factory(), tid)


async def _configure_dormancy(tid: uuid.UUID, admin_id: uuid.UUID, *, months: int = 6) -> None:
    """Configure the period through the REAL P13.7 writer (gate 1.1)."""
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
        await settings_service.update_settings(
            session,
            tid,
            admin_id,
            version=current.version,
            changes={"dormancy_period_months": months},
        )


async def _aged_member(
    tid: uuid.UUID, *, name: str, deposit: str = "0", shares: str = "0"
) -> uuid.UUID:
    """A member admitted long before the window (created_at backdated)."""
    mid = await seed_member(tid, name=name, deposit=deposit, shares=shares)
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE members SET created_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"ts": MEMBER_SINCE, "id": str(mid), "tid": str(tid)},
        )
    return mid


async def _deposit_at(tid: uuid.UUID, mid: uuid.UUID, amount: str, when: datetime) -> uuid.UUID:
    """A member deposit posted through the REAL P7 path at a pinned time."""
    async with tenant_session(factory(), tid) as session:
        result = await post_deposit(
            session, tid, mid, Decimal(amount), Channel.BANK, None, occurred_at=when
        )
    return result.txn_id


async def _interest_at(tid: uuid.UUID, mid: uuid.UUID, amount: str, when: datetime) -> None:
    """A SYSTEM interest posting (INT-) at a pinned time (P11 path)."""
    async with tenant_session(factory(), tid) as session:
        await post_deposit_interest(session, tid, mid, Decimal(amount), None, occurred_at=when)


async def _status(tid: uuid.UUID, mid: uuid.UUID) -> tuple[str, int]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, version FROM members "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(mid), "tid": str(tid)},
            )
        ).one()
    return str(row[0]), int(row[1])


async def _dormancy_audits(tid: uuid.UUID, mid: uuid.UUID | None = None) -> int:
    sql = (
        "SELECT count(*) FROM audit_log WHERE action = 'member.status' "
        "AND after->>'status' = 'dormant' AND after->>'reason' = 'dormancy'"
    )
    if mid is not None:
        sql += " AND entity_id = :eid"
        return await count(tid, sql, eid=str(mid))
    return await count(tid, sql)


async def _reactivation_audits(tid: uuid.UUID, mid: uuid.UUID) -> int:
    return await count(
        tid,
        "SELECT count(*) FROM audit_log WHERE action = 'member.status' "
        "AND after->>'reason' = 'deposit_reactivation' AND entity_id = :eid",
        eid=str(mid),
    )


async def _status_events(tid: uuid.UUID, mid: uuid.UUID, to_status: str) -> int:
    return await count(
        tid,
        "SELECT count(*) FROM outbox_events WHERE event_type = 'member.status_changed' "
        "AND payload->>'member_id' = :mid AND payload->>'to' = :to_status",
        mid=str(mid),
        to_status=to_status,
    )


# ---------------------------------------------------------------------------
# FM1 — activity gaming by system postings (the universal bank bug)
# ---------------------------------------------------------------------------


def test_system_postings_and_reversals_never_reset_the_dormancy_clock() -> None:
    """Three members, identical except for their in-window activity:

      * gamed   — real deposit BEFORE the window (2026-01-15), then
        ONLY system postings inside it (INT- interest on 2026-06-30 —
        and dividends' DV- postings are pinned to FY END, so they are
        classified identically): MUST go dormant on schedule.
      * reversed — same old deposit; the only in-window row is a
        REVERSAL of it (staff correction, reversal_of_id set): MUST
        go dormant.
      * alive   — same old deposit PLUS one real deposit inside the
        window (2026-07-15): MUST stay active.

    Falsifiable: count interest postings or reversal rows as activity
    (widen the allow-list / drop the reversal_of_id predicate) and
    'gamed'/'reversed' stay active, failing the assertions."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)

        gamed = await _aged_member(tid, name="Gamed")
        reversed_m = await _aged_member(tid, name="Reversed")
        alive = await _aged_member(tid, name="Alive")

        for mid in (gamed, reversed_m, alive):
            await _deposit_at(tid, mid, "500.00", OLD)
        # System posting inside the window for the gamed member only.
        await _interest_at(tid, gamed, "12.34", datetime(2026, 6, 30, 23, 59, tzinfo=UTC))
        # The reversal (posted 'now', i.e. inside the window) of the
        # reversed member's old deposit.
        old_txn = await _deposit_at(tid, reversed_m, "100.00", datetime(2026, 1, 20, tzinfo=UTC))
        async with tenant_session(factory(), tid) as session:
            await post_reversal(session, tid, old_txn)
        # Real member-initiated deposit inside the window.
        await _deposit_at(tid, alive, "50.00", IN_WINDOW)

        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.period_months == 6
        assert result.cutoff == CUTOFF.date()
        assert result.transitioned == 2

        assert (await _status(tid, gamed))[0] == "dormant"
        assert (await _status(tid, reversed_m))[0] == "dormant"
        assert (await _status(tid, alive))[0] == "active"

        # Exact figures live in the audit row (rule 7): the recorded
        # last activity is the ledger-derived member-initiated maximum
        # — the OLD deposit, not the in-window interest posting.
        async with tenant_session(factory(), tid) as session:
            recorded = (
                await session.execute(
                    text(
                        "SELECT after->>'last_activity_at', after->>'dormancy_period_months', "
                        "after->>'cutoff' FROM audit_log WHERE action = 'member.status' "
                        "AND entity_id = :eid AND after->>'reason' = 'dormancy'"
                    ),
                    {"eid": str(gamed)},
                )
            ).one()
        assert datetime.fromisoformat(str(recorded[0])) == OLD
        assert recorded[1] == "6"
        assert datetime.fromisoformat(str(recorded[2])) == CUTOFF
        # Member-facing notifications for exactly the two transitions.
        assert await _status_events(tid, gamed, "dormant") == 1
        assert await _status_events(tid, reversed_m, "dormant") == 1
        assert await _status_events(tid, alive, "dormant") == 0

    asyncio.run(run())


def test_cutoff_boundary_the_instant_before_vs_on_the_cutoff() -> None:
    """HAND-COMPUTED boundary (never from the code): as_of 2026-08-01,
    period 6 months -> window starts 2026-02-01T00:00:00Z.

      * edge   — last deposit at exactly 2026-02-01T00:00:00Z (ON the
        cutoff instant): inside the window, stays ACTIVE.
      * late   — last deposit at 2026-01-31T23:59:59Z (one second
        earlier): outside, goes DORMANT.

    A deposit committed the instant before the JOB runs is trivially
    inside the window (2026-07-31 << cutoff test 'alive' above); this
    pins the other end of the interval."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        edge = await _aged_member(tid, name="Edge")
        late = await _aged_member(tid, name="Late")
        await _deposit_at(tid, edge, "10.00", datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC))
        await _deposit_at(tid, late, "10.00", datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC))

        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.transitioned == 1
        assert (await _status(tid, edge))[0] == "active"
        assert (await _status(tid, late))[0] == "dormant"

    asyncio.run(run())


def test_month_length_clamping_31st_as_of_uses_the_real_month_end() -> None:
    """HAND-COMPUTED month-length edge: as_of 2026-03-31 with a
    1-month period -> February has 28 days in 2026, so the cutoff
    clamps to 2026-02-28 (midnight UTC).

      * onday  — deposit at 2026-02-28T12:00Z (ON the clamped cutoff
        day): stays ACTIVE.
      * before — deposit at 2026-02-27T23:00Z: goes DORMANT.

    And the 31-Jan + N months oracle: a member whose last activity is
    2026-01-31 survives a 6-month job run on 2026-07-31 (cutoff
    2026-01-31, activity ON the cutoff day counts) but is dormant on
    2026-08-01 (cutoff 2026-02-01)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=1)
        onday = await _aged_member(tid, name="OnDay")
        before = await _aged_member(tid, name="Before")
        await _deposit_at(tid, onday, "10.00", datetime(2026, 2, 28, 12, 0, 0, tzinfo=UTC))
        await _deposit_at(tid, before, "10.00", datetime(2026, 2, 27, 23, 0, 0, tzinfo=UTC))

        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=date(2026, 3, 31))
        assert result.cutoff == date(2026, 2, 28)  # clamped, hand-computed
        assert result.transitioned == 1
        assert (await _status(tid, onday))[0] == "active"
        assert (await _status(tid, before))[0] == "dormant"

        # 31-Jan + 6 months, both sides of the boundary.
        tid2, admin2, _ = await seed_actor()
        await _configure_dormancy(tid2, admin2, months=6)
        jan31 = await _aged_member(tid2, name="Jan31")
        await _deposit_at(tid2, jan31, "10.00", datetime(2026, 1, 31, 8, 0, 0, tzinfo=UTC))

        july = await run_dormancy_for_tenant(_scope(tid2), tid2, as_of=date(2026, 7, 31))
        assert july.cutoff == date(2026, 1, 31)
        assert (await _status(tid2, jan31))[0] == "active"

        august = await run_dormancy_for_tenant(_scope(tid2), tid2, as_of=date(2026, 8, 1))
        assert august.cutoff == date(2026, 2, 1)
        assert (await _status(tid2, jan31))[0] == "dormant"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — dormant money movement refused by the single gatekeeper
# ---------------------------------------------------------------------------


def test_dormant_member_may_deposit_but_not_withdraw_borrow_pledge_or_topup() -> None:
    """Every money-out/credit path refuses a REAL job-marked dormant
    member with a distinct 409 and zero side effects; a deposit then
    succeeds AND reactivates (audit + member notification in the same
    transaction), after which the previously refused top-up succeeds.

    Falsifiable: widen the capability map (the domain pin in
    test_members_domain.py fails first) or bypass the gatekeeper in
    any route and its refusal assertion here fails."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _aged_member(tid, name="Dormant Mover", deposit="5000.00")
        await _deposit_at(tid, mid, "5000.00", OLD)
        other = await seed_member(tid, name="Borrower", deposit="0")

        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.transitioned == 1
        assert (await _status(tid, mid))[0] == "dormant"

        # Withdrawal refused (money out).
        with pytest.raises(ConflictError, match="requires an active member"):
            async with tenant_session(factory(), tid) as session:
                await record_withdrawal(
                    session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.BANK
                )
        # Share top-up refused (only a deposit may touch a dormant account).
        with pytest.raises(ConflictError, match="requires an active member"):
            async with tenant_session(factory(), tid) as session:
                await record_share_topup(
                    session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.BANK
                )
        # Borrowing refused.
        pid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO loan_products "
                    "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 36)"
                ),
                {"id": str(pid), "tid": str(tid), "name": f"Dorm-{pid.hex[:6]}"},
            )
        with pytest.raises(ConflictError, match="only active members may apply"):
            async with tenant_session(factory(), tid) as session:
                await create_application(
                    session,
                    tid,
                    None,
                    member_id=mid,
                    product_id=pid,
                    amount=Decimal("1000.00"),
                    term_months=6,
                )
        # Pledging refused (capacity 5000 >= 100, so ONLY the status
        # guard can be the refusal — the !29 R2 shape).
        app_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO loan_applications "
                    "(id, tenant_id, member_id, product_id, amount, term_months, "
                    " rate_pct, stage, cover_pct) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    "CAST(:pid AS uuid), 1000, 6, '12.00', 'submitted', '0.00')"
                ),
                {"id": str(app_id), "tid": str(tid), "mid": str(other), "pid": str(pid)},
            )
        with pytest.raises(ConflictError, match="only active members may pledge"):
            async with tenant_session(factory(), tid) as session:
                await pledge_guarantee(
                    session,
                    tid,
                    None,
                    application_id=app_id,
                    guarantor_member_id=mid,
                    amount=Decimal("100.00"),
                )
        # Zero side effects from the refusals: exactly the two setup
        # transactions exist (the old deposit posted twice? no — one
        # posting + the account seed carries no txn), no guarantee row.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE member_id = CAST(:mid AS uuid)",
                mid=str(mid),
            )
            == 1
        )
        assert await count(tid, "SELECT count(*) FROM guarantees") == 0
        assert (await _status(tid, mid))[0] == "dormant"

        # A deposit reactivates in the SAME transaction: status, audit
        # row and member-facing outbox notification (the detection
        # control — drop the enqueue and the event count fails).
        async with tenant_session(factory(), tid) as session:
            await record_deposit(
                session, tid, admin_id, mid, amount=Decimal("25.00"), channel=Channel.BANK
            )
        status, _ = await _status(tid, mid)
        assert status == "active"
        assert await _reactivation_audits(tid, mid) == 1
        assert await _status_events(tid, mid, "active") == 1

        # The previously refused operation now succeeds.
        async with tenant_session(factory(), tid) as session:
            await record_share_topup(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.BANK
            )

        # A second deposit does NOT reactivate again (side-effect
        # counts stay at one — exactly-once reactivation).
        async with tenant_session(factory(), tid) as session:
            await record_deposit(
                session, tid, admin_id, mid, amount=Decimal("5.00"), channel=Channel.BANK
            )
        assert await _reactivation_audits(tid, mid) == 1
        assert await _status_events(tid, mid, "active") == 1

    asyncio.run(run())


def test_manual_status_route_cannot_write_or_clear_dormancy() -> None:
    """The nightly job is the sole dormant writer and the deposit is
    the sole reactivator: the P8 manual status service refuses both
    directions with 409 (the P12 'exited' ownership precedent)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        active = await seed_member(tid, name="Manual A")
        with pytest.raises(ConflictError, match="dormancy job"):
            async with tenant_session(factory(), tid) as session:
                await change_member_status(
                    session, tid, admin_id, active, version=1, new_status=MemberStatus.DORMANT
                )
        dormant = await seed_member(tid, name="Manual B")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE members SET status = 'dormant' "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(dormant), "tid": str(tid)},
            )
        with pytest.raises(ConflictError, match="reactivate automatically on deposit"):
            async with tenant_session(factory(), tid) as session:
                await change_member_status(
                    session, tid, admin_id, dormant, version=1, new_status=MemberStatus.ACTIVE
                )
        assert (await _status(tid, active))[0] == "active"
        assert (await _status(tid, dormant))[0] == "dormant"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — the reactivation race, live two-transaction interleavings
# ---------------------------------------------------------------------------


def test_deposit_holding_the_member_lock_is_skipped_by_the_job() -> None:
    """Interleaving A (deposit first): an uncommitted deposit holds
    the member row FOR UPDATE; the concurrent job run SKIP LOCKED-
    skips the row and transitions nothing. After the deposit commits
    the member has in-window activity, so the re-run scans zero rows.
    Falsifiable: drop the deposit path's FOR UPDATE (back to FOR
    SHARE) and the job's FOR UPDATE would block instead of skip —
    and a dormant marking could interleave with the deposit."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _aged_member(tid, name="Racer", deposit="0")
        await _deposit_at(tid, mid, "100.00", OLD)

        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        try:
            # The deposit transaction, held open before commit: the
            # member row is FOR UPDATE-locked by _require_member.
            await record_deposit(
                session, tid, None, mid, amount=Decimal("20.00"), channel=Channel.BANK
            )
            result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        finally:
            await holder.__aexit__(None, None, None)
        assert result.transitioned == 0  # skipped, never blocked
        assert (await _status(tid, mid))[0] == "active"
        assert await _dormancy_audits(tid, mid) == 0

        # The committed deposit is in-window activity: nothing to scan.
        rerun = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert rerun.scanned == 0
        assert (await _status(tid, mid))[0] == "active"

    asyncio.run(run())


def test_job_winning_the_race_is_reactivated_by_the_following_deposit() -> None:
    """Interleaving B (job first): the job marks the member dormant;
    the deposit that lost the race then reactivates in its own
    transaction. Exactly one dormancy audit + one reactivation audit,
    final state ACTIVE — never Active overwritten to Dormant."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _aged_member(tid, name="Racer B", deposit="0")
        await _deposit_at(tid, mid, "100.00", OLD)

        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.transitioned == 1
        async with tenant_session(factory(), tid) as session:
            await record_deposit(
                session, tid, None, mid, amount=Decimal("20.00"), channel=Channel.BANK
            )

        status, version = await _status(tid, mid)
        assert status == "active"
        # One transition each way; the version moved exactly twice
        # past the seed row's 1 (dormant bump + reactivation bump).
        assert await _dormancy_audits(tid, mid) == 1
        assert await _reactivation_audits(tid, mid) == 1
        assert version == 3
        assert await _status_events(tid, mid, "dormant") == 1
        assert await _status_events(tid, mid, "active") == 1

        # The job re-run after reactivation scans nothing: the
        # reactivating deposit itself is in-window activity.
        rerun = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert rerun.scanned == 0
        assert (await _status(tid, mid))[0] == "active"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — idempotent re-run by side-effect counts (scanned == 0)
# ---------------------------------------------------------------------------


def test_rerun_is_a_lock_free_noop_by_side_effect_counts() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        due_a = await _aged_member(tid, name="Due A")
        due_b = await _aged_member(tid, name="Due B")
        alive = await _aged_member(tid, name="Still Alive")
        for mid in (due_a, due_b, alive):
            await _deposit_at(tid, mid, "40.00", OLD)
        await _deposit_at(tid, alive, "1.00", IN_WINDOW)

        first = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert first.transitioned == 2
        audits_before = await _dormancy_audits(tid)
        events_before = await count(
            tid,
            "SELECT count(*) FROM outbox_events WHERE event_type = 'member.status_changed'",
        )
        versions_before = [(await _status(tid, m))[1] for m in (due_a, due_b, alive)]
        assert audits_before == 2

        # Re-run for the SAME as_of: the status anti-join excludes the
        # dormant pair and the activity anti-join excludes the live
        # member — zero rows scanned, zero locks taken, nothing written.
        second = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert second.scanned == 0
        assert second.transitioned == 0
        assert await _dormancy_audits(tid) == audits_before
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'member.status_changed'",
            )
            == events_before
        )
        assert [(await _status(tid, m))[1] for m in (due_a, due_b, alive)] == versions_before

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — kill-switch mid-batch leaves zero partial state
# ---------------------------------------------------------------------------


def test_kill_switch_mid_batch_leaves_zero_partial_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash INSIDE the batch transaction after the status UPDATE,
    before commit (the audit writer raises): ZERO partial state may
    remain — no dormant member without its audit row and notification.
    Falsifiable: commit the status write in its own transaction and
    the orphaned dormant row survives the abort."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        due = await _aged_member(tid, name="Killed")
        await _deposit_at(tid, due, "40.00", OLD)

        real_record_audit = dormancy_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "member.status":
                raise _KillSwitchError()
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(dormancy_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)

        # ZERO partial state: no transition, no audit, no event.
        status, version = await _status(tid, due)
        assert (status, version) == ("active", 1)
        assert await _dormancy_audits(tid) == 0
        assert await _status_events(tid, due, "dormant") == 0

        # The same run commits cleanly afterwards.
        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.transitioned == 1
        assert (await _status(tid, due))[0] == "dormant"
        assert await _dormancy_audits(tid) == 1
        assert await _status_events(tid, due, "dormant") == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — tenant scoping (issue-#17 probe)
# ---------------------------------------------------------------------------


def test_foreign_tenant_argument_transitions_nothing() -> None:
    """Running tenant B's id through tenant A's session scope must
    touch nothing: RLS masks B's settings row, so the config read
    fails CLOSED (409) before a single member is scanned — and B's
    due member stays untouched. The explicit tenant predicates keep
    this true even under a misconfigured session (v1.1 rule 4)."""

    async def run() -> None:
        tid_a, admin_a, _ = await seed_actor()
        tid_b, admin_b, _ = await seed_actor()
        await _configure_dormancy(tid_a, admin_a, months=6)
        await _configure_dormancy(tid_b, admin_b, months=6)
        due_b = await _aged_member(tid_b, name="Foreign Due")
        await _deposit_at(tid_b, due_b, "40.00", OLD)

        with pytest.raises(ConflictError, match="not configured"):
            await run_dormancy_for_tenant(_scope(tid_a), tid_b, as_of=AS_OF)
        assert (await _status(tid_b, due_b))[0] == "active"
        assert await _dormancy_audits(tid_b) == 0

        # The correctly scoped run still works.
        result = await run_dormancy_for_tenant(_scope(tid_b), tid_b, as_of=AS_OF)
        assert result.transitioned == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — exit-of-dormant end-to-end through the REAL P12 workflow
# ---------------------------------------------------------------------------


def test_dormant_member_with_balances_exits_cleanly_through_p12() -> None:
    """Request -> quorum -> settlement for a JOB-marked dormant member.

    HAND-COMPUTED settlement (the P12 oracle style): shares 2000.00 +
    deposits 5000.00 - loans 0 - fee 0 (unconfigured) = net 7000.00;
    both balances zeroed; terminal status exited. Settlement math is
    UNCHANGED by dormancy — the same components, the same posting.

    The member holds balances but has NO member-initiated transaction
    at all, which also pins the documented never-transacted branch:
    with an empty ledger the window is measured from the IMMUTABLE
    admission timestamp (created_at, backdated here), consistent with
    v1.1 rule 2 — the ledger stays append-only, so old activity can
    only be created historically, never rewritten."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _aged_member(tid, name="Dormant Exiter", deposit="5000.00", shares="2000.00")
        result = await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert result.transitioned == 1
        assert (await _status(tid, mid))[0] == "dormant"

        # The REAL P12 pipeline: request (dormant eligible) -> quorum
        # -> settlement by a non-initiating approve-holder.
        async with tenant_session(factory(), tid) as session:
            exit_record = await exits_service.request_exit(session, tid, admin_id, mid)
        assert exit_record.net_payable == Decimal("7000.00")

        v1, _ = await add_user(tid, "System Admin")
        v2, _ = await add_user(tid, "System Admin")
        async with tenant_session(factory(), tid) as session:
            await exits_service.cast_exit_vote(session, tid, v1, exit_record.id, Vote.APPROVE)
        async with tenant_session(factory(), tid) as session:
            tally = await exits_service.cast_exit_vote(
                session, tid, v2, exit_record.id, Vote.APPROVE
            )
        assert tally.status is ExitStatus.APPROVED

        async with tenant_session(factory(), tid) as session:
            fresh = await exits_service.get_exit(session, tid, exit_record.id)
            settled = await exits_service.post_settlement(
                session, tid, v1, exit_record.id, version=fresh.version, channel=Channel.BANK
            )
        assert settled.exit.status is ExitStatus.SETTLED
        assert settled.exit.net_payable == Decimal("7000.00")
        assert (await _status(tid, mid))[0] == "exited"
        for table in ("deposit_accounts", "share_accounts"):
            # Table name from test code, never user input.
            balance = await count(
                tid,
                f"SELECT count(*) FROM {table} "  # noqa: S608
                "WHERE member_id = CAST(:mid AS uuid) AND balance <> 0",
                mid=str(mid),
            )
            assert balance == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — config fail-closed: loud refusal, zero transitions
# ---------------------------------------------------------------------------


def test_unconfigured_tenant_is_refused_loudly_with_zero_transitions() -> None:
    async def run() -> None:
        # No settings row at all.
        tid, _, _ = await seed_actor()
        due = await _aged_member(tid, name="Unconfigured Due")
        await _deposit_at(tid, due, "40.00", OLD)
        with pytest.raises(ConflictError, match="not configured"):
            await run_dormancy_for_tenant(_scope(tid), tid, as_of=AS_OF)
        assert (await _status(tid, due)) == ("active", 1)
        assert await _dormancy_audits(tid) == 0

        # A settings row that does not include the dormancy key.
        tid2, admin2, _ = await seed_actor()
        async with tenant_session(factory(), tid2) as session:
            await settings_service.update_settings(
                session, tid2, admin2, version=0, changes={"registration_fee": Decimal("10.00")}
            )
        due2 = await _aged_member(tid2, name="Partial Due")
        await _deposit_at(tid2, due2, "40.00", OLD)
        with pytest.raises(ConflictError, match="not configured"):
            await run_dormancy_for_tenant(_scope(tid2), tid2, as_of=AS_OF)
        assert (await _status(tid2, due2)) == ("active", 1)

    asyncio.run(run())


def test_corrupt_stored_period_fails_closed_with_409() -> None:
    """The parse guard, probed directly (corrupt values are reachable
    only by manual SQL past the 0017 DB CHECK — the P13.8 corrupt-
    config precedent). Missing, non-numeric, and out-of-bounds values
    all refuse; the valid domain parses."""
    with pytest.raises(ConflictError, match="not configured"):
        parse_dormancy_period(None, configured=False)
    with pytest.raises(ConflictError, match="not configured"):
        parse_dormancy_period(None, configured=True)
    with pytest.raises(ConflictError, match="corrupt"):
        parse_dormancy_period("six months", configured=True)
    with pytest.raises(ConflictError, match="outside its permitted bounds"):
        parse_dormancy_period(0, configured=True)
    with pytest.raises(ConflictError, match="outside its permitted bounds"):
        parse_dormancy_period(121, configured=True)
    assert parse_dormancy_period(6, configured=True) == 6
    assert parse_dormancy_period(120, configured=True) == 120


def test_nightly_cycle_refuses_bad_tenants_loudly_and_serves_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker cycle (due-tenant discovery): tenant A unconfigured
    -> counted as a loud refusal with zero transitions; tenant B's due
    member still transitions in the same cycle. The registry is
    monkeypatched to exactly these two tenants so the assertion is
    deterministic against the shared CI database."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        tid_b, admin_b, _ = await seed_actor()
        # Cycle uses the real current date: age tenant B's member
        # relative to now (period 1 month, activity ~90 days ago).
        await _configure_dormancy(tid_b, admin_b, months=1)
        due_b = await seed_member(tid_b, name="Cycle Due")
        long_ago = datetime.now(UTC).replace(microsecond=0) - timedelta(days=90)
        async with tenant_session(factory(), tid_b) as session:
            await session.execute(
                text(
                    "UPDATE members SET created_at = :ts "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"ts": long_ago, "id": str(due_b), "tid": str(tid_b)},
            )
        await _deposit_at(tid_b, due_b, "15.00", long_ago)

        async def two_tenants(_factory: Any) -> list[uuid.UUID]:
            return [tid_a, tid_b]

        with monkeypatch.context() as m:
            m.setattr(dormancy_worker, "list_active_tenants", two_tenants)
            summary = await dormancy_worker.run_dormancy_cycle(factory())

        assert summary.tenants == 2
        assert summary.refused == 1  # tenant A, loudly, zero transitions
        assert summary.transitioned >= 1
        assert (await _status(tid_b, due_b))[0] == "dormant"
        assert await _dormancy_audits(tid_a) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# API surface: POST /members/jobs/dormancy
# ---------------------------------------------------------------------------


def test_dormancy_endpoint_runs_job_and_rejects_caller_supplied_period() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        headers = {"authorization": f"Bearer {token}"}
        await _configure_dormancy(tid, admin_id, months=6)
        due = await _aged_member(tid, name="Endpoint Due")
        await _deposit_at(tid, due, "40.00", OLD)

        async with api_client() as client:
            # The dormancy period is NEVER caller-suppliable (v1.1
            # rule 1): extra="forbid" -> 422.
            res = await client.post(
                "/members/jobs/dormancy",
                json={"dormancy_period_months": 1},
                headers=headers,
            )
            assert res.status_code == 422

            res = await client.post(
                "/members/jobs/dormancy",
                json={"as_of": "2026-08-01"},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["period_months"] == 6
            assert body["cutoff"] == "2026-02-01"
            assert body["transitioned"] == 1

            # Idempotent re-run through the API: nothing left to scan.
            res = await client.post(
                "/members/jobs/dormancy", json={"as_of": "2026-08-01"}, headers=headers
            )
            assert res.status_code == 200
            assert res.json()["scanned"] == 0

        assert (await _status(tid, due))[0] == "dormant"

        # A viewer-only role may not run the job (members:edit gate).
        _, auditor_token = await add_user(tid, "Auditor")
        async with api_client() as client:
            res = await client.post(
                "/members/jobs/dormancy",
                json={},
                headers={"authorization": f"Bearer {auditor_token}"},
            )
        assert res.status_code == 403

    asyncio.run(run())
