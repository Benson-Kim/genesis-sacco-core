# ADR-0009: Hosting exit — target architecture off shared cPanel (G6)

- Status: Proposed
- Date: 2026-08-19
- Deciders: (pending — see "The single human decision" below)

## Context

The system runs on shared cPanel hosting (MochaHost), documented honestly in
[`docs/technical/mochahost-deployment.md`](../technical/mochahost-deployment.md)
as a **staging expedient, not production**. The Institutional Gap Register
(`docs/INSTITUTIONAL_GAP_REGISTER.md`, MR !8) entry **G6** names the gap:

> Shared cPanel host: security boundary includes co-tenants; secrets in env
> vars; staging leaks plaintext OTP (being closed)

against the institutional norm of *"Dedicated/isolated infrastructure, WAF,
DDoS absorption, network segmentation, secrets in KMS/HSM."*

Concretely, on the current host:

- **The security boundary includes every co-tenant on the box.** A
  multi-tenant financial ledger shares a kernel, a filesystem, and a Postgres
  server with strangers. There is no code fix for this.
- **No WAF, no DDoS absorption, no network segmentation.** The API, the
  frontend, and the database all live on one shared machine reachable
  directly from the internet.
- **Secrets are env-file custody on shared disk.** `JWT_SIGNING_KEY`,
  `OTP_PEPPER`, `CURSOR_SIGNING_KEY`(+`_PREVIOUS`), `BACKUP_ENCRYPTION_KEY`
  and `DATABASE_URL` live in cPanel app-env screens and `~/.genesis_backup_env`
  (chmod 600 — on a disk we do not exclusively control).
- **Passenger + a2wsgi discards the async architecture** (the FastAPI app is
  adapted to WSGI; workers are cron one-shots because shared hosting cannot
  keep a daemon alive — `mochahost-deployment.md` §3a).
