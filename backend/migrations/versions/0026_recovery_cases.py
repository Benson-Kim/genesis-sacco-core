"""collections & recovery worklist tables

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-01

Additive revision backing the build plan (+ hardening addenda
A1-A7): the minimal recovery workflow behind the prototype's "Initiate
recovery" action and the arrears worklist. NO money moves in this
prompt — neither table carries an amount, balance or rate column.

Sequencing (v1.2 rules 12/14): this revision claims 0026 with
down_revision '0025' (the track's claim) and merges AFTER it —
declared in at branch time, the -after- precedent.

  * recovery_cases — one workflow row per recovery effort on a loan.
    - status machine (concurrency safety): open -> (closed_cured |
      closed_written_off), enforced by the single transition function
      in genesis.domain.recovery; the DB CHECK mirrors the state set.
    - ONE-OPEN-CASE INVARIANT AT THE DB (review addendum): the partial
      UNIQUE index uq_recovery_cases_one_open on (tenant_id, loan_id)
      WHERE status = 'open' is claimed atomically with
      INSERT... ON CONFLICT DO NOTHING checked by rowcount (v1.1 rule
      5) — a concurrent double-open lands exactly one row; a second
      case for the same loan becomes representable only after the
      first closes (re-entry into NPL after a cure gets a NEW case,
      history preserved).
    - DB backstop (open-on-performing unrepresentable):
      classification_at_open is CHECK-restricted to the NPL classes
      and days_past_due_at_open must EXCEED 90 — the exact NPL
      boundary of domain/lending.classify (substandard starts at
      dpd > 90). Both are copied from the loan row read under its
      FOR UPDATE lock at open time; they are forensic workflow
      snapshots, not balances.
    - NAMED CONTRACT — CLASSIFICATION-LADDER MIRROR: the
      CHECKs above hardcode ('substandard', 'doubtful', 'loss') and
      dpd > 90 in the DATABASE while domain/lending.classify owns the
      thresholds in CODE; the NPL_CLASSES<->classify pin test covers
      the code side only and can never see a stranded DB CHECK. The
      ladder is FIXED today, so the mirror holds by construction —
      but if classification bands ever become tenant-configurable
      (the direction), a SUCCESSOR migration MUST relax or
      regenerate these CHECKs in the same MR that changes the ladder.
      Changing the ladder without that successor migration is a
      rejected MR: it would make legitimately-classified loans
      un-openable (or performing loans openable) at the DB layer,
      silently diverging from the domain.
    - SLA timestamps (addendum A7): opened_at / first_assigned_at /
      closed_at are written server-side (DEFAULT now() / now() in the
      service SQL) — no caller-supplied timestamps anywhere (v1.1
      rule 1 applied to time). ck_recovery_cases_closed_at makes a
      closed-without-timestamp (or open-with-timestamp) row
      unrepresentable.
    - version column: assignment is an optimistic-locked mutation
      (409 on stale, concurrency safety).

  * recovery_case_notes — APPEND-ONLY case history (review addendum):
    notes are rows, never an UPDATE-in-place of a notes blob; the API
    ships no edit/delete route, so the collections trail stays
    forensic evidence like audit_log. Assignment changes are audited
    with before/after into audit_log (itself append-only).

  * Indexes shipped with the queries that need them (scalability):
    - idx_loans_dpd_worklist (tenant_id, days_past_due DESC, id DESC)
      serves the keyset worklist ORDER BY l.days_past_due DESC,
      l.id DESC (loan id is the stable tiebreak — valid because the
      one-open-case invariant makes open cases 1:1 with loans);
      EXPLAIN captured in tests/test_p1316_explain.py ->
      backend/perf/explain_p1316.txt, falsifiable by dropping this
      index.
    - uq_recovery_cases_one_open doubles as the worklist's per-loan
      open-case probe and the open claim (the 0019 claim-key-as-index
      precedent).
    - idx_recovery_cases_open_scan (tenant_id, id) WHERE status =
      'open' serves the arrears job's cure-close keyset scan.
    - every FK is indexed (loan_id, assignee_id, opened_by; case_id,
      author_id) per scalability/1.5.

  * RLS enabled AND FORCED with the 0001 tenant_isolation policy
    shape (ADR-0002); both tables join TENANT_TABLES and the leakage
    suite in this MR.

Downgrade drops both tables and the worklist index. The loud-refusal
rule (the 0017/0020 precedent) does NOT bind here, documented per the
 execution instruction: it protects money-bearing history
(ledger postings, write-off/correction figures, dividend snapshots).
recovery_cases and recovery_case_notes hold workflow state and staff
notes only — no amounts, no balances, no postings; by construction
("NO money moves in this prompt") they can never hold money data, so
dropping them reverses the feature without rewriting or losing any
financial record. The audit_log rows the feature wrote remain, exactly
like any reverted feature's trail.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE recovery_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed_cured', 'closed_written_off')),
    assignee_id uuid REFERENCES users(id) ON DELETE RESTRICT,
    opened_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- FM1 backstop: an open-on-performing case is unrepresentable.
    -- NPL classes and the dpd > 90 boundary mirror
    -- domain/lending.classify (substandard/NPL starts above 90 days);
    -- tests/test_recovery_domain.py pins the constants together.
    classification_at_open text NOT NULL
        CHECK (classification_at_open IN ('substandard', 'doubtful', 'loss')),
    days_past_due_at_open integer NOT NULL
        CHECK (days_past_due_at_open > 90),
    -- SLA timestamps (addendum A7): server-side only, never
    -- caller-supplied (v1.1 rule 1 applied to time).
    opened_at timestamptz NOT NULL DEFAULT now(),
    first_assigned_at timestamptz,
    closed_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- A closed case always records when; an open case never does.
    CONSTRAINT ck_recovery_cases_closed_at
        CHECK ((status = 'open') = (closed_at IS NULL))
);

-- The one-open-case invariant (addendum A3): claimed atomically with
-- INSERT ... ON CONFLICT DO NOTHING + rowcount (v1.1 rule 5). Also the
-- worklist's per-loan open-case join probe (gate 1.3).
CREATE UNIQUE INDEX uq_recovery_cases_one_open
    ON recovery_cases (tenant_id, loan_id) WHERE status = 'open';

-- Cure-close keyset scan of the arrears job (open cases in id order).
CREATE INDEX idx_recovery_cases_open_scan
    ON recovery_cases (tenant_id, id) WHERE status = 'open';

-- FK indexes (gate 1.3/1.5). loan_id needs one beyond the partial
-- unique: closed cases (the preserved history) are outside it.
CREATE INDEX idx_recovery_cases_loan ON recovery_cases (tenant_id, loan_id);
CREATE INDEX idx_recovery_cases_assignee ON recovery_cases (tenant_id, assignee_id);
CREATE INDEX idx_recovery_cases_opened_by ON recovery_cases (tenant_id, opened_by);

ALTER TABLE recovery_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE recovery_cases FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recovery_cases
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TABLE recovery_case_notes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    case_id uuid NOT NULL REFERENCES recovery_cases(id) ON DELETE RESTRICT,
    author_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    note text NOT NULL CHECK (length(note) BETWEEN 1 AND 2000),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Serves the per-case notes keyset (created_at, id); leading FK index.
CREATE INDEX idx_recovery_notes_case
    ON recovery_case_notes (tenant_id, case_id, created_at, id);
CREATE INDEX idx_recovery_notes_author
    ON recovery_case_notes (tenant_id, author_id);

ALTER TABLE recovery_case_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE recovery_case_notes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recovery_case_notes
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- Worklist ordering index (gate 1.3, shipped with the query):
-- ORDER BY days_past_due DESC, id DESC under the tenant predicate.
CREATE INDEX idx_loans_dpd_worklist
    ON loans (tenant_id, days_past_due DESC, id DESC);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_loans_dpd_worklist;
DROP TABLE IF EXISTS recovery_case_notes;
DROP TABLE IF EXISTS recovery_cases;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Documented in the module docstring: neither table can hold money
    # data ("NO money moves in this prompt"), so the loud-refusal rule
    # for money-bearing history does not bind; the drop is a clean
    # feature reversal.
    op.execute(_DOWN)
