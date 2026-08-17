"""Integration tests for P12.5 issue #15 (real Postgres, RLS).

Deposit-multiplier enforcement at disbursement and guarantee-loan
linkage. Every oracle is hand-computed in comments; atomicity is
asserted via side-effect row counts.

Falsifiability (§4): the over-cap test fails if the multiplier gate is
removed from disburse_loan; the concurrency test fails if the deposit
read loses its FOR UPDATE (the stale pre-withdrawal balance would then
satisfy the cap); the linkage tests fail if the guarantees UPDATE is
dropped (release-on-closure would match zero rows again).
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.guarantees import consent_guarantee, pledge_guarantee
from genesis.application.ledger import disburse_loan
from genesis.application.rbac import seed_permissions
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures (P9 API flow: create -> appraise -> committee -> quorum approve)
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
    uid = uuid.UUID(str(user_id))
    token = issue_access_token(AuthContext(user_id=uid, tenant_id=tid, role_id=role_id))
    return tid, uid, token


async def _add_voter(tid: uuid.UUID) -> str:
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(text("SELECT id FROM roles WHERE name = 'System Admin'"))
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Second Voter', :email)"
            ),
            {"id": str(user_id), "tid": str(tid), "rid": str(role_id), "email": unique_email()},
        )
        rid = uuid.UUID(str(role_id))
    return issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=rid))


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "idempotency-key": uuid.uuid4().hex}


async def _make_member(tid: uuid.UUID, *, deposit: str) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Multiplier Member')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        for table in ("deposit_accounts", "share_accounts"):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "m": str(mid),
                    "bal": deposit if table == "deposit_accounts" else "0",
                },
            )
    return mid


async def _make_product(token: str, *, multiplier: str = "3.00") -> uuid.UUID:
    async with api_client() as client:
        res = await client.post(
            "/products",
            json={
                "name": f"Mult-{uuid.uuid4().hex[:8]}",
                "rate_pct": "12.00",
                "deposit_multiplier": multiplier,
                "max_term_months": 36,
            },
            headers=_headers(token),
        )
    assert res.status_code == 201, res.text
    return uuid.UUID(str(res.json()["id"]))


async def _approved_application(
    tid: uuid.UUID,
    token: str,
    member_id: uuid.UUID,
    product_id: uuid.UUID,
    amount: str,
) -> uuid.UUID:
    """Create an application and drive it to APPROVED via the P9 quorum."""
    second_token = await _add_voter(tid)
    async with api_client() as client:
        created = await client.post(
            "/applications",
            json={
                "member_id": str(member_id),
                "product_id": str(product_id),
                "amount": amount,
                "term_months": 12,
            },
            headers=_headers(token),
        )
        assert created.status_code == 201, created.text
        app_id = created.json()["id"]
        version = created.json()["version"]
        for target in ("appraisal", "committee"):
            moved = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": version, "target": target},
                headers=_headers(token),
            )
            assert moved.status_code == 200, moved.text
            version = moved.json()["version"]
        for voter_token in (token, second_token):
            voted = await client.post(
                f"/applications/{app_id}/vote",
                json={"vote": "approve"},
                headers=_headers(voter_token),
            )
            assert voted.status_code == 200, voted.text
    return uuid.UUID(str(app_id))


async def _loan_count(tid: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (await session.execute(text("SELECT count(*) FROM loans"))).scalar_one()
    return int(count)


async def _guarantee_row(tid: uuid.UUID, gid: uuid.UUID) -> tuple[str | None, str]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT loan_id, status FROM guarantees WHERE id = CAST(:id AS uuid)"),
                {"id": str(gid)},
            )
        ).one()
    return (str(row[0]) if row[0] is not None else None, str(row[1]))


# ---------------------------------------------------------------------------
# Multiplier gate at disbursement
# ---------------------------------------------------------------------------


def test_over_multiplier_disbursement_409_zero_side_effects() -> None:
    """Hand-computed cap: deposits 10000.00 x multiplier 3.00 + guarantees 0
    = 30000.00. A 30000.01 loan is over by one cent -> 409, nothing
    written (no loan, no txn, stage stays approved)."""

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        pid = await _make_product(token)  # multiplier 3.00
        app_id = await _approved_application(tid, token, mid, pid, "30000.01")

        with pytest.raises(ConflictError) as excinfo:
            async with tenant_session(factory(), tid) as session:
                await disburse_loan(session, tid, app_id, Channel.BANK, uid)
        # Least disclosure: no balances, multiplier, or shortfall echoed.
        message = str(excinfo.value)
        assert "10000" not in message
        assert "30000" not in message
        assert await _loan_count(tid) == 0
        async with tenant_session(factory(), tid) as session:
            stage = (
                await session.execute(
                    text("SELECT stage FROM loan_applications WHERE id = CAST(:id AS uuid)"),
                    {"id": str(app_id)},
                )
            ).scalar_one()
            txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        assert str(stage) == "approved"
        assert int(txns) == 0

    asyncio.run(run())


def test_disbursement_at_exact_cap_succeeds_with_guarantees() -> None:
    """Hand-computed cap: deposits 10000.00 x 3.00 + guarantee 5000.00
    = 35000.00. A loan of exactly 35000.00 disburses."""

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        guarantor = await _make_member(tid, deposit="6000.00")
        pid = await _make_product(token)
        app_id = await _approved_application(tid, token, mid, pid, "35000.00")
        # Pledge landed pre-approval in real flows; pledging is closed
        # after committee, so seed the live pledge row directly.
        gid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                    " application_id, amount, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), CAST(:a AS uuid), '5000.00', 'active')"
                ),
                {
                    "id": str(gid),
                    "tid": str(tid),
                    "g": str(guarantor),
                    "b": str(mid),
                    "a": str(app_id),
                },
            )
        async with tenant_session(factory(), tid) as session:
            result = await disburse_loan(session, tid, app_id, Channel.BANK, uid)
        assert await _loan_count(tid) == 1
        # The pledge is now linked to the loan and active.
        assert await _guarantee_row(tid, gid) == (str(result.loan_id), "active")

    asyncio.run(run())


def test_disbursement_blocks_unconsented_pledged_guarantees() -> None:
    """External Codex review, re-derived: a pledged guarantee may
    support cover% while the application is in flight, but it must
    never be activated as loan collateral without the guarantor's
    recorded consent (the P9 consent contract).

    Hand-computed cap: deposits 10000.00 x 3.00 + pledged guarantee
    5000.00 = 35000.00 — the cap is satisfied, yet disbursement still
    409s while the pledge is unconsented. Zero side effects: no loan,
    no transaction, stage stays approved, the guarantee row is
    untouched (still pledged, no loan_id), no link audit. Falsifiable:
    drop the Step 2c pledged-guarantee check from disburse_loan and
    the loan disburses with unconsented collateral — this test fails.
    """

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        guarantor = await _make_member(tid, deposit="6000.00")
        pid = await _make_product(token)  # multiplier 3.00
        app_id = await _approved_application(tid, token, mid, pid, "35000.00")
        gid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO guarantees "
                    "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                    " application_id, amount, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                    "CAST(:b AS uuid), CAST(:a AS uuid), '5000.00', 'pledged')"
                ),
                {
                    "id": str(gid),
                    "tid": str(tid),
                    "g": str(guarantor),
                    "b": str(mid),
                    "a": str(app_id),
                },
            )

        with pytest.raises(ConflictError) as excinfo:
            async with tenant_session(factory(), tid) as session:
                await disburse_loan(session, tid, app_id, Channel.BANK, uid)
        message = str(excinfo.value)
        assert "consented" in message
        # Least disclosure: no amounts or guarantor identity echoed.
        assert "5000" not in message
        assert str(guarantor) not in message
        assert await _loan_count(tid) == 0
        assert await _guarantee_row(tid, gid) == (None, "pledged")
        async with tenant_session(factory(), tid) as session:
            stage = (
                await session.execute(
                    text("SELECT stage FROM loan_applications WHERE id = CAST(:id AS uuid)"),
                    {"id": str(app_id)},
                )
            ).scalar_one()
            txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
            link_audits = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'guarantee.link_loan'")
                )
            ).scalar_one()
        assert str(stage) == "approved"
        assert int(txns) == 0
        assert int(link_audits) == 0

        # The documented resolution path: consent the pledge, then the
        # same disbursement succeeds and links exactly that guarantee.
        async with tenant_session(factory(), tid) as session:
            await consent_guarantee(session, tid, uid, gid, version=1)
        async with tenant_session(factory(), tid) as session:
            result = await disburse_loan(session, tid, app_id, Channel.BANK, uid)
        assert await _guarantee_row(tid, gid) == (str(result.loan_id), "active")

    asyncio.run(run())


def test_max_eligible_surfaced_on_application_read() -> None:
    """Hand-computed: deposits 10000.00 x 3.00 + live pledge 5000.00
    = 35000.00 on the single-application read model (issue #15)."""

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        guarantor = await _make_member(tid, deposit="6000.00")
        pid = await _make_product(token)
        async with api_client() as client:
            created = await client.post(
                "/applications",
                json={
                    "member_id": str(mid),
                    "product_id": str(pid),
                    "amount": "20000.00",
                    "term_months": 12,
                },
                headers=_headers(token),
            )
            assert created.status_code == 201, created.text
            app_id = created.json()["id"]
        async with tenant_session(factory(), tid) as session:
            record = await pledge_guarantee(
                session,
                tid,
                uid,
                application_id=uuid.UUID(str(app_id)),
                guarantor_member_id=guarantor,
                amount=Decimal("5000.00"),
            )
            await consent_guarantee(session, tid, uid, record.id, version=record.version)
        async with api_client() as client:
            fetched = await client.get(f"/applications/{app_id}", headers=_headers(token))
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["max_eligible"] == "35000.00"

    asyncio.run(run())


def test_concurrent_withdrawal_blocks_over_multiplier_disbursement() -> None:
    """Gate 1.4: the deposit read in disburse_loan is FOR UPDATE, so a
    disbursement racing a withdrawal waits on the account row lock and
    re-verifies against the withdrawn balance.

    Hand-computed: deposits 10000.00 x 3.00 = 30000.00 >= loan 30000.00
    before the withdrawal; after the concurrent withdrawal leaves
    1000.00, the cap is 3000.00 < 30000.00 -> 409, no loan. Falsifiable:
    drop FOR UPDATE from the deposit read and the disbursement reads the
    stale 10000.00 while the withdrawal is uncommitted -> loan created
    -> this test fails.
    """

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        pid = await _make_product(token)
        app_id = await _approved_application(tid, token, mid, pid, "30000.00")

        locked = asyncio.Event()

        async def withdraw_holding_lock() -> None:
            # Simulates the P11 withdrawal writer mid-transaction: row
            # locked, balance updated, commit after the disbursement has
            # started waiting.
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "SELECT balance FROM deposit_accounts "
                        "WHERE member_id = CAST(:m AS uuid) FOR UPDATE"
                    ),
                    {"m": str(mid)},
                )
                await session.execute(
                    text(
                        "UPDATE deposit_accounts SET balance = '1000.00' "
                        "WHERE member_id = CAST(:m AS uuid)"
                    ),
                    {"m": str(mid)},
                )
                locked.set()
                await asyncio.sleep(0.3)

        async def disburse_after_lock() -> bool:
            await locked.wait()
            try:
                async with tenant_session(factory(), tid) as session:
                    await disburse_loan(session, tid, app_id, Channel.BANK, uid)
            except ConflictError:
                return False
            return True

        _, disbursed = await asyncio.gather(withdraw_holding_lock(), disburse_after_lock())
        assert disbursed is False, "over-multiplier disbursement must be blocked"
        assert await _loan_count(tid) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Guarantee-loan linkage: release-on-closure end to end
