"""Integration tests for P13.11: dividend declaration & distribution.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Every numbered banking-grade failure mode from the P13.11 brief has a
falsifiable test here (remove the guard and the test fails):

  1.  Penny conservation — hand-computed three-member oracle: the
      declared total IS the sum of the once-rounded per-member figures
      (zero residue by construction), and SUM(postings) equals it
      exactly after distribution.
  2.  Basis gaming — a member funding on day one and a mid-year joiner
      with IDENTICAL current balances earn different, exactly pro-rata
      dividends (ledger-reconstructed ADB, never a snapshot).
      Falsifiable: switch the basis to share_accounts.balance and the
      joiner collects a full year.
  3.  Double distribution — concurrent double-run yields exactly one
      claim/posting/audit row per member, proven by side-effect counts.
  4.  Approval TOCTOU — a P13.7 rate change after approval never
      changes what is posted (snapshot figures, recorded verbatim);
      population/basis drift 409s with nothing posted; the approved
      snapshot is write-once at the database level.
  5.  Wrong population — exited members are never scanned; zero-ADB
      members get no claim and no 0.00 posting; arrears members with a
      basis are paid (documented allow-list).
  6.  Missing/NULL config fails closed — refused, not defaulted, not a
      500; corrupt config is unit-tested in test_dividends_domain.
  8.  Distribution-vs-lock race — a held member row lock is SKIP
      LOCKED-skipped and picked up by an idempotent re-run; exactly
      once across the race by claim counts.
  9.  Partial batch state — kill-switch mid-batch leaves zero partial
      state; the same run commits cleanly afterwards.
  10. (Scan performance lives in tests/test_p1311_explain.py.)

Idempotency is asserted via side-effect row counts (claims, ledger,
audit), never via return values alone (MASTER_PROMPT section 4).
Fixture history is written through the REAL P7 posting contract and
configuration through the REAL P13.7 writer — never raw SQL.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import dividends as dividends_module
from genesis.application import tenant_settings as settings_service
from genesis.application.dividends import (
    DeclarationStatus,
    cast_dividend_vote,
    compute_declaration_totals,
    declare_dividend,
    distribute_dividend,
    void_declaration,
)
from genesis.application.ledger import post_deposit, post_share_topup
from genesis.application.period_balances import average_daily_balance
from genesis.domain.committee import Vote
from genesis.domain.dividends import FinancialYear
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError, ForbiddenError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside a batch transaction, before commit."""


# The financial year under test: end month 6, declared as of 2026-08-01
# -> FY 2025-07-01 .. 2026-06-30, 365 days (see test_dividends_domain).
FY_END_MONTH = 6
FY = FinancialYear(start=date(2025, 7, 1), end=date(2026, 6, 30))
TODAY = date(2026, 8, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(tid: uuid.UUID) -> partial[Any]:
    return partial(tenant_session, factory(), tid)


async def _configure_dividends(
    tid: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    dividend: str = "5.00",
    rebate: str = "2.00",
) -> None:
    """Configure the three dividend keys through the REAL P13.7 writer."""
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
        await settings_service.update_settings(
            session,
            tid,
            admin_id,
            version=current.version,
            changes={
                "dividend_rate_pct": Decimal(dividend),
                "deposit_rebate_rate_pct": Decimal(rebate),
                "financial_year_end_month": FY_END_MONTH,
            },
        )


async def _member_with_share_history(
    tid: uuid.UUID, *, amount: str, on: date, name: str = "Share Member"
) -> uuid.UUID:
    """Member whose share balance was funded on `on` via the P7 contract.

    transactions is append-only (gate 1.5), so past history is written
    exactly as production wrote it: a real SH- posting with an explicit
    occurred_at; the current balance is seeded to match.
    """
    mid = await seed_member(tid, name=name, shares=amount)
    async with tenant_session(factory(), tid) as session:
        await post_share_topup(
            session,
            tid,
            mid,
            Decimal(amount),
            Channel.BANK,
            occurred_at=datetime.combine(on, time(12, 0), tzinfo=UTC),
        )
    return mid


async def _member_with_deposit_history(
    tid: uuid.UUID, *, amount: str, on: date, name: str = "Deposit Member"
) -> uuid.UUID:
    mid = await seed_member(tid, name=name, deposit=amount)
    async with tenant_session(factory(), tid) as session:
        await post_deposit(
            session,
            tid,
            mid,
            Decimal(amount),
            Channel.BANK,
            occurred_at=datetime.combine(on, time(12, 0), tzinfo=UTC),
        )
    return mid


async def _approve(tid: uuid.UUID, declaration_id: uuid.UUID) -> None:
    """Quorum-approve through the real P9 voting machinery (quorum 2)."""
    voter_one, _ = await add_user(tid, "Branch Manager")
    voter_two, _ = await add_user(tid, "Credit Committee")
    async with tenant_session(factory(), tid) as session:
        await cast_dividend_vote(session, tid, voter_one, declaration_id, Vote.APPROVE)
    async with tenant_session(factory(), tid) as session:
        tally = await cast_dividend_vote(session, tid, voter_two, declaration_id, Vote.APPROVE)
    assert tally.status is DeclarationStatus.APPROVED


async def _sum(tid: uuid.UUID, sql: str, **params: object) -> Decimal:
    """Decimal aggregate (the shared count() coerces to int)."""
    async with tenant_session(factory(), tid) as session:
        value = (await session.execute(text(sql), params)).scalar_one()
    return Decimal(str(value))


async def _balance(tid: uuid.UUID, mid: uuid.UUID, *, table: str) -> Decimal:
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                # Table name from test code, never user input.
                text(
                    f"SELECT balance FROM {table} "  # noqa: S608
                    "WHERE member_id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(mid), "tid": str(tid)},
            )
        ).scalar_one()
    return Decimal(str(value))


