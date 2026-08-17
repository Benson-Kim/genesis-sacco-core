"""Pure domain tests for the user status machine (P13.5, gate 1.4)."""

import pytest

from genesis.domain.users import (
    InvalidUserStatusTransitionError,
    UserStatus,
    transition,
)


def test_allowed_transitions() -> None:
    assert transition(UserStatus.ACTIVE, UserStatus.SUSPENDED) is UserStatus.SUSPENDED
    assert transition(UserStatus.SUSPENDED, UserStatus.ACTIVE) is UserStatus.ACTIVE


def test_self_transitions_are_illegal() -> None:
    """No-op status writes must be rejected, not silently versioned."""
    with pytest.raises(InvalidUserStatusTransitionError):
        transition(UserStatus.ACTIVE, UserStatus.ACTIVE)
    with pytest.raises(InvalidUserStatusTransitionError):
        transition(UserStatus.SUSPENDED, UserStatus.SUSPENDED)
