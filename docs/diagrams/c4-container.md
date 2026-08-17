<!--
  P-DIAG.1 — C4 Level 2: Containers (as-built)
  Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
  Drift rule: v1.2 rule 11 — any MR that changes the layering, a worker
  process, the migration chain head, a middleware, or a trust-relevant
  store property MUST update this file in the same MR.
  Traceability: every box cites its module; `c4-spot-check.py` verifies
  every cited module path exists at the authoring SHA.
-->

# C4 L2 — Containers (P-DIAG.1)

One backend codebase (`backend/src/genesis`), deployed as the API
process plus three worker loops, over PostgreSQL 16 (forced RLS) and
Redis. The four layers and their **import-linter-enforced inward
dependency direction** are contracts in `backend/pyproject.toml`
(`[tool.importlinter]`): domain imports nothing outward; application
never imports api/infrastructure; api never imports the provider or
worker modules.

```mermaid
flowchart TB
    %% Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
    %% L2 containers — layering per backend/pyproject.toml [tool.importlinter].

    subgraph BE["Backend API process — genesis/api/app.py create_app"]
        direction TB
        MW["Middleware seam<br/>IdempotencyMiddleware — genesis/api/idempotency.py<br/>correlation id + error envelope — genesis/api/app.py<br/>tenancy: SET LOCAL app.tenant_id per txn —<br/>genesis/infrastructure/tenancy.py tenant_session"]
        APIL["api layer — routers + schemas + authz<br/>genesis/api (18 routers, see c4-component.md)<br/>RequirePermission — genesis/api/authz.py"]
        APPL["application layer — use-case services, OWN the transactions<br/>genesis/application (audit, outbox writer, batch_runner,<br/>pagination + one service module per feature)"]
        DOM["domain layer — pure logic, no I/O<br/>genesis/domain: lending, ledger, committee, members, exits,<br/>dividends, deposits, money, otp, rbac, tenant_config, users,<br/>member_kyc, documents"]
        INF["infrastructure layer — adapters behind interfaces<br/>genesis/infrastructure: db, tenancy, redis_client,<br/>rate_limit, providers"]
        MW --> APIL
        APIL -->|"calls, injects the tenant-scoped session"| APPL
        APPL -->|"pure calls only"| DOM
        APIL -->|"session/adapters (never providers/workers:<br/>import-linter contract 3)"| INF
    end

    subgraph WK["Worker processes — same codebase, separate loops"]
        OBW["outbox dispatcher<br/>genesis/infrastructure/outbox_worker.py"]
        EXW["export renderer<br/>genesis/infrastructure/export_worker.py"]
        DMW["dormancy cycle — P13.13<br/>genesis/infrastructure/dormancy_worker.py"]
    end

    MIG["Migration runner — alembic upgrade head<br/>backend/alembic.ini + backend/migrations/<br/>versions 0001..0022 (head 0022 verified at authoring)"]

    PG[("PostgreSQL 16 — FORCED RLS (ADR-0002)<br/>append-only: ledger_entries + transactions (0004 triggers),<br/>audit_log (0001 audit_log_append_only)<br/>write-once: dividend_declarations (0020 trigger)<br/>closed-period posting barrier (0012/0014)<br/>advisory-lock tier: lock-order.md §6")]
    RD[("Redis<br/>rate limiting + readyz")]

    APPL -.->|"writes outbox_events in the SAME txn<br/>genesis/application/outbox.py enqueue_event"| PG
    INF --> PG
    INF --> RD
    OBW -->|"claim SKIP LOCKED, dispatch holds NO domain locks<br/>(lock-order.md §3, outbox row)"| PG
    EXW -->|"one REPEATABLE READ txn per export job<br/>genesis/infrastructure/tenancy.py tenant_snapshot_session"| PG
    DMW -->|"per-tenant batches, root-tier member locks only<br/>(lock-order.md §3, dormancy row)"| PG
    OBW -.->|"idempotent by event id"| PROV["provider adapters<br/>genesis/infrastructure/providers.py<br/>StubProvider as-built; real providers PLANNED (P20)"]
    MIG --> PG

    classDef store fill:#fff3cd,stroke:#b8860b;
    class PG,RD store;
    classDef planned fill:#f8f9fa,stroke:#999,stroke-dasharray: 5 5;
    class PROV planned;
```

