"""#35 item 6 — mandatory external transaction reference (migration 0043).

Every channel the teller boundary accepts (mpesa, bank) is an EXTERNAL
money movement minting an external receipt reference (M-Pesa
confirmation code / bank slip ref). The API boundary now REQUIRES the
reference on those channels (sanitized 422 — the value is never
echoed); storage is the expand-only nullable transactions.external_ref
with the partial UNIQUE (tenant_id, channel, external_ref) dedupe
claimed ATOMICALLY at the posting INSERT (v1.1 rule 5). System/job
postings (accrual, internal — and any direct service call without a
ref) legitimately store NULL: history is never backfilled.

Falsifiable (§4, hand-computed oracles in comments):
- FM-ER1 missing/malformed ref => 422 and ZERO transaction rows, zero
  balance change (side-effect row counts, never return values alone);
- FM-ER2 the stored value is the UPPERCASE normalization; the list
  read serves it verbatim (drop the normalization and the
  case-evasion leg FM-ER4 fails first);
- FM-ER3 duplicate (tenant, channel, ref) => 409 with EXACTLY ONE
  ledger row and ONE balance credit (drop uq_txns_external_ref and
  the second posting lands 201, failing this leg);
- FM-ER4 case AND whitespace variants collide (dedupe cannot be
  evaded by casing or padding — review R3);
- FM-ER5 the same ref on the OTHER channel and in ANOTHER tenant both
  post fine (the dedupe key is per tenant per channel, exactly as
  declared);
- FM-ER6 refless system postings still work and read back
  external_ref null (legacy/NULL tolerance);
- FM-ER7 the shape is PER-CHANNEL (review R3): a bank-shaped ref
  (separator characters / off-length) is refused on mpesa at the
  boundary, and the pure-unit divergence leg pins the same string as
  bank-valid + mpesa-invalid — collapse EXTERNAL_REF_SHAPES back to
  one regex and both fail; the shape-map totality over
  EXTERNAL_CHANNELS is pinned alongside (a channel without a shape
  would KeyError at the boundary).
"""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory
from export_helpers import seed_actor, seed_member
from genesis.application import transactions as txn_service
from genesis.domain.ledger import Channel
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _txn_rows(tid: uuid.UUID, member_id: uuid.UUID) -> int:
    async with tenant_session(factory(), tid) as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM transactions "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = CAST(:m AS uuid)"
                ),
                {"tid": str(tid), "m": str(member_id)},
            )
        ).scalar_one()
    return int(count)


async def _balance(tid: uuid.UUID, member_id: uuid.UUID) -> Decimal:
    async with tenant_session(factory(), tid) as session:
        bal = (
            await session.execute(
                text(
                    "SELECT balance FROM deposit_accounts "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND member_id = CAST(:m AS uuid)"
                ),
                {"tid": str(tid), "m": str(member_id)},
            )
        ).scalar_one()
    return Decimal(str(bal))


