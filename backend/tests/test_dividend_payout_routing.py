"""P13.11 distribution engine consumes dividend_payout (#31 batch 12).

Human authorization recorded on #31 (maintainer note, 2026-08-07
21:48 UTC — "(c) AUTHORIZED as BATCH 12"). Requires a migrated
PostgreSQL database (DATABASE_URL) at head >= 0039.

Extends the P13.11 harness (test_dividends.py seeders and the
hand-computed FY oracle — never a fork; the import precedent is
test_p1311_explain.py). Preference fixtures are written through the
REAL versioned member-update writer, never raw SQL.

Anti-reward-hacking posture (MASTER_PROMPT section 4):
  * Every oracle is hand-computed in the test body from the seeds —
    FY 2025-07-01..2026-06-30 (365 days), dividend rate 5%, rebate
    rate 2%:
      - 12,000.00 shares held all 365 days -> share ADB 12,000.00 ->
        dividend 600.00;
      - 7,300.00 deposited 2026-04-02, held 90 days (Apr 29 + May 31
        + Jun 30) -> deposit ADB = 7,300 x 90 / 365 = 1,800.00 ->
        rebate 36.00;
      - total entitlement where a member holds both histories: 636.00.
  * Routing is proven at the LEDGER LEG level (account, side, exact
    Decimal amounts) and by balance side-effects — never by return
    values alone; idempotency by side-effect ROW COUNTS.
  * The design bounds from the recorded authorization are each pinned
    falsifiably: NULL keeps the as-built behaviour byte-for-byte
    (FM1); implementable retention tokens route (FM2/FM3);
    unimplementable external channels are NEVER faked — honest
    fallback with the token recorded verbatim in the audit row (FM4);
    re-runs never double-post (FM5); a mid-run preference flip can
    never split a payout (FM6 TOCTOU).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import factory
from export_helpers import count, seed_actor, seed_member
from genesis.application.dividends import (
    DeclarationStatus,
    declare_dividend,
    distribute_dividend,
)
from genesis.application.ledger import post_deposit, post_share_topup
from genesis.application.members import get_member, update_member
from genesis.domain.ledger import Channel
from genesis.errors import ConflictError
from genesis.infrastructure.tenancy import tenant_session
from test_dividends import (
    FY,
    TODAY,
    _approve,
    _balance,
    _configure_dividends,
    _member_with_share_history,
    _scope,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: The as-built per-member audit `after` keys (batch 8 shape) — the
#: FM1 byte-for-byte pin: a NULL-preference member's audit row must
#: carry EXACTLY these keys, never the batch-12 routing keys.
AS_BUILT_AUDIT_KEYS = {
    "declaration_id",
    "member_id",
    "fy_start",
    "fy_end",
    "share_basis",
    "deposit_basis",
    "dividend_rate_pct",
    "rebate_rate_pct",
    "dividend_amount",
    "rebate_amount",
    "total_amount",
    "txn_ref",
}


async def _set_preference(tid: uuid.UUID, mid: uuid.UUID, token: str) -> None:
    """Write the preference through the REAL versioned update path."""
    async with tenant_session(factory(), tid) as session:
        current = await get_member(session, tid, mid)
        await update_member(session, tid, None, mid, version=current.version, dividend_payout=token)


async def _member_with_both_histories(
    tid: uuid.UUID, *, name: str, preference: str | None = None
) -> uuid.UUID:
    """12,000.00 shares from FY day one and 7,300.00 deposited
    2026-04-02, both through the REAL P7 contract (the test_dividends
    fixture discipline) -> dividend 600.00, rebate 36.00, total 636.00
    at the 5% and 2% rates (hand-computed in the module docstring)."""
    mid = await seed_member(tid, name=name, shares="12000.00", deposit="7300.00")
    async with tenant_session(factory(), tid) as session:
        await post_share_topup(
            session,
            tid,
            mid,
            Decimal("12000.00"),
            Channel.BANK,
            occurred_at=datetime.combine(FY.start, time(12, 0), tzinfo=UTC),
        )
        await post_deposit(
            session,
            tid,
            mid,
            Decimal("7300.00"),
            Channel.BANK,
            occurred_at=datetime.combine(date(2026, 4, 2), time(12, 0), tzinfo=UTC),
        )
    if preference is not None:
        await _set_preference(tid, mid, preference)
    return mid


async def _dv_legs(tid: uuid.UUID, mid: uuid.UUID) -> list[tuple[str, str, Decimal]]:
    """The member's DV posting legs, deterministically ordered."""
    async with tenant_session(factory(), tid) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT le.account, le.side, le.amount FROM ledger_entries le "
                    "JOIN transactions t ON t.id = le.transaction_id "
                    "WHERE t.type = 'dividend_posting' "
                    "AND t.member_id = CAST(:m AS uuid) "
                    "AND t.tenant_id = CAST(:tid AS uuid)"
                ),
                {"m": str(mid), "tid": str(tid)},
            )
        ).all()
    return sorted((str(r[0]), str(r[1]), Decimal(str(r[2]))) for r in rows)


