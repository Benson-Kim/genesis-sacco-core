"""P13.14 — per-guarantee release & substitution (real Postgres, RLS).

Every numbered banking-grade failure mode from the prompt has its own
falsifiable test (remove the guard under test and the test fails):

  1. cover strip (sequential falsifiable + concurrent exactly-one)
  2. bare release on a disbursed loan structurally impossible
  3. substitution shortfall + concurrent over-pledge on the substitute
  4. substitution without consent refused
  5. double release -> exactly one state transition (side-effect counts)
  6. kill-switch mid-substitution -> zero partial state
  7. wrong actor (member principal self-service vs staff matrix — P14.5)
  8. tenant scoping (issue-#17 probe: own-tenant session, foreign arg)
  9. exit unblocked after release/substitution, end-to-end through the
     real P12 workflow
 10. least disclosure: error envelopes never echo figures

All oracles are hand-computed in comments; idempotency/atomicity are
asserted via side-effect row counts, never return values alone.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import guarantees as guarantees_service
from genesis.application.auth import (
    AuthContext,
    MemberAuthContext,
    issue_access_token,
    issue_member_access_token,
)
from genesis.application.rbac import seed_permissions
from genesis.errors import ConflictError, NotFoundError, UnprocessableError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: Evidence citation for staff-attested substitute consent (review R1):
#: mandatory on every substitution; recorded verbatim in the dedicated
#: guarantee.consent audit row and the confirmation outbox event.
_CONSENT_REF = "signed guarantorship form GF-1314"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_actor(
    role_name: str = "System Admin",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Tenant + permissions + one user; returns (tid, user_id, role_id, token)."""
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
    return tid, uid, role_id, token


