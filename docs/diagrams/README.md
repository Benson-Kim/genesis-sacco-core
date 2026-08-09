# docs/diagrams — architecture & threat-model diagrams (P-DIAG series)

Conventions for every diagram in this directory (BUILD_PROMPTS
PHASE B2 common rules):

- **Diagrams-as-code, Mermaid preferred.** One file per diagram,
  kebab-case names. A diagram lives either in a standalone `.mmd` file
  or inside a ` ```mermaid ` fenced block of a companion `.md` document
  (the companion carries the derivation, code citations and arguments
  the diagram alone cannot).
- **Audience rule (P-DIAG drift MR).** Every diagram declares its
  audience. **Business-facing** diagrams (the DFD, all sequence
  diagrams, the `flow-*` user flows) use business vocabulary INSIDE
  the drawing — no function names, table names or HTTP verbs — and
  pair the drawing with (a) a plain-language narrative of the business
  rule and (b) a **Source of truth** footer table mapping every
  element/step to the implementing file(s) and function/route names.
  **Engineering-facing** diagrams (lock-order, ERD, C4, STRIDE) keep
  full technical precision and in-diagram citations.
- **Authoring SHA.** Every diagram file starts with a header comment
  citing the `main` commit SHA it was authored against (plus the SHA
  of any later reconciliation pass). Diagrams depict main **as-built**
  at that commit; not-yet-built flows may appear only with an explicit
  `PLANNED (Pn)` / `INCOMING (!MR)` label and must be flipped to
  as-built by the executing prompt's MR (v1.2 rule 11).
- **No invented structure.** Every box and edge must be traceable to a
  module, table, lock, or route on main; the source file is cited next
  to the element (in the companion/footer table or a `%%` comment).
- **Drift rule (v1.2 rule 11).** Any MR that changes a diagrammed flow,
  table, lock-graph edge, or trust boundary MUST update the affected
  diagram(s) in the same MR. A stale diagram is a rejected MR.
- **CI validation.** The `docs:diagrams` job (`.gitlab-ci.yml`) renders
  every standalone `.mmd` file AND every ` ```mermaid ` block found in
  `docs/diagrams/*.md` with mermaid-cli. It runs only when
  `docs/diagrams/**` changes. A diagram that does not render is a red
  pipeline — never bypass it by moving the diagram out of this
  directory. The render job is a SYNTAX gate; the SEMANTIC gates are
  the checked-in spot-check scripts (run them from the repo root and
  keep them passing; extend them when you add claims):
  `python3 docs/diagrams/c4-spot-check.py` (module paths, router
  completeness, pinned function claims, sequence-diagram citations)
  and `python3 docs/diagrams/erd-spot-check.py` (table coverage, both
  ways). The C4/sequence check runs in CI as the `docs:spot-check`
  job (`.gitlab-ci.yml`, !38 review R1 — triggered by changes to
  `docs/diagrams/**` or `backend/src/genesis/api/app.py`); the ERD
  check is still run manually / by reviewers.

## Index

Status legend: **as-built** = verified against main @ the SHA in the
file header. Source-of-truth convention: business-facing files carry
their code citations in a footer table; engineering-facing files cite
inline/in companion tables.

| Diagram | Audience | Prompt / origin | Status |
|---|---|---|---|
| [`lock-order.md`](lock-order.md) — **authoritative lock-ordering DAG** (the single authority for every lock-order statement; v1.2 rule 11) | engineering | P-DIAG.0 | as-built (E1–E24; §8 incl. the 0034/!51-N1 delta, landed by !54) |
| [`c4-context.md`](c4-context.md) — C4 L1 system context (clients/providers marked PLANNED; member principal as-built since !65) | engineering | P-DIAG.1 | as-built @ head 0037 (same-commit refresh, issue-#30 close-out !71) |
| [`c4-container.md`](c4-container.md) — C4 L2 containers (layers, four workers, migration runner, store properties) | engineering | P-DIAG.1 | as-built @ head 0037 (same-commit refresh, issue-#30 close-out !71) |
| [`c4-component.md`](c4-component.md) — C4 L3, one diagram per API router group (22); spot-check: [`c4-spot-check.py`](c4-spot-check.py) | engineering | P-DIAG.1 | as-built (incl. the P14.5 member-surface routers, !65) |
| [`erd.md`](erd.md) — ERD, all 47 tables at alembic head 0037 (seven subject-area diagrams); spot-check: [`erd-spot-check.py`](erd-spot-check.py) | engineering | P-DIAG.2 | as-built @ head 0037 (0037 refreshed in the same commits as the migration, !71) |
| [`dfd.md`](dfd.md) — **data-flow diagrams**: L0 context + L1 money flows F1–F14, trust boundaries TB1–TB4, source-of-truth footers | business (footers for engineers/auditors) | P-DIAG.3 | as-built incl. corrections/write-off/recovery (!46/!47/!51/!52) |
| [`stride.md`](stride.md) — **STRIDE-per-element threat model** over the dfd.md elements; residuals with named owners or UNOWNED | engineering | P-DIAG.4 | as-built incl. F10–F14 rows |
| [`sequence-committee-voting.md`](sequence-committee-voting.md) — committee voting (4 consumers: loans, exits, dividends, write-offs) | business | P-DIAG.5 | as-built |
| [`sequence-snapshot-bind-reverify.md`](sequence-snapshot-bind-reverify.md) — approve-the-frozen-figures pattern (4 consumers incl. maker-checker adjustments) | business | P-DIAG.5 | as-built |
| [`sequence-outbox-dispatch.md`](sequence-outbox-dispatch.md) — the notification promise (P5 worker + P13.17e hardening) | business | P-DIAG.5 | as-built |
| [`sequence-recovery-receipt.md`](sequence-recovery-receipt.md) — bad-debt recovery receipt: partial/full recovery, over-recovery refusal, guarantee discharge | business | P-DIAG drift MR (issue #21/!51) | as-built |
| [`sequence-repayment-adjustment.md`](sequence-repayment-adjustment.md) — maker-checker adjustment: request → approve-with-reverify → post; reject frees the slot | business | P-DIAG drift MR (!46/!52) | as-built |
| [`sequence-recovery-case-lifecycle.md`](sequence-recovery-case-lifecycle.md) — case open → work → job-only closes + staff dispositions | business | P-DIAG drift MR (P13.16/!47) | as-built incl. !53 dispositions + !54 hardening (flipped by !55) |
| [`sequence-member-exit-claim-guard.md`](sequence-member-exit-claim-guard.md) — member exit and the debts that block the door | business | P-DIAG drift MR (P12 + !51) | as-built |
| [`sequence-guarantor-consent-principal.md`](sequence-guarantor-consent-principal.md) — guarantor consent as the MEMBER principal: link → sign-in → consent/withdraw → attested override (DFD F15) | business | issue-#30 close-out MR !71 (P14.5/!65) | as-built @ `047d4e39` |
| [`flow-teller-money-in.md`](flow-teller-money-in.md) — teller deposits & repayments incl. every visible refusal | business | P-DIAG drift MR | as-built |
| [`flow-loan-lifecycle.md`](flow-loan-lifecycle.md) — the life of a loan: application → committee → payout → arrears → write-off → recovery | business | P-DIAG drift MR | as-built |
| [`flow-checker-approvals.md`](flow-checker-approvals.md) — the checker's four-eyes queue | business | P-DIAG drift MR | as-built |
| [`flow-recovery-officer.md`](flow-recovery-officer.md) — the recovery officer's worklist day | business | P-DIAG drift MR | as-built incl. !53 dispositions + !54 hardening (flipped by !55) |
