"""Unit tests for the backup/restore-drill scripts (no live DB needed).

scripts/backup_db.py and scripts/verify_restore.py are stdlib-only
one-shot cron entrypoints (see their module docstrings); everything
here exercises the pure decision logic — retention selection, filename
rotation, URL handling, scratch-DB naming guards and fail-closed env
validation. The subprocess paths (pg_dump/pg_restore/psql/openssl) are
exercised operationally by the weekly restore drill itself and are
deliberately not mocked here: a mocked pg_dump proves nothing.

Boundary oracles are hand-computed in comments, never captured from
the implementation (MASTER_PROMPT section 4).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# The cron scripts live outside the installed package on purpose
# (stdlib-only DR tooling); import them by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backup_db
import verify_restore

# --- shared naming/URL helpers (duplicated across both scripts so each
# --- stays single-file runnable; test BOTH so they cannot drift apart)


@pytest.mark.parametrize("module", [backup_db, verify_restore])
def test_libpq_url_strips_sqlalchemy_driver(module) -> None:
    assert module.libpq_url("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert module.libpq_url("postgresql+asyncpg://u@h/db") == "postgresql://u@h/db"


@pytest.mark.parametrize("module", [backup_db, verify_restore])
def test_libpq_url_passthrough(module) -> None:
    # Already-plain URLs and non-URL strings come back untouched.
    assert module.libpq_url("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert module.libpq_url("not-a-url") == "not-a-url"


@pytest.mark.parametrize("module", [backup_db, verify_restore])
def test_filename_constants_identical(module) -> None:
    # The drill locates files the backup script wrote; the naming
    # contract must be byte-identical in both.
    assert module.BACKUP_PREFIX == "genesis-"
    assert module.BACKUP_SUFFIX == ".dump.enc"
    assert module.TIMESTAMP_FORMAT == "%Y%m%dT%H%M%SZ"


def test_backup_filename_roundtrip() -> None:
    # 2026-08-18 01:30:00 UTC → hand-written expected name.
    ts = datetime(2026, 8, 18, 1, 30, 0)
    name = backup_db.backup_filename(ts)
    assert name == "genesis-20260818T013000Z.dump.enc"
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
    assert backup_db.parse_backup_timestamp(name) is None
    assert verify_restore.parse_backup_timestamp(name) is None


# --- retention selection -------------------------------------------------


def _daily_names(start: datetime, days: int) -> list[str]:
    return [backup_db.backup_filename(start + timedelta(days=i)) for i in range(days)]


def test_select_prunable_keeps_everything_when_under_budget() -> None:
    names = _daily_names(datetime(2026, 8, 1, 1, 30), 5)
    assert backup_db.select_prunable(names, daily_keep=7, weekly_keep=4) == []


def test_select_prunable_daily_and_weekly_tiers() -> None:
    # 30 nightly dumps 2026-07-20 .. 2026-08-18 (01:30 each).
    # Hand-computed: with daily_keep=7 the newest 7 (Aug 12..18) stay.
    # Sundays in range: Jul 26, Aug 2, Aug 9, Aug 16 — with
    # weekly_keep=4 all four stay (Aug 16 already inside the daily 7).
    # Everything else goes.
    start = datetime(2026, 7, 20, 1, 30)
    names = _daily_names(start, 30)
    doomed = backup_db.select_prunable(names, daily_keep=7, weekly_keep=4)

    expected_kept = {
        backup_db.backup_filename(datetime(2026, 8, d, 1, 30)) for d in range(12, 19)
    } | {
        backup_db.backup_filename(datetime(2026, 7, 26, 1, 30)),
        backup_db.backup_filename(datetime(2026, 8, 2, 1, 30)),
        backup_db.backup_filename(datetime(2026, 8, 9, 1, 30)),
    }
    assert set(names) - set(doomed) == expected_kept
    # 30 files, 10 survivors (7 daily + 3 extra Sundays) → 20 pruned.
    assert len(doomed) == 20


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
        (tmp_path / backup_db.backup_filename(datetime(2026, 8, 1 + i, 1, 30))).write_bytes(b"x")
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

_KEY = "k" * 64  # what secrets.token_hex(32) produces: 64 hex chars
_URL = "postgresql+psycopg://u:p@localhost:5432/genesis"


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


def test_replace_database_swaps_only_the_path() -> None:
    url = verify_restore.replace_database(_URL, "genesis_restore_check")
    assert url == "postgresql://u:p@localhost:5432/genesis_restore_check"


def test_database_name_extraction() -> None:
    assert verify_restore.database_name(_URL) == "genesis"
    with pytest.raises(verify_restore.ConfigError, match="no database name"):
        verify_restore.database_name("postgresql://u:p@localhost:5432/")
