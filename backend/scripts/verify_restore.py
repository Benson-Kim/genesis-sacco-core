"""One-shot restore drill for the encrypted pg_dump backups, for cPanel cron.

A backup that has never been restored is a hope, not a backup. This
script proves the newest dump written by scripts/backup_db.py is
actually restorable:

  1. decrypts the latest ``genesis-*.dump.enc`` in BACKUP_DIR,
  2. verifies ``pg_restore --list`` can read it,
  3. restores it into a scratch database (default ``<dbname>_restore_check``),
  4. runs sanity queries — row counts on tenants / members /
     transactions / ledger_entries, the alembic head, and the
     double-entry invariant (per tenant, SUM of debit amounts equals
     SUM of credit amounts on ledger_entries),
  5. drops the scratch database again,

and emits exactly one machine-greppable ``RESTORE_CHECK
SUCCESS``/``RESTORE_CHECK FAILURE`` line. Alert on the *absence* of
SUCCESS in the weekly log window, which also covers crashes.

Like backup_db.py this is deliberately stdlib-only (no ``genesis``
import): DR tooling must keep working when the app venv is broken.
External requirements: ``pg_restore``, ``psql``, ``openssl``.

Environment (see docs/technical/backup-and-restore.md):
  DATABASE_URL                      required (SQLAlchemy-style accepted)
  BACKUP_ENCRYPTION_KEY             required (same key backup_db.py used)
  BACKUP_DIR                        default ~/backups/db
  RESTORE_CHECK_DB                  optional explicit scratch DB name
  RESTORE_CHECK_DB_SUFFIX           default _restore_check
  RESTORE_CHECK_PRECREATED          "true" if the DB role lacks CREATEDB:
                                    create the scratch DB once by hand
                                    (cPanel → PostgreSQL Databases) and the
                                    drill will reset its public schema
                                    instead of CREATE/DROP DATABASE
  RESTORE_CHECK_MAX_IGNORED_ERRORS  default 0; raise only for known-benign
                                    restore noise (e.g. a CREATE EXTENSION
                                    privilege refusal on shared hosting)
  BACKUP_TIMEOUT_SECONDS            default 3600 (per external command)

Example cron line (weekly, Monday 03:15 — after Sunday's weekly dump):
  15 3 * * 1 . /home/USER/.genesis_backup_env && \
    /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/verify_restore.py >> /home/USER/logs/verify_restore.log 2>&1
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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

#: Must mirror ENCRYPT_ARGS in scripts/backup_db.py (plus ``-d``).
DECRYPT_ARGS = ["-d", "-aes-256-cbc", "-md", "sha256", "-pbkdf2", "-iter", "600000"]

#: Scratch DB names are interpolated into SQL as identifiers; restrict
#: them to characters that need no quoting games.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_IGNORED_ERRORS_RE = re.compile(r"errors ignored on restore: (\d+)")

#: Code-owned literal queries (never string-built): the sanity row
#: counts reported in the drill's SUCCESS line.
_COUNT_QUERIES = {
    "tenants": "SELECT count(*) FROM tenants",
    "members": "SELECT count(*) FROM members",
    "transactions": "SELECT count(*) FROM transactions",
    "ledger_entries": "SELECT count(*) FROM ledger_entries",
}

#: The schema forces row-level security on every table
#: (migrations/versions/0001), so even the restoring owner sees zero
#: rows without the app's tenant GUC. The scratch DB exists only for
#: this drill and is dropped afterwards, so un-forcing RLS there (the
#: owner's right) is the correct way to count rows across all tenants.
_UNFORCE_RLS_SQL = ";\n".join(
    f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"
    for t in ("tenants", "members", "transactions", "ledger_entries")
)

#: Hand-written invariant: number of tenants whose ledger does NOT
#: balance (must be 0). COALESCE guards tenants with one-sided data.
_IMBALANCE_SQL = """
SELECT count(*) FROM (
    SELECT tenant_id
    FROM ledger_entries
    GROUP BY tenant_id
    HAVING COALESCE(SUM(amount) FILTER (WHERE side = 'debit'), 0)
        <> COALESCE(SUM(amount) FILTER (WHERE side = 'credit'), 0)
) imbalanced
""".strip()


class ConfigError(ValueError):
    """Environment/configuration is unusable; refuse to run."""


class DrillError(RuntimeError):
    """A drill stage failed; carries the stage name for the FAILURE line."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class Config:
    database_url: str
    backup_dir: Path
    scratch_db: str
    precreated: bool
    max_ignored_errors: int
    timeout_seconds: int