# ---------------------------------------------------------------------------


def test_linked_guarantee_released_on_loan_closure_end_to_end() -> None:
    """Pledge -> consent -> approve -> disburse links the guarantee to
    the loan; paying the loan off releases it through the P10 closure
    hook (issue #15's exact bug: before the linkage, the hook matched
    zero rows)."""

    async def run() -> None:
        tid, uid, token = await _seed_actor()
        mid = await _make_member(tid, deposit="2000.00")
        guarantor = await _make_member(tid, deposit="6000.00")
        pid = await _make_product(token)
        async with api_client() as client:
            created = await client.post(
                "/applications",
                json={
                    "member_id": str(mid),
                    "product_id": str(pid),
                    "amount": "5000.00",
                    "term_months": 12,
                },
                headers=_headers(token),
            )
            assert created.status_code == 201, created.text
            app_id = created.json()["id"]
            version = created.json()["version"]
        # Pledge while pledgeable (submitted), then drive to approval.
        async with tenant_session(factory(), tid) as session:
            pledge = await pledge_guarantee(
                session,
                tid,
                uid,
                application_id=uuid.UUID(str(app_id)),
                guarantor_member_id=guarantor,
                amount=Decimal("4000.00"),
            )
            await consent_guarantee(session, tid, uid, pledge.id, version=pledge.version)
        second_token = await _add_voter(tid)
        async with api_client() as client:
            for target in ("appraisal", "committee"):
                moved = await client.post(
                    f"/applications/{app_id}/transition",
                    json={"version": version, "target": target},
                    headers=_headers(token),
                )
                assert moved.status_code == 200, moved.text
                version = moved.json()["version"]
            for voter_token in (token, second_token):
                voted = await client.post(
                    f"/applications/{app_id}/vote",
                    json={"vote": "approve"},
                    headers=_headers(voter_token),
                )
                assert voted.status_code == 200, voted.text
            # Cap check (hand-computed): 2000.00 x 3.00 + 4000.00
            # = 10000.00 >= 5000.00 -> disburses.
            disbursed = await client.post(
                f"/applications/{app_id}/disburse",
                json={"channel": "bank"},
                headers=_headers(token),
            )
            assert disbursed.status_code == 201, disbursed.text
            loan_id = disbursed.json()["loan_id"]

        assert await _guarantee_row(tid, pledge.id) == (loan_id, "active")
        async with tenant_session(factory(), tid) as session:
            link_audits = (
                await session.execute(
                    text("SELECT count(*) FROM audit_log WHERE action = 'guarantee.link_loan'")
                )
            ).scalar_one()
        assert int(link_audits) == 1

        # Pay the loan off via the settlement quote; closure releases
        # the guarantee through release_guarantees_for_loan.
        async with api_client() as client:
            quote = await client.get(f"/loans/{loan_id}/settlement-quote", headers=_headers(token))
            assert quote.status_code == 200, quote.text
            total = quote.json()["total"]
            repaid = await client.post(
                f"/loans/{loan_id}/repayments",
                json={"amount": total, "channel": "bank"},
                headers=_headers(token),
            )
            assert repaid.status_code == 201, repaid.text
            assert repaid.json()["status"] == "closed"
        assert await _guarantee_row(tid, pledge.id) == (loan_id, "released")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Contract hardening: money bodies reject unknown fields (issue #12)
# ---------------------------------------------------------------------------


def test_money_bodies_reject_caller_supplied_occurred_at() -> None:
    """A caller-sent posting date must be a 422, never silently ignored
    (extra="forbid"; the P11 caller-input lesson)."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        mid = await _make_member(tid, deposit="10000.00")
        pid = await _make_product(token)
        app_id = await _approved_application(tid, token, mid, pid, "5000.00")
        async with api_client() as client:
            deposit = await client.post(
                f"/members/{mid}/deposits",
                json={
                    "amount": "100.00",
                    "channel": "bank",
                    "occurred_at": "2020-01-01T00:00:00Z",
                },
                headers=_headers(token),
            )
            disburse = await client.post(
                f"/applications/{app_id}/disburse",
                json={"channel": "bank", "disbursed_at": "2020-01-01T00:00:00Z"},
                headers=_headers(token),
            )
        assert deposit.status_code == 422
        assert disburse.status_code == 422
        # Nothing landed from either attempt.
        assert await _loan_count(tid) == 0
        async with tenant_session(factory(), tid) as session:
            txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        assert int(txns) == 0

    asyncio.run(run())
