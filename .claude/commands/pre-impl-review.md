Perform the MASTER_PROMPT §5.9 PRE-IMPLEMENTATION REVIEW for: $ARGUMENTS

Produce (and post into the MR description before writing any code):

1. **Reuse audit** — grep the codebase for existing capability: which modules/primitives/SQL
   constants/test seeders will be REUSED verbatim, which extended, and confirm nothing is
   re-implemented (gate 1.1). List exact file paths.
2. **Lock-order match** — read `docs/diagrams/lock-order.md`; state which existing lock edges
   the change rides and confirm NO new edge (or specify the same-MR diagram update, rule 11).
3. **Threat model** — caller-controlled parameters? TOCTOU windows? tenant scoping (explicit
   `tenant_id = :tid` on every new statement)? partial states (single-transaction ownership)?
   least-disclosure on every rejection path?
4. **Migration plan** — check the alembic head (`ls backend/migrations/versions | sort | tail -3`);
   claim the next free number or justify "ships NO migration" with a named-index audit (rule 14).

Be concrete: file paths, index names, lock edges. No code until this is recorded.