async def _oracle_tenant() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """The hand-computed three-member oracle (documented in comments).

    FY 2025-07-01..2026-06-30 (365 days), dividend 5%, rebate 2%:

      * A: 12,000.00 shares from day one (2025-07-01) -> held all 365
        days -> share ADB 12,000.00 -> dividend 600.00.
      * B (mid-year joiner): 12,000.00 shares from 2026-01-01 -> held
        181 days (Jan 31 + Feb 28 + Mar 31 + Apr 30 + May 31 + Jun 30)
        -> ADB = 12,000 x 181 / 365 = 5,950.6849.. -> 5,950.68 ->
        dividend = 5,950.68 x 5% = 297.534 -> 297.53. EXACTLY pro-rata
        by days, despite a current balance identical to A's.
      * C: 7,300.00 deposit from 2026-04-02 -> held 90 days (Apr 29 +
        May 31 + Jun 30) -> ADB = 7,300 x 90 / 365 = 1,800.00 ->
        rebate = 1,800.00 x 2% = 36.00. No shares -> dividend 0.

    Declared totals (DERIVED from the rounded per-member figures —
    the zero-residue rule): eligible 3, share basis 17,950.68, deposit
    basis 1,800.00, dividend 897.53, rebate 36.00, payout 933.53.
    """
    tid, admin_id, _ = await seed_actor()
    await _configure_dividends(tid, admin_id)
    m_a = await _member_with_share_history(tid, amount="12000.00", on=FY.start, name="Member A")
    m_b = await _member_with_share_history(
        tid, amount="12000.00", on=date(2026, 1, 1), name="Member B"
    )
    m_c = await _member_with_deposit_history(
        tid, amount="7300.00", on=date(2026, 4, 2), name="Member C"
    )
    return tid, admin_id, m_a, m_b, m_c


# ---------------------------------------------------------------------------
# 1 + 2. The dividend oracle: conservation, pro-rata, basis gaming
# ---------------------------------------------------------------------------


def test_declaration_snapshot_matches_the_hand_computed_oracle() -> None:
    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert (record.fy_start, record.fy_end) == (FY.start, FY.end)
        assert record.dividend_rate_pct == Decimal("5.00")
        assert record.rebate_rate_pct == Decimal("2.00")
        assert record.eligible_members == 3
        assert record.total_share_basis == Decimal("17950.68")
        assert record.total_deposit_basis == Decimal("1800.00")
        assert record.total_dividend == Decimal("897.53")
        assert record.total_rebate == Decimal("36.00")
        assert record.total_payout == Decimal("933.53")
        assert record.status is DeclarationStatus.DECLARED

    asyncio.run(run())


