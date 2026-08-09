# Security model

As-built authentication, authorization, segregation-of-duties and disclosure
doctrines. The threat model itself is diagrammed in
[`docs/diagrams/dfd.md`](../diagrams/dfd.md) and
[`docs/diagrams/stride.md`](../diagrams/stride.md) — reference those instead
of re-deriving trust boundaries.

## 1. Authentication

### 1.1 Sign-in flow (OTP gate)

Implemented in `backend/src/genesis/api/auth.py` and
`backend/src/genesis/application/auth.py`; the web login screen is
`web/src/modules/auth/components/LoginGate.tsx`.

1. The client sends `POST /auth/otp/request` with a sign-in identifier and
   the `X-Tenant-ID` header (pre-auth endpoints scope the tenant from this
   explicit header). Rate limiting applies to all auth endpoints.
2. The identifier may be an **email address or a Kenya mobile number**.
   Classification is one rule (`application/auth.py:resolve_signin_identifier`):
   anything the Kenya-mobile rule accepts is a phone; everything else takes
   the email path. Accepted phone input formats are exactly four —
   `07XXXXXXXX`, `01XXXXXXXX`, `+2547XXXXXXXX`, `+2541XXXXXXXX` — normalized
   to E.164 by the single normalizer
   (`domain/members.py:normalize_kenya_msisdn`). Storage is always E.164.
3. A single-use challenge is issued: 6 digits, at most 5 attempts, 5-minute
   TTL, constant-time compare (`domain/otp.py`). Codes are stored only as
   keyed hashes and delivered via the transactional outbox.
   **Non-enumeration:** an unknown identifier produces the same response
   shape and burns the same hash cost as a known one — the wire carries no
   existence oracle.
4. `POST /auth/otp/verify` checks the newest challenge under a row lock
   (user row first, then challenge row — the documented lock order).
   Failures are returned as values, not raised, so the punitive state
   (attempt counter, lockout) commits even though the request fails with a
   sanitized 401.

### 1.2 Tokens

- **Access tokens**: signed JWT, ≤ 15 minutes. Two disjoint audiences —
  staff and member — and the decoder dispatches deny-by-default: a member
  token can never satisfy a staff permission gate and vice versa (403).
- **Refresh tokens**: opaque, hashed at rest, 14-day TTL, organized into
  families. Every use rotates the token; **reuse of a spent token revokes
  the entire family**. Logout revokes the family behind the presented token.
- Suspending a user voids pending OTP challenges and revokes refresh
  families in the same transaction (`application/users.py`), and the authz
  dependency re-checks active status on every request — a live access token
  never bridges a committed suspension.

### 1.3 Member principal

Member-facing routes use `RequireMemberPrincipal` (`api/authz.py`): the
credential→member link is re-verified against the database on every request,
and money-relevant member actions re-verify it again inside their
transaction under the relevant row lock. A revoked or re-pointed credential
dies within one request.

## 2. RBAC (authorization)

Single source of truth: `backend/src/genesis/domain/rbac.py`, enforced
per-request by `api/authz.py:RequirePermission` — a CI test walks the route
table and fails if any business operation lacks the dependency.

- **Flat matrix**: role × module × action (`view`/`create`/`edit`/`approve`),
  **deny by default** — only explicit grants are true.
- Seeded roles: System Admin, Branch Manager, Loan Officer, Teller,
  Credit Committee, Accountant, Auditor, plus the seeded **senior tier**
  (Senior Credit Officer, supervising the Loan Officer —
  `domain/rbac.py:SENIOR_TIERS`). The matrix stays flat: a senior role is a
  distinct seeded role whose explicit grants form a superset of its junior
  role's grants (no inheritance, no chain walk at enforcement time; the
  superset invariant is pinned by tests). Senior tiers gain nothing on the
  narrow channels (`corrections`, `member_identity`) or the admin modules,
  and amount authority stays in the approval-bands matrix — a senior tier
  is a role name plus a higher configured band ceiling, never a new
  enforcement mechanism.
- Modules: members, applications, loan_book, transactions, reports,
  settings, access_control, plus two deliberately **narrow** modules whose
  grants never inherit the generic defaults:
  - `corrections` — the fraud channel (adjustments, fees, write-offs).
    Maker: Accountant (`create`). Checkers: Branch Manager / Credit
    Committee (`approve`). Auditor: view only.
  - `member_identity` — credential links and the staff-attested consent
    override. Only System Admin and Branch Manager hold write powers;
    Auditor views.
