"""Environment-only configuration (MASTER_PROMPT gate 1.6: no literal secrets)."""

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
    # parsing; _split_origins converts it to a list after validation.
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
    # locations (gate 1.6; P13 blocker a).
    export_row_cap: int = 10_000
    export_batch_size: int = 500
    export_artifact_ttl_hours: int = 24
    export_npl_trend_months: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()
