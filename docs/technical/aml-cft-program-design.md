# AML/CFT program design (issue #10, gap register G4)

> **Status: DESIGN, not as-built.** Unlike the rest of
> `docs/technical/`, this document specifies a capability that does
> **not exist yet**. It is the build contract for the phases tracked
> on issue #10, fixed by [ADR-0009](../adr/0009-aml-cft-program.md).
> Every reference to an EXISTING module cites a real file path;
> everything else is design. When a phase ships, its section here is
> reconciled to as-built or superseded by the module docstrings.

## 1. Scope and posture

POCAMLA obligations for a deposit-taking SACCO: sanctions screening,
cash-threshold reporting, suspicious-transaction detection and FRC
STR filing, and periodic KYC refresh. The program is strictly
**detective** — rules open cases for humans; nothing auto-blocks.
The single blocking consequence in the whole design (restricting a
confirmed sanctions match) is itself a human four-eyes case
disposition, never an engine output.

Two constraints dominate every section:

- **Tipping-off (POCAMLA offence).** Nobody outside the compliance
  function may learn a member is under review — least of all the
  member. Enforced structurally (§6), not procedurally.
- **Reproducibility.** Every screening decision must be replayable
  against the exact list version, rule version and threshold set in
  force at decision time. Hence versioned lists (§2.1),
  effective-dated parameters (§3) and versioned rules (§4).

## 2. Sanctions screening

### 2.1 List sources — pinned and versioned

Day-one sources (freely available, no licensing):

| Source | Format | Cadence |
|---|---|---|
| UN Security Council Consolidated List | XML | on publication (checked daily) |
| OFAC SDN | XML/CSV | on publication (checked daily) |

Ingestion writes an immutable snapshot per publication:

- `sanctions_list_versions` — `(id, source, source_version_label,
  published_at, ingested_at, content_hash, entry_count)`. Append-only
  (DB trigger refuses UPDATE/DELETE — the 0018
  `member_profiles_consent_guard` pattern). `content_hash` pins the
  raw payload so an ingest is verifiable.
- `sanctions_list_entries` — `(id, list_version_id, external_ref,
  primary_name, name_normalized, entry_payload JSONB)` plus
  `sanctions_list_entry_aliases` — one row per alias/AKA with its own
  `name_normalized`. Entries belong to exactly one list version;
  a new publication is a NEW full set of rows, never an update.

Operational fail-loud rule: a source that has not produced a
successful ingest within its staleness window raises an operational
alert (outbox event + worker heartbeat, the issue-#4 observability
surface). A stale list must alarm, never silently keep passing
members.

Commercial feeds (World-Check, Dow Jones) and dedicated PEP lists are
**deferred**: the ingest is an adapter seam keyed by `source`, so
adding one is a new adapter + new rows, no engine change.

### 2.2 Matching — normalized names + aliases, scored

Exact matching is compliance theater. The match pipeline:

1. **Normalization** (pure function, `domain/`-layer): Unicode NFKD
   fold, diacritic strip, case fold, punctuation strip, whitespace
   collapse, token-set ordering (so "Ali, Hassan Mohamed" ≡ "hassan
   mohamed ali"). Applied identically to member names (from the
   members row and the KYC profile, `application/member_kyc.py`) and
   to list entries + aliases at ingest (`name_normalized`).
2. **Candidate generation**: token-overlap prefilter against
   `name_normalized` (indexed), bounding the expensive step.
3. **Scoring**: token-set similarity (Jaro-Winkler or trigram —
   picked in Phase A behind a pure seam with golden-pinned oracles)
   across the primary name AND every alias; the entry score is the
   max. Secondary attributes on the list entry (DOB, nationality,
   ID numbers) adjust the score when the KYC profile carries them.
4. **Thresholding**: score ≥ review threshold ⇒ HIT (opens a case);
   below ⇒ CLEAR. The threshold is effective-dated configuration
   (§3), never hardcoded — tuning it is a recorded parameter change.

No auto-disposition above any score: even a 1.0 score is a HIT for a
human, because homonyms exist.