async def _dist_audit_after(tid: uuid.UUID, mid: uuid.UUID) -> dict[str, object]:
    """The member's single dividend_distribution.post audit payload."""
    async with tenant_session(factory(), tid) as session:
        value = (
            await session.execute(
                text(
                    "SELECT after FROM audit_log "
                    "WHERE action = 'dividend_distribution.post' "
                    "AND after->>'member_id' = :m"
                ),
                {"m": str(mid)},
            )
        ).scalar_one()
    assert isinstance(value, dict)
    return value


# ---------------------------------------------------------------------------
# FM2 + FM1: deposit_account routing next to a NULL-preference member
# ---------------------------------------------------------------------------


def test_deposit_account_routing_and_null_preference_identity() -> None:
    """FM2: the whole 636.00 payout of the 'deposit_account' member is
    retained in their deposit account (both credit legs, exact
    Decimals, DR expense classification preserved -> balanced legs).
    FM1: the NULL-preference member in the SAME run keeps the as-built
    behaviour byte-for-byte — identical legs, identical balance
    effect, identical audit key set (no routing keys). Falsifiable:
    route NULL members anywhere (or default 'deposit_account' members
    to shares) and the leg or balance asserts fail.

    Declared totals (hand-computed): eligible 2, dividend 1,200.00,
    rebate 36.00, payout 1,236.00 — routing must not change a single
    declared or posted figure, only the member-side credit account."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        m_null = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Null Preference"
        )
        m_dep = await _member_with_both_histories(
            tid, name="Deposit Router", preference="deposit_account"
        )
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 2
        assert record.total_dividend == Decimal("1200.00")
        assert record.total_rebate == Decimal("36.00")
        assert record.total_payout == Decimal("1236.00")
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 2
        assert result.payout_total == Decimal("1236.00")
        assert result.status is DeclarationStatus.DISTRIBUTED

        # FM2 — the routed member: deposits 7,300.00 -> 7,936.00
        # (hand-computed: 7,300 plus the 636.00 total); shares stay
        # exactly 12,000.00 (nothing capitalised).
        assert await _balance(tid, m_dep, table="deposit_accounts") == Decimal("7936.00")
        assert await _balance(tid, m_dep, table="share_accounts") == Decimal("12000.00")
        assert await _dv_legs(tid, m_dep) == [
            ("expense.dividends", "debit", Decimal("600.00")),
            ("expense.rebates", "debit", Decimal("36.00")),
            ("member.deposits", "credit", Decimal("36.00")),
            ("member.deposits", "credit", Decimal("600.00")),
        ]
        dep_audit = await _dist_audit_after(tid, m_dep)
        assert dep_audit["dividend_payout"] == "deposit_account"
        assert dep_audit["applied_routing"] == "deposit_account"

        # FM1 — the NULL member: byte-for-byte the as-built path.
        # Shares 12,000.00 -> 12,600.00; the audit payload carries
        # EXACTLY the batch-8 key set (routing keys ABSENT).
        assert await _balance(tid, m_null, table="share_accounts") == Decimal("12600.00")
        assert await _dv_legs(tid, m_null) == [
            ("expense.dividends", "debit", Decimal("600.00")),
            ("member.shares", "credit", Decimal("600.00")),
        ]
        null_audit = await _dist_audit_after(tid, m_null)
        assert set(null_audit.keys()) == AS_BUILT_AUDIT_KEYS

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM3: share_capital routing
# ---------------------------------------------------------------------------


def test_share_capital_routing_capitalises_the_whole_payout() -> None:
    """FM3 (hand-computed): the 'share_capital' member's rebate joins
    the dividend in share capital — shares 12,000.00 -> 12,636.00 and
    deposits stay exactly 7,300.00. Both credit legs name
    member.shares with the exact component Decimals; the DR legs keep
    their expense split (the declared totals conserve). Falsifiable:
    leave the rebate on the deposit side and the 12,636.00 and leg
    asserts fail."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        m_share = await _member_with_both_histories(
            tid, name="Share Router", preference="share_capital"
        )
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.total_payout == Decimal("636.00")
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 1
        assert result.status is DeclarationStatus.DISTRIBUTED

        assert await _balance(tid, m_share, table="share_accounts") == Decimal("12636.00")
        assert await _balance(tid, m_share, table="deposit_accounts") == Decimal("7300.00")
        assert await _dv_legs(tid, m_share) == [
            ("expense.dividends", "debit", Decimal("600.00")),
            ("expense.rebates", "debit", Decimal("36.00")),
            ("member.shares", "credit", Decimal("36.00")),
            ("member.shares", "credit", Decimal("600.00")),
        ]
        audit = await _dist_audit_after(tid, m_share)
        assert audit["dividend_payout"] == "share_capital"
        assert audit["applied_routing"] == "share_capital"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM4: unimplementable external channels — honest fallback, never faked
