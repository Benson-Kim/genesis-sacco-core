"""Integration tests for P10: loan servicing & portfolio.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

EXIT criteria covered:
  * Golden-file schedule tests (pure generator + persisted rows)
  * Arrears job idempotent — re-run changes nothing
  * Dashboard aggregates match seeded fixtures to the cent
  * Concurrency on repayment posting — never over-collects
  * Keyset pagination on the loan book (page cap 100)
  * Guarantee release hook on loan closure
  * Early settlement quotes waive future interest
  * All mutations audited in-transaction; notifications outbox-only

Direct service-layer calls sit alongside the API tests per the
test_ledger_integration convention so the coverage gate reflects the
application layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import loans as loans_service
from genesis.application.arrears import run_arrears_for_tenant
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import disburse_loan
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.domain.lending import LoanClass, LoanStatus, build_schedule
from genesis.domain.money import to_cents
from genesis.errors import ConflictError, InvalidInputError, NotFoundError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

GOLDEN_SCHEDULES = Path(__file__).parent / "golden" / "loan_schedules.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
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
    return tid, role_id, token


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_member(tid: uuid.UUID) -> uuid.UUID:
    # The deposit account satisfies the P12.5 multiplier precondition
    # (issue #15): 100000.00 x 3.00 covers every amount disbursed here —
    # servicing tests exercise allocation/closure, not the eligibility
    # gate (test_deposit_multiplier.py owns that).
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Servicing Member')"
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


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Product-{pid.hex[:6]}"},
        )
    return pid


async def _seed_approved_application(
    tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID, amount: Decimal, term: int = 12
) -> uuid.UUID:
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
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
    return app_id


async def _disburse(
    tid: uuid.UUID,
    amount: Decimal,
    *,
    term: int = 12,
    member_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Disburse a fresh loan; returns (loan_id, member_id)."""
    mid = member_id or await _seed_member(tid)
    pid = await _seed_product(tid)
    app_id = await _seed_approved_application(tid, mid, pid, amount, term)
    async with tenant_session(factory(), tid) as session:
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id, mid


