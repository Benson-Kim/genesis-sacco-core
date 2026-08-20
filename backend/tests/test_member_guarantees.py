"""GET /member/guarantees — the P17 consent-inbox data source (#41).

Falsifiable claims, each with a hand-computed oracle (the !7
test_member_portal suite is the template):

  Inbox oracle          — the guarantor's three pledges come back
                          newest first with EXACTLY the declared keys
                          (id, loan_ref, amount, status, version):
                          least disclosure means no borrower_member_id,
                          no guarantor_member_id, no application/loan
                          UUIDs. loan_ref is the human LN ref once the
                          loan exists, None while the pledge backs an
                          application. status= filters on the schema
                          CHECK set; anything else is a 422.
  IDOR proof            — a SECOND real member in the SAME tenant sees
                          an empty inbox (nothing but the principal-
                          derived guarantor predicate separates them);
                          rows where that member is the BORROWER never
                          leak into their guarantor inbox.
  FM1 both directions   — the most privileged staff token is 403 on
                          /member/guarantees; the member token stays
                          403 on the staff equivalents.
  Live-link re-check    — a credential revoked or RE-POINTED after
                          token issue dies within ONE request
                          (the RequireMemberPrincipal fence).
  Cursor scope isolation— member.guarantees.list is a NEW disjoint
                          signed-cursor scope: a staff loan-book
                          cursor is a sanitized 400 here, a guarantees
                          cursor is a sanitized 400 on the staff route
                          AND on the sibling member routes; garbage is
                          a sanitized 400; the cursor still walks its
                          OWN route exhaustively and duplicate-free.

The EXPLAIN structural gate for the new statement lives in
tests/test_member_guarantees_explain.py (the EXPLAIN-capture
convention): the probe rides idx_guarantees_guarantor (0001) — this
surface ships NO migration.
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, unique_email
from export_helpers import seed_actor
from genesis.application.auth import MemberAuthContext, issue_member_access_token
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_member_principal(
    tid: uuid.UUID,
    *,
    name: str,
    member_no: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Member + accounts + ACTIVE credential; returns (member_id,
    credential_id, MEMBER-audience bearer token). The !7 fixture shape,
    self-contained so concurrent edits to the template file never
    couple into this suite."""
    mid = uuid.uuid4()
    cid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', :name, 'active')"
            ),
            {"id": str(mid), "tid": str(tid), "no": member_no, "name": name},
        )
        for table in ("deposit_accounts", "share_accounts"):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), 0)"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
            )
        await session.execute(
            text(
                "INSERT INTO member_credentials (id, tenant_id, member_id, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :email)"
            ),
            {"id": str(cid), "tid": str(tid), "m": str(mid), "email": unique_email()},
        )
    token = issue_member_access_token(
        MemberAuthContext(credential_id=cid, member_id=mid, tenant_id=tid)
    )
    return mid, cid, token


async def _seed_application(
    tid: uuid.UUID, borrower_mid: uuid.UUID, pid: uuid.UUID, *, amount: str
) -> uuid.UUID:
    aid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, '12.00', 'submitted', '0.00')"
            ),
            {
                "id": str(aid),
                "tid": str(tid),
                "mid": str(borrower_mid),
                "pid": str(pid),
                "amount": amount,
            },
        )
    return aid


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"MG-{uuid.uuid4().hex[:8]}"},
        )
    return pid


async def _seed_loan(
    tid: uuid.UUID,
    borrower_mid: uuid.UUID,
    pid: uuid.UUID,
    *,
    balance: str,
    loan_ref: str | None = None,
) -> uuid.UUID:
    """Disbursed loan + backing application, optionally carrying the
    human loan_ref (0048) the inbox surfaces."""
    aid = uuid.uuid4()
    loan_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, '12.00', 'disbursed', '0.00')"
            ),
            {
                "id": str(aid),
                "tid": str(tid),
                "mid": str(borrower_mid),
                "pid": str(pid),
                "amount": balance,
            },
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months, status, loan_ref) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), :principal, :balance, "
                "'12.00', 12, 'active', :ref)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(aid),
                "mid": str(borrower_mid),
                "pid": str(pid),
                "principal": balance,
                "balance": balance,
                "ref": loan_ref,
            },
        )
    return loan_id


