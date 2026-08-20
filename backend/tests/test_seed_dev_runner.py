"""#48 — fail-closed guard for the seed_dev.sql runner (scripts/seed_dev.py).

seed_dev.sql opens with `TRUNCATE TABLE ... CASCADE` across ~all tenant
tables against whatever DATABASE_URL the environment carries — and
TRUNCATE bypasses the append-only trigger on ledger_entries, so against
a production DSN this is unrecoverable ledger destruction. The runner is
the only documented way to invoke the SQL file and must refuse to run
unless the environment AFFIRMATIVELY proves the target is a dev/test
database (allowlist only — never a blocklist of production hostnames),
mirroring backend/tests/test_seed_guard.py's structure for #34:

  (a) no explicit ALLOW_DESTRUCTIVE_SEED=1 opt-in  -> refusal, even for a
      dev-named DSN;
  (b) opt-in present but the DSN is not provably dev/test (no dev/test/
      local marker in the database name, SEED_EXPECTED_DB_NAME mismatch,
      or any ambiguity: unset/unparseable DSN, missing database name)
      -> refusal;
  (c) opt-in AND an aligned DSN -> the guard admits the run.

Every case executes the REAL runner entrypoint via runpy with
run_name="__main__". psql is NEVER executed: every refusal leg runs with
an EMPTY PATH (a broken guard would surface as the psql-missing exit
code, not the refusal code — falsifiable), and the admission legs use
the runner's documented SEED_RUNNER_DRY_RUN=1 stub, which logs the exact
psql command and exits 0 without executing anything. A second admission
leg drops the dry-run flag but keeps the empty PATH: the run provably
proceeds PAST the guard (psql-missing exit code), still never touching a
database.
"""

import logging
import os
import re
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_dev.py"
SQL_FILE = Path(__file__).resolve().parents[1] / "scripts" / "seed_dev.sql"

#: Mirrors SEED_GUARD_REFUSAL_EXIT_CODE / PSQL_MISSING_EXIT_CODE in the
#: runner (kept honest by the wiring test below), matching #34's guard.
REFUSAL_EXIT = 3
PSQL_MISSING_EXIT = 127

DEV_DSN = "postgresql+psycopg://genesis@localhost:5432/genesis_dev"
PROD_SHAPED_DSN = "postgresql+psycopg://genesis@db.internal:5432/genesis"

_GUARD_VARS = ("ALLOW_DESTRUCTIVE_SEED", "SEED_EXPECTED_DB_NAME", "SEED_RUNNER_DRY_RUN")


def _run_runner(
    monkeypatch,
    caplog,
    tmp_path,
    *,
    dsn,
    allow=None,
    expected=None,
    dry_run=False,
) -> int:
    """Execute the runner as __main__ with a controlled environment."""
    for var in _GUARD_VARS:
        monkeypatch.delenv(var, raising=False)
    if allow is not None:
        monkeypatch.setenv("ALLOW_DESTRUCTIVE_SEED", allow)
    if expected is not None:
        monkeypatch.setenv("SEED_EXPECTED_DB_NAME", expected)
    if dry_run:
        monkeypatch.setenv("SEED_RUNNER_DRY_RUN", "1")
    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn)
    # Safety net: psql can never be found, let alone executed — an empty
    # PATH turns any exec attempt into the distinct psql-missing exit code.
    monkeypatch.setenv("PATH", str(tmp_path))
    caplog.set_level(logging.INFO)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    code = excinfo.value.code
    return code if isinstance(code, int) else 1


# ---------------------------------------------------------------------------
# Wiring: mirrored constants and the SQL-side secondary layer stay honest
# ---------------------------------------------------------------------------