def test_distribution_pays_the_oracle_figures_and_conserves_every_cent() -> None:
    """EXIT criterion: the mid-year joiner earns exactly pro-rata, and
    SUM(member postings) == the declared total with zero drift."""

    async def run() -> None:
        tid, admin_id, m_a, m_b, m_c = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 3
        assert result.dividend_total == Decimal("897.53")
        assert result.rebate_total == Decimal("36.00")
        assert result.payout_total == Decimal("933.53")
        assert result.pending_members == 0
        assert result.status is DeclarationStatus.DISTRIBUTED

        # Balances: dividend capitalises into shares, rebate into deposits.
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, m_b, table="share_accounts") == Decimal("12297.53")
        assert await _balance(tid, m_c, table="deposit_accounts") == Decimal("7336.00")

        # Penny conservation by SIDE-EFFECT rows (never return values):
        # the ledger's member.shares credits across DV- postings sum to
        # the declared dividend, member.deposits credits to the rebate,
        # and the claim rows to the payout — all exactly.
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "JOIN transactions t ON t.id = le.transaction_id "
            "WHERE t.type = 'dividend_posting' AND le.side = 'credit' "
            "AND le.account = 'member.shares'",
        ) == Decimal("897.53")
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "JOIN transactions t ON t.id = le.transaction_id "
            "WHERE t.type = 'dividend_posting' AND le.side = 'credit' "
            "AND le.account = 'member.deposits'",
        ) == Decimal("36.00")
        assert await _sum(
            tid, "SELECT COALESCE(SUM(total_amount), 0) FROM dividend_distributions"
        ) == Decimal("933.53")

        # Postings carry occurred_at at the very end of the FY (1.5)
        # and DV- references from the P7 generator.
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT txn_ref, occurred_at FROM transactions "
                        "WHERE type = 'dividend_posting' ORDER BY txn_ref"
                    )
                )
            ).all()
        assert [str(r[0]) for r in rows] == ["DV-000001", "DV-000002", "DV-000003"]
        for _, occurred in rows:
            assert occurred.date() == FY.end
            assert (occurred.hour, occurred.minute) == (23, 59)

        # ... so the dividend COMPOUNDS into the next period's basis:
        # A's reconstructed end-of-day balance on the first day of the
        # next FY is 12,600.00 (hand-computed: 12,000 + 600 dividend).
        async with tenant_session(factory(), tid) as session:
            next_fy_day_one = await average_daily_balance(
                session,
                tid,
                m_a,
                kind="share",
                start=date(2026, 7, 1),
                end=date(2026, 7, 1),
            )
        assert next_fy_day_one == Decimal("12600.00")

    asyncio.run(run())


def test_leap_year_fy_and_last_day_parking_earn_exactly_pro_rata() -> None:
    """Failure mode 2 pin: a December FY containing 29-Feb has 366
    days, and shares parked on the FY's LAST day earn exactly 1/366 of
    a full year. Hand-computed: 3,660.00 held 1 of 366 days -> ADB =
    3,660/366 = 10.00 -> dividend at 10% = 1.00. Falsifiable: a
    point-in-time basis pays 366.00."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        async with tenant_session(factory(), tid) as session:
            current = await settings_service.get_settings(session, tid)
            await settings_service.update_settings(
                session,
                tid,
                admin_id,
                version=current.version,
                changes={
                    "dividend_rate_pct": Decimal("10.00"),
                    "deposit_rebate_rate_pct": Decimal("0.00"),
                    "financial_year_end_month": 12,
                },
            )
        mid = await _member_with_share_history(
            tid, amount="3660.00", on=date(2024, 12, 31), name="Parker"
        )
        record = await declare_dividend(_scope(tid), tid, admin_id, today=date(2025, 3, 1))
        assert (record.fy_start, record.fy_end) == (date(2024, 1, 1), date(2024, 12, 31))
        assert record.total_share_basis == Decimal("10.00")
        assert record.total_dividend == Decimal("1.00")
        assert record.total_payout == Decimal("1.00")
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 1
        assert await _balance(tid, mid, table="share_accounts") == Decimal("3661.00")

    asyncio.run(run())


def test_totals_are_the_sum_of_rounded_figures_not_a_rounded_pot() -> None:
    """Failure mode 1, the residue oracle: two members each with a
    101.00 share ADB at 2.50% earn 2.525 -> 2.53 EACH (rounded exactly
    once per member), so the declared total is 5.06 — a naive pot
    allocation (202.00 x 2.5% = 5.05) differs by a cent and could
    never reconcile with the member postings. Falsifiable: derive the
    total from the summed bases instead and this fails by 0.01."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id, dividend="2.50", rebate="0.00")
        m_one = await _member_with_share_history(tid, amount="101.00", on=FY.start, name="One")
        m_two = await _member_with_share_history(tid, amount="101.00", on=FY.start, name="Two")
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.total_share_basis == Decimal("202.00")
        assert record.total_dividend == Decimal("5.06")  # NOT the pot's 5.05
        assert record.total_payout == Decimal("5.06")

        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 2
        assert result.status is DeclarationStatus.DISTRIBUTED
        # Conservation: SUM(member postings) == the declared total
        # exactly (zero residue by construction, by side-effect rows).
        assert await _sum(
            tid,
            "SELECT COALESCE(SUM(le.amount), 0) FROM ledger_entries le "
            "JOIN transactions t ON t.id = le.transaction_id "
            "WHERE t.type = 'dividend_posting' AND le.side = 'credit' "
            "AND le.account = 'member.shares'",
        ) == Decimal("5.06")
        assert await _balance(tid, m_one, table="share_accounts") == Decimal("103.53")
        assert await _balance(tid, m_two, table="share_accounts") == Decimal("103.53")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. Double distribution — concurrent double-run