### 2.3 Screening runs and append-only results on the KYC record

- `screening_runs` — `(id, tenant_id, member_id, trigger
  ∈ {onboarding, list_update, periodic, manual}, list_version_ids,
  ran_at, outcome ∈ {clear, hit}, score_summary)`. **Append-only**
  (trigger-enforced). The run row pins the list versions consulted —
  the reproducibility anchor. This is the screening result "on the
  KYC record": rows are keyed by member and surfaced (compliance-only,
  §6) alongside the `member_profiles` row; the profile JSONB itself
  is not rewritten (its shape is DB-CHECKed by 0018 and consent is
  trigger-guarded — screening history does not belong inside it).
- `screening_hits` — one row per (run, list entry) above threshold,
  carrying the score and the matched alias; each hit references the
  AML case (§5) it opened.

Triggers for a run:

| Trigger | Population |
|---|---|
| Onboarding | the new member, synchronously with member creation (result recorded; onboarding is NOT blocked — a hit opens a case) |
| List update | delta re-screen of the full member base against the new version (batched via `application/batch_runner.run_in_batches`, the dormancy-worker shape) |
| Periodic | by risk rating: high monthly, medium quarterly, low annually (effective-dated cadence config, §3) |
| Manual | compliance-initiated, e.g. during an investigation |

### 2.4 Hit adjudication — four-eyes via `application/sod.py`

Every hit opens a case in the **screening queue** (a case type in the
shared case model, §5). Disposition is two-step:

1. An adjudicator proposes `false_positive` or `true_match` with a
   written rationale.
2. A DIFFERENT principal confirms —
   `application/sod.py::require_distinct_non_assurance_checker`
   verbatim (distinct, tenant-vouched, non-assurance), backstopped by
   `ck_aml_cases_sod CHECK (approver_id IS NULL OR approver_id <>
   investigator_id)` plus a write-once trigger (the 0031/0040
   pattern).

A confirmed `true_match` produces a mandated follow-up task list
(restrict operations via the §7 gate, file STR, notify FRC) — each an
explicit human action, audited.

## 3. Threshold monitoring — effective-dated configuration (issue #3 pattern)

Cash-equivalent reporting thresholds are regulatory parameters. They
use the **issue-#3 regulatory-parameter table pattern**. Issue #3 has
no branch/ADR yet; the pattern is already instantiated once by
ADR-0008's `approval_band_sets` (!20): tenant-scoped, **append-only**
(triggers refuse UPDATE/DELETE), one immutable parameter set per
`effective_from` date, "set in force at date D = newest
`effective_from` ≤ D", code-owned day-one defaults, validation at
write AND read (corrupt stored parameters fail closed — the
`parse_dormancy_period` refuse-loudly shape,
`application/dormancy.py`). AML does **not** fork this: Phase B
builds `aml_parameter_sets` to the identical discipline, and when the
issue-#3 general table lands, AML parameters migrate onto it (an
explicit convergence work item, not a TODO comment).

Parameters carried (day-one defaults in parentheses):

| Parameter | Default |
|---|---|
| `ctr_threshold` | POCAMLA cash-transaction figure — USD 15,000 equivalent, expressed in KES per tenant |
| `structuring_window_days`, `structuring_aggregate_pct` | 3 days, 90% of threshold |
| `screening_score_threshold` | tuned in Phase A, golden-pinned |
| `rescreen_cadence_{high,medium,low}` | 1m / 3m / 12m |
| `kyc_refresh_{high,medium,low}` | 12m / 24m / 36m |

The monitor scans the transactions ledger (member-initiated cash
types — reusing `domain/ledger.MEMBER_INITIATED`, never a parallel
type list) per posting date: single transactions ≥ threshold, and
same-member same-day/window aggregations ≥ threshold, write
`threshold_reports` rows (append-only) and open a case when a
structuring rule (§4) also fires. Reporting selects the parameter set
in force **at the transaction date**, not today's — the issue-#3
reporting-date rule.

## 4. Detection rules — declarative, versioned, shared with G5

### 4.1 Rule-definition format

