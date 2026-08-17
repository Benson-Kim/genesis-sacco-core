<!--
  P-DIAG.4 — STRIDE-per-element threat model over the P-DIAG.3 DFDs
  Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
  Status: as-built. Element ids reference dfd.md (P-DIAG.3); lock
  statements cite lock-order.md edge ids only (v1.2 rule 11).
  Honesty rules: every mitigation cites real code/migration on main —
  no invented mitigations; every residual names an owner prompt or is
  listed as UNOWNED (v1.2 rule 13 spirit), never hidden.
  Drift rule: v1.2 rule 11 — an MR that changes a mitigated flow or
  boundary updates the affected rows here in the same MR.
-->

# STRIDE-per-element threat model (P-DIAG.4)

## 0. Method

- **Elements** are the `dfd.md` (P-DIAG.3) element ids; boundary rows
  use the TB1–TB4 ids. Standard STRIDE-per-element applicability:
  external entities S/R; processes S/T/R/I/D/E; stores T/R/I/D; flows
  crossing a boundary T/I/D.
- **Grouping / coverage map.** Rows that share one mitigation
  mechanism group their element ids (e.g. all ledger stores share the
  0004 triggers). Elements not named in a row are covered by a class
  row, explicitly: every router process (`*_P1`) inherits the L0_API
  rows (deny-by-default E; idempotency S/T); every audit store
  (`TXN_S6`, `DSB_S7`, `RPY_S5`, `EXIT_S8`, `DIV_S7`, `INT_S6`,
  `DRM_S4`, `EXP_S4`) inherits the audit_log rows; every outbox store
  (`TXN_S7`, `DSB_S8`, `RPY_S6`, `EXIT_S9`, `DIV_S8`, `DRM_S5`,
  `EXP_S5`, `OBX_S1`) inherits the OBX/TB4 rows; every tenant-owned
  domain store (members/accounts/loans/guarantees/config rows in each
  diagram) inherits the TB2 rows (I/E) plus its flow's T rows.
- **Residual column.** `—` means no residual worth naming at the
  authoring SHA. A PLANNED mitigation names its owner prompt
  (P13.15/P14.5/P19/P20/P22/P23). A real residual nobody owns is
  **UNOWNED**, stated as such.
- **Machine-checking** of these citations (do the cited
  functions/migrations exist at the SHA?) is NOT done by this file —
  it is a named follow-up for the CI/spot-check thread (see the MR
  description). Verification here was by manual read of main at the
  authoring SHA.
- P23 uses this file as its DAST triage map (BUILD_PROMPTS P-DIAG.4
  EXIT).

## 1. Trust boundaries and shared elements

