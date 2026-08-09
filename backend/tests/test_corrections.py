"""Integration tests for P13.15: repayment adjustments, misc fees, write-off.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Failure modes covered (BUILD_PROMPTS P13.15, v1.2 rule 15) — every
oracle HAND-COMPUTED in comments, never captured from the code under
test; every guard test fails with its guard removed (§4):

  FM1 component drift    — adjustment restores balance / penalty_due /
                           schedule paid_amounts / ledger position
                           component-by-component.
  FM2 double adjustment  — blocked by the atomic claim, proven by
                           side-effect row counts.
  FM3 adjust-vs-repay    — serialises on the loan row lock; BOTH
                           interleavings reconcile to the cent.
  FM4 unauthorised WO    — written_off reachable ONLY through quorum
                           voting bound to the DB-level write-once
                           snapshot (SQL-level trigger proof included).
  FM5 caller fee amount  — 422 via extra="forbid".
  FM6 silent reopen      — CLOSED -> ACTIVE only via the documented
                           adjustment branch.
  FM7 partial correction — kill-switch mid-adjustment leaves zero state.
  FM8 conservation       — DR/CR balance and loans.balance reconstructs
                           from the append-only ledger.
  FM9 performing WO      — (review B3) write-off requires a stored NPL
                           classification; service 409 + 0025 DB CHECK.
  FM10 unsecured reopen  — (review B2) adjusting the closing repayment
                           of a loan whose guarantees were RELEASED at
                           closure is refused; an unguaranteed reopen
                           carries the had_released_guarantees audit
                           marker.

Core-banking addenda: A1 storno linkage + zero-sum conservation per
account; A2 occurred_at = NOW + the closed-period 409 gate; A3 dedicated
permissions / distinct maker; A4 repayment on written_off refused
loudly; A5 adversarial set (adjust-after-write-off, exited member,
cross-actor idempotency replay miss); A6 DB triggers proven at the SQL
level.

Loan fixture anchors to the P10 golden case `standard_24000_12pct_12m`
(tests/golden/loan_schedules.json — hand-verified there): 24,000.00 at
12%/yr over 12 months -> installment 1 is 2,132.37 total = 240.00
interest + 1,892.37 principal.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import api_client, factory, seed_user, unique_email
from export_helpers import drain_export_queue
from genesis.application import corrections as corrections_service
from genesis.application import loans as loans_service
from genesis.application.accounting_periods import close_period
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import disburse_loan, post_loan_interest_accrual
from genesis.application.rbac import seed_permissions
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.domain.lending import LoanStatus
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import ConflictError, ForbiddenError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures
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
                "'Corrections Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        # Deposits satisfy the P12.5 multiplier gate: 100,000 x 3.00
        # covers every principal disbursed here.
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '100000.00')"
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
    """Disburse the golden-case loan; returns (loan_id, member_id)."""
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


async def _backdate_installment(tid: uuid.UUID, loan_id: uuid.UUID, days_ago: int = 40) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loan_schedules "
                "SET due_date = CURRENT_DATE - make_interval(days => :days) "
                "WHERE loan_id = CAST(:lid AS uuid) AND installment_no = 1"
            ),
            {"days": days_ago, "lid": str(loan_id)},
        )


async def _seed_penalty(tid: uuid.UUID, loan_id: uuid.UUID, amount: str) -> None:
    """Fixture stand-in for the P13.8 accrual: a receivable-side
    penalty on the loan row (no ledger leg — the P13.8 rule)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE loans SET penalty_due = :p WHERE id = CAST(:lid AS uuid)"),
            {"p": amount, "lid": str(loan_id)},
        )


async def _classify_npl(tid: uuid.UUID, loan_id: uuid.UUID, cls: str = "substandard") -> None:
    """Fixture stand-in for the P10 arrears job's persisted output: a
    stored NPL classification on the loan row (FM9 requires one before
    any write-off request). Figures consistent with classify(120):
    substandard, 25% provision."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loans SET classification = :cls, days_past_due = 120, "
                "provision_pct = 25.00 WHERE id = CAST(:lid AS uuid)"
            ),
            {"cls": cls, "lid": str(loan_id)},
        )


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
    tid: uuid.UUID, actor: uuid.UUID, repayment_id: uuid.UUID, reason: str = "teller keying error"
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


async def _adjust(
    tid: uuid.UUID, actor: uuid.UUID, repayment_id: uuid.UUID, reason: str = "teller keying error"
) -> corrections_service.AdjustmentResult:
    """Two-phase since issue #24 (N1): the MAKER requests, then a
    DISTINCT freshly-seeded System Admin CHECKER approves — composing
    both phases preserves the P13.15 single-call test shape."""
    pending = await _request(tid, actor, repayment_id, reason=reason)
    checker, _ = await _seed_extra_user(tid, "System Admin")
    return await _approve(tid, checker, pending.id)


async def _loan_state(tid: uuid.UUID, loan_id: uuid.UUID) -> tuple[Decimal, Decimal, str]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT balance, penalty_due, status FROM loans WHERE id = CAST(:l AS uuid)"),
                {"l": str(loan_id)},
            )
        ).one()
    return Decimal(str(row[0])), Decimal(str(row[1])), str(row[2])


async def _schedule_paid(tid: uuid.UUID, loan_id: uuid.UUID) -> dict[int, Decimal]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT installment_no, paid_amount FROM loan_schedules "
                    "WHERE loan_id = CAST(:l AS uuid) ORDER BY installment_no"
                ),
                {"l": str(loan_id)},
            )
        ).all()
    return {int(r[0]): Decimal(str(r[1])) for r in rows}


async def _counts(tid: uuid.UUID) -> tuple[int, int, int, int, int]:
    """(transactions, ledger legs, repayments rows, adjustments, audits)."""
    async with tenant_session(factory(), tid) as session:
        txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        legs = (await session.execute(text("SELECT count(*) FROM ledger_entries"))).scalar_one()
        reps = (await session.execute(text("SELECT count(*) FROM repayments"))).scalar_one()
        adjs = (
            await session.execute(text("SELECT count(*) FROM repayment_adjustments"))
        ).scalar_one()
        audits = (await session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
    return int(txns), int(legs), int(reps), int(adjs), int(audits)


async def _reconstructed_balance(
    session: AsyncSession, tid: uuid.UUID, loan_id: uuid.UUID
) -> Decimal:
    """Independent FM8 oracle (hand-rolled here, NOT the service's SQL):
    disbursed principal minus the signed net of loans.receivable legs
    across the loan's repayment-linked transactions."""
    principal = (
        await session.execute(
            text("SELECT principal FROM loans WHERE id = CAST(:l AS uuid)"),
            {"l": str(loan_id)},
        )
    ).scalar_one()
    net = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(CASE WHEN le.side = 'credit' THEN le.amount "
                "ELSE -le.amount END), 0) "
                "FROM repayments r JOIN ledger_entries le "
                "ON le.transaction_id = r.transaction_id "
                "WHERE r.loan_id = CAST(:l AS uuid) AND le.account = 'loans.receivable'"
            ),
            {"l": str(loan_id)},
        )
    ).scalar_one()
    return Decimal(str(principal)) - Decimal(str(net))


async def _configure_fee(tid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenant_settings (tenant_id, registration_fee) "
                "VALUES (CAST(:tid AS uuid), :fee) "
                "ON CONFLICT (tenant_id) DO UPDATE SET registration_fee = :fee"
            ),
            {"tid": str(tid), "fee": amount},
        )


# ---------------------------------------------------------------------------
# FM1 — component drift: the adjustment restores hand-computed state
# ---------------------------------------------------------------------------


