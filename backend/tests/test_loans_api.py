"""API + integration tests for P9: products, applications, votes, guarantees.

Requires a migrated PostgreSQL database (DATABASE_URL env var).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import loan_applications as applications_service
from genesis.application import loan_products as products_service
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.guarantees import consent_guarantee, pledge_guarantee
from genesis.application.rbac import seed_permissions
from genesis.domain.committee import Decision, Vote
from genesis.domain.lending import ApplicationStage
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


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


async def _add_actor(tid: uuid.UUID, role_id: uuid.UUID) -> str:
    """Add another user to an existing tenant and return their token."""
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                "CAST(:rid AS uuid), 'Extra Voter', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    return issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=role_id))


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _make_member(headers: dict[str, str], name: str) -> str:
    async with api_client() as client:
        res = await client.post(
            "/members",
            json={"type": "person", "name": name},
            headers=headers,
        )
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


async def _set_deposit_balance(tid: uuid.UUID, member_id: str, balance: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE deposit_accounts SET balance = :b WHERE member_id = CAST(:m AS uuid)"),
            {"b": balance, "m": member_id},
        )


async def _make_product(headers: dict[str, str], name: str) -> str:
    async with api_client() as client:
        res = await client.post(
            "/products",
            json={
                "name": name,
                "rate_pct": "12.00",
                "deposit_multiplier": "3.00",
                "max_term_months": 36,
            },
            headers=headers,
        )
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


async def _make_application(
    headers: dict[str, str],
    member_id: str,
    product_id: str,
    amount: str,
) -> dict[str, object]:
    async with api_client() as client:
        res = await client.post(
            "/applications",
            json={
                "member_id": member_id,
                "product_id": product_id,
                "amount": amount,
                "term_months": 12,
            },
            headers=headers,
        )
    assert res.status_code == 201, res.text
    body: dict[str, object] = res.json()
    return body


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def test_product_crud_and_duplicate_409() -> None:
    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        pid = await _make_product(headers, "Biashara Loan")
        async with api_client() as client:
            dup = await client.post(
                "/products",
                json={
                    "name": "Biashara Loan",
                    "rate_pct": "14.00",
                    "deposit_multiplier": "2.00",
                    "max_term_months": 24,
                },
                headers=headers,
            )
            assert dup.status_code == 409
            listed = await client.get("/products", headers=headers)
            assert listed.status_code == 200
            assert [p["name"] for p in listed.json()] == ["Biashara Loan"]
            updated = await client.put(
                f"/products/{pid}",
                json={"version": 1, "rate_pct": "13.50"},
                headers=headers,
            )
            assert updated.status_code == 200
            assert updated.json()["rate_pct"] == "13.50"
            stale = await client.put(
                f"/products/{pid}",
                json={"version": 1, "active": False},
                headers=headers,
            )
        assert stale.status_code == 409

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Applications: cover computation, product rules
# ---------------------------------------------------------------------------


def test_application_cover_and_product_rules() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Borrower One")
        await _set_deposit_balance(tid, member_id, "25000.00")
        product_id = await _make_product(headers, "Maendeleo Loan")
        app = await _make_application(headers, member_id, product_id, "50000.00")
        assert app["stage"] == "submitted"
        assert app["cover_pct"] == "50.00"
        assert app["rate_pct"] == "12.00"  # derived from the product, never client-supplied
        async with api_client() as client:
            over_term = await client.post(
                "/applications",
                json={
                    "member_id": member_id,
                    "product_id": product_id,
                    "amount": "10000.00",
                    "term_months": 48,
                },
                headers=headers,
            )
        assert over_term.status_code == 400
        assert over_term.json()["category"] == "validation_error"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: stage machine adversarial tests
# ---------------------------------------------------------------------------


def test_stage_machine_adversarial() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Stage Walker")
        await _set_deposit_balance(tid, member_id, "10000.00")
        product_id = await _make_product(headers, "Stage Product")
        app = await _make_application(headers, member_id, product_id, "5000.00")
        aid = app["id"]
        async with api_client() as client:
            direct_approve = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 1, "target": "approved"},
                headers=headers,
            )
            assert direct_approve.status_code == 409  # committee quorum only
            skip = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 1, "target": "committee"},
                headers=headers,
            )
            assert skip.status_code == 409  # submitted -> committee is illegal
            ok1 = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=headers,
            )
            assert ok1.status_code == 200
            ok2 = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 2, "target": "committee"},
                headers=headers,
            )
            assert ok2.status_code == 200
            backwards = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 3, "target": "appraisal"},
                headers=headers,
            )
            assert backwards.status_code == 409
        # Rejection is terminal.
        app2 = await _make_application(headers, member_id, product_id, "3000.00")
        aid2 = app2["id"]
        async with api_client() as client:
            rejected = await client.post(
                f"/applications/{aid2}/transition",
                json={"version": 1, "target": "rejected"},
                headers=headers,
            )
            assert rejected.status_code == 200
            revive = await client.post(
                f"/applications/{aid2}/transition",
                json={"version": 2, "target": "appraisal"},
                headers=headers,
            )
        assert revive.status_code == 409

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Committee voting: quorum, double-vote, closed stages
# ---------------------------------------------------------------------------


async def _application_in_committee(
    headers: dict[str, str], tid: uuid.UUID, name: str, product_id: str
) -> str:
    member_id = await _make_member(headers, name)
    await _set_deposit_balance(tid, member_id, "10000.00")
    app = await _make_application(headers, member_id, product_id, "5000.00")
    aid = str(app["id"])
    async with api_client() as client:
        r1 = await client.post(
            f"/applications/{aid}/transition",
            json={"version": 1, "target": "appraisal"},
            headers=headers,
        )
        assert r1.status_code == 200
        r2 = await client.post(
            f"/applications/{aid}/transition",
            json={"version": 2, "target": "committee"},
            headers=headers,
        )
        assert r2.status_code == 200
    return aid


def test_committee_voting_quorum_and_double_vote() -> None:
    async def run() -> None:
        tid, role_id, token = await _seed_actor()
        headers = _headers(token)
        product_id = await _make_product(headers, "Committee Product")
        aid = await _application_in_committee(headers, tid, "Vote Target", product_id)
        voter2 = _headers(await _add_actor(tid, role_id))
        voter3 = _headers(await _add_actor(tid, role_id))
        async with api_client() as client:
            v1 = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "approve"},
                headers=headers,
            )
            assert v1.status_code == 200
            assert v1.json()["decision"] is None
            dup = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "approve"},
                headers=headers,
            )
            assert dup.status_code == 409  # UNIQUE: one vote per member
            v2 = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "reject"},
                headers=voter2,
            )
            assert v2.status_code == 200
            assert v2.json()["decision"] is None
            v3 = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "approve"},
                headers=voter3,
            )
            assert v3.status_code == 200
            assert v3.json()["decision"] == "approved"
            assert v3.json()["stage"] == "approved"
            late_voter = _headers(await _add_actor(tid, role_id))
            closed = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "approve"},
                headers=late_voter,
            )
            assert closed.status_code == 409  # voting closed after decision
        async with tenant_session(factory(), tid) as session:
            decided = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'loan.application_decided'"
                    )
                )
            ).scalar_one()
        assert int(decided) == 1

    asyncio.run(run())


def test_committee_rejection_quorum() -> None:
    async def run() -> None:
        tid, role_id, token = await _seed_actor()
        headers = _headers(token)
        product_id = await _make_product(headers, "Reject Product")
        aid = await _application_in_committee(headers, tid, "Reject Target", product_id)
        voter2 = _headers(await _add_actor(tid, role_id))
        async with api_client() as client:
            r1 = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "reject"},
                headers=headers,
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/applications/{aid}/vote",
                json={"vote": "reject"},
                headers=voter2,
            )
        assert r2.status_code == 200
        assert r2.json()["decision"] == "rejected"
        assert r2.json()["stage"] == "rejected"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# EXIT: concurrent over-pledge - capacity never exceeded
# ---------------------------------------------------------------------------


def test_concurrent_over_pledge_capacity_never_exceeded() -> None:
    """10 parallel pledges of 300 against 1000 capacity: exactly 3 land."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Pledge Borrower")
        guarantor = await _make_member(headers, "Pledge Guarantor")
        await _set_deposit_balance(tid, borrower, "1000.00")
        await _set_deposit_balance(tid, guarantor, "1000.00")
        product_id = await _make_product(headers, "Pledge Product")
        app = await _make_application(headers, borrower, product_id, "9000.00")
        aid = uuid.UUID(str(app["id"]))
        g_id = uuid.UUID(guarantor)

        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=20, max_overflow=0)
        sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

        async def one_pledge() -> object:
            try:
                async with tenant_session(sm, tid) as session:
                    return await pledge_guarantee(
                        session,
                        tid,
                        None,
                        application_id=aid,
                        guarantor_member_id=g_id,
                        amount=Decimal("300.00"),
                    )
            except ConflictError as exc:
                return exc

        results = await asyncio.gather(*[one_pledge() for _ in range(10)])
        await engine.dispose()

        successes = [r for r in results if not isinstance(r, ConflictError)]
        failures = [r for r in results if isinstance(r, ConflictError)]
        assert len(successes) == 3, f"expected exactly 3 pledges, got {len(successes)}"
        assert len(failures) == 7
        async with tenant_session(factory(), tid) as session:
            total = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                        "WHERE guarantor_member_id = CAST(:g AS uuid) "
                        "AND status IN ('pledged', 'active')"
                    ),
                    {"g": guarantor},
                )
            ).scalar_one()
        assert Decimal(str(total)) == Decimal("900.00")
        assert Decimal(str(total)) <= Decimal("1000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Guarantees: consent flow, cover recompute, self-guarantee
# ---------------------------------------------------------------------------


def test_guarantee_consent_and_cover_recompute() -> None:
    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Cover Borrower")
        guarantor = await _make_member(headers, "Cover Guarantor")
        await _set_deposit_balance(tid, guarantor, "30000.00")
        product_id = await _make_product(headers, "Cover Product")
        app = await _make_application(headers, borrower, product_id, "40000.00")
        aid = app["id"]
        assert app["cover_pct"] == "0.00"  # borrower has no deposits
        async with api_client() as client:
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "20000.00"},
                headers=headers,
            )
            assert pledge.status_code == 201, pledge.text
            gid = pledge.json()["id"]
            refreshed = await client.get(f"/applications/{aid}", headers=headers)
            assert refreshed.json()["cover_pct"] == "50.00"
            consent = await client.post(
                f"/guarantees/{gid}/consent",
                json={"version": 1},
                headers=headers,
            )
            assert consent.status_code == 200
            assert consent.json()["status"] == "active"
            double = await client.post(
                f"/guarantees/{gid}/consent",
                json={"version": 2},
                headers=headers,
            )
            assert double.status_code == 409
            selfie = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": borrower, "amount": "1000.00"},
                headers=headers,
            )
        assert selfie.status_code == 400

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RBAC: deny-by-default on loans routes
# ---------------------------------------------------------------------------


