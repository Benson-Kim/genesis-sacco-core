# Genesis Prestige — Admin Web (`web/`)

Next.js + TypeScript **strict** admin app (MASTER_PROMPT §2.3), built P14.
Carries the P13.5 screens (system users administration + audit-log viewer),
ported from the !25 stopgap console; the remaining feature screens land in P15.

## Module layout

```
web/
├── packages/
│   ├── design-system/     # @genesis/design-system — tokens (verbatim from the
│   │                      #   prototype :root CSS variables) + UI primitives.
│   │                      #   The ONLY source of colors/spacing (gate 1.1).
│   └── api-client/        # @genesis/api-client — typed client. Types are
│       ├── openapi.json   #   GENERATED from the backend OpenAPI contract;
│       └── src/generated/ #   never hand-written (MASTER_PROMPT §2.1).
├── src/
│   ├── app/               # Next.js app router (thin pages only)
│   ├── lib/               # env + the single authenticated API client
│   └── modules/
│       ├── auth/          # OTP flow, session store, token refresh, RequireAuth,
│       │                  #   FrameGuard (clickjacking defence in depth)
│       ├── authz/         # /me/permissions guards (RequireModule, deny-by-default)
│       ├── layout/        # app shell: sidebar (permission-filtered nav), header
│       ├── table/         # keyset-pagination table + cursor hooks (gate 1.3)
│       ├── users/         # P13.5 users administration (Access-control tab)
│       └── audit/         # P13.5 audit-log viewer (redacted payloads as text)
└── scripts/
    └── check-client-drift.sh
```

Internal packages are consumed through TypeScript path aliases
(`@genesis/design-system`, `@genesis/api-client`); views import primitives and
clients only through those public entry points.

## Commands

| Command | Purpose |
|---|---|
| `npm run dev` | dev server |
| `npm run lint` | eslint, **zero warnings** (`--max-warnings 0`) |
| `npm run typecheck` | `tsc --noEmit` (strict) |
| `npm test` | jest (jsdom) |
| `npm run build` | production build |
| `npm run generate:api` | regenerate the client from `packages/api-client/openapi.json` |
| `npm run check:client-drift` | fail if the generated client is stale |

## API client regeneration & drift-check

The contract flows one way: backend code → OpenAPI → generated client.

1. `cd backend && python scripts/export_openapi.py ../web/packages/api-client/openapi.json`
2. `cd web && npm run generate:api`
3. Commit both outputs.

CI enforces both halves on every pipeline:

- **`web:spec-drift`** — re-exports the spec from backend code and diffs it
  against the committed `openapi.json` (fails when the backend contract moved).
- **`web:client-drift`** — regenerates the TypeScript client from the committed
  `openapi.json` and diffs it against `src/generated/` (fails on a stale client).

## Configuration

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | API origin (default `http://localhost:8000`) |
| `NEXT_PUBLIC_TENANT_ID` | optional gate pre-fill for the tenant id (the tenant is chosen at sign-in, not baked into the build) |

No secrets here — `NEXT_PUBLIC_*` values are public by definition (gate 1.6).

## Auth & session notes

- BOTH tokens are held in JS module memory only — no cookies, no local or
  session storage (a page reload requires re-login; accepted trade-off for a
  core-banking console, carried from the !25 threat model). Refresh is
  proactive and single-flight before expiry; the refresh token travels only
  in POST bodies; a failed refresh or any 401 tears the session down to the
  sign-in gate.
- The only persisted value is the tenant id — a routing UUID, not a
  credential (`gp.tenant` in localStorage; `auth/session.ts` is the single
  allowlisted storage site, lint- and test-enforced).
- Route guards (`RequireAuth`, `RequireModule`) consume `/me/permissions` and
  are deny-by-default; they shape UX only — the API authorizes every call.
- Mutations send an `Idempotency-Key` with the stability/rotation contract:
  stable across retries of an identical logical submission, rotated the
  moment the body changes (gate 1.4). Mutations never auto-retry.
- All list UIs use the keyset (cursor) contract `{items, next_cursor}` —
  cursors are opaque server strings, echoed back, never parsed (gate 1.3).

## Threat model (P13.5 screens — carried forward from !25, updated for the React/Next.js attack surface)

| Threat | Defence |
|---|---|
| **XSS via audit-payload / profile rendering** (named threat; payloads embed attacker-influenced names/branches/purposes) | React text interpolation only — `dangerouslySetInnerHTML`, `innerHTML`/`outerHTML`/`insertAdjacentHTML`, `document.write`, `eval` family are eslint **errors** and a source-scan jest gate; payloads render as pretty-printed JSON via text nodes in `<pre>`, byte-identical to the API response; hostile-payload tests run through the real screens |
| Hydration injection / SSR data leakage | All privileged data is fetched client-side after auth (TanStack Query); no `getServerSideProps`-style serialization of tokens/PII into page props; pages are thin client components; nothing sensitive in `NEXT_PUBLIC_*` |
| CSRF on mutations | P3 transport exactly: Bearer header + refresh token in POST bodies — **no auth cookies exist**, so a cross-site page cannot attach credentials |
| Token/OTP/PII leakage via storage/logs/URLs | Memory-only tokens (storage writes are test-asserted to carry only the tenant id); `console.*` is a lint error in shipped source; auth travels only in headers/bodies (network tests assert no token substring in any URL); OTP endpoints return side-effect **counts** only and have no code render surface |
| Clickjacking on admin actions | `frame-ancestors 'none'` + `X-Frame-Options: DENY` from `next.config.ts` headers, in-app `FrameGuard` as defence in depth, confirmations on all destructive actions |
| Lost update / stale write | Every mutation sends the loaded `version`; 409 ⇒ explicit "record changed — reload" flow; never silent overwrite or auto-retry (mutations `retry: 0`) |
| Double submit / replay | `Idempotency-Key` stability/rotation contract per logical submission; buttons disabled in flight |
| Privilege-escalation UI probing | Affordances gated by `GET /me/permissions` (UX only, deny-by-default `can()`); 403 renders "Not permitted." + correlation id — no capability enumeration |
| Hostile cursors/filters | Cursors opaque (echoed, never parsed/rendered); filters length-limited; actor filter UUID-validated client-side; server validates regardless |
| Dependency supply chain | `package.json` pinned to exact lockfile versions; `npm ci` against the committed lockfile; no CDN assets; generated client drift-checked in CI |
| CSP limits | `script-src` retains `'unsafe-inline'` because Next.js emits inline bootstrap scripts — the nonce-based strict CSP is the P22 deployment item; primary XSS control is the absence of injection sinks (documented finding S2) |
