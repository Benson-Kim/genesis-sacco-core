# ADR-0012: Member auth factors & device security — PIN role, biometric scope, device inventory, panic lock, self-service limits, session policy

- Status: Proposed
- Date: 2026-08-20
- Deciders: issue #46 ADR pass (Developer + Security Analyst roles);
  implementation gated per §8 — NO code, schema, migration, or CI
  change ships with this document.

## Context

The P17 capability register (#43, T3.3) catalogues "log in with PIN,
enable biometric, view/remove devices, temporarily lock account, set
transaction limits". The shipped model contradicts most of that
(`docs/member-auth.md`, as-built on !65):

- A member authenticates as a **`member_credentials` link row** via
  **OTP-per-session**: 6-digit code, 5-minute TTL, single-use, ≤5
  attempts, constant-time compare — the SAME P3 machinery as staff
  (`application/member_auth.py`, reuse-first). Tokens carry the
  `genesis-member` audience; access tokens live ≤15 minutes
  (`ACCESS_TOKEN_TTL_SECONDS = 900`); refresh tokens are **opaque
  non-JWTs** (`secrets.token_urlsafe(48)`, hashed at rest), rotated
  per use in families of 14-day TTL (`REFRESH_TOKEN_TTL_SECONDS`),
  with **reuse-revokes-the-family** semantics.
- The **live link is re-verified on every request**
  (`RequireMemberPrincipal` → `live_credential_by_id`, api/authz.py —
  the !7 discipline): a revoked or re-pointed credential dies within
  ONE request (401 = session over), regardless of remaining token
  lifetime. Refresh rotation re-checks the link BEFORE rotating.
- **No PIN, no biometric, no device rows, no member lock, no
  member-settable limits exist anywhere.**

The issue's own rule binds this ADR: *the app must not invent a local
PIN that gates nothing server-side while TEACHING members that it
does* — decorative security is worse than none. Whatever we decide
about the PIN, the copy and the threat model must agree.

Constraints and in-flight work this ADR must not contradict:

- **#29 attestation seam** (in flight, `application/device_attestation.py`):
  server-side Play Integrity / App Attest verification gating
  `POST /member/auth/otp/request`, rollout `off → log-only → enforce`,
  fail-closed on unverifiable. Its verified verdict is the ONLY
  trustworthy device-identity source in the system. This ADR
  **consumes** that seam's output (§3); it does not redesign it.
- **#6 / !18 (ADR for EdDSA/kid rotation)**: nothing here may assume
  HS256, a shared verification secret, or any specific signing
  algorithm. This is cheap to honour: every server-side factor
  decision below hangs off DATABASE rows (credential status, device
  rows, lock rows, refresh families) re-checked at use time — never
  off token claims. Refresh tokens are opaque and untouched by the
  signing migration.
- **#38 (rate-limiter follow-ups)**: any new verifier endpoint rides
  the existing `_rate_guard` sliding-window limiter and inherits its
  fail-closed Redis posture; a PIN verifier additionally needs a
  per-credential attempt counter (DB-backed, like OTP attempts),
  because an IP-scoped limiter alone does not stop a distributed
  guess against one victim.
- **#18 (real SMS gateway) + #29**: OTP delivery is about to cost
  money. Member credentials deliver via email today, but phone
  identifiers are in flight (E.164 KYC work) and the catalogue assumes
  SMS. The cost and SIM-swap math is part of the PIN argument (§1).
- **!17 (tenant caps / withdrawal_holds)**: transaction limits are
  tenant-owned; **#8** (maker-checker engine) governs staff-reviewed
  raises; **#44** (money-out ADR, number 0010) owns the controls for
  money leaving; **#45** (notifications ADR, number 0011) owns the
  member-visible alert surface this ADR emits events into.

MASTER_PROMPT bindings: gate 1.6 (server-side authz, no PII in logs,
no literal secrets), 1.4 (transitions under lock, idempotency), 1.5
(DB constraints, in-transaction audit), §4 (falsifiable adversarial
tests), §5 DoD.

The six numbered sections below are the issue's six decision areas,
in order. Each takes a position.

## Decision

### 1. PIN's role: local-unlock-only NOW; a named, gated upgrade path to server-verified second factor

**Decision: option (b) — the PIN is purely a local app-unlock in
front of the stored refresh token. It is NOT sent to the server, NOT
stored server-side, and gates NOTHING server-side today. Option (a)
— a server-verified Argon2id PIN factor — is the named upgrade path,
gated on the money-out ADR (#44 / ADR-0010) shipping. Option (c),
rejecting the PIN outright, is rejected.**

The argument, with the math the issue demands:

**SMS-cost math (#18, #29).** OTP-per-session is about to become
OTP-per-session *at SMS prices*. At Kenyan aggregator rates
(indicatively KES 0.5–1.0/SMS), 10,000 members × ~20 app sessions a
month = ~200,000 OTP SMS ≈ **KES 100k–200k per month** — recurring,
forever, and that is before the bot/pumping traffic #29 exists to
stop. A local PIN (or biometric, §2) unlocking the stored 14-day
rotating refresh family collapses OTP delivery from per-SESSION to
per-FAMILY-ESTABLISHMENT (first login on a device, or after
revocation/expiry/reuse-kill): roughly 10,000 × ~2/month = ~20,000
SMS — a ~90% reduction. The refresh family and its rotation/reuse
machinery ALREADY exist; the PIN adds zero server surface to capture
this saving. A server-verified PIN could achieve the same cadence —
at the price of a new verifier, a new lockout state machine, a new
reset flow, and a new brute-force surface, none of which changes what
an attacker can DO with a session today (next paragraph).

**SIM-swap math (#18, #29).** A SIM-swap attacker owns the victim's
SMS channel, so once SMS OTP lands, OTP-as-sole-factor is defeated by
a SIM swap for SESSION ESTABLISHMENT. What does that session buy
today? The member write surface is guarantor consent/release; the
read surface is !7; money-IN (deposits) is ADR-0008's problem and
*benefits the victim's account*. **Money cannot leave the SACCO on a
member's sole authority anywhere in the shipped or in-flight
surface.** The asset a server PIN protects — member-initiated
outbound money — does not exist until #44 ships. A server PIN today
is complexity spent guarding an empty vault, while its real costs
(lockout-and-reset support burden, a new credential members reuse
from their M-Pesa PIN, a new online guessing surface) start
immediately. Conversely, the moment #44 ships, a SIM-swap attacker's
session CAN drain: that is exactly when the second, SMS-independent,
server-verified knowledge factor earns its keep — as **step-up on
money-out actions**, alongside #44's own cooling-off and velocity
controls.

**The honesty constraint (the issue's own warning).** Local-unlock-only
is acceptable ONLY as an honest feature:

- UI copy MUST describe the PIN as unlocking *the app on this
  device* — never as protecting *the account* or *transactions*.
  Copy claiming server protection is a review-blocking defect.
- The PIN key-wraps the refresh token in platform secure storage
  (Keystore/StrongBox, Keychain/Secure Enclave). N failed local
  attempts (recommended: 5, matching `OTP_MAX_ATTEMPTS`) wipe the
  stored tokens — the device falls back to full OTP login. Forgot
  PIN = wipe + OTP re-login; no reset flow, no server involvement.
- Server-side security posture is UNCHANGED and stated as such:
  OTP + rotation + reuse detection + per-request live-link re-check.

**The upgrade path, named and gated.** When ADR-0010 (#44) is
Accepted and its implementation starts, the server-verified PIN
factor ships as its step-up control, to the spec issue #46 option (a)
already fixes: **Argon2id-hashed** (unique salt, memory-hard
parameters recorded in the implementation MR), **per-credential**
(hangs off `member_credentials`, XOR-principal discipline like
migration 0035), verified server-side under the OTP-style
attempt-counter-under-row-lock pattern, **rate-limited** by
`_rate_guard` PLUS a per-credential counter, **lockout** after ≤5
failures with reset ONLY via **staff-assisted identity verification
plus a fresh OTP re-proof — NEVER knowledge questions**. Verification
compares hashes in constant time; the PIN never appears in logs,
errors, or analytics (gate 1.6). Until that MR, no server PIN code is
written.

**Why not (c) — reject the PIN entirely.** Without any local unlock,
capturing the SMS-cost saving would require silently using the
refresh token with NO local gate — a stolen unlocked phone then has a
free 14-day session. The local PIN/biometric gate is the cheapest
honest mitigation for the stolen-device case, and it is the
credential-custody discipline docs/member-auth.md §3.4 already
demands of the client.

### 2. Biometric scope: local unlock of the stored refresh token only — never a server factor

Biometric (fingerprint/face via platform APIs) is a **local
convenience alias for the §1 local PIN**: it releases the same
platform-keystore-wrapped refresh token. It is NEVER transmitted,
NEVER attested to the server as a factor claim, and no server code
path may branch on "biometric was used" — the server cannot verify it
and MUST NOT pretend to. Biometric enrolment change or failure falls
back to the local PIN; PIN failure falls back to full OTP login (§1
wipe rule). When the §1 upgrade path ships a server PIN for money-out
step-up, biometric MAY locally release a client-held PIN only if the
platform keystore enforces biometric-bound key release
(hardware-backed); otherwise step-up means typing the PIN. That
choice belongs to the #44-gated implementation MR, not to this ADR.

### 3. Device inventory: a `member_devices` registry keyed on attestation-verified identity; revoke = live-link kill within one request

**Decision: a NEW `member_devices` table** (tenant-scoped, RLS-forced,
FK to `member_credentials`), NOT overloading `member_credentials` —
one member credential legitimately spans several devices, and
credential revocation (staff act) must stay distinct from device
revocation (member act).

- **Identity source: the #29 attestation seam's VERIFIED output** —
  the App Attest key id (iOS) / the verified Play Integrity
  app+device basis (Android), as surfaced by the seam. A
  client-asserted device id (installation UUID, `device_name` string)
  is display metadata ONLY, never the key: only a server-verified
  verdict binds a row (#29's own rejected-design rule). Device rows
  are created/refreshed at OTP verify (session establishment) when
  attestation mode is `log-only` or `enforce`; in `off` mode no row
  is minted (no fabricated identity). The stored key is a digest of
  the attestation key id — raw attestation material is not persisted.
- **Refresh families bind to devices**: `refresh_tokens.family_id`
  gains a device linkage at issue time, so every live session is
  attributable to exactly one device row.
- **Surface**: `GET /member/devices` (keyset, !7 cursor discipline,
  behind the member principal gate; shows display name, platform,
  created/last-seen, current-device marker) and
  `POST /member/devices/{id}/revoke` (optimistic-locked, idempotent,
  in-transaction audit row).
- **Revoke = live-link kill, effective within ONE request.** The
  one-request property is not aspirational — it is the property the
  system already has: `RequireMemberPrincipal` re-verifies the live
  link against the database on EVERY request
  (`live_credential_by_id`, the ONE implementation shared by the
  per-request gate, refresh rotation, and the consent/release
  transactions — !7's discipline), which is exactly how a revoked
  credential already dies mid-token-lifetime today
  (docs/member-auth.md §1). Device revoke reuses the same fence:
  revoking a device (i) revokes all refresh families bound to that
  device row in the same transaction (`_revoke_family` reuse), and
  (ii) the per-request re-check extends to the device row's status,
  so an outstanding ≤15-minute access token from the revoked device
  dies at its NEXT request with 401 — same guarantee, same mechanism,
  no new invariant to invent.
- **New-device login event**: first session establishment on a new
  device row enqueues an outbox event (`member_auth.new_device_login`)
  feeding the notifications read model (ADR-0011 / #45 — "new-device
  login" is on its named source list) and #1 security telemetry.

### 4. Temporary account lock (member panic): instant server-side kill; the UNLOCK is strictly stronger than the lock

**Decision: ship it, with an asymmetric strength rule — locking is
one tap; unlocking is a staff-verified act.**

- **Lock**: a single authenticated member request
  (`POST /member/lock`, idempotent). In one transaction it (i) sets a
  lock state on the credential (a `locked_at`/lock-state column or
  companion row — implementation MR's choice, with a DB constraint,
  gate 1.5), (ii) revokes ALL refresh families for the credential,
  (iii) writes the audit row, (iv) enqueues a security event (#45,
  #1). The per-request live-link re-check (§3) makes every
  outstanding access token die at its next request — instant in the
  only sense that matters (one request), and OTP request/verify
  refuse a locked credential behind the same opaque 202 posture
  (least disclosure — a locked account is not an oracle).
- **Unlock — the rule that makes the feature worth having**: an
  attacker holding the victim's phone (and, post-SIM-swap, their SMS)
  must not be able to unlock what the victim locked. Therefore the
  unlock path MUST NOT be satisfiable by anything the phone alone
  provides. **Day one: staff-assisted only** — the member contacts
  the SACCO; staff verify identity out-of-band and unlock via the
  existing staff `member_identity` authority (maker-checker per #8
  once the engine lands), with full audit. A future self-service
  unlock may ship ONLY if it chains at least full re-KYC-grade
  identity proof PLUS a fresh OTP — and never SMS-OTP alone. The
  deliberate consequence: a panicked member who locks and then finds
  their phone must call the SACCO. That friction is the feature.
- Lock is credential-scoped (all devices), not device-scoped —
  panic semantics are "freeze my access everywhere"; per-device
  removal is §3's job.

### 5. Self-service transaction limits: LOWER-only; raising is staff maker-checker with cooling-off

**Decision: a member may LOWER their own effective transaction limits
below the !17 tenant caps, effective immediately. A member can NEVER
raise a limit — any raise (including restoring a self-lowered limit)
is a staff maker-checker act (#8) with a cooling-off delay before
effect.**

- Effective limit = `MIN(tenant cap (!17), member self-cap)`. The
  self-cap is stored per member (tenant-scoped, audited, optimistic-
  locked) and enforced server-side inside the same atomic withdrawal
  chain !17 built (cap read + day-total + posting in one transaction
  — no TOCTOU); the client renders it, never enforces it.
- Lowering: immediate, one authenticated request, in-transaction
  audit + notification event. Lowering is always safe — it only
  shrinks what an attacker can take.
- Raising: the SIM-swap attacker's FIRST move is raising limits, so
  raising is not a member self-service act at all. It enters the #8
  maker-checker queue (maker = staff recording the member's verified
  request; checker = a different principal), and takes effect only
  after a cooling-off window (tenant-configurable, recommended
  ≥24h) — with a notification (#45) fired at request time AND at
  effect time, so the real member sees the raise coming while it can
  still be cancelled/locked (§4).
- Until #8's engine lands, there is no raise path at all: self-caps
  can go down and staff can restore them to the tenant cap via the
  existing staff authority with audit — never above it.

### 6. Session policy: keep OTP-per-family + rotation/reuse-kill; DB-fenced, algorithm-agnostic; concurrent sessions allowed and visible

- **Inactivity**: access tokens stay ≤15 min; the client keeps its T0
  inactivity auto-logout (#43). Server-side inactivity is enforced by
  refresh-family idle expiry: a family unused past its idle window
  dies (the 14-day absolute TTL already exists; the implementation
  adds an idle bound ≤ the absolute bound). No sliding-forever
  sessions.
- **Rotation + reuse detection: UNCHANGED — this posture is already
  correct.** Rotate-per-use, reuse-revokes-the-family, revocation
  committed even on the failed request (`rotate_member_refresh_token`).
  This ADR binds new state (device rows §3, lock state §4) into the
  SAME fences: the rotation-time live-link re-check and the
  per-request gate.
- **Algorithm-agnostic by construction (#6 / !18)**: every decision
  in this ADR keys off DB rows re-checked at use time — credential
  status, device status, lock state, family status — never off token
  claims, signatures, or the signing algorithm. Refresh tokens are
  opaque non-JWTs the EdDSA migration explicitly leaves alone. When
  !18's kid-versioned EdDSA lands, nothing in this ADR moves. No new
  claim is added to member access tokens by this ADR; if the #44-era
  step-up ever needs a step-up marker, it will be a short-lived
  DB-backed grant, not a long-lived token claim.
- **Concurrent sessions: allowed.** One credential, many devices,
  each device one refresh family (§3). We choose visibility +
  revocability (device list, new-device alerts, panic lock) over an
  artificial single-session rule, which punishes phone upgrades and
  does nothing against an attacker who simply evicts the victim.

## Alternatives considered

- **Server-verified PIN now (option a)** — rejected FOR NOW, adopted
  as the #44-gated upgrade path (§1): a new verifier, lockout machine,
  reset flow and guessing surface, guarding a surface from which
  money cannot yet leave; complexity before its asset exists.
- **No PIN at all (option c)** — rejected: forfeits the ~90% OTP-cost
  reduction or (worse) invites an ungated stored refresh token;
  stolen-unlocked-phone mitigation is lost (§1).
- **Local PIN marketed as account security** — rejected as the
  issue's named anti-pattern: decorative security teaching members a
  false model. Honesty constraints in §1 are binding.
- **Biometric as a server factor** — rejected: the server cannot
  verify a fingerprint; a client-asserted "biometric passed" flag is
  the same rejected design as a client-asserted attestation flag (#29).
- **Overloading `member_credentials` as the device table** — rejected:
  cardinality (one credential, many devices) and authority (staff
  revoke ≠ member device removal) both differ (§3).
- **Client-asserted device identifiers as the inventory key** —
  rejected: trivially forgeable; only the #29 attestation-verified
  identity binds a row.
- **SMS-OTP-satisfiable unlock of the panic lock** — rejected: the
  SIM-swap attacker holds SMS; the unlock would be weaker than the
  lock, inverting §4's rule.
- **Member self-service limit RAISING with step-up OTP** — rejected:
  step-up OTP over SMS is exactly what the SIM-swap attacker has;
  raising stays a staff maker-checker act with cooling-off (§5).
- **Single-concurrent-session policy** — rejected (§6): eviction
  races help attackers, hurt legitimate multi-device members, and add
  nothing the device inventory + alerts don't do better.

## Adversarial tests the implementation MRs MUST ship (named now, §4 falsifiability rule)

1. **Stolen-device PIN brute force**: N failed local unlock attempts
   wipe stored tokens (client suite); for the #44-era server PIN —
   the per-credential counter under row lock engages lockout after ≤5
   failures, the counter increment COMMITS on the failed request
   (the OTP `AuthFailure` pattern), a distributed guess across IPs
   still hits the per-credential lockout, and reset without
   staff-verified identity + fresh OTP re-proof is impossible. Each
   test must FAIL when its guard (counter, lock, wipe) is removed.
2. **Revoked-device next-request kill**: revoke device A while its
   ≤15-min access token is still unexpired → the very next request
   with that token is 401; device A's refresh token is family-dead;
   device B's session is untouched. Falsifier: delete the per-request
   device-status re-check and the test must fail.
3. **Lock-then-attacker-unlock attempt**: member locks; an attacker
   holding valid device tokens AND the SMS channel replays every
   member-reachable path (refresh, OTP request+verify with a fresh
   SMS code, all `/member` routes) — nothing unlocks or mints a
   session; OTP request answers the same opaque 202 with NO dispatch;
   only the staff-verified unlock path (audited, maker-checker once
   #8 lands) restores access.
4. **Limit-raise-then-drain**: with a compromised member session,
   attempt self-raise (route must not exist / 403), then simulate a
   staff-approved raise and attempt an over-old-limit withdrawal
   INSIDE the cooling-off window → refused atomically in the !17
   withdrawal transaction (typed 409, audit row); at window expiry
   the raise applies. Boundary-exact, concurrent-race leg included
   (!17's straddle-test pattern).

## Consequences

**Positive**: zero new server auth surface now; ~90% OTP delivery
cost reduction once local unlock + stored-family custody ships; the
device/lock/limit controls give members real, honest agency; every
new control reuses an existing proven fence (live-link re-check,
family revocation, !17 atomic caps, #29 verdicts, #8 maker-checker);
nothing binds to a signing algorithm.

**Negative / accepted costs**: the local PIN genuinely does not
protect the account server-side until the #44-gated upgrade — the
honesty constraints exist because this is a real limitation; locked
members must contact staff to unlock (deliberate); limit raises are
slow by design; device inventory quality depends on #29 attestation
rollout reaching `log-only`+ (in `off` mode there is no inventory).

**Sequencing / gating**: §3 device inventory needs the #29 seam
wired (log-only suffices for rows; enforce for trust) and lands after
!7-style member read/write discipline; §5 raising needs #8's engine;
§1's server PIN is gated on ADR-0010 (#44) Accepted; §4 lock and §5
lowering have no external gates beyond this ADR's acceptance. Events
in §§3–5 feed ADR-0011 (#45).

**Rollback**: this is a docs-only decision record — revert the
commit. Each implementation slice must ship expand→migrate→contract
migrations and its §-named adversarial tests; any slice can be
reverted independently since each reuses existing fences rather than
replacing them.

## References

- Issue #46 (this ADR's mandate), #43 (T3.3), #29 (attestation seam),
  #18 (SMS gateway/cost), #6 + !18 (EdDSA/kid ADR direction), #38
  (rate limiter), #8 (maker-checker), #44/ADR-0010 (money-out),
  #45/ADR-0011 (notifications), !17 (tenant caps, withdrawal_holds),
  !7 (live-link re-check discipline).
- `docs/member-auth.md`; `backend/src/genesis/application/member_auth.py`;
  `backend/src/genesis/api/member.py`; `backend/src/genesis/api/authz.py`
  (`RequireMemberPrincipal`).