async def _backdate_installment(
    tid: uuid.UUID, loan_id: uuid.UUID, installment_no: int, days_ago: int
) -> None:
    """Move one installment's due date into the past to manufacture arrears."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE loan_schedules "
                "SET due_date = CURRENT_DATE - make_interval(days => :days) "
                "WHERE loan_id = CAST(:lid AS uuid) AND installment_no = :no"
            ),
            {"days": days_ago, "lid": str(loan_id), "no": installment_no},
        )


def _scope(tid: uuid.UUID) -> partial[object]:
    return partial(tenant_session, factory(), tid)


# ---------------------------------------------------------------------------
# EXIT: golden-file schedule tests
# ---------------------------------------------------------------------------


def test_golden_schedules_pure_generator() -> None:
    """build_schedule reproduces every golden case exactly, to the cent."""
    cases = json.loads(GOLDEN_SCHEDULES.read_text())
    assert len(cases) >= 5
    for case in cases:
        schedule = build_schedule(
            Decimal(case["principal"]), Decimal(case["annual_rate_pct"]), case["months"]
        )
        assert len(schedule) == len(case["installments"]), case["name"]
        for got, expected in zip(schedule, case["installments"], strict=True):
            assert got.number == expected["number"], case["name"]
            assert got.principal_due == Decimal(expected["principal_due"]), case["name"]
            assert got.interest_due == Decimal(expected["interest_due"]), case["name"]
            assert got.total_due == Decimal(expected["total_due"]), case["name"]


def test_golden_schedule_persisted_by_disbursement() -> None:
    """The schedule rows written by disburse_loan match the golden file."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"), term=12)
        async with tenant_session(factory(), tid) as session:
            rows = await loans_service.get_schedule(session, tid, loan_id)
        cases = json.loads(GOLDEN_SCHEDULES.read_text())
        golden = next(c for c in cases if c["name"] == "standard_24000_12pct_12m")
        assert len(rows) == len(golden["installments"])
        for got, expected in zip(rows, golden["installments"], strict=True):
            assert got.installment_no == expected["number"]
            assert got.principal_due == Decimal(expected["principal_due"])
            assert got.interest_due == Decimal(expected["interest_due"])
            assert got.total_due == Decimal(expected["total_due"])
            assert got.paid_amount == Decimal("0.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Repayment allocation: penalties -> interest -> principal
# ---------------------------------------------------------------------------


def test_repayment_allocation_split_legs_and_balance() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, mid = await _disburse(tid, Decimal("24000.00"), term=12)
        await _backdate_installment(tid, loan_id, 1, 40)
        await _backdate_installment(tid, loan_id, 2, 10)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE loans SET penalty_due = 150.00 WHERE id = CAST(:id AS uuid)"),
                {"id": str(loan_id)},
            )

        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        due_interest = schedule[0].interest_due + schedule[1].interest_due

        async with tenant_session(factory(), tid) as session:
            result = await loans_service.record_repayment(
                session, tid, None, loan_id, amount=Decimal("1000.00"), channel=Channel.MPESA
            )

        expected_interest = min(Decimal("850.00"), due_interest)
        expected_principal = Decimal("850.00") - expected_interest
        assert result.txn_ref.startswith("RP-")
        assert result.allocation.penalties == Decimal("150.00")
        assert result.allocation.interest == expected_interest
        assert result.allocation.principal == expected_principal
        assert result.balance_after == Decimal("24000.00") - expected_principal
        assert result.penalty_due_after == Decimal("0.00")
        assert result.status is LoanStatus.ACTIVE

        async with tenant_session(factory(), tid) as session:
            legs = (
                await session.execute(
                    text(
                        "SELECT account, side, amount FROM ledger_entries "
                        "WHERE transaction_id = CAST(:id AS uuid) ORDER BY account, side"
                    ),
                    {"id": str(result.txn_id)},
                )
            ).all()
            loan_row = (
                await session.execute(
                    text("SELECT balance, penalty_due FROM loans WHERE id = CAST(:id AS uuid)"),
                    {"id": str(loan_id)},
                )
            ).one()
            repay_amount = (
                await session.execute(
                    text("SELECT amount FROM repayments WHERE transaction_id = CAST(:id AS uuid)"),
                    {"id": str(result.txn_id)},
                )
            ).scalar_one()
            audit_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log WHERE action = 'loan.repayment' "
                        "AND entity_id = :eid"
                    ),
                    {"eid": str(loan_id)},
                )
            ).scalar_one()
            event_payload = (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox_events "
                        "WHERE event_type = 'ledger.repayment_posted' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).scalar_one()

        by_leg = {(str(a), str(s)): Decimal(str(amt)) for a, s, amt in legs}
        assert by_leg[("cash.mpesa", "debit")] == Decimal("1000.00")
        assert by_leg[("income.penalties", "credit")] == Decimal("150.00")
        assert by_leg[("income.interest", "credit")] == expected_interest
        assert by_leg[("loans.receivable", "credit")] == expected_principal
        assert Decimal(str(loan_row[0])) == result.balance_after
        assert Decimal(str(loan_row[1])) == Decimal("0.00")
        assert Decimal(str(repay_amount)) == Decimal("1000.00")
        assert int(audit_count) == 1
        # Outbox-only notification with the allocation breakdown, no PII.
        assert event_payload["loan_id"] == str(loan_id)
        assert event_payload["penalties"] == "150.00"
        assert set(event_payload) <= {
            "txn_id",
            "txn_ref",
            "member_id",
            "loan_id",
            "amount",
            "penalties",
            "interest",
            "principal",
            "channel",
        }
        assert mid is not None

    asyncio.run(run())


def test_full_repayment_closes_loan_and_releases_guarantees() -> None:
    """P10 closure hook: balance zero -> closed -> guarantees released."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, mid = await _disburse(tid, Decimal("5000.00"), term=6)
        guarantor = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            # P14.5 FM4: a row born 'active' must carry a consent
            # principal — seeded fixtures attest via any tenant user.
            attestor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, loan_id, "
                    " amount, status, consent_attested_by, consent_reference) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), CAST(:lid AS uuid), 2000.00, 'active', "
                    "CAST(:att AS uuid), 'seeded fixture (P14.5 FM4)')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "g": str(guarantor),
                    "b": str(mid),
                    "lid": str(loan_id),
                    "att": str(attestor),
                },
            )

        async with tenant_session(factory(), tid) as session:
            quote = await loans_service.get_settlement_quote(session, tid, loan_id)
        assert quote.total == Decimal("5000.00")  # nothing due yet: principal only

        async with tenant_session(factory(), tid) as session:
            result = await loans_service.record_repayment(
                session, tid, None, loan_id, amount=quote.total, channel=Channel.BANK
            )
        assert result.status is LoanStatus.CLOSED
        assert result.balance_after == Decimal("0.00")

        async with tenant_session(factory(), tid) as session:
            loan = await loans_service.get_loan(session, tid, loan_id)
            guarantee_status = (
                await session.execute(
                    text("SELECT status FROM guarantees WHERE loan_id = CAST(:lid AS uuid)"),
                    {"lid": str(loan_id)},
                )
            ).scalar_one()
            closed_events = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE event_type = 'loan.closed'")
                )
            ).scalar_one()
            audit_closed = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log WHERE action = 'loan.closed' "
                        "AND entity_id = :eid"
                    ),
                    {"eid": str(loan_id)},
                )
            ).scalar_one()
        assert loan.status is LoanStatus.CLOSED
        assert loan.closed_at is not None
        assert str(guarantee_status) == "released"
        assert int(closed_events) == 1
        assert int(audit_closed) == 1

        # A closed loan accepts no further repayments.
        with pytest.raises(ConflictError, match="cannot accept repayments"):
            async with tenant_session(factory(), tid) as session:
                await loans_service.record_repayment(
                    session, tid, None, loan_id, amount=Decimal("10.00"), channel=Channel.BANK
                )

    asyncio.run(run())


def test_overpayment_beyond_payoff_is_rejected() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("1000.00"), term=6)
        with pytest.raises(ConflictError, match="payoff"):
            async with tenant_session(factory(), tid) as session:
                await loans_service.record_repayment(
                    session, tid, None, loan_id, amount=Decimal("2000.00"), channel=Channel.BANK
                )
        # Nothing was persisted: the transaction rolled back atomically.
        async with tenant_session(factory(), tid) as session:
            loan = await loans_service.get_loan(session, tid, loan_id)
            repayments = (
                await session.execute(
                    text("SELECT count(*) FROM repayments WHERE loan_id = CAST(:lid AS uuid)"),
                    {"lid": str(loan_id)},
                )
            ).scalar_one()
        assert loan.balance == Decimal("1000.00")
        assert int(repayments) == 0

    asyncio.run(run())


def test_repayment_service_input_guards() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(InvalidInputError, match="positive"):
                await loans_service.record_repayment(
                    session, tid, None, uuid.uuid4(), amount=Decimal("0"), channel=Channel.BANK
                )
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await loans_service.record_repayment(
                    session,
                    tid,
                    None,
                    uuid.uuid4(),
                    amount=Decimal("10.00"),
                    channel=Channel.BANK,
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Early settlement quotes
# ---------------------------------------------------------------------------


def test_settlement_quote_includes_due_interest_and_penalties() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"), term=12)
        await _backdate_installment(tid, loan_id, 1, 15)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE loans SET penalty_due = 75.50 WHERE id = CAST(:id AS uuid)"),
                {"id": str(loan_id)},
            )
        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        async with tenant_session(factory(), tid) as session:
            quote = await loans_service.get_settlement_quote(session, tid, loan_id)
        # Only installment 1 interest is due; future interest is waived.
        assert quote.principal_balance == Decimal("24000.00")
        assert quote.interest_due == schedule[0].interest_due
        assert quote.penalties_due == Decimal("75.50")
        assert quote.total == to_cents(
            Decimal("24000.00") + schedule[0].interest_due + Decimal("75.50")
        )
        # Settling at the quote closes the loan without collecting future interest.
        async with tenant_session(factory(), tid) as session:
            result = await loans_service.record_repayment(
                session, tid, None, loan_id, amount=quote.total, channel=Channel.BANK
            )
        assert result.status is LoanStatus.CLOSED
        assert result.allocation.interest == schedule[0].interest_due

    asyncio.run(run())


def test_settlement_quote_missing_and_closed_loans() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await loans_service.get_settlement_quote(session, tid, uuid.uuid4())
        loan_id, _ = await _disburse(tid, Decimal("1000.00"), term=6)
        async with tenant_session(factory(), tid) as session:
            await loans_service.record_repayment(
                session, tid, None, loan_id, amount=Decimal("1000.00"), channel=Channel.BANK
            )
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ConflictError, match="nothing to settle"):
                await loans_service.get_settlement_quote(session, tid, loan_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: concurrency on repayment posting
# ---------------------------------------------------------------------------


def test_concurrent_repayments_never_over_collect() -> None:
    """10 parallel repayments of 1000 against a 5000 balance: exactly 5 land."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("5000.00"), term=6)

        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=20, max_overflow=0)
        sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

        async def one_repayment() -> object:
            try:
                async with tenant_session(sm, tid) as session:
                    return await loans_service.record_repayment(
                        session,
                        tid,
                        None,
                        loan_id,
                        amount=Decimal("1000.00"),
                        channel=Channel.MPESA,
                    )
            except ConflictError as exc:
                return exc

        results = await asyncio.gather(*[one_repayment() for _ in range(10)])
        await engine.dispose()

        successes = [r for r in results if isinstance(r, loans_service.RepaymentResult)]
        failures = [r for r in results if isinstance(r, ConflictError)]
        assert len(successes) == 5, f"expected exactly 5 repayments, got {len(successes)}"
        assert len(failures) == 5
        refs = {r.txn_ref for r in successes}
        assert len(refs) == 5  # unique race-safe references

        async with tenant_session(factory(), tid) as session:
            loan = await loans_service.get_loan(session, tid, loan_id)
            collected = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM repayments "
                        "WHERE loan_id = CAST(:lid AS uuid)"
                    ),
                    {"lid": str(loan_id)},
                )
            ).scalar_one()
        assert loan.balance == Decimal("0.00")
        assert loan.status is LoanStatus.CLOSED
        assert Decimal(str(collected)) == Decimal("5000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: arrears job — DPD -> classification -> provisioning, idempotent
# ---------------------------------------------------------------------------


def test_arrears_job_classifies_and_is_idempotent() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        current, _ = await _disburse(tid, Decimal("10000.00"))
        watch, _ = await _disburse(tid, Decimal("20000.00"))
        substd, _ = await _disburse(tid, Decimal("15000.00"))
        loss, _ = await _disburse(tid, Decimal("8000.00"))
        await _backdate_installment(tid, watch, 1, 45)
        await _backdate_installment(tid, substd, 1, 120)
        await _backdate_installment(tid, loss, 1, 400)
        as_of = datetime.now(UTC).date()

        # batch_size=2 forces several short transactions (gate 1.3).
        first = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of, batch_size=2)
        assert first.scanned == 4
        assert first.updated == 3  # the current loan is already normal/0/1%
        assert first.batches == 2

        expected = {
            current: (0, LoanClass.NORMAL, Decimal("1.00")),
            watch: (45, LoanClass.WATCH, Decimal("5.00")),
            substd: (120, LoanClass.SUBSTANDARD, Decimal("25.00")),
            loss: (400, LoanClass.LOSS, Decimal("100.00")),
        }
        async with tenant_session(factory(), tid) as session:
            for loan_id, (dpd, label, provision) in expected.items():
                loan = await loans_service.get_loan(session, tid, loan_id)
                assert loan.days_past_due == dpd, loan_id
                assert loan.classification is label, loan_id
                assert loan.provision_pct == provision, loan_id
            audit_before = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'loan.classified'")
                )
            ).scalar_one()
            events_before = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'loan.classification_changed'"
                    )
                )
            ).scalar_one()
        assert int(audit_before) == 3
        assert int(events_before) == 3

        # EXIT: re-run changes nothing.
        second = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of, batch_size=2)
        assert second.scanned == 4
        assert second.updated == 0
        async with tenant_session(factory(), tid) as session:
            audit_after = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'loan.classified'")
                )
            ).scalar_one()
            events_after = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'loan.classification_changed'"
                    )
                )
            ).scalar_one()
        assert int(audit_after) == int(audit_before)
        assert int(events_after) == int(events_before)

        # Classification-change payloads carry ids and figures only (no PII).
        async with tenant_session(factory(), tid) as session:
            payloads = (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox_events "
                        "WHERE event_type = 'loan.classification_changed'"
                    )
                )
            ).scalars()
            for payload in payloads:
                assert set(payload) <= {
                    "loan_id",
                    "from",
                    "to",
                    "days_past_due",
                    "provision_pct",
                }

    asyncio.run(run())


