"""P13.7 tenant settings, parameters & approval matrix suite.

Covers the EXIT criteria of BUILD_PROMPTS P13.7 plus the hardened
extras:

* settings CRUD: unconfigured defaults, atomic first-row claim
  (version 0), optimistic-locked edits (stale -> 409), the concurrent
  double-submit race (exactly one 200, one 409), audit rows with exact
  before/after (side-effect counts),
* matrix coverage of GET/PUT /settings for every role (teller/auditor
  denied write), forged-body extra field -> 422, DB CHECKs proven
  independently of the app with raw SQL,
* quorum consumers: hand-computed quorum-change-mid-vote tests in BOTH
  directions for P9 committee votes and for P12 exit votes — in-flight
  votes are decided under the config read at vote time, never
  retroactively,
* approval authority bands: officer ratifying above their band -> 403
  with the stage provably unchanged (fails with the guard removed);
  band-change-mid-stage in both directions (tighten blocks the NEXT
  transition only; loosen unblocks it; committed history is never
  revisited); corrupted stored bands fail closed (409),
* explicit tenant-predicate falsifiability probes (foreign tenant id
  against an RLS-satisfied session),
* per-product guarantors_required config with bounds.

Oracles are hand-computed in comments (MASTER_PROMPT §4); every guard
test asserts both the refusal AND the untouched side-effect state so
removing the guard makes it fail.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db_helpers import api_client, factory
from export_helpers import add_user, count, seed_actor, seed_member
from genesis.application import loan_applications as applications_service
from genesis.application import member_exits as exits_service
from genesis.application import tenant_settings as settings_service
from genesis.application.deposit_interest import resolve_run_parameters
from genesis.domain.committee import Decision, Vote
from genesis.domain.lending import ApplicationStage
from genesis.domain.rbac import ROLE_NAMES, Action, Module, seed_matrix
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_PROTO_MATRIX = [
    {"authority": "Loan Officer", "max_amount": "100000.00"},
    {"authority": "Branch Manager", "max_amount": "500000.00"},
    {"authority": "Credit Committee", "max_amount": "2000000.00"},
    {"authority": "System Admin", "max_amount": None},
]


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _configure(tid: uuid.UUID, actor: uuid.UUID, **changes: object) -> int:
    """Service-level settings write; returns the new version."""
    async with tenant_session(factory(), tid) as session:
        current = await settings_service.get_settings(session, tid)
    async with tenant_session(factory(), tid) as session:
        record = await settings_service.update_settings(
            session, tid, actor, version=current.version, changes=dict(changes)
        )
    return record.version


async def _make_product(headers: dict[str, str], name: str) -> str:
    async with api_client() as client:
        res = await client.post(
            "/products",
            json={
                "name": name,
                "rate_pct": "12.00",
                "deposit_multiplier": "3.00",
                "max_term_months": 36,
            },
            headers=headers,
        )
    assert res.status_code == 201, res.text
    return str(res.json()["id"])


async def _make_application(
    tid: uuid.UUID, headers: dict[str, str], *, amount: str, name: str
) -> uuid.UUID:
    member_id = await seed_member(tid, name=name)
    async with api_client() as client:
        res = await client.post(
            "/applications",
            json={
                "member_id": str(member_id),
                "product_id": await _make_product(headers, f"P-{uuid.uuid4().hex[:8]}"),
                "amount": amount,
                "term_months": 12,
            },
            headers=headers,
        )
    assert res.status_code == 201, res.text
    return uuid.UUID(str(res.json()["id"]))


async def _advance(tid: uuid.UUID, application_id: uuid.UUID, *, to_committee: bool) -> None:
    """Drive an application forward via the explicit system-actor bypass
    (review R1): fixtures deliberately opt out of the band guard so it
    is exercised only where a test wants it. A bare actor_id=None would
    now raise (see test_bare_none_actor_transition_refused...)."""
    async with tenant_session(factory(), tid) as session:
        await applications_service.transition_stage(
            session,
            tid,
            None,
            application_id,
            version=1,
            target=ApplicationStage.APPRAISAL,
            system_actor=True,
        )
    if to_committee:
        async with tenant_session(factory(), tid) as session:
            await applications_service.transition_stage(
                session,
                tid,
                None,
                application_id,
                version=2,
                target=ApplicationStage.COMMITTEE,
                system_actor=True,
            )


async def _stage_of(tid: uuid.UUID, application_id: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        stage = (
            await session.execute(
                text("SELECT stage FROM loan_applications WHERE id = CAST(:id AS uuid)"),
                {"id": str(application_id)},
            )
        ).scalar_one()
    return str(stage)


async def _vote(
    tid: uuid.UUID, voter: uuid.UUID, application_id: uuid.UUID, vote: Vote
) -> applications_service.VoteTally:
    async with tenant_session(factory(), tid) as session:
        return await applications_service.cast_vote(session, tid, voter, application_id, vote)


# ---------------------------------------------------------------------------
# Settings CRUD, optimistic locking, audit, adversarial
# ---------------------------------------------------------------------------


def test_settings_unconfigured_defaults_then_claim_then_edit() -> None:
    async def run() -> None:
        tid, _, token = await seed_actor()
        async with api_client() as client:
            res = await client.get("/settings", headers=_headers(token))
            assert res.status_code == 200
            body = res.json()
            assert body["configured"] is False
            assert body["version"] == 0
            assert body["committee_quorum"] is None

            # First write claims the row atomically (version 0 -> 1).
            res = await client.put(
                "/settings",
                json={
                    "version": 0,
                    "registration_fee": "500.00",
                    "committee_size": 5,
                    "committee_quorum": 3,
                    "loan_rate_bands": [
                        {"min_amount": "0", "max_amount": "100000.00", "rate_pct": "10.00"},
                        {"min_amount": "100000.00", "max_amount": None, "rate_pct": "12.00"},
                    ],
                },
                headers=_headers(token),
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["configured"] is True
            assert body["version"] == 1
            assert body["registration_fee"] == "500.00"
            assert body["committee_quorum"] == 3
            assert body["loan_rate_bands"][1]["max_amount"] is None
            # exit_fee is NOT NULL DEFAULT 0 (0010): visible once the
            # row exists even though this write never sent it.
            assert body["exit_fee"] == "0.00"

            # Optimistic edit: version 1 -> 2; a repeat of version 1 is
            # stale and must 409 without a second effect.
            res = await client.put(
                "/settings",
                json={"version": 1, "registration_fee": "750.00"},
                headers=_headers(token),
            )
            assert res.status_code == 200
            assert res.json()["version"] == 2
            res = await client.put(
                "/settings",
                json={"version": 1, "registration_fee": "999.00"},
                headers=_headers(token),
            )
            assert res.status_code == 409
        # Audit side-effects: exactly one row per successful mutation
        # (2), each carrying the exact figures (gate 1.5).
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:t AS uuid) "
            "AND entity = 'tenant_settings'",
            t=str(tid),
        )
        assert audits == 2
        async with tenant_session(factory(), tid) as session:
            before, after = (
                await session.execute(
                    text(
                        "SELECT before, after FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND entity = 'tenant_settings' AND (before IS NOT NULL) LIMIT 1"
                    ),
                    {"tid": str(tid)},
                )
            ).one()
        assert before["registration_fee"] == "500.00"
        assert after["registration_fee"] == "750.00"

    asyncio.run(run())


def test_settings_double_submit_race_exactly_one_wins() -> None:
    """Two concurrent optimistic writers at the same version: exactly
    one succeeds, the other gets 409 (gate 1.4). Fails if the
    `AND version = :ver` predicate is removed from the UPDATE."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure(tid, admin_id, registration_fee=Decimal("100.00"))

        async def attempt(fee: str) -> str:
            try:
                async with tenant_session(factory(), tid) as session:
                    await settings_service.update_settings(
                        session,
                        tid,
                        admin_id,
                        version=1,
                        changes={"registration_fee": Decimal(fee)},
                    )
                return "ok"
            except ConflictError:
                return "conflict"

        outcomes = sorted(await asyncio.gather(attempt("200.00"), attempt("300.00")))
        assert outcomes == ["conflict", "ok"]
        # Side-effect proof: 1 claim + exactly 1 winning update audited.
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:t AS uuid) "
            "AND entity = 'tenant_settings'",
            t=str(tid),
        )
        assert audits == 2

    asyncio.run(run())