def test_exit_codes_mirror_the_runner() -> None:
    """Keeps the mirrored constants honest without importing the module."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert f"SEED_GUARD_REFUSAL_EXIT_CODE = {REFUSAL_EXIT}" in source
    assert f"PSQL_MISSING_EXIT_CODE = {PSQL_MISSING_EXIT}" in source


def test_sql_file_carries_the_secondary_guard_before_truncate() -> None:
    """#48 layer 2: seed_dev.sql itself refuses on a non-dev
    current_database() — the DO guard must sit BEFORE the TRUNCATE so a
    direct psql invocation that bypasses the runner is still protected."""
    sql = SQL_FILE.read_text(encoding="utf-8")
    guard_at = sql.find("seed guard: REFUSED")
    truncate_at = sql.find("TRUNCATE TABLE")
    assert guard_at != -1, "seed_dev.sql lost its DO $$ guard block"
    assert truncate_at != -1
    assert guard_at < truncate_at
    assert "current_database()" in sql


# ---------------------------------------------------------------------------
# (a) refusal without the explicit opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow", [None, "", "0", "true", "yes", "1 "])
def test_refuses_without_explicit_opt_in(allow, monkeypatch, caplog, tmp_path) -> None:
    """Even a dev-named DSN is refused: absence of the exact "1" opt-in
    (unset, empty, truthy-looking variants) is absence of the signal."""
    code = _run_runner(monkeypatch, caplog, tmp_path, dsn=DEV_DSN, allow=allow)
    assert code == REFUSAL_EXIT
    assert "seed guard: REFUSED" in caplog.text
    assert "seed guard: OK" not in caplog.text


# ---------------------------------------------------------------------------
# (b) refusal when the DSN is not provably dev/test, even WITH the opt-in
# ---------------------------------------------------------------------------


def test_refuses_non_dev_database_name_even_with_opt_in(monkeypatch, caplog, tmp_path) -> None:
    code = _run_runner(monkeypatch, caplog, tmp_path, dsn=PROD_SHAPED_DSN, allow="1")
    assert code == REFUSAL_EXIT
    assert "seed guard: REFUSED" in caplog.text


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        # DSN names a non-dev database, expectation says otherwise.
        (PROD_SHAPED_DSN, "genesis_dev"),
        # Explicit expectation DOMINATES the marker heuristic: a dev-named
        # DSN that is not the declared target is still the wrong database.
        (DEV_DSN, "genesis"),
    ],
)
def test_refuses_expected_name_mismatch_even_with_opt_in(
    dsn, expected, monkeypatch, caplog, tmp_path
) -> None:
    code = _run_runner(monkeypatch, caplog, tmp_path, dsn=dsn, allow="1", expected=expected)
    assert code == REFUSAL_EXIT
    assert "seed guard: REFUSED" in caplog.text


@pytest.mark.parametrize(
    "dsn",
    [
        None,  # DATABASE_URL unset -- the implicit default is refused
        "",  # set but empty
        "postgresql://genesis@localhost:5432",  # no database name at all
        "postgresql://genesis@localhost:5432/",  # empty database name
        "host=localhost dbname=genesis_dev",  # libpq keyword form: unparseable
        "mysql://genesis@localhost:3306/genesis_dev",  # wrong scheme
    ],
)
def test_refuses_any_ambiguous_dsn_even_with_opt_in(dsn, monkeypatch, caplog, tmp_path) -> None:
    """Fail closed: ambiguity is never resolved in favour of running."""
    code = _run_runner(monkeypatch, caplog, tmp_path, dsn=dsn, allow="1")
    assert code == REFUSAL_EXIT
    assert "seed guard: REFUSED" in caplog.text


# ---------------------------------------------------------------------------
# (c) the guard admits the run when opt-in and DSN align
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (DEV_DSN, None),  # marker heuristic: name contains "dev"
        ("postgresql://genesis@localhost:5432/sacco_test", None),  # "test"
        ("postgresql://genesis@localhost:5432/genesis", "genesis"),  # exact expected name
    ],
)
def test_admits_the_run_when_opt_in_and_dsn_align(
    dsn, expected, monkeypatch, caplog, tmp_path
) -> None:
    """Dry-run stub: the guard admits, the runner logs the exact psql
    command it WOULD execute, and exits 0 without executing anything."""
    code = _run_runner(
        monkeypatch, caplog, tmp_path, dsn=dsn, allow="1", expected=expected, dry_run=True
    )
    assert code == 0
    assert "seed guard: OK" in caplog.text
    assert "seed guard: REFUSED" not in caplog.text
    assert "dry run: would execute" in caplog.text
    assert "seed_dev.sql" in caplog.text
    # The SQLAlchemy-shaped scheme is normalized to the libpq form.
    assert "postgresql+psycopg://" not in caplog.text


def test_admitted_run_without_dry_run_proceeds_past_the_guard(
    monkeypatch, caplog, tmp_path
) -> None:
    """Without the dry-run stub the run provably gets PAST the guard: with
    an empty PATH it exits with the distinct psql-missing code (never the
    refusal code, never an actual psql execution)."""
    code = _run_runner(monkeypatch, caplog, tmp_path, dsn=DEV_DSN, allow="1")
    assert code == PSQL_MISSING_EXIT
    assert "seed guard: OK" in caplog.text
    assert "seed guard: REFUSED" not in caplog.text
    assert "psql executable not found" in caplog.text


# ---------------------------------------------------------------------------
# Layer 2 executed for real: the DO $$ guard block from seed_dev.sql runs
# against the suite database (rolled back, never the TRUNCATE) so its SQL
# is verified, not just text-matched. DB-backed — runs on every CI
# pipeline; skipped only where no database exists (local sandboxes).
# ---------------------------------------------------------------------------

_DB_MARKERS = ("dev", "test", "local")


def _sql_guard_block() -> str:
    match = re.search(r"DO \$guard\$.*?\$guard\$;", SQL_FILE.read_text(encoding="utf-8"), re.S)
    assert match, "seed_dev.sql lost its DO $guard$ block"
    return match.group(0)


def _db_connection():
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL not set (DB-backed legs run in CI)")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    try:
        return psycopg.connect(dsn)
    except psycopg.OperationalError:
        pytest.skip("database unreachable (DB-backed legs run in CI)")


def test_sql_guard_admits_marker_named_database() -> None:
    with _db_connection() as conn:
        (db,) = conn.execute("SELECT current_database()").fetchone()
        if not any(marker in db.lower() for marker in _DB_MARKERS):
            pytest.skip("suite database name carries no dev/test/local marker")
        conn.execute(_sql_guard_block())  # must not raise
        conn.rollback()


def test_sql_guard_refuses_on_expectation_mismatch_despite_marker() -> None:
    """The handed-down expectation DOMINATES the marker heuristic: even the
    marker-named suite database is refused when it is not the declared
    target (mirrors the runner's SEED_EXPECTED_DB_NAME semantics)."""
    psycopg = pytest.importorskip("psycopg")
    with _db_connection() as conn:
        conn.execute(
            "SELECT set_config('seed.expected_db_name', 'definitely_not_this_db', false)"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="seed guard: REFUSED"):
            conn.execute(_sql_guard_block())
        conn.rollback()


def test_sql_guard_admits_exact_expected_name() -> None:
    with _db_connection() as conn:
        (db,) = conn.execute("SELECT current_database()").fetchone()
        conn.execute("SELECT set_config('seed.expected_db_name', %s, false)", (db,))
        conn.execute(_sql_guard_block())  # must not raise
        conn.rollback()