## 2. Traceability table

| Container / element | Source on main @ `08541b8` |
|---|---|
| api layer | `genesis/api/` — 18 routers wired in `genesis/api/app.py` `create_app`; per-handler authz dependency `genesis/api/authz.py` (`RequirePermission` L28, `RequireAnyPermission` L54) |
| Idempotency middleware | `genesis/api/idempotency.py` (`IdempotencyMiddleware` L98): atomic `ON CONFLICT DO NOTHING` claim per (tenant, key), replay returns the stored response, actor is part of the request hash |
| Tenancy middleware/seam | `genesis/infrastructure/tenancy.py` (`tenant_session` L12 — `SET LOCAL app.tenant_id` via `set_config(..., true)`; `tenant_snapshot_session` L31 — REPEATABLE READ for exports) |
| application layer | `genesis/application/` — one service module per feature; cross-cutting: `audit.py` (in-txn audit rows), `outbox.py` (`enqueue_event` L17), `batch_runner.py` (shared batch jobs), `pagination.py` (keyset) |
| domain layer | `genesis/domain/` — 14 pure modules, zero I/O (import-linter contract 1: forbidden from api/application/infrastructure) |
| infrastructure layer | `genesis/infrastructure/` — `db.py`, `tenancy.py`, `redis_client.py`, `rate_limit.py`, `providers.py` |
| import-linter contracts | `backend/pyproject.toml` `[tool.importlinter]`: (1) domain is pure; (2) application independent of delivery/adapters; (3) request handlers never import `providers` / `outbox_worker` / `export_worker` |
| outbox dispatcher | `genesis/infrastructure/outbox_worker.py` (`dispatch_due` L48: claim txn commits BEFORE any provider call; backoff L31; dead-letter after `MAX_ATTEMPTS = 8` L26) — the P-DIAG.5 sequence diagram details this flow once it lands |
| export renderer | `genesis/infrastructure/export_worker.py` → `genesis/application/exports.py` (`run_export_job` L381, claim `CLAIM_SQL` L82) |
| dormancy worker | `genesis/infrastructure/dormancy_worker.py` (`run_dormancy_cycle` L65 — fail-closed per tenant, per-tenant error isolation per !37) → `genesis/application/dormancy.py` (`run_dormancy_for_tenant` L370) |
| migration runner | `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001..0022` — head `0022` (`0022_dividend_dormant_policy.py`, `down_revision = "0021"`), verified at authoring |
| PostgreSQL 16 | forced RLS per ADR-0002; store properties below |
| Redis | `genesis/infrastructure/redis_client.py` (`ping_redis` — `/readyz`), `genesis/infrastructure/rate_limit.py` (auth endpoints) |

## 3. Trust-relevant store properties (by reference)

- **Forced-RLS boundary**: every tenant-owned table has RLS enabled
  AND forced (ADR-0002, migration `0001`); the request/worker session
  is scoped per transaction (`tenancy.py`). Cross-tenant leakage suite
  is release-blocking (MASTER_PROMPT 1.6).
- **Append-only stores**: `ledger_entries` and `transactions`
  (UPDATE/DELETE blocked by triggers, migration `0004` —
  `ledger_entries_no_update`/`_no_delete`, `transactions_no_update`/
  `_no_delete`); `audit_log` (`audit_log_append_only`, migration
  `0001` L336). Corrections are reversing entries, never UPDATE
  (MASTER_PROMPT 1.5).
- **Write-once snapshot**: `dividend_declarations`
  (`dividend_declarations_write_once` trigger, migration `0020`) — the
  snapshot-bind-reverify pattern's DB-level anchor.
- **Advisory-lock tier** (reference generation, period barrier): owned
  by [`lock-order.md`](lock-order.md) **§6** — cited, not restated.
- **Outbox holds no domain locks**: owned by
  [`lock-order.md`](lock-order.md) **§3** (the `outbox_events`
  single-node locker row) — cited, not restated.