def test_loans_rbac_enforced() -> None:
    async def run() -> None:
        _, _, teller_token = await _seed_actor("Teller")
        teller = _headers(teller_token)
        async with api_client() as client:
            res = await client.get("/applications", headers=teller)
            assert res.status_code == 403
            res = await client.post(
                "/applications",
                json={
                    "member_id": str(uuid.uuid4()),
                    "product_id": str(uuid.uuid4()),
                    "amount": "1000.00",
                    "term_months": 6,
                },
                headers=teller,
            )
            assert res.status_code == 403
            res = await client.get("/products", headers=teller)
        assert res.status_code == 403

    asyncio.run(run())


def test_application_for_unknown_member_returns_404() -> None:
    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        product_id = await _make_product(headers, "Ghost Member Product")
        async with api_client() as client:
            res = await client.post(
                "/applications",
                json={
                    "member_id": str(uuid.uuid4()),
                    "product_id": product_id,
                    "amount": "1000.00",
                    "term_months": 6,
                },
                headers=headers,
            )
        assert res.status_code == 404

    asyncio.run(run())


def test_application_listing_pagination_and_stage_filter() -> None:
    """Covers list_applications: stage filter, cursor, next_cursor."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "List Borrower")
        await _set_deposit_balance(tid, member_id, "100000.00")
        product_id = await _make_product(headers, "List Product")
        for _ in range(3):
            await _make_application(headers, member_id, product_id, "5000.00")
        async with api_client() as client:
            all_apps = await client.get("/applications", headers=headers)
            assert all_apps.status_code == 200
            assert len(all_apps.json()["items"]) == 3
            page1 = await client.get("/applications", params={"limit": 2}, headers=headers)
            assert page1.status_code == 200
            body1 = page1.json()
            assert len(body1["items"]) == 2
            assert body1["next_cursor"] is not None
            page2 = await client.get(
                "/applications",
                params={"limit": 2, "cursor": body1["next_cursor"]},
                headers=headers,
            )
            assert page2.status_code == 200
            body2 = page2.json()
            assert len(body2["items"]) == 1
            assert body2["next_cursor"] is None
            by_stage = await client.get(
                "/applications",
                params={"stage": "submitted"},
                headers=headers,
            )
            assert by_stage.status_code == 200
            assert all(a["stage"] == "submitted" for a in by_stage.json()["items"])
            empty = await client.get(
                "/applications",
                params={"stage": "approved"},
                headers=headers,
            )
            assert empty.status_code == 200
            assert len(empty.json()["items"]) == 0

    asyncio.run(run())


def test_application_listing_bad_cursor_returns_400() -> None:
    """Covers the InvalidInputError path in list_applications cursor parsing."""

    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            res = await client.get(
                "/applications",
                params={"cursor": "not|a-valid-cursor"},
                headers=headers,
            )
        assert res.status_code == 400
        assert res.json()["category"] == "validation_error"

    asyncio.run(run())


def test_get_single_application() -> None:
    """Covers get_application directly via the GET endpoint."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Get Borrower")
        await _set_deposit_balance(tid, member_id, "10000.00")
        product_id = await _make_product(headers, "Get Product")
        app = await _make_application(headers, member_id, product_id, "5000.00")
        aid = app["id"]
        async with api_client() as client:
            res = await client.get(f"/applications/{aid}", headers=headers)
            assert res.status_code == 200
            assert res.json()["id"] == aid
            assert res.json()["stage"] == "submitted"
            missing = await client.get(f"/applications/{uuid.uuid4()}", headers=headers)
            assert missing.status_code == 404

    asyncio.run(run())


