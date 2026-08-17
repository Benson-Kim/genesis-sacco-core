# GENESIS PRESTIGE — SEQUENTIAL BUILD PROMPTS (P0–P24)

Execute strictly in order. A prompt may start only when every prompt it
depends on has met its EXIT criteria. Every prompt inherits the whole of
`docs/MASTER_PROMPT.md`; gate references (1.1–1.6) are merge blockers.
Each prompt = one branch, atomic commits, one MR, pipeline fully green.
Agent commits carry `Duo-Workflow-Definition: ci_expert_agent/v1`.

Prompt format: ROLE / DEPENDS / PROMPT (give verbatim to the executor) / EXIT.

---

## PHASE A — FOUNDATION

### P0 — Governance merge & repo hygiene
ROLE: Solutions Architect. DEPENDS: none.
PROMPT: Merge MR !1. Then add: `.gitlab/merge_request_templates/Default.md`
containing the Definition of Done checklist (MASTER_PROMPT §5) with EXPLAIN
and rollback-plan fields; `CODEOWNERS` requiring architect review on
`docs/`, `.gitlab-ci.yml`, and `backend/src/domain/`. Configure branch
protection on `main`: merge only via MR with green pipeline.
EXIT: !1 merged; protections active; MR template appears on new MRs.

### P1 — Backend scaffold (issue #1)
ROLE: Developer. DEPENDS: P0.
PROMPT: Create `backend/` FastAPI skeleton per MASTER_PROMPT §2.1: packages
`src/api`, `src/application`, `src/domain`, `src/infrastructure`; enforce
inward dependency direction with import-linter in CI. Add `pyproject.toml`
(ruff strict, mypy --strict, pytest, coverage ≥85) so `backend:*` CI jobs
activate. Implement `/healthz`, `/readyz` (checks DB+Redis), structured JSON
logging with correlation IDs and a PII-scrubbing filter (1.6), env-only
settings, and the global error envelope: `{category, correlation_id}` —
never stack traces (1.2, 1.6).
EXIT: pipeline green with backend jobs running; /readyz verified against
compose services; import-linter blocks a deliberate violation in a test.

### P2 — Database schema v1 + tenancy (issue #2)
ROLE: Developer + DBA. DEPENDS: P1.
PROMPT: Implement all MASTER_PROMPT §2.2 tables via Alembic. Every
tenant-owned table: non-null `tenant_id`, RLS policy on
`current_setting('app.tenant_id')`, app role without BYPASSRLS (ADR-0002).
DB-level CHECKs (amounts ≥ 0, rate/term bounds), tenant-scoped UNIQUEs
(member_no, txn_ref), NOT NULL default, explicit ON DELETE, `version`
columns on editable aggregates (1.4), NUMERIC(18,2) money, composite
indexes leading with tenant_id, every FK indexed (1.3, 1.5). Add tenancy
middleware issuing `SET LOCAL app.tenant_id` per request transaction.
Write the cross-tenant leakage suite: for every table, prove tenant B
cannot read/write tenant A rows even with raw SQL through the app role.
EXIT: `backend:migrate-check` green (upgrade/downgrade/upgrade); leakage
suite green and marked release-blocking.

### P3 — Authentication (issue #3)
ROLE: Developer + Security Analyst. DEPENDS: P2.
PROMPT: Implement OTP step-up auth mirroring the prototype gate: 6 digits,
≤5 attempts, 5-min TTL, single-use, constant-time compare, delivery via
outbox stub. JWT access ≤15 min + rotating refresh with family-revocation
on reuse. Rate-limit auth endpoints. Add `Idempotency-Key` middleware
(request-hash + stored response, replay returns stored response) (1.4).
Adversarial tests: brute-force lockout, refresh reuse, concurrent identical
idempotency keys resolving to exactly one effect.
EXIT: all adversarial tests green; Security Analyst sign-off note on the MR.

### P4 — RBAC (issue #4)
ROLE: Developer. DEPENDS: P3.
PROMPT: Seed the 7-role permission matrix from the prototype `seedPerms()`
(System Admin, Branch Manager, Loan Officer, Teller, Credit Committee,
Accountant, Auditor × modules × view/create/edit/approve). Enforce
deny-by-default via a FastAPI dependency required on every router (1.6);
CI test walks the OpenAPI spec and fails if any operation lacks the authz
dependency. Expose `/me/permissions`. Audit-log permission changes (1.5).
Matrix-driven tests: every endpoint × every role.
EXIT: spec-walk test green; matrix tests green.

### P5 — Transactional outbox (issue #5)
ROLE: Developer. DEPENDS: P2.
PROMPT: Implement `core/outbox`: `outbox_events` written in the same
transaction as domain changes; worker (Celery or arq per ADR-0001) with
exponential backoff + jitter, dead-letter table after N attempts, lag and
failure metrics. Dispatch must hold no domain row locks (1.4). Prove
atomicity: rollback removes the event. Provider adapters (email/SMS/push)
behind interfaces, idempotent by event id (1.2).
EXIT: atomicity, retry-then-success, and dead-letter tests green; a
request-handler direct-provider-call lint rule active in CI.

