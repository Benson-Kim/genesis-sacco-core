"""Dividends & share lifecycle services.

Workflow (mirrors the P12 exit machinery, reused not forked):

  1. declare_dividend — resolves the config (fail closed) and
     the last COMPLETED financial year server-side, computes every
     member's entitlement from the ledger-reconstructed average share
     and deposit balances over that year (the P11 ADB precedent via
     the shared period_balances helper — v1.1 rule 2), and persists
     the APPROVAL SNAPSHOT (v1.1 rule 3): rates, FY period,
     eligible-member count, total bases, total payout. Totals are
     DERIVED as the sum of the already-rounded per-member figures
     (each rounded exactly once by domain.dividends.entitlement_amount)
     so SUM(member postings) equals the declared total identically —
     the documented zero-residue rule (failure mode 1).
  2. cast_dividend_vote — committee approval via the P9 voting
     machinery (quorum read AT VOTE TIME under the declaration row
     lock; one vote per voter by DB UNIQUE; the declaring user can
     never vote on their own declaration).
  3. distribute_dividend — the distribution job through the shared
     batch runner. The FIRST run re-verifies the snapshot component by
     component (recomputing count/bases/payout at the SNAPSHOT rates)
     and 409s on any drift, posting nothing (approval TOCTOU, failure
     mode 4). A configured-rate change after approval never changes what is
     posted: figures always derive from the snapshot rates, and the
     0020 write-once trigger makes the snapshot immutable even against
     manual SQL. Each batch: members scanned in id-keyset order FOR
     UPDATE SKIP LOCKED with a NOT EXISTS anti-join on the claim table
     (rule 8: a fully distributed re-run scans, locks and reconstructs
     nothing), then per member — under the member row lock and the
     deposit -> share account row locks, the exact P12 chain prefix —
     the entitlement is recomputed, claimed with INSERT ... ON
     CONFLICT DO NOTHING checked by rowcount (v1.1 rule 5), posted
     through the P7 contract with occurred_at at the very end of the
     financial year (data integrity: dividends compound into the next
     period's basis), balances credited, and the audit row written
     with the declaration id and every figure verbatim — ONE batch
     transaction, no partial state (failure modes 3 and 9).
     Since the recorded design decision the paying scan
     also snapshots the member's stored dividend_payout preference in
     the SAME locking statement and routes the member-side credit
     legs per PAYOUT_CREDIT_ROUTING: 'deposit_account' and
     'share_capital' retain the whole payout in the mapped account;
     the external-channel tokens in PAYOUT_FALLBACK_TOKENS ('mpesa',
     'bank' — integrations unbuilt) are NEVER faked and
     fall back to the as-built default path with the token recorded
     verbatim in the audit row. A NULL preference keeps the as-built
     behaviour byte-for-byte. The unclaimed disposition below ignores
     the preference by design (an exited member's accounts are
     settled; the payable is the only honest destination).
  4. void_declaration — declared/approved -> rejected escape hatch for
     drifted snapshots; refused once any claim exists (voiding a
     partially distributed declaration would let a re-declaration pay
     the same year twice).
  5. share transfers — TWO-PHASE maker-checker
     (a human-review HIGH finding, remediated
     with the 0031 repayment-adjustment pattern): the
     MAKER's request validates under the FULL lock set (BOTH member
     rows FOR UPDATE in global member-id order — so opposing transfers
     can never deadlock, failure mode 7 — then both share accounts in
     the same order, the member -> accounts edge of the P12 chain) and
     INSERTs a PENDING share_transfers row bound to the persisted
     approval snapshot (from_balance_at_request, v1.1 rule 3); NO
     money moves. A DISTINCT, non-assurance CHECKER approves
     (server-side SoD + the 0040 DB CHECK; the shared
     application/sod.py guard): transfer row FOR UPDATE FIRST (the
     workflow anchor, the 0031 anchor-first shape), full lock set
     retaken, EVERY snapshot component re-verified (both members
     strictly ACTIVE, transferor balance identical) — 409 on drift
     posting NOTHING — then the ST- postings, both balance updates,
     the one-shot decision fill, the audit row and the outbox events
     (operator event + member notices to BOTH members, the
     detection-control precedent) commit in ONE transaction. Rejection
     is a version-pinned checker decision with a mandatory rationale
     (the recorded-rationale posture) locking the transfer row alone. The
     ledger-(m) register read walks the 0040 pending-first band index
     (the 0038 keyset pattern).

Population rules (all falsifiable):
active, arrears AND dormant members are scanned (allow-list — the
recorded policy decision: dormant members remain shareholders and are
paid on their share basis; entitlement follows the RECORD DATE, i.e.
the persisted declaration snapshot, so a member going dormant between
declaration and distribution is still paid). Exited members are never
scanned and never paid directly (the partial index cannot serve them);
a member who EXITS mid-run terminates in the explicit 'unclaimed'
disposition below — never a permanent silent pending_members
shortfall. Zero-entitlement members are SKIPPED — no claim row, no
0.00 posting, no noise rows.
Documented trade-off: because zero-entitlement members are (by design)
never claimed, a re-run re-examines exactly that class; the lock-free
no-op guarantee of the anti-join applies to every member who was paid.

Verification-vs-rerun note (documented, deliberate): aggregate snapshot
re-verification runs only while NO claim exists for the declaration.
Once distribution has begun, each already-paid member's OWN posting
(occurred_at inside the FY's last day, per the recognition rule) becomes part of
their reconstructed basis, so recomputing the aggregate would no longer
match the snapshot; unpaid members' bases are member-attributed and
therefore unaffected, and the write-once snapshot plus the per-member
claims carry the approved figures for the remainder of the run.

Unclaimed disposition (the SACCO-standard treatment of
mid-run exit drift): after the paying batches, the distribution job
runs a disposition pass over members who EXITED AFTER the declaration
was created — fenced by member_exits.settled_at > the declaration's
created_at, so members who exited before the record date were never on
the register and stay excluded ('exited stays excluded from direct
payment'). Each such member's snapshot-rate entitlement is claimed on
the same (tenant, declaration, member) UNIQUE key with
disposition='unclaimed' (exactly-once, v1.1 rule 5) and posted
DR expense / CR liability.unclaimed_dividends — an explicit payable;
their settled member accounts are never credited. The audit row
carries the exact figures (rule 7) and the dividend.unclaimed outbox
event is the alert surface. Because the unclaimed claim participates
in the snapshot reconciliation, the run COMPLETES (distributed)
instead of pending forever. The payable is resolvable only through
the correction paths (forward reference — reversing entries,
never UPDATE/DELETE). An exit BEFORE any claim exists stays on the
existing loud path instead: first-run verification 409s, the
declaration is voided and redeclared, and the leaver is simply not on
the new register.

Failure-mode boundaries: missing config (any of dividend rate, rebate
rate, financial-year-end month) fails CLOSED — the declaration is
refused with 409, never a defaulted rate; corrupted stored config
(manual SQL past the CHECKs) raises 409 loudly (the established
parse_penalty_config precedent). Every read AND write carries an
explicit bound tenant_id predicate on top of forced RLS (v1.1 rule 4);
all values travel as bound parameters (rule 6); errors are least-
disclosure — exact figures live in the audit rows only (rule 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.batch_runner import SessionScope, run_in_batches
from genesis.application.ledger import (
    post_dividend_distribution,
    post_share_transfer,
    post_unclaimed_dividend,
)
from genesis.application.outbox import enqueue_event
from genesis.application.pagination import (
    build_band_register_cursor,
    build_created_id_cursor,
    decode_cursor,
    encode_cursor,
    parse_band_register_cursor,
    parse_created_id_cursor,
)
from genesis.application.period_balances import average_daily_balance
from genesis.application.sod import require_distinct_non_assurance_checker
from genesis.application.tenant_settings import committee_quorum
from genesis.application.transactions import _lock_account, _set_balance
from genesis.domain.committee import Decision, Vote, decide
from genesis.domain.dividends import (
    FinancialYear,
    entitlement_amount,
    last_completed_financial_year,
)
from genesis.domain.ledger import Account
from genesis.domain.members import DividendPayout, MemberStatus, MoneyOperation, member_may
from genesis.domain.money import ZERO, to_cents
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError, NotFoundError

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DIVIDEND_CONFIG_SQL",
    "PAYOUT_CREDIT_ROUTING",
    "PAYOUT_FALLBACK_TOKENS",
    "DeclarationRecord",
    "DeclarationStatus",
    "DeclarationTotals",
    "DistributionRunResult",
    "DividendConfig",
    "DividendVoteTally",
    "MemberEntitlement",
    "ShareTransferPage",
    "ShareTransferRecord",
    "ShareTransferResult",
    "ShareTransferStatus",
    "approve_share_transfer",
    "cast_dividend_vote",
    "compute_declaration_totals",
    "compute_member_entitlement",
    "declare_dividend",
    "distribute_dividend",
    "get_declaration",
    "get_share_transfer",
    "list_declarations",
    "list_share_transfers",
    "members_scan_sql",
    "parse_dividend_config",
    "reject_share_transfer",
    "request_share_transfer",
    "resolve_dividend_config",
    "share_transfer_transition",
    "share_transfers_register_sql",
    "unclaimed_scan_sql",
    "void_declaration",
]

#: Members processed per batch transaction (the P10/P11 precedent).
DEFAULT_BATCH_SIZE = 200

#: dividend_payout token -> member-side CREDIT destination (the
#: recorded design
#: decision retiring the preference-only fence). Both credit
#: legs of the member's payout posting route to the mapped retention
#: account; the DR legs keep their expense classification, so the
#: legs stay balanced and the declared totals conserve exactly.
#: CODE-OWNED: keys are the enum, values are ledger accounts — never
#: caller input (least disclosure).
PAYOUT_CREDIT_ROUTING: dict[DividendPayout, Account] = {
    DividendPayout.DEPOSIT_ACCOUNT: Account.MEMBER_DEPOSITS,
    DividendPayout.SHARE_CAPITAL: Account.MEMBER_SHARES,
}

#: Tokens with NO implementable routing in the current ledger model:
#: external cash channels (M-Pesa B2C integration is unbuilt,
#: unbuilt; no bank disbursement integration exists either — a mass
#: job auto-posting a cash leg would assert money left the till with
#: no transfer executed). NEVER faked: these fall back to the as-built
#: default posting path and the member's preference token is recorded
#: VERBATIM in the distribution audit row; the gap is a recorded follow-up.
#: Together with PAYOUT_CREDIT_ROUTING this set is pinned set-equal to
#: the DividendPayout enum (falsifiable — a future token forces a
#: deliberate routing decision here).
PAYOUT_FALLBACK_TOKENS: frozenset[DividendPayout] = frozenset(
    {DividendPayout.MPESA, DividendPayout.BANK}
)

#: Dividend configuration read (v1.1 rule 1): one PK probe of the
#: one-row-per-tenant settings table, resolved server-side. Explicit
#: tenant predicate on top of forced RLS (v1.1 rule 4). Exported for
#: the EXPLAIN capture (tests/test_p1311_explain.py).
DIVIDEND_CONFIG_SQL = (
    "SELECT dividend_rate_pct, deposit_rebate_rate_pct, financial_year_end_month "
    "FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"
)

_DECLARATION_COLS = (
    "id, fy_start, fy_end, dividend_rate_pct, rebate_rate_pct, "
    "eligible_members, total_share_basis, total_deposit_basis, "
    "total_dividend, total_rebate, total_payout, status, requested_by, "
    "decided_at, distributed_at, version, created_at"
)


class DeclarationStatus(StrEnum):
    DECLARED = "declared"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISTRIBUTED = "distributed"


#: The single allowed-transitions map (concurrency safety). rejected/distributed
#: are terminal; approving is the committee's, distributing the job's.
_DECLARATION_TRANSITIONS: dict[DeclarationStatus, frozenset[DeclarationStatus]] = {
    DeclarationStatus.DECLARED: frozenset({DeclarationStatus.APPROVED, DeclarationStatus.REJECTED}),
    DeclarationStatus.APPROVED: frozenset(
        {DeclarationStatus.DISTRIBUTED, DeclarationStatus.REJECTED}
    ),
    DeclarationStatus.REJECTED: frozenset(),
    DeclarationStatus.DISTRIBUTED: frozenset(),
}


def _transition(current: DeclarationStatus, target: DeclarationStatus) -> None:
    if target not in _DECLARATION_TRANSITIONS[current]:
        raise ConflictError(f"dividend declaration cannot move from '{current}' to '{target}'")


@dataclass(frozen=True)
class DividendConfig:
    """The tenant's dividend parameters, snapshotted at declaration."""

    dividend_rate_pct: Decimal
    rebate_rate_pct: Decimal
    fy_end_month: int


