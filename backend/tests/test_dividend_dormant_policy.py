"""Issue #19: dormant members x dividend/rebate distributions (P13.11 x P13.13).

The maintainer policy decision, pinned mode by mode (each falsifiable —
remove the guard and the test fails; oracles hand-computed in comments;
idempotency by side-effect row counts, never return values):

  FM1  Dormant members ARE paid their exact pro-rata entitlement on
       the widened ('active','arrears','dormant') population.
  FM2  Record date: a member going dormant AFTER declaration and
       BEFORE distribution is still paid (the persisted snapshot is
       the register; dormancy is no longer a mid-run drift trigger).
  FM3  Mid-run EXIT terminates in the audited unclaimed disposition
       (test_dividends.test_member_exiting_mid_run_terminates_in_the_
       audited_unclaimed_disposition — kept beside the !30 suite it
       supersedes).
  FM4  A DV- posting to a dormant member does NOT reactivate them
       (it never goes through record_deposit) and does NOT reset the
       dormancy clock (dividend_posting stays classified SYSTEM —
       the P13.13 FM1 pin, proven here behaviourally end to end).
  FM5  Idempotent re-run of the WIDENED scan: a re-run scans nothing
       and changes no claim/posting/audit counts.
  FM6  Kill-switch mid-disposition: zero partial state, including
       zero orphan unclaimed rows/postings/events.
  FM7  Issue-#17 cross-tenant probe on the new unclaimed read/write.

Fixture history is written through the REAL P7 posting contract,
configuration through the REAL P13.7 writer, dormancy through the REAL
P13.13 job, and exits through the REAL P12 workflow — never raw state
flips on the paths under test.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import factory
from export_helpers import count, seed_actor
from genesis.application import dividends as dividends_module
from genesis.application import tenant_settings as settings_service
from genesis.application.dividends import (
    DeclarationRecord,
    DeclarationStatus,
    declare_dividend,
    distribute_dividend,
    unclaimed_scan_sql,
)
from genesis.application.dormancy import run_dormancy_for_tenant
from genesis.application.ledger import post_deposit
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session
from test_dividends import (
    FY,
    TODAY,
    _approve,
    _balance,
    _configure_dividends,
    _exit_via_p12,
    _member_with_share_history,
    _scope,
    _sum,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside a batch transaction, before commit."""


# ---------------------------------------------------------------------------
# Helpers (REAL P13.7 config writer + REAL P13.13 dormancy job)
# ---------------------------------------------------------------------------


async def _configure_dormancy(tid: uuid.UUID, admin_id: uuid.UUID, *, months: int = 6) -> None:
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
        await settings_service.update_settings(
            session,
            tid,
            admin_id,
            version=current.version,
            changes={"dormancy_period_months": months},
        )