Rules are data; rule SEMANTICS are code. A rule row
(`aml_rules`, append-only versions):

```json
{
  "rule_key": "structuring_subthreshold_v1",
  "rule_type": "windowed_aggregate",
  "version": 3,
  "effective_from": "2026-09-01",
  "enabled": true,
  "severity": "high",
  "case_queue": "aml",
  "params": {
    "window_days": 3,
    "min_events": 3,
    "aggregate_gte_param": "ctr_threshold",
    "each_below_param": "ctr_threshold"
  }
}
```

- `rule_type` is a **code-owned vocabulary** (the
  `domain/members.MoneyOperation` / `MEMBER_INITIATED` allow-list
  discipline): each type is a pure evaluator in `domain/` taking
  `(signal window, params)` → verdict. Unknown types are refused at
  write time — never free-form caller logic, never eval.
  Day-one types: `windowed_aggregate` (structuring/smurfing),
  `velocity` (count/sum per window vs. member baseline),
  `state_change_then_activity` (dormant-account sudden activity),
  `threshold_single` (single-event trigger).
- `params` may reference §3 parameters by name
  (`"aggregate_gte_param": "ctr_threshold"`) so a threshold change
  does not require a rule change.
- `case_queue` routes the output: `aml` (this program) or `fraud`
  (G5). **The engine is queue-agnostic — this single field is what
  makes the same engine serve G5 fraud ops.** G5 adds rule types and
  a `fraud` queue with its own RBAC module; it does not add an engine.
- Rules are versioned append-only; a case records the exact
  `(rule_key, version)` that opened it — reproducibility.

### 4.2 Signals consumed

