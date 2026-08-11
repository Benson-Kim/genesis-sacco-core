# Architecture

As-built system overview. The authoritative diagrams live under
[`docs/diagrams/`](../diagrams/README.md) — this document narrates them and
cites code; it deliberately does not duplicate any drawing:

- Context / containers / components (C4): [`c4-context.md`](../diagrams/c4-context.md),
  [`c4-container.md`](../diagrams/c4-container.md),
  [`c4-component.md`](../diagrams/c4-component.md)
- Entity-relationship diagram: [`erd.md`](../diagrams/erd.md)
- Data-flow / threat model: [`dfd.md`](../diagrams/dfd.md),
  [`stride.md`](../diagrams/stride.md)
- Lock-ordering DAG (the single authority for lock-order statements):
  [`lock-order.md`](../diagrams/lock-order.md)

## 1. What the system is

A multi-tenant SACCO (savings & credit co-operative) core-banking platform:

- **Backend** — Python 3.12 / FastAPI / PostgreSQL 16 / Redis, plus worker
  processes for the transactional outbox, exports and scheduled jobs
  (`backend/src/genesis/infrastructure/{outbox_worker,export_worker,dormancy_worker,idempotency_worker}.py`).
- **Admin web console** — Next.js (App Router) + TypeScript strict, TanStack
  Query for server state, Zod-validated boundaries, a generated OpenAPI
  client, and an in-house design system (`web/`).
- **Prototype** — `genesis_prestige_app.html` is the canonical UX/domain
  source the platform was derived from.

Currency is KES, stored as `NUMERIC(18,2)`. Money is never a float and is
never computed client-side.

## 2. Backend layering

Layered/hexagonal with the dependency direction pointing inward:

```
api  →  application  →  domain          (infrastructure sits behind interfaces)
```

| Layer | Path | Responsibility |
|---|---|---|
| `api` | `backend/src/genesis/api/` | FastAPI routers, one per module (members, transactions, loans, dividends, branches, corrections, recovery, dashboard, reports, users, settings, …). Pydantic request/response models with `extra="forbid"`; the deny-by-default authorization dependency (`api/authz.py`); shared request-parameter guards (`api/params.py`); the idempotency middleware (`api/idempotency.py`). |
| `application` | `backend/src/genesis/application/` | Use-case services. Each service **owns its transaction** (commit/rollback); SQL lives in module-level constants so the EXPLAIN test suites exercise the production statements. Cross-cutting helpers: `pagination.py` (signed keyset cursors), `sod.py` (maker–checker guard), `audit.py` (in-transaction audit rows), `outbox.py` (transactional outbox writes). |
| `domain` | `backend/src/genesis/domain/` | Pure logic, zero I/O: loan math (`lending.py`), the double-entry posting builders (`ledger.py`), status machines (`members.py`, `exits.py`, `recovery.py`, `users.py`), the RBAC matrix (`rbac.py`), OTP policy (`otp.py`), tenant-configuration validation and approval bands (`tenant_config.py`), money rounding (`money.py`). |
| `infrastructure` | `backend/src/genesis/infrastructure/` | Database session factory (`db.py`), tenant-scoped sessions (`tenancy.py`), Redis client and rate limiting, worker loops, provider adapters (`providers.py`). |

Layer boundaries are CI-enforced (`lint-imports` in the `backend:lint` job):
a domain module importing from application or infrastructure fails the
pipeline.

## 3. Web application structure

```
web/src/app/                    Next.js App Router routes
  login/                        sign-in (OTP gate)
  (shell)/dashboard/            dashboard
  (shell)/modules/<module>/     one route tree per console module
web/src/modules/<module>/       screens, api.ts, schemas.ts, __tests__/
web/src/modules/authz/          client-side RBAC mirror (modules.ts, usePermissions.ts)
web/packages/api-client/        openapi.json snapshot + generated schema.d.ts
web/packages/design-system/     shared UI primitives + cross-cutting app shell/table/form
                                 machinery (ConfirmDangerModal, AppShell, Sidebar (nav.ts),
                                 banners, KeysetTable + useKeysetList, FormField, …) — the
                                 ONLY source of these; imported as named exports from
                                 `@genesis/design-system` (no deep/subpath imports).
```

Module conventions (every console module follows them):

- `api.ts` — typed wrappers over the generated client; every response is
  Zod-validated at the boundary (`schemas.ts`).
- `components/` — the screen plus its drawers/dialogs. Screens are read-only
  tables with detail drawers; mutations live in explicit drawers/dialogs.
- `__tests__/` — screen tests plus `.network.test.ts` wire suites that assert
  request counts and bodies at the network boundary.
