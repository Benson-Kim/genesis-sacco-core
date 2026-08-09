"""P13.9 — dashboard & guarantor aggregates (real Postgres, RLS).

Every numbered banking-grade failure mode from the prompt has its own
falsifiable test (remove the guard under test and the test fails):

  FM1 aggregate-vs-ledger drift — deposit/share totals reconcile to the
      cent against the ledger reconstruction; the drift-detector arm
      mutates a balance column raw and proves the oracle CAN fail.
  FM2 cross-tenant aggregate bleed — issue-#17 probe (session AS tenant
      A, tenant-B argument -> zeros, never a mixed aggregate) plus a
      two-tenant API assertion that each sees exactly its own figures.
  FM3 permission-slicing bypass — full 7-role walk asserting per-slice
      PRESENCE and ABSENCE (omitted, not zeroed); a Teller gets no
      loan-book slice, a Credit Committee member no deposit totals.
  FM4 period-boundary error — a deposit at 23:59:59.999999 on month-end
      (the FY-end dividend pinning convention) and one at 00:00:00 on
      the 1st land in different UTC buckets; reversals subtract.
  FM5 free-capacity divergence — the dashboard figure comes from
      live_pledged_total and is cross-checked against an ACTUAL pledge
      at the boundary (exact amount accepted, one cent more refused).
  FM6 unbounded scans / N+1 — flat query count as data grows; the
      guarantor list obeys the server-resolved hard cap; the month
      window clamps.

All oracles are hand-computed in comments (never captured from the
implementation); dashboard figures match seeded fixtures TO THE CENT.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor
from genesis.application import dashboard as dashboard_service
from genesis.application import guarantees as guarantees_service
from genesis.application import transactions as txn_service
from genesis.application.ledger import post_deposit, post_reversal
from genesis.domain.ledger import Channel
from genesis.domain.rbac import Module
from genesis.errors import ConflictError
from genesis.infrastructure.db import get_engine
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

ALL_SLICES = frozenset({Module.TRANSACTIONS, Module.MEMBERS, Module.APPLICATIONS, Module.LOAN_BOOK})


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_member(
    tid: uuid.UUID,
    *,
    member_type: str = "person",
    status: str = "active",
    name: str = "Dash Member",
    deposit: str = "0",
    shares: str = "0",
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, :ty, :name, :st)"
            ),
            {
                "id": str(mid),
                "tid": str(tid),
                "no": f"GP-{mid.hex[:6].upper()}",
                "ty": member_type,
                "name": name,
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
    pid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"P139-{uuid.uuid4().hex[:8]}"},
        )
    return pid


async def _seed_application(
    tid: uuid.UUID,
    mid: uuid.UUID,
    pid: uuid.UUID,
    *,
    amount: str = "5000.00",
    stage: str = "submitted",
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


async def _deposit(tid: uuid.UUID, actor: uuid.UUID, mid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, actor, mid, amount=Decimal(amount), channel=Channel.MPESA
        )


async def _withdraw(tid: uuid.UUID, actor: uuid.UUID, mid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_withdrawal(
            session, tid, actor, mid, amount=Decimal(amount), channel=Channel.MPESA
        )


async def _share_topup(tid: uuid.UUID, actor: uuid.UUID, mid: uuid.UUID, amount: str) -> None:
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_share_topup(
            session, tid, actor, mid, amount=Decimal(amount), channel=Channel.BANK
        )


async def _pledge(
    tid: uuid.UUID,
    actor: uuid.UUID,
    *,
    application_id: uuid.UUID,
    guarantor: uuid.UUID,
    amount: str,
    consent: bool = False,
) -> uuid.UUID:
    async with tenant_session(factory(), tid) as session:
        record = await guarantees_service.pledge_guarantee(
            session,
            tid,
            actor,
            application_id=application_id,
            guarantor_member_id=guarantor,
            amount=Decimal(amount),
        )
    if consent:
        async with tenant_session(factory(), tid) as session:
            await guarantees_service.consent_guarantee_override(
                session,
                tid,
                actor,
                record.id,
                version=record.version,
                consent_reference="signed form GF-P139",
            )
    return record.id


async def _get_summary(token: str) -> dict[str, Any]:
    async with api_client() as client:
        res = await client.get("/dashboard/summary", headers=_headers(token))
    assert res.status_code == 200, res.text
    return dict(res.json())


# ---------------------------------------------------------------------------
# Fixture oracle — every figure hand-computed, to the cent
# ---------------------------------------------------------------------------


def test_dashboard_summary_matches_seeded_fixtures_to_the_cent() -> None:
    """P10-EXIT precedent: the composite matches the fixture exactly.

    Fixture arithmetic (hand-computed, never captured from the code):
      m1 person/active:  +1234.56 +765.44 -500.00 = 1500.00 deposits;
                         +1000.00 shares
      m2 person/active:  +2500.25 deposits; +750.75 shares
      m3 company/arrears: +999.99 deposits
      total_deposits      = 1500.00 + 2500.25 + 999.99 = 5000.24
      total_share_capital = 1000.00 + 750.75 = 1750.75
      active_members = 2 (m1, m2); by_type: company 1/0, person 2/2
      current-month deposit flow = 1234.56 + 765.44 + 2500.25 + 999.99
        = 5500.24 (withdrawals and share top-ups are NOT deposit flows)
      pipeline: submitted 2 (a1, a2), committee 1, approved 1, others 0
      guarantors: m2 pledges 600.00 on a1 -> 1 live guarantee, total
        600.00; m2 free capacity = 2500.25 - 600.00 = 1900.25
      loan book: no loans -> zeros (reused P10 portfolio summary)
    """

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m1 = await _seed_member(tid, name="M One")
        m2 = await _seed_member(tid, name="M Two")
        m3 = await _seed_member(tid, member_type="company", status="arrears", name="M Co")
        await _deposit(tid, admin_id, m1, "1234.56")
        await _deposit(tid, admin_id, m1, "765.44")
        await _withdraw(tid, admin_id, m1, "500.00")
        await _share_topup(tid, admin_id, m1, "1000.00")
        await _deposit(tid, admin_id, m2, "2500.25")
        await _share_topup(tid, admin_id, m2, "750.75")
        await _deposit(tid, admin_id, m3, "999.99")
        pid = await _seed_product(tid)
        a1 = await _seed_application(tid, m1, pid, stage="submitted")
        await _seed_application(tid, m1, pid, stage="submitted")
        await _seed_application(tid, m2, pid, stage="committee")
        await _seed_application(tid, m2, pid, stage="approved")
        await _pledge(tid, admin_id, application_id=a1, guarantor=m2, amount="600.00")

        body = await _get_summary(token)

        assert body["deposits"] == {
            "total_deposits": "5000.24",
            "total_share_capital": "1750.75",
        }
        assert body["members"]["active_members"] == 2
        assert body["members"]["by_type"] == [
            {"type": "company", "total": 1, "active": 0},
            {"type": "person", "total": 2, "active": 2},
        ]
        flows = body["monthly_flows"]
        # Server-resolved window: 6 months (settings default), oldest
        # first, silent months zero-filled.
        assert len(flows) == 6
        now = datetime.now(UTC)
        assert flows[-1]["month"] == f"{now.year:04d}-{now.month:02d}"
        assert flows[-1]["deposits"] == "5500.24"
        assert flows[-1]["disbursements"] == "0"
        for entry in flows[:-1]:
            assert entry["deposits"] == "0.00"
            assert entry["disbursements"] == "0.00"
        pipeline = {p["stage"]: p["count"] for p in body["pipeline"]}
        assert pipeline == {
            "submitted": 2,
            "appraisal": 0,
            "committee": 1,
            "approved": 1,
            "rejected": 0,
            "disbursed": 0,
        }
        g = body["guarantors"]
        assert g["active_guarantees"] == 1
        assert g["total_pledged"] == "600.00"
        assert g["distinct_guarantors"] == 1
        assert len(g["guarantors"]) == 1
        assert g["guarantors"][0]["member_id"] == str(m2)
        assert g["guarantors"][0]["pledged_total"] == "600.00"
        # 2500.25 - 600.00 = 1900.25 (advisory read; FM5 proves the
        # enforcement path agrees at the boundary).
        assert g["guarantors"][0]["free_capacity"] == "1900.25"
        assert body["loan_book"]["active_loans"] == 0
        assert body["loan_book"]["outstanding_balance"] == "0"

    asyncio.run(run())


def test_dashboard_dormant_member_books_counted_activity_excluded() -> None:
    """Combined P13.9 + P13.13 state (the v1.2 rule 12 refresh).

    Dashboards report BOOKS, not activity: a dormant member's balances
    stay in the tenant totals, while active_members and the by_type
    "active" column exclude them (the members-slice aggregate counts
    status = 'active' only).

    Fixture arithmetic (hand-computed, never captured from the code):
      m_active person/active:  +100.00 deposits; +40.00 shares
      m_dorm   person/dormant: +300.25 deposits; +200.50 shares
        (deposited while ACTIVE, then flipped to dormant by the fixture
         — a legal Active->Dormant edge of the P13.13 machine; a NEW
         deposit cannot be used to build the fixture because it would
         reactivate the member inside the deposit transaction)
      total_deposits      = 100.00 + 300.25 = 400.25
      total_share_capital =  40.00 + 200.50 = 240.50
      active_members = 1 (m_active); by_type: person total 2, active 1

    Falsifiable both ways: counting dormant members as active breaks
    the member assertions; dropping dormant balances from the account
    aggregates breaks the totals.
    """

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m_active = await _seed_member(tid, name="Active One")
        m_dorm = await _seed_member(tid, name="Dormant One")
        await _deposit(tid, admin_id, m_active, "100.00")
        await _share_topup(tid, admin_id, m_active, "40.00")
        await _deposit(tid, admin_id, m_dorm, "300.25")
        await _share_topup(tid, admin_id, m_dorm, "200.50")
        # Fixture-level status flip (the arrears direct-seed precedent
        # above): balances and ledger stay consistent because the money
        # moved while the member was still active.
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE members SET status = 'dormant' "
                    "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(m_dorm), "tid": str(tid)},
            )

        body = await _get_summary(token)

        assert body["deposits"] == {
            "total_deposits": "400.25",
            "total_share_capital": "240.50",
        }
        assert body["members"]["active_members"] == 1
        assert body["members"]["by_type"] == [
            {"type": "person", "total": 2, "active": 1},
        ]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM1 — aggregate-vs-ledger drift
# ---------------------------------------------------------------------------


def test_fm1_totals_reconcile_to_ledger_and_detect_drift() -> None:
    """The balance columns (the documented P8/P11 source) must equal the
    ledger reconstruction to the cent on seeded history.

    Oracle: deposits ledger = CR - DR on member.deposits
      = (100.10 + 49.90 + 200.00) - 75.00 = 275.00
    shares ledger = CR on member.shares = 33.33.
    Drift-detector arm (falsifiability): a raw +100.00 mutation of one
    balance column makes the balance-derived dashboard figure diverge
    from the ledger reconstruction by exactly 100.00 — proving this
    oracle fails whenever a figure is pointed at a stale/mutated column.
    """

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m1 = await _seed_member(tid)
        await _deposit(tid, admin_id, m1, "100.10")
        await _deposit(tid, admin_id, m1, "49.90")
        await _deposit(tid, admin_id, m1, "200.00")
        await _withdraw(tid, admin_id, m1, "75.00")
        await _share_topup(tid, admin_id, m1, "33.33")

        async def ledger_total(account: str) -> Decimal:
            async with tenant_session(factory(), tid) as session:
                value = (
                    await session.execute(
                        text(
                            "SELECT COALESCE(SUM(CASE WHEN side = 'credit' THEN amount "
                            "ELSE -amount END), 0) FROM ledger_entries "
                            "WHERE tenant_id = CAST(:tid AS uuid) AND account = :acct"
                        ),
                        {"tid": str(tid), "acct": account},
                    )
                ).scalar_one()
            return Decimal(str(value))

        body = await _get_summary(token)
        assert body["deposits"]["total_deposits"] == "275.00"
        assert body["deposits"]["total_share_capital"] == "33.33"
        assert await ledger_total("member.deposits") == Decimal("275.00")
        assert await ledger_total("member.shares") == Decimal("33.33")

        # Drift-detector arm: mutate the balance column behind the
        # ledger's back (the exact production defect class).
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE deposit_accounts SET balance = balance + 100.00 "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = CAST(:m AS uuid)"
                ),
                {"tid": str(tid), "m": str(m1)},
            )
        drifted = await _get_summary(token)
        drifted_total = Decimal(drifted["deposits"]["total_deposits"])
        ledger = await ledger_total("member.deposits")
        assert drifted_total == Decimal("375.00")
        assert drifted_total - ledger == Decimal("100.00"), (
            "reconciliation must detect a mutated balance column"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — cross-tenant aggregate bleed
# ---------------------------------------------------------------------------


def test_fm2_issue17_probe_foreign_tenant_argument_returns_zero_aggregates() -> None:
    """Session AS tenant A (RLS satisfied, cannot mask the predicate),
    tenant-B argument: every aggregate must come back empty/zero via
    the explicit bound tenant predicates alone — never A's figures
    under B's label, never a mixed aggregate. Remove any tenant
    predicate in application.dashboard and this test fails."""

    async def run() -> None:
        tid_a, admin_a, _ = await seed_actor()
        tid_b = uuid.uuid4()  # foreign tenant id used purely as an argument
        m1 = await _seed_member(tid_a)
        await _deposit(tid_a, admin_a, m1, "1000.00")
        await _share_topup(tid_a, admin_a, m1, "500.00")
        pid = await _seed_product(tid_a)
        a1 = await _seed_application(tid_a, m1, pid)
        m2 = await _seed_member(tid_a, deposit="800.00")
        await _pledge(tid_a, admin_a, application_id=a1, guarantor=m2, amount="100.00")

        async with tenant_session(factory(), tid_a) as session:
            summary = await dashboard_service.dashboard_summary(session, tid_b, granted=ALL_SLICES)
        assert summary.deposits is not None
        assert summary.deposits.total_deposits == Decimal("0")
        assert summary.deposits.total_share_capital == Decimal("0")
        assert summary.members is not None
        assert summary.members.active_members == 0
        assert summary.members.by_type == ()
        assert summary.monthly_flows is not None
        assert all(
            f.deposits == Decimal("0.00") and f.disbursements == Decimal("0.00")
            for f in summary.monthly_flows
        )
        assert summary.pipeline is not None
        assert all(p.count == 0 for p in summary.pipeline)
        assert summary.guarantors is not None
        assert summary.guarantors.active_guarantees == 0
        assert summary.guarantors.total_pledged == Decimal("0")
        assert summary.guarantors.guarantors == ()
        assert summary.loan_book is not None
        assert summary.loan_book.active_loans == 0

    asyncio.run(run())


def test_fm2_two_tenants_each_see_exactly_their_own_figures() -> None:
    """Two tenants, different figures; each token sees only its own.

    Oracle: tenant A deposits 111.11; tenant B deposits 222.22."""

    async def run() -> None:
        tid_a, admin_a, token_a = await seed_actor()
        tid_b, admin_b, token_b = await seed_actor()
        ma = await _seed_member(tid_a)
        mb = await _seed_member(tid_b)
        await _deposit(tid_a, admin_a, ma, "111.11")
        await _deposit(tid_b, admin_b, mb, "222.22")
        body_a = await _get_summary(token_a)
        body_b = await _get_summary(token_b)
        assert body_a["deposits"]["total_deposits"] == "111.11"
        assert body_b["deposits"]["total_deposits"] == "222.22"
        assert body_a["members"]["active_members"] == 1
        assert body_b["members"]["active_members"] == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — permission slicing (full 7-role walk)
# ---------------------------------------------------------------------------

#: Expected slice PRESENCE per seeded P4 role (module x view):
#:   deposits + monthly_flows <- transactions:view
#:   members                  <- members:view
#:   pipeline + guarantors    <- applications:view
#:   loan_book                <- loan_book:view
_ROLE_SLICES: dict[str, set[str]] = {
    "System Admin": {"deposits", "monthly_flows", "members", "pipeline", "guarantors", "loan_book"},
    "Branch Manager": {
        "deposits",
        "monthly_flows",
        "members",
        "pipeline",
        "guarantors",
        "loan_book",
    },
    "Loan Officer": {"deposits", "monthly_flows", "members", "pipeline", "guarantors", "loan_book"},
    "Teller": {"deposits", "monthly_flows", "members"},
    "Credit Committee": {"pipeline", "guarantors", "loan_book"},
    "Accountant": {"deposits", "monthly_flows", "members", "loan_book"},
    "Auditor": {"deposits", "monthly_flows", "members", "pipeline", "guarantors", "loan_book"},
}

_ALL_SLICE_KEYS = {"deposits", "monthly_flows", "members", "pipeline", "guarantors", "loan_book"}


def test_fm3_permission_slicing_full_role_walk() -> None:
    """Composite endpoints are the classic leak path: one router gate,
    many figures. Assert per-slice presence AND absence for all 7
    seeded roles — ungranted slices are OMITTED from the JSON entirely
    (a zeroed slice would still disclose its shape). Removing the
    per-slice grant check in the handler fails this test (e.g. a
    Teller would receive the loan-book slice)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        m1 = await _seed_member(tid)
        await _deposit(tid, admin_id, m1, "500.00")
        pid = await _seed_product(tid)
        await _seed_application(tid, m1, pid)
        for role, expected in _ROLE_SLICES.items():
            _, token = await add_user(tid, role)
            body = await _get_summary(token)
            present = _ALL_SLICE_KEYS & set(body.keys())
            assert present == expected, f"{role}: got {sorted(present)}"
            for absent in _ALL_SLICE_KEYS - expected:
                assert absent not in body, f"{role} must not receive {absent}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — period boundaries (UTC calendar months, reversal-aware)
