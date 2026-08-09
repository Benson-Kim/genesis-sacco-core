"""Sign-in identifier suite (#35 round): phone OR email through ONE rule.

Staff OTP sign-in accepts a single identifier that may be an email
address or a Kenya mobile number. Classification and normalization go
through resolve_signin_identifier, which delegates to the ONE existing
normalizer (domain/members.py:normalize_kenya_msisdn) — never a second
rule. Falsifiable legs (oracles hand-computed in comments, never
captured from the code under test):

- FM1: a user stored with E.164 phone signs in by typing any accepted
  spelling; remove the normalization and the lookup misses.
- FM2: unknown identifiers (phone-shaped AND email-shaped) get the
  same 202 body as a known user (dev flag off) and leave ZERO
  otp_challenges rows (side-effect row count, not return values).
- FM3: verify with an unknown identifier answers the same sanitized
  category as a known user without a challenge — no existence oracle.
- FM4: bodies with BOTH email and identifier, or NEITHER, are a 422.
- FM5: the phone probe is index-served (structural EXPLAIN gate, zero
  Seq Scan nodes with enable_seqscan=off; drop 0044 and this fails).
- FM7: the legacy email-only wire shape keeps working byte-identically.
- Cross-tenant: the phone probe under another tenant's header resolves
  nothing (forced RLS) and issues no challenge.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, latest_otp_code, seed_user, unique_email
from genesis.application.auth import (
    IDENTIFIER_EMAIL,
    IDENTIFIER_PHONE,
    USER_ID_BY_PHONE_SQL,
    USER_LOCK_BY_PHONE_SQL,
    resolve_signin_identifier,
)
from genesis.infrastructure.tenancy import tenant_session

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

OUT_PATH = Path(__file__).resolve().parents[1] / "perf" / "explain_signin_phone.txt"


def test_classifier_is_the_one_msisdn_rule() -> None:
    # Hand-computed oracles: 0712345678 matches 0(7)(12345678) so the
    # E.164 form is '+254' + '7' + '12345678' = '+254712345678'; the
    # 01-prefix twin maps to +2541...; an already-E.164 value is kept;
    # surrounding whitespace is tolerated (the normalizer's own rule).
    assert resolve_signin_identifier("0712345678") == (IDENTIFIER_PHONE, "+254712345678")
    assert resolve_signin_identifier("0110000000") == (IDENTIFIER_PHONE, "+254110000000")
    assert resolve_signin_identifier("+254712345678") == (IDENTIFIER_PHONE, "+254712345678")
    assert resolve_signin_identifier(" 0712345678 ") == (IDENTIFIER_PHONE, "+254712345678")
    # Everything the msisdn rule rejects takes the email path VERBATIM —
    # including phone-ish typos (no digit harvesting, no second rule).
    assert resolve_signin_identifier("user@example.com") == (IDENTIFIER_EMAIL, "user@example.com")
    assert resolve_signin_identifier("0712") == (IDENTIFIER_EMAIL, "0712")
    assert resolve_signin_identifier("0812345678") == (IDENTIFIER_EMAIL, "0812345678")


@requires_db
def test_phone_identifier_full_otp_flow() -> None:
    """FM1: stored E.164 +254712345678 signs in via the local spelling."""

    async def run() -> None:
        email = unique_email()
        # One distinct number per run: uuid-derived 8-digit suffix.
        suffix = f"{uuid.uuid4().int % 10**8:08d}"
        e164 = f"+2547{suffix}"
        local = f"07{suffix}"
        tid, _ = await seed_user(email, phone=e164)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request", json={"identifier": local}, headers=headers
            )
            assert res.status_code == 202
            code = await latest_otp_code(tid)
            # Verify through the OTHER accepted spelling: every spelling
            # of the same number converges on the same stored user.
            res = await client.post(
                "/auth/otp/verify", json={"identifier": e164, "code": code}, headers=headers
            )
            assert res.status_code == 200
            assert res.json()["expires_in"] == 900

    asyncio.run(run())


@requires_db
def test_email_identifier_and_legacy_email_field() -> None:
    """FM7: identifier=email works; the legacy email-only shape still works."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            # New unified field carrying an email.
            res = await client.post(
                "/auth/otp/request", json={"identifier": email}, headers=headers
            )
            assert res.status_code == 202
            code = await latest_otp_code(tid)
            res = await client.post(
                "/auth/otp/verify", json={"identifier": email, "code": code}, headers=headers
            )
            assert res.status_code == 200
            # Legacy wire shape (expand phase): byte-identical behaviour.
            res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert res.status_code == 202
            code = await latest_otp_code(tid)
            res = await client.post(
                "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
            )
            assert res.status_code == 200

    asyncio.run(run())


