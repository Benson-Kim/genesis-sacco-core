"""One-shot encrypted PostgreSQL backup, for cPanel Cron Jobs.

Runs `pg_dump --format=custom` against DATABASE_URL, verifies the dump
is readable (`pg_restore --list`), encrypts it with AES-256 via the
host's `openssl` binary (symmetric key from BACKUP_ENCRYPTION_KEY —
the run fails loudly if it is unset), enforces a daily/weekly
retention policy, and emits exactly one machine-greppable
`BACKUP_DB SUCCESS`/`BACKUP_DB FAILURE` line for dead-man-switch
monitoring (alert on the *absence* of SUCCESS, which also covers
crashes and a host that never ran cron at all).

Deliberately stdlib-only and independent of the `genesis` package:
disaster-recovery tooling must keep working even when the application
venv or codebase is broken — any python3 >= 3.8 on the host can run it
(3.11+-only conveniences like the datetime UTC alias are deliberately
avoided). Shared plumbing lives in the sibling module
scripts/backup_common.py, which must be deployed alongside this file.
The only external requirements are the `pg_dump`/`pg_restore`/`psql`
client binaries and `openssl`, all present on the MochaHost cPanel
host.

Privilege prerequisite (runbook §2a): migration 0001 puts FORCE ROW
LEVEL SECURITY on every tenant table, so the dump role must have
BYPASSRLS (or be superuser) — `pg_dump` runs with row_security=off and
errors out otherwise, and `--enable-row-security` would silently dump
only policy-visible rows, which is worse. A preflight check turns that
failure mode into an actionable config error.

Environment (see docs/technical/backup-and-restore.md for the runbook):
  DATABASE_URL              required; SQLAlchemy-style URLs accepted
                            (the `+psycopg` driver marker is stripped);
                            the password never reaches argv or logs —
                            it is passed via PGPASSWORD to the child
                            processes only
  BACKUP_ENCRYPTION_KEY     required, >= 32 chars; generate with
                            `python -c "import secrets; print(secrets.token_hex(32))"`
  BACKUP_DIR                default ~/backups/db
  BACKUP_RETENTION_DAILY    default 7  (newest N dumps kept)
  BACKUP_RETENTION_WEEKLY   default 4  (newest dump of each of the
                            newest N ISO weeks kept — calendar-week
                            bucketing, immune to the local-time cron /
                            UTC-stamp weekday shift)
  BACKUP_TIMEOUT_SECONDS    default 3600 (per external command)
  BACKUP_HEARTBEAT_URL      optional https:// heartbeat check URL
                            (e.g. Healthchecks.io); pinged on success,
                            <url>/fail on failure — the service alerts
                            on ping ABSENCE, the true dead-man switch.
                            Required for production per the runbook §3

cPanel cron does NOT inherit the Passenger app's env vars, so source an
env file (chmod 600) in the cron line. Example (nightly at 01:30):
  30 1 * * * . /home/USER/.genesis_backup_env && \
    /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/backup_db.py >> /home/USER/logs/backup_db.log 2>&1

Decrypting a dump by hand (parameters pinned in
backup_common.ENCRYPT_ARGS; the drill derives its decrypt arguments
from the same list, so they cannot drift):
  openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 600000 \
    -in genesis-<ts>.dump.enc -out restore.dump -pass env:BACKUP_ENCRYPTION_KEY
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backup_common import (
    ENCRYPT_ARGS,
    ConfigError,
    StageError,
    backup_filename,
    check,
    child_env,
    connection_args,
    int_env,
    parse_backup_timestamp,
    pre_create_private,
    require_binary,
    require_encryption_key,
    run,
    script_logger,
    send_heartbeat,
)

logger = script_logger(__file__)

BackupError = StageError

#: True iff the session role can read RLS-forced tables with
#: row_security=off — exactly what pg_dump needs (see module docstring).
_PREFLIGHT_SQL = "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = session_user"


@dataclass(frozen=True)
class Config:
    database_url: str
    backup_dir: Path
    retention_daily: int
    retention_weekly: int
    timeout_seconds: int


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment; raise ConfigError on anything unusable.

    BACKUP_ENCRYPTION_KEY is validated but intentionally NOT stored on
    the Config object — it stays in the process environment, where
    `openssl enc -pass env:BACKUP_ENCRYPTION_KEY` reads it without the
    key ever appearing in argv (visible in `ps`) or logs. The database
    password gets the same treatment via backup_common.connection_args.
    """
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not configured")
    require_encryption_key(
        env,
        missing=(
            "refusing to write an unencrypted backup. Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        ),
        short="refusing weak key material",
    )
    backup_dir = Path(env.get("BACKUP_DIR", "").strip() or "~/backups/db").expanduser()
    return Config(
        database_url=database_url,
        backup_dir=backup_dir,
        retention_daily=int_env(env, "BACKUP_RETENTION_DAILY", 7, 1),
        retention_weekly=int_env(env, "BACKUP_RETENTION_WEEKLY", 4, 0),
        timeout_seconds=int_env(env, "BACKUP_TIMEOUT_SECONDS", 3600, 1),
    )


def select_prunable(names: Iterable[str], daily_keep: int, weekly_keep: int) -> list[str]:
    """Choose which backup files to delete under the retention policy.

    Keeps the newest `daily_keep` backups plus the newest backup of
    each of the newest `weekly_keep` distinct ISO calendar weeks.

    ISO-week bucketing instead of "dumps taken on Sunday" (MR !5
    finding M1): filenames are stamped in UTC but cron fires at host
    local time, so in any zone east of UTC+1 a "Sunday 01:30" dump is
    stamped with a *Saturday* UTC weekday and a Sunday-classifier would
    silently never populate the weekly tier. Every calendar week has an
    ISO week number in every timezone, so this tier cannot silently
    starve.

    Filenames that don't match the backup naming scheme are never
    selected — pruning only ever touches files this script itself
    produced.
    """
    dated = sorted(
        ((ts, name) for name in names if (ts := parse_backup_timestamp(name)) is not None),
        reverse=True,
    )
    keep = {name for _, name in dated[:daily_keep]}
    newest_per_week: dict[tuple[int, int], str] = {}
    for ts, name in dated:  # newest first, so first hit per week wins
        iso = ts.isocalendar()
        week = (iso[0], iso[1])
        if week not in newest_per_week:
            newest_per_week[week] = name
    for week in sorted(newest_per_week, reverse=True)[:weekly_keep]:
        keep.add(newest_per_week[week])
    return sorted(name for _, name in dated if name not in keep)


def prune_backups(backup_dir: Path, daily_keep: int, weekly_keep: int) -> list[str]:
    """Apply the retention policy on disk; returns the deleted filenames."""
    names = [p.name for p in backup_dir.iterdir() if p.is_file()]
    doomed = select_prunable(names, daily_keep, weekly_keep)
    for name in doomed:
        (backup_dir / name).unlink()
    return doomed


def run_backup(cfg: Config) -> tuple[Path, int, list[str]]:
    """Preflight, dump, verify, encrypt, prune. Returns (path, size, pruned)."""
    pg_dump = require_binary("pg_dump")
    pg_restore = require_binary("pg_restore")
    psql = require_binary("psql")
    openssl = require_binary("openssl")

    # Password-free URL for argv (ps-visible on a shared host);
    # password rides in PGPASSWORD in the child env only (finding B3).
    conn_url, password = connection_args(cfg.database_url)
    pg_env = child_env(password)

    preflight = run(
        "preflight",
        [
            psql,
            conn_url,
            "-X",
            "-A",
            "-t",
            "--no-password",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            _PREFLIGHT_SQL,
        ],
        cfg.timeout_seconds,
        env=pg_env,
    )
    check("preflight", preflight)
    if preflight.stdout.strip() != "t":
        raise BackupError(
            "preflight",
            "database role lacks BYPASSRLS and is not superuser: the schema FORCEs "
            "row-level security on every tenant table (migration 0001), so pg_dump "
            "(row_security=off) will fail — and --enable-row-security would silently "
            "dump only policy-visible rows, which is worse for a ledger. Grant "
            "BYPASSRLS to the dump role first (runbook §2a)",
        )

    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir.chmod(0o700)

    # UP017 suppressed: the UTC alias is 3.11-only; DR scripts must
    # run under the host system python (>= 3.8) — finding B4.
    name = backup_filename(datetime.now(timezone.utc))  # noqa: UP017
    encrypted = cfg.backup_dir / name
    # ".plain.tmp" never matches BACKUP_SUFFIX, so a leftover plaintext
    # temp file can never be mistaken for (or pruned as) a real backup.
    plaintext = cfg.backup_dir / (name + ".plain.tmp")

    try:
        # Pre-create 0600: pg_dump/openssl keep an existing file's mode,
        # so the plaintext dump is never readable via cron's umask.
        pre_create_private(plaintext)
        pre_create_private(encrypted)
        check(
            "pg_dump",
            run(
                "pg_dump",
                [
                    pg_dump,
                    "--format=custom",
                    "--no-password",
                    f"--file={plaintext}",
                    f"--dbname={conn_url}",
                ],
                cfg.timeout_seconds,
                env=pg_env,
            ),
        )
        if not plaintext.exists() or plaintext.stat().st_size == 0:
            raise BackupError("verify_dump", "pg_dump produced an empty file")

        listing = run(
            "verify_dump",
            [pg_restore, "--list", str(plaintext)],
            cfg.timeout_seconds,
            env=pg_env,
        )
        check("verify_dump", listing)
        if not listing.stdout.strip():
            raise BackupError(
                "verify_dump", "pg_restore --list returned an empty table of contents"
            )

        check(
            "encrypt",
            run(
                "encrypt",
                [
                    openssl,
                    "enc",
                    *ENCRYPT_ARGS,
                    "-in",
                    str(plaintext),
                    "-out",
                    str(encrypted),
                    "-pass",
                    "env:BACKUP_ENCRYPTION_KEY",
                ],
                cfg.timeout_seconds,
            ),
        )
        if not encrypted.exists() or encrypted.stat().st_size == 0:
            raise BackupError("encrypt", "openssl produced an empty encrypted file")
        encrypted.chmod(0o600)
    except BaseException:
        # A failed run must not leave a half-written .dump.enc behind —
        # it would look like a valid newest backup to verify_restore.py.
        encrypted.unlink(missing_ok=True)
        raise
    finally:
        plaintext.unlink(missing_ok=True)

    try:
        pruned = prune_backups(cfg.backup_dir, cfg.retention_daily, cfg.retention_weekly)
    except OSError as exc:
        # Distinct stage so the FAILURE line tells ops the truth: the
        # backup itself was written and verified; only pruning failed.
        raise BackupError(
            "prune",
            f"backup {encrypted.name} was written and verified, but retention "
            f"pruning failed: {exc}",
        ) from exc
    return encrypted, encrypted.stat().st_size, pruned


def main() -> int:
    # Read the heartbeat URL independently of load_config so even a
    # config-stage failure still emits the explicit /fail ping (#27).
    heartbeat = os.environ.get("BACKUP_HEARTBEAT_URL", "").strip()
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        logger.error(f"BACKUP_DB FAILURE stage=config error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    try:
        encrypted, size, pruned = run_backup(cfg)
    except BackupError as exc:
        logger.error(f"BACKUP_DB FAILURE stage={exc.stage} error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    except Exception:
        # Blind on purpose: the greppable FAILURE line must always be emitted.
        logger.exception("unexpected error")
        logger.error("BACKUP_DB FAILURE stage=unexpected error=see traceback above")
        send_heartbeat(heartbeat, ok=False)
        return 1
    if pruned:
        logger.info(f"retention pruned {len(pruned)}: {', '.join(pruned)}")
    logger.info(f"BACKUP_DB SUCCESS file={encrypted} bytes={size} pruned={len(pruned)}")
    send_heartbeat(heartbeat, ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