# ---------------------------------------------------------------------------


def test_concurrent_double_run_pays_every_member_exactly_once() -> None:
    """EXIT criterion: double distribution is impossible, proven by
    claim/ledger/audit side-effect counts across a concurrent race.
    Falsifiable: drop uq_dividend_distributions_claim (or the rowcount
    check) and members are paid twice."""

    async def run() -> None:
        tid, admin_id, m_a, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        results = await asyncio.wait_for(
            asyncio.gather(
                distribute_dividend(_scope(tid), tid, None, record.id),
                distribute_dividend(_scope(tid), tid, None, record.id),
            ),
            timeout=60,
        )
        assert sum(r.claimed for r in results) == 3  # exactly once, combined
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 3
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions WHERE transaction_id IS NULL",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 3
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'dividend_distribution.post'",
            )
            == 3
        )
        # And the money moved exactly once: A holds 12,600.00, not 13,200.00.
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. Approval TOCTOU
# ---------------------------------------------------------------------------


def test_rate_change_after_approval_posts_the_snapshot_figures() -> None:
    """A P13.7 rate change applies to FUTURE declarations only: the old
    approval pays the SNAPSHOT figures, recorded verbatim in every
    claim and audit row. Falsifiable: re-read the live config inside
    the distribution and A collects 1,200.00."""

    async def run() -> None:
        tid, admin_id, m_a, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        # Double the dividend rate through the real writer AFTER approval.
        async with tenant_session(factory(), tid) as session:
            current = await settings_service.get_settings(session, tid)
            await settings_service.update_settings(
                session,
                tid,
                admin_id,
                version=current.version,
                changes={"dividend_rate_pct": Decimal("10.00")},
            )

        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 3
        assert result.dividend_total == Decimal("897.53")  # 5%, not 10%
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")

        # Every claim row records the snapshot rate verbatim (rule 7).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions "
                "WHERE dividend_rate_pct = CAST('5.00' AS numeric)",
            )
            == 3
        )
        # ... and every audit row carries the declaration id + figures.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'dividend_distribution.post' "
                "AND after->>'declaration_id' = :did "
                "AND after->>'dividend_rate_pct' = '5.00'",
                did=str(record.id),
            )
            == 3
        )

    asyncio.run(run())


def test_population_drift_after_approval_returns_409_and_posts_nothing() -> None:
    """The committee approved THOSE totals: a member (with FY basis)
    appearing after approval is drift -> 409, zero claims, zero
    postings. Falsifiable: remove _verify_snapshot and the run pays."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        # Drift: a fourth member with in-FY share history (backdated
        # through the real P7 contract) joins the eligible set.
        await _member_with_share_history(tid, amount="1000.00", on=date(2026, 6, 1), name="Drifter")

        with pytest.raises(ConflictError, match="stale"):
            await distribute_dividend(_scope(tid), tid, None, record.id)
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 0
        )

        # Escape hatch: void, then redeclare capturing the new population.
        fresh = await get_fresh(tid, record.id)
        async with tenant_session(factory(), tid) as session:
            await void_declaration(session, tid, admin_id, record.id, version=fresh)
        redeclared = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert redeclared.eligible_members == 4

    asyncio.run(run())


async def get_fresh(tid: uuid.UUID, declaration_id: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        record = await dividends_module.get_declaration(session, tid, declaration_id)
    return record.version


def test_approved_snapshot_is_write_once_at_the_database_level() -> None:
    """0020 trigger: mutating a snapshot column — even by manual SQL
    through the app role — fails loudly. Falsifiable: drop the
    dividend_declarations_write_once trigger and the UPDATE succeeds."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE dividend_declarations "
                        "SET dividend_rate_pct = CAST('9.00' AS numeric) "
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"id": str(record.id), "tid": str(tid)},
                )

    asyncio.run(run())