### P6 — Lending domain engine (issue #6)
ROLE: Developer + QE. DEPENDS: P1 (pure domain; parallel with P2–P5 allowed).
PROMPT: Implement `domain/lending`: reducing-balance amortization
(Decimal-only, documented rounding, Hypothesis property tests on the
sum-of-installments invariant), schedule generation, classification
(Normal ≤30d/1%, Watch ≤90d/5%, Substandard ≤180d/25% NPL, Doubtful
≤360d/50% NPL, Loss >360d/100% NPL), provisioning, and the application
stage machine (Submitted→Appraisal→Committee→Approved→Disbursed, plus
Rejected) where illegal transitions raise (1.4). Zero I/O in this package;
100% branch coverage.
EXIT: property tests + full-matrix transition tests green; coverage 100%
on the package.

### P7 — Double-entry ledger (issue #7)
ROLE: Developer + DBA. DEPENDS: P2, P5, P6.
PROMPT: Implement `ledger_entries` and posting services for Deposit,
Disbursement, Loan repayment, Share top-up, Withdrawal, Interest posting
across channels M-Pesa/Bank/Accrual. Balanced DR/CR enforced by trigger;
UPDATE/DELETE blocked by trigger; corrections are reversing entries (1.5).
Reference generation (MP-/LN-/RP-/SH-/WD-/INT-) via
`pg_advisory_xact_lock` + UNIQUE + retry (1.4). Disbursement is one atomic
application-service transaction: approval check + posting + schedule +
outbox (1.5). Audit-log all postings. Concurrency test: 50 parallel posts,
zero gaps or duplicates.
EXIT: trigger, reversal, and concurrency tests green.

---

## PHASE B — DOMAIN FEATURES (API)

### P8 — Members module
ROLE: Developer. DEPENDS: P4, P7.
PROMPT: Implement members CRUD for types person|company|group|vehicle with
the prototype's add-member flow fields; race-safe `GP-XXXX` numbering
(advisory lock + UNIQUE + retry, 1.4); optimistic-locked edits returning
409 on stale version; share and deposit accounts opened atomically with the
member; member statement endpoint (keyset-paginated, mirrors prototype
statement rows); status transitions Active↔Arrears→Exited via the
transition function (1.4). Audit + outbox welcome notification (1.5, 1.2).
EXIT: N+1 query-count assertions on list endpoints; double-submit creates
exactly one member; 409 test green.

### P9 — Loan applications, committee, guarantors
ROLE: Developer. DEPENDS: P8.
PROMPT: Implement loan products (rate, deposit multiplier, max term),
applications with cover% computation (deposits+guarantees vs amount ×
product rules), the P6 stage machine under SELECT…FOR UPDATE, committee
voting (quorum → Approved/Rejected, one vote per member enforced by
UNIQUE), and guarantorship: pledge creation with available-capacity check
(deposits minus existing pledges) computed under lock to prevent
over-pledging (1.4); guarantor consent recorded; release on full repayment.
All mutations audit-logged; decisions notified via outbox.
EXIT: concurrent over-pledge test proves capacity never exceeded; stage
machine adversarial tests green.

### P10 — Loan servicing & portfolio
ROLE: Developer. DEPENDS: P9.
PROMPT: Implement disbursement (P7 atomic contract), repayment allocation
(penalties→interest→principal, documented), nightly arrears job computing
days-past-due → classification → provisioning per tenant in batches (no
long transactions, 1.3), loan book endpoints with classification pills and
NPL/PAR-30 portfolio summaries feeding the dashboard, and early settlement
quotes. Guarantee release hook on closure.
EXIT: golden-file schedule tests; arrears job idempotent (re-run changes
nothing); dashboard aggregates match seeded fixtures to the cent.

### P11 — Transactions & interest
ROLE: Developer. DEPENDS: P8, P7.
PROMPT: Implement deposits, withdrawals (balance check under row lock, no
blocking I/O while held, 1.3/1.4), share top-ups, and the quarterly
deposit-interest accrual job: rate exclusively from tenant configuration,
period resolved server-side in strict quarter order (never caller-supplied
or backdatable), basis = ledger-reconstructed AVERAGE DAILY BALANCE under
the account row lock (never a point-in-time snapshot), batched via the
shared runner, idempotent by period UNIQUE claimed ON CONFLICT DO NOTHING,
INT- postings stamped at period end via P7. Ledger listing endpoint with
keyset pagination and filters matching the prototype columns (date, ref,
member, type, DR/CR, channel) — bound parameters only.
EXIT: concurrent withdrawal test never overdraws; interest job re-run
posts nothing new and scans nothing; a last-day deposit earns exactly its
pro-rata share (hand-computed oracle).

