"""Integration tests for the issue-#8 (G2) approval engine — ADR-0008.

Requires a migrated PostgreSQL database (DATABASE_URL env var).

Failure modes (v1.2 rule 15) — every oracle HAND-COMPUTED in comments,
never captured from the code under test; every guard test fails with
its guard removed (MASTER_PROMPT §4):

  FM1 boundary drift        — the engine's <= vs < at exactly
                              100k/500k/2M through submit_operation
                              (golden boundaries; the pure falsifiers
                              live in test_approvals_domain.py).
  FM2 maker checks own      — ratify by the maker -> 409 with zero
                              side effects (application/sod.py); direct
                              SQL maker = checker -> the 0049
                              ck_pending_approvals_sod CHECK violation
                              (collusion-resistant, DB layer).
  FM3 band change in flight — a pending approval RE-RESOLVES bands at
                              ratification; the STRICTER of request-
                              time and current bands applies in BOTH
                              directions (tighten binds; loosen never
                              weakens).
  FM4 assurance checker     — an Auditor ratifying/declining -> 409
                              (audit independence, beneath RBAC).
  FM5 band history rewrite  — approval_band_sets refuses UPDATE and
                              DELETE at the DB (append-only trigger);
                              a duplicate effective date is a 409 via
                              the atomic claim (service layer).
  FM6 workflow rewrite      — pending_approvals is write-once: pinned
                              money/identity columns, one-shot decision
                              fills, terminal statuses terminal, no
                              DELETE (the 0031 discipline).
  FM7 cross-tenant bands    — tenant B resolves ITS OWN bands (never
                              tenant A's) and cannot read, mutate or
                              forge A's engine rows via raw SQL
                              (ADR-0002).
  FM8 corrupt config        — stored bands failing read revalidation
                              fail CLOSED (409), never a silently
                              skipped guard.
  FM9 board tier            — above a finite top band NO platform
                              principal ratifies; the request stays
                              pending.

Plus the 0049 loud-refusal downgrade guard executed from the REAL
migration module (the 0017/0031 _DOWN_GUARD falsifiability precedent)
and the structural pin of transactions.checked_by (both principals on
resulting postings — the 0036 created_by extension).
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

from db_helpers import factory, seed_user, unique_email
from genesis.application.approvals import (
    band_schedules,
    bands_as_of,
    configure_bands,
    decline_pending,
    ratify_pending,
    submit_operation,
)
from genesis.application.rbac import seed_permissions
from genesis.domain.approvals import DEFAULT_APPROVAL_BANDS, OperationType
from genesis.domain.rbac import (
    AUDITOR,
    BRANCH_MANAGER,
    CREDIT_COMMITTEE,
    LOAN_OFFICER,
    SYSTEM_ADMIN,
)
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

_OP = OperationType.LOAN_DISBURSEMENT

#: Tightened matrix: 400k moves from Branch-Manager tier (defaults) to
#: Credit-Committee tier. Loosened matrix: 400k moves to Loan-Officer
#: tier. Both hand-computed against the band boundaries.
_STRICT_BANDS = [
    {"authority": LOAN_OFFICER, "max_amount": "50000.00"},
    {"authority": BRANCH_MANAGER, "max_amount": "200000.00"},
    {"authority": CREDIT_COMMITTEE, "max_amount": "2000000.00"},
]
_LOOSE_BANDS = [
    {"authority": LOAN_OFFICER, "max_amount": "1000000.00"},
    {"authority": BRANCH_MANAGER, "max_amount": "1500000.00"},
    {"authority": CREDIT_COMMITTEE, "max_amount": "2000000.00"},
]


# ---------------------------------------------------------------------------
# Fixtures (the test_maker_checker.py house style)
# ---------------------------------------------------------------------------


async def _tenant() -> uuid.UUID:
    """A tenant with the full seeded role matrix."""
    tid, _ = await seed_user(unique_email())
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
    return tid


async def _user(tid: uuid.UUID, role_name: str) -> uuid.UUID:
    """An active user holding the named seeded role."""
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"), {"name": role_name}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Engine Test User', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
    return user_id


async def _submit(
    tid: uuid.UUID, maker: uuid.UUID, amount: str, *, operation: OperationType = _OP
) -> Any:
    async with tenant_session(factory(), tid) as session:
        return await submit_operation(
            session, tid, maker, operation=operation, amount=Decimal(amount)
        )


async def _pending_row(tid: uuid.UUID, pending_id: uuid.UUID) -> Any:
    async with tenant_session(factory(), tid) as session:
        return (
            await session.execute(
                text(
                    "SELECT status, checker_id, decided_at, decision_reason, "
                    "required_tier_at_request, maker_id, amount "
                    "FROM pending_approvals WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(pending_id)},
            )
        ).one()


async def _backdated_pending(
    tid: uuid.UUID, maker: uuid.UUID, amount: str, tier_at_request: int
) -> uuid.UUID:
    """A pending row requested YESTERDAY (the band-change scenarios:
    requested_at has DEFAULT now() in the engine path, so the
    request-day/decision-day split is seeded directly; the write-once
    trigger only guards UPDATE/DELETE)."""
    pending_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO pending_approvals "
                "(id, tenant_id, operation_type, amount, maker_id, "
                "required_tier_at_request, requested_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :op, :amount, "
                "CAST(:maker AS uuid), :tier, now() - interval '1 day')"
            ),
            {
                "id": str(pending_id),
                "tid": str(tid),
                "op": _OP.value,
                "amount": Decimal(amount),
                "maker": str(maker),
                "tier": tier_at_request,
            },
        )
    return pending_id


async def _configure_effective_today(
    tid: uuid.UUID, actor: uuid.UUID, bands: list[dict[str, str | None]]
) -> None:
    async with tenant_session(factory(), tid) as session:
        today = (await session.execute(text("SELECT current_date"))).scalar_one()
        await configure_bands(session, tid, actor, bands=bands, effective_from=today)


# ---------------------------------------------------------------------------
# FM1 — golden band boundaries through the engine (<= vs < falsifiers)
# ---------------------------------------------------------------------------


def test_below_band_proceeds_and_above_band_pends_at_exact_boundaries() -> None:
    """100,000.00 by a Loan Officer proceeds (tier 0); 100,000.01 pends
    at tier 1; likewise 500k/BM -> tier 2 and 2M/CC -> tier 3. Each
    boundary hand-computed against the prototype matrix."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        committee = await _user(tid, CREDIT_COMMITTEE)

        at_ceiling = await _submit(tid, officer, "100000.00")
        assert at_ceiling.proceed is True
        assert at_ceiling.required_tier == 0
        assert at_ceiling.pending_id is None

        over = await _submit(tid, officer, "100000.01")
        assert over.proceed is False
        assert over.required_tier == 1
        assert over.pending_id is not None
        row = await _pending_row(tid, over.pending_id)
        # The row records the maker, the tier snapshot and no checker.
        assert str(row[0]) == "pending"
        assert row[1] is None
        assert int(row[4]) == 1
        assert uuid.UUID(str(row[5])) == officer
        assert Decimal(str(row[6])) == Decimal("100000.01")

        assert (await _submit(tid, manager, "500000.00")).proceed is True
        mgr_over = await _submit(tid, manager, "500000.01")
        assert mgr_over.proceed is False
        assert mgr_over.required_tier == 2

        assert (await _submit(tid, committee, "2000000.00")).proceed is True
        board = await _submit(tid, committee, "2000000.01")
        assert board.proceed is False
        assert board.required_tier == 3

    asyncio.run(run())


