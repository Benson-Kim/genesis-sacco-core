"""Per-KPI trend series (#31 batch 8, ledger (k)) — GET /dashboard/summary.

The authorized expand-only contract change recorded by the human
review of !76 / issue #32: the composite dashboard read gains a
kpi_trends slice serving bounded monthly series for the PAR-30 and
active-members KPI cards. SERVER-computed end-to-end (the #32
route-(a) precedent).

Accounting-truth posture under test (each leg falsifiable):
  * PAR-30 points are RECOMPUTED from posting-history truth
    (reconstruct_month at the code-owned PAR_DPD_DAYS=30 threshold) —
    the seeded loan's MUTABLE days_past_due/classification columns are
    left at their defaults (0 / normal), so an implementation that
    reads the snapshot columns (§1.5 exploit class) or the NPL 90-day
    threshold yields 0.00 where the oracle demands 100.00.
  * Members points derive from STATUS-TRANSITION FACTS (append-only
    audit_log 'member.status' rows + members.created_at) — a dormant
    member's CURRENT status column says dormant, yet the cutoff before
    their transition fact must count them active; reading the mutable
    column fails that oracle.
  * Every oracle is hand-computed in comments from the seeds (§4).

Requires a migrated PostgreSQL database (DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor
from genesis.application import dashboard as dashboard_service
from genesis.domain.rbac import Module
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_ends_back(n: int) -> list[date]:
    """The last n fully elapsed calendar month ends, oldest first
    (the P13.17a test convention)."""
    today = datetime.now(UTC).date()
    ends: list[date] = []
    cursor = today.replace(day=1) - timedelta(days=1)
    for _ in range(n):
        ends.append(cursor)
        cursor = cursor.replace(day=1) - timedelta(days=1)
    ends.reverse()
    return ends


async def _get_summary(token: str) -> dict[str, Any]:
    async with api_client() as client:
        res = await client.get("/dashboard/summary", headers=_headers(token))
    assert res.status_code == 200, res.text
    return dict(res.json())


async def _seed_par_loan(tid: uuid.UUID) -> tuple[date, date]:
    """One loan whose PAR-30 state is hand-computable per month end.

    m0, m1 = the last two fully elapsed month ends. Principal
    10000.00, disbursed (m1 - 70 days), single installment 5000.00 due
    (m1 - 35 days), NO repayments. days past due at a cutoff = cutoff
    minus due date:

      m0: dpd = 35 - (m1 - m0 = 28..31 days) = 4..7  -> <= 30, clean.
      m1: dpd = 35                                   -> > 30, PAR;
                                                        <= 90, NOT NPL.
      now: dpd = 35 + days since m1                  -> > 30, PAR.

    PAR-30 ratio oracle: m0 '0.00'; m1 and the current month '100.00'
    (the flagged loan IS the whole book). The NPL threshold (90) would
    yield '0.00' at m1 — running the series at the wrong classify()
    threshold fails this oracle. The loans row keeps days_past_due=0 /
    classification='normal' (defaults; the arrears job never ran), so
    a snapshot-column implementation also fails it.
    """
    m0, m1 = _month_ends_back(2)
    member_id, product_id, app_id, loan_id = (uuid.uuid4() for _ in range(4))
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'PAR Member')"
            ),
            {"id": str(member_id), "tid": str(tid), "no": f"GP-{member_id.hex[:6].upper()}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, '12.00', '3.00', 36)"
            ),
            {"id": str(product_id), "tid": str(tid), "name": f"KPI-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, "
                " rate_pct, stage, cover_pct) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '10000.00', 12, '12.00', 'disbursed', '0.00')"
            ),
            {"id": str(app_id), "tid": str(tid), "mid": str(member_id), "pid": str(product_id)},
        )
        await session.execute(
            text(
                "INSERT INTO loans "
                "(id, tenant_id, application_id, member_id, product_id, principal, "
                " balance, rate_pct, term_months, disbursed_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:aid AS uuid), "
                "CAST(:mid AS uuid), CAST(:pid AS uuid), '10000.00', '10000.00', "
                "'12.00', 12, :disbursed)"
            ),
            {
                "id": str(loan_id),
                "tid": str(tid),
                "aid": str(app_id),
                "mid": str(member_id),
                "pid": str(product_id),
                "disbursed": datetime.combine(m1 - timedelta(days=70), time(12, 0), tzinfo=UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO loan_schedules "
                "(id, tenant_id, loan_id, installment_no, due_date, "
                " principal_due, interest_due, total_due) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:lid AS uuid), "
                "1, :due, 5000, 0, 5000)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "lid": str(loan_id),
                "due": m1 - timedelta(days=35),
            },
        )
    return m0, m1


async def _seed_member_history(tid: uuid.UUID) -> tuple[date, date]:
    """Four members whose per-cutoff active counts are hand-computable
    from created_at + append-only member.status facts.

    m0, m1 = the last two fully elapsed month ends.
      A: created (m0 - 10d), no transitions          -> active at m0, m1, now.
      B: created (m0 - 10d), fact 'exited' (m1 - 5d) -> active at m0 ONLY.
      C: created (m1 - 3d)                            -> active at m1, now
                                                        (did not exist at m0).
      D: created (m0 - 10d), fact 'dormant' (m0 + 2d) -> active at m0
                                                        (the fact lands
                                                        AFTER the m0
                                                        cutoff), not at
                                                        m1 or now.
    Oracle: active(m0) = A, B, D = 3; active(m1) = A, C = 2;
    active(now) = A, C = 2. The members.status COLUMN is updated to
    the terminal states (exited/dormant), so an implementation reading
    the mutable column instead of the facts sees 2 at m0 and fails.
    """
    m0, m1 = _month_ends_back(2)

    async def member(created: datetime, status: str, name: str) -> uuid.UUID:
        mid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members "
                    "(id, tenant_id, member_no, type, name, status, created_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', :name, "
                    ":st, :created)"
                ),
                {
                    "id": str(mid),
                    "tid": str(tid),
                    "no": f"GP-{mid.hex[:6].upper()}",
                    "name": name,
                    "st": status,
                    "created": created,
                },
            )
        return mid

    async def status_fact(mid: uuid.UUID, at: datetime, before: str, after: str) -> None:
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO audit_log "
                    "(tenant_id, actor_id, action, entity, entity_id, before, after, at) "
                    "VALUES (CAST(:tid AS uuid), NULL, 'member.status', 'members', :eid, "
                    "CAST(:before AS jsonb), CAST(:after AS jsonb), :at)"
                ),
                {
                    "tid": str(tid),
                    "eid": str(mid),
                    "before": f'{{"status": "{before}"}}',
                    "after": f'{{"status": "{after}"}}',
                    "at": at,
                },
            )

    noon = time(12, 0)
    created_early = datetime.combine(m0 - timedelta(days=10), noon, tzinfo=UTC)
    await member(created_early, "active", "A Stays")
    b = await member(created_early, "exited", "B Exits")
    b_exit_at = datetime.combine(m1 - timedelta(days=5), noon, tzinfo=UTC)
    await status_fact(b, b_exit_at, "active", "exited")
    await member(datetime.combine(m1 - timedelta(days=3), noon, tzinfo=UTC), "active", "C Joins")
    d = await member(created_early, "dormant", "D Sleeps")
    d_sleep_at = datetime.combine(m0 + timedelta(days=2), noon, tzinfo=UTC)
    await status_fact(d, d_sleep_at, "active", "dormant")
    return m0, m1


# ---------------------------------------------------------------------------
# PAR-30 series — posting-history truth at the classify() PAR threshold
# ---------------------------------------------------------------------------


def test_par30_trend_recomputes_from_posting_history_at_the_par_threshold() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        m0, m1 = await _seed_par_loan(tid)
        body = await _get_summary(token)

        trends = body["kpi_trends"]
        points = {p["month"]: p for p in trends["par30"]}
        # Hand-computed (see _seed_par_loan): clean at m0, the whole
        # book PAR-flagged at m1 and now. Values are decimal STRINGS
        # serialized verbatim.
        assert points[_month_key(m0)]["ratio_pct"] == "0.00"
        assert points[_month_key(m1)]["ratio_pct"] == "100.00"
        today = datetime.now(UTC).date()
        assert points[_month_key(today)]["ratio_pct"] == "100.00"
        # Geometry: share of the window peak (100.00) — integers 0..100.
        assert points[_month_key(m0)]["pct"] == 0
        assert points[_month_key(m1)]["pct"] == 100
        # The MUTABLE snapshot columns still say the book is clean
        # (days_past_due=0/'normal' defaults — the arrears job never
        # ran): the sibling loan_book slice reports 0.00 while the
        # reconstruction reports the truth. This is the documented
        # §1.5 distinction — a series pointed at the snapshot columns
        # would echo the 0.00 and fail the oracles above.
        assert body["loan_book"]["par30_ratio_pct"] == "0.00"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Members series — status-transition facts, never the mutable column
# ---------------------------------------------------------------------------


def test_members_trend_derives_from_status_transition_facts() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        m0, m1 = await _seed_member_history(tid)
        body = await _get_summary(token)

        points = {p["month"]: p for p in body["kpi_trends"]["members"]}
        # Hand-computed (see _seed_member_history): 3 at m0 (A, B, D —
        # D's dormancy fact lands after the m0 cutoff), 2 at m1 and
        # now (A, C). Reading the mutable status column would say 2 at
        # m0 (B exited, D dormant TODAY) — that implementation fails
        # here.
        assert points[_month_key(m0)]["active"] == 3
        assert points[_month_key(m1)]["active"] == 2
        today = datetime.now(UTC).date()
        assert points[_month_key(today)]["active"] == 2
        # Geometry: peak 3 -> m0 is 100; 2/3 rounds HALF_UP to 67.
        assert points[_month_key(m0)]["pct"] == 100
        assert points[_month_key(m1)]["pct"] == 67

    asyncio.run(run())


def test_members_trend_audit_coverage_boundary_is_honest() -> None:
    """Audit-coverage boundary (review R5), hand-computed: ONE member
    created (m1 - 3d) with ZERO 'member.status' facts ever recorded —
    the member predates status-audit coverage entirely.

      * m0 (cutoff BEFORE created_at): the member is ABSENT from the
        count — active(m0) = 0 is the tenant's true empty state at
        that cutoff, never an invented/backfilled point for a member
        who did not yet exist.
      * m1 and now (created before the cutoff, NO facts): the honest
        COALESCE fallback counts them active — members are created
        ACTIVE (the P8 invariant create_member hardcodes) and every
        status writer records a 'member.status' fact, so fact-absence
        means never-transitioned; the fallback agrees with the
        member's current status column ('active') by construction.
        active(m1) = 1, active(now) = 1.

    Falsifiable BOTH ways: dropping the COALESCE fallback (counting
    only members WITH transition facts) reads 0 at m1/now and fails;
    dropping the created_at predicate (backfilling the member into
    months before they existed) reads 1 at m0 and fails."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        m0, m1 = _month_ends_back(2)
        mid = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members "
                    "(id, tenant_id, member_no, type, name, status, created_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                    "'Coverage Boundary', 'active', :created)"
                ),
                {
                    "id": str(mid),
                    "tid": str(tid),
                    "no": f"GP-{mid.hex[:6].upper()}",
                    "created": datetime.combine(m1 - timedelta(days=3), time(12, 0), tzinfo=UTC),
                },
            )
        body = await _get_summary(token)

        points = {p["month"]: p for p in body["kpi_trends"]["members"]}
        assert points[_month_key(m0)]["active"] == 0
        assert points[_month_key(m1)]["active"] == 1
        today = datetime.now(UTC).date()
        assert points[_month_key(today)]["active"] == 1
        # Geometry documents the same boundary: window peak 1 ->
        # 0 at m0, 100 at m1 and the current month.
        assert points[_month_key(m0)]["pct"] == 0
        assert points[_month_key(m1)]["pct"] == 100
        assert points[_month_key(today)]["pct"] == 100

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Deny by default — each series follows its KPI card's parent grant
# ---------------------------------------------------------------------------


