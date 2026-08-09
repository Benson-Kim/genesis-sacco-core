"""System-user domain: status machine (concurrency safety).

Zero I/O. Mirrors the prototype Access-control Users tab statuses:
Active <-> Suspended, enforced by a single transition function so every
writer (there is exactly one: application/users.change_user_status)
shares the same allowed-transitions map. Illegal moves raise.
"""

from __future__ import annotations

import enum


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class InvalidUserStatusTransitionError(Exception):
    """Raised when a user status transition is not allowed (concurrency safety)."""


_ALLOWED: dict[UserStatus, frozenset[UserStatus]] = {
    UserStatus.ACTIVE: frozenset({UserStatus.SUSPENDED}),
    UserStatus.SUSPENDED: frozenset({UserStatus.ACTIVE}),
}


def transition(current: UserStatus, target: UserStatus) -> UserStatus:
    """Validate a status transition; raise on any illegal move (concurrency safety).

    Self-transitions (active->active, suspended->suspended) are illegal:
    a no-op status write would still bump the version and emit audit and
    outbox noise without a state change.
    """
    if target not in _ALLOWED[current]:
        raise InvalidUserStatusTransitionError(f"cannot move user from {current} to {target}")
    return target
