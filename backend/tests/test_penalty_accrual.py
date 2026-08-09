"""Integration tests for P13.8: penalty-on-arrears accrual.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Every numbered banking-grade failure mode from BUILD_PROMPTS P13.8 has
its own falsifiable test here (remove the guard and the test fails):

  1.  Rounding drift — hand-computed oracle (12 days past grace at
      1%/mo on a 10,000.00 instalment accrues exactly 39.96) plus
      month-boundary and leap-year pins of the actual/30 convention.
  2.  Grace off-by-one — day == grace accrues nothing; day == grace+1
      accrues; the 0019 DB CHECK is probed directly.
  3.  Double accrual — concurrent double-run yields exactly one claim,
      proven by claim/audit side-effect counts.
  4.  Penalty-on-penalty — a loan carrying accrued penalty accrues the
      same figure as an identical loan without.
  5.  Retroactive config — a rate change never recomputes claimed days;
      claim/audit rows record the config each figure used.
  6.  Missing/NULL config fails closed — skipped, not defaulted, not a
      500; corrupt config raises 409 (P13.7 precedent).
  7.  Wrong population — closed/written-off/cured loans and foreign
      tenants never accrue (issue-#17 predicate probe included).
  8.  Accrual-vs-repayment race — serialised on the loan row lock;
      penalty_due ends consistent with the side-effect rows.
  9.  Partial batch state — kill-switch mid-batch leaves no partial
      accrual claims; the same run commits cleanly afterwards.
  10. (Scan performance lives in tests/test_p138_explain.py.)

Idempotency is asserted via side-effect row counts (claims, audit,
penalty_due), never via return values alone (MASTER_PROMPT section 4).
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db_helpers import api_client, factory, seed_user, unique_email
from export_helpers import count, seed_actor, seed_member
from genesis.application import arrears as arrears_module
from genesis.application import loans as loans_service
from genesis.application import tenant_settings as settings_service
from genesis.application.arrears import (
    parse_penalty_config,
    run_arrears_for_tenant,
)
from genesis.application.ledger import disburse_loan
from genesis.domain.ledger import Channel
from genesis.domain.tenant_config import PenaltyChargedOn
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class _KillSwitchError(RuntimeError):
    """Simulated crash inside a batch transaction, before commit."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(tid: uuid.UUID) -> partial[Any]:
    return partial(tenant_session, factory(), tid)


async def _disburse(tid: uuid.UUID, amount: Decimal, *, term: int = 12) -> uuid.UUID:
    """Disburse a fresh loan through the real P7 contract; returns loan_id."""
    mid = await seed_member(tid, name="Penalty Member", deposit="100000.00")
    pid = uuid.uuid4()
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Pen-{pid.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, stage) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, :term, 12.00, 'approved')"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(pid),
                "amount": str(amount),
                "term": term,
            },
        )
    async with tenant_session(factory(), tid) as session:
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id


async def _configure_penalty(
    tid: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    rate: str = "1.00",
    grace: int = 5,
    basis: PenaltyChargedOn = PenaltyChargedOn.INSTALMENT_IN_ARREARS,
) -> None:
    """Configure the penalty keys through the REAL P13.7 writer (1.1)."""
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
        await settings_service.update_settings(
            session,
            tid,
            admin_id,
            version=current.version,
            changes={
                "penalty_rate_pct_per_month": Decimal(rate),
                "penalty_grace_days": grace,
                "penalty_charged_on": basis,
            },
        )


async def _pin_first_instalment(
    tid: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    due: date,
    principal: str = "9000.00",
    interest: str = "1000.00",
) -> None:
    """Pin instalment 1 to a fixed due date and EXACT dues (oracle setup).

    total_due becomes principal+interest — 10,000.00 by default, the
    documented BUILD_PROMPTS oracle basis.
    """
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loan_schedules SET due_date = :due, principal_due = :p, "
                "interest_due = :i, total_due = :t "
                "WHERE loan_id = CAST(:lid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND installment_no = 1"
            ),
            {
                "due": due,
                "p": principal,
                "i": interest,
                "t": str(Decimal(principal) + Decimal(interest)),
                "lid": str(loan_id),
                "tid": str(tid),
            },
        )