async def _add_user(
    tid: uuid.UUID, role_name: str, *, email: str | None = None
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Extra user in an existing tenant; returns (user_id, role_id, token)."""
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
                "email": email or unique_email(),
            },
        )
        rid = uuid.UUID(str(role_id))
    token = issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=rid))
    return user_id, rid, token


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_member(
    tid: uuid.UUID,
    *,
    deposit: str = "0",
    shares: str = "0",
    email: str | None = None,
    status: str = "active",
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, email, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'G Member', :email, :st)"
            ),
            {
                "id": str(mid),
                "tid": str(tid),
                "no": f"GP-{mid.hex[:6].upper()}",
                "email": email,
                "st": status,
            },
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


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    """Product with multiplier 3.00 — the factor in every hand-computed
    cover oracle below (max_eligible = deposits x 3 + live guarantees)."""
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"P1314-{uuid.uuid4().hex[:8]}"},
        )
    return pid


async def _seed_application(
    tid: uuid.UUID,
    mid: uuid.UUID,
    pid: uuid.UUID,
    *,
    amount: str,
    stage: str = "approved",
) -> uuid.UUID:
    aid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, '12.00', :stage, '0.00')"
            ),
            {
                "id": str(aid),
                "tid": str(tid),
                "mid": str(mid),
                "pid": str(pid),
                "amount": amount,
                "stage": stage,
            },
        )
    return aid


async def _seed_loan(
    tid: uuid.UUID, mid: uuid.UUID, pid: uuid.UUID, aid: uuid.UUID, *, balance: str
) -> uuid.UUID:
    loan_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), :amount, :amount, '12.00', 12)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(aid),
                "mid": str(mid),
                "pid": str(pid),
                "amount": balance,
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
    application_id: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
) -> uuid.UUID:
    gid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        attestor = None
        if status == "active":
            # P14.5 FM4: the 0035 trigger refuses a row ENTERING
            # 'active' without a principal, so seeded active fixtures
            # carry a staff attestation (any tenant user).
            attestor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                " application_id, loan_id, amount, status, "
                " consent_attested_by, consent_reference) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), CAST(:a AS uuid), CAST(:l AS uuid), :amount, :status, "
                "CAST(:att AS uuid), :cref)"
            ),
            {
                "id": str(gid),
                "tid": str(tid),
                "g": str(guarantor),
                "b": str(borrower),
                "a": str(application_id) if application_id else None,
                "l": str(loan_id) if loan_id else None,
                "amount": amount,
                "status": status,
                "att": str(attestor) if attestor is not None else None,
                "cref": "seeded fixture (P14.5 FM4)" if attestor is not None else None,
            },
        )
    return gid


async def _link_member_credential(
    tid: uuid.UUID, member_id: uuid.UUID, *, email: str | None = None
) -> uuid.UUID:
    """Seed an ACTIVE member credential link (the P14.5 identity)."""
    cid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO member_credentials (id, tenant_id, member_id, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :email)"
            ),
            {
                "id": str(cid),
                "tid": str(tid),
                "m": str(member_id),
                "email": email or unique_email(),
            },
        )
    return cid


def _member_headers(
    tid: uuid.UUID, credential_id: uuid.UUID, member_id: uuid.UUID
) -> dict[str, str]:
    token = issue_member_access_token(
        MemberAuthContext(credential_id=credential_id, member_id=member_id, tenant_id=tid)
    )
    return {"authorization": f"Bearer {token}"}


async def _guarantee_state(tid: uuid.UUID, gid: uuid.UUID) -> tuple[str, int]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT status, version FROM guarantees WHERE id = CAST(:g AS uuid)"),
                {"g": str(gid)},
            )
        ).first()
    assert row is not None
    return str(row[0]), int(row[1])


async def _release_side_effects(tid: uuid.UUID, gid: uuid.UUID) -> tuple[int, int]:
    """(release audit rows for this guarantee, released outbox events)."""
    async with tenant_session(factory(), tid) as session:
        audits = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'guarantee.release' AND entity_id = :gid"
                ),
                {"gid": str(gid)},
            )
        ).scalar_one()
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'guarantee.released' "
                    "AND payload->>'guarantee_id' = :gid"
                ),
                {"gid": str(gid)},
            )
        ).scalar_one()
    return int(audits), int(events)


async def _substitution_side_effects(tid: uuid.UUID, gid: uuid.UUID) -> tuple[int, int]:
    """(substitute audit rows referencing gid, substituted outbox events)."""
    async with tenant_session(factory(), tid) as session:
        audits = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'guarantee.substitute' AND after->>'replaces' = :gid"
                ),
                {"gid": str(gid)},
            )
        ).scalar_one()
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE event_type = 'guarantee.substituted' "
                    "AND payload->>'released_guarantee_id' = :gid"
                ),
                {"gid": str(gid)},
            )
        ).scalar_one()
    return int(audits), int(events)


def _engine_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=10, max_overflow=0)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Happy path: staff release of a pledged guarantee + cover recompute
# ---------------------------------------------------------------------------


def test_staff_release_pledged_recomputes_cover_and_notifies_both_sides() -> None:
    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        headers = _headers(token)
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="30000.00")
        pid = await _seed_product(tid)
        async with api_client() as client:
            created = await client.post(
                "/applications",
                json={
                    "member_id": str(borrower),
                    "product_id": str(pid),
                    "amount": "40000.00",
                    "term_months": 12,
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            aid = created.json()["id"]
            pledge = await client.post(
                f"/applications/{aid}/guarantees",
                json={"guarantor_member_id": str(guarantor), "amount": "20000.00"},
                headers=headers,
            )
            assert pledge.status_code == 201, pledge.text
            gid = pledge.json()["id"]
            # Hand-computed: (0 deposits + 20000 pledge) / 40000 = 50%.
            refreshed = await client.get(f"/applications/{aid}", headers=headers)
            assert refreshed.json()["cover_pct"] == "50.00"

            released = await client.post(
                f"/guarantees/{gid}/release",
                json={"version": 1},
                headers=headers,
            )
            assert released.status_code == 200, released.text
            body = released.json()
            assert body["status"] == "released"
            assert body["version"] == 2
            # No amounts arrive from the caller; the row's amount echoes back.
            assert body["amount"] == "20000.00"

            # Hand-computed: cover drops back to 0/40000 = 0%.
            after = await client.get(f"/applications/{aid}", headers=headers)
            assert after.json()["cover_pct"] == "0.00"

        status, version = await _guarantee_state(tid, uuid.UUID(gid))
        assert (status, version) == ("released", 2)
        # Exactly one audit row and one outbox notification per side
        # (guarantor + borrower = 2), written in-transaction.
        audits, events = await _release_side_effects(tid, uuid.UUID(gid))
        assert (audits, events) == (1, 2)
        async with tenant_session(factory(), tid) as session:
            notified = (
                await session.execute(
                    text(
                        "SELECT payload->>'notify_member_id' FROM outbox_events "
                        "WHERE event_type = 'guarantee.released' "
                        "AND payload->>'guarantee_id' = :gid"
                    ),
                    {"gid": gid},
                )
            ).scalars()
            assert set(notified) == {str(guarantor), str(borrower)}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 1 — cover strip (falsifiable + concurrent exactly-one)
# ---------------------------------------------------------------------------


def test_release_below_cover_rejected_under_lock_falsifiable() -> None:
    """EXIT criterion: release-below-cover rejected.

    Hand-computed: amount 5000; borrower deposits 0 (x3 multiplier = 0);
    single active guarantee 6000. Post-release eligibility would be
    0 + 0 = 0 < 5000 -> refused. Falsifiable: remove the under-lock
    re-verification in release_guarantee and the release succeeds,
    failing this test.
    """

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)  # deposit 0
        guarantor = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="approved")
        gid = await _seed_guarantee(
            tid, guarantor=guarantor, borrower=borrower, amount="6000.00", application_id=aid
        )
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/release",
                json={"version": 1},
                headers=_headers(token),
            )
        assert res.status_code == 409, res.text
        # Least disclosure (failure mode 10): the envelope carries the
        # category and correlation id only — no cover/capacity figures.
        assert set(res.json()) == {"category", "correlation_id"}
        assert res.json()["category"] == "conflict"
        # Zero mutations: the rejection rolled the release back.
        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _release_side_effects(tid, gid) == (0, 0)

    asyncio.run(run())


def test_concurrent_releases_that_jointly_strip_cover_exactly_one_wins() -> None:
    """Adversarial EXIT criterion: two releases race on one application.

    Hand-computed: amount 5000, deposits 0, guarantees 6000 + 6000.
    Either release alone leaves 6000 >= 5000 (valid); both would leave
    0 < 5000. The application row lock serialises them; the second
    re-verifies against the first's committed effect -> exactly one
    succeeds.
    """

    async def run() -> None:
        tid, uid, _role_id, _ = await _seed_actor()
        borrower = await _seed_member(tid)
        g1 = await _seed_member(tid, deposit="10000.00")
        g2 = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="approved")
        gid1 = await _seed_guarantee(
            tid, guarantor=g1, borrower=borrower, amount="6000.00", application_id=aid
        )
        gid2 = await _seed_guarantee(
            tid, guarantor=g2, borrower=borrower, amount="6000.00", application_id=aid
        )

        engine, sm = _engine_factory()

        async def one_release(gid: uuid.UUID) -> object:
            try:
                async with tenant_session(sm, tid) as session:
                    return await guarantees_service.release_guarantee(
                        session, tid, uid, gid, version=1
                    )
            except ConflictError as exc:
                return exc

        results = await asyncio.gather(one_release(gid1), one_release(gid2))
        await engine.dispose()

        successes = [r for r in results if not isinstance(r, ConflictError)]
        assert len(successes) == 1, "exactly one of the two releases must land"
        async with tenant_session(factory(), tid) as session:
            live_total = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                        "WHERE application_id = CAST(:a AS uuid) "
                        "AND status IN ('pledged', 'active')"
                    ),
                    {"a": str(aid)},
                )
            ).scalar_one()
        # Hand-computed: one 6000 guarantee must survive (6000 >= 5000).
        assert Decimal(str(live_total)) == Decimal("6000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 2 — bare release on a disbursed loan is impossible
# ---------------------------------------------------------------------------


def test_bare_release_of_disbursed_loan_guarantee_returns_409() -> None:
    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="5000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/release",
                json={"version": 1},
                headers=_headers(token),
            )
        assert res.status_code == 409, res.text
        assert set(res.json()) == {"category", "correlation_id"}
        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _release_side_effects(tid, gid) == (0, 0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Substitution happy path (atomic swap) + failure modes 3 and 4
# ---------------------------------------------------------------------------


def test_substitution_swaps_atomically_and_notifies_all_parties() -> None:
    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="8000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="5000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            body = res.json()
        assert body["released"]["status"] == "released"
        assert body["released"]["id"] == str(gid)
        replacement = body["replacement"]
        # Amount omitted from the request -> derived from the guarantee
        # row (hand-computed floor: 5000.00).
        assert replacement["amount"] == "5000.00"
        assert replacement["status"] == "active"
        assert replacement["loan_id"] == str(loan_id)
        assert replacement["application_id"] == str(aid)
        assert replacement["guarantor_member_id"] == str(new_g)

        assert await _guarantee_state(tid, gid) == ("released", 2)
        assert await _guarantee_state(tid, uuid.UUID(replacement["id"])) == ("active", 1)
        # Side effects: 1 release audit + 1 substitute audit; 3
        # substituted notifications (old guarantor, new guarantor,
        # borrower) — all in one transaction.
        release_audits, release_events = await _release_side_effects(tid, gid)
        assert (release_audits, release_events) == (1, 0)
        sub_audits, sub_events = await _substitution_side_effects(tid, gid)
        assert (sub_audits, sub_events) == (1, 3)
        async with tenant_session(factory(), tid) as session:
            notified = (
                await session.execute(
                    text(
                        "SELECT payload->>'notify_member_id' FROM outbox_events "
                        "WHERE event_type = 'guarantee.substituted' "
                        "AND payload->>'released_guarantee_id' = :gid"
                    ),
                    {"gid": str(gid)},
                )
            ).scalars()
            assert set(notified) == {str(old_g), str(new_g), str(borrower)}

    asyncio.run(run())


def test_substitution_shortfall_and_missing_consent_refused() -> None:
    """Failure modes 3 (shortfall) and 4 (no consent): both 422, both
    leave the original guarantee fully intact."""

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="8000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="5000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            # Hand-computed shortfall: 4999.99 < released 5000.00.
            short = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": _CONSENT_REF,
                    "amount": "4999.99",
                },
                headers=_headers(token),
            )
            assert short.status_code == 422, short.text
            assert set(short.json()) == {"category", "correlation_id"}

            # P14.5: the caller-asserted consented boolean is REMOVED
            # from the contract (the !29 lesson made structural) —
            # presenting it is an unknown field, a structural 422
            # (extra="forbid"), never an accepted assertion.
            legacy_flag = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consented": True,
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
            assert legacy_flag.status_code == 422, legacy_flag.text

            # Review R1: the attestation must cite its evidence — a
            # missing or blank consent_reference is refused before any
            # read or write (both 422, zero mutations asserted below).
            no_ref = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                },
                headers=_headers(token),
            )
            assert no_ref.status_code == 422, no_ref.text
            blank_ref = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": "   ",
                },
                headers=_headers(token),
            )
            assert blank_ref.status_code == 422, blank_ref.text
            assert set(blank_ref.json()) == {"category", "correlation_id"}

            # The borrower can never be their own substitute guarantor.
            selfie = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(borrower),
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
            assert selfie.status_code == 400, selfie.text

        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _substitution_side_effects(tid, gid) == (0, 0)
        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM guarantees WHERE loan_id = CAST(:l AS uuid)"),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
        assert int(count) == 1  # no replacement row was ever created

    asyncio.run(run())


def test_concurrent_substitutions_never_over_pledge_the_substitute() -> None:
    """Failure mode 3, adversarial (the P9 over-pledge pattern).

    Hand-computed: substitute capacity 1000; two concurrent swaps of
    800 each onto the same substitute -> exactly one lands; the loser's
    already-written release must roll back with it (guarantee stays
    active). Falsifiable: drop the deposit-account lock or the capacity
    check and both land (1600 > 1000).
    """

    async def run() -> None:
        tid, uid, _, _ = await _seed_actor()
        substitute = await _seed_member(tid, deposit="1000.00")
        pid = await _seed_product(tid)
        gids: list[uuid.UUID] = []
        for _n in range(2):
            borrower = await _seed_member(tid)
            old_g = await _seed_member(tid, deposit="2000.00")
            aid = await _seed_application(tid, borrower, pid, amount="800.00", stage="disbursed")
            loan_id = await _seed_loan(tid, borrower, pid, aid, balance="800.00")
            gids.append(
                await _seed_guarantee(
                    tid,
                    guarantor=old_g,
                    borrower=borrower,
                    amount="800.00",
                    application_id=aid,
                    loan_id=loan_id,
                )
            )

        engine, sm = _engine_factory()

        async def one_swap(gid: uuid.UUID) -> object:
            try:
                async with tenant_session(sm, tid) as session:
                    return await guarantees_service.substitute_guarantee(
                        session,
                        tid,
                        uid,
                        gid,
                        version=1,
                        guarantor_member_id=substitute,
                        consent_reference=_CONSENT_REF,
                    )
            except ConflictError as exc:
                return exc

        results = await asyncio.gather(one_swap(gids[0]), one_swap(gids[1]))
        await engine.dispose()

        successes = [r for r in results if not isinstance(r, ConflictError)]
        assert len(successes) == 1, "exactly one substitution may land"
        async with tenant_session(factory(), tid) as session:
            pledged = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                        "WHERE guarantor_member_id = CAST(:g AS uuid) "
                        "AND status IN ('pledged', 'active')"
                    ),
                    {"g": str(substitute)},
                )
            ).scalar_one()
        # Hand-computed: 800 <= 1000 capacity; never 1600.
        assert Decimal(str(pledged)) == Decimal("800.00")
        # The losing swap's release rolled back: its guarantee is intact.
        states = [await _guarantee_state(tid, gid) for gid in gids]
        assert sorted(s[0] for s in states) == ["active", "released"]

    asyncio.run(run())


def test_substitution_capacity_shortfall_leaves_original_intact() -> None:
    """Sequential capacity refusal under the substitute's deposit lock.

    Hand-computed: capacity 500 < replacement 800 -> 409; the release
    written earlier in the same transaction rolls back.
    """

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="2000.00")
        poor_sub = await _seed_member(tid, deposit="500.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="800.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="800.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="800.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(poor_sub),
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
        assert res.status_code == 409, res.text
        assert set(res.json()) == {"category", "correlation_id"}
        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _substitution_side_effects(tid, gid) == (0, 0)

    asyncio.run(run())


def test_substitution_of_pledged_or_undisbursed_guarantee_refused() -> None:
    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="approved")
        pledged_gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            status="pledged",
            application_id=aid,
        )
        active_undisbursed_gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            application_id=aid,
        )
        async with api_client() as client:
            for gid in (pledged_gid, active_undisbursed_gid):
                res = await client.post(
                    f"/guarantees/{gid}/substitute",
                    json={
                        "version": 1,
                        "guarantor_member_id": str(new_g),
                        "consent_reference": _CONSENT_REF,
                    },
                    headers=_headers(token),
                )
                assert res.status_code == 409, res.text
        assert await _guarantee_state(tid, pledged_gid) == ("pledged", 1)
        assert await _guarantee_state(tid, active_undisbursed_gid) == ("active", 1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 5 — double release: exactly one state transition
# ---------------------------------------------------------------------------


def test_double_release_concurrent_and_idempotency_key_replay() -> None:
    async def run() -> None:
        tid, uid, _role_id, token = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="submitted")
        gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            status="pledged",
            application_id=aid,
        )

        engine, sm = _engine_factory()

        async def one_release() -> object:
            try:
                async with tenant_session(sm, tid) as session:
                    return await guarantees_service.release_guarantee(
                        session, tid, uid, gid, version=1
                    )
            except ConflictError as exc:
                return exc

        results = await asyncio.gather(one_release(), one_release())
        await engine.dispose()
        successes = [r for r in results if not isinstance(r, ConflictError)]
        assert len(successes) == 1, "concurrent double release must land exactly once"
        # Side-effect row counts prove the single transition: one audit
        # row, one notification per side, one version bump.
        assert await _guarantee_state(tid, gid) == ("released", 2)
        assert await _release_side_effects(tid, gid) == (1, 2)

        # Idempotency-Key replay on the API route (gate 1.4): a second
        # pledged guarantee, released twice under one key -> the replay
        # returns the stored response and adds NO side effects.
        gid2 = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            status="pledged",
            application_id=aid,
        )
        key = f"p1314-{uuid.uuid4().hex[:10]}"
        headers = {**_headers(token), "idempotency-key": key}
        async with api_client() as client:
            first = await client.post(
                f"/guarantees/{gid2}/release", json={"version": 1}, headers=headers
            )
            assert first.status_code == 200, first.text
            replay = await client.post(
                f"/guarantees/{gid2}/release", json={"version": 1}, headers=headers
            )
            assert replay.status_code == 200
            assert replay.headers.get("idempotency-replayed") == "true"
            assert replay.json() == first.json()
            # A retry WITHOUT the key hits the real state: 409.
            retry = await client.post(
                f"/guarantees/{gid2}/release", json={"version": 2}, headers=_headers(token)
            )
            assert retry.status_code == 409
        assert await _guarantee_state(tid, gid2) == ("released", 2)
        assert await _release_side_effects(tid, gid2) == (1, 2)

    asyncio.run(run())


def test_release_stale_version_409_and_unknown_404_and_forbidden_extras() -> None:
    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="submitted")
        gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            status="pledged",
            application_id=aid,
        )
        async with api_client() as client:
            stale = await client.post(
                f"/guarantees/{gid}/release", json={"version": 99}, headers=_headers(token)
            )
            assert stale.status_code == 409
            missing = await client.post(
                f"/guarantees/{uuid.uuid4()}/release",
                json={"version": 1},
                headers=_headers(token),
            )
            assert missing.status_code == 404
            # extra="forbid": a smuggled amount is a structural 422.
            extra = await client.post(
                f"/guarantees/{gid}/release",
                json={"version": 1, "amount": "1.00"},
                headers=_headers(token),
            )
            assert extra.status_code == 422
        assert await _guarantee_state(tid, gid) == ("pledged", 1)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 6 — kill-switch mid-substitution: zero partial state
# ---------------------------------------------------------------------------


class _KillSwitchError(RuntimeError):
    pass


def test_kill_switch_mid_substitution_leaves_original_guarantee_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXIT criterion: abort AFTER the release write, BEFORE the
    replacement pledge is inserted (the capacity computation sits
    between the two) -> the transaction rolls back and the ORIGINAL
    guarantee is fully intact. Zero partial state: no released status,
    no replacement row, no audit, no outbox."""

    async def run() -> None:
        tid, uid, _, _ = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="5000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=loan_id,
        )

        async def boom(
            session: object, tenant_id: uuid.UUID, guarantor_member_id: uuid.UUID
        ) -> Decimal:
            raise _KillSwitchError()

        # live_pledged_total runs inside _guarantor_available_capacity,
        # after the release UPDATE and before the replacement INSERT.
        monkeypatch.setattr(guarantees_service, "live_pledged_total", boom)
        with pytest.raises(_KillSwitchError):
            async with tenant_session(factory(), tid) as session:
                await guarantees_service.substitute_guarantee(
                    session,
                    tid,
                    uid,
                    gid,
                    version=1,
                    guarantor_member_id=new_g,
                    consent_reference=_CONSENT_REF,
                )
        monkeypatch.undo()

        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _release_side_effects(tid, gid) == (0, 0)
        assert await _substitution_side_effects(tid, gid) == (0, 0)
        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM guarantees WHERE loan_id = CAST(:l AS uuid)"),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
        assert int(count) == 1  # only the original — zero partial state

        # The same call commits cleanly afterwards: the abort above was
        # the only reason it failed.
        async with tenant_session(factory(), tid) as session:
            released, replacement = await guarantees_service.substitute_guarantee(
                session,
                tid,
                uid,
                gid,
                version=1,
                guarantor_member_id=new_g,
                consent_reference=_CONSENT_REF,
            )
        assert released.status == "released"
        assert replacement.status == "active"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 7 — wrong actor
