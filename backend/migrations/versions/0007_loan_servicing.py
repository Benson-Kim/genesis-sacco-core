"""loan servicing: penalties bucket, closure timestamp, servicing indexes

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-29

Additive only (expand phase; gate on backward-compatible migrations):

  * loans.penalty_due — the penalties bucket consumed first by the
    documented repayment allocation order (penalties → interest →
    principal). DB CHECK keeps it non-negative (data integrity).
  * loans.closed_at — set once when the balance reaches zero and the
    status machine moves the loan to 'closed'.
  * idx_loans_created_keyset — the loan book listing paginates on
    (created_at, id) DESC; leading with tenant_id matches the RLS
    predicate so keyset pages stay index-backed at any depth (scalability).
  * idx_loans_active_scan — the nightly arrears job walks active loans
    in id keyset batches; the partial index covers exactly that scan.
  * idx_schedules_unpaid — days-past-due and the interest-due bucket
    both aggregate unpaid installments (paid_amount < total_due) per
    loan; the partial index serves the arrears join and the repayment
    allocation query (scalability: index shipped with the query).
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE loans
    ADD COLUMN penalty_due numeric(18,2) NOT NULL DEFAULT 0
        CHECK (penalty_due >= 0),
    ADD COLUMN closed_at timestamptz;

CREATE INDEX idx_loans_created_keyset
    ON loans (tenant_id, created_at DESC, id DESC);

CREATE INDEX idx_loans_active_scan
    ON loans (tenant_id, id)
    WHERE status = 'active';

CREATE INDEX idx_schedules_unpaid
    ON loan_schedules (tenant_id, loan_id, due_date)
    WHERE paid_amount < total_due;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_schedules_unpaid;
DROP INDEX IF EXISTS idx_loans_active_scan;
DROP INDEX IF EXISTS idx_loans_created_keyset;
ALTER TABLE loans
    DROP COLUMN IF EXISTS closed_at,
    DROP COLUMN IF EXISTS penalty_due;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
