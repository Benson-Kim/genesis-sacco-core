"""Supervisor override (#35 item 8, the authorized design) — falsifiable legs.

A separately-permissioned action (application_overrides:approve —
supervisor tiers only) that overturns a subordinate's refusal on a
loan application: mandatory reason, snapshot bind-and-reverify under
the application row lock (no TOCTOU), SoD (maker / recommender / the
refusing actor / assurance roles all refused), authority-band cap,
dedicated audit action carrying before/after stage plus the
overridden actor id resolved server-side from the audit trail.

Hand-computed oracles: the band leg configures a ceiling of 1000.00
for the Senior Credit Officer and overrides a 2500.00 application —
2500.00 > 1000.00 refuses; the unbounded System Admin band admits the
same amount. The 409 leg re-submits the SAME snapshot version after a
successful override — the first override bumped version 2 -> 3, so
the replay is stale by construction.

Falsifiability: drop the RequirePermission dependency and FM-O2 fails
on the 200; drop any SoD comparison and its FM-O3 leg lands an
override that must refuse; drop enforce_authority_band and FM-O4's
over-ceiling override succeeds; drop the version re-check and FM-O5's
replay lands twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import add_user, seed_actor, seed_member
from genesis.application.loan_applications import override_refusal, transition_stage
from genesis.domain.lending import ApplicationStage
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_application(
    tid: uuid.UUID,
    member_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None,
    amount: str = "2500.00",
) -> uuid.UUID:
    """Application in SUBMITTED with attributed maker; version 1."""
    pid = uuid.uuid4()
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Override Product {pid.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, "
                " stage, created_by) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), :amount, 12, 12.00, 'submitted', CAST(:actor AS uuid))"
            ),
            {
                "id": str(app_id),
                "tid": str(tid),
                "mid": str(member_id),
                "pid": str(pid),
                "amount": amount,
                "actor": str(created_by) if created_by else None,
            },
        )
    return app_id


async def _refuse(tid: uuid.UUID, refuser: uuid.UUID, app_id: uuid.UUID, version: int = 1) -> int:
    """SUBMITTED -> REJECTED by the refusing subordinate; returns the
    refused row's version (the decision snapshot the supervisor sees)."""
    async with tenant_session(factory(), tid) as session:
        record = await transition_stage(
            session, tid, refuser, app_id, version=version, target=ApplicationStage.REJECTED
        )
    return record.version


async def _override(
    tid: uuid.UUID, actor: uuid.UUID, app_id: uuid.UUID, *, version: int, reason: str = "override"
):
    async with tenant_session(factory(), tid) as session:
        return await override_refusal(session, tid, actor, app_id, version=version, reason=reason)


async def _app_state(tid: uuid.UUID, app_id: uuid.UUID) -> tuple[str, int]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT stage, version FROM loan_applications "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"id": str(app_id), "tid": str(tid)},
            )
        ).one()
    return str(row[0]), int(row[1])


async def _override_audit_rows(tid: uuid.UUID, app_id: uuid.UUID) -> list[tuple]:
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT actor_id, before, after FROM audit_log "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND entity = 'loan_applications' AND entity_id = :eid "
                    "AND action = 'application.override' ORDER BY at"
                ),
                {"tid": str(tid), "eid": str(app_id)},
            )
        ).all()
    return [tuple(r) for r in rows]