def test_pending_request_writes_its_audit_row_in_transaction() -> None:
    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        outcome = await _submit(tid, officer, "150000.00")
        assert outcome.pending_id is not None
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT actor_id, action FROM audit_log "
                        "WHERE entity = 'pending_approvals' AND entity_id = :eid"
                    ),
                    {"eid": str(outcome.pending_id)},
                )
            ).one()
        assert uuid.UUID(str(row[0])) == officer
        assert str(row[1]) == "approval.request"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — maker-self-check refused at BOTH layers
# ---------------------------------------------------------------------------


def test_maker_cannot_check_own_request_app_layer() -> None:
    """The application/sod.py guard (reuse-first, ONE copy of the
    rule): the maker ratifying or declining their own request is a 409
    with zero side effects."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        outcome = await _submit(tid, officer, "150000.00")
        assert outcome.pending_id is not None

        with pytest.raises(ConflictError, match="segregation of duties"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, officer, outcome.pending_id)
        with pytest.raises(ConflictError, match="segregation of duties"):
            async with tenant_session(factory(), tid) as session:
                await decline_pending(session, tid, officer, outcome.pending_id, reason="mine")

        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "pending"
        assert row[1] is None and row[2] is None

    asyncio.run(run())


def test_maker_cannot_check_own_request_db_layer() -> None:
    """Direct SQL on the app role setting checker_id = maker_id
    violates the 0049 ck_pending_approvals_sod CHECK: a
    maker-ratified-own row is UNREPRESENTABLE, not merely refused by
    the service (collusion resistance)."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        outcome = await _submit(tid, officer, "150000.00")
        assert outcome.pending_id is not None

        with pytest.raises(DBAPIError, match="ck_pending_approvals_sod"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE pending_approvals SET status = 'ratified', "
                        "checker_id = maker_id, decided_at = now() "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(outcome.pending_id)},
                )

        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "pending"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — assurance exclusion; fail-closed principals