- Enforcement is server-side on every request: token decode, then an
  active-status re-check and the grant lookup against the database. The web
  console mirrors the matrix for navigation only
  (`web/src/modules/authz/`); hiding is UX, never security.

## 3. Segregation-of-duties invariants

- **Recommender-cannot-vote** (`application/loan_applications.py`): moving
  an application into committee records the acting user as recommender; that
  user is refused as a voter on the same application (checked under the
  application row lock).
- **Maker–checker** (`application/sod.py:require_distinct_non_assurance_checker`,
  one shared implementation): for repayment adjustments, write-offs and
  share transfers, the checker must be a *different user* than the maker
  (DB CHECK backstop on the workflow tables) and must not hold an assurance
  role.
- **Assurance-role exclusion** (`domain/rbac.py:ASSURANCE_ROLES`): the
  Auditor's function is reviewing the trail, so the Auditor is excluded from
  acting inside the workflows it audits (checker actions, collections
  assignability) even where a view grant exists. Role names are resolved
  server-side from the actor's role id, never from the JWT or a client flag.
- **Last-admin guard** (`application/users.py`): the last active System
  Admin can be neither suspended nor re-roled (409), evaluated under a lock
  on the ordered active-admin set.
- **Committee quorum** (`domain/committee.py`): a decision is reached when
  one side accumulates the tenant-configured quorum; when both sides reach
  quorum in the same read, **rejection wins** (prudential conservatism).
  One vote per member per application is a DB UNIQUE constraint.

## 4. Approval authority bands

`domain/tenant_config.py` (validation + resolution) and
`application/tenant_settings.py:enforce_authority_band` (enforcement).

- The tenant-configurable approval matrix is a list of bands with
  **contiguous `(lower, upper]` semantics**: strictly increasing ceilings,
  authorities drawn only from the code-owned role names, at most one
  unbounded final band. A finite top band is legal — amounts above it
  resolve to *no listed authority* (an explicit "board above" tier).
- Enforcement runs under the workflow's own row lock at ratifying actions
  (stage transitions to/through approval, committee votes, correction
  approvals): the actor's role is resolved server-side and checked against
  the band containing the amount.
- **Fail-closed posture**: an actor without a resolvable role has no
  authority (403). A role holding the RBAC grant but *not listed* in a
  configured matrix degrades to the **first band's ceiling** — a
  misconfigured matrix can never silently uncap a money ceiling. Only a
  tenant with no matrix configured at all skips band enforcement. Stored
  band JSON is **revalidated at read time**; corrupted configuration fails
  closed with a 409 rather than disabling the guard.

## 5. Disclosure doctrines

- **Least disclosure**: error messages never echo balances, capacities,
  pledge totals, submitted references, or other derived figures — the
  client receives a sanitized category plus correlation id
  (`backend/src/genesis/errors.py`); the in-transaction audit row records
  the exact figures for staff entitled to them. Withdrawal and pledge
  refusals are deliberately generic for this reason.
- **Non-enumeration**: authentication never reveals whether an identifier
  exists (equal response shape, equal hash work); malformed phone-like
  identifiers surface the same outcome as any unknown email. Pagination
  cursors are opaque and HMAC-signed so keyset positions can neither be
  read nor forged (see
  [ledger-and-money.md](ledger-and-money.md#8-keyset-pagination-and-signed-cursors)).
- **No PII in logs**, analytics, error messages or URLs; OTP codes are never
  logged; secrets come only from environment/CI variables (literal
  credentials fail secret-detection CI).
- Money-affecting parameters (rates, fees, periods) are resolved
  **server-side** from tenant/product configuration; request bodies reject
  unknown fields (`extra="forbid"`), so a caller-supplied rate is
  structurally impossible.

## 6. DEV-ONLY OTP display flag

`Settings.dev_otp_display` (`backend/src/genesis/settings.py`), consumed
only by the OTP-request handler in `backend/src/genesis/api/auth.py`.

- SMS/email delivery is not built yet, so with the flag enabled the issued
  code is returned in the `/auth/otp/request` response body (`dev_otp`) for
  testers. The code is never logged and appears in no error path.
- The flag is **fail-closed**: off by default; enabling requires an explicit
  `DEV_OTP_DISPLAY` environment value in a development environment.

> ⚠️ **This flag and its API consumer MUST be removed before any staging or
> production deployment.** The removal note is recorded in both the settings
> module and the handler. Until removal, treat any environment with the flag
> enabled as a development sandbox only.
