"""#35 item 14 — GET /members member_no exact-match lookup (posting drawer).

Expand-only declared filter on the members register, served by the
0001 UNIQUE (tenant_id, member_no) key — no new index, no new
statement (the clause joins the shared _member_list_clauses builder).

Falsifiable (§4): two members are seeded per tenant, so every
exact-match leg returns EXACTLY ONE row — drop the member_no clause
from _member_list_clauses and each leg sees both rows and fails.
Least disclosure: an unknown number is an EMPTY 200 page (never 404 —
no existence oracle beyond the members:view grant the route already
requires); cross-tenant numbers resolve to ZERO rows (RLS + the
explicit tenant predicate).
"""

import asyncio
import os

import pytest

from db_helpers import api_client
from export_helpers import seed_actor, seed_member_no

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_member_no_lookup_exact_match_unknown_and_aggregates_paths() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        await seed_member_no(tid, "GP-7001", name="Lookup Target")
        await seed_member_no(tid, "GP-7002", name="Lookup Decoy")
        async with api_client() as client:
            # Exact match: EXACTLY the one row (falsifiable: without
            # the clause both seeded members come back).
            res = await client.get(
                "/members", params={"member_no": "GP-7001"}, headers=_headers(token)
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert [(m["member_no"], m["name"]) for m in body["items"]] == [
                ("GP-7001", "Lookup Target")
            ]
            assert body["next_cursor"] is None

            # Unknown number: an EMPTY page, never a 404 (least
            # disclosure — the route cannot become an existence probe
            # sharper than the register it already serves).
            res = await client.get(
                "/members", params={"member_no": "GP-0000"}, headers=_headers(token)
            )
            assert res.status_code == 200, res.text
            assert res.json() == {"items": [], "next_cursor": None}

            # The aggregates opt-in respects the same filter (ONE row,
            # aggregates object present per the include contract).
            res = await client.get(
                "/members",
                params={"member_no": "GP-7002", "include": "aggregates"},
                headers=_headers(token),
            )
            assert res.status_code == 200, res.text
            rows = res.json()["items"]
            assert [m["member_no"] for m in rows] == ["GP-7002"]
            assert "aggregates" in rows[0]

    asyncio.run(run())


def test_member_no_lookup_is_tenant_fenced() -> None:
    """Adversarial cross-tenant leg (§4): tenant B's number yields ZERO
    rows for tenant A — RLS plus the explicit tenant predicate."""

    async def run() -> None:
        tid_a, _, token_a = await seed_actor()
        tid_b, _, _ = await seed_actor()
        await seed_member_no(tid_b, "GP-8001", name="Foreign Member")
        del tid_a  # actor A's own tenant seeds no members at all
        async with api_client() as client:
            res = await client.get(
                "/members", params={"member_no": "GP-8001"}, headers=_headers(token_a)
            )
            assert res.status_code == 200, res.text
            assert res.json() == {"items": [], "next_cursor": None}

    asyncio.run(run())
