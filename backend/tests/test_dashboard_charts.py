"""Issue #32 (#31 batch 5, U3+X4) — dashboard chart-series read model.

The RECORDED #32 design decision, route (a): chart geometry is
SERVER-computed (Decimal end-to-end) from figures the composite already
fetched in the same REPEATABLE READ snapshot; the client renders the
integer percentages as visual scale only. No new SQL ships — the pure
functions are unit-tested against hand-computed oracles and the API
slice is walked for permission presence/absence.

Failure modes, one falsifiable test each:

  FM-C1 normalization drift — scale_pct matrix (zero/negative/ceiling/
        rounding) hand-computed; flows/portfolio oracles to the digit.
  FM-C2 negative-month leak — a reversal-only month must clamp to 0 in
        the geometry while the SIGNED figure stays in monthly_flows.
  FM-C3 permission bypass — charts sub-slices must follow their parent
        grants exactly (Teller: flows only; Credit Committee: portfolio
        only; neither parent -> charts key ABSENT, never zeroed).
  FM-C4 series misalignment — chart months must be the SAME keys, in
        the SAME order, as the sibling monthly_flows series.
  FM-C5 figure reconstruction — the charts slice carries ONLY integer
        percentages and the single axis_max scale string; no other
        money-derived value may appear.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor
from genesis.application import dashboard as dashboard_service
from genesis.application import transactions as txn_service
from genesis.application.loans import ClassificationSlice, PortfolioSummary
from genesis.domain.ledger import Channel
from genesis.domain.lending import LoanClass
from genesis.domain.rbac import Module
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _get_summary(token: str) -> dict[str, Any]:
    async with api_client() as client:
        res = await client.get("/dashboard/summary", headers=_headers(token))
    assert res.status_code == 200, res.text
    return dict(res.json())


def _flow(month: str, deposits: str, disbursements: str) -> dashboard_service.MonthlyFlow:
    return dashboard_service.MonthlyFlow(
        month=month, deposits=Decimal(deposits), disbursements=Decimal(disbursements)
    )


def _book(
    outstanding: str,
    npl: str,
    slices: list[tuple[LoanClass, str]],
) -> PortfolioSummary:
    return PortfolioSummary(
        active_loans=len(slices),
        outstanding_balance=Decimal(outstanding),
        npl_balance=Decimal(npl),
        npl_ratio_pct=Decimal("0.00"),
        par30_balance=Decimal("0"),
        par30_ratio_pct=Decimal("0.00"),
        provisions=Decimal("0"),
        penalties_due=Decimal("0"),
        by_classification=[
            ClassificationSlice(
                classification=cls, count=1, balance=Decimal(bal), provisions=Decimal("0")
            )
            for cls, bal in slices
        ],
    )


# ---------------------------------------------------------------------------
# FM-C1 — normalization oracles (pure functions, hand-computed)
# ---------------------------------------------------------------------------


def test_scale_pct_matrix_hand_computed() -> None:
    """Every branch of the scale, to the digit (never captured from the
    implementation): zero/negative values and non-positive ceilings are
    0; value == ceiling is 100; defensive above-ceiling clamps to 100;
    125/1000 = 12.5% rounds HALF-UP to 13 while 124/1000 = 12.4% rounds
    to 12; 1/3 of 3 is 33.33..% -> 33."""
    cases: list[tuple[str, str, int]] = [
        ("0", "100.00", 0),
        ("0.00", "100.00", 0),
        ("-50.00", "100.00", 0),  # reversal-only month clamps (FM-C2)
        ("50.00", "0", 0),  # empty window: no fabricated bar
        ("50.00", "-1.00", 0),  # defensive: negative ceiling
        ("100.00", "100.00", 100),
        ("150.00", "100.00", 100),  # defensive clamp above the ceiling
        ("125.00", "1000.00", 13),  # 12.5 -> HALF-UP -> 13
        ("124.00", "1000.00", 12),  # 12.4 -> 12
        ("1.00", "3.00", 33),  # 33.33.. -> 33
        ("2.00", "3.00", 67),  # 66.66.. -> 67
        ("0.01", "10000.00", 0),  # 0.0001% -> 0 (too coarse to leak)
    ]
    for value, ceiling, expected in cases:
        got = dashboard_service.scale_pct(Decimal(value), Decimal(ceiling))
        assert got == expected, f"scale_pct({value}, {ceiling}) = {got}, want {expected}"


def test_flows_chart_oracle_common_ceiling_and_negative_clamp() -> None:
    """Hand-computed: window peak = 200.00 (a DEPOSITS value), shared by
    both series. Month 1: 100/200=50, 50/200=25. Month 2 nets negative
    (reversal-only) -> 0 geometry (FM-C2). Month 3: 200/200=100,
    150/200=75. axis_max is the Decimal peak verbatim."""
    chart = dashboard_service.flows_chart(
        (
            _flow("2026-05", "100.00", "50.00"),
            _flow("2026-06", "-25.00", "0"),
            _flow("2026-07", "200.00", "150.00"),
        )
    )
    assert chart.axis_max == Decimal("200.00")
    assert [(m.month, m.deposits_pct, m.disbursements_pct) for m in chart.months] == [
        ("2026-05", 50, 25),
        ("2026-06", 0, 0),
        ("2026-07", 100, 75),
    ]


def test_flows_chart_all_zero_window_is_flat_zero() -> None:
    """A silent window renders NO bars (all 0) with axis_max 0 — never a
    fabricated full-height series (division-by-zero guard)."""
    chart = dashboard_service.flows_chart(
        (_flow("2026-06", "0.00", "0.00"), _flow("2026-07", "0", "0"))
    )
    assert chart.axis_max == Decimal("0")
    assert all(m.deposits_pct == 0 and m.disbursements_pct == 0 for m in chart.months)


def test_portfolio_chart_oracle_donut_complement_and_class_shares() -> None:
    """Hand-computed on outstanding 8000.00: npl 2000.00 -> 25%,
    performing = exact complement 75%. Classification shares:
    normal 5000/8000 = 62.5% -> HALF-UP 63; watch 1000/8000 = 12.5%
    -> 13; loss 2000/8000 = 25% -> 25 (deliberately NON-ADDITIVE with
    rounding: 63+13+25=101 — bars are independent, never summed)."""
    chart = dashboard_service.portfolio_chart(
        _book(
            "8000.00",
            "2000.00",
            [
                (LoanClass.NORMAL, "5000.00"),
                (LoanClass.WATCH, "1000.00"),
                (LoanClass.LOSS, "2000.00"),
            ],
        )
    )
    assert chart.npl_pct == 25
    assert chart.performing_pct == 75
    assert chart.performing_pct + chart.npl_pct == 100
    assert [(s.classification, s.pct) for s in chart.classification] == [
        ("normal", 63),
        ("watch", 13),
        ("loss", 25),
    ]


def test_portfolio_chart_empty_book_is_honest_zero() -> None:
    """Outstanding 0 -> 0/0 donut (the client renders the empty state,
    never a fabricated 100% ring) and zero classification shares."""
    chart = dashboard_service.portfolio_chart(_book("0", "0", [(LoanClass.NORMAL, "0")]))
    assert chart.performing_pct == 0
    assert chart.npl_pct == 0
    assert [s.pct for s in chart.classification] == [0]


def test_dashboard_charts_assembly_follows_parent_slices() -> None:
    """None/None -> None (the charts KEY is omitted, deny-by-default);
    a single granted parent yields exactly its sub-slice."""
    assert dashboard_service.dashboard_charts(None, None) is None
    flows_only = dashboard_service.dashboard_charts((_flow("2026-07", "10.00", "0"),), None)
    assert flows_only is not None
    assert flows_only.flows is not None
    assert flows_only.portfolio is None
    book_only = dashboard_service.dashboard_charts(None, _book("100.00", "0", []))
    assert book_only is not None
    assert book_only.flows is None
    assert book_only.portfolio is not None


# ---------------------------------------------------------------------------
# API integration — end-to-end oracle + FM-C3/FM-C4/FM-C5
# ---------------------------------------------------------------------------


async def _seed_member(tid: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, status) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', 'Chart Member', 'active')"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}"},
        )
        for table in ("deposit_accounts", "share_accounts"):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '0')"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
            )
    return mid


def test_charts_flows_end_to_end_oracle_and_month_alignment() -> None:
    """End-to-end oracle (hand-computed): current-month deposit 100.00;
    previous-month raw disbursement 400.00 (the FM4 seeding precedent).
    Window peak = 400.00 -> axis_max "400.00"; previous month renders
    (0, 100), current (25, 0); every other month is (0, 0). FM-C4: the
    chart months are the SAME keys in the SAME order as monthly_flows.
    FM-C5: the seeded amounts appear ONLY in monthly_flows/axis_max —
    never inside the months geometry."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, admin_id, mid, amount=Decimal("100.00"), channel=Channel.MPESA
            )
        now = datetime.now(UTC)
        first_of_current = datetime(now.year, now.month, 1, tzinfo=UTC)
        prev_month_end = datetime.combine(
            (first_of_current - timedelta(days=1)).date(), time.max, tzinfo=UTC
        )
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO transactions (id, tenant_id, txn_ref, member_id, type, "
                    "amount, channel, occurred_at) VALUES (CAST(:id AS uuid), "
                    "CAST(:tid AS uuid), :ref, CAST(:m AS uuid), 'loan_disbursement', "
                    "'400.00', 'bank', :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "ref": f"LN-CHART-{uuid.uuid4().hex[:8]}",
                    "m": str(mid),
                    "ts": prev_month_end,
                },
            )

        body = await _get_summary(token)
        charts = body["charts"]
        flows = charts["flows"]
        assert flows["axis_max"] == "400.00"
        # FM-C4: 1:1 alignment with the sibling series.
        assert [m["month"] for m in flows["months"]] == [f["month"] for f in body["monthly_flows"]]
        by_month = {m["month"]: m for m in flows["months"]}
        prev_key = f"{prev_month_end.year:04d}-{prev_month_end.month:02d}"
        cur_key = f"{now.year:04d}-{now.month:02d}"
        assert by_month[prev_key]["deposits_pct"] == 0
        assert by_month[prev_key]["disbursements_pct"] == 100
        assert by_month[cur_key]["deposits_pct"] == 25  # 100/400
        assert by_month[cur_key]["disbursements_pct"] == 0
        for key, month in by_month.items():
            if key not in (prev_key, cur_key):
                assert month["deposits_pct"] == 0
                assert month["disbursements_pct"] == 0
        # FM-C5: geometry carries integers only — the months objects
        # hold exactly {month, deposits_pct, disbursements_pct} and no
        # decimal-string figure.
        for month in flows["months"]:
            assert set(month.keys()) == {"month", "deposits_pct", "disbursements_pct"}
            assert isinstance(month["deposits_pct"], int)
            assert isinstance(month["disbursements_pct"], int)
        assert set(flows.keys()) == {"months", "axis_max"}
        # Portfolio sub-slice rides loan_book:view (granted here): an
        # empty book is the honest 0/0 donut.
        assert charts["portfolio"]["performing_pct"] == 0
        assert charts["portfolio"]["npl_pct"] == 0

    asyncio.run(run())


