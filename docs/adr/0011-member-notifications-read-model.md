# ADR-0011: Member notifications read model — the notification center is member state, the outbox is delivery infrastructure

- Status: Proposed
- Date: 2026-08-20
- Deciders: Genesis Prestige engineering (drafted for issue #45, P17/T3.2)

## Context

Every "receive alert / notification center" line in the P17 member-app
catalogue (#43, T3.2) — deposits received, withdrawals completed, loan
updates, guarantor requests, missed-payment alerts, statements ready,
security alerts, new-device login, announcements — is blocked on a read
model that does not exist. The only event store we have is the
transactional outbox (`backend/src/genesis/infrastructure/outbox_worker.py`),
and it is deliberately unusable as one:

- **It is delivery infrastructure, not an archive.** Dispatched rows are
  delivery receipts purged after `DISPATCHED_RETENTION_DAYS` (30 days) in
  bounded batches; dead rows are an alertable dead-letter queue.
- **It is redacted at rest.** The dead-letter path (!2) and, with !24, the
  dispatched-mark path strip OTP payloads (`code`, full `destination`)
  **in the same UPDATE statement that marks the row processed**. A row's
  payload is trustworthy only while the row is `pending`; the moment
  delivery succeeds or dead-letters, the sensitive fields are gone by
  design. Exposing outbox rows to members would either leak PII (pending
  rows) or show redacted stubs (processed rows) — both wrong.
- **It is at-least-once.** Redelivery and lease-expiry replays mean any
  consumer must dedupe by event id.

Constraints this design must satisfy (MASTER_PROMPT references):

- §1.5 data integrity: audit/side-effect rows written in-transaction;
  multi-step operations atomic; DB constraints, not app validation alone.
- §1.6 / ADR-0002: tenant isolation via forced RLS; member-scoped access
  with identity derived exclusively from the authenticated principal
  (the ADR-0007 rule enforced by `RequireMemberPrincipal`'s per-request
  live-link re-check, upgraded to `RequireMemberReadPrincipal` once #31
  lands).
- §1.3: keyset pagination, limit ≤ 100, opaque signed cursors — !7
  established the discipline that member list cursors live in **new
  member-own signed-cursor scopes** (`member.transactions.list`,
  `member.loans.list`, `member.statement`) so pagination state can never
  replay across the staff/member boundary or across endpoints.
- The !24 lesson (issue #45 item 2): least disclosure is enforced **at
  write time**, not by cleanup jobs racing readers.

Feeders that already exist or are in flight: the `security.anomaly` /
`security.refusal.*` event stream (!11), payment-intent lifecycle events
(ADR-0008 §recon, !19), withdrawal holds and velocity-cap refusals (!17),
guarantor requests (#41's list plus the existing consent/release acts).

## Decision

### 1. A `member_notifications` read model, written in the same transaction as the domain event

We introduce a tenant-scoped, member-scoped, append-only table
`member_notifications` (design sketch — **no migration ships with this
ADR**):

- `id uuid` PK, `tenant_id` (RLS, all composite indexes lead with it),
  `member_id`, `category` (§4 taxonomy), `title`, `body` (rendered-safe
  text, §2), `event_type` (machine key, e.g. `deposit.received`),
  `source_event_id uuid` with `UNIQUE (tenant_id, source_event_id)` so a
  retried write path can never double-insert, `created_at`,
  `read_at timestamptz NULL` — the read/unread flag is the **only**
  mutable bit; everything else is append-only, constraint-enforced.
- Index `(tenant_id, member_id, created_at DESC, id DESC)` shipped with
  the list query that needs it (MASTER_PROMPT §5.3).

**The write happens in the same database transaction as the domain
event**, by the same application service that already writes the audit
row, alongside (not instead of) the outbox event that will later drive
push delivery.

**Alternative — project from the outbox before redaction — rejected, on
the as-built evidence:**

- *Redaction race.* !24 redacts dispatched OTP rows **in the same UPDATE
  that marks them dispatched**; !2 does the same for dead letters. There
  is no "after dispatch, before redaction" window to project from — a
  projector would have to run before or inside the dispatch worker,
  coupling read-model freshness to delivery cadence (5 s cycles,
  exponential backoff to 8 attempts, 300 s claim leases). Worse: a
  dead-lettered event is redacted too, so a member would fail to receive
  the in-app notification precisely when the external delivery channel
  is broken — the one time the in-app copy matters most.
- *Retention race.* Dispatched rows are purged after 30 days. Any
  projector outage longer than the retention window silently loses
  events, unrecoverably.
- *Replay semantics.* The outbox is at-least-once; a projector must
  dedupe by event id and tolerate observing a row in either pre- or
  post-redaction state across replays. That is exactly the class of
  subtle bug the same-transaction write makes structurally impossible.

**The costs of the same-transaction write, weighed honestly:**

- *Posting-path latency:* one additional single-row INSERT on a
  connection that already holds the domain locks — no new lock
  acquisition, no new round-trip pattern beyond what the in-transaction
  audit row already costs. Acceptable.
- *Failure coupling:* a failed notification INSERT aborts the domain
  action. This is deliberate and consistent with the house rule that
  audit rows ride the domain transaction (§1.5): a money movement whose
  notification cannot be recorded should fail loudly, not succeed
  silently unobserved. The INSERT touches a table with no triggers, no
  foreign keys into hot rows, and a per-transaction unique key — the
  realistic failure modes (connection loss, constraint bug) already
  abort the transaction anyway. We accept the coupling and refuse the
  "log and continue" pattern (§1.2: no silent failures).

The outbox keeps its job: it remains the only channel for external
side-effects (push/SMS later, §5). The read model and the outbox event
are written in the same transaction, so they can never disagree about
whether an event happened.

### 2. Least disclosure at rest and on the wire — rendered-safe at write time

Notification rows survive long after the event, so the !24 lesson is
applied **at write time, not cleanup time**: the row is born clean; there
is nothing to redact later and no race between a redactor and a reader.

- `title`/`body` are rendered from a server-side template registry at
  write time. Raw event payloads are **never** stored in
  `member_notifications`.
- Prohibited content, enforced by the template registry and by tests
  (§7): OTP codes or any `\b\d{6}\b`-shaped secret, full MSISDNs
  (masked form only, reusing the masking helper !24 uses for retained
  diagnosis fields), counterparty PII beyond policy (a member-to-member
  transfer notification names the counterparty only to the extent the
  statement line already does), tokens, internal ids beyond the
  `source_event_id` correlation key.
- Amounts and references that already appear on the member's own
  statement are permitted — the notification never discloses more than
  the surfaces the member can already read.
- Logs and error payloads follow §1.6: category + ids only, never
  rendered text.

### 3. Surface: list + mark-read

- `GET /member/notifications` — keyset pagination on
  `(created_at DESC, id DESC)`, `limit ≤ 100`, optional
  `category` filter. Cursors are minted in a **new** signed-cursor scope
  `member.notifications.list` per !7's discipline: a notifications
  cursor is unusable on any other endpoint and no staff or other member
  cursor is usable here, both directions test-enforced. Identity comes
  exclusively from `MemberAuthContext`; no member id in path/query/body.
  Gated by `RequireMemberPrincipal` (live-link re-check every request),
  upgraded to `RequireMemberReadPrincipal` and the member-read rate
  bucket once #31 lands.
- `POST /member/notifications/{notification_id}/read` — **idempotent,
  version-free**. The decision, argued:
  - The only mutable bit is monotonic (`NULL → read_at`) and commutative:
    concurrent retries of "mark read" cannot conflict on anything worth
    protecting. The write is one statement,
    `SET read_at = COALESCE(read_at, now()) WHERE tenant_id = … AND
    member_id = … AND id = …`, returning 200 with the row state whether
    or not this call was the one that flipped it.
  - *Optimistic locking rejected:* the house 409-on-stale rule
    (MASTER_PROMPT §1.4) protects multi-field aggregates where a lost
    update destroys information. Here a "lost" concurrent mark-read
    loses nothing — both writers wanted the same terminal state — and a
    409 would punish exactly the mobile-retry behaviour we must expect
    on flaky networks. Version-free idempotency is the stronger
    property for this shape.
  - The member-scoped `WHERE` clause makes another member's notification
    id indistinguishable from a nonexistent one (404 either way — no
    existence oracle).
  - Un-read, edit, and delete do not exist. Bulk mark-read is a possible
    follow-up (idempotent, bounded by a `created_before` timestamp) but
    is not required by this ADR.
- **Retention window: notifications are NOT the ledger.** The financial
  record lives in the ledger, transactions, and statement history; a
  notification row is a rendered, disposable copy whose deletion deletes
  nothing of record — the audit trail (in-transaction audit rows) is a
  separate, append-only surface with its own retention. Default
  retention: **365 days**, tenant-configurable with a 90-day floor,
  purged in bounded batches through the shared batch runner (the outbox
  purge pattern). Statement history is explicitly the answer to "show me
  older activity"; the notification center never is.

### 4. Category taxonomy and member preference model

Categories (CHECK-constrained enumeration, extensible by migration):

| category | examples | member-mutable? |
|---|---|---|
| `security` | new-device login, anomaly alerts (!11 feed), credential changes | **NEVER** — immutable-on, by policy and by constraint |
| `money_movement` | deposit received, withdrawal completed, payment-intent outcomes (ADR-0008), withdrawal holds (!17) | no — always on |
| `loans` | disbursement, status changes, repayment posted | no — always on |
| `guarantor` | new guarantee request (#41 feed), release | no — always on (actionable) |
| `arrears` | missed-payment alerts | no — always on |
| `statements` | statement ready | yes — mutable off |
| `announcements` | SACCO announcements | yes — mutable off |

- Preference model: `member_notification_preferences (tenant_id,
  member_id, category, enabled)` mutated only through a member endpoint
  that **rejects server-side** (400, fail closed) any attempt to disable
  a non-mutable category, backed by a DB CHECK restricting stored
  preference rows to the mutable set — defense in depth, not app
  validation alone (§1.5).
- Rationale for the always-on money/security core: a member who could
  mute withdrawal or security alerts has silenced their own
  account-takeover alarm; the in-app record of these categories is a
  fraud-detection control, not a convenience. Preferences will
  additionally govern future push-channel delivery (§5) for mutable
  categories; they never suppress the in-app row for the always-on set.

### 5. Push delivery (FCM/APNs) is OUT of scope — decided

This ADR delivers the in-app center only. **Recommendation: OUT.**

- Push requires device-token custody — registration, rotation, revocation
  on logout/credential-revocation, and per-device targeting. That is a
  device-inventory problem, and device inventory is the auth-factors
  ADR's territory: **device-token custody is deferred to ADR-0012
  (auth-factors, #46)**, which owns the device model this would attach
  to. Designing token custody here would either duplicate or preempt it.
- The seam is already correct: every notification-worthy domain event
  also enqueues an outbox event, and the outbox worker dispatches through
  the `NotificationProvider` adapter port. A future FCM/APNs adapter
  slots in behind that port without touching this read model.

### 6. Backfill doctrine: the center starts empty

No fabricated history at feature launch. **Rejected alternatives:**

- *Project historical outbox rows* — impossible in principle: processed
  rows are redacted (!2/!24) and purged (30 days); the history does not
  exist.
- *Synthesize from the ledger/audit trail* — dishonest telemetry: a
  notification row asserts "the member was notified at `created_at`",
  and manufacturing such rows for events that predate the feature
  fabricates a record that never happened. Audit rows are also not
  member-rendered and would require retroactive least-disclosure
  rendering nobody can verify.

The first notification a member sees is the first event after their
tenant's feature launch. Statement history covers everything earlier.

## Alternatives considered (summary)

- **Expose/query the outbox as the notification feed** — rejected:
  redacted at rest, purged at 30 days, at-least-once, and delivery
  infrastructure by contract (see Context).
- **Async projection from the outbox before redaction** — rejected:
  redaction happens atomically with the processed mark, dead letters are
  redacted too, retention bounds projector lag, replay demands dedupe
  (§1, argued in full).
- **Optimistic-locked mark-read** — rejected: monotonic single-bit
  transition; 409s punish legitimate retries for zero integrity gain
  (§3).
- **Push delivery in scope** — rejected: device-token custody belongs to
  the ADR-0012 device inventory (§5).
- **Backfill from ledger or audit history** — rejected: fabricated
  notification history (§6).
- **Unbounded retention "because members might want it"** — rejected:
  notifications are not the ledger; statements are the archive (§3).

## Mandatory adversarial tests for the implementation MR

Named up front so the implementation cannot quietly skip them:

1. `test_cross_member_notification_leak_probe` — with two real members
   in one tenant (and a third in another tenant): member B's credential
   must never list, read-count, or mark-read member A's rows — via the
   list, via direct id probing on mark-read (expect the indistinguishable
   404), and across tenants under forced RLS. Both directions.
2. `test_notification_rows_pii_at_rest_sweep` — mirror !24's regex sweep:
   for every template in the registry, render with hostile fixture data
   and sweep the **raw row text** for `\b\d{6}\b` OTP shapes, full
   MSISDN patterns, and known counterparty-PII fixtures; masked forms
   only may survive.
3. `test_mark_read_idempotency_under_concurrent_retry` — fire concurrent
   duplicate mark-read calls for the same row: exactly one `read_at`
   value persists, every call returns 200 with consistent state, no 409,
   no double-write anomaly.
4. `test_notification_cursor_scope_isolation_both_directions` — a
   `member.notifications.list` cursor is a sanitized 400 on every other
   member and staff list endpoint, and every other scope's cursor
   (member or staff) is a sanitized 400 here — never a silently empty
   page.

## Consequences

- Positive: the member app gets a queryable, member-scoped notification
  state that is clean at rest by construction; the outbox contract
  (delivery-only, redacted, purged) stays intact; every posting path
  gains a member-visible receipt in the same transaction that moves the
  money.
- Negative: every notification-worthy domain service takes one more
  in-transaction INSERT and a template-registry dependency; the template
  registry becomes a reviewed surface (PII policy lives there); purge
  adds one more scheduled job (reusing the batch-runner pattern).
- Preconditions for the implementation (not this ADR): !7 merged (the
  member read surface + cursor-scope infrastructure), #31 member read
  rate limits (or gate the routes with `RequireMemberPrincipal` and
  upgrade), the #41 guarantor list for that feeder's UX, ADR-0012 (#46)
  for any future push slice.
- Rollback path: the ADR is docs-only — delete the file. For the future
  implementation: the read model is additive (new tables, new routes);
  dropping the routes and tables removes the feature without touching
  the ledger, audit trail, or outbox.