def test_arrears_job_recovery_reclassifies_downwards() -> None:
    """Paying off arrears moves the loan back toward normal on the next run."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"))
        await _backdate_installment(tid, loan_id, 1, 45)
        as_of = datetime.now(UTC).date()
        await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)
        async with tenant_session(factory(), tid) as session:
            loan = await loans_service.get_loan(session, tid, loan_id)
        assert loan.classification is LoanClass.WATCH

        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        async with tenant_session(factory(), tid) as session:
            await loans_service.record_repayment(
                session, tid, None, loan_id, amount=schedule[0].total_due, channel=Channel.BANK
            )
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=as_of)
        assert result.updated == 1
        async with tenant_session(factory(), tid) as session:
            loan = await loans_service.get_loan(session, tid, loan_id)
        assert loan.classification is LoanClass.NORMAL
        assert loan.days_past_due == 0

    asyncio.run(run())


def test_arrears_job_empty_tenant_and_bad_batch_size() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=datetime.now(UTC).date())
        assert result.scanned == 0
        assert result.updated == 0
        assert result.batches == 0
        with pytest.raises(ValueError, match="batch_size"):
            await run_arrears_for_tenant(
                _scope(tid), tid, as_of=datetime.now(UTC).date(), batch_size=0
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: dashboard aggregates match seeded fixtures to the cent
# ---------------------------------------------------------------------------


def test_portfolio_summary_matches_seeded_fixtures_to_the_cent() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        fixtures: list[tuple[Decimal, int, Decimal]] = [
            # (principal, days overdue, provision_pct after classification)
            (Decimal("10000.00"), 0, Decimal("1")),
            (Decimal("20000.00"), 45, Decimal("5")),
            (Decimal("15000.55"), 120, Decimal("25")),
            (Decimal("8000.33"), 400, Decimal("100")),
        ]
        loan_ids: list[uuid.UUID] = []
        for principal, days, _prov in fixtures:
            loan_id, _ = await _disburse(tid, principal)
            if days:
                await _backdate_installment(tid, loan_id, 1, days)
            loan_ids.append(loan_id)
        # One closed loan proves closed books never leak into the dashboard.
        closed_id, _ = await _disburse(tid, Decimal("777.77"), term=6)
        async with tenant_session(factory(), tid) as session:
            await loans_service.record_repayment(
                session, tid, None, closed_id, amount=Decimal("777.77"), channel=Channel.BANK
            )
        await run_arrears_for_tenant(_scope(tid), tid, as_of=datetime.now(UTC).date())

        outstanding = sum(p for p, _, _ in fixtures)
        npl = sum(p for p, d, _ in fixtures if d > 90)
        par30 = sum(p for p, d, _ in fixtures if d > 30)
        provisions = sum(to_cents(p * prov / Decimal("100")) for p, _, prov in fixtures)
        npl_ratio = to_cents(npl * Decimal("100") / outstanding)
        par30_ratio = to_cents(par30 * Decimal("100") / outstanding)

        # Direct service-layer aggregate (coverage convention).
        async with tenant_session(factory(), tid) as session:
            summary = await loans_service.portfolio_summary(session, tid)
        assert summary.active_loans == 4
        assert summary.outstanding_balance == outstanding
        assert summary.npl_balance == npl
        assert summary.par30_balance == par30
        assert summary.provisions == provisions
        assert summary.npl_ratio_pct == npl_ratio
        assert summary.par30_ratio_pct == par30_ratio
        slices = {s.classification: s for s in summary.by_classification}
        assert slices[LoanClass.NORMAL].balance == Decimal("10000.00")
        assert slices[LoanClass.WATCH].balance == Decimal("20000.00")
        assert slices[LoanClass.SUBSTANDARD].balance == Decimal("15000.55")
        assert slices[LoanClass.LOSS].balance == Decimal("8000.33")
        assert slices[LoanClass.LOSS].provisions == Decimal("8000.33")

        # Same figures through the dashboard endpoint, cent-exact.
        async with api_client() as client:
            res = await client.get("/portfolio/summary", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["active_loans"] == 4
        assert body["outstanding_balance"] == str(outstanding)
        assert body["npl_balance"] == str(npl)
        assert body["npl_ratio_pct"] == str(npl_ratio)
        assert body["par30_balance"] == str(par30)
        assert body["par30_ratio_pct"] == str(par30_ratio)
        assert body["provisions"] == str(provisions)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Loan book API: keyset pagination (cap 100), filters, detail, schedule
# ---------------------------------------------------------------------------


def test_loan_book_keyset_pagination_and_filters() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        for amount in (Decimal("1000.00"), Decimal("2000.00"), Decimal("3000.00")):
            await _disburse(tid, amount)
        async with api_client() as client:
            page1 = await client.get("/loans", params={"limit": 2}, headers=headers)
            assert page1.status_code == 200
            body1 = page1.json()
            assert len(body1["items"]) == 2
            assert body1["next_cursor"] is not None
            page2 = await client.get(
                "/loans", params={"limit": 2, "cursor": body1["next_cursor"]}, headers=headers
            )
            assert page2.status_code == 200
            body2 = page2.json()
            assert len(body2["items"]) == 1
            assert body2["next_cursor"] is None
            ids = {x["id"] for x in body1["items"]} | {x["id"] for x in body2["items"]}
            assert len(ids) == 3  # no overlap between keyset pages
            over_cap = await client.get("/loans", params={"limit": 101}, headers=headers)
            assert over_cap.status_code == 422  # page cap 100 (gate 1.3)
            bad_cursor = await client.get(
                "/loans", params={"cursor": "not|a-cursor"}, headers=headers
            )
            assert bad_cursor.status_code == 400
            by_status = await client.get("/loans", params={"status": "active"}, headers=headers)
            assert by_status.status_code == 200
            assert len(by_status.json()["items"]) == 3
            by_cls = await client.get("/loans", params={"classification": "loss"}, headers=headers)
            assert by_cls.status_code == 200
            assert by_cls.json()["items"] == []

            detail = await client.get(f"/loans/{body2['items'][0]['id']}", headers=headers)
            assert detail.status_code == 200
            pill = detail.json()
            assert pill["classification"] == "normal"
            assert pill["days_past_due"] == 0
            assert pill["provision_pct"] == "1.00"
            missing = await client.get(f"/loans/{uuid.uuid4()}", headers=headers)
            assert missing.status_code == 404
            schedule = await client.get(
                f"/loans/{body2['items'][0]['id']}/schedule", headers=headers
            )
            assert schedule.status_code == 200
            assert len(schedule.json()) == 12

    asyncio.run(run())


def test_loan_book_direct_service_listing() -> None:
    """Direct list_loans coverage: filters, cursor round-trip, cap."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        for amount in (Decimal("1000.00"), Decimal("2000.00"), Decimal("3000.00")):
            await _disburse(tid, amount)
        async with tenant_session(factory(), tid) as session:
            page1, cursor = await loans_service.list_loans(session, tid, limit=2)
            assert len(page1) == 2
            assert cursor is not None
            page2, end = await loans_service.list_loans(session, tid, cursor=cursor, limit=2)
            assert len(page2) == 1
            assert end is None
            active, _ = await loans_service.list_loans(
                session, tid, status=LoanStatus.ACTIVE, classification=LoanClass.NORMAL, limit=200
            )
            assert len(active) == 3  # limit clamped to the 100 cap internally
            with pytest.raises(InvalidInputError, match="cursor"):
                await loans_service.list_loans(session, tid, cursor="garbage|garbage")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Disbursement + repayment through the API (P7 contract exposure)