def test_settings_routes_matrix_every_role() -> None:
    """GET needs settings:view, PUT needs settings:edit (P4 matrix)."""

    async def run() -> None:
        matrix = seed_matrix()
        tid, admin_id, _ = await seed_actor()
        version = await _configure(tid, admin_id, registration_fee=Decimal("50.00"))
        for role_name in ROLE_NAMES:
            _, token = await add_user(tid, role_name)
            can_view = matrix[role_name][Module.SETTINGS][Action.VIEW]
            can_edit = matrix[role_name][Module.SETTINGS][Action.EDIT]
            async with api_client() as client:
                res = await client.get("/settings", headers=_headers(token))
                assert res.status_code == (200 if can_view else 403), role_name
                res = await client.put(
                    "/settings",
                    json={"version": version, "registration_fee": "60.00"},
                    headers=_headers(token),
                )
                assert res.status_code == (200 if can_edit else 403), role_name
                if can_edit:
                    version = res.json()["version"]

    asyncio.run(run())


def test_settings_forged_body_and_bounds_rejected() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        async with api_client() as client:
            # Unknown key -> 422 (extra="forbid"): settings are the
            # money parameters, nothing rides along (v1.1 rule 1).
            res = await client.put(
                "/settings",
                json={"version": 0, "penalty_backdoor_pct": "99.00"},
                headers=_headers(token),
            )
            assert res.status_code == 422
            # Out-of-bounds values -> 422 (mirrored by the DB CHECK).
            res = await client.put(
                "/settings",
                json={"version": 0, "dormancy_period_months": 0},
                headers=_headers(token),
            )
            assert res.status_code == 422
            # Band contract violations -> 422 at the boundary.
            res = await client.put(
                "/settings",
                json={
                    "version": 0,
                    "approval_bands": [{"authority": "Board", "max_amount": None}],
                },
                headers=_headers(token),
            )
            assert res.status_code == 422
            # Cross-field: quorum must not exceed committee size (400).
            res = await client.put(
                "/settings",
                json={"version": 0, "committee_size": 3, "committee_quorum": 4},
                headers=_headers(token),
            )
            assert res.status_code == 400
        # The service refuses unknown keys on its own (second fence,
        # falsifiable independently of the API model).
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(InvalidInputError):
                await settings_service.update_settings(
                    session, tid, admin_id, version=0, changes={"not_a_setting": 1}
                )

    asyncio.run(run())


