"""richer recovery-case dispositions + post-closure outcome notes

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-02

Successor revision to 0026 backing the maintainer-
follow-ups. NO money moves: no amount, balance or rate
column is added — recovery_cases stays workflow state only.

Sequencing (v1.2 rules 12/14): this revision claims 0033 with
down_revision '0032' (that track's claim) and merges AFTER it
— declared in at branch time; batch order -> ->.

THE 0026 NAMED-CONTRACT DISCIPLINE, HONOURED: 0026's docstring
records the CLASSIFICATION-LADDER MIRROR contract — a DB CHECK that
mirrors a code-owned set may only change via a successor migration in
the SAME MR that changes the set. This MR widens the recovery status
set in genesis.domain.recovery (the single transition gatekeeper), so
this revision regenerates every mirrored shape in the same MR:

  * status CHECK — widened to the six-state machine: the three 0026
    states plus 'irrecoverable_pending_write_off' (recovery concluded impossible, committee
    write-off not yet landed — the / linkage), 'disputed' (member contests the arrears; the case
    pauses
    without pretending to be workable), 'closed_restructured'
    (terminal: the loan was restructured, the case's premise no longer
    holds). Postgres stores the inline 0026 CHECK under the
    auto-generated name recovery_cases_status_check; it is dropped and
    recreated under that same name.
  * ck_recovery_cases_closed_at — regenerated from "closed_at set
    <=> not open" to "closed_at set <=> TERMINAL": the two new live
    (non-terminal) postures carry no closed_at, exactly like open.
  * uq_recovery_cases_one_open — the one-OPEN-case invariant becomes
    one-LIVE-case: WHERE status IN ('open',
    'irrecoverable_pending_write_off', 'disputed'). A paused case
    still blocks a second case for the same loan (the A3 invariant
    extended); the service's ON CONFLICT claim predicate follows.
    Regenerated under the SAME name so the worklist EXPLAIN pin
    (tests/test_p1316_explain.py) keeps asserting it: the worklist's
    status = 'open' join predicate implies the wider index predicate,
    so the probe stays served.
  * idx_recovery_cases_open_scan — the arrears close pass now scans
    ALL live statuses (loan facts end a pause: cure or write-off
    closes a paused case too); same name, live-set predicate.

POST-CLOSURE OUTCOME NOTES (issue option 1): exactly ONE outcome
note at/after closure, server-enforced —

  * recovery_case_notes.is_outcome boolean NOT NULL DEFAULT false
    (expand-only; every existing note is a regular live-case note).
  * uq_recovery_notes_one_outcome partial UNIQUE (tenant_id, case_id)
    WHERE is_outcome — claimed atomically with INSERT... ON CONFLICT
    DO NOTHING + rowcount: a concurrent double-add lands
    exactly one row. Notes stay append-only (review addendum): no edit or
    delete route exists anywhere; the outcome note is a NEW row.

Downgrade REFUSES LOUDLY (the 0017/0020 discipline applied to
audit-relevant workflow history — the same posture as 0031): if any
case row holds one of the three new statuses, or any outcome note
exists, the disposition/outcome trail would be silently rewritten by
restoring the 0026 shapes, so the guard raises instead. On a clean DB
it restores the exact 0026 CHECK/constraint/index definitions and
drops the outcome-note column and index.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE recovery_cases
    DROP CONSTRAINT recovery_cases_status_check;
ALTER TABLE recovery_cases
    ADD CONSTRAINT recovery_cases_status_check
    CHECK (status IN ('open', 'irrecoverable_pending_write_off', 'disputed',
                      'closed_cured', 'closed_written_off', 'closed_restructured'));

-- closed_at set <=> terminal (live postures carry none, like open).
ALTER TABLE recovery_cases
    DROP CONSTRAINT ck_recovery_cases_closed_at;
ALTER TABLE recovery_cases
    ADD CONSTRAINT ck_recovery_cases_closed_at
    CHECK ((status IN ('open', 'irrecoverable_pending_write_off', 'disputed'))
           = (closed_at IS NULL));

-- One-LIVE-case invariant (A3 extended); same names, wider predicate.
DROP INDEX uq_recovery_cases_one_open;
CREATE UNIQUE INDEX uq_recovery_cases_one_open
    ON recovery_cases (tenant_id, loan_id)
    WHERE status IN ('open', 'irrecoverable_pending_write_off', 'disputed');

DROP INDEX idx_recovery_cases_open_scan;
CREATE INDEX idx_recovery_cases_open_scan
    ON recovery_cases (tenant_id, id)
    WHERE status IN ('open', 'irrecoverable_pending_write_off', 'disputed');

-- N3: exactly one outcome note per case, at/after closure only (the
-- terminal gate lives in the service under the case row lock; the
-- partial UNIQUE is the atomic claim it checks by rowcount).
ALTER TABLE recovery_case_notes
    ADD COLUMN is_outcome boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX uq_recovery_notes_one_outcome
    ON recovery_case_notes (tenant_id, case_id) WHERE is_outcome;
"""

_DOWN_GUARD = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM recovery_cases
        WHERE status IN ('irrecoverable_pending_write_off', 'disputed',
                         'closed_restructured')
    ) OR EXISTS (
        SELECT 1 FROM recovery_case_notes WHERE is_outcome
    ) THEN
        RAISE EXCEPTION
            'refusing downgrade: recovery_cases holds disposition history '
            '(irrecoverable_pending_write_off/disputed/closed_restructured '
            'rows) or recovery_case_notes holds outcome notes (migration '
            '0033) — audit-relevant workflow state is never silently '
            'rewritten';
    END IF;
END
$$;
"""

_DOWN = (
    _DOWN_GUARD
    + """
DROP INDEX uq_recovery_notes_one_outcome;
ALTER TABLE recovery_case_notes DROP COLUMN is_outcome;

DROP INDEX idx_recovery_cases_open_scan;
CREATE INDEX idx_recovery_cases_open_scan
    ON recovery_cases (tenant_id, id) WHERE status = 'open';

DROP INDEX uq_recovery_cases_one_open;
CREATE UNIQUE INDEX uq_recovery_cases_one_open
    ON recovery_cases (tenant_id, loan_id) WHERE status = 'open';

ALTER TABLE recovery_cases
    DROP CONSTRAINT ck_recovery_cases_closed_at;
ALTER TABLE recovery_cases
    ADD CONSTRAINT ck_recovery_cases_closed_at
    CHECK ((status = 'open') = (closed_at IS NULL));

ALTER TABLE recovery_cases
    DROP CONSTRAINT recovery_cases_status_check;
ALTER TABLE recovery_cases
    ADD CONSTRAINT recovery_cases_status_check
    CHECK (status IN ('open', 'closed_cured', 'closed_written_off'));
"""
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
