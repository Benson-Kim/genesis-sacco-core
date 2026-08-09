# GENESIS PRESTIGE — SEQUENTIAL BUILD PROMPTS (P0–P24)

Execute strictly in order. A prompt may start only when every prompt it
depends on has met its EXIT criteria. Every prompt inherits the whole of
`docs/MASTER_PROMPT.md`; gate references (1.1–1.6) are merge blockers.
Each prompt = one branch, atomic commits, one MR, pipeline fully green.
Agent commits carry `Duo-Workflow-Definition: ci_expert_agent/v1`.

Prompt format: ROLE / DEPENDS / PROMPT (give verbatim to the executor) / EXIT.

---

## STATUS REGISTER (authoritative — update in the same MR as the work)

Evidence-based as of main @ `cf1744e0` (2026-08-07), post-!79–!83
merges + the batch-10 merge `a507c62` (alembic head on main **0041**
as-merged — 0039 member dividend payout preference / 0040
share-transfer maker-checker / 0041 members numeric member_no index;
NO in-flight migration claim; see the registry deltas below). Legend: ✅ DONE (merged to main, EXIT met) · 🔄 IN PROGRESS
(open MR cited) · ❌ TODO (no evidence on main). A prompt may not be
marked ✅ without citing its evidence (migration number, merged MR,
or test/artifact present on main).

| Prompt | Status | Evidence on main |
|---|---|---|
| P0 | ✅ DONE | CODEOWNERS, MR template, protected main |
| P1 | ✅ DONE | backend/ scaffold, backend:* CI jobs |
| P2 | ✅ DONE | 0001 schema v1 + RLS + leakage suite |
| P3 | ✅ DONE | 0002 refresh tokens; test_auth, test_otp_domain |
| P4 | ✅ DONE | test_rbac_matrix / test_rbac_endpoints |
| P5 | ✅ DONE | 0003 outbox; test_outbox P5 suites |
| P6 | ✅ DONE | domain/lending; test_lending |
| P7 | ✅ DONE | 0004 ledger; test_ledger_* |
| P8 | ✅ DONE | test_members_api / test_members_domain |
| P9 | ✅ DONE | 0005 committee votes, 0006 keyset index |
| P10 | ✅ DONE | 0007 loan servicing; test_loan_servicing |
| P11 | ✅ DONE | 0008/0009; test_transactions_integration |
| P12 | ✅ DONE | 0010 member exit; test_member_exits |
| P12.5 | ✅ DONE | 0011 guarantee backfill, 0012 accounting periods |
| P13 | ✅ DONE | 0013 exports, 0014 ledger integrity; test_run_export |
| P13.5 | ✅ DONE | 0015; test_users_admin, test_audit_log_api |
| P13.6 | ✅ DONE | 0016 branches; test_branches |
| P13.7 | ✅ DONE | 0017 tenant settings; test_tenant_settings |
| P13.8 | ✅ DONE | 0019 penalty accruals; test_penalty_accrual |
| P13.9 | ✅ DONE | test_dashboard (no migration, per prompt) |
| P13.10 | ✅ DONE | 0023 (!40); PAR-aging/register/income/SASRA export tests |
| P13.11 | ✅ DONE | 0020 (!30); test_dividends*, test_share_transfers |
| P13.12 | ✅ DONE | 0018 member KYC; test_member_kyc_* |
| P13.13 | ✅ DONE | 0021 dormancy (+0022 dividend-dormant policy fix); test_dormancy |
| P13.14 | ✅ DONE | test_guarantee_release |
| P13.15 | ✅ DONE | 0025 (!46, merged 2026-08-02); test_corrections |
| P13.16 | ✅ DONE | 0026 recovery cases (!47, merged 2026-08-02); test_recovery_cases / test_recovery_domain; combined-state pipelines 2725256564 / 2725262530 |
| P13.17 | ✅ DONE | (e) DSA-6 via !44 (0024, merged); (a)–(d) via !49 (0027–0029, merged 2026-08-02); FM1–FM4 suites green, combined-state pipeline 2725427849 (757 passed) |
| P-DIAG.0 | ✅ DONE | docs/diagrams/lock-order.md |
| P-DIAG.1 | ✅ DONE | c4-context/container/component + c4-spot-check.py |
| P-DIAG.2 | ✅ DONE | erd.md + erd-spot-check.py |
| P-DIAG.3 | ✅ DONE | dfd.md |
| P-DIAG.4 | ✅ DONE | stride.md |
| P-DIAG.5 | ✅ DONE | sequence-committee-voting / -outbox-dispatch / -snapshot-bind-reverify |
| P14 | ✅ DONE | !13 merged 2026-08-03; web:* jobs green (incl. spec/client drift + permanent stale-client negative proof) |
| P14.5 | ✅ DONE | 0035 member identity (!65); test_member_auth / test_member_identity FM1–FM5 + test_idempotency cross-actor MISS; combined-state pipeline 2732748398 green incl. migrate-check up→down→up (823 passed, cov 89.03%) |
| P15 | 🔄 IN PROGRESS | module batches 1–7 DELIVERED & MERGED, one MR per batch — batch 1 !73 (squash `fc9312a` / merge `4cf3ca3`), batch 2 !72 (`3ca038d` / `9b44170`), batch 3 !74 (`58c7e7a` / `99d2f0c`), batch 4 !75 (`1fb3a2b` / `4ec2cee`), batch 5 !76 (`ad593d5` / `fbbfe42`), batch 6 !77 (`b28e3c0` / `eeb8113`), batch 7 !78 (`b4fb116` / `34f4ace`), batch 8 !79 (`e6f6936` / `66d22cf`, migration 0039), batch 10 — share-transfer maker-checker safeguards + history register, migration 0040 — merged as `a507c62` (squash `adfa0a5`; its MR record did not survive the 2026-08-07 re-import — rule 16: git-log shas are the citation), batch-7 remediation !83 N1–N3 (`b2a7eba` / `961be8b`, migration 0041) — every sha verified in the main git log 2026-08-07; batch 9 !81 — DA-72.2 Kv promotion 29→1 to the design system (`180b229` / `23dfec7`) — and the !82 docs pack (CLAUDE.md + .claude, `5399568` / `cf1744e0`) merged alongside; batch 11 (hygiene round: Kv residuals (o), the !70 nullable flag, this register refresh) IN PROGRESS on !84, branch `duo/feature/31-batch11-hygiene-round` (claims NO migration) |
| P16 | ❌ TODO | no mobile/ tree |
| P17 | ❌ TODO | depends P16, P14.5 |
| P18 | ❌ TODO | depends P16 |
| P19 | ❌ TODO | no payment_intents |
| P20 | ❌ TODO | providers are StubProvider only |
| P21 | ❌ TODO | — |
| P22 | ❌ TODO | security-template non-spawn debt still open (rule 13) |
| P23 | ❌ TODO | — |
| P24 | ❌ TODO | — |

**Provenance note (rule 16, 2026-08-07):** this repository has been
re-imported/moved multiple times — most recently RE-IMPORTED to
`urus-group4/sacco`, project id **85226474** (2026-08-07 ~21:00 UTC).
Pipeline ids recorded in this register or in merged MR descriptions
BEFORE the re-import are pre-import history and no longer resolve at
the current project id (e.g. the !79/!81/!83 descriptions' 2740608692 /
2740658037 / 2740175038); the git-log evidence (migration numbers,
merge/squash shas) remains authoritative, and fresh pipeline
re-citations live in the issue **#31** re-citation tables rather than
being rewritten here.

**P-DIAG drift flip (rule 11, 2026-08-02 — MR !55, docs-only):** the
P-DIAG.1–.5 rows above remain ✅ with their diagrams reconciled to
main @ `8f46aa5` (alembic head 0032): dfd.md/stride.md P13.15/P13.16
PLANNED labels flipped as-built; the !46/!47/!51/!52 flows drawn
first-class (dfd F10–F14, C4 routers 19/20, ERD subject areas 2.F/2.G,
sequence + `flow-*` business diagrams); both spot-check scripts —
which FAILED on pre-!55 main — extended and passing. At authoring the
in-flight !53 (0033) / !54 (0034) claims were drawn `INCOMING`, never
as-built. *Flip addendum (2026-08-03):* !53 (merge `60dc280`) and !54
(merge `d517769`, squash `cd85309`) landed on main BEFORE this MR, so
!55 itself flips those markers as-built — sequence-recovery-case-
lifecycle.md, flow-recovery-officer.md, dfd.md F14, the stride.md F14
rows and the erd.md head note (→ 0034) — each statement verified
against merged main code at `d517769` (rule 11).

### Review follow-ups (maintainer review of !44, 2026-08-01) — placement