def test_stale_version_on_transition_returns_409() -> None:
    """Covers the optimistic-lock check in transition_stage (rowcount != 1)."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Stale Transition")
        await _set_deposit_balance(tid, member_id, "10000.00")
        product_id = await _make_product(headers, "Stale Product")
        app = await _make_application(headers, member_id, product_id, "5000.00")
        aid = app["id"]
        async with api_client() as client:
            res = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 999, "target": "appraisal"},
                headers=headers,
            )
        assert res.status_code == 409

    asyncio.run(run())


def test_guarantee_on_non_pledgeable_stage_returns_409() -> None:
    """Covers the _PLEDGEABLE check in pledge_guarantee."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Pledge Stage Borrower")
        guarantor = await _make_member(headers, "Pledge Stage Guarantor")
        await _set_deposit_balance(tid, guarantor, "50000.00")
        product_id = await _make_product(headers, "Pledge Stage Product")
        app = await _make_application(headers, borrower, product_id, "5000.00")
        aid = app["id"]
        async with api_client() as client:
            reject = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 1, "target": "rejected"},
                headers=headers,
            )
            assert reject.status_code == 200
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "1000.00"},
                headers=headers,
            )
        assert pledge.status_code == 409

    asyncio.run(run())


def test_inactive_product_rejection() -> None:
    """Covers the product.active check in create_application."""

    async def run() -> None:
        _tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Inactive Prod Borrower")
        product_id = await _make_product(headers, "Deactivated Product")
        async with api_client() as client:
            deactivated = await client.put(
                f"/products/{product_id}",
                json={"version": 1, "active": False},
                headers=headers,
            )
            assert deactivated.status_code == 200
            res = await client.post(
                "/applications",
                json={
                    "member_id": member_id,
                    "product_id": product_id,
                    "amount": "1000.00",
                    "term_months": 6,
                },
                headers=headers,
            )
        assert res.status_code == 400

    asyncio.run(run())