def test_fm1_adjustment_restores_pre_repayment_state_component_by_component() -> None:
    """HAND-COMPUTED (golden case 24,000 @ 12% x 12m; installment 1 =
    240.00 interest + 1,892.37 principal = 2,132.37, backdated 40 days;
    penalty_due seeded 150.00):

      repayment 2,500.00 allocates penalties -> interest -> principal:
        penalties = min(2500, 150)          = 150.00
        interest  = min(2350, 240)          = 240.00
        principal = 2500 - 150 - 240        = 2110.00
      after repayment: balance   = 24000 - 2110   = 21890.00
                       penalty   = 150 - 150      = 0.00
                       inst1 paid = min(2350, 2132.37) = 2132.37

      the adjustment must restore EVERY component to the pre-repayment
      figures: balance 24000.00, penalty_due 150.00, inst1 paid 0.00,
      and the ledger position (loans.receivable reconstruction) 24000.

    Falsifiable: drop any leg of the reversal, skip the schedule
    replay, or restore only one component and an assert fails."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")

        repayment = await _repay(tid, actor, loan_id, "2500.00")
        assert repayment.allocation.penalties == Decimal("150.00")
        assert repayment.allocation.interest == Decimal("240.00")
        assert repayment.allocation.principal == Decimal("2110.00")
        assert repayment.balance_after == Decimal("21890.00")
        assert (await _schedule_paid(tid, loan_id))[1] == Decimal("2132.37")

        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        result = await _adjust(tid, actor, repayment_id)

        # The claim row records the exact components undone.
        assert result.amount == Decimal("2500.00")
        assert result.penalties == Decimal("150.00")
        assert result.interest == Decimal("240.00")
        assert result.principal == Decimal("2110.00")

        balance, penalty, status = await _loan_state(tid, loan_id)
        assert balance == Decimal("24000.00")
        assert penalty == Decimal("150.00")
        assert status == "active"
        paid = await _schedule_paid(tid, loan_id)
        assert paid[1] == Decimal("0.00")
        assert all(v == Decimal("0.00") for v in paid.values())
        async with tenant_session(factory(), tid) as session:
            assert await _reconstructed_balance(session, tid, loan_id) == Decimal("24000.00")

    asyncio.run(run())


def test_a1_storno_linkage_and_per_account_zero_sum() -> None:
    """A1: the reversing transaction carries reversal_of_id = the
    original, and the pair nets to ZERO per account. HAND-COMPUTED:
    original legs DR cash.bank 2500 / CR income.penalties 150 /
    CR income.interest 240 / CR loans.receivable 2110; the reversal is
    the exact mirror, so each account's signed sum over the PAIR is 0.
    Falsifiable: reverse a subset of legs (partial-leg reversal) and a
    per-account sum goes non-zero."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        repayment = await _repay(tid, actor, loan_id, "2500.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        result = await _adjust(tid, actor, repayment_id)

        async with tenant_session(factory(), tid) as session:
            linked = (
                await session.execute(
                    text("SELECT reversal_of_id FROM transactions WHERE id = CAST(:id AS uuid)"),
                    {"id": str(result.reversal_txn_id)},
                )
            ).scalar_one()
            assert uuid.UUID(str(linked)) == repayment.txn_id
            rows = (
                await session.execute(
                    text(
                        "SELECT account, "
                        "SUM(CASE WHEN side = 'debit' THEN amount ELSE -amount END) "
                        "FROM ledger_entries WHERE transaction_id IN "
                        "(CAST(:a AS uuid), CAST(:b AS uuid)) GROUP BY account"
                    ),
                    {"a": str(repayment.txn_id), "b": str(result.reversal_txn_id)},
                )
            ).all()
            assert len(rows) == 4  # cash.bank + three credit buckets
            for account, signed_sum in rows:
                assert Decimal(str(signed_sum)) == Decimal("0.00"), account
            # The negative-linked repayments correction row: the pair
            # sums to zero in the servicing history too.
            total = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM repayments "
                        "WHERE loan_id = CAST(:l AS uuid)"
                    ),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
            assert Decimal(str(total)) == Decimal("0.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — double adjustment blocked by the atomic claim
# ---------------------------------------------------------------------------


def test_fm2_second_adjustment_of_the_same_repayment_is_blocked_by_the_claim() -> None:
    """Side-effect proof (never return values alone): after the refused
    second run there is exactly ONE adjustment row, ONE reversal
    transaction, and the repayments history still sums to zero.
    Falsifiable: drop uq_repayment_adjustments_claim (or replace the
    ON CONFLICT claim with SELECT-then-INSERT) and the second run
    double-restores the balance to 26,500."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor, loan_id, "2500.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        await _adjust(tid, actor, repayment_id)
        before = await _counts(tid)

        with pytest.raises(ConflictError, match="already been adjusted"):
            await _adjust(tid, actor, repayment_id)

        assert await _counts(tid) == before
        balance, _, _ = await _loan_state(tid, loan_id)
        assert balance == Decimal("24000.00")  # restored once, not twice
        async with tenant_session(factory(), tid) as session:
            adjustments = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM repayment_adjustments "
                        "WHERE repayment_id = CAST(:r AS uuid)"
                    ),
                    {"r": str(repayment_id)},
                )
            ).scalar_one()
            assert int(adjustments) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — adjust-vs-repay: both interleavings + the live race
# ---------------------------------------------------------------------------


def test_fm3_interleaving_adjust_then_repay() -> None:
    """HAND-COMPUTED: after the FM1 adjustment (balance 24,000, penalty
    150, inst1 open), a NEW repayment of 500.00 allocates
    penalties 150.00 -> interest min(350, 240) = 240.00 -> principal
    110.00: balance 23,890.00, penalty 0.00, inst1 paid 350.00."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        first = await _repay(tid, actor, loan_id, "2500.00")
        await _adjust(tid, actor, await _repayment_id_for_txn(tid, first.txn_id))

        second = await _repay(tid, actor, loan_id, "500.00")
        assert second.allocation.penalties == Decimal("150.00")
        assert second.allocation.interest == Decimal("240.00")
        assert second.allocation.principal == Decimal("110.00")
        balance, penalty, _ = await _loan_state(tid, loan_id)
        assert balance == Decimal("23890.00")
        assert penalty == Decimal("0.00")
        assert (await _schedule_paid(tid, loan_id))[1] == Decimal("350.00")
        async with tenant_session(factory(), tid) as session:
            assert await _reconstructed_balance(session, tid, loan_id) == balance

    asyncio.run(run())


def test_fm3_interleaving_repay_then_adjust() -> None:
    """HAND-COMPUTED: repayment 1 (2,500) then repayment 2 (500 — all
    principal: penalties and due interest were cleared by repayment 1,
    inst1 fully paid) leaves balance 21,890 - 500 = 21,390. Adjusting
    repayment 1 adds back its 2,110 principal and 150 penalties:
    balance 23,500.00, penalty_due 150.00; the schedule replay
    reassigns repayment 2's 500.00 cash to inst1 (paid 500.00); the
    ledger reconstructs 24,000 - 500 = 23,500."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        first = await _repay(tid, actor, loan_id, "2500.00")
        second = await _repay(tid, actor, loan_id, "500.00")
        assert second.allocation.principal == Decimal("500.00")
        assert second.balance_after == Decimal("21390.00")

        await _adjust(tid, actor, await _repayment_id_for_txn(tid, first.txn_id))
        balance, penalty, _ = await _loan_state(tid, loan_id)
        assert balance == Decimal("23500.00")
        assert penalty == Decimal("150.00")
        assert (await _schedule_paid(tid, loan_id))[1] == Decimal("500.00")
        async with tenant_session(factory(), tid) as session:
            assert await _reconstructed_balance(session, tid, loan_id) == balance

    asyncio.run(run())


def test_fm3_live_race_adjustment_and_repayment_serialise_on_the_loan_row() -> None:
    """The live race (two-phase since issue #24): a PENDING adjustment
    of repayment 1 exists; its APPROVAL races a new 500.00 repayment.
    The loan FOR UPDATE serialises them (never a deadlock) and the
    snapshot-bind-reverify decides the loser deterministically:

      * approval wins -> repayment 2 applies to the RESTORED state
        (balance 24,000, penalty 150, inst1 open): pen2 = min(500,150)
        = 150.00, so penalty == 150 - pen2 and balance == 24,000 -
        prn2 (allocations read from the append-only legs);
      * repayment 2 wins -> the approval finds balance drifted
        (21,890 - prn2 != snapshot 21,890) and 409s POSTING NOTHING:
        the adjustment stays pending, penalty stays 0.00 (nothing was
        due, so pen2 = 0), balance == 21,890 - prn2.

    BOTH branches must reconcile the stored balance to the independent
    ledger reconstruction. Falsifiable: drop the loan FOR UPDATE from
    either path and the lost update leaves the reconstruction unequal
    to the stored balance; drop the snapshot re-verification and the
    losing branch posts anyway."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        first = await _repay(tid, actor, loan_id, "2500.00")
        first_rid = await _repayment_id_for_txn(tid, first.txn_id)
        pending = await _request(tid, actor, first_rid)
        checker, _ = await _seed_extra_user(tid, "System Admin")

        approve_outcome, repay_outcome = await asyncio.gather(
            _approve(tid, checker, pending.id),
            _repay(tid, actor, loan_id, "500.00"),
            return_exceptions=True,
        )
        assert not isinstance(repay_outcome, BaseException), repay_outcome

        balance, penalty, status = await _loan_state(tid, loan_id)
        assert status == "active"
        async with tenant_session(factory(), tid) as session:
            # repayment 2's allocation, read from its append-only legs.
            pen2, prn2 = (
                await session.execute(
                    text(
                        "SELECT "
                        "COALESCE(SUM(le.amount) FILTER (WHERE le.account = 'income.penalties' "
                        " AND le.side = 'credit'), 0), "
                        "COALESCE(SUM(le.amount) FILTER (WHERE le.account = 'loans.receivable' "
                        " AND le.side = 'credit'), 0) "
                        "FROM repayments r JOIN ledger_entries le "
                        "ON le.transaction_id = r.transaction_id "
                        "WHERE r.loan_id = CAST(:l AS uuid) AND r.amount > 0 "
                        "AND r.transaction_id <> CAST(:first AS uuid)"
                    ),
                    {"l": str(loan_id), "first": str(first.txn_id)},
                )
            ).one()
            record = await corrections_service.get_adjustment(session, tid, pending.id)
            if isinstance(approve_outcome, BaseException):
                # Repayment 2 won: the approval drifted and posted
                # NOTHING — the adjustment is still pending.
                assert isinstance(approve_outcome, ConflictError), approve_outcome
                assert "drifted" in str(approve_outcome)
                assert record.status is corrections_service.AdjustmentStatus.PENDING_APPROVAL
                assert penalty == Decimal("0.00")  # nothing was due; pen2 = 0
                assert Decimal(str(pen2)) == Decimal("0.00")
                assert balance == Decimal("21890.00") - Decimal(str(prn2))
            else:
                # The approval won: repayment 2 applied to the restored
                # state (penalty 150 due again, inst1 open).
                assert record.status is corrections_service.AdjustmentStatus.POSTED
                assert penalty == Decimal("150.00") - Decimal(str(pen2))
                assert balance == Decimal("24000.00") - Decimal(str(prn2))
            assert await _reconstructed_balance(session, tid, loan_id) == balance

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — reopen only via the documented branch
# ---------------------------------------------------------------------------


def test_fm6_adjusting_the_closing_repayment_reopens_the_loan_explicitly() -> None:
    """Pay the loan off in full (status closed), then adjust the payoff:
    the loan re-opens through the documented CLOSED -> ACTIVE branch
    with its balance restored. HAND-COMPUTED: full payoff of a fresh
    24,000 loan with nothing due = 24,000.00 principal; the adjustment
    restores balance 24,000.00 and status active; closed_at cleared;
    the claim row records reopened_loan = true."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        payoff = await _repay(tid, actor, loan_id, "24000.00")
        assert payoff.status is LoanStatus.CLOSED

        repayment_id = await _repayment_id_for_txn(tid, payoff.txn_id)
        result = await _adjust(tid, actor, repayment_id)
        assert result.reopened is True
        assert result.status is LoanStatus.ACTIVE
        balance, _, status = await _loan_state(tid, loan_id)
        assert balance == Decimal("24000.00")
        assert status == "active"
        async with tenant_session(factory(), tid) as session:
            closed_at, reopened_flag = (
                await session.execute(
                    text(
                        "SELECT l.closed_at, a.reopened_loan FROM loans l "
                        "JOIN repayment_adjustments a ON a.loan_id = l.id "
                        "WHERE l.id = CAST(:l AS uuid)"
                    ),
                    {"l": str(loan_id)},
                )
            ).one()
            assert closed_at is None
            assert bool(reopened_flag) is True
            # FM10 (review B2): the unguaranteed reopen carries the
            # explicit audit marker proving the released-guarantee
            # check ran and found nothing.
            audit_after = (
                await session.execute(
                    text(
                        "SELECT after FROM audit_log "
                        "WHERE action = 'correction.repayment_adjusted' "
                        "AND entity_id = :eid"
                    ),
                    {"eid": str(result.adjustment_id)},
                )
            ).scalar_one()
            assert audit_after["reopened"] is True
            assert audit_after["had_released_guarantees"] is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM10 (review B2) — a reopen must never resurrect an UNSECURED exposure
# ---------------------------------------------------------------------------


async def _seed_active_guarantee(tid: uuid.UUID, loan_id: uuid.UUID, borrower: uuid.UUID) -> None:
    """A consented (active) guarantee behind the loan, from a second
    member — the state P9 disbursement linkage leaves behind, so the
    P10 closure hook releases it through the REAL code path."""
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


def test_fm10_reopen_of_a_guaranteed_loan_is_refused_after_release() -> None:
    """Banking principle (review B2): a discharged surety cannot be
    unilaterally re-bound. The closing repayment releases the guarantee
    (the REAL P10 closure hook, exercised here); adjusting that
    repayment would reopen the loan UNSECURED, so it is a 409 with
    ZERO side effects — the remedy is the P13.14 substitution /
    re-pledge flow BEFORE adjusting. Falsifiable: remove the
    released-guarantee guard in adjust_repayment and the adjustment
    succeeds, failing the status and count assertions."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, mid = await _disburse(tid)
        await _seed_active_guarantee(tid, loan_id, mid)

        payoff = await _repay(tid, actor, loan_id, "24000.00")
        assert payoff.status is LoanStatus.CLOSED
        async with tenant_session(factory(), tid) as session:
            released = (
                await session.execute(
                    text("SELECT status FROM guarantees WHERE loan_id = CAST(:l AS uuid)"),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
            assert str(released) == "released"  # the P10 closure hook fired

        repayment_id = await _repayment_id_for_txn(tid, payoff.txn_id)
        before = await _counts(tid)
        with pytest.raises(ConflictError, match="released at closure"):
            await _adjust(tid, actor, repayment_id)

        # ZERO side effects: no reversal, no correction row, no claim,
        # no audit; the loan stays CLOSED and the guarantors stay
        # discharged.
        assert await _counts(tid) == before
        _, _, status = await _loan_state(tid, loan_id)
        assert status == "closed"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — kill-switch: abort mid-adjustment leaves zero partial state
# ---------------------------------------------------------------------------


class _KillSwitchError(RuntimeError):
    """Simulated crash after the approval executed, before commit."""


def test_fm7_kill_switch_mid_adjustment_leaves_zero_partial_state() -> None:
    """Kill-switch on the money-moving phase (the issue-#24 APPROVAL):
    abort inside the checker's transaction and prove zero partial
    state — no posting, no correction row, no decision write (the
    adjustment stays pending with no checker), no audit, no
    balance/penalty/schedule drift."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        repayment = await _repay(tid, actor, loan_id, "2500.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)

        # Phase 1 commits the PENDING request (workflow state only).
        pending = await _request(tid, actor, repayment_id)
        checker, _ = await _seed_extra_user(tid, "System Admin")
        before_counts = await _counts(tid)
        before_paid = await _schedule_paid(tid, loan_id)

        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.approve_repayment_adjustment(
                    session, tid, checker, pending.id
                )
                raise _KillSwitchError()  # crash inside the transaction

        # ZERO partial state.
        assert await _counts(tid) == before_counts
        balance, penalty, status = await _loan_state(tid, loan_id)
        assert (balance, penalty, status) == (Decimal("21890.00"), Decimal("0.00"), "active")
        assert await _schedule_paid(tid, loan_id) == before_paid
        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.get_adjustment(session, tid, pending.id)
        assert record.status is corrections_service.AdjustmentStatus.PENDING_APPROVAL
        assert record.checker_id is None
        assert record.reversal_transaction_id is None

        # The same approval commits cleanly afterwards — the abort
        # above was the only reason nothing landed.
        result = await _approve(tid, checker, pending.id)
        assert result.balance_after == Decimal("24000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 / A6 — conservation + the DB triggers, proven at the SQL level
# ---------------------------------------------------------------------------


def test_fm8_ledger_balances_and_append_only_triggers_fire_at_sql_level() -> None:
    """After an adjustment: global DR == CR (the 0014 trigger enforced
    every commit on the way here), loans.balance reconstructs from the
    append-only ledger, and the 0004 append-only triggers plus the
    0025 write-once trigger FIRE on direct SQL through the app role
    (A6: attempt the forbidden write, expect the DB error)."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor, loan_id, "2500.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        await _adjust(tid, actor, repayment_id)

        async with tenant_session(factory(), tid) as session:
            dr, cr = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount) FILTER (WHERE side = 'debit'), 0), "
                        "COALESCE(SUM(amount) FILTER (WHERE side = 'credit'), 0) "
                        "FROM ledger_entries"
                    )
                )
            ).one()
            assert Decimal(str(dr)) == Decimal(str(cr))
            assert await _reconstructed_balance(session, tid, loan_id) == Decimal("24000.00")

        # A6: the ledger is append-only — UPDATE and DELETE must fail
        # at the DATABASE (0004 triggers), not just be absent from app
        # code. Each statement runs in its own session because the
        # error poisons the transaction.
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("UPDATE ledger_entries SET amount = amount + 1"))
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("DELETE FROM ledger_entries"))
        # A6: the adjustment claim row is write-once (0025 trigger).
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("UPDATE repayment_adjustments SET amount = amount + 1"))
        # A6: the claim UNIQUE holds against a direct duplicate INSERT
        # (status 'posted' keeps the probe inside the 0031 partial
        # unique AND exempt from the pending-snapshot CHECK, so the
        # unique claim is what fires).
        with pytest.raises(IntegrityError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO repayment_adjustments "
                        "(tenant_id, repayment_id, loan_id, original_transaction_id, "
                        " maker_id, reason, amount, penalties, interest, principal, status) "
                        "SELECT tenant_id, repayment_id, loan_id, original_transaction_id, "
                        " maker_id, 'dup', amount, penalties, interest, principal, 'posted' "
                        "FROM repayment_adjustments WHERE repayment_id = CAST(:r AS uuid)"
                    ),
                    {"r": str(repayment_id)},
                )

    asyncio.run(run())