| # | Finding | Lands in |
|---|---|---|
| RF1 | R7: SECURITY DEFINER functions PUBLIC-executable (cross-tenant activity oracle) | !44 itself (0024 REVOKE/GRANT + proacl sweep test) — in flight |
| RF2 | Dead-letter operator requeue/resolve path (dead rows now accumulate forever by design) | P20 — added to its scope: delivery-status lifecycle must include an audited, RBAC-gated requeue/resolve admin path for status='dead' rows |
| RF3 | lock-order.md §8 re-derivation pass over !36/!37 grep totals (owed per its own note) | ✅ settled by !47 (merged 2026-08-02): §8 totals re-derived (!36 +2, !37 +0, P13.16 +4; combined-state 50 → 65) and recorded in lock-order.md §8 |
| RF4 | CI flake eradication: issue #20 EXPLAIN planner flakes + test_run_export latency threshold (pipeline 2724154615) | !44 dispositions the export threshold; the EXPLAIN-flake class needed a micro-MR BEFORE P21 hardens perf gates (P21 FM1 forbids quietly raised thresholds) — ✅ !48 merged 2026-08-02 (tests-only: observed-alternates citations + the enable_sort=off shape pin; no threshold touched; pipeline 2725256675 green, 712 passed; issue #20 closed) |

Migration-claim registry delta (rule 14, updated 2026-08-02): 0001–0029
on main (0024 !44/P13.17e · 0025 !46/P13.15 · 0026 !47/P13.16 ·
0027–0029 !49/P13.17a–d). In-flight claims of the post-P13 backend
hardening batch (issues #21 → #24 → #23, ONE MR each, declared merge
order per rule 12 — each later MR chains on the earlier one's claim):
**0030 is issue #21's** (loan_recoveries, `down_revision = '0029'`);
issue #24's MR claims 0031–0032 next; issue #23's MR claims 0033 after
that. The next free number after the batch is 0034.

Registry delta update (2026-08-02, the !54 sync): **0030–0032 are on
main** (!51 merged `7ec83ad`; !52 merged `8f46aa5`). **0033 stays
!53's in-flight claim** (`down_revision = '0032'`, merges last in the
batch). **0034 is !54's** (the !51-N1/!52-F2/!53-F1-F2 review
follow-up micro-MR: `0034_recovery_claim_cap_lock`,
`down_revision = '0033'`, declared in the MR description at branch
time; merges AFTER !53). The next free number is **0035**.

Registry delta update (2026-08-03, the !55 sync): **0033 and 0034 are
on main** — !53 merged as merge commit `60dc280` (no squash, ancestry
preserved; combined-state pipeline 2726361694 green) and !54 merged
2026-08-03 (merge `d517769`, squash `cd85309`; combined-state pipeline
2726403180 green on head `75e7c94`). Alembic head on main is **0034**;
the next free number stays **0035**.

Registry delta update (2026-08-04, the P14.5 claim): **0035 is the
P14.5 MR's** (member identity & member-facing auth:
`0035_member_identity.py`, `down_revision = '0034'`, declared in the
MR description at branch time per rule 14; head 0034 re-verified
against main's `migrations/versions/` at branch time — no other
in-flight claim exists). The next free number is **0036**.

Registry delta update (2026-08-04, the issue-#30 follow-up claim):
**0035 is the open P14.5 MR's (!65)** (`0035_member_identity.py`,
`down_revision = '0034'`, declared on !65 at its branch time). **0036
is the issue-#30 audit-contract-follow-ups MR's (!66)**
(`0036_actor_attribution.py`, `down_revision = '0035'` — the chained
claim, declared in that MR's description at branch time per rule 14;
head 0034 + the single in-flight 0035 claim re-verified against main's
`migrations/versions/` and the open-MR list at branch time). Declared
merge order (rule 12): **!65 merges FIRST**, then !66; the chain is
exercised on the !66 branch by carrying !65's commits via a merge
commit (the !46-carries-!44 precedent, no rebase). If !65 closes
instead of merging, 0036 re-chains to `'0034'` (the !26/0017 re-chain
discipline). The next free number is **0037**.

Registry delta update (2026-08-05, the issue-#30 close-out claim):
**0035 and 0036 are on main** (!65 and !66 merged 2026-08-05; alembic
head on main is **0036**, re-verified against main's
`migrations/versions/` at branch time — merge commits `2a83323` /
`32fe61e`, remediated to as-built diagrams by !70, merge `047d4e3`).
**0037 is the issue-#30 close-out MR's**
(`0037_committee_recommender.py`, `down_revision = '0036'` —
committee-recommender attribution on `loan_applications`, declared in
that MR's description at branch time per rule 14; head 0036 + the
open-MR list re-verified at branch time: the only open MR, !11, claims
no migration, so no other in-flight claim exists). The next free
number is **0038**.

Registry delta update (2026-08-06, the issue-#31 batch-6 claim):
**0038 is the #31 batch-6 ledger-contract-round MR's**
(`0038_corrections_register_indexes.py`, `down_revision = '0037'` —
two expand-only register keyset indexes on `repayment_adjustments` /
`loan_write_offs` shipped with the ledger (a).1/(a).2 LIST queries,
declared in that MR's description at branch time per rule 14; head
0037 + the open-MR list re-verified at branch time: the in-flight !75
(batch 4) and !76 (batch 5) branches both top out at 0037 and claim no
migration, and !11 is mobile-only, so no other in-flight claim
exists). The next free number is **0039**.

Registry delta update (2026-08-06, the register-refresh sync): **0037
and 0038 are on main** — 0037 via the issue-#30 close-out MR !71
(merged 2026-08-05, merge `508bc96`, squash `578d75d`) and 0038 via
the #31 batch-6 MR !77 (merged 2026-08-06, merge `eeb8113`, squash
`b28e3c0`); alembic head on main is **0038**, re-verified against
main's `migrations/versions/` at `34f4ace`. **0039 stays the #31
batch-8 MR's (!79) in-flight claim**
(`0039_member_dividend_payout.py`, `down_revision = '0038'`, declared
in that MR's description at branch time per rule 14) — `INCOMING`,
never as-built until !79 merges. The next free number is **0040**.

Registry delta update (2026-08-07, the batch-11 sync): **0039, 0040
and 0041 are on main** — 0039 via the #31 batch-8 MR !79 (merged
2026-08-07, merge `66d22cf`, squash `e6f6936`), 0040
(`0040_share_transfer_maker_checker`, `down_revision = '0039'`) via
the #31 batch-10 merge `a507c62` (squash `adfa0a5`; no MR record
resolves at the current project id after the re-import — rule 16:
git-log evidence) and 0041 (`0041_members_numeric_member_no_index`,
`down_revision = '0040'`) via the #31 batch-7-remediation MR !83
(merged 2026-08-07, merge `961be8b`, squash `b2a7eba`); alembic head
on main is **0041**, re-verified against main's `migrations/versions/`
at `cf1744e0`. No in-flight claim exists (the open !84, #31 batch 11,
claims NO migration). The next free number is **0042**.

### Post-P13 hardening follow-up batch (issues #21/#24/#23) — status

| Issue | Scope | Status |
|---|---|---|
| #21 | bad-debt recovery receipts for written-off loans (P13.15 A4) | ✅ DONE — 0030 merged to main 2026-08-02 (!51, squash `7da3ed7` / merge `7ec83ad`) |
| #24 | P13.15 N1 maker-checker for adjustments + N4 repayments append-only trigger | ✅ DONE — 0031–0032 merged to main 2026-08-02 (!52, squash `c866f5a` / merge `8f46aa5`); branch pipeline 2725596935 + rule-12 combined-state pipeline 2725604737 green (row flipped additively by the !54 sync — the flip instruction recorded on the !51/!52 reviews) |
| #23 | P13.16 N2 richer case dispositions + N3 post-closure outcome notes | ✅ DONE — 0033 merged to main 2026-08-03 (!53, merge commit `60dc280`, NO squash — ancestry preserved for the stacked !54); branch pipeline 2725635455 green @ `bf0d508` + rule-12 combined-state pipeline 2726361694 green @ `433cfec` (row flipped additively by the !55 sync) |
| — | Review follow-ups !51 N1 (0034 claim-cap FOR UPDATE) + !52 F2 (rejection rationale) + !53 F1/F2 (atomic restructure-close note + pause reason) | ✅ DONE — 0034 merged to main 2026-08-03 (!54, merge `d517769`, squash `cd85309`); rule-12 combined-state pipeline 2726403180 green on head `75e7c94` (row flipped additively by the !55 sync) |

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
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15), each with a falsifiable test +
    hand-computed oracle: FM1 cross-tenant aggregate bleed — issue-#17
    probe (session AS tenant A, foreign tenant argument → zero rows,
    never a mixed aggregate); FM2 permission-slicing bypass (full
    7-role matrix; composite response omits ungranted slices entirely,
    not zeroed); FM3 aggregate-vs-ledger drift — every KPI figure
    reconciles to the cent against the seeded ledger/fixtures, oracle
    arithmetic in comments; FM4 guarantor free-capacity divergence —
    the aggregate MUST call `live_pledged_total` (1.1); a forked sum is
    a rejected MR, proven by a test that breaks if the aggregate and
    the P9 pledge path ever disagree on the same fixture.
(b) Lock order: this prompt takes NO row locks (read-only MVCC snapshot
    reads, documented as advisory vs the binding gates). No new
    lock-graph edges — the established chains (member → accounts →
    loans; application/loan → guarantor member FOR SHARE → guarantor
    deposit) are untouched.
