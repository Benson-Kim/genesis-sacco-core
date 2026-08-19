# Hosting-exit cost tiers (honest monthly numbers)

Money half of the hosting-exit decision package
([ADR-0009](../adr/0009-hosting-exit-target-architecture.md), issue #11,
gap register G6). The point of this document, per the issue: *"what the
minimum defensible setup costs monthly vs the shared host, so the tradeoff
is a decision, not a drift."*

> **All prices are INDICATIVE provider list prices** (public price pages,
> checked 2026-08; USD; excl. VAT/tax; usage-based items estimated at this
> workload's scale). They exist to size the decision, not to be an invoice.
> **Verify against the provider's current price page on decision day.**

## Workload being priced

Small: one Postgres database (GBs, not TBs), one containerized FastAPI API,
four low-frequency workers, Redis, a Next.js frontend, modest traffic. The
sizing below is deliberately minimal-but-defensible, with the scaling path
noted, not pre-bought.

## Tier 0 — current shared cPanel host (the baseline being exited)

| Item | Indicative monthly |
|---|---|
| MochaHost shared cPanel plan | **~$5–15** |

What that price does *not* buy (G6): isolation (co-tenants share the
security boundary), WAF, DDoS absorption, network segmentation, secrets
custody, PITR (24h RPO, #26), daemons/containers (Passenger+a2wsgi discards
the async architecture), or a `BYPASSRLS` dump role without a support-ticket
lottery. The delta to Tier 1 is the monthly price of closing G6.

## Tier 1 — minimum defensible setup (the recommendation)

Managed Postgres **with PITR** + 2 small compute nodes (API+workers on one,
frontend+proxy on the other — or both roles on both, proxy on each) + Redis +
DDoS-absorbing edge. Private network is free at all three providers below.

### Option A — DigitalOcean (recommended reference option)

| Item | Sizing | Indicative monthly |
|---|---|---|
| Managed PostgreSQL (PITR built in, daily backups + WAL, single node) | 1 vCPU / 2 GB | ~$30 |
| 2× Droplet (containers: API, workers, proxy, frontend) | 2 vCPU / 4 GB each | ~$48 (2 × ~$24) |
| Managed Redis (or run Redis as a container: $0) | 1 GB | ~$15 (or $0 self-run) |
| Spaces object storage (offsite encrypted dumps, exports) | 250 GB incl. | ~$5 |
| Cloudflare edge (DDoS absorption) | Free tier | $0 |
| Cloudflare Pro (managed WAF rulesets) — optional but recommended | | ~$20 |
| **Total** | | **~$95–120** |

### Option B — Hetzner Cloud (cost floor — but Postgres is self-managed)

Hetzner has no managed Postgres: PITR becomes **our** pgBackRest/wal-g duty
(ADR-0009 rejected this as the recommendation for G10 reasons — no on-call
rotation exists; it is priced because it is the honest cheapest defensible
option if the budget forces it).

| Item | Sizing | Indicative monthly |
|---|---|---|
| 1× DB server (self-managed Postgres 16 + pgBackRest) | CX32-class, 4 vCPU / 8 GB | ~$9–11 |
| 2× app nodes | CX22-class, 2 vCPU / 4 GB each | ~$8 (2 × ~$4) |
| Object storage (WAL archive + dumps) | 1 TB bucket | ~$6 |
| Load balancer (or DNS-to-one-proxy: $0) | LB11 | ~$6 |
| Cloudflare Free / Pro | | $0 / ~$20 |
| **Total** | | **~$30–55** |

Honest caveat: the ~$65–90/month saved vs Option A buys a recurring
operational duty (WAL shipping health, restore testing, storage lifecycle,
Postgres upgrades) with no provider SLA behind the database. Cheap hosting
plus an unowned PITR pipeline can be *worse* than the current known-limited
nightly dump.

### Option C — AWS Lightsail (hyperscaler adjacency without hyperscaler pricing)

| Item | Sizing | Indicative monthly |
|---|---|---|
| Lightsail managed PostgreSQL (PITR: point-in-time restore built in) | 1 GB, standard | ~$15 |
| 2× Lightsail instances | 2 GB each | ~$24 (2 × ~$12) |
| Redis as container | | $0 |
| S3/Lightsail bucket (offsite dumps) | 250 GB | ~$5 |
| Cloudflare Free / Pro (or Lightsail CDN distribution ~$2.50) | | $0–20 |
| **Total** | | **~$45–70** |

Caveats: 1 GB managed DB is entry-tier (HA doubles it); Lightsail's
simplicity ceiling is real — outgrowing it means graduating to RDS/ECS at
several times the price.

## Tier 2 — comfortable / pre-regulated posture (for context, not proposed now)

Adds: DB standby node (provider HA failover), a third app node or bigger
nodes, paid WAF, managed secrets service, staging environment clone.

| Provider class | Indicative monthly |
|---|---|
| DigitalOcean (HA managed PG ~$60+, 3 nodes, managed Redis, Pro WAF, staging) | **~$250–350** |

This is the tier a SASRA-supervised deposit-taking posture will eventually
require (tested failover, G8); it is listed so the growth path is priced,
not to inflate today's ask.

## Recommendation and the decision required

**Recommendation: Tier 1 Option A (DigitalOcean-class managed Postgres +
2 nodes + Cloudflare), indicative ~$95–140/month** (~$120 with Pro WAF and
managed Redis; the range covers self-run vs managed Redis and the WAF
add-on). Rationale: it is the cheapest option where **PITR is the
provider's operated duty** (#26 closes with an SLA behind it, not a new
midnight job), and every G6 line item is covered by a managed service
rather than new operational surface.

**The single human decision** (carried from ADR-0009): approve a provider
and a monthly budget —

- ~**$100–140/mo** → Tier 1 Option A (recommended), or
- ~**$35–55/mo** → Tier 1 Option B, accepting self-managed PITR and its
  operational duty in writing, or
- ~**$50–70/mo** → Tier 1 Option C, accepting the entry-tier DB sizing.

Any of the three closes G6's structural items; only the budget owner can
weigh the delta. Everything else in the package (ADR-0009, the
[migration runbook](hosting-exit-migration-runbook.md)) is
provider-agnostic and executes unchanged once this is chosen.
