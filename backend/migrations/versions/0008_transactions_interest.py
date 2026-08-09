"""transactions & deposit interest: accrual idempotency, listing indexes

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29

Additive only (expand phase):

  * deposit_interest_accruals — exactly one row per (account, quarter);
    the UNIQUE constraint is the database-level idempotency guarantee
    for the quarterly job: re-runs post nothing new and a crash mid-run
    resumes safely (the house gates). Zero-interest accounts claim a row
    with amount 0 and no transaction, so transaction_id is nullable and
    amount is CHECKed >= 0 (not > 0).
  * idx_txns_occurred_keyset — the ledger listing paginates on
    (occurred_at, id) DESC; leading with tenant_id matches the RLS
    predicate so pages stay index-backed at any depth (scalability).
  * idx_txns_member_keyset — the same page filtered to one member
    (statement-style lookups) at any depth.
  * idx_deposit_accounts_scan — the accrual job walks positive-balance
    accounts in id-keyset batches; the partial index covers exactly
    that scan (scalability: index shipped with the query).
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE deposit_interest_accruals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    account_id uuid NOT NULL
        REFERENCES deposit_accounts(id) ON DELETE RESTRICT,
    period_start date NOT NULL,
    period_end date NOT NULL CHECK (period_end > period_start),
    annual_rate_pct numeric(5,2) NOT NULL
        CHECK (annual_rate_pct > 0 AND annual_rate_pct <= 100),
    amount numeric(18,2) NOT NULL CHECK (amount >= 0),
    transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, account_id, period_start)
);
CREATE INDEX idx_accruals_account
    ON deposit_interest_accruals (tenant_id, account_id);
CREATE INDEX idx_accruals_txn
    ON deposit_interest_accruals (tenant_id, transaction_id);

ALTER TABLE deposit_interest_accruals ENABLE ROW LEVEL SECURITY;
ALTER TABLE deposit_interest_accruals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON deposit_interest_accruals
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE INDEX idx_txns_occurred_keyset
    ON transactions (tenant_id, occurred_at DESC, id DESC);

CREATE INDEX idx_txns_member_keyset
    ON transactions (tenant_id, member_id, occurred_at DESC, id DESC);

CREATE INDEX idx_deposit_accounts_scan
    ON deposit_accounts (tenant_id, id)
    WHERE balance > 0;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_deposit_accounts_scan;
DROP INDEX IF EXISTS idx_txns_member_keyset;
DROP INDEX IF EXISTS idx_txns_occurred_keyset;
DROP TABLE IF EXISTS deposit_interest_accruals CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
