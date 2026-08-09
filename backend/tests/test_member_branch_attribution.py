"""#31 ledger (j).2: member branch attribution read suite.

`branch_id` on the member read contract — an expand-only NULLABLE
field carrying the 0016 FK that ONLY the batch-4 assignment route
writes. NULL is the honest "unassigned" state (attribution is never
invented).

Covers (hand-computed oracles, MASTER_PROMPT §4):
- every member read (create response, detail, flat list, opted-in
  aggregates list) carries branch_id — NULL before assignment, the
  branch UUID after the REAL assignment route ran,
- key-exactness: the flat row and detail contracts are the batch-3
  shapes PLUS branch_id and nothing else — the aggregates object
  stays byte-identical (same four keys, same figures),
- the profile edit (PUT /members/{id}) never touches attribution.

Falsifiability: invent a default branch, drop the column from a
SELECT list, or let the profile edit clear the FK, and the exact
key-set / value assertions below fail.
"""

import asyncio
import os
import uuid

import pytest

from db_helpers import api_client
from export_helpers import seed_actor

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

FLAT_KEYS = {
    "id",
    "member_no",
    "type",
    "name",
    "phone",
    "email",
    "status",
    "version",
    "branch_id",
    # dividend_payout joined the flat contract with the authorized
    # #31 batch-8 expand (ledger (c)) — nullable-never-optional, so
    # the key is ALWAYS present (TEN keys).
    "dividend_payout",
}
AGGREGATE_KEYS = {
    "deposits_total",
    "shares_total",
    "loans_outstanding",
    "guarantees_pledged",
}


def test_member_reads_carry_nullable_branch_attribution() -> None:
    async def run() -> None:
        _, _, token = await seed_actor()
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            # Create: a new member starts UNASSIGNED — branch_id null,
            # never an invented default.
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Attribution Member"},
                headers=headers,
            )
            assert res.status_code == 201, res.text
            created = res.json()
            assert set(created.keys()) == FLAT_KEYS
            assert created["branch_id"] is None
            member_id = created["id"]

            # Detail: null attribution + the UNCHANGED batch-3
            # aggregates object (key-exact, zero-activity scale).
            res = await client.get(f"/members/{member_id}", headers=headers)
            assert res.status_code == 200, res.text
            detail = res.json()
            assert set(detail.keys()) == FLAT_KEYS | {"aggregates"}
            assert detail["branch_id"] is None
            assert set(detail["aggregates"].keys()) == AGGREGATE_KEYS
            assert detail["aggregates"]["deposits_total"] == "0.00"

            # Assign through the REAL batch-4 route (the column's sole
            # writer), then re-read: attribution appears verbatim.
            res = await client.post(
                "/branches",
                json={"name": f"Attr-{uuid.uuid4().hex[:8]}"},
                headers=headers,
            )
            assert res.status_code == 201, res.text
            branch_id = res.json()["id"]
            res = await client.put(
                f"/branches/{branch_id}/members/{member_id}",
                json={"version": detail["version"]},
                headers=headers,
            )
            assert res.status_code == 200, res.text

            res = await client.get(f"/members/{member_id}", headers=headers)
            assert res.status_code == 200, res.text
            assert res.json()["branch_id"] == branch_id

            # Flat list rows carry it too (key-exact)...
            res = await client.get("/members", headers=headers)
            assert res.status_code == 200, res.text
            rows = {m["id"]: m for m in res.json()["items"]}
            assert set(rows[member_id].keys()) == FLAT_KEYS
            assert rows[member_id]["branch_id"] == branch_id
            # ...and the opted-in aggregates rows: batch-3 contract
            # byte-identical otherwise (same aggregate keys/figures).
            res = await client.get("/members", params={"include": "aggregates"}, headers=headers)
            assert res.status_code == 200, res.text
            rows = {m["id"]: m for m in res.json()["items"]}
            assert set(rows[member_id].keys()) == FLAT_KEYS | {"aggregates"}
            assert rows[member_id]["branch_id"] == branch_id
            assert set(rows[member_id]["aggregates"].keys()) == AGGREGATE_KEYS

            # The profile edit is NOT an attribution writer: a rename
            # keeps the FK exactly as assigned.
            res = await client.put(
                f"/members/{member_id}",
                json={"version": 2, "name": "Attribution Member Renamed"},
                headers=headers,
            )
            assert res.status_code == 200, res.text
            assert res.json()["branch_id"] == branch_id

    asyncio.run(run())
