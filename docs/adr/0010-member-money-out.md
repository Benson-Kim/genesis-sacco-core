# ADR-0010: Member money-OUT — v1: M-Pesa B2C to the member's OWN verified MSISDN + own-account internal transfers; v2 designed here: beneficiary registry, name enquiry, member-to-member send

- Status: Proposed
- Date: 2026-08-20
- Deciders: issue #44 ADR pass; **v1 rail scope fixed by owner
  sign-off recorded on #44, 2026-08-20** (v1 = M-Pesa B2C to the
  member's own verified MSISDN + own-account internal transfers;
  member-to-member send, bank rails and the beneficiary registry are
  explicitly OUT of v1)

*(Numbering: this document takes **0010**, claimed by !28 before the
concurrent design sessions opened theirs; **0011** is taken by the
notifications read-model ADR (#45, !30) and **0012** by the member
auth-factors ADR (#46, !29) — verified against open branches and MRs:
no other branch or MR claims `docs/adr/0010-*`. Note that `develop`
now carries `0008-jwt-asymmetric-signing-rotation.md`, so the
payment-intents ADR open in !19 — numbered 0008 on its branch — will
need renumbering before merge; this document therefore cites it as
"the payment-intents ADR (!19, #7)" rather than by bare number.)*

## Context

Issue #44, from the P17 capability register (#43, T3.1). The member-app
catalogue's Withdraw / Transfer / Send groups are the single riskiest
expansion of the platform and had no design. This is money leaving the
SACCO **on a member's sole authority** — a categorically different
fraud profile from the payment-intents ADR's money-IN (STK push, !19),
where the worst callback forgery yields a false credit that
reconciliation catches. Here the worst case is unrecoverable: cash
paid out to an attacker's phone.

**Scope is fixed by the owner sign-off on #44 (2026-08-20).** v1
money-out rails are exactly two: M-Pesa B2C withdrawals **to the
member's OWN verified MSISDN only** (the OTP-enrolled number), and
**own-account internal transfers** (within the same member, pure
ledger). Member-to-member send, bank rails and the beneficiary
registry are OUT of v1. This ADR still DESIGNS the beneficiary
registry, name enquiry and member-to-member controls (issue points 4
and 5) — as **v2 sections with named activation triggers** — so that
v1 is built with the v2 seams in place, but nothing in v1 exposes
them.

This ADR is design-only. NO code, NO schema, NO migration ships with
it. Implementation is gated by the precondition checklist in §9.

Constraints that bind the design (MASTER_PROMPT §5 gates and the
as-built code, all verified against the tree):

- Append-only double-entry ledger (`genesis/domain/ledger.py`;
  UPDATE/DELETE forbidden by trigger; corrections are reversing
  entries only — `docs/technical/ledger-and-money.md`). Withdrawals
  already have a posting builder (`build_withdrawal_posting`,
  `DR member.deposits / CR cash`, `WD-` reference) and a posting
  service (`genesis/application/transactions.py::record_withdrawal`)
  that owns the member `FOR SHARE` → deposit-account `FOR UPDATE`
  lock chain, the guarantee-pledge exclusion (withdrawable funds
  exclude live guarantee pledges) and the withdrawal-source rule
  (**share capital never leaves through a withdrawal**; deposits
  only). External-channel postings carry a validated `external_ref`
  (M-Pesa: exactly 10 uppercase alphanumerics) deduplicated by a
  partial UNIQUE on (tenant, channel, reference).
- !17 (as-built on its branch, migration 0049 +
  `genesis/domain/withdrawals.py`): per-member **daily velocity cap**
  (`tenant_settings.daily_withdrawal_limit`) and **notice threshold**
  (`withdrawal_notice_threshold`), evaluated by the pure
  `evaluate_withdrawal` function under the account row lock — cap
  before threshold, exact boundary semantics, cap ALWAYS re-checked
  at hold execution. The `withdrawal_holds` table is the notice-state
  machine (`pending_notice → executed | cancelled`). Two as-built
  facts this ADR must design around, called out honestly: **(a)** the
  table's CHECKs assume decision and posting are simultaneous
  (`(status='executed') = (executed_txn_id IS NOT NULL)` and
  `(status='pending_notice') = (decided_at IS NULL)`) — true for a
  teller, false for an asynchronous rail where approval, the B2C
  call and the paid posting are separated in time — and its actor
  columns (`requested_by`/`decided_by`) reference `users` (staff),
  not member credentials; **(b)** !17 has **NO member-audience read
  routes** — "track pending withdrawals / view daily limits" (the
  #43 T1 gap) does not exist. §9 makes the member read surface a
  REQUIRED COMPANION of v1, and §6 decides where the async earmark
  state lives.
- Status changes go through pure, code-owned transition maps executed
  under the row lock — the single-gatekeeper convention (!17's
  `hold_transition` follows it).
- Member identity on `/member` routes derives EXCLUSIVELY from the
  authenticated principal (`MemberAuthContext`, ADR-0007 rule); no
  member id in path/query/body; money-relevant acts re-verify the
  credential live-link INSIDE the transaction under the row lock.
- Side-effects ride the transactional outbox only; external providers
  live behind application-layer Protocol ports (the `OtpDeliveryPort`
  seam and the `PaymentRailPort` precedent from the payment-intents
  ADR, !19), enforced by import-linter.
- Mutating endpoints are idempotent via `Idempotency-Key` middleware;
  no blocking provider I/O inside transactions holding row locks
  (gate 1.3 / ADR-0003); all configuration env-only; no client-side
  money math, ever.
- #46 / ADR-0012 (in flight, !29) fixes the step-up factor this ADR
  consumes: the **server-verified Argon2id PIN factor** is ADR-0012's
  named upgrade path, explicitly gated on THIS ADR shipping — it is
  the step-up control for money-out, with fresh OTP as the enrolment
  and re-proof mechanism.
- Threat model: a Daraja **B2C Result callback** is exactly as
  untrusted as the STK callback — unauthenticated, retried,
  spoofable — and the !19 doctrines (capability URL, rail-id dedupe,
  verify-against-stored-row, suspense for
  rail-confirmed-but-mismatched) are **mirrored here, not
  reinvented**. What money-OUT adds on top: SIM-swap account
  takeover, beneficiary injection (v2), enumeration via name enquiry
  (v2), and the irreversibility of a paid-out B2C.

The decision sections below follow issue #44's seven decision areas
in order; they are the binding table of contents. §0 states the cut
line first.

## Decision

### 0. The v1 cut line (binding)

| Capability | v1? | Activation trigger for later versions |
|---|---|---|
| M-Pesa B2C withdrawal to the member's **OWN verified MSISDN** (the OTP-enrolled, KYC-verified number) | **YES** | — |
| Own-account internal transfer (same member: deposits → share capital, §1/§7) | **YES** | — |
| Withdraw-to-bank (member-initiated) | **NO** | Bank integration partner selected (procurement, §11); rides the same `PayoutRailPort` seam |
| Beneficiary registry (§4) | **NO** | Ships WITH the first non-own-destination capability, never before; design frozen here |
| Name enquiry (§5) | **NO** | Ships WITH member-to-member send, never as a standalone surface |
| Member-to-member send (§3–§5) | **NO** | **HARD GATE: #10's AML/CFT program exists** — plus beneficiary registry + name enquiry + cooling-off live. Not behind a feature flag, not in a pilot tenant |

Consequences of the cut, stated so no implementation MR can blur it:
v1 has **no beneficiary concept at all** — the B2C destination is
resolved server-side from the member's stored, KYC-verified phone,
NEVER from the request body (the !19 MSISDN rule, applied outbound);
v1 has **no name enquiry** (there is no counterparty); v1's internal
transfer has **no counterparty** either (same member both sides), so
it carries the lightest AML surface and is the correct first write.
Changing the verified MSISDN is NOT a money-out act: it is a KYC
identity-field change through staff review (the #30 rule), and §2/§8
attach a payout delay to it precisely because it is the v1 analogue
of "add a beneficiary".

### 1. Rails: B2C behind a payout port; internal is pure ledger; bank deferred

**Withdraw-to-M-Pesa** uses Daraja **B2C** — a separate API from STK
push, with a different shape: an initiator credential +
`SecurityCredential` (certificate-encrypted initiator password), a
per-request `OriginatorConversationID`, TWO callback URLs
(`ResultURL` and `QueueTimeOutURL`), per-transaction B2C tariffs,
and — decisively — **float-account custody**: B2C pays out of the
organisation's pre-funded utility/float balance held at Safaricom.
The adapter lives behind a new application-layer Protocol port,
`genesis/application/payout_rail.py::PayoutRailPort`
(`initiate_b2c(...)`, `query_transaction_status(...)`), a sibling of
!19's `PaymentRailPort` — the same seam pattern, a separate port
because the credential set, request shape and failure modes are
disjoint. Concrete adapter in `genesis/infrastructure/daraja_b2c.py`,
added to the import-linter forbidden list so routes can never reach
it directly.

Float custody consequences, decided not hand-waved: a payout can fail
for **insufficient float** — an institutional condition, not a member
error — which is a typed refusal, an ops alert via the outbox, and is
never disclosed to the member as anything but "temporarily
unavailable" (least disclosure). The float level is **monitored as a
first-class control**: a float-balance gauge (from the B2C responses'
utility-balance fields and the #9 statement side), a configurable
low-water alert, and refusal-before-submit when a payout would exceed
known float — failure-mode row FM1 in §8. Float top-up discipline is
an operational duty recorded in §11. B2C tariffs are reconciled from
the Daraja/statement side by the #9 EOD framework, never estimated
per transaction.

**Own-account internal transfer**: **pure ledger, no rail** — a
single balanced journal (§7), no adapter, no callback, no float.
Direction decided by the as-built product rules: **deposits → share
capital only** (`DR member.deposits / CR member.shares`, channel
`internal`). The reverse direction (shares → deposits) is REFUSED:
the withdrawal-source rule is doctrine — share capital leaves a
member's position only through exit settlement or the maker-checker
share transfer (`ledger-and-money.md` §3) — and an internal transfer
must not become a third share-exit path.

**Withdraw-to-bank**: **deferred — no member-initiated bank rail
ships under this ADR.** No bank API partner is selected (Pesalink via
a partner bank vs manual EFT batch is an institutional procurement
decision, §11). Bank withdrawals remain the existing staff-recorded
flow (`WD-`, channel `bank`, operator-entered external reference).
The `PayoutRailPort` seam is designed so a bank adapter slots in
later without touching the control chain.

### 2. Control chain: ordered, server-side, atomic with the posting

Every money-out act passes the following chain **in this order,
inside the one database transaction that also owns the earmark/
posting** (the !17 pattern: member `FOR SHARE` → deposit account
`FOR UPDATE`, so cap read, day-total read, earmark and posting are
one atomic unit — no TOCTOU). Every refusal is a typed 4xx with an
in-transaction audit row; **never a silent clamp**:

1. **!17 daily velocity caps + notice-threshold holds** — the
   as-built pure evaluation (`evaluate_withdrawal`: cap before
   threshold, exact boundaries, cap re-checked at execution), reused
   unchanged. B2C payouts and internal transfers count against the
   SAME per-member daily figure as staff-recorded withdrawals (one
   bucket for money leaving, or the split-rail bypass is free), and
   the day-total input MUST include live in-flight payout earmarks
   (§6), not just posted rows — FM4. Amounts strictly above
   `withdrawal_notice_threshold` enter the notice state instead of
   executing (§6 decides where that state lives for the async rail).
2. **#29 device attestation in `enforce`** — money-out requires the
   attestation seam in enforce mode, not log-only (log-only is
   acceptable for money-in per !19 §9; it is not acceptable when the
   money leaves).
3. **#31 member rate limits, stricter money-out bucket** — the
   per-credential limiter with a dedicated, tighter bucket for
   money-out creation (and, in v2, for name enquiry, §5).
4. **Step-up per #46's approved factor, above a tenant-set
   threshold**: the **server-verified Argon2id PIN** (ADR-0012 §1
   upgrade path — per-credential, attempt-counter under row lock,
   lockout ≤5, constant-time compare), with fresh OTP as the
   enrolment/re-proof mechanism. The challenge is **bound to the
   specific act** — it commits to a hash of (destination, amount,
   currency) so a phished step-up cannot be replayed against a
   different payout. Verified inside the transaction.
5. **Cooling-off window for NEW beneficiaries — v2 slot** (§4): in
   v1 no beneficiary exists, so this link is dormant; its v1
   ANALOGUE is live from day one: after a staff-approved change of
   the member's verified MSISDN, B2C payouts are delayed by a
   tenant-set window (default 24h) or capped at a tenant-set
   first-payout limit during the window — the SIM-swap/
   number-change drain defence (FM5, FM7).
6. **#8 maker-checker above tenant-set thresholds**: the payout
   enters the approval engine as a pending act; staff approval
   executes through the same chain (caps re-checked at execution
   time, the !17 convention).

The chain is evaluated server-side only. Client-side pre-checks are
UX sugar and gate nothing. **Required companion (§9): the member-
facing READ of !17's controls** — view daily limits/headroom, track
pending holds and payouts — because v1 deliberately makes money-out
slower (notice holds, step-up, maker-checker), and controls a member
cannot see become support tickets and social-engineering surface.

### 3. AML/CFT (#10, POCAMLA): what is blocked until the program exists

Member-initiated outbound transfers — and member-to-member send above
all — create screening and reporting obligations under POCAMLA that
this codebase currently has no program for. Ruthless default, stated
plainly: **member-to-member send DOES NOT SHIP before #10's AML/CFT
program exists.** Not behind a feature flag, not in a pilot tenant —
a flag is not a compliance program. This is the v2 hard gate in §0.

What #10 must supply before member-to-member (and what this ADR
consumes, not designs): sanctions-screening timing — this ADR fixes
the hook points as **at beneficiary add/edit AND pre-posting at each
send, fail-closed** (screening unavailable means the send refuses);
threshold reporting (CTR thresholds and aggregation rules per #10's
program; the day-total machinery !17 already maintains is the
aggregation substrate); STR workflow; and the compliance-officer
audit trail (the in-transaction audit rows this ADR mandates
everywhere are written FOR that reader: full figures, screening
outcome, device/attestation context — audit rows carry what refusal
messages must not).

v1's two capabilities are deliberately the lightest AML surface:
B2C to the member's own KYC-verified number has no third-party
recipient, and the own-account internal transfer has no counterparty
at all. v1 still builds the threshold-reporting hooks in from day
one (per-act audit rows carrying amounts, channel, day aggregates)
so #10's program plugs in without re-plumbing.

### 4. Beneficiary registry — v2, designed now, activation-gated

**Nothing in this section ships in v1.** Activation trigger: the
first non-own-destination capability (external-MSISDN payout or
member-to-member send); member-to-member additionally requires #10
(§0). Design frozen here so v2 does not re-litigate:

- **Types**: own-M-Pesa (exactly one, seeded from the KYC-verified
  member phone, not freely editable — changing it is a KYC act
  through staff review, the #30 identity-fields rule; in v1 this is
  the ONLY destination and lives on the member record, not in a
  registry), external M-Pesa MSISDN, and (once #10 clears) internal
  member — stored as an opaque internal id resolved via name enquiry
  (§5), never a raw member number typed free-form at send time.
- **Step-up on add/edit**: every add or edit is step-up-gated (§2.4)
  and resets the cooling-off clock. An EDIT is as dangerous as an add
  (swap the MSISDN under a trusted label) — same treatment.
- **Cooling-off**: `pending_activation → active` after the tenant-set
  window; first-send cap during the window (§2.5). Timestamped
  server-side; the state lives in the DB, not the app.
- **Per-member count cap**: tenant-set (sane default, e.g. 10). A cap
  keeps the registry reviewable by its owner and bounds the blast
  radius of a takeover.
- **In-transaction audit rows** on add/edit/remove/first-use,
  carrying the full before/after and the acting credential + device
  context.
- **Deletion never orphans pending transfers**: removal is a
  soft-deactivation (`active → removed` transition under lock); a
  beneficiary with any live reference (pending hold, in-flight
  payout, maker-checker-pending send) refuses removal with a typed
  409 until those settle. Pending transfers always resolve against
  the beneficiary row **as it was at initiation** (the earmark stores
  the resolved destination, not a re-read at execution).
- Every add/edit/remove emits a member-notification outbox event
  ("beneficiary added to your account") — the cooling-off window is
  what makes that notification actionable. The notification READ
  surface is ADR-0011's (#45).

### 5. Name enquiry — v2, designed now: masked confirm, never an oracle

**Nothing in this section ships in v1** (v1 has no counterparty).
Activation trigger: ships WITH member-to-member send, i.e. behind the
#10 hard gate; never a standalone surface. "Verify recipient before
sending" is an enumeration oracle unless designed. Decision:

- **Masked rendering only**: first name + initial(s) (e.g. "WANJIKU
  K."), rendered server-side; never the full legal name, never
  account status, balance or phone.
- **Rate-limited per credential** in the stricter #31 money-out
  bucket, with a low daily enquiry budget per member.
- **Step-up-gated**: enquiry is only reachable inside an
  authenticated, step-up-cleared beneficiary-add or send flow —
  never a standalone free-lookup endpoint.
- **No existence oracle beyond the masked confirm**: an unknown,
  closed, blocked or non-consenting account returns the SAME generic
  "cannot verify this recipient" shape and comparable latency as any
  other failure — the only positive signal that exists is the masked
  name of a real, sendable account, and only inside a flow that has
  already paid step-up + rate-limit cost.
- **Full audit**: every enquiry (hit or miss) writes an
  in-transaction audit row (who asked, what was asked, what was
  disclosed); repeated misses feed the #1 anomaly stream as an
  enumeration signal.

### 6. Idempotency ×3 and reconciliation: !19 mirrored, with the money-OUT inversion

The !19 doctrines carry over verbatim with B2C identifiers; what
changes is the **order of money and rail**:

**Earmark-before-rail (the inversion).** Money-in posts after the
rail confirms. Money-out must earmark funds BEFORE the rail is
called, or two in-flight payouts can spend the same balance. The
payout lifecycle (pure transition map under `FOR UPDATE`, house
convention) lives on a new `payout_intents` table (+ append-only
`payout_intent_events`, the !19 shape):
`created → held_notice → held → submitted → paid | failed | expired`:

- `created→held` (or `created→held_notice` when strictly above the
  notice threshold): the §2 control chain passes and funds are
  earmarked, all in one transaction. Earmarked funds are excluded
  from withdrawable balance and INCLUDED in the !17 day-total input
  (FM4). `held_notice→held` is the staff notice decision (#8
  approval surface); `held_notice→failed` its refusal/cancellation.
- **Where the async earmark state lives — decided**: on
  `payout_intents`, NOT by extending !17's `withdrawal_holds` table.
  The as-built `withdrawal_holds` CHECKs bind decision and posting
  to the same instant (`executed ⇔ executed_txn_id`,
  `pending_notice ⇔ decided_at IS NULL`) and attribute actors to
  staff `users` — correct for the teller flow it was built for,
  structurally wrong for an async rail where approval, the B2C call
  and the paid posting are three separate moments. Extending it
  would mean an ALTER of !17's table (new statuses, member
  attribution) — a schema change to another MR's machinery, refused
  here. What IS reused from !17, unchanged: the pure
  `evaluate_withdrawal` decision function, both tenant settings,
  the precedence and boundary semantics, and the cap-re-check-at-
  execution convention. `withdrawal_holds` remains the staff-channel
  notice machine; `payout_intents.held_notice` is the member-channel
  notice state; the member read surface (§9) and the staff pending
  register present BOTH. The one-hold-register alternative (extend
  !17's table expand-only) is recorded as a rejected alternative and
  filed as gap work item **#47** so a human can overrule before
  implementation.
- `held→submitted`: the B2C call happens OUTSIDE any row lock
  (gate 1.3 — three short transactions, the !19 §1 pattern); the
  stored `OriginatorConversationID` (generated by us, **UNIQUE per
  tenant** — the dedupe key) and the returned `ConversationID` are
  persisted.
- `submitted→paid`: **only** on rail-verified success; the ledger
  posting (§7) executes in the SAME transaction as the transition —
  "posted" and "paid" cannot diverge.
- `submitted→failed` / `held→failed`: rail refusal, timeout-callback
  with a verified not-processed status, or control failure — the
  earmark is released in the same transaction.
- `expired`: pending past TTL with no rail-confirmed outcome, set
  only by the recon job after a status query. Terminal states never
  re-open; late-arriving rail truth goes through suspense, never
  resurrection (the !19 §1 doctrine).

**Idempotency layer 1 — member retry**: `Idempotency-Key` required
on the creation endpoint, existing middleware unchanged; a replay
returns the stored response, a concurrent duplicate gets 409.

**Layer 2 — Result/QueueTimeout callbacks are untrusted retried
input, ingested idempotently** (!19 §3 mirrored): capability-URL
token per payout (hashed at rest; unknown/consumed token →
200-and-drop + security event); dedupe on the UNIQUE
(tenant, `OriginatorConversationID`) under the row lock — a replayed
Result for a terminal payout no-ops with 200; **server-side
verification against the STORED payout row** (amount, MSISDN,
receiver-party fields); above a configurable threshold a success
Result is NOT sufficient — the Transaction Status API must confirm
before the posting; **the posting is never parameterised by callback
fields** — the callback permits, the stored row parameterises. The
`QueueTimeOutURL` callback is a HINT of unknown status, never a
fact: it triggers a status query, **never a re-submit** — a
duplicate B2C is unrecoverable money (FM3).

**Layer 3 — reconciliation re-poll**: a cron one-shot (cron_lock +
active-tenant walk + `FOR UPDATE SKIP LOCKED`, the dormancy/!19
recon pattern) claims payouts stuck past TTL and queries the status
API: verified paid → same verify-and-post path; verified failed →
release earmark; unknown past hard TTL → `expired` + ops event (a
human decides — the money may still move). A callback landing
mid-poll serialises behind the same row lock.

**Suspense doctrine** (!19 §4 mirrored for the outbound direction):
rail-confirmed-but-mismatched — the rail proves money left the float
but disagrees with the stored payout (amount differs, paid after
expiry, paid with no matching row) — posts the RAIL-VERIFIED amount
`DR suspense / CR cash.mpesa` (the outbound mirror of !19's intake),
emits a security/ops event, and enters the staff break-resolution
queue; suspense is SASRA class CLEARING and must be watched to zero.
Never silently accepted, never auto-attributed to the member.

**EOD reconciliation binds to #9**: every paid B2C posting carries
`external_ref` = the M-Pesa B2C `TransactionID`/receipt — the
matching key #9's statement-ingest matching engine keys on. The
payout recon job (layer 3) is the intent-side source; #9's EOD
statement recon is the statement side; breaks from either land in
ONE break-management framework (queue, ageing, four-eyes sign-off),
exactly as money-in does. **A B2C rail without #9's EOD recon
running is unaccounted money movement — §9 makes #9 a v1 build
precondition.** Recon and mismatch events feed #1 (detective
controls) and #4 (metrics/alerting).

### 7. Ledger: existing withdrawal services only; internal transfer is ONE journal

- **No parallel posting path.** B2C payouts post exclusively through
  the existing withdrawal machinery (`record_withdrawal` →
  `build_withdrawal_posting`, `DR member.deposits / CR cash.mpesa`,
  `WD-` reference, channel `mpesa`, `external_ref` = the B2C
  `TransactionID`, `actor_id=None` system posting with the acting
  member credential recorded on the payout intent and its events —
  the !19 convention), inheriting the guarantee-pledge exclusion,
  the withdrawal-source rule and the day-total base for free. No
  existing builder or service changes.
- **Internal transfer = a single balanced journal, never two
  half-postings.** One new pure additive builder
  (`build_internal_transfer_posting`: `DR member.deposits /
  CR member.shares`, same member both legs, new reference prefix
  `TR-`, channel `internal` — which the chart already defines and
  which correctly requires no external ref) posted in ONE
  transaction under the standard member → account lock chain. There
  is no instant where the books show the money in zero or two
  places. A "transfer" implemented as a withdrawal posting plus a
  share top-up posting is forbidden — two half-postings create an
  unbalanced window and two references for one economic event (FM8).
  Direction is one-way per §1; the reverse is a typed refusal.
- The only other NEW builders are the additive outbound-suspense
  pair (§6). Fees, if a tenant charges them for money-out, post
  through the existing `build_fee_posting` (`FE-`) in the same
  transaction — no new fee path.

### 8. Failure modes (rule 15) — falsifiable rows

Each row names the failure, the control that defeats it, and the
test that MUST exist (and fail with the control removed) before the
implementation MR merges. FM1–FM8 are v1; FM9–FM11 attach to the v2
activation triggers.

| # | Failure mode | Control (§) | Falsifiable test |
|---|---|---|---|
| FM1 | **Float exhaustion**: payouts submitted against an empty/insufficient Safaricom float — silent member-facing failures, unpaid "paid" expectations | Float-balance gauge + low-water alert + refuse-before-submit; typed "temporarily unavailable" refusal; ops event via outbox (§1) | Fixture float below request → creation refuses with the typed error, ops event emitted, NO rail call recorded by the fake adapter; alert fires at the low-water fixture |
| FM2 | **Result-callback forgery/replay**: attacker or Safaricom retry posts/releases twice | Capability token (hashed) + UNIQUE (tenant, OriginatorConversationID) dedupe under row lock + verify-against-stored-row + status-query above threshold (§6) | Forged Result on guessed/consumed token → 200-and-drop + security event, zero state change; replayed genuine Result → exactly one posting, one terminal transition (side-effect row counts) |
| FM3 | **Duplicate B2C payout**: timeout/unknown status handled by re-submitting — the same withdrawal paid twice, unrecoverable | QueueTimeout = hint → status query only; `submitted` is a one-way state; recon resolves via status API, never re-initiation (§6) | Result-vs-QueueTimeout race and recon-vs-callback race → fake adapter records exactly ONE `initiate_b2c` call per intent, ever |
| FM4 | **Cap-straddling concurrent payouts**: two in-flight acts (B2C + internal, or B2C + teller withdrawal) each pass the cap because earmarks aren't counted | Day-total input includes live earmarks + posted rows; evaluation under the account `FOR UPDATE` (§2.1, §6) | Two concurrent money-out acts straddling the cap (mixed rails) → exactly one succeeds (the !17 race test generalised across earmarks + postings) |
| FM5 | **SIM-swap session drain**: attacker owning the victim's SMS establishes a session and drains to the victim's own (swapped) number | #29 enforce + #46 server-verified PIN step-up bound to (destination, amount, currency) + !17 caps/notice + #8 above thresholds (§2) | Session WITHOUT valid step-up → creation refused; step-up challenge for act A replayed against act B → refused; both audited |
| FM6 | **Rail-confirmed-but-mismatched**: float provably shrank but amount/party disagrees with the stored intent, or money moved after expiry | Outbound suspense posting of the RAIL-VERIFIED amount + security event + staff break queue; never auto-attributed (§6) | Mismatched verified Result → `DR suspense / CR cash.mpesa` for the rail amount, intent → failed/expired reason recorded, break-queue row exists; suspense nets to zero after staff resolution fixtures |
| FM7 | **Number-change-then-drain** (v1 analogue of beneficiary injection): change the verified MSISDN via social-engineered staff KYC, then immediately B2C | MSISDN change is staff-reviewed (#30) + post-change payout delay / first-payout cap (§2.5) + member notification event | Payout within the post-change window above the first-payout cap → typed refusal; notification event emitted in-transaction with the KYC change |
| FM8 | **Internal transfer as two half-postings**: unbalanced window, two references for one event, crash between halves | Single balanced journal via one builder; DB balance trigger; one `TR-` reference (§7) | Transfer posts exactly one journal (leg count + single reference asserted); `PostingSpec.assert_balanced` property test; shares→deposits direction → typed refusal |
| FM9 | *(v2)* **Beneficiary injection**: add/edit a beneficiary under a hijacked session, drain immediately | Step-up on add/edit + cooling-off + first-send cap + notification (§4) | Add-then-max-drain and edit-then-send inside the window → refused; events emitted in-transaction |
| FM10 | *(v2)* **Name-enquiry enumeration**: scripted walk across MSISDN/member space harvests names/existence | Masked render + in-flow-only + stricter #31 bucket + uniform miss shape/latency + full audit + #1 anomaly signal (§5) | Scripted probe → bucket exhausts, responses shape- and latency-uniform across all miss classes, audit rows accumulate, anomaly event fires |
| FM11 | *(v2)* **Screening bypass**: member-to-member send proceeds while #10 screening is down or unwired | Fail-closed screening hooks at add/edit and pre-posting; capability absent entirely before #10 (§3) | Screening-port-down fixture → send refuses; with #10 absent, no member-to-member route exists (404, not 403) |

Plus the mirrored !19 adversarial set: cross-tenant
`OriginatorConversationID` probe, full-matrix transition test on the
payout lifecycle, and double-submit on the creation endpoint.

### 9. Build preconditions gating any implementation MR

Issue #44 states them, quoted verbatim so no implementation MR can
claim readiness early:

> **Blocked-on (build, not ADR): !17 merged, #29 enforce-capable,
> #31 merged, #10 program for anything member-to-member.**

As a checklist, with the v1-binding additions the owner sign-off
attached ("binding preconditions v1 inherits regardless of rail"):

- [ ] **!17 merged** — the daily velocity caps + notice machinery is
  on `develop`; §2's chain reuses its pure evaluation and settings.
- [ ] **#29 enforce-capable** — device attestation can run in
  `enforce` mode for member money writes (log-only is insufficient
  for money-out).
- [ ] **#31 merged** — per-credential member rate limits exist, with
  the stricter money-out bucket configurable.
- [ ] **#10 program for anything member-to-member** — restated
  plainly: **member-to-member send DOES NOT SHIP before #10** (§0
  hard gate; not needed for v1's two capabilities, whose
  threshold-reporting hooks are still built in from day one, §3).
- [ ] **#8 maker-checker** available for threshold amounts (§2.6) —
  the payout approval surface.
- [ ] **#9 EOD reconciliation** running against the B2C settlement
  statement — B2C without EOD recon is unaccounted money movement
  (§6).
- [ ] **#46 step-up factor** — the server-verified Argon2id PIN
  (ADR-0012 §1 upgrade path) implemented for the confirm act (§2.4).
- [ ] **Member read surface for !17's controls — REQUIRED COMPANION
  of v1**: !17 as-built has NO member-audience read routes; "view
  daily limits / track pending withdrawals" (the #43 T1 gap) must
  land with or before the first money-out write, extended to show
  payout-intent state. Members must be able to SEE the controls
  acting on their money.

## Alternatives considered

- **Post the withdrawal before the rail confirms, reverse on
  failure** — rejected: books run ahead of reality; every rail
  failure demands a reversing entry; the member sees phantom debits;
  a crash between posting and rail call strands a debit with no
  payout.
- **Call the rail first, post after, with no earmark** — rejected:
  classic TOCTOU — concurrent payouts spend the same balance; the
  rail pays money the ledger never earmarked. Earmark-first (§6) is
  the only ordering where funds are always in exactly one state.
- **Extend !17's `withdrawal_holds` into the async payout earmark
  (one hold register)** — rejected for this design: its as-built
  CHECKs bind decision and posting to one instant and its actor
  model is staff-only; supporting an async rail would force an ALTER
  of another MR's table plus semantics changes to a machine built
  and tested for the teller flow. The pure evaluation, settings and
  conventions ARE reused; the earmark state lives on
  `payout_intents`. Recorded as an overrulable gap on #44 (a human
  may prefer the one-register ALTER; it is expand-only but touches
  !17's contract).
- **A parallel cap/threshold evaluation for member payouts** —
  rejected: reuse-first (gate 1.1); one decision function
  (`evaluate_withdrawal`), one pair of tenant settings, one
  day-total definition across all rails, or the split-rail bypass is
  free.
- **v1 payout to body-supplied MSISDN with OTP confirm** — rejected:
  turns every hijacked session into an arbitrary-destination drain;
  v1 destination is server-resolved from the KYC-verified phone
  only, and destination changes ride the staff KYC path (§0).
- **Auto-retry B2C on timeout/unknown status** — rejected: a
  duplicate payout is unrecoverable; unknown status is resolved by
  the Transaction Status query only, and past hard TTL by a human.
- **Reject (refuse to book) rail-confirmed-but-mismatched payouts**
  — rejected: the float provably shrank; refusing to book guarantees
  a books-vs-rail break every EOD until hand-posted. Suspense (§6)
  makes the anomaly visible, ageing and staff-resolvable — the !19
  §4 reasoning, outbound.
- **Unmasked name enquiry / standalone lookup endpoint** — rejected:
  an enumeration and doxxing oracle over the member base; masked,
  step-up-gated, in-flow-only confirm (§5) is the maximum safe
  disclosure.
- **Two half-postings for internal transfer** (withdrawal + share
  top-up) — rejected: unbalanced window, two references for one
  event, and a crash between the halves loses money in flight;
  single balanced journal only (§7).
- **Shares → deposits internal transfer** — rejected: a third share-
  exit path around the withdrawal-source rule and the maker-checker
  share-transfer/exit machinery; refused at the domain layer.
- **Client-side or app-enforced limits/step-up** — rejected: no
  client money math (gate 1.1, #43 catalogue contradictions); every
  control in §2 is server-side and atomic with the posting.
- **Shipping member-to-member behind a feature flag before #10** —
  rejected: a flag is not a compliance program; POCAMLA obligations
  attach to the capability existing, not to its default.
- **OTP-only step-up (no server-verified PIN)** — rejected: SMS OTP
  falls to exactly the SIM-swap attacker money-out must resist;
  ADR-0012 §1 fixes the SMS-independent knowledge factor and gates
  it on this ADR — the two documents interlock.
- **OTP-only beneficiary activation, no cooling-off (v2)** —
  rejected: same SIM-swap reasoning; time is the only factor the
  attacker cannot phish.
- **Reusing `IdempotencyMiddleware` for callback dedupe** — rejected
  (!19 precedent): the rail sends no `Idempotency-Key`; dedupe keys
  on the rail's own identifier (`OriginatorConversationID`) under
  the row lock.
- **Bank rail in scope now** — rejected: no partner selected; a
  half-designed bank integration would dilute the B2C doctrine.
  Deferred behind the `PayoutRailPort` seam (§11).

## Consequences

Positive: money-out arrives with the full house doctrine intact —
earmark-first lifecycle, one control chain atomic with the posting,
!19's callback/idempotency/suspense doctrines reused rather than
re-derived, !17's controls reused as the single cap/threshold truth,
and the riskiest capabilities (beneficiaries, name enquiry,
member-to-member) designed once, frozen, and fenced behind named
triggers with #10 as the hard gate. Refusals are typed and audited;
every failure mode in §8 has a named falsifiable test.

Negative: the member experience of money-out is deliberately slower
than the catalogue's ideal (notice holds, step-up, post-MSISDN-change
delay, maker-checker above thresholds) — that friction is the control
surface, and the required companion read surface (§9) must explain it
or support will drown. Float custody adds an institutional liquidity
duty (top-up discipline, low-water alerting) the SACCO does not have
today. A second Daraja credential set, two callback routes and a
recon worker widen the operational surface (#4's monitoring and
dead-man switch apply). Two notice-state homes exist (staff
`withdrawal_holds`, member `payout_intents.held_notice`) until/unless
the one-register alternative is taken up — both surfaces must render
both.

Migration path: expand-only when implementation comes — new tables
(`payout_intents`, `payout_intent_events`; v2: `beneficiaries`), new
settings, one import-linter entry, additive posting builders
(`build_internal_transfer_posting`, the outbound-suspense pair); NO
existing table, builder or service changes; !17's machinery is
untouched. Rollback of the implementation: remove routes, worker and
tables; posted ledger rows are permanent history, which is correct.
Rollback of this ADR: delete the file.

## Open questions requiring human sign-off (§11)

1. Bank rail partner selection (Pesalink via partner bank vs EFT
   batch) — procurement/institutional, blocks any member bank
   withdrawal.
2. One-hold-register overrule (§6): accept the expand-only ALTER of
   `withdrawal_holds` instead of the `payout_intents.held_notice`
   state? Decide before the implementation MR (gap work item #47).
3. Tenant default values: step-up threshold, post-MSISDN-change
   delay/first-payout cap, maker-checker threshold; v2: cooling-off
   duration, first-send cap, beneficiary count cap.
4. B2C tariff bearing: absorbed by the SACCO vs passed to the member
   as a `FE-` fee — a pricing/policy decision.
5. CTR/STR threshold figures and aggregation windows — owned by
   #10's program, consumed here (§3).
6. Float low-water figure and top-up SLA — treasury/operations duty
   (§1, FM1).
