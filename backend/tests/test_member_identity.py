"""P14.5 member identity suite — FM2 (link authority), FM3 (link
takeover), FM4 (consent forgery) against the real migrated Postgres.

Falsifiability, per the MR contract:

  * FM2 — re-introduce ANY email join between the credential and the
    member (or between users and members) and the spoofed
    consent/release below LANDS, failing the state assertions;
  * FM3 — drop the route gate or the atomic ON CONFLICT claim and the
    self-service/concurrent-claim tests fail;
  * FM4 — drop the 0035 guarantee_consent_requires_principal trigger
    and the raw principal-less UPDATE lands, failing the
    check-violation assertion.

All oracles are hand-computed in comments; effects are asserted by
side-effect row state, never return values alone.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import (
    AuthContext,
    MemberAuthContext,
    issue_access_token,
    issue_member_access_token,
)
from genesis.application.rbac import seed_permissions
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Fixtures (the test_guarantee_release shapes, kept small)
# ---------------------------------------------------------------------------


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
    """Tenant + permissions + one staff user; returns (tid, user_id, token)."""
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


async def _add_staff(tid: uuid.UUID, role_name: str) -> str:
    """Extra staff user in an existing tenant; returns their token."""
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
                "'Identity Staff', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
        rid = uuid.UUID(str(role_id))
    return issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=rid))


async def _seed_member(
    tid: uuid.UUID, *, email: str | None = None, status: str = "active", deposit: str = "0"
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, email, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                "'Identity Member', :email, :st)"
            ),
            {
                "id": str(mid),
                "tid": str(tid),
                "no": f"MI-{mid.hex[:6].upper()}",
                "email": email,
                "st": status,
            },
        )
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": deposit},
        )
    return mid


async def _link_credential(tid: uuid.UUID, member_id: uuid.UUID, *, email: str) -> uuid.UUID:
    cid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO member_credentials (id, tenant_id, member_id, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :email)"
            ),
            {"id": str(cid), "tid": str(tid), "m": str(member_id), "email": email},
        )
    return cid


def _member_headers(tid: uuid.UUID, cid: uuid.UUID, mid: uuid.UUID) -> dict[str, str]:
    token = issue_member_access_token(
        MemberAuthContext(credential_id=cid, member_id=mid, tenant_id=tid)
    )
    return {"authorization": f"Bearer {token}"}


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_pledged_guarantee(
    tid: uuid.UUID, *, guarantor: uuid.UUID, borrower: uuid.UUID
) -> uuid.UUID:
    """A PLEDGED guarantee on a submitted application (consentable)."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    gid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"P145-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '1000.00', 12, '12.00', 'submitted', '0.00')"
            ),
            {"id": str(aid), "tid": str(tid), "mid": str(borrower), "pid": str(pid)},
        )
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                " application_id, amount, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), CAST(:a AS uuid), '500.00', 'pledged')"
            ),
            {
                "id": str(gid),
                "tid": str(tid),
                "g": str(guarantor),
                "b": str(borrower),
                "a": str(aid),
            },
        )
    return gid


async def _guarantee_row(tid: uuid.UUID, gid: uuid.UUID) -> tuple[str, int, str | None, str | None]:
    """(status, version, consented_by_credential_id, consent_attested_by)."""
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, version, consented_by_credential_id, consent_attested_by "
                    "FROM guarantees WHERE id = CAST(:g AS uuid)"
                ),
                {"g": str(gid)},
            )
        ).first()
    assert row is not None
    return (
        str(row[0]),
        int(row[1]),
        str(row[2]) if row[2] is not None else None,
        str(row[3]) if row[3] is not None else None,
    )


async def _active_credentials(tid: uuid.UUID, *, email: str) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM member_credentials "
                    "WHERE email = :email AND status = 'active'"
                ),
                {"email": email},
            )
        ).scalar_one()
    return int(count)


# ---------------------------------------------------------------------------
# FM2 — the LINK, never any email, is authoritative
# ---------------------------------------------------------------------------


