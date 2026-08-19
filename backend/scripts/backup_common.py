"""Shared plumbing for the stdlib-only backup / disaster-recovery scripts.

Extracted from backup_db.py and verify_restore.py (MR !5 review, DRY
mandate): the two scripts carried verbatim copies of every helper
below, and this repo's documented failure mode is divergent duplicates
left behind by merges. Sharing a sibling module preserves the DR
constraints the duplication was meant to serve:

- stdlib-only, no ``genesis`` import: DR tooling must keep working
  when the application venv or codebase is broken;
- runnable under the host's system python, **3.8 or newer** —
  the 3.11-only ``UTC`` alias and other late conveniences are deliberately
  avoided (MR !5 finding B4);
- no installation step: python puts the executed script's directory on
  ``sys.path``, so ``import backup_common`` works with no venv as long
  as this file is deployed alongside backup_db.py / verify_restore.py
  (the runbook's deploy list names all three files).

Security posture shared by both scripts (MR !5 findings B2/B3):

- ``BACKUP_ENCRYPTION_KEY`` is validated but never stored, logged or
  put in argv — ``openssl enc -pass env:BACKUP_ENCRYPTION_KEY`` reads
  it from the environment.
- The database password gets the same discipline: ``connection_args``
  splits the URL into a password-free form for argv (argv is
  world-readable via /proc/<pid>/cmdline on a shared host) and a
  ``PGPASSWORD`` entry for the child environment (readable only by
  the owning user); any URL headed for a log line or error message
  must go through ``redact_url`` first.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

BACKUP_PREFIX = "genesis-"
BACKUP_SUFFIX = ".dump.enc"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MIN_KEY_LENGTH = 32

#: Key-id segment inside a backup filename (issue #28):
#: ``genesis-<UTC-stamp>.k<8-hex>.dump.enc``. The id binds the artifact
#: to the BACKUP_ENCRYPTION_KEY that wrote it, so restore day after a
#: rotation is a lookup ("fetch the escrowed key labelled k3f9a2c81"),
#: never a guessing game over opaque openssl "bad decrypt" errors.
#: Filenames without the segment are the pre-#28 legacy form and stay
#: fully supported (retention, drill, offsite copy).
_KEY_ID_RE = re.compile(r"^k([0-9a-f]{8})$")

#: Symmetric-encryption parameters. `openssl enc` offers no AEAD mode;
#: integrity is instead proven end-to-end by scripts/verify_restore.py
#: (decrypt + pg_restore into a scratch DB). The manual decrypt command
#: in the runbook / script docstrings must match these.
ENCRYPT_ARGS = ["-aes-256-cbc", "-md", "sha256", "-pbkdf2", "-iter", "600000", "-salt"]

#: DERIVED from ENCRYPT_ARGS, never hand-mirrored (MR !5 review, DRY
#: mandate): decryption is encryption minus `-salt` (openssl reads the
#: salt back from the file header) plus `-d`.
DECRYPT_ARGS = ["-d", *(arg for arg in ENCRYPT_ARGS if arg != "-salt")]


class ConfigError(ValueError):
    """Environment/configuration is unusable; refuse to run."""


class StageError(RuntimeError):
    """A stage failed; carries the stage name for the greppable FAILURE line."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def script_logger(script_file: str) -> logging.Logger:
    """Timestamped logging so a cron log answers "did this cycle actually
    fire, and when?" — bare stdout prints cannot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger(Path(script_file).stem)


def int_env(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
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


def require_encryption_key(env: Mapping[str, str], *, missing: str, short: str) -> None:
    """Validate BACKUP_ENCRYPTION_KEY without ever returning or storing it.

    The key stays in the process environment, where
    ``openssl enc -pass env:BACKUP_ENCRYPTION_KEY`` reads it without
    the key ever appearing in argv (visible in ``ps``) or logs.
    """
    key = env.get("BACKUP_ENCRYPTION_KEY", "")
    if not key:
        raise ConfigError(f"BACKUP_ENCRYPTION_KEY is not set — {missing}")
    if len(key) < MIN_KEY_LENGTH:
        raise ConfigError(
            f"BACKUP_ENCRYPTION_KEY is shorter than {MIN_KEY_LENGTH} characters — {short}"
        )


def libpq_url(url: str) -> str:
    """Strip a SQLAlchemy driver marker (`postgresql+psycopg://`) for libpq tools."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return scheme.partition("+")[0] + sep + rest


def _netloc(parts: SplitResult, password_repr: str | None) -> str:
    """Rebuild a URL netloc with the password removed or replaced."""
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal needs its brackets back
        host = f"[{host}]"
    auth = quote(parts.username or "", safe="")
    if password_repr is not None:
        auth += f":{password_repr}"
    netloc = f"{auth}@{host}" if auth else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return netloc


def connection_args(url: str) -> tuple[str, str | None]:
    """Split a connection URL into (password-free libpq URL, password).

    The URL is what goes into argv — world-readable via
    /proc/<pid>/cmdline on a shared host — so it must never carry the
    password; the password travels via PGPASSWORD in the child
    environment instead (MR !5 finding B3), mirroring the env-only
    discipline already applied to BACKUP_ENCRYPTION_KEY. The returned
    password is URL-decoded, which is exactly what PGPASSWORD expects.
    """
    parts = urlsplit(libpq_url(url))
    if parts.password is None:
        return urlunsplit(parts), None
    return urlunsplit(parts._replace(netloc=_netloc(parts, None))), unquote(parts.password)


