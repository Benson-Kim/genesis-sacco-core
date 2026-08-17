"""Idempotency-Key middleware (gate 1.4).

The first request claims (tenant_id, key) with a UNIQUE insert. Concurrent
duplicates find the claim and receive 409 while the original is in flight;
once a response is stored, replays return it verbatim. A 5xx or crash
releases the claim so the client can retry.

Replay scope (review R4): a stored response is returned ONLY to the
same (tenant, actor, method, path, body) — the actor rides the request
hash, so a DIFFERENT user presenting the same key can never read the
first caller's stored response (that would bypass the per-handler
authorization the original caller passed). A mismatched hash gets the
least-disclosure 409 envelope instead.

Expiry (P13.17c / DSA-3, named failure mode FM3): every claim carries
expires_at = now() + Settings.idempotency_retention_hours (server
config, v1.1 rule 1 — no request field exists for it). The fence
``expires_at > now()`` is enforced in BOTH places a stored row can be
read — the replay lookup below AND the claim statement's takeover arm —
so expiry holds even before the retention purge
(application/idempotency_purge.py) has ever run:

  * the claim is ONE atomic statement: INSERT .. ON CONFLICT DO UPDATE
    whose WHERE arm takes over the existing row ONLY when it has
    expired (resetting hash/response/epoch). Two concurrent requests
    on an expired key serialise on the row: the winner's takeover
    commits a fresh live epoch and the loser's WHERE re-evaluates to
    false — exactly one new effect per claim epoch (FM3);
  * an expired key therefore executes as a NEW request with its own
    single effect — it never replays the stale stored response;
  * _store/_release are pinned to the caller's own epoch
    (request_hash match + still in flight), so a stale writer that
    outlived its retention window can never clobber a successor epoch.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from genesis.application.auth import decode_access_token
from genesis.errors import UnauthenticatedError
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Atomic claim + expired-claim takeover (DSA-3, FM3) in ONE statement:
#: a fresh key INSERTs; a conflicting LIVE row does nothing (no row
#: returned → replay/409 path); a conflicting EXPIRED row is taken
#: over — hash and response reset, a fresh retention window started —
#: under the row lock, so concurrent takeovers serialise and exactly
#: one wins (the loser's WHERE re-evaluates against the committed
#: fresh epoch and fails). Module-level so tests pin the exact SQL.
CLAIM_SQL = (
    "INSERT INTO idempotency_keys (tenant_id, key, request_hash, expires_at) "
    "VALUES (CAST(:tid AS uuid), :key, :rh, "
    "now() + make_interval(hours => :hours)) "
    "ON CONFLICT (tenant_id, key) DO UPDATE SET "
    "request_hash = EXCLUDED.request_hash, "
    "response_status = NULL, "
    "response_body = NULL, "
    "created_at = now(), "
    "expires_at = EXCLUDED.expires_at "
    "WHERE idempotency_keys.expires_at <= now() "
    "RETURNING id"
)

#: Replay lookup with the expiry fence (DSA-3): an expired row is
#: invisible here even before the purge job has ever run.
REPLAY_LOOKUP_SQL = (
    "SELECT request_hash, response_status, response_body "
    "FROM idempotency_keys "
    "WHERE tenant_id = CAST(:tid AS uuid) AND key = :key "
    "AND expires_at > now()"
)


def idempotency_retention_hours() -> int:
    """Replay retention from server config (v1.1 rule 1) — the
    exports.py accessor pattern so tests can pin values."""
    return get_settings().idempotency_retention_hours


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _tenant_from_scope(scope: Scope) -> uuid.UUID | None:
    raw = _header(scope, b"x-tenant-id")
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None
    bearer = _header(scope, b"authorization") or ""
    if bearer.lower().startswith("bearer "):
        try:
            return decode_access_token(bearer[7:]).tenant_id
        except UnauthenticatedError:
            return None
    return None


def _actor_from_scope(scope: Scope) -> str:
    """Actor discriminator for the request hash (review R4).

    Authenticated requests hash the token's user_id so replays are
    per-user; pre-auth requests (x-tenant-id header, no bearer) have no
    actor and hash the empty string — their identity lives in the body
    (e.g. the OTP email), which is already part of the hash.
    """
    bearer = _header(scope, b"authorization") or ""
    if bearer.lower().startswith("bearer "):
        try:
            return str(decode_access_token(bearer[7:]).user_id)
        except UnauthenticatedError:
            return ""
    return ""


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _send_json(send: Send, status: int, payload: Any, *, replayed: bool) -> None:
    body = b"" if payload is None or status == 204 else json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if replayed:
        headers.append((b"idempotency-replayed", b"true"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    """Raw ASGI middleware so the request body can be buffered and replayed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _MUTATING:
            await self.app(scope, receive, send)
            return
        key = _header(scope, b"idempotency-key")
        settings = get_settings()
        if not key or not settings.database_url:
            await self.app(scope, receive, send)
            return
        tenant_id = _tenant_from_scope(scope)
        if tenant_id is None:
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        method: str = scope["method"]
        path: str = scope["path"]
        # The actor is part of the hash (review R4): a different user
        # replaying the same key mismatches and gets the 409 envelope,
        # never the stored response of a request they never authorized.
        actor = _actor_from_scope(scope)
        request_hash = hashlib.sha256(
            b"|".join([actor.encode(), method.encode(), path.encode(), body])
        ).hexdigest()
        factory = get_sessionmaker(settings.database_url)

        stored: tuple[Any, Any, Any] | None = None
        async with tenant_session(factory, tenant_id) as session:
            claimed = (
                await session.execute(
                    text(CLAIM_SQL),
                    {
                        "tid": str(tenant_id),
                        "key": key,
                        "rh": request_hash,
                        "hours": idempotency_retention_hours(),
                    },
                )
            ).first()
            if claimed is None:
                # A LIVE claim exists (the takeover arm refused it).
                # The lookup repeats the expiry fence: if the row
                # expired in the meantime, neither replay nor 409 is
                # served from it — the client simply retries into the
                # takeover path.
                row = (
                    await session.execute(
                        text(REPLAY_LOOKUP_SQL),
                        {"tid": str(tenant_id), "key": key},
                    )
                ).first()
                if row is not None:
                    stored = (row[0], row[1], row[2])

        if claimed is None:
            if stored is not None and stored[0] == request_hash and stored[1] is not None:
                await _send_json(send, int(stored[1]), stored[2], replayed=True)
                return
            await _send_json(
                send,
                409,
                {
                    "category": "conflict",
                    "correlation_id": _header(scope, b"x-request-id") or "",
                },
                replayed=False,
            )
            return

        status_code: int | None = None
        parts: list[bytes] = []
        replay_done = False

        async def replay_receive() -> Message:
            nonlocal replay_done
            if not replay_done:
                replay_done = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def capture_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            elif message["type"] == "http.response.body":
                parts.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except Exception:
            await _release(factory, tenant_id, key, request_hash)
            raise

        if status_code is not None and status_code < 500:
            await _store(factory, tenant_id, key, request_hash, status_code, b"".join(parts))
        else:
            await _release(factory, tenant_id, key, request_hash)


