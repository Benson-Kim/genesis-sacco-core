"""Member credential-link administration (scopes 1/3).

The member_credentials row IS member identity: authentication and
every member-principal authorization resolve through it (the link, never any email, is
authoritative). So link mutations are the
takeover surface, and makes them narrow:

  * there is NO self-service mutation path — creating or revoking a
    link is a staff mutation under the dedicated member_identity
    module (the narrow P4 matrix: System Admin / Branch Manager only),
    audited in-transaction like every other mutation (data integrity);
  * re-pointing an email at another member is NEVER one write: it is
    revoke + create — two audited mutations, each notifying the
    member (detection control, the lesson);
  * the active-email claim is ATOMIC: INSERT... ON
    CONFLICT on the 0035 partial UNIQUE
    uq_member_credentials_email_active, checked by rowcount — two
    concurrent links of one email land exactly one row;
  * revocation is recorded, never deleted: identity history stays
    forensic (the audit_log posture).

Lock posture (lock-order.md — no new lock-graph edges): every link
mutation locks the MEMBER row FOR UPDATE first (chain ROOT of
member -> accounts -> loans, the recovery-case-open single-node
pattern) and performs only plain writes on member_credentials under
it. The member lock serialises link mutations per member (the
one-active-credential-per-member claim is decided under it; the 0035
partial UNIQUE is the DB backstop) and conflicts with a terminal exit
at T1. Nothing below T1 is ever acquired.

Every query carries an explicit bound tenant_id predicate on top of
forced RLS (least disclosure); every value is a bound parameter
(rule 6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.domain.members import MemberStatus
from genesis.errors import ConflictError, NotFoundError

#: The atomic active-email claim. The conflict
#: target is the 0035 partial UNIQUE uq_member_credentials_email_active
#: — a concurrent claim of the same active email lands exactly one row
#: and the loser reads rowcount 0. Module-level so tests pin the SQL.
CLAIM_EMAIL_SQL = (
    "INSERT INTO member_credentials (id, tenant_id, member_id, email) "
    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:mid AS uuid), :email) "
    "ON CONFLICT (tenant_id, email) WHERE status = 'active' DO NOTHING"
)


@dataclass(frozen=True)
class CredentialRecord:
    id: uuid.UUID
    member_id: uuid.UUID
    email: str
    status: str
    version: int


def _to_record(row: Any) -> CredentialRecord:
    return CredentialRecord(
        id=uuid.UUID(str(row[0])),
        member_id=uuid.UUID(str(row[1])),
        email=str(row[2]),
        status=str(row[3]),
        version=int(row[4]),
    )


async def _lock_member(session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID) -> str:
    """Member row FOR UPDATE — chain ROOT (lock-order.md): the

    serialisation point for every link mutation on this member."""
    row = (
        await session.execute(
            text(
                "SELECT status FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"member {member_id} not found")
    return str(row[0])


async def create_credential(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    member_id: uuid.UUID,
    email: str,
) -> CredentialRecord:
    """Link a login email to a member (audited admin mutation).

    Decided under the member row lock; the active-email claim is the
    atomic ON CONFLICT insert checked by rowcount. Least disclosure
    (least disclosure): the email-taken refusal never names the member holding
    it.
    """
    status = await _lock_member(session, tenant_id, member_id)
    if MemberStatus(status) is MemberStatus.EXITED:
        # Terminal: a settled member has no live relationship to
        # authenticate against (the member_auth login-status rule).
        raise ConflictError(f"member {member_id} is exited: no credential can be linked")
    existing = (
        await session.execute(
            text(
                "SELECT 1 FROM member_credentials "
                "WHERE member_id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) AND status = 'active'"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if existing is not None:
        # One active credential per member (decided under the member
        # lock; uq_member_credentials_member_active is the backstop).
        # Re-linking is revoke + create, never a second live link.
        raise ConflictError(f"member {member_id} already has an active credential")
    credential_id = uuid.uuid4()
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(CLAIM_EMAIL_SQL),
            {
                "id": str(credential_id),
                "tid": str(tenant_id),
                "mid": str(member_id),
                "email": email,
            },
        ),
    )
    if result.rowcount != 1:
        # The atomic claim lost: the email is already someone's
        # ACTIVE credential. Least disclosure: never say whose.
        raise ConflictError("this email is already linked to an active credential")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_credential.create",
        entity="member_credentials",
        entity_id=str(credential_id),
        after={"member_id": str(member_id), "email": email, "status": "active"},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_identity.credential_created",
        payload={
            "credential_id": str(credential_id),
            "member_id": str(member_id),
            "notify_member_id": str(member_id),
        },
    )
    return CredentialRecord(
        id=credential_id, member_id=member_id, email=email, status="active", version=1
    )


async def revoke_credential(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    credential_id: uuid.UUID,
    *,
    version: int,
) -> CredentialRecord:
    """Revoke a credential link (audited admin mutation).

    Lock order: unlocked probe to learn the member, member row FOR
    UPDATE (chain ROOT — matching create_credential, so revoke and
    create can never interleave on one member), then the optimistic
    single-statement revocation. The row is kept forever (forensic
    history); every live use — OTP verify, refresh rotation, the
    per-request member gate, in-transaction consent/release re-checks —
    dies at its next live_credential_by_id call.
    """
    probe = (
        await session.execute(
            text(
                "SELECT member_id, email FROM member_credentials "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(credential_id), "tid": str(tenant_id)},
        )
    ).first()
    if probe is None:
        raise NotFoundError(f"credential {credential_id} not found")
    member_id = uuid.UUID(str(probe[0]))
    email = str(probe[1])
    await _lock_member(session, tenant_id, member_id)
    updated = (
        await session.execute(
            text(
                "UPDATE member_credentials SET status = 'revoked', revoked_at = now(), "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver AND status = 'active' RETURNING version"
            ),
            {"id": str(credential_id), "tid": str(tenant_id), "ver": version},
        )
    ).first()
    if updated is None:
        raise ConflictError(
            f"stale version {version} for credential {credential_id}, or it is already revoked"
        )
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="member_credential.revoke",
        entity="member_credentials",
        entity_id=str(credential_id),
        before={"member_id": str(member_id), "email": email, "status": "active"},
        after={"status": "revoked"},
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_identity.credential_revoked",
        payload={
            "credential_id": str(credential_id),
            "member_id": str(member_id),
            "notify_member_id": str(member_id),
        },
    )
    return CredentialRecord(
        id=credential_id,
        member_id=member_id,
        email=email,
        status="revoked",
        version=int(updated[0]),
    )


async def list_member_credentials(
    session: AsyncSession, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[CredentialRecord]:
    """Full link history for one member (member_identity:view) —
    bounded by the one-active rule, so no pagination is needed; served
    by idx_member_credentials_member (0035)."""
    member = (
        await session.execute(
            text(
                "SELECT 1 FROM members WHERE id = CAST(:m AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if member is None:
        raise NotFoundError(f"member {member_id} not found")
    rows = (
        await session.execute(
            text(
                "SELECT id, member_id, email, status, version FROM member_credentials "
                "WHERE member_id = CAST(:m AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "ORDER BY created_at DESC, id"
            ),
            {"m": str(member_id), "tid": str(tenant_id)},
        )
    ).all()
    return [_to_record(row) for row in rows]