async def _penalty_due(tid: uuid.UUID, loan_id: uuid.UUID) -> Decimal:
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                text(
                    "SELECT penalty_due FROM loans "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(loan_id), "tid": str(tid)},
            )
        ).scalar_one()
    return Decimal(str(value))


async def _claims(tid: uuid.UUID, loan_id: uuid.UUID) -> list[tuple[date, Decimal, Decimal, int]]:
    """(accrual_date, amount, rate, dpd) claim rows for one loan."""
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT accrual_date, amount, rate_pct_per_month, days_past_due "
                    "FROM penalty_accruals WHERE loan_id = CAST(:lid AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) ORDER BY accrual_date"
                ),
                {"lid": str(loan_id), "tid": str(tid)},
            )
        ).all()
    return [(r[0], Decimal(str(r[1])), Decimal(str(r[2])), int(r[3])) for r in rows]


async def _audit_count(tid: uuid.UUID, loan_id: uuid.UUID) -> int:
    return await count(
        tid,
        "SELECT count(*) FROM audit_log WHERE action = 'loan.penalty_accrued' AND entity_id = :eid",
        eid=str(loan_id),
    )


# ---------------------------------------------------------------------------
# 1. ROUNDING DRIFT — the hand-computed oracle (EXIT criterion)
# ---------------------------------------------------------------------------


def test_oracle_12_days_past_grace_accrues_exactly_39_96() -> None:
    """HAND-COMPUTED oracle (BUILD_PROMPTS P13.8), never from the code:

    instalment in arrears = 10,000.00 (pinned dues, unpaid)
    rate 1.00 %/mo -> 10,000.00 x 0.01 = 100.00 per month
    actual/30      -> 100.00 / 30 = 3.3333.. -> to_cents = 3.33 per day
    grace 5 days, due 2026-06-01:
      as_of 2026-06-02 .. 2026-06-06 (dpd 1..5)  -> AT/WITHIN grace: 0.00
      as_of 2026-06-07 .. 2026-06-18 (dpd 6..17) -> 12 accrual days
    total = 12 x 3.33 = 39.96 EXACTLY (per-day rounding rule).

    Falsifiable: change the day-count divisor, the rounding point or
    the grace comparison and the penny-exact asserts below fail.
    """

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=5)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))

        for day in range(2, 19):  # 2026-06-02 .. 2026-06-18
            await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, day))

        claims = await _claims(tid, loan_id)
        assert len(claims) == 12  # dpd 6..17 only — none within grace
        assert [c[0] for c in claims] == [date(2026, 6, d) for d in range(7, 19)]
        assert all(c[1] == Decimal("3.33") for c in claims)
        assert [c[3] for c in claims] == list(range(6, 18))
        assert await _penalty_due(tid, loan_id) == Decimal("39.96")
        assert await _audit_count(tid, loan_id) == 12

        # Ledger boundary (documented): accrual writes NO ledger rows —
        # income.penalties is posted on receipt by the P10 allocation.
        income_legs = await count(
            tid, "SELECT count(*) FROM ledger_entries WHERE account = 'income.penalties'"
        )
        assert income_legs == 0

    asyncio.run(run())


