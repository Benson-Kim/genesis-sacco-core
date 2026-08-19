# infra/ — hosting-exit target-shape skeletons

Illustrative artifacts for the hosting exit
([ADR-0009](../docs/adr/0009-hosting-exit-target-architecture.md), issue
#11 / gap register G6). **Nothing here is deployed by CI** and nothing here
is the production deploy artifact — these files exist so the target shape
in the ADR is concrete and reviewable, not prose-only.

- `docker-compose.target.yml` — the application tier as containers:
  reverse proxy (sole public listener), API (native ASGI — no
  Passenger/a2wsgi), the four workers, Redis. **Postgres is deliberately
  absent**: the database is the provider-managed service with PITR
  (ADR-0009 part 1), reached over the private network. Contrast with the
  repo-root `docker-compose.yml`, which models only the local-dev Postgres
  dependency.

Conventions the skeleton encodes (and the real deploy must keep):

- **No secrets in files.** Every credential is `${VAR}` indirection,
  injected at start from the secrets manager (ADR-0009 part 6). No default
  values are provided on purpose — a missing secret must fail loudly.
- **Two database DSNs.** `DATABASE_URL_API` may point at a
  transaction-pooled endpoint; `DATABASE_URL_WORKER` must be direct or
  session-pooled — session-level advisory locks in
  `backend/src/genesis/infrastructure/cron_lock.py` break under
  transaction pooling (issue #21; migration-runbook checklist §2a).
- **Only the proxy publishes a port.** API, workers, and Redis live on the
  internal network (ADR-0009 part 5); the proxy's 443 is fronted by the
  DDoS-absorbing edge and accepts only edge ranges (part 4).
- `TRUSTED_PROXY_IPS` is set to the proxy's internal address so per-IP rate
  limiting keys on real client IPs (MR !3; runbook checklist §2b).
