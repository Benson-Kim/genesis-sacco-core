"""committee votes: one vote per member per application (P9)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28

Gates satisfied:
  1.4 - UNIQUE (tenant_id, application_id, voter_id) makes double-voting
        impossible at the database level.
  1.6 - RLS enabled and forced, matching the 0001 tenancy pattern.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE committee_votes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    application_id uuid NOT NULL
        REFERENCES loan_applications(id) ON DELETE CASCADE,
    voter_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    vote text NOT NULL CHECK (vote IN ('approve', 'reject')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, application_id, voter_id)
);
CREATE INDEX idx_votes_application
    ON committee_votes (tenant_id, application_id);
CREATE INDEX idx_votes_voter ON committee_votes (tenant_id, voter_id);

ALTER TABLE committee_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE committee_votes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON committee_votes
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
DROP TABLE IF EXISTS committee_votes CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
