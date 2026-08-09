"""#35 item 13 — GET /transactions `search` (ref prefix + member match).

Expand-only declared probe on the ledger listing: `txn_ref LIKE
:prefix` OR an EXISTS member match (member_no EXACT / name PREFIX).
Bound parameters only; LIKE metacharacters escaped code-side; keyset
order (occurred_at DESC, id DESC) preserved.

Falsifiable (§4, hand-computed oracles): the seeded tenant carries
Alice Wanjiku (GP-9001, TWO mpesa deposits) and Bob Otieno (GP-9002,
ONE bank deposit) — three postings total, so every leg's expected row
set is computable by hand and drops to "all three rows" if the search
clause is removed. The '%' inertness leg fails if the code-side LIKE
escaping is dropped (unescaped, '%' matches everything).

EXPLAIN structural gate (gate 1.3, the P13.9/N1 convention): the exact
production statement (TXN_SEARCH_CLAUSE composed by list_transactions'
own builder path) is index-served under enable_seqscan=off AND
enable_sort=off — no Seq Scan node anywhere (members is probed by
PRIMARY KEY inside the EXISTS; the driving walk rides the keyset
index), no Sort node (the order comes from the index). Artifact
captured to backend/perf/explain_txn_search.txt BEFORE any assertion.
"""

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import api_client, factory
from export_helpers import seed_actor, seed_member_no
from genesis.application import transactions as txn_service
from genesis.application.transactions import TXN_SEARCH_CLAUSE, _search_params
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_txn_search.txt"


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _open_accounts(tid: uuid.UUID, mid: uuid.UUID) -> None:
    async with tenant_session(factory(), tid) as session:
        for table in ("deposit_accounts", "share_accounts"):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), 0)"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
            )


async def _seed_search_tenant() -> tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]:
    """Alice (GP-9001, 2 mpesa deposits), Bob (GP-9002, 1 bank deposit)."""
    tid, _, token = await seed_actor()
    alice = await seed_member_no(tid, "GP-9001", name="Alice Wanjiku")
    bob = await seed_member_no(tid, "GP-9002", name="Bob Otieno")
    for mid in (alice, bob):
        await _open_accounts(tid, mid)
    async with tenant_session(factory(), tid) as session:
        await txn_service.record_deposit(
            session, tid, None, alice, amount=Decimal("100.00"), channel=Channel.MPESA
        )
        await txn_service.record_deposit(
            session, tid, None, alice, amount=Decimal("200.00"), channel=Channel.MPESA
        )
        await txn_service.record_deposit(
            session, tid, None, bob, amount=Decimal("300.00"), channel=Channel.BANK
        )
    return tid, token, alice, bob


def test_search_matches_ref_prefix_member_no_exact_and_name_prefix() -> None:
    async def run() -> None:
        _, token, alice, bob = await _seed_search_tenant()
        async with api_client() as client:
            # Baseline: three postings visible without search.
            res = await client.get("/transactions", headers=_headers(token))
            assert res.status_code == 200
            assert len(res.json()["items"]) == 3

            # Ref prefix, case-folded ("mp-" -> the two MP- rows).
            res = await client.get(
                "/transactions", params={"search": "mp-"}, headers=_headers(token)
            )
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 2
            assert all(i["txn_ref"].startswith("MP-") for i in items)
            assert all(i["member_id"] == str(alice) for i in items)

            # member_no EXACT (case-folded): Bob's single posting.
            res = await client.get(
                "/transactions", params={"search": "gp-9002"}, headers=_headers(token)
            )
            assert res.status_code == 200
            items = res.json()["items"]
            assert [i["member_id"] for i in items] == [str(bob)]

            # A member_no PREFIX is NOT a match (exactness is the
            # declared contract — names collide, numbers do not).
            res = await client.get(
                "/transactions", params={"search": "GP-900"}, headers=_headers(token)
            )
            assert res.status_code == 200
            assert res.json()["items"] == []

            # Name PREFIX, case-folded: "alice" -> Alice's two rows.
            res = await client.get(
                "/transactions", params={"search": "alice"}, headers=_headers(token)
            )
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 2
            assert all(i["member_id"] == str(alice) for i in items)

    asyncio.run(run())


def test_like_metacharacters_are_inert_and_keyset_walk_is_preserved() -> None:
    async def run() -> None:
        _, token, _alice, _bob = await _seed_search_tenant()
        async with api_client() as client:
            # '%' probes LITERALLY (escaped code-side): nothing matches.
            # Falsifiable: drop the escaping and '%' matches all three.
            res = await client.get("/transactions", params={"search": "%"}, headers=_headers(token))
            assert res.status_code == 200
            assert res.json()["items"] == []

            # Keyset walk UNDER the search filter: limit 1 pages through
            # Alice's two rows newest-first, no skip, no duplicate.
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(5):
                params: dict[str, object] = {"search": "alice", "limit": 1}
                if cursor:
                    params["cursor"] = cursor
                res = await client.get("/transactions", params=params, headers=_headers(token))
                assert res.status_code == 200
                body = res.json()
                seen.extend(i["id"] for i in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            assert len(seen) == 2
            assert len(set(seen)) == 2

    asyncio.run(run())


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_search_page_is_index_served_without_seq_scan_or_sort() -> None:
    """EXPLAIN structural gate: the production search page — the exact
    module-level TXN_SEARCH_CLAUSE inside the list statement shape —
    rides the keyset index with the members EXISTS as a PRIMARY-KEY
    probe: no Seq Scan anywhere, no Sort node (drop the keyset index
    or rewrite the EXISTS as an unanchored scan and this gate fails)."""

    async def run() -> None:
        tid, _, _, _ = await _seed_search_tenant()
        clauses = ["tenant_id = CAST(:tid AS uuid)", TXN_SEARCH_CLAUSE]
        params: dict[str, object] = {"tid": str(tid), "limit": 21}
        params.update(_search_params("alice"))
        sql = (
            # Static clause literals from production code; every value
            # is a bound parameter (mirrors list_transactions).
            "SELECT id, txn_ref, member_id, type, amount, channel, occurred_at, "  # noqa: S608
            "reversal_of_id, created_by, external_ref FROM transactions "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at DESC, id DESC LIMIT :limit"
        )
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            plan = await _explain(session, sql, params)

        # Artifact BEFORE assertions (CI job log + backend/perf/).
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            "#35 item 13 ledger search EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off AND enable_sort=off (tiny CI tables): the gate\n"
            "is structural — the keyset walk rides the transactions keyset\n"
            "index, members is probed by PRIMARY KEY inside the EXISTS, and\n"
            "no Seq Scan or Sort node appears anywhere in the plan.\n"
            f"\n=== search page (search='alice') ===\n{plan}\n"
        )

        assert "Seq Scan" not in plan, "search page fell back to a sequential scan"
        assert "Index" in plan, "search page is not index-served"
        assert "Sort" not in plan, "search page sorts instead of riding the keyset index"

    asyncio.run(run())