# ---------------------------------------------------------------------------


def test_assurance_role_is_excluded_from_checking() -> None:
    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        auditor = await _user(tid, AUDITOR)
        outcome = await _submit(tid, officer, "150000.00")
        assert outcome.pending_id is not None
        with pytest.raises(ConflictError, match="audit independence"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, auditor, outcome.pending_id)

    asyncio.run(run())


def test_unvouched_principals_fail_closed() -> None:
    """An actor the users table cannot vouch for makes NOTHING and
    checks NOTHING (the enforce_authority_band posture)."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        ghost = uuid.uuid4()
        with pytest.raises(ForbiddenError, match="no resolvable role"):
            await _submit(tid, ghost, "10.00")
        outcome = await _submit(tid, officer, "150000.00")
        assert outcome.pending_id is not None
        with pytest.raises(ForbiddenError, match="no resolvable role"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, ghost, outcome.pending_id)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Happy path: both principals recorded; decisions are terminal
# ---------------------------------------------------------------------------


def test_ratify_records_both_principals_and_audits() -> None:
    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        outcome = await _submit(tid, officer, "250000.00")
        assert outcome.pending_id is not None

        async with tenant_session(factory(), tid) as session:
            record = await ratify_pending(session, tid, manager, outcome.pending_id)
        assert record.status == "ratified"
        assert record.maker_id == officer
        assert record.checker_id == manager

        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "ratified"
        assert uuid.UUID(str(row[1])) == manager
        assert row[2] is not None

        async with tenant_session(factory(), tid) as session:
            audit = (
                await session.execute(
                    text(
                        "SELECT actor_id, after FROM audit_log "
                        "WHERE entity = 'pending_approvals' AND entity_id = :eid "
                        "AND action = 'approval.ratify'"
                    ),
                    {"eid": str(outcome.pending_id)},
                )
            ).one()
        assert uuid.UUID(str(audit[0])) == manager
        assert audit[1]["maker_id"] == str(officer)
        assert audit[1]["checker_id"] == str(manager)

    asyncio.run(run())


def test_decided_requests_cannot_be_decided_again() -> None:
    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        admin = await _user(tid, SYSTEM_ADMIN)
        outcome = await _submit(tid, officer, "250000.00")
        assert outcome.pending_id is not None
        async with tenant_session(factory(), tid) as session:
            await ratify_pending(session, tid, manager, outcome.pending_id)
        with pytest.raises(ConflictError, match="already decided"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, admin, outcome.pending_id)
        with pytest.raises(ConflictError, match="already decided"):
            async with tenant_session(factory(), tid) as session:
                await decline_pending(session, tid, admin, outcome.pending_id, reason="late")

    asyncio.run(run())


def test_decline_requires_a_checker_rationale() -> None:
    """!52 F2 precedent: the rejection carries a REQUIRED rationale;
    a missing/blank reason is refused with zero side effects."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        outcome = await _submit(tid, officer, "250000.00")
        assert outcome.pending_id is not None

        for blank in ("", "   "):
            with pytest.raises(InvalidInputError, match="rationale"):
                async with tenant_session(factory(), tid) as session:
                    await decline_pending(session, tid, manager, outcome.pending_id, reason=blank)
        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "pending"

        async with tenant_session(factory(), tid) as session:
            record = await decline_pending(
                session, tid, manager, outcome.pending_id, reason="exceeds branch exposure"
            )
        assert record.status == "declined"
        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "declined"
        assert str(row[3]) == "exceeds branch exposure"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — band change between request and ratification (stricter applies)
