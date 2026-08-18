"""ADR-0007 member self-service READ surface (P17 mobile unblock).

Falsifiable claims, each with a hand-computed oracle:

  IDOR proof            — member A's token NEVER reads member B's data
                          on ANY new route: B's loan detail is a 404
                          (indistinguishable from nonexistent — the
                          ownership predicate lives IN the query), B's
                          postings/loans/statement lines never appear
                          on A's pages.
  FM1 both directions   — the most privileged staff token is 403 on
                          every new /member read; the member token
                          stays 403 on the staff equivalents.
  Live-link re-check    — a credential revoked or RE-POINTED after
                          token issue dies within ONE request on every
                          new route (the RequireMemberPrincipal fence).
  Cursor scope isolation— a staff transactions/loan-book/statement
                          cursor is a sanitized 400 on the member
                          route and vice versa (member.transactions.list
                          / member.loans.list / member.statement are
                          disjoint signed-cursor scopes); each cursor
                          still walks its OWN route exhaustively.
  /member/me oracle     — deposit 150.00 + shares 50.00 + two active
                          loans (100.00 + 200.00) and one closed row:
                          count 2, total 300.00 — the closed loan is
                          excluded exactly like the staff drawer.
  Installment status    — derived server-side from the schedule rows:
                          paid_amount >= total_due -> 'paid',
                          0 < paid < total -> 'partial', 0 -> 'open'.

EXPLAIN gates for the new statements live in
tests/test_member_portal_explain.py (the EXPLAIN-capture convention);
the member transactions page and the statement page reuse statements
already gated by test_p11_explain / test_p13_explain.
"""

import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, unique_email
from export_helpers import seed_actor
from genesis.application import transactions as txn_service
from genesis.application.auth import MemberAuthContext, issue_member_access_token
from genesis.domain.ledger import Channel
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
    credential_id, MEMBER-audience bearer token)."""
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


async def _seed_product(tid: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"MP-{uuid.uuid4().hex[:8]}"},
        )
    return pid


async def _seed_loan(
    tid: uuid.UUID,
    mid: uuid.UUID,
    pid: uuid.UUID,
    *,
    balance: str,
    status: str = "active",
) -> uuid.UUID:
    """Loan + backing application seeded raw at a chosen status (the
    test_member_aggregates fixture shape — a closed row with nonzero
    balance is unreachable through the closure rule, so the status
    filter is the ONLY thing keeping it out of the /member/me summary)."""
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
                "mid": str(mid),
                "pid": str(pid),
                "amount": balance if Decimal(balance) > 0 else "1.00",
            },
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, "
                " principal, balance, rate_pct, term_months, status) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), :principal, :balance, "
                "'12.00', 12, :status)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(aid),
                "mid": str(mid),
                "pid": str(pid),
                "principal": balance if Decimal(balance) > 0 else "1.00",
                "balance": balance,
                "status": status,
            },
        )
    return loan_id


async def _add_installment(
    tid: uuid.UUID,
    loan_id: uuid.UUID,
    *,
    installment_no: int,
    due: date,
    total_due: str,
    paid_amount: str = "0",
) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due, paid_amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                ":no, :due, :total, 0, :total, :paid)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "lid": str(loan_id),
                "no": installment_no,
                "due": due,
                "total": total_due,
                "paid": paid_amount,
            },
        )


async def _deposit(tid: uuid.UUID, mid: uuid.UUID, amount: str) -> str:
    """Post a real deposit through the P11 service; returns the txn_ref."""
    async with tenant_session(factory(), tid) as session:
        result = await txn_service.record_deposit(
            session, tid, None, mid, amount=Decimal(amount), channel=Channel.MPESA
        )
    return result.txn_ref


def test_member_me_profile_balances_and_loan_summary() -> None:
    """Oracle (hand-computed): 100.00 + 50.00 deposits = 150.00,
    shares stay 0.00; active loans 100.00 + 200.00 -> count 2, total
    300.00; the closed 75.00 row is EXCLUDED (remove the status filter
    and count reads 3 / total 375.00). Least disclosure: the response
    carries exactly the declared keys — no guarantee figures, no
    internal ids, no version."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid, _, token = await _seed_member_principal(tid, name="Amina Yusuf", member_no="GP-7001")
        await _deposit(tid, mid, "100.00")
        await _deposit(tid, mid, "50.00")
        pid = await _seed_product(tid)
        await _seed_loan(tid, mid, pid, balance="100.00")
        await _seed_loan(tid, mid, pid, balance="200.00")
        await _seed_loan(tid, mid, pid, balance="75.00", status="closed")
        async with api_client() as client:
            res = await client.get("/member/me", headers=_headers(token))
        assert res.status_code == 200, res.text
        body = res.json()
        assert set(body) == {
            "member_no",
            "name",
            "status",
            "deposit_balance",
            "share_balance",
            "loans",
        }
        assert body["member_no"] == "GP-7001"
        assert body["name"] == "Amina Yusuf"
        assert body["status"] == "active"
        assert body["deposit_balance"] == "150.00"
        assert body["share_balance"] == "0.00"
        assert body["loans"] == {"count": 2, "total_outstanding": "300.00"}

    asyncio.run(run())