| Element | STRIDE | Threat | As-built mitigation (citation) | Residual risk → owner |
|---|---|---|---|---|
| TB1 edge (L0_STAFF → L0_API) | S | Attacker impersonates a staff principal | OTP step-up: `domain/otp.py:evaluate_challenge` (6 digits, ≤5 attempts, 5-min TTL, single-use, constant-time `hmac.compare_digest`; codes stored only as peppered HMAC `hash_code`) + `application/auth.py:verify_otp`; JWT access ≤15 min (`application/auth.py:issue_access_token`), signature-checked on every request (`decode_access_token` via `api/authz.py:get_auth_context`); refresh rotation with family revocation on reuse (`rotate_refresh_token`) | Signing key is a single static env secret (`application/auth.py:_signing_key`); no rotation mechanism exists and no prompt names one — **UNOWNED** (P22 owns secret *storage*, not rotation) |
| TB1 edge | S | Tenant spoofing via the pre-auth `x-tenant-id` header | Pre-auth endpoints only ever *deliver an OTP to the registered user of that tenant* (`application/auth.py:request_otp` resolves the user by tenant+email; delivery via outbox); knowing a tenant UUID grants nothing without the OTP; all pre-auth routes rate-limited (`api/auth.py:_rate_guard`) | OTP-request flooding as SMS/email cost abuse becomes real only with real providers → **P20** (circuit breakers, per-channel budgets) |
| TB1 edge | T | Tampered bearer token / replayed refresh token | JWT signature verification (`decode_access_token`); refresh tokens stored hashed (`_hash_refresh_token`), rotation + family revocation on reuse (`rotate_refresh_token`; failure committed before the 401, `api/auth.py:refresh` comment) | — |
| TB1 edge | R | "I never logged in / that wasn't me" | Punitive OTP state (attempt counters, consumption) is committed even when the request fails (`api/auth.py:verify_otp` comment); suspension voids challenges and revokes token families in the same txn (`application/users.py:_void_pending_otp_challenges` / `_revoke_refresh_families`); `users.last_active_at` at token issue (P13.5) | No `audit_log` row is written for token issue/login events themselves — session-level repudiation window: **UNOWNED** |
| TB1 edge | I | Error/response oracle leaks balances, capacities, PII | Global sanitized envelope `{category, correlation_id}` only (`api/app.py:app_error_handler`); least-disclosure errors with exact figures in the audit row instead (v1.1 rule 7; e.g. `application/transactions.py:record_withdrawal`); PII scrubbed from logs (`genesis/logging.py:scrub`) | — |
| TB1 edge | D | Credential-stuffing / OTP brute force / auth flooding | `infrastructure/rate_limit.py:check_rate_limit` (fixed window per route+tenant+client) on every auth route (`api/auth.py:_rate_guard`); OTP attempt cap in `domain/otp.py` | Rate-limit falls back to an **in-process** window when Redis is unconfigured (`rate_limit.py` module docstring) — limits don't hold across instances in a multi-instance deploy: **UNOWNED** (deployment topology is P22's area but no prompt names this) |
| L0_API (process) | E | Authenticated user acts beyond their role | Deny-by-default `api/authz.py:RequirePermission` / `RequireAnyPermission` on every business route; CI spec-walk test fails any operation lacking the dependency (P4); suspended/vanished users refused mid-token-lifetime (authz.py Review-F3 comment); self-role-edit and last-admin guards (`application/users.py`, P13.5) | — |
| L0_API (process) | S/T | Forged or replayed mutation (double-submit) | `api/idempotency.py:IdempotencyMiddleware` — atomic `ON CONFLICT` claim + stored-response replay; request models `extra="forbid"` reject unknown/money fields (v1.1 rule 1) | — |
| TB2 (L0_PG, all tenant stores) | I | Cross-tenant read/write | Forced RLS on every tenant-owned table (migration `0001`: `FORCE ROW LEVEL SECURITY` + `tenant_isolation` policies); `SET LOCAL app.tenant_id` per transaction (`infrastructure/tenancy.py:tenant_session`); app role `NOSUPERUSER NOBYPASSRLS` (ADR-0002; CI bootstrap in `.gitlab-ci.yml`); defence in depth: explicit `tenant_id = :tid` predicates on reads AND writes (v1.1 rule 4); release-blocking cross-tenant leakage suite (P2) | — |
| TB2 | E | App DB role escalates past RLS | Role created without `BYPASSRLS`/`SUPERUSER` (ADR-0002, `.gitlab-ci.yml` `backend:test` bootstrap); RLS *forced* so even the table owner path is policy-checked (migration `0001`) | Production role provisioning is infra, rehearsed only in CI as-built → **P22** |
| TB2 | T | SQL injection through values | All values bound parameters, enum values included; identifiers only from code-owned mappings with a stating comment (v1.1 rule 6, e.g. `application/dividends.py` `# noqa: S608` sites) | — |
| Ledger store (TXN_S5, DSB_S6, RPY_S4, EXIT_S7, DIV_S6, INT_S5) | T | Rewrite money history (UPDATE/DELETE a posting) | Append-only triggers on `ledger_entries` and `transactions` (migration `0004`: `ledger_append_only`); balanced-DR/CR constraint trigger (`0004`/`0014`: `check_ledger_balanced`); closed-period posting trigger (`0014`: `forbid_closed_period_posting`); corrections only as reversing entries (`application/ledger.py:reverse_transaction`) | Corrections/misc-fees/write-off flows are **PLANNED (P13.15)** — until then reversal is the only correction path |
| Ledger store | R | Deny a posting happened / who did it | Every posting audit-logged in-transaction (`application/ledger.py:_post` callers + `application/audit.py:record_audit`); txn refs race-safe and UNIQUE (`_next_ref`, lock-order.md §6) | — |
| audit_log (all `*_S` audit stores) | T | Tamper with the audit trail | Append-only trigger `audit_log_append_only` (migration `0001`); written in the same transaction as the mutation (`application/audit.py:record_audit`) | — |
| audit_log | I | Audit payloads over-disclose to viewers | Audit viewer: `RequirePermission(access_control:view)`, before/after payloads redacted per role entitlement, keyset-paginated (P13.5, `api/audit_log.py`/`application/audit_log.py`) | — |
| Snapshot stores (EXIT_S1, DIV_S1) | T | Mutate an approved settlement/declaration snapshot | Dividend declarations write-once by DB trigger (migration `0020`: `forbid_dividend_snapshot_mutation`); execution re-verifies component-by-component under the full lock set and 409s on drift (`application/member_exits.py:_compute_under_locks`, `application/dividends.py:_verify_snapshot`) | — |
| L0_REDIS | T/D | Poison/flush rate-limit counters | Rate limiting is the only Redis consumer as-built (`infrastructure/rate_limit.py`); poisoning can only *tighten or loosen throttling*, never touch money paths | Redis network exposure/auth is deployment config → **P22** |
| TB3 (L0_OBXW, L0_EXPW, L0_DRMW) | S | Nothing authenticates a "worker" — could untrusted code run cycles? | Workers are in-process loops deployed with the backend (no network trigger surface exists as-built: `infrastructure/*_worker.py:run_worker`); the only staff-triggerable batch requires `RequirePermission(TRANSACTIONS, EDIT)` (`api/transactions.py` `/jobs/deposit-interest`) | — |
| TB3 | R | Worker mutations unattributable | Worker-side mutations write audit rows with `actor_id=None` = system actor (`application/audit.py:record_audit` nullable actor; e.g. `application/dormancy.py:_mark_dormant`) | — |
| TB3 | D | One tenant's failure starves the rest | Per-tenant cycles with failure isolation (`infrastructure/dormancy_worker.py:run_dormancy_cycle`, !32 R1); outbox backoff + dead-letter caps retry work (`outbox_worker.py:_record_failure`); export claim is single-row SKIP LOCKED (`application/exports.py:CLAIM_SQL`) | — |
| TB4 (OBX_E1, L0_PROVIDERS) | S | Spoofed/hostile provider endpoint | As-built the seam never leaves the process: `infrastructure/providers.py:StubProvider` logs event ids only; the protocol requires idempotency by event id; handlers cannot call providers (import-linter contract) | Real provider endpoint auth, credentials, delivery-status writeback are **PLANNED (P20)**; M-Pesa callback verification **PLANNED (P19)** |
| TB4 | I | Notification payload leaks PII to a third party | `StubProvider` never logs payload contents (`providers.py` docstring + implementation); outbox payloads are written by domain services, minimal by convention | Payload-contract allow-list for real templates is **PLANNED (P20 FM3)** — until then minimality is convention, not a gate: residual owned by **P20** |
| TB4 | D | Provider outage exhausts the system | Outbox decouples: domain txns succeed regardless (gate 1.2); exponential backoff + jitter, dead-letter at `MAX_ATTEMPTS` (`outbox_worker.py:backoff_delay`, `_record_failure`) | Per-channel circuit breakers **PLANNED (P20 FM5)** |

