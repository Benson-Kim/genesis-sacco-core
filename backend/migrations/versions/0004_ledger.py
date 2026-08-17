"""ledger: balanced DR/CR trigger, append-only trigger, txn_ref sequence table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28

Gates satisfied:
  1.4 — pg_advisory_xact_lock + UNIQUE + retry for reference generation
  1.5 — balanced DR/CR enforced by deferred constraint trigger;
        UPDATE/DELETE blocked by trigger; reversal linkage via
        transactions.reversal_of_id (one reversal per original)

Note: forbid_row_mutation() is defined in migration 0001 and reused here.
No per-table GRANTs exist in this project — access control is RLS-based
(single app role), so txn_ref_sequences needs no GRANT beyond its policy.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_UP = """
-- -------------------------------------------------------------------------
-- txn_ref_sequences: per-tenant, per-prefix monotonic counter used by the
-- application service to generate race-safe references via
-- pg_advisory_xact_lock (gate 1.4).
-- -------------------------------------------------------------------------
CREATE TABLE txn_ref_sequences (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    prefix    text NOT NULL,
    last_val  bigint NOT NULL DEFAULT 0 CHECK (last_val >= 0),
    PRIMARY KEY (tenant_id, prefix)
);

ALTER TABLE txn_ref_sequences ENABLE ROW LEVEL SECURITY;
ALTER TABLE txn_ref_sequences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON txn_ref_sequences
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- -------------------------------------------------------------------------
-- Trigger: ledger_entries is append-only (gate 1.5)
-- UPDATE and DELETE are blocked; corrections must be reversing entries.
-- -------------------------------------------------------------------------
CREATE FUNCTION ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION
        'ledger_entries is append-only (MASTER_PROMPT gate 1.5): '
        'use a reversing entry to correct a posting';
END
$fn$;

CREATE TRIGGER ledger_entries_no_update
    BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

CREATE TRIGGER ledger_entries_no_delete
    BEFORE DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

-- -------------------------------------------------------------------------
-- Trigger: transactions is append-only (gate 1.5)
-- -------------------------------------------------------------------------
CREATE TRIGGER transactions_no_update
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();

CREATE TRIGGER transactions_no_delete
    BEFORE DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();

-- -------------------------------------------------------------------------
-- Reversal linkage (gate 1.5): corrections are reversing entries.
-- reversal_of_id points at the transaction being reversed; the partial
-- UNIQUE index enforces at most one reversal per original, tenant-scoped
-- (consistent with the UNIQUE (tenant_id, txn_ref) pattern in 0001).
-- -------------------------------------------------------------------------
ALTER TABLE transactions
    ADD COLUMN reversal_of_id uuid;

ALTER TABLE transactions
    ADD CONSTRAINT uq_transactions_tenant_id_id UNIQUE (tenant_id, id);

ALTER TABLE transactions
    ADD CONSTRAINT fk_transactions_reversal_same_tenant
    FOREIGN KEY (tenant_id, reversal_of_id)
    REFERENCES transactions (tenant_id, id)
    ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_transactions_reversal_of
    ON transactions (tenant_id, reversal_of_id)
    WHERE reversal_of_id IS NOT NULL;

-- -------------------------------------------------------------------------
-- Constraint trigger: balanced DR/CR and transaction amount enforced per transaction (gate 1.5)
--
-- DEFERRABLE INITIALLY DEFERRED: the check runs at COMMIT, after all lines
-- of a posting are inserted — regardless of how many statements were used.
-- This also protects raw SQL writers that bypass the application service.
-- Constraint triggers must be FOR EACH ROW, so the function checks the
-- total DR vs CR of the inserted row's transaction at commit time.
-- -------------------------------------------------------------------------
CREATE FUNCTION check_ledger_balanced() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    dr numeric;
    cr numeric;
    tx_amount numeric;
BEGIN
    SELECT t.amount
    INTO txn_amount
    FROM transactions AS t
    WHERE t.id = NEW.transacton_id;

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

    IF dr <> txn_amount THEN
        RAISE EXCEPTION
            'Ledger totals for transactions % do not match transaction amount %: '
            'DR=% CR=%',
            NEW.transaction_id, txn_amount, dr, cr;
    END IF;

    RETURN NULL;
END
$fn$;

CREATE CONSTRAINT TRIGGER ledger_entries_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_ledger_balanced();
"""

_DOWN = """
DROP TRIGGER IF EXISTS ledger_entries_balanced   ON ledger_entries;
DROP TRIGGER IF EXISTS ledger_entries_no_update  ON ledger_entries;
DROP TRIGGER IF EXISTS ledger_entries_no_delete  ON ledger_entries;
DROP TRIGGER IF EXISTS transactions_no_update    ON transactions;
DROP TRIGGER IF EXISTS transactions_no_delete    ON transactions;
DROP FUNCTION IF EXISTS check_ledger_balanced();
DROP INDEX IF EXISTS uq_transactions_reversal_of;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS fk_transactions_reversal_same_tenant;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS uq_transactions_tenant_id_id;
ALTER TABLE transactions DROP COLUMN IF EXISTS reversal_of_id;
DROP FUNCTION IF EXISTS ledger_append_only();
DROP TABLE IF EXISTS txn_ref_sequences CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
