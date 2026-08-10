"""report vocabulary CHECK widening: transactions_ledger

Revision ID: 0047
Revises: 0044
Create Date: 2026-08-09

Claimed as 0047 with down_revision 0044 per the migration-declaration
rule — 0044 (users phone sign-in index) verified as this branch's
migration head at claim time, with 0045 (ID-number expression index)
and 0046 (KYC phone E.164 backfill) already claimed by the open
sibling MRs.

Expand-only in the SCHEMA sense (nothing dropped or narrowed; one
release backward compatible): the 0013 exports.report CHECK pins the
report vocabulary and the registry gains exactly ONE report here —
transactions_ledger (the exact 0020/0023 dividend_rebate_schedule
precedent). NO new tables, NO new indexes: the register export reuses
idx_ledger_txn / the transactions PK shipped with their queries, and
the register page's own keyset index — the report is a projection of
the already-indexed register read path.

Lock posture (honest, the house rules): the CHECK re-add (ALTER
TABLE... DROP CONSTRAINT + ADD CONSTRAINT) FULLY VALIDATES existing
exports rows under ACCESS EXCLUSIVE, blocking reads and writes on
exports for the scan — the maintenance-window runner model this
project operates (see 0023).

Downgrade restores the 0023 eleven-report CHECK exactly: a database
holding transactions_ledger export jobs refuses the narrowing loudly
(the 0017/0020 conditional-refusal precedent) instead of stranding
invalid rows.
"""

from alembic import op

revision = "0047"
down_revision = "0044"
branch_labels = None
depends_on = None

_UP = """
-- Expand the report vocabulary (0020/0023 precedent, expand-only).
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement',
         'dividend_rebate_schedule', 'portfolio_at_risk_aging',
         'membership_register', 'income_statement', 'sasra_return',
         'transactions_ledger'));
"""

_DOWN = """
-- Restoring the narrower CHECK validates existing rows: a database
-- holding transactions_ledger export jobs refuses this loudly (the
-- 0017/0020 conditional-refusal precedent) instead of stranding
-- invalid rows.
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement',
         'dividend_rebate_schedule', 'portfolio_at_risk_aging',
         'membership_register', 'income_statement', 'sasra_return'));
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
