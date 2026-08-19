# Database backup and restore

Backup, restore-verification and disaster-recovery runbook for the
PostgreSQL 16 database. Companion to
[mochahost-deployment.md](mochahost-deployment.md) — the same shared
cPanel host, the same constraints (Passenger app slots, cron jobs
available, ports 21/22 filtered at the network edge, no Docker). The
executable pieces are `backend/scripts/backup_db.py` (nightly encrypted
dump) and `backend/scripts/verify_restore.py` (weekly restore drill);
this document is the operational contract around them.

For a financial ledger, an unverified backup is a hope, not a backup:
**every backup is exercised** — the nightly job refuses to report
success until `pg_restore --list` can read the dump it just wrote, and
the weekly drill restores the newest dump into a scratch database and
re-proves the double-entry invariant on the restored copy.

## 1. Recovery objectives (current, honest numbers)

| Objective | Target | Why |
|---|---|---|
| **RPO** (max data loss) | **up to 24 hours** | Nightly logical dumps (`pg_dump`). Any transaction posted after the last successful dump is lost in a restore. |
| **RTO** (max downtime) | **≤ 4 hours** | Manual procedure (§7): fetch dump → decrypt → restore → smoke-test. The weekly drill keeps the restore path known-working; the monthly manual drill (§6) keeps a human current on it. |

**A 24-hour RPO is a stopgap for a system holding member money, not an
end state.** The required next step is **WAL archiving /
point-in-time recovery (PITR)**, which brings RPO down to minutes. It
needs hosting capability shared cPanel does not offer: filesystem
access to the Postgres server's WAL directory and control over
`archive_command`/`archive_library` in `postgresql.conf` (or a managed
Postgres with PITR built in). Concretely, any of:

- a VPS/dedicated plan running its own Postgres (then: `pgBackRest` or
  `wal-g` shipping WAL to object storage), or
- a managed database service with PITR (most offer ~5-minute RPO), or
- MochaHost enabling streaming replication to a replica we control.

Until one of those exists, the honest statement to stakeholders is:
*a database loss can cost up to one day of postings, and the recovery
playbook below re-enters them from the paper/M-PESA trail.*

