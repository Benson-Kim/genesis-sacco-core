# Member authentication & the MEMBER principal (P14.5) — P17 consumer guide

Status: as-built on !65 (backend). Audience: P17 (member mobile app)
and P18; also the P16 scaffold's `gp_api_client` generation. The
OpenAPI snapshot (`web/packages/api-client/openapi.json`) is the
binding contract; this document explains the flows and the rules the
client MUST respect.

## 1. The principal model

* A member authenticates as a **`member_credentials` link row** — never
  as a `users` row and never by email coincidence. The link is created
  and revoked ONLY by staff administrators (`member_identity` module);
  there is no self-service signup, linking, or email change.
* Tokens carry the **`genesis-member` audience**. A member access token
  can never satisfy a staff gate, and a staff token can never satisfy a
  member gate (both directions are 403 — FM1). Do not attempt to reuse
  staff endpoints with a member token.
* `sub` is the credential id, `mid` the member id **at token issue**,
  `tid` the tenant. The server re-verifies the LIVE link on every
  request: a revoked or re-pointed credential dies within one request
  (401), regardless of remaining token lifetime. Clients must treat any
  401 as session-over and return to login.

## 2. Endpoints

All pre-auth endpoints require the `x-tenant-id` header (UUID) and are
rate-limited per (route, tenant, client).

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /member/auth/otp/request` `{email}` | `x-tenant-id` | Always `202 {"status": "sent"}` — never reveals whether a credential exists. The 6-digit code is delivered out-of-band (outbox → provider), TTL 5 minutes, single use, ≤5 attempts. |
| `POST /member/auth/otp/verify` `{email, code}` | `x-tenant-id` | `200 TokenResponse` (`access_token` ≤15 min, rotating `refresh_token`, `expires_in`) or `401`. Failed attempts count even though the request fails. |
| `POST /member/auth/refresh` `{refresh_token}` | `x-tenant-id` | Rotates the refresh token. **Reusing a spent token revokes the whole family** — the client must always persist the newest refresh token before using it, and must treat a 401 here as a full logout. |
| `POST /member/guarantees/{id}/consent` `{version}` | Bearer (member) | Consent to a PLEDGED guarantee where THIS member is the guarantor. The principal IS the consent — there is no field for who consents (a caller-asserted identity is a rejected design). `403` for any wrong-principal shape; `409` on a stale `version`. |
| `POST /member/guarantees/{id}/release` `{version}` | Bearer (member) | Withdraw the member's OWN still-PLEDGED (unconsented) guarantee. Consented collateral cannot be self-released — staff paths only. |

Idempotency: send an `Idempotency-Key` header on every mutation. Keys
are scoped server-side per (tenant, actor principal, route) — another
actor replaying your key can never fetch your stored response (FM5),
and your own retry of the identical request replays safely.

## 3. Rules the client must respect

1. **Least disclosure**: every error is `{category, correlation_id}` —
   never parse error text for business meaning. `403` on a member
   guarantee act means "not yours / not in a consentable state" without
   distinguishing which.
2. **No consent flags**: bodies are `extra="forbid"`; sending anything
   beyond the documented fields is a 422.
3. **Statuses**: EXITED members cannot authenticate (silently — the OTP
   request still answers 202). ARREARS/DORMANT members authenticate and
   may consent/withdraw; their money-movement limits are enforced per
   operation server-side.
4. **Token custody** (P16 blockers): secure storage only, never in
   logs; the access token expires ≤15 minutes — refresh proactively;
   after ANY 401, discard both tokens and re-run the OTP flow.

## 4. Where the flows are proven

* FM1 audience separation, OTP policy, refresh families, revocation
  liveness: `backend/tests/test_member_auth.py`.
* FM2 link authority, FM3 admin-only link mutations, FM4 consent
  principal at the DB: `backend/tests/test_member_identity.py`.
* FM5 idempotency scoping: `backend/tests/test_idempotency.py::
  test_fm5_cross_actor_replay_is_a_miss`.
* Member self-release semantics: `backend/tests/test_guarantee_release.py::
  test_guarantor_releases_own_unconsented_pledge_only`.
* Hot-path plan: `backend/tests/test_p145_explain.py` →
  `backend/perf/explain_p145.txt` (CI artifact).

Trust-boundary and threat rows: `docs/diagrams/dfd.md` (TB1M),
`docs/diagrams/stride.md` (§1 TB1M rows; the !29 F3/F4 register entry
is closed).