(c) Parallel track: expected to ship NO migration; if an aggregate
    needs an index, claim the next free number up front per v1.2 rule
    14 (0020 is !30's). No TENANT_TABLES / ENTITY_MODULES additions (no
    new tables, no new audited entities); no `.gitlab-ci.yml` edits —
    the EXPLAIN artifact rides the existing `backend/perf/explain_*.txt`
    + `tests/test_*_explain.py` convention.
(d) v1.1 restated for this surface: explicit bound `tenant_id`
    predicates on every aggregate read on top of forced RLS (rule 4);
    bound parameters only, incl. month-window bounds (rule 6);
    server-resolved window size — the bounded-months config, never a
    caller-supplied range (rule 1); least disclosure — error envelopes
    never echo aggregate figures (rule 7).
(e) Honest DoD: in-project pipeline links only; security-template
    non-spawn recorded per v1.2 rule 13. Process per v1.2 rule 16.
EXIT: fixture-oracle tests green; permission-slicing test green (a
teller gets no loan-book slice); FM1–FM4 falsifiable tests green and
each fails with its guard removed; EXPLAIN artifact captured via the
existing CI convention.

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
on this registry, keeping strict prompt ordering — !30 delivers it;
verify !30's final merged state at execution time and do NOT
re-implement it here. Each report: EXPLAIN + index in the same MR,
cardinality bounds documented, formula-injection tests for every new
text column source.
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15), each falsifiable with
    hand-computed oracles: FM1 PAR-aging bucket-boundary error — a loan
    exactly 30/31/90/91/180/181/360/361 dpd lands in the documented
    bucket, balances reconstructed from schedule-vs-repayment history
    (the NPL-trend method, never mutable state — v1.1 rule 2); FM2
    income-statement conservation — P&L line totals reconcile to the
    cent against the trial-balance income/expense aggregates over the
    same period (cross-oracle, fails if either query drifts); FM3 PII
    over-disclosure — membership-register columns are role-gated
    allow-lists (P13 blocker e); a role without members:view gets no
    PII columns, proven per role; FM4 formula injection — the
    `=HYPERLINK(...)` member-name test through the FULL render path of
    every new report; FM5 snapshot interleaving — each report renders
    from one transaction / explicit as-of (P13 blocker h), proven by a
    concurrent-settlement test.
(b) Lock order: read-only exports take NO row locks; snapshot
    consistency comes from the transaction, not locks. No new
    lock-graph edges.
(c) Parallel track: indexes-only migration if needed — claim the next
    free number up front (v1.2 rule 14); no new tables expected, so no
    TENANT_TABLES / ENTITY_MODULES delta; no `.gitlab-ci.yml` edits
    (EXPLAIN artifacts ride the existing convention). SASRA return
    skeleton is VERSIONED per return format in code-owned mappings —
    never caller-supplied line-item mappings (v1.1 rules 1/6).
(d) v1.1 restated: all P13 blockers (a)–(l) inherited verbatim as merge
    blockers; explicit tenant predicates on every read; keyset only;
    export audit rows; idempotency by side-effect counts.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16.
EXIT: per-report oracle tests green; FM1–FM5 falsifiable tests green
(each fails with its guard removed); truncation/audit/idempotency
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
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15): FM1 last-activity gaming —
    "member-initiated" is a code-owned allow-list of transaction types;
    system postings (INT-/DV- accruals, penalty bookkeeping) do NOT
    reset the clock: a member whose only in-window activity is an INT-
    posting goes dormant (hand-computed date oracle), falsifiable by
    widening the allow-list; FM2 dormant money movement — a dormant
    member's borrow/pledge/withdraw attempts are refused by the SINGLE
    transition-function gatekeeper; test fails if any route bypasses
    it; FM3 reactivation race — a deposit concurrent with the dormancy
    batch serialises on the member row lock: exactly one final status,
    proven by side-effect counts (one audit row, one transition), never
    Active-overwritten-to-Dormant; FM4 job re-run — lock-free no-op via
    anti-join on status + ledger-derived last-activity (v1.1 rule 8),
    `scanned == 0` asserted.
(b) Lock order (verbatim): the batch locks member rows FOR UPDATE SKIP
    LOCKED in id order — the ROOT tier of the established chain
    member → accounts → loans (the !30 distribution precedent);
    reactivation already holds member → deposit account in chain order
    inside the deposit transaction. No new lock-graph edges.
(c) Parallel track: ONE expand-only CHECK migration (add `dormant` to
    the member-status CHECK) — claim the next free number up front per
    v1.2 rule 14 (0020 is !30's) and state it in the MR description.
    Downgrade must REFUSE LOUDLY on a DB holding dormant members (the
    0017/0020 refusal precedent) — member state is never silently
    rewritten. No new tables → no TENANT_TABLES delta; audit uses
    `entity="members"` (already in ENTITY_MODULES — verify, don't add);
    no `.gitlab-ci.yml` edits.
(d) v1.1 restated: dormancy period exclusively from P13.7 config (rule
    1); last-activity ledger-derived, never a mutable column (rule 2);
    explicit tenant predicates on the scan and every status write (rule
    4); batch runner + anti-join (rule 8).
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; kill-switch
    mid-batch test proves zero partial transitions (§4).
EXIT: full-matrix transition tests updated; FM1–FM4 falsifiable tests
green (each fails with its guard removed); dormancy job idempotence by
side-effect counts; reactivation-on-deposit test; exit-of-dormant test;
migrate-check up→down→up green incl. the loud-refusal downgrade path.

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
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15), hand-computed oracles each: FM1
    component drift — the adjustment restores loans.balance,
    penalty_due, schedule paid_amounts, and the ledger position to the
    hand-computed pre-repayment figures COMPONENT BY COMPONENT; FM2
    double adjustment — a second adjustment of the same repayment is
    blocked by an atomic claim (`INSERT … ON CONFLICT DO NOTHING` +
    rowcount, v1.1 rule 5), proven by side-effect counts; FM3
    adjust-vs-repay race — an adjustment concurrent with a new
    repayment serialises on the loan row lock; the interleaved outcome
    reconciles to the cent; FM4 unauthorised write-off — written_off is
    reachable ONLY through P9 quorum voting bound to a persisted,
    DB-level WRITE-ONCE snapshot of the write-off figures (the !30
    0020-trigger precedent; v1.1 rule 3); test fails with the quorum or
    the write-once trigger removed; FM5 caller-supplied fee — fee
    amounts come exclusively from P13.7 config; a fee amount in the
    request body is 422 (`extra="forbid"`, v1.1 rule 1); FM6 silent
    reopen — a closed loan re-opens ONLY via the explicit documented
    transition branch; full-matrix transition test updated; FM7 partial
    correction — kill-switch mid-adjustment: zero postings, zero
    correction rows, zero balance/schedule drift; FM8 conservation —
    after any correction, DR/CR still balance (0014 trigger) and
    loans.balance reconstructs from the append-only ledger.
(b) Lock order (verbatim): corrections lock the LOAN row — the terminal
    node of the established chain member → accounts → loans (the
    P10/P13.8 pattern); any account write in the same transaction takes
    member → account FIRST, preserving chain order. No new lock-graph
    edges is the default; if one is unavoidable, justify it against
    both established chains in the MR before coding (§5.9).
(c) Parallel track: claim the migration number up front (v1.2 rule 14).
    New correction/claim tables are additive with RLS enabled AND
    forced per ADR-0002; extend TENANT_TABLES and the leakage suite;
    new audited entity strings must be added to ENTITY_MODULES (a named
    shared collision surface — coordinate if another track touches it).
    Downgrades that would drop correction/write-off money history
    REFUSE LOUDLY (the 0017/0020 precedent).
(d) v1.1 restated: append-only ledger — corrections are reversing
    entries, never UPDATE/DELETE (1.5, the Codex-review reversal-block
    precedent this prompt exists to satisfy); FE- refs via the P7
    advisory-lock generator; explicit tenant predicates on reads AND
    writes; least-disclosure errors with exact figures in audit rows;
    Idempotency-Key on every mutation, replay proven by side-effect
    counts.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16.
EXIT: adjustment restores hand-computed pre-repayment state
component-by-component; corrected-then-re-adjusted double-run blocked
by claim; write-off reachable only through quorum bound to a write-once
snapshot; FM1–FM8 falsifiable tests green (each fails with its guard
removed); kill-switch tests green; migrate-check green incl. the
loud-refusal downgrade.

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
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15): FM1 open-on-performing — a case
    can be opened only for an NPL-classified loan, checked under the
    loan row lock; falsifiable (guard removed → test fails); FM2
    duplicate case — at most one open case per loan, enforced by a
    partial UNIQUE claimed atomically (v1.1 rule 5), concurrent
    double-open lands exactly one; FM3 close-on-cure exactly-once — the
    arrears job auto-closes on cure idempotently (re-run closes
    nothing new, side-effect counts); FM4 assignment to a
    suspended/foreign user refused (P13.5 status + tenant checks); FM5
    cross-tenant probe — issue-#17 pattern on every new route.
(b) Lock order (verbatim): the NPL check locks the loan row — terminal
    node of member → accounts → loans; case mutations lock the case row
    only. No new lock-graph edges.
