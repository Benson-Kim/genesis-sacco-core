"""OTP delivery port: the hexagonal seam a real SMS/email provider plugs into.

The staging decision recorded in docs/technical/mochahost-deployment.md
§0 exists ONLY because OTP delivery was unbuilt — testers read the code
from the response behind the dev-only display flag. This port makes
delivery a first-class application concern with a real seam: issued
OTPs already ride the transactional outbox (`auth.otp_requested` /
`member_auth.otp_requested`, enqueued in the SAME transaction as the
challenge row — reliability), and the dispatcher hands them to an
implementation of this port. Concrete adapters live in
`genesis.infrastructure.otp_delivery` (the layering contract: the
application layer never imports infrastructure); dropping in a real
provider is an infrastructure-only change.

Pure declarations only — no I/O, no framework imports.
"""

from __future__ import annotations

from typing import Protocol

#: Delivery channels. The sign-in identifier classifier
#: (application.auth.resolve_signin_identifier) decides which one an
#: issued OTP travels on: a Kenya mobile number goes out as SMS, an
#: email address as email.
OTP_CHANNEL_EMAIL = "email"
OTP_CHANNEL_SMS = "sms"


class OtpDeliveryPort(Protocol):
    """Delivery seam for issued OTP codes.

    Delivery is AT-LEAST-ONCE: the outbox redelivers on any crash
    between the provider call and the dispatched-mark, and the cron
    one-shot deployment rebuilds the adapter every tick, so any
    dedupe-by-event-id an implementation keeps is per-process,
    best-effort memory only (a durable delivery marker is the open
    question in #20). That is the ACCEPTED semantic for OTP: a re-sent
    code is an annoyance, an unsent one blocks sign-in, and the code
    expires after OTP_TTL_SECONDS regardless.

    Implementations MUST NEVER log or persist the code or the full
    destination (gate 1.6). Raising signals the outbox dispatcher to
    retry with backoff and dead-letter after MAX_ATTEMPTS — a delivery
    outage is never a silent drop.
    """

    async def deliver_otp(
        self, *, event_id: str, channel: str, destination: str, code: str
    ) -> None:
        """Deliver one issued OTP to its destination on the given channel."""
        ...
