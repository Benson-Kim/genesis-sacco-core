"""Shared request-parameter guards for API routers (the house gates).

Single source of truth (DRY) for checks that several routers repeat:
the cash-channel restriction on money-moving endpoints and the
no-future-dates rule for as_of style parameters.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from genesis.domain.ledger import Channel
from genesis.errors import InvalidInputError, UnprocessableError

#: Channels on which money physically moves. Accrual/internal postings
#: are made by jobs through their dedicated services, never by routes.
CASH_CHANNELS = frozenset({Channel.MPESA, Channel.BANK})

#: Channels whose money movement happens OUTSIDE this system:
#: an M-Pesa transfer or a bank deposit each mint an external
#: receipt reference that reconciliation needs verbatim. Today this
#: set equals CASH_CHANNELS (both teller channels are external money
#: movements); it is a separate code-owned vocabulary because the
#: requirement is "external channels", not "teller channels" — a
#: future physical-cash channel would join CASH_CHANNELS but not this
#: set.
EXTERNAL_CHANNELS = frozenset({Channel.MPESA, Channel.BANK})

#: Code-owned PER-CHANNEL shapes for operator-entered external
#: references: the dedupe is only as strong as its
#: canonical form, so each channel pins the exact shape its receipts
#: actually carry. Validated AFTER trim + uppercase normalization, so
#: the partial UNIQUE (tenant_id, channel, external_ref) dedupe (0043)
#: is case- and whitespace-insensitive by construction.
#:
#: * M-Pesa confirmation codes: EXACTLY 10 uppercase alphanumerics
#:   (e.g. SGH3KLM9QT) — no separator characters exist in the format.
#: * Bank slip/EFT refs: 2..40 chars, alphanumeric with the common
#:   separator characters, alphanumeric at both edges.
#:
#: Every EXTERNAL_CHANNELS member MUST have a shape here (totality is
#: pinned by a test leg in tests/test_external_ref.py).
EXTERNAL_REF_SHAPES: dict[Channel, re.Pattern[str]] = {
    Channel.MPESA: re.compile(r"^[A-Z0-9]{10}$"),
    Channel.BANK: re.compile(r"^[A-Z0-9][A-Z0-9 /\-]{0,38}[A-Z0-9]$"),
}


def require_cash_channel(channel: Channel) -> Channel:
    if channel not in CASH_CHANNELS:
        raise InvalidInputError(f"channel must be one of: mpesa, bank; got '{channel.value}'")
    return channel


def require_external_ref(channel: Channel, external_ref: str | None) -> str | None:
    """Mandatory external reference on EXTERNAL-channel postings.

    Missing or malformed references are a sanitized 422 — the category
    only, the submitted value is NEVER echoed (least disclosure, gate
    1.6; the stored/audited row carries it for entitled staff).
    Non-external channels return None: system postings keep their
    system-generated txn_ref and never carry an external reference.
    """
    if channel not in EXTERNAL_CHANNELS:
        return None
    if external_ref is None or external_ref.strip() == "":
        raise UnprocessableError("external channel postings require a transaction reference")
    normalized = external_ref.strip().upper()
    if EXTERNAL_REF_SHAPES[channel].fullmatch(normalized) is None:
        raise UnprocessableError("invalid external transaction reference format")
    return normalized


def resolve_as_of(as_of: date | None) -> date:
    """Default to today and reject future dates (least disclosure).

    A future as_of would let a caller inflate days-past-due, fabricate
    interest that is not yet due, or accrue a period that has not ended.
    Backdating stays allowed for reconciliation and idempotent re-runs.
    """
    today = datetime.now(UTC).date()
    if as_of is None:
        return today
    if as_of > today:
        raise InvalidInputError("as_of must not be in the future")
    return as_of
