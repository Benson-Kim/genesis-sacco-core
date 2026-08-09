"""Member dividend payout PREFERENCE (#31 batch 8, ledger (c)).

The authorized expand-only contract change: members carry a stored,
nullable payout preference resolved against the CODE-OWNED vocabulary
(domain.members.DividendPayout — deposit_account | share_capital |
mpesa | bank; provenance documented on the enum). Requires a migrated
PostgreSQL database (DATABASE_URL) at head >= 0039.

Anti-reward-hacking posture (MASTER_PROMPT section 4):
  * Every oracle below is hand-computed from the seeds/requests in the
    test body — never captured from the implementation.
  * Refusal paths are proven by SIDE-EFFECT ROW COUNTS (members,
    audit_log, outbox_events), never by status codes alone.
  * The regulatory fence (preference ONLY — the P13.11 distribution
    engine must not consume it in this batch) is pinned FALSIFIABLY:
    the moment any dividends module references the column, the fence
    test fails and forces the separately-authorized routing batch to
    retire it deliberately.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.rbac import seed_permissions
from genesis.domain.members import DividendPayout
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, str]:
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    token = issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )
    return tid, token


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _counts(tid: uuid.UUID) -> tuple[int, int, int]:
    """Tenant-scoped (members, member.create audits, welcome events)."""
    async with tenant_session(factory(), tid) as session:
        members = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
        audits = (
            await session.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'member.create'")
            )
        ).scalar_one()
        welcomes = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = 'member.welcome'")
            )
        ).scalar_one()
    return int(members), int(audits), int(welcomes)


# ---------------------------------------------------------------------------
# Create: stored preference, honest NULL, audit truth
# ---------------------------------------------------------------------------


def test_create_with_preference_persists_and_audits() -> None:
    """Oracle (hand-computed): POST with dividend_payout='mpesa' must
    yield a 201 whose body carries dividend_payout == 'mpesa', a
    members row storing 'mpesa', and ONE member.create audit row whose
    after JSON records the preference."""

    async def run() -> None:
        tid, token = await _seed_actor()
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Amina Yusuf", "dividend_payout": "mpesa"},
                headers=_headers(token),
            )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["dividend_payout"] == "mpesa"
        async with tenant_session(factory(), tid) as session:
            stored = (
                await session.execute(
                    text(
                        "SELECT dividend_payout FROM members WHERE id = CAST(:m AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": body["id"], "tid": str(tid)},
                )
            ).scalar_one()
            audit_after = (
                await session.execute(
                    text(
                        "SELECT after->>'dividend_payout' FROM audit_log "
                        "WHERE action = 'member.create' AND entity_id = :m"
                    ),
                    {"m": body["id"]},
                )
            ).scalar_one()
        assert stored == "mpesa"
        assert audit_after == "mpesa"

    asyncio.run(run())


def test_create_without_preference_serializes_null_never_omitted() -> None:
    """Nullable-never-optional: an omitted preference stays NULL (the
    honest 'not chosen' state — never a backfilled default) and the
    key is STILL serialized on MemberOut (a missing key would make the
    web Zod boundary reject the row)."""

    async def run() -> None:
        tid, token = await _seed_actor()
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "company", "name": "Kifaru Logistics"},
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            body = res.json()
            assert "dividend_payout" in body
            assert body["dividend_payout"] is None
            # The detail read and the flat register carry the key too.
            detail = await client.get(f"/members/{body['id']}", headers=_headers(token))
            assert detail.status_code == 200
            assert detail.json()["dividend_payout"] is None
            page = await client.get("/members", headers=_headers(token))
            assert page.status_code == 200
            assert all("dividend_payout" in item for item in page.json()["items"])
        # tid keeps the RLS fence honest for the helper signature.
        assert tid is not None

    asyncio.run(run())


def test_unknown_preference_is_422_vocabulary_not_echoed_zero_side_effects() -> None:
    """Least disclosure + fail-closed. Oracle: an unknown value yields
    422 whose body is the sanitized envelope ONLY — none of the four
    code-owned vocabulary values appears anywhere in the response —
    and ZERO rows were written (member/audit/outbox side-effect counts
    unchanged; the service validates BEFORE the first INSERT)."""

    async def run() -> None:
        tid, token = await _seed_actor()
        before = await _counts(tid)
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Probe", "dividend_payout": "cheque"},
                headers=_headers(token),
            )
        assert res.status_code == 422, res.text
        envelope = res.json()
        assert envelope["category"] == "validation_error"
        assert set(envelope) == {"category", "correlation_id"}
        for word in ("deposit_account", "share_capital", "mpesa", "bank"):
            assert word not in res.text
        assert await _counts(tid) == before

    asyncio.run(run())


def test_unknown_body_field_is_422_extra_forbid() -> None:
    """extra='forbid' on the create body (gate 1.6): an invented field
    is a structural 422, never silently dropped — with zero writes."""

    async def run() -> None:
        tid, token = await _seed_actor()
        before = await _counts(tid)
        async with api_client() as client:
            res = await client.post(
                "/members",
                json={"type": "person", "name": "Probe", "payout_rate_pct": "99"},
                headers=_headers(token),
            )
        assert res.status_code == 422, res.text
        assert await _counts(tid) == before

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Edit: versioned optimistic-lock path ONLY
# ---------------------------------------------------------------------------


def test_preference_updates_via_versioned_path_and_stale_write_is_409() -> None:
    """Oracle: the PUT with version=1 sets 'bank' and bumps version to
    2 with ONE member.update audit row whose before/after JSON records
    NULL -> 'bank'. A second PUT replaying version=1 (stale) is 409
    AND leaves the stored preference untouched (side-effect proof:
    still 'bank', version still 2, still exactly one update audit)."""

    async def run() -> None:
        tid, token = await _seed_actor()
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Grace Wanjiru"},
                headers=_headers(token),
            )
            assert created.status_code == 201
            mid = created.json()["id"]

            ok = await client.put(
                f"/members/{mid}",
                json={"version": 1, "dividend_payout": "bank"},
                headers=_headers(token),
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["dividend_payout"] == "bank"
            assert ok.json()["version"] == 2

            stale = await client.put(
                f"/members/{mid}",
                json={"version": 1, "dividend_payout": "mpesa"},
                headers=_headers(token),
            )
            assert stale.status_code == 409, stale.text

        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT dividend_payout, version FROM members "
                        "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": mid, "tid": str(tid)},
                )
            ).one()
            audit = (
                await session.execute(
                    text(
                        "SELECT count(*), "
                        "MIN(before->>'dividend_payout'), MIN(after->>'dividend_payout') "
                        "FROM audit_log WHERE action = 'member.update' AND entity_id = :m"
                    ),
                    {"m": mid},
                )
            ).one()
        assert (str(row[0]), int(row[1])) == ("bank", 2)
        # Exactly ONE update audit; before was the honest NULL, after
        # is the stored preference (the stale 409 wrote nothing).
        assert int(audit[0]) == 1
        assert audit[1] is None
        assert audit[2] == "bank"

    asyncio.run(run())


def test_unknown_preference_on_update_is_422_before_the_compare_and_swap() -> None:
    """The service resolves the vocabulary BEFORE the compare-and-swap:
    an unknown value with a CORRECT version is 422 (not 409) and the
    row is untouched (version still 1, preference still NULL)."""

    async def run() -> None:
        tid, token = await _seed_actor()
        async with api_client() as client:
            created = await client.post(
                "/members",
                json={"type": "person", "name": "Peter Otieno"},
                headers=_headers(token),
            )
            assert created.status_code == 201
            mid = created.json()["id"]
            res = await client.put(
                f"/members/{mid}",
                json={"version": 1, "dividend_payout": "REINVEST"},
                headers=_headers(token),
            )
        assert res.status_code == 422, res.text
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT dividend_payout, version FROM members "
                        "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                    ),
                    {"m": mid, "tid": str(tid)},
                )
            ).one()
        assert row[0] is None
        assert int(row[1]) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Database constraint (gate 1.5) — falsifiable beneath the app layer
# ---------------------------------------------------------------------------


def test_db_check_rejects_vocabulary_bypass() -> None:
    """The 0039 CHECK is the defence in depth BENEATH the service: a
    direct INSERT smuggling an out-of-vocabulary value must fail AT
    THE DATABASE. Falsifiable: drop ck_members_dividend_payout and
    this test fails (the INSERT would succeed)."""

    async def run() -> None:
        tid, _ = await _seed_actor()
        with pytest.raises(IntegrityError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "INSERT INTO members "
                        "(tenant_id, member_no, type, name, dividend_payout) "
                        "VALUES (CAST(:tid AS uuid), 'GP-9999', 'person', 'X', 'cheque')"
                    ),
                    {"tid": str(tid)},
                )

    asyncio.run(run())


def test_db_check_value_set_equals_the_code_owned_enum_exactly() -> None:
    """Single source of truth (review R3(b)): the 0039 CHECK's literal
    value set must equal the DividendPayout StrEnum member set by SET
    EQUALITY. The deployed expression is read from the live catalog
    (pg_get_constraintdef) — never from this test's own copy of the
    vocabulary. Falsifiable in BOTH directions: adding an enum member
    without widening the CHECK fails, dropping one while the CHECK
    still carries it fails, and dropping or renaming the constraint
    itself fails (scalar_one raises on zero rows)."""

    async def run() -> None:
        tid, _ = await _seed_actor()
        async with tenant_session(factory(), tid) as session:
            definition = (
                await session.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = 'members'::regclass "
                        "AND conname = 'ck_members_dividend_payout'"
                    )
                )
            ).scalar_one()
        # In the deparsed form only the vocabulary literals are
        # single-quoted (identifiers stay bare; the casts render as
        # ::text): CHECK ((dividend_payout IS NULL) OR
        # (dividend_payout = ANY (ARRAY['deposit_account'::text, ...]))).
        db_values = set(re.findall(r"'([^']*)'", str(definition)))
        assert db_values == {member.value for member in DividendPayout}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The routing map (#31 batch 12) — the fence's falsifiable successor
# ---------------------------------------------------------------------------


def test_payout_routing_map_is_total_over_the_enum() -> None:
    """FM7 (#31 batch 12): the batch-8 preference-only fence was retired
    DELIBERATELY under the maintainer authorization recorded on #31
    (2026-08-07 — '(c) AUTHORIZED as BATCH 12'). Its successor guard:
    every DividendPayout token is EITHER routed by the code-owned
    PAYOUT_CREDIT_ROUTING map OR explicitly recorded as an
    unimplementable fallback token (external channels, issue #10) —
    set-equal, disjoint. A future enum token fails this test until a
    deliberate routing decision is recorded, so a preference can never
    silently take the default path by omission. Routed destinations
    are restricted to the member retention accounts: routing to a cash
    account would fake an external payment (never allowed here)."""

    from genesis.application.dividends import PAYOUT_CREDIT_ROUTING, PAYOUT_FALLBACK_TOKENS
    from genesis.domain.ledger import Account

    routed = set(PAYOUT_CREDIT_ROUTING)
    fallback = set(PAYOUT_FALLBACK_TOKENS)
    assert routed & fallback == set(), "a token cannot be both routed and fallback"
    assert routed | fallback == set(DividendPayout), (
        "every DividendPayout token needs a deliberate routing decision: "
        "either a PAYOUT_CREDIT_ROUTING destination or an explicit "
        "PAYOUT_FALLBACK_TOKENS entry (never a silent default)"
    )
    assert set(PAYOUT_CREDIT_ROUTING.values()) <= {
        Account.MEMBER_DEPOSITS,
        Account.MEMBER_SHARES,
    }, "dividend routing may only target member retention accounts"
