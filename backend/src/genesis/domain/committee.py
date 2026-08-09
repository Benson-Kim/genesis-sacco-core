"""Committee voting rules (P9). Pure logic; persistence lives outside.

A decision is reached when either side accumulates the quorum of votes.
One vote per committee member is enforced by a DB UNIQUE constraint;
this module only decides outcomes from tallies (concurrency safety).
"""

from __future__ import annotations

import enum

#: Votes on one side needed to decide an application.
COMMITTEE_QUORUM = 2


class Vote(enum.StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Decision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


def decide(approvals: int, rejections: int, *, quorum: int = COMMITTEE_QUORUM) -> Decision | None:
    """Return the decision once a side reaches quorum, else None.

    Rejection wins a tally where both sides reach quorum in the same
    read: prudential conservatism - never approve on an ambiguous count.
    """
    if approvals < 0 or rejections < 0:
        raise ValueError("vote counts must not be negative")
    if quorum <= 0:
        raise ValueError("quorum must be positive")
    if rejections >= quorum:
        return Decision.REJECTED
    if approvals >= quorum:
        return Decision.APPROVED
    return None
