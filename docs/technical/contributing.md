# Contributing — the house engineering doctrine, distilled

The binding documents are [`docs/MASTER_PROMPT.md`](../MASTER_PROMPT.md)
(the gates are merge blockers) and [`docs/BUILD_PROMPTS.md`](../BUILD_PROMPTS.md)
(the sequenced build plan and process rules), plus
[`CLAUDE.md`](../../CLAUDE.md) for agent-session conventions. When this
summary and those documents disagree, the doctrine documents win.

## 1. The non-negotiable gates (merge blockers)

| Gate | Short form |
|---|---|
| **1.1 Reuse-first** | Search the codebase before writing anything; re-implementation of an existing capability is a rejected MR. Shared logic lives in shared modules/packages. |
| **1.2 Reliability** | No silent failures (no bare `except`, no swallow-and-continue); side effects only via the transactional outbox; telemetry never breaks the action; health/readiness probes on every service. |
| **1.3 Scalability** | Keyset pagination on all lists (max 100); no N+1 (query-count assertions); every hot query index-served with EXPLAIN evidence in the MR; exports only via the shared streaming helper; no blocking I/O under row locks. |
| **1.4 Concurrency** | State transitions through a single transition function under `SELECT … FOR UPDATE`; optimistic `version` columns (stale = 409); `Idempotency-Key` on every mutation; approvals bind to persisted snapshots and re-verify under locks (no TOCTOU); atomic uniqueness claims checked by rowcount; race-safe reference generation. |
| **1.5 Data integrity** | Constraints in the database; append-only double-entry ledger (corrections are reversing entries); in-transaction audit row for every mutation; multi-step money operations in one transaction; period-based values derive from posting history, never snapshots. |
| **1.6 Security** | Every endpoint authenticated and RBAC-authorized server-side per handler; forced RLS **plus** explicit tenant predicates; money parameters server-resolved (`extra="forbid"`); bound parameters only; least disclosure; no PII in logs; secrets via CI variables only. |

## 2. Pre-implementation review (§5.9)

Before the first line of code, record in the MR description:

1. **Reuse audit** — what already exists, what will be reused, what
   duplication is removed.
2. **Lock-order match** — the lock chains the change will take, verified
   against [`docs/diagrams/lock-order.md`](../diagrams/lock-order.md) (the
   single authority; MRs reference it, never restate chains).
3. **Threat model** — caller-controlled parameters? TOCTOU windows? tenant
   scoping? partial-state risks?

## 3. Testing expectations (falsifiability)

- Test oracles are **hand-computed** and documented in comments — never
  captured from the implementation under test.
- Idempotency proven by side-effect **row counts**, never return values.
- **Falsifiability matrix**: every guard test must fail when its guard is
  removed (row lock deleted, UNIQUE bypassed, anti-join dropped). MRs carry
  an FM table mapping each guard to the test that dies without it. A test
  that cannot fail is a rejected test.
- Mandatory adversarial legs per feature: concurrent double-submit (exactly
  one effect), stale-version edit (409), cross-tenant access (zero rows),
  truncated-export headers, outbox retry after provider failure.
- Kill-switch atomicity for every multi-step money operation: abort
  mid-transaction and prove zero partial state.
- No coverage padding, no weakened gates, no `allow_failure` on gating jobs,
  no skipped downgrade paths. Never make a failing test pass by weakening it.

## 4. Migration declarations (rule 14)

Claim migration numbers **up front** in the MR description (check the chain
head first — parallel tracks collide otherwise). An MR that ships no
migration states "ships NO migration" only after an index audit of the
queries it adds. Numbering, staging and downgrade requirements:
[operations.md](operations.md#2-migration-workflow).

## 5. Evidence honesty (rule 16)

- Tick a Definition-of-Done checkbox only with pipeline evidence; pending
  items stay unchecked with a stated reason.
- Cite only pipelines/commits that resolve at the **current** project path
  — the repository has been re-imported before, and stale ids do not
  resolve. Re-resolve; never trust old citations.
- Keep an incident register in the MR description: what failed, what
  fallback was used, what remains unverified. Hiding a trade-off is not
  allowed.
- Report real coverage of partial data (paginated API reads, sampled
  checks) as partial — never present it as complete.

## 6. MR and merge policy

- One coherent unit of work = one branch = one MR, **Draft until the
  terminal pipeline is green** at the final HEAD; undraft with the pipeline
  id cited in the description.
- **Humans merge** — contributors (and agents) never merge their own MRs;
  squash on merge. Merged MR descriptions are never rewritten — corrections
  land as comments.
- Plain-merge `main` into feature branches; **never rebase or force-push**;
  on a rejected push, fetch and plain-merge.
- No auto-closing keywords (`Closes`/`Fixes`/`Resolves`) — issue lifecycle
  is managed deliberately, never as a merge side effect.
- Diagram drift (rule 11): an MR changing a diagrammed flow, table,
  lock-graph edge or trust boundary updates the affected diagram(s) in the
  same MR.
- **Narrative hygiene**: repository documentation is provenance-free —
  prose describes the system, not the process that built it (no issue/MR
  numbers, review-finding codes or batch labels in docs; migration
  filenames appear only in the migration catalog table).

## 7. Anti-corruption editing discipline

Hard rules earned from real incidents (full list in `CLAUDE.md`):

- Never hand-edit generated files (`openapi.json`, `schema.d.ts`).
- Never interactively whole-file-edit files over ~2000 lines; use scripted,
  single-asserted replacements (assert the old string occurs exactly once).
- After any edit: re-read the changed region, verify the line count did not
  shrink unexpectedly, grep each intended hunk — then commit.
- Commit and push every coherent unit within minutes; crashed sessions must
  never lose completed work. Remote is truth: after pushing, re-fetch and
  diff.
- Money words never abut `+`, `/` or `-` in code or comments (the no-money-
  math grep gate); comma-separate money words in prose.