def test_day_count_convention_across_month_boundary_and_leap_day() -> None:
    """actual/30 pin (HAND-COMPUTED): every calendar day past grace
    accrues the SAME daily figure — 10,000.00 x 1%/mo / 30 = 3.33 —
    including 2024-02-29 (leap day) and the Feb->Mar month boundary.
    7 days (2024-02-25 .. 2024-03-02) accrue 7 x 3.33 = 23.31."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2024, 2, 24))

        days = [date(2024, 2, 25) + timedelta(days=n) for n in range(7)]
        for as_of in days:
            await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)

        claims = await _claims(tid, loan_id)
        assert [c[0] for c in claims] == days  # incl. Feb 29 and Mar 1/2
        assert all(c[1] == Decimal("3.33") for c in claims)
        assert await _penalty_due(tid, loan_id) == Decimal("23.31")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. GRACE OFF-BY-ONE — exactly-at-boundary
# ---------------------------------------------------------------------------


def test_exactly_at_grace_accrues_nothing_grace_plus_one_accrues() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=5)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))

        # day == grace (dpd exactly 5): NO accrual.
        at_grace = await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 6))
        assert at_grace.penalty_configured is True
        assert await _claims(tid, loan_id) == []
        assert await _audit_count(tid, loan_id) == 0
        assert await _penalty_due(tid, loan_id) == Decimal("0.00")

        # day == grace + 1 (dpd 6): exactly one accrual.
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 7))
        claims = await _claims(tid, loan_id)
        assert len(claims) == 1
        assert claims[0][3] == 6
        assert await _penalty_due(tid, loan_id) == Decimal("3.33")

    asyncio.run(run())


def test_db_check_refuses_within_grace_and_zero_amount_claims() -> None:
    """The 0019 backstops directly probed with raw SQL through the app
    role: a claim row AT grace (days_past_due == grace_days) or with a
    zero amount is unrepresentable."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id = await _disburse(tid, Decimal("1000.00"))
        for dpd, grace, amount in ((5, 5, "1.00"), (6, 5, "0.00")):
            with pytest.raises(DBAPIError):
                async with tenant_session(factory(), tid) as session:
                    await session.execute(
                        text(
                            "INSERT INTO penalty_accruals "
                            "(tenant_id, loan_id, accrual_date, basis, basis_amount, "
                            " rate_pct_per_month, grace_days, days_past_due, amount) "
                            "VALUES (CAST(:tid AS uuid), CAST(:lid AS uuid), :day, "
                            "'instalment_in_arrears', '100.00', '1.00', :grace, :dpd, :amount)"
                        ),
                        {
                            "tid": str(tid),
                            "lid": str(loan_id),
                            "day": date(2026, 6, 1),
                            "grace": grace,
                            "dpd": dpd,
                            "amount": amount,
                        },
                    )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Idempotent re-run (EXIT criterion) — side-effect counts, anti-join
# ---------------------------------------------------------------------------


def test_rerun_accrues_nothing_new_by_side_effect_counts() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        as_of = date(2026, 6, 10)

        first = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)
        assert first.penalties_accrued == 1
        claims_before = await _claims(tid, loan_id)
        audits_before = await _audit_count(tid, loan_id)
        due_before = await _penalty_due(tid, loan_id)
        assert len(claims_before) == 1
        assert audits_before == 1
        assert due_before == Decimal("3.33")

        # Re-run for the SAME day: the anti-join scans the loan but
        # attempts no claim; nothing is written anywhere.
        second = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)
        assert second.scanned == first.scanned  # classification still scans
        assert second.penalties_accrued == 0
        assert await _claims(tid, loan_id) == claims_before
        assert await _audit_count(tid, loan_id) == audits_before
        assert await _penalty_due(tid, loan_id) == due_before

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. DOUBLE ACCRUAL — concurrent double-run
# ---------------------------------------------------------------------------


def test_concurrent_double_run_accrues_exactly_once() -> None:
    """Two simultaneous job runs -> exactly ONE accrual, proven by
    claim + audit side-effect counts (never return values).
    Falsifiable: drop FOR UPDATE SKIP LOCKED and the ON CONFLICT claim
    and both runners would write, doubling penalty_due."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        as_of = date(2026, 6, 10)

        engines = [
            create_async_engine(os.environ["DATABASE_URL"], pool_size=5, max_overflow=0)
            for _ in range(2)
        ]
        try:
            scopes = [
                partial(
                    tenant_session,
                    async_sessionmaker(engine, expire_on_commit=False),
                    tid,
                )
                for engine in engines
            ]
            await asyncio.gather(
                *[run_arrears_for_tenant(scope, tid, as_of=as_of) for scope in scopes]
            )
        finally:
            for engine in engines:
                await engine.dispose()

        assert len(await _claims(tid, loan_id)) == 1
        assert await _audit_count(tid, loan_id) == 1
        assert await _penalty_due(tid, loan_id) == Decimal("3.33")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. PENALTY-ON-PENALTY — the basis never includes penalty_due
# ---------------------------------------------------------------------------


def test_accrued_penalty_never_inflates_the_next_accrual() -> None:
    """Two loans identical in every respect except loan B already
    carries 500.00 of accrued penalty. Both must accrue the SAME
    figure, on BOTH configurable bases. Falsifiable: add penalty_due
    to either basis and B's claim amount diverges."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        loan_a = await _disburse(tid, Decimal("24000.00"))
        loan_b = await _disburse(tid, Decimal("24000.00"))
        for loan_id in (loan_a, loan_b):
            await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET penalty_due = 500.00 "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(loan_b), "tid": str(tid)},
            )

        # Basis 1: instalment in arrears (10,000.00 -> 3.33/day).
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))
        claims_a = await _claims(tid, loan_a)
        claims_b = await _claims(tid, loan_b)
        assert claims_a[0][1] == claims_b[0][1] == Decimal("3.33")

        # Basis 2: full outstanding principal (24,000.00 x 1%/mo =
        # 240.00/mo / 30 = 8.00/day, hand-computed) — penalty_due and
        # the day-1 accrual are both excluded from the basis.
        await _configure_penalty(
            tid, admin_id, rate="1.00", grace=0, basis=PenaltyChargedOn.FULL_OUTSTANDING
        )
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 11))
        claims_a = await _claims(tid, loan_a)
        claims_b = await _claims(tid, loan_b)
        assert claims_a[1][1] == claims_b[1][1] == Decimal("8.00")

        assert await _penalty_due(tid, loan_a) == Decimal("11.33")  # 3.33 + 8.00
        assert await _penalty_due(tid, loan_b) == Decimal("511.33")  # 500 + same accruals

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. RETROACTIVE CONFIG — future periods only, figures reconstructable
# ---------------------------------------------------------------------------