# ---------------------------------------------------------------------------


def test_disburse_and_repay_via_api() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        mid = await _seed_member(tid)
        pid = await _seed_product(tid)
        app_id = await _seed_approved_application(tid, mid, pid, Decimal("12000.00"))
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/disburse",
                json={"channel": "bank"},
                headers=headers,
            )
            assert res.status_code == 201, res.text
            body = res.json()
            assert body["txn_ref"].startswith("LN-")
            assert len(body["schedule"]) == 12
            loan_id = body["loan_id"]

            again = await client.post(
                f"/applications/{app_id}/disburse",
                json={"channel": "bank"},
                headers=headers,
            )
            assert again.status_code == 409  # stage machine: disbursed is terminal

            bad_channel = await client.post(
                f"/applications/{uuid.uuid4()}/disburse",
                json={"channel": "accrual"},
                headers=headers,
            )
            assert bad_channel.status_code == 400

            repay = await client.post(
                f"/loans/{loan_id}/repayments",
                json={"amount": "500.00", "channel": "mpesa"},
                headers=headers,
            )
            assert repay.status_code == 201, repay.text
            repay_body = repay.json()
            assert repay_body["txn_ref"].startswith("RP-")
            assert repay_body["principal"] == "500.00"  # nothing due yet: all principal
            assert repay_body["balance_after"] == "11500.00"
            assert repay_body["status"] == "active"

            quote = await client.get(f"/loans/{loan_id}/settlement-quote", headers=headers)
            assert quote.status_code == 200
            assert quote.json()["total"] == "11500.00"

            bad_repay_channel = await client.post(
                f"/loans/{loan_id}/repayments",
                json={"amount": "10.00", "channel": "internal"},
                headers=headers,
            )
            assert bad_repay_channel.status_code == 400
            zero = await client.post(
                f"/loans/{loan_id}/repayments",
                json={"amount": "0", "channel": "bank"},
                headers=headers,
            )
            assert zero.status_code == 422

    asyncio.run(run())


