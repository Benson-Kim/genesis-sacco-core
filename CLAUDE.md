# CLAUDE.md — Genesis Prestige (SACCO management platform)

This file is auto-loaded project memory for Claude Code. Read it fully; follow it strictly.
The two binding doctrine documents are `docs/MASTER_PROMPT.md` (gates 1.1–1.6 are MERGE
BLOCKERS) and `docs/BUILD_PROMPTS.md` (sequenced prompts P0–P24 + rules; rule 11 diagram
drift, rule 14 migration declarations, rule 16 evidence provenance). When this file and
those documents disagree, the docs win.

## What this project is

Multi-tenant SACCO (savings & credit co-operative) core-banking platform:
- **Backend**: Python 3.12 / FastAPI / PostgreSQL 16 (RLS-enforced multi-tenancy) / Redis / worker (outbox, exports, jobs). Layered: `api → application (owns transactions) → domain (pure) → infrastructure`.
- **Admin Web**: Next.js + TypeScript strict, TanStack Query, Zod-validated boundaries, generated OpenAPI client, in-house design system.
- **Mobile (planned, P16+)**: Flutter member + admin apps.
- **Prototype**: `genesis_prestige_app.html` is the canonical UX/domain source. It is LARGE — never read it whole; grep for the specific affordance.
- Currency: KES as `NUMERIC(18,2)`. NEVER float. NEVER client-side money math.

## Repository layout (orient here, do not re-explore blindly)

- `backend/src/genesis/api/` — FastAPI routers (one per module: members, transactions, loans, dividends, branches, corrections, recovery, dashboard, …)
- `backend/src/genesis/application/` — use-case services; own commit/rollback; module-level SQL constants (so EXPLAIN suites test the production statements)
- `backend/src/genesis/domain/` — pure logic: `lending.py` (amortization, classify() 30/90/180/360 → 1/5/25/50/100% provisioning), `ledger.py`, `members.py`, `rbac.py`
- `backend/migrations/versions/` — alembic chain (check the head before claiming a number; expand→migrate→contract; downgrade must pass migrate-check)
- `backend/tests/` — pytest against REAL Postgres (RLS on); `export_helpers.py`/`db_helpers.py` are the shared seeders; `test_*_explain.py` capture EXPLAIN artifacts to `backend/perf/`
- `web/src/modules/<module>/` — screens, `api.ts`, `schemas.ts` (Zod), `__tests__/` (screen + `.network.test.ts` wire suites)
- `web/e2e/` — Playwright against the real build with the API mocked at the browser network boundary
- `web/packages/api-client/` — `openapi.json` snapshot + `src/generated/schema.d.ts` (GENERATED — never hand-edit, see Hard Rules)
- `web/packages/design-system/` — shared UI primitives (`ConfirmDangerModal`, `KeysetTable`, …)
- `docs/diagrams/` — authoritative C4/ERD/DFD/STRIDE/lock-order diagrams + spot-check scripts (rule 11: schema/lock/trust-boundary changes update them IN THE SAME MR)

## Non-negotiable engineering gates (short form; full text in docs/MASTER_PROMPT.md §1)

1. **Reuse-first**: search before writing; re-implementation is a rejected MR.
2. **Reliability**: no silent failures; side-effects via the transactional outbox only.
3. **Scalability**: keyset pagination everywhere (max 100); every hot query index-served with EXPLAIN evidence in the MR; no N+1; no blocking I/O under row locks.
4. **Concurrency**: transitions under `SELECT … FOR UPDATE`; optimistic `version` (stale = 409); `Idempotency-Key` on every mutation; approvals bind to persisted snapshots and re-verify under locks (no TOCTOU).
5. **Data integrity**: constraints in the DATABASE; ledger is append-only double-entry (corrections = reversing entries); in-transaction `audit_log` for every mutation; period-based values derive from posting history, never point snapshots.
6. **Security**: JWT ≤15min + rotating refresh + OTP step-up; RBAC deny-by-default enforced server-side per handler; RLS + explicit `tenant_id = :tid` predicate on EVERY tenant-owned statement; money parameters resolved server-side only (`extra="forbid"`); bound parameters only; least disclosure (errors never echo balances/vocabularies); no PII in logs.

Testing (§4): hand-computed oracles documented in comments; idempotency proven by side-effect ROW COUNTS; every guard test must FAIL when its guard is removed; kill-switch atomicity for multi-step money ops; adversarial legs (double-submit, stale-version 409, cross-tenant zero rows) are mandatory per feature.

DoD (§5): includes §5.9 pre-implementation review (reuse audit + lock-order match vs `docs/diagrams/lock-order.md` + threat model) recorded in the MR description BEFORE the first line of code; honest DoD — tick only with pipeline evidence.

## House MR conventions

