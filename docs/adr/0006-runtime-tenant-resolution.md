# ADR-0006: Runtime tenant resolution for the web app

- Status: Proposed
- Date: 2026-08-18
- Deciders:

## Context
`web/src/lib/env.ts` inlines `NEXT_PUBLIC` tenant identity at build
time, so one tenant requires one build artifact. The backend is
multi-tenant by design (ADR-0002, Postgres RLS); the frontend contradicts
it. Operationally this multiplies build/deploy surface per tenant and
makes tenant onboarding a build pipeline event instead of a data event.

## Decision
Tenant identity is resolved at request time from the hostname. Each
tenant is served on its own subdomain; a server-side mapping (Next.js
server runtime, not client code) resolves hostname to tenant UUID and
injects it into the app shell. One build artifact serves all tenants.
The mapping is configuration read at runtime (environment or a small
server-side endpoint backed by the tenants table), never a client-side
lookup. The `x-tenant-id` header the API client sends is derived from
the injected value. Trust posture is unchanged: the header remains a
pre-auth scoping hint; RLS and credential scoping remain the enforcement
boundary (ADR-0002).

## Alternatives considered
- Status quo (build per tenant) — rejected: O(tenants) builds, deploy
  drift risk between tenants, onboarding friction.
- Path-based tenancy (/t/{tenant}/...) — rejected: leaks tenant into
  every URL, complicates cookies and caching, weaker isolation ergonomics
  than host-based separation.
- Client-side tenant discovery endpoint — rejected: adds a pre-auth
  round trip and an enumeration surface; hostname mapping is server-side
  and cache-friendly.

## Consequences
Positive: single artifact, tenant onboarding becomes DNS + config,
staging/production parity improves. Negative: local dev and e2e need
host aliasing; CDN/page caching must key on host. Migration: introduce
the resolver behind the existing env fallback, move the deployed tenant
to a subdomain, then remove the build-time variable. Rollback: the env
fallback path remains for one release.
