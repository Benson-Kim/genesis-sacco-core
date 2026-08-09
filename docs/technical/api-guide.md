# API guide

How to read and consume the HTTP API. The contract is OpenAPI; clients are
generated from it, never hand-written.

## 1. Reading the OpenAPI snapshot

The committed contract snapshot is
`web/packages/api-client/openapi.json`, exported from the running
application by `backend/scripts/export_openapi.py`. It is large — do not
read it whole; query it for the path or schema you need. The generated
TypeScript client (`web/packages/api-client/src/generated/schema.d.ts`) is
derived from the snapshot; both files are **generated and never
hand-edited**, and two CI drift jobs keep backend ↔ snapshot ↔ client
byte-identical (see
[architecture.md](architecture.md#6-the-generated-client-contract-flow)).

Routers are wired in `backend/src/genesis/api/app.py`; each router module
under `backend/src/genesis/api/` corresponds to one tag group in the
snapshot (auth, members, member KYC, member exits, member identity, loans,
loan book, recovery, transactions, corrections, dashboard, dividends,
reports, tenant settings, accounting periods, users, access, audit log,
branches, health, me).

## 2. Headers

| Header | When | Meaning |
|---|---|---|
| `Authorization: Bearer <jwt>` | Every authenticated request | Staff or member access token (≤ 15 minutes; disjoint audiences — see [security-model.md](security-model.md#12-tokens)). |
| `X-Tenant-ID: <uuid>` | Pre-auth endpoints only (`/auth/*`) | Explicit tenant scope before a tenant context exists. Authenticated requests derive the tenant from the token. |
| `Idempotency-Key: <opaque>` | Every mutating request | Atomic claim + stored-response replay (see [ledger-and-money.md](ledger-and-money.md#7-idempotency-keys)). |
| `X-Request-ID` | Optional, any request | Correlation id; generated when absent and echoed back on the response. |

## 3. Pagination, filters and vocabularies

- **Keyset pagination everywhere**: list endpoints take `cursor` (opaque)
  and `limit` (1–100, server default typically 20) and return `items` +
  `next_cursor` (null on the last page). Cursors are HMAC-signed, scoped to
  one tenant and one endpoint, and expire with key rotation — treat them as
  short-lived opaque state; never parse or store them long-term.
- **Declared parameters only.** Every filter an endpoint supports is a
  declared, typed query parameter; unknown body fields are rejected
  (`extra="forbid"` → 422). Filter values that form vocabularies (statuses,
  transaction types, report names, channels) are **code-owned enums** — the
  server never accepts caller-invented tokens, and enum values are always
  bound parameters, never interpolated.
- **No money parameters from callers.** Rates, fees, periods and limits are
  resolved server-side from tenant/product configuration.
- **Date parameters**: `as_of`-style dates default to today and reject
  future dates (`api/params.py:resolve_as_of`); backdating stays allowed for
  reconciliation.
- **Exports**: report scope is chosen by report name plus the declared
  id/date filters only; formats, row caps and storage are server-resolved.
  Truncation is signalled via `X-Export-Truncated` / `X-Export-Limit`
  response headers.

## 4. Error taxonomy

Errors return a sanitized envelope — category and correlation id only,
never internals, figures or submitted values
(`backend/src/genesis/errors.py`, handler in `api/app.py`):

```json
{ "category": "conflict", "correlation_id": "…" }
```

| HTTP | Category | Raised for |
|---|---|---|
| 400 | `validation_error` | Semantic parameter failures the application judges (e.g. malformed/expired cursor, future date, non-cash channel). |
| 401 | `unauthenticated` | Missing/invalid/expired token, failed OTP, dead session (including a suspended user's still-valid token, and a revoked member credential). |
| 403 | `forbidden` | Missing RBAC grant, wrong principal kind (staff vs member token), authority-band refusal, assurance-role exclusion. |
| 404 | `not_found` | Missing rows — including rows in another tenant (cross-tenant probes see zero rows, indistinguishable from absence). |
| 409 | `conflict` | Optimistic-lock version mismatch (retriable: re-read and re-apply), duplicates (e.g. duplicate external reference, second vote), state-machine refusals, maker-checking-own-work, last-admin guard, drifted approval snapshots. |
| 422 | `validation_error` | Structural body failures (FastAPI) and structurally-valid bodies that fail server-side schema checks (wrong KYC member type, malformed external reference, unknown settings key). |
| 429 | `rate_limited` | Auth-sensitive endpoints called too often. |
| 500 | `internal_error` | Unhandled failures and broken structural invariants; details only in server logs keyed by the correlation id. |

Client guidance: 409 on a version mismatch means *re-read, then retry with
the fresh version*; 409 on a duplicate claim means *the work is already
done*; 4xx categories never carry an existence oracle.

## 5. Contract evolution policy (expand-only)

The generated client and the drift jobs make the OpenAPI snapshot a merge
gate, so contract changes follow expand-only evolution:

1. **Additive first**: new endpoints, new optional fields, widened response
   models. Removals or type changes of an in-use field are breaking and
   require a deliberate, coordinated change (the earlier field is kept
   accepted for at least one more release — e.g. the sign-in body accepts
   the legacy `email` field alongside the newer `identifier` field,
   exactly one of the two).
2. Regenerate and commit **both** the snapshot and the client in the same
   MR (`backend/scripts/export_openapi.py`, then `npm run generate:api` in
   `web/`); the `web:spec-drift` and `web:client-drift` jobs arbitrate.
3. Never hand-edit `openapi.json` or `schema.d.ts` — if generators cannot
   run locally, derive the delta from the drift job's printed diff and apply
   it by script (see [contributing.md](contributing.md)).
