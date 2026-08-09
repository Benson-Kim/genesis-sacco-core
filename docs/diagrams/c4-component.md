<!--
  P-DIAG.1 — C4 Level 3: Components, one diagram per API router group (as-built)
  Authored against main @ 08541b860f1445b16c342c39b6606d86b9dbeb17
  Reconciled to main @ 8f46aa54250ff1a066af423924f3eb54a9c72fb7
  (P-DIAG drift MR: !46 corrections + !47 recovery routers added as
  diagrams 19/20; the arrears close-pass seam added to diagram 7).
  Reconciled to main @ 4ea6bf288460e121a42815b67965adc1854a6320
  (docs/CI follow-up MR, !38 review R3: the four P13.10 report
  builders (!40) join diagram 11 with their domain seams — the drift
  !40 could not fix itself because docs/diagrams/** was owned by a
  concurrent session).
  Reconciled to main @ eb90a80ede68aed673c317ecd833b464ac17eac4
  (post-merge remediation MR: the P14.5 member surface (!65) joins as
  diagrams 21/22 — member-identity credential-link admin + the
  member-facing /member router — with their PINNED_CLAIMS).
  Router groups enumerated from genesis/api/app.py create_app at the
  reconciliation SHA (23 include_router calls resolving to 22 router
  modules — accounting_periods wires two routers). Every box names a REAL
  module: node labels carry the genesis/... path; the checked-in
  spot-check script `c4-spot-check.py` asserts (a) every cited module
  path exists and (b) every router wired in app.py has a diagram here.
  Drift rule: v1.2 rule 11 — an MR that adds/removes a router, moves a
  service boundary, or re-routes a dependency MUST update this file in
  the same MR.
  Lock statements are NOT made here: every lock taken by these services
  is owned by lock-order.md (P-DIAG.0) — cited by edge id, never
  restated.
-->

# C4 L3 — Components per router group (P-DIAG.1)

Reading guide: each diagram maps **router → application service →
domain module(s)**. The common seam every router shares (diagram 0) is
drawn once and not repeated. Session injection: the api layer composes
`tenant_session` (infrastructure) and passes the `AsyncSession` into
the application service — the application layer never imports
infrastructure (import-linter contract 2, `backend/pyproject.toml`).
Cross-cutting application modules (`audit.py`, `outbox.py`,
`pagination.py`, `batch_runner.py`) appear only where they carry the
flow being mapped.

## 0. Common request seam (shared by all 22 router groups)

```mermaid
flowchart LR
    REQ["HTTP request"] --> IDEM["genesis/api/idempotency.py<br/>IdempotencyMiddleware — mutating verbs:<br/>atomic key claim / stored-response replay"]
    IDEM --> AUTH["genesis/application/auth.py<br/>AuthContext from JWT"]
    AUTH --> PERM["genesis/api/authz.py<br/>RequirePermission — deny by default,<br/>P4 matrix via genesis/domain/rbac.py<br/>(member surface: RequireMemberPrincipal — diagram 22)"]
    PERM --> TS["genesis/infrastructure/tenancy.py<br/>tenant_session — SET LOCAL app.tenant_id"]
    TS --> H["router handler (diagrams 1-22)"]
```

## 1. health — `genesis/api/health.py`

Liveness/readiness only; no application/domain layer, no tenant scope.

```mermaid
flowchart LR
    R1["genesis/api/health.py<br/>GET /healthz, GET /readyz"] --> I1A["genesis/infrastructure/db.py<br/>DB ping"]
    R1 --> I1B["genesis/infrastructure/redis_client.py<br/>ping_redis"]
```

## 2. auth — `genesis/api/auth.py`

```mermaid
flowchart LR
    R2["genesis/api/auth.py<br/>POST /auth/otp/request|otp/verify|refresh|logout"] --> RL["genesis/infrastructure/rate_limit.py<br/>per-endpoint rate guard (Redis)"]
    R2 --> S2["genesis/application/auth.py<br/>OTP challenge, JWT issue,<br/>rotating refresh + family revocation"]
    S2 --> D2["genesis/domain/otp.py<br/>6-digit, TTL, constant-time compare"]
    S2 --> OB["genesis/application/outbox.py<br/>OTP delivery via outbox stub"]
```

## 3. members — `genesis/api/members.py`

```mermaid
flowchart LR
    R3["genesis/api/members.py<br/>/members CRUD, statement, status,<br/>POST /members/jobs/dormancy"] --> S3A["genesis/application/members.py<br/>GP-XXXX numbering, transitions,<br/>atomic account opening"]
    R3 --> S3B["genesis/application/dormancy.py<br/>run_dormancy_for_tenant — P13.13 batch"]
    S3A --> D3A["genesis/domain/members.py<br/>status machine incl. dormant"]
    S3B --> D3A
    S3B --> D3B["genesis/domain/ledger.py<br/>member-initiated txn-type allow-list<br/>(ledger-derived last activity)"]
    S3B --> BR["genesis/application/batch_runner.py<br/>shared batch runner"]
```

## 4. member KYC — `genesis/api/member_kyc.py`

```mermaid
flowchart LR
    R4["genesis/api/member_kyc.py<br/>/members/id/profile, /members/id/documents"] --> S4["genesis/application/member_kyc.py<br/>per-type profiles, consent immutability,<br/>document metadata + access audit"]
    S4 --> D4A["genesis/domain/member_kyc.py<br/>per-type validation matrix"]
    S4 --> D4B["genesis/domain/members.py"]
```

## 5. member exits — `genesis/api/member_exits.py`

```mermaid
flowchart LR
    R5["genesis/api/member_exits.py<br/>/member-exits request|votes|void|settlement|statement"] --> S5["genesis/application/member_exits.py<br/>snapshot request, exit votes,<br/>settlement posting (locks: lock-order.md E1, E10, E12, E14)"]
    S5 --> D5A["genesis/domain/exits.py<br/>compute_settlement, exit transitions"]
    S5 --> D5B["genesis/domain/committee.py<br/>decide - quorum"]
    S5 --> D5C["genesis/domain/ledger.py"]
    S5 --> S5B["genesis/application/ledger.py<br/>post_exit_settlement"]
    S5 --> S5C["genesis/application/loans.py<br/>per-loan closure"]
    S5 --> S5D["genesis/application/guarantees.py<br/>live_pledged_total eligibility, sweep"]
    S5 --> S5E["genesis/application/tenant_settings.py<br/>exit fee + quorum from config"]
```

## 6. loans (products / applications / guarantees) — `genesis/api/loans.py`

```mermaid
flowchart LR
    R6["genesis/api/loans.py<br/>/products, /applications, votes, transitions,<br/>/applications/id/guarantees, /guarantees/id/consent|release|substitute"] --> S6A["genesis/application/loan_products.py<br/>product config CRUD"]
    R6 --> S6B["genesis/application/loan_applications.py<br/>stage machine, cast_vote, cover pct<br/>(locks: APP row alone — lock-order.md §3 single-node rows)"]
    R6 --> S6C["genesis/application/guarantees.py<br/>pledge, consent, release, substitution<br/>(locks: lock-order.md E3, E4, E6-E9, E11)"]
    S6B --> D6A["genesis/domain/lending.py<br/>stage machine, product rules"]
    S6B --> D6B["genesis/domain/committee.py<br/>decide - quorum"]
    S6C --> D6A
    S6B --> S6D["genesis/application/tenant_settings.py<br/>committee_quorum, enforce_authority_band"]
```

## 7. loan book (disburse / repay / portfolio / arrears) — `genesis/api/loan_book.py`

```mermaid
flowchart LR
    R7["genesis/api/loan_book.py<br/>POST /applications/id/disburse, /loans list|schedule|settlement-quote,<br/>/loans/id/repayments, /portfolio/summary, POST /jobs/arrears"] --> S7A["genesis/application/ledger.py<br/>disburse_loan — one atomic txn<br/>(locks: lock-order.md E5, E15, E16)"]
    R7 --> S7B["genesis/application/loans.py<br/>record_repayment, allocation, closure<br/>(locks: lock-order.md loans row, E7)"]
    R7 --> S7C["genesis/application/arrears.py<br/>arrears + penalty batch (lock-order.md §3 arrears row)"]
    S7A --> D7A["genesis/domain/ledger.py<br/>balanced DR/CR posting specs"]
    S7B --> D7B["genesis/domain/lending.py<br/>inst(), classify(), provisioning"]
    S7C --> D7B
    S7C --> D7C["genesis/domain/tenant_config.py<br/>penalty config shape"]
    S7C --> S7D["genesis/application/recovery.py<br/>run_recovery_close_pass — P13.16 auto-close<br/>after the classify pass (lock-order.md §3 recovery rows)"]
```

## 8. transactions — `genesis/api/transactions.py`

```mermaid
flowchart LR
    R8["genesis/api/transactions.py<br/>/members/id/deposits|withdrawals|share-topups,<br/>GET /transactions, POST /jobs/deposit-interest"] --> S8A["genesis/application/transactions.py<br/>record_deposit (Dormant-to-Active reactivation),<br/>record_withdrawal, record_share_topup<br/>(locks: lock-order.md E10, E13)"]
    R8 --> S8B["genesis/application/deposit_interest.py<br/>quarterly accrual batch (lock-order.md §3 row)"]
    S8A --> D8A["genesis/domain/ledger.py"]
    S8A --> D8B["genesis/domain/members.py<br/>member_may capability map"]
    S8B --> D8C["genesis/domain/deposits.py<br/>ADB interest math"]
    S8B --> S8C["genesis/application/period_balances.py<br/>ledger-reconstructed average daily balance"]
```

## 9. dashboard — `genesis/api/dashboard.py`

```mermaid
flowchart LR
    R9["genesis/api/dashboard.py<br/>GET /dashboard/summary"] --> S9["genesis/application/dashboard.py<br/>per-permission slices, one REPEATABLE READ<br/>snapshot, NO locks (lock-order.md §7 P13.9 row)"]
    S9 --> D9A["genesis/domain/ledger.py"]
    S9 --> D9B["genesis/domain/lending.py<br/>classification for NPL/PAR"]
    S9 --> S9B["genesis/application/guarantees.py<br/>live_pledged_total — single source (1.1)"]
```

## 10. dividends — `genesis/api/dividends.py`

```mermaid
flowchart LR
    R10["genesis/api/dividends.py<br/>/dividends/declarations declare|votes|void|distribution,<br/>POST /members/id/share-transfers"] --> S10["genesis/application/dividends.py<br/>declare (write-once snapshot), cast_dividend_vote,<br/>distribute_dividend + unclaimed disposition,<br/>transfer_shares (locks: lock-order.md E2, E10, E12, E13)"]
    S10 --> D10A["genesis/domain/dividends.py<br/>financial-year resolution"]
    S10 --> D10B["genesis/domain/committee.py<br/>decide - quorum"]
    S10 --> S10B["genesis/application/ledger.py<br/>post_dividend_distribution, post_unclaimed_dividend,<br/>post_share_transfer"]
    S10 --> S10C["genesis/application/period_balances.py<br/>FY average share balance basis"]
    S10 --> S10D["genesis/application/tenant_settings.py<br/>rates + FY end from config only"]
    S10 --> BR10["genesis/application/batch_runner.py"]
```

## 11. reports & exports — `genesis/api/reports.py`

```mermaid
flowchart LR
    R11["genesis/api/reports.py<br/>POST /exports, GET /exports/id,<br/>GET /exports/downloads/token, POST /jobs/exports"] --> S11A["genesis/application/exports.py<br/>registry, claim (CLAIM_SQL), run_export_job —<br/>REPEATABLE READ, truncation headers, export audit"]
    S11A --> S11B["genesis/application/reports.py<br/>report queries: statements, trial balance,<br/>loan book, NPL trend + P13.10 (!40):<br/>_build_par_aging, _build_membership_register,<br/>_build_income_statement, _build_sasra_return"]
    S11A --> D11A["genesis/domain/documents.py<br/>CSV/PDF rendering, formula-injection escaping"]
    S11B --> D11B["genesis/domain/ledger.py<br/>ACCOUNT_CLASS chart map + fail-loud<br/>account_class (P13.10 income statement)"]
    S11B --> D11C["genesis/domain/sasra.py<br/>SASRA_LINES (SASRA-DS versioned, code-owned),<br/>fail-loud line_for_account (P13.10 return)"]
```

## 12. tenant settings — `genesis/api/tenant_settings.py`

```mermaid
flowchart LR
    R12["genesis/api/tenant_settings.py<br/>GET/PUT /settings"] --> S12["genesis/application/tenant_settings.py<br/>versioned optimistic-locked settings writer —<br/>the single legitimate writer of money parameters"]
    S12 --> D12A["genesis/domain/tenant_config.py<br/>validation bounds, tiered rate bands"]
    S12 --> D12B["genesis/domain/committee.py<br/>quorum defaults"]
```

## 13. accounting periods — `genesis/api/accounting_periods.py`

```mermaid
flowchart LR
    R13["genesis/api/accounting_periods.py<br/>POST /accounting-periods/close, GET /accounting-periods"] --> S13["genesis/application/accounting_periods.py<br/>close_period — exclusive advisory barrier,<br/>ON CONFLICT claim (lock-order.md §6)"]
```

## 14. me — `genesis/api/me.py`

```mermaid
flowchart LR
    R14["genesis/api/me.py<br/>GET /me/permissions"] --> S14["genesis/application/rbac.py"]
    S14 --> D14["genesis/domain/rbac.py<br/>role x module x action matrix"]
```

## 15. access control — `genesis/api/access.py`

```mermaid
flowchart LR
    R15["genesis/api/access.py<br/>GET /access/roles, GET|PUT role permissions"] --> S15["genesis/application/rbac.py<br/>update_permission (lock-order.md §3 PERM row)"]
    S15 --> D15["genesis/domain/rbac.py"]
```

## 16. users — `genesis/api/users.py`

```mermaid
flowchart LR
    R16["genesis/api/users.py<br/>/users CRUD, status, role,<br/>otp/invalidate, otp/reenrol"] --> S16["genesis/application/users.py<br/>transitions, last-admin guard, family revocation<br/>(locks: lock-order.md E17-E19)"]
    S16 --> D16A["genesis/domain/users.py<br/>user status machine"]
    S16 --> D16B["genesis/domain/rbac.py"]
```

## 17. audit log viewer — `genesis/api/audit_log.py`

```mermaid
flowchart LR
    R17["genesis/api/audit_log.py<br/>GET /audit-log"] --> S17["genesis/application/audit_log.py<br/>keyset read, per-role before/after redaction"]
    S17 --> D17["genesis/domain/rbac.py<br/>entitlement for payload disclosure"]
```

## 18. branches — `genesis/api/branches.py`

```mermaid
flowchart LR
    R18["genesis/api/branches.py<br/>/branches CRUD, user/member assignment,<br/>POST /jobs/branch-backfill"] --> S18["genesis/application/branches.py<br/>registry CRUD + legacy-text backfill"]
    S18 --> BR18["genesis/application/batch_runner.py"]
```

## 19. corrections (fees / maker-checker adjustments / write-offs / recovery receipts) — `genesis/api/corrections.py`

P13.15 (!46) + issue #21 (!51) + issue #24 (!52). Every route carries
the DEDICATED corrections-module permissions (`corrections:view/
create/approve`) — never generic `transactions:edit`.

```mermaid
flowchart LR
    R19["genesis/api/corrections.py<br/>POST /corrections/fees,<br/>/corrections/repayment-adjustments + approval|reject,<br/>/corrections/write-offs + votes|void|posting|recoveries"] --> S19["genesis/application/corrections.py<br/>post_misc_fee; request/approve/reject_repayment_adjustment<br/>(two-phase maker-checker, snapshot-bind-reverify);<br/>request/vote/void/post_write_off (committee quorum,<br/>write-once snapshot); record_recovery_receipt<br/>(locks: lock-order.md E20-E24 + §3 single-node rows)"]
    S19 --> D19A["genesis/domain/lending.py<br/>loan_transition (the ONE closed-to-active reopen branch;<br/>terminal written_off), NPL_CLASSES prudential gate"]
    S19 --> D19B["genesis/domain/committee.py<br/>decide - write-off quorum"]
    S19 --> D19C["genesis/domain/rbac.py<br/>ASSURANCE_ROLES - checker exclusion (SoD)"]
    S19 --> S19B["genesis/application/ledger.py<br/>post_fee, post_reversal (storno),<br/>post_loan_write_off, post_loan_recovery"]
    S19 --> S19C["genesis/application/guarantees.py<br/>release_guarantees_for_loan<br/>(full-recovery discharge only)"]
    S19 --> S19D["genesis/application/tenant_settings.py<br/>fee amounts + enforce_authority_band from config"]
    S19 --> S19E["genesis/application/transactions.py<br/>_require_member - the P13.13 status gatekeeper"]
```

## 20. recovery cases — `genesis/api/recovery.py`

P13.16 (!47). Workflow state only — no money moves through this
router; routes carry `loan_book:view/create/edit`.

```mermaid
flowchart LR
    R20["genesis/api/recovery.py<br/>POST /recovery-cases, GET worklist,<br/>/recovery-cases/id assign|notes"] --> S20["genesis/application/recovery.py<br/>open_recovery_case (NPL check under the loan lock),<br/>assign_recovery_case, add_recovery_note (append-only),<br/>list_worklist (keyset by days-past-due),<br/>run_recovery_close_pass (arrears-job seam, diagram 7)<br/>(locks: lock-order.md §3 recovery single-node rows)"]
    S20 --> D20A["genesis/domain/recovery.py<br/>transition - the single case-status gatekeeper"]
    S20 --> D20B["genesis/domain/lending.py<br/>NPL_CLASSES - open gate + close-pass cure test"]
    S20 --> D20C["genesis/domain/rbac.py<br/>ASSURANCE_ROLES - assignee exclusion"]
    S20 --> S20B["genesis/application/rbac.py<br/>actor_access - assignee grant check"]
    S20 --> BR20["genesis/application/batch_runner.py<br/>close pass batches"]
```

## 21. member identity (credential-link admin) — `genesis/api/member_identity.py`

P14.5 FM3 (!65). Credential-link create/revoke are AUDITED ADMIN
MUTATIONS under the dedicated narrow `member_identity:*` permissions —
never self-service, never implied by `members:edit` (deny by default,
gate 1.6). Re-linking an email to another member is revoke + create:
two audited mutations, each notifying the member.

```mermaid
flowchart LR
    R21["genesis/api/member_identity.py<br/>GET|POST /members/id/credentials,<br/>POST /credentials/id/revoke"] --> S21["genesis/application/member_identity.py<br/>create_credential (atomic active-email claim:<br/>ON CONFLICT DO NOTHING + rowcount, 0035 partial UNIQUEs),<br/>revoke_credential, list_member_credentials<br/>(locks: lock-order.md §3 member-credential link row —<br/>member row MSELF alone, chain ROOT)"]
    S21 --> D21A["genesis/domain/members.py<br/>MemberStatus — link eligibility"]
    S21 --> S21B["genesis/application/audit.py<br/>in-txn audit rows (FM3)"]
    S21 --> S21C["genesis/application/outbox.py<br/>member notification via outbox"]
```

## 22. member (member-facing /member surface) — `genesis/api/member.py`

P14.5 (!65). The MEMBER principal's own surface. `/member/auth/*`
reuses the staff endpoint SHAPES from `genesis/api/auth.py` (bodies,
response models, rate guard — imported, not copied; gate 1.1) but
issues MEMBER-audience tokens that can never satisfy a staff
`RequirePermission` gate (FM1). Business routes carry
`RequireMemberPrincipal` — the per-request live-link re-check (FM2) —
and the consent/release services re-verify the link again INSIDE the
transaction under the guarantee row lock.

```mermaid
flowchart LR
    R22["genesis/api/member.py<br/>POST /member/auth/otp/request|otp/verify|refresh,<br/>POST /member/guarantees/id/consent|release"] --> RL22["genesis/infrastructure/rate_limit.py<br/>rate guard shared with genesis/api/auth.py (gate 1.1)"]
    R22 --> PERM22["genesis/api/authz.py<br/>RequireMemberPrincipal — per-request<br/>live-link re-check (FM2)"]
    R22 --> S22A["genesis/application/member_auth.py<br/>request_member_otp, verify_member_otp,<br/>rotate_member_refresh_token — identity resolves through<br/>member_credentials (0035), never users.email<br/>(locks: lock-order.md §3 member OTP / refresh rows)"]
    S22A --> S22B["genesis/application/auth.py<br/>MemberAuthContext — MEMBER-audience<br/>token issue/decode (FM1)"]
    S22A --> D22A["genesis/domain/otp.py<br/>6-digit, TTL, constant-time compare"]
    S22A --> D22B["genesis/domain/members.py<br/>MemberStatus gate"]
    S22A --> OB22["genesis/application/outbox.py<br/>OTP delivery via outbox"]
    R22 --> S22C["genesis/application/guarantees.py<br/>consent_guarantee_as_member, release_guarantee_as_member<br/>— in-txn link re-verify under the guarantee row lock<br/>(locks: lock-order.md §3 guarantee-consent row, GUAR alone)"]
```

## Verification

- **Router completeness**: the 22 groups above are exactly the
  router modules resolved from the `include_router` calls in
  `genesis/api/app.py` `create_app` at the reconciliation SHA (health,
  auth, members, member_kyc, member_exits, member_identity, member,
  loans, loan_book, transactions, dashboard, dividends, reports,
  tenant_settings, accounting_periods, me, access, users, audit_log,
  branches, corrections, recovery).
- **No invented boxes**: run `python3 docs/diagrams/c4-spot-check.py`
  from the repo root — it fails if any cited `genesis/...` module path
  does not exist, or if a router wired in `app.py` has no diagram
  section here, or if a diagram names a router that is not wired.
- **Locks**: every lock reference above is an edge id or §-row of
  [`lock-order.md`](lock-order.md) — the single authority; nothing is
  restated here (v1.2 rule 11).