### P12 — Member exit & settlement
ROLE: Developer. DEPENDS: P10, P11.
PROMPT: Implement exit workflow per prototype: eligibility check under the
member row lock (no active loan unless netted; active guarantees block exit
until released/substituted), settlement computation (shares + deposits −
loan balance − fees) persisted as an approved SNAPSHOT row that the
committee approves and that posting re-verifies component-by-component
under the full lock set (409 on drift — 1.4 snapshot rule). Exit fees come
from tenant/product configuration, never the request body (1.6). Handle
negative settlements (loan balance exceeds assets) as an explicit,
documented, tested branch. Committee approval reuses the P9 voting
machinery. Atomic settlement posting via P7 in ONE application-service
transaction (postings + zeroed balances + guarantee release + terminal
transition + audit + outbox). Exit statement document via export path (if
P13 is not yet built, ship a minimal statement endpoint and record the
blocker issue per the standing rule).
EXIT: guarantee-blocked exit test green; settlement is atomic (kill-switch
test leaves no partial state); approval-drift test returns 409 and posts
nothing; exited members are rejected by every mutation path.

### P12.5 — Phase B debt consolidation (issues #12, #13, #15)
ROLE: Developer + DBA. DEPENDS: P12.
PROMPT: Close all open Phase B debt before reports, so P13 reads a settled, trustworthy ledger:
- Issue #13: reproduce and fix the pre-existing backend:test failures on main; a fully green main pipeline gates every later prompt.
- Issue #12 (P7): enforce open accounting period / occurred_at validation on EVERY ledger posting — period resolved server-side, never caller-backdatable (the P11 caller-input lesson); postings into closed periods return 409; additive migration if a periods table is needed, with RLS matching 0001.
- Issue #15 (P9→P10): enforce the product deposit-multiplier rule at approval/disbursement under the full row-lock set, and link guarantees to the loan at disbursement so P10 closure release and P12 exit sweeps always find them; backfill-safe for existing rows.
- Verify #14 and #17 are fully closed by !19; fix any residue here.
All under standing gates: explicit tenant predicates on reads AND writes, RequirePermission, Idempotency-Key, least-disclosure errors, hand-computed oracles, EXPLAIN + indexes shipped together, kill-switch atomicity wherever money moves.
EXIT: main pipeline fully green including backend:test; backdated and closed-period posting tests return 409; concurrent over-multiplier disbursement provably blocked; guarantee–loan linkage proven by release-on-closure and exit-sweep tests; issues #12, #13, #15 closed.

