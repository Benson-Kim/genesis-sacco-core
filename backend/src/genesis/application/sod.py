"""Shared maker-checker segregation-of-duties guard (reuse-first).

ONE copy of the checker-separation check, hoisted from
application/corrections.py when the share-transfer maker-checker
workflow became its second consumer —
the two workflows must never diverge on the rule:

  * the checker must be a DIFFERENT user than the maker (the
    collusion-resistant DB CHECK — 0031 ck_repayment_adjustments_sod /
    0040 ck_share_transfers_sod — is the backstop behind this guard);
  * the checker must not hold an assurance function (the B2 principle: the Auditor reviews the
  workflow trail and therefore can never act inside it). The role name is resolved SERVER-SIDE
    from the actor's role_id (users -> roles join), never from the JWT
    or a client-supplied flag;
  * an actor the users table cannot vouch for checks NOTHING (fail
    closed — the enforce_authority_band posture).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.domain.rbac import ASSURANCE_ROLES
from genesis.errors import ConflictError, ForbiddenError

__all__ = ["require_distinct_non_assurance_checker"]


async def require_distinct_non_assurance_checker(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    maker_id: uuid.UUID,
    *,
    subject: str,
    subject_plural: str,
) -> None:
    """Refuse the CHECKER action unless the actor is a distinct,
    non-assurance, tenant-vouched principal.

    ``subject`` / ``subject_plural`` name the workflow in the least-
    disclosure error messages (e.g. "an adjustment" / "adjustments").
    """
    if actor_id == maker_id:
        raise ConflictError(f"the maker of {subject} cannot check it (segregation of duties)")
    row = (
        await session.execute(
            text(
                "SELECT r.name FROM users u "
                "JOIN roles r ON r.id = u.role_id AND r.tenant_id = CAST(:tid AS uuid) "
                "WHERE u.id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid)"
            ),
            {"uid": str(actor_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        # Fail closed (the enforce_authority_band posture): an actor
        # the users table cannot vouch for checks nothing.
        raise ForbiddenError(f"actor {actor_id} has no resolvable role for the checker action")
    if str(row[0]) in ASSURANCE_ROLES:
        raise ConflictError(
            f"assurance roles are excluded from checking {subject_plural} (audit independence)"
        )