# ---------------------------------------------------------------------------


def test_guarantor_releases_own_unconsented_pledge_only() -> None:
    """P14.5 (FM2, wrong actor): the guarantor acts for themselves as
    the MEMBER principal on /member/guarantees/{id}/release — the
    retired email-match never decides. Their own PLEDGED guarantee
    releases; someone else's pledge and their own ACTIVE collateral
    get the SAME single refusal (least disclosure). A staff token with
    applications:view only (Auditor) is now refused AT THE ROUTE: the
    staff release path is applications:edit exactly.
    """

    async def run() -> None:
        tid, _, _, _ = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        other_member = await _seed_member(tid, deposit="10000.00")
        g_cred = await _link_member_credential(tid, guarantor)
        other_cred = await _link_member_credential(tid, other_member)
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="submitted")
        own_pledged = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="500.00",
            status="pledged",
            application_id=aid,
        )
        own_active = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="500.00",
            status="active",
            application_id=aid,
        )
        self_headers = _member_headers(tid, g_cred, guarantor)
        other_headers = _member_headers(tid, other_cred, other_member)

        async with api_client() as client:
            # Another member's principal can release nobody's pledge.
            res = await client.post(
                f"/member/guarantees/{own_pledged}/release",
                json={"version": 1},
                headers=other_headers,
            )
            assert res.status_code == 403, res.text
            assert set(res.json()) == {"category", "correlation_id"}
            assert await _guarantee_state(tid, own_pledged) == ("pledged", 1)

            # The guarantor cannot self-release CONSENTED collateral.
            res = await client.post(
                f"/member/guarantees/{own_active}/release",
                json={"version": 1},
                headers=self_headers,
            )
            assert res.status_code == 403, res.text
            assert await _guarantee_state(tid, own_active) == ("active", 1)

            # An applications:view-only STAFF token is refused at the
            # staff route (applications:edit exactly since P14.5).
            _, _, viewer_token = await _add_user(tid, "Auditor")
            res = await client.post(
                f"/guarantees/{own_pledged}/release",
                json={"version": 1},
                headers=_headers(viewer_token),
            )
            assert res.status_code == 403, res.text
            assert await _guarantee_state(tid, own_pledged) == ("pledged", 1)

            # The guarantor CAN withdraw their own unconsented pledge —
            # and the audit actor is the CREDENTIAL, the acting
            # principal (P14.5 actor model).
            res = await client.post(
                f"/member/guarantees/{own_pledged}/release",
                json={"version": 1},
                headers=self_headers,
            )
            assert res.status_code == 200, res.text
        assert await _guarantee_state(tid, own_pledged) == ("released", 2)
        async with tenant_session(factory(), tid) as session:
            actor = (
                await session.execute(
                    text(
                        "SELECT actor_id FROM audit_log "
                        "WHERE action = 'guarantee.release' AND entity_id = :gid"
                    ),
                    {"gid": str(own_pledged)},
                )
            ).scalar_one()
        assert uuid.UUID(str(actor)) == g_cred

    asyncio.run(run())


