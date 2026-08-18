"""OTP delivery seam (review item 1b/1c): port, adapters, outbox routing.

The seam that retires the dev-mode OTP display: issued OTPs ride the
transactional outbox and the dispatcher hands them to the application
port `OtpDeliveryPort` through `OtpRoutingProvider`; a real SMS/email
gateway drops in as an `OtpChannelProvider` in
`infrastructure.otp_delivery.default_otp_delivery()` with no
application or API change. Falsifiable both ways:

- routing: an `*.otp_requested` event with routing fields reaches the
  port (and ONLY the port); every other event — including legacy OTP
  events enqueued before payloads carried routing fields — reaches the
  wrapped fallback provider unchanged;
- the code the port receives is the REAL challenge (it verifies at
  /auth/otp/verify) with the right channel per identifier kind (email
  vs Kenya MSISDN);
- delivery is AT-LEAST-ONCE: dedupe by event id is bounded per-process
  memory only (within-cycle redelivery is deduped; the cap evicts the
  oldest — both directions tested), an unconfigured channel fails
  LOUDLY (retry/dead-letter, never a silent drop), and the logging
  adapter NEVER logs the code or the full destination (gate 1.6);
- #20 hardening: a dead-lettered OTP event is REDACTED in the same
  transaction (no plaintext code, no full destination at rest; non-OTP
  dead letters untouched), and an already-expired challenge is dropped
  with an explicit audit line — marked processed, never retried,
  never silent.

The unit legs are pure (no DB) and run on every pipeline.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.otp_delivery import (
    OTP_CHANNEL_EMAIL,
    OTP_CHANNEL_SMS,
    OtpDeliveryPort,
)
from genesis.application.outbox import enqueue_event
from genesis.infrastructure.otp_delivery import (
    OTP_EVENT_TYPES,
    SEEN_CAP,
    LoggingOtpChannelProvider,
    OtpDelivery,
    OtpRoutingProvider,
    default_otp_delivery,
    mask_destination,
)
from genesis.infrastructure.outbox_worker import MAX_ATTEMPTS, dispatch_due
from genesis.infrastructure.providers import StubProvider
from genesis.infrastructure.tenancy import tenant_session

_requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


class RecordingPort:
    """OtpDeliveryPort double: records every delivery it receives."""

    def __init__(self) -> None:
        self.deliveries: list[dict[str, str]] = []

    async def deliver_otp(
        self, *, event_id: str, channel: str, destination: str, code: str
    ) -> None:
        self.deliveries.append(
            {"event_id": event_id, "channel": channel, "destination": destination, "code": code}
        )


class RecordingChannelProvider:
    """OtpChannelProvider double for the routing/idempotency legs."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.sent: list[tuple[str, str]] = []

    async def send_otp(self, *, destination: str, code: str) -> None:
        self.sent.append((destination, code))


# ---------------------------------------------------------------------------
# Pure unit legs (no DB).
# ---------------------------------------------------------------------------


def test_port_contract_is_satisfied_by_the_concrete_delivery() -> None:
    """The seam is REAL: the concrete implementation satisfies the port."""
    port: OtpDeliveryPort = default_otp_delivery()
    assert port is not None


def test_mask_destination_redacts_the_middle() -> None:
    assert mask_destination("member@example.com") == "me***om"
    assert mask_destination("+254712345678") == "+2***78"
    # Too short to keep anything: fully redacted, never sliced negative.
    assert mask_destination("a@b") == "***"
    assert "mber@examp" not in mask_destination("member@example.com")


def test_routing_provider_peels_otp_events_onto_the_port() -> None:
    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        for event_type in sorted(OTP_EVENT_TYPES):
            await provider.send(
                f"evt-{event_type}",
                event_type,
                {"code": "123456", "channel": "email", "destination": "m@x.com"},
            )
        assert [d["event_id"] for d in port.deliveries] == [
            "evt-auth.otp_requested",
            "evt-member_auth.otp_requested",
        ]
        # The port took them; the fallback saw NOTHING.
        assert fallback.delivered_event_ids == []

    asyncio.run(run())


def test_routing_provider_hands_non_otp_events_to_the_fallback() -> None:
    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        await provider.send("evt-1", "loans.approved", {"loan_id": "x"})
        assert port.deliveries == []
        assert fallback.delivered_event_ids == ["evt-1"]

    asyncio.run(run())