def test_fm2_email_rewrite_never_redirects_the_link() -> None:
    """The !29 attack, replayed against the new identity: an attacker
    who rewrites EVERY email column they can reach (their member row,
    the guarantor's member row) still cannot consent to or release the
    guarantor's pledge — their credential row links to THEM, and the
    link is re-verified inside the transaction. Meanwhile the true
    guarantor keeps acting despite their member email being vandalised:
    the link decides POSITIVELY too. Falsifiable: re-introduce the
    email join and the attacker's consent lands (state flips to
    active, failing the assertions)."""

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        g_email = unique_email()
        guarantor = await _seed_member(tid, email=g_email, deposit="10000.00")
        borrower = await _seed_member(tid)
        attacker = await _seed_member(tid, email=unique_email(), deposit="10000.00")
        g_cred = await _link_credential(tid, guarantor, email=g_email)
        a_cred = await _link_credential(tid, attacker, email=unique_email())
        gid = await _seed_pledged_guarantee(tid, guarantor=guarantor, borrower=borrower)

        # The attacker rewrites every reachable email to the guarantor's.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text("UPDATE members SET email = :e WHERE id = CAST(:m AS uuid)"),
                {"e": g_email, "m": str(attacker)},
            )
            await session.execute(
                text("UPDATE members SET email = :e WHERE id = CAST(:m AS uuid)"),
                {"e": unique_email(), "m": str(guarantor)},
            )

        async with api_client() as client:
            for path in (f"/member/guarantees/{gid}/consent", f"/member/guarantees/{gid}/release"):
                res = await client.post(
                    path, json={"version": 1}, headers=_member_headers(tid, a_cred, attacker)
                )
                assert res.status_code == 403, f"{path}: {res.text}"
                assert set(res.json()) == {"category", "correlation_id"}
            assert await _guarantee_row(tid, gid) == ("pledged", 1, None, None)

            # The true guarantor consents fine — their member email no
            # longer matches anything, and it does not need to.
            res = await client.post(
                f"/member/guarantees/{gid}/consent",
                json={"version": 1},
                headers=_member_headers(tid, g_cred, guarantor),
            )
            assert res.status_code == 200, res.text
        status, version, consented_by, attested_by = await _guarantee_row(tid, gid)
        assert (status, version) == ("active", 2)
        # FM4 positive: the consent row carries the MEMBER principal.
        assert consented_by == str(g_cred)
        assert attested_by is None

        # The audit actor is the acting CREDENTIAL under the dedicated
        # member-consent category (never consent_override).
        async with tenant_session(factory(), tid) as session:
            actor = (
                await session.execute(
                    text(
                        "SELECT actor_id FROM audit_log "
                        "WHERE action = 'guarantee.consent' AND entity_id = :gid"
                    ),
                    {"gid": str(gid)},
                )
            ).scalar_one()
        assert uuid.UUID(str(actor)) == g_cred

    asyncio.run(run())


def test_fm2_repointed_credential_acts_for_the_new_member_only() -> None:
    """Link takeover happens through revoke + create (FM3); the moment
    it commits, a token minted for the OLD link dies (the credential id
    in the token no longer maps to a live row) — within one request."""

    async def run() -> None:
        tid, _, staff_token = await _seed_actor()
        member_a = await _seed_member(tid)
        email = unique_email()
        async with api_client() as client:
            created = await client.post(
                f"/members/{member_a}/credentials",
                json={"email": email},
                headers=_headers(staff_token),
            )
            assert created.status_code == 201, created.text
            old_cred = uuid.UUID(created.json()["id"])

            # Old-link token minted BEFORE the re-link.
            stale_headers = _member_headers(tid, old_cred, member_a)

            revoked = await client.post(
                f"/credentials/{old_cred}/revoke",
                json={"version": 1},
                headers=_headers(staff_token),
            )
            assert revoked.status_code == 200, revoked.text

            member_b = await _seed_member(tid)
            recreated = await client.post(
                f"/members/{member_b}/credentials",
                json={"email": email},
                headers=_headers(staff_token),
            )
            assert recreated.status_code == 201, recreated.text
            assert recreated.json()["member_id"] == str(member_b)

            # The stale principal is dead at the gate (401 — session
            # over, not under-privileged).
            res = await client.post(
                f"/member/guarantees/{uuid.uuid4()}/release",
                json={"version": 1},
                headers=stale_headers,
            )
            assert res.status_code == 401, res.text
        assert await _active_credentials(tid, email=email) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — link takeover requires the audited admin mutation
# ---------------------------------------------------------------------------


