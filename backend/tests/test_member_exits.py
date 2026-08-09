"""Integration tests for P12 member exit & settlement (real Postgres, RLS).

Every settlement figure in these tests is hand-computed in comments —
never derived from the implementation (no oracle-from-implementation).
Idempotency and atomicity are proven by side-effect row counts.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import member_exits as exits_service
from genesis.application import transactions as txn_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.guarantees import pledge_guarantee
from genesis.application.loan_applications import create_application
from genesis.application.members import change_member_status
from genesis.application.rbac import seed_permissions
from genesis.domain.committee import Vote
from genesis.domain.exits import ExitStatus
from genesis.domain.ledger import Channel
from genesis.domain.members import MemberStatus
from genesis.errors import ConflictError, ForbiddenError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
    """Tenant + seeded permissions + bearer token; returns (tid, user_id, token)."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    uid = uuid.UUID(str(user_id))
    token = issue_access_token(AuthContext(user_id=uid, tenant_id=tid, role_id=role_id))
    return tid, uid, token


async def _add_user(tid: uuid.UUID, role_name: str) -> tuple[uuid.UUID, str]:
    """Extra user in an existing tenant (committee voters need >1 approver)."""
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
                "'Extra Voter', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
        rid = uuid.UUID(str(role_id))
    token = issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=rid))
    return user_id, token


async def _seed_member(
    tid: uuid.UUID, *, deposit: str = "0", shares: str = "0", status: str = "active"
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Exit Member', :status)"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}", "status": status},
        )
        for table, bal in (("deposit_accounts", deposit), ("share_accounts", shares)):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": bal},
            )
    return mid


async def _seed_active_loan(
    tid: uuid.UUID,
    mid: uuid.UUID,
    *,
    balance: str,
    penalty_due: str = "0",
    due_interest: str | None = None,
    future_interest: str | None = None,
) -> uuid.UUID:
    """Active loan + product + disbursed application + optional schedule rows.

    due_interest lands on an installment due YESTERDAY (collected in a
    settlement); future_interest on one due NEXT MONTH (waived, per the
    P10 early-settlement rule).
    """
    loan_id = uuid.uuid4()
    product_id = uuid.uuid4()
    app_id = uuid.uuid4()
    today = datetime.now(UTC).date()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(product_id), "tid": str(tid), "name": f"Exit-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, '12.00', 'disbursed', '0.00')"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(product_id),
                "amount": balance,
            },
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months, penalty_due) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), :amount, :amount, "
                "'12.00', 12, :penalty)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(app_id),
                "mid": str(mid),
                "pid": str(product_id),
                "amount": balance,
                "penalty": penalty_due,
            },
        )
        rows = []
        if due_interest is not None:
            rows.append((1, today - timedelta(days=1), due_interest))
        if future_interest is not None:
            rows.append((2, today + timedelta(days=30), future_interest))
        for no, due, interest in rows:
            await session.execute(
                text(
                    "INSERT INTO loan_schedules "
                    "(id, tenant_id, loan_id, installment_no, due_date, "
                    " principal_due, interest_due, total_due) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                    ":no, :due, 5000, :interest, 5000 + CAST(:interest AS numeric))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "lid": str(loan_id),
                    "no": no,
                    "due": due,
                    "interest": interest,
                },
            )
    return loan_id


async def _seed_guarantee(
    tid: uuid.UUID,
    *,
    guarantor: uuid.UUID,
    borrower: uuid.UUID,
    amount: str,
    status: str = "active",
    loan_id: uuid.UUID | None = None,
) -> uuid.UUID:
    gid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        attestor = None
        if status == "active":
            # P14.5 FM4: a row born 'active' must carry a consent
            # principal — seeded fixtures attest via any tenant user.
            attestor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                " amount, status, loan_id, consent_attested_by, consent_reference) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), :amount, :status, CAST(:lid AS uuid), "
                "CAST(:att AS uuid), :cref)"
            ),
            {
                "id": str(gid),
                "tid": str(tid),
                "g": str(guarantor),
                "b": str(borrower),
                "amount": amount,
                "status": status,
                "lid": str(loan_id) if loan_id else None,
                "att": str(attestor) if attestor is not None else None,
                "cref": "seeded fixture (P14.5 FM4)" if attestor is not None else None,
            },
        )
    return gid