def parse_dividend_config(
    dividend_raw: object, rebate_raw: object, month_raw: object
) -> DividendConfig | None:
    """Fail-closed parse of the stored dividend configuration.

    None (declaration refused as unconfigured — never a defaulted
    rate, never a 500) when ANY of the three keys is unconfigured: a
    partially configured tenant is exactly as unconfigured as one with
    no settings row. Corrupted stored values — reachable only by
    manual SQL bypassing the 0017/0020 DB CHECKs — raise ConflictError
    (409), the established fail-closed consumer precedent: a money
    guard is never silently disabled. Least disclosure: the message
    names the defect category, never a figure (least disclosure).
    """
    if dividend_raw is None or rebate_raw is None or month_raw is None:
        return None
    try:
        dividend = Decimal(str(dividend_raw))
        rebate = Decimal(str(rebate_raw))
        month = int(cast(int, month_raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConflictError(
            "stored dividend configuration is corrupt; repair via PUT /settings"
        ) from exc
    if (
        not dividend.is_finite()
        or not rebate.is_finite()
        or dividend < 0
        or dividend > 100
        or rebate < 0
        or rebate > 100
        or month < 1
        or month > 12
    ):
        raise ConflictError(
            "stored dividend configuration is outside its permitted bounds; "
            "repair via PUT /settings"
        )
    return DividendConfig(dividend_rate_pct=dividend, rebate_rate_pct=rebate, fy_end_month=month)


async def resolve_dividend_config(
    session: AsyncSession, tenant_id: uuid.UUID
) -> DividendConfig | None:
    """The parameters a declaration must run with, or None (refuse)."""
    row = (await session.execute(text(DIVIDEND_CONFIG_SQL), {"tid": str(tenant_id)})).first()
    if row is None:
        return None
    return parse_dividend_config(row[0], row[1], row[2])


# ---------------------------------------------------------------------------
# Entitlements and totals (the shared declaration/verification math)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberEntitlement:
    member_id: uuid.UUID
    share_basis: Decimal
    deposit_basis: Decimal
    dividend: Decimal
    rebate: Decimal

    @property
    def total(self) -> Decimal:
        return to_cents(self.dividend + self.rebate)


async def compute_member_entitlement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    fy: FinancialYear,
    dividend_rate_pct: Decimal,
    rebate_rate_pct: Decimal,
) -> MemberEntitlement:
    """One member's dividend + rebate for the year, ledger-reconstructed.

    Basis = the average daily balance over the financial year from the
    shared P11 reconstruction (v1.1 rule 2: never a point-in-time
    balance — parking shares on the measurement date earns only its
    pro-rata days). Each component figure is rounded exactly once by
    the single domain rounding point (failure mode 1).
    """
    share_basis = await average_daily_balance(
        session, tenant_id, member_id, kind="share", start=fy.start, end=fy.end
    )
    deposit_basis = await average_daily_balance(
        session, tenant_id, member_id, kind="deposit", start=fy.start, end=fy.end
    )
    return MemberEntitlement(
        member_id=member_id,
        share_basis=share_basis,
        deposit_basis=deposit_basis,
        dividend=entitlement_amount(share_basis, dividend_rate_pct),
        rebate=entitlement_amount(deposit_basis, rebate_rate_pct),
    )


def members_scan_sql(*, with_after: bool, with_claims: bool, for_update: bool) -> str:
    """The eligible-population scan (id-keyset over active/arrears/dormant).

    Served by idx_members_dividend_scan (predicate widened to include
    dormant members in 0022 per the recorded policy decision, shipped
    with this query; exited members still never widen the scan — they
    are excluded from direct payment); the claim anti-join by
    uq_dividend_distributions_claim (0020 — the index ships with this
    query, scalability). Fragments are static literals chosen in code;
    every value is a bound parameter (v1.1 rule 6). Explicit tenant
    predicate on top of forced RLS (v1.1 rule 4). Exported for the
    EXPLAIN capture (tests/test_p1311_explain.py).
    """
    after = "AND m.id > CAST(:after AS uuid) " if with_after else ""
    anti = ""
    if with_claims:
        # Anti-join (rule 8): a fully distributed re-run scans and
        # locks nothing; the ON CONFLICT claim remains the
        # authoritative guard (v1.1 rule 5).
        anti = (
            "AND NOT EXISTS (SELECT 1 FROM dividend_distributions dd "
            "WHERE dd.tenant_id = m.tenant_id "
            "AND dd.declaration_id = CAST(:decl AS uuid) "
            "AND dd.member_id = m.id) "
        )
    lock = "FOR UPDATE OF m SKIP LOCKED" if for_update else ""
    cols = "m.id, m.dividend_payout" if for_update else "m.id"
    return (
        # Static fragments chosen in code; all values bound parameters.
        f"SELECT {cols} FROM members m "  # noqa: S608
        "WHERE m.tenant_id = CAST(:tid AS uuid) "
        f"AND m.status IN ('active', 'arrears', 'dormant') {after}{anti}"
        f"ORDER BY m.id LIMIT :limit {lock}"
    )


def unclaimed_scan_sql(*, with_after: bool) -> str:
    """Members owed an unclaimed disposition (id-keyset).

    Exited members with a settled P12 exit AFTER the declaration was
    created (the record-date fence: whoever exited before the
    declaration was never on the register) and no claim yet for this
    declaration. Served by idx_members_exited_scan (0022 — shipped
    with this query, scalability); the exit probe by idx_exits_member
    (0001) and the claim anti-join by uq_dividend_distributions_claim
    (0020). FOR UPDATE SKIP LOCKED on the member row only — the ROOT
    tier of the established chain, exactly like the paying scan; the
    disposition never touches account rows, so no new lock-graph
    edges. Fragments are static literals chosen in code; every value
    is a bound parameter (v1.1 rule 6). Explicit tenant predicate on
    top of forced RLS (v1.1 rule 4). Exported for the EXPLAIN capture
    (tests/test_p1311_explain.py).
    """
    after = "AND m.id > CAST(:after AS uuid) " if with_after else ""
    return (
        # Static fragments chosen in code; all values bound parameters.
        "SELECT m.id FROM members m "  # noqa: S608
        "WHERE m.tenant_id = CAST(:tid AS uuid) "
        "AND m.status = 'exited' "
        "AND EXISTS (SELECT 1 FROM member_exits e "
        " WHERE e.tenant_id = m.tenant_id AND e.member_id = m.id "
        " AND e.status = 'settled' AND e.settled_at > :declared_at) "
        "AND NOT EXISTS (SELECT 1 FROM dividend_distributions dd "
        " WHERE dd.tenant_id = m.tenant_id "
        " AND dd.declaration_id = CAST(:decl AS uuid) "
        " AND dd.member_id = m.id) "
        f"{after}"
        "ORDER BY m.id LIMIT :limit FOR UPDATE OF m SKIP LOCKED"
    )


@dataclass(frozen=True)
class DeclarationTotals:
    eligible_members: int
    total_share_basis: Decimal
    total_deposit_basis: Decimal
    total_dividend: Decimal
    total_rebate: Decimal

    @property
    def total_payout(self) -> Decimal:
        return to_cents(self.total_dividend + self.total_rebate)


async def compute_declaration_totals(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    *,
    fy: FinancialYear,
    dividend_rate_pct: Decimal,
    rebate_rate_pct: Decimal,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DeclarationTotals:
    """Totals over the eligible population — the declaration AND the
    execution re-verification run this exact function (reuse-first), so
    the two sides can never diverge in method.

    Lock-free id-keyset batches through the shared runner (short
    transactions, scalability): the FY basis is append-only history and
    the end-anchor is single-statement consistent (see
    period_balances), so no row locks are needed to read it. A member
    counts as eligible when their rounded total entitlement is
    positive — exactly the set the distribution will claim.
    """

    async def process(
        session: AsyncSession, after_id: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, DeclarationTotals]:
        params: dict[str, object] = {"tid": str(tenant_id), "limit": batch_size}
        if after_id is not None:
            params["after"] = str(after_id)
        rows = (
            await session.execute(
                text(
                    members_scan_sql(
                        with_after=after_id is not None, with_claims=False, for_update=False
                    )
                ),
                params,
            )
        ).all()
        eligible = 0
        share_basis = ZERO
        deposit_basis = ZERO
        dividend = ZERO
        rebate = ZERO
        for (member_id_raw,) in rows:
            entitlement = await compute_member_entitlement(
                session,
                tenant_id,
                uuid.UUID(str(member_id_raw)),
                fy=fy,
                dividend_rate_pct=dividend_rate_pct,
                rebate_rate_pct=rebate_rate_pct,
            )
            if entitlement.total <= ZERO:
                continue
            eligible += 1
            share_basis += entitlement.share_basis
            deposit_basis += entitlement.deposit_basis
            dividend += entitlement.dividend
            rebate += entitlement.rebate
        last_id = uuid.UUID(str(rows[-1][0])) if rows else None
        payload = DeclarationTotals(
            eligible_members=eligible,
            total_share_basis=share_basis,
            total_deposit_basis=deposit_basis,
            total_dividend=dividend,
            total_rebate=rebate,
        )
        return len(rows), last_id, payload

    _, _, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    return DeclarationTotals(
        eligible_members=sum(p.eligible_members for p in payloads),
        total_share_basis=sum((p.total_share_basis for p in payloads), ZERO),
        total_deposit_basis=sum((p.total_deposit_basis for p in payloads), ZERO),
        total_dividend=sum((p.total_dividend for p in payloads), ZERO),
        total_rebate=sum((p.total_rebate for p in payloads), ZERO),
    )


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclarationRecord:
    id: uuid.UUID
    fy_start: date
    fy_end: date
    dividend_rate_pct: Decimal
    rebate_rate_pct: Decimal
    eligible_members: int
    total_share_basis: Decimal
    total_deposit_basis: Decimal
    total_dividend: Decimal
    total_rebate: Decimal
    total_payout: Decimal
    status: DeclarationStatus
    requested_by: uuid.UUID | None
    decided_at: datetime | None
    distributed_at: datetime | None
    version: int
    created_at: datetime


def _row_to_declaration(row: Any) -> DeclarationRecord:
    return DeclarationRecord(
        id=uuid.UUID(str(row[0])),
        fy_start=row[1],
        fy_end=row[2],
        dividend_rate_pct=Decimal(str(row[3])),
        rebate_rate_pct=Decimal(str(row[4])),
        eligible_members=int(row[5]),
        total_share_basis=Decimal(str(row[6])),
        total_deposit_basis=Decimal(str(row[7])),
        total_dividend=Decimal(str(row[8])),
        total_rebate=Decimal(str(row[9])),
        total_payout=Decimal(str(row[10])),
        status=DeclarationStatus(str(row[11])),
        requested_by=uuid.UUID(str(row[12])) if row[12] is not None else None,
        decided_at=row[13],
        distributed_at=row[14],
        version=int(row[15]),
        created_at=row[16],
    )


async def get_declaration(
    session: AsyncSession, tenant_id: uuid.UUID, declaration_id: uuid.UUID
) -> DeclarationRecord:
    row = (
        await session.execute(
            text(
                f"SELECT {_DECLARATION_COLS} FROM dividend_declarations "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(declaration_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"dividend declaration {declaration_id} not found")
    return _row_to_declaration(row)


#: Cursor scope id: signed cursors are bound to this
#: endpoint and this tenant — no cross-scope replay (tenant isolation).
DECLARATIONS_LIST_SCOPE = "dividends.declarations"


async def list_declarations(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[DeclarationRecord], str | None]:
    """Keyset-paginated declarations, newest first (scalability).

    Backed by idx_dividend_declarations_keyset (0020, shipped with
    this query).
    """
    limit = max(1, min(limit, 100))
    clauses = ["tenant_id = CAST(:tid AS uuid)"]
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor, tenant_id=tenant_id, endpoint=DECLARATIONS_LIST_SCOPE, entity="declaration"
        )
        params["c_ts"], params["c_id"] = parse_created_id_cursor(inner, entity="declaration")
        clauses.append("(created_at, id) < (:c_ts, CAST(:c_id AS uuid))")
    rows = (
        await session.execute(
            text(
                f"SELECT {_DECLARATION_COLS} FROM dividend_declarations "  # noqa: S608
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page_rows = rows[:limit]
    items = [_row_to_declaration(r) for r in page_rows]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            build_created_id_cursor(last[16], last[0]),
            tenant_id=tenant_id,
            endpoint=DECLARATIONS_LIST_SCOPE,
        )
    return items, next_cursor


async def declare_dividend(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    today: date | None = None,
) -> DeclarationRecord:
    """Create the approval snapshot for the last completed financial year.

    Rates and the FY period come exclusively from tenant configuration
    (v1.1 rule 1) — the request body never carries them. Missing config
    refuses with 409 (fail closed, never a default); a payout of zero
    (nothing to distribute) also refuses: an empty declaration is
    unrepresentable (0020 CHECK total_payout > 0).
    """
    async with session_scope() as session:
        config = await resolve_dividend_config(session, tenant_id)
    if config is None:
        raise ConflictError(
            "dividends are not configured for this tenant (tenant_settings."
            "dividend_rate_pct, deposit_rebate_rate_pct, financial_year_end_month)"
        )
    as_of = today or datetime.now(UTC).date()
    fy = last_completed_financial_year(as_of, config.fy_end_month)
    totals = await compute_declaration_totals(
        session_scope,
        tenant_id,
        fy=fy,
        dividend_rate_pct=config.dividend_rate_pct,
        rebate_rate_pct=config.rebate_rate_pct,
        batch_size=batch_size,
    )
    if totals.total_payout <= ZERO:
        raise ConflictError(
            "nothing to declare: no member has a positive entitlement for the financial year"
        )
    declaration_id = uuid.uuid4()
    async with session_scope() as session:
        try:
            await session.execute(
                text(
                    "INSERT INTO dividend_declarations "
                    "(id, tenant_id, fy_start, fy_end, dividend_rate_pct, "
                    " rebate_rate_pct, eligible_members, total_share_basis, "
                    " total_deposit_basis, total_dividend, total_rebate, "
                    " total_payout, status, requested_by) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :fy_start, "
                    ":fy_end, :d_rate, :r_rate, :eligible, :share_basis, "
                    ":deposit_basis, :dividend, :rebate, :payout, :status, "
                    "CAST(:actor AS uuid))"
                ),
                {
                    "id": str(declaration_id),
                    "tid": str(tenant_id),
                    "fy_start": fy.start,
                    "fy_end": fy.end,
                    "d_rate": str(config.dividend_rate_pct),
                    "r_rate": str(config.rebate_rate_pct),
                    "eligible": totals.eligible_members,
                    "share_basis": str(totals.total_share_basis),
                    "deposit_basis": str(totals.total_deposit_basis),
                    "dividend": str(totals.total_dividend),
                    "rebate": str(totals.total_rebate),
                    "payout": str(totals.total_payout),
                    "status": DeclarationStatus.DECLARED.value,
                    "actor": str(actor_id) if actor_id else None,
                },
            )
        except IntegrityError as exc:
            # uq_dividend_declarations_fy: one live declaration per FY —
            # concurrent double-declares collapse to exactly one.
            raise ConflictError(
                "a live dividend declaration already exists for this financial year"
            ) from exc
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action="dividend_declaration.declare",
            entity="dividend_declarations",
            entity_id=str(declaration_id),
            after={
                "fy_start": fy.start.isoformat(),
                "fy_end": fy.end.isoformat(),
                "dividend_rate_pct": str(config.dividend_rate_pct),
                "rebate_rate_pct": str(config.rebate_rate_pct),
                "eligible_members": totals.eligible_members,
                "total_share_basis": str(totals.total_share_basis),
                "total_deposit_basis": str(totals.total_deposit_basis),
                "total_dividend": str(totals.total_dividend),
                "total_rebate": str(totals.total_rebate),
                "total_payout": str(totals.total_payout),
            },
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="dividend.declared",
            payload={
                "declaration_id": str(declaration_id),
                "fy_start": fy.start.isoformat(),
                "fy_end": fy.end.isoformat(),
                "total_payout": str(totals.total_payout),
            },
        )
        return await get_declaration(session, tenant_id, declaration_id)


# ---------------------------------------------------------------------------
# Committee voting (the P9 machinery, the P12 cast_exit_vote shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DividendVoteTally:
    approvals: int
    rejections: int
    decision: Decision | None
    status: DeclarationStatus


async def cast_dividend_vote(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    voter_id: uuid.UUID,
    declaration_id: uuid.UUID,
    vote: Vote,
) -> DividendVoteTally:
    """Record a committee vote on a declaration (concurrency safety).

    The declaration row lock serialises voters; the DB UNIQUE makes
    double-voting impossible outside this path too. Separation of
    duties: the declaring user can never vote on their own
    declaration. The quorum is read from tenant configuration AT VOTE
    TIME under the row lock: a quorum change between votes
    governs the next tally only, never retroactively.
    """
    row = (
        await session.execute(
            text(
                "SELECT status, requested_by FROM dividend_declarations "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(declaration_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"dividend declaration {declaration_id} not found")
    current = DeclarationStatus(str(row[0]))
    requested_by = uuid.UUID(str(row[1])) if row[1] is not None else None
    if current is not DeclarationStatus.DECLARED:
        raise ConflictError(f"voting is only open on declared dividends, not '{current.value}'")
    if requested_by is not None and voter_id == requested_by:
        raise ForbiddenError("the declarer of a dividend cannot vote on it")
    try:
        await session.execute(
            text(
                "INSERT INTO dividend_declaration_votes "
                "(tenant_id, declaration_id, voter_id, vote) "
                "VALUES (CAST(:tid AS uuid), CAST(:did AS uuid), "
                "CAST(:vid AS uuid), :vote)"
            ),
            {
                "tid": str(tenant_id),
                "did": str(declaration_id),
                "vid": str(voter_id),
                "vote": vote.value,
            },
        )
    except IntegrityError as exc:
        raise ConflictError("committee member has already voted on this declaration") from exc
    tally_rows = (
        await session.execute(
            text(
                "SELECT vote, count(*) FROM dividend_declaration_votes "
                "WHERE declaration_id = CAST(:did AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) GROUP BY vote"
            ),
            {"did": str(declaration_id), "tid": str(tenant_id)},
        )
    ).all()
    counts = {str(r[0]): int(r[1]) for r in tally_rows}
    approvals = counts.get(Vote.APPROVE.value, 0)
    rejections = counts.get(Vote.REJECT.value, 0)
    await record_audit(
        session,
        tenant_id,
        voter_id,
        action="dividend_declaration.vote",
        entity="dividend_declarations",
        entity_id=str(declaration_id),
        after={"vote": vote.value, "approvals": approvals, "rejections": rejections},
    )
    decision = decide(approvals, rejections, quorum=await committee_quorum(session, tenant_id))
    status: DeclarationStatus = current
    if decision is not None:
        target = (
            DeclarationStatus.APPROVED
            if decision is Decision.APPROVED
            else DeclarationStatus.REJECTED
        )
        _transition(current, target)
        decided = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE dividend_declarations SET status = :st, decided_at = now(), "
                    "version = version + 1, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"st": target.value, "id": str(declaration_id), "tid": str(tenant_id)},
            ),
        )
        if decided.rowcount != 1:  # pragma: no cover - unreachable under the row lock
            raise ConflictError(f"declaration {declaration_id} changed during voting; retry")
        status = target
        await record_audit(
            session,
            tenant_id,
            voter_id,
            action="dividend_declaration.decided",
            entity="dividend_declarations",
            entity_id=str(declaration_id),
            before={"status": current.value},
            after={
                "status": target.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
        await enqueue_event(
            session,
            tenant_id,
            event_type="dividend.decided",
            payload={
                "declaration_id": str(declaration_id),
                "decision": decision.value,
                "approvals": approvals,
                "rejections": rejections,
            },
        )
    return DividendVoteTally(
        approvals=approvals, rejections=rejections, decision=decision, status=status
    )


async def void_declaration(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    declaration_id: uuid.UUID,
    *,
    version: int,
) -> DeclarationRecord:
    """Void an open declaration (declared/approved -> rejected; concurrency safety).

    The escape hatch when an approved snapshot has drifted (execution
    returned 409): voiding frees the one-live-declaration-per-FY slot
    so a fresh declaration captures current figures. REFUSED once any
    distribution claim exists — a partially distributed declaration
    can never be voided, because re-declaring the same year would pay
    already-paid members twice (the claim key is per declaration).
    """
    row = (
        await session.execute(
            text(
                "SELECT status FROM dividend_declarations "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(declaration_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"dividend declaration {declaration_id} not found")
    current = DeclarationStatus(str(row[0]))
    _transition(current, DeclarationStatus.REJECTED)
    claimed = (
        await session.execute(
            text(
                "SELECT 1 FROM dividend_distributions "
                "WHERE declaration_id = CAST(:did AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"did": str(declaration_id), "tid": str(tenant_id)},
        )
    ).first()
    if claimed is not None:
        raise ConflictError(
            "distribution has already begun for this declaration; it can no longer be voided"
        )
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE dividend_declarations SET status = :st, decided_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": DeclarationStatus.REJECTED.value,
                "id": str(declaration_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for dividend declaration {declaration_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="dividend_declaration.void",
        entity="dividend_declarations",
        entity_id=str(declaration_id),
        before={"status": current.value},
        after={"status": DeclarationStatus.REJECTED.value},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="dividend.voided",
        payload={"declaration_id": str(declaration_id)},
    )
    return await get_declaration(session, tenant_id, declaration_id)


# ---------------------------------------------------------------------------
# Distribution job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionRunResult:
    scanned: int
    claimed: int
    skipped_zero: int
    skipped_claimed: int
    #: Mid-run-exited members disposed as unclaimed by THIS run
    #:; the claim rows/audit carry the exact figures.
    unclaimed: int
    dividend_total: Decimal
    rebate_total: Decimal
    payout_total: Decimal
    pending_members: int
    batches: int
    status: DeclarationStatus


async def _claims_exist(
    session: AsyncSession, tenant_id: uuid.UUID, declaration_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM dividend_distributions "
                "WHERE declaration_id = CAST(:did AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"did": str(declaration_id), "tid": str(tenant_id)},
        )
    ).first()
    return row is not None


async def _verify_snapshot(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    snapshot: DeclarationRecord,
    *,
    batch_size: int,
) -> None:
    """Approval-TOCTOU defence (v1.1 rule 3, failure mode 4).

    Recomputes eligible count, bases and payout AT THE SNAPSHOT RATES
    via the exact function the declaration used, and 409s on any
    component drift, posting nothing. A configured-rate change is NOT
    drift: figures always derive from the snapshot rates, so a rate
    change applies to future declarations only. Basis or population
    drift (a backdated posting into the FY, a member exiting right
    after approval) means the committee approved different totals —
    refuse; void and redeclare. A PRE-distribution exit therefore
    never needs the unclaimed disposition: the redeclared snapshot
    simply excludes the leaver (the recorded policy: 'exited stays excluded
    from direct payment').
    """
    fy = FinancialYear(start=snapshot.fy_start, end=snapshot.fy_end)
    totals = await compute_declaration_totals(
        session_scope,
        tenant_id,
        fy=fy,
        dividend_rate_pct=snapshot.dividend_rate_pct,
        rebate_rate_pct=snapshot.rebate_rate_pct,
        batch_size=batch_size,
    )
    if (
        totals.eligible_members != snapshot.eligible_members
        or totals.total_share_basis != snapshot.total_share_basis
        or totals.total_deposit_basis != snapshot.total_deposit_basis
        or totals.total_dividend != snapshot.total_dividend
        or totals.total_rebate != snapshot.total_rebate
        or totals.total_payout != snapshot.total_payout
    ):
        # Least disclosure: no figures echoed; the declaration and the
        # recomputation are both reconstructable by entitled staff.
        raise ConflictError(
            "dividend declaration snapshot is stale: the eligible population or "
            "its bases changed since approval; void the declaration and redeclare"
        )


async def _distribute_one(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    snapshot: DeclarationRecord,
    member_id: uuid.UUID,
    *,
    fy: FinancialYear,
    posted_at: datetime,
    preference: DividendPayout | None,
) -> tuple[int, int, Decimal, Decimal]:
    """Pay one LOCKED member; returns (claimed, skipped_claimed, dividend, rebate).

    Caller holds the member row lock from the batch scan. Lock order
    below is the P12 chain prefix: deposit account -> share account
    (both FOR UPDATE, taken unconditionally BEFORE any routing branch
    — no new lock-graph edges); the entitlement is computed under
    those locks, then claim + posting + balance updates + audit commit
    atomically with the batch (failure modes 3, 8, 9). A member
    without one of the accounts simply has a zero basis on that side
    (the reconstruction anchors on the account row).

    `preference` is the member's stored dividend_payout token, read by
    the batch scan in the SAME statement that took the member row lock
    (consistent snapshot, no TOCTOU window). Routing:
    PAYOUT_CREDIT_ROUTING tokens send BOTH credit legs (and the one
    balance update) to the mapped retention account;
    PAYOUT_FALLBACK_TOKENS (external channels, unbuilt)
    and None keep the as-built default byte-for-byte. When a
    preference is set, the audit row records the token VERBATIM plus
    the routing actually applied; a NULL-preference member's audit row
    stays byte-identical to its original form.
    """
    deposit_account_id: uuid.UUID | None = None
    share_account_id: uuid.UUID | None = None
    deposit_balance = ZERO
    share_balance = ZERO
    try:
        deposit_account_id, deposit_balance = await _lock_account(
            session, tenant_id, kind="deposit", member_id=member_id
        )
    except NotFoundError:
        deposit_account_id = None
    try:
        share_account_id, share_balance = await _lock_account(
            session, tenant_id, kind="share", member_id=member_id
        )
    except NotFoundError:
        share_account_id = None
    entitlement = await compute_member_entitlement(
        session,
        tenant_id,
        member_id,
        fy=fy,
        dividend_rate_pct=snapshot.dividend_rate_pct,
        rebate_rate_pct=snapshot.rebate_rate_pct,
    )
    if entitlement.total <= ZERO:
        # Failure mode 5: skipped, never 0.00-posted, no claim row.
        return 0, 0, ZERO, ZERO
    claim_id = uuid.uuid4()
    claim = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Single atomic claim on the UNIQUE idempotency key
                # (tenant_id, declaration_id, member_id): rowcount 0
                # means already paid — no SELECT-then-INSERT race, no
                # IntegrityError mid-batch (v1.1 rule 5). Bases and
                # the snapshot rates are recorded verbatim so every
                # posted shilling stays reconstructable (rule 7).
                "INSERT INTO dividend_distributions "
                "(id, tenant_id, declaration_id, member_id, share_basis, "
                " deposit_basis, dividend_rate_pct, rebate_rate_pct, "
                " dividend_amount, rebate_amount, total_amount) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                "CAST(:did AS uuid), CAST(:mid AS uuid), :share_basis, "
                ":deposit_basis, :d_rate, :r_rate, :dividend, :rebate, :total) "
                "ON CONFLICT (tenant_id, declaration_id, member_id) DO NOTHING"
            ),
            {
                "id": str(claim_id),
                "tid": str(tenant_id),
                "did": str(snapshot.id),
                "mid": str(member_id),
                "share_basis": str(entitlement.share_basis),
                "deposit_basis": str(entitlement.deposit_basis),
                "d_rate": str(snapshot.dividend_rate_pct),
                "r_rate": str(snapshot.rebate_rate_pct),
                "dividend": str(entitlement.dividend),
                "rebate": str(entitlement.rebate),
                "total": str(entitlement.total),
            },
        ),
    )
    if claim.rowcount == 0:
        # A concurrent runner claimed this member between the anti-join
        # scan and the insert; idempotency wins (failure mode 3).
        return 0, 1, ZERO, ZERO
    # The preference token routes the member-side credit
    # legs. Fallback tokens (external channels, unbuilt) and None keep
    # routed_account=None — the as-built default path byte-for-byte.
    routed_account = PAYOUT_CREDIT_ROUTING.get(preference) if preference is not None else None
    posting = await post_dividend_distribution(
        session,
        tenant_id,
        member_id,
        dividend=entitlement.dividend,
        rebate=entitlement.rebate,
        actor_id=actor_id,
        occurred_at=posted_at,
        credit_account=routed_account,
    )
    if routed_account is Account.MEMBER_DEPOSITS:
        # Whole payout retained in the deposit account (both credit
        # legs above named member.deposits). Missing-account handling
        # mirrors the as-built path: the ledger legs are the truth and
        # the reconstruction anchors on the account row.
        if entitlement.total > ZERO and deposit_account_id is not None:
            await _set_balance(
                session,
                tenant_id,
                kind="deposit",
                account_id=deposit_account_id,
                balance=to_cents(deposit_balance + entitlement.total),
            )
    elif routed_account is Account.MEMBER_SHARES:
        # Whole payout capitalised into share capital.
        if entitlement.total > ZERO and share_account_id is not None:
            await _set_balance(
                session,
                tenant_id,
                kind="share",
                account_id=share_account_id,
                balance=to_cents(share_balance + entitlement.total),
            )
    else:
        # As-built default (NULL preference or fallback token):
        # dividend capitalises into shares, rebate credits deposits.
        if entitlement.dividend > ZERO and share_account_id is not None:
            await _set_balance(
                session,
                tenant_id,
                kind="share",
                account_id=share_account_id,
                balance=to_cents(share_balance + entitlement.dividend),
            )
        if entitlement.rebate > ZERO and deposit_account_id is not None:
            await _set_balance(
                session,
                tenant_id,
                kind="deposit",
                account_id=deposit_account_id,
                balance=to_cents(deposit_balance + entitlement.rebate),
            )
    await session.execute(
        text(
            "UPDATE dividend_distributions SET transaction_id = CAST(:txn AS uuid) "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"txn": str(posting.txn_id), "id": str(claim_id), "tid": str(tenant_id)},
    )
    # Exact figures live here (rule 7): declaration id, snapshot rates
    # and reconstructed bases verbatim, so every shilling posted is
    # reconstructable even after any later config change.
    audit_after: dict[str, Any] = {
        "declaration_id": str(snapshot.id),
        "member_id": str(member_id),
        "fy_start": fy.start.isoformat(),
        "fy_end": fy.end.isoformat(),
        "share_basis": str(entitlement.share_basis),
        "deposit_basis": str(entitlement.deposit_basis),
        "dividend_rate_pct": str(snapshot.dividend_rate_pct),
        "rebate_rate_pct": str(snapshot.rebate_rate_pct),
        "dividend_amount": str(entitlement.dividend),
        "rebate_amount": str(entitlement.rebate),
        "total_amount": str(entitlement.total),
        "txn_ref": posting.txn_ref,
    }
    if preference is not None:
        # The preference token VERBATIM plus the routing
        # actually applied — 'default' means the honest fallback (the
        # external channel is unbuilt; the money took the as-built
        # path). Keys are ABSENT for NULL-preference members so their
        # audit rows stay byte-identical to the original form.
        audit_after["dividend_payout"] = preference.value
        audit_after["applied_routing"] = (
            preference.value if routed_account is not None else "default"
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="dividend_distribution.post",
        entity="dividend_distributions",
        entity_id=str(claim_id),
        after=audit_after,
    )
    return 1, 0, entitlement.dividend, entitlement.rebate


async def _dispose_unclaimed_one(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    snapshot: DeclarationRecord,
    member_id: uuid.UUID,
    *,
    fy: FinancialYear,
    posted_at: datetime,
) -> int:
    """Dispose one LOCKED mid-run-exited member as unclaimed; 0 or 1.

    Caller holds the member row lock from the disposition scan (root
    tier only — no account rows are locked or written: the member
    exited and their balances are settled). The entitlement is
    recomputed at the SNAPSHOT rates via the exact declaration
    function; it reconstructs to the record-date figure identically
    because every post-exit posting (settlement, share transfer)
    carries occurred_at AFTER the financial year end — the append-only
    trigger and the open-period guard make a backdated posting into
    the FY unrepresentable. The claim reuses the one
    (tenant, declaration, member) UNIQUE key with
    disposition='unclaimed' (v1.1 rule 5: exactly-once even against a
    concurrent runner), the posting parks the money as the
    liability.unclaimed_dividends payable, and claim + posting + audit
    + outbox commit atomically with the batch.
    """
    entitlement = await compute_member_entitlement(
        session,
        tenant_id,
        member_id,
        fy=fy,
        dividend_rate_pct=snapshot.dividend_rate_pct,
        rebate_rate_pct=snapshot.rebate_rate_pct,
    )
    if entitlement.total <= ZERO:
        # Never part of the eligible count: nothing owed, no noise row.
        return 0
    claim_id = uuid.uuid4()
    claim = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # The same atomic claim as the paying path (v1.1 rule
                # 5) with the explicit terminal disposition; the
                # 'unclaimed' literal is a code-owned static fragment
                # (rule 6). Bases and snapshot rates are recorded
                # verbatim so the payable stays reconstructable.
                "INSERT INTO dividend_distributions "
                "(id, tenant_id, declaration_id, member_id, share_basis, "
                " deposit_basis, dividend_rate_pct, rebate_rate_pct, "
                " dividend_amount, rebate_amount, total_amount, disposition) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                "CAST(:did AS uuid), CAST(:mid AS uuid), :share_basis, "
                ":deposit_basis, :d_rate, :r_rate, :dividend, :rebate, "
                ":total, 'unclaimed') "
                "ON CONFLICT (tenant_id, declaration_id, member_id) DO NOTHING"
            ),
            {
                "id": str(claim_id),
                "tid": str(tenant_id),
                "did": str(snapshot.id),
                "mid": str(member_id),
                "share_basis": str(entitlement.share_basis),
                "deposit_basis": str(entitlement.deposit_basis),
                "d_rate": str(snapshot.dividend_rate_pct),
                "r_rate": str(snapshot.rebate_rate_pct),
                "dividend": str(entitlement.dividend),
                "rebate": str(entitlement.rebate),
                "total": str(entitlement.total),
            },
        ),
    )
    if claim.rowcount == 0:
        # A concurrent runner disposed (or paid) this member between
        # the anti-join scan and the insert; idempotency wins.
        return 0
    posting = await post_unclaimed_dividend(
        session,
        tenant_id,
        member_id,
        dividend=entitlement.dividend,
        rebate=entitlement.rebate,
        actor_id=actor_id,
        occurred_at=posted_at,
    )
    await session.execute(
        text(
            "UPDATE dividend_distributions SET transaction_id = CAST(:txn AS uuid) "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"txn": str(posting.txn_id), "id": str(claim_id), "tid": str(tenant_id)},
    )
    # Exact figures live here (rule 7): the payable is reconstructable
    # to the cent, and the disposition is resolvable only through the
    # Correction paths (forward reference).
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="dividend_distribution.unclaimed",
        entity="dividend_distributions",
        entity_id=str(claim_id),
        after={
            "declaration_id": str(snapshot.id),
            "member_id": str(member_id),
            "disposition": "unclaimed",
            "fy_start": fy.start.isoformat(),
            "fy_end": fy.end.isoformat(),
            "share_basis": str(entitlement.share_basis),
            "deposit_basis": str(entitlement.deposit_basis),
            "dividend_rate_pct": str(snapshot.dividend_rate_pct),
            "rebate_rate_pct": str(snapshot.rebate_rate_pct),
            "dividend_amount": str(entitlement.dividend),
            "rebate_amount": str(entitlement.rebate),
            "total_amount": str(entitlement.total),
            "txn_ref": posting.txn_ref,
        },
    )
    # The alert surface: operators subscribe to the
    # unclaimed disposition exactly like other money events.
    await enqueue_event(
        session,
        tenant_id,
        event_type="dividend.unclaimed",
        payload={
            "declaration_id": str(snapshot.id),
            "member_id": str(member_id),
            "dividend": str(entitlement.dividend),
            "rebate": str(entitlement.rebate),
            "total": str(entitlement.total),
            "txn_ref": posting.txn_ref,
        },
    )
    return 1


async def distribute_dividend(
    session_scope: SessionScope,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    declaration_id: uuid.UUID,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DistributionRunResult:
    """Distribute an approved declaration (idempotent, batched, atomic).

    Postings carry occurred_at at the very end of the financial year
    (data integrity): the dividend sorts after the year's real activity and
    compounds into the next period's basis. SKIP LOCKED members (a
    concurrent exit settlement or share transfer holds their lock) are
    picked up by an idempotent re-run (failure mode 8). The
    declaration is marked distributed only when nothing is pending and
    the claims reconcile exactly with the approved snapshot. Mid-run
    exits are settled by the unclaimed disposition pass after the
    paying batches (see the module docstring), so an
    exit never strands the declaration in a permanent pending state.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    f"SELECT {_DECLARATION_COLS} FROM dividend_declarations "  # noqa: S608
                    "WHERE id = CAST(:id AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
                ),
                {"id": str(declaration_id), "tid": str(tenant_id)},
            )
        ).first()
        if row is None:
            raise NotFoundError(f"dividend declaration {declaration_id} not found")
        snapshot = _row_to_declaration(row)
        if snapshot.requested_by is not None and actor_id == snapshot.requested_by:
            # User-level separation of duties (the P12 settlement ban):
            # the declarer can never execute the mass money-mover on
            # their own declaration.
            raise ForbiddenError("the declarer of a dividend cannot execute its distribution")
        if snapshot.status is not DeclarationStatus.APPROVED:
            raise ConflictError(
                f"dividend declaration is '{snapshot.status.value}': "
                "only approved declarations can be distributed"
            )
        verify = not await _claims_exist(session, tenant_id, declaration_id)

    if verify:
        # First run only (see the module docstring's verification-vs-
        # rerun note): once claims exist, paid members' own FY-end
        # postings are part of their reconstructed bases by design.
        await _verify_snapshot(session_scope, tenant_id, snapshot, batch_size=batch_size)

    fy = FinancialYear(start=snapshot.fy_start, end=snapshot.fy_end)
    # The end of the period the payout recognises (data integrity; the P11
    # review-finding-5 convention).
    posted_at = datetime.combine(fy.end, time.max, tzinfo=UTC)

    async def process(
        session: AsyncSession, after_id: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, tuple[int, int, int, Decimal, Decimal]]:
        # Void-vs-distribute TOCTOU (review finding F1): the opening
        # status check released its row lock with its session, so a
        # void (legal while zero claims exist) could otherwise commit
        # between that check and the first batch — paying a REJECTED
        # declaration and freeing the FY slot for a second one (a
        # year-level double). Re-verify under FOR SHARE, held to batch
        # commit: void's FOR UPDATE serialises behind it and then sees
        # this batch's claims (refused), or its committed rejection is
        # seen here and nothing is posted. A concurrent runner that
        # flipped the row to 'distributed' is not a conflict — the
        # anti-join already yields only unpaid members.
        status_row = (
            await session.execute(
                text(
                    "SELECT status FROM dividend_declarations "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "FOR SHARE"
                ),
                {"id": str(declaration_id), "tid": str(tenant_id)},
            )
        ).first()
        if status_row is None or str(status_row[0]) == DeclarationStatus.REJECTED.value:
            raise ConflictError(
                "dividend declaration was voided while distribution was starting; "
                "nothing was posted by this batch"
            )
        params: dict[str, object] = {
            "tid": str(tenant_id),
            "decl": str(declaration_id),
            "limit": batch_size,
        }
        if after_id is not None:
            params["after"] = str(after_id)
        rows = (
            await session.execute(
                text(
                    members_scan_sql(
                        with_after=after_id is not None, with_claims=True, for_update=True
                    )
                ),
                params,
            )
        ).all()
        claimed = 0
        skipped_zero = 0
        skipped_claimed = 0
        dividend_total = ZERO
        rebate_total = ZERO
        for member_id_raw, payout_raw in rows:
            # Fail-closed resolve against the code-owned enum: a value
            # outside it is unreachable past the 0039 DB CHECK, so a
            # ValueError here means catalog-level corruption — the
            # batch aborts loudly posting nothing, never a silent
            # misroute (the established precedent).
            preference = DividendPayout(str(payout_raw)) if payout_raw is not None else None
            one_claimed, one_conflict, dividend, rebate = await _distribute_one(
                session,
                tenant_id,
                actor_id,
                snapshot,
                uuid.UUID(str(member_id_raw)),
                fy=fy,
                posted_at=posted_at,
                preference=preference,
            )
            claimed += one_claimed
            skipped_claimed += one_conflict
            if one_claimed == 0 and one_conflict == 0:
                skipped_zero += 1
            dividend_total += dividend
            rebate_total += rebate
        last_id = uuid.UUID(str(rows[-1][0])) if rows else None
        return (
            len(rows),
            last_id,
            (claimed, skipped_zero, skipped_claimed, dividend_total, rebate_total),
        )

    scanned, batches, payloads = await run_in_batches(session_scope, process, batch_size=batch_size)
    claimed = sum(p[0] for p in payloads)
    skipped_zero = sum(p[1] for p in payloads)
    skipped_claimed = sum(p[2] for p in payloads)
    dividend_total = sum((p[3] for p in payloads), ZERO)
    rebate_total = sum((p[4] for p in payloads), ZERO)

    async def process_unclaimed(
        session: AsyncSession, after_id: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, int]:
        # Unclaimed disposition pass: members who EXITED
        # after the declaration was created and hold no claim yet. Runs
        # AFTER the paying batches so a member who exits mid-run is
        # caught in the same run; a member merely SKIP LOCKED-skipped
        # is still in the paying population and is never disposed here
        # (the scan's status/exit-fence predicates exclude them). Same
        # void-vs-distribute fence as the paying batches (review
        # finding F1): the FOR SHARE re-check is held to batch commit,
        # so a racing void either wins before any disposition or is
        # refused by the committed claims.
        status_row = (
            await session.execute(
                text(
                    "SELECT status FROM dividend_declarations "
                    "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "FOR SHARE"
                ),
                {"id": str(declaration_id), "tid": str(tenant_id)},
            )
        ).first()
        if status_row is None or str(status_row[0]) == DeclarationStatus.REJECTED.value:
            raise ConflictError(
                "dividend declaration was voided while distribution was starting; "
                "nothing was posted by this batch"
            )
        params: dict[str, object] = {
            "tid": str(tenant_id),
            "decl": str(declaration_id),
            "declared_at": snapshot.created_at,
            "limit": batch_size,
        }
        if after_id is not None:
            params["after"] = str(after_id)
        rows = (
            await session.execute(text(unclaimed_scan_sql(with_after=after_id is not None)), params)
        ).all()
        disposed = 0
        for (member_id_raw,) in rows:
            disposed += await _dispose_unclaimed_one(
                session,
                tenant_id,
                actor_id,
                snapshot,
                uuid.UUID(str(member_id_raw)),
                fy=fy,
                posted_at=posted_at,
            )
        last_id = uuid.UUID(str(rows[-1][0])) if rows else None
        return len(rows), last_id, disposed

    _, _, unclaimed_payloads = await run_in_batches(
        session_scope, process_unclaimed, batch_size=batch_size
    )
    unclaimed = sum(unclaimed_payloads)

    async with session_scope() as session:
        claims_row = (
            await session.execute(
                text(
                    "SELECT count(*), COALESCE(SUM(total_amount), 0) "
                    "FROM dividend_distributions "
                    "WHERE declaration_id = CAST(:did AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"did": str(declaration_id), "tid": str(tenant_id)},
            )
        ).one()
        claims_count = int(claims_row[0])
        claims_total = Decimal(str(claims_row[1]))
        unclaimed_count = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM dividend_distributions "
                        "WHERE declaration_id = CAST(:did AS uuid) "
                        "AND tenant_id = CAST(:tid AS uuid) "
                        "AND disposition = 'unclaimed'"
                    ),
                    {"did": str(declaration_id), "tid": str(tenant_id)},
                )
            ).scalar_one()
        )
        # The unpaid remainder of the APPROVED eligible set. A member
        # whose claim is still missing was SKIP LOCKED-skipped (an
        # idempotent re-run picks them up); a member who EXITED mid-run
        # was disposed as an 'unclaimed' claim by the disposition pass
        # above, so exits count against the snapshot
        # exactly like paid members and the run can COMPLETE instead of
        # pending forever. Zero-entitlement members are not part of the
        # eligible count and never hold the declaration open.
        pending = snapshot.eligible_members - claims_count
        status = DeclarationStatus.APPROVED
        if claims_count == snapshot.eligible_members and claims_total == snapshot.total_payout:
            _transition(DeclarationStatus.APPROVED, DeclarationStatus.DISTRIBUTED)
            done = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE dividend_declarations SET status = :st, "
                        "distributed_at = now(), version = version + 1, updated_at = now() "
                        "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                        "AND status = :from_st"
                    ),
                    {
                        "st": DeclarationStatus.DISTRIBUTED.value,
                        "id": str(declaration_id),
                        "tid": str(tenant_id),
                        "from_st": DeclarationStatus.APPROVED.value,
                    },
                ),
            )
            if int(done.rowcount or 0) == 1:
                status = DeclarationStatus.DISTRIBUTED
                await record_audit(
                    session,
                    tenant_id,
                    actor_id,
                    action="dividend_declaration.distributed",
                    entity="dividend_declarations",
                    entity_id=str(declaration_id),
                    before={"status": DeclarationStatus.APPROVED.value},
                    after={
                        "status": DeclarationStatus.DISTRIBUTED.value,
                        "claims": claims_count,
                        "unclaimed_members": unclaimed_count,
                        "payout_total": str(claims_total),
                    },
                )
                await enqueue_event(
                    session,
                    tenant_id,
                    event_type="dividend.distributed",
                    payload={
                        "declaration_id": str(declaration_id),
                        "claims": claims_count,
                        "unclaimed_members": unclaimed_count,
                        "payout_total": str(claims_total),
                    },
                )
            else:
                # A concurrent run finished the reconciliation first;
                # idempotency wins.
                status = (await get_declaration(session, tenant_id, declaration_id)).status

    return DistributionRunResult(
        scanned=scanned,
        claimed=claimed,
        skipped_zero=skipped_zero,
        skipped_claimed=skipped_claimed,
        unclaimed=unclaimed,
        dividend_total=dividend_total,
        rebate_total=rebate_total,
        payout_total=to_cents(dividend_total + rebate_total),
        pending_members=pending,
        batches=batches,
        status=status,
    )


# ---------------------------------------------------------------------------
# Share transfers — TWO-PHASE maker-checker workflow (a
# human-review HIGH finding, remediated with the
# 0031 repayment-adjustment pattern) + the transfer-history keyset
# history register (the 0038 band pattern, served by the 0040 index).
# ---------------------------------------------------------------------------


class ShareTransferStatus(StrEnum):
    """The 0040 transfer workflow machine."""

    PENDING = "pending"
    POSTED = "posted"
    REJECTED = "rejected"


_TRANSFER_ALLOWED: dict[ShareTransferStatus, frozenset[ShareTransferStatus]] = {
    ShareTransferStatus.PENDING: frozenset(
        {ShareTransferStatus.POSTED, ShareTransferStatus.REJECTED}
    ),
    ShareTransferStatus.POSTED: frozenset(),
    ShareTransferStatus.REJECTED: frozenset(),
}


def share_transfer_transition(current: ShareTransferStatus, target: ShareTransferStatus) -> None:
    """THE single gatekeeper for transfer status moves (concurrency safety).

    The only writers are approve/reject below; the 0040 write-once
    trigger enforces the same machine at the database, so a terminal
    row can never move again even via direct SQL. Illegal moves raise.
    """
    if target not in _TRANSFER_ALLOWED[current]:
        raise ConflictError(
            f"share transfer cannot move from '{current.value}' to '{target.value}'"
        )


@dataclass(frozen=True)
class ShareTransferRecord:
    id: uuid.UUID
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    amount: Decimal
    status: ShareTransferStatus
    out_transaction_id: uuid.UUID | None
    in_transaction_id: uuid.UUID | None
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    decided_at: datetime | None
    version: int
    created_at: datetime
    #: The persisted approval snapshot (v1.1 rule 3) — INTERNAL to the
    #: workflow: the API read models deliberately do NOT serialise it
    #: (least disclosure, rule 7 — the approval re-verifies it
    #: server-side; the audit rows carry the exact figures). NULL only
    #: on pre-0040 single-phase history.
    from_balance_at_request: Decimal | None


_TRANSFER_COLS = (
    "id, from_member_id, to_member_id, amount, status, "
    "out_transaction_id, in_transaction_id, created_by, approved_by, "
    "decided_at, version, created_at, from_balance_at_request"
)


def _row_to_transfer(row: Any) -> ShareTransferRecord:
    return ShareTransferRecord(
        id=uuid.UUID(str(row[0])),
        from_member_id=uuid.UUID(str(row[1])),
        to_member_id=uuid.UUID(str(row[2])),
        amount=Decimal(str(row[3])),
        status=ShareTransferStatus(str(row[4])),
        out_transaction_id=uuid.UUID(str(row[5])) if row[5] is not None else None,
        in_transaction_id=uuid.UUID(str(row[6])) if row[6] is not None else None,
        created_by=uuid.UUID(str(row[7])) if row[7] is not None else None,
        approved_by=uuid.UUID(str(row[8])) if row[8] is not None else None,
        decided_at=row[9],
        version=int(row[10]),
        created_at=row[11],
        from_balance_at_request=Decimal(str(row[12])) if row[12] is not None else None,
    )


@dataclass(frozen=True)
class ShareTransferResult:
    """The APPROVAL result — the money actually moved (phase 2)."""

    transfer_id: uuid.UUID
    out_txn_ref: str
    in_txn_ref: str
    amount: Decimal
    from_balance_after: Decimal
    to_balance_after: Decimal


async def _lock_member_status(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> str:
    """Member row FOR UPDATE; explicit tenant predicate on top of RLS."""
    row = (
        await session.execute(
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    return str(row[0])


@dataclass(frozen=True)
class _TransferLockContext:
    from_account_id: uuid.UUID
    from_balance: Decimal
    to_account_id: uuid.UUID
    to_balance: Decimal


async def _lock_transfer_chain(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_member_id: uuid.UUID,
    to_member_id: uuid.UUID,
) -> _TransferLockContext:
    """The FULL transfer lock set, shared VERBATIM by request and
    approval so the two phases can never diverge on the rule.

    Lock ordering (docs/diagrams/lock-order.md; the P12 settlement
    chain prefix): BOTH member rows FOR UPDATE in global member-id
    order — two-member operations use the id total order, so opposing
    transfers (A->B racing B->A) serialise instead of deadlocking
    (failure mode 7) — then both share accounts FOR UPDATE in the same
    member order (the existing member -> account edge; no new edges
    below the workflow anchor). Status checks run UNDER those locks,
    never as pre-checks:

      * transferor must be strictly ACTIVE (the P11 withdrawal rule:
        an arrears member may not move equity out from under their
        debt; exited members cannot transact),
      * transferee must be strictly ACTIVE (dormant/exited/arrears
        refused — the documented allow-list, owned by the
        member_may capability map).
    """
    statuses: dict[uuid.UUID, str] = {}
    for member_id in sorted((from_member_id, to_member_id)):
        statuses[member_id] = await _lock_member_status(session, tenant_id, member_id)
    # Code-owned capability map: both transfer sides are
    # strictly active-only, so arrears/dormant/exited (and any future
    # status) are refused by construction — the strictly-active
    # rule, owned by the single domain gatekeeper.
    if not member_may(MemberStatus(statuses[from_member_id]), MoneyOperation.SHARE_TRANSFER_OUT):
        raise ConflictError(
            f"member {from_member_id} is '{statuses[from_member_id]}': "
            "only active members may transfer shares"
        )
    if not member_may(MemberStatus(statuses[to_member_id]), MoneyOperation.SHARE_TRANSFER_IN):
        raise ConflictError(
            f"member {to_member_id} is '{statuses[to_member_id]}': "
            "shares may only be transferred to an active member"
        )
    accounts: dict[uuid.UUID, tuple[uuid.UUID, Decimal]] = {}
    for member_id in sorted((from_member_id, to_member_id)):
        accounts[member_id] = await _lock_account(
            session, tenant_id, kind="share", member_id=member_id
        )
    return _TransferLockContext(
        from_account_id=accounts[from_member_id][0],
        from_balance=accounts[from_member_id][1],
        to_account_id=accounts[to_member_id][0],
        to_balance=accounts[to_member_id][1],
    )


async def request_share_transfer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    from_member_id: uuid.UUID,
    *,
    to_member_id: uuid.UUID,
    amount: Decimal,
) -> ShareTransferRecord:
    """Phase 1 — the MAKER requests a transfer.

    Creates a PENDING share_transfers row capturing the persisted
    approval SNAPSHOT (the transferor's share balance at request —
    v1.1 rule 3) under the FULL lock set; NOTHING posts here. The
    ST- postings, both balance updates and the member notices happen
    only in approve_share_transfer, executed by a DISTINCT checker.

    Self-transfer is refused here AND unrepresentable at the database
    (the 0020 CHECK); the amount is re-verified under the transferor's
    account row lock (least-disclosure rejection — no balances echoed;
    the audit row carries the exact figures for staff entitled to
    them).
    """
    amount = to_cents(amount)
    if amount <= ZERO:
        raise InvalidInputError("transfer amount must be positive")
    if from_member_id == to_member_id:
        raise InvalidInputError("cannot transfer shares to the same member")
    ctx = await _lock_transfer_chain(session, tenant_id, from_member_id, to_member_id)
    if amount > ctx.from_balance:
        # Least disclosure (rule 7): no balances echoed; the audit rows
        # carry the exact figures.
        raise ConflictError(
            "insufficient share balance: the requested amount exceeds the "
            "transferable share capital"
        )
    transfer_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO share_transfers "
            "(id, tenant_id, from_member_id, to_member_id, amount, "
            " status, created_by, from_balance_at_request) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:f AS uuid), "
            "CAST(:t AS uuid), :amount, :status, CAST(:actor AS uuid), :snap)"
        ),
        {
            "id": str(transfer_id),
            "tid": str(tenant_id),
            "f": str(from_member_id),
            "t": str(to_member_id),
            "amount": str(amount),
            "status": ShareTransferStatus.PENDING.value,
            "actor": str(actor_id),
            "snap": str(ctx.from_balance),
        },
    )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="share_transfer.requested",
        entity="share_transfers",
        entity_id=str(transfer_id),
        after={
            "from_member_id": str(from_member_id),
            "to_member_id": str(to_member_id),
            "amount": str(amount),
            "status": ShareTransferStatus.PENDING.value,
            "from_balance_at_request": str(ctx.from_balance),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="share_transfer.requested",
        payload={
            "transfer_id": str(transfer_id),
            "from_member_id": str(from_member_id),
            "to_member_id": str(to_member_id),
            "amount": str(amount),
        },
    )
    return await get_share_transfer(session, tenant_id, transfer_id)


async def approve_share_transfer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    transfer_id: uuid.UUID,
) -> ShareTransferResult:
    """Phase 2 — a DISTINCT CHECKER approves and executes, atomically
    (the house gates).

    Snapshot-bind-reverify (v1.1 rule 3, the P12/0031 pattern —
    sequence-snapshot-bind-reverify.md): lock the pending transfer row
    FOR UPDATE (the workflow ANCHOR — the 0031 anchor-first shape;
    nothing anywhere acquires a share_transfers row while holding
    member/account locks, so the anchor sits above the member tier
    acyclically) -> status gatekeeper + segregation-of-duties checks
    (maker <> checker, server-side AND the 0040 DB CHECK behind it;
    assurance roles excluded — the shared application/sod.py guard) ->
    retake the FULL lock set (shared verbatim with the request) ->
    re-verify EVERY snapshot component (both members strictly ACTIVE,
    transferor balance IDENTICAL to the snapshot) -> 409 on drift,
    posting NOTHING -> only then the ST-OUT/ST-IN postings (P7
    contract, advisory-lock refs), both balance updates, the one-shot
    status/approver/decided_at/transaction fill, the in-transaction
    audit row, and the outbox events — the existing operator event
    (ledger.share_transfer_posted, enqueued by post_share_transfer)
    plus a member NOTICE to EACH of the two members (the
    dormancy-reactivation precedent records member
    notification as the insider-fraud DETECTION control — the victim
    of a colluding maker/checker pair sees their equity move). ONE
    transaction; an abort at any point leaves zero partial state
    (kill-switch tested).
    """
    ts = datetime.now(UTC)
    row = (
        await session.execute(
            text(
                f"SELECT {_TRANSFER_COLS} FROM share_transfers "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(transfer_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"share transfer {transfer_id} not found")
    record = _row_to_transfer(row)
    share_transfer_transition(record.status, ShareTransferStatus.POSTED)
    if record.created_by is None or record.from_balance_at_request is None:
        # Fail closed: only reachable via manual SQL — every pending
        # row the request path writes carries both (the 0040
        # pending-snapshot CHECK stands behind the snapshot half).
        raise ConflictError(
            f"share transfer {transfer_id} carries no maker attribution or "
            "snapshot and cannot be checked"
        )
    await require_distinct_non_assurance_checker(
        session,
        tenant_id,
        actor_id,
        record.created_by,
        subject="a share transfer",
        subject_plural="share transfers",
    )
    ctx = await _lock_transfer_chain(session, tenant_id, record.from_member_id, record.to_member_id)
    # Component-by-component re-verification against the persisted
    # snapshot (never "the current state"): any transferor-balance
    # drift since the request — a deposit, a dividend, another transfer
    # — is a 409 and NOTHING posts; reject the stale request and raise
    # a fresh one. (Both member statuses were re-verified strictly
    # ACTIVE under the locks inside _lock_transfer_chain.)
    if ctx.from_balance != record.from_balance_at_request:
        raise ConflictError(
            f"share transfer {transfer_id} snapshot has drifted since request "
            "(transferor balance); reject it and request afresh"
        )
    out_posting, in_posting = await post_share_transfer(
        session,
        tenant_id,
        from_member_id=record.from_member_id,
        to_member_id=record.to_member_id,
        amount=record.amount,
        actor_id=actor_id,
    )
    from_after = to_cents(ctx.from_balance - record.amount)
    to_after = to_cents(ctx.to_balance + record.amount)
    await _set_balance(
        session, tenant_id, kind="share", account_id=ctx.from_account_id, balance=from_after
    )
    await _set_balance(
        session, tenant_id, kind="share", account_id=ctx.to_account_id, balance=to_after
    )
    # The decision write — the ONLY post-insert mutation the 0040
    # write-once trigger permits: the pending -> posted transition plus
    # the one-shot NULL -> value fills (approved_by, decided_at, both
    # transaction ids). Every pinned column stays untouched; the
    # ck_share_transfers_sod CHECK re-verifies maker <> checker at the
    # database.
    decided = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE share_transfers "
                "SET status = :st, approved_by = CAST(:chk AS uuid), decided_at = :ts, "
                "out_transaction_id = CAST(:out_txn AS uuid), "
                "in_transaction_id = CAST(:in_txn AS uuid), "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {
                "st": ShareTransferStatus.POSTED.value,
                "chk": str(actor_id),
                "ts": ts,
                "out_txn": str(out_posting.txn_id),
                "in_txn": str(in_posting.txn_id),
                "id": str(transfer_id),
                "tid": str(tenant_id),
            },
        ),
    )
    if decided.rowcount != 1:  # pragma: no cover - unreachable under the row lock
        raise ConflictError(f"share transfer {transfer_id} vanished mid-transaction")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="share_transfer.posted",
        entity="share_transfers",
        entity_id=str(transfer_id),
        before={"status": ShareTransferStatus.PENDING.value},
        after={
            "status": ShareTransferStatus.POSTED.value,
            "from_member_id": str(record.from_member_id),
            "to_member_id": str(record.to_member_id),
            "amount": str(record.amount),
            "maker_id": str(record.created_by),
            "approved_by": str(actor_id),
            "from_balance_before": str(ctx.from_balance),
            "from_balance_after": str(from_after),
            "to_balance_before": str(ctx.to_balance),
            "to_balance_after": str(to_after),
            "out_txn_ref": out_posting.txn_ref,
            "in_txn_ref": in_posting.txn_ref,
        },
    )
    # Member NOTICES to BOTH members (the established
    # detection control; the guarantee.consented notify_member_id
    # shape). Ids and the amount only — never names (least disclosure). ONE
    # event per member so the dispatcher can address each recipient;
    # removing either enqueue fails the outbox-count test (FM11).
    for notify_member_id, role, counterparty in (
        (record.from_member_id, "from", record.to_member_id),
        (record.to_member_id, "to", record.from_member_id),
    ):
        await enqueue_event(
            session,
            tenant_id,
            event_type="share_transfer.member_notice",
            payload={
                "transfer_id": str(transfer_id),
                "notify_member_id": str(notify_member_id),
                "role": role,
                "counterparty_member_id": str(counterparty),
                "amount": str(record.amount),
                "out_txn_ref": out_posting.txn_ref,
                "in_txn_ref": in_posting.txn_ref,
            },
        )
    return ShareTransferResult(
        transfer_id=transfer_id,
        out_txn_ref=out_posting.txn_ref,
        in_txn_ref=in_posting.txn_ref,
        amount=record.amount,
        from_balance_after=from_after,
        to_balance_after=to_after,
    )


async def reject_share_transfer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    transfer_id: uuid.UUID,
    *,
    version: int,
    reason: str,
) -> ShareTransferRecord:
    """Reject a pending transfer — optimistic-locked (the
    0031 reject shape).

    Locks the transfer row ALONE (a single-node locker — the DECL/WOFF
    void pattern; no money lock is taken because nothing posts). The
    rejection is a CHECKER decision: the maker cannot decide their own
    request (server-side SoD + the 0040 DB CHECK; a maker withdrawing
    a mistaken request asks a checker to reject it) and assurance
    roles are excluded (the B2 principle). The rejected row is
    terminal, write-once workflow history (0040 trigger).

    The checker's rejection RATIONALE is required (the recorded-rationale posture:
    the four-eyes record must show WHY). It is workflow metadata —
    never a money parameter — and lands in the audit ``after``
    payload; the outbox payload stays ids-only and no error envelope
    ever echoes it (rule 7).
    """
    if not reason.strip():
        # Defence in depth beneath the boundary validation (the
        # Pydantic body already enforces min_length=1).
        raise InvalidInputError("a rejection reason is required")
    ts = datetime.now(UTC)
    row = (
        await session.execute(
            text(
                "SELECT status, created_by, version FROM share_transfers "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"id": str(transfer_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"share transfer {transfer_id} not found")
    current = ShareTransferStatus(str(row[0]))
    maker_id = uuid.UUID(str(row[1])) if row[1] is not None else None
    share_transfer_transition(current, ShareTransferStatus.REJECTED)
    if maker_id is None:
        # Fail closed: only reachable via manual SQL (see approve).
        raise ConflictError(
            f"share transfer {transfer_id} carries no maker attribution and cannot be checked"
        )
    await require_distinct_non_assurance_checker(
        session,
        tenant_id,
        actor_id,
        maker_id,
        subject="a share transfer",
        subject_plural="share transfers",
    )
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                "UPDATE share_transfers "
                "SET status = :st, approved_by = CAST(:chk AS uuid), decided_at = :ts, "
                "version = version + 1, updated_at = :ts "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "st": ShareTransferStatus.REJECTED.value,
                "chk": str(actor_id),
                "ts": ts,
                "id": str(transfer_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for share transfer {transfer_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="share_transfer.rejected",
        entity="share_transfers",
        entity_id=str(transfer_id),
        before={"status": current.value},
        after={
            "status": ShareTransferStatus.REJECTED.value,
            "approved_by": str(actor_id),
            # The checker's rationale, on the record.
            "reason": reason,
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="share_transfer.rejected",
        payload={"transfer_id": str(transfer_id)},
    )
    return await get_share_transfer(session, tenant_id, transfer_id)


async def get_share_transfer(
    session: AsyncSession, tenant_id: uuid.UUID, transfer_id: uuid.UUID
) -> ShareTransferRecord:
    """One transfer by id; explicit tenant predicate on top of RLS."""
    row = (
        await session.execute(
            text(
                f"SELECT {_TRANSFER_COLS} FROM share_transfers "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(transfer_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"share transfer {transfer_id} not found")
    return _row_to_transfer(row)


def share_transfers_register_sql(*, with_cursor: bool) -> str:
    """The share-transfer history register.

    ORDER BY (status = 'pending') DESC, created_at DESC, id DESC — the
    checker's job order: PENDING FIRST, newest first inside each band;
    terminal history behind (the 0038 register pattern, incl. its
    accepted property: a row whose band changes mid-pagination may
    surface twice across a paging session — duplicated, never missed).
    Uniform-DESC keyset row comparison, served by
    idx_share_transfers_register (0040, shipped with this query — gate
    1.3; EXPLAIN-asserted, falsifiable by dropping it). Static
    fragments chosen in code; every value is a bound parameter (v1.1
    rule 6); explicit tenant predicate on top of forced RLS (rule 4).
    """
    cursor = (
        "AND ((status = 'pending'), created_at, id) "
        "< (CAST(:c_flag AS boolean), CAST(:c_ts AS timestamptz), CAST(:c_id AS uuid)) "
    )
    return (
        f"SELECT {_TRANSFER_COLS} FROM share_transfers "  # noqa: S608
        "WHERE tenant_id = CAST(:tid AS uuid) "
        f"{cursor if with_cursor else ''}"
        "ORDER BY (status = 'pending') DESC, created_at DESC, id DESC "
        "LIMIT :limit"
    )


@dataclass(frozen=True)
class ShareTransferPage:
    items: list[ShareTransferRecord]
    next_cursor: str | None


#: Cursor scope id: signed cursors are bound to this
#: endpoint and this tenant — no cross-scope replay (tenant isolation).
SHARE_TRANSFERS_SCOPE = "dividends.share_transfers"


async def list_share_transfers(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
) -> ShareTransferPage:
    """Keyset transfer register, pending-first."""
    params: dict[str, object] = {"tid": str(tenant_id), "limit": limit + 1}
    if cursor is not None:
        # Opaque signed cursor: verify+unseal first;
        # the plaintext parse stays as defense-in-depth.
        inner = decode_cursor(
            cursor,
            tenant_id=tenant_id,
            endpoint=SHARE_TRANSFERS_SCOPE,
            entity="share transfer register",
        )
        c_flag, c_ts, c_id = parse_band_register_cursor(inner, entity="share transfer register")
        params["c_flag"] = c_flag
        params["c_ts"] = c_ts
        params["c_id"] = c_id
    stmt = text(share_transfers_register_sql(with_cursor=cursor is not None))
    rows = (await session.execute(stmt, params)).all()
    items = [_row_to_transfer(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = encode_cursor(
            build_band_register_cursor(
                last.status is ShareTransferStatus.PENDING, last.created_at, last.id
            ),
            tenant_id=tenant_id,
            endpoint=SHARE_TRANSFERS_SCOPE,
        )
    return ShareTransferPage(items=items, next_cursor=next_cursor)
