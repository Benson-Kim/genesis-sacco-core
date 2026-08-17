"""P13.16 domain tests: recovery-case state machine (addendum A1) and
the NPL-by-label pin (addendum A6 / v1.1 rule 2). No database needed.
"""

from __future__ import annotations

from itertools import product

import pytest

from genesis.domain.lending import NPL_CLASSES, LoanClass, classify
from genesis.domain.recovery import (
    InvalidRecoveryTransitionError,
    RecoveryCaseStatus,
    transition,
)

_ALLOWED_PAIRS = {
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.CLOSED_CURED),
    (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.CLOSED_WRITTEN_OFF),
}


def test_full_matrix_transitions() -> None:
    """Addendum A1: EVERY (current, target) pair is asserted — the two
    documented closes succeed, all other moves (including every
    self-transition and any reopen of a terminal state) raise.
    Falsifiable: widening the allowed map fails the illegal branch;
    removing an allowed edge fails the legal branch."""
    for current, target in product(RecoveryCaseStatus, RecoveryCaseStatus):
        if (current, target) in _ALLOWED_PAIRS:
            assert transition(current, target) is target
        else:
            with pytest.raises(InvalidRecoveryTransitionError):
                transition(current, target)


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
