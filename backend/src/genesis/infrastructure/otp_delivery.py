"""OTP delivery adapters behind the application port (hexagonal seam).

Implements `genesis.application.otp_delivery.OtpDeliveryPort` for the
outbox dispatcher. Layout:

* `OtpChannelProvider` — the SMS/email PROVIDER adapter interface: one
  adapter per channel. A real gateway (e.g. an SMS aggregator or an
  SMTP relay) lands as a new class here implementing `send_otp`, is
  registered in `default_otp_delivery()`, and nothing above this module
  changes — the seam is the point.
* `LoggingOtpChannelProvider` — the first concrete adapter: records the
  dispatch in the log with a MASKED destination and NEVER the code
  (gate 1.6). A deliberate no-op transport so the whole path
  (enqueue → outbox claim → routing → channel adapter) is exercised end
  to end before any real gateway exists.
* `OtpDelivery` — the port implementation: routes by channel, idempotent
  by event id (the outbox redelivery contract), fails LOUDLY on an
  unconfigured channel so the event retries/dead-letters instead of
  silently vanishing.
* `OtpRoutingProvider` — a NotificationProvider wrapper for the outbox
  dispatcher: peels `*.otp_requested` events onto the port and hands
  every other event (and legacy OTP events enqueued before the payload
  carried routing fields) to the wrapped fallback provider unchanged.

Request handlers never import this module — delivery rides the outbox
worker exclusively (MASTER_PROMPT 1.2; enforced by import-linter).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from genesis.application.otp_delivery import (
    OTP_CHANNEL_EMAIL,
    OTP_CHANNEL_SMS,
    OtpDeliveryPort,
)
from genesis.infrastructure.providers import NotificationProvider

logger = logging.getLogger("genesis.infrastructure.otp_delivery")

#: Outbox event types that carry an issued OTP (staff and member paths —
#: both enqueue in application code with channel/destination/code).
OTP_EVENT_TYPES = frozenset({"auth.otp_requested", "member_auth.otp_requested"})


def mask_destination(destination: str) -> str:
    """Redact a phone/email for log lines (no PII in logs — gate 1.6)."""
    if len(destination) <= 4:
        return "***"
    return f"{destination[:2]}***{destination[-2:]}"


class OtpChannelProvider(NotificationProvider.__class__.__mro_entries__ and object):  # type: ignore[misc]
    """Placeholder — replaced below; see module docstring."""


class LoggingOtpChannelProvider:
    """First concrete channel adapter: logs the dispatch, sends nothing.

    The destination is masked and the CODE NEVER APPEARS in any log
    line (gate 1.6) — this adapter proves the seam end to end and is
    swapped for a real SMS/email gateway in default_otp_delivery().
    """

    def __init__(self, channel: str) -> None:
        self.channel = channel

    async def send_otp(self, *, destination: str, code: str) -> None:
        logger.info("otp dispatched via %s to %s", self.channel, mask_destination(destination))


class OtpDelivery:
    """Concrete OtpDeliveryPort: per-channel routing, idempotent by event id."""

    def __init__(self, providers: Mapping[str, OtpChannelProvider]) -> None:
        self._providers = dict(providers)
        self._seen: set[str] = set()

    async def deliver_otp(
        self, *, event_id: str, channel: str, destination: str, code: str
    ) -> None:
        provider = self._providers.get(channel)
        if provider is None:
            # Loud failure: the outbox retries with backoff and
            # dead-letters after MAX_ATTEMPTS — never a silent drop.
            msg = f"no OTP delivery provider configured for channel '{channel}'"
            raise RuntimeError(msg)
        if event_id in self._seen:
            return
        await provider.send_otp(destination=destination, code=code)
        self._seen.add(event_id)


class OtpRoutingProvider:
    """Outbox NotificationProvider that routes OTP events onto the port.

    Non-OTP events — and OTP events enqueued BEFORE the payload carried
    channel/destination routing fields (in-flight rows during a deploy)
    — go to the wrapped fallback provider exactly as before.
    """

    def __init__(self, delivery: OtpDeliveryPort, fallback: NotificationProvider) -> None:
        self._delivery = delivery
        self._fallback = fallback
        self.channel = f"otp+{fallback.channel}"

    async def send(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if event_type in OTP_EVENT_TYPES:
            channel = payload.get("channel")
            destination = payload.get("destination")
            code = payload.get("code")
            if isinstance(channel, str) and isinstance(destination, str) and isinstance(code, str):
                await self._delivery.deliver_otp(
                    event_id=event_id, channel=channel, destination=destination, code=code
                )
                return
        await self._fallback.send(event_id, event_type, payload)


def default_otp_delivery() -> OtpDelivery:
    """Composition root for OTP delivery.

    A real provider drops in HERE: implement the channel adapter above
    and register it for its channel — no application or API change.
    """
    return OtpDelivery(
        {
            OTP_CHANNEL_SMS: LoggingOtpChannelProvider(OTP_CHANNEL_SMS),
            OTP_CHANNEL_EMAIL: LoggingOtpChannelProvider(OTP_CHANNEL_EMAIL),
        }
    )
