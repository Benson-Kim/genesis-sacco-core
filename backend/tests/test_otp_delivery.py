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
- delivery is idempotent by event id (outbox redelivery contract), an
  unconfigured channel fails LOUDLY (retry/dead-letter, never a silent
  drop), and the logging adapter NEVER logs the code or the full
  destination (gate 1.6).

The unit legs are pure (no DB) and run on every pipeline.
"""

import asyncio
import logging
import os
import re
import uuid

import pytest

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.otp_delivery import (
    OTP_CHANNEL_EMAIL,
    OTP_CHANNEL_SMS,
    OtpDeliveryPort,
)
from genesis.infrastructure.otp_delivery import (
    OTP_EVENT_TYPES,
    LoggingOtpChannelProvider,
    OtpDelivery,
    OtpRoutingProvider,
    default_otp_delivery,
    mask_destination,
)
from genesis.infrastructure.outbox_worker import dispatch_due
from genesis.infrastructure.providers import StubProvider

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
    in flight during a deploy) keep the previous stub behavior."""

    async def run() -> None:
        port = RecordingPort()
        fallback = StubProvider(channel="stub")
        provider = OtpRoutingProvider(port, fallback)
        await provider.send("evt-legacy", "auth.otp_requested", {"user_id": "u", "code": "123456"})
        assert port.deliveries == []
        assert fallback.delivered_event_ids == ["evt-legacy"]

    asyncio.run(run())


def test_delivery_is_idempotent_by_event_id() -> None:
    """Outbox redelivery of the same event never double-sends."""

    async def run() -> None:
        sms = RecordingChannelProvider(OTP_CHANNEL_SMS)
        delivery = OtpDelivery({OTP_CHANNEL_SMS: sms})
        for _ in range(3):
            await delivery.deliver_otp(
                event_id="evt-1", channel="sms", destination="+254712345678", code="123456"
            )
        assert sms.sent == [("+254712345678", "123456")]

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