def test_release_and_substitute_permission_matrix_per_role() -> None:
    """P4 matrix walk for the guarantee routes (P14.5 posture).

    release: applications:edit EXACTLY (System Admin, Branch Manager,
    Loan Officer); every other role — including applications:view
    holders (Credit Committee, Auditor) — is refused AT THE ROUTE (the
    email-match self-service is retired; members act on /member).
    substitute: applications:edit only. consent: the staff path is the
    attested override under member_identity:approve — System Admin and
    Branch Manager ONLY (deny by default; falsifiable: route consent
    through applications:edit and the Loan Officer denial fails).
    """

    consent_override_expectation = {
        "System Admin": 200,
        "Branch Manager": 200,
        "Loan Officer": 403,
        "Credit Committee": 403,
        "Teller": 403,
        "Accountant": 403,
        "Auditor": 403,
    }
    release_expectation = {
        "System Admin": 200,
        "Branch Manager": 200,
        "Loan Officer": 200,
        "Credit Committee": 403,
        "Teller": 403,
        "Accountant": 403,
        "Auditor": 403,
    }
    substitute_expectation = {
        "System Admin": 201,
        "Branch Manager": 201,
        "Loan Officer": 201,
        "Credit Committee": 403,
        "Teller": 403,
        "Accountant": 403,
        "Auditor": 403,
    }

    async def run() -> None:
        tid, _, _, _ = await _seed_actor()
        pid = await _seed_product(tid)
        async with api_client() as client:
            for role, expected in consent_override_expectation.items():
                borrower = await _seed_member(tid)
                guarantor = await _seed_member(tid, deposit="10000.00")
                aid = await _seed_application(
                    tid, borrower, pid, amount="1000.00", stage="submitted"
                )
                gid = await _seed_guarantee(
                    tid,
                    guarantor=guarantor,
                    borrower=borrower,
                    amount="500.00",
                    status="pledged",
                    application_id=aid,
                )
                _, _, token = await _add_user(tid, role)
                res = await client.post(
                    f"/guarantees/{gid}/consent",
                    json={"version": 1, "consent_reference": _CONSENT_REF},
                    headers=_headers(token),
                )
                assert res.status_code == expected, f"consent override as {role}: {res.text}"

            for role, expected in release_expectation.items():
                borrower = await _seed_member(tid)
                guarantor = await _seed_member(tid, deposit="10000.00")
                aid = await _seed_application(
                    tid, borrower, pid, amount="1000.00", stage="submitted"
                )
                gid = await _seed_guarantee(
                    tid,
                    guarantor=guarantor,
                    borrower=borrower,
                    amount="500.00",
                    status="pledged",
                    application_id=aid,
                )
                _, _, token = await _add_user(tid, role)
                res = await client.post(
                    f"/guarantees/{gid}/release",
                    json={"version": 1},
                    headers=_headers(token),
                )
                assert res.status_code == expected, f"release as {role}: {res.text}"

            for role, expected in substitute_expectation.items():
                borrower = await _seed_member(tid)
                guarantor = await _seed_member(tid, deposit="10000.00")
                new_g = await _seed_member(tid, deposit="10000.00")
                aid = await _seed_application(
                    tid, borrower, pid, amount="1000.00", stage="disbursed"
                )
                loan_id = await _seed_loan(tid, borrower, pid, aid, balance="1000.00")
                gid = await _seed_guarantee(
                    tid,
                    guarantor=guarantor,
                    borrower=borrower,
                    amount="1000.00",
                    application_id=aid,
                    loan_id=loan_id,
                )
                _, _, token = await _add_user(tid, role)
                res = await client.post(
                    f"/guarantees/{gid}/substitute",
                    json={
                        "version": 1,
                        "guarantor_member_id": str(new_g),
                        "consent_reference": _CONSENT_REF,
                    },
                    headers=_headers(token),
                )
                assert res.status_code == expected, f"substitute as {role}: {res.text}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 8 — tenant scoping (issue-#17 falsifiability probe)
