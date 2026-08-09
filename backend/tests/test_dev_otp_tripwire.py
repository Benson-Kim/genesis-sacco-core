"""#35 W2 — DEV_OTP_DISPLAY deployment-config tripwire (no DB needed).

The dev-mode OTP display (settings.dev_otp_display) is FAIL-CLOSED and
must be enabled per-developer only — the flag must NEVER be baked into
a CI/CD or deployment configuration surface, or it silently reaches a
non-dev environment before the recorded pre-staging removal (#35).

This suite is a cheap grep-shaped guard over every config surface that
exists in the repository today (CI pipeline, workflow mirrors, and any
container/orchestration manifest that may appear later — the globs are
forward-looking on purpose). Falsifiable: add `DEV_OTP_DISPLAY: "1"`
to `.gitlab-ci.yml` (or any workflow/Dockerfile/compose/k8s file) and
the scan fails naming the offender.

Deliberately NOT gated on DATABASE_URL: a pure file scan must run on
every pipeline, including environments without a migrated database.
"""

import os
from pathlib import Path

import pytest

from genesis.api.app import create_app
from genesis.settings import Settings, assert_dev_otp_display_dev_only, get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Deployment/config surfaces the flag must never reach. Source and test
# files are intentionally out of scope: the settings definition and the
# behavioural suite legitimately name the variable.
_CONFIG_GLOBS = (
    ".gitlab-ci.yml",
    ".gitlab/**/*.yml",
    ".gitlab/**/*.yaml",
    ".github/**/*.yml",
    ".github/**/*.yaml",
    "Dockerfile*",
    "**/Dockerfile*",
    "docker-compose*",
    "**/docker-compose*",
    "k8s/**/*",
    "deploy/**/*",
    "helm/**/*",
    "*.env.example",
    "**/*.env.example",
)


def _config_surfaces() -> list[Path]:
    surfaces: set[Path] = set()
    for pattern in _CONFIG_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file() and ".git" not in path.parts:
                surfaces.add(path)
    return sorted(surfaces)


def test_dev_otp_display_is_absent_from_every_deployment_config_surface() -> None:
    surfaces = _config_surfaces()
    # Structural guard: the scan must actually see the CI pipeline file
    # — an empty scan would be a vacuously green tripwire.
    assert REPO_ROOT / ".gitlab-ci.yml" in surfaces
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in surfaces
        if "DEV_OTP_DISPLAY" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], f"DEV_OTP_DISPLAY must not reach deployment config: {offenders}"


# ---------------------------------------------------------------------------
# #35 enforced control: the flag is structurally incapable of leaving
# development. These legs convert the old "strip before staging"
# reminder into gates that FAIL the pipeline instead of relying on
# memory. No DB needed.
# ---------------------------------------------------------------------------


def test_flag_default_is_false_at_the_class() -> None:
    """CI gate: the DEFAULT can never silently become on. Falsifiable:
    flip `dev_otp_display: bool = False` to True and this fails on
    every pipeline before any environment is even consulted."""
    assert Settings.model_fields["dev_otp_display"].default is False


def test_guard_refuses_the_flag_outside_development_both_ways() -> None:
    """Fail-closed BOTH ways: on+non-dev refuses loudly; on+dev and
    off+non-dev boot fine (the flag stays usable for dev testers)."""
    for environment in ("staging", "production", "test"):
        with pytest.raises(RuntimeError, match="development-only"):
            assert_dev_otp_display_dev_only(Settings(environment=environment, dev_otp_display=True))
    # The permitted configurations raise nothing.
    assert_dev_otp_display_dev_only(Settings(environment="development", dev_otp_display=True))
    assert_dev_otp_display_dev_only(Settings(environment="staging", dev_otp_display=False))
    assert_dev_otp_display_dev_only(Settings(environment="production", dev_otp_display=False))


@pytest.fixture()
def _staging_with_flag():
    """Misconfigured deployment, exactly as an operator would produce
    it: both values arrive via the environment."""
    os.environ["ENVIRONMENT"] = "staging"
    os.environ["DEV_OTP_DISPLAY"] = "1"
    get_settings.cache_clear()
    yield
    os.environ.pop("ENVIRONMENT", None)
    os.environ.pop("DEV_OTP_DISPLAY", None)
    get_settings.cache_clear()


def test_create_app_refuses_to_boot_when_misconfigured(_staging_with_flag) -> None:
    """The guard is wired at the ONE boot chokepoint: create_app raises
    at startup — a misdeployed flag can never serve a request."""
    with pytest.raises(RuntimeError, match="development-only"):
        create_app()


@pytest.fixture()
def _development_with_flag():
    os.environ["ENVIRONMENT"] = "development"
    os.environ["DEV_OTP_DISPLAY"] = "1"
    get_settings.cache_clear()
    yield
    os.environ.pop("ENVIRONMENT", None)
    os.environ.pop("DEV_OTP_DISPLAY", None)
    get_settings.cache_clear()


def test_create_app_boots_with_the_flag_in_development(_development_with_flag) -> None:
    """The control never breaks the dev workflow it protects."""
    assert create_app() is not None
