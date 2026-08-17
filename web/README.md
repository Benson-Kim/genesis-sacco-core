# Genesis Prestige — Admin Web (P13.5 slice)

Static, **dependency-free** admin frontend for the P13.5 backend
(system-users administration + audit-log viewer), delivered against the
API on `duo/feature/p13-5-system-users` (MR !24).

## Stack decision (pre-implementation reuse audit, MASTER_PROMPT §5.9)

The only frontend that exists in this repository is the canonical
prototype `genesis_prestige_app.html` — a single static HTML page with
vanilla JS and CSS variables. The P14 Next.js scaffold (MASTER_PROMPT
§2.3) is an unmerged Phase C prompt and does not exist on `main` or on
the P13.5 base branch. Per the standing instruction for this slice —
*reuse the repository's actual frontend conventions; do not introduce a
new framework* — this app is:

- plain HTML + vanilla ES modules + CSS, **zero runtime dependencies**,
  no CDN scripts (supply-chain surface = this repo only);
- visual tokens (CSS variables, card/table/pill/drawer/gate components)
  copied from the prototype stylesheet;
- served as static files; the API base defaults to same-origin and can
  be overridden by the host page via `window.GENESIS_API_BASE`.

When P14 lands, these screens are the reference implementation to port;
nothing here duplicates backend logic (no client-side authz decisions,
no local redaction/eligibility math, no local money math).

### Reused, not re-implemented

| Capability | Reused from |
|---|---|
| OTP sign-in, refresh rotation, logout | P3 `/auth/*` endpoints, transport unchanged (Bearer header + `x-tenant-id` pre-auth header) |
| Permission gating data | P4 `GET /me/permissions` (UI affordance only; server enforces) |
| Role catalogue | P4 `GET /access/roles` |
| Idempotent mutations | P3 `Idempotency-Key` middleware (fresh UUID per logical submission) |
| Error envelope | `{category, correlation_id}` from `genesis.errors` |
| Look & feel | prototype CSS variables and component classes, copied verbatim where applicable |

## Threat model (attacker lens — written before implementation)

1. **XSS via audit-payload rendering** (named threat). `audit_log`
   `before`/`after` JSON is attacker-influenced (member names, branch
   strings, purposes...). Defence: DOM construction goes exclusively
   through `src/dom.js`, which creates elements/text nodes and never
   parses HTML; payloads render as pretty-printed JSON assigned via
   `textContent` into a `<pre>`. `innerHTML`, `outerHTML`,
   `insertAdjacentHTML`, `document.write`, `eval`, `new Function` and
   inline event handlers are **build failures** (`scripts/lint.mjs`),
   and the tests prove hostile payloads stay inert text.
2. **CSRF on mutations.** The existing session transport is a Bearer
   token in the `Authorization` header — there are no auth cookies in
   this stack, so a cross-site attacker cannot attach credentials.
   We follow that transport exactly and introduce no cookie fallback.
3. **Token / OTP / PII leakage.** Access & refresh tokens live in JS
   module memory only — never `localStorage`/`sessionStorage` (lint +
   test enforced; the only persisted key is the non-secret tenant id),
   never in URLs (auth travels in headers/bodies only; tested), never
   logged. OTP codes are never displayed: the prototype's on-screen
   OTP is a demo artifact; the API never returns codes and the UI has
   no surface that could show one — OTP admin actions render
   side-effect **counts** only. A page reload therefore requires
   re-login; that is an accepted trade-off for a core-banking console.
4. **IDOR via client-forged ids.** The client never treats its own
   state as authority; every id is round-tripped to the server, which
   enforces tenant + RBAC per request. 403/404 render least-disclosure
   messages.
5. **Privilege-escalation probing.** UI affordances are gated by
   `GET /me/permissions` (`access_control` × view/create/edit) as UX
   only; a 403 shows a generic "not permitted" message with the
   correlation id — no hints about which permission was missing or
   whether the target exists.
6. **Clickjacking on admin actions.** `main.js` refuses to run framed
   (frame guard), destructive actions require explicit confirmation
   dialogs, and the deployment host MUST additionally send
   `Content-Security-Policy: frame-ancestors 'none'` (meta CSP cannot
   carry frame-ancestors; documented deployment requirement).
7. **Lost update / stale write.** Every mutation carries the `version`
   the record was rendered from; a 409 surfaces an explicit
   "record changed — reload" flow. The client never auto-retries with
   a fresher version and never silently overwrites.
8. **Double submit.** Every mutation sends a fresh `Idempotency-Key`
   (UUID) that is kept stable for retries of the *same* logical
   submission; buttons are disabled while a call is in flight.
9. **Hostile cursors / filters.** Keyset cursors are opaque strings:
   stored, echoed back as query parameters, and never rendered or
   parsed client-side. Filter inputs are length/shape-limited
   client-side and validated server-side.
10. **Supply chain.** Zero dependencies, no CDN, CSP `script-src 'self'`
    in `index.html`; the lockfile is empty by construction.

## Layout

```
web/
  index.html        static shell (gate + sidebar + tab panels), meta CSP
  assets/app.css    prototype tokens + components (no inline styles → strict CSP)
  src/dom.js        the only module that builds DOM; text-node only
  src/session.js    in-memory token store, tenant persistence, JWT sub decode
  src/api.js        fetch wrapper: bearer, idempotency, refresh-once-on-401,
                    error envelope mapping (401/403/409/422/429 distinct)
  src/format.js     pure formatting (dates, pretty JSON), CursorPager
  src/users.js      users administration screen
  src/audit.js      audit-log viewer
  src/main.js       boot, frame guard, login gate, permission-gated tabs
  scripts/lint.mjs  security lint (merge blocker), module import check
  scripts/run-tests.mjs / build.mjs
  tests/            node:test suites (fake-DOM XSS proofs, client contract)
```

## Local verification

```
cd web
npm ci          # no-op install (zero deps) — mirrors CI
npm run lint    # security lint + module load check
npm test        # node:test suites
npm run build   # copies the static site to web/dist
```

Serve `web/` from any static host that (a) also proxies the API
same-origin or sets `window.GENESIS_API_BASE`, and (b) sends
`frame-ancestors 'none'`/`X-Frame-Options: DENY` and
`X-Content-Type-Options: nosniff`.
