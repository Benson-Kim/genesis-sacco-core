"""Outbox dispatcher suite (P5): atomicity, retry, dead-letter, idempotency."""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text

from db_helpers import factory, seed_user, unique_email
from genesis.application.outbox import enqueue_event
from genesis.infrastructure import outbox_worker
from genesis.infrastructure.db import get_engine
from genesis.infrastructure.outbox_worker import (
    CLAIM_LEASE_SECONDS,
    DISPATCHED_RETENTION_DAYS,
    MAX_ATTEMPTS,
    PURGE_BATCH_SIZE,
    backoff_delay,
    dispatch_due,
    list_active_tenants,
    list_due_tenants,
    outbox_metrics,
    purge_dispatched,
    run_dispatch_cycle,
)
from genesis.infrastructure.providers import StubProvider
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class FlakyProvider:
    """Fails the first `fail_times` sends, then succeeds."""

    channel = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def send(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("provider outage (simulated)")


async def _reset_due(tid: uuid.UUID) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE outbox_events SET next_attempt_at = now() WHERE status = 'pending'")
        )


def test_rollback_removes_event_atomically() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        with pytest.raises(RuntimeError):
            async with tenant_session(factory(), tid) as session:
                await enqueue_event(session, tid, event_type="t.atomic", payload={"k": "v"})
                raise RuntimeError("force rollback")
        async with tenant_session(factory(), tid) as session:
            count = (await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one()
        assert int(count) == 0

    asyncio.run(run())


def test_retry_then_success_records_attempts() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            event_id = await enqueue_event(session, tid, event_type="n.retry", payload={})
        provider = FlakyProvider(fail_times=2)
        for _ in range(3):
            await _reset_due(tid)
            await dispatch_due(factory(), tid, provider)
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, attempts, last_error FROM outbox_events "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(event_id)},
                )
            ).first()
        assert row is not None
        assert row[0] == "dispatched"
        assert int(row[1]) == 3
        assert row[2] is not None
        assert provider.calls == 3

    asyncio.run(run())


def test_dead_letter_after_max_attempts_and_never_repicked() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            event_id = await enqueue_event(session, tid, event_type="n.dead", payload={})
        provider = FlakyProvider(fail_times=10**6)
        for _ in range(MAX_ATTEMPTS):
            await _reset_due(tid)
            await dispatch_due(factory(), tid, provider)
        async with tenant_session(factory(), tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, attempts, last_error FROM outbox_events "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(event_id)},
                )
            ).first()
        assert row is not None
        assert row[0] == "dead"
        assert int(row[1]) == MAX_ATTEMPTS
        assert row[2] is not None
        calls_before = provider.calls
        await _reset_due(tid)
        await dispatch_due(factory(), tid, provider)
        assert provider.calls == calls_before

    asyncio.run(run())


def test_stub_provider_is_idempotent_by_event_id() -> None:
    async def run() -> None:
        provider = StubProvider()
        await provider.send("evt-1", "x", {})
        await provider.send("evt-1", "x", {})
        await provider.send("evt-2", "x", {})
        assert provider.delivered_event_ids == ["evt-1", "evt-2"]

    asyncio.run(run())


def test_backoff_grows_exponentially_within_jitter_bounds() -> None:
    assert backoff_delay(1, 0.0) == 30.0
    assert backoff_delay(1, 1.0) == 60.0
    assert backoff_delay(3, 0.0) == 120.0
    assert backoff_delay(3, 1.0) == 240.0


def test_metrics_report_lag_and_failures() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            await enqueue_event(session, tid, event_type="m.pending", payload={})
        metrics = await outbox_metrics(factory(), tid)
        assert metrics.pending == 1
        assert metrics.dead == 0
        assert metrics.oldest_pending_seconds >= 0

    asyncio.run(run())


def test_worker_cycle_covers_active_tenants() -> None:
    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            await enqueue_event(session, tid, event_type="n.cycle", payload={})
        tenants = await list_active_tenants(factory())
        assert tid in tenants
        provider = StubProvider()
        delivered = await run_dispatch_cycle(factory(), provider)
        assert delivered >= 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P13.17(e) / DSA-6 — set-based lease, retention purge, due-tenant discovery
# ---------------------------------------------------------------------------

