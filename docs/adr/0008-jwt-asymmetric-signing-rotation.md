# ADR-0008: JWT signing moves from shared-secret HS256 to EdDSA (Ed25519) with kid-versioned rotation

- Status: Proposed
- Date: 2026-08-19
- Deciders: issue #6 ADR pass (Developer + Solutions Architect
  roles); implementation gated on !3 (which owns `api/auth.py` and
  the settings/`.env.example` surface this migration must touch)

## Context

Both token audiences — STAFF and MEMBER — are signed with a single
symmetric HS256 key (`backend/src/genesis/application/auth.py`,
`_JWT_ALGORITHM = "HS256"`, key from `Settings.jwt_signing_key`).
Under HMAC, verifying and minting are the same capability: anything
that can *verify* a token can *forge* one. That is survivable while
exactly one monolith holds the key; it becomes untenable the moment a
second verifier exists. The member mobile app track is creating that
pressure now: the design brief (`docs/technical/member-mobile-app.md`)
names a possible mobile BFF, the ADR-0007 alternatives explicitly keep
the surface "BFF-compatible later", and the member WRITE sequencing
issue (#30) plus the attestation gate (#29) both anticipate additional
server-side consumers (reporting, webhook consumers) that will need to
check tokens without being trusted to mint them.

Constraints that shape the design:

- Refresh tokens are NOT JWTs. They are opaque `secrets.token_urlsafe`
  values hashed at rest, rotated per use, family-revoked on reuse
  (`application/auth.py`). The signing algorithm never touches them —
  a fact the cutover plan below leans on.
- Access tokens live at most 15 minutes (`ACCESS_TOKEN_TTL_SECONDS =
  900`), so legacy-signed access tokens drain themselves within one
  TTL of the issuer flip.
- The house already owns a key-rotation design: the opaque-cursor
  signing keys in `genesis/settings.py` (`cursor_signing_key` /
  `cursor_signing_key_previous` + version pair, dual-version decode
  window, boot fails closed on missing/short/colliding key material
  via `application.pagination.assert_cursor_signing_key_configured`,
  called from `api.app.create_app`). Reuse-first applies to designs,
  not just code.
- Audience separation is a proven invariant: STAFF and MEMBER
  principals are disjoint, dispatch is deny-by-default, and both
  rejection directions are falsifiably tested. The migration must
  re-prove this, not merely preserve the code.

## Decision

### 1. Algorithm: EdDSA (Ed25519); RS256 only on a named trigger

New tokens are signed with **EdDSA over Ed25519** (RFC 8037). Reasons
over RS256: 32-byte keys and 64-byte signatures (RS256: 2048-bit keys,
256-byte signatures on every request); deterministic signatures with
no padding scheme to misconfigure (RSA drags PKCS#1 v1.5 history
behind it); constant-time implementations by default in modern
libraries; faster signing on the token-issue hot path.

Verifier-compatibility check against the planned consumers (#29/#30):

- **This backend (issuer + verifier)**: PyJWT (already pinned
  `pyjwt>=2.9`) supports `EdDSA` when the `cryptography` extra is
  installed — the implementation MR must move the dependency to
  `pyjwt[crypto]`.
- **The Flutter member app**: per the design brief it treats tokens as
  opaque bearers (any 401 = session over, back to login) and never
  verifies signatures, so app-side algorithm support is NOT a
  constraint. Should Dart-side verification ever appear,
  `dart_jsonwebtoken` supports EdDSA/Ed25519.
- **A mobile BFF** (deferred in ADR-0007, anticipated by #30): the
  plausible stacks are Node (the web app is Next.js; `jose` has native
  EdDSA via Node's crypto) or Python (same PyJWT as above). Both pass.
- **JWKS representability**: Ed25519 public keys serialize as
  `kty: "OKP", crv: "Ed25519"` (RFC 8037) and mainstream JOSE
  libraries consume them.

RS256 is the fallback, not the peer option: it is adopted ONLY if a
concrete verifier that we are mandated to integrate with (e.g. a
managed API gateway or a third-party SaaS webhook consumer) is shown
to lack OKP/EdDSA support. That finding reopens this ADR; nobody
downgrades to RSA as a side effect of an integration ticket.

### 2. `kid` header: mandatory from day one

Every token minted by the new issuer carries a `kid` header derived
from the active key version (`v{n}`, e.g. `v1`). Verification resolves
keys ONLY through a code-owned `kid → (algorithm, key)` map — never by
trying keys until one works, and never by reading key material hints
from the token itself. An EdDSA token with a missing or unknown `kid`
fails closed as unauthenticated. Legacy HS256 tokens carry no `kid`
(the current issuer sets none); during the migration window they are
verified exclusively against the legacy symmetric key, and that path
dies with phase 2 below.

### 3. Rotation: the cursor-key pattern, applied to an asymmetric pair

Settings mirror the `CURSOR_SIGNING_KEY`/`_PREVIOUS` + version design,
with one asymmetry the cursor pattern does not have: the *previous*
slot needs only the PUBLIC key, because old tokens need verification,
never re-issue.

- `jwt_signing_private_key_ed25519` — the active Ed25519 private key
  as the 64-hex-char encoding of the 32-byte seed (the
  `secrets.token_hex(32)` generation convention the cursor key already
  documents in `.env.example`); environment-only, no literal secrets.
- `jwt_key_version` — active version (1–255); becomes the `kid`.
- `jwt_verify_public_key_previous` — 64-hex-char raw Ed25519 public
  key; EMPTY = single-key mode (no window).
- `jwt_key_version_previous` — version of the previous key.

Rotation procedure (the B13-R10 dual-version window, verbatim from the
cursor precedent): deploy the NEW private key + version as the active
pair and demote the old key's PUBLIC half to `*_previous` — decode
accepts BOTH versions during the window (in-flight access tokens keep
working); encode mints ONLY the active version. Retire the window by
clearing the previous pair; a token under any older version (N-2)
fails closed. The window must cover at least one access-token TTL
(15 min) plus deploy overlap; operationally it is retired at the next
release, like cursors.

Boot fails closed, matching the cursor-key precedent: a new
`assert_jwt_signing_key_configured` guard (called from
`api.app.create_app` alongside `assert_cursor_signing_key_configured`)
refuses startup when the active seed is missing, not exactly 32 bytes
after hex-decode, or fails Ed25519 key construction; when a configured
previous public key is malformed; or when the two versions collide.
Never a first-decode surprise, never a fleet signing under guessable
material.

### 4. Verify-side algorithm allow-list, pinned per migration phase

The `algorithms=` argument to every decode is a code-owned constant
enumerating EXACTLY the accepted set for the current phase. The
token's own `alg` header is never trusted to select anything.

- **Phase 1 (the migration release)**: issue EdDSA; verify allow-list
  is exactly `{EdDSA, HS256}`. HS256 verification is bound solely to
  the legacy symmetric key (`jwt_signing_key`, demoted to
  verify-only); EdDSA verification is bound solely to the public keys
  in the `kid` map. The two key sets never cross an algorithm
  boundary.
- **Phase 2 (one release later)**: verify allow-list is exactly
  `{EdDSA}`. The HS256 verify path and the `jwt_signing_key` setting
  are deleted, `.env.example` included. A token signed with the
  retired HS256 key — or with a retired Ed25519 key after its window —
  fails closed.

Two classic downgrade attacks are in scope and MUST ship with
falsifying tests in the implementation MR (tests that fail if the
defense is removed, the `test_description_hygiene.py` non-vacuity
posture):

- **`alg=none`**: a well-formed unsigned token (header `alg: "none"`,
  empty signature) is rejected as unauthenticated in BOTH phases.
  `none` never appears in any allow-list; the test crafts the token by
  hand rather than trusting the library to refuse to.
- **HS256 key confusion**: a token with header `alg: HS256` whose HMAC
  key is the Ed25519 PUBLIC key bytes (raw and PEM forms) is rejected
  in BOTH phases. The defense is structural — public keys are never
  reachable from the HMAC verify path because key selection pairs
  algorithm and key in one code-owned map — and the test proves the
  structure holds.

### 5. Public-key distribution: a JWKS endpoint, not config-fanout

Public keys are published at an unauthenticated
`GET /.well-known/jwks.json`: the active key and, during a rotation
window, the previous key — each as `{kty: OKP, crv: Ed25519,
alg: EdDSA, use: sig, kid: v{n}, x: …}`. Private material is
structurally absent (the JWK is built from the public half only, and a
test asserts no `d` parameter can ever appear). Responses carry a
short `Cache-Control: max-age` so verifiers re-fetch on unknown `kid`
— the standard JOSE client behavior — which makes rotation invisible
to them.

Decided against config-distributed public keys because of who is
coming: the mobile BFF (#30), reporting consumers, and webhook
consumers (#29's attestation flow names server-side verification).
With config fanout, every rotation becomes an N-party coordinated
redeploy where the slowest consumer pins the window open; with JWKS,
rotation is a one-party act and consumers follow by `kid`. Least
disclosure holds: an Ed25519 public key is public by construction, the
endpoint carries no tenant or member data, and — unlike the HS256
status quo — possessing everything the endpoint serves mints nothing.

### 6. Outstanding HS256 refresh-token families: survive the cutover

Refresh tokens are opaque secrets, not JWTs (see Context); their
validity is independent of the signing algorithm. Decision: existing
refresh families SURVIVE the migration untouched. The next
`/auth/refresh` or `/member/auth/refresh` rotation simply mints an
EdDSA access token; the family chain, reuse-revocation, and hashes at
rest are unaffected.

The rejected alternative — revoking all families at cutover (forced
re-auth) — was weighed explicitly for the member-UX cost: every member
would be logged out and pushed through OTP again, which for members
means an SMS round-trip (a real cost and harassment vector — the exact
surface #29 gates) in exchange for zero security gain, since refresh
custody and rotation semantics do not change. Forced family revocation
remains the correct INCIDENT lever if the HS256 key itself is ever
suspected compromised — but that is an incident response, not a
migration step, and conflating them would train operators to treat
mass logout as routine.

### 7. Audience-separation invariants re-proven under EdDSA

The FM1 invariants are properties of the decode dispatch, and the
decode path is exactly what this migration rewrites — so the existing
falsifiable tests are re-proven, parameterized over algorithm and
phase, not merely re-run:

- a MEMBER token can never satisfy a staff `RequirePermission` gate
  (403), and a STAFF token can never satisfy the member gate (403) —
  under EdDSA, and, during phase 1, for legacy HS256 tokens crossing
  the same dispatch;
- a token with a missing or unknown audience is refused as
  unauthenticated (deny-by-default), regardless of which key verified
  its signature;
- no cross-algorithm crossover: an HS256-verified member token gains
  no staff access in phase 1 that an EdDSA member token would not
  have.

### 8. Implementation checklist (the follow-up code MR must satisfy)

**Precondition: !3 has merged.** !3 owns `api/auth.py` and is adding
settings to `genesis/settings.py` / `.env.example` — the same surface
this migration touches. Nothing below starts before that merge; the
declared merge order stands.

- [ ] `backend/pyproject.toml` — `pyjwt` → `pyjwt[crypto]` (pulls
      `cryptography` for Ed25519); coordinate with the lockfile work
      (#5) if it has landed.
- [ ] `backend/src/genesis/application/auth.py` — retire
      `_JWT_ALGORITHM`; issue EdDSA with mandatory `kid`; decode via
      the code-owned `kid → (alg, key)` map and the phase-pinned
      allow-list; both issue helpers and `decode_principal` covered.
- [ ] `backend/src/genesis/settings.py` — the four new settings
      (decision 3); `jwt_signing_key` demoted to verify-only for
      phase 1; `assert_jwt_signing_key_configured` fail-closed boot
      guard wired in `api.app.create_app` beside the cursor and
      dev-OTP guards.
- [ ] `backend/.env.example` — new variables with dev-only
      placeholders and a generation one-liner (the cursor-key
      documentation convention); never a real key.
- [ ] API surface — JWKS router (`/.well-known/jwks.json`) wired in
      `api/app.py`; OpenAPI snapshot and generated client regenerated
      (spec-drift gate); C4/diagram spot-check inputs updated if the
      router wiring changes what the semantic gate reads.
- [ ] Tests — falsifiers for `alg=none` and HS256 key confusion
      (decision 4, both phases); rotation window proves a
      previous-`kid` token verifies inside the window and fails after
      retirement; boot-guard refusal on missing/short/malformed seed
      and version collision (both directions); audience matrix of
      decision 7 parameterized over algorithm × phase; JWKS response
      shape with no private parameter.
- [ ] `docs/technical/security-model.md` — token section updated from
      "signed JWT" to name the algorithm, `kid`, and rotation design.
- [ ] Phase 2 follow-up (one release later, its own MR): shrink the
      allow-list to `{EdDSA}`, delete the HS256 verify path and
      `JWT_SIGNING_KEY` everywhere, keep the retired-key falsifier.

## Alternatives considered

- **RS256** — not rejected on correctness, demoted to a
  trigger-gated fallback: every named future verifier passes the
  EdDSA compatibility check today, and RSA buys nothing here except
  10x larger signatures on every request and a padding-scheme
  footgun. If a mandated verifier lacks OKP support, that concrete
  finding reopens this ADR (decision 1).
- **ES256 (ECDSA/P-256)** — rejected: nonce-reuse fragility is a
  real historical failure class EdDSA removes by construction;
  broader legacy-verifier reach is not a requirement any anticipated
  consumer imposes.
- **Keep HS256, distribute the key to new verifiers** — rejected
  outright: every recipient becomes a minting authority; one
  compromised reporting job forges staff tokens for every tenant.
  This is the exact failure the migration exists to remove.
- **Config-distributed public keys instead of JWKS** — rejected
  (decision 5): correct for a single static verifier, but rotation
  then requires a coordinated redeploy of every consumer, and the
  roadmap (#29/#30) is plural. Deferring JWKS would bake the fanout
  problem into the first BFF.
- **Hard cutover without the accept-both window** — rejected: it
  strands up to 15 minutes of live access tokens and turns a routine
  deploy into a visible outage; the dual-version window is already
  house practice for cursors and costs one settings pair.
- **Forcing re-auth of all refresh families at cutover** — rejected
  (decision 6): all member-UX and SMS cost, no security gain, and it
  degrades the incident lever into a routine.

## Consequences

- Positive: verification capability no longer implies minting
  capability — future verifiers (BFF, reporting, webhooks) consume
  JWKS and can forge nothing; rotation becomes a routine one-party
  operation with a proven house pattern; the downgrade attack surface
  is pinned shut by construction and by falsifying tests.
- Negative (accepted): a new dependency surface (`cryptography` via
  `pyjwt[crypto]`) enters the supply chain — mitigated by the
  dependency-scanning stage and the lockfile work; one release must
  carry the dual-algorithm verify path (bounded: the allow-list is
  two entries for exactly one release); an unauthenticated JWKS route
  joins the public surface (serving only public material).
- Migration path: phase 1 (issue EdDSA, verify {EdDSA, HS256}) →
  phase 2 one release later (verify EdDSA only, HS256 material
  deleted) — expand → migrate → contract, applied to key material.
  Refresh families ride through untouched.
- Rollback: phase 1 is rollback-safe by revert — the HS256 verify
  path is still live, EdDSA access tokens die within their 15-minute
  TTL, and refresh families were never touched. After phase 2, "back
  to HS256" is a new key deployment, not a revert (the old secret
  must be treated as retired), which is one release of notice — the
  price of actually removing the shared secret.
