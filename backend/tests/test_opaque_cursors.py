"""Falsifiable suite for the opaque signed cursor codec (#31 batch 13, N4).

Pure, no I/O. Every leg names the failure mode it guards (MASTER_PROMPT
§4): FM1 round-trip exactness per keyset tuple shape (with a
hand-computed independent oracle), FM2/FM3 payload/tag bit-flips,
FM4 truncated/garbage/non-base64, FM5 cross-endpoint replay, FM6
cross-tenant replay, FM7 wrong key version, FM8 constant-time compare
(falsifiable by swapping ``hmac.compare_digest`` for ``==``), FM13
dual-version rotation window (review B13-R10: previous-version tokens
verify under their OWN key, N-2 refused, encode mints only the active
version, boot rejects a weak or version-colliding previous key).
"""

import base64
import hashlib
import hmac as hmac_module
import os
import struct
import uuid

import pytest

from genesis.api.app import create_app
from genesis.application import pagination
from genesis.application.branches import BRANCH_MEMBERS_SCOPE
from genesis.application.members import MEMBERS_LIST_SCOPE
from genesis.application.pagination import decode_cursor, encode_cursor
from genesis.errors import InvalidInputError
from genesis.settings import get_settings

TENANT = uuid.UUID("6dcd4ce0-6dcd-4ce0-8dcd-4ce06dcd4ce0")
OTHER_TENANT = uuid.UUID("1b2f8a44-1b2f-4a44-8b2f-8a441b2f8a44")
ENDPOINT = MEMBERS_LIST_SCOPE  # the real exported scope symbol (B13-R2)

#: Every distinct plaintext keyset tuple shape served on the wire
#: (the batch-13 inventory): raw member_no (the N1 (length, member_no)
#: numeric tuple), raw doc_type string, ISO date (periods),
#: timestamp|uuid composite, bigint audit composite, days-past-due
#: int|uuid worklist composite, and the 0038 two-band register shape.
SHAPES = (
    "GP-0042",
    "GP-10000",
    "kra_pin",
    "2026-01-31",
    "2026-07-29T12:00:00+00:00|8c5f3ab0-90fb-4b21-9d70-4a4b6a4f3a10",
    "2026-07-29T12:00:00+00:00|1048576",
    "184|8c5f3ab0-90fb-4b21-9d70-4a4b6a4f3a10",
    "1|2026-07-29T12:00:00+00:00|8c5f3ab0-90fb-4b21-9d70-4a4b6a4f3a10",
)


