# Hosting-exit migration runbook (shared cPanel → target architecture)

Operational procedure for executing the hosting exit decided in
[ADR-0009](../adr/0009-hosting-exit-target-architecture.md) (issue #11,
gap register G6). Companion documents:

- [`mochahost-deployment.md`](mochahost-deployment.md) — the outgoing host,
  its constraints, and its cron-based worker wiring.
- `backup-and-restore.md` (MR !5) — the dump/drill tooling this runbook
  reuses; deploys `backend/scripts/backup_db.py`, `verify_restore.py`,
  `backup_common.py`.
- [`hosting-exit-cost-tiers.md`](hosting-exit-cost-tiers.md) — what the
  target costs monthly.

**Precondition:** the single human decision from ADR-0009 has been made —
provider and monthly budget approved. Nothing below is provider-specific
beyond that choice.

**Status:** this runbook is written ahead of the migration. Sections marked
`[MEASURE]` must be filled with real numbers during the rehearsal (§4) before
the production cutover is scheduled.

## 1. Objectives and budgets

| Item | Budget | Rationale |
|---|---|---|
| **Downtime budget** | **≤ 2 hours** planned, announced window | Write-freeze → final dump → restore → verify → DNS cutover. The rehearsal (§4) must demonstrate the data steps fit in ≤ 1 hour to leave margin. `[MEASURE]` actual rehearsal time: ___ |
| **Rollback window** | **72 hours** after cutover | The old host stays frozen but intact. Within the window, rollback = repoint DNS + unfreeze (accepting loss of postings made on the new host — see §7 for the reconciliation rule). After the window, the old host is decommissioned and rollback becomes a restore exercise. |
| **RPO during migration** | **0** | The final dump is taken *after* the write freeze; no posting may occur between freeze and dump. |
| **RPO after migration** | minutes (provider PITR) + 24h independent logical dump | ADR-0009 part 1; #26 closes. |

Schedule the window at the lowest-traffic time the SACCO's operations allow
and announce it to staff in advance (members: the member surface is
read-only/limited today, but announce anyway — precedent matters).

## 2. Mandatory pre-cutover checklist

These three items are **blocking**. The cutover does not proceed with any of
them undecided or unset.

### 2a. MANDATORY — Pooler-mode decision (issue #21)

