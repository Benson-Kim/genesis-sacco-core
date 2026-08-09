"""Nightly dormancy job: Active -> Dormant on ledger-derived inactivity
(the house gates).

Runs per tenant in bounded batches through the shared batch runner
(genesis.application.batch_runner), each batch inside its own short
transaction (scalability). The caller injects a session scope (e.g.
functools.partial(tenant_session, factory, tenant_id)) so this module
stays free of infrastructure imports (the / precedent); the
worker cycle (genesis.infrastructure.dormancy_worker) or the admin
endpoint owns scheduling.

Failure-mode contract (the build plan, all falsifiable in tests/test_dormancy.py):

  * last-activity gaming — "member-initiated" is the code-owned
    allow-list genesis.domain.ledger.MEMBER_INITIATED. System postings
    (INT- accruals, DV- distributions — whose occurred_at is pinned to
    FY END, potentially in the past) never reset the clock, and
    neither do reversal rows (reversal_of_id IS NOT NULL — staff
    corrections). Last activity is LEDGER-DERIVED from the
    transactions table, never a mutable column; a
    member with no member-initiated transaction at all is measured
    from their immutable created_at.
  * reactivation race — the scan takes each member row FOR UPDATE
    SKIP LOCKED. A concurrent deposit holds the same member row FOR
    UPDATE (application/transactions.record_deposit), so the job
    either skips the row (deposit wins, member stays active) or the
    deposit waits and reactivates after the job's commit — exactly one
    final state, never Active silently overwritten to Dormant.
  * idempotent re-run — a lock-free no-op via the anti-join on
    status ('active' only — dormant members leave the scan population
    the moment they transition) plus the ledger-derived activity
    anti-join: a re-run for the same as_of scans zero
    rows and writes nothing.
  * partial batch state — the status write, its audit row and its
    outbox event commit in ONE batch transaction; a mid-batch abort
    leaves zero partial transitions.
  * tenant scoping — explicit bound tenant_id predicates on the
    scan and the status write, on top of forced RLS.
  * config fail-closed — dormancy_period_months comes EXCLUSIVELY
    from the tenant settings. A missing key
    REFUSES THE RUN LOUDLY (409-class ConflictError, zero transitions
    — never a silent default); a corrupt stored value (reachable only
    by manual SQL past the 0017 DB CHECK) refuses identically (the parse_penalty_config fail-closed
    shape, hardened from "skip" to "refuse" per the contract).

Lock order (verbatim, the hardened block): the batch locks
member rows FOR UPDATE SKIP LOCKED in id order — the ROOT tier of the
established chain member -> accounts -> loans (the distribution precedent); reactivation already
holds member -> deposit account in
chain order inside the deposit transaction. No new lock-graph edges.
Nothing is locked after the member row in this job, so no cycle with
 settlement, P9/ pledging or withdrawals is possible.

Cutoff semantics (hand-computed oracles in tests/test_dormancy.py):
the window is [cutoff, as_of], cutoff = as_of minus period_months
calendar months with end-of-month clamping
(genesis.domain.members.dormancy_cutoff), compared as midnight UTC of
the cutoff date. Member-initiated activity with occurred_at >= that
instant keeps the member active — a deposit committed the instant
before the job runs therefore always keeps the member active, and
activity ON the cutoff day still counts as inside the window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.outbox import enqueue_event
from genesis.domain.ledger import member_initiated_types
from genesis.domain.members import MemberStatus, dormancy_cutoff, transition
from genesis.errors import ConflictError

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DORMANCY_CONFIG_SQL",
    "DormancyRunResult",
    "SessionScope",
    "dormancy_scan_sql",
    "parse_dormancy_period",
    "resolve_dormancy_period",
    "run_dormancy_for_tenant",
]

#: Members transitioned per transaction (the / batch sizing).
DEFAULT_BATCH_SIZE = 200

#: Dormancy configuration read: a single PK probe of the one-row-per-
#: tenant settings table, resolved server-side once per run (v1.1
#: rule 1). Explicit tenant predicate on top of forced RLS (v1.1
#: rule 4). Exported for the EXPLAIN capture
#: (tests/test_p1313_explain.py).
DORMANCY_CONFIG_SQL = (
    "SELECT dormancy_period_months FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"
)

#: Registry bounds for dormancy_period_months (domain/tenant_config.py,
#: mirrored by the 0017 DB CHECK).
_MIN_PERIOD_MONTHS = 1
_MAX_PERIOD_MONTHS = 120


def parse_dormancy_period(raw: object, *, configured: bool) -> int:
    """Fail-closed parse of the stored dormancy period.

    Mirrors arrears.parse_penalty_config, hardened per the
    contract: a missing key does NOT silently skip — the run is
    REFUSED LOUDLY with a 409-class error and zero transitions (a
    silent default period would either never dorm anyone or dorm
    everyone). Corrupt stored values — reachable only by manual SQL
    bypassing the 0017 DB CHECK — refuse identically. Least
    disclosure: the message names the defect category only (least disclosure).
    """
    if not configured or raw is None:
        raise ConflictError(
            "dormancy_period_months is not configured for this tenant; "
            "no members were transitioned — configure it via PUT /settings"
        )
    try:
        period = int(cast(int, raw))
    except (TypeError, ValueError) as exc:
        raise ConflictError(
            "stored dormancy configuration is corrupt; no members were "
            "transitioned — repair via PUT /settings"
        ) from exc
    if period < _MIN_PERIOD_MONTHS or period > _MAX_PERIOD_MONTHS:
        raise ConflictError(
            "stored dormancy configuration is outside its permitted bounds; "
            "no members were transitioned — repair via PUT /settings"
        )
    return period


async def resolve_dormancy_period(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """The dormancy period the run must use; raises 409 when unusable."""
    row = (await session.execute(text(DORMANCY_CONFIG_SQL), {"tid": str(tenant_id)})).first()
    if row is None:
        return parse_dormancy_period(None, configured=False)
    return parse_dormancy_period(row[0], configured=True)


def _member_initiated_placeholders(params: dict[str, object]) -> str:
    """Bound placeholders for the member-initiated type allow-list.

    Only the numbered placeholder NAMES are interpolated; the type
    values themselves travel as bound parameters (the _direction_clause shape). The values come
    exclusively from the
    code-owned MEMBER_INITIATED map.
    """
    keys: list[str] = []
    for i, value in enumerate(member_initiated_types()):
        key = f"mi_{i}"
        params[key] = value
        keys.append(f":{key}")
    return ", ".join(keys)


def dormancy_scan_sql(*, with_after: bool, type_placeholders: str) -> str:
    """The dormancy batch scan (the // scan shape).

    Walks idx_members_dormancy_scan (0021: tenant_id, id WHERE status = 'active' — shipped in the
    same migration as this query, scalability);
    the activity anti-join is served by idx_txns_member_keyset (0008).
    Fragments are static literals chosen in code; every value is a
    bound parameter. Explicit tenant predicate on top of
    forced RLS. Exported so the EXPLAIN capture
    (tests/test_p1313_explain.py) asserts against the production SQL.

    Predicates, in anti-join order (a re-run is a
    lock-free no-op):

      * status = 'active' — only active members are candidates;
        already-dormant members leave the population on transition;
      * created_at <:cutoff — a member younger than the window can
        never be dormant (their admission IS activity; created_at is
        immutable, consistent with);
      * NOT EXISTS member-initiated, non-reversal transaction with
        occurred_at >=:cutoff — the LEDGER-DERIVED activity test
        system postings and reversals never keep a member
        active.

    The correlated MAX(occurred_at) subselect feeds the audit row with
    the exact last-activity figure (rule 7: exact figures live in the
    audit row, never in caller-facing messages).
    """
    after = "AND m.id > CAST(:after AS uuid) " if with_after else ""
    return (
        # Static fragments chosen in code; all values bound (rule 6).
        "SELECT m.id, m.version, m.created_at, "  # noqa: S608
        "(SELECT MAX(t.occurred_at) FROM transactions t "
        " WHERE t.tenant_id = m.tenant_id AND t.member_id = m.id "
        " AND t.reversal_of_id IS NULL "
        f" AND t.type IN ({type_placeholders})) AS last_activity_at "
        "FROM members m "
        "WHERE m.tenant_id = CAST(:tid AS uuid) "
        "AND m.status = 'active' "
        "AND m.created_at < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM transactions t "
        " WHERE t.tenant_id = m.tenant_id AND t.member_id = m.id "
        " AND t.occurred_at >= :cutoff "
        " AND t.reversal_of_id IS NULL "
        f" AND t.type IN ({type_placeholders})) "
        f"{after}"
        "ORDER BY m.id LIMIT :limit "
        "FOR UPDATE OF m SKIP LOCKED"
    )


@dataclass(frozen=True)
class DormancyRunResult:
    scanned: int
    transitioned: int
    batches: int
    period_months: int
    cutoff: date


async def _mark_dormant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    member_id: str,
    version: int,
    created_at: datetime,
    last_activity_at: datetime | None,
    cutoff: datetime,
    period_months: int,
    as_of: date,
) -> int:
    """Transition one LOCKED member Active -> Dormant; returns 0 or 1.

    The pure transition function validates the move (concurrency safety — the single transition
    gatekeeper); the UPDATE re-checks status =
    'active' and the version as defence in depth (both are stable
    under the row lock). Status is member state, so the optimistic
    version bumps like every other status writer. The audit row and
    the member-facing outbox notification commit in this same batch
    transaction (the house gates/1.5).
    """
    transition(MemberStatus.ACTIVE, MemberStatus.DORMANT)
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, least disclosure v1.1).
                "UPDATE members SET status = :st, version = version + 1, "
                "updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND status = :from_st AND version = :ver"
            ),
            {
                "st": MemberStatus.DORMANT.value,
                "id": member_id,
                "tid": str(tenant_id),
                "from_st": MemberStatus.ACTIVE.value,
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"member {member_id} changed state during the dormancy run; retry")
    # Exact figures live in the audit row (rule 7): the ledger-derived
    # last activity, the window and the config in force, so every
    # transition stays reconstructable after a config change.
    await record_audit(
        session,
        tenant_id,
        None,
        action="member.status",
        entity="members",
        entity_id=member_id,
        before={"status": MemberStatus.ACTIVE.value},
        after={
            "status": MemberStatus.DORMANT.value,
            "reason": "dormancy",
            "last_activity_at": (
                last_activity_at.isoformat() if last_activity_at is not None else None
            ),
            "member_since": created_at.isoformat(),
            "cutoff": cutoff.isoformat(),
            "dormancy_period_months": period_months,
            "as_of": as_of.isoformat(),
        },
    )
    # Member-facing notification (reliability, outbox-only): dormancy is
    # part of the insider-fraud detection surface — the member learns
    # their account was parked, and later reactivation notifies them
    # again (application/members.reactivate_dormant_member).
    await enqueue_event(
        session,
        tenant_id,
        event_type="member.status_changed",
        payload={
            "member_id": member_id,
            "from": MemberStatus.ACTIVE.value,
            "to": MemberStatus.DORMANT.value,
            "reason": "dormancy",
        },
    )
    return 1


async def _process_batch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    after_id: uuid.UUID | None,
    batch_size: int,
    cutoff: datetime,
    period_months: int,
    as_of: date,
) -> tuple[int, uuid.UUID | None, int]:
    """One keyset batch: mark scanned members dormant under their row lock."""
    params: dict[str, object] = {"tid": str(tenant_id), "cutoff": cutoff, "limit": batch_size}
    placeholders = _member_initiated_placeholders(params)
    if after_id is not None:
        params["after"] = str(after_id)
    scan_sql = dormancy_scan_sql(with_after=after_id is not None, type_placeholders=placeholders)
    rows = (await session.execute(text(scan_sql), params)).all()
    transitioned = 0
    for row in rows:
        transitioned += await _mark_dormant(
            session,
            tenant_id,
            member_id=str(row[0]),
            version=int(row[1]),
            created_at=row[2],
            last_activity_at=row[3],
            cutoff=cutoff,
            period_months=period_months,
            as_of=as_of,
        )
    last_id = uuid.UUID(str(rows[-1][0])) if rows else None
    return len(rows), last_id, transitioned


async def _process_one(
    session: AsyncSession,
    after_id: uuid.UUID | None,
    *,
    tenant_id: uuid.UUID,
    batch_size: int,
    cutoff: datetime,
    period_months: int,
    as_of: date,
) -> tuple[int, uuid.UUID | None, int]:
    """Adapter matching the shared BatchProcessor signature."""
    return await _process_batch(
        session,
        tenant_id,
        after_id=after_id,
        batch_size=batch_size,
        cutoff=cutoff,
        period_months=period_months,
        as_of=as_of,
    )


async def run_dormancy_for_tenant(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    as_of: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DormancyRunResult:
    """Mark one tenant's inactive members dormant in short batches.

    The dormancy period is resolved ONCE, before the first batch, from
    tenant settings only: an unconfigured or corrupt
    period REFUSES the whole run with a 409-class error and zero
    transitions (never a silent default). A config change
    mid-run affects the NEXT run; every audit row records the values
    actually used.
    """
    async with session_scope() as session:
        period_months = await resolve_dormancy_period(session, tenant_id)
    cutoff_date = dormancy_cutoff(as_of, period_months)
    # Midnight UTC at the start of the cutoff day: activity anywhere ON
    # the cutoff day still counts as inside the window (documented
    # boundary; hand-computed oracles in tests/test_dormancy.py).
    cutoff = datetime.combine(cutoff_date, time.min, tzinfo=UTC)
    process = partial(
        _process_one,
        tenant_id=tenant_id,
        batch_size=batch_size,
        cutoff=cutoff,
        period_months=period_months,
        as_of=as_of,
    )
    scanned, batches, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    return DormancyRunResult(
        scanned=scanned,
        transitioned=sum(payloads),
        batches=batches,
        period_months=period_months,
        cutoff=cutoff_date,
    )
