# ADR-0009: AML/CFT program — sanctions screening, threshold monitoring, suspicious-activity cases, STR workflow

- Status: Proposed
- Date: 2026-08-19
- Deciders: Genesis Prestige engineering (issue #10, gap register G4)

## Context

AML/CFT does not exist in this system in any form
(`docs/INSTITUTIONAL_GAP_REGISTER.md` G4). POCAMLA obligations apply
to SACCOs: the FRC expects suspicious-transaction reports (STRs),
sanctions screening, cash-threshold reporting and periodic KYC
refresh. This is the license to operate, not gold-plating, and it
takes months to build — so the program is designed now, before a
regulator asks.

This ADR is **docs-only**: it fixes the architecture; every build
phase is a separate work item, and all of them are gated on the
issue-#1 security-event stream (in flight via !11). The companion
document `docs/technical/aml-cft-program-design.md` carries the full
schema sketches, the rule-definition format and the phase plan.

Constraints that shaped this decision:

- **Reuse-first (MASTER_PROMPT §1.1).** The SoD checker guard exists
  once (`application/sod.py::require_distinct_non_assurance_checker`)
  and must not fork. Report data leaves the system only through the
  export engine (`application/exports.py::run_export`, ADR-0004).
  Audit rows go through `application/audit.record_audit`; side
  effects through the transactional outbox
  (`application/outbox.enqueue_event`); batch scans through
  `application/batch_runner.run_in_batches` (the dormancy-worker
  shape).
- **Effective-dated regulatory configuration (issue #3 direction).**
  Issue #3 has no branch or ADR yet at the time of writing; the
  pattern it will build is already instantiated once by ADR-0008's
  `approval_band_sets` (!20): tenant-scoped, append-only,
  one immutable parameter set per `effective_from` date, code-owned
  day-one defaults, fail-closed validation at write AND read. AML
  thresholds REUSE that pattern — when the issue-#3 table lands, AML
  parameters converge onto it rather than keeping a private fork.
- **Tipping-off is a POCAMLA offence.** Disclosing that a member is
  under AML review — to staff outside the compliance function or,
  worst of all, to the member — is itself a crime. Case visibility is
  therefore a first-class design constraint, not an RBAC afterthought.
- **Detection must never auto-block.** False positives against real
  members' money are a harm; auto-blocking also leaks the existence
  of a review (tipping-off). Rules produce CASES for humans.
- **The G5 fraud-ops program shares the rules-engine investment.**
  The rule-definition format is designed once, for both consumers.

## Decision

We build the AML/CFT program as an application-layer capability set
behind its own permission wall, in five capabilities:

1. **Sanctions screening is fuzzy-match, human-adjudicated,
   reproducible.** Exact-name matching is compliance theater
   (transliteration, aliases, name-order variance). Screening runs a
   normalized-name comparison (case/diacritic/whitespace folding,
   token-set ordering) plus alias expansion from the list data, with
   a similarity score against a tuned threshold. List sources are
   pinned and VERSIONED — UN Security Council Consolidated List and
   OFAC SDN as the freely available baselines — ingested as immutable
   `sanctions_list_versions` snapshots, so every screening decision
   is reproducible against the exact list version in force that day.
   Every hit opens a case in the screening queue; disposition (false
   positive / true match) is four-eyes via
   `application/sod.py` — the adjudicator and the confirmer are
   distinct, tenant-vouched, non-assurance principals. Screening
   results are recorded **append-only on the KYC record** (the 0018
   `member_profiles` consent-guard trigger is the precedent: a DB
   trigger refuses UPDATE/DELETE). Members are screened at
   onboarding, on every new list version (delta re-screen), and
   periodically by risk rating.

2. **Cash-equivalent threshold monitoring is effective-dated
   configuration.** Reporting thresholds (day-one default: the
   POCAMLA cash-transaction figure, USD 15,000 equivalent, expressed
   in KES per tenant) live in the issue-#3 regulatory-parameter
   pattern — append-only, `effective_from`-selected, code-owned
   defaults, never hardcoded in the monitor. Threshold breaches and
   same-day aggregation breaches emit report entries and cases; they
   never block the transaction.

3. **Suspicious-activity detection rules feed a case queue — never
   auto-blocks.** Rules cover structuring/smurfing (sub-threshold
   splitting, same-day aggregation), velocity anomalies, and
   dormant-account sudden activity — the last one REUSES the dormancy
   worker's signals: the `member.status_changed` outbox events and
   the ledger-derived `MEMBER_INITIATED` activity definition
   (`application/dormancy.py`, `domain/ledger.py`), never a parallel
   activity notion. Rules are declarative: a code-owned
   `rule_type` vocabulary with data-carried, versioned parameters
   (the full format is in the companion doc), so the SAME engine
   serves G5 fraud ops with a different rule set and a different case
   queue. The engine consumes the issue-#1 security-event stream and
   the transaction ledger; it is strictly detective.

4. **STR workflow: investigate → file/dismiss with SoD, audited,
   exported through `run_export` only.** A case is investigated by a
   compliance user; the file/dismiss decision requires a DIFFERENT
   principal (investigator ≠ approver) via
   `require_distinct_non_assurance_checker`, backstopped by a DB
   CHECK (the 0031/0040 `ck_*_sod` pattern) and a write-once trigger.
   Every case action writes an in-transaction audit row
   (`record_audit`) — the full case file (evidence, decision,
   timestamps, list version, rule version) IS the audit artifact.
   FRC-format STR export is a new report in the `REPORTS` registry
   rendered through the existing `run_export` path (ADR-0004 artifact
   discipline: unguessable tokens, expiring links, requester-only,
   audited downloads) — no second export channel. Direct goAML
   web-service submission is deferred; the artifact is filed manually.

5. **Tipping-off protection: a dedicated `aml` RBAC module and zero
   member-surface visibility.** A new `Module.AML` with deliberately
   NARROW grants (the `_CORRECTIONS_GRANTS` precedent: roles absent
   hold NOTHING) and a new seeded `Compliance Officer` role. Tellers,
   loan officers, branch managers and the credit committee hold no
   AML grant of any kind; the Auditor holds view-only and is excluded
   from acting by `ASSURANCE_ROLES`; the System Admin keeps the
   root convention. AML case data NEVER appears on the member
   surface: no member-facing endpoint, event, status or error message
   may derive from AML case state. KYC-expiry restrictions (which ARE
   communicable — they are the member's own obligation) are the only
   member-visible consequence, and their wording never references
   screening or cases.

6. **KYC refresh cycles by risk rating; expiry restricts via
   `member_may`.** A risk rating (low/medium/high) is added to the
   KYC profile; refresh cadence is rating-driven (high 1y, medium 2y,
   low 3y — effective-dated configuration like the thresholds).
   Expired KYC maps to **operation-level restrictions through the
   existing code-owned capability map**
   (`domain/members.member_may`): a pure KYC-standing gate is ANDed
   with the status gate at the same call sites — money IN stays
   allowed (deposits, repayments reduce risk), money OUT and credit
   origination are refused until refresh. Restriction, never
   deletion.

## Alternatives considered

- **Exact-name screening** — rejected: misses transliterations and
  aliases; it is the form of compliance without the substance.
- **Commercial screening SaaS (World-Check, Dow Jones)** — rejected
  for day one: cost and data-residency questions (member PII leaving
  the system contradicts the least-disclosure doctrine). The list
  ingestion is an adapter seam, so a commercial feed can be added as
  another versioned source later without touching the engine.
- **Auto-blocking on rule hits** — rejected: false positives harm
  members, and a visible block leaks the review (tipping-off). Only
  a confirmed-true sanctions match escalates to an account
  restriction, and that action is itself a four-eyes case
  disposition, not an engine output.
- **Reusing `audit_log` as the case store** — rejected: cases are a
  WORKFLOW (state, assignment, disposition); the audit log is the
  immutable trail OF that workflow. Conflating them makes the trail
  mutable by construction.
- **Folding AML visibility into existing modules** (e.g. gating the
  queue behind `transactions:approve`) — rejected: every existing
  grant is held by operational staff, which is exactly who must NOT
  see AML cases. The tipping-off wall requires a module no
  operational role holds.
- **A parallel activity/dormancy signal for rule 3** — rejected:
  the dormancy worker already defines member-initiated activity
  ledger-derived (`MEMBER_INITIATED`); a second definition would
  drift.

## Consequences

Positive: POCAMLA obligations get a designed, phased path; screening
decisions are reproducible (pinned list versions); the rules engine
is a shared investment with G5; the tipping-off wall is structural
(deny-by-default module), not procedural; every workflow reuses the
existing SoD/audit/outbox/export machinery, so no new trust
primitives are invented.

Negative: fuzzy matching produces false positives that consume human
adjudication time (tunable threshold, but the queue is real work); a
new seeded role widens `ROLE_NAMES` and the approval-band vocabulary
(the W57-5 drift tripwire and the web authority-picker mirror must
move in the same change); list ingestion adds an external-data
operational dependency (stale lists must alarm, not silently pass);
append-only screening results grow with list-update frequency.

Migration/rollback: every phase is additive (new tables, new module
grants, new report registry entry) — no existing table changes shape
except the KYC profile gaining a risk-rating column with a backfilled
default. Rolling back a phase drops its tables and grants; the
member-facing behaviour change (KYC-expiry restrictions) ships dark
behind the refresh-cycle configuration being unset (fail-closed
refusal to run, the dormancy `parse_dormancy_period` precedent —
no cadence configured means no expiry sweep, never a silent default).

Build phases and their dependency DAG (#1 event stream, #3 parameter
tables, G5 shared engine) are specified in
`docs/technical/aml-cft-program-design.md` and tracked as follow-up
work items on issue #10.
