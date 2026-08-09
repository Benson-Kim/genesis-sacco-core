"""ledger corrections, misc fees & loan write-off

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-01

Expand-only revision backing the build plan. RLS enabled AND
FORCED on every new table with the 0001/0019/0020 tenant_isolation
policy shape (ADR-0002); DB CHECKs on every bound (data integrity); the
leakage suite is extended to all three tables.

  * transactions.type CHECK expanded (expand-only, the 0020 precedent)
    with 'fee' (FE- misc fees, amounts exclusively from config)
    and 'loan_write_off' (WO- provisioning posting).

  * repayments.amount CHECK relaxed from (> 0) to (<> 0): the
    repayment adjustment writes a NEGATIVE-linked correction row (the
    storno pair of the original) whose transaction is the reversing
    posting. Zero rows stay unrepresentable. The downgrade restores
    (> 0), which VALIDATES existing rows — a database holding
    correction history refuses the downgrade loudly (the 0017/0020
    conditional-refusal precedent; money history is never silently
    deleted).

  * repayment_adjustments — the atomic one-adjustment-per-repayment
    CLAIM: UNIQUE (tenant_id, repayment_id) claimed with
    INSERT... ON CONFLICT DO NOTHING + rowcount, so a second
    adjustment of the same repayment is unrepresentable. The row
    records the maker distinctly (maker-checker), the exact
    allocation components being undone (identity CHECK: amount =
    penalties + interest + principal) and BOTH transaction ids — the
    original and the reversing posting (storno linkage; the ledger linkage itself is
    transactions.reversal_of_id, tenant-safe per 0014). A write-once trigger (the 0020 precedent)
    pins every
    column after insert except the one workflow write: filling
    reversal_transaction_id from NULL inside the same adjustment
    transaction.

  * loan_write_offs — the PERSISTED, DB-LEVEL WRITE-ONCE approval
    snapshot (the 0020-trigger precedent): the loan's
    balance, penalty_due, classification and provision at request
    time, which the committee approves and posting re-verifies
    component-by-component (409 on drift, posting nothing). The
    write-once trigger refuses any mutation of the snapshot figures in
    ANY status — even manual SQL through the app role fails loudly
    (/A6); only the status workflow columns (status, decided_at,
    posted_at, version, updated_at) and the NULL -> value fill of
    transaction_id may move. The classification CHECK restricts the
    snapshot to NPL classes ('substandard'/'doubtful'/'loss') — the
    prudential DB backstop behind the request_write_off service gate
    (/): a performing-loan write-off snapshot is
    unrepresentable at the database. A4 (write-off is NOT forgiveness): the
    snapshot persists the written-off claim on the member after the
    receivable is derecognised; post-write-off recovery is a future
    explicit branch (follow-up issue referencing /).

  * uq_loan_write_offs_open — at most ONE live (non-rejected)
    write-off per loan: concurrent double-requests collapse to exactly
    one row, and a POSTED loan can never be requested again.
    Rejected/voided requests free the slot (the 0010/0020 shape).

  * loan_write_off_votes — the P9/0005/0010/0020 committee-voting
    shape: one vote per (tenant, write_off, voter) enforced by UNIQUE
    (concurrency safety).

Downgrade reverses this revision: the new tables, triggers and
functions are dropped and the original repayments/transactions CHECKs
are restored. Restoring the narrower CHECKs VALIDATES existing rows,
so a database holding fee/write-off transactions or negative
correction rows refuses the downgrade loudly (the 0017/0020
precedent); CI's migrate-check (up -> down -> up) runs on a database
without such rows and passes.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Transaction taxonomy: fee + write-off types (expand-only, 0020 shape)
-- ---------------------------------------------------------------------------
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement',
         'dividend_posting', 'share_transfer_out', 'share_transfer_in',
         'fee', 'loan_write_off'));

-- ---------------------------------------------------------------------------
-- 2. Negative-linked repayment correction rows (P13.15 adjustment)
-- ---------------------------------------------------------------------------
ALTER TABLE repayments DROP CONSTRAINT repayments_amount_check;
ALTER TABLE repayments ADD CONSTRAINT repayments_amount_check
    CHECK (amount <> 0);

-- ---------------------------------------------------------------------------
-- 3. Repayment adjustments: the one-adjustment-per-repayment claim (FM2)
-- ---------------------------------------------------------------------------
CREATE TABLE repayment_adjustments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    repayment_id uuid NOT NULL REFERENCES repayments(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    original_transaction_id uuid NOT NULL
        REFERENCES transactions(id) ON DELETE RESTRICT,
    -- Filled from NULL inside the same adjustment transaction (the
    -- 0020 dividend_distributions.transaction_id precedent); the
    -- write-once trigger permits exactly that one write.
    reversal_transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    -- A3 maker-checker: the maker is recorded distinctly and
    -- immutably; the audit row carries the same actor.
    maker_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    -- The COMPLETE original allocation being undone (A1: partial-leg
    -- reversal is unrepresentable — the identity CHECK pins the parts
    -- to the whole).
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    penalties numeric(18,2) NOT NULL CHECK (penalties >= 0),
    interest numeric(18,2) NOT NULL CHECK (interest >= 0),
    principal numeric(18,2) NOT NULL CHECK (principal >= 0),
    -- FM6 evidence: did this adjustment take the documented
    -- CLOSED -> ACTIVE reopen branch?
    reopened_loan boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_repayment_adjustments_components
        CHECK (amount = penalties + interest + principal),
    -- The atomic idempotency/double-adjustment claim (v1.1 rule 5).
    CONSTRAINT uq_repayment_adjustments_claim UNIQUE (tenant_id, repayment_id)
);

CREATE INDEX idx_repayment_adjustments_loan
    ON repayment_adjustments (tenant_id, loan_id);
CREATE INDEX idx_repayment_adjustments_orig_txn
    ON repayment_adjustments (tenant_id, original_transaction_id);
CREATE INDEX idx_repayment_adjustments_rev_txn
    ON repayment_adjustments (tenant_id, reversal_transaction_id);
CREATE INDEX idx_repayment_adjustments_maker
    ON repayment_adjustments (tenant_id, maker_id);

-- Write-once (A6, the 0020 precedent): an adjustment is money history.
-- The ONLY legal update is filling reversal_transaction_id from NULL
-- (done inside the same adjustment transaction); everything else must
-- fail loudly, even from manual SQL through the app role.
CREATE OR REPLACE FUNCTION forbid_repayment_adjustment_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF (NEW.tenant_id, NEW.repayment_id, NEW.loan_id,
        NEW.original_transaction_id, NEW.maker_id, NEW.reason,
        NEW.amount, NEW.penalties, NEW.interest, NEW.principal,
        NEW.reopened_loan, NEW.created_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.repayment_id, OLD.loan_id,
        OLD.original_transaction_id, OLD.maker_id, OLD.reason,
        OLD.amount, OLD.penalties, OLD.interest, OLD.principal,
        OLD.reopened_loan, OLD.created_at)
       OR (OLD.reversal_transaction_id IS NOT NULL
           AND NEW.reversal_transaction_id
               IS DISTINCT FROM OLD.reversal_transaction_id)
    THEN
        RAISE EXCEPTION
            'repayment adjustment % is write-once (P13.15)', OLD.id;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER repayment_adjustments_write_once
    BEFORE UPDATE ON repayment_adjustments
    FOR EACH ROW EXECUTE FUNCTION forbid_repayment_adjustment_mutation();

ALTER TABLE repayment_adjustments ENABLE ROW LEVEL SECURITY;
ALTER TABLE repayment_adjustments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON repayment_adjustments
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 4. Loan write-offs: the write-once approval snapshot (v1.1 rule 3, FM4)
-- ---------------------------------------------------------------------------
CREATE TABLE loan_write_offs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    -- The snapshot figures the committee approves (re-verified
    -- component-by-component at posting; 409 on drift). balance is
    -- the principal receivable the WO- posting derecognises;
    -- penalty_due is receivable-side loan-row bookkeeping (no ledger
    -- leg — penalty income is recognised on receipt, the P13.8 rule).
    balance numeric(18,2) NOT NULL CHECK (balance >= 0),
    penalty_due numeric(18,2) NOT NULL CHECK (penalty_due >= 0),
    -- A4: the surviving legal claim on the member. Writing off
    -- nothing is unrepresentable.
    total_written_off numeric(18,2) NOT NULL CHECK (total_written_off > 0),
    -- Review B3 / FM9: the prudential DB backstop. Write-off is the
    -- LAST stage of credit deterioration (SASRA-aligned, IFRS-9
    -- consistent), so a snapshot of a PERFORMING loan is
    -- unrepresentable — even via direct SQL through the app role
    -- (collusion resistance: a committee quorum is an authorisation
    -- control, not a prudential one). The service guard in
    -- request_write_off refuses first; this CHECK is the backstop.
    classification text NOT NULL
        CHECK (classification IN ('substandard', 'doubtful', 'loss')),
    provision_pct numeric(5,2) NOT NULL
        CHECK (provision_pct >= 0 AND provision_pct <= 100),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'approved', 'rejected', 'posted')),
    requested_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    decided_at timestamptz,
    posted_at timestamptz,
    transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_loan_write_offs_totals
        CHECK (total_written_off = balance + penalty_due)
);

-- One live write-off workflow per loan (concurrent double-requests
-- collapse; a posted loan is permanently closed to new requests).
CREATE UNIQUE INDEX uq_loan_write_offs_open
    ON loan_write_offs (tenant_id, loan_id)
    WHERE status <> 'rejected';

CREATE INDEX idx_loan_write_offs_loan ON loan_write_offs (tenant_id, loan_id);
CREATE INDEX idx_loan_write_offs_member
    ON loan_write_offs (tenant_id, member_id);
CREATE INDEX idx_loan_write_offs_txn
    ON loan_write_offs (tenant_id, transaction_id);

-- Write-once snapshot columns (FM4, the 0020 trigger precedent): the
-- figures the committee votes on can never be edited after creation,
-- in ANY status — a drifted snapshot is voided and re-requested,
-- never edited. transaction_id may only be filled from NULL (inside
-- the posting transaction). DB-enforced so even manual SQL through
-- the app role fails loudly.
CREATE OR REPLACE FUNCTION forbid_write_off_snapshot_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF (NEW.tenant_id, NEW.loan_id, NEW.member_id, NEW.balance,
        NEW.penalty_due, NEW.total_written_off, NEW.classification,
        NEW.provision_pct, NEW.reason, NEW.requested_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.loan_id, OLD.member_id, OLD.balance,
        OLD.penalty_due, OLD.total_written_off, OLD.classification,
        OLD.provision_pct, OLD.reason, OLD.requested_by, OLD.created_at)
       OR (OLD.transaction_id IS NOT NULL
           AND NEW.transaction_id IS DISTINCT FROM OLD.transaction_id)
    THEN
        RAISE EXCEPTION
            'loan write-off % snapshot is write-once: void and re-request',
            OLD.id;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER loan_write_offs_write_once
    BEFORE UPDATE ON loan_write_offs
    FOR EACH ROW EXECUTE FUNCTION forbid_write_off_snapshot_mutation();

ALTER TABLE loan_write_offs ENABLE ROW LEVEL SECURITY;
ALTER TABLE loan_write_offs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON loan_write_offs
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 5. Committee votes on a write-off (the P9/0005/0010/0020 voting shape)
-- ---------------------------------------------------------------------------
CREATE TABLE loan_write_off_votes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    write_off_id uuid NOT NULL
        REFERENCES loan_write_offs(id) ON DELETE RESTRICT,
    voter_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    vote text NOT NULL CHECK (vote IN ('approve', 'reject')),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Double-voting impossible at the database level (gate 1.4).
    UNIQUE (tenant_id, write_off_id, voter_id)
);
CREATE INDEX idx_loan_write_off_votes_voter
    ON loan_write_off_votes (tenant_id, voter_id);

ALTER TABLE loan_write_off_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE loan_write_off_votes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON loan_write_off_votes
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
DROP TABLE IF EXISTS loan_write_off_votes;
DROP TRIGGER IF EXISTS loan_write_offs_write_once ON loan_write_offs;
DROP FUNCTION IF EXISTS forbid_write_off_snapshot_mutation();
DROP TABLE IF EXISTS loan_write_offs;
DROP TRIGGER IF EXISTS repayment_adjustments_write_once
    ON repayment_adjustments;
DROP FUNCTION IF EXISTS forbid_repayment_adjustment_mutation();
DROP TABLE IF EXISTS repayment_adjustments;

-- Restoring the narrower CHECKs validates existing rows: a database
-- holding correction rows or fee/write-off transactions refuses this
-- loudly (0017/0020 conditional-refusal precedent) instead of
-- stranding invalid money history.
ALTER TABLE repayments DROP CONSTRAINT repayments_amount_check;
ALTER TABLE repayments ADD CONSTRAINT repayments_amount_check
    CHECK (amount > 0);

ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement',
         'dividend_posting', 'share_transfer_out', 'share_transfer_in'));
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