def redact_url(url: str) -> str:
    """Mask the password for log lines and error messages (MR !5 finding B2)."""
    try:
        parts = urlsplit(libpq_url(url))
    except ValueError:
        return "<unparseable database URL>"
    if parts.password is None:
        return urlunsplit(parts)
    return urlunsplit(parts._replace(netloc=_netloc(parts, "***")))


def child_env(password: str | None) -> dict[str, str]:
    """Environment for pg_dump/pg_restore/psql subprocesses.

    - LC_ALL=C pins tool output to English so error parsing (the
      drill's ignored-errors budget) is deterministic under any host
      locale.
    - PGPASSWORD carries the DB password out of argv (finding B3).
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    if password is not None:
        env["PGPASSWORD"] = password
    return env


def key_id(key: str) -> str:
    """8-hex-char identifier of an encryption key (issue #28).

    A SHA-256 prefix identifies the key without revealing usable key
    material; escrow entries must be labelled with the same id
    (runbook §5).
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def backup_filename(ts: datetime, key_id_hex: str | None = None) -> str:
    """Backup filename; embeds the key id when one is supplied (#28)."""
    stamp = ts.strftime(TIMESTAMP_FORMAT)
    middle = f"{stamp}.k{key_id_hex}" if key_id_hex else stamp
    return BACKUP_PREFIX + middle + BACKUP_SUFFIX


def parse_backup_name(name: str) -> tuple[datetime, str | None] | None:
    """Split a backup filename into (timestamp, key id or None).

    Returns None for foreign names — anything unparseable is untouchable
    for retention and invisible to the drill/offsite scripts.
    """
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    middle = name[len(BACKUP_PREFIX) : len(name) - len(BACKUP_SUFFIX)]
    stamp, _, key_part = middle.partition(".")
    kid: str | None = None
    if key_part:
        match = _KEY_ID_RE.fullmatch(key_part)
        if match is None:
            return None
        kid = match.group(1)
    try:
        # Naive-UTC by contract: filenames are always written in UTC.
        return datetime.strptime(stamp, TIMESTAMP_FORMAT), kid
    except ValueError:
        return None


def parse_backup_timestamp(name: str) -> datetime | None:
    """Return the timestamp encoded in a backup filename, or None if foreign."""
    parsed = parse_backup_name(name)
    return None if parsed is None else parsed[0]


def latest_backup(names: Iterable[str]) -> str | None:
    """Pick the newest backup by encoded timestamp; foreign files are ignored."""
    dated = [(ts, name) for name in names if (ts := parse_backup_timestamp(name)) is not None]
    if not dated:
        return None
    return max(dated)[1]


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise StageError("tooling", f"required binary {name!r} not found on PATH")
    return path


def run(
    stage: str,
    argv: list[str],
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one external command; any failure to execute becomes a StageError."""
    try:
        # S603 suppression rationale: argv is built entirely from
        # validated env config, shutil.which-resolved binary paths and
        # identifier-checked DB names; shell=False throughout.
        return subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise StageError(stage, f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise StageError(stage, f"failed to execute {argv[0]}: {exc}") from exc


def check(stage: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "; ".join(detail[-5:]) if detail else "no output"
        raise StageError(stage, f"exit code {result.returncode}: {tail}")


def pre_create_private(path: Path) -> None:
    """Create/truncate `path` with mode 0600 before a tool writes into it.

    pg_dump and openssl open an existing output file without changing
    its mode, so pre-creating pins the plaintext dump at 0600 for its
    whole life regardless of cron's umask — defense in depth on top of
    the 0700 backup directory (MR !5 review, minor finding).
    """
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    # The mode argument only applies on creation; pin pre-existing files too.
    os.chmod(path, 0o600)


def send_heartbeat(url: str, *, ok: bool, timeout: int = 10) -> None:
    """Ping a dead-man-switch heartbeat service (issue #27).

    Healthchecks.io convention: ping ``url`` on success, ``url``/fail
    on failure. The service alerts both on an explicit /fail ping and
    on the *absence* of any ping past the grace window — which also
    covers crashes, a broken system python, and a host that never ran
    cron at all. That absence-based alerting is the actual dead-man
    switch; the /fail ping just makes explicit failures page faster.

    This function never raises and must never change the caller's exit
    code: a failed ping must not turn a good backup into a failed run —
    the missed ping itself is what raises the alarm. Empty URL means
    heartbeat monitoring is not configured (the runbook makes it
    required for production; the scripts stay runnable without it so a
    monitoring outage can never block a backup).
    """
    if not url:
        return
    if not url.startswith("https://"):
        logging.getLogger(__name__).warning("heartbeat URL is not https:// — refusing to ping it")
        return
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        # S310 suppression rationale: scheme is pinned to https just above.
        with urllib.request.urlopen(target, timeout=timeout) as response:  # noqa: S310
            response.read()
    except Exception as exc:  # noqa: BLE001 — deliberate: see docstring
        logging.getLogger(__name__).warning(f"heartbeat ping failed: {exc}")