def test_n2_fm8_reconstruction_survives_a_loan_interest_accrual() -> None:
    """N2 (review): the FM8 in-transaction self-check reconstructs
    loans.balance from LOANS.RECEIVABLE legs joined through the
    repayments history. A P11 loan-interest accrual on the same member
    posts DR interest.receivable / CR income.interest — a DIFFERENT
    receivable account with no repayments row — so it must neither
    409 a legitimate adjustment nor leak into the reconstruction.
    HAND-COMPUTED: accrual 75.00 touches no loans.receivable leg, so
    the adjustment still restores balance 24,000.00 and the ledger
    reconstruction equals it to the cent; global DR == CR includes the
    accrual pair. Falsifiable: point the accrual builder (or the
    reconstruction SQL) at loans.receivable and the in-transaction FM8
    check aborts this adjustment."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, mid = await _disburse(tid)
        await _backdate_installment(tid, loan_id)
        await _seed_penalty(tid, loan_id, "150.00")
        repayment = await _repay(tid, actor, loan_id, "2500.00")

        # The non-repayment posting BEFORE the adjustment (N2).
        async with tenant_session(factory(), tid) as session:
            await post_loan_interest_accrual(session, tid, mid, Decimal("75.00"), actor)

        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        result = await _adjust(tid, actor, repayment_id)
        assert result.balance_after == Decimal("24000.00")

        async with tenant_session(factory(), tid) as session:
            assert await _reconstructed_balance(session, tid, loan_id) == Decimal("24000.00")
            dr, cr = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount) FILTER (WHERE side = 'debit'), 0), "
                        "COALESCE(SUM(amount) FILTER (WHERE side = 'credit'), 0) "
                        "FROM ledger_entries"
                    )
                )
            ).one()
            assert Decimal(str(dr)) == Decimal(str(cr))

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A2 — posting date vs value date; the closed-period gate
# ---------------------------------------------------------------------------


def test_a2_correction_of_a_closed_period_repayment_posts_into_the_open_period() -> None:
    """Standard GL practice: the repayment sits in a month that has
    since CLOSED; the adjustment posts its reversal with occurred_at =
    NOW (open period), referencing the original via reversal_of_id;
    the original row's date is never rewritten."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        first_of_this_month = datetime.now(UTC).date().replace(day=1)
        prev = first_of_this_month - timedelta(days=1)
        inside = datetime.combine(date(prev.year, prev.month, 15), time(12, 0), tzinfo=UTC)
        async with tenant_session(factory(), tid) as session:
            repayment = await loans_service.record_repayment(
                session,
                tid,
                actor,
                loan_id,
                amount=Decimal("1000.00"),
                channel=Channel.BANK,
                received_at=inside,
            )
        # Close the repayment's month, then adjust.
        async with tenant_session(factory(), tid) as session:
            await close_period(session, tid, actor, year=prev.year, month=prev.month)

        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        result = await _adjust(tid, actor, repayment_id)
        async with tenant_session(factory(), tid) as session:
            original_at, reversal_at = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT occurred_at FROM transactions WHERE id = CAST(:o AS uuid)), "
                        "(SELECT occurred_at FROM transactions WHERE id = CAST(:r AS uuid))"
                    ),
                    {"o": str(repayment.txn_id), "r": str(result.reversal_txn_id)},
                )
            ).one()
        assert original_at.date() == inside.date()  # value date preserved
        assert reversal_at.date() >= first_of_this_month  # posted NOW

    asyncio.run(run())