def test_missing_or_malformed_ref_is_sanitized_422_with_zero_side_effects() -> None:
    """FM-ER1: the refusal happens BEFORE any money moves — zero
    transaction rows, balance unchanged — and the response never
    echoes the submitted value (least disclosure, gate 1.6)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, deposit="0")
        hostile = "<script>alert(1)</script>"
        async with api_client() as client:
            for body in (
                {"amount": "100.00", "channel": "mpesa"},  # missing
                {"amount": "100.00", "channel": "mpesa", "external_ref": "   "},  # blank
                {"amount": "100.00", "channel": "bank", "external_ref": hostile},  # shape
                {"amount": "100.00", "channel": "bank", "external_ref": "X"},  # too short
                # FM-ER7 (review R3): mpesa's shape is EXACTLY 10
                # alphanumerics — off-length and separator-carrying
                # values that the BANK shape would accept are refused.
                {"amount": "100.00", "channel": "mpesa", "external_ref": "SGH3KLM9Q"},  # 9
                {"amount": "100.00", "channel": "mpesa", "external_ref": "SGH3KLM9QTX"},  # 11
                # bank-shaped (separators) — see FM-ER7:
                {"amount": "100.00", "channel": "mpesa", "external_ref": "SLIP-90/22"},
            ):
                res = await client.post(
                    f"/members/{mid}/deposits", json=body, headers=_headers(token)
                )
                assert res.status_code == 422, res.text
                # Sanitized: the submitted value is NEVER echoed.
                assert hostile not in res.text
        # Side-effect oracle: nothing landed from any attempt.
        assert await _txn_rows(tid, mid) == 0
        assert await _balance(tid, mid) == Decimal("0")

    asyncio.run(run())


def test_per_channel_shape_map_is_total_and_divergent() -> None:
    """FM-ER7 pure-unit legs (review R3).

    Totality: every EXTERNAL channel has a code-owned shape — a channel
    joining EXTERNAL_CHANNELS without a shape entry would KeyError at
    require_external_ref, and this leg names it first. Divergence:
    'SLIP-90/22' (a 10-char separator-carrying bank slip ref) is
    bank-valid and mpesa-invalid; 'SGH3KLM9QT' is valid on both (the
    formats genuinely overlap on plain 10-char alphanumerics — FM-ER5's
    cross-channel dedupe leg rides exactly that overlap). Collapse the
    shapes to one regex and the divergence pair fails.
    """
    from genesis.api.params import EXTERNAL_CHANNELS, EXTERNAL_REF_SHAPES

    assert set(EXTERNAL_REF_SHAPES) == EXTERNAL_CHANNELS
    bank, mpesa = EXTERNAL_REF_SHAPES[Channel.BANK], EXTERNAL_REF_SHAPES[Channel.MPESA]
    assert bank.fullmatch("SLIP-90/22") is not None
    assert mpesa.fullmatch("SLIP-90/22") is None
    assert bank.fullmatch("SGH3KLM9QT") is not None
    assert mpesa.fullmatch("SGH3KLM9QT") is not None


def test_ref_stored_uppercase_served_in_the_list_read_and_deduped() -> None:
    """FM-ER2 + FM-ER3 + FM-ER4: post 'sgh3klm9qt' (lowercase entry of
    a real M-Pesa-shaped code) => stored/served as 'SGH3KLM9QT';
    replaying the SAME code — any casing — is a 409 with EXACTLY ONE
    row and ONE credit (hand-computed: one 250.00 deposit => balance
    250.00, rows == 1)."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, deposit="0")
        async with api_client() as client:
            first = await client.post(
                f"/members/{mid}/deposits",
                json={"amount": "250.00", "channel": "mpesa", "external_ref": "sgh3klm9qt"},
                headers=_headers(token),
            )
            assert first.status_code == 201, first.text

            # FM-ER4: the case-AND-whitespace-variant replay collides
            # (trim + uppercase is what makes the dedupe evasion-proof
            # — drop either normalization and this 409 becomes a 201;
            # review R3: ' SGH3klm9QT ' claims the SAME slot).
            dup = await client.post(
                f"/members/{mid}/deposits",
                json={"amount": "250.00", "channel": "mpesa", "external_ref": " SGH3klm9QT "},
                headers=_headers(token),
                # A NEW idempotency intent, so only the ref dedupe can
                # refuse it (the middleware never sees a replayed key).
                # (Idempotency-Key is optional on this route; absent
                # here by design.)
            )
            assert dup.status_code == 409, dup.text
            # Least disclosure: no balance, no stored ref echoed.
            assert "250" not in dup.text
            assert "SGH3KLM9QT" not in dup.text

            # FM-ER2: the read model serves the normalized ref.
            listing = await client.get("/transactions", headers=_headers(token))
            assert listing.status_code == 200
            items = [i for i in listing.json()["items"] if i["member_id"] == str(mid)]
            assert [i["external_ref"] for i in items] == ["SGH3KLM9QT"]

        # FM-ER3 side-effect oracle: exactly ONE row, exactly ONE credit.
        assert await _txn_rows(tid, mid) == 1
        assert await _balance(tid, mid) == Decimal("250.00")

    asyncio.run(run())