def test_void_racing_the_first_distribution_batch_pays_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding F1 (the void-vs-distribute TOCTOU): the opening
    status check releases its row lock with its session, so a void —
    legal while no claim exists — can commit between the snapshot
    verification and the first batch. The per-batch FOR SHARE status
    re-check refuses with NOTHING posted, and the freed FY slot
    re-declares cleanly. Falsifiable: remove the per-batch status
    re-check and the run pays a REJECTED declaration (then the
    redeclaration would pay the year twice)."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        real_verify = dividends_module._verify_snapshot

        async def verify_then_void(*args: Any, **kwargs: Any) -> None:
            await real_verify(*args, **kwargs)
            # The race window: a void lands after verification passed,
            # before the first batch claims anything.
            async with tenant_session(factory(), tid) as session:
                await void_declaration(
                    session,
                    tid,
                    admin_id,
                    record.id,
                    version=await get_fresh(tid, record.id),
                )

        with monkeypatch.context() as m:
            m.setattr(dividends_module, "_verify_snapshot", verify_then_void)
            with pytest.raises(ConflictError, match="voided"):
                await distribute_dividend(_scope(tid), tid, None, record.id)

        # ZERO side effects for the rejected declaration.
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 0
        )
        # No member was paid, so the fresh declaration is the year's first.
        fresh = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert fresh.eligible_members == 3

    asyncio.run(run())


def test_one_live_declaration_per_financial_year() -> None:
    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        with pytest.raises(ConflictError, match="already exists"):
            await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        # Voiding frees the FY slot for a fresh declaration.
        async with tenant_session(factory(), tid) as session:
            await void_declaration(session, tid, admin_id, record.id, version=record.version)
        fresh = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert fresh.id != record.id

    asyncio.run(run())


