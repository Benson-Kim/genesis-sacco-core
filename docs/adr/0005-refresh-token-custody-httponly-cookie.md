# ADR-0005: Refresh-token custody moves to an httpOnly cookie

- Status: Proposed
- Date: 2026-08-18
- Deciders:

## Context
The web app keeps the access token in memory (correct) but persists the
refresh token in `sessionStorage` (`web/src/modules/auth/session.ts`,
`gp.refresh_token`). Any successful XSS can exfiltrate it. Existing
mitigations are real — 15-minute access tokens, rotation on every use,
family revocation on reuse (`backend/src/genesis/application/auth.py`) —
but they bound the damage window; they do not remove script-readable
custody of a 14-day credential in a financial system. The AI-era threat
model assumes automated, scaled exploitation of any script-readable
secret, not a lone human attacker.

## Decision
The backend issues the refresh token as an `httpOnly; Secure;
SameSite=Strict` cookie, path-scoped to the auth endpoints, set on
`/auth/otp/verify` and rotated on `/auth/refresh`. The response body
stops carrying `refresh_token`. The frontend deletes all refresh-token
storage and reads; `getValidAccessToken()` calls `/auth/refresh` with
`credentials: include` and receives only a new access token in the
body. Logout becomes a server-side cookie clear plus family revocation.
The API must be served same-site with the web app (subdomain of the
same registrable domain) so `SameSite=Strict` holds; CSRF exposure on
the cookie-bearing endpoints is closed by SameSite plus a required
custom header (the client already sends `x-tenant-id` on every call,
which cross-site form posts cannot).

## Alternatives considered
- Keep sessionStorage custody — rejected: script-readable custody of a
  long-lived credential is the single largest client-side risk; rotation
  limits but does not remove it.
- localStorage with shorter refresh TTL — rejected: strictly worse
  (persists across tabs/restarts) for the same readability.
- Full BFF (backend-for-frontend session proxy) — rejected for now:
  correct end-state for a larger estate, but disproportionate to the
  current single-app deployment; this ADR is forward-compatible with it.

## Consequences
Positive: refresh token becomes unreachable from any script context;
token-theft pivots to reuse-detection territory the family-revocation
design already covers. Negative: CORS moves to credentialed mode with an
explicit origin allowlist; e2e and network wire tests must model the
cookie; local dev needs same-site host aliases. Migration: additive
first (cookie set alongside body field for one release), frontend
switches, then the body field is removed. Rollback: re-enable the body
field; cookies expire naturally.
