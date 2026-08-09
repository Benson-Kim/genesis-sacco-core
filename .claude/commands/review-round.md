Perform a senior core-banking review of merge request: $ARGUMENTS

Audit axes (verdict per axis, findings numbered R1..Rn with severity blocker/major/minor/info,
file:line, the exact gate/rule violated, and the minimal remedy):

- **Gates 1.1–1.6** (docs/MASTER_PROMPT.md): reuse, reliability, keyset/index discipline,
  FOR UPDATE / version / idempotency on mutations, DB constraints, RBAC/tenant/least-disclosure.
- **Accounting truth**: double-entry rendered verbatim (never summed/netted client-side),
  append-only ledger, corrections as reversing entries, period-basis derivations (no snapshot
  basis), occurred_at end-of-period, snapshot-bind-reverify on approvals, SoD/maker-checker.
- **Contract discipline**: expand-only, nullable-never-optional, generated files arbitrated by
  the drift jobs (never hand-edited), key-exactness suites, extra="forbid" on bodies.
- **Test integrity (§4)**: hand-computed oracles, falsifiability (would each guard test fail if
  its guard were removed?), side-effect row-count idempotency proofs, no weakened assertions.
- **Evidence provenance (rule 16)**: every cited pipeline/commit resolves at the CURRENT
  project path; DoD ticks backed by real evidence; EXPLAIN from a real CI trace.
- **Diagram drift (rule 11)**: schema/lock/trust-boundary changes update the diagrams in-MR.

Persist the findings as ONE MR note immediately (so nothing is lost if the session dies),
blocking items first. Do not fix anything in this review pass.
