# Genesis Prestige — Admin Web (`web/`)

Next.js + TypeScript **strict** admin app (MASTER_PROMPT §2.3), built P14.
Scaffold only — feature screens land in P15.

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
│       ├── auth/          # OTP flow, session store, token refresh, RequireAuth
│       ├── authz/         # /me/permissions guards (RequireModule, deny-by-default)
│       ├── layout/        # app shell: sidebar (permission-filtered nav), header
│       └── table/         # keyset-pagination table + cursor hooks (gate 1.3)
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
| `NEXT_PUBLIC_TENANT_ID` | tenant scope sent as `X-Tenant-Id` on pre-auth calls |

No secrets here — `NEXT_PUBLIC_*` values are public by definition (gate 1.6).

## Auth & session notes

- Access token (≤15 min JWT) is held in memory only; the rotating refresh
  token lives in `sessionStorage` (per-tab, cleared on close). Refresh is
  proactive and single-flight before expiry.
- Route guards (`RequireAuth`, `RequireModule`) consume `/me/permissions` and
  are deny-by-default; they shape UX only — the API authorizes every call.
- Mutations send an `Idempotency-Key` (one key per logical submission, gate 1.4).
- All list UIs use the keyset (cursor) contract `{items, next_cursor}` (gate 1.3).

## Idempotency-Key MATERIAL rule (gate 1.4 — issue #30 finding S7)

Every mutation calls the single shared
`idempotencyKeyFor(slot, material)` (`@genesis/api-client`): identical
material reuses the key (the server's idempotency store replays the
stored response); changed material rotates it. The slot lives in a
`useRef` for the lifetime of one form/dialog instance — never module
scope, never shared across records. What goes INTO the material is a
RULE per mutation class, not a per-module choice (this section
codifies the previously unwritten convention — audit finding S7):

1. **Always**: an `op` discriminator naming the mutation class (e.g.
   `"member-create"`, `"perm"`) plus the FULL canonical write body —
   `JSON.stringify` of exactly what will be sent. A submission whose
   content changed is a NEW logical intent and MUST rotate the key.
2. **Simple creates** (no optimistic lock; the server holds the
   uniqueness/first-write claim — e.g. member registration): rule 1
   is sufficient. Precedent: `MemberCreateDrawer`.
3. **Versioned (optimistic-locked) writes**: additionally fold the
   record `version` being acted on. A reload that advanced the
   version is a NEW intent — a stale submission must never replay
   across a reload.
4. **Money-movers and state-machine executions** (disburse, settle,
   vote, void): additionally fold the fresh record version + reload
   epoch + a per-SUCCESS intent counter, so an intentional repeat
   AFTER a success is a NEW intent (never served the stored response)
   while retries of an identical failed attempt still reuse the key.
   Precedent: exits settlement, loans `DisburseDialog` (the !60 F3 /
   W59-3 lessons).
5. **Never fold**: timestamps, random values, or anything the
   operator did not change — they break retry stability, the property
   the server-side replay window depends on.

The stability/rotation contract itself is falsifiably tested at the
package (`packages/api-client/src/__tests__/contract-helpers.test.ts`),
screen (per-module jest suites) and wire (network suites, e2e header
proofs) levels.

## Security posture (gate 1.6 — banking standard)

**Token storage & XSS blast radius.** No token ever touches
`localStorage` (readable forever by any injected script). The access
token exists only in a module-scope variable: an XSS payload would have
to execute *while the tab lives* and intercept the in-memory store —
it cannot harvest tokens at rest. The refresh token is per-tab
`sessionStorage`, single-use (rotated with family-revocation on reuse
by P3), so a stolen value dies on its first legitimate rotation. This
bounds the blast radius of any script injection to the lifetime of one
tab's session rather than "forever until the user clears storage".

**Content-Security-Policy.** `src/middleware.ts` mints a per-request
nonce; `script-src 'self' 'nonce-…' 'strict-dynamic'` — **no
`unsafe-inline` scripts, no `unsafe-eval`**. The root layout forces
dynamic rendering so Next's own inline bootstrap scripts carry the
nonce. Documented Next.js-imposed exception: `style-src` keeps
`'unsafe-inline'` because Next/styled-jsx inject inline `<style>`
elements without nonce support — styles cannot execute script or read
tokens; the script execution vector stays nonce-gated. `connect-src`
allows only self + the API origin.

**Other headers** (set in middleware AND `next.config.ts` for
static-asset coverage): `frame-ancestors 'none'` + `X-Frame-Options:
DENY` (clickjacking), `Referrer-Policy: strict-origin-when-cross-origin`,
minimal `Permissions-Policy`, `X-Content-Type-Options: nosniff`.
The whole set is test-enforced (`src/lib/__tests__/security-headers.test.ts`).

**Least disclosure.** Errors render only the sanitized
`{category, correlation_id}` envelope from the backend contract — no
figures, no internals, no stack traces reach the DOM.

**Hygiene gate (FM3).** `src/__tests__/client-hygiene.test.ts` +
eslint `no-console` fail the pipeline on console logging, localStorage,
cookie fiddling, any third-party analytics/telemetry, or CDN script
references. The dependency lockfile is produced by CI and committed —
no scripts are pulled from CDNs at runtime.

## Performance budget (P15 EXIT — Lighthouse)

Budgets for the admin console on a mid-tier laptop / throttled 4G
(Lighthouse desktop preset), per authenticated route:

| Metric | Budget |
|---|---|
| Performance score | ≥ 90 |
| LCP | < 2.5 s |
| TBT | < 200 ms |
| CLS | < 0.1 |
| Initial route JS (gzip) | < 300 KB |
| Third-party requests | **0** (no CDN scripts/fonts — hygiene-gate enforced) |

How the budget is defended structurally:

- **Route-level code splitting** is Next.js default; **tab panels and
  drawers are additionally `next/dynamic` chunks** (access-control tabs,
  create/detail drawers) so list routes ship without editor code.
- **TanStack Query staleTime per entity class** (`src/lib/query.ts`):
  reference 5 min, composite 60 s, lists 30 s, records 0 — avoids
  refetch storms without ever serving a stale record into an
  optimistic-locked edit.
- **Keyset pages are 20 rows** with explicit *Load more* — no unbounded
  DOM growth; virtualization is deliberately NOT needed at this page
  size (revisit if a module ever demands larger pages).
- **No CDN dependencies** anywhere (client-hygiene gate); fonts are the
  system stack / self-hosted only.

Measurement: Lighthouse runs are part of the P15 E2E stage (Playwright
+ LHCI against the review app) — until that stage lands, this budget is
the documented target and the structural defences above are
test-enforced; no numeric claim is made without an in-project run.
