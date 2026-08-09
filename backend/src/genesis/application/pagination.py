"""Shared keyset cursor helpers for (created_at, id) pagination (scalability).

Single source of truth for cursor encoding (DRY): the loan book and the
applications listing paginate on the same (created_at DESC, id DESC)
keyset and must parse cursors identically.

Opaque signed cursor codec:
``encode_cursor``/``decode_cursor`` wrap EVERY wire cursor in an
HMAC-SHA256-signed base64url token so clients can neither read nor
forge keyset positions. Token layout::

    token = base64url( V || payload || tag )        (padding stripped)
    tag   = HMAC-SHA256( key, V || len(scope)_4 || scope || payload )
    scope = "<tenant uuid>|<endpoint id>"

* ``V`` is the key-version byte (``Settings.cursor_key_version``) —
  rotation (review B13-R10) = deploy the new key/version as the ACTIVE
  pair and demote the old pair to ``Settings.cursor_signing_key_previous``
  / ``cursor_key_version_previous``: decode accepts BOTH versions during
  the deploy window (each verified strictly under its OWN key), encode
  mints only the active version, and any OLDER version (N-2) fails
  closed as a 400 (acceptable: cursors are short-lived pagination
  state). Retire the window by clearing the previous pair.
* The scope is length-prefixed inside the MAC (no concatenation
  ambiguity) and binds the token to ONE tenant and ONE endpoint — a
  cursor can never be replayed across tenants or endpoints (least disclosure).
* ``payload`` is the EXACT plaintext keyset string the inner
  ``parse_*``/``build_*`` helpers below already speak — pagination
  semantics (ordering, page walk, boundaries) are unchanged.
* Every decode failure (bad base64, short token, wrong version, tag
  mismatch, non-UTF-8 payload) raises the house ``InvalidInputError``
  — a sanitized 400, never a 500, never a silently empty page. The
  tag check uses ``hmac.compare_digest`` (no timing oracle).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import uuid
from datetime import datetime

from genesis.errors import InvalidInputError
from genesis.settings import get_settings

#: HMAC-SHA256 digest length; the token's trailing tag is exactly this.
_TAG_LEN = 32

#: Minimum signing-key material (bytes): an HMAC key should be at
#: least the digest size (RFC 2104 / FIPS 198-1 guidance for
#: HMAC-SHA256). Enforced at BOOT, fail closed.
MIN_CURSOR_KEY_BYTES = 32


def assert_cursor_signing_key_configured() -> None:
    """Fail-closed BOOT guard.

    An empty or short ``Settings.cursor_signing_key`` is a DEPLOYMENT
    error and must abort startup — never surface at the first decode
    (where it would blame a caller) or, worse, sign every cursor in
    the fleet with guessable key material. The guard covers BOTH keys
    (review B13-R10): a configured-but-weak PREVIOUS key would quietly
    keep the whole rotation window verifiable under guessable material,
    and a version collision would make the window ambiguous. Called by
    ``genesis.api.app.create_app`` before any router is wired.
    """
    settings = get_settings()
    key = settings.cursor_signing_key.encode()
    if len(key) < MIN_CURSOR_KEY_BYTES:
        raise RuntimeError(
            "cursor_signing_key must be configured with at least "
            f"{MIN_CURSOR_KEY_BYTES} bytes of key material"
        )
    previous = settings.cursor_signing_key_previous.encode()
    if previous:
        if len(previous) < MIN_CURSOR_KEY_BYTES:
            raise RuntimeError(
                "cursor_signing_key_previous must be configured with at least "
                f"{MIN_CURSOR_KEY_BYTES} bytes of key material when set"
            )
        if settings.cursor_key_version_previous == settings.cursor_key_version:
            raise RuntimeError("cursor_key_version_previous must differ from cursor_key_version")


def _cursor_signing_key() -> bytes:
    """Environment-provided secret (the jwt_signing_key house pattern).

    An unconfigured key is a DEPLOYMENT error, not client input — it
    surfaces as the sanitized 500 envelope, never as a 400 that would
    blame the caller for a server misconfiguration.
    """
    key = get_settings().cursor_signing_key
    if not key:
        raise RuntimeError("cursor signing key not configured")
    return key.encode()


def _accepted_decode_keys() -> dict[int, bytes]:
    """Key material by version byte (review B13-R10 rotation window).

    Decode accepts the ACTIVE version and, when a previous pair is
    configured, the PREVIOUS version — each verified strictly under
    its OWN key. Any other version byte (N-2, garbage) has no entry
    and fails closed as a sanitized 400. Encode never consults this
    map: it always mints the active version.
    """
    settings = get_settings()
    keys = {settings.cursor_key_version: _cursor_signing_key()}
    previous = settings.cursor_signing_key_previous
    if previous:
        keys.setdefault(settings.cursor_key_version_previous, previous.encode())
    return keys


def _cursor_tag(
    key: bytes, version: int, tenant_id: uuid.UUID, endpoint: str, payload: bytes
) -> bytes:
    scope = f"{tenant_id}|{endpoint}".encode()
    message = bytes([version]) + struct.pack(">I", len(scope)) + scope + payload
    return hmac.new(key, message, hashlib.sha256).digest()


def encode_cursor(inner: str, *, tenant_id: uuid.UUID, endpoint: str) -> str:
    """Seal a plaintext keyset position into an opaque signed token."""
    version = get_settings().cursor_key_version
    payload = inner.encode()
    tag = _cursor_tag(_cursor_signing_key(), version, tenant_id, endpoint, payload)
    raw = bytes([version]) + payload + tag
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(token: str, *, tenant_id: uuid.UUID, endpoint: str, entity: str) -> str:
    """Verify an opaque token and return the plaintext keyset position.

    Tamper, garbage, foreign-scope and wrong-version tokens all raise
    the same sanitized ``InvalidInputError`` (least disclosure: the
    client learns only that the cursor is invalid, never which check
    failed or what a cursor contains).
    """
    try:
        # validate=True rejects any non-alphabet byte instead of
        # silently discarding it (strict decode).
        # binascii.Error (bad padding/alphabet) subclasses ValueError.
        raw = base64.b64decode(token.encode() + b"=" * (-len(token) % 4), b"-_", validate=True)
    except ValueError as exc:
        raise InvalidInputError(f"invalid {entity} cursor") from exc
    if len(raw) < 1 + _TAG_LEN:
        raise InvalidInputError(f"invalid {entity} cursor")
    version, payload, tag = raw[0], raw[1:-_TAG_LEN], raw[-_TAG_LEN:]
    key = _accepted_decode_keys().get(version)
    if key is None:
        raise InvalidInputError(f"invalid {entity} cursor")
    if not hmac.compare_digest(tag, _cursor_tag(key, version, tenant_id, endpoint, payload)):
        raise InvalidInputError(f"invalid {entity} cursor")
    try:
        return payload.decode()
    except UnicodeDecodeError as exc:
        raise InvalidInputError(f"invalid {entity} cursor") from exc


def parse_created_id_cursor(cursor: str, *, entity: str) -> tuple[datetime, str]:
    """Parse an opaque '<created_at iso>|<uuid>' keyset cursor.

    Naive timestamps are rejected: cursors are only ever built from
    timestamptz values, so a missing UTC offset means a forged or
    corrupted cursor rather than a valid page position.
    """
    ts_raw, _, id_raw = cursor.partition("|")
    try:
        ts = datetime.fromisoformat(ts_raw)
        row_id = str(uuid.UUID(id_raw))
    except ValueError as exc:
        raise InvalidInputError(f"invalid {entity} cursor") from exc
    if ts.tzinfo is None:
        raise InvalidInputError(f"invalid {entity} cursor")
    return ts, row_id


def build_created_id_cursor(created_at: datetime, row_id: object) -> str:
    """Encode the keyset position of the last row of a page."""
    return f"{created_at.isoformat()}|{row_id}"


def parse_band_register_cursor(cursor: str, *, entity: str) -> tuple[bool, datetime, str]:
    """Parse an opaque '<actionable 0|1>|<created_at iso>|<uuid>' keyset
    cursor for a two-band (actionable-first) register order (the 0038
    pattern). The leading flag is the band of the last row of the
    previous page; a forged flag is rejected exactly like a forged
    timestamp.

    Hoisted verbatim from application/corrections.py (reuse-first) when
    the share-transfers register became
    the pattern's third consumer — the band registers must never
    diverge on cursor encoding.
    """
    flag_raw, _, rest = cursor.partition("|")
    if flag_raw not in {"0", "1"}:
        raise InvalidInputError(f"invalid {entity} cursor")
    c_ts, c_id = parse_created_id_cursor(rest, entity=entity)
    return flag_raw == "1", c_ts, c_id


def build_band_register_cursor(actionable: bool, created_at: datetime, row_id: object) -> str:
    """Encode the two-band keyset position of the last row of a page."""
    return f"{1 if actionable else 0}|{build_created_id_cursor(created_at, row_id)}"