@requires_db
def test_unknown_identifier_request_is_non_disclosing() -> None:
    """FM2: unknown phone/email answer the KNOWN-user body; zero challenges."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email, phone="+254700000001")
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            known = await client.post(
                "/auth/otp/request", json={"identifier": email}, headers=headers
            )
            unknown_phone = await client.post(
                "/auth/otp/request", json={"identifier": "0733999999"}, headers=headers
            )
            unknown_email = await client.post(
                "/auth/otp/request", json={"identifier": "ghost@example.com"}, headers=headers
            )
        assert known.status_code == unknown_phone.status_code == unknown_email.status_code == 202
        # Byte-identical bodies (dev flag off is the default fail-closed
        # state): no key, value or shape distinguishes existence.
        assert known.json() == unknown_phone.json() == unknown_email.json() == {"status": "sent"}
        # Side-effect row counts (never return values alone): exactly the
        # ONE challenge from the known request exists in this tenant.
        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM otp_challenges"))
            ).scalar_one()
        assert int(count) == 1

    asyncio.run(run())


@requires_db
def test_unknown_identifier_verify_matches_no_challenge_category() -> None:
    """FM3: unknown identifier and known-user-without-challenge are twins."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)  # known user, NO challenge issued
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            no_challenge = await client.post(
                "/auth/otp/verify", json={"identifier": email, "code": "123456"}, headers=headers
            )
            unknown = await client.post(
                "/auth/otp/verify",
                json={"identifier": "0733999999", "code": "123456"},
                headers=headers,
            )
        assert no_challenge.status_code == unknown.status_code == 401
        # The sanitized envelope carries category only; both paths emit
        # the SAME category so the wire holds no existence oracle.
        assert no_challenge.json()["category"] == unknown.json()["category"] == "unauthenticated"
        assert set(no_challenge.json()) == set(unknown.json())

    asyncio.run(run())


@requires_db
def test_both_or_neither_identifier_is_422() -> None:
    """FM4: exactly one of email/identifier — both or neither is a 422."""

    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            both = await client.post(
                "/auth/otp/request",
                json={"email": email, "identifier": email},
                headers=headers,
            )
            neither = await client.post("/auth/otp/request", json={}, headers=headers)
            verify_both = await client.post(
                "/auth/otp/verify",
                json={"email": email, "identifier": email, "code": "123456"},
                headers=headers,
            )
            verify_neither = await client.post(
                "/auth/otp/verify", json={"code": "123456"}, headers=headers
            )
        assert both.status_code == 422
        assert neither.status_code == 422
        assert verify_both.status_code == 422
        assert verify_neither.status_code == 422

    asyncio.run(run())


@requires_db
def test_cross_tenant_phone_probe_resolves_nothing() -> None:
    """Forced RLS: tenant B's header never resolves tenant A's phone."""

    async def run() -> None:
        email = unique_email()
        suffix = f"{uuid.uuid4().int % 10**8:08d}"
        e164 = f"+2547{suffix}"
        tid_a, _ = await seed_user(email, phone=e164)
        tid_b, _ = await seed_user(unique_email())
        headers_b = {"x-tenant-id": str(tid_b)}
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request", json={"identifier": e164}, headers=headers_b
            )
        assert res.status_code == 202  # non-disclosing shape either way
        # Zero challenge rows in EITHER tenant: the probe found nothing.
        for tid in (tid_a, tid_b):
            async with tenant_session(factory(), tid) as session:
                count = (
                    await session.execute(text("SELECT count(*) FROM otp_challenges"))
                ).scalar_one()
            assert int(count) == 0

    asyncio.run(run())


@requires_db
def test_phone_lookup_is_index_backed() -> None:
    """FM5: both production phone statements are index-served (0044)."""

    async def run() -> None:
        email = unique_email()
        suffix = f"{uuid.uuid4().int % 10**8:08d}"
        e164 = f"+2547{suffix}"
        tid, _ = await seed_user(email, phone=e164)
        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plans: list[str] = []
            for label, sql in (
                ("request lookup", USER_ID_BY_PHONE_SQL),
                ("verify lock", USER_LOCK_BY_PHONE_SQL),
            ):
                rows = (
                    await session.execute(
                        text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), {"ident": e164}
                    )
                ).scalars()
                plans.append(f"=== {label} ===\n" + "\n".join(str(r) for r in rows))
        combined = "\n".join(plans)
        header = (
            "sign-in phone lookup (users) — structural gate: tiny CI tables\n"
            "make seqscan cheaper, so enable_seqscan=off proves the probes\n"
            "are SERVABLE by 0044's partial idx_users_phone (tenant_id,\n"
            "phone) WHERE phone IS NOT NULL under the forced-RLS tenant\n"
            "qual. Falsifiable: drop the index and these plans seq-scan.\n"
        )
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(f"{header}\n{combined}\n")
        assert "Seq Scan" not in combined, "the phone probe fell back to a seq scan"
        assert "idx_users_phone" in combined, "the phone probe is not riding 0044's index"

    asyncio.run(run())
