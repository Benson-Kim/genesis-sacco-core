"""Notification provider adapters behind interfaces (reliability).

Real SMS/email/push wiring lands in; every adapter MUST be idempotent
by event id so outbox redeliveries never double-send. Request handlers are
forbidden from importing this module (import-linter contract).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("genesis.infrastructure.providers")


class NotificationProvider(Protocol):
    """Delivery adapter contract; implementations are idempotent by event id."""

    channel: str

    async def send(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Deliver one event. Raising triggers retry with backoff."""
        ...


class StubProvider:
    """Logs deliveries (never payload contents); used until wires real providers."""

    def __init__(self, channel: str = "stub") -> None:
        self.channel = channel
        self.delivered_event_ids: list[str] = []
        self._seen: set[str] = set()

    async def send(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        self.delivered_event_ids.append(event_id)
        logger.info("delivered event %s type %s via %s", event_id, event_type, self.channel)
