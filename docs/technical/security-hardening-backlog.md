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

## 4. Trusted-proxy forwarded-for handling (backend) — IMPLEMENTED

Resolved (issue #13): the auth rate guard now keys BOTH buckets on
`genesis.api.auth.resolve_client_ip`, which consults `X-Forwarded-For`
only when the immediate peer is listed in the `TRUSTED_PROXY_IPS` setting
(`backend/src/genesis/settings.py:Settings.trusted_proxy_ips`; default
empty = never trust the header). The chain is walked from the right,
every candidate is `ipaddress`-validated, and malformed chains collapse
to one shared bucket. **Deployment prerequisite remains operational**:
the MochaHost/Passenger deployment (`docs/technical/mochahost-deployment.md`)
must set `TRUSTED_PROXY_IPS` to the actual proxy address, or the per-IP
backstop stays one shared bucket behind the proxy (the safe default, not
a meaningful per-client limit).