- Route-level authorization mirrors the server RBAC
  (`web/src/modules/authz/modules.ts` matches the backend `Module` enum):
  the UI hides what the API forbids; the API still enforces.

The sidebar structure (Operations / Governance / Insights / Administration)
is code-owned in `web/packages/design-system/src/layout/nav.ts`; prototype areas without
their own RBAC module (guarantors, committee, member exit, dormancy, share
transfers, dividends, accounting periods, recovery) live under their owning
module's route tree and are gated by that module's permissions server-side.

End-to-end tests (`web/e2e/`, Playwright) run against the real production
build with the API mocked at the browser network boundary.

## 4. Multi-tenancy model

Two independent fences, both mandatory (defence in depth):

1. **Row-Level Security (RLS).** Every tenant-owned table carries
   `tenant_id`; PostgreSQL RLS policies are FORCED, and each request runs
   inside a tenant-scoped session that executes `SET LOCAL app.tenant_id`
   (`backend/src/genesis/infrastructure/tenancy.py`). The application
   database role is a non-superuser without `BYPASSRLS` — the CI test job
   provisions exactly such a role so the test suite runs with RLS actually
   enforced (`.gitlab-ci.yml`, `backend:test`). A session without a tenant
   context fails loudly rather than reading zero-filtered data.
2. **Explicit tenant predicates.** Every tenant-owned statement — reads and
   writes, `UPDATE`/`DELETE` included — additionally carries a bound
   `tenant_id = :tid` predicate. A query filtering by row id alone is a
   rejected merge request: one misconfigured session must never equal
   cross-tenant access.

The one documented exemption is pre-authentication: the sign-in lookups in
`backend/src/genesis/application/auth.py` run before a tenant context can be
trusted, are keyed by unguessable secrets (token hashes, challenge rows), and
remain fenced by forced RLS. Pre-auth endpoints scope the tenant from the
explicit `X-Tenant-ID` header (`backend/src/genesis/api/auth.py`).

## 5. Request lifecycle

A representative mutating request (e.g. posting a deposit):

1. **Transport** — the browser sends the request with `Authorization:
   Bearer <access token>` and an `Idempotency-Key` header (the web client
   mints one key per submission).
2. **Idempotency middleware** (`api/idempotency.py`) — claims the key
   atomically; a replay of an already-processed key returns the stored
   response without re-executing side effects.
3. **Authorization dependency** (`api/authz.py`, `RequirePermission`) —
   decodes the token (staff/member audiences are disjoint), re-checks the
   user's active status and the role × module × action grant against the
   database on every request. Deny by default; a suspended user's live token
   dies within one request.
4. **Router handler** (`api/<module>.py`) — validates the body
   (`extra="forbid"`), applies shared parameter guards (cash channel,
   external reference shape, no-future-dates), and delegates to exactly one
   application-service function.
5. **Application service** (`application/<module>.py`) — opens the
   tenant-scoped session, takes row locks in the documented order
   ([`lock-order.md`](../diagrams/lock-order.md)), re-verifies
   state under those locks (no TOCTOU), calls pure domain functions for the
   arithmetic and status transitions, writes the domain rows, the
   **in-transaction audit row** (`application/audit.py`) and any **outbox
   events** (`application/outbox.py`), then commits. Multi-step money
   operations are one transaction: no partial success.
6. **Side effects** — a worker dispatches outbox events after commit with
   retry and dead-lettering; request handlers never call providers directly
   ([`sequence-outbox-dispatch.md`](../diagrams/sequence-outbox-dispatch.md)).
7. **Response** — explicit response models only (no ORM leakage); errors
   surface a sanitized category plus correlation id
   (`backend/src/genesis/errors.py`), never internals.

## 6. The generated-client contract flow

OpenAPI is the contract; clients are generated, never hand-written:

```
backend code
   │  backend/scripts/export_openapi.py
   ▼
web/packages/api-client/openapi.json        (committed snapshot)
   │  npm run generate:api
   ▼
web/packages/api-client/src/generated/schema.d.ts   (committed generated client)
```

Two CI drift jobs arbitrate the chain on every pipeline (`.gitlab-ci.yml`):

- `web:spec-drift` — regenerates the OpenAPI document from the backend and
  diffs it byte-for-byte against the committed snapshot; a stale snapshot is
  a red pipeline.
- `web:client-drift` — regenerates the client from the committed snapshot
  and diffs it against the committed `schema.d.ts`; the job additionally
  proves its own falsifiability on every run by staling a workspace copy and
  asserting the check then fails.

Both generated files are **never hand-edited** (see
[contributing.md](contributing.md)). Contract changes are expand-only — see
[api-guide.md](api-guide.md#5-contract-evolution-policy-expand-only).