def test_a_distributed_financial_year_can_never_be_redeclared_or_voided() -> None:
    """uq_dividend_declarations_fy covers every non-rejected status:
    once a year is DISTRIBUTED its slot never frees — a second
    declaration for the same FY is refused, and the terminal status
    cannot be voided back open. Falsifiable: scope the unique index to
    'declared' only and the redeclaration lands (the year paid twice)."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.status is DeclarationStatus.DISTRIBUTED

        with pytest.raises(ConflictError, match="already exists"):
            await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        with pytest.raises(ConflictError, match="cannot move"):
            async with tenant_session(factory(), tid) as session:
                await void_declaration(
                    session,
                    tid,
                    admin_id,
                    record.id,
                    version=await get_fresh(tid, record.id),
                )
        assert await count(tid, "SELECT count(*) FROM dividend_declarations") == 1
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 3

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Voting (P9 machinery)
# ---------------------------------------------------------------------------


def test_voting_separation_of_duties_double_votes_and_rejection() -> None:
    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)

        # The declarer can never vote on their own declaration.
        with pytest.raises(ForbiddenError, match="cannot vote"):
            async with tenant_session(factory(), tid) as session:
                await cast_dividend_vote(session, tid, admin_id, record.id, Vote.APPROVE)

        voter_one, _ = await add_user(tid, "Branch Manager")
        voter_two, _ = await add_user(tid, "Credit Committee")
        async with tenant_session(factory(), tid) as session:
            tally = await cast_dividend_vote(session, tid, voter_one, record.id, Vote.REJECT)
        assert tally.decision is None  # quorum 2 not yet reached

        # One vote per voter, enforced by the DB UNIQUE.
        with pytest.raises(ConflictError, match="already voted"):
            async with tenant_session(factory(), tid) as session:
                await cast_dividend_vote(session, tid, voter_one, record.id, Vote.REJECT)

        async with tenant_session(factory(), tid) as session:
            tally = await cast_dividend_vote(session, tid, voter_two, record.id, Vote.REJECT)
        assert tally.status is DeclarationStatus.REJECTED

        # Voting is closed on decided declarations.
        voter_three, _ = await add_user(tid, "Branch Manager")
        with pytest.raises(ConflictError, match="only open"):
            async with tenant_session(factory(), tid) as session:
                await cast_dividend_vote(session, tid, voter_three, record.id, Vote.APPROVE)

        # A rejected declaration can never be distributed.
        with pytest.raises(ConflictError, match="only approved"):
            await distribute_dividend(_scope(tid), tid, None, record.id)

    asyncio.run(run())


def test_declarer_cannot_execute_their_own_distribution() -> None:
    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)
        with pytest.raises(ForbiddenError, match="cannot execute"):
            await distribute_dividend(_scope(tid), tid, admin_id, record.id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. Wrong population
# ---------------------------------------------------------------------------


def test_exited_zero_basis_and_arrears_members_get_the_documented_treatment() -> None:
    """Exited members are never scanned (partial index + allow-list);
    zero-ADB members are skipped without a claim or a 0.00 posting;
    arrears members WITH a basis are paid. Falsifiable per class:
    widen the status allow-list (exited paid), or claim zero
    entitlements (noise rows appear)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        # Paid: active with a full-year share basis -> dividend 600.00.
        m_active = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Active"
        )
        # Paid: arrears with C's deposit oracle -> rebate 36.00.
        m_arrears = await _member_with_deposit_history(
            tid, amount="7300.00", on=date(2026, 4, 2), name="Arrears"
        )
        # Never scanned: exited, despite a full-year basis.
        m_exited = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Exited"
        )
        # Scanned but skipped: no history, zero balances.
        m_zero = await seed_member(tid, name="Zero")
        async with tenant_session(factory(), tid) as session:
            for mid, status in ((m_arrears, "arrears"), (m_exited, "exited")):
                await session.execute(
                    text(
                        "UPDATE members SET status = :st "
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"st": status, "id": str(mid), "tid": str(tid)},
                )

        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        # Hand-computed: eligible = active (600.00) + arrears (36.00).
        assert record.eligible_members == 2
        assert record.total_payout == Decimal("636.00")

        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        # Scanned: active, arrears, zero — NEVER the exited member.
        assert result.scanned == 3
        assert result.claimed == 2
        assert result.skipped_zero == 1
        assert result.status is DeclarationStatus.DISTRIBUTED

        assert await _balance(tid, m_active, table="share_accounts") == Decimal("12600.00")
        assert await _balance(tid, m_arrears, table="deposit_accounts") == Decimal("7336.00")
        assert await _balance(tid, m_exited, table="share_accounts") == Decimal("12000.00")
        assert await _balance(tid, m_zero, table="share_accounts") == Decimal("0.00")
        # No claim and no posting for the exited/zero members (no noise).
        for mid in (m_exited, m_zero):
            assert (
                await count(
                    tid,
                    "SELECT count(*) FROM dividend_distributions "
                    "WHERE member_id = CAST(:m AS uuid)",
                    m=str(mid),
                )
                == 0
            )

    asyncio.run(run())


