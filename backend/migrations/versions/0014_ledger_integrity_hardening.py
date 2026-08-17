"""ledger integrity hardening (external Codex review, re-derived)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-30

Re-derivation of the DB-level findings from the external Codex code
review (branch fix/codex-code-reviews, commits 21f19fb2 / 982f8c2d /
78ab9267). The original commits edited migrations 0004/0006/0012 in
place — already-applied revisions that alembic would never re-run on an
existing database — so every change lands here as a NEW, additive
(expand-only) revision with a working downgrade. All changes were
verified against every posting factory in genesis.domain.ledger and
every direct-SQL test fixture before being enforced at the database.

  1. Tenant-safe reversal linkage: transactions.reversal_of_id was a
     single-column FK to transactions(id), so raw SQL running as a
     privileged role could link a reversal across tenants (RLS fences
     the app role; defence in depth must not depend on it — gate 1.6
     v1.1). A UNIQUE (tenant_id, id) target plus a composite
     (tenant_id, reversal_of_id) FK makes cross-tenant linkage
     impossible at the database. The FK is added NOT VALID and then
     VALIDATEd so existing rows are checked without blocking writes
     for the whole scan (expand-safe on live data; validation passing
     is the proof existing rows are safe). The old single-column FK is
     dropped only after the composite one is in place.

  2. check_ledger_balanced now also enforces that the transaction's
     ledger totals equal transactions.amount (deferred, at COMMIT):
     balanced-but-short or balanced-but-extra leg sets are rejected
     even from raw SQL. Verified against every PostingSpec factory
     (deposit, withdrawal, share top-up, disbursement, repayment,
     allocated repayment, both interest postings, exit settlement,
     reversal): each constructs DR total == spec.amount by design.
     IS DISTINCT FROM guards the (FK-impossible) NULL parent case.
     The balance check stays first so existing "Unbalanced ledger"
     semantics and tests are unchanged.

  3. forbid_closed_period_posting takes the same SHARED per-tenant
     advisory lock as the application guard
     (accounting_periods.assert_open_period: namespace
     crc32('acct_period') & 0x7fffffff = 740180831, key = first 4
     bytes of the tenant uuid, big-endian, masked to int4) before
     reading accounting_periods. Without it a raw-SQL insert racing a
     concurrent close_period() could commit into the period being
     closed (close takes the EXCLUSIVE lock and only fences writers
     that take the shared one). uuid_send yields the RFC-4122 bytes in
     order, so the key derivation is byte-for-byte the Python
     _tenant_lock_key.

  4. Stage-filtered application listing index:
     (tenant_id, stage, created_at DESC, id DESC). The Codex fix
     REPLACED the 0006 keyset index with this shape — which would have
     broken the unfiltered listing (stage in the middle cannot serve
     global (created_at, id) order without a sort). Here the 0006
     index is KEPT for the unfiltered scan and the stage-shaped index
     is ADDED; the 0001 idx_applications_stage (tenant_id, stage)
     prefix duplicate becomes redundant and is dropped.

  5. idx_repayments_transaction — repayments.transaction_id was an
     unindexed FK; the new repayment-reversal guard
     (ledger.post_reversal) and the NPL-trend joins query by it
     (gate 1.3: index shipped with the query that needs it).

Working downgrade restores the exact 0004/0012 function bodies and the
0001 index, and drops only the new objects.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_UP = """
-- -------------------------------------------------------------------------
-- 1. Tenant-safe reversal linkage (gate 1.6 v1.1 defence in depth)
-- -------------------------------------------------------------------------
ALTER TABLE transactions
    ADD CONSTRAINT uq_transactions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE transactions
    ADD CONSTRAINT fk_transactions_reversal_same_tenant
    FOREIGN KEY (tenant_id, reversal_of_id)
    REFERENCES transactions (tenant_id, id)
    ON DELETE RESTRICT
    NOT VALID;

ALTER TABLE transactions
    VALIDATE CONSTRAINT fk_transactions_reversal_same_tenant;

ALTER TABLE transactions
    DROP CONSTRAINT transactions_reversal_of_id_fkey;

-- -------------------------------------------------------------------------
-- 2. Ledger totals must equal transactions.amount (gate 1.5)
-- -------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_ledger_balanced() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    dr numeric;
    cr numeric;
    txn_amount numeric;
