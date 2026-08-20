# ADR-0010: Member money-OUT — withdrawals (M-Pesa B2C / bank), internal and member-to-member transfers, beneficiary registry, name enquiry

- Status: Proposed
- Date: 2026-08-20
- Deciders:

*(Numbering: 0008 is contested by three open MRs (!18 JWT/EdDSA,
!19 payment intents, !20 approval engine — first-to-merge keeps it)
and 0009 by two (!22 AML/CFT program, !23 hosting-exit target
architecture). This ADR deliberately takes **0010** to stay clear of
both races. Concurrent design sessions are assigned **0011**
(notifications read model, #45) and **0012** (member auth factors,
#46); this document does not take those numbers. Verified before
writing: no open branch or MR claims `docs/adr/0010-*`.)*

## Context

Issue #44, from the P17 capability register (#43, T3.1). The member-app
catalogue's Withdraw / Transfer / Send groups are the single riskiest
expansion of the platform and have no design. This is money leaving the
SACCO **on a member's sole authority** — a categorically different
fraud profile from ADR-0008's money-IN (STK push), where the worst
callback forgery yields a false credit that reconciliation catches.
Here the worst case is unrecoverable: cash paid to an attacker's phone.

This ADR is design-only. NO code, NO schema, NO migration ships with
it. Implementation is gated by the precondition checklist in §9.

Constraints that bind the design (MASTER_PROMPT §5 gates and the
as-built code):

- Append-only double-entry ledger (`genesis/domain/ledger.py`;
  UPDATE/DELETE forbidden by trigger; corrections are reversing
  entries only). Withdrawals already have a posting builder
  (`build_withdrawal_posting`, `DR member.deposits / CR cash`, `WD-`
  reference) and a posting service
  (`genesis/application/transactions.py::record_withdrawal`) that
  owns the member `FOR SHARE` → deposit-account `FOR UPDATE` lock
  chain, the guarantee-pledge exclusion (withdrawable funds exclude
  live guarantee pledges) and the share-capital rule (**share capital
  never leaves through a withdrawal**; deposits only).
- Status changes go through pure, code-owned transition maps executed
  under the row lock — the single-gatekeeper convention. !17's
  `withdrawal_holds` state machine follows this convention and is the
  as-built home for "money earmarked to leave but not yet gone".
- Member identity on `/member` routes derives EXCLUSIVELY from the
  authenticated principal (`MemberAuthContext`, ADR-0007 rule); no
  member id in path/query/body; money-relevant acts re-verify the
  credential live-link INSIDE the transaction under the row lock.
- Side-effects ride the transactional outbox only; external providers
  live behind application-layer Protocol ports (the `OtpDeliveryPort`
  seam and ADR-0008's `PaymentRailPort` precedent), enforced by
  import-linter.
- Mutating endpoints are idempotent via `Idempotency-Key` middleware;
  no blocking provider I/O inside transactions holding row locks
  (gate 1.3 / ADR-0003); all configuration env-only; no client-side
  money math, ever.
- Threat model: a Daraja **B2C result callback** is exactly as
  untrusted as ADR-0008's STK callback — unauthenticated, retried,
  spoofable — and the ADR-0008 doctrines (capability URL, rail-id
  dedupe, verify-against-stored-row, suspense for
  rail-confirmed-but-mismatched) are **mirrored here, not
  reinvented**. What money-OUT adds on top: SIM-swap account takeover,
  beneficiary-injection, enumeration via name enquiry, and the
  irreversibility of a paid-out B2C.

The decision sections below follow issue #44's seven decision areas in
order; they are the binding table of contents.

## Decision

### 1. Rails: B2C behind a payout port; bank deferred; internal is pure ledger

**Withdraw-to-M-Pesa** uses Daraja **B2C** — a separate API from STK
push, with a different shape: an initiator credential +
`SecurityCredential` (certificate-encrypted initiator password), a
per-request `OriginatorConversationID`, TWO callback URLs (`ResultURL`
and `QueueTimeOutURL`), per-transaction B2C tariffs, and — decisively —
**float-account custody**: B2C pays out of the organisation's
pre-funded utility/float balance held at Safaricom. The adapter lives
behind a new application-layer Protocol port,
`genesis/application/payout_rail.py::PayoutRailPort`
(`initiate_b2c(...)`, `query_transaction_status(...)`), a sibling of
ADR-0008's `PaymentRailPort` — the same seam pattern, a separate port
because the credential set, request shape and failure modes are
disjoint. Concrete adapter in `genesis/infrastructure/daraja_b2c.py`,
added to the import-linter forbidden list so routes can never reach it
directly. Float custody consequences: a payout can fail for
**insufficient float** (an institutional condition, not a member
error — typed refusal, ops alert via outbox, never disclosed to the
member as anything but "temporarily unavailable"); float level and
B2C tariff charges are reconciled from the Daraja/statement side by
the #9 EOD framework, not estimated per transaction.

**Withdraw-to-bank**: **deferred — no member-initiated bank rail ships
under this ADR.** No bank API partner is selected (Pesalink via a
partner bank vs manual EFT batch is an institutional procurement
decision, an open question in §11). Bank withdrawals remain the
existing staff-recorded flow (`WD-`, channel `bank`, operator-entered
external reference). The `PayoutRailPort` seam is designed so a bank
adapter slots in later without touching the control chain.

**Internal transfers** (own-accounts, and member-to-member send once
§3 clears): **pure ledger, no rail** — a single balanced journal (§7),
no adapter, no callback, no float.

### 2. Control chain: ordered, server-side, atomic with the posting

Every money-out act passes the following chain **in this order, inside
the one database transaction that also owns the posting/hold** (the
!17 pattern: member `FOR SHARE` → deposit account `FOR UPDATE`, so cap
read, day-total read, hold creation and posting are one atomic unit —
no TOCTOU). Every refusal is a typed 4xx with an in-transaction audit
row; **never a silent clamp**:

1. **!17 daily velocity cap** (`tenant_settings.daily_withdrawal_limit`)
   — the as-built check, re-used unchanged; B2C payouts and
   member-to-member sends count against the same per-member daily
   figure as staff-recorded withdrawals (one bucket for money leaving,
   or the split-rail bypass is free).
2. **!17 notice-threshold hold** — amounts above
   `withdrawal_notice_threshold` enter the `withdrawal_holds`
   `pending_notice` machine instead of executing, exactly as built.
   The member-facing READ of !17 (track pending withdrawals, view
   daily limits) is a **named follow-up after !17 merges** (#43
   T1 gap) — required for a humane UX, **not part of this ADR**.
3. **#29 device attestation in `enforce`** — money-out requires the
   attestation seam in enforce mode, not log-only (log-only is
   acceptable for money-IN per ADR-0008 §9; it is not acceptable when
   the money leaves).
4. **#31 member rate limits, stricter money-out bucket** — the
   per-credential limiter with a dedicated, tighter bucket for
   money-out creation and for name enquiry (§5).
5. **Step-up OTP per action** above a tenant-set threshold: a fresh
   OTP through the existing OTP infrastructure, **bound to the
   specific act** — the challenge commits to a hash of
   (beneficiary/destination, amount, currency) so a phished OTP cannot
   be replayed against a different payout. Verified inside the
   transaction.
6. **Cooling-off window for NEW beneficiaries** (§4): first send to a
   newly added or edited beneficiary is delayed by a tenant-set window
   (default 24h) **or** amount-capped to a tenant-set first-send limit
   during the window. This is the industry-standard SIM-swap defence:
   the attacker who controls the SIM can pass OTP step-up; the
   cooling-off makes the drain slow enough for the real member's
   "beneficiary added" notification to matter.
7. **#8 maker-checker** above tenant-set thresholds: the payout enters
   the approval engine as a pending act; staff approval executes
   through the same chain (caps re-checked at execution time, the !17
   convention).

The chain is evaluated server-side only. Client-side pre-checks are
UX sugar and gate nothing.

### 3. AML/CFT (#10, POCAMLA): what is blocked until the program exists

Member-initiated outbound transfers — and member-to-member send above
all — create screening and reporting obligations under POCAMLA that
this codebase currently has no program for. Ruthless default, stated
plainly: **member-to-member send DOES NOT SHIP before #10's AML/CFT
program exists.** Not behind a feature flag, not in a pilot tenant — a
flag is not a compliance program.

What #10 must supply before member-to-member (and what this ADR
consumes, not designs): sanctions-screening timing (this ADR fixes the
hook points: at beneficiary add/edit AND pre-posting at each send,
fail-closed — screening unavailable means the send refuses), threshold
reporting (CTR thresholds and aggregation rules per #10's program; the
day-total machinery !17 already maintains is the aggregation
substrate), STR workflow, and the compliance-officer audit trail (the
in-transaction audit rows this ADR mandates everywhere are written FOR
that reader: full figures, screening outcome, device/attestation
context — audit rows carry what refusal messages must not).

Withdraw-to-own-M-Pesa (KYC-verified own phone, §4) is lower risk and
may ship on the control chain of §2 once the §9 preconditions clear —
but its threshold-reporting hooks are still built in from day one so
#10's program plugs in without re-plumbing.

### 4. Beneficiary registry

A design for a tenant-scoped, RLS-forced `beneficiaries` registry
(schema ships with the implementation MR, not here):

- **Types**: own-M-Pesa (exactly one, seeded from the KYC-verified
  member phone, not freely editable — changing it is a KYC act through
  staff review, the #30 identity-fields rule), external M-Pesa MSISDN,
  and (once §3 clears) internal member — stored as an opaque internal
  id resolved via name enquiry (§5), never a raw member number typed
  free-form at send time.
- **Step-up on add/edit**: every add or edit is OTP-step-up-gated
  (§2.5) and resets the cooling-off clock. An EDIT is as dangerous as
  an add (swap the MSISDN under a trusted label) — same treatment.
- **Cooling-off**: `pending_activation → active` after the tenant-set
  window; first-send cap during the window (§2.6). Timestamped
  server-side; the state lives in the DB, not the app.
- **Per-member count cap**: tenant-set (sane default, e.g. 10). A cap
  keeps the registry reviewable by its owner and bounds the blast
  radius of a takeover.
- **In-transaction audit rows** on add/edit/remove/first-use, carrying
  the full before/after and the acting credential + device context.
- **Deletion never orphans pending transfers**: removal is a
  soft-deactivation (`active → removed` transition under lock);
  a beneficiary with any live reference (pending hold, in-flight
  payout, maker-checker-pending send) refuses removal with a typed
  409 until those settle. Pending transfers always resolve against
  the beneficiary row **as it was at initiation** (the hold stores the
  resolved destination, not a re-read at execution).
- Every add/edit/remove emits a member-notification outbox event
  ("beneficiary added to your account") — the cooling-off window (§2.6)
  is what makes that notification actionable.

### 5. Name enquiry: masked confirm, never an oracle

"Verify recipient before sending" is an enumeration oracle unless
designed. Decision:

- **Masked rendering only**: first name + initial(s) (e.g. "WANJIKU
  K."), rendered server-side; never the full legal name, never account
  status, balance or phone.
- **Rate-limited per credential** in the stricter #31 money-out bucket,
  with a low daily enquiry budget per member.
- **Step-up-gated**: enquiry is only reachable inside an authenticated,
  step-up-cleared beneficiary-add or send flow — never a standalone
  free-lookup endpoint.
- **No existence oracle beyond the masked confirm**: an unknown,
  closed, blocked or non-consenting account returns the SAME generic
  "cannot verify this recipient" shape and comparable latency as any
  other failure — the only positive signal that exists is the masked
  name of a real, sendable account, and only inside a flow that has
  already paid step-up + rate-limit cost.
- **Full audit**: every enquiry (hit or miss) writes an in-transaction
  audit row (who asked, what was asked, what was disclosed); repeated
  misses feed the #1 anomaly stream as an enumeration signal.

### 6. Idempotency ×3 and reconciliation: ADR-0008 mirrored, with the money-OUT inversion

The ADR-0008 doctrines carry over verbatim with B2C identifiers; what
changes is the **order of money and rail**:

**Debit-before-rail (the inversion).** Money-in posts after the rail
confirms. Money-out must earmark funds BEFORE the rail is called, or
two in-flight payouts can spend the same balance. The payout lifecycle
(pure transition map under `FOR UPDATE`, house convention) is
`created → held → submitted → paid | failed | expired`:

- `created→held`: the §2 control chain passes and funds are earmarked
  **through the !17 `withdrawal_holds` machine** (extended with a
  payout hold kind — reuse, not a parallel earmark), all in one
  transaction. Held funds are excluded from withdrawable balance.
- `held→submitted`: the B2C call happens OUTSIDE any row lock
  (gate 1.3 — three short transactions, the ADR-0008 §1 pattern);
  the stored `OriginatorConversationID` (generated by us, UNIQUE per
  tenant) and the returned `ConversationID` are persisted.
- `submitted→paid`: **only** on rail-verified success; the ledger
  posting (§7) executes in the SAME transaction as the transition —
  "posted" and "paid" cannot diverge.
- `submitted→failed` / `held→failed`: rail refusal, timeout-callback
  with a verified not-processed status, or control failure — the hold
  is released in the same transaction.
- `expired`: pending past TTL with no rail-confirmed outcome, set only
  by the recon job after a status query. Terminal states never
  re-open; late-arriving rail truth goes through suspense, never
  resurrection (the ADR-0008 §1 doctrine).

**Idempotency layer 1 — member retry**: `Idempotency-Key` required on
the creation endpoint, existing middleware unchanged.

**Layer 2 — result/timeout callbacks are untrusted retried input**
(ADR-0008 §3 mirrored): capability-URL token per payout (hashed at
rest; unknown/consumed token → 200-and-drop + security event); dedupe
on the UNIQUE (tenant, `OriginatorConversationID`) under the row lock —
a replayed Result for a terminal payout no-ops with 200; **server-side
verification against the STORED payout row** (amount, MSISDN,
receiver-party fields); above a configurable threshold a success
Result is NOT sufficient — the Transaction Status API must confirm
before the posting; **the posting is never parameterised by callback
fields** — the callback permits, the stored row parameterises. The
`QueueTimeOutURL` callback is a HINT of unknown status, never a fact:
it triggers a status query, **never a re-submit** — a duplicate B2C is
unrecoverable money.

**Layer 3 — reconciliation re-poll**: a cron one-shot
(cron_lock + active-tenant walk + `FOR UPDATE SKIP LOCKED`, the
dormancy/ADR-0008 recon pattern) claims payouts stuck past TTL and
queries the status API: verified paid → same confirm-and-post path;
verified failed → release hold; unknown past hard TTL → `expired` +
ops event (a human decides — the money may still move). A callback
landing mid-poll serialises behind the same row lock.

**Suspense doctrine** (ADR-0008 §4 mirrored for the outbound
direction): rail-confirmed-but-mismatched — the rail proves money left
the float but disagrees with the stored payout (amount differs, paid
after expiry, paid with no matching row) — posts the RAIL-VERIFIED
amount `DR suspense / CR cash.mpesa` (the outbound mirror of
ADR-0008's intake), emits a security/ops event, and enters the staff
break-resolution queue; suspense is SASRA class CLEARING and must be
watched to zero. Never silently accepted, never auto-attributed to the
member. Recon events feed #1 (detective controls), #4 (metrics) and
match into #9's EOD statement recon on `external_ref` — one break
framework, as with money-in.

### 7. Ledger: existing withdrawal services only; internal transfer is ONE journal

- **No parallel posting path.** B2C and bank withdrawals post
  exclusively through the existing withdrawal machinery
  (`record_withdrawal` → `build_withdrawal_posting`,
  `DR member.deposits / CR cash.mpesa|cash.bank`, `WD-` reference,
  channel `mpesa`/`bank`, `external_ref` = the M-Pesa B2C
  `TransactionID`/receipt), inheriting the guarantee-pledge exclusion
  and the share-capital rule for free. The only NEW builders are the
  additive pair for the outbound suspense doctrine (§6) and the
  internal-transfer journal below.
- **Internal transfer = a single balanced journal, never two
  half-postings.** One new pure builder (e.g.
  `build_internal_transfer_posting`: `DR sender member.deposits /
  CR recipient member.deposits`, one new reference prefix, channel
  `internal`) posted in ONE transaction with both member rows locked
  in the canonical lock order (the lock-order DAG doc governs; sorted
  key order to make deadlock impossible). There is no instant where
  the books show money in zero or two places. A "transfer" implemented
  as a withdrawal posting plus a deposit posting is forbidden — two
  half-postings create an unbalanced window and two references for one
  economic event.
- Fees, if a tenant charges them for money-out, post through the
  existing `build_fee_posting` (`FE-`) in the same transaction — no
  new fee path.

### 8. Named adversarial tests for the implementation MR

Mandatory before any implementation MR merges (extending !17's and
ADR-0008 §8's suites, not replacing them):

1. **SIM-swap beneficiary race**: attacker with SIM control passes
   OTP, adds a beneficiary, immediately attempts maximum drain →
   cooling-off/first-send cap refuses; edit-then-send within the
   window equally refused; the "beneficiary added" notification event
   is emitted in-transaction.
2. **B2C result callback forgery/replay**: forged Result on a
   guessed/expired capability token → 200-and-drop + security event,
   no state change; replayed genuine Result → exactly one posting,
   one terminal transition; Result-vs-QueueTimeout race → status query
   arbitrates, never a re-submit.
3. **Limit-raise-then-drain**: a raised tenant cap (or a compromised
   staff raise) followed by an immediate drain → the raise is a
   maker-checker staff act and caps are re-checked at execution time
   inside the posting transaction; a hold created under the old cap
   does not execute over the new day-total.
4. **Name-enquiry enumeration probe**: scripted walk across
   account/MSISDN space → rate-limit bucket exhausts, responses stay
   shape- and latency-uniform for all misses, audit rows accumulate,
   anomaly event fires.
5. **Concurrent cap-straddling withdrawals**: two simultaneous
   money-out acts (mixed rails: one B2C, one internal) straddling the
   daily cap → exactly one succeeds (the !17 test generalised across
   rails and across holds-plus-postings).

Plus the mirrored ADR-0008 set: cross-tenant
`OriginatorConversationID` probe, callback-vs-recon race, full-matrix
transition test, and double-submit on the creation endpoint.

### 9. Preconditions gating any implementation MR

The implementation MR may not open until every box is checked:

- [ ] **!17 merged** — the daily velocity caps + `withdrawal_holds`
  machine is on `develop`; this design's §2 chain and §6 hold reuse
  build directly on it.
- [ ] **#29 enforce-capable** — device attestation can run in
  `enforce` mode for member money writes (log-only is insufficient
  for money-out).
- [ ] **#31 merged** — per-credential member rate limits exist, with
  the stricter money-out bucket configurable.
- [ ] **#10 AML/CFT program exists** for anything member-to-member —
  restated plainly: **member-to-member send DOES NOT SHIP before
  #10.** Withdraw-to-own-M-Pesa may proceed ahead of it only with the
  threshold-reporting hooks (§3) built in.

Named follow-up (not a precondition, not part of this ADR): the
member-facing READ routes of !17 (view daily limits, track pending
withdrawals) — the #43 T1 gap — should land alongside or before the
first money-out write so members can see the controls acting on them.

## Alternatives considered

- **Post the withdrawal before the rail confirms, reverse on
  failure** — rejected: books run ahead of reality; every rail failure
  demands a reversing entry; the member sees phantom debits; a crash
  between posting and rail call strands a debit with no payout.
- **Call the rail first, post after, with no hold** — rejected:
  classic TOCTOU — concurrent payouts spend the same balance; the
  rail pays money the ledger never earmarked. The hold-first lifecycle
  (§6) is the only ordering where funds are always in exactly one
  state.
- **A parallel earmark mechanism instead of extending !17's
  `withdrawal_holds`** — rejected: reuse-first (gate 1.1); two hold
  machines over one balance invite a straddle between them.
- **Auto-retry B2C on timeout/unknown status** — rejected: a duplicate
  payout is unrecoverable; unknown status is resolved by the
  Transaction Status query only, and past hard TTL by a human.
- **Reject (refuse to book) rail-confirmed-but-mismatched payouts** —
  rejected: the float provably shrank; refusing to book guarantees a
  books-vs-rail break every EOD until hand-posted. Suspense (§6)
  makes the anomaly visible, ageing and staff-resolvable — the
  ADR-0008 §4 reasoning, outbound.
- **Unmasked name enquiry / standalone lookup endpoint** — rejected:
  an enumeration and doxxing oracle over the member base; masked,
  step-up-gated, in-flow-only confirm (§5) is the maximum safe
  disclosure.
- **Two half-postings for internal transfer** (withdrawal + deposit) —
  rejected: unbalanced window, two references for one event, and a
  crash between the halves loses money in flight; single balanced
  journal only (§7).
- **Client-side or app-enforced limits/step-up** — rejected: no client
  money math (gate 1.1 of the catalogue contradictions, #43); every
  control in §2 is server-side and atomic with the posting.
- **Shipping member-to-member behind a feature flag before #10** —
  rejected: a flag is not a compliance program; POCAMLA obligations
  attach to the capability existing, not to its default.
- **OTP-only beneficiary activation (no cooling-off)** — rejected: OTP
  falls to exactly the SIM-swap attacker money-out must resist;
  time is the only factor the attacker cannot phish.
- **Reusing `IdempotencyMiddleware` for callback dedupe** — rejected
  (ADR-0008 precedent): the rail sends no `Idempotency-Key`; dedupe
  keys on the rail's own identifier under the row lock.
- **Bank rail in scope now** — rejected: no partner selected; a
  half-designed bank integration would dilute the B2C doctrine.
  Deferred behind the `PayoutRailPort` seam with an open question
  (§11 below).

## Consequences

Positive: money-out arrives with the full house doctrine intact —
hold-first lifecycle on the as-built !17 machine, one control chain
atomic with the posting, ADR-0008's callback/idempotency/suspense
doctrines reused rather than re-derived, and the riskiest capability
(member-to-member) explicitly fenced behind the AML/CFT program.
Refusals are typed and audited; enumeration and SIM-swap have named,
testable defences.

Negative: the member experience of money-out is deliberately slower
than the catalogue's ideal (cooling-off, step-up, notice holds,
maker-checker above thresholds) — that friction is the control
surface, and the member-facing !17 reads (follow-up) must explain it
or support will drown; float custody adds an institutional liquidity
duty (top-up discipline, float-level alerting) the SACCO does not have
today; a second Daraja credential set and two more callback routes
widen the operational surface (#4's monitoring applies).

Migration path: expand-only when implementation comes — new tables
(payout intents/events, beneficiaries), a payout hold kind on !17's
machine, new settings, one import-linter entry, two additive posting
builders; no existing table or builder changes. Rollback of the
implementation: remove routes, worker and tables; posted ledger rows
are permanent history, which is correct. Rollback of this ADR: delete
the file.

## Open questions requiring human sign-off (§11)

1. Bank rail partner selection (Pesalink via partner bank vs EFT
   batch) — procurement/institutional, blocks any member bank
   withdrawal.
2. Tenant default values: step-up threshold, cooling-off duration,
   first-send cap, beneficiary count cap, maker-checker threshold.
3. B2C tariff bearing: absorbed by the SACCO vs passed to the member
   as a `FE-` fee — a pricing/policy decision.
4. CTR/STR threshold figures and aggregation windows — owned by #10's
   program, consumed here.