def test_settings_db_checks_hold_against_raw_sql() -> None:
    """The bounds live in the DATABASE, not only the app (gate 1.5):
    a raw INSERT bypassing every application check is still refused."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        for column, value in (
            ("dividend_rate_pct", "200"),
            ("penalty_grace_days", "-1"),
            ("dormancy_period_months", "121"),
            ("committee_quorum", "0"),
        ):
            with pytest.raises(DBAPIError):
                async with tenant_session(factory(), tid) as session:
                    # Column name from the test's own literal list.
                    await session.execute(
                        text(
                            f"INSERT INTO tenant_settings (tenant_id, {column}) "  # noqa: S608
                            "VALUES (CAST(:tid AS uuid), :v)"
                        ),
                        {"tid": str(tid), "v": value},
                    )
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                # Cross-field CHECK: quorum > size refused by the DB.
                await session.execute(
                    text(
                        "INSERT INTO tenant_settings "
                        "(tenant_id, committee_size, committee_quorum) "
                        "VALUES (CAST(:tid AS uuid), 3, 4)"
                    ),
                    {"tid": str(tid)},
                )

    asyncio.run(run())


def test_settings_foreign_tenant_probe_sees_and_touches_nothing() -> None:
    """Explicit tenant-predicate falsifiability (issue #17 style): the
    session satisfies RLS as the row's own tenant; only the bound
    predicate can refuse the foreign tenant-id argument."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure(tid, admin_id, registration_fee=Decimal("100.00"))
        foreign = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            record = await settings_service.get_settings(session, foreign)
            assert record.configured is False  # empty, never tenant A's row
            with pytest.raises(ConflictError):
                await settings_service.update_settings(
                    session,
                    foreign,
                    admin_id,
                    version=1,
                    changes={"registration_fee": Decimal("999.00")},
                )
        # Tenant A's row is untouched.
        async with tenant_session(factory(), tid) as session:
            record = await settings_service.get_settings(session, tid)
        assert record.registration_fee == Decimal("100.00")
        assert record.version == 1

    asyncio.run(run())


def test_settings_row_invisible_and_immutable_across_tenants() -> None:
    """RLS fence (leakage-suite style): tenant B's session can neither
    read nor rewrite tenant A's money parameters even with raw SQL."""

    async def run() -> None:
        tid_a, admin_a, _ = await seed_actor()
        await _configure(tid_a, admin_a, registration_fee=Decimal("100.00"))
        tid_b, _, _ = await seed_actor()
        async with tenant_session(factory(), tid_b) as session:
            visible = (
                await session.execute(text("SELECT count(*) FROM tenant_settings"))
            ).scalar_one()
            assert int(visible) == 0, "tenant B can see tenant A settings"
            updated = await session.execute(
                text("UPDATE tenant_settings SET registration_fee = '999.00'")
            )
            assert updated.rowcount == 0, "tenant B can rewrite tenant A settings"
        async with tenant_session(factory(), tid_a) as session:
            record = await settings_service.get_settings(session, tid_a)
        assert record.registration_fee == Decimal("100.00")

    asyncio.run(run())


def test_deposit_interest_null_rate_is_unconfigured() -> None:
    """A settings row created without a deposit rate must behave exactly
    like a missing row for the P11 accrual job (409, unchanged
    contract). Fails if the NULL guard in resolve_run_parameters is
    removed (Decimal(str(None)) would blow up as a 500 instead)."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure(tid, admin_id, registration_fee=Decimal("10.00"))
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ConflictError):
                await resolve_run_parameters(session, tid)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Quorum consumers (P9 committee, P12 exit) — config read at vote time
# ---------------------------------------------------------------------------


def test_quorum_lowered_mid_vote_never_decides_retroactively() -> None:
    """Hand-computed: quorum 3 configured; two approvals tally 2 < 3 (no
    decision). Lowering the quorum to 2 must NOT decide the application
    (2 >= 2 would hold, but no vote event re-evaluates it). The third
    approval then decides under the config read at ITS vote time
    (approvals 3 >= quorum 2 -> approved)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, committee_size=5, committee_quorum=3)
        app_id = await _make_application(
            tid, _headers(token), amount="50000.00", name="Quorum Borrower"
        )
        await _advance(tid, app_id, to_committee=True)
        voters = [(await add_user(tid, "Credit Committee"))[0] for _ in range(3)]
        first = await _vote(tid, voters[0], app_id, Vote.APPROVE)
        second = await _vote(tid, voters[1], app_id, Vote.APPROVE)
        assert first.decision is None and second.decision is None
        assert second.stage is ApplicationStage.COMMITTEE

        # Config change mid-vote: 3 -> 2.
        await _configure(tid, admin_id, committee_quorum=2)
        # NOT retroactive: the tally (2 approvals) is not re-evaluated.
        assert await _stage_of(tid, app_id) == "committee"

        third = await _vote(tid, voters[2], app_id, Vote.APPROVE)
        assert third.decision is Decision.APPROVED
        assert third.stage is ApplicationStage.APPROVED

    asyncio.run(run())


