# MochaHost deployment (shared cPanel hosting)

Manual deployment runbook for the account described on 2026-08-11: shared
cPanel hosting (not VPS/dedicated), Python 3.12.13 and Node 20.20.2+
available via cPanel's Application Manager (Phusion Passenger), Postgres
available via phpPgAdmin/remote database access, SSH access present. This
is **not** part of the GitLab CI pipeline (`docs/technical/operations.md`)
— CI remains the sole arbiter of code correctness; this document only
covers getting a built, tested artifact running on infrastructure this
repo has no other record of.

Domains: bare domain → frontend (Next.js), `api.<domain>` → backend
(FastAPI).

## 0. Read this first: OTP delivery is not built — staging decision recorded 2026-08-11

**OTP delivery (SMS/email) is not built.** `request_otp` in
`backend/src/genesis/application/auth.py` returns the issued code
in-process only; the API layer (`backend/src/genesis/api/auth.py:94-99`)
surfaces it exclusively behind `DEV_OTP_DISPLAY`, which
`assert_dev_otp_display_dev_only` (`backend/src/genesis/settings.py`)
**refuses to boot** if enabled outside `ENVIRONMENT=development`. Staff
sign-in has no password — it is OTP-only (`backend/scripts/seed_dev.sql`'s
`users` rows carry no credential column).