async def _store(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    key: str,
    request_hash: str,
    status_code: int,
    raw: bytes,
) -> None:
    try:
        payload: Any = json.loads(raw) if raw else None
    except ValueError:
        payload = {"raw": raw.decode("utf-8", errors="replace")}
    async with tenant_session(factory, tenant_id) as session:
        # Epoch pin (DSA-3): only the epoch this request claimed —
        # same hash, still in flight — may receive the response. A
        # writer that outlived its retention window finds a successor
        # epoch (hash reset or response stored) and writes nothing.
        await session.execute(
            text(
                "UPDATE idempotency_keys SET response_status = :st, "
                "response_body = CAST(:body AS jsonb) "
                "WHERE tenant_id = CAST(:tid AS uuid) AND key = :key "
                "AND request_hash = :rh AND response_status IS NULL"
            ),
            {
                "st": status_code,
                "body": json.dumps(payload),
                "tid": str(tenant_id),
                "key": key,
                "rh": request_hash,
            },
        )


async def _release(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    key: str,
    request_hash: str,
) -> None:
    async with tenant_session(factory, tenant_id) as session:
        # Same epoch pin as _store: a 5xx releases only the claim this
        # request owns, never a successor epoch's live claim.
        await session.execute(
            text(
                "DELETE FROM idempotency_keys "
                "WHERE tenant_id = CAST(:tid AS uuid) AND key = :key "
                "AND request_hash = :rh AND response_status IS NULL"
            ),
            {"tid": str(tenant_id), "key": key, "rh": request_hash},
        )