## 2. Money-flow elements (F1–F9)

| Element | STRIDE | Threat | As-built mitigation (citation) | Residual risk → owner |
|---|---|---|---|---|
| TXN_P2 (deposits/withdrawals/top-ups) | T | Caller supplies rates/periods/negative amounts | Amounts validated positive and quantised (`to_cents`, `record_deposit`/`record_withdrawal`/`record_share_topup` guards); no money *parameters* accepted — rates/fees/periods are server-resolved (v1.1 rule 1); `extra="forbid"` | — |
| TXN_P2 | D/T | Concurrent double-submit / overdraw race | Idempotency claim (TXN_P0); balance checked and written under the account row lock, withdrawable excludes live pledges (`record_withdrawal` + `application/guarantees.py:live_pledged_total`); serialisation per lock-order.md E10/E13 | — |
| TXN_P2 | I | Withdrawal-refusal oracle reveals pledge exposure | Deliberately generic rejection; exact figures only in the audit row (`record_withdrawal` docstring + `record_audit` payload) | — |
| DSB_P2 (disbursement) | T | TOCTOU: approve small, disburse big / over the multiplier cap | Stage machine under the anchor lock (step 1); deposit-multiplier eligibility re-verified under the deposit row lock at the money-moving moment (step 2b, issue #15); one atomic application-service transaction — no partial success | — |
| DSB_P2 | E | Collateral activated without the guarantor's consent | Unconsented (pledged-only) guarantees refuse disbursement (`disburse_loan` step 2, Codex-review guard) | — |
| RPY_P2/RPY_P3 (repayment) | T | Allocation manipulated (pay principal, dodge penalties) | Fixed server-side allocation order penalties → interest → principal (`application/loans.py:record_repayment`; posting `post_allocated_repayment`); closure releases pledges only via `release_guarantees_for_loan` under the loan row | — |
| EXIT_P3/EXIT_P4 (settlement) | T | Quote/approve/execute TOCTOU — settle on moved balances | Approval binds to the persisted snapshot (EXIT_S1); execution re-verifies every component under the full lock set and 409s posting nothing (`_compute_under_locks`; gate 1.4) | — |
| EXIT_P3 | E | One person requests and settles an exit | Committee quorum votes (EXIT_S2, one-vote UNIQUE) + separation of duties on settlement (P12 precedent, `post_settlement`) | — |
| DIV_P2–P5 (dividends) | T | Rate injection / caller-set payout | Rates resolved exclusively from tenant config (`resolve_dividend_config`); declaration totals persisted write-once (migration `0020` trigger); `_verify_snapshot` before first payment | — |
| DIV_P3 | T | Void-vs-distribute race pays a rejected declaration | Status re-verified `FOR SHARE` per batch, held to batch commit (`distribute_dividend` `process` comment; lock-order.md E2) | — |
| DIV_P4/DIV_P5 | T/D | Double-pay a member across concurrent runners / re-runs | One `(tenant, declaration, member)` UNIQUE claim `ON CONFLICT DO NOTHING` checked by rowcount (v1.1 rule 5); SKIP-LOCKED members converge via idempotent re-run | — |
| DIV_P5 (!36 unclaimed) | R | Mid-run-exited member's entitlement silently vanishes or is silently paid | Explicit audited terminal `disposition` (`'paid'`/`'unclaimed'`, migration `0022`); unclaimed money parked as `liability.unclaimed_dividends` payable (`post_unclaimed_dividend`) — never credited to settled accounts, never dropped; `0022` downgrade refuses loudly on live unclaimed rows | Resolution/payout of parked unclaimed dividends is **PLANNED (P13.15 correction paths)** |
| INT_P2/INT_P3 (deposit interest) | T | Park-funds-on-measurement-day exploit / backdated period | Basis = ledger-reconstructed average daily balance under the row lock, never a snapshot (gate 1.5, `_process_one`); period resolved server-side in strict quarter order (`resolve_run_parameters`); posting `occurred_at` at period end | — |
| INT_P3 | D/T | Re-run or concurrent runners double-post interest | `ON CONFLICT (tenant_id, account_id, period_start) DO NOTHING` claim + anti-join scan (re-run scans nothing) | — |
| DRM_P2/DRM_P3 (dormancy) | T | Wrong dormancy basis (spoofable "activity") | Last activity derived from member-initiated ledger postings, not editable fields (`dormancy_scan_sql` anti-join); status transition under the held member row; exactly-one-final-state vs concurrent deposits (P13.13 FM3, lock-order.md E10 note) | — |
| DRM_P3 | R | Member never learns they were marked dormant | Audit row + member-facing outbox notification in the same txn (`_mark_dormant`) | Delivery is stubbed until **P20** |
| EXP_* (exports — the exfiltration channel) | I | Export reveals PII beyond the caller's entitlement | Per-role column allow-list resolved server-side at request time — PII columns require `members:view` (`application/exports.py:_allowed_columns`, P13 blocker e); allow-list persisted on the request row and applied at render (`run_export_job` projection) | — |
| EXP_P4/EXP_P5 | I | Artifact URL guessing / stale links | Unguessable `secrets.token_urlsafe(32)` tokens, expiring per `artifact_ttl_hours`, requester-only access (`download_artifact`) | Artifact storage lives in Postgres as-built; object-storage hardening is deployment scope → **P22** |
| EXP_P4 | T | CSV formula injection into a spreadsheet | `domain/documents.py:escape_csv_text` (OWASP prefix set, tab/CR variants); PDF string escaping `_pdf_escape` | — |
| EXP_* | R | Who exported what, and was it truncated? | Audit row for EVERY request, render and download with scope/row-count/truncation (`request_export`/`run_export_job`/`download_artifact` `record_audit` calls; P13 blocker f — the audit IS the control) | — |
| EXP_P3/EXP_P4 | D | Export starves the event loop / unbounded scan | Keyset streaming with `batch_size+1` truncation detection and a hard server-side row cap (`run_export`); rendering via `asyncio.to_thread`; REPEATABLE READ snapshot so no long lock is held (`tenant_snapshot_session`) | — |
| OBX_P0 (outbox write) | T | Notification without a domain change (or vice versa) | Event INSERT in the SAME transaction as the domain change; rollback removes it (P5 atomicity tests; `application/outbox.py:enqueue_event`); direct provider calls from handlers are lint-blocked | — |
| OBX_P2–P4 (dispatch) | D | Poison event retries forever / duplicate delivery storm | Backoff + jitter, dead-letter at `MAX_ATTEMPTS` 8 (`_record_failure`); claim lease prevents concurrent duplicate claims (`dispatch_due` phase 1); adapters idempotent by event id (TB4) | — |
| OBX_S1 | R | Delivery outcome disputable | `status`/`attempts`/`last_error`/`dispatched_at` recorded per event (`dispatch_due` phase 3) | Provider-side delivery receipts (writeback) **PLANNED (P20)** |
| L0_MPESA (all M-Pesa flows) | S/T/R/I/D/E | Entire callback/STK threat surface | **PLANNED (P19)** — nothing built on main; P19's MR must land its L1/L3 DFD and this table's rows in the same MR (rule 11; P19 hardened blockers a–f are the checklist) | Owned by **P19** |
| L0_MEMBER (member-facing edges) | S/E | Member identity & member-scoped authz | **PLANNED (P14.5/P16–P18)** — no member principal exists on main; all member data mutations are staff-mediated behind TB1 | Owned by **P14.5** |

## 3. Accepted-risks register (named, per rule 13 honesty)

| Id | Risk | Where it lives | Owner |
|---|---|---|---|
| !29 F3/F4 | Interim guarantor identity: the caller's `users.email` must equal the guarantor member's `members.email` in-tenant (`application/guarantees.py:_actor_is_guarantor`) — an email-based identity bridge, accepted on !29; and guarantor self-release is impossible for roles without an applications grant | F2/RPY guarantee release/substitution paths (`release_guarantee`, `substitute_guarantee`) | **P14.5** (member identity backend) closes both; restated as a P14 merge blocker (b) |
| rule-13 | Security-template job spawn: v1.2 rule 13 records that SAST/Secret-Detection/Dependency-Scanning template jobs did not spawn on MR pipelines (!26/!28/!29). `.gitlab-ci.yml` on main now includes the `.latest` template variants (the R6 review fix) — per-MR spawn evidence must still be observed and recorded in each MR's DoD until **P22(a)** retires the carve-out | CI (out of this file's scope; do-not-touch for this MR) | **P22(a)** |
| UNOWNED-1 | JWT signing-key rotation (§1 TB1-S row) | `application/auth.py:_signing_key` | **UNOWNED** |
| UNOWNED-2 | No audit row for token issue/login events (§1 TB1-R row) | `application/auth.py:_issue_token_pair` | **UNOWNED** |
| UNOWNED-3 | In-process rate-limit fallback across multiple instances (§1 TB1-D row) | `infrastructure/rate_limit.py` | **UNOWNED** |

## 4. Drift rule

v1.2 rule 11: an MR that changes any mitigated flow, store, boundary,
or flips a PLANNED label updates the affected rows here AND the dfd.md
diagram in the same MR. P19/P20 MRs in particular inherit named row
obligations above. P23 consumes this file as its DAST triage map.
