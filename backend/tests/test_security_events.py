"""Adversarial tests for issue #1 — detective controls on privileged
money actions (application/security_events.py + corrections hooks).

DB cases require a migrated PostgreSQL database (DATABASE_URL); the
pure-domain window/phone oracles run everywhere.

Failure modes (v1.2 rule 15) — oracles HAND-COMPUTED, never captured
from the code under test; every guard test fails with its guard
removed:

  FM1 insider checker      — an approver whose users.email equals the
                             beneficiary members.email gets a 403 via
                             the API, ZERO money side effects, the
                             adjustment stays pending, AND the refused
                             attempt survives the rollback as an
                             audit_log row (security.refusal.
                             self_dealing) plus a security.refusal
                             outbox event.
  FM2 insider maker        — a write-off requester sharing a phone
                             with the borrower ('0712...' vs
                             '+254712...', the normalized MSISDN tail)
                             is a 403 with no loan_write_offs row and
                             the refusal audit row present.
  FM3 insider voter        — a committee voter holding an ACTIVE
                             member_credentials email of the borrower
                             raises SelfDealingError at the service
                             (the 0035 link IS the authority).
  FM4 off-hours anomaly    — a CLEAN approval at Sunday 02:30 EAT
                             succeeds AND writes a security.anomaly
                             audit row with the off_hours signal plus
                             the outbox alert event.
  FM5 cross-branch anomaly — branch-A staff writing off a branch-B
                             member succeeds AND emits exactly
                             ['cross_branch'] in business hours.
  FM6 telemetry never blocks — a raising signal computation is logged
                             and swallowed; the action still commits
                             and no anomaly row appears (gate 1.2).
  FM7 clean action         — in-hours, same-branch, unlinked actors
                             emit NO security.* rows (no alert spam).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application import corrections as corrections_service
from genesis.application import loans as loans_service
from genesis.application import security_events
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.ledger import disburse_loan
from genesis.application.rbac import seed_permissions
from genesis.domain.committee import Vote
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

# ---------------------------------------------------------------------------
# Pure-domain oracles (no DB)
# ---------------------------------------------------------------------------


def test_normalize_phone_kenyan_formats() -> None:
    """'0712345678' and '+254 712 345 678' share the 9-digit tail
    712345678 (hand-computed); runs under 9 digits can never match."""
    assert security_events.normalize_phone("0712345678") == "712345678"
    assert security_events.normalize_phone("+254 712 345 678") == "712345678"
    assert security_events.normalize_phone("254712345678") == "712345678"
    assert security_events.normalize_phone("12345678") == ""  # 8 digits
    assert security_events.normalize_phone(None) == ""
    assert security_events.normalize_phone("") == ""


def test_off_hours_boundaries() -> None:
    """Window is Mon-Sat 07:00-19:00 EAT (= UTC+3, fixed): 06:59 EAT
    off, 07:00 on, 18:59 on, 19:00 off; Sunday always off. Hand-
    computed UTC instants: 07:00 EAT == 04:00 UTC. 2026-08-19 is a
    Wednesday, 2026-08-23 a Sunday."""
    wed = datetime(2026, 8, 19, tzinfo=UTC)
    assert security_events.is_off_hours(wed.replace(hour=3, minute=59)) is True  # 06:59 EAT
    assert security_events.is_off_hours(wed.replace(hour=4, minute=0)) is False  # 07:00 EAT
    assert security_events.is_off_hours(wed.replace(hour=15, minute=59)) is False  # 18:59 EAT
    assert security_events.is_off_hours(wed.replace(hour=16, minute=0)) is True  # 19:00 EAT
    # Sunday 10:00 EAT (07:00 UTC) — inside the daily window, still off.
    assert security_events.is_off_hours(datetime(2026, 8, 23, 7, 0, tzinfo=UTC)) is True


# ---------------------------------------------------------------------------
# DB fixtures (the test_maker_checker house style)
# ---------------------------------------------------------------------------

#: Applied per DB test (NOT module-wide pytestmark: the pure-domain
#: oracles above must run in every pipeline, DB or not).
requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: In-hours instant: Wednesday 2026-08-19 10:00 EAT (07:00 UTC).
IN_HOURS = datetime(2026, 8, 19, 7, 0, tzinfo=UTC)
#: Off-hours instant: Sunday 2026-08-23 02:30 EAT (Sat 23:30 UTC).
OFF_HOURS = datetime(2026, 8, 22, 23, 30, tzinfo=UTC)


async def _seed_actor() -> tuple[uuid.UUID, uuid.UUID, str]:
    email = unique_email()
    tid, role_id = await seed_user(email, role_name="System Admin")
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
    return tid, uuid.UUID(str(user_id)), token


async def _seed_extra_user(
    tid: uuid.UUID,
    role_name: str = "System Admin",
    *,
    email: str | None = None,
    phone: str | None = None,
    branch_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"), {"name": role_name}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email, phone, branch_id) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Extra User', :email, :phone, CAST(:bid AS uuid))"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": email or unique_email(),
                "phone": phone,
                "bid": str(branch_id) if branch_id else None,
            },
        )
    token = issue_access_token(
        AuthContext(user_id=user_id, tenant_id=tid, role_id=uuid.UUID(str(role_id)))
    )
    return user_id, token


async def _seed_member(
    tid: uuid.UUID,
    *,
    email: str | None = None,
    phone: str | None = None,
    branch_id: uuid.UUID | None = None,
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name, email, phone, "
                "branch_id) VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                "'SecEvents Member', :email, :phone, CAST(:bid AS uuid))"
            ),
            {
                "id": str(mid),
                "tid": str(tid),
                "no": f"GP-{mid.hex[:6].upper()}",
                "email": email,
                "phone": phone,
                "bid": str(branch_id) if branch_id else None,
            },
        )
        await session.execute(
            text(
                "INSERT INTO deposit_accounts (id, tenant_id, member_id, balance) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), '100000.00')"
            ),
            {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid)},
        )
    return mid


async def _seed_branch(tid: uuid.UUID, name: str) -> uuid.UUID:
    bid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO branches (id, tenant_id, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name)"
            ),
            {"id": str(bid), "tid": str(tid), "name": name},
        )
    return bid


async def _disburse_for_member(tid: uuid.UUID, mid: uuid.UUID) -> uuid.UUID:
    """Disburse the golden-case loan (24,000.00 @ 12%/yr, 12 months)."""
    pid = uuid.uuid4()
    app_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, 12.00, 3.00, 60)"
            ),
            {"id": str(pid), "tid": str(tid), "name": f"Product-{pid.hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO loan_applications "
                "(id, tenant_id, member_id, product_id, amount, term_months, rate_pct, stage) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), "
                "CAST(:pid AS uuid), '24000.00', 12, 12.00, 'approved')"
            ),
            {"id": str(app_id), "tid": str(tid), "mid": str(mid), "pid": str(pid)},
        )
        result = await disburse_loan(session, tid, app_id, Channel.BANK)
    return result.loan_id


async def _pending_adjustment(
    tid: uuid.UUID, maker: uuid.UUID, loan_id: uuid.UUID
) -> corrections_service.AdjustmentRecord:
    async with tenant_session(factory(), tid) as session:
        repayment = await loans_service.record_repayment(
            session, tid, maker, loan_id, amount=Decimal("1000.00"), channel=Channel.BANK
        )
    async with tenant_session(factory(), tid) as session:
        rid = (
            await session.execute(
                text(
                    "SELECT id FROM repayments WHERE transaction_id = CAST(:t AS uuid) "
                    "AND amount > 0"
                ),
                {"t": str(repayment.txn_id)},
            )
        ).scalar_one()
    async with tenant_session(factory(), tid) as session:
        return await corrections_service.request_repayment_adjustment(
            session, tid, maker, uuid.UUID(str(rid)), reason="keying error"
        )


async def _mark_npl(tid: uuid.UUID, loan_id: uuid.UUID) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE loans SET classification = 'substandard' WHERE id = CAST(:l AS uuid)"),
            {"l": str(loan_id)},
        )


async def _security_rows(tid: uuid.UUID, action: str) -> list[dict[str, Any]]:
    """audit_log `after` payloads for one security action, oldest first."""
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text("SELECT after FROM audit_log WHERE action = :a ORDER BY id"),
                {"a": action},
            )
        ).all()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payloads.append(row[0] if isinstance(row[0], dict) else json.loads(str(row[0])))
    return payloads


async def _outbox_count(tid: uuid.UUID, event_type: str) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = :t"),
                {"t": event_type},
            )
        ).scalar_one()
    return int(count)


async def _money_counts(tid: uuid.UUID) -> tuple[int, int]:
    async with tenant_session(factory(), tid) as session:
        txns = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        legs = (await session.execute(text("SELECT count(*) FROM ledger_entries"))).scalar_one()
    return int(txns), int(legs)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "idempotency-key": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# FM1 — insider checker (shared email): 403 + surviving refusal audit row
# ---------------------------------------------------------------------------


@requires_db
def test_fm1_insider_approval_403_leaves_refusal_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandated adversarial case: an insider approving the reversal
    of a repayment on a member whose members.email equals their own
    users.email is a 403 via the API; the money posts NOTHING; the
    adjustment stays pending; and the ATTEMPT survives the rollback as
    a security.refusal.self_dealing audit row plus a security.refusal
    outbox event. Falsifiable: drop the enforce_no_self_dealing call
    in approve_repayment_adjustment and the approval posts."""
    monkeypatch.setattr(security_events, "_now", lambda: IN_HOURS)

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        insider_email = unique_email()
        _insider_id, insider_token = await _seed_extra_user(tid, email=insider_email)
        mid = await _seed_member(tid, email=insider_email.upper())  # case-insensitive match
        loan_id = await _disburse_for_member(tid, mid)
        pending = await _pending_adjustment(tid, maker, loan_id)
        before = await _money_counts(tid)

        async with api_client() as client:
            response = await client.post(
                f"/corrections/repayment-adjustments/{pending.id}/approval",
                json={},
                headers=_headers(insider_token),
            )
        assert response.status_code == 403
        assert response.json()["category"] == "forbidden"
        # The matched contacts never travel to the client.
        assert insider_email.split("@")[0] not in response.text

        assert await _money_counts(tid) == before
        async with tenant_session(factory(), tid) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM repayment_adjustments WHERE id = CAST(:i AS uuid)"),
                    {"i": str(pending.id)},
                )
            ).scalar_one()
        assert str(status) == "pending_approval"

        refusals = await _security_rows(tid, "security.refusal.self_dealing")
        assert len(refusals) == 1
        assert refusals[0]["attempted_action"] == "correction.adjustment_approved"
        assert refusals[0]["member_id"] == str(mid)
        assert "shared_email" in refusals[0]["signals"]
        assert await _outbox_count(tid, "security.refusal") == 1
        # Least disclosure: signal names only, never the matched value.
        assert insider_email not in json.dumps(refusals[0])

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM2 — insider maker (shared phone): write-off request refused
# ---------------------------------------------------------------------------