def test_fm3_link_mutations_are_admin_only_per_role() -> None:
    """The narrow matrix at the route: System Admin / Branch Manager
    mutate, the Auditor views only, every other role holds NOTHING —
    and no member principal can touch the admin surface at all
    (asserted in test_member_auth FM1). Falsifiable: route these
    through members:edit and the Loan Officer denial fails."""

    create_expectation = {
        "System Admin": 201,
        "Branch Manager": 201,
        "Loan Officer": 403,
        "Teller": 403,
        "Credit Committee": 403,
        "Accountant": 403,
        "Auditor": 403,
    }

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        async with api_client() as client:
            for role, expected in create_expectation.items():
                mid = await _seed_member(tid)
                token = await _add_staff(tid, role)
                res = await client.post(
                    f"/members/{mid}/credentials",
                    json={"email": unique_email()},
                    headers=_headers(token),
                )
                assert res.status_code == expected, f"create as {role}: {res.text}"

            # View: the Auditor reads history; the Teller reads nothing
            # (not even under their members-adjacent view grant).
            mid = await _seed_member(tid)
            await _link_credential(tid, mid, email=unique_email())
            auditor = await _add_staff(tid, "Auditor")
            teller = await _add_staff(tid, "Teller")
            ok = await client.get(f"/members/{mid}/credentials", headers=_headers(auditor))
            assert ok.status_code == 200, ok.text
            assert len(ok.json()) == 1
            denied = await client.get(f"/members/{mid}/credentials", headers=_headers(teller))
            assert denied.status_code == 403, denied.text

            # And the Auditor's view grant never implies revoke.
            cred_id = ok.json()[0]["id"]
            refused = await client.post(
                f"/credentials/{cred_id}/revoke",
                json={"version": 1},
                headers=_headers(auditor),
            )
            assert refused.status_code == 403, refused.text

    asyncio.run(run())


def test_fm3_concurrent_email_claim_lands_exactly_one_active_row() -> None:
    """The atomic ON CONFLICT claim (v1.1 rule 5): two concurrent
    links of ONE email to two different members land exactly one
    active row; the loser gets the least-disclosure 409 that never
    names the member holding the email. Falsifiable: replace the
    INSERT..ON CONFLICT+rowcount with check-then-insert and both
    concurrent claims land (2 active rows)."""

    async def run() -> None:
        tid, _, staff_token = await _seed_actor()
        member_a = await _seed_member(tid)
        member_b = await _seed_member(tid)
        email = unique_email()
        async with api_client() as client:
            results = await asyncio.gather(
                client.post(
                    f"/members/{member_a}/credentials",
                    json={"email": email},
                    headers=_headers(staff_token),
                ),
                client.post(
                    f"/members/{member_b}/credentials",
                    json={"email": email},
                    headers=_headers(staff_token),
                ),
            )
        statuses = sorted(r.status_code for r in results)
        assert statuses == [201, 409], statuses
        loser = next(r for r in results if r.status_code == 409)
        # Least disclosure: envelope only — never whose email it is.
        assert set(loser.json()) == {"category", "correlation_id"}
        assert await _active_credentials(tid, email=email) == 1

    asyncio.run(run())


