"""One-shot offsite copy of the newest encrypted dump, for cPanel cron.

An on-host backup dies with the host (issue #25; runbook §4). This
script pushes the newest ``genesis-*.dump.enc`` from BACKUP_DIR to an
S3-compatible bucket over plain HTTPS — the only outbound channel the
MochaHost edge firewall leaves open (ports 21/22 are filtered, so
scp/rsync/FTP are off the table). It emits exactly one greppable
``BACKUP_OFFSITE SUCCESS``/``BACKUP_OFFSITE FAILURE`` line and pings
an optional heartbeat, same dead-man semantics as the dump itself.

Security posture (matches backup_db.py):

- the artifact is **already encrypted** before it leaves the host —
  the bucket never holds plaintext member financial data;
- the S3 secret key stays in the environment; it is used in-process
  for HMAC signing (AWS Signature Version 4) and never reaches argv,
  a subprocess, or a log line;
- give the credential **write-only** access (PutObject only — no
  list/read/delete) so a compromised host cannot destroy history, and
  set bucket-side lifecycle/retention (e.g. 90 days, versioned).

Stdlib-only like its siblings (urllib + hmac + hashlib — no boto3, no
venv): DR tooling must keep working when the app venv is broken. Runs
under any python3 >= 3.8. Deploy alongside backup_common.py.

Environment (see docs/technical/backup-and-restore.md §4):
  BACKUP_DIR                  default ~/backups/db
  OFFSITE_S3_ENDPOINT         required, https:// base endpoint of the
                              S3-compatible service, e.g.
                              https://s3.us-west-004.backblazeb2.com
                              (path-style addressing is used)
  OFFSITE_S3_BUCKET           required
  OFFSITE_S3_REGION           default us-east-1 (B2/R2 accept any)
  OFFSITE_S3_ACCESS_KEY_ID    required
  OFFSITE_S3_SECRET_ACCESS_KEY  required; env-only, never argv/logs
  OFFSITE_S3_PREFIX           default db/ (object key prefix)
  OFFSITE_TIMEOUT_SECONDS     default 3600 (whole upload)
  OFFSITE_HEARTBEAT_URL       optional https:// heartbeat check URL,
                              same semantics as BACKUP_HEARTBEAT_URL

Example cron line (nightly at 01:45, after the 01:30 dump):
  45 1 * * * . /home/USER/.genesis_backup_env && \
    /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/offsite_backup.py >> /home/USER/logs/backup_offsite.log 2>&1
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from backup_common import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    ConfigError,
    StageError,
    latest_backup,
    script_logger,
    send_heartbeat,
)

logger = script_logger(__file__)

OffsiteError = StageError

_ALGORITHM = "AWS4-HMAC-SHA256"
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Config:
    backup_dir: Path
    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    prefix: str
    timeout_seconds: int


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment; raise ConfigError on anything unusable.

    OFFSITE_S3_SECRET_ACCESS_KEY is validated for presence but NOT
    stored on the Config object — it stays in the environment and is
    read only at signing time, mirroring the BACKUP_ENCRYPTION_KEY
    discipline in backup_db.py.
    """
    endpoint = env.get("OFFSITE_S3_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise ConfigError("OFFSITE_S3_ENDPOINT is not configured")
    if not endpoint.startswith("https://"):
        raise ConfigError(
            "OFFSITE_S3_ENDPOINT must be https:// — member financial data "
            "artifacts do not travel over plaintext transports"
        )
    if urlsplit(endpoint).path not in ("", "/"):
        raise ConfigError("OFFSITE_S3_ENDPOINT must be a bare host endpoint (no path)")
    bucket = env.get("OFFSITE_S3_BUCKET", "").strip()
    if not bucket:
        raise ConfigError("OFFSITE_S3_BUCKET is not configured")
    access_key_id = env.get("OFFSITE_S3_ACCESS_KEY_ID", "").strip()
    if not access_key_id:
        raise ConfigError("OFFSITE_S3_ACCESS_KEY_ID is not configured")
    if not env.get("OFFSITE_S3_SECRET_ACCESS_KEY", ""):
        raise ConfigError("OFFSITE_S3_SECRET_ACCESS_KEY is not set")
    prefix = env.get("OFFSITE_S3_PREFIX", "").strip() or "db/"
    if not prefix.endswith("/"):
        prefix += "/"
    timeout_raw = env.get("OFFSITE_TIMEOUT_SECONDS", "").strip() or "3600"
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError as exc:
        raise ConfigError(
            f"OFFSITE_TIMEOUT_SECONDS must be an integer, got {timeout_raw!r}"
        ) from exc
    if timeout_seconds < 1:
        raise ConfigError(f"OFFSITE_TIMEOUT_SECONDS must be >= 1, got {timeout_seconds}")
    return Config(
        backup_dir=Path(env.get("BACKUP_DIR", "").strip() or "~/backups/db").expanduser(),
        endpoint=endpoint,
        bucket=bucket,
        region=env.get("OFFSITE_S3_REGION", "").strip() or "us-east-1",
        access_key_id=access_key_id,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
    )


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    payload_hash: str,
    now: datetime,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Compute AWS Signature Version 4 headers for a single request.

    Hand-rolled on purpose: the DR constraint is stdlib-only (no
    boto3). Verified against the worked example in the AWS SigV4
    documentation — see the unit test, whose expected signature is the
    documented value, not an output captured from this code.
    """
    parts = urlsplit(url)
    host = parts.netloc
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    headers = {"host": host, "x-amz-date": amz_date}
    if extra_headers:
        headers.update({k.lower(): v for k, v in extra_headers.items()})

    signed_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_uri = quote(parts.path or "/", safe="/-_.~")
    # Query strings are already in canonical (sorted, encoded) form for
    # every URL this script builds; assert rather than re-encode.
    canonical_query = parts.query
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    key = _hmac_sha256(("AWS4" + secret_access_key).encode("utf-8"), datestamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, service)
    key = _hmac_sha256(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    result = dict(headers)
    result["Authorization"] = authorization
    del result["host"]  # urllib sets Host itself; it must not be duplicated
    return result


def object_url(cfg: Config, name: str) -> str:
    """Path-style object URL — works on every S3-compatible service."""
    return f"{cfg.endpoint}/{quote(cfg.bucket, safe='')}/{quote(cfg.prefix + name, safe='/')}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(cfg: Config, path: Path, secret_access_key: str) -> None:
    """PUT one file to the bucket; raise OffsiteError on any failure."""
    url = object_url(cfg, path.name)
    payload_hash = file_sha256(path)
    size = path.stat().st_size
    headers = sigv4_headers(
        method="PUT",
        url=url,
        region=cfg.region,
        service="s3",
        access_key_id=cfg.access_key_id,
        secret_access_key=secret_access_key,
        payload_hash=payload_hash,
        now=datetime.now(timezone.utc),  # noqa: UP017 — 3.8-compat, see backup_common
        extra_headers={
            "x-amz-content-sha256": payload_hash,
            "content-length": str(size),
        },
    )
    with path.open("rb") as body:
        # S310 suppression rationale: load_config pins the endpoint to
        # https:// and the URL is built from validated config.
        request = urllib.request.Request(  # noqa: S310
            url, data=body, method="PUT", headers=headers
        )
        try:
            # S310 suppression rationale: load_config pins the endpoint
            # to https:// and the URL is built from validated config.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=cfg.timeout_seconds
            ) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise OffsiteError(
                "upload", f"HTTP {exc.code} from object store: {detail.strip()}"
            ) from exc
        except OSError as exc:
            raise OffsiteError("upload", f"upload failed: {exc}") from exc
    if status not in (200, 201):
        raise OffsiteError("upload", f"unexpected HTTP status {status} from object store")


def run_offsite(cfg: Config, env: Mapping[str, str]) -> tuple[str, int]:
    """Upload the newest dump. Returns (filename, size in bytes)."""
    if not cfg.backup_dir.is_dir():
        raise OffsiteError("locate", f"backup directory {cfg.backup_dir} does not exist")
    name = latest_backup(p.name for p in cfg.backup_dir.iterdir() if p.is_file())
    if name is None:
        raise OffsiteError(
            "locate", f"no {BACKUP_PREFIX}*{BACKUP_SUFFIX} files in {cfg.backup_dir}"
        )
    path = cfg.backup_dir / name
    size = path.stat().st_size
    if size == 0:
        raise OffsiteError("locate", f"{name} is empty — refusing to upload a broken artifact")
    upload(cfg, path, env["OFFSITE_S3_SECRET_ACCESS_KEY"])
    return name, size


def main() -> int:
    heartbeat = os.environ.get("OFFSITE_HEARTBEAT_URL", "").strip()
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        logger.error(f"BACKUP_OFFSITE FAILURE stage=config error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    try:
        name, size = run_offsite(cfg, os.environ)
    except OffsiteError as exc:
        logger.error(f"BACKUP_OFFSITE FAILURE stage={exc.stage} error={exc}")
        send_heartbeat(heartbeat, ok=False)
        return 1
    except Exception:
        # Blind on purpose: the greppable FAILURE line must always be emitted.
        logger.exception("unexpected error")
        logger.error("BACKUP_OFFSITE FAILURE stage=unexpected error=see traceback above")
        send_heartbeat(heartbeat, ok=False)
        return 1
    logger.info(
        f"BACKUP_OFFSITE SUCCESS file={name} bytes={size} "
        f"target={cfg.endpoint}/{cfg.bucket}/{cfg.prefix}"
    )
    send_heartbeat(heartbeat, ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
