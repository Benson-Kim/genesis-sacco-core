# GENESIS PRESTIGE — MASTER ENGINEERING PROMPT (v1.0)

This document governs every human and AI contributor. No code, config, or
design that violates a MUST in this document may be merged. Reference:
`genesis_prestige_app.html` is the canonical UX/domain prototype.

## 0. MISSION
Build a multi-tenant SACCO management platform:
- **Admin Web** (React/Next.js) and **Admin Mobile** (Flutter): members,
  applications, loan book, guarantors, ledger, committee, exit, reports, RBAC.
- **Member Mobile** (Flutter): balances, statements, deposits (M-Pesa STK),
  loan application, repayments, guarantor consent, notifications.
- **Backend** (Python/FastAPI) + **PostgreSQL 16** + Redis + worker (Celery
  or arq) for outbox/exports/jobs.

Currency: KES, stored as `NUMERIC(18,2)` (or integer minor units) — NEVER float.

## 1. NON-NEGOTIABLE GATES (merge blockers)

### 1.1 Reuse-first (SOLID/DRY)
- Before writing anything, search the codebase and approved dependency list.
  Re-implementation of existing capability is a rejected MR.
- Shared logic lives in versioned internal packages: `core/domain`,
  `core/money`, `core/tenancy`, `core/outbox`, `ui/design-system` (React),
  `packages/gp_ui` + `packages/gp_api_client` (Flutter, generated from OpenAPI).

### 1.2 Reliability
- No silent failures: every `except` either handles specifically and logs a
  sanitized category, or re-raises. Bare `except:` and `except Exception: pass`
  fail CI (ruff rules + custom lint).
- Analytics/telemetry NEVER breaks the action: fire-and-forget with its own
  try/except and circuit breaker.
- Email/SMS/push/webhooks go through the **transactional outbox** only:
  write `outbox_events` in the SAME transaction as the domain change; a worker
  dispatches with retry + exponential backoff + dead-letter table. Direct
  provider calls from request handlers are forbidden.
- Every service exposes `/healthz` (liveness) and `/readyz` (deps checked).

### 1.3 Scalability
- Pagination everywhere: keyset (cursor) pagination on all list endpoints;
  hard max page size 100. Offset pagination only for admin reports <= 10k rows.
- No N+1: SQLAlchemy `selectinload`/`joinedload` mandatory; CI runs a query-count
  assertion per endpoint test (fail if queries grow with result size).
- Every FK and every column in a WHERE/ORDER BY of a production query has an
  index; migrations adding queries must add indexes in the same MR. `EXPLAIN`
  output required in MR description for new hot-path queries.
- Exports: only via `run_export(query, batch_size)` — fetch `batch_size + 1`
  to detect truncation, stream in batches off the event loop (worker/threadpool),
  set `X-Export-Truncated` and `X-Export-Limit` headers, enforce row caps.
- No blocking I/O inside a transaction holding row locks. Lock -> compute ->
  write -> commit -> then dispatch (via outbox).

### 1.4 Concurrency & race conditions (zero tolerance)
- All state transitions (application stages, loan status, exit settlement,
  member status) enforced by a single transition function checking the
  allowed-transitions map, executed under `SELECT ... FOR UPDATE`.
- Optimistic locking on every editable aggregate: `version` column;
  stale writes return HTTP 409 with a retriable error envelope.
- All mutating endpoints accept `Idempotency-Key`; keys stored with request
  hash + response; replay returns the stored response.
- Reference/number generation (member no `GP-XXXX`, txn refs `MP-/LN-/RP-/SH-/WD-`)
  is race-safe: `pg_advisory_xact_lock(tenant_id, seq_name)` + UNIQUE constraint
  + retry-on-conflict. Never `SELECT max()+1` without a lock.
- Approvals of money-moving operations (committee decisions, settlements,
  quotes) bind to a PERSISTED snapshot row (amount + component breakdown +
  version); execution re-verifies every component under the full lock set
  and returns 409 if anything moved since approval. Approving "the current
  state" is a rejected design (quote/approve/execute TOCTOU).
- Uniqueness/idempotency claims are made atomically:
  `INSERT ... ON CONFLICT DO NOTHING` checked by rowcount. SELECT-then-INSERT
  against a UNIQUE key is a rejected pattern (race -> unhandled IntegrityError).
- Outbox dispatch holds no domain row locks.
- Batched jobs (arrears, accruals, backfills) run through the shared batch
  runner: id-keyset batches, one short transaction each, `FOR UPDATE SKIP
  LOCKED`, injected session scope (no infrastructure imports in jobs).

### 1.5 Data integrity
- Constraints in the DATABASE, not just the app: CHECK (amounts >= 0, rate
  bounds, term <= product max, `cover_pct` bounds), UNIQUE (tenant_id + member_no,
  tenant_id + txn_ref, idempotency keys), FK with explicit ON DELETE behavior,
  NOT NULL by default.
