# Member mobile app (Flutter) — design

Status: **Design / P17 pre-implementation review (MASTER_PROMPT §5.9)**
Date: 2026-08-18
Scope: `mobile/member_app` + its share of `mobile/gp_api_client`, `mobile/gp_ui`.
Binding docs: `docs/MASTER_PROMPT.md` §1 (gates), §2.4 (mobile), §4 (testing);
`docs/BUILD_PROMPTS.md` P16 / P17; `docs/member-auth.md` (the P17 consumer guide).

This document is the §5.9 record for P17: reuse audit, layering, the
cross-cutting contracts, the failure-mode matrix, and the honest statement of
what is blocked. It is not an implementation; it is what the implementation
must satisfy.

---

## 1. Reuse audit (gate 1.1 — search before writing)

### 1.1 What already exists and MUST be built on, not replaced

| Asset | Where | Verdict |
|---|---|---|
| P16 Flutter workspace (49 files: `member_app`, `admin_app`, `gp_ui`, `gp_api_client`, `mobile/scripts/regenerate_api_client.sh`) | branch `duo/feature/9-flutter-workspace-scaffold`, merged into `feature/mobile-scaffold` | **Reuse.** P17 lands on top of it. A second scaffold is a rejected MR. |
| `mobile/gp_ui/lib/src/tokens/palette.dart` | same branch | **Reuse verbatim.** Values match `web/packages/design-system/src/tokens.ts` byte-for-byte (navy `#0F2C6B` … loss `#6E241B`). Do not add ad-hoc colors in member screens. |
| `TokenStorage` (keychain / EncryptedSharedPrefs) | `mobile/gp_api_client/lib/src/infrastructure/token_storage.dart` | **Reuse**, extend with the member key namespace (§4.1). |
| `CertificatePinning`, `GpHttpClient`, `ApiError` | `mobile/gp_api_client/lib/src/infrastructure/`, `.../models/api_error.dart` | **Reuse the shape, repair the behaviour** — see the defect register (§9). |
| Single-flight proactive refresh algorithm | [session.ts](../../web/src/modules/auth/session.ts) (`refreshInFlight`, `EXPIRY_MARGIN_SECONDS`) | **Port to Dart.** The web already solved the rotation race; the mobile client must not re-derive it. |
| Idempotency-Key stability/rotation contract | [index.ts:69-88](../../web/packages/api-client/src/index.ts#L69-L88) (`idempotencyKeyFor`) | **Port to Dart** with the same injectable `fresh` seam so the contract stays falsifiable. |
| Member auth flows, token semantics, error envelope | `docs/member-auth.md`; `backend/src/genesis/api/member.py` | **Consume as specified.** The OpenAPI snapshot is the binding contract. |
| `mobile:lint` / `mobile:test` CI jobs | `.gitlab-ci.yml` (gated on `exists: mobile/**/pubspec.yaml`) | **Reuse.** P17 adds only `mobile:integration` (§8). |

### 1.2 Prior-art carried over from the web admin, deliberately

Three doctrines transfer without change, because they are properties of the
API contract rather than of the web platform:

1. **Errors are `{category, correlation_id}`** — never parsed for business
   meaning, never rendered raw. Least disclosure (gate 1.6).
2. **Every mutation carries `Idempotency-Key`**, one key per *logical intent*,
   rotated only when the canonical body changes.
3. **No client money math** (gate 1.1 / P15) — every figure a member sees is a
   server-rendered value from the API.

---

## 2. Blocking finding: the member read surface does not exist yet

P17's prompt lists balances, statements, deposits, loan application, repayments,
notifications. The `genesis-member` audience today reaches **five endpoints only**
(`backend/src/genesis/api/member.py:43-136`):

```
POST /member/auth/otp/request
POST /member/auth/otp/verify
POST /member/auth/refresh
POST /member/guarantees/{guarantee_id}/consent
POST /member/guarantees/{guarantee_id}/release
```

Everything else a member would read is behind a **staff** permission. For
example `/members/{member_id}/statement` is guarded by
`RequirePermission(Module.MEMBERS, Action.VIEW)`
(`backend/src/genesis/api/members.py:303` + `:34-38`) — a member token cannot
satisfy it, and per `docs/member-auth.md` §1 the attempt is a 403 by design (FM1).
`payment_intents` (P19, M-Pesa) does not exist at all.

**Consequence for planning.** Exactly one P17 slice is buildable today: the
**OTP session + guarantor consent inbox**. Everything else needs backend work
first. The honest sequencing is in §10; the required backend surface is:

| Needed member-audience endpoint | Serves | Blocked on |
|---|---|---|
| `GET /member/me` (member id, status, branch, display name) | shell, greeting, status gating | new |
| `GET /member/me/balances` (shares, deposits, loan positions — server-rendered) | Balances screen | new |
| `GET /member/me/statement?cursor=&limit=` (keyset, ≤100) | Statements screen | new; mirror the staff shape at `members.py:303` |
| `GET /member/me/guarantees?status=PLEDGED` | consent inbox listing | new (the *acts* exist, the *list* does not) |
| `GET /member/me/loans/{id}/schedule` | repayment view, installment preview | new |
| `POST /member/me/loan-applications` + `POST …/quote` | loan application with live preview | new; preview MUST be server-computed |
| `POST /member/me/deposits/intents` + `GET …/intents/{id}` | M-Pesa STK, intent polling | **P19** |

Until those land, any "balances screen" in the member app would be reading a
staff endpoint with a staff token — which is precisely the principal confusion
P14.5 exists to prevent. This must be recorded UNVERIFIED in the DoD rather
than faked against a mock (BUILD_PROMPTS P16(b), rule 13).

---

## 3. Architecture

### 3.1 Package layout

```
mobile/
  gp_api_client/          # transport + generated models. NO Flutter widgets.
    lib/src/
      generated/          # GENERATED from openapi.json — never hand-edited
      infrastructure/     # GpHttpClient, TokenStorage, CertificatePinning, IdempotencyKey
      models/             # ApiError + hand-written envelopes not covered by codegen
  gp_ui/                  # tokens + primitives shared by both apps
  member_app/
    lib/src/
      core/               # providers, router, session, cache, biometrics, env
      features/<feature>/
        data/             # repository — the ONLY layer that talks to gp_api_client
        domain/           # immutable view models (no money arithmetic)
        presentation/     # screens + widgets (no direct API calls)
```

The layering mirrors the backend's `api → application → domain → infrastructure`
and the web's `screens → api.ts → schemas.ts`: **presentation never reaches past
its repository**, and the repository is the single place a Dart type meets an
API response.

### 3.2 State: Riverpod

- `AsyncNotifierProvider` per feature-screen; `Provider` for repositories and
  infrastructure singletons.
- **Family providers keyed by nothing member-identifying.** The member's own id
  comes from the server (`GET /member/me`), never from a client-supplied
  parameter — a `memberId` argument on a member-app request is the
  caller-asserted-identity anti-pattern the !29 lesson rejected.
- Providers are **session-scoped**: logout disposes the container subtree, which
  is the Dart equivalent of the web's `createSessionScopedRegistry.ts`. No
  provider may outlive a session and serve a later member's data (FM3).

### 3.3 Navigation

`go_router` with a single `redirect` bound to `sessionProvider`:
`unauthenticated → /login`, `authenticated → /` — and, because a 401 means the
credential link is dead server-side (`docs/member-auth.md` §1), any 401 from any
call collapses the session and lands on `/login`. There is no "re-enter your PIN"
soft state; the session is over.

---

## 4. Cross-cutting contracts

### 4.1 Session and token custody

Rules from `docs/member-auth.md` §3.4, made concrete:

- **Access token: memory only.** Never written to `TokenStorage`, never logged,
  never attached to a crash report. (The current scaffold persists it — §9 D1.)
- **Refresh token: `TokenStorage` only**, under a member-namespaced key
  (`gp_member_refresh`) distinct from the admin app's, so a device carrying both
  apps can never cross-feed a token into the wrong audience (FM4).
- **Single-flight, proactive refresh.** One `Future<String?>? _refreshInFlight`
  guards the whole app. Refresh fires when the access token has less than
  ~30 s of life left, *before* the request, not after a 401. Ported from
  [session.ts:118-150](../../web/src/modules/auth/session.ts#L118-L150).

  This is not a nicety. Refresh tokens rotate, and **reusing a spent one revokes
  the whole family**. Two screens refreshing concurrently on a cold resume would
  log the member out — the scaffold's per-request `_tryRefresh()` does exactly
  that (§9 D2).
- **Persist before use**: write the new refresh token to secure storage *before*
  the retried request goes out, so a crash mid-flight cannot strand the family.
- **401 ⇒ full logout**: discard both tokens, `clearAll()` the cache, dispose the
  session scope, re-run the OTP flow.
- **Biometric step-up** gates guarantee consent, release, and (later) any money
  movement. `local_auth`, checked at the moment of the act, never cached as an
  "unlocked for 5 minutes" flag. Biometry is a **local** gate — the server is the
  only authorizer; it never becomes an API parameter.

### 4.2 Request envelope

| Concern | Rule |
|---|---|
| `x-tenant-id` | **Required on all three pre-auth endpoints.** Currently never sent (§9 D3). Sourced from build config / the tenant chosen at first launch; it is not a secret and not a credential. |
| `Authorization` | `Bearer <access>` on member-principal calls only. The member app never requests, holds, or sends a staff scope (P16 FM4). |
| `Idempotency-Key` | On **every** mutation. One key per logical intent; reused verbatim on retry of an identical body; rotated when the body changes. Keys are scoped server-side per (tenant, principal, route), so another actor replaying a key can never read this member's response (FM5, `docs/member-auth.md` §2). |
| Errors | `ApiError{category, correlationId, statusCode}`. UI copy is chosen from `statusCode` + screen context, **never** from `category` text. Show `correlationId` in a support-copy affordance — it is the only thing worth surfacing. |
| Bodies | `extra="forbid"` server-side: send documented fields only, or take a 422. |

### 4.3 Offline read cache

Hive, read-only from the app's perspective (`offline_cache.dart` is a sound
starting point). Three additions the design requires:

1. **Every cached payload is stored with its `fetchedAt`**, and every screen
   rendering from cache shows an explicit *as-of* banner — "Showing balances as
   of 14:32, 18 Aug". Cached data is never presented as live (P17 FM2).
2. **Cache is encrypted and session-scoped.** Hive box keys derive from the
   member's server-provided id; `clearAll()` runs on logout *and* on any 401.
   A device that changes hands must not retain a prior member's statement page.
3. **Writes never queue in the member app.** Deposits, applications, consent are
   online-only acts with an idempotency key; there is no offline outbox on the
   member side (that is a P18 admin concern). An offline mutation attempt is a
   plain, honest "you are offline" — not a silent local success.

### 4.4 No client money math (gate 1.1)

- Amounts arrive as **strings** and stay strings end-to-end. No `double`, ever.
- No addition, subtraction, division, or comparison of amounts in Dart —
  including "helpful" totals, running totals, or a locally computed installment
  preview. The loan application's preview comes from the server quote endpoint;
  before it exists, the field shows nothing rather than an estimate.
- Enforcement: **port the existing web gate to Dart**, do not invent a new one.
  [no-money-math.test.ts](../../web/src/__tests__/no-money-math.test.ts) is the
  prior art — a grep gate that scans every source file for numeric coercion
  (`parseFloat`/`Number`/unary `+`) or arithmetic applied to a money-named
  identifier, over a `MONEY_FIELDS` list (amount, balance, deposits,
  outstanding, principal, installment, settlement, …). Two properties to carry
  across verbatim:
  - It **blanks string-literal contents before scanning** (quotes kept, escapes
    honoured), so exact API route keys containing `/` or `-` are not misread as
    arithmetic. The Dart port needs the same sanitizer for the same reason.
  - It is **proven non-vacuous**: the test asserts the gate flags synthetic
    offending samples, so deleting a guard regex fails the suite. The sanitizer
    itself is falsified the same way. Reproduce both, or the port is a test that
    cannot fail (a rejected test, MASTER_PROMPT §4).

### 4.5 Certificate pinning

Pinning failure is a **hard connection error** with no bypass, no
"continue anyway", and no fallback to an unpinned client (P16 FM2). Pins are
built into the app with a backup pin, and pin rotation ships a release ahead of
the server change.

---

## 5. Screens

Ordered by what the API can actually support today.

### 5.1 Buildable now

**Login (email → OTP).** Email entry → `POST /member/auth/otp/request` →
*always* the same "if that address is registered, a code is on its way" copy,
regardless of outcome; the endpoint answers 202 unconditionally by design and
the UI must not leak more than it does. Then 6-digit entry →
`POST /member/auth/otp/verify`. Code TTL 5 minutes, single use, ≤5 attempts;
the UI shows a countdown and a resend, and never displays a remaining-attempts
counter (that is a disclosure the server does not make).

**Guarantor consent inbox.** Pledged guarantees where this member is guarantor.
Each row: borrower, amount (server-rendered string), the exposure this pledge
creates, `version`. Two acts, both biometric-gated, both idempotency-keyed:
consent and release. `409` ⇒ "this changed while you were looking" + refetch;
`403` ⇒ a single neutral "this is no longer available to you" (the server
deliberately does not distinguish *not yours* from *not consentable* — §3.1 of
the consumer guide, and the UI must not invent the distinction).

**Session shell.** Splash → session probe → router redirect; profile with logout;
support screen surfacing the last `correlation_id`.

### 5.2 Blocked on the §2 backend surface

Balances (shares, deposits, loan positions with an as-of stamp) · Statements
(keyset-paginated, offline-cached, as-of labelled) · Loan application with a
server-computed installment preview · Repayments · Notifications.

### 5.3 Blocked on P19

Deposit via M-Pesa STK: intent creation, then **polling bound to the returned
intent id**. A status callback for a different intent can never mark this one
paid (P17 FM4); the poll compares intent ids and discards anything else.

---

## 6. Failure-mode matrix (rule 15 — every row falsifiable)

| # | Failure mode | Guard | Test that fails when the guard is removed |
|---|---|---|---|
| FM1 | Double-tap submit creates two effects | One idempotency key per intent, held for the widget's lifetime | Integration: tap consent twice within one frame → API side-effect count is 1 (assert on the server's guarantee-event rows, never on the widget's return value) |
| FM2 | Offline data shown as live | `fetchedAt` stored with every cached payload; as-of banner mandatory | Widget test: render a screen from cache with no network → banner present with the stored timestamp. Delete the banner ⇒ test fails |
| FM3 | Cross-member leak | No member id in any request; session-scoped providers + cache purge on logout | Integration: log in as A, log out, log in as B → zero A-provenance rows in B's cache or UI |
| FM4 | Principal confusion (staff token in the member app) | Separate storage namespace; the member app compiles against a member-only API surface | Test: inject a staff token → every member call is 403 and the session collapses; app never renders staff data |
| FM5 | Refresh-family self-revocation | Single-flight refresh mutex + persist-before-use | Unit: 10 concurrent 401-triggering calls → exactly **one** `/member/auth/refresh` request observed. Remove the mutex ⇒ 10 requests, family revoked |
| FM6 | Token leak into logs / crash reports | Access token never persisted; log redaction; lint gate | Lint gate + test: force an error path with a live session → assert no token substring in any emitted log line |
| FM7 | Local money math | String-only amounts; grep + custom-lint gate | Gate fixture performing arithmetic on a money field must fail CI |
| FM8 | Pin bypass | Hard failure, no fallback client | Test against a server with a wrong certificate → connection error, no request body ever sent |
| FM9 | Payment intent confusion (P19) | Poll compares intent id | Integration: feed an out-of-order status for a different intent → this intent stays pending |

Per MASTER_PROMPT §4, oracles are hand-computed and documented in comments, and
idempotency is asserted by **side-effect row counts**, never by return values.

---

## 7. Testing strategy

- **Unit** (`flutter test`): session/refresh state machine, idempotency key
  rotation contract, `ApiError` decoding (including empty and non-JSON bodies),
  cache as-of stamping.
- **Widget**: each screen against a faked repository — loading, empty, error,
  offline-with-banner, and the 403/409 branches.
- **Integration** (`integration_test`, Android + iOS matrix): the OTP flow, the
  consent flow, airplane-mode statement read, double-tap submit. These run
  against a recorded-contract fake at the HTTP boundary — the mobile analogue of
  the web's Playwright-with-network-mocked posture (`web/e2e/`).
- **Contract**: `gp_api_client` regenerated from `web/packages/api-client/openapi.json`
  and diffed byte-for-byte in CI (P16 FM3). The generated tree is never
  hand-edited — same hard rule as `schema.d.ts`.

---

## 8. CI

`.gitlab-ci.yml` already carries `mobile:lint` and `mobile:test`, gated on
`exists: ["mobile/**/pubspec.yaml"]`. P17 adds:

- `mobile:client-drift` — regenerate `gp_api_client`, diff, fail on drift.
- `mobile:integration` — the Android + iOS matrix.

**No new job is needed for the money-math gate.** The web gate is a *test*
(`web/src/__tests__/no-money-math.test.ts`) running inside `web:test`, not a
standalone CI job — which is why the job list shows none. The Dart port
(§4.4) is likewise a test under the existing `mobile:test`, keeping the
`mobile:*` surface to lint / test / client-drift / integration.

Named `mobile:*` only; no shared-job edits (the P16 collision-surface rule).
Ships **no migration**. Pub proxy blocks, if hit, are recorded honestly per
rule 16 rather than retried.

---

## 9. Scaffold defect register (fix in the first P17 MR)

Found by reading `duo/feature/9-flutter-workspace-scaffold`. The scaffold
predates P14.5 and encodes a phone-based OTP flow that the backend never shipped.

| # | Defect | Evidence | Fix |
|---|---|---|---|
| D1 | Access token persisted to secure storage and read on every request | `token_storage.dart` `_accessKey`; `gp_http_client.dart` `_headers()` | Access token in memory only; storage holds the refresh token alone |
| D2 | Per-request refresh with no mutex | `gp_http_client.dart` `_tryRefresh()` | Single-flight + proactive refresh (§4.1). As written, concurrent 401s revoke the refresh family |
| D3 | `x-tenant-id` never sent | `_headers()` has no tenant header | Required on all pre-auth calls; without it the OTP endpoints cannot resolve a tenant |
| D4 | Wrong auth contract: `phone`/`otp` fields, `POST /auth/token/refresh` | `auth_repository.dart`, `_tryRefresh()` | Real contract is `{email}` / `{email, code}` and `POST /member/auth/refresh` (`docs/member-auth.md` §2) |
| D5 | `logout()` calls a member logout endpoint that does not exist | `auth_repository.dart` | Local-only logout: clear tokens, purge cache, dispose session scope |
| D6 | Blind retry after refresh re-POSTs a mutation | `_sendWithRetry()` | Safe only because the caller's idempotency key is forwarded — make that a documented invariant with a test, and never retry a mutation lacking a key |
| D7 | `_decode` calls `jsonDecode` unconditionally | `gp_http_client.dart` | 204 / empty / non-JSON bodies must not throw a decode error; surface a typed transport error |
| D8 | Cache not encrypted, no `fetchedAt`, keyed by a client-held `memberId` | `offline_cache.dart` | Encrypted box, as-of stamp per payload, id from `GET /member/me` |
| D9 | `phone_screen.dart` naming and copy throughout | both apps | Rename to email; the member principal is an email-linked credential row |

Also: reconcile the open draft **!11** — supersede it or close it with a stated
reason. No second parallel scaffold (P16(a)).

---

## 10. Delivery sequence

| MR | Content | Depends on |
|---|---|---|
| **M0** | Land the P16 scaffold on `develop`; apply defect register D1–D9; port single-flight refresh and the idempotency contract; add `mobile:client-drift` | !11 reconciled |
| **M1** | Member session slice: email OTP, session shell, router redirect, biometric plumbing, FM1/FM4/FM5/FM6/FM8 tests | M0 |
| **M2** | Guarantor consent inbox — **needs `GET /member/me/guarantees`** (backend, small) | M1 + that endpoint |
| **M3** | `GET /member/me` + balances + statements — **backend member read surface first** (§2) | §2 endpoints |
| **M4** | Loan application with server-computed preview; repayments | M3 + quote endpoint |
| **M5** | M-Pesa deposits, STK intent polling, FM9 | **P19** |

M0 and M1 are startable today. M2 needs one small backend addition. M3–M5 are
gated on the §2 surface and P19, and their DoD lines stay unticked until then —
issue #11 (staging API) governs the boot-against-staging criterion the same way.

---

## 11. Open questions for sign-off

1. **Tenant selection.** Does the member app ship one tenant per build, or does
   the member pick/enter a SACCO at first launch? This determines whether
   `x-tenant-id` is build config or user state, and it is a trust-boundary
   question for `docs/diagrams/dfd.md` (TB1M).
2. **`GET /member/me/*` vs. reusing staff paths with a member guard.** The
   design assumes a dedicated member surface — no member-supplied id, ever. Worth
   an ADR, since it sets the pattern for every future member endpoint.
3. **Notification transport** — push (FCM/APNs) or in-app pull only? Push adds a
   trust boundary and a PII-in-payload question that P17's prompt does not settle.
4. **Rule 11 diagram updates**: this design changes no schema, but M3 adds a
   member trust-boundary flow — P-DIAG.3 (`dfd.md` TB1M) and the C4 L1 container
   view update in the same MR that ships it.
