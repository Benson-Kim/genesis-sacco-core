# P17 member app — pre-implementation design review (MASTER_PROMPT §5.9)

Date: 2026-08-20. Status: decision record — binding for every P17 MR.
Anchor work item: #43 (P17 capability register). Gate ledger for member
writes: #30. This review supersedes the earlier §5.9 pass recorded as a
note on #30 (2026-08-20 12:38) where facts have moved; corrections are
cited inline. Every claim below was verified against `develop`
(`ac9a12e`) or the named in-flight MR head — nothing is asserted from
memory.

---

## 1. Reuse audit (gate 1.1 — search before writing)

### 1.1 What exists and MUST be built on

| Capability | Where it lives | Binding rule |
|---|---|---|
| Member OTP session | `POST /member/auth/otp/request`, `/member/auth/otp/verify`, `/member/auth/refresh` — `backend/src/genesis/api/member.py` (on `develop`) | The ONLY member auth. Bodies are `signin_identifier`-shaped (`OtpRequestBody`/`OtpVerifyBody` reused from `api/auth.py`, not copied); pre-auth routes require `x-tenant-id`; refresh reuse revokes the family. |
| Guarantor acts | `POST /member/guarantees/{id}/consent`, `/member/guarantees/{id}/release` — same file | Body is `{version}` only (`extra="forbid"`); the principal IS the consent. `409` on stale version, `403` opaque. |
| OTP-session model + client rules | `docs/member-auth.md` | §1 principal model (FM1 audience separation), §2 endpoint table, §3 client rules (least disclosure, token custody, 401 = session over). The app implements this document, it does not reinterpret it. |
| Codegen contract | `web/packages/api-client/openapi.json` | `gp_api_client` models are GENERATED from this snapshot (repo file, not a URL). Today it carries exactly the five `/member/*` routes above — nothing else member-audience. |
| Idempotency | `Idempotency-Key` on every mutation, scoped per (tenant, actor principal, route) — FM5 proven in `backend/tests/test_idempotency.py` | Client generates a UUID per logical attempt and REUSES it across retries of that attempt. |
| Attestation seam | `DeviceAttestationPort` + `MEMBER_ATTESTATION_MODE` (#29, seam shipped in !26) | The app ships the client half (Play Integrity / App Attest token acquisition) against the seam; enforce-mode wiring is #29's, not P17's. |

### 1.2 Prior art deliberately carried from the web admin

The web admin (`web/`) already solved the client-side problems P17 has;
re-solving them differently is a rejected MR (gate 1.1):

* **Layering** — screens → `modules/<name>/api.ts` → `modules/<name>/schemas.ts`
  → one shared client (`web/src/lib/api.ts`, `createGenesisClient`, no
  ad-hoc fetch). Flutter mirror: `presentation → repository →
  gp_api_client`, one `GpHttpClient` composition root.
* **Money as validated strings** — `web/src/lib/schemas.ts` asserts the
  SHAPE of server decimal strings (`moneySchema` / `signedMoneySchema` /
  `aggregateMoneySchema`) and rejects malformed figures at the boundary.
  Dart equivalent: a `MoneyString` value type that validates shape and
  is rendered verbatim — never parsed to `double`, never arithmetic.
* **Envelope handling / error taxonomy** — every error is
  `{category, correlation_id}` (member-auth.md §3.1); the web client
  surfaces a sanitized category + correlation id and never parses error
  text for business meaning. `ApiError` in `gp_api_client` mirrors this,
  carrying `statusCode` for the 401/403/409/422/429 taxonomy.
* **Cursor discipline** — keyset cursors are opaque, scope-bound
  server-side; the client never fabricates or edits a cursor.

### 1.3 Prior art that exists but is NOT reusable as-is

Branch `duo/feature/9-flutter-workspace-scaffold` (migrated P16 draft;
NOT on `develop`, no open MR in this project) contains a `mobile/` tree
whose structure is right and whose contents are wrong — see §10
(scaffold defect register). It is a parts donor, not a base: the first
P17 MR either supersedes it with the defects fixed or cherry-picks
structure only, and says which in its description (BUILD_PROMPTS
P16(a): no second parallel scaffold).

---

## 2. Blocking finding — the member READ surface is not on `develop`

**Verified 2026-08-20 against `develop` `ac9a12e`:**
`backend/src/genesis/api/member.py` contains exactly the five endpoints
in §1.1 and nothing else. Every other member-relevant read is
staff-audience: e.g. `GET /members/{member_id}/statement`
(`backend/src/genesis/api/members.py:303`) is guarded by
`RequirePermission(Module.MEMBERS, Action.VIEW)` (`members.py:34`, via
`ViewCtx`). Per `docs/member-auth.md` §1, a member token against a
staff gate gets **403 by design (FM1)** — both directions. And
`payment_intents` (P19, #7) does not exist anywhere on `develop`
(zero grep hits in `backend/src`; ADR-0008 for it is docs-only on !19).

**Precision (correcting the stale phrasing of the earlier review, both
directions):** the read surface *does exist in flight* — !7
(`duo/feature/adr0007-member-read-surface`, draft, green) carries
`GET /member/me` (aggregates), `GET /member/transactions`,
`GET /member/loans`, `GET /member/loans/{loan_id}` (with schedule),
`GET /member/statement` — but !7 is unmerged and merges LAST of the
code queue (#19). Until it lands, `develop`'s `openapi.json` has no
member reads, and a client generated from `develop` cannot name them.
The blocker is merge-order, not design; the consequence for P17 is the
same: **screens cannot build against endpoints that are not merged.**

**Consequence: exactly ONE P17 slice is buildable today** — the OTP
session (auth endpoints live on `develop`) plus the guarantor consent
inbox (the consent/release ACTS live on `develop`; the inbox's list
data source is #41, in flight as !31 — the third MR stacked on !7's
branch after !25/#31 and !27/#33 — so the inbox screen builds against
!31's recorded contract and stays UNVERIFIED until #41 merges).

### 2.1 Required member-audience backend surface

Naming note (binding): the paths in the left column are this review's
nominal grouping. Where an as-built or in-flight route exists, **the
right column is the contract and !7's `openapi.json` snapshot is the
binding shape** — the earlier review already established that a client
generated from a nominal table is fiction.

| Nominal group | As-built / in-flight reality | Tracking |
|---|---|---|
| `GET /member/me` | In flight on !7 (aggregates included). | !7 (#30 ledger) |
| `GET /member/me/balances` — per-account breakdown, server-rendered strings, no client money math | **Missing everywhere** (!7 ships aggregates only; #43 T1 gap had no tracker). | **#50 — filed by this review** |
| `GET /member/me/statement?cursor=&limit=` — keyset, ≤100, mirroring the staff shape at `members.py:303` | In flight on !7 as `GET /member/statement` (keyset, `le=100`, same `StatementLineOut` field set). Export/PDF is #32, separate. | !7; #32 |
| `GET /member/me/guarantees?status=PLEDGED` | In flight as `GET /member/guarantees?status=` on !31, stacked on !7 (#41). Least-disclosure rows `{id, loan_ref, amount, status, version}`. | #41 / !31 |
| `GET /member/me/loans/{id}/schedule` | In flight on !7 as `GET /member/loans/{loan_id}` (detail embeds the schedule rows). | !7 |
| `POST /member/me/loan-applications` + `POST …/quote` (server-computed preview) | **Missing everywhere**; #30 sequences it third among writes but had no implementation tracker. | **#51 — filed by this review** |
| `POST /member/me/deposits/intents` + `GET …/intents/{id}` | **Missing — blocked on P19** (#7; ADR-0008 on !19 decides the shapes: `POST /member/payment-intents`). | #7 |

### 2.2 Recorded rules (binding on every P17 MR)

1. **Any screen blocked on this surface is UNVERIFIED in the DoD** —
   recorded unticked with its blocking work-item link, **never faked
   against a mock and ticked** (BUILD_PROMPTS P16(b), rule 13).
2. **The member app NEVER reads a staff endpoint, with any token** —
   the P14.5 principal-confusion rule. Not `/members/*`, not
   `/me/permissions`, not `/auth/*`. FM1 makes it a 403 server-side;
   the client must not even name those paths (§9 CI gate).
3. **Follow-up filing (search-first, done in this session):**
   * `me/balances` → no open item covered it → **#50 filed**, cross-linked to #43, #30, #31, #41.
   * `statement` → covered by !7 (in flight) + #32 (export) → no new item; a duplicate tracker for an in-flight deliverable is itself a gate-1.1 violation.
   * `loans/schedule` → covered by !7 (`/member/loans/{loan_id}` embeds the schedule) → no new item.
   * `loan-applications + quote` → no open implementation item (#30 is a sequencing ledger) → **#51 filed**, cross-linked to #43, #30, #7, #29, #31, #41.
   * `deposits/intents` → covered by #7 (P19) → no new item.

---

## 3. Architecture

### 3.1 Package layout (exact — deviations are review findings)

```
mobile/
  gp_api_client/            # transport + generated models. NO Flutter widget imports.
    lib/src/generated/      # openapi_generator output from web/packages/api-client/openapi.json
                            # NEVER hand-edited; CI diffs a regeneration (§9)
    lib/src/infrastructure/ # GpHttpClient, TokenStorage, CertificatePinning, IdempotencyKey
    lib/src/models/         # ApiError + hand-written envelopes ONLY (error/category,
                            # cursor page wrapper) — anything shape-derivable is generated
  gp_ui/                    # tokens + primitives shared by member_app and admin_app
                            # (prototype palette; no API types, no business logic)
  member_app/
    lib/src/core/           # providers, router, session (state machine), cache,
                            # biometrics, env (per-flavor: base URL + tenant id baked)
    lib/src/features/<name>/
      data/                 # repository — the ONLY layer touching gp_api_client
      domain/               # immutable view models; money fields are validated
                            # strings; NO arithmetic on money, ever
      presentation/         # widgets/screens — never reaches past its repository
```

Dependency direction inward: `presentation → domain ← data → gp_api_client`.
A `presentation` import of `gp_api_client` (or of another feature's
`data/`) fails the import-boundary lint (§9). `gp_ui` depends on
Flutter only; `gp_api_client` depends on Dart + `http`/secure-storage
plugins only.

### 3.2 State and navigation

* **State: Riverpod** (owner sign-off, §5.B) — repositories and session
  exposed as providers; no second state framework.
* **Navigation: 5-item bottom nav** (Home / Accounts / Transact /
  Loans / More per #43 T0) with a **Favorites default set: Deposit,
  Withdraw, Transfer, Repay Loan, My Accounts — each tile greyed (or
  hidden) until its capability tier clears** (#43 rule, sign-off §5.B):
  a Favorites tile must never launch an unbuilt flow. Tier state is a
  build-time capability map keyed to merged backend surface, not a
  runtime feature probe.

---

## 4. Cross-cutting contracts

1. **Session/token custody — `docs/member-auth.md` verbatim.** Secure
   storage only (Keystore/Keychain); tokens never in logs or crash
   reports; access token ≤15 min, refreshed proactively; the newest
   refresh token is PERSISTED before first use (family revocation on
   reuse); **any 401 = session over** — discard both tokens, return to
   login; no transparent refresh-on-401 loop (scaffold defect D5).
2. **Request envelope.** `x-tenant-id` on pre-auth routes; Bearer +
   `Idempotency-Key` (UUID per logical attempt, reused across retries)
   on every mutation; errors decoded only as `{category,
   correlation_id}` + status code — never parsed for business meaning.
3. **Offline READ cache — reads only, never writes.** Cached responses
   carry their as-of moment and screens render the staleness marker
   (P17 FM2); no queued mutations, no optimistic writes; cache is
   encrypted at rest and purged on logout/401.
4. **No client money math (gate 1.1).** All amounts are server-rendered
   decimal strings, shape-validated (the `web/src/lib/schemas.ts`
   doctrine) and rendered verbatim. No installment calculator, no
   client-side totals, no `double`. Enforced by a grep/analyzer gate
   (§9), the P15 precedent.
5. **Certificate pinning — the #42 sign-off, OPTION 1:** pin our
   OWN-KEY **SPKI** hash(es) (not the whole cert, not the CA chain, not
   the shared-host cert), ≥2 pins (offline backup key) in the first pin
   set, and the same keypair carried to the new host at the #11 hosting
   exit so the pin set survives cutover. Pin failure is a hard
   connection error (P16 FM2) — no bypass flag in shipping code.
   Rotation runbook (add-one-release-before / remove-one-release-after)
   is a #42 deliverable before the first store binary.

---

## 5. Owner sign-offs dated 2026-08-20 — recorded as binding inputs

Recorded where cited; this review treats them as decided (not open):

**(B) Foundation** (note on #43): tenant strategy = **one white-label
app per SACCO** — per-tenant bundle id/name/icon/tokens/pin set from a
build-time flavor config, NEVER a runtime tenant switcher (client-side
principal-confusion class). **Flutter + Riverpod**; **Android minSdk 26+,
iOS 15+** (hardware-backed keystore / App Attest floor); **environments:
dev-only for now** (keep `core/env` honest so flavors are config, not
surgery). **Google Play Console and Apple Developer accounts DO NOT
EXIST YET** — an explicit external blocker on the critical path: Play
Integrity needs the Play Console app entry, App Attest needs the Apple
team + bundle registration (#29), and no distribution of any binary
happens without them. Apple D-U-N-S verification alone can take weeks —
account creation is an owner action to start now.

**(C) Security** — #42: pin custody **Option 1** (own-key SPKI, backup
pin, custody carried through the #11 exit). #29: Google Cloud + Apple
attestation credentials WILL be provisioned; **enforce-mode flip is
confirmed as a hard precondition for the first paid SMS** (#18). #46:
**PIN approved as a server-verified factor** (Argon2id-class,
server-side attempt counters, step-up layered on the OTP session) —
ADR-0012 is in flight on !29/its adopted branch; this review references
it and does not duplicate it.

**(D) In-flight queue** — #41 stacks on !7 now as the **third stacked
MR** (!31, after !25/#31 and !27/#33; retargets to develop when !7
lands). #31: **120/min accepted as the launch member-read limit**
(per member principal, shared bucket, env-tunable, 429 metered; #41's
list joins the same bucket).

**(E) Provider decisions pending — each behind a SOLID plug-and-use
interface** (seam now, vendor later; no vendor type leaks past the
adapter): #18 SMS gateway (`SmsGateway` port; go-live gated on #29
enforce). #39 observability (telemetry port; PII scrubbing is ours,
pre-egress; upstream of the member beta). #11 hosting exit (provider +
budget open; rubric fixed — Redis required (#15), customer-held TLS
keys (#42), PITR (#26), surge headroom (#2); exit completes before the
first app-store release). #7 Daraja (sandbox creds + owned
paybill/shortcode committed; `PaymentRailPort` seam; sandbox green is
NOT the release gate — ADR-0008 §9 is). #10 AML/CFT (MLRO to be named +
sanctions source selected; `SanctionsScreeningPort`; member-to-member
transfers do not ship before the program exists).

**(F) Money-out** — #44: **v1 rails = M-Pesa B2C to the member's own
verified MSISDN + own-account internal transfers**; ADR-0010 under
review on !28 (cross-reference, do not duplicate); member-to-member,
bank rails and the beneficiary registry are v2 with named activation
triggers.

---

## 6. Screens — buildability map (each with its blocking item)

**6.1 Buildable now (slice 1):**
* Onboarding + OTP auth + session machine — against `develop`
  (`/member/auth/*`). No blocker.
* Guarantor consent inbox — acts (`consent`/`release`) live on
  `develop`; list contract recorded on !31 (**blocked on #41 merging**;
  the screen's DoD row stays UNVERIFIED until then).
* T0 chrome: bottom nav, Favorites (greyed per tier), hide/reveal
  balances, inactivity logout, static info (#43 T0).

**6.2 Blocked on the §2 read/write surface:**
* Balances/Accounts → !7 (`/member/me`) + **#50** (per-account breakdown).
* Transactions + statement view → !7; statement export → #32.
* Loans list/detail/schedule → !7.
* Loan application + quote → **#51** (and the #30 write order: after #7).
* Profile self-service → #30 (second write; no tracker yet — deliberately,
  the write pattern lands with #7 first).
* Notifications center → #45 / ADR-0011 (!30).

**6.3 Blocked on P19:**
* Deposit via M-Pesa STK + intent status polling → **#7** (ADR-0008 on
  !19; implementation may proceed against sandbox per the 2026-08-20
  sign-off, behind `PaymentRailPort`).
* Repayments (money-in) → #7. Withdrawals/transfers → #44/ADR-0010
  (!28) + !17 member reads (#47 interaction) — explicitly not P17 v1
  screens beyond greyed tiles.

---

## 7. Failure-mode matrix (rule 15 — every row falsifiable)

| FM | Scenario | Required behaviour | Falsifiable check |
|---|---|---|---|
| FM-A token theft | Attacker exfiltrates stored tokens | Tokens only in Keystore/Keychain; access ≤15 min; server live-link re-check kills revoked credentials within one request; refresh reuse revokes the family | Test: token strings never appear in logs/crash payloads (log-sweep lint); integration: reused refresh → 401 → app lands on login with storage cleared |
| FM-B PIN bypass attempt | Client shipped with a local-only PIN, or biometric path skips the server | #46: PIN is SERVER-verified; biometric is a local wrapper around the same server round-trip, never a bypass | Repository fake asserts the step-up call fires for the gated action; removing the server call fails the test |
| FM-C offline stale cache | Airplane mode shows cached statement | Cached reads render with an explicit as-of marker; never presented as live; writes offline are refused, not queued | Golden test: stale banner present; unit: mutation attempted offline → typed failure, zero queued requests |
| FM-D envelope drift | Backend adds/renames a field or error shape | Generated client + contract tests against `openapi.json`; unknown error body → sanitized generic category, never a crash or a parsed guess | §9 codegen drift job red on regeneration diff; decode test with an alien body |
| FM-E cert rotation | Server key rotates / host migrates (#11) | ≥2 SPKI pins (backup key offline); new key pinned one release before serving; own keypair carried across the hosting exit | Pin-set unit test: connection with a cert signed by an unpinned key is a HARD failure; runbook check in #42 before first store binary |
| FM-F downgrade attacks | Old app version with stale pins/contract, or forced HTTP | HTTPS only (no cleartext traffic config); pin failure = hard error, no fallback; minimum-supported-version gate returned by the API is honoured (force-update screen) | Test: `http://` base URL refused at client construction; pin mismatch produces no request body on the wire (handshake-level abort) |
| FM-G double-tap submit | Consent/release tapped twice | One `Idempotency-Key` per logical attempt; button disabled in-flight | Integration: two rapid taps → exactly one server effect (side-effect count via the API, the P17(b) FM1 contract) |
| FM-H principal confusion | Member app given a staff URL/token path | Client names ONLY `/member/*` paths; FM1 server-side 403 both directions | §9 grep gate: any non-`/member` API path literal in `member_app`/generated member client fails CI |

---

## 8. Testing strategy

* **Contract tests against `openapi.json`** — generated client is
  exercised against recorded request/response fixtures derived from the
  spec snapshot; a spec change breaks the contract suite, not a screen.
* **Golden tests for `gp_ui`** — tokens/primitives pinned by goldens;
  a palette or typography drift is a red diff, not an opinion.
* **Repository fakes** — presentation/domain tested against
  hand-written repository fakes with HAND-COMPUTED oracles (never
  captured from the code under test, §4 anti-reward-hacking); the
  repository layer itself tested against a fake `GpHttpClient`.
* **No network in unit tests** — the test runner has no socket access
  to real hosts; anything needing wire shapes uses fixtures.
* **Integration tests** (Flutter `integration_test`) per flow as they
  become buildable: OTP happy path + lockout, consent double-tap
  (FM-G), airplane-mode statement read (FM-C) — each added in the MR
  that makes its screen real, never before its endpoint merges.

---

## 9. CI (mobile jobs added by the first P17 MR)

* `mobile:analyze` — `dart format --set-exit-if-changed` +
  `flutter analyze` (zero warnings, §3 of MASTER_PROMPT).
* `mobile:test` — unit + golden tests, no network.
* `mobile:codegen-drift` — regenerate `gp_api_client/lib/src/generated`
  from `web/packages/api-client/openapi.json` and `git diff
  --exit-code`: hand-edits or a stale snapshot are a red pipeline (the
  falsifiable P16 FM3).
* Guard gates: no-client-money-math grep/analyzer sweep; staff-path
  literal sweep (FM-H); token-in-log sweep (FM-A); import-boundary
  check (presentation ↛ gp_api_client).
* Jobs are `rules:exists`-gated on `mobile/**` per the existing
  `.gitlab-ci.yml` convention; they add named jobs only (the P16(d)
  collision discipline) and this docs-only MR adds none of them.

---

## 10. Scaffold defect register (fix in the first P17 MR)

Source: `mobile/` on branch `duo/feature/9-flutter-workspace-scaffold`
(migrated P16 draft; not on `develop`). Verified by reading the branch
at `2657f49`. If the first P17 MR reuses any of it, every row below is
a merge blocker:

| # | Defect | Evidence | Fix |
|---|---|---|---|
| D1 | **Principal confusion**: `member_app` auth calls STAFF endpoints `/auth/otp/request`, `/auth/otp/verify`, `/me/permissions`, `/auth/logout` with `{phone, otp}` bodies | `gp_api_client/lib/src/api/auth_api.dart`; `member_app/.../auth_repository.dart` | Member app speaks `/member/auth/*` only, `signin_identifier`/`code` bodies; no `/me/permissions`, no staff logout (member "logout" = local token discard) |
| D2 | **Pinning is fail-open and off-spec**: whole-cert SHA-256 (not SPKI — violates #42 Option 1); relies on `badCertificateCallback`, which never fires for a validly-CA-signed rogue cert; `bypassForTesting` flag in shipping code; `_sha256Fingerprint` is a placeholder stub | `gp_api_client/.../certificate_pinning.dart` | SPKI pin set (≥2 pins) enforced on EVERY handshake; no bypass flag; real digest implementation; hard-fail test per FM-E |
| D3 | **Hand-written "generated" client**: `api/` + `models/` are hand-written; no `lib/src/generated/`; regen script points at a non-existent staging URL instead of the repo `openapi.json` | `auth_api.dart`, `members_api.dart`, `mobile/scripts/regenerate_api_client.sh` | Generate from `web/packages/api-client/openapi.json`; hand-written code confined to `infrastructure/` + envelope models; drift job per §9 |
| D4 | **Cache not encrypted, no staleness**: doc-comment claims encrypted Hive but boxes open without an `encryptionCipher`; no as-of marker on cached reads | `member_app/lib/src/core/offline_cache.dart` | Encrypted box (key in secure storage), as-of timestamps, purge on logout/401 (FM-C) |
| D5 | **Session semantics contradict member-auth.md**: transparent refresh-on-401 retry loop against staff `/auth/token/refresh`; `hasTokens()` (mere presence) treated as authenticated; refresh errors swallowed with `catch (_)` | `gp_http_client.dart` (`_sendWithRetry`, `_tryRefresh`), `token_storage.dart` | Proactive refresh via `/member/auth/refresh`; ANY 401 = discard both tokens + return to login; no silent catch |
| D6 | **No `x-tenant-id` header** on pre-auth requests (all `/member/auth/*` require it), and no per-flavor tenant baking | `gp_http_client.dart` `_headers` | Tenant id from the flavor config (`core/env`), attached to pre-auth routes per member-auth.md §2 |
| D7 | **No idempotency-key generation discipline**: header forwarded only if a caller passes one; nothing generates or reuses keys across retries | `gp_http_client.dart` | `IdempotencyKey` helper in infrastructure: UUID per logical attempt, reuse-on-retry (FM-G) |
| D8 | **Member models hand-guessing shapes** (`member_models.dart`) that don't exist on any merged surface | `gp_api_client/lib/src/models/` | Delete; regenerate from the spec when !7 merges (§2 rule 1 — no screen against unmerged shapes) |

---

## 11. Delivery sequence (honest — honours the §2 blocking finding)

| MR | Content | Gated on |
|---|---|---|
| MR-0 | Workspace + packages: `gp_api_client` (generated from `develop`'s spec — the five `/member/*` routes), `gp_ui` tokens, `member_app` core (env/session/router/providers), CI jobs (§9), D1–D8 fixed or superseded | Nothing — buildable today |
| MR-1 | OTP auth slice: request/verify/refresh, session machine, token custody, inactivity logout | MR-0 |
| MR-2 | Guarantor consent inbox: list + consent/release acts, double-tap FM-G, 409-stale handling | **#41 merged** (contract from !31); DoD row UNVERIFIED until then |
| MR-3 | Reads: home/balances, transactions, statement (offline cache + staleness), loans + schedule | **!7 merged** (+ #31 limits in effect); per-account breakdown additionally on **#50** |
| MR-4 | Loan application + server quote | **#51** implemented; ordered after #7 per #30 |
| MR-5 | Deposits/repayments (STK intents + polling) | **#7 (P19) implemented**; #29 enforce for live money |
| — | Distribution/store work, pinning enforce against prod host, attestation client half | Store accounts (§5.B blocker), #42 runbook, #11 exit, #29 |

No MR ticks a DoD row for a screen whose endpoint is unmerged; each
blocked row carries its work-item link, unticked (rule 13).

---

## 12. Open questions for sign-off (NOT answered by §5)

1. **White-label flavor mechanics**: are per-SACCO pin sets served from
   per-tenant hostnames (one cert/keypair each) or one host for all
   tenants (one pin set)? #42 decided custody, not topology; this
   determines whether `CertificatePinning` config is per-flavor or
   global. (Interacts with #11's target architecture.)
2. **Minimum-supported-version gate** (FM-F): should the backend expose
   a version-floor signal (header or `/healthz` extension) so old
   clients can be force-updated? No current endpoint carries one; if
   yes it needs a backend work item before MR-1 hardening.
3. **OTP delivery in dev**: with #18 unresolved, member OTP in dev
   builds rides `DEV_OTP_DISPLAY`. Is that acceptable for MR-1
   integration tests on CI, or do we require the fake-provider seam
   only? (P16(b) honesty rule applies either way.)
4. **Statement offline retention**: how many pages / how many days may
   the encrypted cache retain (DPA 2019 data-minimisation posture)?
   Not decided by any ADR; needed before MR-3.
5. **`#43` T0 "share/download receipt data"**: rendering is client-side
   but the artifact source (server-rendered receipt vs screen dump) is
   undecided; if server-rendered it is a new read-surface item.

---

## DoD note for this MR (docs-only)

This review ships NO code, NO migrations, NO OpenAPI changes; the §5
DoD boxes that require them are N/A with that justification in the MR
description. It does not touch !7-lineage files
(`backend/src/genesis/api/member.py` and friends) or any lockfile.