(c) Parallel track: claim the migration number up front (v1.2 rule 14);
    `recovery_cases` additive with RLS enabled AND forced (ADR-0002),
    added to TENANT_TABLES + leakage suite; the new audited entity
    string added to ENTITY_MODULES (named collision surface —
    coordinate); no `.gitlab-ci.yml` edits.
(d) v1.1 restated: explicit tenant predicates on reads AND writes;
    keyset worklist with its index + EXPLAIN via the existing CI
    convention; least-disclosure errors (no balances/dpd figures in
    error envelopes — they live in the audit row); RequirePermission on
    every route, full 7-role matrix test.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16.
EXIT: open-on-performing-loan rejected (falsifiable); FM1–FM5 tests
green, each failing with its guard removed; auto-close-on-cure test;
worklist EXPLAIN captured; matrix tests for the new routes;
migrate-check green.

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
Each item lands as its own commit AND push with its pipeline observed
(v1.2 rule 16) and before/after EXPLAIN or row-count evidence; re-runs
of every new job are lock-free no-ops.
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15), one per item: FM1 (DSA-1)
    snapshot-vs-reconstruction divergence — equality-with-full-scan
    property test over seeded history, to the cent; month snapshots are
    DB-level WRITE-ONCE rows (the !30 0020-trigger precedent) — a
    restated month is unrepresentable; FM2 (DSA-2/5) rollup divergence
    — trial balance and statement opening balances from rollups equal
    the full-scan figures on the same seeded history; FM3 (DSA-3)
    expiry-fence gap — an expired key replays as a NEW request with
    exactly one new effect (side-effect counts), and the replay lookup
    enforces `expires_at > now()` even before the purge runs
    (falsifiable: drop the fence → test fails); FM4 (DSA-4) memory
    regression — incremental PDF rendering proven by dropping the
    `rows` accumulation; export latency test still green; FM5 (DSA-6)
    purge/lease errors — retention purge is idempotent by side-effect
    counts and never touches pending/dead-letter rows; the set-based
    lease UPDATE claims each row exactly once under concurrency.
(b) Lock order (verbatim): snapshot/rollup writers run at close_period
    under its existing per-tenant advisory lock; purge and backfill
    jobs go through the shared batch runner with FOR UPDATE SKIP
    LOCKED; outbox dispatch continues to hold NO domain row locks. No
    new lock-graph edges against member → accounts → loans or the
    pledge chain.
(c) Parallel track: EVERY migration here is additive; claim numbers up
    front, one per item where separable (v1.2 rule 14); new tables get
    RLS enabled AND forced, TENANT_TABLES + leakage-suite entries;
    downgrades dropping snapshot/rollup history refuse loudly if the
    data is money-bearing (0017/0020 precedent); no `.gitlab-ci.yml`
    edits (EXPLAIN artifacts ride the existing convention).
(d) v1.1 restated: NPL_TREND_MONTH_SQL stays the single source of truth
    for snapshot writing (1.1 — no dual-maintained math); retention
    values from server config, never caller-supplied (rule 1);
    ON CONFLICT claims for every snapshot/rollup/backfill write (rule
    5); explicit tenant predicates everywhere (rule 4).
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16. No
    observable money semantics change — every re-computation is
    cross-checked against the existing reconstruction as oracle.
EXIT: equivalence oracles green (snapshot vs full-scan to the cent);
FM1–FM5 falsifiable tests green (each fails with its guard removed);
purge jobs idempotent by side-effect counts; export latency test still
green; migrate-check up→down→up green for every new migration incl.
loud-refusal paths.

---

## PHASE B2 — ARCHITECTURE & THREAT-MODEL DIAGRAMS (P-DIAG series)

Placement rationale (v1.2): a dedicated series after P13.17 rather than
fragments woven into feature prompts, because (1) the diagrams document
the system AS IT EXISTS on main — their dependency is the merged
backend, not Phase C/D; (2) a dedicated series adds no renumbering risk
to P0–P24; (3) it is docs-only and therefore a perfect parallel track
(no migrations, no TENANT_TABLES/ENTITY_MODULES/.gitlab-ci.yml backend
collisions); (4) the drift rule (v1.2 rule 11) needs ONE authoritative
home for each diagram, not per-prompt copies. P-DIAG.1–.5 may run in
parallel with each other and with Phase C prompts once P-DIAG.0 lands.

Common rules for every P-DIAG prompt:
- Diagrams-as-code, Mermaid preferred, checked into `docs/diagrams/`
  (one file per diagram, kebab-case names, a header comment citing the
  main SHA they were authored against).
- Diagrams depict main AS-BUILT at the authoring commit. Not-yet-built
  flows may appear ONLY with an explicit `PLANNED (Pn)` label and must
  be flipped to as-built in the executing prompt's MR (rule 11).
- CI validation where feasible: add a `docs:diagrams` job rendering all
  `.mmd` files with mermaid-cli; if the runner's npm proxy blocks the
  toolchain, record the gap honestly in the MR (v1.2 rules 13/16
  spirit) and gate on mermaid syntax review instead — never fake a
  render check.
- Each MR: commit + push per diagram batch; reference every new diagram
  from `docs/MASTER_PROMPT.md` §2 and the repo README; honest DoD.
- Never invent structure: every box/edge must be traceable to a module,
  table, lock, or route on main; cite the source file in a comment.

### P-DIAG.0 — Diagram infrastructure & authoritative lock-order DAG
ROLE: Solutions Architect. DEPENDS: P13 (backend shape settled).
PROMPT: Create `docs/diagrams/` with the conventions above, the CI
render job (or its honestly-recorded fallback), and the FIRST and most
load-bearing diagram: the GLOBAL LOCK-ORDERING DAG — the single
authoritative statement of the chains every MR since P7 has re-stated
verbatim: member → deposit account → share account → loans (the P12
settlement chain, with the P10/P13.8 loan-terminal-node pattern and the
!30 batch rule: batch scans lock the root tier FOR UPDATE SKIP LOCKED
in id order; two-member operations lock member rows in global member-id
order); application/loan row → guarantor member FOR SHARE → guarantor
deposit account FOR UPDATE (the P9/!29 pledge chain, with the !29
justification of why the two chains cannot cycle); the per-tenant
advisory-lock tier (reference generation, close_period) and the
outbox-holds-no-domain-locks rule as annotated nodes. Every edge cites
the code that takes it. Future prompts REFERENCE this diagram instead
of restating chains; restatements in MR descriptions must match it
verbatim or update it in the same MR (rule 11).
EXIT: diagram renders (or fallback recorded); every edge carries a code
citation valid at the authoring SHA; MASTER_PROMPT §2 and README link
it; standing-rule 11 text updated to name this file as the authority.

### P-DIAG.1 — C4 levels 1–3 (as-built)
ROLE: Solutions Architect. DEPENDS: P-DIAG.0.
PROMPT: C4 context (L1: staff users, the single deployed FastAPI
backend, Postgres 16 with forced RLS, Redis, the outbox worker, and the
NOT-YET-BUILT clients/providers marked PLANNED), container (L2: api /
application / domain / infrastructure layering with the import-linter
enforced dependency direction, worker processes, migration runner), and
component (L3: one diagram per api router group mapping router →
application service → domain module → infrastructure adapter, traceable
to `backend/src/genesis`). As-built on main only — no aspirational
boxes without PLANNED labels.
EXIT: three+ diagrams render; a spot-check script or documented review
confirms every L3 component names a real module at the authoring SHA;
linked from MASTER_PROMPT §2.1/README; drift rule applies.

### P-DIAG.2 — ERD from the alembic graph (through 0020)
ROLE: DBA + Solutions Architect. DEPENDS: P-DIAG.0; !30 merged (0020).
PROMPT: ERD derived from the alembic migration graph 0001–0020 (all
tables, PK/FK edges, the tenant_id column on every tenant-owned table),
with the RLS boundary drawn explicitly: forced-RLS tables vs the
few non-tenant tables, TENANT_TABLES membership annotated, write-once
tables (dividend declaration snapshots, penalty/interest accrual
claims) and append-only tables (ledger_entries, audit_log) visually
distinguished, and every UNIQUE claim key used for idempotency marked.
Derive from the migrations (generation script welcome, checked in),
then hand-annotate; document the regeneration procedure so 0021+ MRs
can update it (rule 11).
EXIT: ERD renders and names every table present at alembic head 0020;
tenant/RLS boundary and append-only/write-once annotations present;
regeneration procedure documented; linked from MASTER_PROMPT §2.2.

