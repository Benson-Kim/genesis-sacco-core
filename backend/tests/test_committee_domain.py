"""Unit tests for genesis.domain.committee - pure, no I/O (P9)."""

import pytest

from genesis.domain.committee import COMMITTEE_QUORUM, Decision, decide


def test_below_quorum_is_undecided() -> None:
    assert decide(COMMITTEE_QUORUM - 1, COMMITTEE_QUORUM - 1) is None
    assert decide(0, 0) is None


def test_approvals_at_quorum_approve() -> None:
    assert decide(COMMITTEE_QUORUM, 0) is Decision.APPROVED
    assert decide(COMMITTEE_QUORUM + 3, 1) is Decision.APPROVED


def test_rejections_at_quorum_reject() -> None:
    assert decide(0, COMMITTEE_QUORUM) is Decision.REJECTED
    assert decide(1, COMMITTEE_QUORUM + 2) is Decision.REJECTED


def test_rejection_wins_ambiguous_tally() -> None:
    """Prudential conservatism: never approve on an ambiguous count."""
    assert decide(COMMITTEE_QUORUM, COMMITTEE_QUORUM) is Decision.REJECTED


def test_negative_counts_raise() -> None:
    with pytest.raises(ValueError, match="negative"):
        decide(-1, 0)
    with pytest.raises(ValueError, match="negative"):
        decide(0, -1)


def test_non_positive_quorum_raises() -> None:
    with pytest.raises(ValueError, match="quorum"):
        decide(1, 1, quorum=0)