def test_config_change_applies_to_future_days_only() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))

        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))
        assert await _penalty_due(tid, loan_id) == Decimal("3.33")

        # Double the rate, then re-run the ALREADY-CLAIMED day: nothing
        # is recomputed — the claim is write-once (P13.7 TOCTOU
        # precedent). Falsifiable: recompute claimed days and the 1%
        # figures below become 2% figures.
        await _configure_penalty(tid, admin_id, rate="2.00", grace=0)
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))
        claims = await _claims(tid, loan_id)
        assert len(claims) == 1
        assert claims[0][1] == Decimal("3.33")  # still the 1% figure
        assert claims[0][2] == Decimal("1.00")  # config used, recorded

        # The NEXT day accrues at the new rate: 10,000 x 2% / 30 =
        # 200.00 / 30 = 6.6666.. -> 6.67 (hand-computed).
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 11))
        claims = await _claims(tid, loan_id)
        assert len(claims) == 2
        assert claims[1][1] == Decimal("6.67")
        assert claims[1][2] == Decimal("2.00")
        assert await _penalty_due(tid, loan_id) == Decimal("10.00")  # 3.33 + 6.67

        # Every figure reconstructable: audit rows carry the config.
        async with tenant_session(factory(), tid) as session:
            rates = (
                await session.execute(
                    text(
                        "SELECT after->>'rate_pct_per_month' FROM audit_log "
                        "WHERE action = 'loan.penalty_accrued' AND entity_id = :eid "
                        "ORDER BY id"
                    ),
                    {"eid": str(loan_id)},
                )
            ).scalars()
        assert list(rates) == ["1.00", "2.00"]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. MISSING/NULL CONFIG FAILS CLOSED
# ---------------------------------------------------------------------------


def test_unconfigured_and_partially_configured_tenants_accrue_nothing() -> None:
    async def run() -> None:
        # (a) No settings row at all: skipped, classification still runs.
        tid_a, _ = await seed_user(unique_email())
        loan_a = await _disburse(tid_a, Decimal("24000.00"))
        await _pin_first_instalment(tid_a, loan_a, due=date(2026, 6, 1))
        result = await run_arrears_for_tenant(_scope(tid_a), tid_a, as_of=date(2026, 6, 20))
        assert result.penalty_configured is False
        assert result.penalties_accrued == 0
        assert result.updated == 1  # dpd 19 -> classification still ran
        assert await count(tid_a, "SELECT count(*) FROM penalty_accruals") == 0
        assert await _penalty_due(tid_a, loan_a) == Decimal("0.00")

        # (b) Partial config (rate set, grace + basis missing): exactly
        # as unconfigured — no default grace, no default basis.
        tid_b, admin_b, _ = await seed_actor()
        loan_b = await _disburse(tid_b, Decimal("24000.00"))
        await _pin_first_instalment(tid_b, loan_b, due=date(2026, 6, 1))
        async with tenant_session(factory(), tid_b) as session:
            await settings_service.update_settings(
                session,
                tid_b,
                admin_b,
                version=0,
                changes={"penalty_rate_pct_per_month": Decimal("1.00")},
            )
        result_b = await run_arrears_for_tenant(_scope(tid_b), tid_b, as_of=date(2026, 6, 20))
        assert result_b.penalty_configured is False
        assert result_b.penalties_accrued == 0
        assert await count(tid_b, "SELECT count(*) FROM penalty_accruals") == 0

    asyncio.run(run())