EXPLAIN_OUT = Path(__file__).resolve().parents[1] / "perf" / "explain_p1317e.txt"


class _StatementRecorder:
    """Captures (statement, repr(parameters)) for every executed cursor."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, *args: Any) -> None:
        self.records.append((str(args[2]), repr(args[3])))


async def _seed_event(
    tid: uuid.UUID,
    *,
    status: str,
    dispatched_days_ago: int | None = None,
    created_days_ago: int = 0,
    due_in_seconds: int = 0,
) -> uuid.UUID:
    """Insert one outbox row with controlled status and age (test seam)."""
    event_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO outbox_events (id, tenant_id, event_type, payload, "
                "status, attempts, next_attempt_at, created_at, dispatched_at) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), 't.seeded', '{}', "
                ":status, 0, now() + make_interval(secs => :due), "
                "now() - make_interval(days => :created), "
                "now() - make_interval(days => CAST(:ddays AS int)))"
            ),
            {
                "id": str(event_id),
                "tid": str(tid),
                "status": status,
                "due": due_in_seconds,
                "created": created_days_ago,
                "ddays": dispatched_days_ago,
            },
        )
    return event_id


class _LeaseProbeProvider:
    """Succeeds, but first counts the claimed rows carrying the lease.

    send() runs in phase 2, AFTER the claim transaction committed, so an
    independent session observing the lease also proves the
    claim-commits-BEFORE-any-provider-call ordering survived DSA-6.
    """

    channel = "lease-probe"

    def __init__(self, tid: uuid.UUID) -> None:
        self.tid = tid
        self.leased_counts: list[int] = []

    async def send(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        async with tenant_session(factory(), self.tid) as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE status = 'pending' "
                        "AND next_attempt_at > now() + make_interval(secs => :floor)"
                    ),
                    {"floor": CLAIM_LEASE_SECONDS - 60},
                )
            ).scalar_one()
        self.leased_counts.append(int(count))


def test_fm5_lease_update_is_set_based_one_statement_per_batch() -> None:
    """FM5 (DSA-6): the claim lease is ONE set-based UPDATE per batch.

    Hand-computed oracle: 5 claimed events -> exactly 1 statement of the
    lease shape (prefix "UPDATE outbox_events SET next_attempt_at", the
    only statement with that prefix on the success path); the pre-DSA-6
    per-row loop issued 5. Falsifiable: restore the per-row loop and the
    count is 5, not 1.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            for i in range(5):
                await enqueue_event(session, tid, event_type=f"n.lease{i}", payload={})
        engine = get_engine(os.environ["DATABASE_URL"])
        recorder = _StatementRecorder()
        event.listen(engine.sync_engine, "before_cursor_execute", recorder)
        try:
            delivered = await dispatch_due(factory(), tid, StubProvider())
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", recorder)
        assert delivered == 5
        lease_updates = [
            stmt
            for stmt, _ in recorder.records
            if stmt.startswith("UPDATE outbox_events SET next_attempt_at")
        ]
        assert len(lease_updates) == 1
        assert "ANY" in lease_updates[0]

    asyncio.run(run())


def test_lease_covers_every_claimed_row_before_any_provider_call() -> None:
    """Lease equivalence: the set-based UPDATE leases the WHOLE claim.

    Hand-computed oracle: 3 events due NOW (next_attempt_at <= now());
    with CLAIM_LEASE_SECONDS = 300 every claimed row must read
    next_attempt_at > now() + 240s from an independent session on the
    FIRST provider call (all 3 still pending, all 3 leased, claim txn
    already committed). Falsifiable: lease only part of the batch (the
    per-row loop with a break, or ANY over a subset) and the first-call
    count drops below 3.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            for i in range(3):
                await enqueue_event(session, tid, event_type=f"n.probe{i}", payload={})
        provider = _LeaseProbeProvider(tid)
        delivered = await dispatch_due(factory(), tid, provider)
        assert delivered == 3
        assert provider.leased_counts[0] == 3

    asyncio.run(run())


def test_fm5_concurrent_workers_claim_disjoint_sets() -> None:
    """SKIP LOCKED disjointness is preserved with the set-based lease.

    Hand-computed oracle: 10 due events, two concurrent dispatchers with
    batch_size 5 -> the intersection of the delivered id sets is EMPTY
    (each row claimed exactly once, FM5) and no event is delivered
    twice. Falsifiable: drop SKIP LOCKED (or lease outside the claim
    txn) and the sets can overlap.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            for i in range(10):
                await enqueue_event(session, tid, event_type=f"n.conc{i}", payload={})
        provider_a, provider_b = StubProvider(), StubProvider()
        delivered_a, delivered_b = await asyncio.gather(
            dispatch_due(factory(), tid, provider_a, batch_size=5),
            dispatch_due(factory(), tid, provider_b, batch_size=5),
        )
        ids_a = set(provider_a.delivered_event_ids)
        ids_b = set(provider_b.delivered_event_ids)
        assert ids_a & ids_b == set()
        assert delivered_a == len(ids_a)
        assert delivered_b == len(ids_b)
        assert len(ids_a | ids_b) == delivered_a + delivered_b

    asyncio.run(run())


