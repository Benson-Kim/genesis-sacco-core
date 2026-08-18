# ADR-0007: Member self-service read surface

- Status: Proposed
- Date: 2026-08-18
- Deciders:

## Context
The P17 member mobile app requires balances, statements, transaction
history, loan status, repayments and notifications. The
`genesis-member` audience today reaches five endpoints only
(`backend/src/genesis/api/member.py`): three auth routes and two
guarantee acts. Every member-relevant read is a STAFF route behind
`RequirePermission` (e.g. `/members/{member_id}/statement` requires
MEMBERS/VIEW), and per the FM1 audience separation a member token is a
403 there by design. The mobile app is therefore blocked on an API
surface that does not exist. P19 `payment_intents` (M-Pesa) does not
exist in any layer and is out of scope here.

## Decision
A member read surface is built under the existing `/member` router,
every route gated by `RequireMemberPrincipal` (live-link re-check).
Member identity is ALWAYS derived from the authenticated credential
(`ctx` → member id) — never from a path or query parameter; no
`/member/…/{member_id}` shape may exist. The v1 surface:

- `GET /member/me` — profile, deposit/share balances, loan summary.
- `GET /member/transactions` — own postings, keyset-paginated with the
  existing signed-cursor machinery under a NEW member-scoped cursor
  scope id (no cross-scope replay with the staff list).
- `GET /member/loans` and `GET /member/loans/{loan_id}` — own loans
  only; ownership verified against the principal inside the query, not
  by 404-after-fetch.
- `GET /member/statement` — reuses the existing statement application
  service with the principal-derived member id.

Reads reuse the existing application services (reuse-first) with
member-scoping wrappers; RLS and tenant scoping are unchanged.
Notifications and deposits/repayments (money movement) are explicitly
deferred: notifications need a member-facing read model the outbox does
not provide, and money movement is P19 (payment intents) work.
The OpenAPI snapshot and generated client update in the same MR (the
spec-drift and client-drift gates arbitrate).

## Alternatives considered
- Reuse staff endpoints with member tokens — rejected: FM1 audience
  separation is deliberate and falsifiably tested; weakening it to save
  endpoints is the classic IDOR-adjacent mistake.
- A separate mobile BFF service — deferred: correct at larger scale,
  disproportionate now; this surface is BFF-compatible later.
- Member-id path parameters checked against the principal — rejected:
  principal-derived identity leaves nothing to check and nothing to
  get wrong; an id parameter is an invitation to authorization bugs.

## Consequences
Positive: the mobile app unblocks against a contract designed for
hostile clients; the staff/member boundary stays intact. Negative: the
statement and transaction read paths gain a second consumer whose load
profile (many small reads) differs from staff usage — the member list
routes need their own rate-limit posture. Migration: additive only;
nothing changes for staff routes. Rollback: remove the routes; no
schema changes are required for v1.