def test_zero_rate_is_configured_but_accrues_and_claims_nothing() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="0.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 20))
        assert result.penalty_configured is True
        assert result.penalties_accrued == 0
        assert await count(tid, "SELECT count(*) FROM penalty_accruals") == 0
        assert await _penalty_due(tid, loan_id) == Decimal("0.00")

    asyncio.run(run())


def test_corrupt_stored_config_fails_closed_with_409() -> None:
    """P13.7 precedent: corrupted config (reachable only by manual SQL
    bypassing the 0017 CHECKs) surfaces a ConflictError — the money
    guard is never silently disabled and never silently defaulted."""
    # Unknown basis vocabulary.
    with pytest.raises(ConflictError, match="corrupt"):
        parse_penalty_config(Decimal("1.00"), 5, "weekly_vig")
    # Non-numeric rate.
    with pytest.raises(ConflictError, match="corrupt"):
        parse_penalty_config("not-a-rate", 5, "instalment_in_arrears")
    # Out-of-bounds values.
    with pytest.raises(ConflictError, match="bounds"):
        parse_penalty_config(Decimal("-0.01"), 5, "instalment_in_arrears")
    with pytest.raises(ConflictError, match="bounds"):
        parse_penalty_config(Decimal("100.01"), 5, "instalment_in_arrears")
    with pytest.raises(ConflictError, match="bounds"):
        parse_penalty_config(Decimal("1.00"), 366, "instalment_in_arrears")
    with pytest.raises(ConflictError, match="bounds"):
        parse_penalty_config(Decimal("1.00"), -1, "instalment_in_arrears")
    # Missing keys are NOT corruption: unconfigured -> None (skip).
    assert parse_penalty_config(None, 5, "instalment_in_arrears") is None
    assert parse_penalty_config(Decimal("1.00"), None, "instalment_in_arrears") is None
    assert parse_penalty_config(Decimal("1.00"), 5, None) is None


# ---------------------------------------------------------------------------
# 7. WRONG-POPULATION ACCRUAL
# ---------------------------------------------------------------------------


def test_closed_written_off_and_cured_loans_never_accrue() -> None:
    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)

        # Written-off and closed loans: excluded by the status
        # predicate of the scan itself (and the UPDATE re-check).
        skipped: list[uuid.UUID] = []
        for status in ("written_off", "closed"):
            loan_id = await _disburse(tid, Decimal("24000.00"))
            await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE loans SET status = :st "
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"st": status, "id": str(loan_id), "tid": str(tid)},
                )
            skipped.append(loan_id)

        # An EXITED member's loan (failure mode 7, made explicit per
        # review V4): P12 post_settlement nets and closes every loan
        # through the P10 closure hook and marks the member exited, so
        # coverage was transitive via status='active'. Pin that
        # end-state directly — same pattern as the statuses above; the
        # settlement path itself is exercised in test_member_exits.py
        # and a full request/quorum/settle fixture here would be
        # disproportionate scaffolding.
        exited = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, exited, due=date(2026, 6, 1))
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET status = 'closed', balance = 0, penalty_due = 0 "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(exited), "tid": str(tid)},
            )
            await session.execute(
                text(
                    "UPDATE members SET status = 'exited' "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND id = "
                    "(SELECT member_id FROM loans WHERE id = CAST(:id AS uuid))"
                ),
                {"id": str(exited), "tid": str(tid)},
            )
        skipped.append(exited)

        # A repayment that CURES the arrears before the job runs: the
        # oldest-unpaid derivation finds nothing due -> dpd 0 -> no
        # accrual (and the settled instalment leaves no basis).
        cured = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, cured, due=date(2026, 6, 1))
        async with tenant_session(factory(), tid) as session:
            await loans_service.record_repayment(
                session,
                tid,
                None,
                cured,
                amount=Decimal("10000.00"),
                channel=Channel.BANK,
                received_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
            )

        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))
        assert result.penalties_accrued == 0
        assert await count(tid, "SELECT count(*) FROM penalty_accruals") == 0
        for loan_id in [*skipped, cured]:
            assert await _penalty_due(tid, loan_id) == Decimal("0.00")

    asyncio.run(run())


