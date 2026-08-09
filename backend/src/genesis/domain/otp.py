"""OTP policy mirroring the prototype login gate (the house doctrine 1.6).

6 digits, at most 5 attempts, 5-minute TTL, single-use, constant-time
compare. Pure logic only: persistence, delivery, and clocks live outside.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
from datetime import datetime

OTP_LENGTH = 6
OTP_MAX_ATTEMPTS = 5
OTP_TTL_SECONDS = 300


class OtpResult(enum.StrEnum):
    OK = "ok"
    CONSUMED = "consumed"
    LOCKED = "locked"
    EXPIRED = "expired"
    MISMATCH = "mismatch"


def hash_code(code: str, *, salt: str, pepper: str) -> str:
    """Keyed hash of an OTP code; raw codes are never stored or logged."""
    return hmac.new(pepper.encode(), f"{salt}:{code}".encode(), hashlib.sha256).hexdigest()


def evaluate_challenge(
    *,
    stored_hash: str,
    presented_hash: str,
    attempts: int,
    expires_at: datetime,
    consumed_at: datetime | None,
    now: datetime,
) -> OtpResult:
    """Single gatekeeper for OTP acceptance; the compare is constant-time."""
    if consumed_at is not None:
        return OtpResult.CONSUMED
    if attempts >= OTP_MAX_ATTEMPTS:
        return OtpResult.LOCKED
    if now >= expires_at:
        return OtpResult.EXPIRED
    if not hmac.compare_digest(stored_hash, presented_hash):
        return OtpResult.MISMATCH
    return OtpResult.OK