BEGIN
    SELECT
        COALESCE(SUM(CASE WHEN side = 'debit'  THEN amount END), 0),
        COALESCE(SUM(CASE WHEN side = 'credit' THEN amount END), 0)
    INTO dr, cr
    FROM ledger_entries
    WHERE transaction_id = NEW.transaction_id;

    IF dr <> cr THEN
        RAISE EXCEPTION
            'Unbalanced ledger for transaction %: DR=% CR=%',
            NEW.transaction_id, dr, cr;
    END IF;

    SELECT t.amount INTO txn_amount
    FROM transactions t
    WHERE t.id = NEW.transaction_id;

    IF dr IS DISTINCT FROM txn_amount THEN
        RAISE EXCEPTION
            'Ledger totals for transaction % do not match transaction amount %: DR=% CR=%',
            NEW.transaction_id, txn_amount, dr, cr;
    END IF;
    RETURN NULL;
END
$fn$;

-- -------------------------------------------------------------------------
-- 3. Closed-period backstop serialises with close_period() (gate 1.4)
-- -------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_closed_period_posting() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    tid_bytes bytea;
    lock_key integer;
BEGIN
    tid_bytes := uuid_send(NEW.tenant_id);
    lock_key := ((
          (get_byte(tid_bytes, 0)::bigint << 24)
        + (get_byte(tid_bytes, 1)::bigint << 16)
        + (get_byte(tid_bytes, 2)::bigint << 8)
        +  get_byte(tid_bytes, 3)::bigint
    ) & 2147483647)::integer;
    PERFORM pg_advisory_xact_lock_shared(740180831, lock_key);

    IF EXISTS (
        SELECT 1 FROM accounting_periods p
        WHERE p.tenant_id = NEW.tenant_id
          AND p.status = 'closed'
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date >= p.period_start
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date <= p.period_end
    ) THEN
        RAISE EXCEPTION
            'transaction occurred_at falls inside a closed accounting period '
            '(MASTER_PROMPT gate 1.5; issue #12)';
    END IF;
    RETURN NEW;
END
$fn$;

-- -------------------------------------------------------------------------
-- 4. Stage-filtered keyset index (gate 1.3) — ADDED alongside 0006,
--    which keeps serving the unfiltered listing. 0001's
--    idx_applications_stage is a strict prefix of the new index and is
--    dropped as redundant write amplification.
-- -------------------------------------------------------------------------
CREATE INDEX idx_applications_stage_keyset
    ON loan_applications (tenant_id, stage, created_at DESC, id DESC);

DROP INDEX idx_applications_stage;

-- -------------------------------------------------------------------------
-- 5. repayments.transaction_id FK index (gate 1.3)
-- -------------------------------------------------------------------------
CREATE INDEX idx_repayments_transaction ON repayments (tenant_id, transaction_id);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_repayments_transaction;

CREATE INDEX idx_applications_stage ON loan_applications (tenant_id, stage);
DROP INDEX IF EXISTS idx_applications_stage_keyset;

-- Restore the exact 0012 function body.
CREATE OR REPLACE FUNCTION forbid_closed_period_posting() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1 FROM accounting_periods p
        WHERE p.tenant_id = NEW.tenant_id
          AND p.status = 'closed'
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date >= p.period_start
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date <= p.period_end
    ) THEN
        RAISE EXCEPTION
            'transaction occurred_at falls inside a closed accounting period '
            '(MASTER_PROMPT gate 1.5; issue #12)';
    END IF;
    RETURN NEW;
END
$fn$;

-- Restore the exact 0004 function body.
CREATE OR REPLACE FUNCTION check_ledger_balanced() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    dr numeric;
    cr numeric;
BEGIN
    SELECT
        COALESCE(SUM(CASE WHEN side = 'debit'  THEN amount END), 0),
        COALESCE(SUM(CASE WHEN side = 'credit' THEN amount END), 0)
    INTO dr, cr
    FROM ledger_entries
    WHERE transaction_id = NEW.transaction_id;

    IF dr <> cr THEN
        RAISE EXCEPTION
            'Unbalanced ledger for transaction %: DR=% CR=%',
            NEW.transaction_id, dr, cr;
    END IF;
    RETURN NULL;
END
$fn$;

ALTER TABLE transactions
    ADD CONSTRAINT transactions_reversal_of_id_fkey
    FOREIGN KEY (reversal_of_id)
    REFERENCES transactions (id) ON DELETE RESTRICT;
ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS fk_transactions_reversal_same_tenant;
ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS uq_transactions_tenant_id_id;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
