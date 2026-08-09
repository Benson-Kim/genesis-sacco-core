Write the HOUSE-FORMAT close-out MR description for the current branch. Structure:

1. **What / Why** (refs the issue; never auto-close #31/#32/#30 — no `Closes` keywords)
2. **§5.9 pre-implementation review** (keep the recorded one)
3. **Scope → commit map** (every commit from `git log --oneline <base>..HEAD`, grouped by scope)
4. **Falsifiability matrix** — one FALSIFIABLE test per claim (state what removal/flip makes it fail)
5. **Rule-14 migration declaration** — exact numbers + downgrade behaviour, or the audited
   "ships NO migration" with the serving index names
6. **Definition of Done** — tick ONLY with in-project pipeline evidence at the FINAL HEAD;
   cite the pipeline id and coverage. Unticked boxes get a stated reason.
7. **EXPLAIN output** — pasted VERBATIM from the backend:test after_script in the CI trace
   (never a local or hand-transcribed run); state job id + provenance
8. **Evidence honesty (rule 16)** — sandbox limitations, generated-artifact derivation method,
   anything that could mislead a reviewer
9. **Rule-16 incident register** — every mangled edit, lost hunk, flake, dead session, project
   move, with its disposition
10. **Rollback plan**

Then list the review items still open before undraft. DO NOT merge — human review merges.