@requires_db
def test_fm2_insider_write_off_request_403_shared_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_events, "_now", lambda: IN_HOURS)

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        _insider_id, insider_token = await _seed_extra_user(tid, phone="+254 712 345 678")
        mid = await _seed_member(tid, phone="0712345678")
        loan_id = await _disburse_for_member(tid, mid)
        await _mark_npl(tid, loan_id)

        async with api_client() as client:
            response = await client.post(
                "/corrections/write-offs",
                json={"loan_id": str(loan_id), "reason": "uncollectible"},
                headers=_headers(insider_token),
            )
        assert response.status_code == 403

        async with tenant_session(factory(), tid) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM loan_write_offs"))
            ).scalar_one()
        assert int(count) == 0

        refusals = await _security_rows(tid, "security.refusal.self_dealing")
        assert len(refusals) == 1
        assert refusals[0]["attempted_action"] == "write_off.requested"
        assert "shared_phone" in refusals[0]["signals"]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3 — insider voter (active member credential): service-level refusal
# ---------------------------------------------------------------------------


@requires_db
def test_fm3_insider_vote_blocked_by_credential_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_events, "_now", lambda: IN_HOURS)

    async def run() -> None:
        tid, requester, _ = await _seed_actor()
        voter_email = unique_email()
        voter_id, _ = await _seed_extra_user(tid, "Credit Committee", email=voter_email)
        mid = await _seed_member(tid)
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO member_credentials (tenant_id, member_id, email) "
                    "VALUES (CAST(:tid AS uuid), CAST(:mid AS uuid), :email)"
                ),
                {"tid": str(tid), "mid": str(mid), "email": voter_email},
            )
        loan_id = await _disburse_for_member(tid, mid)
        await _mark_npl(tid, loan_id)
        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.request_write_off(
                session, tid, requester, loan_id, reason="uncollectible"
            )

        with pytest.raises(security_events.SelfDealingError, match="identity-linked"):
            async with tenant_session(factory(), tid) as session:
                await corrections_service.cast_write_off_vote(
                    session, tid, voter_id, record.id, Vote.APPROVE
                )
        # The blocked vote left no vote row (the raise rolled back).
        async with tenant_session(factory(), tid) as session:
            votes = (
                await session.execute(text("SELECT count(*) FROM loan_write_off_votes"))
            ).scalar_one()
        assert int(votes) == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4 — off-hours anomaly on a CLEAN approval (telemetry, non-blocking)