def libpq_url(url: str) -> str:
    """Strip a SQLAlchemy driver marker (`postgresql+psycopg://`) for libpq tools."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return scheme.partition("+")[0] + sep + rest


def database_name(url: str) -> str:
    """Extract the database name from a connection URL."""
    name = urlsplit(libpq_url(url)).path.lstrip("/")
    if not name:
        raise ConfigError(f"DATABASE_URL has no database name: cannot derive one from {url!r}")
    return name


def replace_database(url: str, dbname: str) -> str:
    """Return the libpq form of `url` pointing at a different database."""
    parts = urlsplit(libpq_url(url))
    return urlunsplit(parts._replace(path="/" + dbname))


def scratch_db_name(database_url: str, suffix: str, override: str) -> str:
    """Derive (or validate) the scratch DB name; never the source DB itself."""
    source = database_name(database_url)
    name = override.strip() or source + suffix
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ConfigError(
            f"scratch database name {name!r} is not a safe identifier "
            "(lowercase letters, digits and underscores only)"
        )
    if name == source:
        raise ConfigError(
            f"scratch database name {name!r} equals the live database — refusing: "
            "the drill drops/recreates the scratch database"
        )
    return name


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


def latest_backup(names: Iterable[str]) -> str | None:
    """Pick the newest backup by encoded timestamp; foreign files are ignored."""
    dated = [(ts, name) for name in names if (ts := parse_backup_timestamp(name)) is not None]
    if not dated:
        return None
    return max(dated)[1]


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
    """Validate the environment; raise ConfigError on anything unusable."""
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not configured")
    key = env.get("BACKUP_ENCRYPTION_KEY", "")
    if not key:
        raise ConfigError("BACKUP_ENCRYPTION_KEY is not set — cannot decrypt any backup")
    if len(key) < MIN_KEY_LENGTH:
        raise ConfigError(
            f"BACKUP_ENCRYPTION_KEY is shorter than {MIN_KEY_LENGTH} characters — "
            "this cannot be the key the backups were written with"
        )
    scratch = scratch_db_name(
        database_url,
        env.get("RESTORE_CHECK_DB_SUFFIX", "").strip() or "_restore_check",
        env.get("RESTORE_CHECK_DB", ""),
    )
    return Config(
        database_url=database_url,
        backup_dir=Path(env.get("BACKUP_DIR", "").strip() or "~/backups/db").expanduser(),
        scratch_db=scratch,
        precreated=env.get("RESTORE_CHECK_PRECREATED", "").strip().lower() in {"1", "true", "yes"},
        max_ignored_errors=_int_env(env, "RESTORE_CHECK_MAX_IGNORED_ERRORS", 0, 0),
        timeout_seconds=_int_env(env, "BACKUP_TIMEOUT_SECONDS", 3600, 1),
    )


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise DrillError("tooling", f"required binary {name!r} not found on PATH")
    return path


def _run(stage: str, argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one external command; any failure to execute becomes a DrillError."""
    try:
        # S603 suppression rationale: argv is built from validated
        # env config, shutil.which-resolved binaries and
        # identifier-checked DB names; shell=False throughout.
        return subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DrillError(stage, f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise DrillError(stage, f"failed to execute {argv[0]}: {exc}") from exc


def _check(stage: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "; ".join(detail[-5:]) if detail else "no output"
        raise DrillError(stage, f"exit code {result.returncode}: {tail}")


class _Drill:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.psql = _require_binary("psql")
        self.pg_restore = _require_binary("pg_restore")
        self.openssl = _require_binary("openssl")
        self.admin_url = libpq_url(cfg.database_url)
        self.scratch_url = replace_database(cfg.database_url, cfg.scratch_db)

    def _psql(self, stage: str, url: str, sql: str) -> str:
        result = _run(
            stage,
            [self.psql, url, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
            self.cfg.timeout_seconds,
        )
        _check(stage, result)
        return result.stdout.strip()

    def scalar(self, stage: str, sql: str) -> int:
        out = self._psql(stage, self.scratch_url, sql)
        try:
            return int(out)
        except ValueError as exc:
            raise DrillError(stage, f"expected an integer from {sql!r}, got {out!r}") from exc

    def create_scratch(self) -> None:
        if self.cfg.precreated:
            # No CREATEDB privilege: reset the pre-created scratch DB's
            # schema instead. Identifier safety enforced by load_config.
            self._psql(
                "create_scratch",
                self.scratch_url,
                "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public",
            )
            return
        self._psql(
            "create_scratch",
            self.admin_url,
            f'DROP DATABASE IF EXISTS "{self.cfg.scratch_db}" WITH (FORCE)',
        )
        self._psql("create_scratch", self.admin_url, f'CREATE DATABASE "{self.cfg.scratch_db}"')

    def drop_scratch(self) -> None:
        if self.cfg.precreated:
            # Leave no restored financial data at rest in the scratch DB.
            self._psql(
                "drop_scratch",
                self.scratch_url,
                "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public",
            )
            return
        self._psql(
            "drop_scratch",
            self.admin_url,
            f'DROP DATABASE IF EXISTS "{self.cfg.scratch_db}" WITH (FORCE)',
        )

    def decrypt(self, encrypted: Path, plaintext: Path) -> None:
        _check(
            "decrypt",
            _run(
                "decrypt",
                [
                    self.openssl,
                    "enc",
                    *DECRYPT_ARGS,
                    "-in",
                    str(encrypted),
                    "-out",
                    str(plaintext),
                    "-pass",
                    "env:BACKUP_ENCRYPTION_KEY",
                ],
                self.cfg.timeout_seconds,
            ),
        )
        if not plaintext.exists() or plaintext.stat().st_size == 0:
            raise DrillError("decrypt", "decryption produced an empty file")

    def restore(self, plaintext: Path) -> int:
        """Restore into the scratch DB; returns the ignored-error count."""
        listing = _run(
            "verify_dump", [self.pg_restore, "--list", str(plaintext)], self.cfg.timeout_seconds
        )
        _check("verify_dump", listing)
        if not listing.stdout.strip():
            raise DrillError("verify_dump", "pg_restore --list returned an empty table of contents")

        result = _run(
            "restore",
            [
                self.pg_restore,
                "--no-owner",
                "--no-privileges",
                f"--dbname={self.scratch_url}",
                str(plaintext),
            ],
            self.cfg.timeout_seconds,
        )
        if result.returncode == 0:
            return 0
        # pg_restore exits non-zero even for errors it ignored and kept
        # going past; tolerate at most the configured budget of those,
        # anything else is a failed restore.
        match = _IGNORED_ERRORS_RE.search(result.stderr or "")
        if match is not None:
            ignored = int(match.group(1))
            if ignored <= self.cfg.max_ignored_errors:
                logger.warning(
                    f"restore finished with {ignored} ignored error(s) "
                    f"(budget {self.cfg.max_ignored_errors}); stderr tail: "
                    + "; ".join((result.stderr or "").strip().splitlines()[-5:])
                )
                return ignored
            raise DrillError(
                "restore",
                f"{ignored} error(s) ignored on restore exceeds the budget of "
                f"{self.cfg.max_ignored_errors} (RESTORE_CHECK_MAX_IGNORED_ERRORS); "
                "stderr tail: " + "; ".join((result.stderr or "").strip().splitlines()[-5:]),
            )
        _check("restore", result)
        return 0  # unreachable: _check raised

    def sanity_report(self) -> dict[str, int | str]:
        self._psql("sanity", self.scratch_url, _UNFORCE_RLS_SQL)
        alembic = self._psql("sanity", self.scratch_url, "SELECT version_num FROM alembic_version")
        report: dict[str, int | str] = {"alembic": alembic or "MISSING"}
        for table, query in _COUNT_QUERIES.items():
            report[table] = self.scalar("sanity", query)
        report["imbalanced_tenants"] = self.scalar("sanity", _IMBALANCE_SQL)
        return report


def run_drill(cfg: Config) -> tuple[str, dict[str, int | str], int]:
    """Full drill. Returns (backup filename, sanity report, ignored errors)."""
    if not cfg.backup_dir.is_dir():
        raise DrillError("locate", f"backup directory {cfg.backup_dir} does not exist")
    name = latest_backup(p.name for p in cfg.backup_dir.iterdir() if p.is_file())
    if name is None:
        raise DrillError("locate", f"no {BACKUP_PREFIX}*{BACKUP_SUFFIX} files in {cfg.backup_dir}")
    logger.info(f"drilling restore of {name} into scratch database {cfg.scratch_db!r}")

    drill = _Drill(cfg)
    plaintext = cfg.backup_dir / (name + ".drill.tmp")
    scratch_created = False
    try:
        drill.decrypt(cfg.backup_dir / name, plaintext)
        drill.create_scratch()
        scratch_created = True
        ignored = drill.restore(plaintext)
        report = drill.sanity_report()
    finally:
        plaintext.unlink(missing_ok=True)
        if scratch_created:
            try:
                drill.drop_scratch()
            except DrillError as exc:
                # Never mask the original failure; a leftover scratch DB
                # is loud in the log and cleaned up by the next drill.
                logger.error(f"cleanup failed (scratch DB may linger): {exc}")

    if report["alembic"] == "MISSING":
        raise DrillError("sanity", "restored database has no alembic_version row")
    if report["tenants"] == 0:
        raise DrillError("sanity", "restored database contains zero tenants")
    if report["imbalanced_tenants"] != 0:
        raise DrillError(
            "sanity",
            f"ledger invariant violated: {report['imbalanced_tenants']} tenant(s) "
            "where SUM(debits) <> SUM(credits) in the restored copy",
        )
    return name, report, ignored


def main() -> int:
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        logger.error(f"RESTORE_CHECK FAILURE stage=config error={exc}")
        return 1
    try:
        name, report, ignored = run_drill(cfg)
    except DrillError as exc:
        logger.error(f"RESTORE_CHECK FAILURE stage={exc.stage} error={exc}")
        return 1
    except Exception:
        # Blind on purpose: the greppable FAILURE line must always be emitted.
        logger.exception("unexpected error")
        logger.error("RESTORE_CHECK FAILURE stage=unexpected error=see traceback above")
        return 1
    fields = " ".join(f"{key}={value}" for key, value in report.items())
    logger.info(f"RESTORE_CHECK SUCCESS backup={name} {fields} ignored_errors={ignored}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
