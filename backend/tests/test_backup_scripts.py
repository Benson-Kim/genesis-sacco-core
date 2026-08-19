"""Unit tests for the backup/restore-drill scripts (no live DB needed).

scripts/backup_db.py and scripts/verify_restore.py are stdlib-only
one-shot cron entrypoints sharing scripts/backup_common.py (see their
module docstrings); everything here exercises the pure decision logic —
retention selection, filename rotation, URL/credential handling,
scratch-DB naming guards, sanity-gate evaluation, ignored-error budget
parsing and fail-closed env validation.

Mocking policy (MR !5 review): PostgreSQL SEMANTICS are never mocked —
a mocked pg_dump proves nothing about whether a dump restores, and the
weekly drill exercises that operationally. What the "sweep" tests DO
fake is the subprocess boundary itself (backup_common.run), in order
to assert the argv/env CONTRACT this code hands to the OS: the
database password must appear in no argv (world-readable via
/proc/<pid>/cmdline on shared hosting) and must travel only via
PGPASSWORD; the encryption key must appear only as an env: reference.
That contract is our decision logic, and findings B2/B3 were exactly
a wrong decision there.

Boundary oracles are hand-computed in comments, never captured from
the implementation (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# The cron scripts live outside the installed package on purpose
# (stdlib-only DR tooling); import them by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backup_common
import backup_db
import verify_restore

_KEY = "k" * 64  # what secrets.token_hex(32) produces: 64 hex chars
_URL = "postgresql+psycopg://genesis@localhost:5432/genesis"
# A URL carrying a credential, for the no-password-in-argv sweeps.
_LEAKABLE = "s3kr3t-drill-pw"
_PW_URL = f"postgresql+psycopg://genesis:{_LEAKABLE}@localhost:5432/genesis"


# --- shared constants and crypto-parameter derivation ---------------------


def test_filename_constants() -> None:
    # The drill locates files the backup script wrote; the naming
    # contract lives in exactly one module now.
    assert backup_common.BACKUP_PREFIX == "genesis-"
    assert backup_common.BACKUP_SUFFIX == ".dump.enc"
    assert backup_common.TIMESTAMP_FORMAT == "%Y%m%dT%H%M%SZ"


def test_decrypt_args_derived_from_encrypt_args() -> None:
    # Hand-written oracle: decrypt = -d + encrypt minus -salt (openssl
    # reads the salt from the file header). The lists live in ONE
    # module and DECRYPT_ARGS is computed, so they cannot drift — this
    # pins the computed result against the documented manual command.
    assert backup_common.ENCRYPT_ARGS == [
        "-aes-256-cbc",
        "-md",
        "sha256",
        "-pbkdf2",
        "-iter",
        "600000",
        "-salt",
    ]
    assert backup_common.DECRYPT_ARGS == [
        "-d",
        "-aes-256-cbc",
        "-md",
        "sha256",
        "-pbkdf2",
        "-iter",
        "600000",
    ]
    assert "-salt" not in backup_common.DECRYPT_ARGS


def test_scripts_do_not_require_python_311() -> None:
    # Finding B4: the DR claim is "any python3 >= 3.8 can run these".
    # datetime.UTC is 3.11-only; keep it out of the DR scripts forever.
    scripts_dir = Path(backup_common.__file__).resolve().parent
    for name in ("backup_common.py", "backup_db.py", "verify_restore.py"):
        source = (scripts_dir / name).read_text()
        assert "from datetime import UTC" not in source, name
        assert "datetime.UTC" not in source, name


# --- URL / credential handling (findings B2 and B3) ------------------------


def test_libpq_url_strips_sqlalchemy_driver() -> None:
    assert (
        backup_common.libpq_url("postgresql+psycopg://user@h:5432/db")
        == "postgresql://user@h:5432/db"
    )
    assert backup_common.libpq_url("postgresql+asyncpg://u@h/db") == "postgresql://u@h/db"


def test_libpq_url_passthrough() -> None:
    # Already-plain URLs and non-URL strings come back untouched.
    assert backup_common.libpq_url("postgresql://user@h:5432/db") == "postgresql://user@h:5432/db"
    assert backup_common.libpq_url("not-a-url") == "not-a-url"


def test_connection_args_splits_password_out_of_the_url() -> None:
    safe, password = backup_common.connection_args(_PW_URL)
    # Hand-written expectation: driver marker stripped AND password gone.
    assert safe == "postgresql://genesis@localhost:5432/genesis"
    assert password == _LEAKABLE
    assert _LEAKABLE not in safe


def test_connection_args_without_password() -> None:
    safe, password = backup_common.connection_args(_URL)
    assert safe == "postgresql://genesis@localhost:5432/genesis"
    assert password is None


def test_connection_args_decodes_percent_encoded_password() -> None:
    # PGPASSWORD expects the raw password, not the URL encoding.
    result = backup_common.connection_args("postgresql://u:p%40ss%21@h:5/db")
    assert result == ("postgresql://u@h:5/db", "p@ss!")


def test_child_env_carries_password_and_pins_locale() -> None:
    env = backup_common.child_env("pw")
    assert env["PGPASSWORD"] == "pw"
    # LC_ALL=C keeps pg_restore's "errors ignored on restore:" line
    # English so the drill's budget regex is deterministic.
    assert env["LC_ALL"] == "C"
    assert "PGPASSWORD" not in backup_common.child_env(None)


def test_redact_url_masks_the_password() -> None:
    redacted = backup_common.redact_url(_PW_URL)
    assert _LEAKABLE not in redacted
    assert "***" in redacted
    assert "genesis" in redacted  # host/db stay useful for debugging
    # No password → nothing to mask, URL stays informative.
    assert "localhost" in backup_common.redact_url(_URL)


def test_database_name_error_never_leaks_the_password() -> None:
    # Finding B2: this exact ConfigError ends up verbatim in the cron
    # log's RESTORE_CHECK FAILURE line.
    with pytest.raises(verify_restore.ConfigError, match="no database name") as excinfo:
        verify_restore.database_name(f"postgresql://genesis:{_LEAKABLE}@localhost:5432/")
    assert _LEAKABLE not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_database_name_extraction() -> None:
    assert verify_restore.database_name(_URL) == "genesis"


def test_replace_database_swaps_only_the_path() -> None:
    url = verify_restore.replace_database(_URL, "genesis_restore_check")
    assert url == "postgresql://genesis@localhost:5432/genesis_restore_check"


# --- filename round-trip ---------------------------------------------------


def test_backup_filename_roundtrip() -> None:
    # 2026-08-18 01:30:00 UTC → hand-written expected name.
    ts = datetime(2026, 8, 18, 1, 30, 0)
    name = backup_common.backup_filename(ts)
    assert name == "genesis-20260818T013000Z.dump.enc"
    assert backup_common.parse_backup_timestamp(name) == ts
    # Both scripts re-export the shared parser (import surface pinned).
    assert backup_db.parse_backup_timestamp(name) == ts
    assert verify_restore.parse_backup_timestamp(name) == ts


@pytest.mark.parametrize(
    "name",
    [
        "genesis-20260818T013000Z.dump.enc.plain.tmp",  # in-flight temp file
        "genesis-20260818T013000Z.dump.enc.drill.tmp",  # drill temp file
        "genesis-20260818T013000Z.dump",  # unencrypted
        "other-20260818T013000Z.dump.enc",  # foreign prefix
        "genesis-2026-08-18.dump.enc",  # wrong stamp format
        "genesis-.dump.enc",
        "notes.txt",
    ],
)
def test_parse_backup_timestamp_rejects_foreign_names(name: str) -> None:
    assert backup_common.parse_backup_timestamp(name) is None


# --- retention selection (ISO-week tier — finding M1) ----------------------


def _daily_names(start: datetime, days: int) -> list[str]:
    return [backup_common.backup_filename(start + timedelta(days=i)) for i in range(days)]


def test_select_prunable_keeps_everything_when_under_budget() -> None:
    names = _daily_names(datetime(2026, 8, 1, 1, 30), 5)
    assert backup_db.select_prunable(names, daily_keep=7, weekly_keep=4) == []


def test_select_prunable_daily_and_iso_week_tiers() -> None:
    # 30 nightly dumps 2026-07-20 .. 2026-08-18 (01:30 UTC each).
    # Hand-computed: daily_keep=7 keeps Aug 12..18. The dumps span ISO
    # weeks starting Mon Jul 20, Jul 27, Aug 3, Aug 10, Aug 17;
    # weekly_keep=4 keeps the newest dump of the 4 newest weeks:
    # Aug 18 (week of Aug 17), Aug 16 (week of Aug 10), Aug 9 (week of
    # Aug 3), Aug 2 (week of Jul 27) — the first two already sit in the
    # daily tier. Survivors: Aug 12..18 + Aug 9 + Aug 2 = 9 → 21 pruned.
    start = datetime(2026, 7, 20, 1, 30)
    names = _daily_names(start, 30)
    doomed = backup_db.select_prunable(names, daily_keep=7, weekly_keep=4)

    expected_kept = {
        backup_common.backup_filename(datetime(2026, 8, d, 1, 30)) for d in range(12, 19)
    } | {
        backup_common.backup_filename(datetime(2026, 8, 2, 1, 30)),
        backup_common.backup_filename(datetime(2026, 8, 9, 1, 30)),
    }
    assert set(names) - set(doomed) == expected_kept
    assert len(doomed) == 21


def test_weekly_tier_survives_nairobi_cron_utc_stamp_shift() -> None:
    # Finding M1, the falsifying scenario: a weekly cron at 01:30
    # Sunday NAIROBI time (UTC+3) stamps files at 22:30 SATURDAY UTC.
    # A "kept if taken on Sunday" classifier (the old rule) matches
    # none of these files and silently never populates the weekly tier.
    # ISO-week bucketing must keep one dump per calendar week anyway.
    # Ten Saturday-UTC stamps, hand-picked: 2026-06-13 .. 2026-08-15.
    saturdays = [datetime(2026, 6, 13, 22, 30) + timedelta(days=7 * i) for i in range(10)]
    assert all(ts.isoweekday() == 6 for ts in saturdays)  # NOT Sunday
    names = [backup_common.backup_filename(ts) for ts in saturdays]

    doomed = backup_db.select_prunable(names, daily_keep=2, weekly_keep=4)

    # Hand-computed: daily tier keeps Aug 15 + Aug 8; weekly tier keeps
    # the newest dump of the 4 newest ISO weeks → Aug 15, Aug 8, Aug 1,
    # Jul 25. Union = 4 survivors, 6 pruned (Jun 13..Jul 18).
    survivors = set(names) - set(doomed)
    assert survivors == {
        backup_common.backup_filename(datetime(2026, 8, 15, 22, 30)),
        backup_common.backup_filename(datetime(2026, 8, 8, 22, 30)),
        backup_common.backup_filename(datetime(2026, 8, 1, 22, 30)),
        backup_common.backup_filename(datetime(2026, 7, 25, 22, 30)),
    }
    assert len(doomed) == 6


def test_weekly_tier_keeps_only_newest_dump_within_a_week() -> None:
    # Three dumps inside one ISO week (Mon Aug 10 .. Wed Aug 12) plus
    # one the week before: weekly_keep=2 must keep Aug 12 (newest of
    # its week) and Aug 5 — never two dumps from the same week.
    names = [
        backup_common.backup_filename(datetime(2026, 8, 10, 1, 30)),
        backup_common.backup_filename(datetime(2026, 8, 11, 1, 30)),
        backup_common.backup_filename(datetime(2026, 8, 12, 1, 30)),
        backup_common.backup_filename(datetime(2026, 8, 5, 1, 30)),
    ]
    doomed = backup_db.select_prunable(names, daily_keep=1, weekly_keep=2)
    survivors = set(names) - set(doomed)
    assert survivors == {
        backup_common.backup_filename(datetime(2026, 8, 12, 1, 30)),
        backup_common.backup_filename(datetime(2026, 8, 5, 1, 30)),
    }


def test_select_prunable_weekly_zero_keeps_only_daily_tier() -> None:
    start = datetime(2026, 7, 20, 1, 30)
    names = _daily_names(start, 30)
    doomed = backup_db.select_prunable(names, daily_keep=7, weekly_keep=0)
    assert len(names) - len(doomed) == 7


def test_select_prunable_never_touches_foreign_files() -> None:
    names = [
        *(_daily_names(datetime(2026, 7, 1, 1, 30), 20)),
        "notes.txt",
        "genesis-20260701T013000Z.dump.enc.plain.tmp",
    ]
    doomed = backup_db.select_prunable(names, daily_keep=1, weekly_keep=0)
    assert "notes.txt" not in doomed
    assert "genesis-20260701T013000Z.dump.enc.plain.tmp" not in doomed


def test_prune_backups_deletes_on_disk(tmp_path: Path) -> None:
    for i in range(10):
        name = backup_common.backup_filename(datetime(2026, 8, 1 + i, 1, 30))
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "keepme.txt").write_bytes(b"x")
    doomed = backup_db.prune_backups(tmp_path, daily_keep=3, weekly_keep=0)
    survivors = {p.name for p in tmp_path.iterdir()}
    assert len(doomed) == 7
    assert "keepme.txt" in survivors
    # Newest three (Aug 8, 9, 10) survive.
    assert survivors == {
        "keepme.txt",
        "genesis-20260808T013000Z.dump.enc",
        "genesis-20260809T013000Z.dump.enc",
        "genesis-20260810T013000Z.dump.enc",
    }


def test_latest_backup_picks_newest_and_ignores_junk() -> None:
    names = [
        "genesis-20260810T013000Z.dump.enc",
        "genesis-20260818T013000Z.dump.enc",
        "genesis-20260814T013000Z.dump.enc",
        "genesis-20260818T013000Z.dump.enc.drill.tmp",
        "notes.txt",
    ]
    assert verify_restore.latest_backup(names) == "genesis-20260818T013000Z.dump.enc"
    assert verify_restore.latest_backup(["notes.txt"]) is None
    assert verify_restore.latest_backup([]) is None


# --- env validation: both scripts must fail loudly, never default ---------


def test_backup_config_requires_database_url() -> None:
    with pytest.raises(backup_db.ConfigError, match="DATABASE_URL"):
        backup_db.load_config({"BACKUP_ENCRYPTION_KEY": _KEY})


def test_backup_config_requires_encryption_key() -> None:
    # The whole point: an unset key must abort, never write plaintext.
    with pytest.raises(backup_db.ConfigError, match="BACKUP_ENCRYPTION_KEY"):
        backup_db.load_config({"DATABASE_URL": _URL})


def test_backup_config_rejects_short_key() -> None:
    with pytest.raises(backup_db.ConfigError, match="shorter than 32"):
        backup_db.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": "hunter2"})


def test_backup_config_defaults() -> None:
    cfg = backup_db.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY})
    assert cfg.retention_daily == 7
    assert cfg.retention_weekly == 4
    assert cfg.timeout_seconds == 3600
    assert cfg.backup_dir == Path("~/backups/db").expanduser()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("BACKUP_RETENTION_DAILY", "0"),  # keeping zero dumps is not a policy
        ("BACKUP_RETENTION_DAILY", "seven"),
        ("BACKUP_RETENTION_WEEKLY", "-1"),
        ("BACKUP_TIMEOUT_SECONDS", "0"),
    ],
)
def test_backup_config_rejects_bad_numbers(var: str, value: str) -> None:
    env = {"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY, var: value}
    with pytest.raises(backup_db.ConfigError, match=var):
        backup_db.load_config(env)


def test_backup_config_overrides() -> None:
    cfg = backup_db.load_config(
        {
            "DATABASE_URL": _URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": "/var/backups/genesis",
            "BACKUP_RETENTION_DAILY": "14",
            "BACKUP_RETENTION_WEEKLY": "8",
            "BACKUP_TIMEOUT_SECONDS": "600",
        }
    )
    assert cfg.backup_dir == Path("/var/backups/genesis")
    assert cfg.retention_daily == 14
    assert cfg.retention_weekly == 8
    assert cfg.timeout_seconds == 600


def test_drill_config_requires_key_and_url() -> None:
    with pytest.raises(verify_restore.ConfigError, match="DATABASE_URL"):
        verify_restore.load_config({"BACKUP_ENCRYPTION_KEY": _KEY})
    with pytest.raises(verify_restore.ConfigError, match="BACKUP_ENCRYPTION_KEY"):
        verify_restore.load_config({"DATABASE_URL": _URL})
    with pytest.raises(verify_restore.ConfigError, match="shorter than 32"):
        verify_restore.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": "short"})


def test_drill_config_defaults() -> None:
    cfg = verify_restore.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY})
    assert cfg.scratch_db == "genesis_restore_check"
    assert cfg.precreated is False
    assert cfg.max_ignored_errors == 0
    # Fail-closed by default: a hollow restore (0 financial rows) must
    # not pass the drill unless an operator explicitly opts out.
    assert cfg.min_rows == 1


def test_drill_config_min_rows_zero_is_explicit_opt_in() -> None:
    cfg = verify_restore.load_config(
        {"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY, "RESTORE_CHECK_MIN_ROWS": "0"}
    )
    assert cfg.min_rows == 0
    with pytest.raises(verify_restore.ConfigError, match="RESTORE_CHECK_MIN_ROWS"):
        verify_restore.load_config(
            {"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY, "RESTORE_CHECK_MIN_ROWS": "-1"}
        )


# --- scratch DB naming: the drill must never aim at the live database -----


def test_scratch_db_name_default_suffix() -> None:
    assert verify_restore.scratch_db_name(_URL, "_restore_check", "") == "genesis_restore_check"


def test_scratch_db_name_explicit_override() -> None:
    assert verify_restore.scratch_db_name(_URL, "_restore_check", "drill_db") == "drill_db"


def test_scratch_db_name_refuses_live_database() -> None:
    # The drill DROPs the scratch DB — pointing it at the live one via
    # an override must be refused outright.
    with pytest.raises(verify_restore.ConfigError, match="equals the live database"):
        verify_restore.scratch_db_name(_URL, "_restore_check", "genesis")


@pytest.mark.parametrize("bad", ["Bad-Name", 'x"; DROP DATABASE genesis; --', "1abc", "a b"])
def test_scratch_db_name_refuses_unsafe_identifiers(bad: str) -> None:
    with pytest.raises(verify_restore.ConfigError, match="not a safe identifier"):
        verify_restore.scratch_db_name(_URL, "_restore_check", bad)


def test_scratch_db_name_refuses_empty_suffix_collision() -> None:
    # An empty suffix would silently target the live DB.
    with pytest.raises(verify_restore.ConfigError, match="equals the live database"):
        verify_restore.scratch_db_name(_URL, "", "")


def _drill_cfg(**overrides: object) -> verify_restore.Config:
    defaults = {
        "database_url": _PW_URL,
        "backup_dir": Path("/nonexistent"),
        "scratch_db": "genesis_restore_check",
        "precreated": False,
        "min_rows": 1,
        "max_ignored_errors": 0,
        "timeout_seconds": 60,
    }
    defaults.update(overrides)
    return verify_restore.Config(**defaults)  # type: ignore[arg-type]


def _fake_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_restore, "require_binary", lambda name: f"/fake/{name}")


def test_drill_reasserts_scratch_guard_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #34 posture: even if load_config were bypassed, a _Drill
    # aimed at the live DB must refuse to exist.
    _fake_binaries(monkeypatch)
    with pytest.raises(verify_restore.DrillError, match="equals the live database"):
        verify_restore._Drill(_drill_cfg(scratch_db="genesis"))


def test_drill_refuses_destructive_op_on_non_scratch_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_binaries(monkeypatch)
    drill = verify_restore._Drill(_drill_cfg())
    with pytest.raises(verify_restore.DrillError, match="not the scratch database"):
        drill._assert_scratch_target("restore", "postgresql://genesis@localhost:5432/genesis")


# --- sanity-gate evaluation (finding M2: hollow restores must fail) --------


def _report(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "alembic": "0048_head",
        "tenants": 2,
        "members": 10,
        "transactions": 40,
        "ledger_entries": 80,
        "imbalanced_tenants": 0,
    }
    base.update(overrides)
    return base


def test_evaluate_report_accepts_a_sane_restore() -> None:
    verify_restore.evaluate_report(_report(), min_rows=1)  # must not raise


def test_evaluate_report_rejects_missing_alembic() -> None:
    with pytest.raises(verify_restore.DrillError, match="alembic_version"):
        verify_restore.evaluate_report(_report(alembic="MISSING"), min_rows=1)


def test_evaluate_report_rejects_zero_tenants() -> None:
    with pytest.raises(verify_restore.DrillError, match="zero tenants"):
        verify_restore.evaluate_report(_report(tenants=0), min_rows=1)


@pytest.mark.parametrize("table", ["members", "transactions", "ledger_entries"])
def test_evaluate_report_rejects_hollow_restore(table: str) -> None:
    # Finding M2: a dump that lost all financial rows used to produce
    # RESTORE_CHECK SUCCESS. Now every floored table must clear
    # RESTORE_CHECK_MIN_ROWS.
    with pytest.raises(verify_restore.DrillError, match=table):
        verify_restore.evaluate_report(_report(**{table: 0}), min_rows=1)


def test_evaluate_report_min_rows_zero_allows_prelaunch_db() -> None:
    report = _report(members=0, transactions=0, ledger_entries=0)
    verify_restore.evaluate_report(report, min_rows=0)  # must not raise


def test_evaluate_report_rejects_imbalanced_ledger() -> None:
    with pytest.raises(verify_restore.DrillError, match="invariant violated"):
        verify_restore.evaluate_report(_report(imbalanced_tenants=3), min_rows=1)


# --- ignored-error budget parsing (pure decision logic on stderr) ----------


def _completed(argv: list[str], rc: int, stdout: str = "", stderr: str = "") -> object:
    return subprocess.CompletedProcess(argv, rc, stdout, stderr)


def _budget_run_fake(restore_rc: int, restore_stderr: str):
    def fake(stage: str, argv: list[str], timeout: int, env: dict | None = None) -> object:
        if stage == "verify_dump":
            return _completed(argv, 0, stdout="1; 0 TABLE public tenants")
        if stage == "restore":
            return _completed(argv, restore_rc, stderr=restore_stderr)
        raise AssertionError(f"unexpected stage {stage}")

    return fake


def test_restore_budget_tolerates_ignored_errors_within_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_binaries(monkeypatch)
    drill = verify_restore._Drill(_drill_cfg(max_ignored_errors=1))
    stderr = "pg_restore: warning: errors ignored on restore: 1"
    monkeypatch.setattr(verify_restore, "run", _budget_run_fake(1, stderr))
    assert drill.restore(tmp_path / "x.dump") == 1


def test_restore_budget_rejects_ignored_errors_over_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_binaries(monkeypatch)
    drill = verify_restore._Drill(_drill_cfg(max_ignored_errors=0))
    stderr = "pg_restore: warning: errors ignored on restore: 2"
    monkeypatch.setattr(verify_restore, "run", _budget_run_fake(1, stderr))
    with pytest.raises(verify_restore.DrillError, match="exceeds the budget"):
        drill.restore(tmp_path / "x.dump")


def test_restore_hard_failure_without_summary_line_is_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_binaries(monkeypatch)
    drill = verify_restore._Drill(_drill_cfg(max_ignored_errors=5))
    monkeypatch.setattr(
        verify_restore, "run", _budget_run_fake(1, "pg_restore: error: out of memory")
    )
    with pytest.raises(verify_restore.DrillError, match="exit code 1"):
        drill.restore(tmp_path / "x.dump")


def test_check_reports_last_five_stderr_lines() -> None:
    result = _completed([], 3, stderr="l1\nl2\nl3\nl4\nl5\nl6\nl7")
    with pytest.raises(backup_common.StageError, match="exit code 3") as excinfo:
        backup_common.check("stage", result)
    message = str(excinfo.value)
    assert "l3; l4; l5; l6; l7" in message
    assert "l2" not in message


# --- private temp files -----------------------------------------------------


def test_pre_create_private_pins_mode_0600_and_truncates(tmp_path: Path) -> None:
    target = tmp_path / "plain.tmp"
    target.write_bytes(b"leftover")
    target.chmod(0o644)
    backup_common.pre_create_private(target)
    assert target.stat().st_size == 0
    assert (target.stat().st_mode & 0o777) == 0o600


# --- argv/env sweeps: the password must never reach argv (B3) --------------


class _BackupRunRecorder:
    """Fakes backup_common.run for backup_db: records every argv/env,
    simulates file side effects, and captures the plaintext file mode
    at encryption time (when the whole DB sits in it)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict | None]] = []
        self.plaintext_mode: int | None = None

    def __call__(
        self, stage: str, argv: list[str], timeout: int, env: dict | None = None
    ) -> object:
        self.calls.append((stage, list(argv), env))
        stdout = ""
        if stage == "preflight":
            stdout = "t"
        elif stage == "pg_dump":
            for arg in argv:
                if arg.startswith("--file="):
                    Path(arg[len("--file=") :]).write_bytes(b"PGDMP-fake")
        elif stage == "verify_dump":
            stdout = "1; 0 TABLE public tenants"
        elif stage == "encrypt":
            source = Path(argv[argv.index("-in") + 1])
            self.plaintext_mode = source.stat().st_mode & 0o777
            Path(argv[argv.index("-out") + 1]).write_bytes(b"enc-fake")
        return _completed(argv, 0, stdout=stdout)


