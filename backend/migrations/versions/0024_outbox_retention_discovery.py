"""DSA-6 outbox hardening: purge index + due/purgeable tenant registries

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-01

Claimed as 0024 with down_revision 0023 per v1.2 rule 14. At branch
time main's migration head was 0022 (0001-0022 linear) and **0023 was
the open claim of !40 (P13.10)** — this MR therefore merges AFTER !40
and its pipeline can only be green on the combined state (sequencing
declared in the MR description; re-verified per rule 12 before ready).
NO new tables: no TENANT_TABLES / ENTITY_MODULES / RLS delta. Three
expand-only objects backing P13.17(e):

  * idx_outbox_dispatched_purge — partial index over
    (dispatched_at) WHERE status = 'dispatched', the driving index for
    the retention-purge DELETE and the purgeable-tenant registry
    (gate 1.3: shipped in the same MR as the queries it serves,
    infrastructure/outbox_worker.py:purge_dispatched /
    list_purgeable_tenants). idx_outbox_pending (0001) cannot serve
    this predicate — it is partial over status = 'pending'.

  * outbox_due_tenant_ids() — SECURITY DEFINER registry (the exact
    0003 active_tenant_ids() pattern: the dispatcher enumerates
    cross-tenant work without weakening RLS on any table) returning
    the active tenants that have due pending events, so the worker
    wakes only tenants with work instead of sweeping every active
    tenant each interval. The due set IS the pending set:
    idx_outbox_pending (0001) serves it.

  * outbox_purgeable_tenant_ids(cutoff timestamptz) — same pattern for
    the retention purge. The cutoff argument is computed by the worker
    from the module-owned DISPATCHED_RETENTION_DAYS constant (v1.1
    rule 1: config-owned, never caller-supplied); it is a parameter
    only so the retention policy lives in exactly one place.

Both functions join tenants ON status = 'active' — parity with 0003:
suspended tenants are neither dispatched nor purged.

SECURITY (review finding R6): every SECURITY DEFINER function here is
created with `SET search_path = public, pg_temp`. A definer-rights
function with a mutable search_path is a PostgreSQL privilege-
escalation vector — a caller-controlled search_path could shadow
outbox_events/tenants with hostile relations resolved and executed as
the BYPASSRLS migration role. The same audit found the precedent
function active_tenant_ids() (0003) shipped WITHOUT a pinned
search_path, so this migration also hardens it in place via CREATE OR
REPLACE with the identical body plus the pinned search_path. The
downgrade restores active_tenant_ids() to its original 0003 definition
(no SET clause) so 0024's downgrade leaves the database exactly at the
0023 state.

SECURITY (review finding R7): PostgreSQL grants EXECUTE on every
function to PUBLIC by default, so all three SECURITY DEFINER
registries — active_tenant_ids() (0003), outbox_due_tenant_ids() and
outbox_purgeable_tenant_ids(timestamptz) (0024) — were callable by ANY
role: a compromised tenant-scoped session could enumerate every active
tenant id AND observe which tenants have due/purgeable outbox activity
(a cross-tenant activity oracle). This migration REVOKEs EXECUTE FROM
PUBLIC on all three and GRANTs EXECUTE only to `genesis_app`, the
non-superuser NOBYPASSRLS application role of ADR-0002 — the role the
outbox worker's normal session runs as (env.py/DATABASE_URL; the CI
backend:test job runs the whole suite, worker included, as
genesis_app). The GRANT is conditional on the role existing because
app-role provisioning is environment-owned, not migration-owned: in
CI, backend:test creates genesis_app only AFTER `alembic upgrade
head` (its bootstrap grants EXECUTE on these three functions
alongside its table/sequence grants), and backend:migrate-check has
no app role at all. The privileged migration role keeps EXECUTE
implicitly as the function owner. Downgrade restores the pre-0024
grant state: PUBLIC is re-granted on active_tenant_ids() only and the
explicit app-role grant dropped (the 0003 default-ACL state — the
explicit PUBLIC grant is semantically identical to the NULL-proacl
default it replaces); the two 0024 functions are dropped outright,
which removes their ACLs with them.

Lock posture (honest per the !40 R5 disposition): CREATE INDEX
(non-CONCURRENT) takes a SHARE lock on outbox_events, blocking event
INSERTs (i.e. every mutating request) for the duration of the build.
That matches this project's accepted maintenance-window migration
runner model; it is NOT a zero-downtime migration. CREATE FUNCTION
takes no table locks. If a zero-downtime posture is adopted later, the
index moves to CREATE INDEX CONCURRENTLY outside the migration
transaction.

Downgrade drops the two new functions and the index, and restores
active_tenant_ids() to its 0003 definition — no data is touched,
nothing money-bearing is lost, so no conditional refusal is needed
(the purge itself only ever deletes dispatched delivery receipts;
pending and dead rows are exempt by status in the worker SQL).
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_UP = """
-- 1. Driving index for the retention purge (gate 1.3: shipped with the
--    DELETE and the purgeable-tenant registry it serves). Partial over
--    the purge predicate; idx_outbox_pending (0001) is partial over
--    status = 'pending' and cannot serve status = 'dispatched'.
CREATE INDEX idx_outbox_dispatched_purge
    ON outbox_events (dispatched_at)
    WHERE status = 'dispatched';