def _raw(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def _tok(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.mark.parametrize("inner", SHAPES)
def test_round_trip_exactness_per_shape(inner: str) -> None:
    """FM1: decode(encode(x)) == x, byte-exact, for EVERY tuple shape."""
    token = encode_cursor(inner, tenant_id=TENANT, endpoint=ENDPOINT)
    assert token != inner  # actually opaque, not a pass-through
    out = decode_cursor(token, tenant_id=TENANT, endpoint=ENDPOINT, entity="member")
    assert out == inner


def test_hand_computed_oracle_pins_the_token_format() -> None:
    """FM1 oracle: the token is derived here INDEPENDENTLY from the
    documented spec — base64url(V || payload || HMAC-SHA256(key,
    V || len(scope)_4BE || scope || payload)) with scope
    '<tenant>|<endpoint>' — using only stdlib primitives, never the
    codec's own helpers. Falsifiable by ANY drift in the token layout
    (field order, separator, scope shape, digest, encoding).
    """
    key = os.environ["CURSOR_SIGNING_KEY"].encode()
    version = get_settings().cursor_key_version
    payload = b"GP-0042"
    scope = f"{TENANT}|{ENDPOINT}".encode()
    tag = hmac_module.new(
        key, bytes([version]) + struct.pack(">I", len(scope)) + scope + payload, hashlib.sha256
    ).digest()
    expected = base64.urlsafe_b64encode(bytes([version]) + payload + tag).rstrip(b"=").decode()
    assert encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT) == expected


def test_payload_bit_flip_rejected() -> None:
    """FM2: one flipped bit in the payload region breaks the tag."""
    raw = bytearray(_raw(encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)))
    raw[3] ^= 0x01  # inside payload (byte 0 is the version byte)
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(_tok(bytes(raw)), tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_tag_bit_flip_rejected() -> None:
    """FM3: one flipped bit in the tag region fails verification."""
    raw = bytearray(_raw(encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)))
    raw[-1] ^= 0x01
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(_tok(bytes(raw)), tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "zz",  # shorter than version+tag
        "not base64 !!!",  # non-alphabet bytes (strict decode)
        "GP-0042",  # a raw plaintext cursor is no longer accepted
        "2026-07-29T12:00:00+00:00|8c5f3ab0-90fb-4b21-9d70-4a4b6a4f3a10",
    ],
)
def test_truncated_and_garbage_tokens_rejected(bad: str) -> None:
    """FM4: malformed tokens are sanitized 400s — never 500s."""
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(bad, tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_truncated_valid_token_rejected() -> None:
    """FM4: truncating a VALID token (losing tag bytes) is rejected."""
    token = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    half = token[: len(token) // 2]
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(half, tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_cross_endpoint_replay_rejected() -> None:
    """FM5: a token minted for one endpoint never opens another."""
    token = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=MEMBERS_LIST_SCOPE)
    with pytest.raises(InvalidInputError, match="invalid roster cursor"):
        decode_cursor(token, tenant_id=TENANT, endpoint=BRANCH_MEMBERS_SCOPE, entity="roster")


def test_cross_tenant_replay_rejected() -> None:
    """FM6: a token minted for one tenant never opens another's page."""
    token = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(token, tenant_id=OTHER_TENANT, endpoint=ENDPOINT, entity="member")


def test_wrong_key_version_rejected() -> None:
    """FM7: a token carrying a different key-version byte fails closed
    BEFORE tag verification (rotation semantics)."""
    raw = bytearray(_raw(encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)))
    assert raw[0] == get_settings().cursor_key_version
    raw[0] ^= 0xFF
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(_tok(bytes(raw)), tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_tag_check_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """FM8: the primitive verifies via hmac.compare_digest — falsifiable
    by swapping the comparison to ``==`` (the spy then never fires)."""
    calls: list[int] = []
    real = hmac_module.compare_digest

    def spy(a: object, b: object) -> bool:
        calls.append(1)
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(pagination.hmac, "compare_digest", spy)
    token = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    assert decode_cursor(token, tenant_id=TENANT, endpoint=ENDPOINT, entity="member") == "GP-0042"
    assert calls, "decode did not route the tag check through hmac.compare_digest"


def test_unconfigured_key_is_a_server_error_not_a_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset signing key is a DEPLOYMENT error (sanitized 500 path),
    never a 400 that blames the caller (the jwt_signing_key pattern)."""
    unconfigured = get_settings().model_copy(update={"cursor_signing_key": ""})
    monkeypatch.setattr(pagination, "get_settings", lambda: unconfigured)
    with pytest.raises(RuntimeError, match="cursor signing key not configured"):
        encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)


@pytest.mark.parametrize("bad", ["", "short", "x" * 31], ids=["empty", "tiny", "31-bytes"])
def test_boot_rejects_missing_or_short_cursor_signing_key(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B13-R5 fail-closed BOOT guard: an empty or <32-byte signing key
    aborts ``create_app`` — the misconfiguration can never wait for the
    first decode. Falsifiable: drop the guard call from create_app (or
    weaken the length check) and this boots a weakly-keyed app."""
    weak = get_settings().model_copy(update={"cursor_signing_key": bad})
    monkeypatch.setattr(pagination, "get_settings", lambda: weak)
    with pytest.raises(RuntimeError, match="cursor_signing_key must be configured"):
        create_app()


def test_boot_accepts_a_32_byte_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard's exact boundary: 32 bytes of key material boots."""
    ok = get_settings().model_copy(update={"cursor_signing_key": "x" * 32})
    monkeypatch.setattr(pagination, "get_settings", lambda: ok)
    create_app()  # must not raise


# --- FM13: dual-version rotation window (review B13-R10) --------------

#: All three >= 32 bytes so only the LEGS under test can fail, never
#: the B13-R5 length floor.
_ACTIVE_KEY = "rotation-active-key-material-abcdefghij"
_PREVIOUS_KEY = "rotation-previous-key-material-0123456789"
_OLDEST_KEY = "rotation-n-minus-2-key-material-zyxwvutsrq"


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    cfg = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(pagination, "get_settings", lambda: cfg)


def test_fm13_previous_version_token_verifies_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FM13 leg (a): a token minted under the PREVIOUS pair keeps
    decoding through the rotation window — verified under the previous
    KEY, never merely waved through by its version byte. Falsifiable:
    drop the previous entry from ``_accepted_decode_keys`` (or verify
    previous-version tokens under the active key) and this fails."""
    _configure(monkeypatch, cursor_signing_key=_PREVIOUS_KEY, cursor_key_version=7)
    token = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    _configure(
        monkeypatch,
        cursor_signing_key=_ACTIVE_KEY,
        cursor_key_version=8,
        cursor_signing_key_previous=_PREVIOUS_KEY,
        cursor_key_version_previous=7,
    )
    assert decode_cursor(token, tenant_id=TENANT, endpoint=ENDPOINT, entity="member") == "GP-0042"


def test_fm13_previous_version_byte_alone_opens_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FM13 leg (a) negative: a token CARRYING the previous version
    byte but signed with different key material is refused — the tag
    must verify under the previous key itself."""
    _configure(monkeypatch, cursor_signing_key=_OLDEST_KEY, cursor_key_version=7)
    forged = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    _configure(
        monkeypatch,
        cursor_signing_key=_ACTIVE_KEY,
        cursor_key_version=8,
        cursor_signing_key_previous=_PREVIOUS_KEY,
        cursor_key_version_previous=7,
    )
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(forged, tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_fm13_version_n_minus_2_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """FM13 leg (b): the window is exactly TWO versions deep — a token
    minted under version N-2 (its own then-valid key) fails closed as
    the same sanitized 400 once two rotations have passed."""
    _configure(monkeypatch, cursor_signing_key=_OLDEST_KEY, cursor_key_version=6)
    stale = encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT)
    _configure(
        monkeypatch,
        cursor_signing_key=_ACTIVE_KEY,
        cursor_key_version=8,
        cursor_signing_key_previous=_PREVIOUS_KEY,
        cursor_key_version_previous=7,
    )
    with pytest.raises(InvalidInputError, match="invalid member cursor"):
        decode_cursor(stale, tenant_id=TENANT, endpoint=ENDPOINT, entity="member")


def test_fm13_encode_mints_only_the_active_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FM13 leg (c): with a rotation window OPEN, new tokens still
    carry the ACTIVE version byte — the previous pair is decode-only."""
    _configure(
        monkeypatch,
        cursor_signing_key=_ACTIVE_KEY,
        cursor_key_version=8,
        cursor_signing_key_previous=_PREVIOUS_KEY,
        cursor_key_version_previous=7,
    )
    assert _raw(encode_cursor("GP-0042", tenant_id=TENANT, endpoint=ENDPOINT))[0] == 8


def test_fm13_boot_rejects_a_weak_previous_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """FM13 leg (d): the B13-R5 boot guard covers BOTH keys — a
    configured-but-short previous key aborts create_app (it would
    otherwise keep the whole window verifiable under guessable
    material). Falsifiable: drop the previous-key branch from
    ``assert_cursor_signing_key_configured`` and this boots."""
    _configure(
        monkeypatch,
        cursor_signing_key="x" * 32,
        cursor_signing_key_previous="short",
        cursor_key_version_previous=1,
        cursor_key_version=2,
    )
    with pytest.raises(RuntimeError, match="cursor_signing_key_previous"):
        create_app()


def test_fm13_boot_rejects_a_version_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """FM13 leg (d): previous and active pairs sharing one version
    byte would make the window ambiguous — boot refuses."""
    _configure(
        monkeypatch,
        cursor_signing_key="x" * 32,
        cursor_key_version=2,
        cursor_signing_key_previous="y" * 32,
        cursor_key_version_previous=2,
    )
    with pytest.raises(RuntimeError, match="must differ from cursor_key_version"):
        create_app()