def test_zero_amount_application_rejected() -> None:
    """Covers the amount <= ZERO guard in create_application."""

    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Zero Amount Borrower")
        product_id = await _make_product(headers, "Zero Product")
        async with api_client() as client:
            res = await client.post(
                "/applications",
                json={
                    "member_id": member_id,
                    "product_id": product_id,
                    "amount": "0",
                    "term_months": 6,
                },
                headers=headers,
            )
        assert res.status_code == 422

    asyncio.run(run())


def test_products_discovery_for_application_creators() -> None:
    """External Codex review, re-derived: the prototype's new-
    application form needs the product list, so applications:create
    (Loan Officer, who holds no settings permissions in the P4 matrix)
    grants GET /products alongside settings:view. Deny-by-default is
    preserved: Teller (neither grant) stays 403 — asserted in
    test_loans_rbac_enforced — and product WRITES remain settings-only
    (Loan Officer POST /products -> 403 here). Falsifiable: revert the
    route to SettingsViewCtx and the Loan Officer GET returns 403,
    failing this test."""

    async def run() -> None:
        _, _, officer_token = await _seed_actor("Loan Officer")
        officer = _headers(officer_token)
        async with api_client() as client:
            listed = await client.get("/products", headers=officer)
            assert listed.status_code == 200, listed.text
            created = await client.post(
                "/products",
                json={
                    "name": "Officer Product",
                    "rate_pct": "12.00",
                    "deposit_multiplier": "3.00",
                    "max_term_months": 12,
                },
                headers=officer,
            )
        assert created.status_code == 403

    asyncio.run(run())


