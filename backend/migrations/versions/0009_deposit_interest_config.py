"""deposit-interest configuration + full accrual-scan index (review)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-29

Additive only (expand phase):

  * tenant_settings — per-tenant configuration, starting with the
    deposit-interest annual rate. The accrual job reads the rate from
    here exclusively; callers can no longer supply a rate (review
    finding: a caller-chosen rate let any past quarter be accrued at up
    to 100%). One row per tenant; the Settings module (P4 matrix) owns
    its maintenance. RLS matches 0001.
  * idx_deposit_accounts_keyset — the accrual job now scans *every*
    deposit account (not only positive balances) because interest is
    computed from the balance as of the period end, reconstructed from
    the ledger: an account emptied after the quarter ended still earned
    that quarter's interest. The 0008 partial index
    idx_deposit_accounts_scan no longer matches the scan predicate and
    is kept (additive discipline); this full (tenant_id, id) index
    serves the new keyset walk.

No index is needed for the GET /transactions ref filter: the 0001
UNIQUE (tenant_id, txn_ref) constraint on transactions already backs
`txn_ref =:ref` lookups (review finding 12 verified, not added).
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE tenant_settings (
    tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
    deposit_interest_annual_rate_pct numeric(5,2) NOT NULL
        CHECK (deposit_interest_annual_rate_pct > 0
               AND deposit_interest_annual_rate_pct <= 100),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE tenant_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_settings
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE INDEX idx_deposit_accounts_keyset
    ON deposit_accounts (tenant_id, id);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_deposit_accounts_keyset;
DROP TABLE IF EXISTS tenant_settings CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
