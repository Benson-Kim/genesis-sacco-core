"""keyset index for the loan applications listing (P9, scalability)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29

The applications list paginates on (created_at, id) DESC. This composite
index leads with tenant_id (matching the RLS predicate) so the keyset
scan claimed in the MR's EXPLAIN output is actually index-backed at any
page depth.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_applications_created_keyset
    ON loan_applications (tenant_id, created_at DESC, id DESC);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_applications_created_keyset;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
