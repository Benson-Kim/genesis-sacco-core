"""API + integration tests for member KYC profiles & documents (P13.12).

Requires a migrated PostgreSQL database (DATABASE_URL). Runs through
the app as the non-superuser role, so RLS is live under every test.

Anti-reward-hacking (§4): idempotency and audit guarantees are proven
by side-effect ROW COUNTS, never return values; the consent guard test
fails if the 0018 trigger is removed (raw SQL would then succeed);
the tenant-predicate tests run RLS-satisfied sessions with a foreign
tenant argument, so only the explicit predicates can refuse the row
(issue #17 pattern).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor
from genesis.application import member_kyc as kyc_service
from genesis.application import members as members_service
from genesis.application.pagination import encode_cursor
from genesis.domain.members import MemberType
from genesis.errors import NotFoundError
from genesis.infrastructure.db import get_engine
from genesis.infrastructure.tenancy import tenant_session
from kyc_payloads import PII_SENTINEL, VALID_PROFILES

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

ALL_TYPES = list(MemberType)


def _headers(token: str, idem: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if idem:
        headers["idempotency-key"] = idem
    return headers


async def _create_member(client: Any, token: str, member_type: MemberType) -> str:
    res = await client.post(
        "/members",
        json={"type": member_type.value, "name": f"KYC {member_type.value}"},
        headers=_headers(token, idem=uuid.uuid4().hex),
    )
    assert res.status_code == 201
    return str(res.json()["id"])


def _profile_body(member_type: MemberType, *, consent: bool = True) -> dict[str, Any]:
    return {
        "category": "Ordinary",
        "profile": VALID_PROFILES[member_type],
        "dpa_consent": consent,
    }


# ---------------------------------------------------------------------------
# Profile validation matrix (EXIT: wrong-type payload -> 422)
# ---------------------------------------------------------------------------


def test_profile_validation_matrix_and_pii_free_422() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            for member_type in ALL_TYPES:
                for payload_type in ALL_TYPES:
                    mid = await _create_member(client, token, member_type)
                    res = await client.post(
                        f"/members/{mid}/profile",
                        json=_profile_body(payload_type),
                        headers=_headers(token, idem=uuid.uuid4().hex),
                    )
                    if payload_type is member_type:
                        assert res.status_code == 201, (member_type, payload_type)
                        assert res.json()["member_type"] == member_type.value
                    else:
                        assert res.status_code == 422, (member_type, payload_type)
                        # Least disclosure: no submitted value echoes back.
                        assert PII_SENTINEL not in res.text
                        assert "Wanjiku" not in res.text
                        # Side effect: nothing was persisted.
                        rows = await count(
                            tid,
                            "SELECT count(*) FROM member_profiles "
                            "WHERE member_id = CAST(:m AS uuid)",
                            m=mid,
                        )
                        assert rows == 0

    asyncio.run(run())


def test_profile_create_is_idempotent_by_row_counts() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            key = uuid.uuid4().hex
            first = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(token, idem=key),
            )
            replay = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(token, idem=key),
            )
        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json() == first.json()
        # Side-effect counts, not return values (§4).
        profiles = await count(
            tid,
            "SELECT count(*) FROM member_profiles WHERE member_id = CAST(:m AS uuid)",
            m=mid,
        )
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'member_profile.create' AND entity_id = :e",
            e=first.json()["id"],
        )
        assert profiles == 1
        assert audits == 1

    asyncio.run(run())


def test_second_profile_is_refused_by_atomic_claim() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.GROUP)
            first = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.GROUP),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            second = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.GROUP),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert first.status_code == 201
        assert second.status_code == 409
        rows = await count(
            tid,
            "SELECT count(*) FROM member_profiles WHERE member_id = CAST(:m AS uuid)",
            m=mid,
        )
        assert rows == 1

    asyncio.run(run())


def test_profile_update_optimistic_lock_and_audit() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert created.status_code == 201
            ok = await client.put(
                f"/members/{mid}/profile",
                json={"version": 1, "category": "Premium"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            stale = await client.put(
                f"/members/{mid}/profile",
                json={"version": 1, "category": "Stale"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            unknown = await client.put(
                f"/members/{mid}/profile",
                json={"version": 2, "category": "X", "rate_pct": "1.00"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert ok.status_code == 200
        assert ok.json()["version"] == 2
        assert ok.json()["category"] == "Premium"
        assert stale.status_code == 409
        assert unknown.status_code == 422  # extra="forbid"
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'member_profile.update' AND entity_id = :e",
            e=created.json()["id"],
        )
        assert audits == 1
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE action = 'member_profile.update' AND entity_id = :e"
                    ),
                    {"e": created.json()["id"]},
                )
            ).first()
        assert row is not None
        assert row[0]["category"] == "Ordinary"
        assert row[1]["category"] == "Premium"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Consent: captured at registration, timestamped, immutable (DB-enforced)
# ---------------------------------------------------------------------------


def test_consent_captured_at_registration_is_timestamped() -> None:
    async def run() -> None:
        _, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.COMPANY)
            res = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.COMPANY, consent=True),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert res.status_code == 201
        assert res.json()["dpa_consent_at"] is not None

    asyncio.run(run())


def test_consent_grant_once_then_conflict() -> None:
    async def run() -> None:
        _, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON, consent=False),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert created.status_code == 201
            assert created.json()["dpa_consent_at"] is None
            granted = await client.put(
                f"/members/{mid}/profile",
                json={"version": 1, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            again = await client.put(
                f"/members/{mid}/profile",
                json={"version": 2, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            withdrawn = await client.put(
                f"/members/{mid}/profile",
                json={"version": 2, "dpa_consent": False},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert granted.status_code == 200
        assert granted.json()["dpa_consent_at"] is not None
        assert again.status_code == 409
        # dpa_consent accepts only true (Literal[True]): withdrawal is
        # structurally impossible, not merely refused.
        assert withdrawn.status_code == 422

    asyncio.run(run())


def test_consent_is_immutable_even_via_raw_sql() -> None:
    """Falsifiability (EXIT): drop the 0018 consent trigger and every
    branch of this test fails — the raw UPDATE/DELETE would succeed."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON, consent=True),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert created.status_code == 201
        captured = created.json()["dpa_consent_at"]

        # Clearing consent is refused by the DB itself.
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_profiles SET dpa_consent_at = NULL "
                        "WHERE member_id = CAST(:m AS uuid)"
                    ),
                    {"m": mid},
                )
        # Rewriting the timestamp is refused.
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE member_profiles SET dpa_consent_at = now() "
                        "WHERE member_id = CAST(:m AS uuid)"
                    ),
                    {"m": mid},
                )
        # Deleting the consent evidence is refused.
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("DELETE FROM member_profiles WHERE member_id = CAST(:m AS uuid)"),
                    {"m": mid},
                )
        # The captured value survived every attempt.
        async with tenant_session(factory(), tid) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT dpa_consent_at FROM member_profiles "
                        "WHERE member_id = CAST(:m AS uuid)"
                    ),
                    {"m": mid},
                )
            ).scalar_one()
        assert value is not None
        assert value.isoformat() == captured

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Documents: checklist claims, transitions, expiry, access audit
# ---------------------------------------------------------------------------


