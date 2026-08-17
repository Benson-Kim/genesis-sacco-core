"""users administration & audit-log viewer (P13.5)

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-30

Additive (expand-only) revision backing BUILD_PROMPTS P13.5:

  1. users.last_active_at — nullable timestamptz, written ONLY at token
     issue (OTP verify / refresh rotation), never per request, so the
     prototype "last active" column exists without per-request write
     amplification. Nullable: existing users have never been observed.

  2. idx_users_created_keyset — the users listing paginates on the P11
     (created_at DESC, id DESC) keyset (gate 1.3); shipped in the same
     revision as the query that needs it.

  3. Audit-log viewer indexes (gate 1.3, EXPLAIN-asserted in
     tests/test_p135_explain.py). GET /audit-log paginates on
     (at DESC, id DESC) and filters by entity/actor/action/date, so
     each filter shape gets a tenant-led composite index ending in the
     keyset columns:
       - idx_audit_keyset          (tenant_id, at DESC, id DESC)
       - idx_audit_actor_keyset    (tenant_id, actor_id, at DESC, id DESC)
       - idx_audit_action_keyset   (tenant_id, action, at DESC, id DESC)
       - idx_audit_entity_keyset   (tenant_id, entity, at DESC, id DESC)
     0001's idx_audit_time (tenant_id, at) is a strict prefix of
     idx_audit_keyset and is dropped as redundant write amplification
     (the 0014 idx_applications_stage precedent). idx_audit_entity
     (tenant_id, entity, entity_id) is KEPT: it serves the per-record
     history lookup (entity + entity_id equality), which the new
     entity keyset index cannot.

     Trade-off, recorded deliberately: audit_log takes one INSERT per
     mutation, so three net-new indexes add index maintenance to every
     write path. Accepted because the viewer is the governance
     counterpart of the audit-write gate (1.5) and each filter must
     stay index-backed at scale (1.3); audit_log is append-only, so
     the indexes never take UPDATE churn.

Working downgrade restores idx_audit_time and drops only the new
objects (no data is touched: expand-only).
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_UP = """
-- -------------------------------------------------------------------------
-- 1. last_active_at, tracked at token issue only (P13.5)
-- -------------------------------------------------------------------------
ALTER TABLE users ADD COLUMN last_active_at timestamptz;

-- -------------------------------------------------------------------------
-- 2. Users listing keyset index (gate 1.3)
-- -------------------------------------------------------------------------
CREATE INDEX idx_users_created_keyset
    ON users (tenant_id, created_at DESC, id DESC);

-- -------------------------------------------------------------------------
-- 3. Audit-log viewer keyset + filter-shaped indexes (gate 1.3)
-- -------------------------------------------------------------------------
CREATE INDEX idx_audit_keyset
    ON audit_log (tenant_id, at DESC, id DESC);
CREATE INDEX idx_audit_actor_keyset
    ON audit_log (tenant_id, actor_id, at DESC, id DESC);
CREATE INDEX idx_audit_action_keyset
    ON audit_log (tenant_id, action, at DESC, id DESC);
CREATE INDEX idx_audit_entity_keyset
    ON audit_log (tenant_id, entity, at DESC, id DESC);

DROP INDEX idx_audit_time;
"""

_DOWN = """
CREATE INDEX idx_audit_time ON audit_log (tenant_id, at);

DROP INDEX IF EXISTS idx_audit_entity_keyset;
DROP INDEX IF EXISTS idx_audit_action_keyset;
DROP INDEX IF EXISTS idx_audit_actor_keyset;
DROP INDEX IF EXISTS idx_audit_keyset;

DROP INDEX IF EXISTS idx_users_created_keyset;

ALTER TABLE users DROP COLUMN IF EXISTS last_active_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
