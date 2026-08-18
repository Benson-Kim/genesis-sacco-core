# Member mobile app (Flutter) — design brief and readiness gates

Status: pre-implementation. The BLOCKING dependency is ADR-0007 (member
read surface): until it lands, the app can authenticate and act on
guarantees and nothing else. Do not scaffold screens against staff
endpoints; a member token is a 403 there by design (FM1,
`docs/member-auth.md` §1).

## 1. Available vs required surface

| P17 requirement | Endpoint today | Status |
|---|---|---|
| Login (OTP) | `POST /member/auth/otp/request` / `verify` / `refresh` | EXISTS |
| Guarantee consent/release | `POST /member/guarantees/{id}/consent` / `release` | EXISTS |
| Balances | — | BLOCKED → ADR-0007 (`GET /member/me`) |
| Statements | — (staff-only route) | BLOCKED → ADR-0007 |
| Transaction history | — | BLOCKED → ADR-0007 |
| Loan status / schedule | — | BLOCKED → ADR-0007 |
| Deposits / repayments (M-Pesa) | — (`payment_intents` absent) | BLOCKED → P19 issue |
| Notifications | — (outbox is delivery infra, not a member read model) | BLOCKED → deferred in ADR-0007 |

## 2. Device security requirements (non-negotiable)

- Token custody: access and refresh tokens live ONLY in
  Keychain/Keystore-backed secure storage. Never SharedPreferences,
  NSUserDefaults, files, or logs. (ADR-0005's httpOnly-cookie decision
  is a browser control; on mobile the platform secure enclave is the
  equivalent custody boundary.)
- Certificate pinning against the API host, with a backup pin and a
  documented rotation procedure — an un-rotatable pin is an outage.
- Device attestation (Play Integrity / App Attest) verified server-side
  as a precondition for `POST /member/auth/otp/request`. Threat model:
  automated/bot traffic is expected to EXCEED human traffic; the OTP
  request route is an SMS-cost and harassment vector without
  attestation (see issue #1, security telemetry).
- Statement/balance screens: flag as sensitive (FLAG_SECURE /
  screenshot obscuring); no member financial data cached at rest
  unencrypted; cache is session-scoped and purged on logout/401.
- Any 401 = session over: discard both tokens, return to login
  (`docs/member-auth.md` §3).

## 3. Package audit policy (pub.dev)

Every dependency, before adoption:
- Verified publisher or first-party Google/Dart team package; active
  maintenance (commit within 6 months); no unresolved security issues.
- No transitive analytics, advertising, or network SDKs — audit the
  full resolved tree, not the direct list.
- `pubspec.lock` is committed from the first commit; security-relevant
  packages (secure storage, pinning, crypto) are EXACT-pinned.
- The dependency list is reviewed in every MR that touches `pubspec.yaml`;
  additions require a one-line justification in the MR description.
- Baseline set (audit before use, subject to the criteria above):
  secure storage, HTTP client, pinning support, state management —
  chosen for maintenance status, not popularity contests. Fabricating
  nothing here: final names are selected at scaffold time against the
  live registry.

## 4. Contract discipline

The OpenAPI snapshot (`web/packages/api-client/openapi.json`) is the
binding contract for the mobile client generator exactly as it is for
the web client; the app never hand-writes request/response types. Error
envelopes are `{category, correlation_id}` — never parse error text
(least disclosure). Send `Idempotency-Key` on every mutation.