async def _configure_fee(tid: uuid.UUID, fee: str) -> None:
    """Exit fee lives ONLY in tenant_settings (0010; the P11 rate lesson)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenant_settings "
                "(tenant_id, deposit_interest_annual_rate_pct, exit_fee) "
                "VALUES (CAST(:tid AS uuid), '8.00', :fee) "
                "ON CONFLICT (tenant_id) DO UPDATE SET exit_fee = EXCLUDED.exit_fee"
            ),
            {"tid": str(tid), "fee": fee},
        )


async def _balance(tid: uuid.UUID, mid: uuid.UUID, table: str) -> Decimal:
    async with tenant_session(factory(), tid) as session:
        val = (
            await session.execute(
                # Table name from test code, never user input.
                text(
                    f"SELECT balance FROM {table} "  # noqa: S608
                    "WHERE member_id = CAST(:m AS uuid)"
                ),
                {"m": str(mid)},
            )
        ).scalar_one()
    return Decimal(str(val))


async def _member_status(tid: uuid.UUID, mid: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        val = (
            await session.execute(
                text("SELECT status FROM members WHERE id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        ).scalar_one()
    return str(val)


async def _settlement_side_effects(tid: uuid.UUID) -> tuple[int, int, int, int]:
    """(exit txns, ledger legs on exit txns, settled audits, settled events)."""
    async with tenant_session(factory(), tid) as session:
        txns = (
            await session.execute(
                text("SELECT count(*) FROM transactions WHERE type = 'exit_settlement'")
            )
        ).scalar_one()
        legs = (
            await session.execute(
                text(
                    "SELECT count(*) FROM ledger_entries le "
                    "JOIN transactions t ON t.id = le.transaction_id "
                    "WHERE t.type = 'exit_settlement'"
                )
            )
        ).scalar_one()
        audits = (
            await session.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'member_exit.settled'")
            )
        ).scalar_one()
        events = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = 'member_exit.settled'")
            )
        ).scalar_one()
    return int(txns), int(legs), int(audits), int(events)


async def _request(tid: uuid.UUID, actor: uuid.UUID | None, mid: uuid.UUID) -> uuid.UUID:
    async with tenant_session(factory(), tid) as session:
        record = await exits_service.request_exit(session, tid, actor, mid)
    return record.id


async def _approve(tid: uuid.UUID, exit_id: uuid.UUID) -> tuple[int, uuid.UUID]:
    """Two fresh approvers reach quorum; returns (version, settler id).

    The settler is one of the approve-holders and is DISTINCT from the
    initiator: the user-level separation of duties bans the requesting
    user from posting the settlement of their own request.
    """
    v1, _ = await _add_user(tid, "System Admin")
    v2, _ = await _add_user(tid, "System Admin")
    async with tenant_session(factory(), tid) as session:
        await exits_service.cast_exit_vote(session, tid, v1, exit_id, Vote.APPROVE)
    async with tenant_session(factory(), tid) as session:
        tally = await exits_service.cast_exit_vote(session, tid, v2, exit_id, Vote.APPROVE)
    assert tally.status is ExitStatus.APPROVED
    async with tenant_session(factory(), tid) as session:
        record = await exits_service.get_exit(session, tid, exit_id)
    return record.version, v1


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_exit_blocked_by_live_guarantee_given_until_released() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00")
        borrower = await _seed_member(tid)
        gid = await _seed_guarantee(tid, guarantor=guarantor, borrower=borrower, amount="4000.00")
        with pytest.raises(ConflictError, match="guarantee obligations"):
            await _request(tid, uid, guarantor)
        # Nothing persisted by the rejected request.
        async with tenant_session(factory(), tid) as session:
            count = (await session.execute(text("SELECT count(*) FROM member_exits"))).scalar_one()
        assert int(count) == 0
        # Released obligation unblocks the exit.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE guarantees SET status = 'released' WHERE id = CAST(:g AS uuid)"),
                {"g": str(gid)},
            )
        exit_id = await _request(tid, uid, guarantor)
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.get_exit(session, tid, exit_id)
        assert record.status is ExitStatus.REQUESTED
        # Hand-computed: 10000 deposits + 0 shares - 0 loan - 0 fee.
        assert record.net_payable == Decimal("10000.00")

    asyncio.run(run())


def test_negative_settlement_rejected_with_nothing_persisted() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="1000.00", shares="500.00")
        # Hand-computed: equity 1500 < loan payoff 5000 -> net -3500.
        await _seed_active_loan(tid, mid, balance="5000.00")
        with pytest.raises(ConflictError, match="exceeds the member's equity"):
            await _request(tid, uid, mid)
        async with tenant_session(factory(), tid) as session:
            count = (await session.execute(text("SELECT count(*) FROM member_exits"))).scalar_one()
        assert int(count) == 0
        assert await _member_status(tid, mid) == "active"

    asyncio.run(run())


def test_arrears_member_may_request_exit_allow_list() -> None:
    """Status gate is an allow-list: 'active' OR 'arrears' may request.

    An arrears member with no active loan (e.g. reclassified before the
    loan was netted and closed) must still be able to leave and collect
    their equity.
    """

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="2500.00", status="arrears")
        exit_id = await _request(tid, uid, mid)
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.get_exit(session, tid, exit_id)
        assert record.status is ExitStatus.REQUESTED
        # Hand-computed: 2500 deposits + 0 shares - 0 loan - 0 fee.
        assert record.net_payable == Decimal("2500.00")

    asyncio.run(run())


def test_double_submit_creates_exactly_one_open_settlement() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="2000.00")

        async def submit() -> bool:
            try:
                await _request(tid, uid, mid)
            except ConflictError:
                return False
            return True

        results = await asyncio.gather(*(submit() for _ in range(5)))
        assert sum(results) == 1  # the partial UNIQUE collapses the race
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text("SELECT count(*) FROM member_exits WHERE member_id = CAST(:m AS uuid)"),
                    {"m": str(mid)},
                )
            ).scalar_one()
            audits = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'member_exit.request'")
                )
            ).scalar_one()
            events = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'member_exit.requested'"
                    )
                )
            ).scalar_one()
        # Idempotency by side effects: one row, one audit, one event.
        assert (int(rows), int(audits), int(events)) == (1, 1, 1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Committee voting (P9 machinery)
# ---------------------------------------------------------------------------


def test_votes_quorum_unique_initiator_ban_and_reject_path() -> None:
    async def run() -> None:
        tid, initiator, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="3000.00")
        exit_id = await _request(tid, initiator, mid)

        # Separation of duties: the initiator can never vote.
        with pytest.raises(ForbiddenError, match="initiator"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.cast_exit_vote(session, tid, initiator, exit_id, Vote.APPROVE)

        v1, _ = await _add_user(tid, "System Admin")
        v2, _ = await _add_user(tid, "System Admin")
        async with tenant_session(factory(), tid) as session:
            tally = await exits_service.cast_exit_vote(session, tid, v1, exit_id, Vote.REJECT)
        assert (tally.approvals, tally.rejections, tally.decision) == (0, 1, None)

        # One vote per voter, enforced by the DB UNIQUE.
        with pytest.raises(ConflictError, match="already voted"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.cast_exit_vote(session, tid, v1, exit_id, Vote.REJECT)

        async with tenant_session(factory(), tid) as session:
            tally = await exits_service.cast_exit_vote(session, tid, v2, exit_id, Vote.REJECT)
        assert tally.status is ExitStatus.REJECTED  # quorum of 2 decides

        # Voting is closed once decided.
        v3, _ = await _add_user(tid, "System Admin")
        with pytest.raises(ConflictError, match="only open"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.cast_exit_vote(session, tid, v3, exit_id, Vote.APPROVE)

        # A rejected exit frees the one-open-settlement slot.
        new_exit = await _request(tid, initiator, mid)
        assert new_exit != exit_id

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Separation of duties: the initiator can never settle their own request
# ---------------------------------------------------------------------------


def test_initiator_cannot_settle_own_request_distinct_approver_can() -> None:
    """User-level separation of duties on the money-moving step.

    The P4 matrix grants members:edit and members:approve to the same
    roles, so without the user-level ban the initiator could execute
    the settlement of their own request. Initiator attempt -> 403 with
    ZERO side effects; a different approve-holder settles fine.
    """

    async def run() -> None:
        tid, initiator, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="4000.00")
        exit_id = await _request(tid, initiator, mid)
        version, settler = await _approve(tid, exit_id)

        with pytest.raises(ForbiddenError, match="initiator"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.post_settlement(
                    session, tid, initiator, exit_id, version=version, channel=Channel.BANK
                )
        # Zero side effects: no posting, no transition, no balance move.
        assert await _settlement_side_effects(tid) == (0, 0, 0, 0)
        assert await _member_status(tid, mid) == "active"
        assert await _balance(tid, mid, "deposit_accounts") == Decimal("4000.00")
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.get_exit(session, tid, exit_id)
        assert record.status is ExitStatus.APPROVED  # untouched

        # A different approve-holder settles fine.
        async with tenant_session(factory(), tid) as session:
            result = await exits_service.post_settlement(
                session, tid, settler, exit_id, version=version, channel=Channel.BANK
            )
        assert result.exit.status is ExitStatus.SETTLED
        assert await _member_status(tid, mid) == "exited"
        assert (await _settlement_side_effects(tid))[0] == 1

    asyncio.run(run())


def test_initiator_may_void_own_request_documented_withdrawal_path() -> None:
    """Self-void is DELIBERATELY allowed (documented in void_exit).

    Voiding moves no money and only narrows state — it is the request-
    withdrawal path. The user-level bans apply to voting and settling.
    """

    async def run() -> None:
        tid, initiator, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="300.00")
        exit_id = await _request(tid, initiator, mid)
        async with tenant_session(factory(), tid) as session:
            voided = await exits_service.void_exit(session, tid, initiator, exit_id, version=1)
        assert voided.status is ExitStatus.REJECTED
        assert await _member_status(tid, mid) == "active"  # nothing moved

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Governance evidence: exit votes are non-erasable (ON DELETE RESTRICT)
# ---------------------------------------------------------------------------


def test_exit_votes_are_non_erasable_delete_restrict() -> None:
    """exit_votes.exit_id is ON DELETE RESTRICT (0010).

    Votes are governance evidence: deleting a voted-on exit row must
    fail loudly, never silently erase its vote trail (with CASCADE this
    DELETE would succeed and drop the votes — falsifiable).
    """

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="100.00")
        exit_id = await _request(tid, uid, mid)
        voter, _ = await _add_user(tid, "System Admin")
        async with tenant_session(factory(), tid) as session:
            await exits_service.cast_exit_vote(session, tid, voter, exit_id, Vote.APPROVE)
        with pytest.raises(IntegrityError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("DELETE FROM member_exits WHERE id = CAST(:id AS uuid)"),
                    {"id": str(exit_id)},
                )
        # The vote row survives the refused delete.
        async with tenant_session(factory(), tid) as session:
            votes = (
                await session.execute(
                    text("SELECT count(*) FROM exit_votes WHERE exit_id = CAST(:id AS uuid)"),
                    {"id": str(exit_id)},
                )
            ).scalar_one()
        assert int(votes) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Netted-loan settlement, hand-computed end to end
# ---------------------------------------------------------------------------


def test_netted_loan_exit_full_flow_hand_computed() -> None:
    """Prototype flow: equity nets the loan; guarantees on it are released.

    Hand-computed snapshot:
      shares 30000 + deposits 185000            = equity   215000.00
      loan 100000 principal + 1000 interest due
        (+900 future interest WAIVED) + 500 pen = payoff   101500.00
      exit fee (tenant_settings)                = fee         500.00
      net refund = 215000 - 101500 - 500        =          113000.00
    """

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        await _configure_fee(tid, "500.00")
        mid = await _seed_member(tid, deposit="185000.00", shares="30000.00")
        loan_id = await _seed_active_loan(
            tid,
            mid,
            balance="100000.00",
            penalty_due="500.00",
            due_interest="1000.00",
            future_interest="900.00",
        )
        guarantor = await _seed_member(tid, deposit="50000.00")
        await _seed_guarantee(
            tid, guarantor=guarantor, borrower=mid, amount="20000.00", loan_id=loan_id
        )

        exit_id = await _request(tid, uid, mid)
        async with tenant_session(factory(), tid) as session:
            snap = await exits_service.get_exit(session, tid, exit_id)
        assert snap.shares_amount == Decimal("30000.00")
        assert snap.deposits_amount == Decimal("185000.00")
        assert snap.loan_balance == Decimal("101500.00")
        assert snap.fees == Decimal("500.00")
        assert snap.net_payable == Decimal("113000.00")

        version, settler = await _approve(tid, exit_id)
        async with tenant_session(factory(), tid) as session:
            result = await exits_service.post_settlement(
                session, tid, settler, exit_id, version=version, channel=Channel.BANK
            )
        assert result.txn_ref is not None and result.txn_ref.startswith("WD-")
        assert result.exit.status is ExitStatus.SETTLED
        assert result.exit.settlement_transaction_id is not None

        # Ledger legs, hand-computed (DR 215000 == CR 215000).
        async with tenant_session(factory(), tid) as session:
            legs = (
                await session.execute(
                    text(
                        "SELECT le.account, le.side, le.amount FROM ledger_entries le "
                        "JOIN transactions t ON t.id = le.transaction_id "
                        "WHERE t.type = 'exit_settlement'"
                    )
                )
            ).all()
        by_leg = {(str(r[0]), str(r[1])): Decimal(str(r[2])) for r in legs}
        assert by_leg[("member.shares", "debit")] == Decimal("30000.00")
        assert by_leg[("member.deposits", "debit")] == Decimal("185000.00")
        assert by_leg[("loans.receivable", "credit")] == Decimal("100000.00")
        assert by_leg[("income.interest", "credit")] == Decimal("1000.00")
        assert by_leg[("income.penalties", "credit")] == Decimal("500.00")
        assert by_leg[("income.fees", "credit")] == Decimal("500.00")
        assert by_leg[("cash.bank", "credit")] == Decimal("113000.00")
        assert len(by_leg) == 7

        # Terminal state, zeroed accounts, closed loan, released guarantee.
        assert await _member_status(tid, mid) == "exited"
        assert await _balance(tid, mid, "deposit_accounts") == Decimal("0.00")
        assert await _balance(tid, mid, "share_accounts") == Decimal("0.00")
        async with tenant_session(factory(), tid) as session:
            loan = (
                await session.execute(
                    text(
                        "SELECT status, balance, penalty_due FROM loans WHERE id = CAST(:l AS uuid)"
                    ),
                    {"l": str(loan_id)},
                )
            ).one()
            g_status = (
                await session.execute(
                    text(
                        "SELECT status FROM guarantees WHERE borrower_member_id = CAST(:m AS uuid)"
                    ),
                    {"m": str(mid)},
                )
            ).scalar_one()
        assert str(loan[0]) == "closed"
        assert Decimal(str(loan[1])) == Decimal("0.00")
        assert Decimal(str(loan[2])) == Decimal("0.00")
        assert str(g_status) == "released"

        # Audit trail reconstructs the cash rail (channel + the posting
        # channel actually used — BANK here because net > 0).
        async with tenant_session(factory(), tid) as session:
            audit_channels = (
                await session.execute(
                    text(
                        "SELECT after->>'channel', after->>'posting_channel' "
                        "FROM audit_log WHERE action = 'member_exit.settled'"
                    )
                )
            ).one()
        assert (str(audit_channels[0]), str(audit_channels[1])) == ("bank", "bank")

        # A second posting attempt is refused; side effects stay single.
        with pytest.raises(ConflictError):
            async with tenant_session(factory(), tid) as session:
                await exits_service.post_settlement(
                    session, tid, settler, exit_id, version=version + 1, channel=Channel.BANK
                )
        assert await _settlement_side_effects(tid) == (1, 7, 1, 1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TOCTOU: balance moves between approval and posting -> 409, nothing posted
# ---------------------------------------------------------------------------


def test_toctou_drift_after_approval_returns_409_and_posts_nothing() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="5000.00")
        exit_id = await _request(tid, uid, mid)
        version, settler = await _approve(tid, exit_id)

        # The classic exploit window: money moves after approval.
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("100.00"), channel=Channel.BANK
            )

        with pytest.raises(ConflictError, match="stale"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.post_settlement(
                    session, tid, settler, exit_id, version=version, channel=Channel.BANK
                )
        # Nothing posted, nothing transitioned.
        assert await _settlement_side_effects(tid) == (0, 0, 0, 0)
        assert await _member_status(tid, mid) == "active"
        assert await _balance(tid, mid, "deposit_accounts") == Decimal("5100.00")
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.get_exit(session, tid, exit_id)
        assert record.status is ExitStatus.APPROVED  # untouched

        # Documented recovery: void the drifted snapshot, request afresh.
        # The initiator voids their own request — the documented
        # request-withdrawal path (self-void is deliberately allowed;
        # only voting and settling are user-separated).
        async with tenant_session(factory(), tid) as session:
            voided = await exits_service.void_exit(
                session, tid, uid, exit_id, version=record.version
            )
        assert voided.status is ExitStatus.REJECTED
        new_exit = await _request(tid, uid, mid)
        new_version, new_settler = await _approve(tid, new_exit)
        async with tenant_session(factory(), tid) as session:
            result = await exits_service.post_settlement(
                session, tid, new_settler, new_exit, version=new_version, channel=Channel.BANK
            )
        # Hand-computed: 5000 + 100 deposited after approval = 5100.
        assert result.exit.net_payable == Decimal("5100.00")
        assert await _member_status(tid, mid) == "exited"

    asyncio.run(run())


def test_void_with_stale_version_returns_409() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="100.00")
        exit_id = await _request(tid, uid, mid)
        with pytest.raises(ConflictError, match="stale version"):
            async with tenant_session(factory(), tid) as session:
                await exits_service.void_exit(session, tid, uid, exit_id, version=99)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Concurrency: exit settlement vs concurrent withdrawal — exactly one wins
# ---------------------------------------------------------------------------


def test_concurrent_settlement_vs_withdrawal_exactly_one_wins() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="5000.00")
        exit_id = await _request(tid, uid, mid)
        version, settler = await _approve(tid, exit_id)

        async def settle() -> bool:
            try:
                async with tenant_session(factory(), tid) as session:
                    await exits_service.post_settlement(
                        session, tid, settler, exit_id, version=version, channel=Channel.BANK
                    )
            except ConflictError:
                return False
            return True

        async def withdraw() -> bool:
            try:
                async with tenant_session(factory(), tid) as session:
                    await txn_service.record_withdrawal(
                        session, tid, None, mid, amount=Decimal("1000.00"), channel=Channel.BANK
                    )
            except ConflictError:
                return False
            return True

        settled, withdrew = await asyncio.gather(settle(), withdraw())
        assert settled != withdrew, "exactly one of the two must win"
        balance = await _balance(tid, mid, "deposit_accounts")
        if settled:
            # The exit won: member terminal, account zeroed, one posting.
            assert await _member_status(tid, mid) == "exited"
            assert balance == Decimal("0.00")
            assert (await _settlement_side_effects(tid))[0] == 1
        else:
            # The withdrawal won: snapshot drifted, settlement posted nothing.
            assert await _member_status(tid, mid) == "active"
            assert balance == Decimal("4000.00")
            assert await _settlement_side_effects(tid) == (0, 0, 0, 0)

    asyncio.run(run())


def test_concurrent_application_create_waits_for_exit_member_lock() -> None:
    """Gate 1.4 (external Codex review, re-derived): create_application
    takes the member row FOR SHARE (the P9 pledge / P11 _require_member
    precedent), so a concurrent settlement — which holds the member row
    FOR UPDATE and marks the member exited before committing — makes
    the create WAIT, then observe the terminal status and refuse,
    instead of inserting an open application beside an exited member
    (which would corrupt the exit invariant mark_member_exited
    enforces). Falsifiable: drop FOR SHARE from the member read in
    create_application and the status check reads the stale 'active'
    row mid-exit — the application lands and this test fails."""

    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="5000.00")
        product_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO loan_products "
                    "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, "
                    "'12.00', '3.00', 36)"
                ),
                {
                    "id": str(product_id),
                    "tid": str(tid),
                    "name": f"ExitRace-{uuid.uuid4().hex[:8]}",
                },
            )
        locked = asyncio.Event()

        async def exit_holding_member_lock() -> None:
            # Simulates the P12 settlement mid-transaction: member row
            # locked FOR UPDATE, status flipped, commit only after the
            # concurrent create has started waiting.
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
                    ),
                    {"m": str(mid), "tid": str(tid)},
                )
                await session.execute(
                    text(
                        "UPDATE members SET status = 'exited' "
                        "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": str(mid), "tid": str(tid)},
                )
                locked.set()
                await asyncio.sleep(0.3)

        async def apply_after_exit_started() -> bool:
            await locked.wait()
            try:
                async with tenant_session(factory(), tid) as session:
                    await create_application(
                        session,
                        tid,
                        uid,
                        member_id=mid,
                        product_id=product_id,
                        amount=Decimal("1000.00"),
                        term_months=6,
                    )
            except ConflictError:
                return False
            return True

        _, created = await asyncio.gather(exit_holding_member_lock(), apply_after_exit_started())
        assert created is False, "application create must wait, then reject the exited member"
        async with tenant_session(factory(), tid) as session:
            apps = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM loan_applications "
                        "WHERE member_id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": str(mid), "tid": str(tid)},
                )
            ).scalar_one()
        assert int(apps) == 0
        assert await _member_status(tid, mid) == "exited"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# KILL-SWITCH: abort mid-settlement -> zero partial rows (gate 1.5)
# ---------------------------------------------------------------------------


class _KillSwitchError(RuntimeError):
    """Simulated crash after the settlement service ran, before commit."""


def test_kill_switch_mid_settlement_leaves_zero_partial_state() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        await _configure_fee(tid, "250.00")
        mid = await _seed_member(tid, deposit="20000.00", shares="5000.00")
        loan_id = await _seed_active_loan(
            tid, mid, balance="8000.00", penalty_due="100.00", due_interest="400.00"
        )
        exit_id = await _request(tid, uid, mid)
        version, settler = await _approve(tid, exit_id)
        before = await _settlement_side_effects(tid)
        async with tenant_session(factory(), tid) as session:
            audit_before = (
                await session.execute(text("SELECT count(*) FROM audit_log"))
            ).scalar_one()
            outbox_before = (
                await session.execute(text("SELECT count(*) FROM outbox_events"))
            ).scalar_one()

        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await exits_service.post_settlement(
                    session, tid, settler, exit_id, version=version, channel=Channel.BANK
                )
                raise _KillSwitchError()  # crash inside the transaction

        # ZERO partial state: no posting, no balance change, no loan
        # change, no member/exit transition, no audit, no outbox row.
        assert await _settlement_side_effects(tid) == before == (0, 0, 0, 0)
        assert await _balance(tid, mid, "deposit_accounts") == Decimal("20000.00")
        assert await _balance(tid, mid, "share_accounts") == Decimal("5000.00")
        assert await _member_status(tid, mid) == "active"
        async with tenant_session(factory(), tid) as session:
            loan = (
                await session.execute(
                    text(
                        "SELECT status, balance, penalty_due FROM loans WHERE id = CAST(:l AS uuid)"
                    ),
                    {"l": str(loan_id)},
                )
            ).one()
            record = await exits_service.get_exit(session, tid, exit_id)
            audit_after = (
                await session.execute(text("SELECT count(*) FROM audit_log"))
            ).scalar_one()
            outbox_after = (
                await session.execute(text("SELECT count(*) FROM outbox_events"))
            ).scalar_one()
        assert str(loan[0]) == "active"
        assert Decimal(str(loan[1])) == Decimal("8000.00")
        assert Decimal(str(loan[2])) == Decimal("100.00")
        assert record.status is ExitStatus.APPROVED
        assert int(audit_after) == int(audit_before)
        assert int(outbox_after) == int(outbox_before)

        # The same call commits cleanly afterwards — the abort above was
        # the only reason nothing landed. Hand-computed:
        # 25000 equity - (8000 + 400 + 100) payoff - 250 fee = 16250.
        async with tenant_session(factory(), tid) as session:
            result = await exits_service.post_settlement(
                session, tid, settler, exit_id, version=version, channel=Channel.BANK
            )
        assert result.exit.net_payable == Decimal("16250.00")
        assert await _member_status(tid, mid) == "exited"
        assert (await _settlement_side_effects(tid))[0] == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Zero-equity exit: nothing to post, exit still settles
# ---------------------------------------------------------------------------


def test_zero_equity_exit_settles_without_posting() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid)  # all balances zero
        exit_id = await _request(tid, uid, mid)
        version, settler = await _approve(tid, exit_id)
        async with tenant_session(factory(), tid) as session:
            result = await exits_service.post_settlement(
                session, tid, settler, exit_id, version=version, channel=Channel.BANK
            )
        assert result.txn_ref is None  # documented: no money moved
        assert result.exit.status is ExitStatus.SETTLED
        assert await _member_status(tid, mid) == "exited"
        assert (await _settlement_side_effects(tid))[0] == 0  # zero postings

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Terminal state: every existing mutation path rejects exited members
# ---------------------------------------------------------------------------


def test_exited_member_resurrection_paths_all_rejected() -> None:
    async def run() -> None:
        tid, uid, _ = await _seed_actor()
        mid = await _seed_member(tid, deposit="1000.00")
        other = await _seed_member(tid)
        exit_id = await _request(tid, uid, mid)
        version, settler = await _approve(tid, exit_id)
        async with tenant_session(factory(), tid) as session:
            await exits_service.post_settlement(
                session, tid, settler, exit_id, version=version, channel=Channel.BANK
            )
        assert await _member_status(tid, mid) == "exited"

        # P11 money paths.
        for op_name in ("record_deposit", "record_withdrawal", "record_share_topup"):
            with pytest.raises(ConflictError):
                async with tenant_session(factory(), tid) as session:
                    await getattr(txn_service, op_name)(
                        session, tid, None, mid, amount=Decimal("10.00"), channel=Channel.BANK
                    )

        # P8 status machine (exited is terminal; also the sole-writer rule).
        for target in (MemberStatus.ACTIVE, MemberStatus.ARREARS, MemberStatus.EXITED):
            with pytest.raises(ConflictError):
                async with tenant_session(factory(), tid) as session:
                    await change_member_status(
                        session, tid, None, mid, version=2, new_status=target
                    )

        # P9 loan application.
        async with tenant_session(factory(), tid) as session:
            product_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO loan_products "
                    "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, "
                    "'12.00', '3.00', 36)"
                ),
                {
                    "id": str(product_id),
                    "tid": str(tid),
                    "name": f"Resurrect-{uuid.uuid4().hex[:8]}",
                },
            )
        with pytest.raises(ConflictError, match="active"):
            async with tenant_session(factory(), tid) as session:
                await create_application(
                    session,
                    tid,
                    None,
                    member_id=mid,
                    product_id=product_id,
                    amount=Decimal("1000.00"),
                    term_months=6,
                )

        # P9 guarantorship (exited member as guarantor).
        async with tenant_session(factory(), tid) as session:
            app_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO loan_applications "
                    "(id, tenant_id, member_id, product_id, amount, term_months, "
                    " rate_pct, stage, cover_pct) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    "CAST(:pid AS uuid), 1000, 6, '12.00', 'submitted', '0.00')"
                ),
                {
                    "id": str(app_id),
                    "tid": str(tid),
                    "mid": str(other),
                    "pid": str(product_id),
                },
            )
        with pytest.raises(ConflictError, match="active"):
            async with tenant_session(factory(), tid) as session:
                await pledge_guarantee(
                    session,
                    tid,
                    None,
                    application_id=app_id,
                    guarantor_member_id=mid,
                    amount=Decimal("100.00"),
                )

        # P12 itself: no second exit for an exited member.
        with pytest.raises(ConflictError, match="already exited"):
            await _request(tid, uid, mid)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# API: RBAC gates, no money in bodies, statement document
# ---------------------------------------------------------------------------


def test_exit_api_rbac_no_money_in_body_and_statement() -> None:
    async def run() -> None:
        tid, _, admin_token = await _seed_actor()
        admin = {"authorization": f"Bearer {admin_token}"}
        await _configure_fee(tid, "100.00")
        mid = await _seed_member(tid, deposit="900.00", shares="100.00")

        _, teller_token = await _add_user(tid, "Teller")
        teller = {"authorization": f"Bearer {teller_token}"}
        _, officer_token = await _add_user(tid, "Loan Officer")
        officer = {"authorization": f"Bearer {officer_token}"}
        _, accountant_token = await _add_user(tid, "Accountant")
        accountant = {"authorization": f"Bearer {accountant_token}"}

        async with api_client() as client:
            # members:edit gates creation — Teller (view) and Loan Officer
            # (create only) are both refused; money fields are refused
            # outright (fees come from tenant_settings, never the body).
            assert (
                await client.post("/member-exits", json={"member_id": str(mid)}, headers=teller)
            ).status_code == 403
            assert (
                await client.post("/member-exits", json={"member_id": str(mid)}, headers=officer)
            ).status_code == 403
            fee_attempt = await client.post(
                "/member-exits",
                json={"member_id": str(mid), "fees": "0.00"},
                headers=admin,
            )
            assert fee_attempt.status_code == 422  # extra="forbid"

            created = await client.post(
                "/member-exits",
                json={"member_id": str(mid), "reason": "Resignation"},
                headers=admin,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            exit_id = body["id"]
            # Hand-computed: 100 shares + 900 deposits - 100 fee = 900 net.
            assert body["net_payable"] == "900.00"
            assert body["fees"] == "100.00"

            # members:approve gates voting — Accountant (transactions
            # module only) is refused; the admin initiator is banned by
            # the user-level separation rule.
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/votes",
                    json={"vote": "approve"},
                    headers=accountant,
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/votes",
                    json={"vote": "approve"},
                    headers=admin,
                )
            ).status_code == 403

            _, voter1 = await _add_user(tid, "System Admin")
            _, voter2 = await _add_user(tid, "Branch Manager")
            first = await client.post(
                f"/member-exits/{exit_id}/votes",
                json={"vote": "approve"},
                headers={"authorization": f"Bearer {voter1}"},
            )
            assert first.status_code == 201
            assert first.json()["decision"] is None
            second = await client.post(
                f"/member-exits/{exit_id}/votes",
                json={"vote": "approve"},
                headers={"authorization": f"Bearer {voter2}"},
            )
            assert second.status_code == 201
            assert second.json()["status"] == "approved"

            # Settlement: members:approve required (a Teller with
            # transactions:create must NOT be able to finalise an exit);
            # extra money fields refused; accrual channel refused; the
            # admin INITIATOR is refused by the user-level separation
            # rule — a distinct approve-holder must post it.
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/settlement",
                    json={"version": 2, "channel": "bank"},
                    headers=teller,
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/settlement",
                    json={"version": 2, "channel": "bank", "amount": "1.00"},
                    headers=admin,
                )
            ).status_code == 422
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/settlement",
                    json={"version": 2, "channel": "accrual"},
                    headers=admin,
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/member-exits/{exit_id}/settlement",
                    json={"version": 2, "channel": "bank"},
                    headers=admin,
                )
            ).status_code == 403  # initiator cannot settle own request

            settled = await client.post(
                f"/member-exits/{exit_id}/settlement",
                json={"version": 2, "channel": "bank"},
                headers={"authorization": f"Bearer {voter1}"},
            )
            assert settled.status_code == 201, settled.text
            assert settled.json()["txn_ref"].startswith("WD-")

            # Statement document (members:view — the Teller may read it).
            statement = await client.get(f"/member-exits/{exit_id}/statement", headers=teller)
            assert statement.status_code == 200
            doc = statement.json()
            assert doc["shares_amount"] == "100.00"
            assert doc["deposits_amount"] == "900.00"
            assert doc["equity"] == "1000.00"
            assert doc["fees"] == "100.00"
            assert doc["net_payable"] == "900.00"
            assert doc["exit_status"] == "settled"
            assert doc["member_status"] == "exited"
            assert doc["settlement_txn_ref"] == settled.json()["txn_ref"]

            # Listing: keyset page with status filter (members:view).
            listing = await client.get(
                "/member-exits", params={"status": "settled", "limit": 1}, headers=teller
            )
            assert listing.status_code == 200
            assert [e["id"] for e in listing.json()["items"]] == [exit_id]

        # Cross-tenant invisibility (RLS + explicit predicates): a
        # different tenant sees 404 for this exit and its statement.
        _, _, foreign_token = await _seed_actor()
        foreign = {"authorization": f"Bearer {foreign_token}"}
        async with api_client() as client:
            assert (
                await client.get(f"/member-exits/{exit_id}", headers=foreign)
            ).status_code == 404
            assert (
                await client.get(f"/member-exits/{exit_id}/statement", headers=foreign)
            ).status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Issue #30 item 1 — initiator attribution on the exit read contract
# ---------------------------------------------------------------------------


def test_exit_read_contract_exposes_requested_by_least_disclosure() -> None:
    """`requested_by` rides ExitOut on create/get/list (issue #30 R3).

    The column has persisted since 0010 and already drives the
    self-vote/self-settle 403s; this exposes it so the approver can see
    WHO they are checking. Falsifiable: dropping the ExitOut field (or
    the `_out` mapping) makes the equality assertions below fail.

    Least disclosure (gate 1.6, stated per the P4 matrix): members:view
    holders receive the bare staff user UUID ONLY — the initiator's
    name/email never ride the exit read models; resolving the UUID
    stays behind access_control:view (P13.5 users/audit read paths).
    """

    async def run() -> None:
        tid, admin_uid, admin_token = await _seed_actor()
        admin = {"authorization": f"Bearer {admin_token}"}
        mid = await _seed_member(tid, deposit="500.00", shares="50.00")
        # The seeded admin's email — must never appear in exit payloads.
        async with tenant_session(factory(), tid) as session:
            admin_email = (
                await session.execute(
                    text("SELECT email FROM users WHERE id = CAST(:u AS uuid)"),
                    {"u": str(admin_uid)},
                )
            ).scalar_one()

        async with api_client() as client:
            created = await client.post(
                "/member-exits",
                json={"member_id": str(mid), "reason": "Relocation"},
                headers=admin,
            )
            assert created.status_code == 201, created.text
            assert created.json()["requested_by"] == str(admin_uid)

            exit_id = created.json()["id"]
            single = await client.get(f"/member-exits/{exit_id}", headers=admin)
            assert single.status_code == 200
            assert single.json()["requested_by"] == str(admin_uid)

            listing = await client.get(
                "/member-exits", params={"status": "requested", "limit": 5}, headers=admin
            )
            assert listing.status_code == 200
            by_id = {e["id"]: e for e in listing.json()["items"]}
            assert by_id[exit_id]["requested_by"] == str(admin_uid)

            # The statement DOCUMENT carries the initiator too (issue
            # #30 R3): an examiner reading a settlement sees WHO
            # requested it next to the figures — no audit-log context
            # switch. Same least-disclosure posture: the UUID only.
            statement = await client.get(f"/member-exits/{exit_id}/statement", headers=admin)
            assert statement.status_code == 200
            assert statement.json()["requested_by"] == str(admin_uid)
            assert admin_email not in statement.text

            # Least disclosure: the UUID only — no name/email keys, and
            # the initiator's PII appears nowhere in the payloads.
            for payload in (created, single):
                assert "Exit Member" not in str(payload.json().get("requested_by"))
                assert admin_email not in payload.text
                assert "full_name" not in payload.json()
                assert "email" not in payload.json()

        # Attribution is never invented (issue #30 FM2): a legacy-style
        # request recorded without an actor reads back as null, not as
        # some fabricated principal.
        unattributed_id = await _request(tid, None, await _seed_member(tid, deposit="10.00"))
        async with api_client() as client:
            legacy = await client.get(f"/member-exits/{unattributed_id}", headers=admin)
            assert legacy.status_code == 200
            assert legacy.json()["requested_by"] is None
            legacy_statement = await client.get(
                f"/member-exits/{unattributed_id}/statement", headers=admin
            )
            assert legacy_statement.status_code == 200
            assert legacy_statement.json()["requested_by"] is None

    asyncio.run(run())
