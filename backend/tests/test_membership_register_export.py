"""Membership register export (P13.10 on the P13 registry).

FM3/H4 — PII disclosure is a role-gated allow-list proven PER ROLE
(the full 7-role prototype matrix): a role without members:view gets
ZERO PII columns; a role without reports:view cannot export at all.
FM4 — the `=HYPERLINK(...)` member-name formula injection travels the
FULL render path (member row -> keyset page -> worker render ->
download). FM5/H5 — the register renders ONE snapshot: a commit
between keyset batches (new member + status flip, the
concurrent-settlement shape) is invisible. The register carries the
FULL post-!32 status vocabulary including 'dormant'.

Every oracle is hand-computed in comments; falsifiability notes per
test (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import exports as exports_service
from genesis.infrastructure.tenancy import tenant_session
from test_reports_e2e import download, parse_csv, request_and_render

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_FULL_HEADERS = ["Member No", "Member", "Type", "Phone", "Email", "Status", "Joined At"]
_PII_KEYS = {"member_name", "phone", "email"}


async def _set_status(tid: uuid.UUID, mid: uuid.UUID, status: str) -> None:
    """Raw status seed for the RENDER surface under test (the report
    reads the column; the transition machine is P8/P13.13-tested)."""
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "UPDATE members SET status = :status "
                "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"status": status, "m": str(mid), "tid": str(tid)},
        )


def test_register_carries_the_full_status_vocabulary_incl_dormant() -> None:
    """Post-!32 vocabulary: active, arrears, dormant AND exited all
    render verbatim. Falsifiable: filter the register to a narrower
    status set (the pre-!32 vocabulary) and the dormant/exited rows
    vanish, failing the row count and the status column asserts."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        expected: dict[str, str] = {}
        for status in ("active", "arrears", "dormant", "exited"):
            mid = await seed_member(tid, name=f"Member {status.title()}")
            if status != "active":
                await _set_status(tid, mid, status)
            expected[f"Member {status.title()}"] = status

        _, completed = await request_and_render(token, tid, {"report": "membership_register"})
        artifact = completed["artifact"]
        assert artifact is not None
        status_code, _, body = await download(token, artifact["csv_download"])
        assert status_code == 200
        rows = parse_csv(body)
        assert rows[0] == _FULL_HEADERS
        assert len(rows) == 5
        rendered = {row[1]: row[5] for row in rows[1:]}
        assert rendered == expected

    asyncio.run(run())


def test_h4_seven_role_walk_gates_every_pii_column() -> None:
    """The full prototype matrix, role by role (P13 blocker e):

      * System Admin / Branch Manager / Loan Officer / Accountant /
        Auditor hold reports:view AND members:view -> all columns.
      * Credit Committee holds reports:view but NOT members:view ->
        ZERO PII columns (no Member, no Phone, no Email — header and
        cells) while the register itself still renders.
      * Teller holds members:view but NOT reports:view -> 403 before
        any job is queued (deny by default, gate 1.6).

    Falsifiable: grant the PII columns unconditionally (drop the
    members:view check in _allowed_columns) and the Credit Committee
    leg fails on all three asserts; drop RequirePermission and the
    Teller leg fails."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        await seed_member(tid, name="Walk Target")

        for role in (
            "System Admin",
            "Branch Manager",
            "Loan Officer",
            "Accountant",
            "Auditor",
        ):
            _, token = await add_user(tid, role)
            _, completed = await request_and_render(token, tid, {"report": "membership_register"})
            assert set(completed["allowed_columns"]) >= _PII_KEYS, role
            artifact = completed["artifact"]
            assert artifact is not None
            _, _, body = await download(token, artifact["csv_download"])
            rows = parse_csv(body)
            assert rows[0] == _FULL_HEADERS, role
            assert rows[1][1] == "Walk Target", role

        _, committee_token = await add_user(tid, "Credit Committee")
        _, completed = await request_and_render(
            committee_token, tid, {"report": "membership_register"}
        )
        assert not (_PII_KEYS & set(completed["allowed_columns"]))
        artifact = completed["artifact"]
        assert artifact is not None
        _, _, body = await download(committee_token, artifact["csv_download"])
        rows = parse_csv(body)
        assert rows[0] == ["Member No", "Type", "Status", "Joined At"]
        assert all("Walk Target" not in cell for cell in rows[1])

        _, teller_token = await add_user(tid, "Teller")
        async with api_client() as client:
            refused = await client.post(
                "/exports",
                json={"report": "membership_register"},
                headers={"authorization": f"Bearer {teller_token}"},
            )
        assert refused.status_code == 403
        # Deny happens BEFORE any side effect: nothing queued for the
        # teller (side-effect count, never the response alone).
        assert (
            await count(
                tid,
                "SELECT count(*) FROM exports WHERE report = 'membership_register'",
            )
            == 6
        )

    asyncio.run(run())


def test_fm4_formula_injection_through_the_full_register_path() -> None:
    """MANDATORY (P13 blocker b): hostile member name/phone/email
    travel member row -> keyset page -> worker render -> download and
    come out inert. Falsifiable: drop escape_csv_text from the
    renderer and every cell below equals its raw payload."""

    async def run() -> None:
        name = '=HYPERLINK("http://evil.example/x","Open")'
        phone = "@SUM(1+9)"
        email = "+1337@evil.example"
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, name=name)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "UPDATE members SET phone = :phone, email = :email "
                    "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"phone": phone, "email": email, "m": str(mid), "tid": str(tid)},
            )

        _, completed = await request_and_render(token, tid, {"report": "membership_register"})
        artifact = completed["artifact"]
        assert artifact is not None
        _, _, body = await download(token, artifact["csv_download"])
        row = parse_csv(body)[1]
        # Hand-computed inert forms: quote-prefixed text (OWASP).
        assert row[1] == f"'{name}"
        assert row[3] == f"'{phone}"
        assert row[4] == f"'{email}"
        assert not any(cell.startswith(("=", "+", "-", "@")) for cell in row)

    asyncio.run(run())


def test_keyset_pages_stitch_and_the_row_cap_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P13 blockers c/d inherited: single-row keyset batches stitch
    with no gap and no repeat; a row cap below the member count sets
    the exact truncation flag and headers. Falsifiable: break the
    cursor predicate (>= instead of >) and the batch-1 render repeats
    a member; drop the cap enforcement and truncated goes false."""

    async def run() -> None:
        monkeypatch.setattr(exports_service, "export_batch_size", lambda: 1)
        tid, _, token = await seed_actor()
        names = [f"Keyset Member {i}" for i in range(3)]
        for name in names:
            await seed_member(tid, name=name)

        _, completed = await request_and_render(token, tid, {"report": "membership_register"})
        artifact = completed["artifact"]
        assert artifact is not None
        assert artifact["row_count"] == 3
        assert artifact["truncated"] is False
        _, _, body = await download(token, artifact["csv_download"])
        rows = parse_csv(body)
        assert [row[1] for row in rows[1:]] == names  # admission order

        monkeypatch.setattr(exports_service, "export_row_cap", lambda: 2)
        _, capped = await request_and_render(token, tid, {"report": "membership_register"})
        capped_artifact = capped["artifact"]
        assert capped_artifact is not None
        assert capped_artifact["row_count"] == 2
        assert capped_artifact["truncated"] is True
        status_code, headers, body = await download(token, capped_artifact["csv_download"])
        assert status_code == 200
        assert headers["x-export-truncated"] == "true"
        assert headers["x-export-limit"] == "2"
        assert len(parse_csv(body)) == 3  # header + 2 of the 3 members

    asyncio.run(run())


