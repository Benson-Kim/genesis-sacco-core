"""One-shot restore drill for the encrypted pg_dump backups, for cPanel cron.

A backup that has never been restored is a hope, not a backup. This
script proves the newest dump written by scripts/backup_db.py is
actually restorable:

  1. decrypts the latest ``genesis-*.dump.enc`` in BACKUP_DIR,
  2. verifies ``pg_restore --list`` can read it,
  3. restores it into a scratch database (default ``<dbname>_restore_check``),
  4. runs sanity assertions — the alembic head is present, row-count
     floors on tenants / members / transactions / ledger_entries
     (a hollow restore must NOT pass — RESTORE_CHECK_MIN_ROWS below),
     and the double-entry invariant (per tenant, SUM of debit amounts
     equals SUM of credit amounts on ledger_entries),
  5. drops the scratch database again,

and emits exactly one machine-greppable ``RESTORE_CHECK
SUCCESS``/``RESTORE_CHECK FAILURE`` line. Alert on the *absence* of
SUCCESS in the weekly log window, which also covers crashes.

The drill can never aim destructive work at the live database: the
scratch name is validated against the live name at config time AND
re-asserted before any DROP/CREATE/restore runs (the fail-closed
refusal-guard posture of issue #34). The database password never
reaches argv or logs — password-free URLs go into argv, the password
rides in PGPASSWORD (see backup_common).

Like backup_db.py this is deliberately stdlib-only (no ``genesis``
import) and runs under any python3 >= 3.8: DR tooling must keep
working when the app venv is broken. Shared plumbing lives in the
sibling module scripts/backup_common.py, deployed alongside this file.
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
  RESTORE_CHECK_MIN_ROWS            default 1: minimum row count the
                                    restored members / transactions /
                                    ledger_entries tables must each have
                                    for the drill to pass. Set to 0 only
                                    for a pre-launch database that has
                                    genuinely never posted a transaction.
  RESTORE_CHECK_MAX_IGNORED_ERRORS  default 0; raise only for known-benign
                                    restore noise (e.g. a CREATE EXTENSION
                                    privilege refusal on shared hosting)
  BACKUP_TIMEOUT_SECONDS            default 3600 (per external command)
  RESTORE_CHECK_HEARTBEAT_URL       optional https:// heartbeat check URL
                                    (e.g. Healthchecks.io); pinged on
                                    success, <url>/fail on failure —
                                    the service alerts on ping ABSENCE.
                                    Required for production (runbook §3)

Example cron line (weekly, Monday 03:15 — drills the newest dump):
  15 3 * * 1 . /home/USER/.genesis_backup_env && \
    /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/verify_restore.py >> /home/USER/logs/verify_restore.log 2>&1
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from backup_common import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    DECRYPT_ARGS,
    ConfigError,
    StageError,
    check,
    child_env,
    connection_args,
    int_env,
    libpq_url,
    parse_backup_timestamp,
    pre_create_private,
    redact_url,
    require_binary,
    require_encryption_key,
    run,
    script_logger,
    send_heartbeat,
)

logger = script_logger(__file__)

DrillError = StageError

#: Scratch DB names are interpolated into SQL as identifiers; restrict
#: them to characters that need no quoting games.
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: pg_restore's summary line; LC_ALL=C in the child env (backup_common.
#: child_env) pins the message to English so this match is deterministic.
_IGNORED_ERRORS_RE = re.compile(r"errors ignored on restore: (\d+)")

#: Code-owned literal queries (never string-built): the sanity row
#: counts asserted/reported in the drill's SUCCESS line.
_COUNT_QUERIES = {
    "tenants": "SELECT count(*) FROM tenants",
    "members": "SELECT count(*) FROM members",
    "transactions": "SELECT count(*) FROM transactions",
    "ledger_entries": "SELECT count(*) FROM ledger_entries",
}

#: Tables whose restored row counts must clear the configured floor —
#: a restore that silently lost the financial rows must fail the drill
#: (MR !5 finding M2), because for a ledger a restore that "succeeds"
#: with wrong data is worse than one that fails.
_FLOORED_TABLES = ("members", "transactions", "ledger_entries")

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


@dataclass(frozen=True)
class Config:
    database_url: str
    backup_dir: Path
    scratch_db: str
    precreated: bool
    min_rows: int
    max_ignored_errors: int
    timeout_seconds: int


def database_name(url: str) -> str:
    """Extract the database name from a connection URL.

    The error message redacts the URL: a cron log line must never
    carry the embedded password (MR !5 finding B2).
    """
    name = urlsplit(libpq_url(url)).path.lstrip("/")
    if not name:
        raise ConfigError(
            f"DATABASE_URL has no database name: cannot derive one from {redact_url(url)!r}"
        )
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


def latest_backup(names: Iterable[str]) -> str | None:
    """Pick the newest backup by encoded timestamp; foreign files are ignored."""
    dated = [(ts, name) for name in names if (ts := parse_backup_timestamp(name)) is not None]
    if not dated:
        return None
    return max(dated)[1]


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment; raise ConfigError on anything unusable."""
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is not configured")
    require_encryption_key(
        env,
        missing="cannot decrypt any backup",
        short="this cannot be the key the backups were written with",
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
        min_rows=int_env(env, "RESTORE_CHECK_MIN_ROWS", 1, 0),
        max_ignored_errors=int_env(env, "RESTORE_CHECK_MAX_IGNORED_ERRORS", 0, 0),
        timeout_seconds=int_env(env, "BACKUP_TIMEOUT_SECONDS", 3600, 1),
    )


