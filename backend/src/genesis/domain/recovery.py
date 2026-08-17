"""Recovery-case domain: status machine (P13.16, gate 1.4; addendum A1).

Zero I/O. Exactly one transition function is the gatekeeper for every
case status change (the members/loans/users 1.4 pattern): the only
writer is application/recovery, and every close path — cure or
write-off — must pass through :func:`transition`. Illegal moves raise.

Assignment and notes are NON-STATE mutations (addendum A1): they never
touch ``status`` and therefore never pass through this map — assignment
is an optimistic-locked column write, notes are append-only rows.

Both closed states are terminal: a cured-then-re-defaulting loan gets a
NEW case (history preserved, addendum A6), never a reopened one.
"""

from __future__ import annotations

import enum


class RecoveryCaseStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED_CURED = "closed_cured"
    CLOSED_WRITTEN_OFF = "closed_written_off"


class InvalidRecoveryTransitionError(Exception):
    """Raised when a case status transition is not allowed (gate 1.4)."""


_ALLOWED: dict[RecoveryCaseStatus, frozenset[RecoveryCaseStatus]] = {
    RecoveryCaseStatus.OPEN: frozenset(
        {RecoveryCaseStatus.CLOSED_CURED, RecoveryCaseStatus.CLOSED_WRITTEN_OFF}
    ),
    RecoveryCaseStatus.CLOSED_CURED: frozenset(),
    RecoveryCaseStatus.CLOSED_WRITTEN_OFF: frozenset(),
}


def transition(current: RecoveryCaseStatus, target: RecoveryCaseStatus) -> RecoveryCaseStatus:
    """Validate a case transition; raise on any illegal move (gate 1.4).

    Self-transitions are illegal (the P13.5 precedent): a no-op close
    would bump versions and emit audit/outbox noise without a state
    change — idempotent re-runs are achieved by scanning only OPEN
    cases, never by tolerating closed->closed writes.
    """
    if target not in _ALLOWED[current]:
        raise InvalidRecoveryTransitionError(f"{current.value} -> {target.value}")
    return target
