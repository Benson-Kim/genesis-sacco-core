"""Member-facing authentication use-cases.

The MEMBER principal authenticates through the SAME P3 machinery as
staff — one `domain/otp.py` policy (6 digits, at most 5 attempts,
5-minute TTL, single-use, constant-time compare), the same
`otp_challenges` and `refresh_tokens` tables (0035 adds the XOR
`member_credential_id` principal column), the same rotating
refresh-token families with reuse revocation, and the same outbox
delivery stub — a parallel OTP implementation is a rejected MR
(reuse-first). What differs is the PRINCIPAL: challenges and refresh
families hang off a `member_credentials` link row, and issued tokens
carry the MEMBER audience so they can never satisfy a staff
RequirePermission gate.

Lock posture (docs/diagrams/lock-order.md — no new lock-graph edges):
verify locks the newest challenge row ALONE and rotation locks the
refresh-token row ALONE (both existing disjoint-subgraph single-node
nodes; annotations extended by this MR). Unlike the staff path, NO
user/member row is locked first: credential revocation (the analogue of suspension) does not write
challenge/token rows — instead the
credential's live status is re-checked at every use (OTP verify,
refresh rotation, and the per-request member gate), so a revoked link
dies at its next use without any cross-table lock ordering to defend.

Tenant predicates: unlike the staff pre-auth lookups (whose exemption
is documented in application/auth.py), every query here carries an
explicit bound tenant_id predicate on top of forced RLS — the tenant
is already known from the x-tenant-id header (least disclosure).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Deliberate reuse of the P3 internals (reuse-first): these helpers are
# package-private to application/, shared by both principals so the
# hashing, TTLs and family-revocation semantics can never diverge.
from genesis.application.auth import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    AuthFailure,
    MemberAuthContext,
    TokenPair,
    _hash_refresh_token,
    _now,
    _otp_pepper,
    _revoke_family,
    issue_member_access_token,
)
from genesis.application.outbox import enqueue_event
from genesis.domain.members import MemberStatus
from genesis.domain.otp import (
    OTP_LENGTH,
    OTP_TTL_SECONDS,
    OtpResult,
    evaluate_challenge,
    hash_code,
)


@dataclass(frozen=True)
class _LiveCredential:
    credential_id: uuid.UUID
    member_id: uuid.UUID


#: Member statuses allowed to AUTHENTICATE. Authentication is not a
#: money operation (domain/members.MEMBER_OPERATIONS governs those),
#: but EXITED is terminal: a settled member has no live relationship
#: to sign in to. Arrears/dormant members still see their own data —
#: their money-movement limits are enforced per operation.
_LOGIN_STATUSES: tuple[str, str, str] = (
    MemberStatus.ACTIVE.value,
    MemberStatus.ARREARS.value,
    MemberStatus.DORMANT.value,
)

#: Live-credential lookup by login email — THE member-login hot path,
#: served by the partial UNIQUE uq_member_credentials_email_active
#: (0035; EXPLAIN pinned in tests/test_p145_explain.py). Module-level
#: so the plan capture asserts the exact SQL. Every value is a bound
#: parameter.
LIVE_CREDENTIAL_BY_EMAIL_SQL = (
    "SELECT mc.id, mc.member_id FROM member_credentials mc "
    "JOIN members m ON m.id = mc.member_id AND m.tenant_id = mc.tenant_id "
    "WHERE mc.tenant_id = CAST(:tid AS uuid) AND mc.email = :email "
    "AND mc.status = 'active' AND m.status IN (:st0, :st1, :st2)"
)


async def _live_credential_by_email(
    session: AsyncSession, tenant_id: uuid.UUID, email: str
) -> _LiveCredential | None:
    row = (
        await session.execute(
            text(LIVE_CREDENTIAL_BY_EMAIL_SQL),
            {
                "tid": str(tenant_id),
                "email": email,
                "st0": _LOGIN_STATUSES[0],
                "st1": _LOGIN_STATUSES[1],
                "st2": _LOGIN_STATUSES[2],
            },
        )
    ).first()
    if row is None:
        return None
    return _LiveCredential(credential_id=uuid.UUID(str(row[0])), member_id=uuid.UUID(str(row[1])))


async def live_credential_by_id(
    session: AsyncSession, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> _LiveCredential | None:
    """Use-time re-check of the link: the LINK row, not any email,
    decides whether the principal is still live.

    Public on purpose (reuse-first — ONE implementation): refresh
    rotation, the per-request member gate (api/authz.py) and the
    member consent/release transactions (application/guarantees.py)
    all re-verify the link through this exact query."""
    row = (
        await session.execute(
            text(
                "SELECT mc.id, mc.member_id FROM member_credentials mc "
                "JOIN members m ON m.id = mc.member_id AND m.tenant_id = mc.tenant_id "
                "WHERE mc.id = CAST(:cid AS uuid) "
                "AND mc.tenant_id = CAST(:tid AS uuid) "
                "AND mc.status = 'active' AND m.status IN (:st0, :st1, :st2)"
            ),
            {
                "cid": str(credential_id),
                "tid": str(tenant_id),
                "st0": _LOGIN_STATUSES[0],
                "st1": _LOGIN_STATUSES[1],
                "st2": _LOGIN_STATUSES[2],
            },
        )
    ).first()
    if row is None:
        return None
    return _LiveCredential(credential_id=uuid.UUID(str(row[0])), member_id=uuid.UUID(str(row[1])))


async def request_member_otp(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> None:
    """Issue a single-use, 5-minute member OTP (the P3 policy verbatim).

    Never reveals whether a credential exists (the request_otp
    contract); delivery rides the outbox stub — the code never appears
    in a response or a log (the house gates).
    """
    credential = await _live_credential_by_email(session, tenant_id, email)
    if credential is None:
        return
    challenge_id = uuid.uuid4()
    code = f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"
    await session.execute(
        text(
            "INSERT INTO otp_challenges "
            "(id, tenant_id, member_credential_id, code_hash, expires_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:cid AS uuid), :ch, :exp)"
        ),
        {
            "id": str(challenge_id),
            "tid": str(tenant_id),
            "cid": str(credential.credential_id),
            "ch": hash_code(code, salt=str(challenge_id), pepper=_otp_pepper()),
            "exp": _now() + timedelta(seconds=OTP_TTL_SECONDS),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="member_auth.otp_requested",
        payload={
            "member_credential_id": str(credential.credential_id),
            "member_id": str(credential.member_id),
            "challenge_id": str(challenge_id),
            "code": code,
        },
    )


async def verify_member_otp(
    session: AsyncSession, tenant_id: uuid.UUID, email: str, code: str
) -> TokenPair | AuthFailure:
    """Verify the newest member challenge under its row lock (concurrency safety).

    The SAME single gatekeeper as staff (domain/otp.evaluate_challenge:
    consumed/locked/expired/constant-time mismatch) decides acceptance;
    failures are returned, not raised, so the attempt-counter increment
    commits (the P3 AuthFailure contract). Lock posture: the challenge
    row ALONE — see the module docstring for why no principal row lock
    is needed on this path.
    """
    credential = await _live_credential_by_email(session, tenant_id, email)
    if credential is None:
        return AuthFailure("no otp challenge")
    row = (
        await session.execute(
            text(
                "SELECT id, code_hash, attempts, expires_at, consumed_at "
                "FROM otp_challenges "
                "WHERE member_credential_id = CAST(:cid AS uuid) "
                "AND tenant_id = CAST(:tid AS uuid) "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE"
            ),
            {"cid": str(credential.credential_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        return AuthFailure("no otp challenge")
    challenge_id, stored_hash, attempts, expires_at, consumed_at = row
    presented = hash_code(code, salt=str(challenge_id), pepper=_otp_pepper())
    result = evaluate_challenge(
        stored_hash=str(stored_hash),
        presented_hash=presented,
        attempts=int(attempts),
        expires_at=expires_at,
        consumed_at=consumed_at,
        now=_now(),
    )
    if result is OtpResult.MISMATCH:
        await session.execute(
            text(
                "UPDATE otp_challenges SET attempts = attempts + 1 "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(challenge_id), "tid": str(tenant_id)},
        )
    if result is not OtpResult.OK:
        # Returned, not raised: the increment must commit so the
        # lockout engages after OTP_MAX_ATTEMPTS failures (least disclosure).
        return AuthFailure(f"otp {result.value}")
    await session.execute(
        text(
            "UPDATE otp_challenges SET consumed_at = :now "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"now": _now(), "id": str(challenge_id), "tid": str(tenant_id)},
    )
    ctx = MemberAuthContext(
        credential_id=credential.credential_id,
        member_id=credential.member_id,
        tenant_id=tenant_id,
    )
    return await _issue_member_token_pair(session, ctx, family_id=uuid.uuid4())


async def rotate_member_refresh_token(
    session: AsyncSession, tenant_id: uuid.UUID, refresh_token: str
) -> TokenPair | AuthFailure:
    """Rotate a member refresh token; reuse revokes the family (P3).

    The credential's live status is re-checked BEFORE rotation (the
    use-time fence replacing the staff path's user-row lock): a
    revoked link or an exited member can never mint new tokens, even
    from a refresh row that survived revocation.
    """
    peek = (
        await session.execute(
            text(
                "SELECT member_credential_id FROM refresh_tokens "
                "WHERE token_hash = :th AND tenant_id = CAST(:tid AS uuid) "
                "AND member_credential_id IS NOT NULL"
            ),
            {"th": _hash_refresh_token(refresh_token), "tid": str(tenant_id)},
        )
    ).first()
    if peek is None:
        return AuthFailure("unknown refresh token")
    credential = await live_credential_by_id(session, tenant_id, uuid.UUID(str(peek[0])))
    if credential is None:
        # The link was revoked or the member exited since issue: the
        # session is dead (least disclosure — no reason detail).
        return AuthFailure("member credential is not active")
    row = (
        await session.execute(
            text(
                "SELECT id, member_credential_id, family_id, status, expires_at "
                "FROM refresh_tokens WHERE token_hash = :th "
                "AND tenant_id = CAST(:tid AS uuid) FOR UPDATE"
            ),
            {"th": _hash_refresh_token(refresh_token), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        return AuthFailure("unknown refresh token")
    token_id, credential_id, family_id, status, expires_at = row
    if credential_id is None or uuid.UUID(str(credential_id)) != credential.credential_id:
        return AuthFailure("unknown refresh token")
    if status != "active":
        # Reuse detected: the whole family dies (the P3 rule); the
        # revocation commits with the failed request.
        await _revoke_family(session, family_id)
        return AuthFailure("refresh token reuse detected")
    if _now() >= expires_at:
        await _revoke_family(session, family_id)
        return AuthFailure("refresh token expired")
    await session.execute(
        text(
            "UPDATE refresh_tokens SET status = 'rotated', rotated_at = :now "
            "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"now": _now(), "id": str(token_id), "tid": str(tenant_id)},
    )
    ctx = MemberAuthContext(
        credential_id=credential.credential_id,
        member_id=credential.member_id,
        tenant_id=tenant_id,
    )
    return await _issue_member_token_pair(session, ctx, family_id=uuid.UUID(str(family_id)))


async def _issue_member_token_pair(
    session: AsyncSession, ctx: MemberAuthContext, *, family_id: uuid.UUID
) -> TokenPair:
    refresh_token = secrets.token_urlsafe(48)
    await session.execute(
        text(
            "INSERT INTO refresh_tokens "
            "(tenant_id, member_credential_id, family_id, token_hash, expires_at) VALUES "
            "(CAST(:tid AS uuid), CAST(:cid AS uuid), CAST(:fid AS uuid), :th, :exp)"
        ),
        {
            "tid": str(ctx.tenant_id),
            "cid": str(ctx.credential_id),
            "fid": str(family_id),
            "th": _hash_refresh_token(refresh_token),
            "exp": _now() + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
        },
    )
    return TokenPair(
        access_token=issue_member_access_token(ctx),
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
    )
