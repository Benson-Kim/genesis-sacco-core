# ADR-0008: Application-layer limits/approval engine — maker-checker with amount-tiered, effective-dated approval bands

- Status: Accepted
- Date: 2026-08-19
- Deciders: Genesis Prestige engineering (issue #8, gap register G2)

## Context
The coded permission model is a module×action grid (P4 RBAC,
`genesis/domain/rbac.py`). Amount-banded approval authority (Loan
Officer ≤ 100k, Branch Manager ≤ 500k, Credit Committee ≤ 2M, Board
above) exists as a tenant setting consumed by a handful of committee
paths (`tenant_settings.enforce_authority_band`,
`domain/tenant_config.authority_may_ratify`) but not as a general
enforcement engine: most posting-capable operations never ask the
band question, the band matrix is a single mutable settings key with
no effective dating, and there is no first-class pending-approval
record a second principal ratifies. Deposit-taking grade
(MASTER_PROMPT §1.4/§1.5, `docs/INSTITUTIONAL_GAP_REGISTER.md` G2)
means maker-checker on every financial mutation and limits enforced
in the transaction path, not in the UI.

Constraints that shaped this decision:
- Reuse-first (§1.1): the SoD checker guard already exists once
  (`application/sod.py`) and must not fork; the band vocabulary and
  validators already exist (`domain/tenant_config.ApprovalBand`,
  `validate_approval_bands`, `required_band_index`).
- Regulatory parameters are effective-dated configuration (issue #3
  direction): a band change must apply from a date, and history must
  show which bands were in force when a decision was taken.
- Migration-chain governance (issue #22): chain onto the current
  alembic head; if the head moves while in flight, RE-CHAIN the
  unreleased revision — never add a multi-parent merge revision.
- Open MRs own files this work must not touch
  (`application/corrections.py`, `api/security_refusals.py` — !11;
  `api/auth.py`, `infrastructure/rate_limit.py` — !3), so the engine
  ships UNWIRED: a fully-tested module plus schema; routing the
  posting paths through it is an explicit follow-up work item.

## Decision
We build a limits/approval engine in the application layer
(`genesis/application/approvals.py` over pure rules in
`genesis/domain/approvals.py`), enforced in the transaction path.

1. **Operation declaration.** Every posting-capable operation declares
   `(operation type, amount)` to the engine. Operation types are a
   code-owned vocabulary (`domain/approvals.OperationType`), never
   free-form caller strings.

2. **Tenant-configurable, effective-dated bands.** Approval bands live
   in a new `approval_band_sets` table: tenant-scoped, append-only
   (DB triggers refuse UPDATE/DELETE), one immutable band matrix per
   `effective_from` date — the same discipline as the issue-#3
   regulatory-parameter direction. The set in force at a date is the
   newest `effective_from` not after that date. A tenant with no rows
   uses the code-owned day-one defaults, which pin today's prototype
   matrix exactly: Loan Officer ≤ 100,000; Branch Manager ≤ 500,000;
   Credit Committee ≤ 2,000,000; amounts above the finite top band
   resolve to the "Board" tier — deliberately not a platform role
   (the explicit top-band rule in `validate_approval_bands`), so
   nothing inside the platform can self-ratify a board-tier amount.
   Band matrices are validated with the existing
   `validate_approval_bands` at write AND read (corrupt stored bands
   fail closed for consumers).

3. **Band resolution is pure.** `domain/approvals.bands_in_force`
   (schedule selection) and `required_band_index` /
   `authority_may_ratify` (tier resolution, reused from
   `domain/tenant_config`) are pure functions of `(amount, bands)`.
   Boundary semantics are inclusive ceilings: an authority at index i
   may ratify amounts `<= max_amount[i]` — 100,000.00 is Loan-Officer
   business; 100,000.01 is not (golden-pinned at all three defaults).

4. **Below-band proceeds; above-band pends.** If the maker's own
   server-side-resolved role covers the amount under the bands in
   force, the operation proceeds (the maker is recorded as
   `created_by` — the 0036 attribution pattern). Otherwise the engine
   inserts a `pending_approvals` row (tenant-scoped, write-once
   workflow table) recording maker, operation type, amount, the
   required tier at request time, and the operation's branch; the
   operation posts NOTHING until ratified.

5. **A different principal ratifies.** Ratification (and decline)
   reuses `application/sod.require_distinct_non_assurance_checker`
   verbatim: the checker must be a distinct, tenant-vouched,
   non-assurance principal. Beneath the app guard, the DB itself
   makes maker-self-check unrepresentable:
   `ck_pending_approvals_sod CHECK (checker_id IS NULL OR
   checker_id <> maker_id)` plus a write-once trigger (the 0031
   pattern) pinning identity/amount columns, permitting exactly one
   NULL→value fill of `checker_id`/`decided_at`, and enforcing the
   status machine `pending → ratified | declined` at the database.

6. **Ratification re-resolves bands; stricter-of-the-two applies.**
   At decision time the engine resolves the bands in force at the
   REQUEST date and the bands in force NOW, and the checker's
   authority must satisfy BOTH. A tenant tightening its bands between
   request and ratification therefore binds immediately; loosening
   them never retroactively weakens an already-pended request.

7. **Both principals on resulting postings.** `transactions` gains a
   nullable `checked_by uuid REFERENCES users(id)` column beside the
   0036 `created_by`: when a wired posting path executes a ratified
   operation it records maker (`created_by`) and checker
   (`checked_by`) on the posting itself. NULL stays the honest
   "no checker" (system jobs, below-band single-actor postings). The
   0004 append-only fence pins both the moment the row commits.

8. **Branch scoping groundwork.** Staff principals carry a home
   branch (`users.branch_id`, migration 0016); `pending_approvals`
   records the operation's branch. Acting across branches is a NAMED
   permission — never a default: the wiring follow-up seeds it as a
   dedicated narrow-grant module in the P4 matrix (the
   `_CORRECTIONS_GRANTS` precedent), and the engine refuses
   cross-branch maker/checker acts for principals without it. This
   ADR fixes the rule; the permission string ships with the wiring MR
   so the grid and its web mirror move in one change.

9. **Committee paths unchanged.** The engine sits beside — not inside —
   the committee quorum machinery (`domain/committee.decide`,
   quorum 2, rejection wins an ambiguous tally). Day one, committee
   voting behaves byte-identically to today; a golden fixture pins the
   full decision table.

## Alternatives considered
- **Widen the RBAC grid with amount columns** — amounts are not
  permissions; a module×action×amount grid explodes and cannot express
  effective dating or the pending/ratify workflow. Rejected.
- **Keep bands in the `tenant_settings.approval_bands` JSON key** —
  a single mutable row cannot answer "which bands were in force on
  date D" and its UPDATE-in-place history is invisible to audit
  reconstruction. The settings key stays for the existing committee
  consumers until the wiring MR migrates them; new engine consumers
  read only the effective-dated table. Rejected as the system of
  record going forward.
- **Per-workflow maker-checker tables forever (0031/0040 pattern
  per feature)** — correct but O(features) migrations and drift risk;
  the generic `pending_approvals` model gives every posting-capable
  operation the same enforced discipline. The existing 0031/0040
  workflows keep their specialized tables (reuse-first, no rewrite);
  new wiring uses the engine. Partially retained, generalized.
- **Enforce only in the application layer** — a compromised or buggy
  code path could self-approve; the DB CHECK + write-once trigger
  make collusion-by-single-principal unrepresentable even via direct
  SQL on the app role (the 0031 precedent). Rejected.

## Consequences
- Positive: limits become an enforcement engine, not a screen; band
  history is reconstructable per date; maker and checker are both on
  the posting; tenants can tighten authority without a deploy and the
  stricter band binds in-flight requests.
- Negative: one more workflow table to operate; wired paths gain a
  pending state their UX must surface; until the wiring follow-up
  lands, enforcement coverage is unchanged (the engine is dark).
- Migration path: expand-only revision (new tables + one nullable
  column) chained onto the current head; downgrade refuses loudly
  once approval history or checker attributions exist (the 0017/0020
  precedent) and reverses cleanly on an empty expansion.
- Rollback: unwiring is a no-op day one (the engine is dark by
  design); after wiring, rollback means routing posting paths back
  around the engine — the tables stay (append-only history is never
  dropped).