`backend/src/genesis/infrastructure/cron_lock.py` guards the four worker
one-shots (outbox, export, idempotency purge, dormancy) with **session-level**
`pg_try_advisory_lock`. Session advisory locks **break behind pgbouncer in
transaction-pooling mode**: the lock and the unlock land on different
backends and the guard *silently stops guarding* (documented in the module
docstring and `mochahost-deployment.md` §3a; the caveat is #21 item 3, and
#21/#26 both name the hosting exit as the point where this must be re-checked).

**Decision (this runbook, explicit):** **worker DSNs use session pooling or
connect directly to Postgres — never transaction pooling.**

- If the topology fronts Postgres with pgbouncer (or the provider's pooler)
  for the **API** connection budget, that pooler may run in transaction mode
  **for the API DSN only** — note that transaction pooling also constrains
  other session state (prepared statements, `SET` without `LOCAL`); the #2
  capacity tranche owns validating the API under it (§2c).
- The **worker/cron DSN is a separate connection string** pointing either
  directly at Postgres or at a pooler in **session** mode. The workers
  are four low-frequency processes; they do not need transaction pooling's
  connection multiplexing, and correctness of the overlap guard wins.
- Redesigning the locks (e.g. `pg_try_advisory_xact_lock`) was considered
  and rejected: the guard must span a whole multi-transaction cycle, which
  a transaction-scoped lock cannot (see the cron_lock module docstring —
  this is why it is session-level in the first place).

Checklist:

- [ ] Worker/cron `DATABASE_URL` verified to be direct-or-session-pooled
      (run one cycle, confirm no `advisory lock … was no longer held`
      WARNING and no skipped-tick anomaly).
- [ ] If any pooler is deployed: its mode per DSN recorded here: ___
- [ ] Connection budget documented (max_connections vs API pool size ×
      nodes + 4 workers + backup role): ___

### 2b. MANDATORY — `TRUSTED_PROXY_IPS` set to the real proxy chain (MR !3)

MR !3's trusted-proxy resolution defaults to **empty = never trust
`X-Forwarded-For`**. On the target topology every request arrives via the
reverse proxy (and the edge in front of it), so with the default, per-IP rate
limiting would key every client on the proxy's own address — one shared
bucket for the whole world.

At cutover, set `TRUSTED_PROXY_IPS` to the **actual immediate-peer proxy
addresses** (the reverse proxy's private-network address as seen by the API
container; the edge's ranges belong in the *proxy's* real-IP config, not
here). The setting fail-closes on malformed entries at boot (MR !3) — a typo
is a refused boot, not silent shared-bucket behavior.

- [ ] `TRUSTED_PROXY_IPS` set to the real chain: ___
- [ ] Verified from the outside: two different client IPs hitting
      `/auth/otp/request` land in **different** rate buckets (observe 429
      independence), and the API access log shows real client IPs.
- [ ] Proxy configured to **overwrite** (never append to) inbound
      `X-Forwarded-For` from the edge, and the origin only accepts traffic
      from the edge's published ranges (ADR-0009 part 4).

### 2c. MANDATORY — Re-run the #2 load-test harness; write the capacity statement

Issue #2's load-test harness (MR !17, landing under `backend/scripts/` —
draft at the time of writing) models the panic-withdrawal scenario and the
hot-row contention probe. Any capacity number measured on Passenger+a2wsgi
shared hosting is void on the new topology.

- [ ] Run the harness against the **new** topology (staging clone of the
      target, or the production stack inside the cutover window before DNS
      moves).
- [ ] Write the capacity statement into `docs/technical/` as #2 requires:
      measured RPS per endpoint class, saturation behavior, connection-pool
      ceiling (from §2a's budget), and the scaling path (add API node /
      scale DB tier). `[MEASURE]` — this closes the capacity tranche of #2.
- [ ] If the harness has not merged by cutover time: the cutover may proceed
      (the old host has *no* measured capacity either — this must not become
      the reason to stay on worse infrastructure), but the capacity statement
      becomes the first post-cutover task and #2 stays open until it exists.

## 3. Pre-migration build-out (no downtime)

1. **Provision** per ADR-0009: managed Postgres (PITR enabled, PG16+,
   `pgcrypto` available, app role + **`BYPASSRLS` dump role** — the
   `backup-and-restore.md` §2a requirement; the first `backup_db.py` run is
   the acceptance test), compute nodes on a private network, Redis, reverse
   proxy + edge, secrets manager.
2. **Secrets custody**: mint new values *in* the secrets manager per
   ADR-0009 part 6 (rotate-into, never copy). `DATABASE_URL` (new host),
   `REDIS_URL`, new `BACKUP_ENCRYPTION_KEY` (old key stays in escrow, marked
   retired), new `CURSOR_SIGNING_KEY` with the old value as
   `CURSOR_SIGNING_KEY_PREVIOUS` for the dual-key window, new `OTP_PEPPER`
   at cutover, `TRUSTED_PROXY_IPS` per §2b, `ENVIRONMENT=production` — which
   means `DEV_OTP_DISPLAY` must be **absent** (the boot guard refuses it) and
   therefore a **real OTP delivery provider must be wired first**
   (`mochahost-deployment.md` §0: implement `OtpChannelProvider`, register in
   `default_otp_delivery()`). **OTP delivery is a hard prerequisite of
   production cutover** — staff sign-in is OTP-only.
3. **Deploy the stack** (API containers, workers, proxy) pointed at the new
   empty database; run `alembic upgrade head`;
   `psql -f scripts/provision_tenant.sql` is NOT run — production data
   arrives by restore, not re-provisioning.
4. **Wire backups on day one**: nightly `backup_db.py` + weekly
   `verify_restore.py` + offsite upload + heartbeat (#27 wiring), exactly per
   `backup-and-restore.md` §3–4. PITR is enabled at the provider; record the
   retention window: ___

## 4. Rehearsal — dump/restore drill against the new database (MANDATORY, no downtime)

This reuses the restore-verification tooling from MR !5 rather than inventing
a parallel path. Run it end-to-end at least once before scheduling the
window; it doubles as the `[MEASURE]` source for §1.

1. Take a fresh encrypted dump on the old host (`backup_db.py` by hand) or
   use last night's artifact from the offsite copy.
2. On a machine that can reach the new managed Postgres:
   `export DATABASE_URL=<new-host URL>`, `BACKUP_ENCRYPTION_KEY=<key that
   encrypted the dump — the OLD key>`, `BACKUP_DIR=<dir holding the dump>`,
   and run **`backend/scripts/verify_restore.py`**. It decrypts the newest
   dump, restores into a scratch DB on the *new* server, checks the alembic
   head, asserts row-count floors on members/transactions/ledger_entries,
   and re-proves the per-tenant double-entry invariant — then drops the
   scratch DB. A `RESTORE_CHECK SUCCESS` line against the new server is the
   proof the migration's data path works. (If the provider role lacks
   `CREATEDB`, pre-create the scratch DB and set
   `RESTORE_CHECK_PRECREATED=true` — same escape hatch as shared hosting.)
3. Record: dump size ___, decrypt+restore wall-clock ___, drill result ___.
4. Fix anything that surprised you, then re-run until boring.

## 5. Cutover procedure (the downtime window)

Lower DNS TTLs to 300s at least 48h before the window (both the bare domain
and `api.<domain>`).

1. **T0 — freeze writes on the old host.** Stop the Passenger API app,
   disable all app cron jobs (`mochahost-deployment.md` §3a list). Note the
   wall-clock time. From here the old database is read-only-by-absence.
2. **Final dump**: run `backup_db.py` by hand on the old host; copy the
   artifact off-host (offsite bucket path from `backup-and-restore.md` §4).
3. **Restore into the new production database**: decrypt and
   `pg_restore --no-owner --no-privileges` into the real (empty) production
   DB — the §4 rehearsal has already proven this path; this run is the same
   commands against the production target.
4. **Verify** (same assertions the drill automates, run by hand against the
   production DB): alembic head matches deployed code (`alembic upgrade
   head` if the deployed code is newer), row counts sane vs the old host's
   last-known counts, per-tenant `SUM(debits)=SUM(credits)` on
   `ledger_entries` returns zero imbalanced tenants.
5. **Smoke-test the new stack against real data** (still pre-DNS): `/healthz`,
   `/readyz`, a real staff sign-in (real OTP delivery!), one read-path check
   per module, one reversible write (e.g. a memo-level action), §2b's rate-
   bucket check.
6. **DNS cutover**: point the bare domain and `api.<domain>` at the edge.
   With 300s TTLs, propagation is minutes. The old host's vhosts may
   additionally be pointed at a static "we've moved" page for stragglers —
   but its API app stays **stopped** (a live old API accepting writes after
   cutover is the worst failure mode of this plan).
7. **Immediate post-cutover backup**: run `backup_db.py` against the new DB
   so the recovery point is itself protected; confirm PITR shows the
   restore-point history.
8. **Un-freeze**: announce the window closed. Note total downtime vs the §1
   budget: ___

## 6. Post-cutover (within the first week)

- [ ] §2c capacity statement written (if not done pre-cutover).
- [ ] First nightly `backup_db.py` SUCCESS line on the new host observed;
      first weekly `verify_restore.py` drill passed; heartbeat (#27) firing.
- [ ] PITR restore test to an arbitrary timestamp performed once on a scratch
      instance — this, plus updating the RPO/RTO table in
      `backup-and-restore.md` §1 with measured numbers, is the **acceptance
      criterion of #26**.
- [ ] `cron_lock` logs reviewed for lock-loss WARNINGs (§2a verification).
- [ ] Old-host secrets treated as burned: confirm every value that ever
      lived on shared disk is now retired or inside its documented
      demotion window (ADR-0009 part 6) — including cPanel-stored copies.
- [ ] Update `mochahost-deployment.md` header: staging role only, or archive.
- [ ] Gap register G6 status updated (entries graduate by shipping, per the
      register's own rule).

## 7. Rollback

**Trigger:** data verification fails at §5.4, the smoke test fails
unexplainably, or a production-blocking defect surfaces inside the 72-hour
window.

- **Inside the window, before meaningful writes on the new host** (the clean
  case): repoint DNS back to the old host, restart the Passenger app and
  cron jobs, announce. Total member-visible impact: the downtime already
  spent.
- **Inside the window, after writes on the new host**: rollback loses those
  postings. The reconciliation rule is the same as the DR runbook's
  (`backup-and-restore.md` §7.8): re-enter from the M-PESA/bank/paper trail
  through the normal API flows, two people, never raw SQL. This is why the
  window is 72h and not longer — the re-entry burden grows with every hour.
- **After the window**: the old host is gone; "rollback" means restoring a
  dump to freshly provisioned infrastructure — i.e., the DR procedure, not
  this runbook.

Decommissioning the old host (after the window): final archival dump, then
delete the apps, databases, and cron jobs; keep the cPanel account only if
something else uses it. Record the date: ___