def test_quorum_raised_mid_vote_binds_next_vote_to_new_config() -> None:
    """Hand-computed, opposite direction: quorum starts at 2; after one
    approval the quorum is raised to 3. The second approval tallies
    2 < 3 and must NOT decide (under the OLD config it would have:
    2 >= 2) — proving the quorum is read at vote time, not captured at
    vote 1 or hard-coded. The third approval decides (3 >= 3)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, committee_size=5, committee_quorum=2)
        app_id = await _make_application(
            tid, _headers(token), amount="50000.00", name="Quorum Raise Borrower"
        )
        await _advance(tid, app_id, to_committee=True)
        voters = [(await add_user(tid, "Credit Committee"))[0] for _ in range(3)]
        first = await _vote(tid, voters[0], app_id, Vote.APPROVE)
        assert first.decision is None

        await _configure(tid, admin_id, committee_quorum=3)
        second = await _vote(tid, voters[1], app_id, Vote.APPROVE)
        assert second.decision is None, "vote 2 must bind to the raised quorum"
        assert second.stage is ApplicationStage.COMMITTEE

        third = await _vote(tid, voters[2], app_id, Vote.APPROVE)
        assert third.decision is Decision.APPROVED

    asyncio.run(run())


def test_exit_quorum_change_mid_vote_not_retroactive() -> None:
    """The P12 exit workflow consumes the same quorum config at vote
    time. Hand-computed: quorum 3; two approvals leave the exit
    'requested'; lowering to 2 does not decide it; the third vote does."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure(tid, admin_id, committee_size=5, committee_quorum=3)
        member_id = await seed_member(tid, name="Exit Quorum", deposit="1000.00", shares="500.00")
        async with tenant_session(factory(), tid) as session:
            record = await exits_service.request_exit(session, tid, admin_id, member_id)
        voters = [(await add_user(tid, "Credit Committee"))[0] for _ in range(3)]
        async with tenant_session(factory(), tid) as session:
            first = await exits_service.cast_exit_vote(
                session, tid, voters[0], record.id, Vote.APPROVE
            )
        async with tenant_session(factory(), tid) as session:
            second = await exits_service.cast_exit_vote(
                session, tid, voters[1], record.id, Vote.APPROVE
            )
        assert first.decision is None and second.decision is None

        await _configure(tid, admin_id, committee_quorum=2)
        async with tenant_session(factory(), tid) as session:
            still = await exits_service.get_exit(session, tid, record.id)
        assert still.status.value == "requested", "config change must not decide retroactively"

        async with tenant_session(factory(), tid) as session:
            third = await exits_service.cast_exit_vote(
                session, tid, voters[2], record.id, Vote.APPROVE
            )
        assert third.decision is Decision.APPROVED

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Approval authority bands at stage transitions and approve votes
# ---------------------------------------------------------------------------