# ---------------------------------------------------------------------------


@requires_db
def test_fm4_off_hours_approval_succeeds_and_emits_anomaly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_events, "_now", lambda: OFF_HOURS)

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker_id, _ = await _seed_extra_user(tid)
        mid = await _seed_member(tid)
        loan_id = await _disburse_for_member(tid, mid)
        pending = await _pending_adjustment(tid, maker, loan_id)

        async with tenant_session(factory(), tid) as session:
            result = await corrections_service.approve_repayment_adjustment(
                session, tid, checker_id, pending.id
            )
        assert result.reversal_txn_id is not None

        anomalies = await _security_rows(tid, "security.anomaly")
        approve_rows = [a for a in anomalies if a["action"] == "correction.adjustment_approved"]
        assert len(approve_rows) == 1
        assert approve_rows[0]["signals"] == ["off_hours"]
        assert approve_rows[0]["member_id"] == str(mid)
        # The maker's off-hours request emitted too (same clock).
        request_rows = [a for a in anomalies if a["action"] == "correction.adjustment_requested"]
        assert len(request_rows) == 1
        assert await _outbox_count(tid, "security.anomaly") == len(anomalies)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5 — cross-branch anomaly (branch-A staff on a branch-B member)
# ---------------------------------------------------------------------------