def test_fm5_snapshot_never_observes_commits_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P13 blocker h / H5: the register renders from ONE REPEATABLE
    READ transaction. Between two keyset batches a concurrent session
    COMMITS the settlement shape — an existing member flips to
    'exited' and a new member lands with created_at inside the as-of
    window. Neither is observed. Falsifiable: render on a READ
    COMMITTED session and the artifact shows 'exited' and 3 rows."""

    async def run() -> None:
        monkeypatch.setattr(exports_service, "export_batch_size", lambda: 1)
        tid, _, token = await seed_actor()
        await seed_member(tid, name="First Member")
        second = await seed_member(tid, name="Second Member")

        fired = False

        async def interleave() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE members SET status = 'exited' "
                        "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": str(second), "tid": str(tid)},
                )
                await session.execute(
                    text(
                        "INSERT INTO members "
                        "(id, tenant_id, member_no, type, name, created_at) "
                        "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                        "'Mid Export Member', :ts)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": str(tid),
                        "no": f"GP-{uuid.uuid4().hex[:6].upper()}",
                        # Backdated INSIDE the export's as-of window —
                        # the racing-settlement shape.
                        "ts": datetime.now(UTC) - timedelta(hours=1),
                    },
                )

        monkeypatch.setattr(exports_service, "_between_batches", interleave)
        _, completed = await request_and_render(token, tid, {"report": "membership_register"})
        assert fired, "the interleaved commit never ran; the test proved nothing"
        artifact = completed["artifact"]
        assert artifact is not None
        _, _, body = await download(token, artifact["csv_download"])
        rows = parse_csv(body)
        # Hand-computed snapshot: exactly the two pre-export members,
        # both in their pre-commit status.
        assert len(rows) == 3
        assert [row[1] for row in rows[1:]] == ["First Member", "Second Member"]
        assert [row[5] for row in rows[1:]] == ["active", "active"]

        # The commit is real: a FRESH render sees all three members
        # and the flipped status (proves the writes landed and only
        # snapshot isolation hid them above; `fired` keeps the seam a
        # no-op for this second run).
        _, after = await request_and_render(token, tid, {"report": "membership_register"})
        after_artifact = after["artifact"]
        assert after_artifact is not None
        _, _, after_body = await download(token, after_artifact["csv_download"])
        after_rows = parse_csv(after_body)
        assert len(after_rows) == 4
        assert {row[1]: row[5] for row in after_rows[1:]}["Second Member"] == "exited"

    asyncio.run(run())