-- 2. Due-tenant discovery: SECURITY DEFINER so the dispatcher can find
--    tenants with due work without weakening RLS on any other table
--    (the 0003 active_tenant_ids() pattern). The defining role is the
--    migration role (BYPASSRLS/superuser in CI and ops environments).
--    R6: search_path pinned — a definer-rights function with a mutable
--    search_path lets a caller shadow outbox_events/tenants with
--    hostile relations executed as the BYPASSRLS definer role.
CREATE FUNCTION outbox_due_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
    SELECT DISTINCT o.tenant_id
    FROM outbox_events o
    JOIN tenants t ON t.id = o.tenant_id AND t.status = 'active'
    WHERE o.status = 'pending' AND o.next_attempt_at <= now()
$fn$;

-- 3. Purgeable-tenant discovery for the retention purge; the worker
--    passes now() - DISPATCHED_RETENTION_DAYS (module-owned constant).
CREATE FUNCTION outbox_purgeable_tenant_ids(cutoff timestamptz) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
    SELECT DISTINCT o.tenant_id
    FROM outbox_events o
    JOIN tenants t ON t.id = o.tenant_id AND t.status = 'active'
    WHERE o.status = 'dispatched' AND o.dispatched_at < cutoff
$fn$;

-- 4. R6 audit fix for the 0003 precedent: active_tenant_ids() shipped
--    SECURITY DEFINER without a pinned search_path — the same schema-
--    shadowing escalation vector. Re-created in place with the
--    identical body plus the pinned search_path (no signature change,
--    dependents unaffected).
CREATE OR REPLACE FUNCTION active_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$ SELECT id FROM tenants WHERE status = 'active' $fn$;

-- 5. R7 audit fix: PostgreSQL grants EXECUTE to PUBLIC by default, so
--    every role — including a compromised tenant-scoped app session —
--    could call the three definer registries and use them as a
--    cross-tenant activity oracle (which tenants exist, which have
--    due/purgeable outbox work). Lock EXECUTE down to genesis_app,
--    the ADR-0002 application role the outbox worker's normal session
--    runs as. Conditional: app-role provisioning is environment-owned
--    (see the module docstring — in CI the role is created after this
--    migration runs, and the migrate-check database has no app role);
--    the privileged migration role keeps EXECUTE implicitly as owner.
REVOKE EXECUTE ON FUNCTION active_tenant_ids() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION outbox_due_tenant_ids() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION outbox_purgeable_tenant_ids(timestamptz) FROM PUBLIC;
DO $r7$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'genesis_app') THEN
        GRANT EXECUTE ON FUNCTION active_tenant_ids() TO genesis_app;
        GRANT EXECUTE ON FUNCTION outbox_due_tenant_ids() TO genesis_app;
        GRANT EXECUTE ON FUNCTION outbox_purgeable_tenant_ids(timestamptz) TO genesis_app;
    END IF;
END
$r7$;
"""

_DOWN = """
-- Restore active_tenant_ids() to its original 0003 definition (no SET
-- search_path clause) so downgrading 0024 lands exactly on the 0023
-- state.
CREATE OR REPLACE FUNCTION active_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
AS $fn$ SELECT id FROM tenants WHERE status = 'active' $fn$;

-- R7 downgrade: restore the pre-0024 grant state. 0003 shipped
-- active_tenant_ids() with the PostgreSQL DEFAULT function ACL, which
-- includes EXECUTE to PUBLIC — so PUBLIC is re-granted here
-- explicitly and the explicit app-role grant (redundant under PUBLIC)
-- is dropped. (Semantically identical to the 0003 state: pg_proc's
-- NULL proacl *means* owner + PUBLIC EXECUTE; the explicit ACL simply
-- materialises it.) The two 0024 registries need no grant restore:
-- the DROPs below remove them together with their ACLs.
GRANT EXECUTE ON FUNCTION active_tenant_ids() TO PUBLIC;
DO $r7$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'genesis_app') THEN
        REVOKE EXECUTE ON FUNCTION active_tenant_ids() FROM genesis_app;
    END IF;
END
$r7$;

DROP FUNCTION IF EXISTS outbox_purgeable_tenant_ids(timestamptz);
DROP FUNCTION IF EXISTS outbox_due_tenant_ids();
DROP INDEX IF EXISTS idx_outbox_dispatched_purge;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