def test_officer_ratifying_above_band_gets_403_and_stage_unchanged() -> None:
    """EXIT criterion: officer ratifying above their band -> 403, and
    the application provably did not move (fails with the
    enforce_authority_band call removed). A Branch Manager (next band
    up) then performs the same transition successfully."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        # 200 000 sits in band 1 (Branch Manager): above the officer's
        # 100 000 ceiling, within the manager's 500 000 (hand-computed).
        app_id = await _make_application(
            tid, _headers(token), amount="200000.00", name="Band Borrower"
        )
        _, officer_token = await add_user(tid, "Loan Officer")
        _, manager_token = await add_user(tid, "Branch Manager")
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 403, res.text
        assert await _stage_of(tid, app_id) == "submitted"
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(manager_token),
            )
            assert res.status_code == 200, res.text
        assert await _stage_of(tid, app_id) == "appraisal"

    asyncio.run(run())


def test_officer_may_reject_above_band_and_unconfigured_matrix_is_uncapped() -> None:
    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        # No matrix configured: the officer may advance any amount
        # (fallback = pre-P13.7 behaviour).
        big_app = await _make_application(
            tid, _headers(token), amount="900000.00", name="Uncapped Borrower"
        )
        _, officer_token = await add_user(tid, "Loan Officer")
        async with api_client() as client:
            res = await client.post(
                f"/applications/{big_app}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 200, res.text
        # With the matrix in place, rejection above the band remains
        # allowed: it ratifies nothing and moves no money (documented).
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        async with api_client() as client:
            res = await client.post(
                f"/applications/{big_app}/transition",
                json={"version": 2, "target": "rejected"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 200, res.text
        assert await _stage_of(tid, big_app) == "rejected"

    asyncio.run(run())


def test_band_change_mid_stage_applies_forward_only_both_directions() -> None:
    """EXIT-adjacent hardening: tightening the matrix mid-workflow
    blocks the officer's NEXT transition only — the transition already
    committed under the old config stays committed (stage remains
    'appraisal', never rolled back). Loosening it re-enables the next
    transition. Hand-computed bands: 200 000 vs officer ceilings
    500 000 (allowed) then 100 000 (blocked) then 500 000 (allowed)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(
            tid,
            admin_id,
            approval_bands=[
                {"authority": "Loan Officer", "max_amount": "500000.00"},
                {"authority": "System Admin", "max_amount": None},
            ],
        )
        app_id = await _make_application(
            tid, _headers(token), amount="200000.00", name="Mid-Stage Borrower"
        )
        _, officer_token = await add_user(tid, "Loan Officer")
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 200, res.text

        # Tighten: officer ceiling drops below the amount.
        await _configure(
            tid,
            admin_id,
            approval_bands=[
                {"authority": "Loan Officer", "max_amount": "100000.00"},
                {"authority": "System Admin", "max_amount": None},
            ],
        )
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 2, "target": "committee"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 403
        # Forward-only: the already-committed appraisal move survives.
        assert await _stage_of(tid, app_id) == "appraisal"

        # Loosen again: the same next transition now proceeds.
        await _configure(
            tid,
            admin_id,
            approval_bands=[
                {"authority": "Loan Officer", "max_amount": "500000.00"},
                {"authority": "System Admin", "max_amount": None},
            ],
        )
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 2, "target": "committee"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 200, res.text
        assert await _stage_of(tid, app_id) == "committee"

    asyncio.run(run())


