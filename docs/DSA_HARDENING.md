# GENESIS PRESTIGE — DATA-STRUCTURE & ALGORITHM HARDENING (v1.0)

Evidence-based inventory of data structures and algorithms in
`backend/src/genesis` (main @ `556c128`) that sit below core-banking
standard, plus items that were explicitly evaluated and found sound (so the
next reviewer does not re-litigate them). Every entry cites the file and
function actually read; nothing here is speculative.

Priorities: **High** = will hurt at the P21 target load (10k-member tenant)
or is an operational time bomb; **Medium** = correct today, degrades with
data volume, plan the migration; **Low** = cosmetic/theoretical.
No entry is rated Critical: nothing found is incorrect-by-construction —
the weaknesses are complexity-at-scale and unbounded-growth classes.
Every High item carries a remediation reference (prompt P13.17 in
`docs/BUILD_PROMPTS.md`, added alongside this document).

---

## 1. Findings requiring remediation

### DSA-1 — NPL-trend month-end reconstruction: O(months × history) rescans — **High**

* **Where:** `application/reports.py` — `NPL_TREND_MONTH_SQL`,
  `_build_npl_trend`.
* **Current structure:** for each of the configured months (default 6) the
  builder re-executes a 4-CTE query that rescans, per cutoff: all
  repayments joined to transactions (`paid`), all `loans.receivable` credit
  legs joined through repayments (`principal_paid`), a window-function
  cumulative sum over all due schedule rows (`sched`), and an anti-join for
  the first unmet installment. Complexity per export ≈
  `months × (|repayments| + |ledger legs| + |schedules|)`.
* **Why weak:** the same append-only history is scanned six times with only
  the cutoff changing; at 10k loans × 36-month terms the `sched` CTE alone
  window-scans ~360k rows per month, per export. The export job runner is
  serial per tenant (REPEATABLE READ claim), so one NPL-trend export
  monopolises the queue for the whole recomputation. The correctness
  properties (reconstruct from the append-only record, never from mutable
  state) are right and must be preserved.
* **Replacement:** incremental month-end portfolio snapshots: a
  `portfolio_month_snapshots` table (tenant_id, month_end, gross, npl_balance,
  npl_loans) written once when a month completes — either by the nightly
  arrears job on first run after month-end or at `close_period` — using the
  existing reconstruction SQL for exactly one month. The export then reads
  N snapshot rows and reconstructs at most the current (incomplete) month.
  Migration path: additive table + backfill job through the shared batch
  runner; the reconstruction SQL stays as the single source of truth for
  snapshot writing, so no dual-maintenance of the math.
* **Remediation:** P13.17(a).

### DSA-2 — Trial balance: full-ledger aggregate per export — **High (at scale), Medium today**

* **Where:** `application/reports.py` — `TRIAL_BALANCE_SQL`,
  `_build_trial_balance`.
* **Current structure:** `SUM(...) FILTER` over **every** `ledger_entries`
  row of the tenant (joined to `transactions` for `occurred_at <= :as_of`),
  grouped by account, on every export.
* **Why weak:** the ledger is append-only and immutable — the aggregate over
  any closed accounting period can never change, yet it is recomputed on
  every trial-balance run. O(|ledger|) per export, growing without bound for
  the life of the tenant. `idx_ledger_account (tenant_id, account,
  created_at)` does not help the join-filtered sum much; the plan degrades
  toward a full scan of the tenant's ledger partition.
* **Replacement:** per-account period rollups written at `close_period`
  (`account_period_balances(tenant_id, period_start, account, debits,
  credits)`, additive, RLS like 0012): trial balance = sum of closed-period
  rollups + live aggregate over the open period only. The deferred trigger
  from the Codex-review MR (ledger totals = transactions.amount) makes these
  rollups verifiable against `transactions.amount` sums as a cross-check.
* **Remediation:** P13.17(b).

### DSA-3 — Idempotency claim table: unbounded growth, no purge — **High (operational)**

* **Where:** `api/idempotency.py` (`_store` keeps every completed key
  forever); `idempotency_keys` (0001) has no TTL column and no purge path.
* **Why weak:** every mutating request with an `Idempotency-Key` inserts a
  row that is never deleted (only 5xx claims are released). The table grows
  monotonically with write traffic; the `(tenant_id, key)` UNIQUE index
  bloats, and replay semantics silently become "forever", which is stronger
  than any client needs and keeps stored response bodies (which can embed
  member data) indefinitely — a data-retention liability (Kenya DPA
  checklist in P23).
