"""Guarded runner for scripts/seed_dev.sql (#48).

This runner is the ONLY documented way to execute seed_dev.sql. The SQL
file opens with `TRUNCATE TABLE ... CASCADE` across ~all tenant tables —
and TRUNCATE bypasses the append-only trigger on ledger_entries (that
trigger guards UPDATE/DELETE, not TRUNCATE). Against a production DSN
that is unrecoverable ledger destruction, so a plain
`psql "$DATABASE_URL" -f scripts/seed_dev.sql` with zero validation is
never acceptable.

Run from the backend/ directory:

    ALLOW_DESTRUCTIVE_SEED=1 python scripts/seed_dev.py

SAFETY (#48, same fail-closed posture as #34): the runner refuses to
invoke psql unless the environment affirmatively proves the target is a
dev/test database:

  ALLOW_DESTRUCTIVE_SEED  REQUIRED, must be exactly "1" (explicit opt-in)
  DATABASE_URL            REQUIRED explicitly (no implicit default), and its
                          database name must contain dev/test/local, OR
  SEED_EXPECTED_DB_NAME   optional exact database name the DSN must match
                          (when set, it dominates the marker heuristic)
  SEED_RUNNER_DRY_RUN     optional; when exactly "1", an ADMITTED run logs
                          the psql command it would execute and exits 0
                          without executing anything (used by the tests)

Any ambiguity is a hard exit BEFORE psql is invoked. ALLOWLIST ONLY:
there is deliberately no blocklist of production hostnames, and
localhost is NOT sufficient either (tunnels and port forwards make
hostnames meaningless).

NOTE (#34/#48 consolidation): the guard block below deliberately mirrors
the self-contained guard in scripts/generate_dev_data.py (MR !33, issue
#34) — same environment-variable names, same semantics, same refusal
exit code — WITHOUT importing from that file, so the two changes merge
independently. Once both are on develop, the duplicated guard should be
consolidated into a single shared module (tracked by #34/#48).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

# ---------------------------------------------------------------------------
# FAIL-CLOSED destructive-seed guard (#48, mirroring #34)
#
# seed_dev.sql TRUNCATEs ~every tenant table (CASCADE) against WHATEVER
# DSN the environment carries, then seeds well-known dev users/roles.
# The guard refuses to run unless the environment AFFIRMATIVELY proves
# the target is a development/test database:
#
#   1. ALLOW_DESTRUCTIVE_SEED must be exactly "1" (explicit opt-in), AND
#   2. DATABASE_URL must be set explicitly (no implicit default), AND
#   3. the target database name must either
#        - exactly match SEED_EXPECTED_DB_NAME when that is set (the
#          explicit expectation dominates the heuristic), or
#        - contain a dev/test/local marker.
#
# ALLOWLIST ONLY: there is deliberately no blocklist of production
# hostnames, and localhost is NOT sufficient either (tunnels and port
# forwards make hostnames meaningless). Any ambiguity -- missing opt-in,
# unset or unparseable DSN, missing database name, name mismatch -- is a
# hard sys.exit(SEED_GUARD_REFUSAL_EXIT_CODE) BEFORE psql is invoked.
# ---------------------------------------------------------------------------
SEED_GUARD_REFUSAL_EXIT_CODE = 3
PSQL_MISSING_EXIT_CODE = 127
_DEV_DB_NAME_MARKERS = ("dev", "test", "local")
_DSN_SCHEMES = ("postgres", "postgresql", "postgresql+psycopg")

SQL_FILE = Path(__file__).resolve().parent / "seed_dev.sql"

log = logging.getLogger(Path(__file__).stem)


def _seed_target_db_name(dsn: str) -> str | None:
    """Database name from a URL-shaped DSN, or None when ambiguous."""
    try:
        parsed = urllib.parse.urlsplit(dsn)
    except ValueError:
        return None
    if parsed.scheme not in _DSN_SCHEMES:
        return None
    name = urllib.parse.unquote(parsed.path.lstrip("/"))
    if not name or "/" in name:
        return None
    return name


def _seed_guard_verdict(environ: Mapping[str, str]) -> tuple[bool, str]:
    """(allowed, reason) -- pure, never opens a connection."""
    if environ.get("ALLOW_DESTRUCTIVE_SEED") != "1":
        return False, "ALLOW_DESTRUCTIVE_SEED=1 is not set (explicit opt-in required)"
    dsn = environ.get("DATABASE_URL", "")
    if not dsn:
        return False, "DATABASE_URL is not set (the guard refuses implicit defaults)"
    name = _seed_target_db_name(dsn)
    if name is None:
        return False, "target database name could not be determined from DATABASE_URL"
    expected = environ.get("SEED_EXPECTED_DB_NAME")
    if expected is not None:
        if name == expected:
            return True, f"database {name!r} matches SEED_EXPECTED_DB_NAME"
        return False, f"database {name!r} does not match SEED_EXPECTED_DB_NAME={expected!r}"
    if any(marker in name.lower() for marker in _DEV_DB_NAME_MARKERS):
        return True, f"database {name!r} carries a dev/test/local marker"
    return False, (
        f"database {name!r} carries no dev/test/local marker and SEED_EXPECTED_DB_NAME is not set"
    )


def _enforce_seed_guard() -> None:
    """Hard-exit unless the target DSN is provably dev/test (#48)."""
    allowed, reason = _seed_guard_verdict(os.environ)
    if allowed:
        log.info("seed guard: OK -- %s", reason)
        return
    log.error(
        "seed guard: REFUSED -- %s. seed_dev.sql TRUNCATEs every tenant table "
        "(CASCADE, bypassing the append-only ledger_entries trigger) against "
        "the DATABASE_URL target; it only runs when the environment "
        "affirmatively proves a dev/test database. Set ALLOW_DESTRUCTIVE_SEED=1 "
        "AND point DATABASE_URL at a database whose name contains "
        "dev/test/local (or set SEED_EXPECTED_DB_NAME to the exact intended "
        "database name).",
        reason,
    )
    sys.exit(SEED_GUARD_REFUSAL_EXIT_CODE)


def _psql_dsn(dsn: str) -> str:
    """Normalize a SQLAlchemy-shaped DSN to the libpq form psql accepts."""
    if dsn.startswith("postgresql+psycopg://"):
        return "postgresql://" + dsn[len("postgresql+psycopg://") :]
    return dsn


def _pgoptions_escape(value: str) -> str:
    """Escape a value for libpq's space-separated PGOPTIONS string."""
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Layer 1 (#48 primary): refuse BEFORE psql is ever invoked.
    _enforce_seed_guard()

    dsn = _psql_dsn(os.environ["DATABASE_URL"])
    cmd = ["psql", dsn, "--set=ON_ERROR_STOP=1", "-f", str(SQL_FILE)]

    # Layer 2 (#48 secondary) lives INSIDE seed_dev.sql: a leading DO $$
    # block that RAISEs unless current_database() is provably dev/test.
    # When the runner admitted the target via SEED_EXPECTED_DB_NAME (a
    # name without a dev/test/local marker), hand that expectation to the
    # SQL layer through a session GUC so both layers stay coherent.
    env = dict(os.environ)
    expected = env.get("SEED_EXPECTED_DB_NAME")
    if expected:
        pgoptions = env.get("PGOPTIONS", "")
        pgoptions = (pgoptions + " " if pgoptions else "") + (
            "-c seed.expected_db_name=" + _pgoptions_escape(expected)
        )
        env["PGOPTIONS"] = pgoptions

    if os.environ.get("SEED_RUNNER_DRY_RUN") == "1":
        log.info("dry run: would execute %s", " ".join(cmd))
        return 0

    psql = shutil.which("psql")
    if psql is None:
        log.error("psql executable not found on PATH; cannot run seed_dev.sql")
        return PSQL_MISSING_EXIT_CODE
    cmd[0] = psql

    log.info("executing %s", " ".join(cmd))
    completed = subprocess.run(cmd, env=env, check=False)  # noqa: S603
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