def test_idor_member_a_never_reads_member_b_data() -> None:
    """The IDOR proof on every new route: A and B live in the SAME
    tenant, so nothing but the principal-derived member id separates
    them. B's loan detail under A's token is a 404 carrying the
    envelope only (ownership is IN the query — indistinguishable from
    a nonexistent id, no existence oracle); B's refs/loans/lines never
    surface on A's list pages."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid_a, _, token_a = await _seed_member_principal(tid, name="Member A", member_no="GP-7101")
        mid_b, _, _ = await _seed_member_principal(tid, name="Member B", member_no="GP-7102")
        ref_a = await _deposit(tid, mid_a, "10.00")
        ref_b = await _deposit(tid, mid_b, "20.00")
        pid = await _seed_product(tid)
        loan_a = await _seed_loan(tid, mid_a, pid, balance="100.00")
        loan_b = await _seed_loan(tid, mid_b, pid, balance="200.00")
        async with api_client() as client:
            headers = _headers(token_a)

            me = await client.get("/member/me", headers=headers)
            assert me.status_code == 200
            assert me.json()["member_no"] == "GP-7101"

            txns = await client.get("/member/transactions", headers=headers)
            assert txns.status_code == 200
            refs = [t["txn_ref"] for t in txns.json()["items"]]
            assert refs == [ref_a]
            assert ref_b not in refs

            loans = await client.get("/member/loans", headers=headers)
            assert loans.status_code == 200
            ids = [x["id"] for x in loans.json()["items"]]
            assert ids == [str(loan_a)]

            own = await client.get(f"/member/loans/{loan_a}", headers=headers)
            assert own.status_code == 200

            foreign = await client.get(f"/member/loans/{loan_b}", headers=headers)
            assert foreign.status_code == 404, "another member's loan must be a 404"
            # Least disclosure: the envelope only — no figures echoed.
            assert set(foreign.json()) == {"category", "correlation_id"}

            statement = await client.get("/member/statement", headers=headers)
            assert statement.status_code == 200
            lines = [line["txn_ref"] for line in statement.json()["items"]]
            assert lines == [ref_a]

    asyncio.run(run())


def test_audience_separation_both_directions_on_every_new_route() -> None:
    """FM1 on the read surface: the MOST privileged staff token (System
    Admin, every permission) is 403 on all five member reads; the
    member token stays 403 on the staff read equivalents. Falsifiable:
    remove the audience dispatch and both loops collapse."""

    async def run() -> None:
        tid, _, staff_token = await seed_actor()
        mid, _, member_token = await _seed_member_principal(
            tid, name="Boundary Member", member_no="GP-7201"
        )
        pid = await _seed_product(tid)
        loan_id = await _seed_loan(tid, mid, pid, balance="100.00")
        async with api_client() as client:
            for path in (
                "/member/me",
                "/member/transactions",
                "/member/loans",
                f"/member/loans/{loan_id}",
                "/member/statement",
            ):
                res = await client.get(path, headers=_headers(staff_token))
                assert res.status_code == 403, f"staff on {path}: {res.status_code}"
                assert set(res.json()) == {"category", "correlation_id"}
            for path in (
                "/transactions",
                "/loans",
                f"/loans/{loan_id}",
                f"/members/{mid}",
                f"/members/{mid}/statement",
            ):
                res = await client.get(path, headers=_headers(member_token))
                assert res.status_code == 403, f"member on {path}: {res.status_code}"
                assert set(res.json()) == {"category", "correlation_id"}

    asyncio.run(run())


def test_revoked_or_repointed_credential_dies_within_one_request() -> None:
    """The live-link re-check on the read surface: a credential revoked
    OR re-pointed at another member AFTER token issue is 401 on its
    very next use of EVERY new route — the access token's remaining
    lifetime never bridges the committed change."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid, cid, token = await _seed_member_principal(tid, name="Revoked M", member_no="GP-7301")
        other_mid, _, _ = await _seed_member_principal(tid, name="Other M", member_no="GP-7302")
        pid = await _seed_product(tid)
        loan_id = await _seed_loan(tid, mid, pid, balance="100.00")
        paths = (
            "/member/me",
            "/member/transactions",
            "/member/loans",
            f"/member/loans/{loan_id}",
            "/member/statement",
        )
        async with api_client() as client:
            live = await client.get("/member/me", headers=_headers(token))
            assert live.status_code == 200

            # RE-POINT: the credential now links another member — the
            # token's mid no longer matches the live link.
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_credentials SET member_id = CAST(:m AS uuid), "
                        "version = version + 1 WHERE id = CAST(:id AS uuid)"
                    ),
                    {"m": str(other_mid), "id": str(cid)},
                )
            for path in paths:
                res = await client.get(path, headers=_headers(token))
                assert res.status_code == 401, f"re-pointed link on {path}: {res.status_code}"

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
            for path in paths:
                res = await client.get(path, headers=_headers(token))
                assert res.status_code == 401, f"revoked link on {path}: {res.status_code}"

    asyncio.run(run())