### P13 — Reports & exports
ROLE: Developer. DEPENDS: P10, P11, P12.5.
PROMPT: Implement `run_export(query, batch_size)` in `core/exports`:
fetch batch_size+1 for truncation detection, stream batches off the event
loop, set `X-Export-Truncated`/`X-Export-Limit`, enforce row caps (1.3).
Build reports: member statements, trial balance, loan book with
classification/provisions, disbursement & collections, NPL trend (monthly
series as in prototype bars). CSV + PDF rendered in worker via outbox jobs.
Hardened requirements (P11/P12 deep-review lessons — each is a merge
blocker, not guidance):
(a) Callers NEVER supply money, cost, or filesystem-path parameters:
formats, row limits, and storage locations come exclusively from
server-side configuration; request bodies `extra="forbid"` (1.6, the
P11 caller-rate / P12 exit-fee lesson).
(b) CSV formula-injection escaping: any cell whose value begins with
`=`, `+`, `-`, `@` (or tab/CR-prefixed variants) is quoted/prefixed so
spreadsheet apps treat it as text — mandatory test exporting a member
named `=HYPERLINK(...)` and asserting the emitted cell is inert.
(c) Every export route carries `RequirePermission` per the P4 matrix,
and every read carries an explicit bound `tenant_id` predicate on top
of forced RLS (1.6 v1.1; issue #17 precedent).
(d) Keyset streaming only, with hard server-side row caps and the
truncation headers — never OFFSET, never an unbounded scan (1.3).
(e) Export artifacts contain no PII beyond the caller's entitlement
(column allow-lists per role); storage paths are unguessable (random
tokens, never enumerable ids); download links expire.
(f) An audit row for EVERY export capturing who exported what scope
(report, filters, row count, truncation) — exports are the
exfiltration channel, so the audit IS the control (1.5).
(g) Export jobs are idempotent by `Idempotency-Key`; re-submission
proven by side-effect row counts (one artifact, one audit row, one
outbox event), never by return values alone (1.4).
(h) Snapshot-consistent reads: each export renders from a single
transaction (or explicit as-of semantics) so it can never interleave
with a concurrent settlement and show partial state — the P12
quote/approve/post TOCTOU precedent applied to reads.
(i) Kill-switch atomicity test for the export job runner: abort
mid-job and prove zero partial state (no artifact, no claim row, no
audit, no outbox event) (§4).
(j) EXPLAIN assertions for every report query, with the indexes that
back them shipped in the same migration (1.3; P10–P12 precedent).
(k) Wire the P12 exit statement onto the export path (CSV/PDF of the
`GET /member-exits/{id}/statement` document) per blocker issue #16 and
close it — the JSON endpoint stays the canonical data source.
(l) Standing anti-reward-hacking rules: hand-computed oracles in test
comments, no tautological tests (every guard test must fail with the
guard removed), honest DoD per §5.8.
EXIT: truncation-header tests green; event-loop blocking test (export
while serving latency-checked requests) passes; formula-injection test
green; export-audit and idempotency side-effect tests green; kill-switch
job test green; EXPLAIN artifact captured in CI; issue #16 closed with
the exit-statement export tested end to end.

### P13.5 — System users administration & audit-log viewer
ROLE: Developer + Security Analyst. DEPENDS: P4, P13.
PROMPT: Implement the prototype Access-control "Users" tab and the audit
read path (gap register: docs/GAP_ANALYSIS.md §2.1). Users CRUD on the
existing `users` table: create (email/phone/full name/role/branch),
edit (optimistic-locked, 409 on stale version), activate/suspend via a
single transition function (1.4) — a suspended user's tokens are
refused at auth (refresh-family revocation reused from P3) and pending
OTP challenges voided in the same transaction; role assignment is an
audited mutation (1.5); OTP/credential lifecycle: admin-triggered OTP
re-enrolment and challenge invalidation, never OTP disclosure; track
`last_active_at` (updated at token issue only — no per-request write
amplification). Deny-by-default: all routes RequirePermission
access_control × action; a user can never edit their own role/status
(user-level separation, the P12 precedent). Add `GET /audit-log`:
keyset-paginated, filterable by entity/actor/action/date, RequirePermission
access_control:view, explicit bound tenant_id predicates, index shipped
with the query, before/after payloads redacted per role entitlement
(least disclosure, 1.6). Self-lockout guard: the last active System
Admin cannot be suspended (checked under the user row lock).
EXIT: matrix tests cover the new routes for every role; suspended-user
token-refusal and OTP-void tests green; self-role-edit and last-admin
tests green (each fails with its guard removed); audit-viewer EXPLAIN
captured; migrate-check green.

### P13.6 — Branches registry
ROLE: Developer + DBA. DEPENDS: P13.5.
PROMPT: Branches table (tenant-scoped, RLS per ADR-0002, UNIQUE
(tenant_id, name), NOT NULL defaults, version column) with CRUD under
settings permissions; assign users and members to branches:
expand-only migration adding nullable `branch_id` FKs (indexed, 1.3)
alongside the existing free-text `users.branch`, plus a batched
backfill creating branch rows from distinct legacy text values
(shared batch runner, re-run is a lock-free no-op — v1.1 rule 8);
contract of the text column deferred one release (§3 migration
policy). Branch is organisational metadata only — no money path may
key on it in this prompt (cash/till management is explicitly out of
scope; recorded in GAP_ANALYSIS §2.6).
EXIT: leakage suite extended to branches; backfill idempotence proven
by side-effect row counts; migrate-check up→down→up green.

### P13.7 — Tenant settings, parameters & approval matrix
ROLE: Developer + Solutions Architect. DEPENDS: P13, P13.5.
PROMPT: Generalise `tenant_settings` (0009) into the prototype Settings
screens' backend (GAP_ANALYSIS §2.9): interest config (deposit-interest
%, dividend %, penalty rate %/mo, penalty grace days, penalty charged-on
basis, tiered loan-rate bands as a validated JSONB or child table),
global parameters (min share capital, registration fee, min monthly
contribution, max member exposure, dormancy period months, financial
year end, exit notice period), approval matrix (committee size, quorum,
per-authority amount bands), per-product guarantors-required. All
changes versioned, optimistic-locked, audited with before/after (1.5),
RequirePermission settings × view/edit; DB CHECKs on every bound
(1.5); request bodies `extra="forbid"` — settings ARE the money
parameters, so this API is the single legitimate writer and nothing
else may accept them (1.6, v1.1 rule 1). Wire consumers: committee
quorum (P9 `decide`) and exit quorum (P12) read tenant config with the
current constants as fallback defaults; hand-computed quorum-change
tests prove in-flight votes are decided under the config read at vote
time, never retroactively. Approval authority bands enforced at stage
transitions (submitted→…→approved) under the application row lock.
Interest method/basis (flat, actual/365) are stored but the P6 engine
extension is explicitly OUT of scope — attempting it here would fork
loan math; record a follow-up prompt when a tenant needs it.
EXIT: settings CRUD + consumer tests green; a quorum change mid-vote
test proves no retroactive decision; authority-band adversarial test
(officer ratifying above their band → 403/409) green and fails with the
guard removed; migrate-check green.

