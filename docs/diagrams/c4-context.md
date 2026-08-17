<!--
  P-DIAG.1 — C4 Level 1: System Context (as-built)
  Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
  Drift rule: v1.2 rule 11 — any MR that changes a depicted container,
  actor, protocol or trust boundary MUST update this file in the same
  MR. PLANNED (Pn) elements are flipped to as-built by the executing
  prompt's MR (the PHASE B2 common rules; P14/P16/P19/P20 each carry
  that instruction explicitly).
  Traceability: every as-built box cites its module below and in the
  companion table (§2); the checked-in spot-check script
  `c4-spot-check.py` verifies every cited module path exists at the
  authoring SHA.
-->

# C4 L1 — System context (P-DIAG.1)

What exists on `main` at the authoring SHA: **one deployed FastAPI
backend**, PostgreSQL 16 with **forced RLS**, Redis, and three worker
loops sharing the backend codebase. Every client and every external
provider is still `PLANNED (Pn)` — today the only principals are staff
users calling the JSON API directly (there is no member principal
until P14.5).

```mermaid
flowchart TB
    %% Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
    %% L1 context — as-built boxes cite genesis modules; dashed = PLANNED (Pn).

    STAFF["Staff users<br/>7 RBAC roles seeded from the prototype<br/>genesis/domain/rbac.py"]

    subgraph SYS["Genesis Prestige (this system)"]
        API["Backend API — FastAPI, single deployed app<br/>genesis/api/app.py create_app"]
        OBW["Outbox dispatch worker<br/>genesis/infrastructure/outbox_worker.py run_worker"]
        EXW["Export render worker<br/>genesis/infrastructure/export_worker.py run_worker"]
        DMW["Dormancy cycle worker — P13.13<br/>genesis/infrastructure/dormancy_worker.py run_worker"]
    end

    PG[("PostgreSQL 16 — FORCED RLS on every tenant table<br/>ADR-0002; genesis/infrastructure/tenancy.py<br/>alembic head 0022")]
    RD[("Redis<br/>readiness probe + auth rate limiting<br/>genesis/infrastructure/redis_client.py<br/>genesis/infrastructure/rate_limit.py")]

    WEB["Web admin — Next.js<br/>PLANNED (P14)"]
    ADM["Admin mobile — Flutter<br/>PLANNED (P16/P18)"]
    MEM["Member mobile — Flutter<br/>PLANNED (P16/P17)"]
    MPR["Member principal — member-facing auth<br/>PLANNED (P14.5)"]
    MPESA["M-Pesa STK + callbacks<br/>PLANNED (P19)"]
    PROV["SMS / email / push providers<br/>PLANNED (P20) — adapter seam as-built:<br/>genesis/infrastructure/providers.py StubProvider"]

    STAFF -->|"HTTPS JSON, JWT access + OTP step-up<br/>genesis/api/auth.py"| API
    API -->|"asyncpg, SET LOCAL app.tenant_id per txn"| PG
    API -->|"rate-limit counters, readyz probe"| RD
    OBW -->|"FOR UPDATE SKIP LOCKED claim; no domain locks held at dispatch<br/>(lock-order.md §3, outbox row)"| PG
    EXW -->|"REPEATABLE READ snapshot per export job"| PG
    DMW -->|"per-tenant dormancy batches"| PG
    OBW -.->|"dispatch via idempotent adapters"| PROV

    WEB -.-> API
    ADM -.-> API
    MEM -.-> API
    MPR -.-> API
    MPESA -.-> API

    classDef planned fill:#f8f9fa,stroke:#999,stroke-dasharray: 5 5;
    class WEB,ADM,MEM,MPR,MPESA,PROV planned;
    classDef store fill:#fff3cd,stroke:#b8860b;
    class PG,RD store;
```

## 2. Traceability table (every box → code on main)

| Box | As-built? | Source on main @ `08541b8` |
|---|---|---|
| Staff users | as-built | the only authenticated principal: `genesis/api/authz.py` (`RequirePermission`), roles/matrix `genesis/domain/rbac.py`; JWT + OTP step-up `genesis/api/auth.py`, `genesis/application/auth.py` |
| Backend API | as-built | `genesis/api/app.py` `create_app` — the single FastAPI app; 18 routers enumerated in [`c4-component.md`](c4-component.md) |
| Outbox dispatch worker | as-built | `genesis/infrastructure/outbox_worker.py` (`run_worker` L182, `dispatch_due` L48) |
| Export render worker | as-built | `genesis/infrastructure/export_worker.py` (`run_worker` L53, `run_export_cycle` L31) |
| Dormancy cycle worker | as-built (P13.13 !32; resilience hardened by !37) | `genesis/infrastructure/dormancy_worker.py` (`run_worker` L106, `run_dormancy_cycle` L65) |
| PostgreSQL 16, forced RLS | as-built | RLS enabled AND forced per ADR-0002 (`docs/adr/`), session scoping `genesis/infrastructure/tenancy.py` (`tenant_session` L12); migration head `0022` (`backend/migrations/versions/0022_dividend_dormant_policy.py`) |
| Redis | as-built | `genesis/infrastructure/redis_client.py` (readyz), `genesis/infrastructure/rate_limit.py` (auth rate limiting) |
| Web admin | PLANNED (P14) | not on main (an open scaffold MR !13 exists but is unmerged — as-built means merged) |
| Admin mobile / Member mobile | PLANNED (P16/P17/P18) | not on main (draft !11 unmerged) |
| Member principal | PLANNED (P14.5) | no member-facing auth exists; staff tokens are the only principal |
| M-Pesa | PLANNED (P19) | no payment-intent tables, no callback routes on main |
| SMS/email/push providers | PLANNED (P20) | only the adapter contract + logging stub are as-built: `genesis/infrastructure/providers.py` (`NotificationProvider`, `StubProvider`) |

## 3. Trust-relevant properties (by reference — never restated)

- **Forced-RLS tenant boundary**: every request/worker transaction is
  scoped with `SET LOCAL app.tenant_id` (`tenancy.py`); the app role
  cannot bypass RLS (ADR-0002). Explicit bound `tenant_id` predicates
  ride on top (v1.1 rule 4).
- **Lock discipline**: all lock-order statements for the edges above
  are owned by [`lock-order.md`](lock-order.md) (P-DIAG.0) — see its
  §3 catalogue and §6 advisory tier; this file intentionally cites,
  never restates (v1.2 rule 11).
- **Outbox dispatch holds no domain locks**: the worker claim/dispatch
  split is recorded in [`lock-order.md`](lock-order.md) §3 (the
  `outbox_events` single-node locker row).