### P-DIAG.3 — Data Flow Diagrams L0 → L3 with trust boundaries
ROLE: Security Analyst + Solutions Architect. DEPENDS: P-DIAG.0.
PROMPT: DFDs as code: L0 — system context (external entities: staff,
members (PLANNED until member auth), M-Pesa (PLANNED P19), SMS/email
providers (PLANNED P20)); L1 — major subsystems: auth/RBAC, members,
lending, ledger, guarantees, dividends, exports, outbox/notifications,
settings; L2 — one diagram PER MONEY-MOVER: deposits/withdrawals/share
top-ups, disbursement, repayment allocation, deposit-interest accrual,
penalty accrual, dividend distribution, exit settlement, share
transfer, corrections/fees/write-off (PLANNED P13.15), M-Pesa STK +
callback (PLANNED P19); L3 — the highest-risk flows in full detail:
disbursement, repayment allocation, exit settlement, dividend
distribution, guarantee substitution swap, M-Pesa callback — every
store (table) named, every lock taken annotated on the edge that takes
it (cross-referencing the P-DIAG.0 DAG), every idempotency claim and
outbox write shown. Trust boundaries drawn on every level: tenant/RLS,
authn (JWT staff session), staff-vs-member actor, external providers,
and the request-process vs worker-process boundary.
EXIT: L0 + 9 L1 subsystems + all L2 money-movers + the 6 named L3
flows render; every L3 store/lock annotation matches code at the
authoring SHA (spot-check documented); PLANNED labels only where the
flow is unbuilt; linked from MASTER_PROMPT/README; drift rule applies.

### P-DIAG.4 — STRIDE threat model per DFD element
ROLE: Security Analyst. DEPENDS: P-DIAG.3.
PROMPT: For every element (process, store, edge, boundary crossing) of
the L2/L3 DFDs, a STRIDE table in `docs/diagrams/stride/` (markdown,
one file per L2/L3 diagram): threat → affected element → existing
mitigation → THE NAMED FAILURE-MODE TEST THAT COVERS IT (file + test
name, the !28/!29/!30 tables are the source) or, where no test exists,
an OPEN BLOCKER ISSUE created per the standing rule and linked. No
threat may map to "mitigated" without a falsifiable test or an issue —
that is the anti-reward-hacking rule applied to threat modelling. The
!29 F3/F4 accepted risks (interim email identity) and rule-13 security
-template gap MUST appear with their issue/prompt references.
EXIT: one STRIDE table per L2/L3 diagram; 100% of threats mapped to a
cited test or an open issue (spot-check documented); P23 references
these tables as its DAST triage map.

### P-DIAG.5 — Sequence diagrams for the reusable patterns
ROLE: Developer + Solutions Architect. DEPENDS: P-DIAG.0.
PROMPT: Mermaid sequence diagrams for the three patterns every MR
re-explains in prose: (1) committee/voting — vote cast under the row
lock, quorum read at vote time (P13.7 consumer convention), decision
produced only by a vote event, one-vote UNIQUE; (2)
snapshot-bind-reverify — persist snapshot → committee approval binds to
it → execution re-verifies component-by-component under the full lock
set → 409 on drift posting nothing (P12/!30 pattern), incl. the
DB-level write-once trigger; (3) outbox dispatch — same-transaction
event write, worker claim via partial index + FOR UPDATE SKIP LOCKED +
set-based lease, backoff/dead-letter, dispatch holding no domain locks.
Each participant/message cites the implementing function.
EXIT: three diagrams render with code citations valid at the authoring
SHA; linked from MASTER_PROMPT §1.4/§1.2; future prompts reference them
instead of re-describing the patterns; drift rule applies.

---

## PHASE C — CLIENTS

### P14 — Web admin scaffold (issue #8)
ROLE: Developer (Frontend). DEPENDS: P4.
PROMPT: Scaffold `web/` per MASTER_PROMPT §2.3: Next.js + TS strict,
zero-warning eslint, design-system package with tokens extracted verbatim
from prototype CSS variables, OpenAPI-generated client (regeneration script
in CI drift-check), TanStack Query + Zod, auth/OTP flow, route guards from
`/me/permissions`, keyset-pagination table component.
HARDENED (v1.2) — merge blockers:
(a) Reconcile with the OPEN scaffold MR !13 (branch
    `duo/feature/8-web-admin-scaffold`) and the closed throwaway !12:
    review !13, then rebase/supersede or explicitly close it with a
    stated reason — a second parallel scaffold is a rejected outcome
    (1.1 reuse-first applies to in-flight work too).
(b) This prompt closes NO backend risk by itself: the !29 F3/F4
    accepted risk (interim tenant-scoped users.email ↔ members.email
    guarantor identity, and guarantor self-release being impossible for
    roles without an applications grant) is closed by **P14.5** — a
    backend prompt this scaffold explicitly depends on being scheduled;
    reference P14.5 and the !29 findings table in the MR.
(c) Failure modes (v1.2 rule 15, client flavour): FM1 client drift —
    the CI drift-check fails on a stale generated client (falsifiable:
    regenerate against a modified spec); FM2 authz leak — route guards
    mirror `/me/permissions` but every screen still handles API 403/404
    (UI hides, API enforces — 1.6); FM3 PII leak — no PII in client
    analytics/logs/URLs, asserted by a lint/grep gate; FM4 idempotency
    — the mutation helper attaches `Idempotency-Key` on every POST/PUT
    and surfaces 409 stale-version conflicts as inline banners, never
    silent retries.
(d) Parallel track: `web/` tree only — NO backend/ edits, NO migration
    (state "ships NO migration" per v1.2 rule 14), no TENANT_TABLES /
    ENTITY_MODULES delta; `.gitlab-ci.yml` gains only `web:*` jobs (a
    named collision surface — coordinate; never touch backend job
    definitions). npm dependencies resolve only through the CI proxy —
    same honesty rule as PyPI (v1.2 rule 16) if the proxy blocks.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; update
    P-DIAG C4/L1 (mark the web container as-built) in the same MR per
    rule 11 once the diagrams exist.
EXIT: `web:*` CI jobs green; client-drift check fails on stale client
(falsifiability demonstrated); !13 reconciled or closed with reason;
FM1–FM4 gates in place; P14.5 scheduled and referenced.

### P14.5 — Member identity & member-facing auth (backend)
ROLE: Developer + Security Analyst. DEPENDS: P3, P4, P8; before P17
(member app) and before closing !29's F3/F4 accepted risks.
PROMPT: Introduce a first-class MEMBER principal so member-facing
actions stop borrowing staff identity: an explicit, audited
member↔credential link table (additive migration — claim the number up
front, v1.2 rule 14; RLS enabled AND forced per ADR-0002; TENANT_TABLES
+ leakage suite extended), member OTP login reusing the P3 machinery
(same TTL/attempt/constant-time rules, separate token audience/claims
so a member token can NEVER satisfy a staff RequirePermission gate —
deny by default, falsifiable test), and migration of the !29 interim
email-match in `_actor_is_guarantor` to the explicit link (closing !29
F3/F4: guarantor self-release and consent no longer require a staff
role or an email coincidence). Guarantor CONSENT becomes an act of the
member principal per the P9 consent contract — a staff-asserted or
caller-asserted consent flag on behalf of a member is a rejected design
(the !29 substitution-consent review lesson); substitution consent is
collected from the substitute guarantor's principal (or recorded as an
explicit staff-attested override with its own audit category and
permission, documented). Idempotency-Key scoping (the !29 review
lesson): keys are scoped (tenant, actor principal, route) so one
actor's replay can never fetch another actor's stored response — add
the regression test.
HARDENED (v1.2): named failure modes each falsifiable — FM1 member
token on staff route → 403 (and vice versa); FM2 identity spoof via
email rewrite now IMPOSSIBLE (the !29 attack: rewriting users/members
email no longer redirects the link — test proves the link, not the
email, is authoritative); FM3 link takeover — re-linking a credential
to another member requires the audited admin mutation, never
self-service; FM4 consent forgery — consent rows carry the member
principal id; a consent written without it fails the DB constraint;
FM5 idempotency cross-actor replay → miss. Lock order: link mutations
lock the member row (chain ROOT) — no new lock-graph edges. v1.1 rules
4/5/6/7 restated: explicit tenant predicates, atomic claims for the
link UNIQUE, bound parameters, least disclosure. Honest DoD per rule
13; process per rule 16; update the P-DIAG.3 actor trust boundary and
P-DIAG.4 STRIDE rows for the retired interim identity in the same MR
(rule 11).
EXIT: FM1–FM5 green and each fails with its guard removed; !29's F3/F4
risk entries updated/closed with a comment linking this MR; migrate-
check green; leakage suite extended; member-auth flows documented for
P17 consumption.

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
HARDENED (v1.2) — merge blockers:
(a) No client-side money math EVER: installment previews, cover %,
    capacities, settlement figures come from the API (1.1); a locally
    computed money figure is a rejected MR — grep gate for arithmetic
    on money fields in `web/`.
