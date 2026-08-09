"""Environment-only configuration (no literal secrets)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All values come from the environment; nothing is hardcoded."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: str = ""
    redis_url: str = ""
    environment: str = "development"
    jwt_signing_key: str = ""
    otp_pepper: str = ""
    auth_rate_limit_per_minute: int = 60
    # Export configuration (P13): resolved exclusively server-side —
    # request bodies never carry formats, row limits, or storage
    # locations (least disclosure; the blocker-a precedent).
    export_row_cap: int = 10_000
    export_batch_size: int = 500
    export_artifact_ttl_hours: int = 24
    export_npl_trend_months: int = 6
    # Dashboard configuration: the monthly-series window and
    # the guarantor-list size are server-resolved — the endpoint takes
    # no caller input (v1.1 rule 1) and every scan stays bounded
    # (scalability). Hard caps live in application.dashboard.
    dashboard_series_months: int = 6
    dashboard_guarantor_cap: int = 20
    # Idempotency replay retention (P13.17c / DSA-3): how long a
    # claimed key replays its stored response. Server config ONLY
    # (v1.1 rule 1) — no request carries it; the middleware sets
    # expires_at from this value on every claim, and the value must
    # match the 0029 column default's compatibility floor (24h) unless
    # deliberately re-tuned per environment.
    idempotency_retention_hours: int = 24
    # Opaque keyset cursor signing:
    # environment-only HMAC secret (the jwt_signing_key pattern — no
    # literal secrets, least disclosure) plus the active key-version byte
    # (1-255). Rotation (review B13-R10, dual-version window): deploy
    # the NEW key/version as the active pair and demote the old pair
    # to *_previous — decode accepts BOTH versions during the deploy
    # window (in-flight cursors keep working); encode mints ONLY the
    # active version. Retire the window by clearing the previous pair;
    # any older version (N-2) fails closed as a sanitized 400
    # (cursors are short-lived pagination state).
    # LENGTH REQUIREMENT: at least 32 bytes of key
    # material — an HMAC-SHA256 key should be no shorter than the
    # digest (RFC 2104). Boot FAILS CLOSED on an empty/short active
    # key AND on a configured-but-weak previous key or a version
    # collision (application.pagination.assert_cursor_signing_key_configured,
    # called from api.app.create_app) — never a first-decode surprise.
    cursor_signing_key: str = ""
    cursor_key_version: int = 1
    # Previous rotation pair: EMPTY key = single-key mode (no window).
    cursor_signing_key_previous: str = ""
    cursor_key_version_previous: int = 0
    # DEV-ONLY OTP display: SMS/email delivery is not
    # built yet, so testers need the OTP on screen. FAIL-CLOSED: off
    # by default; enabling requires an explicit DEV_OTP_DISPLAY env
    # value in a dev environment. The OTP is returned in the
    # /auth/otp/request response ONLY — it is never logged.
    # REMOVAL NOTE: this flag and its api/auth.py consumer MUST be
    # removed before staging.
    dev_otp_display: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
