-- Local-dev seed data. Run with:
--   psql "$DATABASE_URL" -f backend/scripts/seed_dev.sql
--
-- Every tenant-scoped table has FORCE ROW LEVEL SECURITY (migration 0001),
-- so normal inserts need `app.tenant_id` already set to match ΓÇö impossible
-- for the very first insert of the tenant row itself. This only works when
-- connected as a role that bypasses RLS (a Postgres superuser, or a role
-- with BYPASSRLS). The `genesis` user created by docker-compose.yml's
-- postgres image is a superuser, so this "just works" against that setup;
-- if you're pointed at a different Postgres instance, connect as its
-- superuser (or a BYPASSRLS role) to run this once.

INSERT INTO tenants (id, name, slug, status)
VALUES ('11111111-1111-1111-1111-111111111111', 'Dev Tenant', 'dev-tenant', 'active')
ON CONFLICT (id) DO NOTHING;

-- Roles
-- UUIDs follow an aaaaΓÇª/bbbbΓÇª pattern so they are easy to cross-reference.
INSERT INTO roles (id, tenant_id, name, is_system) VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'System Admin',       true),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '11111111-1111-1111-1111-111111111111', 'Branch Manager',     false),
    ('cccccccc-cccc-cccc-cccc-cccccccccccc', '11111111-1111-1111-1111-111111111111', 'Loan Officer',       false),
    ('dddddddd-dddd-dddd-dddd-dddddddddddd', '11111111-1111-1111-1111-111111111111', 'Teller',             false),
    ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '11111111-1111-1111-1111-111111111111', 'Credit Committee',   false),
    ('ffffffff-ffff-ffff-ffff-ffffffffffff', '11111111-1111-1111-1111-111111111111', 'Accountant',         false),
    ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111', 'Auditor',            false)
ON CONFLICT (id) DO NOTHING;

-- Permissions
-- System Admin: full access on every module.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
SELECT
    '11111111-1111-1111-1111-111111111111',
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    m,
    true, true, true, true
FROM unnest(ARRAY[
    'members', 'applications', 'loan_book', 'transactions',
    'reports', 'settings', 'access_control'
]) AS m
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Branch Manager: view + create + edit on operations; no settings/access_control.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
SELECT
    '11111111-1111-1111-1111-111111111111',
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    m, true, true, true, false
FROM unnest(ARRAY['members', 'applications', 'loan_book', 'transactions', 'reports']) AS m
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Loan Officer: view + create applications; view members/loan_book.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'members',      true,  false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'applications', true,  true,  true,  false),
    ('11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'loan_book',    true,  false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'reports',      true,  false, false, false)
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Teller: view + create transactions and members only.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'members',      true, false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'transactions', true, true,  false, false)
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Credit Committee: view + approve applications and loan_book.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'members',      true,  false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'applications', true,  false, false, true),
    ('11111111-1111-1111-1111-111111111111', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'loan_book',    true,  false, false, true),
    ('11111111-1111-1111-1111-111111111111', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'reports',      true,  false, false, false)
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Accountant: full access to transactions/reports; view-only elsewhere.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'members',      true,  false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'loan_book',    true,  false, false, false),
    ('11111111-1111-1111-1111-111111111111', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'transactions', true,  true,  true,  false),
    ('11111111-1111-1111-1111-111111111111', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'reports',      true,  true,  true,  false)
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Auditor: view-only across all modules; no write permissions.
INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
SELECT
    '11111111-1111-1111-1111-111111111111',
    '22222222-2222-2222-2222-222222222222',
    m,
    true, false, false, false
FROM unnest(ARRAY[
    'members', 'applications', 'loan_book', 'transactions', 'reports'
]) AS m
ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

-- Users
INSERT INTO users (id, tenant_id, role_id, full_name, email, branch, status) VALUES
    ('33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Dev Admin',      'demo@genesisprestige.test', 'HQ',          'active'),
    ('44444444-4444-4444-4444-444444444444', '11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'Antony Maina',   'antony.maina@dev.test',    'Nairobi CBD', 'active'),
    ('55555555-5555-5555-5555-555555555555', '11111111-1111-1111-1111-111111111111', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Francis Karingi','francis.karingi@dev.test', 'Nairobi CBD', 'active'),
    ('66666666-6666-6666-6666-666666666666', '11111111-1111-1111-1111-111111111111', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'Jane Muthoni',   'jane.muthoni@dev.test',    'Thika',       'active'),
    ('77777777-7777-7777-7777-777777777777', '11111111-1111-1111-1111-111111111111', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'Board Member A', 'board.member.a@dev.test',  'HQ',          'active'),
    ('88888888-8888-8888-8888-888888888888', '11111111-1111-1111-1111-111111111111', 'ffffffff-ffff-ffff-ffff-ffffffffffff', 'Ruth Achieng',   'ruth.achieng@dev.test',    'HQ',          'active'),
    ('99999999-9999-9999-9999-999999999999', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'Sam Kiptoo',     'sam.kiptoo@dev.test',      'HQ',          'suspended')
ON CONFLICT (tenant_id, email) DO NOTHING;