(b) Failure modes per module (v1.2 rule 15): the adversarial Playwright
    flow per module must include at least — stale-version edit (409
    banner, no silent overwrite), forbidden-role access (route guard
    AND API 403 handled), double-submit (exactly one effect, verified
    against the API by side-effect, the idempotency key doing the
    work), and least-disclosure rendering (error toasts never echo
    balances/figures the API didn't return).
(c) Module order and evidence: build in the stated order, one MR per
    module batch, commit+push+pipeline per module (v1.2 rule 16) —
    never one giant MR.
(d) Parallel track: `web/` only; no migration ("ships NO migration");
    `.gitlab-ci.yml` edits confined to `web:*`/E2E jobs.
(e) Honest DoD per v1.2 rule 13: E2E evidence is in-project pipeline
    runs, not local screenshots; update P-DIAG diagrams if a flow's
    client interaction changes documented sequences (rule 11).
(f) Phase B primitives — MANDATORY USAGE (binding on every remaining
    module batch; delivered on !56 with falsifiable tests — one copy
    each, gate 1.1; re-implementing any of them is a rejected MR):
    - Shared page grids `@/modules/layout/grid.module.css`
      (`grid.cards4` 4→2→1, `grid.half`/`grid.wide` spans,
      `grid.sideMain` side+main stacking ≤960px; breakpoint convention
      640/960px) — NO private page-grid CSS in module stylesheets
      (`responsive.test.ts` asserts consumers).
    - `KeysetTable` + `useKeysetList` (`@/modules/table/*`): keyset
      `{items, next_cursor}` ONLY (gate 1.3 — no offset pagination),
      keyboard-activatable row drill-down, focusable labelled scroll
      region for narrow viewports; pages are 20 rows with explicit
      Load more (no virtualization at this page size).
    - `FormField` + `form-errors.ts` (`@/modules/forms/*`) for EVERY
      form field: persistent label, aria-describedby/aria-invalid
      wiring, inline errors merging client Zod issues with server 422
      `ApiError.fields` (server verdict wins) — no hand-rolled
      label/error wiring.
    - `ConfirmDangerModal` (`@genesis/design-system`) for every
      destructive/money-adjacent action: typed byte-identical
      confirmation phrase; pending blocks all dismissal paths.
    - `MakerCheckerPanel` (`@/modules/authz/components/…`) for EVERY
      approval flow: checker affordances mount ONLY for a different,
      known principal (`getOwnUserId`); self-approval is structurally
      impossible — there is no override prop; the server enforces
      regardless (gate 1.6).
    - `ConflictBanner` (`@/modules/layout/ConflictBanner`) for every
      409: explicit reload-and-re-enter, never a silent overwrite,
      never an auto-retry; pair with `ErrorBanner` for non-409s.
    - `downloadExport` (`@/lib/file-export`) for every file download:
      Response comes from the GENERATED client; least-disclosure
      failures; transient revoked object URL — no hand-written fetch.
    - `ErrorBanner` + `ApiError.fields` for least-disclosure errors
      ({category, correlation_id} only); `announce()`
      (`@/modules/layout/announcer`) for async success/error AT
      announcements (operator-facing copy only, never raw API data).
    - Focus-trapped `Modal` (drawer/dialog variants, Escape + return
      focus, full-screen ≤640px) — never a bespoke overlay.
    - `idempotencyKeyFor` slot on EVERY mutation (stable across
      identical retries, rotates on content change) + double-submit
      disabled/short-circuited; mutations `retry: 0`.
    - Generated client ONLY; Zod on every response/form; deny-by-
      default `RequireModule`/`can()`; tokens-only styling; money as
      API decimal STRINGS (no client math/balances — blocker (a));
      TanStack Query keys/staleTime per `@/lib/query` entity classes.
    - Per-module adversarial jest suite mirroring
      `users-screen.test.tsx` (XSS inertness, 409 single-attempt +
      reload-never-replays, double-submit single-effect, permission-
      stripped affordances, least-disclosure, 401 teardown) — the
      Playwright E2E per module remains the P15 exit criterion on top.
(g) Process / anti-corruption (v1.2 rule 16, binding per module
    session): commit+push per file-scale unit (never >15 min of work
    unpushed); after ANY tool-assisted edit re-read the ENTIRE file
    (corruption hides in tails, escape layers, dropped hunks); after
    each commit re-fetch from remote and diff hunk-by-hunk against
    intent — the REMOTE is the truth, not the sandbox working copy;
    pair every new symbol with its import in the same edit; npm/PyPI
    are proxy-blocked in the sandbox — CI is the arbiter; red-pipeline
    fixes before any other work.
EXIT: all ten modules E2E-green in-project incl. the per-module
adversarial set; no-client-money-math gate active; Lighthouse perf
budget documented.

### P16 — Flutter workspace (issue #9)
ROLE: Developer (Mobile). DEPENDS: P4.
PROMPT: Scaffold `mobile/` per MASTER_PROMPT §2.4: `member_app`,
`admin_app`, shared `gp_ui` (prototype palette tokens) and generated
`gp_api_client`; Riverpod; secure token storage; certificate pinning;
biometric step-up; offline read cache. `mobile:*` CI jobs green.
HARDENED (v1.2) — merge blockers:
(a) Reconcile with the OPEN draft !11 (branch
    `duo/feature/9-flutter-workspace-scaffold`) — rebase/supersede or
    close it with a stated reason; no second parallel scaffold (1.1).
(b) Issue #11 (staging API unavailable for boot verification) governs
    the EXIT honestly: if staging still does not exist, the
    boot-against-staging criterion is recorded UNVERIFIED with the
    issue link (v1.2 rule 13 spirit) — never faked against a mock and
    ticked.
(c) Failure modes (v1.2 rule 15): FM1 token leak — secure storage only,
    no tokens in logs/crash reports (lint gate); FM2 pin bypass —
    certificate-pinning failure is a hard connection error, tested;
    FM3 client drift — generated `gp_api_client` drift-check in CI,
    falsifiable; FM4 member/staff principal separation (P14.5) — the
    member app never requests staff scopes.
(d) Parallel track: `mobile/` only; ships NO migration; `.gitlab-ci.yml`
    gains only `mobile:*` jobs (named collision surface); pub/npm
    proxy caveats recorded honestly per v1.2 rule 16.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; update
    P-DIAG C4 L1 containers in the same MR (rule 11).
EXIT: `mobile:*` CI jobs green; both apps boot to authenticated shell
against staging API — or the blocker recorded per issue #11, honestly
unticked; !11 reconciled or closed with reason.

### P17 — Member app features
ROLE: Developer (Mobile) + QE. DEPENDS: P16, P14.5 (member principal —
hard dependency), P8–P13, P19 for payments.
PROMPT: Build: onboarding + OTP; balances (shares/deposits/loan);
statements (cursor-paginated, offline-cached); deposit via M-Pesa STK with
pending-intent status polling; loan application with product rules and
live installment preview (values from API, never local math — 1.1);
guarantor consent inbox; repayments; notifications. integration_test per
flow including airplane-mode statement read and double-tap submit
(exactly-one-effect).
HARDENED (v1.2) — merge blockers:
(a) Every member action authenticates as the P14.5 MEMBER principal —
    never a staff token, never the retired email-identity link; the
    guarantor consent inbox records consent AS the member principal
    (the P9 consent contract; the !29 caller-asserted-consent lesson).
(b) Failure modes (v1.2 rule 15): FM1 double-tap submit — exactly one
    effect proven by API side-effect counts (idempotency keys scoped
    per member principal, the !29 scoping lesson); FM2 offline staleness
    — cached statements are labelled with their as-of moment, never
    presented as live; FM3 cross-member leak — a member sees only their
    own balances/statements (server-enforced; probe test); FM4 payment
    intent confusion — STK polling binds to the intent id, an
    out-of-order status can never mark a different intent paid (pairs
    with the P19 adversarial set).
(c) No local money math (1.1): previews/quotes only from the API; grep
    gate as in P15.
(d) Parallel track: `mobile/` only; ships NO migration; honest DoD per
    v1.2 rule 13; process per v1.2 rule 16; update the P-DIAG.3 member
    trust boundary if flows change (rule 11).
EXIT: all flows integration-test green on Android + iOS CI matrix incl.
FM1–FM4; consent flows verified against the member principal.

### P18 — Admin mobile app features
ROLE: Developer (Mobile). DEPENDS: P16, P8–P12.
PROMPT: Build the field-officer subset: member lookup + onboarding,
application capture, committee vote (biometric step-up), arrears worklist,
txn capture. Same gates as P17.
HARDENED (v1.2) — merge blockers:
(a) Committee votes require biometric step-up AND land through the
    same server-side voting machinery (P9/P13.7 quorum-at-vote-time) —
    the device never caches or batches votes; a vote is one authorised
    API call with an idempotency key (double-tap → one vote, the
    UNIQUE one-vote guard proven from the client path).
(b) Failure modes (v1.2 rule 15): FM1 role spoof — on-device RBAC is
    cosmetic; every flow re-verified against API 403 per role (matrix
    test from the client); FM2 offline capture replay — queued
    onboarding/txn captures submit exactly once (idempotency keys
    survive app restarts); FM3 PII at rest — captured KYC data is
    encrypted at rest on-device and purged after successful submission.
(c) Arrears worklist consumes the P13.16 endpoint (1.1) — no
    client-side reconstruction of dpd.
(d) Parallel track: `mobile/` only; ships NO migration; honest DoD per
    v1.2 rule 13; process per v1.2 rule 16.
EXIT: flows integration-test green incl. FM1–FM3; RBAC verified per
role on-device AND against the API matrix.

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
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15), the full adversarial set, each
    falsifiable with hand-computed oracles: FM1 replayed callback —
    identical callback delivered N times → exactly ONE posting, ONE
    balance change, ONE outbox event (side-effect counts; the intent
    claim is `INSERT … ON CONFLICT DO NOTHING` + rowcount, v1.1 rule
    5 — never SELECT-then-INSERT); FM2 forged callback — bad
    source/signature/credentials → rejected, ZERO mutations, rate
    limited, audited as a security event; FM3 out-of-order — a result
    callback racing/preceding the timeout handler converges to ONE
    consistent terminal intent state, never a paid-then-expired flip;
    FM4 timeout-then-success — late success after local expiry lands in
    a held/reconciliation state, never silently posted or silently
    dropped; FM5 amount mismatch — callback amount ≠ intent amount →
    REFUSED LOUDLY into the reconciliation queue, never posted, never
    "adjusted"; FM6 cross-intent confusion — a callback can only ever
    settle the intent it validates against (bound by intent id +
    tenant), probe-tested; FM7 kill-switch — abort between posting and
    intent update → zero partial state (§4).