def test_routing_provider_falls_back_for_legacy_otp_payloads() -> None:
    """OTP events enqueued BEFORE payloads carried routing fields (rows
    in flight during a deploy) keep the previous stub behavior. LEGACY
    means NO routing fields at all — the fallback direction of the
    malformed-vs-legacy rule (#18); the raise direction is below."""

    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        await provider.send("evt-legacy", "auth.otp_requested", {"user_id": "u", "code": "123456"})
        assert port.deliveries == []
        assert fallback.delivered_event_ids == ["evt-legacy"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("null channel", {"channel": None, "destination": "m@x.com", "code": "123456"}),
        ("non-str destination", {"channel": "email", "destination": 7, "code": "123456"}),
        ("missing code", {"channel": "email", "destination": "m@x.com"}),
        ("partial fields", {"destination": "m@x.com", "code": "123456"}),
        (
            "bad expires_at type",
            {"channel": "email", "destination": "m@x.com", "code": "123456", "expires_at": 99},
        ),
        (
            "unparseable expires_at",
            {
                "channel": "email",
                "destination": "m@x.com",
                "code": "123456",
                "expires_at": "not-a-date",
            },
        ),
    ],
)
def test_malformed_routing_payload_raises_instead_of_stub_delivering(
    case: str, payload: dict[str, object]
) -> None:
    """#18: SOME routing fields present but malformed = an enqueue BUG.

    The old behavior silently degraded to the stub — a future bug
    writing channel: null would be “delivered” by the stub and marked
    done. Now it raises so the outbox retries/dead-letters (and the
    dead letter is redacted, #20). Falsifiable both ways: the legacy
    test above proves no-fields still falls back; this proves
    some-fields-malformed reaches NEITHER the port NOR the fallback.
    The error message carries field TYPES only — never the code or
    destination values (no PII in last_error at rest)."""

    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        with pytest.raises(ValueError, match="malformed OTP routing payload") as excinfo:
            await provider.send("evt-bug", "auth.otp_requested", dict(payload))
        assert port.deliveries == []
        assert fallback.delivered_event_ids == []
        # Types only, never values: the message must not leak PII.
        assert "123456" not in str(excinfo.value)
        assert "m@x.com" not in str(excinfo.value)

    asyncio.run(run())


def test_delivery_dedupes_by_event_id_within_the_process() -> None:
    """Within-process (i.e. within-cycle) redelivery never double-sends.

    HONESTY: this is the whole dedupe guarantee — the cron one-shot
    rebuilds OtpDelivery every tick, so delivery is at-least-once
    ACROSS ticks (#20 tracks a durable marker)."""

    async def run() -> None:
        sms = RecordingChannelProvider(OTP_CHANNEL_SMS)
        delivery = OtpDelivery({OTP_CHANNEL_SMS: sms})
        for _ in range(3):
            await delivery.deliver_otp(
                event_id="evt-1", channel="sms", destination="+254712345678", code="123456"
            )
        assert sms.sent == [("+254712345678", "123456")]

    asyncio.run(run())


def test_seen_memory_is_bounded_and_evicts_oldest_first() -> None:
    """#20(c): `_seen` must not grow unboundedly in a long-lived worker.

    Falsifiable both ways: after SEEN_CAP + 1 distinct deliveries the
    memory holds exactly SEEN_CAP ids; the EVICTED (oldest) id re-sends
    on redelivery (at-least-once, documented) while a still-tracked id
    stays deduped. Restore the uncapped set and the bound assertion
    fails; evict newest-first and the dedupe assertion fails."""

    async def run() -> None:
        sms = RecordingChannelProvider(OTP_CHANNEL_SMS)
        delivery = OtpDelivery({OTP_CHANNEL_SMS: sms})
        total = SEEN_CAP + 1
        for i in range(total):
            await delivery.deliver_otp(
                event_id=f"evt-{i}", channel="sms", destination="+254712345678", code="123456"
            )
        assert len(sms.sent) == total
        assert len(delivery._seen) == SEEN_CAP
        assert len(delivery._seen_order) == SEEN_CAP
        # evt-0 was evicted (oldest first): redelivery re-sends.
        await delivery.deliver_otp(
            event_id="evt-0", channel="sms", destination="+254712345678", code="123456"
        )
        assert len(sms.sent) == total + 1
        # The newest id is still tracked: redelivery stays deduped.
        await delivery.deliver_otp(
            event_id=f"evt-{total - 1}",
            channel="sms",
            destination="+254712345678",
            code="123456",
        )
        assert len(sms.sent) == total + 1

    asyncio.run(run())