* **Replacement:** add `expires_at` (server-set, e.g. 24–72h), purge via the
  shared batch runner (`run_in_batches`, anti-join on the claim key —
  standing rule 8), and include `expires_at > now()` in the replay lookup so
  expiry is enforced even before the purge job runs. Migration: additive
  column with default, backfill in batches, then the job.
* **Remediation:** P13.17(c).

### DSA-4 — Export artifacts: full in-memory accumulation + bytea storage — **Medium**

* **Where:** `application/exports.py` — `run_export` (accumulates `rows` for
  the PDF even while CSV streams via `on_batch`), `run_export_job` (whole
  CSV + whole PDF as bytea in `export_artifacts`); `domain/documents.py`
  `CsvBuilder` (single `StringIO`), `render_pdf` (all pages in memory).
* **Why weak:** memory is bounded only by `export_row_cap` (10k) × row
  width × 3 copies (raw cells, CSV buffer, PDF buffer); raising the cap for
  a real tenant multiplies worker RSS. Artifacts as bytea rows inflate the
  main database (TOAST churn, backup size); download streams the full blob
  through the app.
* **Replacement:** render the PDF incrementally per batch (its page model is
  already line-oriented) so `rows` accumulation can be dropped; store
  artifacts in object storage behind an infrastructure adapter (needs an ADR
  per MASTER_PROMPT §6 — flagged, not assumed), keeping the DB row as
  token/metadata only. Acceptable to defer while the cap is 10k.
* **Remediation:** P13.17(d) (bundled with the ADR decision).

### DSA-5 — Member-statement opening balance: O(member history) per export — **Medium**

* **Where:** `application/reports.py` — `member_statement_opening_sql` +
  the sequential Python running balance in `to_cells`;
  `application/deposit_interest.py` — `_balance_as_of_period_end` (same
  pattern: current balance minus post-cutoff net movement).
* **Current structure:** correct append-only reconstruction; the opening
  aggregate groups the member's **entire** pre-window history on each run,
  and the ADB helper's `occurred_at >= :cutoff` sum scans everything after
  the cutoff (cheap for the current quarter, O(history) for backfilled old
  quarters).
* **Why weak (only at scale):** per-member history is small today; for a
  10-year vehicle account with daily contributions it is ~3.6k rows × every
  statement/export/interest backfill. Not a correctness hazard — the
  running balance is computed under one snapshot and `member_direction` is
  the single source of truth.