# ---------------------------------------------------------------------------


def test_tightened_bands_bind_a_pending_request_at_ratification() -> None:
    """400k requested YESTERDAY under the defaults (tier 1: Branch
    Manager). The tenant tightens bands effective TODAY (400k -> tier
    2). The Branch Manager is refused — the CURRENT band is stricter
    and applies; the Credit Committee (sufficient under BOTH) ratifies."""

    async def run() -> None:
        tid = await _tenant()
        admin = await _user(tid, SYSTEM_ADMIN)
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        committee = await _user(tid, CREDIT_COMMITTEE)
        # Hand-computed: 400k under defaults -> tier 1.
        pending_id = await _backdated_pending(tid, officer, "400000.00", 1)
        await _configure_effective_today(tid, admin, _STRICT_BANDS)

        with pytest.raises(ForbiddenError, match="stricter"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, manager, pending_id)
        row = await _pending_row(tid, pending_id)
        assert str(row[0]) == "pending"

        async with tenant_session(factory(), tid) as session:
            record = await ratify_pending(session, tid, committee, pending_id)
        assert record.status == "ratified"

    asyncio.run(run())


def test_loosened_bands_never_weaken_a_pending_request() -> None:
    """400k requested YESTERDAY under the defaults (tier 1). The tenant
    LOOSENS bands effective TODAY (400k -> Loan-Officer tier). A second
    Loan Officer is STILL refused — the REQUEST-TIME band is stricter
    and applies; the Branch Manager (sufficient under BOTH) ratifies."""

    async def run() -> None:
        tid = await _tenant()
        admin = await _user(tid, SYSTEM_ADMIN)
        officer = await _user(tid, LOAN_OFFICER)
        second_officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        pending_id = await _backdated_pending(tid, officer, "400000.00", 1)
        await _configure_effective_today(tid, admin, _LOOSE_BANDS)

        with pytest.raises(ForbiddenError, match="stricter"):
            async with tenant_session(factory(), tid) as session:
                await ratify_pending(session, tid, second_officer, pending_id)
        row = await _pending_row(tid, pending_id)
        assert str(row[0]) == "pending"

        async with tenant_session(factory(), tid) as session:
            record = await ratify_pending(session, tid, manager, pending_id)
        assert record.status == "ratified"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM9 — the Board tier: no platform principal ratifies above the top band
# ---------------------------------------------------------------------------


def test_board_tier_request_stays_pending_for_every_platform_role() -> None:
    async def run() -> None:
        tid = await _tenant()
        committee = await _user(tid, CREDIT_COMMITTEE)
        outcome = await _submit(tid, committee, "2000000.01")
        assert outcome.proceed is False
        assert outcome.pending_id is not None
        for role in (SYSTEM_ADMIN, BRANCH_MANAGER, CREDIT_COMMITTEE):
            checker = await _user(tid, role)
            with pytest.raises(ForbiddenError, match="may not ratify"):
                async with tenant_session(factory(), tid) as session:
                    await ratify_pending(session, tid, checker, outcome.pending_id)
        row = await _pending_row(tid, outcome.pending_id)
        assert str(row[0]) == "pending"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5/FM6 — append-only band history; write-once workflow rows
# ---------------------------------------------------------------------------


def test_band_sets_are_append_only_at_service_and_db() -> None:
    async def run() -> None:
        tid = await _tenant()
        admin = await _user(tid, SYSTEM_ADMIN)
        await _configure_effective_today(tid, admin, _STRICT_BANDS)

        # Service layer: the same effective date cannot be re-claimed.
        with pytest.raises(ConflictError, match="append-only"):
            await _configure_effective_today(tid, admin, _LOOSE_BANDS)

        # DB layer: history is never rewritten or dropped.
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("UPDATE approval_band_sets SET bands = '[]'::jsonb"))
        with pytest.raises(DBAPIError, match="append-only"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text("DELETE FROM approval_band_sets"))

    asyncio.run(run())