def test_kpi_trends_follow_parent_grants_per_role() -> None:
    """Teller (members:view, NO loan_book:view) receives ONLY the
    members series; Credit Committee (loan_book:view, NO members:view)
    receives ONLY par30. Ungranted series are ABSENT — never zeroed,
    never empty-listed (a shape is a disclosure). Removing the
    per-series grant gating in dashboard_summary fails this."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        await _seed_par_loan(tid)

        _, teller_token = await add_user(tid, "Teller")
        teller = await _get_summary(teller_token)
        assert "members" in teller["kpi_trends"]
        assert "par30" not in teller["kpi_trends"]
        # Least disclosure: no PAR figure appears anywhere in the
        # teller's composite.
        assert "ratio_pct" not in str(teller)

        _, committee_token = await add_user(tid, "Credit Committee")
        committee = await _get_summary(committee_token)
        assert "par30" in committee["kpi_trends"]
        assert "members" not in committee["kpi_trends"]

    asyncio.run(run())


def test_kpi_trends_slice_absent_when_neither_parent_granted() -> None:
    """Service-level deny-by-default: with only applications:view
    granted the kpi_trends slice is None (omitted from the response
    entirely), not an empty shell."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        async with tenant_session(factory(), tid) as session:
            summary = await dashboard_service.dashboard_summary(
                session, tid, granted=frozenset({Module.APPLICATIONS})
            )
        assert summary.kpi_trends is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Window bound — server-owned, hard-capped, never caller-tunable
