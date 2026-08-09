"""#31 ledger (g): double-entry legs drill-down read suite.

GET /transactions/{id}/legs (transactions:view) — the per-posting
DR/CR legs verbatim from the append-only ledger_entries truth:
account, side, amount as canonical decimal strings, one row per leg,
NOTHING derived (no netting, no totals — the 0004/0014 constraint
trigger proved balance at commit time; the web client renders rows
verbatim without a computed total).

Covers (hand-computed oracles, MASTER_PROMPT §4):
- multi-leg posting composition: a split-allocation repayment shape
  (1 DR + 2 CR) comes back as EXACTLY three verbatim rows in the
  documented total order (side DESC — debits first — then account,
  then id), amounts as canonical strings; the response carries items
  ONLY — no total, no net, no derived figure a client could echo,
- the simple 2-leg posting through the REAL posting service
  (ledger.post_deposit),
- route x every role (matrix expectations from the P4 seed),
- 404-BEFORE-FACTS: unknown ids and cross-tenant ids (hidden by RLS)
  surface 404, and NO rejection path (403/404) echoes an account or
  an amount (least disclosure, gate 1.6).

Falsifiability: reorder the ORDER BY, net the legs, add a derived
total, or drop the existence probe/tenant predicate, and the exact-
equality and rejection-echo assertions below fail.
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor, seed_member, seed_three_leg_repayment
from genesis.application.ledger import post_deposit
from genesis.domain.ledger import Channel
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def test_legs_multi_leg_composition_verbatim_and_ordered() -> None:
    """Hand-computed oracle: the 3-leg split repayment comes back as
    EXACTLY three verbatim rows — debits first (side DESC), then
    credits by account — and the response carries items ONLY: no
    total, no net, nothing derived (the non-additive contract the web
    fixture re-proves client-side)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Legs Member")
        txn_id = await seed_three_leg_repayment(tid, mid)
        async with api_client() as client:
            res = await client.get(
                f"/transactions/{txn_id}/legs",
                headers={"authorization": f"Bearer {token}"},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        # items ONLY — a derived total/net key would widen the contract.
        assert set(body.keys()) == {"items"}
        assert body["items"] == [
            {"account": "cash.mpesa", "side": "debit", "amount": "100.00"},
            {"account": "income.interest", "side": "credit", "amount": "40.00"},
            {"account": "loans.receivable", "side": "credit", "amount": "60.00"},
        ]
        # Every row is key-exact: account/side/amount, nothing more
        # (no member, actor or channel rides on a leg — least
        # disclosure; those live on the transaction row itself).
        for item in body["items"]:
            assert set(item.keys()) == {"account", "side", "amount"}

    asyncio.run(run())


def test_legs_of_real_service_posting() -> None:
    """The REAL posting path (ledger.post_deposit) yields the classic
    2-leg pair, verbatim: DR cash.mpesa / CR member.deposits at the
    posted amount."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Deposit Legs Member")
        async with tenant_session(factory(), tid) as session:
            result = await post_deposit(session, tid, mid, Decimal("250.50"), Channel.MPESA)
        async with api_client() as client:
            res = await client.get(
                f"/transactions/{result.txn_id}/legs",
                headers={"authorization": f"Bearer {token}"},
            )
        assert res.status_code == 200, res.text
        assert res.json()["items"] == [
            {"account": "cash.mpesa", "side": "debit", "amount": "250.50"},
            {"account": "member.deposits", "side": "credit", "amount": "250.50"},
        ]

    asyncio.run(run())


def test_legs_route_matrix_every_role() -> None:
    """GET /transactions/{id}/legs x every role — expectations from the
    P4 seed matrix (transactions x VIEW); NO rejection body echoes an
    account or a figure."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, _, _ = await seed_actor()
        mid = await seed_member(tid, name="Matrix Legs Member")
        txn_id = await seed_three_leg_repayment(tid, mid)
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            can_view = matrix[role_name][Module.TRANSACTIONS][Action.VIEW]
            async with api_client() as client:
                res = await client.get(
                    f"/transactions/{txn_id}/legs",
                    headers={"authorization": f"Bearer {token}"},
                )
            assert res.status_code == (200 if can_view else 403), role_name
            if not can_view:
                # Least disclosure: the refusal names neither an
                # account nor an amount (gate 1.6).
                assert "100.00" not in res.text, role_name
                assert "cash.mpesa" not in res.text, role_name

    asyncio.run(run())


def test_legs_404_before_facts_unknown_and_cross_tenant() -> None:
    """404-BEFORE-FACTS: an unknown id and a FOREIGN-tenant id (hidden
    by RLS + the explicit tenant predicate) both surface 404 — and the
    rejection echoes no account and no amount."""

    async def run() -> None:
        _, _, token_a = await seed_actor()
        tid_b, _, _ = await seed_actor()
        mid_b = await seed_member(tid_b, name="Foreign Legs Member")
        foreign_txn = await seed_three_leg_repayment(tid_b, mid_b)
        async with api_client() as client:
            headers = {"authorization": f"Bearer {token_a}"}
            res = await client.get(f"/transactions/{uuid.uuid4()}/legs", headers=headers)
            assert res.status_code == 404, res.text
            res = await client.get(f"/transactions/{foreign_txn}/legs", headers=headers)
            assert res.status_code == 404, res.text
            assert "100.00" not in res.text
            assert "cash.mpesa" not in res.text

    asyncio.run(run())


def test_legs_over_cap_guard_sanitized_500() -> None:
    """#31 remediation N3 — the hard defensive cap is FALSIFIABLE: a
    posting whose leg count exceeds MAX_TRANSACTION_LEGS (64) trips a
    sanitized 500 (internal_error category + correlation id) with NO
    account and NO figure echoed. Hand-computed oracle: 40 DR x 1.00 +
    40 CR x 1.00 = 80 balanced legs (DR total = CR total = 40.00 =
    transactions.amount, so the deferred 0004/0014 trigger accepts the
    commit — only direct SQL can build this; every real builder writes
    at most 7 legs). Remove the guard in transaction_legs and this
    test fails with a 200 carrying 80 rows."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name="Over Cap Legs Member")
        txn_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO transactions "
                    "(id, tenant_id, txn_ref, member_id, type, amount, channel) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
                    "CAST(:mid AS uuid), 'loan_repayment', 40.00, 'mpesa')"
                ),
                {
                    "id": str(txn_id),
                    "tid": str(tid),
                    "mid": str(mid),
                    "ref": f"RP-{txn_id.hex[:8]}",
                },
            )
            for account, side in (("cash.mpesa", "debit"), ("loans.receivable", "credit")):
                await session.execute(
                    text(
                        "INSERT INTO ledger_entries "
                        "(id, tenant_id, transaction_id, account, side, amount) "
                        "SELECT gen_random_uuid(), CAST(:tid AS uuid), "
                        "CAST(:txn AS uuid), :account, :side, 1.00 "
                        "FROM generate_series(1, 40)"
                    ),
                    {
                        "tid": str(tid),
                        "txn": str(txn_id),
                        "account": account,
                        "side": side,
                    },
                )
        async with api_client() as client:
            res = await client.get(
                f"/transactions/{txn_id}/legs",
                headers={"authorization": f"Bearer {token}"},
            )
        assert res.status_code == 500, res.text
        body = res.json()
        # Sanitized envelope ONLY: category + correlation id — no leg
        # rows, no account, no amount, no count (least disclosure).
        assert set(body.keys()) == {"category", "correlation_id"}
        assert body["category"] == "internal_error"
        assert "1.00" not in res.text
        assert "40.00" not in res.text
        assert "cash.mpesa" not in res.text
        assert "loans.receivable" not in res.text

    asyncio.run(run())
