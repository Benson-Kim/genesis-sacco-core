"""branches registry (P13.6)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-31

EXPAND-ONLY revision backing BUILD_PROMPTS P13.6 (§3 migration policy:
the free-text users.branch column is NOT touched here; its contract/
removal is deferred one release, after this registry has shipped and
the backfill has run everywhere):

  1. branches — the tenant-scoped registry behind the prototype's
     branch strings ("Nairobi CBD", "Thika", "HQ"). NOT NULL
     throughout, UNIQUE (tenant_id, name) as the atomic-claim key
     (v1.1 rule 5), a DB CHECK refusing blank names (gate 1.5), and a
     version column for optimistic locking (gate 1.4). RLS enabled +
     FORCED with the same tenant_isolation policy shape as 0001
     (ADR-0002); the leakage suite is extended to it.

     Branch is organisational metadata ONLY: no money path keys on it
     in this prompt — cash/till management is explicitly out of scope
     (GAP_ANALYSIS §2.6).

  2. users.branch_id / members.branch_id — nullable FKs (expand-only;
     existing rows stay valid) with explicit ON DELETE RESTRICT: a
     branch with people assigned cannot be deleted out from under
     them. Both FKs are indexed tenant-first (gate 1.3).

  3. idx_branches_created_keyset — the listing paginates on the shared
     (created_at DESC, id DESC) keyset (gate 1.3); shipped in the same
     revision as the query that needs it (EXPLAIN-asserted in
     tests/test_p136_explain.py).

  4. idx_users_branch_backfill — partial index (tenant_id, id) WHERE
     branch IS NOT NULL AND branch_id IS NULL, backing the batched
     backfill scan (application/branches.py). The predicate is the
     backfill's anti-join claim key, so the index shrinks towards
     empty as the backfill completes and a re-run scans (and locks)
     nothing (v1.1 rule 8).

The backfill itself is application code through the shared batch
runner (gate 1.1), NOT a data migration: it is idempotent, resumable
and observable via side-effect counts, which an alembic step cannot
prove. Downgrade fully reverses this revision: it drops only the new
objects and no pre-existing data is touched (branch rows and
assignments created after upgrade are dropped with their objects —
the legacy free-text users.branch column remains authoritative until
the deferred contract migration).
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_UP = """
-- -------------------------------------------------------------------------
-- 1. branches registry (P13.6)
-- -------------------------------------------------------------------------
CREATE TABLE branches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    name text NOT NULL CHECK (btrim(name) <> ''),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- RLS: enable + force + tenant_isolation policy, matching 0001 (ADR-0002).
ALTER TABLE branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE branches FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON branches
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- Listing keyset index (gate 1.3), same shape as 0015's users keyset.
CREATE INDEX idx_branches_created_keyset
    ON branches (tenant_id, created_at DESC, id DESC);

-- -------------------------------------------------------------------------
-- 2. Nullable branch_id FKs on users and members (expand-only, gate 1.3)
-- -------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN branch_id uuid
    REFERENCES branches(id) ON DELETE RESTRICT;
ALTER TABLE members ADD COLUMN branch_id uuid
    REFERENCES branches(id) ON DELETE RESTRICT;

CREATE INDEX idx_users_branch ON users (tenant_id, branch_id);
CREATE INDEX idx_members_branch ON members (tenant_id, branch_id);

-- -------------------------------------------------------------------------
-- 3. Backfill scan index: the anti-join claim key as a partial predicate
-- -------------------------------------------------------------------------
CREATE INDEX idx_users_branch_backfill ON users (tenant_id, id)
    WHERE branch IS NOT NULL AND branch_id IS NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_users_branch_backfill;
DROP INDEX IF EXISTS idx_members_branch;
DROP INDEX IF EXISTS idx_users_branch;

ALTER TABLE members DROP COLUMN IF EXISTS branch_id;
ALTER TABLE users DROP COLUMN IF EXISTS branch_id;

DROP INDEX IF EXISTS idx_branches_created_keyset;
DROP TABLE IF EXISTS branches;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