**Regulatory framing (SASRA / CBK):** supervisory expectations for
deposit-taking institutions in Kenya — SASRA's DT-SACCO regime and
CBK's business-continuity guidance — include *demonstrable* business
continuity and data recoverability: tested restores, documented
recovery objectives, and records that survive the loss of a site. A
24-hour recovery point on member deposit records is not merely an
engineering stopgap; it is the kind of gap a supervisor writes up as a
finding. Treat WAL/PITR (issue #26) as a compliance obligation with a
deadline, not an optimisation, and keep the drill logs from §6 — they
are the evidence of recoverability an examiner will ask for.

## 2. What the nightly backup does

`backend/scripts/backup_db.py`, scheduled from cPanel Cron Jobs:

1. Preflight: verifies the connecting role has `BYPASSRLS` (or is
   superuser) and refuses to run otherwise — see §2a for why this is a
   hard requirement of this schema.
2. `pg_dump --format=custom` (compressed, `pg_restore`-selectable)
   against `DATABASE_URL` — a consistent snapshot transaction; it does
   not block writers. The database password never enters the process
   argument list (argv is world-readable on a shared host) or any log
   line: the scripts strip it from the URL and hand it to the
   PostgreSQL client tools via `PGPASSWORD` only.
3. Verifies the dump is non-empty and `pg_restore --list` can read its
   table of contents — a truncated/corrupt dump fails the run *now*,
   not on restore day.
4. Encrypts it: `openssl enc -aes-256-cbc -md sha256 -pbkdf2 -iter
   600000 -salt`, key read from the `BACKUP_ENCRYPTION_KEY` env var
   (never argv, so it never shows in `ps`). **The run fails loudly if
   the key is unset — an unencrypted backup is never written.** The
   plaintext temp file is created mode `0600` and deleted even on
   failure.
5. Prunes to the retention policy: newest `BACKUP_RETENTION_DAILY`
   (default 7) dumps plus the newest dump of each of the newest
   `BACKUP_RETENTION_WEEKLY` (default 4) ISO calendar weeks. The
   weekly tier is bucketed by ISO week — not by "taken on a Sunday" —
   because cron fires at host-local time while filenames are stamped
   UTC, and in any zone east of UTC+1 a "Sunday" run stamps a Saturday
   file; ISO-week bucketing cannot silently starve the weekly tier.
   Pruning only ever touches files matching the script's own naming
   scheme (`genesis-<UTC-stamp>.dump.enc`).
6. Emits exactly one greppable line:
   `BACKUP_DB SUCCESS file=... bytes=... pruned=...` or
   `BACKUP_DB FAILURE stage=... error=...`.

`openssl enc` has no authenticated (AEAD) mode; tamper-evidence comes
from the weekly drill actually restoring the artifact end-to-end.

## 2a. Dump role privileges — FORCE ROW LEVEL SECURITY

Migration `0001` (and later ones) puts `FORCE ROW LEVEL SECURITY` on
every tenant table, with policies keyed on the application's tenant
GUC. `FORCE` applies to the table *owner* too, and `pg_dump` runs with
`row_security = off` by default, so **a dump role without `BYPASSRLS`
(or superuser) fails on the first RLS-forced table** — this is exactly
what a freshly cPanel-provisioned PostgreSQL user looks like.
`backup_db.py` therefore preflights the role and refuses to run with
an actionable error (`BACKUP_DB FAILURE stage=preflight`) instead of
letting `pg_dump` fail obscurely at 01:30.

**Never "fix" this with `pg_dump --enable-row-security`.** That flag
makes the dump run as an RLS-subject and silently exports only
policy-visible rows — for this schema, an *empty or partial* backup of
member financial data that looks like a success. A silent partial
backup is strictly worse than a loud failure.

Obtaining `BYPASSRLS` on shared cPanel hosting requires a support
ticket (`ALTER ROLE <user> BYPASSRLS` needs superuser); if MochaHost
declines, this is another hard dependency on the hosting exit (#11) —
record the outcome here. The first production run of `backup_db.py`
is the acceptance test for this section: it either preflights clean
and produces a dump, or it names the missing privilege.

## 3. Cron wiring (cPanel)

Same pattern as §3a of the MochaHost runbook. cron does **not**
inherit the Passenger app's environment variables, so first create a
key file the cron lines source:

```
# once, over the cPanel Terminal or a one-off cron line:
umask 077
cat > ~/.genesis_backup_env <<'EOF'
export DATABASE_URL='postgresql+psycopg://USER:PASS@localhost:5432/DBNAME'
export BACKUP_ENCRYPTION_KEY='<64-hex-char key, see §5>'
export BACKUP_DIR="$HOME/backups/db"
EOF
chmod 600 ~/.genesis_backup_env
mkdir -p ~/backups/db ~/logs
```

Then cPanel → Cron Jobs (adjust the app-root path as in the deployment
runbook):

```
# nightly dump at 01:30 (each week's newest dump doubles as the weekly)
30 1 * * *  . /home/USER/.genesis_backup_env && /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/backup_db.py >> /home/USER/logs/backup_db.log 2>&1

# weekly restore drill, Monday 03:15 (drills the newest dump on disk)
15 3 * * 1  . /home/USER/.genesis_backup_env && /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/verify_restore.py >> /home/USER/logs/verify_restore.log 2>&1
```

Both scripts are stdlib-only on purpose — they run under any `python3`
**version 3.8 or newer** on the host even if the app venv is broken,
which is exactly the situation a DR script must survive (they
deliberately avoid 3.11-only stdlib conveniences). They share the
sibling module `scripts/backup_common.py`: **deploy all three files
together** — the two entrypoints import it from their own directory,
no venv or install step needed. Exit codes are non-zero on real
failures; don't redirect stderr to `/dev/null` (cPanel's cron failure
email is part of the alerting).

**Dead-man switch (required for production — issue #27):** monitoring
must alert on the *absence* of the SUCCESS signal, not the presence of
FAILURE — a host that never ran cron prints nothing. The scripts have
the heartbeat built in: set `BACKUP_HEARTBEAT_URL` and
`RESTORE_CHECK_HEARTBEAT_URL` in `~/.genesis_backup_env` and each run
pings its check URL on success and `<url>/fail` on any failure
(including config-stage failures); a ping that simply stops arriving —
crash, dead cron, broken python — trips the same alarm. Setup, once:

1. Create two checks on an external heartbeat service reachable over
   outbound HTTPS (Healthchecks.io works from this host, §4):
   - *nightly dump*: period 1 day, grace 2 h (cron fires 01:30);
   - *weekly drill*: period 7 days, grace 6 h (cron fires Mon 03:15);
   - *offsite upload*: period 1 day, grace 2 h (cron fires 01:45; §4).
2. Put each check's ping URL in `~/.genesis_backup_env`
   (`BACKUP_HEARTBEAT_URL=...`, `RESTORE_CHECK_HEARTBEAT_URL=...`).
3. Route the service's alerts to **every operator on the DR rota**
   (at least two channels — e-mail plus SMS/messenger integration),
   never to a single inbox.

A failed ping never fails the run — the missed ping *is* the alert; a
monitoring outage must not block backups. As a stopgap only, a local
check — POSIX `sh` (cPanel cron does **not** run bash) and bounded to
today, so a days-old SUCCESS cannot satisfy it:

```
tail -n 200 ~/logs/backup_db.log | grep "BACKUP_DB SUCCESS" | tail -n 1 \
  | grep -q "$(date +%Y-%m-%d)" || echo "NO BACKUP TODAY"
```

## 4. Offsite copies (ports 21/22 are filtered)

An on-host backup dies with the host. The MochaHost edge firewall
filters inbound ports 21 (FTP) and 22 (SSH) — conclusively diagnosed in
[mochahost-deployment.md §0b](mochahost-deployment.md#0b-ssh-is-blocked-at-the-hosts-network-edge--use-the-cpanel-path-instead)
— so `scp`/`rsync`/FTP *into* the host are all off the table. What is
reachable: **HTTP/HTTPS in both directions** (80/443 open inbound;
outbound HTTPS from the host works — the deploy path itself uses
cPanel Git clones over HTTPS).

Two workable strategies, in order of preference:

1. **Push from the host over HTTPS (preferred — no new inbound
   surface, and now implemented — issue #25).**
   `backend/scripts/offsite_backup.py` uploads the newest
   `genesis-*.dump.enc` to any S3-compatible bucket (Backblaze B2 /
   Cloudflare R2 / AWS S3) with a stdlib-only AWS SigV4 `PUT` — no
   boto3, no venv, same DR constraints as its siblings. Configure in
   `~/.genesis_backup_env`: `OFFSITE_S3_ENDPOINT` (https:// enforced),
   `OFFSITE_S3_BUCKET`, `OFFSITE_S3_REGION`,
   `OFFSITE_S3_ACCESS_KEY_ID`, `OFFSITE_S3_SECRET_ACCESS_KEY` (env
   only — used in-process for signing, never argv or logs), optional
   `OFFSITE_S3_PREFIX` and `OFFSITE_HEARTBEAT_URL`. It emits one
   greppable `BACKUP_OFFSITE SUCCESS/FAILURE` line and pings its
   heartbeat, same dead-man semantics as the dump (§3). Schedule it
   right after the dump:

   ```
   45 1 * * * . /home/USER/.genesis_backup_env && /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/offsite_backup.py >> /home/USER/logs/backup_offsite.log 2>&1
   ```

   Give the upload credential **write-only** access to the bucket
   (PutObject only — no list/read/delete) so a compromised host cannot
   destroy history, and set the bucket's own lifecycle/retention
   (e.g. 90 days, versioned).

2. **Fetch from outside over HTTPS.** cPanel exposes the filesystem
   over the open cPanel ports; an external machine you control can
   fetch the newest dump daily via the cPanel/UAPI file-download
   endpoint (token-authenticated, HTTPS) and store it locally. Use a
   scoped cPanel API token, not the account password. This keeps all
   credentials off the host but makes the *external* machine's cron
   the thing to monitor.

Either way the artifact leaving the host is **already encrypted** —
the object store / fetching machine never holds plaintext, so a bucket
leak alone does not expose member financial data. Verify the offsite
copy monthly as part of the manual drill (§6): download one dump from
offsite (not from the host) and restore it.

## 5. Key management — `BACKUP_ENCRYPTION_KEY`

- **Generate:** `python -c "import secrets; print(secrets.token_hex(32))"`
  (64 hex chars; the scripts refuse keys shorter than 32 chars).
- **Store on the host** only in `~/.genesis_backup_env`, `chmod 600`.
- **Escrow off the host — this is the rule that matters:** a backup
  encrypted with a key that only lived on the dead host is a shredder,
  not a backup. Keep the key in at least two places that do not share
  fate with the host: the organisation's password manager (a vault
  entry named `genesis BACKUP_ENCRYPTION_KEY`, access limited to the
  operators on the DR rota) and a sealed printed copy with whoever
  holds the SACCO's other statutory records. **Never** in the repo, CI
  variables visible to the pipeline, or the same bucket as the dumps.
- **Rotate** on operator departure or suspected exposure: generate a
  new key, update env file + escrow, take an immediate manual backup
  with the new key, and keep the old key in escrow (marked retired)
  until every dump encrypted with it has aged out of retention —
  including offsite copies. Nothing yet binds a dump file to the key
  that encrypted it — restore-day key ambiguity after a rotation is
  tracked in issue #28 (key-id in the filename).
- **Year-end archival dump:** the rolling retention caps history at a
  few weeks, which satisfies DR but not audit. Take one manual dump at
  each financial year-end, copy it offsite (§4), and retain it for as
  long as the SACCO's statutory record-keeping obligations require —
  with the key that opens it escrowed for the same period.
- **Decrypt by hand** (parameters are pinned; also in the script
  docstrings):

  ```
  openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 600000 \
    -in genesis-<stamp>.dump.enc -out restore.dump -pass env:BACKUP_ENCRYPTION_KEY
  ```

## 6. Restore drills

A restore procedure nobody has run is folklore. Two cadences:

- **Weekly, automated** — `verify_restore.py` (cron line in §3):
  decrypts the newest dump, restores into a scratch DB
  (`<dbname>_restore_check`; it refuses — at config time *and* again
  before every destructive statement — to target the live database),
  checks the alembic head is present, **asserts row-count floors** on
  members/transactions/ledger_entries (each must have at least
  `RESTORE_CHECK_MIN_ROWS` rows, default 1 — a hollow restore that
  lost the financial rows must fail the drill, because a restore that
  "succeeds" with wrong data is worse than one that fails; set 0 only
  for a pre-launch DB that has genuinely never posted), and re-proves
  the ledger invariant — per tenant, `SUM(debits) = SUM(credits)` over
  `ledger_entries` — on the restored copy, then drops the scratch DB.
  Greppable `RESTORE_CHECK SUCCESS/FAILURE` line; same
  absence-alerting as §3.
  If the DB role lacks `CREATEDB` (common on shared hosting): create
  the scratch DB once via cPanel → PostgreSQL Databases and set
  `RESTORE_CHECK_PRECREATED=true` — the drill then resets the scratch
  DB's `public` schema instead of dropping the database. If shared
  hosting refuses `CREATE EXTENSION pgcrypto` during restore, that one
  benign error can be budgeted with
  `RESTORE_CHECK_MAX_IGNORED_ERRORS=1` (default 0 — strict).
- **Monthly, manual full drill** — a human executes §7 end-to-end
  against a scratch database, **starting from the offsite copy**, and
  records in the operations log: date, dump used, time-to-restore,
  row counts, invariant result, and any surprise. The point is
  currency of people, not just of scripts; RTO in §1 is only credible
  while this log stays current.

## 7. Disaster recovery — step by step

Scenario: the live database is lost or corrupted beyond repair.

1. **Freeze writes.** Stop the API app (cPanel → Setup Python App →
   Stop) and disable the app cron jobs (outbox/export/dormancy/purge)
   so nothing writes to a half-restored system. Note the wall-clock
   time.
2. **Choose the dump.** Newest `genesis-*.dump.enc` from
   `$BACKUP_DIR`; if the host is gone, from the offsite copy (§4).
   Its filename timestamp defines the data-loss window (RPO): every
   posting after it must be re-entered in step 8.
3. **Decrypt** with the escrowed key (§5) on the recovery machine.
4. **Verify before touching anything:** `pg_restore --list
   restore.dump` must print a table of contents.
5. **Provision the target database.** Same-host recovery: cPanel →
   PostgreSQL Databases → create a fresh DB + grant the app user (do
   not restore over the corrupted DB — keep it for forensics).
   New-host recovery: follow mochahost-deployment.md §2 first
   (including the pgcrypto note).
6. **Restore:**

   ```
   pg_restore --no-owner --no-privileges \
     --dbname="postgresql://USER:PASS@localhost:5432/NEWDB" restore.dump
   ```

7. **Sanity-check the restored data** (the same checks the drill
   automates): `SELECT version_num FROM alembic_version;` matches the
   deployed code's migration head (`ls backend/migrations/versions |
   sort | tail -1` — valid while revision files stay zero-padded,
   true today); row counts on tenants/members/transactions look
   right; the §6 ledger invariant returns zero imbalanced tenants. If
   the code deployed is newer than the dump, run `alembic upgrade
   head` now.
8. **Reconcile the gap.** Pull the M-PESA/bank statements and paper
   records for the window since the dump timestamp and re-enter
   postings through the normal API flows (never raw SQL — the ledger
   triggers and audit trail must see them). Two people, one entering,
   one checking against the statement.
9. **Repoint and restart.** Update `DATABASE_URL` in the Passenger
   app's env (and `~/.genesis_backup_env`!) to the new DB, start the
   app, re-enable cron, smoke-test `/healthz` + `/readyz` + a sign-in.
10. **Take an immediate backup** of the restored DB (run
    `backup_db.py` by hand) so the recovery point is itself protected,
    and confirm the nightly line is still scheduled.
11. **Write the incident up**: cause, dump used, minutes of data
    re-entered, total downtime vs the §1 RTO, and any step of this
    document that was wrong — then fix the document.

## 8. Environment variable reference

| Variable | Script | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | both | — (required) | SQLAlchemy-style accepted; driver marker stripped for libpq tools |
| `BACKUP_ENCRYPTION_KEY` | both | — (required, ≥ 32 chars) | fail-closed: no key, no backup |
| `BACKUP_DIR` | both | `~/backups/db` | created `0700`; dumps written `0600` |
| `BACKUP_RETENTION_DAILY` | backup | `7` | newest N dumps kept |
| `BACKUP_RETENTION_WEEKLY` | backup | `4` | newest dump of each of the newest N ISO weeks kept |
| `BACKUP_TIMEOUT_SECONDS` | both | `3600` | per external command |
| `BACKUP_HEARTBEAT_URL` | backup | — (required in prod) | https:// check URL; pinged on success, `/fail` on failure (§3) |
| `RESTORE_CHECK_HEARTBEAT_URL` | drill | — (required in prod) | https:// check URL; same semantics (§3) |
| `OFFSITE_S3_ENDPOINT` | offsite | — (required) | https:// S3-compatible endpoint; plaintext transports refused (§4) |
| `OFFSITE_S3_BUCKET` / `OFFSITE_S3_REGION` | offsite | — / `us-east-1` | bucket name; region for SigV4 |
| `OFFSITE_S3_ACCESS_KEY_ID` / `OFFSITE_S3_SECRET_ACCESS_KEY` | offsite | — (required) | write-only credential; secret env-only, never argv/logs |
| `OFFSITE_S3_PREFIX` | offsite | `db/` | object key prefix |
| `OFFSITE_TIMEOUT_SECONDS` | offsite | `3600` | whole upload |
| `OFFSITE_HEARTBEAT_URL` | offsite | — (required in prod) | https:// check URL; same semantics (§3) |
| `RESTORE_CHECK_DB` | drill | `<dbname>_restore_check` | explicit scratch DB name; live-DB collision refused |
| `RESTORE_CHECK_DB_SUFFIX` | drill | `_restore_check` | used when `RESTORE_CHECK_DB` unset |
| `RESTORE_CHECK_PRECREATED` | drill | `false` | for roles without `CREATEDB` (§6) |
| `RESTORE_CHECK_MIN_ROWS` | drill | `1` | per-table floor for members/transactions/ledger_entries; `0` only pre-launch (§6) |
| `RESTORE_CHECK_MAX_IGNORED_ERRORS` | drill | `0` | budget for known-benign restore errors (§6) |