def test_fm5_purge_deletes_only_dispatched_rows_older_than_retention() -> None:
    """FM5 purge selectivity, hand-computed vs DISPATCHED_RETENTION_DAYS=30.

    Seeded: dispatched@31d (31 > 30 -> PURGED), dispatched@1d (1 < 30 ->
    kept), pending seeded 31d ago (pending is WORK -> kept regardless of
    age), dead with dispatched_at 31d ago (dead is the ALERTABLE queue
    -> kept; this is the adversarial row: an age-only predicate would
    delete it). Falsifiable: drop the status = 'dispatched' predicate
    and the dead-old row goes too (purged becomes 2, not 1).
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        old_days = DISPATCHED_RETENTION_DAYS + 1
        purged_id = await _seed_event(tid, status="dispatched", dispatched_days_ago=old_days)
        kept_recent = await _seed_event(tid, status="dispatched", dispatched_days_ago=1)
        kept_pending = await _seed_event(
            tid, status="pending", created_days_ago=old_days, due_in_seconds=3600
        )
        kept_dead = await _seed_event(
            tid, status="dead", created_days_ago=old_days, dispatched_days_ago=old_days
        )
        purged = await purge_dispatched(factory(), tid)
        assert purged == 1
        async with tenant_session(factory(), tid) as session:
            remaining = {
                str(row[0]) for row in await session.execute(text("SELECT id FROM outbox_events"))
            }
        assert str(purged_id) not in remaining
        assert {str(kept_recent), str(kept_pending), str(kept_dead)} <= remaining

    asyncio.run(run())


def test_fm5_purge_is_batched_and_rerun_is_a_no_op() -> None:
    """Purge batch bound + idempotency by side-effect counts (FM5).

    Hand-computed oracle: 5 purgeable rows / batch_size 2 -> DELETEs of
    2, 2, 1 (the shared batch runner stops once a batch removes fewer
    than batch_size) = exactly 3 DELETE statements, each inside its own
    tenant transaction (3 set_config calls), never one long DELETE.
    Re-run: 0 rows purged. Falsifiable: drop the LIMIT and one DELETE
    removes all 5 (count 1, unbounded transaction).
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        old_days = DISPATCHED_RETENTION_DAYS + 1
        for _ in range(5):
            await _seed_event(tid, status="dispatched", dispatched_days_ago=old_days)
        engine = get_engine(os.environ["DATABASE_URL"])
        recorder = _StatementRecorder()
        event.listen(engine.sync_engine, "before_cursor_execute", recorder)
        try:
            purged = await purge_dispatched(factory(), tid, batch_size=2)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", recorder)
        assert purged == 5
        deletes = [
            stmt for stmt, _ in recorder.records if stmt.startswith("DELETE FROM outbox_events")
        ]
        assert len(deletes) == 3
        assert all("LIMIT" in stmt and "SKIP LOCKED" in stmt for stmt in deletes)
        configs = [stmt for stmt, _ in recorder.records if "set_config" in stmt]
        assert len(configs) == 3
        assert await purge_dispatched(factory(), tid, batch_size=2) == 0

    asyncio.run(run())


