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
venv or codebase is broken — any python3 on the host can run it. The
only external requirements are the `pg_dump`/`pg_restore` client
binaries and `openssl`, all present on the MochaHost cPanel host.

Environment (see docs/technical/backup-and-restore.md for the runbook):
  DATABASE_URL              required; SQLAlchemy-style URLs accepted
                            (the `+psycopg` driver marker is stripped)
  BACKUP_ENCRYPTION_KEY     required, >= 32 chars; generate with
                            `python -c "import secrets; print(secrets.token_hex(32))"`
  BACKUP_DIR                default ~/backups/db
  BACKUP_RETENTION_DAILY    default 7  (newest N dumps kept)
  BACKUP_RETENTION_WEEKLY   default 4  (newest N Sunday dumps kept)
  BACKUP_TIMEOUT_SECONDS    default 3600 (per external command)

cPanel cron does NOT inherit the Passenger app's env vars, so source an
env file (chmod 600) in the cron line. Example (nightly at 01:30):
  30 1 * * * . /home/USER/.genesis_backup_env && \
    /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/backup_db.py >> /home/USER/logs/backup_db.log 2>&1

Decrypting a dump by hand (same parameters, keep in sync with
ENCRYPT_ARGS below and with scripts/verify_restore.py):
  openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 600000 \
    -in genesis-<ts>.dump.enc -out restore.dump -pass env:BACKUP_ENCRYPTION_KEY
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Timestamped so a cron log answers "did this cycle actually
#: fire, and when?" — bare stdout prints cannot.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(Path(__file__).stem)

BACKUP_PREFIX = "genesis-"
BACKUP_SUFFIX = ".dump.enc"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MIN_KEY_LENGTH = 32

#: Symmetric-encryption parameters. `openssl enc` offers no AEAD mode;
#: integrity is instead proven end-to-end by scripts/verify_restore.py
#: (decrypt + pg_restore into a scratch DB). Keep these in sync with
#: DECRYPT_ARGS in scripts/verify_restore.py and with the manual
#: decrypt command in the module docstring / runbook.
ENCRYPT_ARGS = ["-aes-256-cbc", "-md", "sha256", "-pbkdf2", "-iter", "600000", "-salt"]


class ConfigError(ValueError):
    """Environment/configuration is unusable; refuse to run."""