def test_a2_closed_period_409_gate_applies_to_corrections_too() -> None:
    """A closed period covering TODAY (seeded directly — close_period
    itself refuses to close a live month) makes the correction a 409
    with ZERO MONEY side effects. Two-phase since issue #24: the
    request lands (a workflow row + audit, no money), and the APPROVAL
    hits the period gate inside _post, posting NOTHING — no
    transaction, no legs, no correction row, no decision (the
    adjustment stays pending). Falsifiable: bypass assert_open_period
    in the posting path and this posts."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor, loan_id, "1000.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        today = datetime.now(UTC).date()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO accounting_periods "
                    "(id, tenant_id, period_start, period_end, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ps, :pe, 'closed')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "ps": today.replace(day=1),
                    "pe": today,
                },
            )
        pending = await _request(tid, actor, repayment_id)
        checker, _ = await _seed_extra_user(tid, "System Admin")
        before = await _counts(tid)
        with pytest.raises(ConflictError, match="closed accounting period"):
            await _approve(tid, checker, pending.id)
        assert await _counts(tid) == before
        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.get_adjustment(session, tid, pending.id)
        assert record.status is corrections_service.AdjustmentStatus.PENDING_APPROVAL
        balance, _, _ = await _loan_state(tid, loan_id)
        assert balance == Decimal("23000.00")  # 24000 - 1000, untouched

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A5 — adversarial set
# ---------------------------------------------------------------------------


def test_a5_adjust_on_an_exited_members_loan_is_refused() -> None:
    """P12 terminal-state rule: an exited member's history is settled;
    the fixture exits the member directly (status flip) after closing
    the loan — the adjustment must refuse on the MEMBER status."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, mid = await _disburse(tid)
        payoff = await _repay(tid, actor, loan_id, "24000.00")  # closes the loan
        repayment_id = await _repayment_id_for_txn(tid, payoff.txn_id)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE members SET status = 'exited' WHERE id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        before = await _counts(tid)
        with pytest.raises(ConflictError, match="exited"):
            await _adjust(tid, actor, repayment_id)
        assert await _counts(tid) == before

    asyncio.run(run())