def test_committee_approve_vote_capped_by_band_reject_uncapped() -> None:
    """3 000 000 exceeds the Credit Committee ceiling (2 000 000): an
    approve vote is refused (403) and leaves no vote row; a reject vote
    is allowed (moves no money). Hand-computed band: 3M -> band index 3
    (System Admin tier) > CC index 2."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        app_id = await _make_application(
            tid, _headers(token), amount="3000000.00", name="Big Ticket Borrower"
        )
        await _advance(tid, app_id, to_committee=True)
        cc_id, _ = await add_user(tid, "Credit Committee")
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ForbiddenError):
                await applications_service.cast_vote(session, tid, cc_id, app_id, Vote.APPROVE)
        votes = await count(
            tid,
            "SELECT count(*) FROM committee_votes WHERE application_id = CAST(:aid AS uuid)",
            aid=str(app_id),
        )
        assert votes == 0, "a refused approve vote must leave no vote row"
        async with tenant_session(factory(), tid) as session:
            tally = await applications_service.cast_vote(session, tid, cc_id, app_id, Vote.REJECT)
        assert tally.rejections == 1

    asyncio.run(run())


def test_bare_none_actor_transition_refused_without_system_flag() -> None:
    """Review R1 falsifiability: the None-actor band bypass must be
    impossible to hit accidentally. A service call with actor_id=None
    and NO explicit system flag on a banded amount is refused with the
    stage provably unchanged and no stage-audit row; the ambiguous
    combination (an actor AND the flag) is refused too; the deliberate
    bypass works and records system_actor=true on the transition's own
    audit row. Fails with the entry guard removed (the bare None call
    would silently ratify the banded amount)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        # 200 000 requires band 1 (Branch Manager) — hand-computed.
        app_id = await _make_application(
            tid, _headers(token), amount="200000.00", name="Bare None Borrower"
        )
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(ForbiddenError):
                await applications_service.transition_stage(
                    session, tid, None, app_id, version=1, target=ApplicationStage.APPRAISAL
                )
        assert await _stage_of(tid, app_id) == "submitted"
        audits = await count(
            tid,
            "SELECT count(*) FROM audit_log WHERE tenant_id = CAST(:t AS uuid) "
            "AND action = 'application.stage' AND entity_id = :eid",
            t=str(tid),
            eid=str(app_id),
        )
        assert audits == 0, "a refused transition must leave no stage-audit row"
        # The ambiguous combination is a refused contract violation.
        async with tenant_session(factory(), tid) as session:
            with pytest.raises(InvalidInputError):
                await applications_service.transition_stage(
                    session,
                    tid,
                    admin_id,
                    app_id,
                    version=1,
                    target=ApplicationStage.APPRAISAL,
                    system_actor=True,
                )
        assert await _stage_of(tid, app_id) == "submitted"
        # The deliberate bypass works and leaves audit evidence.
        async with tenant_session(factory(), tid) as session:
            await applications_service.transition_stage(
                session,
                tid,
                None,
                app_id,
                version=1,
                target=ApplicationStage.APPRAISAL,
                system_actor=True,
            )
        assert await _stage_of(tid, app_id) == "appraisal"
        async with tenant_session(factory(), tid) as session:
            after = (
                await session.execute(
                    text(
                        "SELECT after FROM audit_log "
                        "WHERE tenant_id = CAST(:tid AS uuid) "
                        "AND action = 'application.stage' AND entity_id = :eid"
                    ),
                    {"tid": str(tid), "eid": str(app_id)},
                )
            ).scalar_one()
        assert after["system_actor"] is True

    asyncio.run(run())