(b) RECONCILIATION NEVER MUTATES: the daily job compares provider
    records to intents/ledger and ALERTS; discrepancies are resolved by
    humans through the P13.15 correction paths (append-only reversing
    entries) — any auto-mutation path in the reconciliation job is a
    rejected MR, falsifiable by a test proving the job writes no ledger
    rows.
(c) Lock order (verbatim): the callback posting path follows the
    established chain member → deposit account (FOR UPDATE) with the
    posting via P7 — no new lock-graph edges; NO blocking provider I/O
    while holding row locks (lock → compute → write → commit → outbox,
    1.3) — STK initiation talks to the provider BEFORE opening the
    intent-claiming transaction or via the outbox.
(d) Parallel track: claim the migration number up front (v1.2 rule 14);
    `payment_intents` (and any callback-dedup table) additive with RLS
    enabled AND forced, TENANT_TABLES + leakage suite + ENTITY_MODULES
    entries; secrets via CI/CD variables only — a literal credential
    fails secret detection.
(e) v1.1 restated: no caller-supplied amounts on the posting path
    beyond the validated intent (rule 1); explicit tenant predicates on
    reads AND writes (rule 4); bound parameters (rule 6); least
    disclosure — callback responses and errors never echo balances
    (rule 7); Idempotency-Key scoped per actor+route on the
    member-facing initiation (the !29 scoping lesson).
(f) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; update
    P-DIAG.3's M-Pesa L3 DFD from PLANNED to as-built and its
    P-DIAG.4 STRIDE table in the same MR (rule 11).
EXIT: sandbox end-to-end deposit reflected in ledger and member
statement; FM1–FM7 green, each failing with its guard removed;
reconciliation-writes-nothing test green; threat model + ADR signed
off; diagrams updated.

### P20 — Notifications
ROLE: Developer. DEPENDS: P5, P8–P12.
PROMPT: Wire real SMS/email/push providers behind the P5 adapters with
per-tenant templates and per-channel circuit breakers; delivery-status
writeback; member notification preferences. All sends outbox-only (1.2);
no PII beyond the minimum in payloads (1.6).
HARDENED (v1.2) — merge blockers:
(a) Named failure modes (v1.2 rule 15): FM1 provider outage — domain
    actions still succeed (the outbox IS the decoupling); events retry
    with backoff then dead-letter; replay after recovery delivers
    exactly once BY EVENT ID (side-effect counts at the adapter,
    idempotent per P5); FM2 duplicate dispatch — two workers claiming
    the same event → one send (the SKIP LOCKED + lease pattern,
    P-DIAG.5 diagram 3); FM3 template PII overreach — templates render
    from an allow-listed payload contract; a template referencing a
    non-allow-listed field fails CI; FM4 preference bypass — an opted-
    out member receives nothing (checked at dispatch, tested); FM5
    circuit-breaker flap — a tripped channel fails fast without
    consuming retries of other channels.
(b) Lock order (verbatim): outbox dispatch holds NO domain row locks
    (1.4) — restated as the binding rule; delivery-status writeback is
    its own short transaction on the outbox row only. No new lock-graph
    edges.
(c) Parallel track: claim any migration number up front (preferences
    table additive, RLS forced, TENANT_TABLES + leakage suite +
    ENTITY_MODULES); provider secrets via CI/CD variables only.
(d) v1.1 restated: least disclosure in payloads — an SMS never carries
    balances beyond the documented minimum; exact figures live in the
    audit row (rule 7); explicit tenant predicates (rule 4).
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; update the
    P-DIAG.3 provider trust boundary to as-built (rule 11).
EXIT: provider-outage chaos test: actions succeed, events dead-letter
and replay cleanly exactly once; FM1–FM5 green, each failing with its
guard removed.

### P21 — Observability & performance
ROLE: QE + Developer. DEPENDS: Phase B complete.
PROMPT: Add OpenTelemetry traces/metrics/logs (PII-scrubbed), dashboards
(p95 latency, error rate by category, outbox lag, job durations), alerts.
Load tests (k6): 10k-member tenant, 50 concurrent tellers; verify keyset
pagination flatness and zero lock-wait timeouts; capture EXPLAIN for the
top 20 queries into `docs/perf/`.
HARDENED (v1.2) — merge blockers:
(a) EXPLAIN artifact convention (the established one, cited): plans are
    captured IN CI by `tests/test_*_explain.py` against the migrated
    Postgres service into `backend/perf/explain_*.txt` artifacts, with
    `enable_seqscan=off` plan-shape-at-scale assertions falsifiable by
    dropping the backing index (the P10–P13.8 precedent). The top-20
    capture into `docs/perf/` DERIVES from those artifacts + load-test
    plans — no hand-pasted, unreproducible plans.
(b) Named failure modes (v1.2 rule 15): FM1 perf-gate reward hacking —
    budgets are enforced by failing CI jobs, no `allow_failure`, no
    quietly raised thresholds (a threshold change is its own reviewed
    commit with rationale); FM2 telemetry PII leak — a scrubber test
    feeds PII through every telemetry path and asserts redaction; FM3
    telemetry breaking the action — collector outage must not fail
    domain requests (fire-and-forget + breaker, 1.2, chaos-tested);
    FM4 pagination flatness — keyset page latency at depth 10 ≈ depth
    10k (the DSA-7 lesson: filtered listings need their serving index);
    FM5 DSA validation — the load tests exercise the P13.17 snapshot/
    rollup paths and prove the NPL-trend/trial-balance exports hold
    budget at 10k members (the DSA-1/2 rationale, now measured).
(c) Lock order: observability adds NO locks and NO new lock-graph
    edges; lock-wait timeout metrics are labelled by the P-DIAG.0 DAG
    edge names so a violation names its edge.
(d) Parallel track: ships NO domain migration; `.gitlab-ci.yml` gains
    only perf/observability jobs (named collision surface —
    coordinate).
(e) Honest DoD per v1.2 rule 13: budget numbers come from in-project
    pipeline artifacts; process per v1.2 rule 16.
EXIT: p95 < 300ms on hot reads, < 800ms on posting writes at target
load, enforced by a failing gate (falsifiability demonstrated); FM1–FM5
green; alerts fire in a game-day drill; top-20 EXPLAIN artifacts
reproducible from CI.

### P22 — Deployment & environments
ROLE: CI/CD Engineer. DEPENDS: P21.
PROMPT: Extend the pipeline with deploy-review (per-MR review apps +
DAST), deploy-staging (auto on main), deploy-prod (manual, protected
environment). Container scanning on built images; SBOM artifact. Managed
Postgres with PITR backups; documented + rehearsed restore (DR drill);
zero-downtime migration policy (expand→migrate→contract) enforced by MR
template checkbox. Store demo/live app distribution (Flutter) via CI.
HARDENED (v1.2) — merge blockers:
(a) FIX THE SECURITY-TEMPLATE NON-SPAWN (v1.2 rule 13's standing debt,
    recorded honestly on !26/!28/!29): diagnose why the included SAST /
    Secret-Detection / Dependency-Scanning template jobs do not spawn
    on MR pipelines, fix the template `rules:`, and prove the jobs run
    AND block on findings in this project's pipelines. Until this lands
    green, the security DoD box stays unchecked project-wide; after it
    lands, rule 13's carve-out is retired (update the standing rule in
    the same MR).
(b) Zero-downtime expand→migrate→contract, citing the proven refusal
    precedents: contract steps that would drop or rewrite money-bearing
    history must REFUSE LOUDLY exactly like the 0017 re-chain
    discipline and !30's 0020 downgrade (the transactions.type CHECK
    restore refusing on dividend history) — rehearse BOTH the clean
    contract and the loud refusal in migrate-check; the MR-template
    checkbox references those precedents by number.
(c) Named failure modes (v1.2 rule 15): FM1 restore drift — the DR
    drill restores to a point-in-time and PROVES ledger balance/audit
    integrity post-restore (trial balance equality oracle); FM2 deploy
    skew — old app + new schema runs one full release (expand phase
    proven by running the previous release's test suite against the
    migrated schema); FM3 protected-env bypass — prod deploy is
    impossible without the manual gate + protected branch (tested by a
    deliberate attempt); FM4 supply chain — images pinned by digest,
    SBOM published, container scanning blocks criticals.
(d) Parallel track: this prompt OWNS `.gitlab-ci.yml` restructuring —
    schedule it when no feature track has CI edits in flight (v1.2
    rule 12 merge-sequencing); migration-claim registry (rule 14)
    becomes part of the deploy runbook.