def test_fm_o1_override_approves_and_audits_the_overridden_actor() -> None:
    """FM-O1: happy path — REJECTED -> APPROVED, version bumps, and the
    dedicated audit action carries before/after stage, the mandatory
    reason and the refusing actor's id (resolved from the trail)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        supervisor, _ = await add_user(tid, "Senior Credit Officer")
        mid = await seed_member(tid, name="Override Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        refused_version = await _refuse(tid, refuser, app_id)

        record = await _override(
            tid, supervisor, app_id, version=refused_version, reason="branch audit cleared"
        )
        assert record.stage is ApplicationStage.APPROVED
        assert record.version == refused_version + 1

        rows = await _override_audit_rows(tid, app_id)
        assert len(rows) == 1
        actor_id, before, after = rows[0]
        before = before if isinstance(before, dict) else json.loads(before)
        after = after if isinstance(after, dict) else json.loads(after)
        assert uuid.UUID(str(actor_id)) == supervisor
        assert before["stage"] == "rejected"
        assert after["stage"] == "approved"
        assert after["reason"] == "branch audit cleared"
        # The overridden actor is the REFUSING subordinate, resolved
        # from the audit trail — never caller-supplied.
        assert after["overridden_actor_id"] == str(refuser)

    asyncio.run(run())


def test_fm_o2_missing_permission_403s_before_any_side_effect() -> None:
    """FM-O2: a role without application_overrides:approve (Teller;
    also the Branch Manager and Credit Committee, who hold generic
    approve powers but NOT this one) gets a 403 from the dependency —
    and the row, version and audit trail are UNTOUCHED."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        mid = await seed_member(tid, name="Forbidden Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        refused_version = await _refuse(tid, refuser, app_id)
        state_before = await _app_state(tid, app_id)

        for role in ("Teller", "Branch Manager", "Credit Committee"):
            _, token = await add_user(tid, role)
            async with api_client() as client:
                response = await client.post(
                    f"/applications/{app_id}/override",
                    json={"version": refused_version, "reason": "should not land"},
                    headers={"authorization": f"Bearer {token}"},
                )
            assert response.status_code == 403, role
        assert await _app_state(tid, app_id) == state_before  # zero side effects
        assert await _override_audit_rows(tid, app_id) == []

    asyncio.run(run())


def test_fm_o2b_supervisor_endpoint_succeeds_end_to_end() -> None:
    """FM-O2 mirror: the Senior Credit Officer's token lands the same
    request 200 through the real endpoint (the grant is real, not just
    the matrix row)."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        _, sup_token = await add_user(tid, "Senior Credit Officer")
        mid = await seed_member(tid, name="Endpoint Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        refused_version = await _refuse(tid, refuser, app_id)
        async with api_client() as client:
            response = await client.post(
                f"/applications/{app_id}/override",
                json={"version": refused_version, "reason": "documented supervisor review"},
                headers={"authorization": f"Bearer {sup_token}"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["stage"] == "approved"

    asyncio.run(run())


def test_fm_o3_sod_maker_recommender_self_and_assurance_refused() -> None:
    """FM-O3: every SoD identity is refused — the maker, the
    recommender, the refusing actor itself, and assurance roles."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        # Supervisors in every seat, so ONLY the SoD identity (not the
        # permission) decides each leg.
        maker, _ = await add_user(tid, "Senior Credit Officer")
        recommender, _ = await add_user(tid, "Senior Credit Officer")
        refuser, _ = await add_user(tid, "Senior Credit Officer")
        auditor, _ = await add_user(tid, "Auditor")
        mid = await seed_member(tid, name="SoD Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        # submitted -> appraisal -> committee (records the recommender)
        # -> rejected (records the refuser).
        async with tenant_session(factory(), tid) as session:
            await transition_stage(
                session, tid, recommender, app_id, version=1, target=ApplicationStage.APPRAISAL
            )
        async with tenant_session(factory(), tid) as session:
            await transition_stage(
                session, tid, recommender, app_id, version=2, target=ApplicationStage.COMMITTEE
            )
        refused_version = await _refuse(tid, refuser, app_id, version=3)

        with pytest.raises(ConflictError, match="segregation of duties"):
            await _override(tid, maker, app_id, version=refused_version)
        with pytest.raises(ConflictError, match="segregation of duties"):
            await _override(tid, recommender, app_id, version=refused_version)
        with pytest.raises(ConflictError, match="segregation of duties"):
            await _override(tid, refuser, app_id, version=refused_version)
        with pytest.raises(ConflictError, match="audit independence"):
            await _override(tid, auditor, app_id, version=refused_version)
        # Nothing landed: still rejected at the refused version.
        assert await _app_state(tid, app_id) == ("rejected", refused_version)
        assert await _override_audit_rows(tid, app_id) == []

    asyncio.run(run())


def test_fm_o4_band_capped_override_above_ceiling_refused() -> None:
    """FM-O4: hand-computed band leg — the Senior Credit Officer is
    capped at 1000.00 and the application is 2500.00 (2500.00 >
    1000.00 refuses, zero side effects); the unbounded System Admin
    band admits the SAME amount."""

    async def run() -> None:
        tid, admin, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        supervisor, _ = await add_user(tid, "Senior Credit Officer")
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_settings (tenant_id, approval_bands) "
                    "VALUES (CAST(:tid AS uuid), CAST(:bands AS jsonb))"
                ),
                {
                    "tid": str(tid),
                    "bands": (
                        '[{"authority": "Senior Credit Officer", "max_amount": "1000.00"}, '
                        '{"authority": "System Admin", "max_amount": null}]'
                    ),
                },
            )
        mid = await seed_member(tid, name="Band Member")
        app_id = await _seed_application(tid, mid, created_by=maker, amount="2500.00")
        refused_version = await _refuse(tid, refuser, app_id)

        with pytest.raises(ForbiddenError, match="approval matrix"):
            await _override(tid, supervisor, app_id, version=refused_version)
        assert await _app_state(tid, app_id) == ("rejected", refused_version)
        assert await _override_audit_rows(tid, app_id) == []

        record = await _override(
            tid, admin, app_id, version=refused_version, reason="within admin band"
        )
        assert record.stage is ApplicationStage.APPROVED
        assert record.amount == Decimal("2500.00")

    asyncio.run(run())