def test_pending_rows_are_write_once_and_terminal_at_db() -> None:
    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        manager = await _user(tid, BRANCH_MANAGER)
        outcome = await _submit(tid, officer, "250000.00")
        assert outcome.pending_id is not None
        pid = str(outcome.pending_id)

        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE pending_approvals SET amount = '1.00' WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": pid},
                )
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text("DELETE FROM pending_approvals WHERE id = CAST(:id AS uuid)"),
                    {"id": pid},
                )

        async with tenant_session(factory(), tid) as session:
            await ratify_pending(session, tid, manager, outcome.pending_id)
        # Terminal states are terminal: un-pending via SQL raises.
        with pytest.raises(DBAPIError, match="status cannot move"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE pending_approvals SET status = 'pending', "
                        "checker_id = NULL, decided_at = NULL "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": pid},
                )
        # One-shot decision fields never re-fill.
        ghost_checker = await _user(tid, SYSTEM_ADMIN)
        with pytest.raises(DBAPIError, match="write-once"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(
                    text(
                        "UPDATE pending_approvals "
                        "SET checker_id = CAST(:c AS uuid) WHERE id = CAST(:id AS uuid)"
                    ),
                    {"c": str(ghost_checker), "id": pid},
                )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — tenant isolation of bands (issue #8 EXIT)
# ---------------------------------------------------------------------------


def test_band_configuration_is_tenant_isolated() -> None:
    """Tenant A tightens its bands; tenant B still resolves ITS OWN
    (the defaults) — and B cannot read, mutate or forge A's engine
    rows via raw SQL through the app role (ADR-0002)."""

    async def run() -> None:
        tid_a = await _tenant()
        tid_b = await _tenant()
        admin_a = await _user(tid_a, SYSTEM_ADMIN)
        officer_a = await _user(tid_a, LOAN_OFFICER)
        officer_b = await _user(tid_b, LOAN_OFFICER)
        await _configure_effective_today(tid_a, admin_a, _STRICT_BANDS)

        # Tenant A's tightened matrix governs A: 60k > 50k -> pends.
        outcome_a = await _submit(tid_a, officer_a, "60000.00")
        assert outcome_a.proceed is False
        # Tenant B still resolves the day-one defaults: 60k proceeds.
        outcome_b = await _submit(tid_b, officer_b, "60000.00")
        assert outcome_b.proceed is True
        async with tenant_session(factory(), tid_b) as session:
            assert await band_schedules(session, tid_b) == ()
            today = (await session.execute(text("SELECT current_date"))).scalar_one()
            assert await bands_as_of(session, tid_b, today) == DEFAULT_APPROVAL_BANDS

        # Raw-SQL probes: A's rows are invisible and immutable to B.
        async with tenant_session(factory(), tid_b) as session:
            for table in ("approval_band_sets", "pending_approvals"):
                visible = (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                ).scalar_one()
                assert visible == 0, f"tenant B can see tenant A {table}"
            updated = await session.execute(
                text("UPDATE pending_approvals SET status = 'ratified'")
            )
            assert updated.rowcount == 0, "tenant B can update tenant A pending approvals"

        # Forging a row INTO tenant A from B's session violates RLS.
        with pytest.raises(DBAPIError):
            async with tenant_session(factory(), tid_b) as session:
                await session.execute(
                    text(
                        "INSERT INTO approval_band_sets "
                        "(tenant_id, effective_from, bands, created_by) "
                        "VALUES (CAST(:tid AS uuid), current_date + 1, "
                        "'[]'::jsonb, CAST(:actor AS uuid))"
                    ),
                    {"tid": str(tid_a), "actor": str(officer_b)},
                )

        # Tenant A still sees exactly its own configuration.
        async with tenant_session(factory(), tid_a) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM approval_band_sets"))
            ).scalar_one()
            assert count == 1, "tenant A cannot see its own band schedule"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM8 — corrupt/invalid configuration
# ---------------------------------------------------------------------------


def test_corrupt_stored_bands_fail_closed_for_the_money_path() -> None:
    """A band row corrupted via manual SQL (the API can never write
    one) is a 409 for every consumer — the guard is never silently
    skipped (the tenant_settings posture)."""

    async def run() -> None:
        tid = await _tenant()
        officer = await _user(tid, LOAN_OFFICER)
        admin = await _user(tid, SYSTEM_ADMIN)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO approval_band_sets "
                    "(tenant_id, effective_from, bands, created_by) "
                    "VALUES (CAST(:tid AS uuid), current_date, "
                    "CAST(:bands AS jsonb), CAST(:actor AS uuid))"
                ),
                {
                    "tid": str(tid),
                    "bands": '[{"authority": "Nobody", "max_amount": null}]',
                    "actor": str(admin),
                },
            )
        with pytest.raises(ConflictError, match="failing closed"):
            await _submit(tid, officer, "10.00")

    asyncio.run(run())