def test_unlisted_role_fails_closed_at_first_band_ceiling() -> None:
    """Review R2 falsifiability, both directions: with a matrix that
    omits Branch Manager (who holds applications:edit), the manager may
    still ratify within the FIRST band's ceiling (100 000 — the
    fail-closed floor) but is refused above it (403, stage unchanged).
    Fails with the fail-open `return True` restored (the 200 000
    transition would succeed)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(
            tid,
            admin_id,
            approval_bands=[
                {"authority": "Loan Officer", "max_amount": "100000.00"},
                {"authority": "System Admin", "max_amount": None},
            ],
        )
        _, manager_token = await add_user(tid, "Branch Manager")
        # Above the floor: hand-computed 200 000 > 100 000 (band 0).
        big_app = await _make_application(
            tid, _headers(token), amount="200000.00", name="Unlisted Above Floor"
        )
        async with api_client() as client:
            res = await client.post(
                f"/applications/{big_app}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(manager_token),
            )
            assert res.status_code == 403, res.text
        assert await _stage_of(tid, big_app) == "submitted"
        # Within the floor: 50 000 <= 100 000 resolves to band 0.
        small_app = await _make_application(
            tid, _headers(token), amount="50000.00", name="Unlisted Within Floor"
        )
        async with api_client() as client:
            res = await client.post(
                f"/applications/{small_app}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(manager_token),
            )
            assert res.status_code == 200, res.text
        assert await _stage_of(tid, small_app) == "appraisal"

    asyncio.run(run())


def test_corrupted_stored_bands_fail_closed() -> None:
    """Read-side revalidation (defence against manual SQL edits): a
    stored approval matrix that passes the DB's array CHECK but breaks
    the band contract must refuse ratification (409) rather than
    silently disable the guard. The settings VIEWER degrades per-key
    instead (review R3): GET /settings stays 200 with the corrupt key
    NAMED (payload never echoed) and the version intact."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        app_id = await _make_application(
            tid, _headers(token), amount="50000.00", name="Corrupt Config Borrower"
        )
        async with tenant_session(factory(), tid) as session:
            # Manual edit bypassing the API: an array (DB CHECK passes)
            # whose band is missing max_amount (contract fails).
            await session.execute(
                text(
                    "UPDATE tenant_settings "
                    "SET approval_bands = CAST(:bands AS jsonb) "
                    "WHERE tenant_id = CAST(:tid AS uuid)"
                ),
                {"bands": '[{"authority": "Loan Officer"}]', "tid": str(tid)},
            )
        _, officer_token = await add_user(tid, "Loan Officer")
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 409, res.text
            res = await client.get("/settings", headers=_headers(token))
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["corrupt_keys"] == ["approval_bands"]
            assert body["approval_bands"] is None
            assert body["version"] == 1
        assert await _stage_of(tid, app_id) == "submitted"

    asyncio.run(run())


def test_corrupt_bands_repairable_via_get_version_then_put() -> None:
    """Review R3: the repair path must not deadlock. With corrupt
    stored bands, GET /settings still returns 200 with the version; a
    PUT carrying that version and a valid matrix repairs the row; the
    subsequent GET is clean and the consumer guard is live again.
    Fails if the viewer read reverts to 409 (the repair PUT would need
    a blindly guessed version)."""

    async def run() -> None:
        tid, admin_id, token = await seed_actor()
        await _configure(tid, admin_id, approval_bands=_PROTO_MATRIX)
        async with tenant_session(factory(), tid) as session:
            # Manual corruption bypassing the API (array passes the DB
            # CHECK; the band contract fails at read).
            await session.execute(
                text(
                    "UPDATE tenant_settings "
                    "SET approval_bands = CAST(:bands AS jsonb) "
                    "WHERE tenant_id = CAST(:tid AS uuid)"
                ),
                {"bands": '[{"authority": "Loan Officer"}]', "tid": str(tid)},
            )
        async with api_client() as client:
            res = await client.get("/settings", headers=_headers(token))
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["corrupt_keys"] == ["approval_bands"]
            version = body["version"]
            res = await client.put(
                "/settings",
                json={"version": version, "approval_bands": _PROTO_MATRIX},
                headers=_headers(token),
            )
            assert res.status_code == 200, res.text
            res = await client.get("/settings", headers=_headers(token))
            assert res.status_code == 200
            body = res.json()
            assert body["corrupt_keys"] == []
            assert body["approval_bands"] is not None
        # The guard is enforcing again after the repair: an officer
        # above their 100 000 ceiling is refused — degradation never
        # disabled the consumer-side fail-closed contract.
        app_id = await _make_application(
            tid, _headers(token), amount="200000.00", name="Post Repair Borrower"
        )
        _, officer_token = await add_user(tid, "Loan Officer")
        async with api_client() as client:
            res = await client.post(
                f"/applications/{app_id}/transition",
                json={"version": 1, "target": "appraisal"},
                headers=_headers(officer_token),
            )
            assert res.status_code == 403
        assert await _stage_of(tid, app_id) == "submitted"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Migration 0017 contracts: R4 downgrade guard, R5 role-name immutability
