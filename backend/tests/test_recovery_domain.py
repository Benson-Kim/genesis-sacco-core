"""P13.16 domain tests: recovery-case state machine (addendum A1) and
the NPL-by-label pin (addendum A6 / v1.1 rule 2). No database needed.
"""

from __future__ import annotations

from itertools import product

import pytest

from genesis.domain.lending import NPL_CLASSES, LoanClass, classify
from genesis.domain.recovery import (
    LIVE_STATUSES,
    STAFF_DISPOSITION_TARGETS,
    TERMINAL_STATUSES,
    InvalidRecoveryTransitionError,
    RecoveryCaseStatus,
    transition,
)

# Issue #23 (N2): the FULL six-state legal set, every pair enumerated.
#   open  -> the two pauses, the restructure close, and the two
#            job-only loan-fact closes;
#   pause -> open (resume), or any of the three closes (loan facts end
#            a pause; restructure settles one) — a DIRECT pause-to-
#            pause move is deliberately absent (posture changes are
#            auditable two-steps through the workable state);
#   terminal -> nothing (no reopen, no cross-terminal, no self-move).
_ALLOWED_PAIRS = {
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF),
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.DISPUTED),
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.CLOSED_CURED),
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.CLOSED_WRITTEN_OFF),
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.CLOSED_RESTRUCTURED),
    (RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF, RecoveryCaseStatus.OPEN),
    (RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF, RecoveryCaseStatus.CLOSED_CURED),
    (RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF, RecoveryCaseStatus.CLOSED_WRITTEN_OFF),
    (RecoveryCaseStatus.IRRECOVERABLE_PENDING_WRITE_OFF, RecoveryCaseStatus.CLOSED_RESTRUCTURED),
    (RecoveryCaseStatus.DISPUTED, RecoveryCaseStatus.OPEN),
    (RecoveryCaseStatus.DISPUTED, RecoveryCaseStatus.CLOSED_CURED),
    (RecoveryCaseStatus.DISPUTED, RecoveryCaseStatus.CLOSED_WRITTEN_OFF),
    (RecoveryCaseStatus.DISPUTED, RecoveryCaseStatus.CLOSED_RESTRUCTURED),
}


def test_full_matrix_transitions() -> None:
    """Addendum A1 / issue #23: EVERY (current, target) pair of the
    six-state machine is asserted — 36 pairs, 13 legal (enumerated
    above), 23 illegal (including every self-transition, any reopen of
    a terminal state, every cross-terminal move, and the deliberate
    pause-to-pause exclusions irrecoverable<->disputed).
    Falsifiable: widening the allowed map fails the illegal branch;
    removing an allowed edge fails the legal branch."""
    pairs = list(product(RecoveryCaseStatus, RecoveryCaseStatus))
    assert len(pairs) == 36
    for current, target in pairs:
        if (current, target) in _ALLOWED_PAIRS:
            assert transition(current, target) is target
        else:
            with pytest.raises(InvalidRecoveryTransitionError):
                transition(current, target)


def test_status_partitions_and_staff_targets_pinned() -> None:
    """The live/terminal partition and the staff-settable target set
    are pinned to the matrix (issue #23):

    * LIVE + TERMINAL partition the status set exactly (no status is
      both, none is neither) — the 0033 CHECKs and partial indexes
      mirror these sets in the DB, so a drift here is a stranded CHECK.
    * Terminals allow no outgoing move; every live status allows one.
    * STAFF_DISPOSITION_TARGETS excludes exactly the two job-only
      loan-fact closes (closed_cured / closed_written_off) — a staff-
      declared cure or write-off must be unrepresentable via the
      disposition route (FM2's guard set). Falsifiable: adding either
      close to the staff set fails here."""
    assert set(RecoveryCaseStatus) == LIVE_STATUSES | TERMINAL_STATUSES
    assert not (LIVE_STATUSES & TERMINAL_STATUSES)
    for status in TERMINAL_STATUSES:
        for target in RecoveryCaseStatus:
            with pytest.raises(InvalidRecoveryTransitionError):
                transition(status, target)
    assert (
        set(RecoveryCaseStatus)
        - {
            RecoveryCaseStatus.CLOSED_CURED,
            RecoveryCaseStatus.CLOSED_WRITTEN_OFF,
        }
        == STAFF_DISPOSITION_TARGETS
    )


def test_npl_classes_pinned_to_classify() -> None:
    """NPL_CLASSES (by label) can never drift from classify() (by dpd).

    Samples cover every threshold boundary of the 30/90/180/360 ladder,
    hand-computed: 0/30 normal, 31/90 watch, 91/180 substandard (NPL),
    181/360 doubtful (NPL), 361 loss (NPL). Every LoanClass member must
    appear, so a new class cannot ship unpinned."""
    samples = [0, 30, 31, 90, 91, 180, 181, 360, 361]
    seen: set[LoanClass] = set()
    for dpd in samples:
        result = classify(dpd)
        seen.add(result.label)
        assert result.is_npl == (result.label in NPL_CLASSES), dpd
    assert seen == set(LoanClass)