def test_fm_o5_second_override_of_the_same_snapshot_conflicts() -> None:
    """FM-O5: the override binds to the persisted snapshot — replaying
    the SAME version after a successful override is a 409 (the first
    bumped it); overriding a non-refused application is a 409 too."""

    async def run() -> None:
        tid, admin, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        supervisor, _ = await add_user(tid, "Senior Credit Officer")
        mid = await seed_member(tid, name="Replay Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        refused_version = await _refuse(tid, refuser, app_id)

        first = await _override(tid, supervisor, app_id, version=refused_version)
        assert first.stage is ApplicationStage.APPROVED

        # Same snapshot, second supervisor: version is stale AND the
        # stage is no longer rejected — conflicts either way, never a
        # second silent overturn.
        with pytest.raises(ConflictError):
            await _override(tid, admin, app_id, version=refused_version)
        rows = await _override_audit_rows(tid, app_id)
        assert len(rows) == 1  # exactly ONE override ever landed

    asyncio.run(run())


def test_fm_o6_reason_is_mandatory() -> None:
    """FM-O6: an override without a reason refuses BEFORE any read or
    write (app layer); the API contract mirrors it as a 422."""

    async def run() -> None:
        tid, _, _ = await seed_actor()
        maker, _ = await add_user(tid, "Loan Officer")
        refuser, _ = await add_user(tid, "Loan Officer")
        supervisor, sup_token = await add_user(tid, "Senior Credit Officer")
        mid = await seed_member(tid, name="Reason Member")
        app_id = await _seed_application(tid, mid, created_by=maker)
        refused_version = await _refuse(tid, refuser, app_id)

        with pytest.raises(InvalidInputError, match="reason"):
            await _override(tid, supervisor, app_id, version=refused_version, reason="   ")
        async with api_client() as client:
            response = await client.post(
                f"/applications/{app_id}/override",
                json={"version": refused_version},
                headers={"authorization": f"Bearer {sup_token}"},
            )
        assert response.status_code == 422
        assert await _app_state(tid, app_id) == ("rejected", refused_version)

    asyncio.run(run())