def test_a5_correction_row_itself_cannot_be_adjusted() -> None:
    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor, loan_id, "1000.00")
        result = await _adjust(tid, actor, await _repayment_id_for_txn(tid, repayment.txn_id))
        async with tenant_session(factory(), tid) as session:
            correction_row = (
                await session.execute(
                    text(
                        "SELECT id FROM repayments WHERE transaction_id = CAST(:t AS uuid) "
                        "AND amount < 0"
                    ),
                    {"t": str(result.reversal_txn_id)},
                )
            ).scalar_one()
        with pytest.raises(ConflictError, match="correction row"):
            await _adjust(tid, actor, uuid.UUID(str(correction_row)))

    asyncio.run(run())


def test_a5_cross_actor_idempotency_replay_misses() -> None:
    """The !29 scoping lesson: actor rides the request hash, so a
    DIFFERENT user replaying the same Idempotency-Key + body gets the
    409 conflict envelope — never the first actor's stored response —
    and no second adjustment lands (side-effect counts). The second
    actor is another System Admin, so ONLY the actor differs."""

    async def run() -> None:
        tid, actor_a, token_a = await _seed_actor()
        _, token_b = await _seed_extra_user(tid, "System Admin")
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, actor_a, loan_id, "1000.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        body = {"repayment_id": str(repayment_id), "reason": "keying error"}
        key = f"adj-{uuid.uuid4().hex[:10]}"

        async with api_client() as client:
            first = await client.post(
                "/corrections/repayment-adjustments",
                json=body,
                headers=_headers(token_a, idem=key),
            )
            assert first.status_code == 201, first.text
            counts_after_first = await _counts(tid)

            # Same actor, same key -> stored response replayed.
            replay = await client.post(
                "/corrections/repayment-adjustments",
                json=body,
                headers=_headers(token_a, idem=key),
            )
            assert replay.status_code == 201
            assert replay.headers.get("idempotency-replayed") == "true"
            assert await _counts(tid) == counts_after_first

            # DIFFERENT actor, same key + body -> miss (409 envelope),
            # never the stored response, zero new side effects.
            cross = await client.post(
                "/corrections/repayment-adjustments",
                json=body,
                headers=_headers(token_b, idem=key),
            )
            assert cross.status_code == 409
            assert cross.headers.get("idempotency-replayed") != "true"
            assert cross.json()["category"] == "conflict"
            assert await _counts(tid) == counts_after_first

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Misc fees — FM5 + config resolution
# ---------------------------------------------------------------------------


def test_fee_posts_the_configured_amount_with_fe_reference() -> None:
    """HAND-COMPUTED: registration_fee configured 1,500.00 -> FE- txn
    of exactly 1,500.00, DR cash.mpesa / CR income.fees."""

    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        await _configure_fee(tid, "1500.00")
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            result = await corrections_service.post_misc_fee(
                session,
                tid,
                actor,
                mid,
                fee_type=corrections_service.FeeType.REGISTRATION,
                channel=Channel.MPESA,
            )
        assert result.amount == Decimal("1500.00")
        assert result.txn_ref.startswith("FE-")
        async with tenant_session(factory(), tid) as session:
            legs = (
                await session.execute(
                    text(
                        "SELECT account, side, amount FROM ledger_entries "
                        "WHERE transaction_id = CAST(:t AS uuid) ORDER BY account"
                    ),
                    {"t": str(result.txn_id)},
                )
            ).all()
        assert [(str(r[0]), str(r[1]), Decimal(str(r[2]))) for r in legs] == [
            ("cash.mpesa", "debit", Decimal("1500.00")),
            ("income.fees", "credit", Decimal("1500.00")),
        ]

    asyncio.run(run())