| Signal | Source (existing) |
|---|---|
| Transaction postings | transactions ledger; member-initiated = `domain/ledger.MEMBER_INITIATED` (the dormancy worker's definition — reused, not re-derived) |
| Dormancy transitions | `member.status_changed` outbox events emitted by `application/dormancy.py` (reason `dormancy`) and reactivation events from `application/members.reactivate_dormant_member` |
| Security events | the issue-#1 stream (!11 `application/security_events.py` anomaly events + the structured auth/OTP/override events) — **hard dependency: the engine's event-consumption phase is gated on #1 landing** |

Dormant-account sudden activity (`state_change_then_activity`) is
exactly: reactivation event followed by outbound velocity above
baseline within N days — both legs from existing signals.

### 4.3 Output — cases, never blocks

A firing rule inserts a case (§5) and an outbox event
(`aml.case_opened`) inside the evaluation transaction. It never
touches the triggering transaction, the member row, or any
member-visible state. Rule-evaluation failures on a posting path are
logged-and-swallowed telemetry (the gate-1.2 posture from !11's
`emit_anomaly_signals`): analytics must never break the action.

## 5. Case management and the STR workflow

One case model, multiple queues:

- `aml_cases` — `(id, tenant_id, member_id, case_type ∈ {screening_hit,
  rule_alert, threshold_report, manual}, queue, opened_by (rule or
  user), status ∈ {open, investigating, pending_approval, closed},
  investigator_id, approver_id, disposition ∈ {false_positive,
  dismissed, str_filed, true_match_confirmed}, version, timestamps)`.
  Status moves through a pure transition function (the
  `domain/members.transition` gatekeeper pattern) under optimistic
  locking; identity/disposition columns are write-once
  (trigger-pinned, the 0031 shape); `ck_aml_cases_sod` refuses
  investigator = approver at the DB.
- `aml_case_events` — append-only investigation log: evidence
  attached, notes, status moves. Every event ALSO writes
  `record_audit` in the same transaction (MASTER_PROMPT 1.5: the
  trail can never disagree with the data). The case file — evidence,
  decision, timestamps, pinned list/rule versions — is the audit
  artifact.

STR decision flow:

```
rule/hit/manual → open → investigating (investigator claims)
  → pending_approval (investigator proposes: file STR | dismiss)
  → closed (a DIFFERENT principal approves — sod.py + DB CHECK)
```

Filing produces an `str_filings` row (append-only: FRC reference,
reporting period, artifact export id) and the **FRC-format export**:
a new `ReportName.STR_FILING` entry in the `REPORTS` registry
(`application/reports.py`) rendered through
`application/exports.py::run_export` / `run_export_job` **only** —
the house doctrine 1.3 single exfiltration channel, with ADR-0004
artifact discipline (unguessable tokens, expiring links,
requester-only download, in-transaction download audit). The export
is additionally gated on `aml:view` (§6) — the PII-column pattern
(blocker e) already freezes column entitlement into the job row.
Direct goAML web-service submission is **deferred**; the compliance
officer files the artifact manually and records the FRC reference.

## 6. Tipping-off wall — RBAC additions and member-surface invisibility

### 6.1 New RBAC module and role

A new `Module.AML` in `domain/rbac.py` with **deliberately narrow
grants** (the `_CORRECTIONS_GRANTS` precedent — roles absent hold
NOTHING, deny by default), and a new seeded role `COMPLIANCE_OFFICER`
appended to `ROLE_NAMES` (which, per the W57-5 drift tripwire, moves
the approval-bands vocabulary and the web authority-picker mirror in
the same change):

| Role | aml:view | aml:create | aml:edit | aml:approve |
|---|---|---|---|---|
| System Admin | ✓ | ✓ | ✓ | ✓ |
| Compliance Officer | ✓ | ✓ | ✓ | ✓ |
| Auditor | ✓ | — | — | — |
| every other role (Teller, Loan Officer, Branch Manager, Credit Committee, Accountant, Senior Credit Officer) | — | — | — | — |

- `aml:create` = open manual cases / trigger manual screening;
  `aml:edit` = investigate; `aml:approve` = the four-eyes confirmer
  power. Within the module, investigator ≠ approver per case is
  enforced by `sod.py` + the DB CHECK — two compliance officers are
  required to close any case.
- The Auditor reviews the trail (view-only) and is excluded from
  acting by `ASSURANCE_ROLES` server-side — consistent with the B2
  principle everywhere else.
- **No operational role holds any AML grant.** A teller serving a
  member under review sees exactly what they see for any member.
- System Admin retains the root convention (matrix-shape invariant).
  Recorded risk, not hidden: admin visibility is the existing
  platform-wide posture; tightening it is a platform decision out of
  scope here.
- Audit-log disclosure: `aml.*` audit rows join the ENTITY_MODULES
  redaction map keyed to `aml:view`, so the audit viewer cannot
  become a side channel around the wall.

### 6.2 Member-surface invisibility (absolute)

- No member-facing route, response field, push/SMS template or error
  message may derive from AML case state. The ADR-0007 member read
  surface adds NOTHING for AML.
- Screening and case processing must not alter member-visible timing
  or status: screening at onboarding records results asynchronously
  of the member-visible outcome; a HIT does not delay or annotate
  anything the member can observe.
- The ONLY member-visible consequence in the program is the
  KYC-expiry restriction (§7), which is the member's own compliance
  obligation and is safe to disclose. Its wording is fixed ("KYC
  update required") and never references screening, review or cases —
  including for members whose restriction was actually placed by a
  confirmed sanctions match (that restriction rides the same
  mechanism and the same message; distinguishing them would leak).

## 7. KYC risk rating, refresh cycles, and `member_may` restrictions

- **Risk rating** — `member_profiles` gains `risk_rating ∈ {low,
  medium, high}` (backfilled `medium`; DB CHECK). Assignment is
  rule-assisted (member type, product mix, §4 signals) but
  compliance-confirmed; changes are audited with rationale.
- **Refresh cadence** by rating from §3 parameters (defaults
  high 12m / medium 24m / low 36m). A nightly sweep (batch-runner
  shape, per tenant, fail-closed on missing config exactly like
  `parse_dormancy_period` — unset cadence refuses the run loudly,
  never a silent default) computes KYC standing:
  `current → due (window open, member nudged) → expired`.
- **Expired KYC restricts operations through the existing capability
  gate.** `domain/members.member_may` is the single code-owned
  status × operation allow-list; the KYC gate is a second pure
  function `kyc_allows(kyc_standing, operation)` ANDed at the same
  call sites (transactions, guarantees, loan_applications,
  member_exits, dividends — the existing `member_may` consumers), so
  there is still exactly one place each axis is decided:
  - money IN stays allowed (`DEPOSIT`, `LOAN_REPAYMENT`, `FEE`,
    `RECOVERY` — reducing risk is always allowed, the `_MONEY_IN`
    rationale);
  - money OUT and origination are refused while expired
    (`WITHDRAWAL`, `BORROW`, `PLEDGE`, `SHARE_TOPUP`,
    `SHARE_TRANSFER_*`);
  - `EXIT_REQUEST` stays allowed (exit runs the full workflow with
    its own settlement controls; holding a member hostage to a form
    is not a control).
  Restriction, never deletion; refusal message: "KYC update
  required" (§6.2).

## 8. Phased build plan

Dependency DAG:

```
#1 security-event stream (!11 + successors) ──► Phase C (rule engine event feed)
#3 regulatory-parameter table ──► Phase B convergence (AML params onto the general table)
ADR-0008 approval_band_sets (!20) ──► pattern precedent for Phase B (no code dependency)
Phase A ──► Phase D (cases exist before STR filing)  ──► G5 fraud queue (engine reuse)
```

| Phase | Delivers | Depends on | Gated? |
|---|---|---|---|
| **0 (this MR)** | ADR-0009 + this design | — | docs-only |
| **A — screening foundation** | list ingest + versioned snapshots, normalization/matching (golden-pinned), `screening_runs`/`screening_hits` append-only, screening case queue + four-eyes disposition, `Module.AML` + Compliance Officer role, onboarding + list-update triggers | none hard; ships dark behind the RBAC module | no |
| **B — thresholds & parameters** | `aml_parameter_sets` (issue-#3 discipline), CTR monitor + `threshold_reports`, periodic re-screen cadence by risk rating | #3 for CONVERGENCE only (pattern is buildable now; migrating onto the general table is an explicit sub-item once #3 lands) | soft |
| **C — detection rules engine** | `aml_rules` format + code-owned evaluators, ledger + dormancy-signal feeds, case routing (`aml` queue), velocity baselines | **#1 event stream (hard)** for the security-event feed; ledger/dormancy feeds have no gate | yes |
| **D — STR workflow & FRC export** | investigation workflow, SoD-checked file/dismiss, `str_filings`, `ReportName.STR_FILING` via `run_export`, audit-viewer redaction wiring | Phase A (case model), export registry (exists) | no |
| **E — KYC risk & refresh** | `risk_rating` column, refresh sweep, `kyc_allows` gate at `member_may` call sites, member nudges via outbox | Phase B parameters (cadences) | soft |
| **G5 handoff** | `fraud` case queue + fraud rule types on the Phase-C engine | Phase C | separate program |

Deliberately **deferred** (recorded, not hidden): commercial/PEP list
feeds (adapter seam ready); automated goAML submission (manual
artifact filing first); ML/behavioural scoring (rules first — a
scored model with no case-queue discipline is noise); real-time
pre-transaction interdiction (detective-only by design, see ADR-0009
alternatives); admin-visibility tightening for the AML module
(platform-wide posture question); audit-log retention/partitioning
(already a recorded follow-up from the K4 review note in
`application/member_kyc.py`).

## 9. Open questions for the build phases

1. Similarity algorithm choice (Jaro-Winkler vs. pg_trgm) — decided
   in Phase A behind the pure seam with golden oracles; pg_trgm
   pulls an extension dependency (the pgcrypto-optional precedent
   applies).
2. Screening-threshold tuning data — Phase A ships with a
   conservative default and a compliance-visible false-positive rate
   metric before any loosening.
3. Whether `COMPLIANCE_OFFICER` needs a seeded senior tier
   (SENIOR_TIERS) for approval routing in small SACCOs where exactly
   two compliance principals may not exist — Phase A decision with
   the tenant-onboarding stakeholders; the SoD guard itself is
   non-negotiable.