- Ledger is append-only double-entry: every posting has balanced DR/CR legs;
  corrections are reversing entries, never UPDATE/DELETE (enforced by trigger).
- Immutable audit trail: `audit_log(tenant_id, actor_id, action, entity,
  entity_id, before, after, ip, at)` written in-transaction for every mutation.
- Multi-step operations (disburse = approve check + ledger post + schedule
  generation + outbox) run in ONE transaction owned by a single application-service
  function that owns commit/rollback. No partial success.
- Values derived from balances OVER A PERIOD (deposit interest, fees,
  averages) are computed from the append-only ledger — e.g. the average
  daily balance reconstructed from posting legs under the account row lock —
  never from a point-in-time balance snapshot. A snapshot basis is a proven
  exploit class (park funds on the measurement day, withdraw the next).
- Postings that recognise a period (interest, accruals) carry `occurred_at`
  at the END of that period so they sort after the period's real activity
  and compound into the next period's basis.

### 1.6 Security
- Every endpoint authenticated (JWT access <= 15 min + rotating refresh; OTP
  step-up for auth as in prototype gate) and authorized against the RBAC
  matrix (role x module x view/create/edit/approve, seeded from prototype:
  System Admin, Branch Manager, Loan Officer, Teller, Credit Committee,
  Accountant, Auditor). Deny by default; authz enforced server-side per handler
  via dependency, never only in UI.
- Multi-tenancy isolation: `tenant_id` on every table + PostgreSQL **Row-Level
  Security** with `SET LOCAL app.tenant_id` per request. App DB role cannot
  bypass RLS. A cross-tenant leakage test suite is a release blocker.
- Defence in depth on top of RLS: every tenant-owned query — reads AND
  writes (UPDATE/DELETE included) — carries an explicit bound
  `tenant_id = :tid` predicate. A query filtering by row id alone is a
  rejected MR: one misconfigured session must not equal cross-tenant access.
- Money-affecting parameters (interest rates, fees, accrual periods,
  penalty schedules) are resolved SERVER-SIDE from tenant/product
  configuration (`tenant_settings`, product rows). Request bodies must not
  accept them and must reject unknown fields (`extra="forbid"` -> 422).
  A caller-suppliable rate or backdatable period is a rejected design.
- SQL exclusively via bound parameters — no value (enum values included) is
  ever string-interpolated into a statement. Only identifiers chosen from
  code-owned mappings may be interpolated, with a comment stating so.
- Least disclosure: error messages never echo balances, capacities, pledge
  totals, or other derived figures; the in-transaction audit row records
  the exact numbers for staff entitled to them.
- No PII in logs, analytics, error messages, or URLs. Errors surface a
  sanitized category + correlation ID; stack traces only in secured APM.
- Secrets only via GitLab CI/CD variables / external secret manager. Any
  literal credential fails secret-detection CI.
- Input validation with Pydantic at the boundary; output serialization uses
  explicit response models (no ORM leakage).
- Rate limiting on auth/OTP/M-Pesa callbacks; OTP: 6 digits, <= 5 attempts,
  5-min TTL, single-use, constant-time compare.
- M-Pesa callbacks: verify source, validate against pending intent, idempotent.

## 2. ARCHITECTURE

Authoritative diagrams live under `docs/diagrams/` (P-DIAG series).
The lock-ordering DAG — `docs/diagrams/lock-order.md` — is the single
authority for every lock-order statement (concurrency gates in §1.4);
MRs reference it instead of restating chains, and any MR that changes
a lock-graph edge updates it in the same MR (BUILD_PROMPTS v1.2 rule 11).

### 2.1 Backend (Python 3.12, FastAPI)
Layered/hexagonal, dependency direction inward:
`api (routers/schemas) -> application (use-case services, owns transactions)
-> domain (entities, transitions, pure logic — no I/O) -> infrastructure
(repositories, outbox, mpesa, sms/email adapters behind interfaces)`.
- Loan math (amortization `inst()`, classification `classify()` thresholds
  30/90/180/360 days, provisioning 1/5/25/50/100%) lives in `domain/lending`
  as pure, property-tested functions — single source of truth, mirrored to
  clients only through the API.
- OpenAPI is the contract; Flutter and React clients are GENERATED, never
  hand-written.

### 2.2 Data model (core tables)
tenants, users, roles, permissions, members (type: person|company|group|vehicle),
share_accounts, deposit_accounts, loan_products, loan_applications
(stage machine incl. Rejected), loans, loan_schedules, repayments, guarantees,
ledger_entries (double-entry), transactions, member_exits, outbox_events,
audit_log, idempotency_keys, otp_challenges.

