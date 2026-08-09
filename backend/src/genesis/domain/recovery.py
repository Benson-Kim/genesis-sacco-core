"""Recovery-case domain: status machine (concurrency safety; addendum).

Zero I/O. Exactly one transition function is the gatekeeper for every
case status change (the members/loans/users 1.4 pattern): the only
writer is application/recovery, and every path — staff disposition or
job auto-close — must pass through:func:`transition`. Illegal moves
raise.

Assignment and notes are NON-STATE mutations (review addendum): they never
touch ``status`` and therefore never pass through this map — assignment
is an optimistic-locked column write, notes are append-only rows.

Issue (the dispositions) widens the machine — the
0033 migration regenerates the mirroring DB CHECKs in the same MR per
0026's named-contract discipline:

  * LIVE postures beyond ``open``: ``irrecoverable_pending_write_off``
    (the officer concluded recovery is impossible; the committee write-off has not yet landed on the
    loan — / linkage) and
    ``disputed`` (the member contests the arrears; the case pauses
    without pretending to be workable). Both are entered from ``open``
    and can return to ``open`` — a posture change is an auditable
    two-step through the workable state, so a DIRECT pause-to-pause
    move is illegal.
  * ``closed_restructured`` — terminal: the loan was restructured and
    the case's premise no longer holds (staff-attested).
  * Every terminal state stays terminal: a cured-then-re-defaulting
    loan gets a NEW case (history preserved, review addendum), never a
    reopened one. Loan facts close from ANY live posture: the arrears
    job's auto-close pass moves a paused case to ``closed_cured`` /
    ``closed_written_off`` when the loan cures or is written off —
    money talks; the pause only removes the case from the workable
    worklist.

WHICH targets a STAFF disposition may set is application policy
(``STAFF_DISPOSITION_TARGETS`` — cure/write-off closes are job-only,
loan-fact outcomes); the legality of the move itself has exactly one
owner:func:`transition`.
"""

from __future__ import annotations

import enum


class RecoveryCaseStatus(enum.StrEnum):
    OPEN = "open"
    IRRECOVERABLE_PENDING_WRITE_OFF = "irrecoverable_pending_write_off"
    DISPUTED = "disputed"
    CLOSED_CURED = "closed_cured"
    CLOSED_WRITTEN_OFF = "closed_written_off"
    CLOSED_RESTRUCTURED = "closed_restructured"


#: The non-terminal (LIVE) statuses: exactly the rows the one-live-case
#: partial UNIQUE covers and the auto-close pass scans (0033 mirrors
#: this set in the DB — the named-contract discipline).
LIVE_STATUSES: frozenset[RecoveryCaseStatus] = frozenset(
    {
        RecoveryCaseStatus.OPEN,
        RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF,
        RecoveryCaseStatus.DISPUTED,
    }
)

#: Terminal outcomes; ``closed_at`` is set iff the case is here (0033
#: ck_recovery_cases_closed_at) and the ONE post-closure outcome note
#:  is accepted only here.
TERMINAL_STATUSES: frozenset[RecoveryCaseStatus] = frozenset(
    {
        RecoveryCaseStatus.CLOSED_CURED,
        RecoveryCaseStatus.CLOSED_WRITTEN_OFF,
        RecoveryCaseStatus.CLOSED_RESTRUCTURED,
    }
)

#: The two live PAUSE postures: a staff disposition
#: INTO either must carry a contemporaneous reason, captured into the
#: audit payload by the application service — workflow metadata only,
#: never a money parameter.
PAUSE_STATUSES: frozenset[RecoveryCaseStatus] = frozenset(
    {
        RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF,
        RecoveryCaseStatus.DISPUTED,
    }
)

#: Targets a STAFF disposition may request (application policy):
#: the two pauses, the resume, and the restructure close.
#: ``closed_cured`` / ``closed_written_off`` are deliberately absent —
#: they are LOAN-FACT outcomes only the arrears job's auto-close pass
#: produces; a staff-declared cure or write-off is a rejected design.
STAFF_DISPOSITION_TARGETS: frozenset[RecoveryCaseStatus] = frozenset(
    {
        RecoveryCaseStatus.OPEN,
        RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF,
        RecoveryCaseStatus.DISPUTED,
        RecoveryCaseStatus.CLOSED_RESTRUCTURED,
    }
)


class InvalidRecoveryTransitionError(Exception):
    """Raised when a case status transition is not allowed (concurrency safety)."""


_ALLOWED: dict[RecoveryCaseStatus, frozenset[RecoveryCaseStatus]] = {
    RecoveryCaseStatus.OPEN: frozenset(
        {
            RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF,
            RecoveryCaseStatus.DISPUTED,
            RecoveryCaseStatus.CLOSED_CURED,
            RecoveryCaseStatus.CLOSED_WRITTEN_OFF,
            RecoveryCaseStatus.CLOSED_RESTRUCTURED,
        }
    ),
    RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF: frozenset(
        {
            RecoveryCaseStatus.OPEN,
            RecoveryCaseStatus.CLOSED_CURED,
            RecoveryCaseStatus.CLOSED_WRITTEN_OFF,
            RecoveryCaseStatus.CLOSED_RESTRUCTURED,
        }
    ),
    RecoveryCaseStatus.DISPUTED: frozenset(
        {
            RecoveryCaseStatus.OPEN,
            RecoveryCaseStatus.CLOSED_CURED,
            RecoveryCaseStatus.CLOSED_WRITTEN_OFF,
            RecoveryCaseStatus.CLOSED_RESTRUCTURED,
        }
    ),
    RecoveryCaseStatus.CLOSED_CURED: frozenset(),
    RecoveryCaseStatus.CLOSED_WRITTEN_OFF: frozenset(),
    RecoveryCaseStatus.CLOSED_RESTRUCTURED: frozenset(),
}


def transition(current: RecoveryCaseStatus, target: RecoveryCaseStatus) -> RecoveryCaseStatus:
    """Validate a case transition; raise on any illegal move (concurrency safety).

    Self-transitions are illegal (the precedent): a no-op close
    would bump versions and emit audit/outbox noise without a state
    change — idempotent re-runs are achieved by scanning only LIVE
    cases whose loan facts warrant a close, never by tolerating
    closed->closed writes.
    """
    if target not in _ALLOWED[current]:
        raise InvalidRecoveryTransitionError(f"{current.value} -> {target.value}")
    return target