def test_expired_otp_is_dropped_with_audit_not_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#20(b): an expired challenge's code is worthless — the routing
    provider returns WITHOUT delivering (the dispatcher then marks the
    event processed) and logs an explicit expired-skip audit line with
    a masked destination, never the code. Falsifiable: deliver anyway
    and the port records a delivery; drop the log line and the audit
    assertion fails."""

    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with caplog.at_level(logging.INFO):
            await provider.send(
                "evt-expired",
                "auth.otp_requested",
                {
                    "code": "123456",
                    "channel": "email",
                    "destination": "member@example.com",
                    "expires_at": expired,
                },
            )
        assert port.deliveries == []
        assert fallback.delivered_event_ids == []
        assert "otp expired before delivery" in caplog.text
        assert "evt-expired" in caplog.text
        # The audit line leaks neither the code nor the destination.
        assert "123456" not in caplog.text
        assert "member@example.com" not in caplog.text

    asyncio.run(run())


def test_unexpired_otp_still_delivers(caplog: pytest.LogCaptureFixture) -> None:
    """The other direction: a future expires_at must NOT be skipped —
    guards against an inverted or always-true expiry check."""

    async def run() -> None:
        port = RecordingPort()
        provider = OtpRoutingProvider(port, StubProvider(channel="stub"))
        future = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
        with caplog.at_level(logging.INFO):
            await provider.send(
                "evt-live",
                "auth.otp_requested",
                {
                    "code": "123456",
                    "channel": "email",
                    "destination": "member@example.com",
                    "expires_at": future,
                },
            )
        assert [d["event_id"] for d in port.deliveries] == ["evt-live"]
        assert "otp expired before delivery" not in caplog.text

    asyncio.run(run())


def test_unconfigured_channel_fails_loudly() -> None:
    """Raising makes the outbox retry/dead-letter — never a silent drop."""

    async def run() -> None:
        delivery = OtpDelivery({OTP_CHANNEL_EMAIL: RecordingChannelProvider(OTP_CHANNEL_EMAIL)})
        with pytest.raises(RuntimeError, match="no OTP delivery provider"):
            await delivery.deliver_otp(
                event_id="evt-1", channel="sms", destination="+254712345678", code="123456"
            )

    asyncio.run(run())


def test_logging_adapter_never_logs_the_code_or_full_destination(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gate 1.6: the first concrete adapter leaks neither the six-digit
    code nor the unmasked destination into any log line."""

    async def run() -> None:
        adapter = LoggingOtpChannelProvider(OTP_CHANNEL_EMAIL)
        with caplog.at_level(logging.DEBUG):
            await adapter.send_otp(destination="member@example.com", code="654321")

    asyncio.run(run())
    assert "654321" not in caplog.text
    assert "member@example.com" not in caplog.text
    assert re.search(r"\b\d{6}\b", caplog.text) is None
    # But the dispatch itself IS observable (no silent failures).
    assert "otp dispatched via email" in caplog.text


# ---------------------------------------------------------------------------
# End-to-end against a real Postgres: request → outbox → port, and the
# delivered code is the REAL challenge.
# ---------------------------------------------------------------------------


async def _drain_otp_to_port(tid: uuid.UUID) -> RecordingPort:
    port = RecordingPort()
    provider = OtpRoutingProvider(port, StubProvider(channel="stub"))
    await dispatch_due(factory(), tid, provider)
    return port


@_requires_db
def test_email_identifier_delivers_a_verifiable_code_on_the_email_channel() -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
            assert res.status_code == 202
            port = await _drain_otp_to_port(tid)
            assert len(port.deliveries) == 1
            delivered = port.deliveries[0]
            assert delivered["channel"] == OTP_CHANNEL_EMAIL
            assert delivered["destination"] == email
            # The delivered code is the REAL challenge: it verifies.
            verified = await client.post(
                "/auth/otp/verify",
                json={"email": email, "code": delivered["code"]},
                headers=headers,
            )
            assert verified.status_code == 200
            assert "access_token" in verified.json()

    asyncio.run(run())


@_requires_db
def test_phone_identifier_routes_to_the_sms_channel_with_e164_destination() -> None:
    async def run() -> None:
        email = unique_email()
        e164 = f"+2547{int.from_bytes(os.urandom(4), 'big') % 10**8:08d}"
        tid, _ = await seed_user(email, phone=e164)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            # Local 07XX spelling in, normalized E.164 destination out —
            # the classifier/normalizer pair, one rule.
            local = f"0{e164[4:]}"
            res = await client.post(
                "/auth/otp/request", json={"identifier": local}, headers=headers
            )
            assert res.status_code == 202
        port = await _drain_otp_to_port(tid)
        assert len(port.deliveries) == 1
        delivered = port.deliveries[0]
        assert delivered["channel"] == OTP_CHANNEL_SMS
        assert delivered["destination"] == e164
        assert re.fullmatch(r"\d{6}", delivered["code"]) is not None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# #20 hardening against a real Postgres: dead-letter redaction and