def test_zero_entitlement_claims_are_unrepresentable_in_the_database() -> None:
    """0020 CHECK total_amount > 0: a 0.00 noise row cannot exist even
    via manual SQL. Falsifiable: drop the CHECK and the insert lands."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        mid = await seed_member(tid, name="Noise")
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO dividend_distributions "
                        "(id, tenant_id, declaration_id, member_id, share_basis, "
                        " deposit_basis, dividend_rate_pct, rebate_rate_pct, "
                        " dividend_amount, rebate_amount, total_amount) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                        "CAST(:did AS uuid), CAST(:mid AS uuid), 0, 0, 5, 2, 0, 0, 0)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "did": str(record.id),
                        "mid": str(mid),
                    },
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. Missing config fails closed (corrupt config: test_dividends_domain)
# ---------------------------------------------------------------------------


def test_missing_or_partial_config_refuses_the_declaration() -> None:
    async def run() -> None:
        # (a) No settings row at all.
        tid_a, _, _ = await seed_actor()
        await _member_with_share_history(tid_a, amount="1000.00", on=FY.start)
        with pytest.raises(ConflictError, match="not configured"):
            await declare_dividend(_scope(tid_a), tid_a, None, today=TODAY)

        # (b) Partial config: dividend rate set, rebate + FY month
        # missing — exactly as unconfigured (no default rebate, no
        # default calendar year).
        tid_b, admin_b, _ = await seed_actor()
        await _member_with_share_history(tid_b, amount="1000.00", on=FY.start)
        async with tenant_session(factory(), tid_b) as session:
            await settings_service.update_settings(
                session,
                tid_b,
                admin_b,
                version=0,
                changes={"dividend_rate_pct": Decimal("5.00")},
            )
        with pytest.raises(ConflictError, match="not configured"):
            await declare_dividend(_scope(tid_b), tid_b, None, today=TODAY)
        assert await count(tid_b, "SELECT count(*) FROM dividend_declarations") == 0

    asyncio.run(run())


def test_zero_rates_declare_nothing() -> None:
    """Configured-but-zero rates leave no member with an entitlement:
    the declaration refuses (an empty declaration is unrepresentable,
    0020 CHECK total_payout > 0) — a zero declaration is noise."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id, dividend="0.00", rebate="0.00")
        await _member_with_share_history(tid, amount="12000.00", on=FY.start)
        with pytest.raises(ConflictError, match="nothing to declare"):
            await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert await count(tid, "SELECT count(*) FROM dividend_declarations") == 0

    asyncio.run(run())


