# ADR-0008: Payment intents — M-Pesa (Daraja STK push) deposits and loan repayments

- Status: Proposed
- Date: 2026-08-19
- Deciders:

*(Numbering: 0005/0006 are claimed by the open
`docs/adr-0005-0006-security-architecture` branch and 0007 by
`docs/member-app-readiness` / !7 — this ADR takes the next free
number, 0008.)*

## Context
Issue #7: `payment_intents` does not exist in any layer. Once the
ADR-0007 member READ surface (!7) lands, the member app can show
balances but cannot move money — no deposits, no loan repayments.
This ADR is design-only; NO implementation ships with it. The
implementation MR is gated by the preconditions checklist at the end
(!7 merged; #29 attestation; #30 write-surface sequencing; #31 member
rate limits; the #2 velocity-caps interaction decided below).

Constraints that bind the design (MASTER_PROMPT §5 gates, and the
as-built code every claim below was checked against):

- The ledger is append-only double entry: every money movement posts
  balanced legs through `genesis/application/ledger.py`; corrections
  are reversing entries only — UPDATE/DELETE forbidden by DB trigger
  (gate 1.5). M-Pesa deposits already have a reference prefix (`MP-`)
  and a cash account (`Account.CASH_MPESA`) in
  `genesis/domain/ledger.py`; `Account.SUSPENSE` exists with the
  recorded doctrine "exceptional items only — never a structural leg"
  and SASRA class CLEARING (must net to zero,
  `genesis/domain/sasra.py`).
- Status changes go through a pure, code-owned transition map executed
  under the row lock — the single-gatekeeper convention
  (`genesis/domain/lending.py::loan_transition`,
  `genesis/domain/members.py::transition`,
  `genesis/application/dividends.py::_transition`), pinned by
  full-matrix transition tests (gate 1.4).
- Member identity on `/member` routes derives EXCLUSIVELY from the
  authenticated principal (`MemberAuthContext` via
  `RequireMemberPrincipal`, ADR-0007); no member id in path, query or
  body, ever. Money-relevant member acts re-verify the credential
  live-link INSIDE the transaction under the row lock (the guarantee
  consent/release precedent in `genesis/api/member.py`). Issue #30
  makes these the write-surface preconditions.
- Side-effects ride the transactional outbox only
  (`genesis/application/outbox.py::enqueue_event`, dispatcher in
  `genesis/infrastructure/outbox_worker.py` with backoff and
  dead-letter — gate 1.2). External providers live behind
  application-layer ports: the OTP delivery seam
  (`genesis/application/otp_delivery.py::OtpDeliveryPort`, adapters in
  `genesis/infrastructure/otp_delivery.py`) is the house pattern,
  enforced by the import-linter contracts in `backend/pyproject.toml`.
- Mutating endpoints are idempotent via the Idempotency-Key middleware
  (`genesis/api/idempotency.py`: atomic claim, replay scoped to
  (tenant, actor, method, path, body), expiry via
  `IDEMPOTENCY_RETENTION_HOURS`).
- No blocking I/O inside transactions holding row locks (gate 1.3,
  recorded in ADR-0003) — an HTTP call to Safaricom while holding a
  member or account row lock is forbidden.
- All configuration is environment-only
  (`docs/technical/operations.md` §3, `genesis/settings.py`); no
  literal secrets anywhere (secret-detection CI enforces).
- Threat model: a Daraja callback is UNTRUSTED input — unauthenticated,
  retried by Safaricom, spoofable by anyone who learns the URL. It may
  never be the source of truth for a ledger posting.

## Decision

### 1. Lifecycle: append-only transitions under lock, member-principal creation
A new `payment_intents` table (tenant-scoped, RLS forced as
everywhere) carries the intent lifecycle
`created → pending → confirmed | failed | expired`:

- `created` — row inserted; no Daraja call has happened yet.
- `pending` — Daraja accepted the STK push; `checkout_request_id` (and
  `merchant_request_id`) stored, UNIQUE per tenant.
- `confirmed` — rail-verified success; the ledger posting exists
  (terminal).
- `failed` — Daraja refused the submit, the member cancelled/failed
  the PIN prompt, or verification found an anomaly (terminal).
- `expired` — pending past TTL with no rail-confirmed transaction
  (terminal).

The transition map is a pure function
`genesis/domain/payments.py::payment_intent_transition` — the single
gatekeeper, house convention, pinned by a full-matrix transition test.
Allowed edges: `created→pending`, `created→failed`, `pending→confirmed`,
`pending→failed`, `pending→expired`. Nothing re-opens a terminal state:
money discovered AFTER expiry is a reconciliation break handled through
the suspense path (§4), never a resurrection (the
`loan_transition` WRITTEN_OFF doctrine, applied here). Every transition
executes under `SELECT … FOR UPDATE` on the intent row, appends a row
to an append-only `payment_intent_events` table (state, reason,
occurred_at — fenced by the same no-UPDATE/DELETE trigger pattern as
`ledger_entries`) and writes `record_audit` in the same transaction,
so the full history is reconstructible and tamper-evident.

Creation is a member-principal act:
`POST /member/payment-intents` on the existing `/member` router,
gated by `RequireMemberPrincipal`, member id derived ONLY from
`MemberAuthContext` (the ADR-0007 rule — the request body carries
amount, purpose `deposit | loan_repayment`, and for repayments a
`loan_id` whose ownership is verified against the principal inside the
query; there is NO member id field, `extra="forbid"` per the
`MemberActBody` precedent). The credential live-link is re-verified
inside the transaction for this money-relevant write (#30
precondition 2). The MSISDN for the STK push comes from the member's
stored, KYC-verified phone — never from the request body (a
body-supplied MSISDN would let a hijacked session bill an arbitrary
phone, and would break callback verification below).

Because gate 1.3 forbids provider I/O under row locks, the flow is
three short transactions, not one: (a) insert intent as `created`,
commit; (b) call Daraja (adapter, §2); (c) transition
`created→pending` with the stored `checkout_request_id`, or
`created→failed` on refusal. A crash between (a) and (c) leaves a
`created` row that the reconciliation job (§6) expires — no lost or
half-open state.

### 2. Daraja adapter behind the OTP-seam pattern
A payment-rail port mirrors the OTP delivery seam exactly:

- `genesis/application/payment_rail.py` — a pure `typing.Protocol`
  (`PaymentRailPort`) with `initiate_stk_push(...)` and
  `query_transaction_status(...)`; no I/O, no framework imports.
- `genesis/infrastructure/daraja.py` — the concrete adapter: OAuth
  token acquisition/caching, request signing (shortcode + passkey
  password), sandbox/production base URL from settings, timeouts, and
  a recorded-fixture fake for tests (§8). It MUST never log the
  MSISDN unmasked or any credential (gate 1.6, the
  `LoggingOtpChannelProvider` masking precedent).
- Enforcement: the existing import-linter contracts already forbid
  `genesis.application` → `genesis.infrastructure`;
  `genesis.infrastructure.daraja` is added to the forbidden list of
  the "Request handlers never call providers directly" contract in
  `backend/pyproject.toml`, so only application services (invoked with
  the adapter injected, the `default_otp_delivery()` composition
  pattern) can reach Safaricom.

### 3. Callback handling doctrine: the callback is a HINT, never a fact
Daraja callbacks arrive on a new unauthenticated route OUTSIDE
`/member` (e.g. `POST /payments/daraja/callback/{callback_token}`).
Doctrine:

- **Capability URL**: the registered `CallBackURL` embeds a
  single-use, unguessable per-intent token (stored hashed on the
  intent row). Unknown or already-consumed token → acknowledge 200 and
  drop, emitting a `payment_intent.callback_rejected` security event —
  never an error body that helps an attacker enumerate.
- **Dedupe on `CheckoutRequestID`**: the intent is looked up by
  (tenant, checkout_request_id) — UNIQUE — and locked FOR UPDATE. If
  the intent is already terminal, the callback is a Safaricom retry or
  a replay: acknowledge 200, change nothing (idempotency layer 1, §5).
- **Server-side verification against the STORED intent**: amount,
  MSISDN and account reference from the callback are compared against
  the stored intent row. The STK amount was set server-side from the
  intent, so ANY mismatch is an anomaly (defect or forgery), handled
  per §4 — never silently accepted.
- **Transaction-status query above a threshold**: above the
  configurable `PAYMENT_CONFIRM_QUERY_THRESHOLD` (env-only, §3 of
  operations.md), a successful callback is NOT sufficient — the
  application service must confirm via the adapter's
  `query_transaction_status` (Daraja Transaction Status /
  STK query API) before any ledger posting. Below the threshold the
  field-by-field match against the stored intent suffices (cost/latency
  trade-off, bounded loss).
- **Never post from callback fields**: the ledger posting always uses
  the STORED intent's amount, member and loan — the callback (and even
  the status-query response) only *permits* the posting; it never
  *parameterises* it. The M-Pesa receipt number
  (`MpesaReceiptNumber`) is captured into the posting's
  `external_ref` (migration 0043) — it is the matching key the EOD
  reconciliation (#9) exists to use.

### 4. Amount mismatch: suspense account, not reject, not partial-post
Evaluated:

- **Reject (do not post)** — rejected. If the rail says money arrived
  (status-query-verified), refusing to post guarantees books-vs-rail
  divergence: the member's cash sits in the paybill while the ledger
  shows nothing, and #9's EOD recon would flag a statement-only break
  every day until someone hand-posts it.
- **Partial-post (post the rail-verified amount straight to the
  member)** — rejected. For STK push the amount is fixed server-side,
  so a mismatch is BY CONSTRUCTION an anomaly; auto-crediting the
  member launders anomalous data into a normal-looking `MP-`/`RP-`
  posting and buries the security signal.
- **Suspense account** — **chosen**. Money whose arrival is
  rail-verified but whose details disagree with the stored intent is
  posted `DR cash.mpesa / CR suspense` for the RAIL-VERIFIED amount
  (a new `build_suspense_intake_posting` in `genesis/domain/ledger.py`;
  `Account.SUSPENSE` already exists for exactly this — "exceptional
  items only — never a structural leg"). The intent transitions
  `pending→failed` with reason `amount_mismatch`, a
  `payment_intent.mismatch` security event is emitted, and the item
  enters the staff break-resolution queue (one framework with #9's
  break management).

Double-entry treatment and clearing: suspense is SASRA class CLEARING
and must net to zero in a healthy book. It clears only by staff
resolution, append-only:

- resolved as a legitimate member deposit/repayment →
  `DR suspense / CR member.deposits` (member-attributed
  reclassification posting; net effect across the pair equals a
  deposit), with the loan-servicing update applied through
  `record_repayment`'s allocation logic where applicable;
- resolved as refund-to-payer → `DR suspense / CR cash.mpesa` when the
  M-Pesa reversal is executed;
- wrongly-parked items → the EXISTING correction paths (reversing
  entries only, `genesis/application/corrections.py`), never
  UPDATE/DELETE.

Unreconciled suspense aging surfaces on the dashboard alongside #9's
break queue.

### 5. Idempotency at three layers
1. **Member intent-creation retry**: `POST /member/payment-intents`
   requires `Idempotency-Key` and rides the existing
   `IdempotencyMiddleware` unchanged — a mobile retry replays the
   stored response; a concurrent duplicate gets 409 (#30
   precondition 3).
2. **Callback replay**: dedupe on the UNIQUE
   (tenant, checkout_request_id) + the transition-under-lock — a
   replayed callback finds a terminal intent and no-ops with 200
   (Safaricom stops retrying; nothing posts twice). The ledger posting
   happens in the SAME transaction as the `pending→confirmed`
   transition, so "posted" and "confirmed" cannot diverge.
3. **Reconciliation re-poll**: the recon job (§6) claims intents
   `FOR UPDATE SKIP LOCKED` (the dividends/dormancy scan convention),
   so a callback landing mid-poll serialises behind the same row lock
   and one of the two observes the terminal state and stands down.
   Exactly-once posting per intent holds by construction: the
   transition function is the only gate to `confirmed`, and the
   intent row stores the resulting `transaction_id` (UNIQUE) as the
   final safety net — the `(tenant_id, txn_ref)` UNIQUE pattern from
   `ledger.py::_next_ref`.

### 6. Reconciliation job for pending-past-TTL intents
A new one-shot cron entrypoint (`backend/scripts/cron_payment_recon.py`
+ `genesis/infrastructure/payment_recon_worker.py`) follows the
dormancy-worker deployment pattern: per-worker advisory lock via
`genesis/infrastructure/cron_lock.py`, active-tenant walk via
`list_active_tenants`, per-tenant fail-closed isolation (one tenant's
error never blocks the rest). Each cycle claims intents pending past
`PAYMENT_INTENT_TTL_SECONDS` (SKIP LOCKED) and queries the Daraja
status API:

- rail-confirmed success → same confirm-and-post path as the callback
  (§3 verification included);
- rail-confirmed failure/cancel → `pending→failed`;
- unknown/not-found past the hard TTL → `pending→expired`;
- any disagreement (amount mismatch, confirmed-after-expiry money) →
  suspense path (§4) + a `payment_intent.recon_mismatch` security/ops
  event through the outbox.

Those events are exactly the detective-control feed #1 wants and the
metric surface #4 wants (recon backlog depth, oldest pending age,
mismatch count → alerting); this job is the intent-side source that
dovetails into #9's EOD statement recon — one break-management
framework, two sources, matched on `external_ref`.

### 7. Ledger posting: the existing MP-/RP- services only
Confirmation posts EXCLUSIVELY through the existing posting services —
no new posting path for the happy path:

- Deposits: `genesis/application/transactions.py::record_deposit`
  (member row FOR UPDATE with status gating via `member_may`, deposit
  account row lock, `ledger.post_deposit` →
  `build_deposit_posting` `DR cash.mpesa / CR member.deposits`,
  `MP-` reference via `ref_prefix`, dormant reactivation in the same
  transaction, `ledger.deposit_posted` outbox event, audit row).
- Loan repayments: `genesis/application/loans.py::record_repayment`
  (loan row FOR UPDATE, penalties→interest→principal allocation,
  `ledger.post_allocated_repayment` with split legs, `RP-` reference,
  repayments row, schedule/balance update, close + guarantee release
  on payoff, `ledger.repayment_posted` outbox event).

Both are invoked with `channel=Channel.MPESA` and
`external_ref=MpesaReceiptNumber`, with `actor_id=None` (system
posting — the acting principal is the member credential, which is
recorded on the intent row and its events, not impersonated as a
staff actor). The only NEW posting builders are the exceptional
suspense intake/clear pair (§4).

### 8. Sandbox-first test strategy
- All development and CI against the Daraja SANDBOX; recorded
  request/response fixtures (JSON, secrets and MSISDNs scrubbed)
  committed under `backend/tests/fixtures/daraja/` and replayed
  through a fake transport in the adapter tests — CI makes no network
  calls (the sandbox is proxy-blocked in the agent environment anyway;
  operations.md §4).
- Mandatory adversarial tests before the implementation MR merges:
  callback replay (double-submit), forged callback on a guessed URL,
  amount-mismatch → suspense, confirm-after-expiry → suspense not
  resurrection, cross-tenant `checkout_request_id` probe, concurrent
  callback-vs-recon race, full-matrix transition test.
- Production credentials (`DARAJA_CONSUMER_KEY`,
  `DARAJA_CONSUMER_SECRET`, `DARAJA_PASSKEY`, `DARAJA_SHORTCODE`,
  `DARAJA_BASE_URL`, plus `PAYMENT_INTENT_TTL_SECONDS` and
  `PAYMENT_CONFIRM_QUERY_THRESHOLD`) are environment-only per
  operations.md §3 / `genesis/settings.py`; no production credential
  exists anywhere until the sandbox suite is green.

### 9. Preconditions gating the implementation MR
The implementation MR may not open until every box is checked:

- [ ] **!7 merged** — the ADR-0007 member read surface (and its
  declared merge order !1 → !2 → !3 → !5 → !7) is on `develop`.
- [ ] **#29 decided** — device attestation for member money writes:
  enforcement mode chosen (`off`/`log-only`/`enforce`); intent
  creation ships behind at least `log-only`.
- [ ] **#30 honoured** — the write-surface preconditions (principal-
  derived identity, in-transaction live-link re-check, Idempotency-Key
  on every POST) are restated in the implementation MR's DoD.
