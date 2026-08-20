"""Environment-only configuration (no literal secrets)."""

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
    # Device-attestation enforcement mode for the member OTP request
    # route (#29): "off" → "log-only" → "enforce", in rollout order.
    # log-only by default so early app builds are observed, not
    # bricked; ENFORCE is the required posture before the first PAID
    # SMS gateway goes live (#18). Valid values are pinned by
    # application.device_attestation.ATTESTATION_MODES and
    # boot-validated (fail-closed on a typo) by
    # assert_member_attestation_mode_valid, called from
    # api.app.create_app — the assert_dev_otp_display_dev_only posture.
    # Env-only, like everything in this file: MEMBER_ATTESTATION_MODE.
    member_attestation_mode: str = "log-only"


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