def test_fm5_caller_supplied_fee_amount_is_a_422() -> None:
    """FM5/A5: the request body must not accept an amount —
    extra="forbid" turns it into a structural 422 and nothing posts."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        await _configure_fee(tid, "1500.00")
        mid = await _seed_member(tid)
        before = await _counts(tid)
        async with api_client() as client:
            res = await client.post(
                "/corrections/fees",
                json={
                    "member_id": str(mid),
                    "fee_type": "registration",
                    "channel": "mpesa",
                    "amount": "9999999.00",
                },
                headers=_headers(token, idem=f"fee-{uuid.uuid4().hex[:8]}"),
            )
        assert res.status_code == 422
        assert await _counts(tid) == before

    asyncio.run(run())


def test_fee_fails_closed_when_unconfigured_and_refuses_exited_members() -> None:
    async def run() -> None:
        tid, actor, _ = await _seed_actor()
        mid = await _seed_member(tid)
        # Unconfigured -> 409, fail closed (never a default amount).
        with pytest.raises(ConflictError, match="not configured"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_misc_fee(
                    session,
                    tid,
                    actor,
                    mid,
                    fee_type=corrections_service.FeeType.REGISTRATION,
                    channel=Channel.MPESA,
                )
        await _configure_fee(tid, "1500.00")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE members SET status = 'exited' WHERE id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        with pytest.raises(ConflictError, match="exited"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_misc_fee(
                    session,
                    tid,
                    actor,
                    mid,
                    fee_type=corrections_service.FeeType.REGISTRATION,
                    channel=Channel.MPESA,
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Write-off — FM4 (quorum + write-once snapshot), drift, A4
# ---------------------------------------------------------------------------


async def _request_write_off(
    tid: uuid.UUID, actor: uuid.UUID, loan_id: uuid.UUID
) -> corrections_service.WriteOffRecord:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.request_write_off(
            session, tid, actor, loan_id, reason="irrecoverable"
        )


async def _vote(
    tid: uuid.UUID, voter: uuid.UUID, write_off_id: uuid.UUID, vote: Vote = Vote.APPROVE
) -> corrections_service.WriteOffVoteTally:
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.cast_write_off_vote(
            session, tid, voter, write_off_id, vote
        )


def test_fm4_write_off_end_to_end_quorum_bound_to_write_once_snapshot() -> None:
    """HAND-COMPUTED: loan balance 24,000 - 2,000 repaid = 22,000.00,
    penalty_due seeded 300.00 -> snapshot total_written_off 22,300.00.
    written_off is reached ONLY after two approve votes (the default
    quorum 2) by voters other than the requester, executed by a
    non-requester: the WO- posting derecognises 22,000.00 (DR
    expense.loan_writeoffs / CR loans.receivable), the loan zeroes,
    and the snapshot keeps the surviving 22,300.00 claim (A4).

    Falsifiable (FM4): skip the votes and the posting attempt below
    409s; edit the snapshot via SQL and the 0025 write-once trigger
    raises (both asserted here)."""

    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, _ = await _disburse(tid)
        await _repay(tid, requester, loan_id, "2000.00")
        await _seed_penalty(tid, loan_id, "300.00")
        await _classify_npl(tid, loan_id)  # FM9: write-off needs a stored NPL class

        record = await _request_write_off(tid, requester, loan_id)
        assert record.balance == Decimal("22000.00")
        assert record.penalty_due == Decimal("300.00")
        assert record.total_written_off == Decimal("22300.00")

        # FM4: not approved yet -> posting refused, nothing moves.
        with pytest.raises(ConflictError, match="cannot move"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_write_off(session, tid, voter1, record.id)

        # The requester can never vote (separation of duties).
        with pytest.raises(ForbiddenError, match="cannot vote"):
            await _vote(tid, requester, record.id)

        tally = await _vote(tid, voter1, record.id)
        assert tally.decision is None  # quorum 2 not yet reached
        # FM4: one vote below quorum still cannot post.
        with pytest.raises(ConflictError, match="cannot move"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_write_off(session, tid, voter1, record.id)

        tally = await _vote(tid, voter2, record.id)
        assert tally.status is corrections_service.WriteOffStatus.APPROVED

        # A6/FM4: the approved snapshot is WRITE-ONCE at the database.
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE loan_write_offs SET balance = '1.00' WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(record.id)},
                )

        # The requester can never post their own write-off.
        with pytest.raises(ForbiddenError, match="cannot post"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_write_off(session, tid, requester, record.id)

        async with tenant_session(factory(), tid) as session:
            result = await corrections_service.post_write_off(session, tid, voter1, record.id)
        assert result.total_written_off == Decimal("22300.00")
        assert result.txn_ref is not None and result.txn_ref.startswith("WO-")

        balance, penalty, status = await _loan_state(tid, loan_id)
        assert (balance, penalty, status) == (Decimal("0.00"), Decimal("0.00"), "written_off")
        async with tenant_session(factory(), tid) as session:
            legs = (
                await session.execute(
                    text(
                        "SELECT account, side, amount FROM ledger_entries "
                        "WHERE transaction_id = CAST(:t AS uuid) ORDER BY side"
                    ),
                    {"t": str(result.txn_id)},
                )
            ).all()
            assert [(str(r[0]), str(r[1]), Decimal(str(r[2]))) for r in legs] == [
                ("loans.receivable", "credit", Decimal("22000.00")),
                ("expense.loan_writeoffs", "debit", Decimal("22000.00")),
            ]
            # A4: the claim survives in the write-once snapshot.
            snapshot = await corrections_service.get_write_off(session, tid, record.id)
            assert snapshot.total_written_off == Decimal("22300.00")
            assert snapshot.status is corrections_service.WriteOffStatus.POSTED

        # A4: a repayment against the written-off loan is REFUSED
        # LOUDLY — never silently allocated.
        with pytest.raises(ConflictError, match="cannot accept repayments"):
            await _repay(tid, requester, loan_id, "100.00")
        # A5: adjust-after-write-off refused (the earlier repayment).
        async with tenant_session(factory(), tid) as session:
            earlier = (
                await session.execute(
                    text(
                        "SELECT id FROM repayments WHERE loan_id = CAST(:l AS uuid) AND amount > 0"
                    ),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
        with pytest.raises(ConflictError, match="written off"):
            await _adjust(tid, requester, uuid.UUID(str(earlier)))

    asyncio.run(run())


def test_write_off_drift_409s_posting_nothing_then_void_frees_the_slot() -> None:
    """Snapshot-bind-reverify (v1.1 rule 3): a repayment lands between
    approval and posting, so the loan's components no longer equal the
    approved snapshot -> 409, ZERO postings; void frees the one-live
    slot and a fresh request captures the current figures.
    HAND-COMPUTED: snapshot balance 24,000.00; drift repayment 500.00
    (all principal, nothing due) -> live balance 23,500.00 != snapshot."""

    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, _ = await _disburse(tid)
        await _classify_npl(tid, loan_id)  # FM9: write-off needs a stored NPL class
        record = await _request_write_off(tid, requester, loan_id)
        assert record.balance == Decimal("24000.00")
        await _vote(tid, voter1, record.id)
        await _vote(tid, voter2, record.id)

        # Concurrent double-request collapses (uq_loan_write_offs_open).
        with pytest.raises(ConflictError, match="already exists"):
            await _request_write_off(tid, requester, loan_id)

        await _repay(tid, requester, loan_id, "500.00")  # the drift
        before = await _counts(tid)
        with pytest.raises(ConflictError, match="drifted"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_write_off(session, tid, voter1, record.id)
        assert await _counts(tid) == before  # NOTHING posted
        balance, _, status = await _loan_state(tid, loan_id)
        assert (balance, status) == (Decimal("23500.00"), "active")

        async with tenant_session(factory(), tid) as session:
            current = await corrections_service.get_write_off(session, tid, record.id)
            await corrections_service.void_write_off(
                session, tid, requester, record.id, version=current.version
            )
        fresh = await _request_write_off(tid, requester, loan_id)
        assert fresh.balance == Decimal("23500.00")

    asyncio.run(run())


def test_write_off_kill_switch_leaves_zero_partial_state() -> None:
    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, _ = await _disburse(tid)
        await _classify_npl(tid, loan_id)  # FM9: write-off needs a stored NPL class
        record = await _request_write_off(tid, requester, loan_id)
        await _vote(tid, voter1, record.id)
        await _vote(tid, voter2, record.id)
        before = await _counts(tid)

        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.post_write_off(session, tid, voter1, record.id)
                raise _KillSwitchError()

        assert await _counts(tid) == before
        balance, _, status = await _loan_state(tid, loan_id)
        assert (balance, status) == (Decimal("24000.00"), "active")
        async with tenant_session(factory(), tid) as session:
            snapshot = await corrections_service.get_write_off(session, tid, record.id)
        assert snapshot.status is corrections_service.WriteOffStatus.APPROVED

        # Commits cleanly afterwards.
        async with tenant_session(factory(), tid) as session:
            await corrections_service.post_write_off(session, tid, voter1, record.id)
        _, _, status = await _loan_state(tid, loan_id)
        assert status == "written_off"

    asyncio.run(run())


def test_write_off_requires_an_active_loan_with_something_to_write_off() -> None:
    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)
        await _repay(tid, requester, loan_id, "24000.00")  # closes it
        with pytest.raises(ConflictError, match="only active loans"):
            await _request_write_off(tid, requester, loan_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 (review B3) — a PERFORMING loan can never be written off
# ---------------------------------------------------------------------------


def test_fm9_write_off_request_on_a_performing_loan_is_refused() -> None:
    """Prudential gate (SASRA-aligned, IFRS-9-consistent): write-off is
    the LAST stage of credit deterioration. A fresh disbursement is
    stored 'normal' (0001 default) — the classic insider vector is a
    quorum writing off a crony's GOOD loan, so the request is a 409
    BEFORE any vote can exist, with zero side effects. 'watch' (31-90
    dpd, still performing) is refused identically. Falsifiable: remove
    the NPL_CLASSES guard in request_write_off and both requests
    succeed, failing the count assertion."""

    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        loan_id, _ = await _disburse(tid)  # classification 'normal'
        with pytest.raises(ConflictError, match="non-performing"):
            await _request_write_off(tid, requester, loan_id)

        await _classify_npl(tid, loan_id, cls="watch")  # performing: 31-90 dpd
        with pytest.raises(ConflictError, match="non-performing"):
            await _request_write_off(tid, requester, loan_id)

        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM loan_write_offs"))
            ).scalar_one()
            assert int(count) == 0  # nothing persisted, nothing to vote on

        # A stored NPL class passes the gate (the same loan,
        # reclassified): the guard refuses exactly the non-NPL set.
        await _classify_npl(tid, loan_id, cls="substandard")
        record = await _request_write_off(tid, requester, loan_id)
        assert record.classification == "substandard"

    asyncio.run(run())


def test_fm9_db_check_refuses_a_performing_snapshot_via_direct_sql() -> None:
    """The DB backstop (0025 CHECK): even a direct SQL INSERT through
    the app role — bypassing the service guard entirely (collusion /
    compromised-session scenario) — cannot persist a write-off
    snapshot of a 'normal' loan. Falsifiable: widen the classification
    CHECK in 0025 back to the performing classes and this INSERT
    succeeds."""

    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        loan_id, mid = await _disburse(tid)
        with pytest.raises((IntegrityError, DBAPIError), match="classification"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO loan_write_offs "
                        "(tenant_id, loan_id, member_id, balance, penalty_due, "
                        " total_written_off, classification, provision_pct, reason, "
                        " status, requested_by) "
                        "VALUES (CAST(:tid AS uuid), CAST(:lid AS uuid), "
                        " CAST(:mid AS uuid), '24000.00', '0.00', '24000.00', "
                        " 'normal', 1.00, 'insider vector', 'requested', "
                        " CAST(:actor AS uuid))"
                    ),
                    {
                        "tid": str(tid),
                        "lid": str(loan_id),
                        "mid": str(mid),
                        "actor": str(requester),
                    },
                )

    asyncio.run(run())


def test_n3_member_statement_renders_the_write_off_distinctly_typed() -> None:
    """N3 (review): the WO- entry credits the member's position, so on
    a statement its amount sits in the Credit column exactly like a
    repayment's — an auditor must never be able to mistake it for a
    receipt. The statement's Type column comes straight from
    transactions.type, so the write-off row must read
    'loan_write_off', never 'loan_repayment'. HAND-COMPUTED: repayment
    2,000.00 (Type loan_repayment, Credit 2,000.00) then write-off of
    the remaining 22,000.00 (Type loan_write_off, Credit 22,000.00,
    Reference WO-*). Falsifiable: render the WO- row under a
    payment-like type (or drop the Type column) and the assertions
    below fail."""

    async def run() -> None:
        tid, requester, token = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")
        loan_id, mid = await _disburse(tid)
        await _repay(tid, requester, loan_id, "2000.00")
        await _classify_npl(tid, loan_id)
        record = await _request_write_off(tid, requester, loan_id)
        await _vote(tid, voter1, record.id)
        await _vote(tid, voter2, record.id)
        async with tenant_session(factory(), tid) as session:
            await corrections_service.post_write_off(session, tid, voter1, record.id)

        headers = _headers(token)
        async with api_client() as client:
            created = await client.post(
                "/exports",
                json={"report": "member_statement", "filters": {"member_id": str(mid)}},
                headers=headers,
            )
            assert created.status_code == 201
            export = created.json()
            summary = await drain_export_queue(tid)
            assert summary.failed == 0
            fetched = await client.get(f"/exports/{export['id']}", headers=headers)
            artifact = fetched.json()["artifact"]
            downloaded = await client.get(artifact["csv_download"], headers=headers)
            assert downloaded.status_code == 200

        rows = list(csv.reader(io.StringIO(downloaded.content.decode("utf-8"))))
        header = rows[0]
        ref_col = header.index("Reference")
        type_col = header.index("Type")
        credit_col = header.index("Credit")
        wo_rows = [r for r in rows[1:] if r[ref_col].startswith("WO-")]
        assert len(wo_rows) == 1
        # Distinctly typed: the label is the write-off's own type,
        # never a payment's — the Credit-column amount alone is
        # ambiguous, the Type cell is not.
        assert wo_rows[0][type_col] == "loan_write_off"
        assert wo_rows[0][credit_col] == "22000.00"
        repayment_rows = [r for r in rows[1:] if r[type_col] == "loan_repayment"]
        assert len(repayment_rows) == 1
        assert repayment_rows[0][credit_col] == "2000.00"
        assert repayment_rows[0][type_col] != wo_rows[0][type_col]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# A3 — dedicated permissions: 7-role probe on the corrections routes
# ---------------------------------------------------------------------------


def test_a3_corrections_routes_enforce_the_dedicated_matrix_per_role() -> None:
    """Every role probes a maker route (fee posting) and a checker
    route (write-off vote): outcomes follow corrections:create /
    corrections:approve EXACTLY — holding transactions:edit (the
    Accountant already had it) grants nothing extra, and the Teller /
    Loan Officer (no corrections grants at all) are refused. A 404/409
    on a permitted probe still proves authorisation passed (the probe
    ids are synthetic). Falsifiable: gate the routes with
    transactions:* and the Teller (transactions:create) suddenly posts
    fees, failing this matrix."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await _seed_actor()  # seeds the full matrix once
        for role_name in ROLE_NAMES:
            _, token = await _seed_extra_user(tid, role_name)
            can_create = matrix[role_name][Module.CORRECTIONS][Action.CREATE]
            can_approve = matrix[role_name][Module.CORRECTIONS][Action.APPROVE]
            probe = str(uuid.uuid4())
            async with api_client() as client:
                res = await client.post(
                    "/corrections/fees",
                    json={"member_id": probe, "fee_type": "registration", "channel": "mpesa"},
                    headers=_headers(token, idem=f"m-{uuid.uuid4().hex[:8]}"),
                )
                if can_create:
                    assert res.status_code in (404, 409), (role_name, res.status_code)
                else:
                    assert res.status_code == 403, (role_name, res.status_code)
                res = await client.post(
                    f"/corrections/write-offs/{probe}/votes",
                    json={"vote": "approve"},
                    headers=_headers(token, idem=f"v-{uuid.uuid4().hex[:8]}"),
                )
                if can_approve:
                    assert res.status_code == 404, (role_name, res.status_code)
                else:
                    assert res.status_code == 403, (role_name, res.status_code)

    asyncio.run(run())