def test_arrears_job_endpoint() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        loan_id, _ = await _disburse(tid, Decimal("9000.00"))
        await _backdate_installment(tid, loan_id, 1, 100)
        as_of = datetime.now(UTC).date()
        async with api_client() as client:
            res = await client.post(
                "/jobs/arrears",
                json={"as_of": as_of.isoformat(), "batch_size": 50},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["scanned"] == 1
            assert body["updated"] == 1
            rerun = await client.post(
                "/jobs/arrears", json={"as_of": as_of.isoformat()}, headers=headers
            )
            assert rerun.status_code == 200
            assert rerun.json()["updated"] == 0  # idempotent through the API too

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RBAC: deny-by-default on the loan book routes
# ---------------------------------------------------------------------------


def test_loan_book_rbac_enforced() -> None:
    async def run() -> None:
        _, _, teller_token = await _seed_actor("Teller")
        teller = _headers(teller_token)
        async with api_client() as client:
            assert (await client.get("/loans", headers=teller)).status_code == 403
            assert (await client.get("/portfolio/summary", headers=teller)).status_code == 403
            # Tellers post transactions: authz passes, the loan is simply absent.
            res = await client.post(
                f"/loans/{uuid.uuid4()}/repayments",
                json={"amount": "10.00", "channel": "mpesa"},
                headers=teller,
            )
            assert res.status_code == 404

        _, _, officer_token = await _seed_actor("Loan Officer")
        officer = _headers(officer_token)
        async with api_client() as client:
            assert (await client.get("/loans", headers=officer)).status_code == 200
            res = await client.post(
                f"/applications/{uuid.uuid4()}/disburse",
                json={"channel": "bank"},
                headers=officer,
            )
            assert res.status_code == 403  # loan_book:create is manager-level

        _, _, committee_token = await _seed_actor("Credit Committee")
        committee = _headers(committee_token)
        async with api_client() as client:
            assert (await client.get("/portfolio/summary", headers=committee)).status_code == 200
            res = await client.post("/jobs/arrears", json={}, headers=committee)
            assert res.status_code == 403  # loan_book:edit required

    asyncio.run(run())


def test_cross_tenant_loan_access_is_a_404() -> None:
    async def run() -> None:
        tid_a, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid_a, Decimal("4000.00"))
        _, _, token_b = await _seed_actor()
        async with api_client() as client:
            res = await client.get(f"/loans/{loan_id}", headers=_headers(token_b))
            assert res.status_code == 404
            quote = await client.get(
                f"/loans/{loan_id}/settlement-quote", headers=_headers(token_b)
            )
            assert quote.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Schedule bookkeeping feeds the arrears scan
