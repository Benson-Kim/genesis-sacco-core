# Known hardening follow-ups

Accepted, as-built limitations recorded during a deep module review. Each
item is a deliberate follow-up, not an undiscovered defect; none is fixed by
merely documenting it here.

## 1. Refresh-token custody (web)

The refresh token is held in `sessionStorage`
(`web/src/modules/auth/session.ts`), which is readable by any script running
in the page — an XSS foothold can exfiltrate it. Custody should migrate to
an httpOnly, Secure, SameSite cookie set by the backend so the token never
becomes script-accessible.

## 2. Build-time tenant binding (web)

`web/src/lib/env.ts` bakes `tenantId` from `NEXT_PUBLIC_TENANT_ID` at build
time, so one build artifact serves exactly one tenant. Onboarding a tenant
requires a rebuild and separate deployment; runtime tenant resolution (e.g.
by hostname) is the follow-up.

## 3. Hardcoded chart of accounts (backend)

The chart of accounts is a hardcoded enum
(`backend/src/genesis/domain/ledger.py:Account`) shared by all tenants. No
tenant can add, rename, or restructure accounts without a code change; a
per-tenant chart (or per-tenant overlay) is the follow-up.

## 4. Trusted-proxy forwarded-for handling (backend)

The auth rate guard's pure-IP backstop bucket
(`backend/src/genesis/api/auth.py:_rate_guard`) keys on
`request.client.host`. Behind Passenger (the MochaHost deployment,
`docs/technical/mochahost-deployment.md`) that is the proxy's address, so
all clients share one bucket and per-IP limits are not meaningful. Trusted
proxy configuration with `X-Forwarded-For` validation (trust only the known
proxy hop) is required before the per-IP limit carries real weight.
