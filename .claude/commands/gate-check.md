Run the pre-push gate battery on the working tree and report pass/fail per item:

1. **No-money-math grep gate**: money words (amount, balance, total, deposit, share, loan,
   dividend, interest, fee) must never abut `+`, `/` or `-` in changed files. Show offenders.
2. **Generated-file integrity**: `python3 -c "import json; json.load(open('web/packages/api-client/openapi.json'))"`;
   `wc -l web/packages/api-client/src/generated/schema.d.ts` (must not have shrunk vs git HEAD).
3. **Tenant predicates**: every new/changed SQL statement in the diff carries an explicit
   `tenant_id = CAST(:tid AS uuid)`-style bound predicate. Show any statement without one.
4. **Bound parameters only**: no string-interpolated VALUES in SQL (identifiers from code-owned
   maps only, with the stating comment).
5. **Key-exactness**: if MemberOut/contract shapes changed, grep every fixture/key-pin for the
   old key count and list files still pinning it.
6. **Assertions never weakened**: `git diff` on test files must show no deleted/loosened
   assertions without an explicit justification.
7. **Line-count sanity**: `git diff --stat` — flag any file whose deletions vastly exceed the
   intended edit size (possible truncation).

Fix everything red, commit, push. CI remains the final arbiter.
