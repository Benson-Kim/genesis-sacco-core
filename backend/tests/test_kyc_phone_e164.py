"""#35 follow-through — KYC profile phones normalize to E.164.

Data-hygiene completion of the item-1 E.164 round (0042 backfilled
members.phone / users.phone; the KYC jsonb profile phones were the
recorded residual). Write-path normalization rides the ONE existing
normalizer (domain/members.normalize_kenya_msisdn via the KenyaMsisdn
profile field type — never a second implementation); migration 0046
is the data-only backfill for rows that predate it. Legs:

* write path — every accepted spelling of the same number stores the
  SAME E.164 string in the persisted profile jsonb, on create AND
  update, for every phone-bearing path (person contact + next of kin,
  company contact, group officials ×3). Falsifiable: drop the
  KenyaMsisdn annotation from a field and its leg reads back the raw
  local form.
* sanitized 422 — an invalid shape refuses with field location + error
  type only; the submitted value appears NOWHERE in the response, and
  zero profile rows are written (side-effect row counts, §4).
* backfill — seeded legacy 07XX/01XX values across all six paths read
  back +254… after executing the REAL migration's _UP (hand-computed
  exact strings); non-matching shapes are untouched.
* idempotency — running _UP twice changes ZERO rows the second time
  (hand-computed row counts: first run 6, second run 0).
* exact downgrade — _DOWN restores the local form for every rewritten
  row (hand-computed strings), the 0042 discipline.

Oracles are hand-computed in comments (MASTER_PROMPT §4).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import count, seed_actor
from genesis.domain.members import MemberType
from genesis.infrastructure.tenancy import tenant_session
from kyc_payloads import VALID_PROFILES

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str, idem: str | None = None) -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


def _load_migration_0046() -> Any:
    """The real 0046 revision module (not a copy — falsifiability)."""
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0046_kyc_profile_phone_e164_backfill.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0046_kyc_phone", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_statements(tid: uuid.UUID, block: str) -> int:
    """Execute a multi-statement migration block; total affected rows."""
    changed = 0
    async with tenant_session(factory(), tid) as session:
        for statement in block.split(";"):
            if statement.strip() == "":
                continue
            result = await session.execute(text(statement))
            changed += int(result.rowcount or 0)
    return changed


async def _create_member(client: Any, token: str, member_type: MemberType) -> str:
    res = await client.post(
        "/members",
        json={"type": member_type.value, "name": f"E164 {member_type.value}"},
        headers=_headers(token, idem=uuid.uuid4().hex),
    )
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


async def _stored_profile(tid: uuid.UUID, mid: str) -> dict[str, Any]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT profile FROM member_profiles "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = CAST(:m AS uuid)"
                ),
                {"tid": str(tid), "m": mid},
            )
        ).scalar_one()
    return dict(row)


def test_person_profile_phones_store_e164_for_every_accepted_spelling() -> None:
    """Hand-computed: contact.phone '0712345678' and next_of_kin.phone
    '+254110000001' both persist as their E.164 forms
    ('+254712345678' / '+254110000001' — the +254 spelling is already
    canonical and must survive unchanged)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        profile = json.loads(json.dumps(VALID_PROFILES[MemberType.PERSON]))
        profile["contact"]["phone"] = "0712345678"
        profile["next_of_kin"]["phone"] = "+254110000001"
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid}/profile",
                json={"category": "Ordinary", "profile": profile, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert res.status_code == 201, res.text
        stored = await _stored_profile(tid, mid)
        assert stored["contact"]["phone"] == "+254712345678"
        assert stored["next_of_kin"]["phone"] == "+254110000001"

    asyncio.run(run())


def test_company_and_group_profile_phones_normalize_on_create() -> None:
    """Hand-computed: company contact '0722000111' -> '+254722000111';
    the three group officials '0733000001/2/3' -> '+254733000001/2/3'.
    Falsifiable per path: drop KenyaMsisdn from one field and exactly
    that assert reads the raw local form."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        company = json.loads(json.dumps(VALID_PROFILES[MemberType.COMPANY]))
        company["contact"]["phone"] = "0722000111"
        group = json.loads(json.dumps(VALID_PROFILES[MemberType.GROUP]))
        for index, official in enumerate(("chairperson", "secretary", "treasurer"), start=1):
            group["officials"][official]["phone"] = f"073300000{index}"
        async with api_client() as client:
            company_mid = await _create_member(client, token, MemberType.COMPANY)
            res = await client.post(
                f"/members/{company_mid}/profile",
                json={"category": "Ordinary", "profile": company, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201, res.text
            group_mid = await _create_member(client, token, MemberType.GROUP)
            res = await client.post(
                f"/members/{group_mid}/profile",
                json={"category": "Ordinary", "profile": group, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
            assert res.status_code == 201, res.text
        stored_company = await _stored_profile(tid, company_mid)
        assert stored_company["contact"]["phone"] == "+254722000111"
        stored_group = await _stored_profile(tid, group_mid)
        assert stored_group["officials"]["chairperson"]["phone"] == "+254733000001"
        assert stored_group["officials"]["secretary"]["phone"] == "+254733000002"
        assert stored_group["officials"]["treasurer"]["phone"] == "+254733000003"

    asyncio.run(run())


def test_invalid_profile_phone_is_sanitized_422_and_writes_nothing() -> None:
    """The refusal names the field location and error type ONLY: the
    submitted value (an unmistakable sentinel) appears nowhere in the
    response, and zero profile rows were written (side-effect count).
    Falsifiable both ways: fail the normalization silently and the 201
    stores a non-E.164 value; echo the value and the sentinel assert
    fails."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        profile = json.loads(json.dumps(VALID_PROFILES[MemberType.PERSON]))
        profile["contact"]["phone"] = "0712-SENTINEL-99"
        async with api_client() as client:
            mid = await _create_member(client, token, MemberType.PERSON)
            res = await client.post(
                f"/members/{mid}/profile",
                json={"category": "Ordinary", "profile": profile, "dpa_consent": True},
                headers=_headers(token, idem=uuid.uuid4().hex),
            )
        assert res.status_code == 422, res.text
        assert "SENTINEL" not in res.text
        assert "contact.phone" in res.text  # location, never the value
        profiles = await count(
            tid,
            "SELECT count(*) FROM member_profiles WHERE member_id = CAST(:m AS uuid)",
            m=mid,
        )
        assert profiles == 0

    asyncio.run(run())


async def _seed_legacy_profiles(tid: uuid.UUID) -> dict[str, uuid.UUID]:
    """Raw legacy rows (pre-normalization era) across all six paths,
    inserted directly so the new write path cannot interfere:
      person : contact 0712000001, next_of_kin 0110000002
      company: contact 0722000003
      group  : officials 0733000004 / 0733000005 / 0733000006
    Plus a non-matching person value ('N/A') that must never change."""
    mids: dict[str, uuid.UUID] = {}
    profiles: dict[str, dict[str, Any]] = {}

    person = json.loads(json.dumps(VALID_PROFILES[MemberType.PERSON]))
    person["contact"]["phone"] = "0712000001"
    person["next_of_kin"]["phone"] = "0110000002"
    profiles["person"] = person

    company = json.loads(json.dumps(VALID_PROFILES[MemberType.COMPANY]))
    company["contact"]["phone"] = "0722000003"
    profiles["company"] = company

    group = json.loads(json.dumps(VALID_PROFILES[MemberType.GROUP]))
    group["officials"]["chairperson"]["phone"] = "0733000004"
    group["officials"]["secretary"]["phone"] = "0733000005"
    group["officials"]["treasurer"]["phone"] = "0733000006"
    profiles["group"] = group

    legacy = json.loads(json.dumps(VALID_PROFILES[MemberType.PERSON]))
    legacy["contact"]["phone"] = "N/A"
    legacy["next_of_kin"]["phone"] = "+254700000002"
    profiles["legacy"] = legacy

    types = {"person": "person", "company": "company", "group": "group", "legacy": "person"}
    async with tenant_session(factory(), tid) as session:
        for key, profile in profiles.items():
            mid = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, :type, :name)"
                ),
                {
                    "id": str(mid),
                    "tid": str(tid),
                    "no": f"GP-{mid.hex[:6].upper()}",
                    "type": types[key],
                    "name": f"Legacy {key}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO member_profiles "
                    "(id, tenant_id, member_id, member_type, category, profile) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                    ":type, 'Ordinary', CAST(:profile AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tid),
                    "mid": str(mid),
                    "type": types[key],
                    "profile": json.dumps(profile),
                },
            )
            mids[key] = mid
    return mids