- [ ] **#31 landed** — per-credential member rate limits exist;
  `POST /member/payment-intents` gets its own (stricter) bucket — every
  creation is an SMS-adjacent STK prompt to a real phone.
- [ ] **#2 interaction decided (here)**: the per-member daily
  WITHDRAWAL velocity caps do NOT apply — a deposit/repayment is
  money-in and reduces institutional risk (the same reasoning that
  lets arrears and dormant members deposit in `record_deposit`).
  Money-in gets its OWN controls instead: the #31 creation rate limit,
  a configurable per-member daily intent-creation cap, and a
  configurable maximum single-intent amount (≤ the M-Pesa per-txn
  limit) — an AML/abuse brake, not a liquidity control.

## Alternatives considered
- **Post to the ledger directly from the callback** — rejected: the
  callback is unauthenticated retried input; this is the canonical
  mobile-money fraud hole (spoofed callback = free money).
- **A new posting path for M-Pesa confirmations** — rejected:
  reuse-first; `record_deposit`/`record_repayment` already own the
  locks, status gates, allocation, audit and outbox events; a second
  path would fork the money doctrine.
- **Synchronous STK initiation inside the intent-creation
  transaction** — rejected: provider I/O under a row lock (gate 1.3);
  a slow Safaricom call would hold member/account locks hostage.