* **Replacement:** period-anchored member balance snapshots (natural join
  with DSA-2's rollups at `close_period` granularity): opening balance =
  last snapshot ≤ window start + delta since. Migrate only when P21 load
  tests show statement latency breaching budget.
* **Remediation:** tracked under P13.17(b) (same rollup infrastructure);
  explicitly deferred otherwise.

### DSA-6 — Outbox: dispatched-row retention and per-row lease updates — **Medium**

* **Where:** `infrastructure/outbox_worker.py` — `dispatch_due`,
  `run_dispatch_cycle`; `outbox_events` (0001/0003).
* **Current structure:** the pending scan is properly served by the partial
  index `idx_outbox_pending (next_attempt_at) WHERE status='pending'` with
  `FOR UPDATE SKIP LOCKED` — sound. But (a) `dispatched` rows are kept
  forever (same growth class as DSA-3, with PII-minimised but real payloads);
  (b) the claim loop issues one `UPDATE` per claimed row instead of one
  `UPDATE … WHERE id = ANY(:ids)` (N round-trips per batch); (c)
  `run_dispatch_cycle` iterates every tenant every 5s serially —
  O(tenants) wake-ups even when idle.
* **Replacement:** (a) retention purge/archival via the shared batch runner;
  (b) single set-based lease UPDATE; (c) a cross-tenant due-work query
  (`SELECT DISTINCT tenant_id FROM outbox_events WHERE status='pending' AND
  next_attempt_at <= now()`) through the SECURITY DEFINER pattern of 0003 to
  wake only tenants with work.
* **Remediation:** P13.17(e).

### DSA-7 — Stage-filtered application listing lacked a serving index — **High (fixed in the Part 3 code MR)**

* **Where:** `application/loan_applications.py` — `list_applications` with
  `stage=` filter; index 0006 `(tenant_id, created_at DESC, id DESC)`.
* **Why weak:** with a stage filter the scan walks the keyset index and
  discards non-matching rows — O(rows scanned / stage selectivity) per page;
  rare stages (e.g. `approved`) at depth degrade toward a full tenant scan.
  The 0001 `idx_applications_stage (tenant_id, stage)` cannot serve the
  keyset order, so Postgres must sort.
* **Replacement:** additional index `(tenant_id, stage, created_at DESC,
  id DESC)`; **keep** 0006 for the unfiltered listing — an index with
  `stage` in the middle cannot produce `(created_at, id)` global order
  without a sort, so replacing 0006 (as the external Codex fix did) would
  have broken the unfiltered scan it was shipped to serve. The now-redundant
  0001 `idx_applications_stage` prefix index is dropped in the same
  migration.
* **Remediation:** delivered in the `fix(review)` MR (migration 0014) with
  EXPLAIN assertions for both the filtered and unfiltered page.

---

## 2. Evaluated and found sound (kept for the record)

| Area | Evidence | Verdict |
|---|---|---|
| Amortization rounding & remainder distribution | `domain/lending.py: build_schedule` — cent-rounded annuity, final installment absorbs drift, negative/over-balance clamps, Hypothesis property tests on `sum(principal_due) == principal` | Sound; standard final-installment absorption. Not a defect. |
| Repayment allocation | `domain/lending.py: allocate_repayment` — three `min()` bucket steps, overpayment rejected against the payoff quote | Sound; O(1), Decimal-only, hand-computed oracles in tests. |
| Deposit-interest ADB walk | `deposit_interest.py: _average_daily_balance` — one grouped SQL read per account+period, then an O(days-in-period) backward walk | Sound design (ledger-reconstructed under the account lock); only the pre-cutoff aggregate is the DSA-5 scale note. |
| Reference generation | `ledger._next_ref` — advisory lock + upsert counter + UNIQUE net; CRC-32 prefix hash with a documented reason (anagram collision of a naive char sum) | Sound. |
| Advisory key derivation | first 4 UUID bytes & 0x7FFFFFFF (`ledger._advisory_key`, `accounting_periods._tenant_lock_key`) — cross-tenant collision ≈ birthday over 2³¹: ~0.02% at 1k tenants, causing only needless serialisation, never correctness loss | Low — accept; revisit only if tenant count reaches tens of thousands. |
| Keyset cursor encoding | `application/pagination.py` — plaintext `iso|uuid`, tz-aware + UUID validated, used only inside the caller's own tenant scope (RLS + bound tenant predicate) | Low — forgeable but harmless (a forged cursor just repositions the caller's own window); an HMAC-signed cursor is cosmetic hardening. Leaks row UUIDs + timestamps the same response already contains. |
| OTP + token generation/comparison | `application/auth.py`, `domain/otp.py` — `secrets.randbelow` OTP with attempt cap + TTL, HMAC-SHA256 pepper + `hmac.compare_digest`; refresh tokens `token_urlsafe(48)` stored as SHA-256, family revocation; export download tokens `token_urlsafe(32)` resolved via UNIQUE index (DB b-tree compare is not constant-time, but 256-bit random tokens make timing search infeasible) | Sound. |
| Outbox pending scan pattern | partial index + `SKIP LOCKED` + lease, claim transaction holds no domain locks | Sound (growth/retention is DSA-6). |
| Batch jobs | `batch_runner.run_in_batches` — id-keyset batches, one short transaction each, `FOR UPDATE SKIP LOCKED` in the arrears scan, payload lists bounded by batch count | Sound; no unbounded accumulation found in workers (export `rows` is DSA-4, bounded by the row cap). |
| Schedule/leg insert loops | `disburse_loan` inserts ≤120 schedule rows and ≤7 ledger legs as individual statements | Low — executemany batching is a micro-optimisation; bounded counts, inside one transaction. |
| In-Python vs in-SQL aggregation | statement opening balance groups in SQL and applies direction in Python via `member_direction` | Deliberate (single source of truth for DR/CR); keep. |

---

## 3. Priority summary

| ID | Item | Priority | Remediation |
|---|---|---|---|
| DSA-1 | NPL trend month rescans | High | P13.17(a) |
| DSA-2 | Trial-balance full-ledger aggregate | High | P13.17(b) |
| DSA-3 | Idempotency key growth/no purge | High | P13.17(c) |
| DSA-7 | Stage-filtered listing index | High | fixed in the `fix(review)` MR (0014) |
| DSA-4 | Export in-memory buffering / bytea artifacts | Medium | P13.17(d) + ADR |
| DSA-5 | Statement opening-balance rescans | Medium | with P13.17(b), else deferred |
| DSA-6 | Outbox retention / lease batching / tenant sweep | Medium | P13.17(e) |
| — | Advisory-key collisions, cursor signing, insert batching | Low | accepted, documented above |
