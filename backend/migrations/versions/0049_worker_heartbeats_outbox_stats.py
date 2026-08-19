"""worker heartbeats table + outbox queue-stats aggregate (issue #4)

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-19

Claimed as 0049 with down_revision 0048 per the migration-declaration
rule — 0048 (loan/exit human refs) verified as this branch's migration
head at claim time. Per issue #22: if a sibling MR moves the head
first, this revision RE-CHAINS (down_revision edit only) — never a
merge revision.

Two objects, both observability substrate (issue #4):

1. ``worker_heartbeats`` — one row per cron one-shot worker, written
   by the four ``backend/scripts/cron_*.py`` entrypoints at cycle end:

   * ``last_success_at`` — the dead-man-switch signal: a worker that
     silently stops firing shows up as a stale timestamp, without
     grepping any log;
   * ``consecutive_lock_skips`` — the COUNTABLE form of the
     cron_lock.py skip log line. One skip is benign backpressure;
     consecutive skips mean a wedged or pathologically slow cycle
     (the !2 review's paging signal). Reset to 0 on every successful
     cycle.

   DELIBERATELY GLOBAL (no tenant_id, no RLS): the row describes the
   worker PROCESS — which itself walks tenants — not tenant data,
   exactly the cron_lock advisory-lock posture. Contents are a
   code-owned worker name, timestamps and a counter: no member data,
   no money, no PII can exist in this table by construction.

2. ``outbox_queue_stats()`` — SECURITY DEFINER aggregate (the 0024
   discovery-function pattern: pinned search_path, EXECUTE revoked
   from PUBLIC, conditional grant to genesis_app) returning exactly
   three numbers: pending count, dead-letter count, oldest-pending
   age. Needed because outbox_events is RLS-forced and the ops
   endpoint's session carries no tenant GUC; the aggregate discloses
   queue TOTALS only — never rows, payloads or tenant ids (least
   disclosure; strictly less than the 0024 registries, which disclose
   tenant ids).

Index audit (rule 14): no new indexes — worker_heartbeats is a
four-row table keyed by its primary key; outbox_queue_stats() is an
ops-cadence scrape (not a request hot path) served by a full scan of
outbox_events plus idx_outbox_pending where applicable.

Downgrade drops both objects exactly; heartbeat rows are derived
operational state (rewritten within one cron cadence), so no
conditional refusal is needed.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE worker_heartbeats (
    worker text PRIMARY KEY,
    last_success_at timestamptz,
    consecutive_lock_skips integer NOT NULL DEFAULT 0
        CHECK (consecutive_lock_skips >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Queue totals for the ops scrape: SECURITY DEFINER because
-- outbox_events is RLS-forced and the scrape session carries no
-- tenant GUC (the 0024 pattern; search_path pinned per its R6 note).
CREATE FUNCTION outbox_queue_stats()
RETURNS TABLE (
    pending_count bigint,
    dead_count bigint,
    oldest_pending_age_seconds double precision
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
    SELECT count(*) FILTER (WHERE status = 'pending'),
           count(*) FILTER (WHERE status = 'dead'),
           COALESCE(EXTRACT(EPOCH FROM
               (now() - min(created_at) FILTER (WHERE status = 'pending'))
           )::double precision, 0)
    FROM outbox_events
$fn$;

-- 0024 R7 discipline: default PUBLIC EXECUTE off, explicit app-role
-- grant on (conditional: app-role provisioning is environment-owned;
-- in CI the role is created after migrations run and gets its grant
-- from the pipeline bootstrap).
REVOKE EXECUTE ON FUNCTION outbox_queue_stats() FROM PUBLIC;
DO $grant$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'genesis_app') THEN
        GRANT EXECUTE ON FUNCTION outbox_queue_stats() TO genesis_app;
    END IF;
END
$grant$;
"""

_DOWN = """
DROP FUNCTION IF EXISTS outbox_queue_stats();
DROP TABLE worker_heartbeats;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