def test_a3_adjustment_above_the_authority_band_is_refused() -> None:
    """A3: adjustments route through the SAME P13.7 approval-matrix
    authority check as approvals (reuse, 1.1). HAND-COMPUTED: the
    matrix caps the Accountant at 1,000.00; the 2,500.00 adjustment
    exceeds it -> 403 with zero side effects; the System Admin band is
    unbounded -> the same adjustment succeeds. Falsifiable: drop the
    enforce_authority_band call from adjust_repayment and the
    Accountant's over-band adjustment lands."""

    async def run() -> None:
        tid, admin, _ = await _seed_actor()
        accountant, _ = await _seed_extra_user(tid, "Accountant")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_settings (tenant_id, approval_bands) "
                    "VALUES (CAST(:tid AS uuid), CAST(:bands AS jsonb))"
                ),
                {
                    "tid": str(tid),
                    "bands": (
                        '[{"authority": "Accountant", "max_amount": "1000.00"}, '
                        '{"authority": "System Admin", "max_amount": null}]'
                    ),
                },
            )
        loan_id, _ = await _disburse(tid)
        repayment = await _repay(tid, admin, loan_id, "2500.00")
        repayment_id = await _repayment_id_for_txn(tid, repayment.txn_id)
        before = await _counts(tid)

        with pytest.raises(ForbiddenError, match="approval matrix"):
            await _adjust(tid, accountant, repayment_id)
        assert await _counts(tid) == before

        result = await _adjust(tid, admin, repayment_id)
        assert result.balance_after == Decimal("24000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Cross-tenant probes (issue #17 pattern) on the new tables
# ---------------------------------------------------------------------------


def test_cross_tenant_corrections_rows_are_invisible() -> None:
    async def run() -> None:
        tid_a, actor_a, _ = await _seed_actor()
        tid_b, _, token_b = await _seed_actor()
        loan_id, _ = await _disburse(tid_a)
        repayment = await _repay(tid_a, actor_a, loan_id, "1000.00")
        await _adjust(tid_a, actor_a, await _repayment_id_for_txn(tid_a, repayment.txn_id))
        await _classify_npl(tid_a, loan_id)  # FM9: write-off needs a stored NPL class
        record = await _request_write_off(tid_a, actor_a, loan_id)

        async with tenant_session(factory(), tid_b) as session:
            for table in ("repayment_adjustments", "loan_write_offs", "loan_write_off_votes"):
                count = (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                ).scalar_one()
                assert int(count) == 0, table
        async with api_client() as client:
            res = await client.get(
                f"/corrections/write-offs/{record.id}", headers=_headers(token_b)
            )
        assert res.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Issue #31 batch 6 — ledger (a).1/(a).2: the corrections registers
# (human-authorized read-contract expansion; read-only, expand-only)
# ---------------------------------------------------------------------------


async def _reject_adjustment(tid: uuid.UUID, adjustment_id: uuid.UUID, version: int = 1) -> None:
    """Reject a pending adjustment through a distinct freshly-seeded
    checker (the service enforces maker != checker regardless)."""
    checker, _ = await _seed_extra_user(tid, "System Admin")
    async with tenant_session(factory(), tid) as session:
        await corrections_service.reject_repayment_adjustment(
            session, tid, checker, adjustment_id, version=version, reason="register fixture"
        )


def test_a1_register_adjustments_lists_pending_first_with_exact_keyset_order() -> None:
    """Ledger (a).1 ordering oracle, HAND-COMPUTED from creation order:
    four repayments (500/600/700/800) each get an adjustment —
    A1 (500, POSTED), A2 (600, PENDING), A3 (700, REJECTED),
    A4 (800, PENDING), created strictly in that order. The checker's
    register order is the PENDING band newest-first, then terminal
    history newest-first: [A4, A2, A3, A1]. The limit=1 keyset walk
    must reproduce EXACTLY that sequence page-by-page (opaque cursor
    echoed verbatim) and terminate with a null cursor. Falsifiable:
    drop the pending-first band from adjustments_register_sql and the
    expected sequence breaks at page 1 (created_at DESC alone would
    lead with A4 but page 2 would surface A3, not A2)."""

    async def run() -> None:
        tid, actor, token = await _seed_actor()
        loan_id, _ = await _disburse(tid)

        async def make(amount: str) -> corrections_service.AdjustmentRecord:
            repayment = await _repay(tid, actor, loan_id, amount)
            return await _request(tid, actor, await _repayment_id_for_txn(tid, repayment.txn_id))

        a1 = await make("500.00")
        checker, _ = await _seed_extra_user(tid, "System Admin")
        await _approve(tid, checker, a1.id)  # POSTED
        a2 = await make("600.00")  # PENDING
        a3 = await make("700.00")
        await _reject_adjustment(tid, a3.id)  # REJECTED
        a4 = await make("800.00")  # PENDING
        expected = [str(a4.id), str(a2.id), str(a3.id), str(a1.id)]

        async with api_client() as client:
            # Full page: the whole register in one read.
            res = await client.get("/corrections/repayment-adjustments", headers=_headers(token))
            assert res.status_code == 200, res.text
            page = res.json()
            assert [row["id"] for row in page["items"]] == expected
            assert page["next_cursor"] is None
            # Money strings serialise verbatim (blocker (a)): the
            # PENDING leader carries its hand-computed allocation.
            assert page["items"][0]["amount"] == "800.00"
            assert page["items"][0]["status"] == "pending_approval"
            assert page["items"][3]["status"] == "posted"

            # limit=1 keyset walk reproduces the exact sequence. The
            # opaque cursor travels URL-ENCODED via params (the house
            # walk convention — it embeds an ISO timestamp whose "+"
            # a raw interpolation would decode as a space).
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(4):
                params: dict[str, str | int] = {"limit": 1}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get(
                    "/corrections/repayment-adjustments", params=params, headers=_headers(token)
                )
                assert res.status_code == 200, res.text
                body = res.json()
                assert len(body["items"]) == 1
                seen.append(body["items"][0]["id"])
                cursor = body["next_cursor"]
            assert seen == expected
            assert cursor is None

    asyncio.run(run())


def test_a2_register_write_offs_lists_live_first_with_exact_keyset_order() -> None:
    """Ledger (a).2 ordering oracle, HAND-COMPUTED from creation order
    across four loans: W1 rejected (voided), W2 requested, W3 posted
    (quorum 2 then executed), W4 approved (quorum 2, unposted) —
    created strictly W1 < W2 < W3 < W4. The committee's register order
    is the LIVE band (requested/approved) newest-first, then terminal
    history newest-first: [W4, W2, W3, W1] — proving BOTH live
    statuses lead and BOTH terminal statuses trail. The limit=1 keyset
    walk reproduces exactly that sequence. Falsifiable: drop the
    live-first band from write_offs_register_sql and page 1 leads with
    W4 but page 2 surfaces W3, not W2."""

    async def run() -> None:
        tid, requester, token = await _seed_actor()
        voter1, _ = await _seed_extra_user(tid, "Credit Committee")
        voter2, _ = await _seed_extra_user(tid, "Credit Committee")

        async def make() -> corrections_service.WriteOffRecord:
            loan_id, _ = await _disburse(tid)
            await _classify_npl(tid, loan_id)
            return await _request_write_off(tid, requester, loan_id)

        w1 = await make()
        async with tenant_session(factory(), tid) as session:
            await corrections_service.void_write_off(
                session, tid, voter1, w1.id, version=1
            )  # REJECTED
        w2 = await make()  # REQUESTED
        w3 = await make()
        await _vote(tid, voter1, w3.id)
        await _vote(tid, voter2, w3.id)
        async with tenant_session(factory(), tid) as session:
            await corrections_service.post_write_off(session, tid, voter1, w3.id)  # POSTED
        w4 = await make()
        await _vote(tid, voter1, w4.id)
        await _vote(tid, voter2, w4.id)  # APPROVED (unposted)
        expected = [str(w4.id), str(w2.id), str(w3.id), str(w1.id)]

        async with api_client() as client:
            res = await client.get("/corrections/write-offs", headers=_headers(token))
            assert res.status_code == 200, res.text
            page = res.json()
            assert [row["id"] for row in page["items"]] == expected
            assert page["next_cursor"] is None
            statuses = [row["status"] for row in page["items"]]
            assert statuses == ["approved", "requested", "posted", "rejected"]
            # Verbatim snapshot money (untouched loans: balance
            # 24,000.00, penalty 0.00 -> total 24,000.00).
            assert page["items"][0]["total_written_off"] == "24000.00"

            # The opaque cursor travels URL-ENCODED via params (the
            # house walk convention — it embeds an ISO timestamp).
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(4):
                params: dict[str, str | int] = {"limit": 1}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get(
                    "/corrections/write-offs", params=params, headers=_headers(token)
                )
                assert res.status_code == 200, res.text
                body = res.json()
                assert len(body["items"]) == 1
                seen.append(body["items"][0]["id"])
                cursor = body["next_cursor"]
            assert seen == expected
            assert cursor is None

    asyncio.run(run())


def test_a3_register_routes_enforce_corrections_view_per_role() -> None:
    """Both register LIST routes sit under the EXISTING corrections
    view gate (never generic transactions): every seeded role probes
    both reads and the outcome follows corrections x VIEW exactly —
    200 (an empty register is still an authorised read) or 403.
    Falsifiable: gate the routes on transactions:view and the Teller
    suddenly reads the fraud-channel register, failing this matrix."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await _seed_actor()  # seeds the full permission matrix
        for role_name in ROLE_NAMES:
            _, token = await _seed_extra_user(tid, role_name)
            can_view = matrix[role_name][Module.CORRECTIONS][Action.VIEW]
            async with api_client() as client:
                for path in ("/corrections/repayment-adjustments", "/corrections/write-offs"):
                    res = await client.get(path, headers=_headers(token))
                    if can_view:
                        assert res.status_code == 200, (role_name, path, res.status_code)
                    else:
                        assert res.status_code == 403, (role_name, path, res.status_code)

    asyncio.run(run())


def test_registers_cross_tenant_invisible_and_rejections_echo_no_figures() -> None:
    """Explicit tenant predicate doubling RLS: tenant A's adjustment
    (1,000.00) and write-off (24,000.00) rows are INVISIBLE to tenant
    B's register reads (200 with empty items — never a 403 oracle that
    would confirm existence). Rejection paths echo NO figures (rule 7):
    the Teller's 403 envelope and a forged-cursor 400 envelope carry
    the category + correlation id ONLY — never a money string from the
    register rows."""

    async def run() -> None:
        tid_a, actor_a, _ = await _seed_actor()
        _, teller_token = await _seed_extra_user(tid_a, "Teller")
        _, _, token_b = await _seed_actor()
        loan_id, _ = await _disburse(tid_a)
        repayment = await _repay(tid_a, actor_a, loan_id, "1000.00")
        await _request(tid_a, actor_a, await _repayment_id_for_txn(tid_a, repayment.txn_id))
        wo_loan, _ = await _disburse(tid_a)
        await _classify_npl(tid_a, wo_loan)
        await _request_write_off(tid_a, actor_a, wo_loan)

        async with api_client() as client:
            for path in ("/corrections/repayment-adjustments", "/corrections/write-offs"):
                res = await client.get(path, headers=_headers(token_b))
                assert res.status_code == 200, (path, res.status_code)
                assert res.json() == {"items": [], "next_cursor": None}

                forbidden = await client.get(path, headers=_headers(teller_token))
                assert forbidden.status_code == 403
                assert set(forbidden.json().keys()) == {"category", "correlation_id"}
                for figure in ("1000.00", "24000.00"):
                    assert figure not in forbidden.text

                forged = await client.get(f"{path}?cursor=not-a-cursor", headers=_headers(token_b))
                assert forged.status_code == 400
                assert set(forged.json().keys()) == {"category", "correlation_id"}
                for figure in ("1000.00", "24000.00"):
                    assert figure not in forged.text

    asyncio.run(run())