# ---------------------------------------------------------------------------


def test_fm4_month_boundary_and_reversals_bucket_correctly() -> None:
    """A deposit at 23:59:59.999999 on the previous month's last day —
    exactly the P13.11 FY-end pinning convention
    (datetime.combine(period_end, time.max, UTC)) — lands in the
    PREVIOUS bucket; one at 00:00:00 on the 1st lands in the CURRENT
    bucket. A reversal subtracts in the month it posts.

    Oracle: prev month deposits = 100.00; current month
      = 200.00 + 300.00 - 300.00 (reversal) = 200.00;
    prev month disbursements = 5000.00 (raw-seeded LN- transaction)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m1 = await _seed_member(tid)
        now = datetime.now(UTC)
        first_of_current = datetime(now.year, now.month, 1, tzinfo=UTC)
        prev_month_end = datetime.combine(
            (first_of_current - timedelta(days=1)).date(), time.max, tzinfo=UTC
        )
        async with tenant_session(factory(), tid) as session:
            await post_deposit(
                session,
                tid,
                m1,
                Decimal("100.00"),
                Channel.MPESA,
                admin_id,
                occurred_at=prev_month_end,
            )
            await post_deposit(
                session,
                tid,
                m1,
                Decimal("200.00"),
                Channel.MPESA,
                admin_id,
                occurred_at=first_of_current,
            )
            reversed_txn = await post_deposit(
                session, tid, m1, Decimal("300.00"), Channel.MPESA, admin_id
            )
        async with tenant_session(factory(), tid) as session:
            await post_reversal(session, tid, reversed_txn.txn_id, admin_id)
        # Disbursement leg of the series: a raw LN- transaction pinned
        # into the previous month (the series reads transactions only).
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO transactions (id, tenant_id, txn_ref, member_id, type, "
                    "amount, channel, occurred_at) VALUES (CAST(:id AS uuid), "
                    "CAST(:tid AS uuid), :ref, CAST(:m AS uuid), 'loan_disbursement', "
                    "'5000.00', 'bank', :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "ref": f"LN-TEST-{uuid.uuid4().hex[:8]}",
                    "m": str(m1),
                    "ts": prev_month_end,
                },
            )

        body = await _get_summary(token)
        flows = {f["month"]: f for f in body["monthly_flows"]}
        prev_key = f"{prev_month_end.year:04d}-{prev_month_end.month:02d}"
        cur_key = f"{now.year:04d}-{now.month:02d}"
        assert prev_key != cur_key
        assert flows[prev_key]["deposits"] == "100.00"
        assert flows[prev_key]["disbursements"] == "5000.00"
        assert flows[cur_key]["deposits"] == "200.00"
        assert flows[cur_key]["disbursements"] == "0"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — free-capacity consistency with the enforcement path
# ---------------------------------------------------------------------------


def test_fm5_free_capacity_matches_pledge_path_at_the_boundary() -> None:
    """The dashboard must use live_pledged_total and the SAME status set
    ('pledged','active') as P9 enforcement — a divergence is how a
    member is shown capacity the pledge endpoint then refuses.

    Oracle: deposit 10000.00; live pledges = 4000.00 (pledged, not yet
    consented) + 2500.00 (consented -> active) = 6500.00; free
    capacity = 10000.00 - 6500.00 = 3500.00. An actual pledge of
    exactly 3500.00 succeeds; one more cent is refused. Filtering only
    'active' (a forked status set) would report 7500.00 free and fail
    both assertions."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        guarantor = await _seed_member(tid, deposit="10000.00", name="Guarantor")
        borrower = await _seed_member(tid, deposit="9000.00", name="Borrower")
        pid = await _seed_product(tid)
        a1 = await _seed_application(tid, borrower, pid, amount="20000.00")
        a2 = await _seed_application(tid, borrower, pid, amount="20000.00")
        a3 = await _seed_application(tid, borrower, pid, amount="20000.00")
        await _pledge(tid, admin_id, application_id=a1, guarantor=guarantor, amount="4000.00")
        await _pledge(
            tid,
            admin_id,
            application_id=a2,
            guarantor=guarantor,
            amount="2500.00",
            consent=True,
        )

        body = await _get_summary(token)
        rows = {g["member_id"]: g for g in body["guarantors"]["guarantors"]}
        assert rows[str(guarantor)]["pledged_total"] == "6500.00"
        assert rows[str(guarantor)]["free_capacity"] == "3500.00"
        # Cross-check against the single capacity implementation.
        async with tenant_session(factory(), tid) as session:
            live = await guarantees_service.live_pledged_total(session, tid, guarantor)
        assert live == Decimal("6500.00")

        # The enforcement path accepts EXACTLY the advertised capacity...
        await _pledge(tid, admin_id, application_id=a3, guarantor=guarantor, amount="3500.00")
        after = await _get_summary(token)
        after_rows = {g["member_id"]: g for g in after["guarantors"]["guarantors"]}
        assert after_rows[str(guarantor)]["free_capacity"] == "0.00"
        # ...and refuses one cent more (least-disclosure envelope: the
        # capacity figure itself is never echoed by the refusal).
        with pytest.raises(ConflictError) as exc_info:
            async with tenant_session(factory(), tid) as session:
                await guarantees_service.pledge_guarantee(
                    session,
                    tid,
                    admin_id,
                    application_id=a3,
                    guarantor_member_id=guarantor,
                    amount=Decimal("0.01"),
                )
        assert "0.00" not in str(exc_info.value).replace("0.01", "")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — bounded scans, hard caps, no N+1