def test_product_body_rejects_over_precise_decimals() -> None:
    """numeric(5,2) alignment (external Codex review, re-derived): a
    three-decimal rate or multiplier is a clean 422 at the boundary,
    never a DB precision error surfacing as a 500."""

    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        async with api_client() as client:
            bad_rate = await client.post(
                "/products",
                json={
                    "name": "Precise Rate",
                    "rate_pct": "12.005",
                    "deposit_multiplier": "3.00",
                    "max_term_months": 12,
                },
                headers=headers,
            )
            bad_mult = await client.put(
                f"/products/{uuid.uuid4()}",
                json={
                    "version": 1,
                    "deposit_multiplier": "3.005",
                },
                headers=headers,
            )
        assert bad_rate.status_code == 422
        assert bad_mult.status_code == 422

    asyncio.run(run())


def test_product_update_all_fields() -> None:
    """Covers update_product when updating deposit_multiplier, max_term_months, etc."""

    async def run() -> None:
        _, _, token = await _seed_actor()
        headers = _headers(token)
        pid = await _make_product(headers, "Full Update Product")
        async with api_client() as client:
            res = await client.put(
                f"/products/{pid}",
                json={
                    "version": 1,
                    "rate_pct": "15.00",
                    "deposit_multiplier": "4.50",
                    "max_term_months": 60,
                },
                headers=headers,
            )
            assert res.status_code == 200
            body = res.json()
            assert body["rate_pct"] == "15.00"
            assert body["deposit_multiplier"] == "4.50"
            assert body["max_term_months"] == 60

    asyncio.run(run())