def test_api_rejects_caller_supplied_money_parameters_with_422() -> None:
    """v1.1 rule 1: a caller-supplied rate/period/total is a 422 —
    never silently ignored, never applied."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure_dividends(tid, admin_id)
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            for body in (
                {"dividend_rate_pct": "99.00"},
                {"fy_start": "2020-01-01"},
                {"total_payout": "1.00"},
            ):
                response = await client.post("/dividends/declarations", json=body, headers=headers)
                assert response.status_code == 422, response.text
            distribution = await client.post(
                f"/dividends/declarations/{uuid.uuid4()}/distribution",
                json={"amount": "1.00"},
                headers=headers,
            )
            assert distribution.status_code == 422
        assert await count(tid, "SELECT count(*) FROM dividend_declarations") == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. Distribution-vs-lock race: SKIP LOCKED + idempotent re-run
# ---------------------------------------------------------------------------


def test_locked_member_is_skipped_and_paid_exactly_once_by_the_rerun() -> None:
    """EXIT criterion (failure mode 8): a member row lock held by a
    concurrent settlement/transfer is SKIP LOCKED-skipped; the
    idempotent re-run picks up exactly the remainder (anti-join), and
    claim counts prove exactly-once across the race. The re-run scans
    ONLY the unpaid member — the paid ones are excluded by the
    anti-join without taking a single lock (rule 8)."""

    async def run() -> None:
        tid, admin_id, m_a, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        # Hold A's member row lock in an open transaction (what an
        # exit settlement or share transfer would hold).
        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        await session.execute(
            text(
                "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(m_a), "tid": str(tid)},
        )
        try:
            first = await distribute_dividend(_scope(tid), tid, None, record.id)
        finally:
            await holder.__aexit__(None, None, None)
        assert first.claimed == 2  # B and C; A was skipped, not blocked
        assert first.pending_members == 1
        assert first.status is DeclarationStatus.APPROVED  # not yet complete

        # While partially distributed, the declaration can NOT be
        # voided (re-declaring would double-pay B and C).
        with pytest.raises(ConflictError, match="no longer be voided"):
            async with tenant_session(factory(), tid) as session_two:
                await void_declaration(
                    session_two, tid, admin_id, record.id, version=await get_fresh(tid, record.id)
                )

        second = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert second.scanned == 1  # ONLY A: the anti-join excluded B, C
        assert second.claimed == 1
        assert second.status is DeclarationStatus.DISTRIBUTED
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 3
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 3
        )
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")

    asyncio.run(run())


def test_member_exiting_mid_run_is_skipped_and_the_declaration_stays_approved() -> None:
    """The documented mid-run population change (the verification-vs-
    rerun convention): a member who leaves the eligible population
    AFTER the first-run verification is skipped by the status
    allow-list in the locked batch scan; the declaration stays
    APPROVED with the shortfall visible as pending_members, the FY
    slot stays held, and nobody is re-posted. Falsifiable: widen the
    scan allow-list to exited members and the leaver is paid."""

    async def run() -> None:
        tid, admin_id, m_a, _, _ = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        # First run with A's member row locked elsewhere (verification
        # passes; A is SKIP LOCKED-skipped, B and C are paid).
        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        await session.execute(
            text(
                "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(m_a), "tid": str(tid)},
        )
        try:
            first = await distribute_dividend(_scope(tid), tid, None, record.id)
        finally:
            await holder.__aexit__(None, None, None)
        assert first.claimed == 2

        # A exits between the verification and their batch.
        async with tenant_session(factory(), tid) as session_two:
            await session_two.execute(
                text(
                    "UPDATE members SET status = 'exited' "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(m_a), "tid": str(tid)},
            )

        second = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert second.scanned == 0  # the leaver is never even scanned
        assert second.claimed == 0
        assert second.pending_members == 1  # the shortfall stays visible
        assert second.status is DeclarationStatus.APPROVED

        # Paid members were paid exactly once; the leaver never was.
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 2
        assert (
            await count(
                tid,
                "SELECT count(*) FROM dividend_distributions WHERE member_id = CAST(:m AS uuid)",
                m=str(m_a),
            )
            == 0
        )
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12000.00")
        # The approved declaration still holds the FY slot: the year
        # can never be re-declared over a distributed remainder.
        with pytest.raises(ConflictError, match="already exists"):
            await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 9. Partial batch state — kill-switch atomicity (EXIT criterion)
# ---------------------------------------------------------------------------


def test_kill_switch_mid_batch_leaves_zero_partial_distribution_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash INSIDE the batch transaction after the claim insert, the
    posting and the balance updates, before commit (the audit writer
    raises): ZERO partial state — no claim without its posting and
    audit row and vice versa, no balance drift. Falsifiable: commit
    the claim in its own transaction and the orphan survives."""

    async def run() -> None:
        tid, admin_id, m_a, m_b, m_c = await _oracle_tenant()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        await _approve(tid, record.id)

        real_record_audit = dividends_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "dividend_distribution.post":
                raise _KillSwitchError()  # crash inside the transaction
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(dividends_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await distribute_dividend(_scope(tid), tid, None, record.id)

        # ZERO partial state, by side-effect counts.
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 0
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
            )
            == 0
        )
        assert (
            await count(
                tid,
                "SELECT count(*) FROM audit_log WHERE action = 'dividend_distribution.post'",
            )
            == 0
        )
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12000.00")
        assert await _balance(tid, m_b, table="share_accounts") == Decimal("12000.00")
        assert await _balance(tid, m_c, table="deposit_accounts") == Decimal("7300.00")

        # The same run commits cleanly afterwards.
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 3
        assert result.status is DeclarationStatus.DISTRIBUTED
        assert await _balance(tid, m_a, table="share_accounts") == Decimal("12600.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tenant scoping (issue-#17 falsifiability probes)
# ---------------------------------------------------------------------------


def test_foreign_tenant_argument_reads_and_pays_nothing() -> None:
    """Session opened AS the rows' own tenant (RLS satisfied), foreign
    tenant_id ARGUMENT: only the explicit predicates can refuse — and
    they do. Remove any predicate under test and this fails."""

    async def run() -> None:
        tid, admin_id, _, _, _ = await _oracle_tenant()
        foreign = uuid.uuid4()
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)

        # Config read: unconfigured for the foreign tenant -> refused.
        with pytest.raises(ConflictError, match="not configured"):
            await declare_dividend(_scope(tid), foreign, None, today=TODAY)

        # Population scan: zero members for the foreign tenant argument.
        totals = await compute_declaration_totals(
            _scope(tid),
            foreign,
            fy=FY,
            dividend_rate_pct=Decimal("5.00"),
            rebate_rate_pct=Decimal("2.00"),
        )
        assert totals.eligible_members == 0

        # Distribution: the declaration is invisible to a foreign
        # tenant argument (404), zero claims anywhere.
        with pytest.raises(NotFoundError):
            await distribute_dividend(_scope(tid), foreign, None, record.id)
        assert await count(tid, "SELECT count(*) FROM dividend_distributions") == 0

    asyncio.run(run())