# ---------------------------------------------------------------------------


class _QueryCounter:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, *args: Any) -> None:
        self.value += 1


async def _counted_summary_queries(token: str) -> int:
    engine = get_engine(os.environ["DATABASE_URL"])
    counter = _QueryCounter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        await _get_summary(token)
        return counter.value
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)


def test_fm6_query_count_is_flat_as_data_grows() -> None:
    """P8 precedent: the composite issues a CONSTANT number of queries
    regardless of row counts (guarantor count held fixed — the only
    data-driven loop is capped by server config, exercised below)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m1 = await _seed_member(tid)
        await _deposit(tid, admin_id, m1, "100.00")
        pid = await _seed_product(tid)
        a1 = await _seed_application(tid, m1, pid)
        m2 = await _seed_member(tid, deposit="900.00")
        await _pledge(tid, admin_id, application_id=a1, guarantor=m2, amount="50.00")
        small = await _counted_summary_queries(token)
        assert small > 0
        for _ in range(10):
            extra = await _seed_member(tid)
            await _deposit(tid, admin_id, extra, "10.00")
            await _seed_application(tid, extra, pid)
        large = await _counted_summary_queries(token)
        assert small == large, f"query count grew with rows: {small} -> {large}"

    asyncio.run(run())


def test_fm6_guarantor_list_hard_cap_and_month_window_clamp() -> None:
    """The guarantor list obeys the server-resolved cap (largest
    exposure first) and the month window clamps to MAX_SERIES_MONTHS —
    no caller-controlled or unbounded scan exists on this surface.

    Oracle: 3 guarantors pledging 300/200/100; cap 2 -> exactly the
    300.00 and 200.00 rows, in that order."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        borrower = await _seed_member(tid, deposit="5000.00")
        pid = await _seed_product(tid)
        aid = await _seed_application(tid, borrower, pid, amount="15000.00")
        amounts = ("300.00", "200.00", "100.00")
        for amount in amounts:
            g = await _seed_member(tid, deposit="1000.00")
            await _pledge(tid, admin_id, application_id=aid, guarantor=g, amount=amount)
        async with tenant_session(factory(), tid) as session:
            summary = await dashboard_service.dashboard_summary(
                session, tid, granted=frozenset({Module.APPLICATIONS}), guarantor_cap=2
            )
        assert summary.guarantors is not None
        assert summary.guarantors.distinct_guarantors == 3
        assert summary.guarantors.total_pledged == Decimal("600.00")
        assert [g.pledged_total for g in summary.guarantors.guarantors] == [
            Decimal("300.00"),
            Decimal("200.00"),
        ]
        async with tenant_session(factory(), tid) as session:
            clamped = await dashboard_service.dashboard_summary(
                session, tid, granted=frozenset({Module.TRANSACTIONS}), months=999
            )
        assert clamped.monthly_flows is not None
        assert len(clamped.monthly_flows) == dashboard_service.MAX_SERIES_MONTHS

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Authn edge: no token -> 401 envelope, no figures
# ---------------------------------------------------------------------------


def test_dashboard_requires_authentication() -> None:
    async def run() -> None:
        async with api_client() as client:
            res = await client.get("/dashboard/summary")
        assert res.status_code == 401
        assert set(res.json().keys()) == {"category", "correlation_id"}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Snapshot honesty: as_of is a point-in-time marker, fresh per request
# ---------------------------------------------------------------------------


def test_dashboard_has_no_cache_two_reads_see_committed_change() -> None:
    """Staleness honesty: there is NO caching layer — a committed change
    between two requests is visible in the second (a memoizing
    implementation would fail this)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        m1 = await _seed_member(tid)
        await _deposit(tid, admin_id, m1, "100.00")
        first = await _get_summary(token)
        assert first["deposits"]["total_deposits"] == "100.00"
        await _deposit(tid, admin_id, m1, "50.00")
        second = await _get_summary(token)
        assert second["deposits"]["total_deposits"] == "150.00"
        assert second["as_of"] >= first["as_of"]

    asyncio.run(run())