**Decision (2026-08-11): this MochaHost deployment is STAGING, not
production**, specifically so people can start testing the app today
without blocking on a real SMS/email provider. This deployment runs with
`ENVIRONMENT=development` + `DEV_OTP_DISPLAY=true` on purpose — the
`environment` setting has exactly one effect anywhere in the backend (the
boot guard above; grepped, it's the only reference), so this does not
silently change any other behavior. **This combination must never be used
for the real production deployment** — that one needs a real delivery
provider wired in first.

**Update (security-hardening review): the delivery SEAM is now built,
the transport still is not.** Issued OTPs ride the transactional outbox
with routing fields (`channel`, `destination`) and the dispatcher hands
them to the OTP delivery port
(`backend/src/genesis/application/otp_delivery.py`); the first concrete
adapter (`backend/src/genesis/infrastructure/otp_delivery.py`) only
LOGS the dispatch (masked destination, never the code). Wiring a real
SMS/email gateway is now an infrastructure-only change: implement
`OtpChannelProvider` and register it in `default_otp_delivery()` —
after which `DEV_OTP_DISPLAY` should be retired from this deployment
and `ENVIRONMENT` set honestly (the boot guard then enforces it).

**What "staging with DEV_OTP_DISPLAY=true" actually exposes**: with this
flag on, `POST /auth/otp/request` returns the plaintext OTP in its JSON
response to *any* caller who supplies a valid sign-in identifier — not
just the real owner of that email/phone. It's rate-limited
(`AUTH_RATE_LIMIT_PER_MINUTE`, default 60/min) but not otherwise
restricted, so on a fully public staging URL this is a real
account-takeover surface for anyone who knows or guesses a staff member's
email/phone. Two independent mitigations, both cheap on cPanel, both
recommended:
- **cPanel → Security → Directory Privacy** (or an `.htaccess` HTTP Basic
  Auth rule) on the `api.<domain>` app root, so the staging API itself
  needs a shared password before anything else runs.
- Keep the staging user roster small and don't reuse real members'
  contact details as sign-in identifiers while this is on.

Because this is staging, the backend env var table in §3 below uses
`ENVIRONMENT=development` and `DEV_OTP_DISPLAY=true`, not the
`production`/unset pairing a real go-live would use.

## 0b. SSH is blocked at the host's network edge — use the cPanel path instead

**Diagnosed 2026-08-11, conclusively.** A TCP port scan of the server
(`69.72.248.125` / `s9678.lon1.stableserver.net`) from the deploying
network returned:

| Port | Service | Result |
|---|---|---|
| 21 | FTP | filtered (timeout) |
| **22** | **SSH** | **filtered (timeout)** |
| 80 / 443 | web | OPEN |
| 2082 / 2083 | cPanel | OPEN (HTTP 200) |
| 2086 / **2087** | **WHM** | OPEN (HTTP 200) |

The server is fully reachable; exactly two ports (21 and 22) are
filtered. This rules out every explanation support checked: it is not the
account's shell setting (support confirmed `/bin/bash` is set), not an
IP block on the source address (`102.206.97.58` — support unblocked it,
no change), and not the client (SSH to `github.com:22` from the same
machine works normally). ICMP is also dropped (100% packet loss), which
is consistent with an edge firewall filtering by port/protocol rather
than by source.

**Do not spend more time on SSH.** Everything this runbook needs is
reachable over the open cPanel/WHM ports:

| Need | SSH command | cPanel equivalent |
|---|---|---|
| Get code onto the server | `git clone` / `scp` | **Git Version Control** (cPanel Files section — clones a repo directly), or **File Manager** upload |
| Install Python deps | `pip install --require-hashes -r requirements.txt` then `pip install --no-deps --no-build-isolation -e .` | **Setup Python App → Run Pip Install** (uses the app's own venv) |
| Install Node deps | `npm ci` | **Setup Node.js App → Run NPM Install** |
| Run migrations | `alembic upgrade head` | **Cron Jobs** — a cron entry is arbitrary shell execution; schedule it a minute out, redirect output to a log, read the log in File Manager (§3.5a) |
| Run arbitrary SQL | `psql -f file.sql` | **phpPgAdmin** (paste and execute) |
| Background workers | long-running daemon | **Cron Jobs** — already the plan regardless (§3a) |

If a browser shell is wanted anyway: **WHM on port 2087 is open** and the
reseller account (`hightech`) may have a **Terminal** feature there,
depending on the ACLs the parent host granted. Worth one click; not
required by anything below.

### Optional: the one message that would actually end the support thread

If you still want SSH opened (convenience only, nothing here needs it),
this is the specific, verifiable request — unlike "SSH doesn't work",
an admin can act on it immediately:

> A TCP scan of 69.72.248.125 from 102.206.97.58 shows ports 80, 443,
> 2082, 2083, 2086 and 2087 all OPEN, while ports 21 and 22 time out.
> The server is reachable and the account's shell is set to /bin/bash,
> so this is a network-edge firewall filtering ports 21/22 — not an
> account setting and not a source-IP block. Please either open inbound
> TCP 22, or tell me the non-standard port sshd actually listens on.

## 1. Architecture mapping

| Local / CI | MochaHost equivalent | Notes |
|---|---|---|
| `docker-compose.yml` postgres service | cPanel PostgreSQL database (phpPgAdmin) | Real Postgres, not a swap to MySQL. |
| Redis (`REDIS_URL`) | *(not provisioned)* | Not in your cPanel toolset, and not required — `rate_limit.py` falls back to an in-process window when `REDIS_URL` is unset, which is correct for a single-process deployment. Leave it unset. |
| `uvicorn` (backend dev server) | cPanel "Setup Python App" (Passenger) | Passenger wants a WSGI callable; `backend/passenger_wsgi.py` (new) adapts the ASGI app with `a2wsgi`. |
| `next dev` / `next start` (frontend) | cPanel "Setup Node.js App" (Passenger) | Passenger's Node integration runs a startup file that listens on `process.env.PORT`; `web/server.js` (new) does that. |
| `outbox_worker.run_worker()` / `export_worker.run_worker()` / `idempotency_worker.run_worker()` / `dormancy_worker.run_worker()` (persistent loops) | cPanel Cron Jobs calling one-shot scripts | Shared/Passenger hosting cannot keep a `while True` daemon alive. Each worker already exposes a single-pass function under its loop (`run_dispatch_cycle`, `run_purge_cycle`, `run_export_cycle`, `run_dormancy_cycle`) — `backend/scripts/cron_*.py` (new) call those once per invocation; nothing about the workers' own logic changed. |
| `alembic upgrade head` (CI / local) | Same command, run once via SSH | See §3. |
| `scripts/seed_dev.sql` (fixture data) | `backend/scripts/provision_tenant.sql` (new) | Dev seed truncates and reseeds fixture members/loans on every run; the production script runs once, creates one real tenant + branch + System Admin role + your own user, and leaves everything else to be configured from the Access Control screen. |

## 2. Provision the database

1. cPanel → PostgreSQL Databases: create a database and a database user,
   grant the user all privileges on the database. Note the resulting
   names (cPanel prefixes both with your cPanel username, e.g.
   `cpuser_genesis` / `cpuser_app`).
2. Check the Postgres server version (phpPgAdmin → server properties, or
   `psql --version` over SSH). `gen_random_uuid()` is built into core
   Postgres from **v13 onward** — if it's older than that, this needs a
   different plan before continuing.
3. Migration `0001_schema_v1.py` runs `CREATE EXTENSION IF NOT EXISTS
   pgcrypto;` as a precaution. On a PG13+ host this line is a no-op in
   practice but Postgres still checks the privilege to run it — if your
   database user isn't allowed to create extensions (common on shared
   hosting), the very first migration fails on that line before it does
   anything else. If that happens: open a MochaHost support ticket asking
   them to enable/install the `pgcrypto` extension for your database —
   this is a routine, safe request most managed-Postgres providers grant
   without hesitation (pgcrypto needs no elevated runtime trust).
4. cPanel → Databases → "Remote Database Access": temporarily allow your
   current IP, only if you intend to run migrations from your own machine
   instead of via SSH (§3 uses SSH and doesn't need this).

## 3. Backend: Python app + migrations

1. cPanel → Setup Python App → Create Application: Python **3.12.13**,
   application root e.g. `api`, application URL `api.<domain>`,
   startup file `passenger_wsgi.py`, entry point `application`.
2. Set environment variables in that same screen (values, not
   placeholders — generate real secrets with the commands below; none of
   these are ever committed to the repo):

   | Variable | How to set it |
   |---|---|
   | `DATABASE_URL` | `postgresql+psycopg://USER:PASSWORD@localhost:5432/DBNAME` from step 2 |
   | `ENVIRONMENT` | `development` — **staging-only choice** (§0): required for `DEV_OTP_DISPLAY=true` to boot at all; a real production deployment must use `production` instead, with a real OTP provider wired in first |
   | `DEV_OTP_DISPLAY` | `true` — **staging-only** (§0); set up the Directory Privacy mitigation from §0 before or immediately after this goes live |
   | `JWT_SIGNING_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `OTP_PEPPER` | same command, a different value |
   | `CURSOR_SIGNING_KEY` | same command, a different value (≥32 bytes; boot fails closed otherwise) |
   | `CURSOR_KEY_VERSION` | `1` |
   | `CORS_ORIGINS` | `https://<bare-domain>` (the frontend origin — no trailing slash) |
   | `AUTH_RATE_LIMIT_PER_MINUTE` | leave default (60) unless you have a reason to change it |
   | `REDIS_URL` | leave unset |

3. Upload the repo (or just `backend/` plus the repo root files it needs)
   to the application root, or `git clone` it there over SSH.
4. SSH in, activate the venv cPanel created (path shown on the Setup
   Python App page, typically
   `~/virtualenv/api/3.12/bin/activate`), then:
   ```
   cd ~/api
   pip install -e ".[dev]"    # or: pip install -r requirements.txt (runtime only)
   ```
5. Run migrations once, same venv:
   ```
   alembic upgrade head
   ```
   Confirm the extension privilege issue from §2.3 doesn't fire here; if
   it does, stop and resolve that with MochaHost support before
   continuing — don't work around it by hand-editing the migration.
6. Run the tenant bootstrap once (edit the placeholders in the file
   first — your real SACCO name/slug, bylaws figures, and your own
   sign-in email/phone):
   ```
   psql "$DATABASE_URL" -f scripts/provision_tenant.sql
   ```
   Save the printed `tenant_id` — it goes into the frontend's
   `NEXT_PUBLIC_TENANT_ID` in §4.
7. Restart the app from the Setup Python App screen so it picks up the
   uploaded code and env vars.
8. Smoke test: `curl https://api.<domain>/healthz` and `/readyz`.

### 3a. Background jobs (cron, not a daemon)

cPanel → Cron Jobs. Use the same venv's `python` binary. Adjust the app
root path to match step 1:

```
*/2 * * * *  /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/cron_outbox.py >> /home/USER/logs/cron_outbox.log 2>&1
*/2 * * * *  /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/cron_export.py >> /home/USER/logs/cron_export.log 2>&1
7 * * * *    /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/cron_idempotency_purge.py >> /home/USER/logs/cron_idempotency.log 2>&1
30 2 * * *   /home/USER/virtualenv/api/3.12/bin/python /home/USER/api/scripts/cron_dormancy.py >> /home/USER/logs/cron_dormancy.log 2>&1
```

Create `~/logs/` first (`mkdir -p ~/logs`). Each script exits non-zero on
a real failure, which is what makes cPanel's cron failure email useful —
don't redirect stderr to `/dev/null`.

The one-shots guard themselves with per-worker session-level Postgres
advisory locks (`genesis.infrastructure.cron_lock`): a tick that finds its
worker's lock held logs a skip and exits 0. Two caveats (#21):

- **Best-effort overlap reduction, not mutual exclusion.** The lock
  session idles in a transaction for the whole cycle, so
  `idle_in_transaction_session_timeout`, a DB restart, or a network blip
  can free the lock mid-cycle and let the next tick overlap the
  still-running cycle (safe — the workers are SKIP LOCKED and
  idempotent). A lost lock is logged as a WARNING (`advisory lock … was
  no longer held at cycle end`) in the worker's cron log — watch for it.
- **Transaction pooling breaks session advisory locks.** Behind pgbouncer
  in transaction-pooling mode the lock and unlock land on different
  backends and the guard silently stops guarding. Fine on today's direct
  connections; re-check as part of the hosting exit (#11) before fronting
  the app with a transaction pooler.

## 4. Frontend: Node app

1. cPanel → Setup Node.js App → Create Application: Node **20.20.2**,
   application root e.g. `web`, application URL the bare domain, startup
   file `server.js`.
2. Upload/clone the repo's `web/` directory to the application root.
3. Create `web/.env.production` from `web/.env.production.example`:
   ```
   NEXT_PUBLIC_API_BASE_URL=https://api.<domain>
   NEXT_PUBLIC_TENANT_ID=<the tenant_id printed in §3.6>
   ```
   These are `NEXT_PUBLIC_*` values — Next.js **inlines them at build
   time** (`src/lib/env.ts`), so this file must exist *before* the build
   in the next step, not just before `server.js` starts.
4. SSH in, in the Node app's environment (cPanel gives you an "Enter to
   this virtual environment" command on the Setup Node.js App page — run
   that first so `npm`/`node` resolve to the versions you picked):
   ```
   cd ~/web
   npm ci
   npm run build
   ```
5. Restart the app from the Setup Node.js App screen.
6. Smoke test: load `https://<bare-domain>` — expect the sign-in screen
   (not a working sign-in, per §0).

## 5. Verification checklist

- [ ] `curl https://api.<domain>/healthz` → 200
- [ ] `curl https://api.<domain>/readyz` → 200 (confirms DB connectivity)
- [ ] Frontend loads at the bare domain, requests hit `api.<domain>` (check
      browser network tab — confirms `NEXT_PUBLIC_API_BASE_URL` baked in
      correctly)
- [ ] Cron jobs firing (check `~/logs/*.log` after a few minutes)
- [ ] `POST /auth/otp/request` response includes `dev_otp` (confirms the
      staging OTP-display path works end to end)
- [ ] §0's Directory Privacy / Basic Auth mitigation applied on
      `api.<domain>` before sharing the staging URL beyond the immediate
      testing group

## 6. What this deliberately does not do

- No containerization — shared hosting has no Docker. If MochaHost's plan
  ever changes to a VPS, the existing `docker-compose.yml` pattern becomes
  the better fit and most of this document stops applying.
- No CI/CD wiring to this host — deploys here are manual (SSH + cPanel UI)
  until/unless that's explicitly wanted; GitLab CI continues to be the
  sole arbiter of whether code is correct, this only covers where a
  correct build actually runs.