def test_foreign_tenant_argument_scans_and_accrues_nothing() -> None:
    """Issue-#17 falsifiability probe: the session is opened AS the
    row's own tenant (RLS satisfied) but the job runs with a DIFFERENT
    tenant_id argument — only the explicit bound tenant predicates can
    refuse the rows. Remove any of them and this test fails."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))

        foreign = uuid.uuid4()
        result = await run_arrears_for_tenant(_scope(tid), foreign, as_of=date(2026, 6, 10))
        assert result.scanned == 0
        # The config read is fenced too: tenant A's settings are
        # RLS-visible to this session, but the explicit predicate
        # refuses them for the foreign tenant argument.
        assert result.penalty_configured is False
        assert await count(tid, "SELECT count(*) FROM penalty_accruals") == 0
        assert await _penalty_due(tid, loan_id) == Decimal("0.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. ACCRUAL-vs-REPAYMENT RACE — serialised on the loan row lock
# ---------------------------------------------------------------------------


def test_concurrent_repayment_and_accrual_end_consistent() -> None:
    """A repayment allocating to penalties concurrent with the accrual
    job must serialise on the loan row: whatever the interleaving,
    penalty_due ends equal to
      seeded 150.00 + SUM(claim amounts) - SUM(income.penalties legs)
    reconstructed from side-effect rows — a lost update on either side
    breaks the equation. Falsifiable: drop FOR UPDATE from the scan
    and the read-compute-write of penalty_due can interleave."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE loans SET penalty_due = 150.00 "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(loan_id), "tid": str(tid)},
            )

        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=10, max_overflow=0)
        sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
        try:

            async def repay() -> None:
                async with tenant_session(sm, tid) as session:
                    await loans_service.record_repayment(
                        session,
                        tid,
                        None,
                        loan_id,
                        amount=Decimal("150.00"),
                        channel=Channel.MPESA,
                        received_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
                    )

            await asyncio.gather(
                run_arrears_for_tenant(
                    partial(tenant_session, sm, tid), tid, as_of=date(2026, 6, 10)
                ),
                repay(),
            )
        finally:
            await engine.dispose()

        # A loan locked by the in-flight repayment is legitimately
        # SKIPPED (SKIP LOCKED) and picked up by the next run — run
        # once more so the day's accrual has landed in EVERY
        # interleaving; idempotency guarantees it lands exactly once.
        await run_arrears_for_tenant(_scope(tid), tid, as_of=date(2026, 6, 10))

        claims = await _claims(tid, loan_id)
        accrued = sum((c[1] for c in claims), Decimal("0.00"))
        async with tenant_session(factory(), tid) as session:
            collected = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
                        "WHERE account = 'income.penalties' AND side = 'credit'"
                    )
                )
            ).scalar_one()
        final = await _penalty_due(tid, loan_id)
        # Conservation across the race, from side-effect rows only:
        assert final == Decimal("150.00") + accrued - Decimal(str(collected))
        # And the repayment collected the full seeded 150.00 (its lock
        # observed penalty_due >= 150.00 in every legal interleaving).
        assert Decimal(str(collected)) == Decimal("150.00")
        assert len(claims) == 1  # the accrual landed exactly once
        assert final == accrued

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 9. PARTIAL BATCH STATE — kill-switch atomicity (EXIT criterion)
# ---------------------------------------------------------------------------


