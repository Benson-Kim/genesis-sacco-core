"""#35 item 14 residual — GET /members id_number exact-match lookup.

Completes the posting-drawer unique-identifier story: the member_no
probe (remainder round) resolved the platform-issued number; this
probe resolves the person KYC profile's NATIONAL ID through
MEMBER_ID_NUMBER_LOOKUP_SQL, shipped in the SAME MR as its 0045
partial expression index (the house rule: no probe without its
index). Legs:

* resolve — a seeded ID number returns EXACTLY the hand-computed
  member (number + name); a decoy person with a different ID never
  rides along. Falsifiable: drop the id-number equality from the SQL
  and the decoy appears.
* honest miss — an unknown ID is an EMPTY page (200), never a 404,
  and the body echoes nothing about the probed value beyond the empty
  list (least disclosure / no existence oracle).
* tenant fence — another tenant's ID number resolves NOTHING despite
  existing (explicit tenant predicates on both relations + RLS).
* exclusivity — id_number combines with NO other parameter (422): one
  declared meaning, never an ambiguous merged scope.
* EXPLAIN structural gate — with enable_seqscan=off the production
  statement's plan carries ZERO Seq Scan and ZERO Sort nodes: the
  0045 expression index drives, members resolves by PRIMARY KEY.
  Falsifiable: DROP INDEX idx_member_profiles_id_number and the
  member_profiles entry point must seq-scan, failing the gate.

Oracles are hand-computed in comments (MASTER_PROMPT §4).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import seed_actor, seed_member_no
from genesis.application.members import MEMBER_ID_NUMBER_LOOKUP_SQL
from genesis.domain.members import MemberType
from genesis.infrastructure.tenancy import tenant_session
from kyc_payloads import VALID_PROFILES

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_idnumber_lookup.txt"


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _seed_person_with_profile(
    tid: uuid.UUID, *, member_no: str, name: str, id_number: str
) -> uuid.UUID:
    """Member row + a shape-valid person KYC profile (0018 CHECK) whose
    bio.id_number is the seeded value."""
    mid = await seed_member_no(tid, member_no, name=name)
    profile: dict[str, Any] = {
        **VALID_PROFILES[MemberType.PERSON],
        "bio": {**VALID_PROFILES[MemberType.PERSON]["bio"], "id_number": id_number},
    }
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO member_profiles "
                "(id, tenant_id, member_id, member_type, category, profile) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "'person', 'Ordinary', CAST(:profile AS jsonb))"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(tid),
                "mid": str(mid),
                "profile": json.dumps(profile),
            },
        )
    return mid


def test_seeded_id_number_resolves_the_hand_computed_member() -> None:
    """Hand-computed: ID 20735544 belongs to GP-7301 'Idno Target';
    the decoy GP-7302 carries 30846655 and must never appear.
    Falsifiable: dropping the id-number equality from
    MEMBER_ID_NUMBER_LOOKUP_SQL leaks the decoy and fails the exact
    single-row assert."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        await _seed_person_with_profile(
            tid, member_no="GP-7301", name="Idno Target", id_number="20735544"
        )
        await _seed_person_with_profile(
            tid, member_no="GP-7302", name="Idno Decoy", id_number="30846655"
        )
        async with api_client() as client:
            res = await client.get(
                "/members", params={"id_number": "20735544"}, headers=_headers(token)
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert [(m["member_no"], m["name"]) for m in body["items"]] == [("GP-7301", "Idno Target")]
        assert body["next_cursor"] is None

    asyncio.run(run())


def test_unknown_id_number_is_an_honest_empty_page() -> None:
    """Miss: 200 + empty items — never 404, and the response discloses
    NOTHING about the probed value (the probe string must not echo)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        await _seed_person_with_profile(
            tid, member_no="GP-7311", name="Idno Present", id_number="41957766"
        )
        async with api_client() as client:
            res = await client.get(
                "/members", params={"id_number": "99990000"}, headers=_headers(token)
            )
        assert res.status_code == 200, res.text
        assert res.json() == {"items": [], "next_cursor": None}
        assert "99990000" not in res.text

    asyncio.run(run())


def test_id_number_lookup_is_tenant_fenced() -> None:
    """Tenant A probes tenant B's real ID number: EMPTY page — a
    foreign identity is indistinguishable from a missing one."""

    async def run() -> None:
        _tid_a, _, token_a = await seed_actor()
        tid_b, _, _ = await seed_actor()
        await _seed_person_with_profile(
            tid_b, member_no="GP-8301", name="Foreign Person", id_number="52068877"
        )
        async with api_client() as client:
            res = await client.get(
                "/members", params={"id_number": "52068877"}, headers=_headers(token_a)
            )
        assert res.status_code == 200
        assert res.json() == {"items": [], "next_cursor": None}

    asyncio.run(run())


def test_id_number_is_an_exclusive_probe() -> None:
    """Combining id_number with any other parameter is a 422 — one
    declared meaning, never an ambiguous merged scope. Falsifiable:
    allowing the combination silently ignores one of the filters."""

    async def run() -> None:
        _, _, token = await seed_actor()
        for params in (
            {"id_number": "20735544", "member_no": "GP-7301"},
            {"id_number": "20735544", "status": "active"},
            {"id_number": "20735544", "include": "aggregates"},
            {"id_number": "20735544", "cursor": "opaque"},
        ):
            async with api_client() as client:
                res = await client.get("/members", params=params, headers=_headers(token))
            assert res.status_code == 422, (params, res.status_code)

    asyncio.run(run())


def test_id_number_lookup_plan_is_index_served_no_sort() -> None:
    """EXPLAIN structural gate (gate 1.3): with enable_seqscan=off the
    EXACT production statement enters member_profiles through the 0045
    partial expression index and resolves members by PRIMARY KEY —
    zero Seq Scan nodes AND zero Sort nodes. Falsifiable: DROP INDEX
    idx_member_profiles_id_number and the member_profiles entry point
    seq-scans (enable_seqscan=off only penalises the cost), failing
    the zero-seqscan assert; adding an ORDER BY to the statement
    introduces a Sort node, failing the zero-sort assert."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        await _seed_person_with_profile(
            tid, member_no="GP-7321", name="Plan Member", id_number="63179988"
        )
        # Cardinality that discriminates (the tiny-CI-tables discipline):
        # with a single member row the members-driven join order ties on
        # cost and the planner may bypass the expression index. Decoy
        # members with distinct id_numbers make the tenant-wide members
        # entry point strictly more expensive, and ANALYZE hands the
        # planner the real row counts, so the winning plan must enter
        # through idx_member_profiles_id_number — while dropping the
        # index still fails the asserts below (no plan can name it).
        for i in range(8):
            await _seed_person_with_profile(
                tid,
                member_no=f"GP-74{i:02d}",
                name=f"Plan Decoy {i}",
                id_number=f"771002{i:02d}",
            )
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("ANALYZE members, member_profiles"))
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            rows = (
                await session.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS) {MEMBER_ID_NUMBER_LOOKUP_SQL}"),
                    {"tid": str(tid), "id_number": "63179988", "limit": 20},
                )
            ).scalars()
            plan = "\n".join(str(r) for r in rows)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(plan + "\n", encoding="utf-8")
        assert "Seq Scan" not in plan, plan
        assert "Sort" not in plan, plan
        assert "idx_member_profiles_id_number" in plan, plan

    asyncio.run(run())