def test_vote_and_consent_and_guarantor_404s_and_409s() -> None:
    """Covers NotFound and Capacity error paths in voting and guarantees."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Error Borrower")
        product_id = await _make_product(headers, "Error Product")
        app = await _make_application(headers, borrower, product_id, "10000.00")
        aid = app["id"]

        async with api_client() as client:
            # 404 on voting for non-existent application
            vote_missing = await client.post(
                f"/applications/{uuid.uuid4()}/vote",
                json={"vote": "approve"},
                headers=headers,
            )
            assert vote_missing.status_code == 404

            # 404 on consenting non-existent guarantee
            consent_missing = await client.post(
                f"/guarantees/{uuid.uuid4()}/consent",
                json={"version": 1},
                headers=headers,
            )
            assert consent_missing.status_code == 404

            # 404 on guarantor without deposit account
            no_dep_guarantor = uuid.uuid4()
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO members (id, tenant_id, member_no, type, name) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                        "'GP-9999', 'person', 'No Deposit')"
                    ),
                    {"id": str(no_dep_guarantor), "tid": str(tid)},
                )
            pledge_no_dep = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": str(no_dep_guarantor), "amount": "1000.00"},
                headers=headers,
            )
            assert pledge_no_dep.status_code == 404

            # Capacity exceeded error (409)
            guarantor = await _make_member(headers, "Broke Guarantor")
            await _set_deposit_balance(tid, guarantor, "100.00")
            over_capacity = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "500.00"},
                headers=headers,
            )
            assert over_capacity.status_code == 409

    asyncio.run(run())


def test_release_guarantees_for_loan_service() -> None:
    """Covers release_guarantees_for_loan in guarantees.py."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Release Borrower")
        guarantor = await _make_member(headers, "Release Guarantor")
        await _set_deposit_balance(tid, guarantor, "10000.00")
        product_id = await _make_product(headers, "Release Product")
        app = await _make_application(headers, borrower, product_id, "5000.00")
        aid = app["id"]

        async with api_client() as client:
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "2000.00"},
                headers=headers,
            )
            assert pledge.status_code == 201
            gid = pledge.json()["id"]

        loan_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO loans "
                    "(id, tenant_id, application_id, member_id, product_id, "
                    " principal, balance, rate_pct, term_months) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                    "CAST(:mid AS uuid), CAST(:pid AS uuid), 5000, 5000, '12.00', 12)"
                ),
                {
                    "id": str(loan_id),
                    "tid": str(tid),
                    "aid": aid,
                    "mid": borrower,
                    "pid": product_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE guarantees SET loan_id = CAST(:lid AS uuid) "
                    "WHERE id = CAST(:gid AS uuid)"
                ),
                {"lid": str(loan_id), "gid": gid},
            )

        from genesis.application.guarantees import release_guarantees_for_loan

        async with tenant_session(factory(), tid) as session:
            released = await release_guarantees_for_loan(session, tid, None, loan_id)
            assert released == 1

        async with tenant_session(factory(), tid) as session:
            released_again = await release_guarantees_for_loan(session, tid, None, loan_id)
            assert released_again == 0

    asyncio.run(run())