def test_document_checklist_wrong_type_422_and_duplicate_409() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            wrong = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "logbook"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert wrong.status_code == 422
            rows = await count(
                tid,
                "SELECT count(*) FROM member_documents WHERE member_id = CAST(:m AS uuid)",
                m=mid,
            )
            assert rows == 0
            first = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "national_id_copy"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            dup = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "national_id_copy"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert first.status_code == 201
        assert first.json()["status"] == "pending"
        assert dup.status_code == 409
        rows = await count(
            tid,
            "SELECT count(*) FROM member_documents WHERE member_id = CAST(:m AS uuid)",
            m=mid,
        )
        assert rows == 1

    asyncio.run(run())


def test_document_create_is_idempotent_by_row_counts() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.VEHICLE)
            key = uuid.uuid4().hex
            body = {"doc_type": "insurance_cert", "expires_at": "2026-09-30"}
            first = await client.post(
                f"/members/{mid}/documents", json=body, headers=_headers(token, idem=key)
            )
            replay = await client.post(
                f"/members/{mid}/documents", json=body, headers=_headers(token, idem=key)
            )
        assert first.status_code == 201
        assert first.json()["expires_at"] == "2026-09-30"
        assert replay.json() == first.json()
        rows = await count(
            tid,
            "SELECT count(*) FROM member_documents WHERE member_id = CAST(:m AS uuid)",
            m=mid,
        )
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'member_document.create' AND entity_id = :e",
            e=first.json()["id"],
        )
        assert rows == 1
        assert audits == 1

    asyncio.run(run())