# ---------------------------------------------------------------------------


def test_release_and_substitute_refuse_foreign_tenant_argument() -> None:
    """Session opened AS the row's own tenant (RLS passes, so it cannot
    mask a missing predicate); the service is called with a FOREIGN
    tenant argument. Only the explicit bound tenant predicates can
    refuse the row -> zero rows, zero mutations."""

    async def run() -> None:
        tid, uid, _role_id, _ = await _seed_actor()
        foreign = uuid.uuid4()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="1000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="1000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await guarantees_service.release_guarantee(session, foreign, uid, gid, version=1)
            with pytest.raises(NotFoundError):
                await guarantees_service.substitute_guarantee(
                    session,
                    foreign,
                    uid,
                    gid,
                    version=1,
                    guarantor_member_id=new_g,
                    consent_reference=_CONSENT_REF,
                )
        assert await _guarantee_state(tid, gid) == ("active", 1)
        assert await _release_side_effects(tid, gid) == (0, 0)
        assert await _substitution_side_effects(tid, gid) == (0, 0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Failure mode 9 — exit unblocked after release/substitution (end-to-end)
# ---------------------------------------------------------------------------


async def _exit_member_via_workflow(
    tid: uuid.UUID, admin_headers: dict[str, str], member_id: uuid.UUID
) -> None:
    """Drive the REAL P12 workflow: request -> quorum votes -> settle."""
    _, _, voter1 = await _add_user(tid, "System Admin")
    _, _, voter2 = await _add_user(tid, "Branch Manager")
    async with api_client() as client:
        created = await client.post(
            "/member-exits", json={"member_id": str(member_id)}, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        exit_id = created.json()["id"]
        for token in (voter1, voter2):
            vote = await client.post(
                f"/member-exits/{exit_id}/votes",
                json={"vote": "approve"},
                headers=_headers(token),
            )
            assert vote.status_code == 201, vote.text
        settled = await client.post(
            f"/member-exits/{exit_id}/settlement",
            json={"version": 2, "channel": "bank"},
            headers=_headers(voter1),
        )
        assert settled.status_code == 201, settled.text


async def _member_status(tid: uuid.UUID, mid: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        val = (
            await session.execute(
                text("SELECT status FROM members WHERE id = CAST(:m AS uuid)"),
                {"m": str(mid)},
            )
        ).scalar_one()
    return str(val)


def test_exit_unblocked_after_release_end_to_end() -> None:
    """EXIT criterion: guarantor blocked by an ACTIVE guarantee given ->
    staff release it (cover rule holds) -> the full P12 workflow exits
    the member. Hand-computed cover for the release: borrower deposits
    2000 x 3 = 6000 >= amount 3000 with zero guarantees left."""

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        admin = _headers(token)
        borrower = await _seed_member(tid, deposit="2000.00")
        guarantor = await _seed_member(tid, deposit="7500.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="3000.00", stage="approved")
        gid = await _seed_guarantee(
            tid, guarantor=guarantor, borrower=borrower, amount="4000.00", application_id=aid
        )
        async with api_client() as client:
            blocked = await client.post(
                "/member-exits", json={"member_id": str(guarantor)}, headers=admin
            )
            assert blocked.status_code == 409, blocked.text

            released = await client.post(
                f"/guarantees/{gid}/release", json={"version": 1}, headers=admin
            )
            assert released.status_code == 200, released.text

        await _exit_member_via_workflow(tid, admin, guarantor)
        assert await _member_status(tid, guarantor) == "exited"

    asyncio.run(run())


def test_exit_unblocked_after_substitution_end_to_end() -> None:
    """EXIT criterion, substitution flavour: the old guarantor of a
    DISBURSED loan (bare release impossible) becomes exitable once a
    substitute takes over, end-to-end through the real P12 workflow."""

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        admin = _headers(token)
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="5000.00")
        new_g = await _seed_member(tid, deposit="9000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="2000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="2000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="2000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            blocked = await client.post(
                "/member-exits", json={"member_id": str(old_g)}, headers=admin
            )
            assert blocked.status_code == 409, blocked.text

            bare = await client.post(
                f"/guarantees/{gid}/release", json={"version": 1}, headers=admin
            )
            assert bare.status_code == 409, bare.text  # failure mode 2

            swapped = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": _CONSENT_REF,
                },
                headers=admin,
            )
            assert swapped.status_code == 201, swapped.text

        await _exit_member_via_workflow(tid, admin, old_g)
        assert await _member_status(tid, old_g) == "exited"
        # The loan's collateral survived the guarantor's exit:
        # hand-computed live cover on the loan stays 2000.
        async with tenant_session(factory(), tid) as session:
            live = (
                await session.execute(
                    text(
                        "SELECT COALESCE(SUM(amount), 0) FROM guarantees "
                        "WHERE loan_id = CAST(:l AS uuid) AND status = 'active'"
                    ),
                    {"l": str(loan_id)},
                )
            ).scalar_one()
        assert Decimal(str(live)) == Decimal("2000.00")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Review R1 — consent attestation is a first-class audited fact
# ---------------------------------------------------------------------------


def test_substitution_consent_attestation_is_first_class_audited_fact() -> None:
    """Review R1 (consent integrity): substitution consent is the
    explicit staff-attested override (P14.5), so it is staff-
    attested — and the attestation must be a recorded fact, not a bare
    boolean (P14.5: the boolean no longer exists). Hand-computed
    oracle: ONE guarantee.consent_override audit row on
    the replacement naming the attesting actor and the cited evidence,
    plus ONE guarantee.consented outbox confirmation addressed to the
    substitute guarantor (the detection control for a conscripted
    member). Falsifiable: remove either write in substitute_guarantee
    and the corresponding count drops to zero."""

    async def run() -> None:
        tid, uid, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="8000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        loan_id = await _seed_loan(tid, borrower, pid, aid, balance="5000.00")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=loan_id,
        )
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            replacement_id = res.json()["replacement"]["id"]

        async with tenant_session(factory(), tid) as session:
            consent_audits = (
                await session.execute(
                    text(
                        "SELECT actor_id, after->>'attested_by', "
                        "after->>'consent_reference', after->>'replaces' "
                        "FROM audit_log WHERE action = 'guarantee.consent_override' "
                        "AND entity_id = :rid"
                    ),
                    {"rid": replacement_id},
                )
            ).all()
            assert len(consent_audits) == 1
            row = consent_audits[0]
            # WHO attested, on WHAT basis, replacing WHICH guarantee.
            assert str(row[0]) == str(uid)
            assert str(row[1]) == str(uid)
            assert str(row[2]) == _CONSENT_REF
            assert str(row[3]) == str(gid)

            confirmations = (
                await session.execute(
                    text(
                        "SELECT payload->>'notify_member_id', "
                        "payload->>'consent_reference' FROM outbox_events "
                        "WHERE event_type = 'guarantee.consented' "
                        "AND payload->>'guarantee_id' = :rid"
                    ),
                    {"rid": replacement_id},
                )
            ).all()
            assert len(confirmations) == 1
            # The confirmation goes to the SUBSTITUTE guarantor.
            assert str(confirmations[0][0]) == str(new_g)
            assert str(confirmations[0][1]) == _CONSENT_REF

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Review R2 — only ACTIVE members may be substitute guarantors
# ---------------------------------------------------------------------------


def test_non_active_member_cannot_be_substitute_guarantor() -> None:
    """Review R2: the substitution path must enforce the P9 pledge rule
    that only ACTIVE members pledge. Hand-computed: all candidates hold
    deposit 10000 >= replacement 800, so capacity can never be the
    refusal — only the member-status guard inside
    _guarantor_available_capacity can refuse. Falsifiable: remove the
    status check under the member FOR SHARE lock and every swap lands,
    failing every assertion below. ('dormant' landed with P13.13: the
    guard is the code-owned capability map, PLEDGE = active-only, so
    dormant is refused by construction and pinned here.)"""

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        pid = await _seed_product(tid)
        for bad_status in ("exited", "arrears", "dormant"):
            borrower = await _seed_member(tid)
            old_g = await _seed_member(tid, deposit="2000.00")
            bad_sub = await _seed_member(tid, deposit="10000.00", status=bad_status)
            aid = await _seed_application(tid, borrower, pid, amount="800.00", stage="disbursed")
            loan_id = await _seed_loan(tid, borrower, pid, aid, balance="800.00")
            gid = await _seed_guarantee(
                tid,
                guarantor=old_g,
                borrower=borrower,
                amount="800.00",
                application_id=aid,
                loan_id=loan_id,
            )
            async with api_client() as client:
                res = await client.post(
                    f"/guarantees/{gid}/substitute",
                    json={
                        "version": 1,
                        "guarantor_member_id": str(bad_sub),
                        "consent_reference": _CONSENT_REF,
                    },
                    headers=_headers(token),
                )
            assert res.status_code == 409, f"{bad_status}: {res.text}"
            assert set(res.json()) == {"category", "correlation_id"}
            # The release leg rolled back with the refusal: zero
            # mutations, no replacement row.
            assert await _guarantee_state(tid, gid) == ("active", 1)
            assert await _substitution_side_effects(tid, gid) == (0, 0)
            async with tenant_session(factory(), tid) as session:
                count = (
                    await session.execute(
                        text("SELECT count(*) FROM guarantees WHERE loan_id = CAST(:l AS uuid)"),
                        {"l": str(loan_id)},
                    )
                ).scalar_one()
            assert int(count) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Review R3 — RETIRED by P14.5
# ---------------------------------------------------------------------------
# The R3 suite (byte-exact users.email <-> members.email matching, the
# email-rewriters matrix walk) defended the INTERIM identity bridge
# that P14.5 removes: no code path joins users.email to members.email
# any more, so there is no byte-exactness or rewriter-privilege
# property left to pin. Its threat (identity spoof via email rewrite)
# is covered STRONGER by the FM2 suite in tests/test_member_identity.py
# — rewriting either email provably redirects nothing because the
# member_credentials LINK row is the only authority.

# ---------------------------------------------------------------------------
# Review R4 — Idempotency-Key replay is scoped to (tenant, actor, route)
# ---------------------------------------------------------------------------


def test_idempotency_replay_scoped_to_actor_and_tenant() -> None:
    """Review R4, completed by P14.5 FM5: the claim key is scoped
    (tenant, actor principal, route), so a DIFFERENT user presenting
    the same client key is a MISS — their request claims its OWN row
    and hits the REAL handler (here: 409 stale version, because the
    release already landed), never the first caller's stored 200. A
    different tenant likewise claims its own row (404 for the foreign
    id). Falsifiable: drop the actor from scoped_storage_key and the
    second user receives the first caller's 200 replay, failing the
    replayed-header and claim-row-count assertions."""

    async def run() -> None:
        tid, _, _, token_a = await _seed_actor()
        borrower = await _seed_member(tid)
        guarantor = await _seed_member(tid, deposit="10000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="1000.00", stage="submitted")
        gid = await _seed_guarantee(
            tid,
            guarantor=guarantor,
            borrower=borrower,
            amount="500.00",
            status="pledged",
            application_id=aid,
        )
        _, _, token_b = await _add_user(tid, "System Admin")
        foreign_tid, _, _, token_c = await _seed_actor()
        assert foreign_tid != tid

        key = f"p1314-scope-{uuid.uuid4().hex[:10]}"
        body = {"version": 1}
        async with api_client() as client:
            first = await client.post(
                f"/guarantees/{gid}/release",
                json=body,
                headers={**_headers(token_a), "idempotency-key": key},
            )
            assert first.status_code == 200, first.text

            # Same tenant, DIFFERENT user, same key/path/body: a MISS —
            # the REAL handler runs under user B's own claim and
            # reports the true state (stale version 1 -> 409); user A's
            # stored 200 is never served.
            other_user = await client.post(
                f"/guarantees/{gid}/release",
                json=body,
                headers={**_headers(token_b), "idempotency-key": key},
            )
            assert other_user.status_code == 409, other_user.text
            assert other_user.headers.get("idempotency-replayed") is None
            assert set(other_user.json()) == {"category", "correlation_id"}

            # Different tenant, same key: its own claim, the real
            # handler, and a 404 for the foreign guarantee id.
            other_tenant = await client.post(
                f"/guarantees/{gid}/release",
                json=body,
                headers={**_headers(token_c), "idempotency-key": key},
            )
            assert other_tenant.status_code == 404, other_tenant.text
            assert other_tenant.headers.get("idempotency-replayed") is None

            # The original caller still replays cleanly.
            replay = await client.post(
                f"/guarantees/{gid}/release",
                json=body,
                headers={**_headers(token_a), "idempotency-key": key},
            )
            assert replay.status_code == 200
            assert replay.headers.get("idempotency-replayed") == "true"
            assert replay.json() == first.json()

        # The MISS is structural: each same-tenant actor claimed a
        # DISTINCT scoped row for the one client key (A's and B's).
        async with tenant_session(factory(), tid) as session:
            claims = (
                await session.execute(
                    text("SELECT count(*) FROM idempotency_keys WHERE key LIKE :suffix"),
                    {"suffix": f"%:{key}"},
                )
            ).scalar_one()
        assert int(claims) == 2, "each actor must claim its own scoped row"
        # Exactly one release happened (side-effect counts, not return
        # values): 1 audit row, 2 notifications, one version bump.
        assert await _guarantee_state(tid, gid) == ("released", 2)
        assert await _release_side_effects(tid, gid) == (1, 2)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Review R7 — stage-only DISBURSED rows are substitution-only
# ---------------------------------------------------------------------------


def test_stage_disbursed_without_loan_id_is_substitution_only() -> None:
    """Review R7: pre-P12.5 data shape — the application reached
    DISBURSED but the guarantee row carries loan_id NULL. The disbursed
    determination (loan_id IS NOT NULL OR stage = DISBURSED) must catch
    the stage-only path: bare release refused (409), substitution the
    only exit. Falsifiable: drop the `or stage is DISBURSED` branch in
    release_guarantee and the bare release lands (DISBURSED is not a
    cover-guarded stage), failing this test."""

    async def run() -> None:
        tid, _, _, token = await _seed_actor()
        borrower = await _seed_member(tid)
        old_g = await _seed_member(tid, deposit="10000.00")
        new_g = await _seed_member(tid, deposit="8000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="5000.00", stage="disbursed")
        gid = await _seed_guarantee(
            tid,
            guarantor=old_g,
            borrower=borrower,
            amount="5000.00",
            application_id=aid,
            loan_id=None,
        )
        async with api_client() as client:
            bare = await client.post(
                f"/guarantees/{gid}/release",
                json={"version": 1},
                headers=_headers(token),
            )
            assert bare.status_code == 409, bare.text
            assert set(bare.json()) == {"category", "correlation_id"}
            assert await _guarantee_state(tid, gid) == ("active", 1)
            assert await _release_side_effects(tid, gid) == (0, 0)

            swapped = await client.post(
                f"/guarantees/{gid}/substitute",
                json={
                    "version": 1,
                    "guarantor_member_id": str(new_g),
                    "consent_reference": _CONSENT_REF,
                },
                headers=_headers(token),
            )
            assert swapped.status_code == 201, swapped.text
            replacement = swapped.json()["replacement"]
            assert replacement["application_id"] == str(aid)
            assert replacement["loan_id"] is None
        assert await _guarantee_state(tid, gid) == ("released", 2)
        assert await _substitution_side_effects(tid, gid) == (1, 3)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Service-level validation: consent flag checked before any read
# ---------------------------------------------------------------------------


def test_unconsented_substitute_refused_at_service_level() -> None:
    """P14.5: with no consent boolean left to assert, the ONLY consent
    channel is the cited attestation — a blank consent_reference is
    refused before any read or write (the FM4 posture at the service
    boundary)."""

    async def run() -> None:
        tid, uid, _, _ = await _seed_actor()
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(UnprocessableError):
                await guarantees_service.substitute_guarantee(
                    session,
                    tid,
                    uid,
                    uuid.uuid4(),
                    version=1,
                    guarantor_member_id=uuid.uuid4(),
                    consent_reference="   ",
                )

    asyncio.run(run())