(e) Honest DoD per v1.2 rule 13; process per v1.2 rule 16; update
    P-DIAG.1 C4 L1/L2 with the deployment topology (rule 11).
EXIT: full promote path exercised; restore drill documented under 30
min with the integrity oracle green; security-template jobs spawn and
block (rule 13 carve-out retired); FM1–FM4 green; refusal rehearsal
recorded in migrate-check.

### P23 — Security hardening & tenant onboarding
ROLE: Security Analyst. DEPENDS: P22.
PROMPT: Run full DAST against staging; triage every scanner finding
(critical=block). Verify: RLS leakage suite, rate limits, secret scanning
clean, dependency review, audit-log completeness sampling, log PII audit.
Build tenant onboarding runbook: tenant record, admin bootstrap, product
config, M-Pesa credentials via secret manager, RBAC seed. Data protection
(Kenya DPA) checklist: retention, subject access export via P13, breach
playbook.
HARDENED (v1.2) — merge blockers:
(a) DPA-2019 hooks aligned with the MERGED P13.12 KYC surface (0018 —
    this executed AHEAD of expectation, so the alignment lands here):
    subject-access export covers the KYC profile tables and document
    metadata via the P13 export registry with per-role PII allow-lists
    and full export-audit rows (P13 blockers e/f); a retention schedule
    is DEFINED AND ENFORCED for KYC documents, idempotency-key stored
    responses (the DSA-3 expiry work), and dispatched outbox payloads
    (DSA-6 retention) — retention values from server config, purges via
    the shared batch runner, idempotent by side-effect counts; the
    P13.12 consent-flag immutability is re-verified in the audit
    completeness sampling; the breach playbook names the append-only
    audit_log + ledger as the forensic sources.
(b) STRIDE-driven triage: the P-DIAG.4 tables are the DAST triage map —
    every finding is matched to its DFD element; a finding with no
    covering test becomes an open blocker issue per the standing rule
    (no silent accepts).
(c) Named failure modes (v1.2 rule 15): FM1 leakage regression — the
    cross-tenant suite is re-run against the FULL table set at the
    current migration head (cross-checked against TENANT_TABLES — a
    table missing from the suite fails the check); FM2 subject-access
    overreach — the export returns exactly the data subject's records,
    probe-tested across tenants and members; FM3 onboarding drift — the
    runbook is executed verbatim on staging for a pilot tenant; any
    manual deviation is a runbook bug; FM4 secret sprawl — M-Pesa/SMS
    credentials exist only in the secret manager/CI variables,
    grep+scanner proven.
(d) Parallel track: retention/purge migrations claim numbers up front
    (v1.2 rule 14); downgrades never resurrect purged PII.
(e) Honest DoD per v1.2 rule 13 (by now P22 should have retired the
    carve-out — verify, don't assume); process per v1.2 rule 16.
EXIT: zero open critical/high findings; onboarding runbook executed
successfully for a pilot tenant on staging; FM1–FM4 green; DPA
checklist items each mapped to an enforced mechanism, not a document.

### P24 — UAT & launch
ROLE: Product Manager + QE. DEPENDS: P23.
PROMPT: Script UAT against every prototype screen/behavior as acceptance
cases (dashboard figures, classification pills, committee flow, exit
settlement, statements, RBAC per role). Pilot tenant runs 2 weeks on
staging with real workflows; defects triaged daily, fixes follow the full
gate process (no shortcuts). Launch checklist: alerts on, on-call rota,
rollback plan rehearsed, support runbook.
HARDENED (v1.2) — merge blockers:
(a) Acceptance oracles are HAND-COMPUTED (the standing anti-reward-
    hacking rule applied to UAT): dashboard figures, settlement
    quotes, interest/penalty/dividend amounts in the UAT script carry
    their arithmetic — sign-off against unexplained numbers is
    invalid.
(b) Defect fixes during the pilot follow EVERY gate: named failure-mode
    test first (v1.2 rule 15), migration-claim registry (rule 14),
    merge-sequencing/rebase-re-run (rule 12), diagram drift (rule 11 —
    a fix touching a diagrammed flow updates the diagram in the same
    MR), honest DoD (rule 13). "It's just UAT" is not a gate waiver.
(c) Named failure modes: FM1 sign-off theatre — every acceptance case
    links the pipeline evidence or staging record proving it ran; FM2
    week-one error budget — breach triggers the documented rollback,
    rehearsed before go-live, not improvised.
(d) Process per v1.2 rule 16.
EXIT: UAT sign-off with hand-computed oracles attached; production
tenant live; week-one error budget intact; zero gate waivers granted.

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

HARDENED STANDARDS (v1.2 — ADDITIVE to v1.1, which stays in force
unchanged; proven on !26–!30; apply to every prompt from here forward):
11. Diagram drift rule: once the P-DIAG diagrams exist under
    `docs/diagrams/`, any MR that changes a diagrammed flow, table,
    lock-graph edge, or trust boundary MUST update the affected
    diagram(s) in the same MR. A stale diagram is a rejected MR — the
    diagrams are load-bearing review artifacts, not decoration.
    `docs/diagrams/lock-order.md` (P-DIAG.0) is the AUTHORITATIVE
    lock-ordering DAG: lock-order statements in MR descriptions must
    match it verbatim or update it in the same MR; the per-MR default
    is "no new lock-graph edges", and adding one requires updating the
    DAG (edge + code citation + acyclicity note) in the same MR.
12. Merge-sequencing / rebase-re-run rule: parallel-track MRs declare
    their merge order up front in the MR description (the !26/!27
    "merges FIRST" precedent). Before merging, a branch merges current
    `main` and re-runs its pipeline green on the COMBINED state (the
    !29 `a0af60c` precedent — !28/0019 landed mid-session and the
    pipeline was re-observed). Conflicts are resolved with merge
    commits; force-push only via the documented backup-branch +
    `--force-with-lease` rebase procedure, never bare.
13. Security-template honesty rule: the included SAST / Secret-Detection
    / Dependency-Scanning template jobs historically DID NOT SPAWN on
    this project's MR pipelines (recorded on !26, !28, !29). Since the
    `.latest` template variants landed the jobs DO spawn and pass on MR
    pipelines — observed on !64's pipelines 2731230146 / 2731298427 and
    !67's 2731979233, and re-verified IN THIS PROJECT (post-move) on
    !70's final MR pipeline 2734307715 (`semgrep-sast`,
    `secret_detection`, `gemnasium-dependency_scanning`,
    `gemnasium-python-dependency_scanning` — all spawned and passed;
    the pre-move pipeline ids no longer resolve here, so 2734307715 is
    the citable evidence), reconciled here by the issue-#30 close-out
    MR (Hat 6 D7: the standing rule itself had become the stale
    claim). The rule's HONESTY core is unchanged:
    every MR's DoD ticks the security box ONLY against observed
    in-project job evidence at that MR's final HEAD (job ids cited),
    and records it unchecked with the reason when the jobs did not
    spawn on that pipeline. Ticking it without in-project job evidence
    is a rejected MR; silently "fixing" a non-spawn by removing the
    template is worse. P22(a) still owns retiring the historical
    carve-out from the older MRs' DoD records.
14. Migration-claim registry: exactly one in-flight claim per alembic
    number. State the claim (number + `down_revision`) in the MR
    description at branch time, before the first commit. Registry at
    v1.2 authoring: 0001–0019 on main; **0020 is !30's (P13.11)**; the
    next migration-bearing session claims 0021. Prompts that need no
    migration state "ships NO migration" explicitly in the MR (the !29
    precedent). If a reserved number frees up (MR closed) or lands out
    of order, re-chain `down_revision` in your own MR like the 0017
    re-chain in !26 — never renumber another track's claim.
15. Named banking-grade failure modes: every prompt that moves or
    derives money ships a NUMBERED failure-mode table in its MR
    description, one falsifiable test per mode (the test fails with its
    guard removed) with hand-computed oracles in comments — the
    !28/!29/!30 pattern. "Covered by the general suite" is a rejected
    answer; each mode is named, each test is cited.
16. Process rules (every session): commit + push per coherent unit —
    never one end-of-session commit; never force-push (rule 12 governs
    the only exception). PyPI is proxy-blocked in the session
    environment — never attempt local `pip install`; the CI image is
    the only place Python deps resolve, so CI is the arbiter of
    lint/test/migrate results. Format with the exact ruff version the
    CI image resolves (0.16 line at authoring) — an older local ruff
    formats differently and reds `backend:lint`. File-integrity audit
    (upgraded 2026-08-02 from the !26 F7 dropped-hunk and !47 B1
    corrupted-tail incidents): after ANY tool-assisted edit, re-read
    the ENTIRE touched file — never only the edited region (B1 lived
    in the un-inspected tail) — and grep-audit for silently dropped,
    duplicated, or mangled hunks: every section heading greps to
    exactly one occurrence and the file's final line is the intended
    one. After each commit, RE-FETCH every touched file from the
    remote and walk the diff hunk-by-hunk against the commit's own
    intended change list (the diff-of-diffs audit); any unintended
    hunk is fixed in its own immediate commit before further work.
