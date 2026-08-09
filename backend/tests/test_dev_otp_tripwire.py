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

from pathlib import Path

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
