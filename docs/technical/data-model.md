# Data model

Entity catalog at the current migration head. The authoritative
entity-relationship diagram — every table and foreign key, gated in CI by a
spot-check script — is [`docs/diagrams/erd.md`](../diagrams/erd.md); this
document narrates it and adds the migration catalog. Domain enumerations and
status machines cited below live under `backend/src/genesis/domain/`.

Conventions that hold for every tenant-owned table:

- `tenant_id` column + forced Row-Level Security + explicit tenant predicate
  on every statement (see [architecture.md](architecture.md#4-multi-tenancy-model)).
- Constraints live in the **database** (CHECK, UNIQUE, FK, NOT NULL by
  default), not only in application validation.
- Editable aggregates carry an optimistic-locking `version` column; stale
  writes surface as HTTP 409.
- Money columns are `NUMERIC(18,2)`.

## 1. Tenancy, identity and access

| Table | Purpose |
|---|---|
| `tenants` | One row per SACCO. |
| `users` | Staff accounts: email, phone (E.164), role, `status` (`active`/`suspended`, single transition map in `domain/users.py`), `last_active_at` (written at token issue only). |
| `roles`, `permissions` | The seeded RBAC matrix (role × module × view/create/edit/approve), deny-by-default; seed data mirrors `domain/rbac.py:seed_matrix()`. A DB trigger refuses renames of system roles so approval bands keyed by role name can never silently detach. |
| `otp_challenges` | Hashed one-time codes: 6 digits, at most 5 attempts, 5-minute TTL, single-use (`domain/otp.py`). Raw codes are never stored. |
| `refresh_tokens` | Rotating refresh-token families, hashed at rest; reuse of a spent token revokes the whole family (`application/auth.py`). |
| `member_credentials` | The member-principal substrate: links an authentication credential to a member; re-verified live on every member request. |
| `idempotency_keys` | Claimed `Idempotency-Key` values with request hash + stored response; replay returns the stored response. Rows expire per the retention setting and are purged by a worker. |
| `audit_log` | Immutable in-transaction audit trail for every mutation: actor, action, entity, entity id, before/after payloads. |

## 2. Members

| Table | Purpose |
|---|---|
| `members` | The member register. Types: `person`, `company`, `group`, `vehicle`. Statuses: `active`, `arrears`, `dormant`, `exited` (terminal); transitions run through the single map in `domain/members.py`. Member numbers use the `GP-XXXX` format with race-safe allocation. Carries the phone (E.164), branch assignment and the stored `dividend_payout` preference. |
| `member_profiles` | Per-type KYC profile sections (`domain/member_kyc.py`), validated with `extra="forbid"` schemas; a DB CHECK backstops the section shape. |
| `member_documents` | KYC document checklist rows with their own status machine. |
| `branches` | The branches registry; members and users can be assigned to a branch. |

**Status-capability map.** Which statuses may perform which money operation
is decided in exactly one place, `domain/members.py:MEMBER_OPERATIONS`
(allow-list, never deny-list):

| Operation | active | arrears | dormant | exited |
|---|---|---|---|---|
| Deposit, loan repayment, fee, recovery receipt | ✓ | ✓ | ✓ | — |
| Share top-up | ✓ | ✓ | — | — |
| Withdrawal, borrow, pledge, share transfer (either side) | ✓ | — | — | — |
| Exit request | ✓ | ✓ | ✓ | — |

A deposit into a dormant account also reactivates the member inside the same
transaction (`application/transactions.py:record_deposit`). Dormancy itself
is entered only by the nightly dormancy job when no member-initiated
transaction fell inside the tenant-configured window (`application/dormancy.py`;
the member-initiated classification of every transaction type is code-owned
in `domain/ledger.py:MEMBER_INITIATED`).

## 3. Accounts, transactions and the ledger

| Table | Purpose |
|---|---|
| `share_accounts`, `deposit_accounts` | One balance row per member per kind, updated only under a row lock inside the posting transaction. |
| `transactions` | One row per posting: type, channel, system reference (`txn_ref`), member attribution, the operator-entered `external_ref` for external channels, `reversal_of_id` for reversing entries, `created_by` actor attribution. |
| `ledger_entries` | The double-entry legs (account, side, amount). Append-only: a DB trigger forbids UPDATE/DELETE, and a trigger enforces balanced DR/CR per posting. |
| `txn_ref_sequences` | Race-safe reference numbering (advisory lock + UNIQUE + retry), one sequence per prefix. |
| `deposit_interest_accruals`, `penalty_accruals` | Idempotency claim rows for the interest and penalty jobs — each claimed period/day is final and never restated. |
| `accounting_periods` | Period rows with a closed-period trigger: postings cannot land in a closed period. |
| `account_period_balances`, `member_period_balances`, `portfolio_month_snapshots` | Derived per-period rollups and month-end portfolio snapshots, reconstructed from posting history (never point-in-time snapshots as a value basis). |

Transaction types, channels, reference prefixes, account chart and posting
shapes are catalogued in [ledger-and-money.md](ledger-and-money.md).

## 4. Lending

| Table | Purpose |
|---|---|
| `loan_products` | Product definitions (rate, term bounds); money parameters are resolved server-side from these rows, never from request bodies. |
| `loan_applications` | The application stage machine: `submitted → appraisal → committee → approved → disbursed`, with `rejected` reachable from the first three stages (`domain/lending.py:ApplicationStage`). Carries amount, cover figures, `created_by`, `recommended_by` (the actor who moved it into committee) and `version`. |
| `committee_votes` | One vote per committee member per application (DB UNIQUE); quorum and decision rules in `domain/committee.py` (rejection wins an ambiguous tally). |
| `loans` | Disbursed loans: status `active → closed / written_off` (`written_off` terminal; `closed` re-opens only via the repayment-adjustment reversal branch), classification label, provision percentage. |
| `loan_schedules` | The amortization schedule (reducing-balance annuity; the final installment absorbs rounding drift; `domain/lending.py:build_schedule`). |
| `repayments` | Append-only repayment rows (DB trigger); allocation order penalties → interest → principal (`domain/lending.py:allocate_repayment`). |
| `guarantees` | Pledges backing applications/loans. Capacity = deposit balance minus live pledges, computed under the guarantor's deposit-account row lock (`application/guarantees.py`); live pledges also reduce the withdrawable balance. |

Prudential classification (`domain/lending.py:classify`): days past due
30/90/180/360 map to normal/watch/substandard/doubtful/loss with
1/5/25/50/100% provisioning; substandard and worse are NPL.

## 5. Exits, dividends and share transfers

| Table | Purpose |
|---|---|
| `member_exits` | Exit workflow: `requested → approved → settled`, with `rejected` reachable before settlement (`domain/exits.py`). The settlement computation (shares + deposits − loan payoff − exit fee) is persisted as a snapshot at request time and re-verified component-by-component under the full lock set at posting time. A negative net settlement is rejected at request time. |
| `exit_votes` | Committee votes on exit approvals (same quorum machinery as loan applications). |
| `dividend_declarations` | One declaration per completed financial year: dividend rate on the average daily share balance, rebate rate on the average daily deposit balance; totals derived as the sum of already-rounded per-member figures (`domain/dividends.py`). |
| `dividend_declaration_votes` | Committee approval of a declaration. |
| `dividend_distributions` | Per-member payout rows; a member who exited between declaration and distribution has the entitlement parked as an explicit unclaimed-dividends liability. |
| `share_transfers` | Member-to-member share transfers under maker–checker: one user proposes, a distinct non-assurance user approves (DB CHECK backstop); the posting is two member-attributed legs through a clearing account that nets to zero in the same transaction. |

## 6. Corrections, write-offs and recovery

| Table | Purpose |
|---|---|
| `repayment_adjustments` | Maker–checker repayment corrections: the maker requests, a distinct checker approves/executes (DB CHECK enforces maker ≠ checker); execution posts reversing entries, never edits. |
| `loan_write_offs` | Write-once snapshot of a committee-approved write-off — the surviving legal claim on the member. Write-off never resurrects the loan. |
| `loan_write_off_votes` | Committee votes on write-off proposals. |
| `loan_recoveries` | Append-only bad-debt recovery receipts drawn against the surviving claim, capped at the outstanding claim under the write-off row lock. |
| `recovery_cases` | Collections worklist: statuses `open`, `irrecoverable_pending_write_off`, `disputed` (live) and `closed_cured` / `closed_written_off` / `closed_restructured` (terminal); cure/write-off closes are produced only by the arrears job's auto-close pass, never by staff declaration (`domain/recovery.py`). One live case per loan (partial UNIQUE). |
| `recovery_case_notes` | Append-only case notes; one post-closure outcome note is allowed on a terminal case. |

## 7. Configuration, reporting and plumbing

| Table | Purpose |
|---|---|
| `tenant_settings` | Every tenant-configurable money parameter, one typed registry (`domain/tenant_config.py:SETTINGS_REGISTRY`): interest/penalty/dividend rates, dormancy period, financial-year end month, exit fee, registration fee, committee size/quorum, the approval-authority bands, tiered loan-rate bands. Bounds are mirrored 1:1 by DB CHECKs; band JSON is revalidated at read time. |
| `exports`, `export_artifacts` | The export job queue and its rendered artifacts (row-capped, truncation-flagged, TTL-bound). |
| `outbox_events` | The transactional outbox: written in the same transaction as the domain change; dispatched by a worker with retry/backoff and dead-lettering. |

## 8. Migration catalog

The alembic chain under `backend/migrations/versions/`. Every migration has
an exact downgrade (CI runs upgrade → downgrade → upgrade on every pipeline).

| # | Name | Purpose |
|---|---|---|
| 0001 | `0001_schema_v1` | Core tables, constraints, indexes, forced RLS tenancy. |
| 0002 | `0002_auth_refresh_tokens` | Refresh-token families with rotation state. |
| 0003 | `0003_outbox_worker` | Outbox `last_error` column and worker tenant registry. |
| 0004 | `0004_ledger` | Balanced DR/CR trigger, append-only trigger, txn-ref sequence table. |
| 0005 | `0005_committee_votes` | One vote per committee member per application. |
| 0006 | `0006_applications_keyset_index` | Keyset index for the applications listing. |
| 0007 | `0007_loan_servicing` | Penalties bucket, closure timestamp, servicing indexes. |
| 0008 | `0008_transactions_interest` | Accrual idempotency, transaction-listing indexes. |
| 0009 | `0009_deposit_interest_config` | Deposit-interest configuration + accrual-scan index. |
| 0010 | `0010_member_exit` | Exit snapshot columns, exit votes, exit fee. |
| 0011 | `0011_guarantee_loan_backfill` | Guarantee-to-loan linkage backfill. |
| 0012 | `0012_accounting_periods` | Accounting periods table, RLS, closed-period trigger. |
| 0013 | `0013_exports` | Export jobs, artifacts, report-query indexes. |
| 0014 | `0014_ledger_integrity_hardening` | Ledger integrity hardening. |
| 0015 | `0015_users_admin_audit_viewer` | Users administration & audit-log viewer. |
| 0016 | `0016_branches_registry` | Branches registry. |
| 0017 | `0017_tenant_settings_general` | Tenant settings, parameters & approval matrix. |
| 0018 | `0018_member_kyc` | Member KYC profiles & documents. |
| 0019 | `0019_penalty_accruals` | Penalty accrual claim table. |
| 0020 | `0020_dividends_share_lifecycle` | Dividends & share lifecycle. |
| 0021 | `0021_member_dormancy` | Member dormancy lifecycle. |
| 0022 | `0022_dividend_dormant_policy` | Dividends × dormancy policy. |
| 0023 | `0023_p1310_report_indexes` | Register keyset index + report vocabulary CHECK widening. |
| 0024 | `0024_outbox_retention_discovery` | Outbox purge index + due/purgeable tenant registries. |
| 0025 | `0025_corrections_write_off` | Ledger corrections, misc fees & loan write-off. |
| 0026 | `0026_recovery_cases` | Collections & recovery worklist tables. |
| 0027 | `0027_portfolio_month_snapshots` | Month-end portfolio snapshots. |
| 0028 | `0028_period_rollups` | Per-account and per-member period rollups. |
| 0029 | `0029_idempotency_expiry` | Idempotency key expiry. |
| 0030 | `0030_loan_recoveries` | Bad-debt recovery receipts for written-off loans. |
| 0031 | `0031_adjustment_maker_checker` | Maker–checker workflow for repayment adjustments. |
| 0032 | `0032_repayments_append_only` | Append-only trigger discipline for repayments rows. |
| 0033 | `0033_recovery_case_dispositions` | Richer recovery-case dispositions + post-closure outcome notes. |
| 0034 | `0034_recovery_claim_cap_lock` | Claim-cap trigger locking probe (parent FOR UPDATE). |
| 0035 | `0035_member_identity` | Member-credential link table + principal columns. |
| 0036 | `0036_actor_attribution` | `created_by` on loan applications and transactions. |
| 0037 | `0037_committee_recommender` | `recommended_by` on loan applications. |
| 0038 | `0038_corrections_register_indexes` | Corrections register keyset indexes. |
| 0039 | `0039_member_dividend_payout` | Stored member dividend-payout preference. |
| 0040 | `0040_share_transfer_maker_checker` | Share-transfer maker–checker workflow + history register. |
| 0041 | `0041_members_numeric_member_no_index` | Numeric member-number keyset index. |
| 0042 | `0042_phone_e164_backfill` | Members/users phone local-format → E.164 backfill. |
| 0043 | `0043_external_txn_ref_and_search_index` | External transaction reference + ledger search prefix index. |
| 0044 | `0044_users_phone_signin_index` | Users phone sign-in lookup index. |
