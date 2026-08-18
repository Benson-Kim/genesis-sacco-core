# Consolidation Migration Note — genesis-sacco-core → sacco-var

**Date:** 2026-08-18
**Source project:** `mobto-group/genesis-sacco-core` (GitLab project ID 85523723)
**Destination (single source of truth):** `mobto-group/sacco-var` (GitLab project ID 85523661)
**Baseline:** both projects' `develop` branches were identical at `a2f6a9ac` at migration time. `main` was identical at `73c34cf4`.

## ⚠️ PULL-MIRROR RISK — READ FIRST

`sacco-var` is configured as a **GitLab pull mirror** of `https://github.com/Benson-Kim/genesis-sacco-core.git` with:

- `mirror: true`
- `mirror_overwrites_diverged_branches: false`

This is incompatible with sacco-var being the single source of truth: pushes to the
GitHub repo will silently appear here, and any branch that diverges between GitHub
and here will silently **stop updating**. Nothing was force-pushed during this
migration and nothing must ever be. Disabling the mirror is tracked in
**issue #12** ("Disable GitHub pull mirroring on sacco-var — single source of truth").

## Branches pushed (fast-forward/new only, no force)

Seven branches were pushed from `genesis-sacco-core` (all were absent here; none
required a fast-forward; none were diverged):

| Branch | SHA |
|---|---|
| `docs/adr-0005-0006-security-architecture` | `d8f50041` |
| `docs/institutional-gap-register` | `4cc8e947` |
| `docs/member-app-readiness` | `05becaae` |
| `duo/feature/adr0007-member-read-surface` | `50ccb6f0` |
| `duo/feature/db-backup-restore` | `6864338d` |
| `duo/fix/auth-rate-limit-hardening` | `96da72aa` |
| `duo/fix/security-hardening-review` | `15aa5aa5` |

Not pushed, with reasons:

- `duo/fix/develop-baseline-ci` (`72f8f890`) — already present here at the identical
  SHA when this pass ran (pushed by a concurrent migration agent).
- `develop` (`a2f6a9ac`) and `main` (`73c34cf4`) — identical in both projects; nothing to push.
- All 18 `feature/*` branches (`feature/auth` … `feature/users-admin`) — already
  present here at identical SHAs (both projects mirror the same GitHub origin).
- No push failed on protection rules; no branch was diverged; no force push was
  needed or attempted.

## Merge request mapping (old → new)

All 8 open MRs were recreated with identical source/target branches, identical
titles (Draft: prefixes preserved verbatim, including the doubled `Draft: Draft:`
on old !6), and identical descriptions with a provenance line prepended and a
corrected merge-order note appended.

| Old MR | New MR | Source branch | Title (abridged) |
|---|---|---|---|
| !8 | **!1** | `duo/fix/develop-baseline-ci` | fix(ci): repair the develop baseline (created by a concurrent agent; not recreated by this pass) |
| !1 | **!2** | `duo/fix/security-hardening-review` | fix: neutralize DEV_OTP_DISPLAY exposure, cron overlap locks, develop CI gate |
| !2 | **!3** | `duo/fix/auth-rate-limit-hardening` | fix(auth): rate-limit hardening |
| !3 | **!4** | `docs/adr-0005-0006-security-architecture` | docs(adr): ADR-0005 + ADR-0006 |
| !4 | **!5** | `duo/feature/db-backup-restore` | feat(ops): database backup and restore-verification |
| !5 | **!6** | `docs/member-app-readiness` | docs: ADR-0007 + mobile app design brief |
| !6 | **!7** | `duo/feature/adr0007-member-read-surface` | feat(member): ADR-0007 member self-service read surface |
| !7 | **!8** | `docs/institutional-gap-register` | docs: institutional gap register |

**Corrected merge order (new iids):** !1 (baseline) first → !2 (hardening) →
!3 (rate limiting) → !5 (backups) → !7 (member read surface). Docs MRs !4, !6, !8
any time after !1.

The pipeline-analysis discussion note from old !4 was copied onto new !5,
attributed as a copy. A migration review comment with branch-state and
cross-MR-risk findings was left on every MR (!1–!8).

## Issues

Not migrated by design: sacco-var issues **#1–#11** already existed with valid
numbering and match the old project's issues #1–#11 title-for-title. The old
project still has its duplicate open set — closing those is part of the
archival follow-up (see below). New issues created during migration:
**#12** (disable pull mirror).

## Not migrated

- **Old MR discussion history** — only old !4's pipeline-analysis note was copied
  (onto new !5). System notes, approvals, labels, milestones, and assignees were
  not migrated on any MR.
- **Old project's issues #1–#11** — deliberate (duplicates of this project's #1–#11).
- **Tags** — none exist in either project; nothing to migrate.
- **CI/CD variables, webhooks, project settings, container/package registries,
  wiki, snippets** — not inspected and not migrated; if any exist in the old
  project they must be reviewed before archival.
- **Commit-message cross-references** — commit messages on migrated branches
  reference the *old* project's MR iids (e.g. `98780cc` cites "MR !2/!6", which are
  new !3/!7 here). History is immutable; the mapping table above is the decoder.
- **Stale pipeline links** — the copied pipeline note on !5 references a pipeline
  under a `riva-group1` namespace (an earlier home of this repo). Those links do
  not resolve from this project; all CI claims in migrated descriptions are
  historical testimony until pipelines run green here.

## Final delta pass

The old remote was re-fetched at the end of the migration and every branch
re-compared against sacco-var:

- **Result: zero deltas.** No branch in `genesis-sacco-core` gained commits during
  the migration window; no re-push was required. `develop` and `main` remained at
  `a2f6a9ac` / `73c34cf4` in both projects.

## Follow-ups

- #12 — disable GitHub pull mirroring (blocking true single-source-of-truth status).
- Archive `mobto-group/genesis-sacco-core` (and the GitHub origin) after this
  note's MR merges — tracked in a dedicated follow-up issue.