def test_dsa6_idle_tenant_gets_zero_claim_queries() -> None:
    """Due-tenant discovery: a tenant with no due events is not walked.

    Hand-computed setup: tenant B holds one future-dated pending row
    (due in 1h -> not due) and one fresh dispatched row -> excluded by
    outbox_due_tenant_ids(), so a full run_dispatch_cycle issues ZERO
    tenant-scoped statements for B (before DSA-6 the cycle opened a
    claim transaction for EVERY active tenant, B included, every
    interval — falsifiable by restoring the list_active_tenants walk).
    Tenant A (one due event) is discovered and delivered.
    """

    async def run() -> None:
        tid_a, _ = await seed_user(unique_email())
        tid_b, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid_a) as session:
            await enqueue_event(session, tid_a, event_type="n.due", payload={})
        await _seed_event(tid_b, status="pending", due_in_seconds=3600)
        await _seed_event(tid_b, status="dispatched", dispatched_days_ago=1)
        due = await list_due_tenants(factory())
        assert tid_a in due
        assert tid_b not in due
        engine = get_engine(os.environ["DATABASE_URL"])
        recorder = _StatementRecorder()
        event.listen(engine.sync_engine, "before_cursor_execute", recorder)
        try:
            delivered = await run_dispatch_cycle(factory(), StubProvider())
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", recorder)
        assert delivered >= 1
        assert not any(str(tid_b) in params for _, params in recorder.records)

    asyncio.run(run())


def test_dsa6_one_tenant_failure_never_aborts_the_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """!37 per-tenant isolation preserved on the due-tenant walk.

    An unexpected claim-phase failure for tenant A (injected around
    dispatch_due — the category the per-event provider handling cannot
    catch) is logged and skipped; tenant B's due event is still
    delivered and the cycle returns normally. Falsifiable: remove the
    per-tenant except in run_dispatch_cycle and the injected error
    propagates out of the cycle, failing this test regardless of the
    tenant walk order (A IS in the due set).
    """

    async def run() -> None:
        tid_a, _ = await seed_user(unique_email())
        tid_b, _ = await seed_user(unique_email())
        for tid in (tid_a, tid_b):
            async with tenant_session(factory(), tid) as session:
                await enqueue_event(session, tid, event_type="n.iso", payload={})
        real_dispatch_due = outbox_worker.dispatch_due

        async def flaky(factory_: Any, tenant_id: uuid.UUID, provider: Any, **kw: Any) -> int:
            if tenant_id == tid_a:
                raise RuntimeError("claim-phase failure (simulated)")
            return await real_dispatch_due(factory_, tenant_id, provider, **kw)

        monkeypatch.setattr(outbox_worker, "dispatch_due", flaky)
        delivered = await run_dispatch_cycle(factory(), StubProvider())
        assert delivered >= 1
        async with tenant_session(factory(), tid_b) as session:
            status = (
                await session.execute(
                    text("SELECT status FROM outbox_events WHERE event_type = 'n.iso'")
                )
            ).scalar_one()
        assert status == "dispatched"

    asyncio.run(run())