### 2.3 Frontend (Next.js + TypeScript strict)
- TanStack Query for server state (no ad-hoc fetch), Zod-validated responses,
  design system package only (tokens extracted from prototype CSS variables).
- Route-level authz mirroring RBAC; UI hides what API forbids, API still enforces.

### 2.4 Mobile (Flutter, Dart 3)
- One repo, two apps (`member_app`, `admin_app`) + shared packages.
- Riverpod for state, offline-tolerant read cache, secure storage for tokens,
  certificate pinning, biometric unlock for step-up actions.

## 3. CI/CD (GitLab)
Stages: `lint -> test -> security -> build -> migrate-check -> deploy-review ->
deploy-staging -> deploy-prod (manual)`.
- Lint: ruff+mypy(strict), eslint+tsc, dart analyze+dart format. Zero warnings.
- Test: unit + integration (Postgres service, real RLS) + contract tests
  against OpenAPI; coverage gate >= 85% backend domain/application layers.
- Security: SAST, Dependency Scanning, Secret Detection, Container Scanning,
  DAST on review apps; License scanning; SBOM published as artifact. Critical
  vulnerabilities block merge.
- Migrations: `alembic upgrade head` + downgrade smoke test in CI; migrations
  must be backward-compatible one release (expand -> migrate -> contract).
- DAG with `needs:`; cache pip/npm/pub keyed on lockfiles; images pinned;
  jobs run as non-root; `rules:` not `only/except`.
- Commits: atomic, Conventional Commits, trailer
  `Duo-Workflow-Definition: ci_expert_agent/v1` on agent-authored commits.
- Incremental push discipline: work is committed and pushed after each
  coherent unit (fix, feature slice) and its pipeline observed before the
  next unit. Long-lived unpushed work is forbidden — a crashed session must
  never lose completed work.
- Never present or commit CI config that fails `gitlab-ci` lint.

## 4. TESTING STRATEGY (Quality gates)
- Pyramid: pure-domain unit tests (incl. property tests for money/interest/
  classification), integration tests with real Postgres (RLS, constraints,
  triggers, advisory locks), API contract tests, minimal E2E happy paths
  (Playwright web, integration_test Flutter).
- Mandatory adversarial tests per feature: concurrent double-submit (must
  produce exactly one effect), stale-version edit (409), cross-tenant access
  (404/403 + zero rows), truncated export headers, outbox retry after
  provider failure.
- Anti-reward-hacking rules (every MR):
  * Test oracles are HAND-COMPUTED and documented in comments — never
    captured from the implementation under test.
  * Idempotency is asserted via side-effect row counts (ledger, audit,
    outbox, claim tables), never via return values alone.
  * Falsifiability: every guard test must fail when its guard is removed
    (row lock deleted, UNIQUE claim bypassed, anti-join dropped). A test
    that cannot fail is a rejected test.
  * No coverage padding, no weakened gates, no `allow_failure`, no skipped
    downgrade paths.
- Kill-switch atomicity tests for every multi-step money operation: abort
  mid-transaction and prove zero partial state (no posting, no balance
  change, no state transition, no claim row).

## 5. DEFINITION OF DONE (every MR)
1. Reuses existing modules; no duplication.
2. All gates in section 1 satisfied; adversarial tests added.
3. DB constraints + indexes shipped with the query that needs them.
4. Audit log + outbox used for mutations/side-effects.
5. OpenAPI updated; clients regenerated.
6. Pipeline fully green including security stage.
7. MR description: what/why, EXPLAIN for new hot queries, rollback plan.
8. Honest DoD: a checkbox is ticked only when pipeline evidence exists;
   pending items stay unchecked with a stated reason. Post-implementation
   self-review findings are listed in the MR description with severity and
   fix (the !17 review-sweep table is the template).
9. Pre-implementation review: before the first line of code, record the
   reuse audit (what exists, what will be reused, duplications removed),
   the lock-ordering the change must match, and a short threat model
   (caller-controlled parameters? TOCTOU windows? tenant scoping? partial
   states?).

## 6. ROLE SUB-PROMPTS
- **Product Manager**: prototype is the source of scope; write issues as
  user stories with acceptance criteria that reference section 1 gate IDs.
- **Solutions Architect**: guard section 2 boundaries; any new dependency or
  service requires an ADR in `docs/adr/`.
- **Developer**: follow sections 1-2; never bypass application-service
  transaction ownership; no TODOs in merged code.
- **Tester/QE**: own section 4; every bug fixed gets a regression test first.
- **Security Analyst**: own section 1.6 + triage of scanner findings within
  48h; quarterly dependency review; threat model per new integration
  (M-Pesa, SMS).
- **CI/CD Engineer**: own section 3; pipeline changes via MR with lint proof.