# ---------------------------------------------------------------------------


def test_external_channel_tokens_fall_back_honestly_and_are_never_faked() -> None:
    """FM4: 'mpesa' and 'bank' have NO implementable routing in the
    current ledger model (M-Pesa integration is #10, unbuilt; no bank
    disbursement integration exists). The recorded authorization
    forbids faking them: each member's money takes the as-built
    default path (dividend capitalised, rebate to deposits), NO cash
    account appears on any DV leg, and the preference token is
    recorded VERBATIM in the audit row with applied_routing 'default'.
    Falsifiable: post a cash leg (faking an external payment) and the
    zero-cash-legs count fails; drop the verbatim token and the audit
    asserts fail."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        m_mpesa = await _member_with_both_histories(
            tid, name="Mpesa Preference", preference="mpesa"
        )
        m_bank = await _member_with_both_histories(tid, name="Bank Preference", preference="bank")
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.total_payout == Decimal("1272.00")  # 2 x 636.00
        await _approve(tid, record.id)
        result = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert result.claimed == 2
        assert result.status is DeclarationStatus.DISTRIBUTED

        for mid in (m_mpesa, m_bank):
            # The as-built default split, exactly (hand-computed:
            # shares 12,000 grow by the 600.00 dividend; deposits
            # 7,300 grow by the 36.00 rebate).
            assert await _balance(tid, mid, table="share_accounts") == Decimal("12600.00")
            assert await _balance(tid, mid, table="deposit_accounts") == Decimal("7336.00")
            assert await _dv_legs(tid, mid) == [
                ("expense.dividends", "debit", Decimal("600.00")),
                ("expense.rebates", "debit", Decimal("36.00")),
                ("member.deposits", "credit", Decimal("36.00")),
                ("member.shares", "credit", Decimal("600.00")),
            ]
        # Never faked: zero cash-account legs across ALL DV postings.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM ledger_entries le "
                "JOIN transactions t ON t.id = le.transaction_id "
                "WHERE t.type = 'dividend_posting' AND le.account LIKE 'cash.%'",
            )
            == 0
        )
        mpesa_audit = await _dist_audit_after(tid, m_mpesa)
        assert mpesa_audit["dividend_payout"] == "mpesa"  # verbatim
        assert mpesa_audit["applied_routing"] == "default"
        bank_audit = await _dist_audit_after(tid, m_bank)
        assert bank_audit["dividend_payout"] == "bank"  # verbatim
        assert bank_audit["applied_routing"] == "default"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM5: idempotent re-run with routed members
# ---------------------------------------------------------------------------


def test_rerun_with_routed_members_posts_nothing_new() -> None:
    """FM5: with routed members already paid, a re-run (while the
    declaration is still 'approved' because one NULL member was SKIP
    LOCKED-skipped) scans ONLY the unpaid remainder — the anti-join
    excludes the routed claims without re-posting or re-crediting.
    Proven by side-effect ROW COUNTS (claims, DV transactions, DV
    ledger legs, audit rows) and exact balances, never return values.
    Falsifiable: break the claim key for routed members and the counts
    double."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        m_dep = await _member_with_both_histories(
            tid, name="Deposit Router", preference="deposit_account"
        )
        m_share = await _member_with_both_histories(
            tid, name="Share Router", preference="share_capital"
        )
        m_null = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Null Holdout"
        )
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 3
        await _approve(tid, record.id)

        # Hold the NULL member's row lock so run 1 leaves the
        # declaration 'approved' (the test_dividends holdout pattern).
        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        await session.execute(
            text(
                "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(m_null), "tid": str(tid)},
        )
        try:
            first = await distribute_dividend(_scope(tid), tid, None, record.id)
        finally:
            await holder.__aexit__(None, None, None)
        assert first.claimed == 2  # both routed members paid
        assert first.status is DeclarationStatus.APPROVED

        def counts_sql() -> tuple[str, str, str]:
            return (
                "SELECT count(*) FROM dividend_distributions",
                "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'",
                "SELECT count(*) FROM ledger_entries le "
                "JOIN transactions t ON t.id = le.transaction_id "
                "WHERE t.type = 'dividend_posting'",
            )

        claims_sql, txns_sql, legs_sql = counts_sql()
        assert await count(tid, claims_sql) == 2
        assert await count(tid, txns_sql) == 2
        legs_after_first = await count(tid, legs_sql)

        second = await distribute_dividend(_scope(tid), tid, None, record.id)
        # The anti-join scanned ONLY the unpaid NULL member (rule 8).
        assert second.scanned == 1
        assert second.claimed == 1
        assert second.status is DeclarationStatus.DISTRIBUTED

        # Routed members: not re-claimed, not re-posted, not
        # re-credited — counts grew by exactly the holdout's rows.
        assert await count(tid, claims_sql) == 3
        assert await count(tid, txns_sql) == 3
        assert await count(tid, legs_sql) == legs_after_first + 2  # holdout: 1 DR + 1 CR
        assert await _balance(tid, m_dep, table="deposit_accounts") == Decimal("7936.00")
        assert await _balance(tid, m_dep, table="share_accounts") == Decimal("12000.00")
        assert await _balance(tid, m_share, table="share_accounts") == Decimal("12636.00")
        assert await _balance(tid, m_share, table="deposit_accounts") == Decimal("7300.00")
        assert (
            await count(
                tid, "SELECT count(*) FROM audit_log WHERE action = 'dividend_distribution.post'"
            )
            == 3
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# FM6: mid-run preference flip — the TOCTOU snapshot pin
# ---------------------------------------------------------------------------


def test_mid_run_preference_flip_never_splits_the_payout() -> None:
    """FM6: the preference is read in the SAME single statement that
    takes the member's FOR UPDATE row lock, atomically with the claim
    and the posting — so a member flipping their preference mid-run is
    paid EXACTLY ONCE, per the preference in effect at claim time
    under the lock, never split across destinations. And once the run
    has completed, a further flip moves nothing: the declaration is
    terminal and re-distribution is refused.

    Hand-computed: the flipper holds 12,000.00 shares all 365 days ->
    dividend 600.00, rebate 0.00. Flipped to 'deposit_account' before
    their claim: the whole 600.00 lands in their (empty) deposit
    account; shares stay 12,000.00. Falsifiable: read the preference
    outside the locking scan (e.g. once at run start, before the
    flip) and the deposit destination asserts fail."""

    async def run() -> None:
        tid, admin_id, _ = await seed_actor()
        await _configure_dividends(tid, admin_id)
        m_flip = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Mid Run Flipper"
        )
        m_other = await _member_with_share_history(
            tid, amount="12000.00", on=FY.start, name="Steady Member"
        )
        record = await declare_dividend(_scope(tid), tid, admin_id, today=TODAY)
        assert record.eligible_members == 2
        await _approve(tid, record.id)

        # Run 1 with the flipper's row locked elsewhere: they are SKIP
        # LOCKED-skipped (NULL preference at this point), the other
        # member is paid, the run stays incomplete — the documented
        # mid-run window.
        holder = tenant_session(factory(), tid)
        session = await holder.__aenter__()
        await session.execute(
            text(
                "SELECT id FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(m_flip), "tid": str(tid)},
        )
        try:
            first = await distribute_dividend(_scope(tid), tid, None, record.id)
        finally:
            await holder.__aexit__(None, None, None)
        assert first.claimed == 1
        assert first.pending_members == 1
        assert first.status is DeclarationStatus.APPROVED

        # The mid-run flip.
        await _set_preference(tid, m_flip, "deposit_account")

        second = await distribute_dividend(_scope(tid), tid, None, record.id)
        assert second.claimed == 1
        assert second.status is DeclarationStatus.DISTRIBUTED

        # EXACTLY ONE posting, following EXACTLY ONE routing — the one
        # read under the row lock at claim time. Never split.
        assert (
            await count(
                tid,
                "SELECT count(*) FROM transactions "
                "WHERE type = 'dividend_posting' AND member_id = CAST(:m AS uuid)",
                m=str(m_flip),
            )
            == 1
        )
        assert await _dv_legs(tid, m_flip) == [
            ("expense.dividends", "debit", Decimal("600.00")),
            ("member.deposits", "credit", Decimal("600.00")),
        ]
        assert await _balance(tid, m_flip, table="deposit_accounts") == Decimal("600.00")
        assert await _balance(tid, m_flip, table="share_accounts") == Decimal("12000.00")
        flip_audit = await _dist_audit_after(tid, m_flip)
        assert flip_audit["dividend_payout"] == "deposit_account"
        assert flip_audit["applied_routing"] == "deposit_account"
        # The steady NULL member kept the as-built path in run 1.
        assert await _balance(tid, m_other, table="share_accounts") == Decimal("12600.00")

        # Flipping AFTER completion moves nothing: the declaration is
        # terminal ('distributed'); re-distribution is refused and the
        # side-effect counts stay put.
        await _set_preference(tid, m_flip, "share_capital")
        with pytest.raises(ConflictError, match="only approved declarations"):
            await distribute_dividend(_scope(tid), tid, None, record.id)
        assert (
            await count(tid, "SELECT count(*) FROM transactions WHERE type = 'dividend_posting'")
            == 2
        )
        assert await _balance(tid, m_flip, table="deposit_accounts") == Decimal("600.00")

    asyncio.run(run())
