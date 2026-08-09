"""bad-debt recovery receipts for written-off loans

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-02

Expand-only revision backing (the follow-up: WRITE-OFF IS NOT FORGIVENESS). The 0025 write-once
``loan_write_offs``
snapshot persists the legal claim on the member after the WO- posting
derecognises the receivable; this revision makes cash received against
that surviving claim representable:

  * transactions.type CHECK expanded (expand-only, the 0020/0025
    precedent) with 'loan_recovery' — the RC- bad-debt recovery
    receipt: DR cash / CR income.bad_debt_recoveries (the accounting treatment named on: recovery
    INCOME, never a reversal of the write-off posting and never a resurrection of the receivable).

  * loan_recoveries — one APPEND-ONLY row per recovery receipt, bound
    to the surviving snapshot claim (write_off_id) so partial
    recoveries are tracked against loan_write_offs.total_written_off.
    - APPEND-ONLY (the 0004/0014 ledger-trigger discipline): recovery
      receipts are money history; UPDATE and DELETE raise via the
      0001 forbid_row_mutation() function, even through manual SQL on
      the app role. The recovered total is therefore always
      reconstructed by summing the append-only rows —
      no mutable recovered_total column exists anywhere.
    - OVER-RECOVERY UNREPRESENTABLE (the DB backstop behind the
      service guard): the constraint trigger below re-checks, after
      every INSERT, that the receipts of a write-off never exceed its
      write-once total_written_off AND that the write-off is POSTED
      (a requested/approved/rejected snapshot has no derecognised
      claim to recover against). The application service serialises
      concurrent receipts on the loan_write_offs row FOR UPDATE; this
      trigger is the collusion-resistant backstop for direct SQL.
    - recovery_case_id records the linkage EXPLICITLY (item 2): the closed_written_off recovery case
    the receipt
      was collected under, when one exists. Nullable — a write-off
      posted without a recovery case recovers with a NULL linkage.
    - every FK indexed (scalability/1.5); the (tenant_id, write_off_id,
      created_at, id) index serves both the per-write-off receipts
      keyset page and the recovered-total SUM.
    - RLS enabled AND FORCED with the 0001 tenant_isolation policy
      shape (ADR-0002); the table joins TENANT_TABLES and the leakage
      suite in this MR.

GUARANTEE DISPOSITION (item 3, the documented policy decision): guarantees behind a written-off loan
back the SURVIVING
claim and stay untouched (the A4 posture). They are released
ONLY by the receipt that recovers the claim IN FULL — full recovery
discharges the sureties exactly like genuine closure does (the release hook, reused in the same
transaction). No schema is needed for
this; it is recorded here because this revision is the recovery
branch's home.

Downgrade reverses this revision. The narrower transactions.type CHECK
is restored FIRST and VALIDATES existing rows: every recovery receipt
row references an RC- transaction of type 'loan_recovery', so a
database holding recovery money history refuses the downgrade loudly
(the 0017/0020/0025 conditional-refusal precedent) before the table
drop is reached — money history is never silently deleted. CI's
migrate-check (up -> down -> up) runs on a database without such rows
and passes.
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Transaction taxonomy: the RC- recovery receipt (expand-only, 0020 shape)
-- ---------------------------------------------------------------------------
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement',
         'dividend_posting', 'share_transfer_out', 'share_transfer_in',
         'fee', 'loan_write_off', 'loan_recovery'));

-- ---------------------------------------------------------------------------
-- 2. Recovery receipts against the surviving write-off claim (issue #21)
-- ---------------------------------------------------------------------------
CREATE TABLE loan_recoveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    -- The surviving claim this receipt recovers against (the 0025
    -- write-once snapshot).
    write_off_id uuid NOT NULL REFERENCES loan_write_offs(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    -- The P13.16 linkage, recorded explicitly (issue #21 item 2):
    -- the closed_written_off case the collection was worked under.
    recovery_case_id uuid REFERENCES recovery_cases(id) ON DELETE RESTRICT,
    -- The RC- posting (DR cash / CR income.bad_debt_recoveries).
    transaction_id uuid NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    recorded_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Serves the per-write-off receipts keyset page AND the
-- recovered-total SUM under the write-off row lock (gate 1.3).
CREATE INDEX idx_loan_recoveries_write_off
    ON loan_recoveries (tenant_id, write_off_id, created_at, id);
-- FK indexes (gate 1.3/1.5).
CREATE INDEX idx_loan_recoveries_loan ON loan_recoveries (tenant_id, loan_id);
CREATE INDEX idx_loan_recoveries_member ON loan_recoveries (tenant_id, member_id);
CREATE INDEX idx_loan_recoveries_case
    ON loan_recoveries (tenant_id, recovery_case_id);
CREATE INDEX idx_loan_recoveries_txn
    ON loan_recoveries (tenant_id, transaction_id);
CREATE INDEX idx_loan_recoveries_recorded_by
    ON loan_recoveries (tenant_id, recorded_by);

-- APPEND-ONLY (the 0004/0014 ledger discipline): recovery receipts
-- are money history; corrections are reversing entries, never
-- UPDATE/DELETE (gate 1.5). forbid_row_mutation() is defined in
-- migration 0001 and reused here (the 0004 transactions precedent).
CREATE TRIGGER loan_recoveries_no_update
    BEFORE UPDATE ON loan_recoveries
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();
CREATE TRIGGER loan_recoveries_no_delete
    BEFORE DELETE ON loan_recoveries
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();

-- OVER-RECOVERY / PREMATURE-RECOVERY BACKSTOP (FM1/FM2): after every
-- INSERT, the receipts of a write-off must not exceed its write-once
-- total_written_off, and the write-off must be POSTED. The service
-- guard serialises on the loan_write_offs row FOR UPDATE; this
-- trigger makes a bypassing direct-SQL INSERT fail loudly. The
-- explicit tenant predicate on the parent lookup keeps the check
-- tenant-safe even under a privileged role (the 0014 rationale).
CREATE OR REPLACE FUNCTION check_recovery_within_claim() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    claim numeric;
    wo_status text;
    recovered numeric;
BEGIN
    SELECT w.total_written_off, w.status INTO claim, wo_status
    FROM loan_write_offs w
    WHERE w.id = NEW.write_off_id AND w.tenant_id = NEW.tenant_id;
    IF claim IS NULL THEN
        RAISE EXCEPTION
            'recovery receipt % references no same-tenant write-off (issue #21)',
            NEW.id;
    END IF;
    IF wo_status <> 'posted' THEN
        RAISE EXCEPTION
            'recovery receipts apply to POSTED write-offs only (issue #21): '
            'write-off % is ''%''', NEW.write_off_id, wo_status;
    END IF;
    SELECT COALESCE(SUM(r.amount), 0) INTO recovered
    FROM loan_recoveries r
    WHERE r.write_off_id = NEW.write_off_id AND r.tenant_id = NEW.tenant_id;
    IF recovered > claim THEN
        RAISE EXCEPTION
            'recovery receipts for write-off % would exceed the written-off '
            'claim (issue #21)', NEW.write_off_id;
    END IF;
    RETURN NULL;
END
$fn$;

CREATE CONSTRAINT TRIGGER loan_recoveries_within_claim
    AFTER INSERT ON loan_recoveries
    FOR EACH ROW EXECUTE FUNCTION check_recovery_within_claim();

ALTER TABLE loan_recoveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE loan_recoveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON loan_recoveries
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
-- Restoring the narrower type CHECK VALIDATES existing rows FIRST:
-- every loan_recoveries row references a 'loan_recovery' transaction,
-- so a database holding recovery money history refuses this loudly
-- (0017/0020/0025 conditional-refusal precedent) before any drop runs.
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement',
         'dividend_posting', 'share_transfer_out', 'share_transfer_in',
         'fee', 'loan_write_off'));

DROP TRIGGER IF EXISTS loan_recoveries_within_claim ON loan_recoveries;
DROP FUNCTION IF EXISTS check_recovery_within_claim();
DROP TRIGGER IF EXISTS loan_recoveries_no_update ON loan_recoveries;
DROP TRIGGER IF EXISTS loan_recoveries_no_delete ON loan_recoveries;
DROP TABLE IF EXISTS loan_recoveries;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