class BackupError(RuntimeError):
    """A backup stage failed; carries the stage name for the FAILURE line."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class Config:
    database_url: str
    backup_dir: Path
    retention_daily: int
    retention_weekly: int
    timeout_seconds: int


def _int_env(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment; raise ConfigError on anything unusable.

    BACKUP_ENCRYPTION_KEY is validated here but intentionally NOT
    stored on the Config object — it stays in the process environment,
    where `openssl enc -pass env:BACKUP_ENCRYPTION_KEY` reads it
    without the key ever appearing in argv (visible in `ps`) or logs.
    """
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not configured")
    key = env.get("BACKUP_ENCRYPTION_KEY", "")
    if not key:
        raise ConfigError(
            "BACKUP_ENCRYPTION_KEY is not set — refusing to write an unencrypted "
            "backup. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(key) < MIN_KEY_LENGTH:
        raise ConfigError(
            f"BACKUP_ENCRYPTION_KEY is shorter than {MIN_KEY_LENGTH} characters — "
            "refusing weak key material"
        )
    backup_dir = Path(env.get("BACKUP_DIR", "").strip() or "~/backups/db").expanduser()
    return Config(
        database_url=database_url,
        backup_dir=backup_dir,
        retention_daily=_int_env(env, "BACKUP_RETENTION_DAILY", 7, 1),
        retention_weekly=_int_env(env, "BACKUP_RETENTION_WEEKLY", 4, 0),
        timeout_seconds=_int_env(env, "BACKUP_TIMEOUT_SECONDS", 3600, 1),
    )


def libpq_url(url: str) -> str:
    """Strip a SQLAlchemy driver marker (`postgresql+psycopg://`) for libpq tools."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return scheme.partition("+")[0] + sep + rest


def backup_filename(ts: datetime) -> str:
    return BACKUP_PREFIX + ts.strftime(TIMESTAMP_FORMAT) + BACKUP_SUFFIX


def parse_backup_timestamp(name: str) -> datetime | None:
    """Return the timestamp encoded in a backup filename, or None if foreign."""
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    stamp = name[len(BACKUP_PREFIX) : len(name) - len(BACKUP_SUFFIX)]
    try:
        # Naive-UTC by contract: filenames are always written in UTC.
        return datetime.strptime(stamp, TIMESTAMP_FORMAT)
    except ValueError:
        return None


def select_prunable(names: Iterable[str], daily_keep: int, weekly_keep: int) -> list[str]:
    """Choose which backup files to delete under the retention policy.

    Keeps the newest `daily_keep` backups plus the newest `weekly_keep`
    backups taken on a Sunday (the weekly tier). Filenames that don't
    match the backup naming scheme are never selected — pruning only
    ever touches files this script itself produced.
    """
    dated = sorted(
        ((ts, name) for name in names if (ts := parse_backup_timestamp(name)) is not None),
        reverse=True,
    )
    keep = {name for _, name in dated[:daily_keep]}
    sundays = [name for ts, name in dated if ts.isoweekday() == 7]
    keep.update(sundays[:weekly_keep])
    return sorted(name for _, name in dated if name not in keep)


def prune_backups(backup_dir: Path, daily_keep: int, weekly_keep: int) -> list[str]:
    """Apply the retention policy on disk; returns the deleted filenames."""
    names = [p.name for p in backup_dir.iterdir() if p.is_file()]
    doomed = select_prunable(names, daily_keep, weekly_keep)
    for name in doomed:
        (backup_dir / name).unlink()
    return doomed


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupError("tooling", f"required binary {name!r} not found on PATH")
    return path


def _run(stage: str, argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one external command; any failure becomes a BackupError."""
    try:
        # S603 suppression rationale: argv is built entirely from
        # validated env config and shutil.which-resolved absolute
        # binary paths; shell=False throughout.
        return subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(stage, f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise BackupError(stage, f"failed to execute {argv[0]}: {exc}") from exc


def _check(stage: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "; ".join(detail[-5:]) if detail else "no output"
        raise BackupError(stage, f"exit code {result.returncode}: {tail}")


def run_backup(cfg: Config) -> tuple[Path, int, list[str]]:
    """Dump, verify, encrypt, prune. Returns (path, size_bytes, pruned)."""
    pg_dump = _require_binary("pg_dump")
    pg_restore = _require_binary("pg_restore")
    openssl = _require_binary("openssl")

    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir.chmod(0o700)

    name = backup_filename(datetime.now(UTC))
    encrypted = cfg.backup_dir / name
    # ".plain.tmp" never matches BACKUP_SUFFIX, so a leftover plaintext
    # temp file can never be mistaken for (or pruned as) a real backup.
    plaintext = cfg.backup_dir / (name + ".plain.tmp")

    try:
        _check(
            "pg_dump",
            _run(
                "pg_dump",
                [
                    pg_dump,
                    "--format=custom",
                    "--no-password",
                    f"--file={plaintext}",
                    f"--dbname={libpq_url(cfg.database_url)}",
                ],
                cfg.timeout_seconds,
            ),
        )
        if not plaintext.exists() or plaintext.stat().st_size == 0:
            raise BackupError("verify_dump", "pg_dump produced an empty file")

        listing = _run("verify_dump", [pg_restore, "--list", str(plaintext)], cfg.timeout_seconds)
        _check("verify_dump", listing)
        if not listing.stdout.strip():
            raise BackupError(
                "verify_dump", "pg_restore --list returned an empty table of contents"
            )

        _check(
            "encrypt",
            _run(
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

    pruned = prune_backups(cfg.backup_dir, cfg.retention_daily, cfg.retention_weekly)
    return encrypted, encrypted.stat().st_size, pruned


def main() -> int:
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        logger.error(f"BACKUP_DB FAILURE stage=config error={exc}")
        return 1
    try:
        encrypted, size, pruned = run_backup(cfg)
    except BackupError as exc:
        logger.error(f"BACKUP_DB FAILURE stage={exc.stage} error={exc}")
        return 1
    except Exception:
        # Blind on purpose: the greppable FAILURE line must always be emitted.
        logger.exception("unexpected error")
        logger.error("BACKUP_DB FAILURE stage=unexpected error=see traceback above")
        return 1
    if pruned:
        logger.info(f"retention pruned {len(pruned)}: {', '.join(pruned)}")
    logger.info(f"BACKUP_DB SUCCESS file={encrypted} bytes={size} pruned={len(pruned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