def test_invalid_matrices_and_amounts_are_rejected() -> None:
    async def run() -> None:
        tid = await _tenant()
        admin = await _user(tid, SYSTEM_ADMIN)
        officer = await _user(tid, LOAN_OFFICER)
        for bad_bands in ([], [{"authority": "Nobody", "max_amount": None}]):
            with pytest.raises(InvalidInputError):
                await _configure_effective_today(tid, admin, bad_bands)
        for bad_amount in ("0.00", "-5.00", "1.001"):
            with pytest.raises(InvalidInputError):
                await _submit(tid, officer, bad_amount)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Migration contracts: checked_by shipped; 0049 loud-refusal downgrade
# ---------------------------------------------------------------------------


def test_transactions_checked_by_extends_the_0036_attribution() -> None:
    """Structural pin: the checker principal column exists on
    transactions, uuid-typed, nullable (NULL = the honest 'no
    checker'), FK to users — beside the 0036 created_by."""

    async def run() -> None:
        tid = await _tenant()
        async with tenant_session(factory(), tid) as session:
            column = (
                await session.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'transactions' AND column_name = 'checked_by'"
                    )
                )
            ).one()
            assert str(column[0]) == "uuid"
            assert str(column[1]) == "YES"
            fk_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint con "
                        "JOIN pg_attribute att ON att.attrelid = con.conrelid "
                        "AND att.attnum = ANY (con.conkey) "
                        "WHERE con.conrelid = 'transactions'::regclass "
                        "AND con.contype = 'f' AND att.attname = 'checked_by' "
                        "AND con.confrelid = 'users'::regclass"
                    )
                )
            ).scalar_one()
            assert int(fk_count) == 1

    asyncio.run(run())


def _load_migration(filename: str) -> Any:
    """The real revision module (not a copy — the 0017 falsifiability
    precedent)."""
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:4]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0049_downgrade_refuses_over_approval_history() -> None:
    """The 0049 guard passes a tenant with no engine history and
    REFUSES the moment band schedules or pending approvals exist (RLS
    scopes the counts to the probing tenant). Also pins _DOWN to start
    with the guard. Fails with the guard removed."""

    async def run() -> None:
        migration = _load_migration("0049_approval_engine.py")
        assert migration._DOWN.startswith(migration._DOWN_GUARD)
        tid = await _tenant()
        admin = await _user(tid, SYSTEM_ADMIN)

        # Clean expansion: the guard passes.
        async with tenant_session(factory(), tid) as session:
            await session.execute(text(migration._DOWN_GUARD))

        # Band-schedule history refuses.
        await _configure_effective_today(tid, admin, _STRICT_BANDS)
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid) as session:
                await session.execute(text(migration._DOWN_GUARD))

        # Pending-approval workflow history refuses too (a second
        # tenant, so each history class is proven independently).
        tid2 = await _tenant()
        officer2 = await _user(tid2, LOAN_OFFICER)
        outcome = await _submit(tid2, officer2, "150000.00")
        assert outcome.pending_id is not None
        with pytest.raises(DBAPIError, match="refusing downgrade"):
            async with tenant_session(factory(), tid2) as session:
                await session.execute(text(migration._DOWN_GUARD))

    asyncio.run(run())