def test_run_backup_sweep_password_never_in_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _BackupRunRecorder()
    monkeypatch.setattr(backup_db, "require_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(backup_db, "run", recorder)
    cfg = backup_db.load_config(
        {
            "DATABASE_URL": _PW_URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": str(tmp_path / "bk"),
        }
    )

    encrypted, size, pruned = backup_db.run_backup(cfg)

    stages = [stage for stage, _, _ in recorder.calls]
    assert stages == ["preflight", "pg_dump", "verify_dump", "encrypt"]
    for stage, argv, _env in recorder.calls:
        # The credential sweep, finding B3: nothing that reaches argv
        # (ps-visible on shared hosting) may carry the DB password —
        # and the encryption key may appear only as an env: reference.
        assert all(_LEAKABLE not in arg for arg in argv), stage
        assert all(_KEY not in arg for arg in argv), stage
    for stage in ("preflight", "pg_dump"):
        env = next(e for s, _, e in recorder.calls if s == stage)
        assert env is not None and env["PGPASSWORD"] == _LEAKABLE, stage
    encrypt_argv = next(a for s, a, _ in recorder.calls if s == "encrypt")
    assert "env:BACKUP_ENCRYPTION_KEY" in encrypt_argv

    # Plaintext was 0600 while it held the dump, and is gone afterwards.
    assert recorder.plaintext_mode == 0o600
    assert not list(tmp_path.glob("**/*.plain.tmp"))
    assert encrypted.exists()
    assert (encrypted.stat().st_mode & 0o777) == 0o600
    assert size > 0
    assert pruned == []


def test_run_backup_preflight_fails_closed_without_bypassrls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Finding B1: FORCE RLS + a role without BYPASSRLS means pg_dump
    # cannot produce a complete dump. The preflight must refuse with an
    # actionable message instead of letting pg_dump fail obscurely (or
    # worse, letting someone "fix" it with --enable-row-security).
    def fake_run(stage: str, argv: list[str], timeout: int, env: dict | None = None) -> object:
        assert stage == "preflight"
        return _completed(argv, 0, stdout="f")

    monkeypatch.setattr(backup_db, "require_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(backup_db, "run", fake_run)
    cfg = backup_db.load_config(
        {
            "DATABASE_URL": _PW_URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": str(tmp_path / "bk"),
        }
    )
    with pytest.raises(backup_db.BackupError, match="BYPASSRLS") as excinfo:
        backup_db.run_backup(cfg)
    assert excinfo.value.stage == "preflight"


class _DrillRunRecorder:
    """Fakes backup_common.run for verify_restore: canned pg outputs
    keyed off the SQL text, plus openssl/pg_restore side effects."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict | None]] = []

    def __call__(
        self, stage: str, argv: list[str], timeout: int, env: dict | None = None
    ) -> object:
        self.calls.append((stage, list(argv), env))
        stdout = ""
        if stage == "decrypt":
            Path(argv[argv.index("-out") + 1]).write_bytes(b"PGDMP-fake")
        elif stage == "verify_dump":
            stdout = "1; 0 TABLE public tenants"
        elif stage == "sanity":
            sql = argv[-1]
            if "version_num" in sql:
                stdout = "0048_head"
            elif sql.startswith("SELECT count(*) FROM ("):
                stdout = "0"  # imbalanced tenants
            elif "count(*)" in sql:
                stdout = "7"
        return _completed(argv, 0, stdout=stdout)


def test_run_drill_sweep_password_never_in_argv_and_only_scratch_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup_name = backup_common.backup_filename(datetime(2026, 8, 16, 1, 30))
    (tmp_path / backup_name).write_bytes(b"enc-fake")
    recorder = _DrillRunRecorder()
    monkeypatch.setattr(verify_restore, "require_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(verify_restore, "run", recorder)
    cfg = verify_restore.load_config(
        {
            "DATABASE_URL": _PW_URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": str(tmp_path),
        }
    )

    name, report, ignored = verify_restore.run_drill(cfg)

    assert name == backup_name
    assert ignored == 0
    assert report["alembic"] == "0048_head"
    assert report["tenants"] == 7
    assert report["imbalanced_tenants"] == 0

    for stage, argv, env in recorder.calls:
        assert all(_LEAKABLE not in arg for arg in argv), stage
        assert all(_KEY not in arg for arg in argv), stage
        if stage != "decrypt":  # pg tools get PGPASSWORD; openssl inherits
            assert env is not None and env["PGPASSWORD"] == _LEAKABLE, stage
    # Every destructive statement names ONLY the scratch database.
    for _stage, argv, _ in recorder.calls:
        sql = argv[-1]
        if "DROP DATABASE" in sql or "CREATE DATABASE" in sql:
            assert '"genesis_restore_check"' in sql
            assert '"genesis"' not in sql
    restore_argv = next(a for s, a, _ in recorder.calls if s == "restore")
    dbname = next(a for a in restore_argv if a.startswith("--dbname="))
    assert dbname.endswith("/genesis_restore_check")
    # The decrypted plaintext temp file is cleaned up.
    assert not list(tmp_path.glob("*.drill.tmp"))


# --- main() exit codes: the cron contract -----------------------------------


def test_backup_main_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(env: object) -> object:
        raise backup_db.ConfigError("nope")

    monkeypatch.setattr(backup_db, "load_config", boom)
    assert backup_db.main() == 1

    cfg = backup_db.Config(
        database_url=_URL,
        backup_dir=tmp_path,
        retention_daily=7,
        retention_weekly=4,
        timeout_seconds=60,
    )
    monkeypatch.setattr(backup_db, "load_config", lambda env: cfg)
    monkeypatch.setattr(backup_db, "run_backup", lambda cfg: (tmp_path / "f.dump.enc", 123, []))
    assert backup_db.main() == 0

    def stage_fail(cfg: object) -> object:
        raise backup_db.BackupError("pg_dump", "exit code 1: boom")

    monkeypatch.setattr(backup_db, "run_backup", stage_fail)
    assert backup_db.main() == 1


def test_drill_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(env: object) -> object:
        raise verify_restore.ConfigError("nope")

    monkeypatch.setattr(verify_restore, "load_config", boom)
    assert verify_restore.main() == 1

    monkeypatch.setattr(verify_restore, "load_config", lambda env: _drill_cfg())
    monkeypatch.setattr(
        verify_restore,
        "run_drill",
        lambda cfg: ("genesis-20260816T013000Z.dump.enc", _report(), 0),
    )
    assert verify_restore.main() == 0

    def stage_fail(cfg: object) -> object:
        raise verify_restore.DrillError("sanity", "hollow")

    monkeypatch.setattr(verify_restore, "run_drill", stage_fail)
    assert verify_restore.main() == 1


# --- dead-man-switch heartbeat (#27) ----------------------------------------


class _UrlopenRecorder:
    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url: str, timeout: int = 0) -> object:
        self.urls.append(url)
        if self.fail:
            raise OSError("network down")

        class _Resp:
            def __enter__(self) -> object:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"OK"

        return _Resp()


def test_send_heartbeat_pings_success_and_fail_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _UrlopenRecorder()
    monkeypatch.setattr(backup_common.urllib.request, "urlopen", recorder)
    backup_common.send_heartbeat("https://hc-ping.com/abc", ok=True)
    backup_common.send_heartbeat("https://hc-ping.com/abc", ok=False)
    assert recorder.urls == [
        "https://hc-ping.com/abc",
        "https://hc-ping.com/abc/fail",
    ]


def test_send_heartbeat_is_optional_and_https_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _UrlopenRecorder()
    monkeypatch.setattr(backup_common.urllib.request, "urlopen", recorder)
    backup_common.send_heartbeat("", ok=True)  # unconfigured: no ping
    # Plain http would leak the check UUID on the wire; refused.
    backup_common.send_heartbeat("http://hc-ping.com/abc", ok=True)
    assert recorder.urls == []


def test_send_heartbeat_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A monitoring outage must never turn a good backup into a failure:
    # the missed ping is itself the alert.
    recorder = _UrlopenRecorder(fail=True)
    monkeypatch.setattr(backup_common.urllib.request, "urlopen", recorder)
    backup_common.send_heartbeat("https://hc-ping.com/abc", ok=True)  # no raise
    assert recorder.urls == ["https://hc-ping.com/abc"]


def _heartbeat_spy() -> tuple[list[bool], object]:
    pings: list[bool] = []

    def spy(url: str, *, ok: bool, timeout: int = 10) -> None:
        if url:
            pings.append(ok)

    return pings, spy


def test_backup_main_pings_heartbeat_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pings, spy = _heartbeat_spy()
    monkeypatch.setattr(backup_db, "send_heartbeat", spy)
    monkeypatch.setenv("BACKUP_HEARTBEAT_URL", "https://hc-ping.com/abc")

    def boom(env: object) -> object:
        raise backup_db.ConfigError("nope")

    monkeypatch.setattr(backup_db, "load_config", boom)
    assert backup_db.main() == 1  # even a config failure pings /fail

    cfg = backup_db.Config(
        database_url=_URL,
        backup_dir=tmp_path,
        retention_daily=7,
        retention_weekly=4,
        timeout_seconds=60,
    )
    monkeypatch.setattr(backup_db, "load_config", lambda env: cfg)
    monkeypatch.setattr(backup_db, "run_backup", lambda cfg: (tmp_path / "f", 1, []))
    assert backup_db.main() == 0

    assert pings == [False, True]


def test_drill_main_pings_heartbeat_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pings, spy = _heartbeat_spy()
    monkeypatch.setattr(verify_restore, "send_heartbeat", spy)
    monkeypatch.setenv("RESTORE_CHECK_HEARTBEAT_URL", "https://hc-ping.com/def")

    monkeypatch.setattr(verify_restore, "load_config", lambda env: _drill_cfg())

    def stage_fail(cfg: object) -> object:
        raise verify_restore.DrillError("sanity", "hollow")

    monkeypatch.setattr(verify_restore, "run_drill", stage_fail)
    assert verify_restore.main() == 1

    monkeypatch.setattr(
        verify_restore,
        "run_drill",
        lambda cfg: ("genesis-20260816T013000Z.dump.enc", _report(), 0),
    )
    assert verify_restore.main() == 0

    assert pings == [False, True]


# --- offsite copy (#25): SigV4, URL building, fail-closed config -----------

import hashlib  # noqa: E402
import io  # noqa: E402
import urllib.error  # noqa: E402

import offsite_backup  # noqa: E402

_S3_ENV = {
    "OFFSITE_S3_ENDPOINT": "https://s3.example.com",
    "OFFSITE_S3_BUCKET": "genesis-dr",
    "OFFSITE_S3_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "OFFSITE_S3_SECRET_ACCESS_KEY": "x" * 40,
}


def test_sigv4_matches_the_documented_aws_example() -> None:
    # Oracle: the worked example in the AWS Signature Version 4
    # documentation (GET iam.amazonaws.com ListUsers, 2015-08-30).
    # Expected signature is copied from the docs, NOT captured from
    # this implementation.
    headers = offsite_backup.sigv4_headers(
        method="GET",
        url="https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        region="us-east-1",
        service="iam",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",  # noqa: S106 — documented AWS example value
        payload_hash=("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        now=datetime(2015, 8, 30, 12, 36, 0),
        extra_headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert "host" not in headers  # urllib must set Host itself


def test_offsite_config_fail_closed() -> None:
    with pytest.raises(offsite_backup.ConfigError, match="OFFSITE_S3_ENDPOINT"):
        offsite_backup.load_config({})
    http_env = dict(_S3_ENV, OFFSITE_S3_ENDPOINT="http://s3.example.com")
    with pytest.raises(offsite_backup.ConfigError, match="https"):
        offsite_backup.load_config(http_env)
    for missing in ("OFFSITE_S3_BUCKET", "OFFSITE_S3_ACCESS_KEY_ID"):
        env = {k: v for k, v in _S3_ENV.items() if k != missing}
        with pytest.raises(offsite_backup.ConfigError, match=missing):
            offsite_backup.load_config(env)
    no_secret = {k: v for k, v in _S3_ENV.items() if k != "OFFSITE_S3_SECRET_ACCESS_KEY"}
    with pytest.raises(offsite_backup.ConfigError, match="SECRET_ACCESS_KEY"):
        offsite_backup.load_config(no_secret)


def test_offsite_config_defaults_and_secret_not_stored() -> None:
    cfg = offsite_backup.load_config(_S3_ENV)
    assert cfg.region == "us-east-1"
    assert cfg.prefix == "db/"
    assert cfg.timeout_seconds == 3600
    # The secret must never sit on the Config object (same discipline
    # as BACKUP_ENCRYPTION_KEY): only presence is validated.
    assert "x" * 40 not in repr(cfg)


def test_offsite_object_url_path_style() -> None:
    cfg = offsite_backup.load_config(_S3_ENV)
    url = offsite_backup.object_url(cfg, "genesis-20260818T013000Z.dump.enc")
    assert url == ("https://s3.example.com/genesis-dr/db/genesis-20260818T013000Z.dump.enc")


class _UploadRecorder:
    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: int = 0) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        status = self.status

        class _Resp:
            def __enter__(self) -> object:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        _Resp.status = status
        return _Resp()


def test_offsite_upload_sweep_secret_never_in_url_or_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    leakable_key = "s3kr3t-offsite-key-material-1234567890"
    name = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30))
    (tmp_path / name).write_bytes(b"encrypted-bytes")
    recorder = _UploadRecorder()
    monkeypatch.setattr(offsite_backup.urllib.request, "urlopen", recorder)
    env = dict(_S3_ENV, OFFSITE_S3_SECRET_ACCESS_KEY=leakable_key, BACKUP_DIR=str(tmp_path))
    cfg = offsite_backup.load_config(env)

    uploaded, size = offsite_backup.run_offsite(cfg, env)

    assert uploaded == name
    assert size == len(b"encrypted-bytes")
    (request,) = recorder.requests
    assert request.get_method() == "PUT"
    assert request.full_url == offsite_backup.object_url(cfg, name)
    header_blob = " ".join(f"{k}={v}" for k, v in request.header_items())
    assert leakable_key not in request.full_url
    assert leakable_key not in header_blob
    # Payload integrity is bound into the signature.
    expected_hash = hashlib.sha256(b"encrypted-bytes").hexdigest()
    assert request.get_header("X-amz-content-sha256") == expected_hash
    assert "AWS4-HMAC-SHA256" in request.get_header("Authorization")


def test_offsite_upload_http_error_is_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30))
    (tmp_path / name).write_bytes(b"encrypted-bytes")
    error = urllib.error.HTTPError(
        "https://s3.example.com/x", 403, "Forbidden", None, io.BytesIO(b"denied")
    )
    monkeypatch.setattr(offsite_backup.urllib.request, "urlopen", _UploadRecorder(error=error))
    env = dict(_S3_ENV, BACKUP_DIR=str(tmp_path))
    cfg = offsite_backup.load_config(env)
    with pytest.raises(offsite_backup.OffsiteError, match="HTTP 403") as excinfo:
        offsite_backup.run_offsite(cfg, env)
    assert excinfo.value.stage == "upload"


def test_offsite_refuses_empty_artifact_and_missing_dir(tmp_path: Path) -> None:
    env = dict(_S3_ENV, BACKUP_DIR=str(tmp_path / "missing"))
    cfg = offsite_backup.load_config(env)
    with pytest.raises(offsite_backup.OffsiteError, match="does not exist"):
        offsite_backup.run_offsite(cfg, env)

    env = dict(_S3_ENV, BACKUP_DIR=str(tmp_path))
    cfg = offsite_backup.load_config(env)
    with pytest.raises(offsite_backup.OffsiteError, match="no genesis-"):
        offsite_backup.run_offsite(cfg, env)

    name = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30))
    (tmp_path / name).write_bytes(b"")
    with pytest.raises(offsite_backup.OffsiteError, match="refusing to upload"):
        offsite_backup.run_offsite(cfg, env)


def test_offsite_main_exit_codes_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pings, spy = _heartbeat_spy()
    monkeypatch.setattr(offsite_backup, "send_heartbeat", spy)
    monkeypatch.setenv("OFFSITE_HEARTBEAT_URL", "https://hc-ping.com/ghi")

    def boom(env: object) -> object:
        raise offsite_backup.ConfigError("nope")

    monkeypatch.setattr(offsite_backup, "load_config", boom)
    assert offsite_backup.main() == 1  # config failure still pings /fail

    cfg = offsite_backup.Config(
        backup_dir=tmp_path,
        endpoint="https://s3.example.com",
        bucket="genesis-dr",
        region="us-east-1",
        access_key_id="AKIDEXAMPLE",
        prefix="db/",
        timeout_seconds=60,
    )
    monkeypatch.setattr(offsite_backup, "load_config", lambda env: cfg)
    monkeypatch.setattr(offsite_backup, "run_offsite", lambda cfg, env: ("f.dump.enc", 5))
    assert offsite_backup.main() == 0

    def stage_fail(cfg: object, env: object) -> object:
        raise offsite_backup.OffsiteError("upload", "HTTP 403")

    monkeypatch.setattr(offsite_backup, "run_offsite", stage_fail)
    assert offsite_backup.main() == 1

    assert pings == [False, True, False]


# --- key-id binding (#28): rotation must never make restore-day a guess ----


def test_key_id_is_a_stable_8_hex_prefix() -> None:
    kid = backup_common.key_id(_KEY)
    assert len(kid) == 8
    assert kid == backup_common.key_id(_KEY)  # deterministic
    assert all(c in "0123456789abcdef" for c in kid)
    assert kid != backup_common.key_id("different-key-material-" + "x" * 40)
    # Identifies without revealing: the id is not a substring of the key.
    assert kid not in _KEY


def test_backup_filename_roundtrip_with_key_id() -> None:
    ts = datetime(2026, 8, 18, 1, 30, 0)
    name = backup_common.backup_filename(ts, "a1b2c3d4")
    assert name == "genesis-20260818T013000Z.ka1b2c3d4.dump.enc"
    assert backup_common.parse_backup_name(name) == (ts, "a1b2c3d4")
    assert backup_common.parse_backup_timestamp(name) == ts
    # Legacy names parse with no key id — pre-#28 artifacts stay usable.
    legacy = backup_common.backup_filename(ts)
    assert backup_common.parse_backup_name(legacy) == (ts, None)


@pytest.mark.parametrize(
    "name",
    [
        "genesis-20260818T013000Z.kZZZZZZZZ.dump.enc",  # non-hex id
        "genesis-20260818T013000Z.ka1b2c3.dump.enc",  # 6 chars
        "genesis-20260818T013000Z.kA1B2C3D4.dump.enc",  # uppercase
        "genesis-20260818T013000Z.x12345678.dump.enc",  # wrong marker
    ],
)
def test_parse_backup_name_rejects_malformed_key_ids(name: str) -> None:
    assert backup_common.parse_backup_name(name) is None


def test_retention_and_latest_handle_mixed_legacy_and_key_id_names() -> None:
    legacy = backup_common.backup_filename(datetime(2026, 8, 10, 1, 30))
    keyed_old = backup_common.backup_filename(datetime(2026, 8, 14, 1, 30), "a1b2c3d4")
    keyed_new = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30), "a1b2c3d4")
    names = [legacy, keyed_new, keyed_old]
    assert backup_common.latest_backup(names) == keyed_new
    doomed = backup_db.select_prunable(names, daily_keep=1, weekly_keep=0)
    assert set(doomed) == {legacy, keyed_old}  # both forms prunable


def test_backup_config_carries_the_key_id() -> None:
    cfg = backup_db.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY})
    assert cfg.key_id == backup_common.key_id(_KEY)
    drill_cfg = verify_restore.load_config({"DATABASE_URL": _URL, "BACKUP_ENCRYPTION_KEY": _KEY})
    assert drill_cfg.key_id == cfg.key_id


def test_run_backup_embeds_key_id_in_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _BackupRunRecorder()
    monkeypatch.setattr(backup_db, "require_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(backup_db, "run", recorder)
    cfg = backup_db.load_config(
        {
            "DATABASE_URL": _PW_URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": str(tmp_path / "bk"),
        }
    )
    encrypted, _, _ = backup_db.run_backup(cfg)
    parsed = backup_common.parse_backup_name(encrypted.name)
    assert parsed is not None
    assert parsed[1] == backup_common.key_id(_KEY)


def test_drill_refuses_mismatched_key_id_before_decrypting() -> None:
    # The whole point of #28: fail with a WHICH-KEY message, never an
    # opaque openssl "bad decrypt", and fail before any work happens.
    name = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30), "0badc0de")
    with pytest.raises(verify_restore.DrillError, match="k0badc0de") as excinfo:
        verify_restore.check_key_binding(name, "a1b2c3d4")
    assert excinfo.value.stage == "keyid"
    assert "escrowed key" in str(excinfo.value)
    # Matching id and legacy (no id) both proceed.
    verify_restore.check_key_binding(name, "0badc0de")
    legacy = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30))
    verify_restore.check_key_binding(legacy, "a1b2c3d4")


def test_run_drill_stops_at_keyid_stage_on_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wrong = backup_common.backup_filename(datetime(2026, 8, 18, 1, 30), "0badc0de")
    (tmp_path / wrong).write_bytes(b"enc")
    recorder = _DrillRunRecorder()
    monkeypatch.setattr(verify_restore, "require_binary", lambda name: f"/fake/{name}")
    monkeypatch.setattr(verify_restore, "run", recorder)
    cfg = verify_restore.load_config(
        {
            "DATABASE_URL": _PW_URL,
            "BACKUP_ENCRYPTION_KEY": _KEY,
            "BACKUP_DIR": str(tmp_path),
        }
    )
    with pytest.raises(verify_restore.DrillError, match="k0badc0de"):
        verify_restore.run_drill(cfg)
    assert recorder.calls == []  # nothing decrypted, nothing touched