def test_fm3_one_active_credential_per_member_and_relink_lifecycle() -> None:
    """One active link per member (second create → 409); re-linking is
    revoke + create — two audited mutations, each with its member
    notification; a stale revoke version is a 409 and an unknown
    credential a 404; revoked history stays readable (forensic)."""

    async def run() -> None:
        tid, staff_id, staff_token = await _seed_actor()
        mid = await _seed_member(tid)
        async with api_client() as client:
            first = await client.post(
                f"/members/{mid}/credentials",
                json={"email": unique_email()},
                headers=_headers(staff_token),
            )
            assert first.status_code == 201, first.text
            cred_id = first.json()["id"]

            second = await client.post(
                f"/members/{mid}/credentials",
                json={"email": unique_email()},
                headers=_headers(staff_token),
            )
            assert second.status_code == 409, "one active credential per member"

            stale = await client.post(
                f"/credentials/{cred_id}/revoke",
                json={"version": 99},
                headers=_headers(staff_token),
            )
            assert stale.status_code == 409, stale.text

            missing = await client.post(
                f"/credentials/{uuid.uuid4()}/revoke",
                json={"version": 1},
                headers=_headers(staff_token),
            )
            assert missing.status_code == 404, missing.text

            revoked = await client.post(
                f"/credentials/{cred_id}/revoke",
                json={"version": 1},
                headers=_headers(staff_token),
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["status"] == "revoked"

            relinked = await client.post(
                f"/members/{mid}/credentials",
                json={"email": unique_email()},
                headers=_headers(staff_token),
            )
            assert relinked.status_code == 201, relinked.text

            history = await client.get(f"/members/{mid}/credentials", headers=_headers(staff_token))
            assert history.status_code == 200
            assert sorted(c["status"] for c in history.json()) == ["active", "revoked"]

        async with tenant_session(factory(), tid) as session:
            audits = (
                await session.execute(
                    text(
                        "SELECT action, actor_id FROM audit_log "
                        "WHERE entity = 'member_credentials' ORDER BY id"
                    )
                )
            ).all()
            events = (
                await session.execute(
                    text(
                        "SELECT event_type, payload->>'notify_member_id' FROM outbox_events "
                        "WHERE event_type LIKE 'member_identity.%'"
                    )
                )
            ).all()
        # 2 creates + 1 revoke, every one attributed to the staff actor
        # (FM3: audited admin mutations, never self-service).
        actions = [str(a[0]) for a in audits]
        assert actions == [
            "member_credential.create",
            "member_credential.revoke",
            "member_credential.create",
        ]
        assert all(uuid.UUID(str(a[1])) == staff_id for a in audits)
        # Detection control: every mutation notifies the member
        # (counted, not ordered — outbox ids are UUIDs).
        assert sorted(str(e[0]) for e in events) == [
            "member_identity.credential_created",
            "member_identity.credential_created",
            "member_identity.credential_revoked",
        ]
        assert all(str(e[1]) == str(mid) for e in events)

    asyncio.run(run())


def test_fm3_no_link_for_exited_member_or_foreign_tenant() -> None:
    """An exited member has no live relationship to authenticate (409);
    a foreign tenant's member id is a 404 through RLS + the explicit
    predicate (issue-#17 posture: the row simply does not exist here)."""

    async def run() -> None:
        tid, _, staff_token = await _seed_actor()
        exited = await _seed_member(tid, status="exited")
        foreign_tid, _ = await seed_user(unique_email())
        foreign_member = await _seed_member(foreign_tid)
        async with api_client() as client:
            res = await client.post(
                f"/members/{exited}/credentials",
                json={"email": unique_email()},
                headers=_headers(staff_token),
            )
            assert res.status_code == 409, res.text
            res = await client.post(
                f"/members/{foreign_member}/credentials",
                json={"email": unique_email()},
                headers=_headers(staff_token),
            )
            assert res.status_code == 404, res.text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — consent forgery fails AT THE DATABASE
# ---------------------------------------------------------------------------


def test_fm4_consent_without_principal_fails_db_constraint() -> None:
    """The 0035 trigger is the last line: a raw UPDATE flipping a
    pledged guarantee to active WITHOUT a member principal or a staff
    attestation is refused by the database itself — the service layer
    never gets a vote. Falsifiable: drop the trigger and the raw
    UPDATE lands (status flips), failing this test."""

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00")
        borrower = await _seed_member(tid)
        gid = await _seed_pledged_guarantee(tid, guarantor=guarantor, borrower=borrower)

        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("UPDATE guarantees SET status = 'active' WHERE id = CAST(:g AS uuid)"),
                    {"g": str(gid)},
                )
        assert (await _guarantee_row(tid, gid))[0] == "pledged"

        # An INSERT born active without a principal dies the same way.
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO guarantees "
                        "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                        " amount, status) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                        "CAST(:g AS uuid), CAST(:b AS uuid), '100.00', 'active')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "g": str(guarantor),
                        "b": str(borrower),
                    },
                )

        # And a staff attestation without cited evidence is
        # unrepresentable (ck_guarantees_attested_reference).
        async with tenant_session(factory(), tid) as session:
            attestor = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE guarantees SET status = 'active', "
                        "consent_attested_by = CAST(:att AS uuid) "
                        "WHERE id = CAST(:g AS uuid)"
                    ),
                    {"att": str(attestor), "g": str(gid)},
                )
        assert (await _guarantee_row(tid, gid))[0] == "pledged"

    asyncio.run(run())


def test_fm4_member_consent_and_staff_override_each_carry_their_principal() -> None:
    """The two legitimate consent shapes, side by side: the member
    principal stamps consented_by_credential_id; the staff override
    stamps consent_attested_by + the cited evidence and lands under its
    OWN audit category (guarantee.consent_override) with the guarantor
    notified — never a silent staff consent."""

    async def run() -> None:
        tid, staff_id, staff_token = await _seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00")
        borrower = await _seed_member(tid)
        g_cred = await _link_credential(tid, guarantor, email=unique_email())
        member_gid = await _seed_pledged_guarantee(tid, guarantor=guarantor, borrower=borrower)
        override_gid = await _seed_pledged_guarantee(tid, guarantor=guarantor, borrower=borrower)

        async with api_client() as client:
            res = await client.post(
                f"/member/guarantees/{member_gid}/consent",
                json={"version": 1},
                headers=_member_headers(tid, g_cred, guarantor),
            )
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "active"

            # The override demands its evidence: a blank citation is 422.
            blank = await client.post(
                f"/guarantees/{override_gid}/consent",
                json={"version": 1, "consent_reference": "   "},
                headers=_headers(staff_token),
            )
            assert blank.status_code == 422, blank.text

            res = await client.post(
                f"/guarantees/{override_gid}/consent",
                json={"version": 1, "consent_reference": "signed form GF-145"},
                headers=_headers(staff_token),
            )
            assert res.status_code == 200, res.text

        _, _, consented_by, attested_by = await _guarantee_row(tid, member_gid)
        assert consented_by == str(g_cred) and attested_by is None
        _, _, consented_by, attested_by = await _guarantee_row(tid, override_gid)
        assert consented_by is None and attested_by == str(staff_id)

        async with tenant_session(factory(), tid) as session:
            override_audit = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_log "
                        "WHERE action = 'guarantee.consent_override' AND entity_id = :gid"
                    ),
                    {"gid": str(override_gid)},
                )
            ).scalar_one()
            notified = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events "
                        "WHERE event_type = 'guarantee.consented' "
                        "AND payload->>'guarantee_id' = :gid "
                        "AND payload->>'notify_member_id' = :mid"
                    ),
                    {"gid": str(override_gid), "mid": str(guarantor)},
                )
            ).scalar_one()
        assert int(override_audit) == 1
        assert int(notified) == 1, "the attested-over guarantor must be notified"

    asyncio.run(run())


