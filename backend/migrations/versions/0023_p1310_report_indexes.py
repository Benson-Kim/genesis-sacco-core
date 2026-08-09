"""report registry expansion: register keyset index + report
vocabulary CHECK widening (the house gates)

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-01

Claimed as 0023 with down_revision 0022 per the migration-declaration rule — 0022
(dividends x dormancy policy) verified as main's migration head
at branch time (0001-0022 linear). NO new tables: no TENANT_TABLES /
ENTITY_MODULES / RLS delta. Two changes, expand-only in the SCHEMA
sense (nothing dropped or narrowed; one-release backward compatible):

  * the 0013 exports.report CHECK pins the report vocabulary; the
     registry gains the four reports here (the exact 0020
    dividend_rebate_schedule precedent);
  * idx_members_register_keyset, below.

Lock posture (honest, the house rules): this migration
is NOT lock-free and NOT zero-downtime. CREATE INDEX (non-CONCURRENT)
takes a SHARE lock on members for the whole build — every write to
members blocks until it finishes. The CHECK re-add (ALTER TABLE...
DROP CONSTRAINT + ADD CONSTRAINT) FULLY VALIDATES existing exports
rows under ACCESS EXCLUSIVE, blocking reads and writes on exports for
the scan. This migration therefore ASSUMES the maintenance-window
runner model this project operates: migrations run via a dedicated
job with the app quiesced, and both CI migrate-check and the test
harness apply plain transactional DDL (CREATE INDEX CONCURRENTLY and
NOT VALID/VALIDATE are non-transactional and would break them). The
zero-downtime alternative — CREATE INDEX CONCURRENTLY plus ADD
CONSTRAINT... NOT VALID followed by VALIDATE CONSTRAINT — is
deliberately deferred to the ops-hardening prompt.

The membership register streams the full members table through
the export engine in (created_at, id) keyset order. This composite
index leads with tenant_id (matching the RLS predicate) so the keyset
page stays index-backed at any page depth (EXPLAIN gate:
tests/test_p1310_explain.py -> backend/perf/explain_p1310.txt).

The other reports ship NO new index by design:
  * portfolio-at-risk aging reuses the NPL-trend reconstruction shape
    (idx_schedules_due / idx_repayments_loan / idx_ledger_txn /
    idx_loans_status, all shipped with their queries in 0001/0013);
  * income statement and SASRA return aggregate the ledger exactly
    like the trial balance (idx_ledger_txn + transactions PK),
    with cardinality bounded by the chart of accounts.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_members_register_keyset
    ON members (tenant_id, created_at, id);

-- Expand the report vocabulary (0020 precedent, expand-only).
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement',
         'dividend_rebate_schedule', 'portfolio_at_risk_aging',
         'membership_register', 'income_statement', 'sasra_return'));
"""

_DOWN = """
DROP INDEX IF EXISTS idx_members_register_keyset;

-- Restoring the narrower CHECK validates existing rows: a database
-- holding P13.10 export jobs refuses this loudly (the 0017/0020
-- conditional-refusal precedent) instead of stranding invalid rows.
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement',
         'dividend_rebate_schedule'));
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
