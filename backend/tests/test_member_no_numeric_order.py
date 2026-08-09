"""#31 remediation N1: numeric member_no order across the 4->5 digit boundary.

Senior review round 2 on !78, finding N1: the branch member roster and
the P8 tenant-wide members listing ordered and keyset-paginated on
TEXT member_no. The domain format (domain/members.format_member_no:
'GP-' + zero-padded sequence of AT LEAST four digits) is monotonic
NUMERICALLY, not lexicographically — as text 'GP-10000' < 'GP-9999' —
so past 9,999 members the order was wrong and keyset walks
skipped/duplicated rows. The fix orders both reads by
(length(member_no), member_no) with the matching row-value keyset
predicate; the 0041 expression index (tenant_id, length(member_no),
member_no) keeps the tenant-wide listing index-served (gate 1.3).

Covers (HAND-COMPUTED oracles, MASTER_PROMPT §4 — controlled
member_nos GP-9998, GP-9999, GP-10000, GP-10001 seeded by direct
INSERT in ONE tenant/branch; numeric order 9998 < 9999 < 10000 <
10001, whereas the OLD lexicographic SQL yields GP-10000 < GP-10001 <
GP-9998 < GP-9999, so every leg below FAILS against the old SQL):

- (a) FULL-LIST order is numeric on BOTH endpoints (GET /members and
  GET /branches/{id}/members),
- (b) a limit-2 keyset walk is EXHAUSTIVE and NON-OVERLAPPING across
  the 4->5 digit boundary on both endpoints (old SQL: the page-1
  cursor 'GP-9999' matches nothing lexicographically — the walk
  truncates, dropping both 5-digit members),
- (c) cursor 'GP-9999' yields EXACTLY the 5-digit successors
  [GP-10000, GP-10001]; a 5-digit cursor 'GP-10000' (accepted by the
  batch-7 F3 guard, which stays in force) resumes correctly with
  [GP-10001],
- the EXPLAIN gate: the exact production statements (MEMBER_LIST_SQL
  / MEMBER_LIST_AGGREGATES_SQL over the shared clause builder) are
  index-served with NO Sort node under enable_seqscan=off AND
  enable_sort=off — the plan rides the 0041 expression index, proven
  falsifiable: drop idx_members_member_no_numeric and a Sort node is
  the only remaining path, failing the gate. (The equality-probed
  single-branch roster stays a bounded top-N under idx_members_branch
  and is deliberately NOT gated on sort-freeness.)
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import api_client, factory
from export_helpers import create_branch, seed_actor, seed_member_no
from genesis.application.branches import BRANCH_MEMBERS_SCOPE
from genesis.application.guarantees import live_guarantee_params
from genesis.application.members import (
    MEMBER_LIST_AGGREGATES_SQL,
    MEMBER_LIST_SQL,
    MEMBERS_LIST_SCOPE,
    _member_list_clauses,
)
from genesis.application.pagination import encode_cursor
from genesis.domain.lending import LoanStatus
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_remediation_n1.txt"

#: Hand-computed NUMERIC order of the seeded boundary set. The old
#: lexicographic SQL returns [GP-10000, GP-10001, GP-9998, GP-9999].
_BOUNDARY_ORDER = ["GP-9998", "GP-9999", "GP-10000", "GP-10001"]


async def _seed_boundary_tenant() -> tuple[uuid.UUID, str, str]:
    """One tenant, one branch, the four boundary members on the branch."""
    tid, _, token = await seed_actor()
    branch_id = await create_branch(token, f"N1-{uuid.uuid4().hex[:8]}")
    for i, member_no in enumerate(_BOUNDARY_ORDER):
        await seed_member_no(tid, member_no, name=f"Boundary {i}", branch_id=branch_id)
    return tid, branch_id, token


async def _walk(client: AsyncClient, path: str, headers: dict[str, str]) -> list[list[str]]:
    """limit-2 keyset walk to exhaustion; returns member_no pages."""
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(10):  # hard stop: a looping cursor is a failure, not a hang
        params: dict[str, object] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        res = await client.get(path, params=params, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        pages.append([m["member_no"] for m in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("keyset walk did not terminate")


def test_numeric_order_full_list_and_boundary_walk_both_endpoints() -> None:
    """Legs (a) + (b): full-list order is numeric and the limit-2 walk
    is exhaustive and non-overlapping across the 4->5 digit boundary
    on BOTH the tenant listing and the branch roster."""

    async def run() -> None:
        _, branch_id, token = await _seed_boundary_tenant()
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            for path in ("/members", f"/branches/{branch_id}/members"):
                # (a) full list: numeric order — the old SQL returns
                # the lexicographic permutation and fails here.
                res = await client.get(path, params={"limit": 100}, headers=headers)
                assert res.status_code == 200, res.text
                assert [m["member_no"] for m in res.json()["items"]] == _BOUNDARY_ORDER, path
                # (b) limit-2 walk: exactly two pages of two, in
                # numeric order — exhaustive (union == seeded set) and
                # non-overlapping (concatenation has no duplicates).
                # Old SQL: page 1 = [GP-10000, GP-10001]... and the
                # 4-digit resumes lose the 5-digit rows — wrong pages.
                pages = await _walk(client, path, headers)
                assert pages == [_BOUNDARY_ORDER[:2], _BOUNDARY_ORDER[2:]], path
                flat = [no for page in pages for no in page]
                assert len(flat) == len(set(flat)) == 4, path

    asyncio.run(run())


def test_cursor_gp9999_yields_exactly_the_five_digit_successors() -> None:
    """Leg (c): resuming from the 4-digit cursor 'GP-9999' yields
    EXACTLY [GP-10000, GP-10001] on both endpoints (old SQL: empty —
    no member_no sorts lexicographically after 'GP-9999'); resuming
    from the 5-digit 'GP-10000' (accepted by the F3 guard, which
    stays) yields exactly [GP-10001]."""

    async def run() -> None:
        tid, branch_id, token = await _seed_boundary_tenant()
        headers = {"authorization": f"Bearer {token}"}
        async with api_client() as client:
            # #31 batch 13: raw cursors are no longer accepted on the
            # wire — mint the SAME boundary positions through the
            # opaque codec (per-endpoint scope). The plaintext keyset
            # and the oracles below are unchanged.
            for path, scope in (
                ("/members", MEMBERS_LIST_SCOPE),
                (f"/branches/{branch_id}/members", BRANCH_MEMBERS_SCOPE),
            ):
                res = await client.get(
                    path,
                    params={
                        "cursor": encode_cursor("GP-9999", tenant_id=tid, endpoint=scope),
                        "limit": 100,
                    },
                    headers=headers,
                )
                assert res.status_code == 200, res.text
                assert [m["member_no"] for m in res.json()["items"]] == [
                    "GP-10000",
                    "GP-10001",
                ], path
                res = await client.get(
                    path,
                    params={
                        "cursor": encode_cursor("GP-10000", tenant_id=tid, endpoint=scope),
                        "limit": 100,
                    },
                    headers=headers,
                )
                assert res.status_code == 200, res.text
                assert [m["member_no"] for m in res.json()["items"]] == ["GP-10001"], path

    asyncio.run(run())


async def _explain(session: AsyncSession, sql: str, params: dict[str, object]) -> str:
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)).scalars()
    return "\n".join(str(r) for r in rows)


def test_numeric_list_statements_are_index_served_without_sort() -> None:
    """EXPLAIN gate (gate 1.3, the P13.9/P13.11 structural convention,
    STRENGTHENED with sort-freeness): the exact production statements
    behind GET /members (plain and include=aggregates), cursorless AND
    resuming from the boundary cursor 'GP-9999', are index-served with
    NO Sort node under enable_seqscan=off AND enable_sort=off — the
    driving order comes from the 0041 expression index
    (tenant_id, length(member_no), member_no). Falsifiable: drop that
    index and a Sort over the tenant slice is the only remaining
    order path (enable_sort=off only penalises its cost), so 'Sort'
    appears in the plan and this gate fails."""

    async def run() -> None:
        tid, _, _ = await _seed_boundary_tenant()

        captures: list[tuple[str, str]] = []
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            await session.execute(text("SET LOCAL enable_sort = off"))
            for title, cursor in (("cursorless", None), ("from cursor GP-9999", "GP-9999")):
                clauses, params = _member_list_clauses(
                    tid, cursor=cursor, limit=20, status=None, member_type=None
                )
                captures.append(
                    (
                        f"members list ({title})",
                        await _explain(
                            session, MEMBER_LIST_SQL.format(where=" AND ".join(clauses)), params
                        ),
                    )
                )
            clauses, params = _member_list_clauses(
                tid, cursor="GP-9999", limit=20, status=None, member_type=None, col="m."
            )
            params["loan_active"] = LoanStatus.ACTIVE.value
            params.update(live_guarantee_params())
            captures.append(
                (
                    "members list with aggregates (from cursor GP-9999)",
                    await _explain(
                        session,
                        MEMBER_LIST_AGGREGATES_SQL.format(where=" AND ".join(clauses)),
                        params,
                    ),
                )
            )

        # Capture the artifact BEFORE any assertion so the CI job log
        # and backend/perf/ artifact always carry the full plans.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "#31 remediation N1 EXPLAIN (ANALYZE, BUFFERS) — captured in CI\n"
            "against the migrated Postgres service under the RLS app role.\n"
            "enable_seqscan=off AND enable_sort=off because CI tables are\n"
            "tiny; the assertion is structural AND sort-free: the numeric\n"
            "(length(member_no), member_no) order of the tenant-wide members\n"
            "listing must come from the 0041 expression index\n"
            "idx_members_member_no_numeric — no Seq Scan, no Sort node.\n"
            "Falsifiable: drop the 0041 index and 'Sort' appears, failing\n"
            "the gate.\n"
        )
        OUT_PATH.write_text(
            header + "".join(f"\n=== {title} ===\n{plan}\n" for title, plan in captures)
        )

        for title, plan in captures:
            assert "Seq Scan" not in plan, f"{title} fell back to a sequential scan"
            assert "Index" in plan, f"{title} is not index-served"
            assert "Sort" not in plan, f"{title} sorts instead of riding the 0041 index"
            assert "members" in plan, f"{title} plan does not touch members"

    asyncio.run(run())