def test_fm_c3_charts_sub_slices_follow_parent_grants() -> None:
    """Permission walk on the two roles that hold exactly ONE parent
    grant each: a Teller (transactions:view, no loan_book) receives
    flows but NO portfolio key; a Credit Committee member (loan_book,
    no transactions) receives portfolio but NO flows key. Ungranted
    sub-slices are ABSENT, never zeroed (gate 1.6)."""

    async def run() -> None:
        tid, _admin_id, _token = await seed_actor()
        _, teller_token = await add_user(tid, "Teller")
        _, committee_token = await add_user(tid, "Credit Committee")

        teller = await _get_summary(teller_token)
        assert "charts" in teller
        assert "flows" in teller["charts"]
        assert "portfolio" not in teller["charts"]

        committee = await _get_summary(committee_token)
        assert "charts" in committee
        assert "portfolio" in committee["charts"]
        assert "flows" not in committee["charts"]

    asyncio.run(run())


def test_fm_c3_charts_key_absent_when_neither_parent_granted() -> None:
    """Service-level (no seeded role holds neither parent): with only
    members:view granted the charts slice is None — the router then
    omits the key entirely (response_model_exclude_none)."""

    async def run() -> None:
        tid, _admin_id, _token = await seed_actor()
        async with tenant_session(factory(), tid) as session:
            summary = await dashboard_service.dashboard_summary(
                session, tid, granted=frozenset({Module.MEMBERS})
            )
        assert summary.charts is None

    asyncio.run(run())


def test_charts_cross_tenant_probe_yields_zero_geometry() -> None:
    """Issue-#17 probe: session AS tenant A, foreign tenant-B argument —
    the charts derive from the zeroed parent slices, so the geometry is
    flat zero with axis_max 0, never another tenant's shape."""

    async def run() -> None:
        tid_a, admin_a, _token = await seed_actor()
        mid = await _seed_member(tid_a)
        async with tenant_session(factory(), tid_a) as session:
            await txn_service.record_deposit(
                session, tid_a, admin_a, mid, amount=Decimal("750.00"), channel=Channel.MPESA
            )
        tid_b = uuid.uuid4()
        async with tenant_session(factory(), tid_a) as session:
            summary = await dashboard_service.dashboard_summary(
                session, tid_b, granted=frozenset({Module.TRANSACTIONS})
            )
        assert summary.charts is not None
        assert summary.charts.flows is not None
        assert summary.charts.flows.axis_max == Decimal("0")
        assert all(
            m.deposits_pct == 0 and m.disbursements_pct == 0 for m in summary.charts.flows.months
        )

    asyncio.run(run())