# ---------------------------------------------------------------------------


def _load_migration_0017() -> Any:
    """The real 0017 revision module (not a copy — falsifiability)."""
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0017_tenant_settings_general.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0017_tenant_settings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0017_downgrade_refuses_while_null_rate_rows_exist() -> None:
    """Review R4 falsifiability: the 0017 downgrade must REFUSE (never
    DELETE) when tenant_settings rows hold a NULL deposit rate. The
    guard block from the real migration raises against a tenant whose
    row was configured without a deposit rate, and the configured money
    parameters survive; once a rate is set the same guard passes. Also
    pins that the destructive DELETE is gone from _DOWN. Fails with the
    guard removed (reverted to the silent DELETE)."""

    async def run() -> None:
        migration = _load_migration_0017()
        assert "DELETE FROM tenant_settings" not in migration._DOWN
        assert migration._DOWN.startswith(migration._DOWN_GUARD)
        tid, admin_id, _ = await seed_actor()
        await _configure(tid, admin_id, registration_fee=Decimal("500.00"))
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))
        # The refused downgrade destroyed nothing.
        async with tenant_session(factory(), tid) as session:
            record = await settings_service.get_settings(session, tid)
        assert record.registration_fee == Decimal("500.00")
        # The documented manual step (set a rate) satisfies the guard.
        await _configure(tid, admin_id, deposit_interest_annual_rate_pct=Decimal("5.00"))
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())


def test_system_role_rename_refused_by_db_trigger() -> None:
    """Review R5: approval-band authorities key off seeded role NAMES,
    so a rename would silently detach a configured money ceiling. No
    application path updates roles.name (application/rbac.py mutates
    permissions only); the 0017 trigger enforces the invariant at the
    DB, so raw SQL through the app role is refused too. Fails with the
    trigger dropped."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("UPDATE roles SET name = 'Renamed Officer' WHERE name = 'Loan Officer'")
                )
        async with tenant_session(factory(), tid) as session:
            survivors = (
                await session.execute(
                    text("SELECT count(*) FROM roles WHERE name = 'Loan Officer'")
                )
            ).scalar_one()
        assert int(survivors) == 1, "the seeded role name must survive the refused rename"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Per-product guarantors-required (stored config)
# ---------------------------------------------------------------------------


def test_product_guarantors_required_config() -> None:
    async def run() -> None:
        _tid, _, token = await seed_actor()
        async with api_client() as client:
            res = await client.post(
                "/products",
                json={
                    "name": f"G-{uuid.uuid4().hex[:8]}",
                    "rate_pct": "12.00",
                    "deposit_multiplier": "3.00",
                    "max_term_months": 36,
                    "guarantors_required": 3,
                },
                headers=_headers(token),
            )
            assert res.status_code == 201, res.text
            assert res.json()["guarantors_required"] == 3
            product_id = res.json()["id"]
            res = await client.put(
                f"/products/{product_id}",
                json={"version": 1, "guarantors_required": 5},
                headers=_headers(token),
            )
            assert res.status_code == 200
            assert res.json()["guarantors_required"] == 5
            # Bounds mirror the DB CHECK (0..10).
            res = await client.put(
                f"/products/{product_id}",
                json={"version": 2, "guarantors_required": 11},
                headers=_headers(token),
            )
            assert res.status_code == 422

    asyncio.run(run())