### P13.8 — Penalty-on-arrears accrual
ROLE: Developer + QE. DEPENDS: P10, P13.7.
PROMPT: Nightly penalty accrual in the arrears job path: for loans past
the tenant-configured penalty grace, accrue penalty at the configured
rate on the configured basis (instalment-in-arrears or full
outstanding) into `loans.penalty_due` — config exclusively from P13.7
tenant settings (v1.1 rule 1), period basis derived from the schedule
and repayment ledger, never from mutable point-in-time state (v1.1
rule 2). Batched via the shared runner, idempotent by (loan, period)
claim (`INSERT ... ON CONFLICT DO NOTHING` + rowcount — v1.1 rule 5);
re-run accrues nothing new (anti-join test). Ledger recognition stays
on receipt (P10 allocation posts income.penalties) — this prompt only
maintains the receivable-side `penalty_due`, documented as such.
Hand-computed oracle: a loan 12 days past grace at 1%/mo on a 10,000
instalment accrues exactly the documented figure.
EXIT: idempotent re-run proven by side-effect counts; oracle test
green; kill-switch mid-batch leaves no partial accrual claims;
EXPLAIN for the accrual scan captured with its index.

### P13.9 — Dashboard & guarantor aggregates
ROLE: Developer. DEPENDS: P11, P13.
PROMPT: Serve the remaining prototype dashboard figures (GAP_ANALYSIS
§2.2, §2.4): `GET /dashboard/summary` — total deposits, total share
capital, active-member count, members-by-type (SQL aggregates over
account/member tables, never Python loops over rows); monthly
deposits-vs-disbursements series reconstructed from the transactions
ledger (bounded months from server config, the NPL-trend precedent);
applications pipeline counts per stage; guarantor aggregates (active
guarantees, total pledged, per-guarantor free capacity reusing
live_pledged_total — 1.1). Every read carries explicit bound tenant_id
predicates on top of RLS; RequirePermission per module the figure
belongs to — a caller sees only slices their matrix grants (deny by
default, composite responses assembled per-permission, 1.6). Read-only
display data: no locks taken, documented as advisory vs the binding
gates. Indexes shipped with every aggregate query + EXPLAIN assertions
(1.3). Dashboard figures match seeded fixtures to the cent (P10 EXIT
precedent).
EXIT: fixture-oracle tests green; permission-slicing test green (a
teller gets no loan-book slice); EXPLAIN artifact captured.