def test_kill_switch_mid_batch_leaves_no_partial_accrual_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash INSIDE the batch transaction after the claim insert and
    the penalty_due update, before commit (the audit writer raises):
    ZERO partial state may remain — no claim without its audit row, no
    penalty_due drift. Falsifiable: commit the claim in its own
    transaction and the orphaned claim row survives the abort."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        as_of = date(2026, 6, 10)

        real_record_audit = arrears_module.record_audit

        async def exploding_audit(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("action") == "loan.penalty_accrued":
                raise _KillSwitchError()  # crash inside the transaction
            await real_record_audit(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(arrears_module, "record_audit", exploding_audit)
            with pytest.raises(_KillSwitchError):
                await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)

        # ZERO partial state: no claim, no audit, no penalty_due change.
        assert await count(tid, "SELECT count(*) FROM penalty_accruals") == 0
        assert await _audit_count(tid, loan_id) == 0
        assert await _penalty_due(tid, loan_id) == Decimal("0.00")

        # The same run commits cleanly afterwards — the abort above was
        # the only reason nothing landed.
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)
        assert result.penalties_accrued == 1
        assert await count(tid, "SELECT count(*) FROM penalty_accruals") == 1
        assert await _audit_count(tid, loan_id) == 1
        assert await _penalty_due(tid, loan_id) == Decimal("3.33")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# API surface: /jobs/arrears reports the penalty outcome
# ---------------------------------------------------------------------------


def test_arrears_endpoint_reports_penalty_outcome() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        headers = {"authorization": f"Bearer {token}"}

        # Unconfigured: fail closed is a 200 with penalty_configured
        # false — never a 500, never a defaulted rate.
        loan_id = await _disburse(tid, Decimal("24000.00"))
        await _pin_first_instalment(tid, loan_id, due=date(2026, 6, 1))
        async with api_client() as client:
            res = await client.post("/jobs/arrears", json={"as_of": "2026-06-10"}, headers=headers)
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["penalty_configured"] is False
            assert body["penalties_accrued"] == 0
            assert body["penalty_total"] == "0.00"

            await _configure_penalty(tid, admin_id, rate="1.00", grace=0)
            res = await client.post("/jobs/arrears", json={"as_of": "2026-06-10"}, headers=headers)
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["penalty_configured"] is True
            assert body["penalties_accrued"] == 1
            assert body["penalty_total"] == "3.33"

            # v1.1 rule 1: the body accepts NO money parameter — a
            # caller-supplied rate/grace/basis is a 422, never applied.
            forged = await client.post(
                "/jobs/arrears",
                json={"as_of": "2026-06-11", "penalty_rate_pct_per_month": "99.00"},
                headers=headers,
            )
            assert forged.status_code == 422

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Cross-tenant leakage: the claim table joins the release-blocking suite
# ---------------------------------------------------------------------------


def test_cross_tenant_penalty_claims_are_invisible_and_immutable() -> None:
    async def run() -> None:
        tid_a, admin_a, _ = await seed_actor()
        await _configure_penalty(tid_a, admin_a, rate="1.00", grace=0)
        loan_a = await _disburse(tid_a, Decimal("24000.00"))
        await _pin_first_instalment(tid_a, loan_a, due=date(2026, 6, 1))
        await run_arrears_for_tenant(_scope(tid_a), tid_a, as_of=date(2026, 6, 10))
        assert await count(tid_a, "SELECT count(*) FROM penalty_accruals") == 1

        tid_b, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid_b) as session:
            visible = (
                await session.execute(text("SELECT count(*) FROM penalty_accruals"))
            ).scalar_one()
            assert int(visible) == 0, "tenant B can see tenant A penalty claims"
            updated = await session.execute(text("UPDATE penalty_accruals SET amount = '9999.99'"))
            assert updated.rowcount == 0, "tenant B can rewrite tenant A penalty claims"
            deleted = await session.execute(text("DELETE FROM penalty_accruals"))
            assert deleted.rowcount == 0, "tenant B can delete tenant A penalty claims"

        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid_b) as session:
                await session.execute(
                    text(
                        "INSERT INTO penalty_accruals "
                        "(tenant_id, loan_id, accrual_date, basis, basis_amount, "
                        " rate_pct_per_month, grace_days, days_past_due, amount) "
                        "VALUES (CAST(:tid AS uuid), CAST(:lid AS uuid), :day, "
                        "'instalment_in_arrears', '1.00', '1.00', 0, 1, '0.01')"
                    ),
                    {"tid": str(tid_a), "lid": str(loan_a), "day": date(2026, 6, 11)},
                )

        assert await count(tid_a, "SELECT count(*) FROM penalty_accruals") == 1

    asyncio.run(run())