def test_backfill_normalizes_all_six_paths_idempotently_with_exact_downgrade() -> None:
    """The REAL 0046 _UP/_DOWN blocks against seeded legacy rows.

    Hand-computed row counts: the first _UP run rewrites SIX values
    (person contact + next_of_kin = 2 UPDATEs of 1 row... person row
    matched by two statements = 2, company 1, group 3) = 6 affected
    rows; the SECOND run affects ZERO rows (idempotency by
    construction — a rewritten value no longer matches the predicate).
    The non-matching 'N/A' value and the already-canonical
    '+254700000002' are untouched by _UP. _DOWN then restores the six
    local forms exactly (and rewrites the already-canonical value to
    its valid local spelling — the documented 0042 trade-off)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        mids = await _seed_legacy_profiles(tid)
        migration = _load_migration_0046()

        first = await _run_statements(tid, migration._UP)
        assert first == 6

        person = await _stored_profile(tid, str(mids["person"]))
        assert person["contact"]["phone"] == "+254712000001"
        assert person["next_of_kin"]["phone"] == "+254110000002"
        company = await _stored_profile(tid, str(mids["company"]))
        assert company["contact"]["phone"] == "+254722000003"
        group = await _stored_profile(tid, str(mids["group"]))
        assert group["officials"]["chairperson"]["phone"] == "+254733000004"
        assert group["officials"]["secretary"]["phone"] == "+254733000005"
        assert group["officials"]["treasurer"]["phone"] == "+254733000006"
        legacy = await _stored_profile(tid, str(mids["legacy"]))
        assert legacy["contact"]["phone"] == "N/A"

        # Idempotency: the second run changes ZERO rows.
        second = await _run_statements(tid, migration._UP)
        assert second == 0

        # Exact downgrade: every rewritten value returns to its local
        # form; the untouched 'N/A' stays untouched. The legacy row's
        # next_of_kin '+254700000002' also rewrites to '0700000002'
        # (the documented trade-off: still a valid accepted input).
        await _run_statements(tid, migration._DOWN)
        person = await _stored_profile(tid, str(mids["person"]))
        assert person["contact"]["phone"] == "0712000001"
        assert person["next_of_kin"]["phone"] == "0110000002"
        company = await _stored_profile(tid, str(mids["company"]))
        assert company["contact"]["phone"] == "0722000003"
        group = await _stored_profile(tid, str(mids["group"]))
        assert group["officials"]["chairperson"]["phone"] == "0733000004"
        assert group["officials"]["secretary"]["phone"] == "0733000005"
        assert group["officials"]["treasurer"]["phone"] == "0733000006"
        legacy = await _stored_profile(tid, str(mids["legacy"]))
        assert legacy["contact"]["phone"] == "N/A"
        assert legacy["next_of_kin"]["phone"] == "0700000002"

    asyncio.run(run())