### P13.10 — Remaining prototype reports
ROLE: Developer. DEPENDS: P13, P13.7.
PROMPT: Add to the P13 export registry (reuse run_export — 1.1, all P13
blockers a–l inherited as merge blockers): portfolio-at-risk aging
(balances bucketed 0–30/31–90/91–180/181–360/360+ from
schedule-vs-repayment reconstruction, the NPL-trend method); membership
register (keyset over members, PII columns gated per role — blocker e);
income statement (P&L grouping over the ledger income/expense accounts,
period-scoped); SASRA return (skeleton mapping the trial balance to the
regulator's line items, clearly versioned per return format). The
dividend & rebate schedule report ships WITH P13.11 (its data source)
on this registry, keeping strict prompt ordering. Each report: EXPLAIN +
index in the same MR, cardinality bounds documented, formula-injection
tests for every new text column source.
EXIT: per-report oracle tests green; truncation/audit/idempotency
side-effect tests inherited from the P13 harness pass for each new
report; EXPLAIN artifact extended.

### P13.11 — Dividends & share lifecycle
ROLE: Developer + DBA. DEPENDS: P11, P13.7.
PROMPT: Dividend on shares and deposit rebates per the prototype
settings: declaration (rate from P13.7 config only — v1.1 rule 1;
committee approval via the P9 voting machinery binding to a persisted
snapshot of the declaration totals — v1.1 rule 3), distribution job
(shared batch runner; basis = ledger-reconstructed average share
balance over the FY, the P11 ADB precedent — v1.1 rule 2; idempotent
per (member, declaration) claim — rule 5; postings via P7 with
occurred_at at period end — 1.5), and share transfer at exit
(transferee must be an active member; runs under both members' row
locks in id order to prevent deadlock; documented against the P12 lock
chain). New tables additive with RLS; every guard falsifiable;
kill-switch atomicity on the distribution batch and the transfer.
EXIT: hand-computed dividend oracle (incl. a member who joined
mid-year earning pro-rata); double-distribution proven impossible by
claim counts; transfer deadlock test (two opposing transfers) passes;
migrate-check green.

### P13.12 — Member KYC profiles & documents
ROLE: Developer + Security Analyst. DEPENDS: P8.
PROMPT: Persist the prototype's type-specific registration data
(GAP_ANALYSIS §2.3): per-type profile tables or a validated
per-type JSONB with DB CHECK on member type (Person bio/employment/
next-of-kin; Company registration/signatories; Group officials;
Vehicle compliance/ownership incl. licence and insurance expiries),
member category, DPA-2019 consent flag captured at registration
(timestamped, immutable once set), and the per-type document checklist:
document metadata rows (type, status, expiry) with binary content
behind an infrastructure storage adapter — object-store choice needs an
ADR (§6); until then metadata-only with upload deferred is acceptable
and recorded. PII discipline (1.6): KYC fields never appear in logs,
error messages, or export columns without the members:view entitlement;
document access is audited like exports (P13 blocker f precedent).
EXIT: profile validation matrix tests (wrong-type payload → 422);
consent immutability test; document-access audit rows proven; leakage
suite extended to the new tables.

### P13.13 — Dormancy lifecycle
ROLE: Developer. DEPENDS: P8, P13.7.
PROMPT: Add `dormant` to the member status machine (expand-only CHECK
migration): Active→Dormant by a nightly job when no member-initiated
transaction has occurred within the configured dormancy period
(ledger-derived last-activity, not a mutable column — v1.1 rule 2);
Dormant→Active on any new deposit (automatic, in the deposit
transaction); Dormant→Exited allowed through the P12 workflow. Dormant
members may deposit but not borrow/pledge/withdraw (documented, tested;
transition function is the single gatekeeper — 1.4). Job via shared
batch runner, idempotent re-run, member FOR UPDATE per transition batch
row consistent with the P12 lock chain (member first).
EXIT: full-matrix transition tests updated; dormancy job idempotence by
side-effect counts; reactivation-on-deposit test; exit-of-dormant test.

### P13.14 — Guarantee release & substitution
ROLE: Developer. DEPENDS: P9, P12.
PROMPT: Per-guarantee release/substitution (prototype Guarantors screen
"Release"; unblocks P12 exits and unconsented-pledge disbursements —
GAP_ANALYSIS §2.4). Release rules under the application/loan row lock
(1.4): a pledged (unconsented) guarantee may be released by the
guarantor or staff with applications:edit; an active guarantee backing
an undisbursed application may be released only if remaining cover
still satisfies the product rule, re-verified under the borrower's
deposit-account lock (the P7 gate math — 1.1); an active guarantee
behind a DISBURSED loan may only be substituted, never bare-released:
substitution = new consented pledge of ≥ the released amount in the
SAME transaction (atomic swap, kill-switch tested). recompute_cover
runs in-transaction; audit + outbox both sides (guarantor notified —
1.5/1.2). Lock order: application/loan row → guarantor member FOR
SHARE → guarantor deposit account FOR UPDATE (the established pledge
chain; document against the P12 settlement set).
EXIT: release-below-cover rejected (fails with guard removed);
substitution atomicity kill-switch test; exit-unblocked-after-release
end-to-end test green.

### P13.15 — Ledger corrections, misc fees & write-off
ROLE: Developer + DBA. DEPENDS: P10, P13.7 (fee config).
PROMPT: The documented correction paths the reversal blocks require
(the Codex-review MR blocks generic reversal of repayment-linked
transactions because it would desynchronise loans.balance/penalty_due
and the repayments history): (1) repayment adjustment — a dedicated
service that, under the loan row lock, posts the reversing ledger legs
via P7, writes a negative-linked repayments correction row, restores
loans.balance/penalty_due/schedule state from the allocation being
undone, and re-opens a closed loan ONLY via an explicit documented
branch of the transition map (1.4) — one atomic transaction,
kill-switch tested; (2) misc fee posting (prototype "Fee" type) with
fee amounts exclusively from P13.7 config (v1.1 rule 1), FE- reference
prefix via the P7 generator; (3) loan write-off: committee-approved
(P9 voting, snapshot-bound — v1.1 rule 3) transition to written_off
with the provisioning posting, making the domain status reachable
(GAP_ANALYSIS §2.5). All corrections append-only — never UPDATE a
posted row (1.5); audit rows carry the exact figures, errors stay
least-disclosure.
EXIT: adjustment restores hand-computed pre-repayment state
component-by-component; corrected-then-re-adjusted double-run blocked
by claim; write-off reachable only through quorum; kill-switch tests
green; migrate-check green.

### P13.16 — Collections & recovery worklist
ROLE: Developer. DEPENDS: P10, P13.5.
PROMPT: Minimal recovery workflow behind the prototype's "Initiate
recovery" action and the P18 arrears worklist: `recovery_cases`
(additive, RLS): open (only for NPL-classified loans, checked under the
loan row lock — 1.4), assign (P13.5 users), note, close on cure
(automatic when the loan leaves NPL in the arrears job) or on
write-off (P13.15). Keyset worklist endpoint ordered by days-past-due
with its index + EXPLAIN (1.3). No money moves in this prompt.
Audit + outbox on every case mutation (1.5/1.2).
EXIT: open-on-performing-loan rejected (falsifiable); auto-close-on-cure
test; worklist EXPLAIN captured; matrix tests for the new routes.

### P13.17 — DSA hardening remediations
ROLE: Developer + DBA. DEPENDS: P13 (a,b,d), P3 (c), P5 (e).
PROMPT: Execute the High/Medium remediations of docs/DSA_HARDENING.md
without changing any observable money semantics (every migration
additive, every re-computation cross-checked against the existing
reconstruction as oracle):
(a) DSA-1: month-end portfolio snapshots written incrementally (arrears
    job or close_period), NPL-trend export reads snapshots + current
    month only; snapshot writer reuses NPL_TREND_MONTH_SQL as the
    single source of truth; backfill via shared batch runner.
(b) DSA-2/DSA-5: per-account period rollups at close_period; trial
    balance = closed rollups + open-period aggregate; equality-with-
    full-scan property test on seeded history is the merge gate.
(c) DSA-3: idempotency_keys expires_at + replay-lookup fence + batched
    purge job (v1.1 rule 8); retention value from server config.
(d) DSA-4: incremental PDF rendering (drop the rows accumulation);
    object-store artifact ADR decision — implementation only if the
    ADR is accepted, otherwise document the bounded-cap rationale.
(e) DSA-6: outbox dispatched-row retention purge, set-based lease
    UPDATE, due-tenant discovery query replacing the all-tenant sweep.
Each item lands as its own commit with before/after EXPLAIN or
row-count evidence; re-runs of every new job are lock-free no-ops.
EXIT: equivalence oracles green (snapshot vs full-scan to the cent);
purge jobs idempotent by side-effect counts; export latency test still
green; migrate-check up→down→up green for every new migration.

---

## PHASE C — CLIENTS

### P14 — Web admin scaffold (issue #8)
ROLE: Developer (Frontend). DEPENDS: P4.
PROMPT: Scaffold `web/` per MASTER_PROMPT §2.3: Next.js + TS strict,
zero-warning eslint, design-system package with tokens extracted verbatim
from prototype CSS variables, OpenAPI-generated client (regeneration script
in CI drift-check), TanStack Query + Zod, auth/OTP flow, route guards from
`/me/permissions`, keyset-pagination table component.
EXIT: `web:*` CI jobs green; client-drift check fails on stale client.

### P15 — Web admin features
ROLE: Developer (Frontend) + QE. DEPENDS: P14 + each corresponding API
prompt (build module-by-module in this order):
Dashboard (P10) → Members (P8) → Applications+Committee (P9) → Loan book
(P10) → Guarantors (P9) → Transactions (P11) → Member exit (P12) →
Reports (P13) → Settings/products (P9) → Access control (P4).
PROMPT: Reproduce the prototype screens with real data; optimistic-lock 409
handling as inline conflict banners; idempotency keys on all mutations;
no PII in client analytics (1.6). Playwright E2E per module happy path plus
one adversarial flow (stale edit, forbidden role).
EXIT: all ten modules E2E-green; Lighthouse perf budget documented.

### P16 — Flutter workspace (issue #9)
ROLE: Developer (Mobile). DEPENDS: P4.
PROMPT: Scaffold `mobile/` per MASTER_PROMPT §2.4: `member_app`,
`admin_app`, shared `gp_ui` (prototype palette tokens) and generated
`gp_api_client`; Riverpod; secure token storage; certificate pinning;
biometric step-up; offline read cache. `mobile:*` CI jobs green.
EXIT: both apps boot to authenticated shell against staging API.

### P17 — Member app features
ROLE: Developer (Mobile) + QE. DEPENDS: P16, P8–P13, P19 for payments.
PROMPT: Build: onboarding + OTP; balances (shares/deposits/loan);
statements (cursor-paginated, offline-cached); deposit via M-Pesa STK with
pending-intent status polling; loan application with product rules and
live installment preview (values from API, never local math — 1.1);
guarantor consent inbox; repayments; notifications. integration_test per
flow including airplane-mode statement read and double-tap submit
(exactly-one-effect).
EXIT: all flows integration-test green on Android + iOS CI matrix.

### P18 — Admin mobile app features
ROLE: Developer (Mobile). DEPENDS: P16, P8–P12.
PROMPT: Build the field-officer subset: member lookup + onboarding,
application capture, committee vote (biometric step-up), arrears worklist,
txn capture. Same gates as P17.
EXIT: flows integration-test green; RBAC verified per role on-device.

---

## PHASE D — INTEGRATIONS & LAUNCH

### P19 — M-Pesa (issue #10)
ROLE: Developer + Security Analyst. DEPENDS: P11.
PROMPT: Write ADR + threat model first (sign-off required). Implement STK
push against stored payment intents; source-verified, intent-validated,
idempotent callbacks (duplicate → one posting); posting + notification via
outbox in one transaction; daily reconciliation job that alerts on mismatch
and never auto-mutates the ledger; callback rate limiting; secrets via
CI/CD variables only. Adversarial tests: replayed, forged, out-of-order,
timeout-then-success callbacks.
EXIT: sandbox end-to-end deposit reflected in ledger and member statement;
all adversarial tests green.

### P20 — Notifications
ROLE: Developer. DEPENDS: P5, P8–P12.
PROMPT: Wire real SMS/email/push providers behind the P5 adapters with
per-tenant templates and per-channel circuit breakers; delivery-status
writeback; member notification preferences. All sends outbox-only (1.2);
no PII beyond the minimum in payloads (1.6).
EXIT: provider-outage chaos test: actions succeed, events dead-letter and
replay cleanly.

### P21 — Observability & performance
ROLE: QE + Developer. DEPENDS: Phase B complete.
PROMPT: Add OpenTelemetry traces/metrics/logs (PII-scrubbed), dashboards
(p95 latency, error rate by category, outbox lag, job durations), alerts.
Load tests (k6): 10k-member tenant, 50 concurrent tellers; verify keyset
pagination flatness and zero lock-wait timeouts; capture EXPLAIN for the
top 20 queries into `docs/perf/`.
EXIT: p95 < 300ms on hot reads, < 800ms on posting writes at target load;
alerts fire in a game-day drill.

### P22 — Deployment & environments
ROLE: CI/CD Engineer. DEPENDS: P21.
PROMPT: Extend the pipeline with deploy-review (per-MR review apps +
DAST), deploy-staging (auto on main), deploy-prod (manual, protected
environment). Container scanning on built images; SBOM artifact. Managed
Postgres with PITR backups; documented + rehearsed restore (DR drill);
zero-downtime migration policy (expand→migrate→contract) enforced by MR
template checkbox. Store demo/live app distribution (Flutter) via CI.
EXIT: full promote path exercised; restore drill documented under 30 min.

### P23 — Security hardening & tenant onboarding
ROLE: Security Analyst. DEPENDS: P22.
PROMPT: Run full DAST against staging; triage every scanner finding
(critical=block). Verify: RLS leakage suite, rate limits, secret scanning
clean, dependency review, audit-log completeness sampling, log PII audit.
Build tenant onboarding runbook: tenant record, admin bootstrap, product
config, M-Pesa credentials via secret manager, RBAC seed. Data protection
(Kenya DPA) checklist: retention, subject access export via P13, breach
playbook.
EXIT: zero open critical/high findings; onboarding runbook executed
successfully for a pilot tenant on staging.

### P24 — UAT & launch
ROLE: Product Manager + QE. DEPENDS: P23.
PROMPT: Script UAT against every prototype screen/behavior as acceptance
cases (dashboard figures, classification pills, committee flow, exit
settlement, statements, RBAC per role). Pilot tenant runs 2 weeks on
staging with real workflows; defects triaged daily, fixes follow the full
gate process (no shortcuts). Launch checklist: alerts on, on-call rota,
rollback plan rehearsed, support runbook.
EXIT: UAT sign-off; production tenant live; week-one error budget intact.

---

## STANDING RULES
If any prompt's EXIT cannot be met, stop, record the blocker as an issue
referencing the prompt ID, and do not start dependent prompts. Never
weaken a gate to pass an EXIT.

HARDENED STANDARDS (v1.1 — proven on the !17 deep-review sweeps; apply to
every prompt, retroactively on touched code and forward on new code):
1. Money parameters (rates, fees, periods) are server-resolved from tenant/
   product configuration; request bodies reject them (`extra="forbid"`).
2. Period-derived values use ledger-reconstructed bases (average daily
   balance), never point-in-time snapshots.
3. Approvals bind to persisted snapshots re-verified under locks at
   execution; drift returns 409 and posts nothing.
4. Explicit bound `tenant_id` predicates on every tenant-owned read AND
   write, on top of forced RLS.
5. Uniqueness claims are atomic (`INSERT ... ON CONFLICT DO NOTHING` +
   rowcount), never SELECT-then-INSERT.
6. SQL values are always bound parameters; identifiers only from code-owned
   mappings, commented as such.
7. Least-disclosure errors; exact figures live in the audit row.
8. Batched jobs go through the shared batch runner; re-runs must be
   lock-free no-ops (anti-join on the claim key).
9. Anti-reward-hacking test rules and kill-switch atomicity tests per
   MASTER_PROMPT §4; honest DoD per §5.8; pre-implementation review per
   §5.9.
10. Incremental push discipline: commit + push + observe pipeline per
    coherent unit; a crashed session must never lose completed work.