async def _backdate_member(tid: uuid.UUID, mid: uuid.UUID, *, to: date) -> None:
    """Age a member's immutable admission timestamp (test seeding only:
    the dormancy scan requires created_at < cutoff before it will even
    consider a member — the P13.13 fixture convention)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE members SET created_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"ts": datetime.combine(to, time(12, 0), tzinfo=UTC), "id": str(mid), "tid": str(tid)},
        )


async def _status(tid: uuid.UUID, mid: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                text(
                    "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(mid), "tid": str(tid)},
            )
        ).scalar_one()
    return str(value)


async def _paid_then_exited() -> tuple[
    uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, DeclarationRecord
]:
    """The disposition-due state shared by FM6/FM7: A paid, B exited
    mid-run and still owed.

    HAND-COMPUTED: A 12,000.00 shares all 365 days -> dividend 600.00;
    B 6,000.00 all year -> 300.00; no deposits -> rebates 0.00;
    eligible 2, payout 900.00. The first run pays A while B's member
    row is locked elsewhere (the mid-run window); B then exits through
    the REAL P12 workflow (6,000.00 equity refunded, balances zeroed,
    member_exits.settled_at stamped AFTER the declaration).
    """
    tid, admin_id, _ = await seed_actor()
    await _configure_dividends(tid, admin_id)
    m_a = await _member_with_share_history(tid, amount="12000.00", on=FY.start, name="Stays")
    m_b = await _member_with_share_history(tid, amount="6000.00", on=FY.start, name="Leaves")
    record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
    assert record.eligible_members == 2
    assert record.total_payout == Decimal("900.00")
    await _approve(tid, record.id)
    holder = tenant_session(factory(), tid)
    session = await holder.__aenter__()
    await session.execute(
        text(
            "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
            "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
        ),
        {"m": str(m_b), "tid": str(tid)},
    )
    try:
        first = await distribute_dividend(_scope(tid), tid, None, record.id)
    finally:
        await holder.__aexit__(None, None, None)
    assert first.claimed == 1  # A; B was SKIP LOCKED-skipped
    assert first.status is DeclarationStatus.APPROVED
    await _exit_via_p12(tid, m_b)
    return tid, admin_id, m_a, m_b, record


# ---------------------------------------------------------------------------
# FM1 — dormant members are paid their exact pro-rata entitlement
# ---------------------------------------------------------------------------


def test_fm1_dormant_members_are_paid_their_exact_pro_rata_entitlement() -> None:
    """Issue #19 P1: dormant members remain shareholders and are paid
    on their share basis — including exact pro-rata for a mid-year
    joiner (the !30 oracle style applied to DORMANT members).

    HAND-COMPUTED (FY 2025-07-01..2026-06-30, 365 days, dividend 5%):
      * D1: 12,000.00 shares from day one, dormant -> ADB 12,000.00
        -> dividend 600.00 -> balance 12,600.00.
      * D2: 12,000.00 shares from 2026-01-01, dormant -> held 181 days
        -> ADB 12,000 x 181/365 = 5,950.68 -> dividend 297.53 ->
        balance 12,297.53.
    Both go dormant through the REAL P13.13 job (as_of 2026-08-01,
    period 6 months -> cutoff 2026-02-01: their only member-initiated
    activity, the SH- funding, is outside the window). Falsifiable:
    restore the ('active','arrears') population (scan predicate or
    partial index) and eligible_members == 2 / the balances fail.
    """

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        await _configure_dormancy(tid, admin_id, months=6)
        d_one = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Dormant Full Year"
        )
        d_two = await _member_with_share_history(
            tid, amount="12000.00", on=date(2026, 1, 1), name="Dormant Mid Year"
        )
        await _backdate_member(tid, d_one, to=FY.start)
        await _backdate_member(tid, d_two, to=date(2026, 1, 1))
        job = await run_dormancy_for_tenant(_scope(tid), tid, as_of=TODAY)
        assert job.transitioned == 2
        assert await _status(tid, d_one) == "dormant"
        assert await _status(tid, d_two) == "dormant"

        # The eligibility snapshot the declaration binds to (v1.1 rule
        # 3) counts the dormant shareholders: 600.00 + 297.53 = 897.53.
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 2
        assert record.total_share_basis == Decimal("17950.68")
        assert record.total_dividend == Decimal("897.53")
        assert record.total_payout == Decimal("897.53")

        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 2
        assert result.unclaimed == 0
        assert result.pending_members == 0
        assert result.status is DeclarationStatus.DISTRIBUTED

        assert await _balance(tid, d_one, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, d_two, table="share_accounts") == Decimal("12297.53")
        # Paid dispositions on the claim rows; conservation by
        # side-effect rows (SUM(postings) == declared total).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions WHERE disposition = 'paid'",
            )
            == 2
        )
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "JOIN transactions t ON t.id = le.transaction_id "
            "WHERE t.type = 'dividend_posting' AND le.side = 'credit' "
            "AND le.account = 'member.shares'",
        ) == Decimal("897.53")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — record date: dormant-mid-run is still paid
# ---------------------------------------------------------------------------


def test_fm2_member_going_dormant_between_declaration_and_distribution_is_paid() -> None:
    """Issue #19 P2: entitlement follows the persisted declaration
    snapshot (the record date). M is ACTIVE at declaration, goes
    dormant through the REAL P13.13 job AFTER committee approval, and
    is still paid their hand-computed entitlement: 12,000.00 shares
    all 365 days -> 600.00 -> balance 12,600.00. With the widened
    population this happens naturally: the first-run snapshot
    re-verification recomputes over ('active','arrears','dormant') and
    still matches. Falsifiable: restore the ('active','arrears')
    population and the verification 409s (or M is never scanned) —
    dormancy would again be a mid-run drift trigger.
    """

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        await _configure_dormancy(tid, admin_id, months=6)
        mid = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Dorms Mid Run"
        )
        await _backdate_member(tid, mid, to=FY.start)

        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 1
        assert record.total_dividend == Decimal("600.00")
        await _approve(tid, record.id)
        assert await _status(tid, mid) == "active"  # on the register at record date

        # The drift window: M goes dormant AFTER approval and BEFORE
        # distribution (the REAL nightly job, cutoff 2026-02-01).
        job = await run_dormancy_for_tenant(_scope(tid), tid, as_of=TODAY)
        assert job.transitioned == 1
        assert await _status(tid, mid) == "dormant"

        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 1
        assert result.pending_members == 0
        assert result.status is DeclarationStatus.DISTRIBUTED
        assert await _balance(tid, mid, table="share_accounts") == Decimal("12600.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — a DV- posting never reactivates and never resets the clock
# ---------------------------------------------------------------------------


def test_fm4_dividend_posting_never_reactivates_and_never_resets_the_dormancy_clock() -> None:
    """Issue #19 P4, both invariants end to end:

    (a) No reactivation: the distribution credits a dormant member's
        share balance directly (never through record_deposit), so M
        stays 'dormant' with ZERO reactivation audit rows and ZERO
        member.status_changed outbox events to 'active'. Falsifiable:
        route the credit through record_deposit and every assert
        below fails.
    (b) No clock reset (the P13.13 FM1 pin, proven behaviourally): X
        is paid while ACTIVE with a DV- posting stamped at FY end
        2026-06-30; a later dormancy run (as_of 2026-10-02, 6 months
        -> cutoff HAND-COMPUTED 2026-04-02) sees X's only in-window
        transaction is that DV- posting — X goes dormant anyway,
        because dividend_posting is classified SYSTEM. Falsifiable:
        add dividend_posting to MEMBER_INITIATED and X stays active
        (and the exact-set pin in test_ledger_domain.py fails first).

    HAND-COMPUTED figures: M and X each hold 12,000.00 shares all 365
    days at 5% -> 600.00 each; declared payout 1,200.00 (rebate 0%).
    X's in-window activity at first: a 40.00 deposit on 2026-03-01
    (>= cutoff 2026-02-01 -> stays active on the first run).
    """

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id, dividend="5.00", rebate="0.00")
        await _configure_dormancy(tid, admin_id, months=6)
        m_dormant = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Dormant Payee"
        )
        m_active = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Active Payee"
        )
        await _backdate_member(tid, m_dormant, to=FY.start)
        await _backdate_member(tid, m_active, to=FY.start)
        # X's member-initiated activity inside the first window.
        async with tenant_session(factory(), tid) as session:
            await post_deposit(
                session,
                tid,
                m_active,
                Decimal("40.00"),
                Channel.BANK,
                occurred_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            )

        job_one = await run_dormancy_for_tenant(_scope(tid), tid, as_of=TODAY)
        assert job_one.transitioned == 1  # only M; X deposited in-window
        assert await _status(tid, m_dormant) == "dormant"
        assert await _status(tid, m_active) == "active"

        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 2
        assert record.total_dividend == Decimal("1200.00")
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 2
        assert result.status is DeclarationStatus.DISTRIBUTED
        assert await _balance(tid, m_dormant, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, m_active, table="share_accounts") == Decimal("12600.00")

        # (a) M was paid but NOT reactivated: status unchanged, zero
        # reactivation audit rows, zero status_changed-to-active
        # events; the only status event in the tenant is the job's
        # active -> dormant transition for M.
        assert await _status(tid, m_dormant) == "dormant"
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'member.status' "
                "AND after->>'reason' = 'deposit_reactivation'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events "
                "WHERE event_type = 'member.status_changed' AND payload->>'to' = 'active'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'member.status_changed'",
            )
            == 1
        )

        # (b) The DV- posting (occurred_at 2026-06-30, inside the next
        # window) does NOT keep X active: cutoff 2026-10-02 minus 6
        # months = 2026-04-02; X's deposit (2026-03-01) is now outside,
        # the DV- inside — and X goes dormant anyway.
        job_two = await run_dormancy_for_tenant(_scope(tid), tid, as_of=date(2026, 10, 2))
        assert job_two.transitioned == 1  # X; M is already dormant (not rescanned)
        assert await _status(tid, m_active) == "dormant"
        assert await _status(tid, m_dormant) == "dormant"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — idempotent re-run of the widened scan
# ---------------------------------------------------------------------------


def test_fm5_rerun_of_the_widened_scan_is_a_lock_free_noop_by_side_effect_counts() -> None:
    """v1.1 rule 8 on the WIDENED population: a re-run while the paid
    dormant member is excluded by the claim anti-join scans NOTHING
    and changes no side-effect counts; after completion the year is
    closed and further runs are refused with counts still unchanged.

    HAND-COMPUTED: D (dormant) and A (active) each 12,000.00 all year
    at 5% -> 600.00 each. Falsifiable: drop the NOT EXISTS anti-join
    and the second run re-scans (and re-locks) the paid member.
    """

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id, dividend="5.00", rebate="0.00")
        await _configure_dormancy(tid, admin_id, months=6)
        d_mid = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Dormant Paid"
        )
        a_mid = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Active Held"
        )
        await _backdate_member(tid, d_mid, to=FY.start)
        job = await run_dormancy_for_tenant(_scope(tid), tid, as_of=TODAY)
        assert job.transitioned == 1  # only D was backdated into the window
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 2
        await _approve(tid, record.id)

        # Run 1 and re-run 2 both while A's row lock is held: run 1
        # pays D; run 2 scans ZERO rows (D excluded by the anti-join
        # WITHOUT being locked, A excluded by SKIP LOCKED) and changes
        # nothing — by claim/posting/audit side-effect counts.
        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        await session.execute(
            text(
                "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(a_mid), "tid": str(tid)},
        )
        try:
            first = await distribute_dividend(_scope(tid), tid, None, record.id)
            assert first.claimed == 1
            assert first.status is DeclarationStatus.APPROVED
            second = await distribute_dividend(_scope(tid), tid, None, record.id)
        finally:
            await holder.__aexit__(None, None, None)
        assert second.scanned == 0
        assert second.claimed == 0
        assert second.unclaimed == 0
        for probe, expected in (
            ("SELECT count(*) FROM dividend_distributions", 1),
            ("SELECT count(*) FROM transactions WHERE type = 'dividend_posting'", 1),
            (
                "SELECT count(*) FROM audit_log WHERE action = 'dividend_distribution.post'",
                1,
            ),
        ):
            assert await count(tid, probe) == expected

        # The released remainder completes the run; the closed year
        # then refuses further runs with the claim count unchanged at
        # exactly the two paid claims.
        third = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert third.claimed == 1
        assert third.status is DeclarationStatus.DISTRIBUTED
        with pytest.raises(ConflictError, match="only approved"):
            await distribute_dividend(_scope(tid), tid, None, record.id)
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 2
        assert await _balance(tid, d_mid, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, a_mid, table="share_accounts") == Decimal("12600.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — kill-switch mid-disposition: zero orphan unclaimed state
# ---------------------------------------------------------------------------


def test_fm6_kill_switch_mid_disposition_leaves_zero_orphan_unclaimed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash INSIDE the disposition batch after the unclaimed claim
    insert and the payable posting, before commit (the audit writer
    raises): ZERO partial state — no orphan unclaimed row, no payable
    posting, no alert event, no balance drift; the clean re-run then
    disposes exactly once. Falsifiable: commit the claim in its own
    transaction and the orphan survives the crash.

    HAND-COMPUTED (from _paid_then_exited): B is owed 300.00
    (6,000.00 all year at 5%); A's paid claim (600.00) is untouched
    throughout.
    """

    async def run() -> None:
        tid, _, m_a, m_b, record = await _paid_then_exited()

        real_record_audit = dividends_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "dividend_distribution.unclaimed":
                raise _KillSwitchError()  # crash inside the batch transaction
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(dividends_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await distribute_dividend(_scope(tid), tid, None, record.id)

        # ZERO partial disposition state, by side-effect counts; A's
        # paid claim from the first run is intact.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions WHERE disposition = 'unclaimed'",
            )
            == 0
        )
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 1
        assert (
            await count(
                tid,
                "SELECT count(*) FROM ledger_entries "
                "WHERE account = 'liability.unclaimed_dividends'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM outbox_events WHERE event_type = 'dividend.unclaimed'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'dividend_distribution.unclaimed'",
            )
            == 0
        )

        # The clean re-run disposes B exactly once and closes the run.
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.unclaimed == 1
        assert result.status is DeclarationStatus.DISTRIBUTED
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "WHERE le.account = 'liability.unclaimed_dividends' AND le.side = 'credit'",
        ) == Decimal("300.00")
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, m_b, table="share_accounts") == Decimal("0.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — issue-#17 cross-tenant probe on the new read/write
# ---------------------------------------------------------------------------


def test_fm7_foreign_tenant_argument_disposes_and_reads_nothing() -> None:
    """Session opened AS the rows' own tenant (RLS satisfied), foreign
    tenant_id ARGUMENT: only the explicit bound predicates can refuse
    — and they do, on both the new read (unclaimed scan) and the new
    write path (the disposition run). Remove the tenant predicate
    under test and this fails; the same probe against the real tenant
    id returns the owed member, proving the fixture would catch it.
    """

    async def run() -> None:
        tid, _, _, m_b, record = await _paid_then_exited()
        foreign = uuid.uuid4()

        # Write path: the declaration is invisible to a foreign tenant
        # argument (404); nothing is disposed.
        with pytest.raises(NotFoundError):
            await distribute_dividend(_scope(tid), foreign, None, record.id)
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions WHERE disposition = 'unclaimed'",
            )
            == 0
        )

        # Read path: the production disposition scan with a foreign
        # tenant argument yields zero rows; the real tenant id yields
        # exactly the owed member (falsifiability sanity check).
        for probe_tid, expected in ((foreign, 0), (tid, 1)):
            async with tenant_session(factory(), tid) as session:
                rows = (
                    await session.execute(
                        text(unclaimed_scan_sql(with_after=False)),
                        {
                            "tid": str(probe_tid),
                            "decl": str(record.id),
                            "declared_at": record.created_at,
                            "limit": 10,
                        },
                    )
                ).all()
            assert len(rows) == expected, f"probe for {probe_tid} returned {len(rows)}"
        assert [str(r[0]) for r in rows] == [str(m_b)]

        # And the legitimate run still disposes exactly once.
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.unclaimed == 1
        assert result.status is DeclarationStatus.DISTRIBUTED

    asyncio.run(run())
