"""Authentication use-cases: OTP step-up and JWT lifecycle (the house gates).

Access tokens live at most 15 minutes. Refresh tokens are hashed at rest,
rotate on every use, and belong to a family: any reuse of a spent token
revokes the whole family. OTP delivery goes through the transactional
outbox stub (reliability). All verification runs under row locks (concurrency safety).

Lock ordering: user row -> otp_challenges row -> refresh_tokens
rows, matching application/users.py (which additionally leads with the
ordered active-admin set). Token issue writes users.last_active_at
under the already-held user row lock and acquires nothing new.

Tenant-predicate exemption (documented per, least disclosure v1.1):
authentication queries here deliberately do NOT carry an explicit bound
tenant_id predicate. They run BEFORE a tenant context exists — the
email lookup establishes which tenant the user belongs to, and the
refresh/OTP lookups are keyed by unguessable secrets (token hashes,
challenge rows found via the locked user id). Forced RLS still fences
every one of these tables once app.tenant_id is set; pre-context
sessions fail loudly (see test_missing_tenant_context_fails_loudly).
Adding a tenant predicate here would require the caller to know the
tenant before authenticating, which is the wrong trust direction.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.outbox import enqueue_event
from genesis.domain.members import normalize_kenya_msisdn
from genesis.domain.otp import (
    OTP_LENGTH,
    OTP_TTL_SECONDS,
    OtpResult,
    evaluate_challenge,
    hash_code,
)
from genesis.errors import ForbiddenError, UnauthenticatedError
from genesis.settings import get_settings

ACCESS_TOKEN_TTL_SECONDS = 900
REFRESH_TOKEN_TTL_SECONDS = 14 * 24 * 3600
_JWT_ALGORITHM = "HS256"

#: Token audiences: STAFF and MEMBER principals are
#: disjoint credential populations. Every issued token carries exactly
#: one of these code-owned audience values and every decode dispatches
#: on it deny-by-default — a member token can never satisfy a staff
#: RequirePermission gate and a staff token can never satisfy the
#: member principal gate (both directions falsifiably tested).
STAFF_AUDIENCE = "genesis-staff"
MEMBER_AUDIENCE = "genesis-member"

#: Sign-in user resolution (#35 sign-in identifier round) as module-level
#: constants so the EXPLAIN structural gate exercises the production
#: statements (house convention). The documented tenant-predicate
#: exemption above applies to all four: forced RLS supplies the tenant
#: fence. The phone probes ride 0044's partial idx_users_phone
#: (tenant_id, phone) WHERE phone IS NOT NULL under that qual; the email
#: probes keep riding the 0001 UNIQUE (tenant_id, email). The identifier
#: is ALWAYS a bound parameter (gate 1.6), never interpolated.
USER_ID_BY_EMAIL_SQL = "SELECT id FROM users WHERE email = :ident AND status = 'active'"
USER_ID_BY_PHONE_SQL = "SELECT id FROM users WHERE phone = :ident AND status = 'active'"
USER_LOCK_BY_EMAIL_SQL = "SELECT id, role_id FROM users WHERE email = :ident FOR UPDATE"
USER_LOCK_BY_PHONE_SQL = "SELECT id, role_id FROM users WHERE phone = :ident FOR UPDATE"

#: Code-owned identifier kinds (the classifier returns one of these two).
IDENTIFIER_PHONE = "phone"
IDENTIFIER_EMAIL = "email"


def resolve_signin_identifier(identifier: str) -> tuple[str, str]:
    """Classify a sign-in identifier as phone or email — ONE rule (#35).

    A value the maintainer-declared Kenya msisdn rule accepts IS a
    phone: it is normalized through the single existing normalizer
    (domain/members.py:normalize_kenya_msisdn — never a second
    normalizer) and resolves by its E.164 form against stored
    users.phone (E.164 by the 0042 backfill). Everything else takes
    the email path byte-identically — including malformed phone-ish
    strings, which simply resolve no user and surface the same
    non-disclosing outcome as any unknown email (gate 1.6: no
    existence oracle, no echoed identifier).
    """
    normalized = normalize_kenya_msisdn(identifier)
    if normalized is not None:
        return (IDENTIFIER_PHONE, normalized)
    return (IDENTIFIER_EMAIL, identifier)


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class AuthFailure:
    """Failed authentication outcome whose side effects must survive.

    Punitive state changes (OTP attempt counters, refresh-family
    revocation) have to be committed even though the request fails, so
    failures are returned as values and translated into a 401 by the API
    layer only after the transaction has committed (the house gates).
    Raising inside the transaction would roll the punitive state back.
    """

    reason: str


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class MemberAuthContext:
    """The MEMBER principal: a member_credentials link row.

    ``credential_id`` is the authenticated link (the authoritative identity); ``member_id`` is the
    member it mapped to AT TOKEN
    ISSUE and is re-verified against the live link on every request by
    the member gate (api/authz.py:RequireMemberPrincipal) and inside
    every consent/release transaction, so a re-linked or revoked
    credential can never keep acting for the old member.
    """

    credential_id: uuid.UUID
    member_id: uuid.UUID
    tenant_id: uuid.UUID


def _now() -> datetime:
    return datetime.now(UTC)


def _signing_key() -> str:
    key = get_settings().jwt_signing_key
    if not key:
        raise UnauthenticatedError("jwt signing key not configured")
    return key


def _otp_pepper() -> str:
    pepper = get_settings().otp_pepper
    if not pepper:
        raise UnauthenticatedError("otp pepper not configured")
    return pepper


def _hash_refresh_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_access_token(ctx: AuthContext, *, now: datetime | None = None) -> str:
    """Signed STAFF access token, at most 15 minutes (the house gatesFM1)."""
    issued_at = now or _now()
    claims: dict[str, Any] = {
        "sub": str(ctx.user_id),
        "tid": str(ctx.tenant_id),
        "rid": str(ctx.role_id),
        "aud": STAFF_AUDIENCE,
        "iat": int(issued_at.timestamp()),
        "exp": int(issued_at.timestamp()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, _signing_key(), algorithm=_JWT_ALGORITHM)


def issue_member_access_token(ctx: MemberAuthContext, *, now: datetime | None = None) -> str:
    """Signed MEMBER access token, at most 15 minutes."""
    issued_at = now or _now()
    claims: dict[str, Any] = {
        "sub": str(ctx.credential_id),
        "tid": str(ctx.tenant_id),
        "mid": str(ctx.member_id),
        "aud": MEMBER_AUDIENCE,
        "iat": int(issued_at.timestamp()),
        "exp": int(issued_at.timestamp()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, _signing_key(), algorithm=_JWT_ALGORITHM)


def decode_principal(token: str) -> AuthContext | MemberAuthContext:
    """Validate a bearer token and dispatch on its audience.

    Signature and expiry are verified first; the audience dispatch is
    a code-owned mapping and DENY BY DEFAULT — a missing or unknown
    audience (including earlier legacy tokens, which live at most 15 minutes) is refused as
    unauthenticated.
    """
    try:
        claims = jwt.decode(
            token,
            _signing_key(),
            algorithms=[_JWT_ALGORITHM],
            # Audience is dispatched manually below so BOTH principal
            # kinds decode through this single entry point; signature
            # and exp verification stay on.
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("invalid access token") from exc
    audience = claims.get("aud")
    if audience == STAFF_AUDIENCE:
        return AuthContext(
            user_id=uuid.UUID(str(claims["sub"])),
            tenant_id=uuid.UUID(str(claims["tid"])),
            role_id=uuid.UUID(str(claims["rid"])),
        )
    if audience == MEMBER_AUDIENCE:
        return MemberAuthContext(
            credential_id=uuid.UUID(str(claims["sub"])),
            member_id=uuid.UUID(str(claims["mid"])),
            tenant_id=uuid.UUID(str(claims["tid"])),
        )
    raise UnauthenticatedError("unknown token audience")


def decode_access_token(token: str) -> AuthContext:
    """STAFF-only decode; a member token is refused with a 403.

    403, not 401: the token is valid, the principal is simply the
    wrong KIND for a staff permission gate — deny by default, and the
    member learns nothing about the staff surface (least disclosure).
    """
    principal = decode_principal(token)
    if isinstance(principal, MemberAuthContext):
        raise ForbiddenError("member principal cannot satisfy a staff permission gate")
    return principal


def decode_member_access_token(token: str) -> MemberAuthContext:
    """MEMBER-only decode; a staff token is refused with a 403."""
    principal = decode_principal(token)
    if isinstance(principal, AuthContext):
        raise ForbiddenError("staff principal cannot satisfy the member gate")
    return principal


async def request_otp(session: AsyncSession, tenant_id: uuid.UUID, identifier: str) -> str | None:
    """Issue a single-use, 5-minute OTP; never reveals whether the user exists.

    The identifier may be an email address or a Kenya mobile number
    (#35 sign-in identifier): classification and normalization go
    through resolve_signin_identifier — one rule, one normalizer.

    Returns the issued code IN-PROCESS ONLY (None when no challenge was
    issued): the API layer surfaces it exclusively behind the
    fail-closed dev_otp_display flag (item 11 — REMOVE before staging) and it is never logged.
    """
    kind, value = resolve_signin_identifier(identifier)
    lookup_sql = USER_ID_BY_PHONE_SQL if kind == IDENTIFIER_PHONE else USER_ID_BY_EMAIL_SQL
    row = (await session.execute(text(lookup_sql), {"ident": value})).first()
    if row is None:
        # Equal-work non-disclosure (gate 1.6): burn the same hash cost
        # the issue path pays before returning, so a rejected identifier
        # matches an accepted one in response shape AND in the work the
        # request performs (the falsifiable legs assert the shape and
        # the zero-challenge side effect; wall-clock timing is
        # deliberately not asserted — it flakes on loaded CI runners).
        hash_code(
            f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}",
            salt=str(uuid.uuid4()),
            pepper=_otp_pepper(),
        )
        return None
    user_id = str(row[0])
    challenge_id = uuid.uuid4()
    code = f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"
    await session.execute(
        text(
            "INSERT INTO otp_challenges (id, tenant_id, user_id, code_hash, expires_at) "
            "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:uid AS uuid), :ch, :exp)"
        ),
        {
            "id": str(challenge_id),
            "tid": str(tenant_id),
            "uid": user_id,
            "ch": hash_code(code, salt=str(challenge_id), pepper=_otp_pepper()),
            "exp": _now() + timedelta(seconds=OTP_TTL_SECONDS),
        },
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="auth.otp_requested",
        payload={"user_id": user_id, "challenge_id": str(challenge_id), "code": code},
    )
    return code


async def verify_otp(
    session: AsyncSession, tenant_id: uuid.UUID, identifier: str, code: str
) -> TokenPair | AuthFailure:
    """Verify the newest challenge under a row lock (concurrency safety).

    The identifier may be an email address or a Kenya mobile number
    (#35 sign-in identifier), resolved by the same one-rule classifier
    as the request path.

    Failures are returned (not raised) so the attempt-counter increment
    commits with the surrounding transaction; the API layer raises the
    401 after commit.
    """
    # Lock ordering: the USER row is locked before the challenge
    # row, matching the suspension writer (application/users.py), which
    # holds the user row while voiding challenges. Locking in the other
    # order (the earlier join) would deadlock verify-vs-suspend.
    kind, value = resolve_signin_identifier(identifier)
    lock_sql = USER_LOCK_BY_PHONE_SQL if kind == IDENTIFIER_PHONE else USER_LOCK_BY_EMAIL_SQL
    user_row = (await session.execute(text(lock_sql), {"ident": value})).first()
    if user_row is None:
        # Same sanitized category as a known user with no challenge —
        # the wire carries no existence oracle (gate 1.6).
        return AuthFailure("no otp challenge")
    user_id, role_id = user_row
    row = (
        await session.execute(
            text(
                "SELECT id, code_hash, attempts, expires_at, consumed_at "
                "FROM otp_challenges WHERE user_id = CAST(:uid AS uuid) "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE"
            ),
            {"uid": str(user_id)},
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
            text("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = CAST(:id AS uuid)"),
            {"id": str(challenge_id)},
        )
    if result is not OtpResult.OK:
        # Returned, not raised: the increment above must commit so the
        # lockout engages after OTP_MAX_ATTEMPTS failures (least disclosure).
        return AuthFailure(f"otp {result.value}")
    await session.execute(
        text("UPDATE otp_challenges SET consumed_at = :now WHERE id = CAST(:id AS uuid)"),
        {"now": _now(), "id": str(challenge_id)},
    )
    ctx = AuthContext(
        user_id=uuid.UUID(str(user_id)),
        tenant_id=tenant_id,
        role_id=uuid.UUID(str(role_id)),
    )
    return await _issue_token_pair(session, ctx, family_id=uuid.uuid4())


async def rotate_refresh_token(
    session: AsyncSession, tenant_id: uuid.UUID, refresh_token: str
) -> TokenPair | AuthFailure:
    """Rotate under a row lock; reuse of a spent token revokes its family.

    Failures are returned (not raised) so the family-wide revocation
    commits with the surrounding transaction; the API layer raises the
    401 after commit.
    """
    # Lock ordering: peek at the token row WITHOUT a lock to
    # learn the user, lock the USER row first, then re-lock and
    # re-validate the token row. The suspension writer holds the user
    # row while revoking refresh rows, so taking the token row first
    # (the earlier order) would deadlock rotate-vs-suspend. The
    # re-validation under the lock keeps rotation race-safe: a token
    # spent between peek and lock is caught by the status check below.
    peek = (
        await session.execute(
            text("SELECT user_id FROM refresh_tokens WHERE token_hash = :th"),
            {"th": _hash_refresh_token(refresh_token)},
        )
    ).first()
    if peek is None:
        return AuthFailure("unknown refresh token")
    user_row = (
        await session.execute(
            text("SELECT role_id FROM users WHERE id = CAST(:uid AS uuid) FOR UPDATE"),
            {"uid": str(peek[0])},
        )
    ).first()
    if user_row is None:
        # the user vanished between the unlocked peek and the
        # row lock (refresh_tokens.user_id is ON DELETE CASCADE, so the
        # committed state is gone too). A 401 is the honest outcome —
        # scalar_one() here would surface a 500 instead.
        return AuthFailure("refresh token user vanished")
    role_id = user_row[0]
    row = (
        await session.execute(
            text(
                "SELECT id, user_id, family_id, status, expires_at "
                "FROM refresh_tokens WHERE token_hash = :th FOR UPDATE"
            ),
            {"th": _hash_refresh_token(refresh_token)},
        )
    ).first()
    if row is None:
        return AuthFailure("unknown refresh token")
    token_id, user_id, family_id, status, expires_at = row
    if status != "active":
        # Returned, not raised: the revocation must commit so every
        # descendant token in the family dies with the reuse (least disclosure).
        await _revoke_family(session, family_id)
        return AuthFailure("refresh token reuse detected")
    if _now() >= expires_at:
        await _revoke_family(session, family_id)
        return AuthFailure("refresh token expired")
    await session.execute(
        text(
            "UPDATE refresh_tokens SET status = 'rotated', rotated_at = :now "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"now": _now(), "id": str(token_id)},
    )
    ctx = AuthContext(
        user_id=uuid.UUID(str(user_id)),
        tenant_id=tenant_id,
        role_id=uuid.UUID(str(role_id)),
    )
    return await _issue_token_pair(session, ctx, family_id=uuid.UUID(str(family_id)))


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> None:
    """Logout: revoke the whole family behind the presented token."""
    row = (
        await session.execute(
            text("SELECT family_id FROM refresh_tokens WHERE token_hash = :th"),
            {"th": _hash_refresh_token(refresh_token)},
        )
    ).first()
    if row is None:
        return
    await _revoke_family(session, row[0])


async def _issue_token_pair(
    session: AsyncSession, ctx: AuthContext, *, family_id: uuid.UUID
) -> TokenPair:
    refresh_token = secrets.token_urlsafe(48)
    # last_active_at is written at TOKEN ISSUE only — never per
    # request — so the prototype "last active" column costs one write
    # per login/refresh instead of one per API call. Both callers
    # already hold this user row FOR UPDATE (lock ordering above), so
    # this update never acquires a new lock. Explicit tenant predicate
    # on the write: the tenant is known here.
    await session.execute(
        text(
            "UPDATE users SET last_active_at = :now "
            "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
        ),
        {"now": _now(), "uid": str(ctx.user_id), "tid": str(ctx.tenant_id)},
    )
    await session.execute(
        text(
            "INSERT INTO refresh_tokens "
            "(tenant_id, user_id, family_id, token_hash, expires_at) VALUES "
            "(CAST(:tid AS uuid), CAST(:uid AS uuid), CAST(:fid AS uuid), :th, :exp)"
        ),
        {
            "tid": str(ctx.tenant_id),
            "uid": str(ctx.user_id),
            "fid": str(family_id),
            "th": _hash_refresh_token(refresh_token),
            "exp": _now() + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
        },
    )
    return TokenPair(
        access_token=issue_access_token(ctx),
        refresh_token=refresh_token,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
    )


async def _revoke_family(session: AsyncSession, family_id: object) -> None:
    await session.execute(
        text(
            "UPDATE refresh_tokens SET status = 'revoked' "
            "WHERE family_id = CAST(:fid AS uuid) AND status <> 'revoked'"
        ),
        {"fid": str(family_id)},
    )