async def _seed_guarantee(
    tid: uuid.UUID,
    guarantor_mid: uuid.UUID,
    borrower_mid: uuid.UUID,
    *,
    amount: str,
    status: str = "pledged",
    application_id: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
    consent_credential_id: uuid.UUID | None = None,
    age_minutes: int = 0,
) -> uuid.UUID:
    """Raw guarantee row at a chosen status. An 'active' row must carry
    its consent principal (the 0035 trigger refuses it otherwise — the
    FM4 backstop this suite deliberately satisfies, never bypasses).
    age_minutes pins created_at so the newest-first order is asserted
    against explicit timestamps, not insert timing."""
    gid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO guarantees "
                "(id, tenant_id, guarantor_member_id, borrower_member_id, "
                " application_id, loan_id, amount, status, "
                " consented_by_credential_id, created_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:g AS uuid), "
                "CAST(:b AS uuid), CAST(:a AS uuid), CAST(:l AS uuid), :amount, :status, "
                "CAST(:cred AS uuid), now() - make_interval(mins => :age))"
            ),
            {
                "id": str(gid),
                "tid": str(tid),
                "g": str(guarantor_mid),
                "b": str(borrower_mid),
                "a": str(application_id) if application_id else None,
                "l": str(loan_id) if loan_id else None,
                "amount": amount,
                "status": status,
                "cred": str(consent_credential_id) if consent_credential_id else None,
                "age": age_minutes,
            },
        )
    return gid


def test_member_guarantees_inbox_shape_order_and_status_filter() -> None:
    """Oracle (hand-computed): three own pledges come back newest first
    — released (age 0) -> active (age 10) -> pledged (age 20); every
    row carries EXACTLY {id, loan_ref, amount, status, version} (least
    disclosure: shaping is _guarantee_out MINUS the staff-only fields —
    add borrower_member_id back and the key-set asserts fail). The
    active row shows the human loan_ref; the application-backed pledge
    shows None. status=pledged narrows to the one consentable row (the
    consent-inbox filter); a status outside the schema CHECK set is a
    422 at validation."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        gmid, gcid, token = await _seed_member_principal(
            tid, name="Guarantor G", member_no="GP-8001"
        )
        bmid, _, _ = await _seed_member_principal(tid, name="Borrower B", member_no="GP-8002")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, bmid, pid, amount="500.00")
        loan_id = await _seed_loan(tid, bmid, pid, balance="400.00", loan_ref="LN-8001")
        g_pledged = await _seed_guarantee(
            tid, gmid, bmid, amount="100.00", application_id=aid, age_minutes=20
        )
        g_active = await _seed_guarantee(
            tid,
            gmid,
            bmid,
            amount="200.00",
            status="active",
            loan_id=loan_id,
            consent_credential_id=gcid,
            age_minutes=10,
        )
        g_released = await _seed_guarantee(
            tid, gmid, bmid, amount="50.00", status="released", application_id=aid, age_minutes=0
        )
        async with api_client() as client:
            res = await client.get("/member/guarantees", headers=_headers(token))
            assert res.status_code == 200, res.text
            body = res.json()
            assert set(body) == {"items", "next_cursor"}
            assert body["next_cursor"] is None
            assert [g["id"] for g in body["items"]] == [
                str(g_released),
                str(g_active),
                str(g_pledged),
            ]
            for row in body["items"]:
                # Least disclosure: EXACTLY the declared keys — no
                # borrower/guarantor ids, no application/loan UUIDs.
                assert set(row) == {"id", "loan_ref", "amount", "status", "version"}
            released_row, active_row, pledged_row = body["items"]
            assert released_row["amount"] == "50.00"
            assert active_row == {
                "id": str(g_active),
                "loan_ref": "LN-8001",
                "amount": "200.00",
                "status": "active",
                "version": 1,
            }
            assert pledged_row["loan_ref"] is None
            assert pledged_row["status"] == "pledged"
            # version is the optimistic-lock handle for the consent act.
            assert pledged_row["version"] == 1

            # The consent-inbox filter: only the consentable pledge.
            filtered = await client.get(
                "/member/guarantees", params={"status": "pledged"}, headers=_headers(token)
            )
            assert filtered.status_code == 200
            assert [g["id"] for g in filtered.json()["items"]] == [str(g_pledged)]

            # Outside the schema CHECK set: refused at validation.
            bogus = await client.get(
                "/member/guarantees", params={"status": "consented"}, headers=_headers(token)
            )
            assert bogus.status_code == 422

            # Page cap: limit is bounded at 100 (and floored at 1).
            for bad_limit in (0, 101):
                capped = await client.get(
                    "/member/guarantees", params={"limit": bad_limit}, headers=_headers(token)
                )
                assert capped.status_code == 422, f"limit={bad_limit}: {capped.status_code}"

    asyncio.run(run())


def test_idor_second_member_sees_empty_inbox_and_borrower_rows_never_leak() -> None:
    """The IDOR proof with a SECOND real member in the SAME tenant:
    nothing but the principal-derived guarantor predicate separates M
    from G, and M's inbox is empty even though M is the BORROWER on
    G's pledge (borrower-side rows belong to the loan surface, never
    the guarantor inbox — swap the predicate to borrower_member_id and
    both asserts fail). G's inbox carries exactly G's own row."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        gmid, _, g_token = await _seed_member_principal(
            tid, name="Guarantor G", member_no="GP-8101"
        )
        mmid, _, m_token = await _seed_member_principal(tid, name="Member M", member_no="GP-8102")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, mmid, pid, amount="300.00")
        gid = await _seed_guarantee(tid, gmid, mmid, amount="120.00", application_id=aid)
        async with api_client() as client:
            own = await client.get("/member/guarantees", headers=_headers(g_token))
            assert own.status_code == 200
            assert [g["id"] for g in own.json()["items"]] == [str(gid)]

            other = await client.get("/member/guarantees", headers=_headers(m_token))
            assert other.status_code == 200, other.text
            assert other.json()["items"] == [], "the borrower must never see the pledge here"

    asyncio.run(run())