def test_direct_rejection_releases_pledges() -> None:
    """Regression: rejecting an application must release its pledges."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Reject Release Borrower")
        guarantor = await _make_member(headers, "Reject Release Guarantor")
        await _set_deposit_balance(tid, guarantor, "10000.00")
        product_id = await _make_product(headers, "Reject Release Product")
        app = await _make_application(headers, borrower, product_id, "5000.00")
        aid = app["id"]
        async with api_client() as client:
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "4000.00"},
                headers=headers,
            )
            assert pledge.status_code == 201
            gid = pledge.json()["id"]
            rejected = await client.post(
                f"/applications/{aid}/transition",
                json={"version": 1, "target": "rejected"},
                headers=headers,
            )
            assert rejected.status_code == 200
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM guarantees WHERE id = CAST(:g AS uuid)"),
                    {"g": gid},
                )
            ).scalar_one()
        assert status == "released"
        # Capacity is restored: the full balance can be pledged again.
        app2 = await _make_application(headers, borrower, product_id, "5000.00")
        async with api_client() as client:
            repledge = await client.post(
                f"/applications/{app2['id']}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "10000.00"},
                headers=headers,
            )
        assert repledge.status_code == 201

    asyncio.run(run())


def test_committee_rejection_releases_pledges() -> None:
    """Regression: a quorum rejection must release pledges too."""

    async def run() -> None:
        tid, role_id, token = await _seed_actor()
        headers = _headers(token)
        product_id = await _make_product(headers, "Quorum Release Product")
        guarantor = await _make_member(headers, "Quorum Release Guarantor")
        await _set_deposit_balance(tid, guarantor, "10000.00")
        aid = await _application_in_committee(headers, tid, "Quorum Release Borrower", product_id)
        async with api_client() as client:
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": guarantor, "amount": "3000.00"},
                headers=headers,
            )
            assert pledge.status_code == 201
            gid = pledge.json()["id"]
            voter2 = _headers(await _add_actor(tid, role_id))
            r1 = await client.post(
                f"/applications/{aid}/vote", json={"vote": "reject"}, headers=headers
            )
            assert r1.status_code == 200
            r2 = await client.post(
                f"/applications/{aid}/vote", json={"vote": "reject"}, headers=voter2
            )
            assert r2.status_code == 200
            assert r2.json()["decision"] == "rejected"
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM guarantees WHERE id = CAST(:g AS uuid)"),
                    {"g": gid},
                )
            ).scalar_one()
        assert status == "released"

    asyncio.run(run())


def test_cover_pct_clamped_to_column_bounds() -> None:
    """Regression: huge deposits vs a tiny amount must not overflow NUMERIC(6,2)."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        member_id = await _make_member(headers, "Clamp Borrower")
        await _set_deposit_balance(tid, member_id, "25000.00")
        product_id = await _make_product(headers, "Clamp Product")
        app = await _make_application(headers, member_id, product_id, "100.00")
        assert app["cover_pct"] == "9999.99"

    asyncio.run(run())


def test_exited_member_cannot_apply_or_guarantee() -> None:
    """Regression: lifecycle guards - exited members can neither borrow nor pledge."""

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        exited = await _make_member(headers, "Exited Member")
        borrower = await _make_member(headers, "Lifecycle Borrower")
        await _set_deposit_balance(tid, exited, "10000.00")
        product_id = await _make_product(headers, "Lifecycle Product")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE members SET status = 'exited' WHERE id = CAST(:m AS uuid)"),
                {"m": exited},
            )
        async with api_client() as client:
            apply_res = await client.post(
                "/applications",
                json={
                    "member_id": exited,
                    "product_id": product_id,
                    "amount": "1000.00",
                    "term_months": 6,
                },
                headers=headers,
            )
            assert apply_res.status_code == 409
            app = await _make_application(headers, borrower, product_id, "5000.00")
            pledge = await client.post(
                f"/applications/{app['id']}/guarantees",
                json={"guarantor_member_id": exited, "amount": "1000.00"},
                headers=headers,
            )
        assert pledge.status_code == 409

    asyncio.run(run())


