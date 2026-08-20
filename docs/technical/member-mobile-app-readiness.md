# P17 Member mobile app — readiness register & start plan

Compiled 2026-08-20 from the `actte-group/sacco` project export
(`2026-08-20_16-27-431_actte-group_sacco_export.tar.gz`: 48 issues, 34 MRs, 95 pipelines,
`project.bundle` with 173 branches) **reconciled against `Benson-Kim/genesis-sacco-core`**,
which is 2 commits ahead of the export.

**Authoritative plan:** `docs/design/p17-member-app-pre-implementation-review.md` on branch
`duo/docs/p17-member-app-pre-implementation-review` (commit `853395d6`). That review is the
MASTER_PROMPT §5.9 record and it **supersedes** the older `member-mobile-app-design.md`
sitting untracked in this working copy — different defect register (D1–D8, not D1–D9),
different delivery sequence, and it records owner sign-offs the older doc predates.

---

## 0. Repo reconciliation — done

| Check | Result |
|---|---|
| GitHub `Benson-Kim/genesis-sacco-core` vs GitLab export | **GitHub is 2 commits AHEAD**, fast-forward, zero divergence. Only `develop` differed (`109f698b` vs `ac9a12e3` — !33/#34 merged after the export snapshot). |
| Branch inventory | 176 on GitHub vs 173 in the export. GitHub-only: `duo/docs/6-adr-0008-jwt-asymmetric-signing`, `duo/fix/48-seed-dev-sql-guard`, and **`duo/docs/p17-member-app-pre-implementation-review`** — the authoritative P17 plan. |
| Local `develop` | Was 13 commits behind. **Fast-forwarded to `109f698b`.** |
| Credentials | Git's system helper resolved to the `benson-priori` account, which cannot see this private repo. Fixed repo-locally: `credential.https://github.com.helper = !gh auth git-credential` with `username = Benson-Kim`. No token is stored in the repo. |

**Nothing needs pushing to reconcile the mirrors** — the GitLab → GitHub direction is already
current. The export bundle is also fetched here as `refs/remotes/export/*` for archaeology.

---

## 0b. CI is not currently an arbiter — owner action

**GitHub Actions is blocked on billing across the whole repository.** Every
workflow run fails before starting, with:

> The job was not started because recent account payments have failed or your
> spending limit needs to be increased.

This is **pre-existing and repository-wide**, not caused by any mobile change:
every `backend`, `web` and `security` run on `develop` today (17:32, 18:55,
19:06 UTC) failed the same way, as does the MR-0 pull request.

Consequences while it stands:

1. **No Dart in this repository has ever been compiled or tested.** MR-0's tests are written to be falsifiable, but they are unexecuted. The house rule "let CI arbitrate" has no arbiter.
2. GitLab (`actte-group/sacco`) is where the green pipelines cited across the issue tracker actually ran. This working copy has **no GitLab remote and no GitLab token**, so that arbiter is unreachable from here too.

Three ways out, any one sufficient: settle the GitHub billing; configure a
GitLab remote plus token so branches can be pushed where the runners work; or
install the Flutter SDK locally so `flutter analyze` and `flutter test` can run
before anything is pushed. The last is the only one that does not require an
account change, and it is the only one that gives fast feedback while building.

---

## 1. The member API contract

Money is a server-rendered decimal string on every shape below. Every list is keyset-paginated,
`limit` clamped to 100, cursors minted in member-own scopes a staff cursor cannot cross.

### 1.1 Merged on `develop` — 5 routes

```
POST /member/auth/otp/request     202, {"status":"sent"} unconditionally
POST /member/auth/otp/verify      -> TokenResponse
POST /member/auth/refresh         -> TokenResponse, rotating; reuse revokes the family
POST /member/guarantees/{id}/consent
POST /member/guarantees/{id}/release
```

**Body fields — corrected 2026-08-20 against `api/auth.py`, not the route body.**
An earlier reading of this register said `{signin_identifier}`. That was wrong:
`signin_identifier` is a server-side **property** on `OtpIdentifierBody`, not a wire field.
The actual contract is:

- `POST /member/auth/otp/request` — `{identifier}` **or** `{email}`, **exactly one**. Sending both, or neither, is rejected by a model validator.
- `POST /member/auth/otp/verify` — the same identifier envelope plus `{code}` (six digits, `^\d{6}$`).
- `POST /member/auth/refresh` — `{refresh_token}` (16–512 chars).
- Both act bodies carry `{version}` and nothing else (`extra="forbid"` — the principal *is* the authenticated credential).
- `x-tenant-id` is required on all three pre-auth routes.

Two consequences for the app:

1. **Send `identifier`, not `email`.** The docstring marks `email` as "the earlier field, accepted for one more release" — a client shipping `email` is coding against a field with a stated removal horizon.
2. **`identifier` is not an email address.** It accepts an email *or* a Kenyan mobile number in local (`07XX`/`01XX`) or international (`+2547XX`/`+2541XX`) form. The old design doc's D9 ("rename `phone_screen` to email") is therefore itself outdated — the field is an identifier, and the screen should ask for one, accepting either.

### 1.2 In the queue — the !7 stack

All four pipelines green, all Draft. Order is a real dependency chain fixed by #19; each
retargets `develop` only after !7 merges.

| # | MR | Adds |
|---|---|---|
| 1 | **!7** `adr0007-member-read-surface` | `GET /member/me`, `/member/transactions`, `/member/loans`, `/member/loans/{loan_id}`, `/member/statement` |
| 2 | **!25** `31-member-read-rate-limit` | Per-credential limits — **120/min accepted as the launch limit**, shared bucket, env-tunable, 429 metered |
| 3 | **!27** `33-defense-in-depth-followups` | Maintenance-DSN fencing, ANALYZE fail-open, scheduler hardening |
| 4 | **!31** `41-member-guarantees-list` | `GET /member/guarantees?status=` — the consent-inbox data source |

The OpenAPI snapshot on !31's branch carries all **11 member paths**. MR-0's generated client
comes from **develop's** spec (5 routes); the rest regenerate as each MR merges.

### 1.3 Response shapes on the !7 stack

- `MemberMeOut` — `member_no, name, status, deposit_balance, share_balance, loans{count, total_outstanding}`. Aggregates only.
- `MemberTransactionOut` — `id, txn_ref, type, amount, channel, direction, occurred_at, is_reversal, external_ref`
- `MemberLoanOut` — `id, loan_ref, product_name, principal, balance, rate_pct, term_months, status, days_past_due, penalty_due, disbursed_at, closed_at`; the detail shape embeds `schedule[]`
- `StatementLineOut` — `occurred_at, txn_ref, type, channel, amount`. Cursor-only, no date range by design (#32)
- `MemberGuaranteeOut` — `id, loan_ref, amount, status, version`. Staff fields subtracted: no borrower PII, no internal UUIDs

### 1.4 Two gaps the review filed as new issues

- **#50** — `GET /member/me/balances`, the per-account breakdown (share capital vs deposits vs fixed deposits). !7 ships aggregates only; nothing anywhere ships the breakdown.
- **#51** — `POST /member/loan-applications` + the server-computed quote. #30 is a sequencing ledger, not an implementation tracker.

---

## 2. The Flutter scaffold — reuse is a merge blocker until D1–D8 are fixed

`duo/feature/9-flutter-workspace-scaffold` (also `feature/mobile-scaffold`) carries 49 files:
`member_app`, `admin_app`, `gp_ui`, `gp_api_client`, `mobile/scripts/regenerate_api_client.sh`.
Stack: Riverpod 2.5, go_router 13, flutter_secure_storage 9, hive_flutter, local_auth 2.2, uuid 4.4.
Never merged to `develop`.

Revised register from the §5.9 review, verified at scaffold commit `2657f49`:

| # | Defect | Fix |
|---|---|---|
| **D1** | **Principal confusion** — `member_app` calls **staff** endpoints `/auth/otp/request`, `/auth/otp/verify`, `/me/permissions`, `/auth/logout` with `{phone, otp}` bodies | Member app speaks `/member/auth/*` only, `signin_identifier`/`code`; no `/me/permissions`; member logout is a local token discard |
| **D2** | **Pinning fail-open and off-spec** — whole-cert SHA-256 rather than SPKI, relies on `badCertificateCallback` (never fires for a validly-CA-signed rogue cert), ships a `bypassForTesting` flag, and `_sha256Fingerprint` is a stub | SPKI pin set (≥2 pins) enforced on every handshake; no bypass flag in shipping code; real digest; hard-fail test per FM-E |
| **D3** | **Hand-written "generated" client** — no `lib/src/generated/`; the regen script points at a non-existent staging URL | Generate from `web/packages/api-client/openapi.json`; hand-written code confined to `infrastructure/` and envelope models; drift job in CI |
| **D4** | **Cache not encrypted, no staleness** — the doc-comment claims encrypted Hive but boxes open without an `encryptionCipher` | Encrypted box (key in secure storage), as-of stamps, purge on logout/401 |
| **D5** | **Session semantics contradict member-auth.md** — transparent refresh-on-401 retry loop against the staff refresh route; `hasTokens()` treated as authenticated; refresh errors swallowed by `catch (_)` | Proactive refresh via `/member/auth/refresh`; **any 401 = session over**, discard both tokens and return to login; no silent catch |
| **D6** | **No `x-tenant-id`** on pre-auth requests, and no per-flavor tenant baking | Tenant id from the flavor config (`core/env`), attached to pre-auth routes |
| **D7** | **No idempotency-key discipline** — the header is forwarded only if a caller supplies one | `IdempotencyKey` helper: UUID per logical attempt, reused across retries |
| **D8** | **Hand-guessed member models** for shapes that exist on no merged surface | Delete; regenerate from the spec when !7 merges |

---

## 3. Owner sign-offs dated 2026-08-20 — binding

- **Tenant strategy**: one **white-label app per SACCO**. Per-tenant bundle id, name, icon, tokens and pin set from a **build-time flavor config**. A runtime tenant switcher is rejected as a client-side principal-confusion class.
- **Stack**: Flutter + Riverpod. **Android minSdk 26+, iOS 15+** (hardware-backed keystore / App Attest floor). Environments: dev-only for now.
- **⚠️ Google Play Console and Apple Developer accounts DO NOT EXIST YET.** An explicit external blocker on the critical path: Play Integrity needs the Play Console app entry, App Attest needs the Apple team and bundle registration (#29), and no binary is distributed without them. **Apple D-U-N-S verification alone can take weeks — starting these is an owner action, today.**
- **#42 pinning: Option 1** — own-key SPKI pins, offline backup pin, custody carried through the #11 hosting exit. *(See §6 — this contradicts a later instruction.)*
- **#29 attestation**: Google Cloud and Apple credentials will be provisioned; the **enforce-mode flip is a hard precondition for the first paid SMS** (#18).
- **#46 PIN: approved as a SERVER-VERIFIED factor** (Argon2id-class, server-side attempt counters, step-up layered on the OTP session). *(See §6 — this contradicts ADR-0012 on !29.)*
- **#31**: 120/min launch member-read limit.
- **#44 money-out v1**: M-Pesa B2C to the member's own verified MSISDN plus own-account internal transfers. Member-to-member, bank rails and the beneficiary registry are v2.
- **Provider decisions pending, each behind a plug-and-use interface** (seam now, vendor later, no vendor type past the adapter): #18 `SmsGateway`, #39 telemetry port, #11 hosting (rubric fixed: Redis required, customer-held TLS keys, PITR, surge headroom), #7 `PaymentRailPort` (Daraja sandbox creds and an owned paybill committed; sandbox green is *not* the release gate), #10 `SanctionsScreeningPort`.

### Design records already decided — do not re-litigate

| Record | Decision |
|---|---|
| **ADR-0011** notifications (!30, #45) | A dedicated `member_notifications` read model. The outbox is delivery infrastructure and must never be exposed as member state. Rendered-safe text at write time; `GET /member/notifications` under scope `member.notifications.list`; no fabricated history |
| **ADR-0010** money-out (!28, #44, ratified #47) | Async notice/earmark state on a new `payout_intents` lifecycle: `created → held_notice → held → submitted → paid \| failed \| expired`. `withdrawal_holds` stays the staff-channel notice machine, untouched; the one-register ALTER was rejected |
| **ADR-0008** payment intents (!19, #7) | M-Pesa Daraja STK push, money-in. ⚠️ **Must renumber to 0009 before merge** — develop took 0008 for JWT signing and 0010–0012 are claimed |
| **#43** capability register | Tiered T0–T4. Binding: no screen ships against an unmerged endpoint; no capability is promised in UI copy before its tier clears; a Favorites tile never launches an unbuilt flow, and tier state is a build-time capability map, not a runtime probe |

### Catalogue promises the codebase overrules (#43)

- "Estimate my installments" client-side — **banned**. Server quote or nothing (gate 1.1).
- "Set transaction limits" — caps are tenant-owned; members may only lower.
- "Verify recipient before sending" — an enumeration oracle unless masked, rate-limited and step-up-gated.

---

## 4. Package layout (exact — deviations are review findings)

```
mobile/
  gp_api_client/            # transport + generated models. NO Flutter widget imports.
    lib/src/generated/      # openapi_generator output from web/packages/api-client/openapi.json
                            # NEVER hand-edited; CI diffs a regeneration
    lib/src/infrastructure/ # GpHttpClient, TokenStorage, CertificatePinning, IdempotencyKey
    lib/src/models/         # ApiError + hand-written envelopes ONLY
  gp_ui/                    # tokens + primitives shared by member_app and admin_app
  member_app/
    lib/src/core/           # providers, router, session machine, cache, biometrics,
                            # env (per-flavor: base URL + tenant id baked)
    lib/src/features/<name>/
      data/                 # repository — the ONLY layer touching gp_api_client
      domain/               # immutable view models; money = validated strings, no arithmetic
      presentation/         # widgets/screens — never reaches past its repository
```

Dependency direction inward: `presentation → domain ← data → gp_api_client`. A `presentation`
import of `gp_api_client`, or of another feature's `data/`, fails the import-boundary lint.

**CI jobs the first P17 MR adds** (`rules:exists` on `mobile/**`, named jobs only):
`mobile:analyze` (format + `flutter analyze`, zero warnings), `mobile:test` (unit + golden,
no network), `mobile:codegen-drift` (regenerate and `git diff --exit-code`), plus guard sweeps
for client money math, staff-path literals, tokens in logs, and import boundaries.

---

## 5. Delivery sequence

| MR | Content | Gated on |
|---|---|---|
| **MR-0** | Workspace + packages: `gp_api_client` generated from develop's spec (the 5 `/member/*` routes), `gp_ui` tokens, `member_app` core (env/session/router/providers), CI jobs, D1–D8 fixed or superseded | **Nothing — buildable today** |
| **MR-1** | OTP auth slice: request/verify/refresh, session machine, token custody, inactivity logout | MR-0 |
| **MR-2** | Guarantor consent inbox: list + consent/release acts, double-tap FM-G, 409-stale handling | **#41 merged**; DoD row UNVERIFIED until then |
| **MR-3** | Reads: home/balances, transactions, statement (offline cache + staleness), loans + schedule | **!7 merged** (+ #31 limits live); per-account breakdown additionally on **#50** |
| **MR-4** | Loan application + server quote | **#51** implemented; ordered after #7 per #30 |
| **MR-5** | Deposits/repayments (STK intents + polling) | **#7 implemented**; #29 enforce for live money |
| — | Distribution, pinning enforce against the prod host, attestation client half | **Store accounts**, #42 runbook, #11 exit, #29 |

No MR ticks a DoD row for a screen whose endpoint is unmerged; each blocked row carries its
work-item link, unticked.

---

## 6. Two contradictions — resolved 2026-08-20

Both were surfaced to the owner and decided. Recorded here because each
contradicts a document still live in the repo, and the losing document must be
corrected rather than left to resurface.

### 6.1 Certificate pinning — hybrid (own-key SPKI custody now, enforcement after cutover)

The §5.9 review records #42 **Option 1**; the owner separately asked for
**log-only until cutover**. **Decision: both, explicitly.** Adopt Option 1
custody now — pin the SPKI of a keypair we own, with an offline backup pin,
carried through the #11 hosting exit — while enforcement ships as
`PinEnforcement.report` until the cutover completes, then flips to `enforce`.

As built in MR-0:
- `CertificatePinning` pins the DER SubjectPublicKeyInfo, never the whole certificate, so a reissued cert for the same key keeps its pin.
- The check runs on the connected socket via `HttpClient.connectionFactory`, before any request byte is written. The scaffold's `badCertificateCallback` fires only when platform chain validation has *already* failed, so it never sees a rogue cert that some CA validly signed — the exact attack pinning exists to stop.
- Two pins minimum, enforced by a **throw, not an assert**: asserts compile out of release builds, so an assert would let exactly the builds that matter ship without a backup key.
- **No bypass flag.** The mode is a build-time flavor constant, so this is not the runtime toggle D2 flagged as a defect.

**Outstanding:** the real keypair does not exist yet. `Flavor.dev` carries two
well-formed placeholder digests that match no key — which is itself why it must
ship `report`. Generating the primary and backup keypair, and storing the backup
offline, is a #42 deliverable before the first store binary.

### 6.2 PIN role — server-verified factor (the sign-off wins)

ADR-0012 on !29 decides local-unlock-only; the §5.9 review records a sign-off
approving a server-verified factor. **Decision: server-verified.** The PIN is a
real second factor — Argon2id-class verifier, server-side attempt counters,
lockout, step-up layered on the OTP session.

Two consequences that need owner action:

1. **ADR-0012 on !29 must be amended before it merges.** It currently records the losing option as the decision. Left alone, the repo carries two contradictory records of the same 2026-08-20 decision, and the next reader has no way to tell which is live.
2. **No work item tracks the backend verifier.** A server-verified PIN needs new endpoints, a hashing/verification service, attempt counters and a lockout design — none of which appears in any of the 34 merge requests or 48 issues in the export. This needs filing before MR-1 hardens the session machine, because MR-1's state machine differs materially between the two options.

MR-0 is unaffected either way: it ships the session machine and token custody,
and neither depends on which factor sits in front of them.

### Still open from the review (§12)

- Per-SACCO pin-set topology: one cert/keypair per tenant hostname, or one host for all tenants? Determines whether `CertificatePinning` config is per-flavor or global.
- A minimum-supported-version signal from the backend for force-update (no endpoint carries one today) — needs a backend work item before MR-1 hardening if wanted.
- Whether dev-build OTP may ride `DEV_OTP_DISPLAY` on CI for MR-1 integration tests, or the fake-provider seam only.
- Statement offline retention: how many pages/days may the encrypted cache hold (DPA 2019 minimisation)? Needed before MR-3.
- #43 T0 "share/download receipt": server-rendered artifact or screen dump? Server-rendered makes it a new read-surface item.
