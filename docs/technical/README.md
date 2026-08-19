# Technical documentation

Engineering-facing documentation for the Genesis Prestige SACCO management
platform. Every statement here describes **as-built** behaviour on the `main`
branch, with file paths cited so claims can be verified against the code.
The plain-language guide for SACCO staff lives in
[`docs/user-manual/`](../user-manual/README.md).

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | System overview, backend layering, web module structure, multi-tenancy, request lifecycle, the generated-client contract flow |
| [data-model.md](data-model.md) | Entity catalog (members, accounts, transactions, loans, guarantees, exits, dividends, corrections, recovery, branches, users, audit), plus the migration catalog table |
| [security-model.md](security-model.md) | Authentication (OTP, identifiers, refresh tokens), RBAC, segregation-of-duties invariants, approval authority bands, disclosure doctrines, the development-only OTP display flag |
| [ledger-and-money.md](ledger-and-money.md) | Double-entry model, posting builders, the withdrawal-source rule, penalty/interest/dividend flows, accounting periods, idempotency, keyset pagination and signed cursors |
| [api-guide.md](api-guide.md) | Reading the OpenAPI snapshot, auth headers, pagination and filter conventions, error taxonomy, contract evolution policy |
| [operations.md](operations.md) | CI pipeline stages and jobs, migration workflow, environment flags, development constraints |
| [contributing.md](contributing.md) | The house engineering doctrine distilled: merge gates, review expectations, evidence honesty, editing discipline |
| [aml-cft-program-design.md](aml-cft-program-design.md) | **Design, not as-built** — the AML/CFT program build contract (ADR-0009, issue #10): sanctions screening, threshold monitoring, detection rules, STR workflow, tipping-off wall, KYC refresh |

## Related authoritative sources

These documents do not replace the binding doctrine or the diagrams — they
summarize and cross-link them:

- [`docs/MASTER_PROMPT.md`](../MASTER_PROMPT.md) — the non-negotiable
  engineering gates (merge blockers) that govern every contribution.
- [`docs/BUILD_PROMPTS.md`](../BUILD_PROMPTS.md) — the sequenced build plan
  and process rules.
- [`docs/diagrams/`](../diagrams/README.md) — the authoritative C4, ERD, DFD,
  STRIDE, lock-order and sequence diagrams. Diagrams are drift-governed: an
  MR that changes a diagrammed flow updates the diagram in the same MR.
- [`CLAUDE.md`](../../CLAUDE.md) — repository conventions and hard rules for
  agent-driven contributions.