def test_document_status_machine_and_stale_version() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "kra_pin"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            did = created.json()["id"]
            skip = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 1, "status": "verified"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert skip.status_code == 409  # pending -> verified is illegal
            received = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 1, "status": "received"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert received.status_code == 200
            stale = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 1, "status": "verified"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert stale.status_code == 409  # stale version
            verified = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 2, "status": "verified"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert verified.status_code == 200
            unverify = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 3, "status": "received"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert unverify.status_code == 409  # verified is terminal
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'member_document.update' AND entity_id = :e",
            e=did,
        )
        assert audits == 2  # exactly the two successful transitions

    asyncio.run(run())


def test_document_expiry_omitted_kept_explicit_null_cleared() -> None:
    """Review K2 falsifiability: a wrongly-entered expiry must be
    clearable. An update that OMITS expires_at keeps the stored value
    (a status move never wipes the expiry); an EXPLICIT null clears
    it, proven by the response AND the stored row. Fails with the
    pre-K2 'None means keep' semantics restored (the clear step would
    silently keep 2027-01-01)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "kra_pin", "expires_at": "2027-01-01"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert created.status_code == 201
            assert created.json()["expires_at"] == "2027-01-01"
            did = created.json()["id"]
            # Omitted expires_at -> kept.
            kept = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 1, "status": "received"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert kept.status_code == 200
            assert kept.json()["expires_at"] == "2027-01-01"
            # Explicit null -> cleared.
            cleared = await client.put(
                f"/members/{mid}/documents/{did}",
                json={"version": 2, "expires_at": None},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert cleared.status_code == 200
            assert cleared.json()["expires_at"] is None
        # Side-effect proof: the stored row is cleared, not just the echo.
        stored_null = await count(
            tid,
            "SELECT count(*) FROM member_documents "
            "WHERE id = CAST(:d AS uuid) AND expires_at IS NULL",
            d=did,
        )
        assert stored_null == 1

    asyncio.run(run())


def test_document_access_writes_one_audit_row_per_read() -> None:
    """EXIT: document access audited like exports (P13 blocker f),
    proven by side-effect counts."""

    async def run() -> None:
        tid, uid, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "passport_photo"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert created.status_code == 201
            first = await client.get(f"/members/{mid}/documents", headers=_headers(token))
            second = await client.get(f"/members/{mid}/documents", headers=_headers(token))
        assert first.status_code == 200
        assert second.status_code == 200
        assert [item["doc_type"] for item in first.json()["items"]] == ["passport_photo"]
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT actor_id, after FROM audit_log "
                        "WHERE action = 'member_document.access' AND entity_id = :m"
                    ),
                    {"m": mid},
                )
            ).all()
        assert len(rows) == 2  # one audit row for EVERY access
        for actor_id, after in rows:
            assert str(actor_id) == str(uid)  # who
            assert after["document_ids"] == [created.json()["id"]]  # what
            assert after["count"] == 1

    asyncio.run(run())


def test_profile_access_writes_one_audit_row_per_read() -> None:
    """Review K1: the profile body is the highest-value PII read (ID
    numbers, KRA PINs, income, next-of-kin), so every GET writes an
    in-transaction member_profile.access audit row exactly like the
    documents listing — who read whose profile. Proven by side-effect
    counts (2 reads -> 2 rows); a denied (403) read writes none, so
    the trail never claims an access that never happened. Fails with
    the record_audit call removed from read_profile."""

    async def run() -> None:
        tid, uid, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            created = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert created.status_code == 201
            first = await client.get(f"/members/{mid}/profile", headers=_headers(token))
            second = await client.get(f"/members/{mid}/profile", headers=_headers(token))
            assert first.status_code == 200
            assert second.status_code == 200
            # A denied read leaves no trail of an access that never
            # happened (the documents F4 precedent).
            _, committee_token = await add_user(tid, "Credit Committee")
            sealed = await client.get(f"/members/{mid}/profile", headers=_headers(committee_token))
            assert sealed.status_code == 403
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT actor_id, after FROM audit_log "
                        "WHERE action = 'member_profile.access' AND entity_id = :m"
                    ),
                    {"m": mid},
                )
            ).all()
        assert len(rows) == 2  # one audit row for EVERY access, none for the 403
        for actor_id, after in rows:
            assert str(actor_id) == str(uid)  # who
            assert after["member_id"] == mid  # whose profile
            assert after["profile_id"] == created.json()["id"]

    asyncio.run(run())


class _QueryCounter:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, *args: Any) -> None:
        self.value += 1


async def _counted_document_list(client: Any, mid: str, token: str) -> tuple[int, int]:
    engine = get_engine(os.environ["DATABASE_URL"])
    counter = _QueryCounter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        res = await client.get(
            f"/members/{mid}/documents", params={"limit": 50}, headers=_headers(token)
        )
        assert res.status_code == 200
        return counter.value, len(res.json()["items"])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)


def test_document_listing_query_count_is_flat() -> None:
    """No N+1 (gate 1.3): queries stay constant as the checklist grows."""

    async def run() -> None:
        _, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid}/documents",
                json={"doc_type": "kra_pin"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201
            small_queries, small_rows = await _counted_document_list(client, mid, token)
            assert small_rows == 1
            assert small_queries > 0
            for doc_type in ("national_id_copy", "passport_photo", "signature", "next_of_kin_id"):
                res = await client.post(
                    f"/members/{mid}/documents",
                    json={"doc_type": doc_type},
                    headers=_headers(token, idem=uuid.uuid4().hex),
                )
                assert res.status_code == 201
            large_queries, large_rows = await _counted_document_list(client, mid, token)
        assert large_rows == 5
        assert small_queries == large_queries, "document listing query count grew with rows (N+1)"

    asyncio.run(run())


def test_document_listing_paginates_by_doc_type_keyset() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            for doc_type in ("kra_pin", "national_id_copy", "passport_photo"):
                res = await client.post(
                    f"/members/{mid}/documents",
                    json={"doc_type": doc_type},
                    headers=_headers(token, idem=uuid.uuid4().hex),
                )
                assert res.status_code == 201
            page1 = await client.get(f"/members/{mid}/documents?limit=2", headers=_headers(token))
            cursor = page1.json()["next_cursor"]
            page2 = await client.get(
                f"/members/{mid}/documents?limit=2&cursor={cursor}",
                headers=_headers(token),
            )
        assert [i["doc_type"] for i in page1.json()["items"]] == ["kra_pin", "national_id_copy"]
        # #31 batch 13: the served cursor is the OPAQUE signed seal
        # of the same doc_type position (deterministic encode - an
        # exact-equality oracle through the real codec and the
        # exported scope symbol, never a re-typed literal).
        assert cursor == encode_cursor(
            "national_id_copy", tenant_id=tid, endpoint=kyc_service.DOCUMENTS_LIST_SCOPE
        )
        assert [i["doc_type"] for i in page2.json()["items"]] == ["passport_photo"]
        assert page2.json()["next_cursor"] is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# RBAC: deny-by-default per the P4 matrix
# ---------------------------------------------------------------------------


def test_kyc_routes_follow_the_members_matrix() -> None:
    async def run() -> None:
        tid, _, admin_token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, admin_token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(admin_token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201

            # Credit Committee holds no members:view — KYC stays sealed.
            _, committee_token = await add_user(tid, "Credit Committee")
            sealed = await client.get(f"/members/{mid}/profile", headers=_headers(committee_token))
            sealed_docs = await client.get(
                f"/members/{mid}/documents", headers=_headers(committee_token)
            )
            assert sealed.status_code == 403
            assert sealed_docs.status_code == 403

            # Teller: members:view yes, members:create/edit no.
            _, teller_token = await add_user(tid, "Teller")
            viewed = await client.get(f"/members/{mid}/profile", headers=_headers(teller_token))
            assert viewed.status_code == 200
            refused = await client.put(
                f"/members/{mid}/profile",
                json={"version": 1, "category": "Hijack"},
                headers=_headers(teller_token, idem=uuid.uuid4().hex),
            )
            assert refused.status_code == 403
        # The 403 document read never wrote an access audit row for the
        # committee user: authz denies BEFORE the audited read runs.
        denied_audits = await count(
            tid,
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'member_document.access' AND entity_id = :m",
            m=mid,
        )
        assert denied_audits == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# members.type immutability (review K3): the composite-FK failure mode
# is unreachable through the application
# ---------------------------------------------------------------------------


def _update_members_statements() -> list[str]:
    """Every SQL string in the genesis package that updates members.

    Implicitly concatenated literals parse as ONE ast.Constant, so a
    statement split across source lines is still scanned whole.
    """
    # genesis package root, derived from an already-imported module.
    root = Path(kyc_service.__file__).parents[1]
    statements: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        statements.extend(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "UPDATE members" in node.value
        )
    return statements


def test_members_type_immutable_no_app_path_and_db_backstop() -> None:
    """Review K3: the composite FK (member_id, member_type) ->
    members (id, type) turns a members.type update into a raw FK
    violation once a profile exists. That failure mode is unreachable:
    no application path updates members.type — proven structurally
    (every UPDATE-members statement in the package touches other
    columns only; no update body or service signature accepts a type)
    — and for manual SQL the FK violation IS the backstop, proven with
    a raw UPDATE that the database refuses while the member row stays
    intact. Fails if an 'UPDATE members ... type =' path is introduced
    or the composite FK is dropped."""

    async def run() -> None:
        # Structural invariant: no service path can rewrite type.
        statements = _update_members_statements()
        assert statements, "scan must see the known P8 UPDATE statements"
        offenders = [s for s in statements if re.search(r"\btype\s*=", s)]
        assert offenders == [], f"members.type must never be updated: {offenders}"
        assert "type" not in inspect.signature(members_service.update_member).parameters
        from genesis.api.members import MemberStatusBody, MemberUpdateBody

        assert "type" not in MemberUpdateBody.model_fields
        assert "type" not in MemberStatusBody.model_fields

        # DB backstop: with a profile row present, a raw type rewrite
        # is refused by the composite FK (falsifiable: drop the FK and
        # this succeeds).
        tid, _, token = await seed_actor()
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid}/profile",
                json=_profile_body(MemberType.PERSON),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE members SET type = 'company' "
                        "WHERE id = CAST(:m AS uuid) AND tenant_id = CAST(:t AS uuid)"
                    ),
                    {"m": mid, "t": str(tid)},
                )
        survivors = await count(
            tid,
            "SELECT count(*) FROM members WHERE id = CAST(:m AS uuid) AND type = 'person'",
            m=mid,
        )
        assert survivors == 1, "the member row must survive the refused rewrite"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Explicit tenant predicates (issue #17 falsifiability pattern)
# ---------------------------------------------------------------------------


def test_kyc_paths_refuse_foreign_tenant_argument() -> None:
    """Session AS the row's own tenant (RLS satisfied); services called
    with a FOREIGN tenant argument. Only the explicit bound tenant_id
    predicates can refuse the rows — remove any and this fails."""

    async def run() -> None:
        tid, uid, token = await seed_actor()
        foreign = uuid.uuid4()
        async with api_client() as client:
            mid_str = await _create_member(client, token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid_str}/profile",
                json=_profile_body(MemberType.PERSON, consent=False),
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201
            doc = await client.post(
                f"/members/{mid_str}/documents",
                json={"doc_type": "kra_pin"},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert doc.status_code == 201
        mid = uuid.UUID(mid_str)
        did = uuid.UUID(str(doc.json()["id"]))

        async with tenant_session(factory(), tid) as session:
            with pytest.raises(NotFoundError):
                await kyc_service.get_profile(session, foreign, mid)
            with pytest.raises(NotFoundError):
                await kyc_service.create_profile(
                    session,
                    foreign,
                    uid,
                    mid,
                    category="Ordinary",
                    profile=VALID_PROFILES[MemberType.PERSON],
                    dpa_consent=False,
                )
            with pytest.raises(NotFoundError):
                await kyc_service.update_profile(
                    session, foreign, uid, mid, version=1, category="Hijack"
                )
            with pytest.raises(NotFoundError):
                await kyc_service.list_documents(session, foreign, uid, mid)
            with pytest.raises(NotFoundError):
                await kyc_service.update_document(
                    session, foreign, uid, mid, did, version=1, status=None
                )
        # No write landed.
        category = await count(
            tid,
            "SELECT count(*) FROM member_profiles "
            "WHERE member_id = CAST(:m AS uuid) AND category = 'Ordinary'",
            m=mid_str,
        )
        assert category == 1

    asyncio.run(run())
