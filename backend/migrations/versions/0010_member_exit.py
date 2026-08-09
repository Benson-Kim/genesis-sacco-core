"""member exit & settlement: snapshot columns, exit votes, exit fee

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-29

Additive only (expand phase). The member_exits table itself exists
since 0001 (reuse-first, reuse-first) with the snapshot amount columns
and the requested/approved/settled/rejected status CHECK; this
migration adds what the workflow needs on top:

  * member_exits workflow columns — reason, requested_by, decided_at,
    settled_at, settlement_transaction_id. The settlement transaction
    FK links the terminal posting to its approved snapshot.
  * uq_member_exits_open — at most ONE open (requested/approved)
    settlement per member, enforced at the database level: concurrent
    double-submits collapse to exactly one row (concurrency safety). The partial
    index also serves the open-settlement lookup by member.
  * idx_exits_created_keyset — the exits listing paginates on
    (created_at, id) DESC; leading with tenant_id matches the RLS
    predicate (scalability: index shipped with the query).
  * idx_exits_status_created_keyset — the same listing with a status
    filter: (tenant_id, status, created_at DESC, id DESC) so the
    filtered keyset page is index-backed too (scalability; the 0001 idx_exits_status cannot serve
    the keyset ORDER BY).
  * exit_votes — committee approval reusing the P9 voting shape:
    UNIQUE (tenant_id, exit_id, voter_id) makes double-voting
    impossible at the database level (concurrency safety); RLS matches 0001.
    exit_id is ON DELETE RESTRICT: votes are governance evidence and
    must be non-erasable — deleting a decided exit must fail loudly,
    never silently erase its vote trail.
  * tenant_settings.exit_fee — the exit fee comes exclusively from
    tenant configuration (0009 pattern; the caller-rate lesson: money parameters never travel in
    request bodies). Tenants without
    a tenant_settings row charge no exit fee (documented default 0).

net_payable deliberately keeps no CHECK >= 0: the application rejects
negative settlements at request time (documented rule), and the column
must stay able to represent historical/reporting shortfalls without a
schema contraction later.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE tenant_settings
    ADD COLUMN exit_fee numeric(18,2) NOT NULL DEFAULT 0
        CHECK (exit_fee >= 0);

ALTER TABLE member_exits
    ADD COLUMN reason text,
    ADD COLUMN requested_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN settled_at timestamptz,
    ADD COLUMN settlement_transaction_id uuid
        REFERENCES transactions(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_member_exits_open
    ON member_exits (tenant_id, member_id)
    WHERE status IN ('requested', 'approved');

CREATE INDEX idx_exits_created_keyset
    ON member_exits (tenant_id, created_at DESC, id DESC);

CREATE INDEX idx_exits_status_created_keyset
    ON member_exits (tenant_id, status, created_at DESC, id DESC);

CREATE INDEX idx_exits_requested_by
    ON member_exits (tenant_id, requested_by);

CREATE INDEX idx_exits_settlement_txn
    ON member_exits (tenant_id, settlement_transaction_id);

CREATE TABLE exit_votes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    exit_id uuid NOT NULL REFERENCES member_exits(id) ON DELETE RESTRICT,
    voter_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    vote text NOT NULL CHECK (vote IN ('approve', 'reject')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, exit_id, voter_id)
);
CREATE INDEX idx_exit_votes_exit ON exit_votes (tenant_id, exit_id);
CREATE INDEX idx_exit_votes_voter ON exit_votes (tenant_id, voter_id);

ALTER TABLE exit_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE exit_votes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON exit_votes
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
DROP TABLE IF EXISTS exit_votes CASCADE;
DROP INDEX IF EXISTS idx_exits_settlement_txn;
DROP INDEX IF EXISTS idx_exits_requested_by;
DROP INDEX IF EXISTS idx_exits_status_created_keyset;
DROP INDEX IF EXISTS idx_exits_created_keyset;
DROP INDEX IF EXISTS uq_member_exits_open;
ALTER TABLE member_exits
    DROP COLUMN IF EXISTS settlement_transaction_id,
    DROP COLUMN IF EXISTS settled_at,
    DROP COLUMN IF EXISTS decided_at,
    DROP COLUMN IF EXISTS requested_by,
    DROP COLUMN IF EXISTS reason;
ALTER TABLE tenant_settings DROP COLUMN IF EXISTS exit_fee;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
