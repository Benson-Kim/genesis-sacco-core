"""#34 — fail-closed destructive-seed guard for scripts/generate_dev_data.py.

The seeder DELETEs otp_challenges rows and plants a fixed, publicly-known
OTP code against whatever DATABASE_URL the environment carries — an
authentication-bypass primitive if that DSN is ever production. The guard
must therefore refuse to run unless the environment AFFIRMATIVELY proves
the target is a dev/test database (allowlist only — never a blocklist of
production hostnames):

  (a) no explicit ALLOW_DESTRUCTIVE_SEED=1 opt-in  -> refusal, even for a
      dev-named DSN;
  (b) opt-in present but the DSN is not provably dev/test (no dev/test/
      local marker in the database name, SEED_EXPECTED_DB_NAME mismatch,
      or any ambiguity: unset/unparseable DSN, missing database name)
      -> refusal;
  (c) opt-in AND an aligned DSN -> the guard admits the run.

Every case executes the REAL script entrypoint via runpy with
run_name="__main__" — the guard sits before the script's third-party
import block and before any psycopg/httpx connection, so these tests run
on every pipeline regardless of installed dev extras (faker/rich are not
in the backend lock) and can never touch a database. Falsifiable: delete
the guard block (or move it below the third-party imports) and every
refusal leg fails — the script would exit with the missing-package code
instead of the refusal code.

For leg (c) the unit context has no live API or seeded database, so
"normal run" is proven as: the guard logs its OK verdict and the script
proceeds PAST the guard (it then exits on the missing dev extras or the
deliberately unreachable API_BASE_URL — never with the refusal code).
"""

import logging
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_dev_data.py"

#: SEED_GUARD_REFUSAL_EXIT_CODE in the script (not importable without the
#: seeder's dev extras, so mirrored here; the wiring test below keeps the
#: two in sync).
REFUSAL_EXIT = 3

DEV_DSN = "postgresql+psycopg://genesis@localhost:5432/genesis_dev"
PROD_SHAPED_DSN = "postgresql+psycopg://genesis@db.internal:5432/genesis"

_GUARD_VARS = ("ALLOW_DESTRUCTIVE_SEED", "SEED_EXPECTED_DB_NAME")


def _run_seeder(monkeypatch, caplog, *, dsn, allow=None, expected=None) -> int:
    """Execute the script as __main__ with a controlled environment."""
    for var in _GUARD_VARS:
        monkeypatch.delenv(var, raising=False)
    if allow is not None:
        monkeypatch.setenv("ALLOW_DESTRUCTIVE_SEED", allow)
    if expected is not None:
        monkeypatch.setenv("SEED_EXPECTED_DB_NAME", expected)
    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn)
    # Safety net: on a machine with every dev extra installed, an admitted
    # run's next step is the API health check — point it at a dead port so
    # the script can never reach a real API (and thus never a database).
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
    caplog.set_level(logging.INFO)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    code = excinfo.value.code
    return code if isinstance(code, int) else 1


def test_refusal_exit_code_mirrors_the_script() -> None:
    """Keeps the mirrored constant honest without importing the module."""
    assert f"SEED_GUARD_REFUSAL_EXIT_CODE = {REFUSAL_EXIT}" in SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) refusal without the explicit opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow", [None, "", "0", "true", "yes", "1 "])
def test_refuses_without_explicit_opt_in(allow, monkeypatch, caplog) -> None:
    """Even a dev-named DSN is refused: absence of the exact "1" opt-in
    (unset, empty, truthy-looking variants) is absence of the signal."""
    code = _run_seeder(monkeypatch, caplog, dsn=DEV_DSN, allow=allow)
    assert code == REFUSAL_EXIT
    assert "seed guard: REFUSED" in caplog.text
    assert "seed guard: OK" not in caplog.text


# ---------------------------------------------------------------------------
# (b) refusal when the DSN is not provably dev/test, even WITH the opt-in
# ---------------------------------------------------------------------------


def test_refuses_non_dev_database_name_even_with_opt_in(monkeypatch, caplog) -> None:
    code = _run_seeder(monkeypatch, caplog, dsn=PROD_SHAPED_DSN, allow="1")
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
    dsn, expected, monkeypatch, caplog
) -> None:
    code = _run_seeder(monkeypatch, caplog, dsn=dsn, allow="1", expected=expected)
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
def test_refuses_any_ambiguous_dsn_even_with_opt_in(dsn, monkeypatch, caplog) -> None:
    """Fail closed: ambiguity is never resolved in favour of running."""
    code = _run_seeder(monkeypatch, caplog, dsn=dsn, allow="1")
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
def test_admits_the_run_when_opt_in_and_dsn_align(dsn, expected, monkeypatch, caplog) -> None:
    code = _run_seeder(monkeypatch, caplog, dsn=dsn, allow="1", expected=expected)
    assert code != REFUSAL_EXIT
    assert "seed guard: OK" in caplog.text
    assert "seed guard: REFUSED" not in caplog.text