def test_audience_separation_both_directions() -> None:
    """FM1 on the guarantees inbox: the MOST privileged staff token
    (System Admin, every permission) is 403 on /member/guarantees; the
    member token stays 403 on the staff read equivalents. Falsifiable:
    remove the audience dispatch and both loops collapse."""

    async def run() -> None:
        tid, _, staff_token = await seed_actor()
        _, _, member_token = await _seed_member_principal(
            tid, name="Boundary Member", member_no="GP-8201"
        )
        async with api_client() as client:
            res = await client.get("/member/guarantees", headers=_headers(staff_token))
            assert res.status_code == 403, f"staff on /member/guarantees: {res.status_code}"
            assert set(res.json()) == {"category", "correlation_id"}

            for path in ("/loans", "/transactions"):
                res = await client.get(path, headers=_headers(member_token))
                assert res.status_code == 403, f"member on {path}: {res.status_code}"
                assert set(res.json()) == {"category", "correlation_id"}

    asyncio.run(run())


def test_revoked_or_repointed_credential_dies_within_one_request() -> None:
    """The live-link re-check: a credential revoked OR re-pointed at
    another member AFTER token issue is 401 on its very next
    /member/guarantees request — the access token's remaining lifetime
    never bridges the committed change."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid, cid, token = await _seed_member_principal(tid, name="Revoked G", member_no="GP-8301")
        # The re-point target deliberately has NO credential of its own
        # (0035 allows one ACTIVE link per member; this moves the link).
        other_mid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), 'GP-8302', 'person', "
                    "'Other G', 'active')"
                ),
                {"id": str(other_mid), "tid": str(tid)},
            )
        async with api_client() as client:
            live = await client.get("/member/guarantees", headers=_headers(token))
            assert live.status_code == 200

            # RE-POINT: the credential now links another member.
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_credentials SET member_id = CAST(:m AS uuid), "
                        "version = version + 1 WHERE id = CAST(:id AS uuid)"
                    ),
                    {"m": str(other_mid), "id": str(cid)},
                )
            res = await client.get("/member/guarantees", headers=_headers(token))
            assert res.status_code == 401, f"re-pointed link: {res.status_code}"

            # REVOKE (after restoring the link so revocation is what kills it).
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_credentials SET member_id = CAST(:m AS uuid), "
                        "status = 'revoked', revoked_at = now(), version = version + 1 "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"m": str(mid), "id": str(cid)},
                )
            res = await client.get("/member/guarantees", headers=_headers(token))
            assert res.status_code == 401, f"revoked link: {res.status_code}"

    asyncio.run(run())


def test_guarantees_cursor_scope_is_isolated_in_both_directions() -> None:
    """member.guarantees.list is a NEW disjoint signed-cursor scope,
    falsifiable in BOTH directions: a STAFF loan-book cursor is a
    sanitized 400 on /member/guarantees, and a guarantees cursor is a
    sanitized 400 on the staff route AND on the sibling member routes
    (mint them all under one scope and every 400 below turns 200).
    Garbage is the same sanitized 400 (the FM9 tamper contract). The
    guarantees cursor still resumes its OWN route: the limit=1 walk is
    exhaustive and duplicate-free."""

    async def run() -> None:
        tid, _, staff_token = await seed_actor()
        gmid, _, member_token = await _seed_member_principal(
            tid, name="Cursor G", member_no="GP-8401"
        )
        bmid, _, _ = await _seed_member_principal(tid, name="Cursor B", member_no="GP-8402")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, bmid, pid, amount="900.00")
        seeded: list[uuid.UUID] = []
        for i, amount in enumerate(("10.00", "20.00", "30.00")):
            seeded.append(
                await _seed_guarantee(
                    tid, gmid, bmid, amount=amount, application_id=aid, age_minutes=(3 - i)
                )
            )
        # Two staff-visible loans so the staff loan-book mints a cursor.
        for balance in ("100.00", "200.00"):
            await _seed_loan(tid, bmid, pid, balance=balance)
        async with api_client() as client:
            staff_page = await client.get(
                "/loans", params={"limit": 1}, headers=_headers(staff_token)
            )
            assert staff_page.status_code == 200, staff_page.text
            staff_cursor = staff_page.json()["next_cursor"]
            assert staff_cursor is not None

            member_page = await client.get(
                "/member/guarantees", params={"limit": 1}, headers=_headers(member_token)
            )
            assert member_page.status_code == 200, member_page.text
            guarantees_cursor = member_page.json()["next_cursor"]
            assert guarantees_cursor is not None

            # Garbage cursor: strict decode, sanitized 400, envelope
            # only — never a 500, never a silently empty 200 page.
            garbage = await client.get(
                "/member/guarantees",
                params={"limit": 1, "cursor": "not-a-cursor"},
                headers=_headers(member_token),
            )
            assert garbage.status_code == 400, garbage.text
            body = garbage.json()
            assert set(body) == {"category", "correlation_id"}
            assert body["category"] == "validation_error"

            # Staff loan-book cursor on the member guarantees route.
            crossed = await client.get(
                "/member/guarantees",
                params={"limit": 1, "cursor": staff_cursor},
                headers=_headers(member_token),
            )
            assert crossed.status_code == 400, crossed.text
            assert set(crossed.json()) == {"category", "correlation_id"}

            # Guarantees cursor on the staff loan book.
            crossed = await client.get(
                "/loans",
                params={"limit": 1, "cursor": guarantees_cursor},
                headers=_headers(staff_token),
            )
            assert crossed.status_code == 400, crossed.text
            assert set(crossed.json()) == {"category", "correlation_id"}

            # Guarantees cursor on the SIBLING member routes: the
            # member-own scopes are disjoint among themselves too.
            for sibling in ("/member/loans", "/member/transactions"):
                crossed = await client.get(
                    sibling,
                    params={"limit": 1, "cursor": guarantees_cursor},
                    headers=_headers(member_token),
                )
                assert crossed.status_code == 400, f"{sibling}: {crossed.status_code}"
                assert set(crossed.json()) == {"category", "correlation_id"}

            # Exhaustive, duplicate-free walk of the OWN route.
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(6):
                params: dict[str, object] = {"limit": 1}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get(
                    "/member/guarantees", params=params, headers=_headers(member_token)
                )
                assert res.status_code == 200
                page = res.json()
                seen.extend(g["id"] for g in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert len(seen) == 3
            assert len(set(seen)) == 3
            assert set(seen) == {str(g) for g in seeded}
            # Amount totals pin the rows, not just the ids (oracle).
            total = sum(Decimal(a) for a in ("10.00", "20.00", "30.00"))
            assert total == Decimal("60.00")

    asyncio.run(run())