# ---------------------------------------------------------------------------


def test_repayments_advance_schedule_paid_amounts() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"))
        await _backdate_installment(tid, loan_id, 1, 5)
        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        async with tenant_session(factory(), tid) as session:
            await loans_service.record_repayment(
                session, tid, None, loan_id, amount=schedule[0].total_due, channel=Channel.BANK
            )
        async with tenant_session(factory(), tid) as session:
            rows = await loans_service.get_schedule(session, tid, loan_id)
        assert rows[0].paid_amount == rows[0].total_due  # installment 1 settled
        assert all(r.paid_amount == Decimal("0.00") for r in rows[1:])
        # DPD is clear again as of today.
        result = await run_arrears_for_tenant(_scope(tid), tid, as_of=datetime.now(UTC).date())
        assert result.updated == 0  # already normal; nothing changes

    asyncio.run(run())


def test_principal_prepayment_never_consumes_future_interest() -> None:
    """Review finding 3: prepaid principal must not erase future interest.

    Pay installment 1 in full plus a 5000.00 principal prepayment. Cash
    may only settle installments due on or before the payment date;
    future installments keep their full contractual dues, so their
    interest is still owed in full when their due dates arrive (the old
    behaviour spread the prepayment across future rows, silently
    marking interest as collected that never was).
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"))
        await _backdate_installment(tid, loan_id, 1, 5)
        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        amount = to_cents(schedule[0].total_due + Decimal("5000.00"))
        async with tenant_session(factory(), tid) as session:
            result = await loans_service.record_repayment(
                session, tid, None, loan_id, amount=amount, channel=Channel.BANK
            )
        # Allocation oracle: only installment 1 interest is due; every
        # other shilling is principal (installment 1's + the prepayment).
        assert result.allocation.interest == schedule[0].interest_due
        assert result.allocation.principal == to_cents(
            schedule[0].principal_due + Decimal("5000.00")
        )
        assert result.balance_after == to_cents(
            Decimal("24000.00") - schedule[0].principal_due - Decimal("5000.00")
        )

        async with tenant_session(factory(), tid) as session:
            rows = await loans_service.get_schedule(session, tid, loan_id)
        assert rows[0].paid_amount == rows[0].total_due  # installment 1 settled
        # Regression: future installments are untouched by the prepayment.
        assert all(r.paid_amount == Decimal("0.00") for r in rows[1:])

        # Installment 2's interest is still due IN FULL at its due date.
        async with tenant_session(factory(), tid) as session:
            quote = await loans_service.get_settlement_quote(
                session, tid, loan_id, as_of=rows[1].due_date
            )
        assert quote.interest_due == schedule[1].interest_due
        assert quote.principal_balance == result.balance_after

    asyncio.run(run())


def test_settlement_quote_as_of_future_date() -> None:
    """as_of controls which installments count as due."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        loan_id, _ = await _disburse(tid, Decimal("24000.00"))
        schedule = build_schedule(Decimal("24000.00"), Decimal("12.00"), 12)
        future = datetime.now(UTC).date() + timedelta(days=40)
        async with tenant_session(factory(), tid) as session:
            quote_now = await loans_service.get_settlement_quote(session, tid, loan_id)
            quote_future = await loans_service.get_settlement_quote(
                session, tid, loan_id, as_of=future
            )
        assert quote_now.interest_due == Decimal("0.00")
        assert quote_future.interest_due == schedule[0].interest_due
        assert isinstance(future, date)

    asyncio.run(run())