def test_dsa6_purge_and_discovery_queries_are_index_backed() -> None:
    """Gate 1.3 EXPLAIN evidence (the P13.13 capture precedent).

    Writes the real plans to backend/perf/explain_p1317e.txt (CI
    artifact). Tiny CI tables make seqscan the cheaper plan; the capture
    disables it for the session to prove each scan is SERVABLE by its
    index. Falsifiable guards: drop idx_outbox_dispatched_purge (0024)
    or idx_outbox_pending (0001) and the index-name gates fail.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        await _seed_event(
            tid, status="dispatched", dispatched_days_ago=DISPATCHED_RETENTION_DAYS + 1
        )

        async def plan(session: Any, sql: str, params: dict[str, object]) -> str:
            rows = (
                await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)
            ).scalars()
            return "\n".join(str(row) for row in rows)

        async with tenant_session(factory(), tid) as session:
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            purge_plan = await plan(
                session,
                "SELECT id FROM outbox_events WHERE status = 'dispatched' "
                "AND dispatched_at < now() - make_interval(days => :days) "
                "LIMIT :limit FOR UPDATE SKIP LOCKED",
                {"days": DISPATCHED_RETENTION_DAYS, "limit": PURGE_BATCH_SIZE},
            )
            due_plan = await plan(
                session,
                "SELECT DISTINCT tenant_id FROM outbox_events "
                "WHERE status = 'pending' AND next_attempt_at <= now()",
                {},
            )
        EXPLAIN_OUT.parent.mkdir(parents=True, exist_ok=True)
        EXPLAIN_OUT.write_text(
            "== P13.17(e)/DSA-6 purge driving subquery (idx_outbox_dispatched_purge, 0024) ==\n"
            f"{purge_plan}\n\n"
            "== DSA-6 due-tenant discovery body (idx_outbox_pending, 0001) ==\n"
            f"{due_plan}\n"
        )
        assert "idx_outbox_dispatched_purge" in purge_plan
        assert "idx_outbox_pending" in due_plan

    asyncio.run(run())


def test_r6_every_security_definer_function_pins_search_path() -> None:
    """R6 (!44): SECURITY DEFINER without a pinned search_path is a
    schema-shadowing privilege-escalation vector — a caller-controlled
    search_path could resolve hostile relations executed as the
    BYPASSRLS definer role.

    Sweeps pg_proc for EVERY user-schema SECURITY DEFINER function and
    asserts its proconfig pins search_path (0024 pins the two new
    registries and retro-fits the 0003 active_tenant_ids()).
    Falsifiable: remove any `SET search_path = ...` clause from 0024
    and this test names the offending function. Also asserts the three
    known definer functions exist, so the sweep can never pass
    vacuously against an empty schema.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT p.proname, COALESCE(p.proconfig, ARRAY[]::text[]) "
                        "FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE p.prosecdef "
                        "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
                    )
                )
            ).all()
        found = {name for name, _ in rows}
        # Non-vacuous: the 0003 registry and both 0024 registries must exist.
        assert {
            "active_tenant_ids",
            "outbox_due_tenant_ids",
            "outbox_purgeable_tenant_ids",
        } <= found
        unpinned = sorted(
            name
            for name, config in rows
            if not any(str(entry).startswith("search_path=") for entry in config)
        )
        assert unpinned == [], f"SECURITY DEFINER unpinned search_path: {unpinned}"

    asyncio.run(run())


def test_r7_security_definer_functions_deny_public_execute() -> None:
    """R7 (!44): PostgreSQL grants EXECUTE to PUBLIC by default on every
    function, so a SECURITY DEFINER registry left on the default ACL is
    callable by ANY role — a compromised tenant-scoped session could
    enumerate every active tenant id and observe which tenants have
    due/purgeable outbox activity (a cross-tenant activity oracle).
    0024 revokes PUBLIC; only the ADR-0002 application role the worker
    runs as (genesis_app in CI) gets an explicit EXECUTE grant — this
    suite itself runs as that role, so the worker-path tests above
    prove the granted role can still call the registries.

    Sweeps pg_proc.proacl for EVERY user-schema SECURITY DEFINER
    function. Oracle, hand-computed from the PostgreSQL ACL rules: a
    NULL proacl MEANS the default ACL — owner plus EXECUTE to PUBLIC —
    so NULL must FAIL exactly like an explicit PUBLIC grant
    (aclexplode() grantee oid 0 is the PUBLIC pseudo-role; the LEFT
    JOIN LATERAL keeps NULL-proacl rows, flagged via the IS NULL arm).
    Falsifiable: drop any REVOKE from 0024's _UP (or GRANT ... TO
    PUBLIC on any registry) and this test names the offender.
    Non-vacuous: the three known definer functions must appear in the
    sweep, so it can never pass against an empty schema.
    """

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT p.proname, "
                        "       p.proacl IS NULL AS default_acl, "
                        "       bool_or(a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
                        "           AS public_execute "
                        "FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "LEFT JOIN LATERAL aclexplode(p.proacl) a ON true "
                        "WHERE p.prosecdef "
                        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "GROUP BY p.oid, p.proname, p.proacl"
                    )
                )
            ).all()
        found = {name for name, _, _ in rows}
        # Non-vacuous: the 0003 registry and both 0024 registries must exist.
        assert {
            "active_tenant_ids",
            "outbox_due_tenant_ids",
            "outbox_purgeable_tenant_ids",
        } <= found
        offenders = sorted(
            name
            for name, default_acl, public_execute in rows
            # NULL proacl = default ACL = PUBLIC EXECUTE: fails too.
            if default_acl or bool(public_execute)
        )
        assert offenders == [], f"SECURITY DEFINER PUBLIC-executable: {offenders}"

    asyncio.run(run())