class _Drill:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.psql = require_binary("psql")
        self.pg_restore = require_binary("pg_restore")
        self.openssl = require_binary("openssl")
        # Password-free URLs for argv; password rides in PGPASSWORD (B3).
        self.admin_url, password = connection_args(cfg.database_url)
        self.env = child_env(password)
        self.scratch_url = replace_database(self.admin_url, cfg.scratch_db)
        # Defense in depth (issue #34 fail-closed posture): load_config
        # already refused a scratch name equal to the live DB, but this
        # class runs DROP DATABASE — re-assert here so no future caller
        # can construct a _Drill that aims at the live database.
        if database_name(self.admin_url) == cfg.scratch_db:
            raise DrillError(
                "config",
                f"refusing to run: scratch database {cfg.scratch_db!r} equals the live database",
            )

    def _assert_scratch_target(self, stage: str, url: str) -> None:
        """Refuse any destructive statement not aimed at the scratch DB."""
        if database_name(url) != self.cfg.scratch_db:
            raise DrillError(
                stage,
                f"refusing destructive operation: target {redact_url(url)!r} is not "
                f"the scratch database {self.cfg.scratch_db!r}",
            )

    def _psql(self, stage: str, url: str, sql: str) -> str:
        result = run(
            stage,
            [self.psql, url, "-X", "-A", "-t", "--no-password", "-v", "ON_ERROR_STOP=1", "-c", sql],
            self.cfg.timeout_seconds,
            env=self.env,
        )
        check(stage, result)
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
            self._assert_scratch_target("create_scratch", self.scratch_url)
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
        # TEMPLATE template0: template1 may carry site-local objects
        # that collide with the restore (MR !5 review, minor finding).
        self._psql(
            "create_scratch",
            self.admin_url,
            f'CREATE DATABASE "{self.cfg.scratch_db}" TEMPLATE template0',
        )

    def drop_scratch(self) -> None:
        if self.cfg.precreated:
            # Leave no restored financial data at rest in the scratch DB.
            self._assert_scratch_target("drop_scratch", self.scratch_url)
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
        # Pre-create 0600: during the drill the entire plaintext
        # financial DB sits in this file; never readable via cron umask.
        pre_create_private(plaintext)
        check(
            "decrypt",
            run(
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
        listing = run(
            "verify_dump",
            [self.pg_restore, "--list", str(plaintext)],
            self.cfg.timeout_seconds,
            env=self.env,
        )
        check("verify_dump", listing)
        if not listing.stdout.strip():
            raise DrillError("verify_dump", "pg_restore --list returned an empty table of contents")

        self._assert_scratch_target("restore", self.scratch_url)
        result = run(
            "restore",
            [
                self.pg_restore,
                "--no-owner",
                "--no-privileges",
                "--no-password",
                f"--dbname={self.scratch_url}",
                str(plaintext),
            ],
            self.cfg.timeout_seconds,
            env=self.env,
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
        check("restore", result)
        return 0  # unreachable: check raised

    def sanity_report(self) -> dict[str, int | str]:
        self._psql("sanity", self.scratch_url, _UNFORCE_RLS_SQL)
        alembic = self._psql("sanity", self.scratch_url, "SELECT version_num FROM alembic_version")
        report: dict[str, int | str] = {"alembic": alembic or "MISSING"}
        for table, query in _COUNT_QUERIES.items():
            report[table] = self.scalar("sanity", query)
        report["imbalanced_tenants"] = self.scalar("sanity", _IMBALANCE_SQL)
        return report


def evaluate_report(report: Mapping[str, int | str], min_rows: int) -> None:
    """Assert the restored copy is sane; raise DrillError otherwise.

    Pure decision logic, unit-tested directly: this is the gate that
    decides whether a restore drill passes, and for a financial ledger
    a drill that passes on a hollow or imbalanced restore is worse
    than one that fails (MR !5 finding M2).
    """
    if report["alembic"] == "MISSING":
        raise DrillError("sanity", "restored database has no alembic_version row")
    if report["tenants"] == 0:
        raise DrillError("sanity", "restored database contains zero tenants")
    for table in _FLOORED_TABLES:
        count = report[table]
        if isinstance(count, int) and count < min_rows:
            raise DrillError(
                "sanity",
                f"restored {table} has {count} row(s), below the floor of {min_rows} "
                "(RESTORE_CHECK_MIN_ROWS) — refusing to certify a hollow restore",
            )
    if report["imbalanced_tenants"] != 0:
        raise DrillError(
            "sanity",
            f"ledger invariant violated: {report['imbalanced_tenants']} tenant(s) "
            "where SUM(debits) <> SUM(credits) in the restored copy",
        )


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

    evaluate_report(report, cfg.min_rows)
    return name, report, ignored


def main() -> int:
    # Read the heartbeat URL independently of load_config so even a
    # config-stage failure still emits the explicit /fail ping (#27).
    heartbeat = os.environ.get("RESTORE_CHECK_HEARTBEAT_URL", "").strip()
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        logger.error(f"RESTORE_CHECK FAILURE stage=config error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    try:
        name, report, ignored = run_drill(cfg)
    except DrillError as exc:
        logger.error(f"RESTORE_CHECK FAILURE stage={exc.stage} error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    except Exception:
        # Blind on purpose: the greppable FAILURE line must always be emitted.
        logger.exception("unexpected error")
        logger.error("RESTORE_CHECK FAILURE stage=unexpected error=see traceback above")
        send_heartbeat(heartbeat, ok=False)
        return 1
    fields = " ".join(f"{key}={value}" for key, value in report.items())
    logger.info(f"RESTORE_CHECK SUCCESS backup={name} {fields} ignored_errors={ignored}")
    send_heartbeat(heartbeat, ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