def test_member_consent_wrong_principal_shapes_get_one_refusal() -> None:
    """Least disclosure at the consent core: someone else's guarantee,
    a dead link and an already-active guarantee are indistinguishable
    to the wrong principal — a single 403/409 shape, never a hint of
    whose collateral it is."""

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00")
        borrower = await _seed_member(tid)
        other = await _seed_member(tid, deposit="10000.00")
        other_cred = await _link_credential(tid, other, email=unique_email())
        g_cred = await _link_credential(tid, guarantor, email=unique_email())
        gid = await _seed_pledged_guarantee(tid, guarantor=guarantor, borrower=borrower)

        async with api_client() as client:
            # Someone else's pledge.
            res = await client.post(
                f"/member/guarantees/{gid}/consent",
                json={"version": 1},
                headers=_member_headers(tid, other_cred, other),
            )
            assert res.status_code == 403
            assert set(res.json()) == {"category", "correlation_id"}
            assert (await _guarantee_row(tid, gid))[0] == "pledged"

            # Dead link (revoked between token issue and use).
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_credentials SET status = 'revoked', revoked_at = now() "
                        "WHERE id = CAST(:c AS uuid)"
                    ),
                    {"c": str(other_cred)},
                )
            res = await client.post(
                f"/member/guarantees/{gid}/consent",
                json={"version": 1},
                headers=_member_headers(tid, other_cred, other),
            )
            assert res.status_code == 401, "dead links die at the gate"

            # Stale version from the true guarantor: optimistic 409.
            res = await client.post(
                f"/member/guarantees/{gid}/consent",
                json={"version": 7},
                headers=_member_headers(tid, g_cred, guarantor),
            )
            assert res.status_code == 409, res.text

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Migration contract: the 0035 loud-refusal downgrade guard
# ---------------------------------------------------------------------------


def _load_migration(filename: str) -> Any:
    """The real revision module (the 0017/0031/0032 falsifiability
    precedent — never a copy of the guard SQL)."""
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:4]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0035_downgrade_refuses_over_identity_data() -> None:
    """The 0035 guard REFUSES while identity- or consent-bearing data
    exists: an empty expansion passes (migrate-check's up→down→up),
    a linked credential refuses, and a principal-carrying consent
    refuses even after the credential row is gone from the count's
    view. Fails with the guard removed (the downgrade would silently
    destroy the authorization substrate)."""

    async def run() -> None:
        migration = _load_migration("0035_member_identity.py")
        assert "refusing downgrade 0035" in migration._DOWN_GUARD

        # Empty tenant: the guard passes (RLS scopes the counts).
        tid, _, _ = await _seed_actor()
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))

        # A live credential link refuses.
        mid = await _seed_member(tid)
        await _link_credential(tid, mid, email=unique_email())
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

        # A principal-carrying consent refuses in its own right, in a
        # tenant with no credential rows at all (staff attestation).
        tid2, _, staff_token = await _seed_actor()
        guarantor = await _seed_member(tid2, deposit="10000.00")
        borrower = await _seed_member(tid2)
        gid = await _seed_pledged_guarantee(tid2, guarantor=guarantor, borrower=borrower)
        async with api_client() as client:
            res = await client.post(
                f"/guarantees/{gid}/consent",
                json={"version": 1, "consent_reference": "signed form GF-0035"},
                headers=_headers(staff_token),
            )
            assert res.status_code == 200, res.text
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid2) as session:
                await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())