def test_dedupe_is_per_tenant_per_channel_exactly_as_declared() -> None:
    """FM-ER5: the SAME reference posts fine on the other channel and
    in another tenant — the partial UNIQUE key is (tenant_id, channel,
    external_ref), nothing wider."""

    async def run() -> None:
        tid_a, _, token_a = await seed_actor()
        tid_b, _, token_b = await seed_actor()
        mid_a = await seed_member(tid_a, deposit="0")
        mid_b = await seed_member(tid_b, deposit="0")
        async with api_client() as client:
            # Valid on BOTH channels (10 plain alphanumerics — the
            # deliberate shape overlap the FM-ER7 divergence leg pins):
            # this leg probes ONLY the dedupe key's width, never shape.
            ref = "QGH7PLM2ZT"
            one = await client.post(
                f"/members/{mid_a}/deposits",
                json={"amount": "100.00", "channel": "mpesa", "external_ref": ref},
                headers=_headers(token_a),
            )
            assert one.status_code == 201, one.text
            # Same tenant, OTHER channel: allowed.
            two = await client.post(
                f"/members/{mid_a}/deposits",
                json={"amount": "100.00", "channel": "bank", "external_ref": ref},
                headers=_headers(token_a),
            )
            assert two.status_code == 201, two.text
            # OTHER tenant, same channel: allowed.
            three = await client.post(
                f"/members/{mid_b}/deposits",
                json={"amount": "100.00", "channel": "mpesa", "external_ref": ref},
                headers=_headers(token_b),
            )
            assert three.status_code == 201, three.text
        assert await _txn_rows(tid_a, mid_a) == 2
        assert await _txn_rows(tid_b, mid_b) == 1

    asyncio.run(run())


def test_withdrawal_and_share_topup_require_the_ref_too() -> None:
    """The requirement covers ALL three teller writes (they share the
    same MoneyBody + require_external_ref boundary): refusal legs with
    zero side effects, then the honest posting lands."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, deposit="500.00")
        async with api_client() as client:
            for route in (f"/members/{mid}/withdrawals", f"/members/{mid}/share-topups"):
                res = await client.post(
                    route,
                    json={"amount": "50.00", "channel": "bank"},
                    headers=_headers(token),
                )
                assert res.status_code == 422, res.text
            posted = await client.post(
                f"/members/{mid}/withdrawals",
                json={"amount": "50.00", "channel": "bank", "external_ref": "SLIP-77"},
                headers=_headers(token),
            )
            assert posted.status_code == 201, posted.text
        # Hand-computed: 500.00 - 50.00 = 450.00; exactly one posting.
        assert await _balance(tid, mid) == Decimal("450.00")
        assert await _txn_rows(tid, mid) == 1

    asyncio.run(run())


def test_refless_system_postings_still_work_and_read_back_null() -> None:
    """FM-ER6: the direct service path (jobs, seeders, legacy) posts
    without a ref; the row stores NULL and the read model serves the
    honest null — never an invented reference."""

    async def run() -> None:
        tid, _, token = await seed_actor()
        mid = await seed_member(tid, deposit="0")
        async with tenant_session(factory(), tid) as session:
            await txn_service.record_deposit(
                session, tid, None, mid, amount=Decimal("75.00"), channel=Channel.BANK
            )
        async with api_client() as client:
            listing = await client.get("/transactions", headers=_headers(token))
            assert listing.status_code == 200
            items = [i for i in listing.json()["items"] if i["member_id"] == str(mid)]
            assert len(items) == 1
            assert items[0]["external_ref"] is None
            assert "external_ref" in items[0]  # present even when null

    asyncio.run(run())
