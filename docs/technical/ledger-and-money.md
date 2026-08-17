# Ledger and money

How money moves and is recorded. The pure posting rules live in
`backend/src/genesis/domain/ledger.py`; money rounding has exactly one
primitive (`backend/src/genesis/domain/money.py:to_cents`). Money is KES as
`NUMERIC(18,2)` / `Decimal` — never float, never client-side arithmetic.

## 1. Double-entry model

- Every transaction is a **balanced set of debit/credit legs**
  (`PostingSpec.assert_balanced()`); a DB trigger enforces DR == CR again at
  insert time.
- The ledger is **append-only**: UPDATE and DELETE on `ledger_entries` are
  forbidden by trigger. Corrections are **reversing entries**
  (`build_reversal_posting` flips each leg's side), never edits.
- Values derived from balances **over a period** (deposit interest,
  dividends, averages) are reconstructed from the posting history under the
  account row lock — never from a point-in-time balance snapshot (a snapshot
  basis is a known exploit: park funds on the measurement day, withdraw the
  next). Period-recognising postings carry `occurred_at` at the **end** of
  the period so they sort after the period's real activity and compound into
  the next period's basis.

### Chart of accounts (code-owned, `domain/ledger.py:Account`)

| Class | Accounts |
|---|---|
| Asset | `cash.mpesa`, `cash.bank`, `loans.receivable`, `interest.receivable` |
| Liability | `member.deposits`, `liability.unclaimed_dividends` |
| Equity | `member.shares` |
| Income | `income.interest`, `income.penalties`, `income.fees`, `income.bad_debt_recoveries` |
| Expense | `interest.expense`, `expense.dividends`, `expense.rebates`, `expense.loan_writeoffs` |
| Clearing | `clearing.share_transfers`, `suspense` (exceptional items only — never a structural leg) |

Every account has a code-owned financial-statement class
(`ACCOUNT_CLASS`); report builders fail loudly on an unmapped account, so a
future account can never silently vanish from an income statement.

## 2. Code-owned posting builders

Routes never assemble ledger legs; each transaction type has exactly one
pure factory in `domain/ledger.py`:

| Builder | Legs | Reference |
|---|---|---|
| `build_deposit_posting` | DR cash / CR member.deposits | `MP-` (M-Pesa) or `BK-` (bank) |
| `build_withdrawal_posting` | DR member.deposits / CR cash | `WD-` |
| `build_share_topup_posting` | DR cash / CR member.shares | `SH-` |
| `build_disbursement_posting` | DR loans.receivable / CR cash | `LN-` |
| `build_allocated_repayment_posting` | DR cash / CR penalties→interest→principal legs | `RP-` |
| `build_loan_interest_accrual_posting` | DR interest.receivable / CR income.interest | `INT-` |
| `build_deposit_interest_posting` | DR interest.expense / CR member.deposits | `INT-` |
| `build_exit_settlement_posting` | DR shares+deposits / CR loan payoff + fee + net cash | `WD-` |
| `build_dividend_distribution_posting` | DR expense legs / CR member retention account(s) | `DV-` |
| `build_unclaimed_dividend_posting` | DR expense legs / CR liability.unclaimed_dividends | `DV-` |
| `build_share_transfer_out_posting` / `..._in_posting` | member.shares ↔ clearing.share_transfers | `ST-` |
| `build_fee_posting` | DR cash / CR income.fees | `FE-` |
| `build_write_off_posting` | DR expense.loan_writeoffs / CR loans.receivable | `WO-` |
| `build_loan_recovery_posting` | DR cash / CR income.bad_debt_recoveries | `RC-` |

Channels are `mpesa`, `bank`, `accrual`, `internal`. Cash may only move on
`mpesa`/`bank`; deposits, fees and recovery receipts refuse
accrual/internal channels outright, and system accruals never post as
deposits. Reference numbers are race-safe (advisory lock + UNIQUE + retry —
never `SELECT max()+1`).

### External references

Postings on external channels (M-Pesa, bank) must carry the operator-entered
receipt reference (`api/params.py:require_external_ref`): M-Pesa codes are
exactly 10 uppercase alphanumerics; bank references are 2–40 characters,
alphanumeric with common separators, alphanumeric at both edges. References
are normalized (trim + uppercase) before a partial UNIQUE on
(tenant, channel, reference) deduplicates them — a duplicate receipt is a
409. Malformed references surface a sanitized 422 that never echoes the
submitted value.

## 3. The withdrawal-source rule

**Share capital never leaves through a withdrawal.** There is no
share-withdrawal posting builder and no share-withdrawal endpoint;
withdrawals debit `member.deposits` only. Share capital leaves a member's
position through exactly two paths:

1. **Exit settlement** — the atomic set-off posting extinguishes the share
   balance as part of the exit workflow (`domain/exits.py`,
   `application/member_exits.py`).
2. **Member-to-member share transfer** — the maker–checker transfer posts
   two member-attributed legs through `clearing.share_transfers`, which nets
   to zero inside the one atomic transaction
   (`application/` share-transfer service; see
   [data-model.md](data-model.md#5-exits-dividends-and-share-transfers)).

Additionally, withdrawable deposit funds **exclude live guarantee pledges**
(`application/transactions.py:record_withdrawal`): a guarantor can never
withdraw collateral backing someone else's application or loan. The refusal
message is deliberately generic (least disclosure); the audit row carries
the exact figures.

## 4. Penalties and interest

- **Loan interest**: reducing-balance annuity (`domain/lending.py`);
  schedule invariants are property-tested (principal parts sum exactly; the
  final installment absorbs rounding drift). Accrual posts
  DR interest.receivable / CR income.interest; interest collected in a
  repayment is recognised on receipt.
- **Arrears penalty** (`domain/lending.py:daily_penalty`): a rate quoted in
  % per month accrues in daily steps of 1/30 of the monthly figure
  ("actual/30" — every day identical regardless of month length). Rounding
  happens **per day** at the single rounding point; each day's figure is
  final the night it is claimed (idempotency claim rows) and never restated.
  A documented consequence: a sub-half-cent daily figure rounds to zero and
  never accrues — by design, not defect. Penalty basis (instalment in
  arrears vs full outstanding), rate and grace days come from tenant
  settings only.
- **Repayment allocation**: penalties → interest → principal, one code-owned
  order (`allocate_repayment`). Interest not yet due is never collected; a
  payment beyond the due buckets prepays principal; overpayment past the
  full payoff is rejected.
- **Early settlement** (`settlement_quote`): outstanding principal +
  interest already due + penalties — future interest is waived.
- **Deposit interest and dividends**: computed from the average daily
  balance reconstructed from posting legs (365/366-day convention,
  `domain/dividends.py`); each member's figure is rounded exactly once and
  declaration totals are derived as the sum of rounded per-member figures,
  so the residue is zero by construction.

## 5. Dividend flows

- One declaration per **completed** financial year (the year boundary is
  resolved server-side from the tenant's configured year-end month; a caller
  can never supply or backdate a period).
- Distribution credits each member per their stored payout preference via a
  code-owned routing map: deposit-account credit or share-capital top-up are
  implementable and route; external cash channels are not built and fall
  back honestly to the default path with the preference recorded in the
  audit row. The default capitalises the dividend into share capital and
  credits the rebate to deposits.
- A member who exited between declaration and distribution has the
  entitlement parked as an explicit `liability.unclaimed_dividends` payable
  — never silently dropped; resolution happens through correction paths as
  reversing entries.

## 6. Accounting periods

`application/accounting_periods.py` + the closed-period DB trigger: postings
cannot land in a closed period; period close is an approve-gated action.
Per-period balances and month-end portfolio snapshots are derived rollups of
the posting history (see [data-model.md](data-model.md#3-accounts-transactions-and-the-ledger)).

## 7. Idempotency keys

Every mutating endpoint accepts `Idempotency-Key`
(`backend/src/genesis/api/idempotency.py`):

- Keys are claimed atomically (`INSERT … ON CONFLICT DO NOTHING`, checked by
  rowcount) with the request hash; a replay returns the stored response
  without re-executing side effects; the same key with a different body is
  refused.
- Claims expire per `Settings.idempotency_retention_hours` (default 24h) and
  are purged by a worker.
- Tests assert idempotency by **side-effect row counts** (ledger, audit,
  outbox, claim tables), never by return values alone.

## 8. Keyset pagination and signed cursors

`backend/src/genesis/application/pagination.py` — the single cursor codec
shared by every list endpoint:

- Lists paginate by keyset (`created_at DESC, id DESC`; some registers use a
  two-band actionable-first order), hard max page size 100.
- Wire cursors are **opaque HMAC-SHA256-signed base64url tokens**: a
  key-version byte, the plaintext keyset payload, and a tag computed over a
  length-prefixed scope binding the token to one tenant and one endpoint. A
  cursor can never be read, forged, or replayed across tenants/endpoints.
- Key rotation supports a dual-version window (active + previous pair);
  anything older fails closed as a sanitized 400. Boot fails closed on
  missing/short key material (min 32 bytes), never at first decode.
- Every decode failure — bad base64, short token, wrong version, tag
  mismatch — raises the same sanitized 400 (no oracle for which check
  failed). The tag check is constant-time.