# ---------------------------------------------------------------------------


def test_kpi_trend_window_is_hard_capped_at_twelve_points() -> None:
    """months=999 (only reachable server-side; the route passes no
    widths) clamps the FLOW series to MAX_SERIES_MONTHS but the KPI
    series to KPI_TREND_MAX_MONTHS=12 — the documented bound."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        async with tenant_session(factory(), tid) as session:
            summary = await dashboard_service.dashboard_summary(
                session,
                tid,
                granted=frozenset({Module.MEMBERS, Module.LOAN_BOOK}),
                months=999,
            )
        assert summary.kpi_trends is not None
        assert summary.kpi_trends.par30 is not None
        assert summary.kpi_trends.members is not None
        assert len(summary.kpi_trends.par30) == dashboard_service.KPI_TREND_MAX_MONTHS
        assert len(summary.kpi_trends.members) == dashboard_service.KPI_TREND_MAX_MONTHS

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Cross-tenant — explicit predicates on top of RLS (issue-#17 probe)
# ---------------------------------------------------------------------------


def test_kpi_trends_foreign_tenant_argument_yields_zero_series() -> None:
    """Session pinned to tenant A, tenant-B argument: the explicit
    bound tenant predicates must return the honest empty series (all
    zeros), never tenant B's history — even if RLS were misconfigured
    (defence in depth, gate 1.6 v1.1)."""

    async def run() -> None:
        tid_a, _, _ = await seed_actor()
        tid_b, _, _ = await seed_actor()
        await _seed_par_loan(tid_b)
        await _seed_member_history(tid_b)
        async with tenant_session(factory(), tid_a) as session:
            summary = await dashboard_service.dashboard_summary(
                session,
                tid_b,
                granted=frozenset({Module.MEMBERS, Module.LOAN_BOOK}),
            )
        assert summary.kpi_trends is not None
        assert summary.kpi_trends.par30 is not None
        assert summary.kpi_trends.members is not None
        assert all(p.ratio_pct == 0 for p in summary.kpi_trends.par30)
        assert all(p.active == 0 for p in summary.kpi_trends.members)

    asyncio.run(run())
