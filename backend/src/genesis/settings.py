"""Environment-only configuration (no literal secrets)."""

import ipaddress
from functools import lru_cache

from pydantic import field_validator
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
    # Secondary pure-IP bucket for the auth rate guard: applies regardless
    # of the x-tenant-id header, so rotating header values cannot mint a
    # fresh bucket per request. Deliberately higher than the per-tenant
    # limit — it is a backstop, not the primary control. NOTE: behind a
    # reverse proxy, set trusted_proxy_ips (below) so the bucket keys on
    # the forwarded client IP instead of the proxy's own address.
    auth_rate_limit_ip_per_minute: int = 240
    # Comma-separated list of browser origins allowed to call this API.
    # Example: "http://localhost:3000,https://admin.example.com"
    # Stored as a plain string so pydantic-settings does not attempt JSON
    # parsing; cors_origins_list converts it to a list after validation.
    cors_origins: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _coerce_list(cls, v: object) -> str:
        """Accept a pre-split list (e.g. from tests) and join it back to a string."""
        if isinstance(v, list):
            return ",".join(str(i) for i in v)
        return str(v) if v is not None else ""

    @property
    def cors_origins_list(self) -> list[str]:
        """Return origins as a list, filtering out any blank entries."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Trusted reverse-proxy addresses for X-Forwarded-For resolution
    # (issue #13). Comma-separated IP addresses of the proxy hops this
    # deployment actually terminates behind (e.g. the Passenger host on
    # MochaHost). DEFAULT EMPTY = X-Forwarded-For is NEVER trusted and the
    # rate buckets key on the direct peer — today's safe behavior. Only
    # when the immediate peer is in this set does the guard walk the
    # forwarded chain (from the right) for the real client IP.
    trusted_proxy_ips: str = ""

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def _coerce_proxy_list(cls, v: object) -> str:
        """Accept a pre-split list (e.g. from tests) and join it back to a string."""
        if isinstance(v, list):
            return ",".join(str(i) for i in v)
        return str(v) if v is not None else ""

    @field_validator("trusted_proxy_ips")
    @classmethod
    def _validate_proxy_ips(cls, v: str) -> str:
        """FAIL CLOSED at settings load: a malformed trusted-proxy entry is a
        deployment error, never a silently-ignored one — a typo here must
        not quietly leave the deployment on shared-bucket behavior."""
        for entry in v.split(","):
            if entry.strip():
                ipaddress.ip_address(entry.strip())  # raises ValueError on garbage
        return v

    @property
    def trusted_proxy_ips_set(self) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Normalized trusted-proxy addresses (validated at settings load)."""
        return frozenset(
            ipaddress.ip_address(entry.strip())
            for entry in self.trusted_proxy_ips.split(",")
            if entry.strip()
        )

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
    # Idempotency replay retention (P13.17c / DSA-3).
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
    # ENFORCED CONTROL (#35, supersedes the old "strip before
    # staging" reminder): assert_dev_otp_display_dev_only below
    # REFUSES BOOT when this flag is on in any non-development
    # environment, so forgetting to strip it is impossible — the
    # deployment fails loudly instead of leaking OTPs.
    dev_otp_display: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_dev_otp_display_dev_only(settings: Settings | None = None) -> None:
    """Fail-closed BOOT guard (#35): the dev-mode OTP display must be
    IMPOSSIBLE to activate outside development.

    A truthy DEV_OTP_DISPLAY in any environment other than
    "development" is a DEPLOYMENT error and aborts startup here —
    never a silent OTP-disclosure surface in staging/production. This
    converts the old "strip before staging" removal reminder into an
    enforced control: the flag stays available to testers in dev and
    is structurally incapable of reaching anything else. Called by
    ``genesis.api.app.create_app`` before any router is wired (the
    assert_cursor_signing_key_configured posture).
    """
    resolved = settings if settings is not None else get_settings()
    if resolved.dev_otp_display and resolved.environment != "development":
        raise RuntimeError(
            "DEV_OTP_DISPLAY is enabled but ENVIRONMENT is "
            f"'{resolved.environment}' — the dev-mode OTP display is "
            "development-only and refuses to boot anywhere else"
        )


def assert_redis_configured_outside_dev(settings: Settings | None = None) -> None:
    """Fail-closed BOOT guard (#15): outside development the rate limiter
    must never silently degrade to its per-process in-memory fallback.

    With REDIS_URL empty, ``infrastructure.rate_limit`` counts PER
    PROCESS — under N workers the effective auth rate limit is N x the
    configured value, and the degradation is invisible: the deployment
    boots cleanly and *looks* rate-limited. A forgotten REDIS_URL is a
    DEPLOYMENT error and aborts startup here (the
    assert_dev_otp_display_dev_only / assert_cursor_signing_key_configured
    posture). Called by ``genesis.api.app.create_app`` before any router
    is wired.
    """
    resolved = settings if settings is not None else get_settings()
    if resolved.environment != "development" and not resolved.redis_url:
        raise RuntimeError(
            "REDIS_URL is empty but ENVIRONMENT is "
            f"'{resolved.environment}' — the auth rate limiter requires "
            "Redis outside development (the in-process fallback enforces "
            "per-worker limits only); refusing to boot"
        )
