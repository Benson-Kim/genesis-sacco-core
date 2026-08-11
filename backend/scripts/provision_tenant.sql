-- =============================================================================
-- Genesis Prestige — ONE-TIME production tenant bootstrap
-- =============================================================================
-- Unlike scripts/seed_dev.sql (which TRUNCATEs and reseeds fixture data on
-- every run), this script is meant to run EXACTLY ONCE against a freshly
-- migrated, empty production database. It creates real placeholder values
-- for you to edit below — it does not invent SACCO-specific business
-- figures (interest rates, fees, committee size) on your behalf.
--
-- RLS note (see docs/technical/operations.md "runtime DB role must be
-- non-superuser without BYPASSRLS"): every tenant-scoped table has FORCE
-- ROW LEVEL SECURITY, so even the table OWNER cannot INSERT the very first
-- tenant row without either BYPASSRLS (which the app's runtime role must
-- NOT have, by design) or temporarily lifting FORCE on just this one
-- table. This script does that lift/restore itself, scoped to one
-- transaction, so the app role never needs an elevated privilege at
-- runtime.
--
-- Run once, via SSH, from the Python app's venv:
--   psql "$DATABASE_URL" -f scripts/provision_tenant.sql
-- =============================================================================

BEGIN;

ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY;

-- EDIT ME: real SACCO name/slug. Slug must be unique and matches the
-- `slug` UNIQUE constraint from migration 0001.
INSERT INTO tenants (id, name, slug, status)
VALUES (gen_random_uuid(), 'REPLACE ME SACCO', 'replace-me-sacco', 'active')
RETURNING id AS tenant_id \gset

ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

-- Everything below this line runs as the tenant (RLS-scoped) — app.tenant_id
-- is the exact GUC the API sets per-request (genesis.infrastructure.tenancy
-- .tenant_session) and every RLS policy checks (migration 0001's
-- tenant_isolation policy); needed because these INSERTs happen under
-- FORCE RLS again.
SELECT set_config('app.tenant_id', :'tenant_id', true);

-- EDIT ME: at least one branch (branches.tenant_id, name — see migration 0001).
INSERT INTO branches (id, tenant_id, name)
VALUES (gen_random_uuid(), :'tenant_id'::uuid, 'HQ');

-- EDIT ME: real tenant_settings — these are YOUR SACCO's actual bylaws
-- figures (interest rates, fees, dormancy period, exit notice, committee
-- size/quorum), not invented ones. See migration 0001 for the full column
-- list and backend/scripts/seed_dev.sql section 3 for a worked (fixture)
-- example of every field this table expects.
-- INSERT INTO tenant_settings (tenant_id, ...) VALUES (:'tenant_id'::uuid, ...);

-- System Admin role + full permissions (mirrors seed_dev.sql section 4/5,
-- System Admin branch only — this is the minimum needed to sign in and
-- configure the rest of the roles/users from the Access Control screen
-- instead of more SQL).
INSERT INTO roles (id, tenant_id, name, is_system)
VALUES (gen_random_uuid(), :'tenant_id'::uuid, 'System Admin', true)
RETURNING id AS admin_role_id \gset

DO $perms$
DECLARE
    all_mods text[] := ARRAY[
        'members', 'applications', 'loan_book', 'transactions',
        'reports', 'settings', 'access_control', 'corrections',
        'member_identity', 'application_overrides'
    ];
    mod text;
BEGIN
    FOREACH mod IN ARRAY all_mods LOOP
        INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
        VALUES (:'tenant_id'::uuid, :'admin_role_id'::uuid, mod, true, true, true, true);
    END LOOP;
END
$perms$;

-- EDIT ME: your own real email/phone (this is the account you'll sign in
-- with first). No password to set — sign-in is OTP-only (see the
-- OTP-delivery blocker in docs/technical/mochahost-deployment.md before
-- relying on this in production).
INSERT INTO users (id, tenant_id, email, full_name, role_id, status, branch_id)
SELECT gen_random_uuid(), :'tenant_id'::uuid, 'REPLACE-ME@example.com', 'REPLACE ME',
       :'admin_role_id'::uuid, 'active', b.id
FROM branches b WHERE b.tenant_id = :'tenant_id'::uuid AND b.name = 'HQ';

COMMIT;

-- Print the tenant id you need for web/.env.production's
-- NEXT_PUBLIC_TENANT_ID (the build-time value — see that file).
SELECT :'tenant_id' AS tenant_id;