def test_application_service_direct_flow() -> None:
    """Service-layer flow: create, list, pledge, consent, reject, release.

    Exercises the application layer directly (not through the ASGI app),
    following the test_ledger_integration convention, so the coverage
    gate reflects these paths.
    """

    async def run() -> None:
        tid, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _make_member(headers, "Direct Borrower")
        guarantor = await _make_member(headers, "Direct Guarantor")
        await _set_deposit_balance(tid, borrower, "5000.00")
        await _set_deposit_balance(tid, guarantor, "10000.00")
        product_id = await _make_product(headers, "Direct Product")

        async with tenant_session(factory(), tid) as session:
            record = await applications_service.create_application(
                session,
                tid,
                None,
                member_id=uuid.UUID(borrower),
                product_id=uuid.UUID(product_id),
                amount=Decimal("10000.00"),
                term_months=12,
            )
        assert record.stage is ApplicationStage.SUBMITTED
        assert record.cover_pct == Decimal("50.00")

        async with tenant_session(factory(), tid) as session:
            fetched = await applications_service.get_application(session, tid, record.id)
        assert fetched.version == 1

        async with tenant_session(factory(), tid) as session:
            items, next_cur = await applications_service.list_applications(session, tid, limit=10)
        assert next_cur is None
        assert any(a.id == record.id for a in items)

        async with tenant_session(factory(), tid) as session:
            guarantee = await pledge_guarantee(
                session,
                tid,
                None,
                application_id=record.id,
                guarantor_member_id=uuid.UUID(guarantor),
                amount=Decimal("2000.00"),
            )
        async with tenant_session(factory(), tid) as session:
            consented = await consent_guarantee(session, tid, None, guarantee.id, version=1)
        assert consented.status == "active"

        async with tenant_session(factory(), tid) as session:
            moved = await applications_service.transition_stage(
                session, tid, None, record.id, version=1, target=ApplicationStage.APPRAISAL
            )
        assert moved.stage is ApplicationStage.APPRAISAL
        async with tenant_session(factory(), tid) as session:
            moved = await applications_service.transition_stage(
                session, tid, None, record.id, version=2, target=ApplicationStage.COMMITTEE
            )
        assert moved.stage is ApplicationStage.COMMITTEE
        async with tenant_session(factory(), tid) as session:
            rejected = await applications_service.transition_stage(
                session, tid, None, record.id, version=3, target=ApplicationStage.REJECTED
            )
        assert rejected.stage is ApplicationStage.REJECTED
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM guarantees WHERE id = CAST(:g AS uuid)"),
                    {"g": str(guarantee.id)},
                )
            ).scalar_one()
        assert status == "released"

    asyncio.run(run())


def test_vote_service_direct_quorum_approval() -> None:
    """Service-layer voting: two approvals reach quorum and approve."""

    async def run() -> None:
        tid, role_id, token = await _seed_actor()
        headers = _headers(token)
        product_id = await _make_product(headers, "Direct Vote Product")
        aid = await _application_in_committee(headers, tid, "Direct Vote Borrower", product_id)
        application_id = uuid.UUID(aid)

        async def add_voter() -> uuid.UUID:
            user_id = uuid.uuid4()
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                        "CAST(:rid AS uuid), 'Direct Voter', :email)"
                    ),
                    {
                        "id": str(user_id),
                        "tid": str(tid),
                        "rid": str(role_id),
                        "email": unique_email(),
                    },
                )
            return user_id

        voter1 = await add_voter()
        voter2 = await add_voter()
        async with tenant_session(factory(), tid) as session:
            first = await applications_service.cast_vote(
                session, tid, voter1, application_id, Vote.APPROVE
            )
        assert first.decision is None
        assert first.stage is ApplicationStage.COMMITTEE
        async with tenant_session(factory(), tid) as session:
            second = await applications_service.cast_vote(
                session, tid, voter2, application_id, Vote.APPROVE
            )
        assert second.decision is Decision.APPROVED
        assert second.stage is ApplicationStage.APPROVED

    asyncio.run(run())


def test_product_service_direct() -> None:
    """Service-layer product CRUD with optimistic locking."""

    async def run() -> None:
        tid, _, _token = await _seed_actor()
        async with tenant_session(factory(), tid) as session:
            product = await products_service.create_product(
                session,
                tid,
                None,
                name="Direct Service Product",
                rate_pct=Decimal("10.00"),
                deposit_multiplier=Decimal("2.00"),
                max_term_months=24,
            )
        async with tenant_session(factory(), tid) as session:
            updated = await products_service.update_product(
                session, tid, None, product.id, version=1, rate_pct=Decimal("11.00")
            )
        assert updated.rate_pct == Decimal("11.00")
        assert updated.version == 2
        async with tenant_session(factory(), tid) as session:
            got = await products_service.get_product(session, tid, product.id)
        assert got.rate_pct == Decimal("11.00")
        async with tenant_session(factory(), tid) as session:
            listed = await products_service.list_products(session, tid)
        assert any(p.id == product.id for p in listed)

    asyncio.run(run())