def test_cursor_scopes_are_isolated_between_staff_and_member_routes() -> None:
    """Scope isolation, falsifiable in BOTH directions on all three
    paginated pairs: a cursor minted by the staff route is a sanitized
    400 on the member route and vice versa (mint both under one scope
    and every 400 below turns 200). Each cursor still resumes its OWN
    route: the member transaction walk with limit=1 is exhaustive and
    duplicate-free."""

    async def run() -> None:
        tid, _, staff_token = await seed_actor()
        mid, _, member_token = await _seed_member_principal(
            tid, name="Cursor Member", member_no="GP-7401"
        )
        for amount in ("10.00", "20.00", "30.00"):
            await _deposit(tid, mid, amount)
        pid = await _seed_product(tid)
        for balance in ("100.00", "200.00"):
            await _seed_loan(tid, mid, pid, balance=balance)
        async with api_client() as client:
            pairs = (
                ("/transactions", "/member/transactions"),
                ("/loans", "/member/loans"),
                (f"/members/{mid}/statement", "/member/statement"),
            )
            for staff_path, member_path in pairs:
                staff_page = await client.get(
                    staff_path, params={"limit": 1}, headers=_headers(staff_token)
                )
                assert staff_page.status_code == 200, staff_page.text
                staff_cursor = staff_page.json()["next_cursor"]
                assert staff_cursor is not None

                member_page = await client.get(
                    member_path, params={"limit": 1}, headers=_headers(member_token)
                )
                assert member_page.status_code == 200, member_page.text
                member_cursor = member_page.json()["next_cursor"]
                assert member_cursor is not None

                # Staff cursor on the member route: sanitized 400.
                crossed = await client.get(
                    member_path,
                    params={"limit": 1, "cursor": staff_cursor},
                    headers=_headers(member_token),
                )
                assert crossed.status_code == 400, f"{member_path}: {crossed.status_code}"
                assert set(crossed.json()) == {"category", "correlation_id"}

                # Member cursor on the staff route: sanitized 400.
                crossed = await client.get(
                    staff_path,
                    params={"limit": 1, "cursor": member_cursor},
                    headers=_headers(staff_token),
                )
                assert crossed.status_code == 400, f"{staff_path}: {crossed.status_code}"
                assert set(crossed.json()) == {"category", "correlation_id"}

                # The member cursor resumes its OWN route.
                resumed = await client.get(
                    member_path,
                    params={"limit": 1, "cursor": member_cursor},
                    headers=_headers(member_token),
                )
                assert resumed.status_code == 200, resumed.text

            # Exhaustive, duplicate-free member transaction walk.
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(6):
                params: dict[str, object] = {"limit": 1}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get(
                    "/member/transactions", params=params, headers=_headers(member_token)
                )
                assert res.status_code == 200
                body = res.json()
                seen.extend(t["id"] for t in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            assert len(seen) == 3
            assert len(set(seen)) == 3

    asyncio.run(run())


def test_loan_detail_serves_schedule_with_derived_installment_status() -> None:
    """Installment status is derived SERVER-side (no client-side money
    math): fully covered -> 'paid', partially covered -> 'partial',
    untouched -> 'open'. Least disclosure: the loan rows carry no
    classification/provision internals and no lock version."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mid, _, token = await _seed_member_principal(tid, name="Sched M", member_no="GP-7501")
        pid = await _seed_product(tid)
        loan_id = await _seed_loan(tid, mid, pid, balance="300.00")
        await _add_installment(
            tid, loan_id, installment_no=1, due=date(2026, 1, 31), total_due="100.00",
            paid_amount="100.00",
        )
        await _add_installment(
            tid, loan_id, installment_no=2, due=date(2026, 2, 28), total_due="100.00",
            paid_amount="40.00",
        )
        await _add_installment(
            tid, loan_id, installment_no=3, due=date(2026, 3, 31), total_due="100.00",
        )
        async with api_client() as client:
            res = await client.get(f"/member/loans/{loan_id}", headers=_headers(token))
        assert res.status_code == 200, res.text
        body = res.json()
        assert set(body) == {
            "id",
            "loan_ref",
            "product_name",
            "principal",
            "balance",
            "rate_pct",
            "term_months",
            "status",
            "days_past_due",
            "penalty_due",
            "disbursed_at",
            "closed_at",
            "schedule",
        }
        statuses = [(row["installment_no"], row["status"]) for row in body["schedule"]]
        assert statuses == [(1, "paid"), (2, "partial"), (3, "open")]

    asyncio.run(run())