- One batch = one branch (`duo/feature/...`) = one MR, draft until the terminal pipeline is green.
- House description: What/Why · §5.9 review · scope→commit map · falsifiability-matrix (FM) table · rule-14 migration declaration (exact numbers; "ships NO migration" only after an index audit) · REAL EXPLAIN output pasted verbatim from CI · evidence-honesty section · rule-16 incident register · rollback plan.
- Plain-merge `main` into feature branches; NEVER rebase or force-push. Merged MR descriptions are never rewritten — corrections land as comments.
- Issue #31 carries the follow-up ledger; batches update it and never close #31/#32/#30 implicitly (watch for auto-closing `Closes` keywords).
- Evidence provenance (rule 16): this repo has been moved/re-imported repeatedly; cite ONLY pipelines/commits that resolve at the CURRENT project path. Pipeline ids from before a move 404 — re-resolve, do not trust old citations.

## CI (the sole arbiter)

Stages: lint → test → security → build → migrate-check. Key jobs: `backend:lint` (ruff+mypy strict), `web:lint` (eslint+tsc), `backend:test` (pytest vs real Postgres; prints `backend/perf/explain_*.txt` in after_script — copy EXPLAIN evidence from the job trace), `web:test` (jest), `web:e2e` (Playwright), `web:spec-drift`/`web:client-drift` (regenerate OpenAPI snapshot + client and diff byte-for-byte — they arbitrate generated files), `backend:migrate-check` (upgrade→downgrade→upgrade), security stage (SAST/secrets/dependency scanning) — criticals block merge. Coverage gate ≥85% on backend domain/application.

Known flake: `tests/test_run_export.py::test_export_rendering_never_blocks_the_event_loop` can trip on loaded runners (timing outlier). If it is the ONLY red and your change doesn't touch exports, re-run the pipeline rather than weakening the guard.

## Hard rules for Claude Code in this repo (reliability — earned from real incidents)

1. **Never hand-edit generated files**: `web/packages/api-client/openapi.json`, `web/packages/api-client/src/generated/schema.d.ts`. If local generators can't run, derive the delta from the drift job's own printed diff and apply it via a SCRIPT; verify JSON parses and line counts only GROW. (An interactive edit once truncated schema.d.ts from 8356 to 1980 lines.)
2. **Never use interactive whole-file edits on files >2000 lines** — scripted, single-asserted replacements only (assert the old string occurs exactly once before replacing).
3. **After ANY edit**: re-read the changed region, verify the file's line count didn't shrink unexpectedly, grep each intended hunk — THEN commit.
4. **Commit and push every coherent unit immediately** (within minutes). Long-lived unpushed work is forbidden — crashed sessions must never lose completed work. Remote is truth: after pushing, re-fetch and diff.
5. **Never rebase/force-push**; on a rejected push, fetch and plain-merge.
6. If sandbox package installs are blocked (npm/PyPI proxy-block is common here): do NOT retry installs; fix from the CI job trace and let CI arbitrate. Time-box every network-touching command (<120s).
7. Replicate the **no-money-math grep gate** before pushing: money words must never abut `+`, `/` or `-` in code/comments (comma-separate money words in prose).
8. Do not `git fetch --unshallow` (a session died on it) — use shallow, targeted fetches.

## Cost-control rules for Claude Code (read this, save money)

- **Do not read these whole**: `genesis_prestige_app.html`, `web/packages/api-client/src/generated/schema.d.ts` (~8.6k lines), `web/packages/api-client/openapi.json` (~13k lines), `docs/BUILD_PROMPTS.md` (1.6k lines), CI job traces. Use `grep -n` / `sed -n 'X,Yp'` for targeted slices.
- Prefer `git log --oneline`, `git show --stat`, and targeted `git diff <path>` over full-diff dumps.
- For CI verdicts, fetch job LISTS first; read only the FAILED job's trace, and only the failure region (`grep -n -B5 -A30 'FAILED\|✕\|●'`).
- Reuse the shared test seeders (`export_helpers.py`) instead of re-deriving fixtures.
- Start work with `/pre-impl-review` (see `.claude/commands/`) — the reuse audit prevents expensive re-implementation loops.
- Keep context lean: `/clear` between unrelated tasks; summarize long investigations into the issue/MR (persist findings to GitLab — comments on #31/the MR — so a fresh session never re-derives them).
- One MR pipeline run costs minutes; one wrong bulk edit costs a whole review round. Verify locally-cheap things (grep, ast-parse, json-parse) BEFORE pushing.

## Quick commands

- Backend tests (CI does this; locally only if deps exist): `cd backend && pytest -x -q`
- Lint: `ruff check backend && ruff format --check backend`; web: `cd web && npx tsc --noEmit && npx eslint .`
- Alembic head check before claiming a migration number: `ls backend/migrations/versions/ | sort | tail -3`
- Diagram gates: `python docs/diagrams/erd-spot-check.py && python docs/diagrams/c4-spot-check.py`