# TTL-aware processing of expired OTP events.
# ---------------------------------------------------------------------------


class _AlwaysFailingProvider:
    channel = "always-failing"

    async def send(self, event_id: str, event_type: str, payload: dict[str, object]) -> None:
        raise RuntimeError("provider outage (simulated)")


async def _reset_due(tid: uuid.UUID) -> None:
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text("UPDATE outbox_events SET next_attempt_at = now() WHERE status = 'pending'")
        )


async def _dead_letter(tid: uuid.UUID, event_type: str, payload: dict[str, object]) -> uuid.UUID:
    """Enqueue one event and drive it to dead-letter through the real path."""
    async with tenant_session(factory(), tid) as session:
        event_id = await enqueue_event(session, tid, event_type=event_type, payload=payload)
    for _ in range(MAX_ATTEMPTS):
        await _reset_due(tid)
        await dispatch_due(factory(), tid, _AlwaysFailingProvider())
    return event_id


async def _load_row(tid: uuid.UUID, event_id: uuid.UUID) -> tuple[str, dict[str, object]]:
    async with tenant_session(factory(), tid) as session:
        row = (
            await session.execute(
                text("SELECT status, payload FROM outbox_events WHERE id = CAST(:id AS uuid)"),
                {"id": str(event_id)},
            )
        ).one()
    return str(row[0]), dict(row[1])


@_requires_db
def test_dead_lettered_otp_event_is_redacted_at_rest() -> None:
    """#20(a): a dead-lettered OTP row must NOT retain the plaintext
    code or the full destination — dead rows live forever. The channel,
    challenge id and a masked destination stay for diagnosis.
    Falsifiable: skip the redaction UPDATE and the code/destination
    assertions fail on the raw payload text."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        destination = "+254712345678"
        event_id = await _dead_letter(
            tid,
            "auth.otp_requested",
            {
                "user_id": "u-1",
                "challenge_id": "c-1",
                "code": "123456",
                "channel": "sms",
                "destination": destination,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
            },
        )
        status, payload = await _load_row(tid, event_id)
        assert status == "dead"
        assert "code" not in payload
        assert "destination" not in payload
        assert payload["redacted"] is True
        # Diagnosis survives: channel, challenge id, masked destination.
        assert payload["channel"] == "sms"
        assert payload["challenge_id"] == "c-1"
        assert payload["destination_masked"] == mask_destination(destination)
        # Sweep the raw row text: no 6-digit code, no full destination.
        raw = json.dumps(payload)
        assert re.search(r"\b\d{6}\b", raw) is None
        assert destination not in raw

    asyncio.run(run())


@_requires_db
def test_dead_lettered_non_otp_event_payload_is_untouched() -> None:
    """The other direction: redaction applies ONLY to OTP events — a
    non-OTP dead letter keeps its full payload for debugging."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        payload = {"loan_id": "l-1", "amount": "1000"}
        event_id = await _dead_letter(tid, "loans.approved", payload)
        status, stored = await _load_row(tid, event_id)
        assert status == "dead"
        assert stored == payload

    asyncio.run(run())


@_requires_db
def test_expired_otp_event_is_marked_processed_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#20(b) end to end: an expired OTP event dispatches to 'processed'
    (status dispatched, attempts spent ONCE) with the audit line — no
    port delivery, no retry churn. Falsifiable: raise instead of
    returning and the status stays pending with attempts climbing."""

    async def run() -> None:
        tid, _ = await seed_user(unique_email())
        async with tenant_session(factory(), tid) as session:
            event_id = await enqueue_event(
                session,
                tid,
                event_type="auth.otp_requested",
                payload={
                    "user_id": "u-1",
                    "challenge_id": "c-1",
                    "code": "123456",
                    "channel": "sms",
                    "destination": "+254712345678",
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
            )
        port = RecordingPort()
        provider = OtpRoutingProvider(port, StubProvider(channel="stub"))
        with caplog.at_level(logging.INFO):
            await dispatch_due(factory(), tid, provider)
        status, _ = await _load_row(tid, event_id)
        assert status == "dispatched"
        assert port.deliveries == []
        assert "otp expired before delivery" in caplog.text

    asyncio.run(run())