- **Reject or partial-post on amount mismatch** — rejected in §4.
- **C2B validation/confirmation URLs (paybill listener) instead of
  intent-first STK push** — deferred: C2B accepts unsolicited
  payments, which means EVERY payment starts life as the unmatched
  case; intent-first STK inverts that so matching is the norm.
  Unsolicited-payment support can layer onto the same suspense +
  recon framework later.
- **Reusing `IdempotencyMiddleware` for callback dedupe** — rejected:
  Safaricom sends no Idempotency-Key; dedupe must key on the rail's
  own identifier (`CheckoutRequestID`) under the intent row lock.

## Consequences
Positive: the member app gains its first write with the full house
doctrine intact (append-only transitions, reuse-first posting,
outbox-only side effects, three-layer idempotency); recon mismatches
become first-class security/ops events feeding #1/#4/#9 instead of
silent drift; the suspense path makes "the rail and the ledger
disagree" a visible, ageing, staff-resolvable state.

Negative: three new moving parts (adapter, callback route, recon
worker) and a second cron one-shot to watch (#4's dead-man switch
applies); suspense introduces a balance that MUST be watched to zero
(dashboard + SASRA C* class); the status-query threshold adds a config
knob whose mis-setting trades cost against verification depth.

Migration path: expand-only — new tables (`payment_intents`,
`payment_intent_events`), new settings, one new import-linter entry;
no existing table or posting builder changes; the suspense
intake/clear builders are additive. Rollback: remove the routes,
worker and tables; ledger rows already posted are permanent history
(append-only), which is correct — they represent money that really
moved.