- **Backups are capped at a 24-hour RPO.** The backup runbook
  (`docs/technical/backup-and-restore.md`, MR !5, §1) states plainly that
  nightly `pg_dump` is *"a stopgap for a system holding member money, not an
  end state"* — WAL archiving / PITR needs hosting capability shared cPanel
  does not offer (issue #26). The same runbook (§2a) records that the
  `FORCE ROW LEVEL SECURITY` schema requires a `BYPASSRLS` dump role, which
  on shared hosting is a support-ticket lottery.

This ADR is the target-architecture half of the hosting-exit decision package
for issue #11. The migration procedure is the companion runbook,
[`docs/technical/hosting-exit-migration-runbook.md`](../technical/hosting-exit-migration-runbook.md);
the money is in
[`docs/technical/hosting-exit-cost-tiers.md`](../technical/hosting-exit-cost-tiers.md).

## Decision

Exit shared hosting to the following six-part target architecture. Each part
maps to a named gap in G6 or a numbered issue.

### 1. Managed Postgres with built-in PITR (closes the WAL-archiving gap, #26)

The database moves to a **managed Postgres service with point-in-time
recovery built in** (WAL archiving operated by the provider, restore to an
arbitrary timestamp, ~5-minute RPO). This directly closes issue #26: the
backup runbook's three PITR options all required hosting capability the
shared host does not offer; a managed service is the option that adds the
least new operational surface.

Non-negotiable provisioning requirements carried over from MR !5:

- A **dump/replication role with `BYPASSRLS`** (or provider-blessed
  equivalent) — the `FORCE ROW LEVEL SECURITY` schema makes a non-`BYPASSRLS`
  `pg_dump` fail loudly (by design; see `backup-and-restore.md` §2a). The
  nightly encrypted `pg_dump` + weekly `verify_restore.py` drill **continue
  unchanged** on the new host: PITR narrows RPO; the independent logical dump
  remains the provider-failure / account-lockout hedge and the year-end
  archival mechanism.
- `pgcrypto` extension available (migration `0001` runs
  `CREATE EXTENSION IF NOT EXISTS pgcrypto`).
- Postgres **16+** (current schema baseline; `docker-compose.yml` and CI both
  pin `postgres:16`).

### 2. Containerized API + workers

The application tier runs as containers on compute nodes we exclusively
control. `docker-compose.yml` on `develop` today models only the Postgres
dependency, but the *shape* is already fully factored for this:

- the API is a native ASGI app (`uvicorn`) — the Passenger/a2wsgi WSGI
  adapter and its async-discarding compromise are **deleted**, not migrated;
- the four workers (outbox, export, idempotency purge, dormancy) already
  expose both persistent loops (`*_worker.run_worker()`) and one-shot cycles
  (`backend/scripts/cron_*.py`). On owned infrastructure the persistent-loop
  form becomes viable again; either form runs identically in a container.

A skeleton compose file modelling the target shape lives at
[`infra/docker-compose.target.yml`](../../infra/docker-compose.target.yml)
(illustrative, not the deploy artifact). Redis joins the tier as a real
service: the rate limiter fails closed and the production boot guard
(`assert_redis_configured_outside_dev`, MR !3) refuses to boot without
`REDIS_URL` outside development.

### 3. Reverse proxy with WAF

All ingress passes through a reverse proxy (Caddy or nginx) that terminates
TLS and applies a WAF ruleset (provider-managed WAF at the edge — see part
4 — plus, where self-hosted, ModSecurity/Coraza with OWASP CRS). Nothing
except the proxy listens on a public interface.

### 4. DDoS-absorbing edge

DNS and public traffic front through an edge network with DDoS absorption
(Cloudflare-class; the free tier already absorbs volumetric attacks, the paid
tier adds managed WAF rules — priced in the cost-tiers doc). The origin
accepts traffic **only** from the edge's published IP ranges, so the edge
cannot be bypassed by hitting the origin IP directly.

### 5. Private network between tiers

Compute nodes, Redis, and the managed Postgres attach to a **private
network / VPC**. Postgres and Redis accept connections exclusively from the
private network (no public DB endpoint). The only public surface is the
proxy's 443. This is the G6 "network segmentation" line item.

### 6. Secrets-manager custody (sequenced with #6 — keys ROTATE INTO custody)

Secrets move from env-files-on-shared-disk to a secrets manager (provider
KMS-backed store, or self-hosted Vault/Infisical in the cost-optimised tier),
injected into containers at start, never written to disk on the host.

**Sequencing rule with issue #6 (JWT asymmetric migration), stated as
policy: keys are ROTATED INTO the new custody; old key material is never
copied.** Concretely:

- The new EdDSA signing keys (#6) are **generated inside** the secrets
  manager custody. The HS256 `JWT_SIGNING_KEY` that lived on shared disk is
  never imported — it serves out the #6 accept-both verification window and
  is then retired. If #6 lands before the hosting exit, its keys are
  re-rotated at cutover; if after, its migration starts directly in the new
  custody. Either order works; copying is what is forbidden.
- `CURSOR_SIGNING_KEY` rotates via the mechanism the codebase already owns
  (`CURSOR_SIGNING_KEY_PREVIOUS` + `CURSOR_KEY_VERSION` dual-key window in
  `genesis/settings.py`): new key minted in the secrets manager, old key
  demoted to `_PREVIOUS` for the window, then dropped. Cursors are
  short-lived pagination state; the window is days, not months.
- `OTP_PEPPER` rotation invalidates only in-flight OTPs (≤5-minute lifetime):
  mint a new value in custody at cutover.
- `BACKUP_ENCRYPTION_KEY`: a **new** key is minted in custody for all
  post-cutover dumps; the old key stays in the existing escrow (marked
  retired, per `backup-and-restore.md` §5) until every dump encrypted with it
  has aged out of retention — including the year-end archival dumps, which
  means the retired key escrow is long-lived. It is never loaded into the new
  runtime.

## Alternatives considered

- **Stay on shared cPanel and harden in code** — rejected. G6 is explicit
  that there is no code fix: the security boundary includes co-tenants, and
  `archive_command`/WAL access (#26), daemons, containers, and network
  segmentation are all capabilities the hosting class does not sell.
- **Single VPS, everything self-managed (including Postgres + pgBackRest
  PITR)** — viable and cheapest (priced as Tier B in the cost-tiers doc), but
  it converts #26 from "provider operates PITR" into a new 24/7 operational
  duty (WAL shipping, restore testing, storage monitoring) for a team that
  does not yet have an on-call rotation (gap register G10). Kept as the
  documented fallback if the budget decision forces it; not the
  recommendation.
- **Full hyperscaler (AWS RDS + ECS/EKS + WAF + Shield)** — architecturally
  the ceiling, rejected for now on cost and complexity: the same six
  properties are available at the Hetzner/DigitalOcean/Lightsail class for a
  fraction of the monthly spend, and nothing in the workload (single-digit
  nodes, one Postgres) needs hyperscaler primitives yet. Re-evaluate at the
  point of regulated deposit-taking scale.
- **MochaHost VPS upgrade** — same provider, dedicated resources. Rejected:
  it solves co-tenancy but keeps everything else manual (no managed Postgres,
  no VPC story, no managed WAF), and the provider relationship has already
  cost diagnostic days (the §0b SSH saga in the deployment runbook).

## Consequences

**Positive**

- Closes G6's named items: isolation, WAF, DDoS absorption, segmentation,
  secrets custody.
- Unblocks the dependency chain that is explicitly gated on #11:
  - **#26** — PITR arrives with the managed database (RPO 24h → minutes);
  - **#2** — the capacity tranche (pooling, backpressure, capacity statement)
    lands on infrastructure with headroom, not Passenger;
  - **#21** — the pooler-mode caveat gets an explicit decision instead of a
    latent footgun (runbook checklist item A);
  - **#6** — key custody exists for the asymmetric keys to rotate into;
  - **!3's `TRUSTED_PROXY_IPS`** stops defaulting to "never trust XFF" and
    per-IP rate limiting becomes meaningful (runbook checklist item B).
- The async architecture runs as designed (uvicorn, persistent workers).

**Negative / costs**

- Monthly spend rises from shared-host pocket change to a real line item —
  quantified honestly in the cost-tiers doc; that is the point of this
  package: the tradeoff becomes a decision, not a drift.
- New operational duties: container runtime upkeep, edge/WAF configuration,
  secrets-manager availability. Mitigated by choosing managed services for
  the stateful pieces.
- A migration event with a downtime window (budgeted in the runbook).

**Migration / rollback path**

The step-by-step procedure, the rollback window, and the downtime budget are
the companion runbook's subject matter. Summary: dump/restore drill with
`backend/scripts/verify_restore.py` (MR !5) against the new database before
any cutover; DNS cutover with a short-TTL window; the old host stays frozen
but intact as the rollback target until the rollback window closes.

## The single human decision

Everything above is sequencing and engineering. The one decision that needs a
human with budget authority: **pick the provider tier and approve the monthly
spend** (see `hosting-exit-cost-tiers.md`). The recommendation there is the
minimum defensible tier (managed Postgres + 2 nodes + edge), indicative
~US$95–140/month.