@requires_db
def test_fm5_cross_branch_write_off_request_emits_anomaly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_events, "_now", lambda: IN_HOURS)

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        branch_a = await _seed_branch(tid, "Branch A")
        branch_b = await _seed_branch(tid, "Branch B")
        officer_id, _ = await _seed_extra_user(tid, branch_id=branch_a)
        mid = await _seed_member(tid, branch_id=branch_b)
        loan_id = await _disburse_for_member(tid, mid)
        await _mark_npl(tid, loan_id)

        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.request_write_off(
                session, tid, officer_id, loan_id, reason="uncollectible"
            )
        assert record.status is corrections_service.WriteOffStatus.REQUESTED

        anomalies = await _security_rows(tid, "security.anomaly")
        assert len(anomalies) == 1
        assert anomalies[0]["signals"] == ["cross_branch"]
        assert anomalies[0]["actor_branch_id"] == str(branch_a)
        assert anomalies[0]["member_branch_id"] == str(branch_b)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6 — telemetry failure never blocks the action (gate 1.2)
# ---------------------------------------------------------------------------


@requires_db
def test_fm6_signal_computation_failure_never_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_at: datetime) -> bool:
        raise RuntimeError("clock exploded")

    monkeypatch.setattr(security_events, "is_off_hours", boom)

    async def run() -> None:
        tid, _, _ = await _seed_actor()
        officer_id, _ = await _seed_extra_user(tid)
        mid = await _seed_member(tid)
        loan_id = await _disburse_for_member(tid, mid)
        await _mark_npl(tid, loan_id)

        async with tenant_session(factory(), tid) as session:
            record = await corrections_service.request_write_off(
                session, tid, officer_id, loan_id, reason="uncollectible"
            )
        assert record.status is corrections_service.WriteOffStatus.REQUESTED
        assert await _security_rows(tid, "security.anomaly") == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM7 — clean action emits nothing (no alert spam)
# ---------------------------------------------------------------------------


@requires_db
def test_fm7_clean_in_hours_action_emits_no_security_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_events, "_now", lambda: IN_HOURS)

    async def run() -> None:
        tid, maker, _ = await _seed_actor()
        checker_id, _ = await _seed_extra_user(tid)
        mid = await _seed_member(tid)
        loan_id = await _disburse_for_member(tid, mid)
        pending = await _pending_adjustment(tid, maker, loan_id)
        async with tenant_session(factory(), tid) as session:
            await corrections_service.approve_repayment_adjustment(
                session, tid, checker_id, pending.id
            )
        assert await _security_rows(tid, "security.anomaly") == []
        assert await _security_rows(tid, "security.refusal.self_dealing") == []
        assert await _outbox_count(tid, "security.anomaly") == 0

    asyncio.run(run())
